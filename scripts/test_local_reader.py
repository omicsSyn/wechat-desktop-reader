"""Synthetic regressions for strict image identity and local image decoding."""
import hashlib
import io
import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from local_reader import attachment_name, decode_image, decrypt_image


def field(number, value):
    if len(value) >= 128:
        raise ValueError('Small synthetic fixture required')
    return bytes([(number << 3) | 2, len(value)]) + value


class LocalImageTests(unittest.TestCase):
    def test_only_structural_filename_is_selected(self):
        wanted = b'a' * 32
        decoy = b'b' * 32
        packed = field(3, field(4, wanted) + field(5, decoy))
        self.assertEqual(attachment_name(packed), wanted.decode())

    def test_arbitrary_xml_hash_is_not_a_filename(self):
        with self.assertRaises(ValueError):
            attachment_name(b'<img md5="' + b'a' * 32 + b'" other_hash="' + b'b' * 32 + b'"/>')

    def test_duplicate_fields_are_rejected(self):
        child = field(4, b'a' * 32)
        for packed in (field(3, child) * 2, field(3, child * 2)):
            with self.subTest(packed=packed), self.assertRaises(ValueError):
                attachment_name(packed)

    def test_truncated_unknown_and_path_fields_are_rejected(self):
        for packed in (b'', b'\x1a\xff', field(2, field(4, b'a' * 32)),
                       field(3, field(4, b'../../unrelated'))):
            with self.subTest(packed=packed), self.assertRaises(ValueError):
                attachment_name(packed)

    def test_empty_image_reports_a_regular_failure(self):
        with self.assertRaises(ValueError):
            decrypt_image(b'', 123, 'synthetic_account')

    def test_v2_round_trip_and_full_decode(self):
        buffer = io.BytesIO()
        Image.new('RGB', (3, 2), (10, 20, 30)).save(buffer, format='PNG')
        image = buffer.getvalue()
        first_size = 32
        tail_size = 8
        config = 123
        account = 'synthetic_account'
        key = hashlib.md5((str(config) + account).encode()).hexdigest()[:16].encode()
        first = image[:first_size] + b'\x10' * 16
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        encrypted = enc.update(first) + enc.finalize()
        packed = (b'\x07\x08\x56\x32\x08\x07'
                  + struct.pack('<II', first_size, tail_size) + b'\x00'
                  + encrypted + image[first_size:-tail_size]
                  + bytes(value ^ (config & 255) for value in image[-tail_size:]))
        plain = decrypt_image(packed, config, account)
        self.assertEqual(plain, image)
        _, extension, dimensions, converted = decode_image(plain)
        self.assertEqual((extension, dimensions, converted), ('png', [3, 2], False))
        with self.assertRaises(ValueError):
            decrypt_image(packed, config + 1, account)


if __name__ == '__main__':
    unittest.main()
