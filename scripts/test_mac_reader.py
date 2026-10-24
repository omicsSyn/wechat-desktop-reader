"""macOS adapter tests with synthetic accounts only; no real WeChat is used."""
import contextlib
import ctypes
import io
import json
import stat
import sys
import unittest
from unittest.mock import patch

import local_reader as lr
import mac_reader as mac
from test_page_reader import KEY
from test_local_reader import field
import test_pipeline


class MacReaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_pipeline.LocalPipelineTests('test_full_main_two_shards_and_fallback')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.key_file = self.f.root / 'keys.json'
        self.secret = dict(version=1, account='synthetic_account',
                           account_dir=str(self.f.account.resolve()), keys=self.f.keys, image_config=123)
        self.save_keys()
        self.args = ['read', '--account-dir', str(self.f.account), '--account', 'synthetic_account',
                     '--contact', self.f.contact, '--key-file', str(self.key_file),
                     '--timezone', 'Asia/Shanghai', '--limit', '2', '--images', '1',
                     '--output', str(self.f.output)]

    def save_keys(self):
        self.key_file.write_text(json.dumps(self.secret), encoding='utf-8')
        self.key_file.chmod(0o600)

    def invoke(self, args=None, native=True):
        stdout = io.StringIO()
        platform = 'darwin' if native else 'linux'
        with patch.object(sys, 'platform', platform), contextlib.redirect_stdout(stdout), \
                patch.object(lr, 'winpath', side_effect=AssertionError('Windows path helper called')), \
                patch.object(lr, 'start_observer', side_effect=AssertionError('Windows observer called')), \
                patch.object(lr.subprocess, 'run', side_effect=AssertionError('Unexpected subprocess')):
            code = mac.main(args or self.args)
        text = stdout.getvalue()
        self.assertNotIn(KEY.hex(), text)
        self.assertNotIn('hello', text)
        return code, json.loads(text)

    def result(self):
        return json.loads((self.f.output / 'result.json').read_text())

    def test_native_pipeline_private_outputs_and_no_window_claim(self):
        original_key_file = self.key_file.read_bytes()
        code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        result = self.result()
        self.assertEqual([m['server_id'] for m in result['messages']], [101, 103])
        self.assertEqual([m['sender'] for m in result['messages']], ['contact', 'self'])
        self.assertIsNone(result['background_verified'])
        self.assertIsNone(result['minimized_verified'])
        self.assertFalse(result['background_observation']['supported'])
        self.assertFalse(result['ui_automation_used'])
        self.assertTrue(result['experimental'])
        self.assertEqual(result['account'], 'synthetic_account')
        self.assertEqual(result['account_dir'], str(self.f.account.resolve()))
        self.assertNotIn('synthetic_account', json.dumps(summary))
        self.assertNotIn(str(self.f.account.resolve()), json.dumps(summary))
        self.assertEqual(result['images'][0]['size'], [5, 3])
        self.assertEqual(result['images'][0]['path'], str((self.f.output / 'image_01.png').resolve()))
        self.assertEqual(stat.S_IMODE(self.f.output.stat().st_mode), 0o700)
        for path in self.f.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(KEY.hex(), (self.f.output / 'result.json').read_text())
        self.assertEqual(self.key_file.read_bytes(), original_key_file)
        self.assertEqual(self.f.hashes(), self.f.before)

    def test_plaintext_image_without_image_config(self):
        del self.secret['image_config']
        self.save_keys()
        (self.f.attach / (self.f.digest + '.dat')).write_bytes(self.f.png)
        code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual((self.f.output / 'image_01.png').read_bytes(), self.f.png)

    def test_missing_v2_image_key_keeps_messages_and_explicit_gap(self):
        del self.secret['image_config']
        self.save_keys()
        (self.f.attach / (self.f.digest + '_h.dat')).unlink()
        (self.f.attach / (self.f.digest + '.dat')).write_bytes(b'\x07\x08V2\x08\x07' + b'\x00' * 40)
        code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.result()['images'][0]['status'], 'missing_image_key')
        self.assertEqual(len(self.result()['messages']), 2)
        self.assertEqual({p.name for p in self.f.output.iterdir()}, {'result.json'})

    def test_images_zero_does_not_read_attachments(self):
        self.args[self.args.index('--images') + 1] = '0'
        with patch.object(lr, 'decrypt_image', side_effect=AssertionError('Image should not be read')):
            code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.result()['images'], [])
        self.assertEqual(self.result()['account'], 'synthetic_account')
        self.assertEqual(self.result()['account_dir'], str(self.f.account.resolve()))

    def test_non_mac_platform_fails_before_key_read(self):
        with patch.object(mac, 'load_keys') as keys:
            code, summary = self.invoke(native=False)
        self.assertEqual(code, 2)
        keys.assert_not_called()
        self.assertFalse(self.f.output.exists())

    def test_wrong_and_missing_database_keys_fail_without_output(self):
        for wrong in (True, False):
            with self.subTest(wrong=wrong):
                self.secret['keys'] = dict(self.f.keys)
                if wrong:
                    self.secret['keys']['message/message_1.db'] = '00' * 32
                else:
                    del self.secret['keys']['message/message_1.db']
                self.save_keys()
                code, summary = self.invoke()
                self.assertEqual(code, 2, summary)
                self.assertFalse(self.f.output.exists())
        self.assertEqual(self.f.hashes(), self.f.before)

    def test_key_scope_schema_and_private_mode_checks(self):
        base = dict(self.secret)
        variants = [dict(base, account='different'), dict(base, account_dir=str(self.f.root)),
                    dict(base, version=True), dict(base, image_config=True),
                    dict(base, image_config=-1), dict(base, image_config=2 ** 64),
                    dict(base, unexpected='not_allowed'), dict(base, keys={'../outside.db': KEY.hex()})]
        for secret in variants:
            with self.subTest(secret_fields=tuple(secret)):
                self.secret = secret
                self.save_keys()
                self.assertEqual(self.invoke()[0], 2)
                self.assertFalse(self.f.output.exists())
        self.secret = base
        self.save_keys()
        self.key_file.chmod(0o644)
        code, summary = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn('0600', summary['message'])

    def test_key_file_duplicate_field_symlink_and_size_rejected(self):
        self.key_file.write_text('{"version":1,"version":1}')
        self.assertEqual(self.invoke()[0], 2)
        self.save_keys()
        link = self.f.root / 'key-link.json'
        link.symlink_to(self.key_file)
        index = self.args.index('--key-file') + 1
        self.args[index] = str(link)
        self.assertEqual(self.invoke()[0], 2)
        self.args[index] = str(self.key_file)
        self.key_file.write_bytes(b' ' * (mac.MAX_KEY_FILE_BYTES + 1))
        self.assertEqual(self.invoke()[0], 2)

    def test_source_or_repository_output_and_existing_output_rejected(self):
        targets = [self.f.account / 'out', mac.REPOSITORY / 'UNUSED_TEST_OUTPUT', self.f.root]
        for target in targets:
            self.args[-1] = str(target)
            with self.subTest(target=target), patch.object(mac, 'load_keys') as keys:
                self.assertEqual(self.invoke()[0], 2)
                keys.assert_not_called()

    def test_contact_group_and_ambiguous_match_rejected(self):
        cases = [([], 'contact_not_found'), (['synthetic@chatroom'], 'individual_contact_required'),
                 (['one', 'two'], 'contact_ambiguous')]
        for peers, expected in cases:
            self.f.make_db('contact/contact.db', ['CREATE TABLE contact(username,nick_name,remark)'] +
                           [('INSERT INTO contact VALUES(?,?,?)', (peer, self.f.contact, '')) for peer in peers])
            code, summary = self.invoke()
            self.assertEqual(code, 2)
            self.assertEqual(summary['code'], expected)
            self.assertNotIn(self.f.contact, json.dumps(summary))
            self.assertFalse(self.f.output.exists())

    def test_database_symlink_outside_account_rejected(self):
        path = self.f.account / 'db_storage/message/message_1.db'
        target = self.f.root / 'external.db'
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(self.invoke()[0], 2)
        self.assertFalse(self.f.output.exists())

    def test_attachment_symlink_outside_account_not_exported(self):
        path = self.f.attach / (self.f.digest + '.dat')
        target = self.f.root / 'external.dat'
        path.rename(target)
        path.symlink_to(target)
        code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.result()['images'][0]['status'], 'decode_failed')
        self.assertFalse((self.f.output / 'image_01.png').exists())

    def test_source_change_is_retried_once(self):
        original = lr.read_target
        calls = []

        def changing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise lr.SourceChangedError('synthetic')
            return original(*args, **kwargs)

        with patch.object(lr, 'read_target', side_effect=changing):
            code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual(len(calls), 2)

    def test_status_reads_no_key_file_or_chat(self):
        with patch.object(mac, 'load_keys', side_effect=AssertionError('Key read')), \
                patch.object(lr, 'read_target', side_effect=AssertionError('Chat read')):
            code, report = self.invoke(['status', '--account-dir', str(self.f.account)])
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report['databases']), 3)
        self.assertFalse(report['key_extraction_supported'])
        self.assertFalse(report['live_wechat_tested'])

    def test_invalid_timezone_and_query_bounds_rejected(self):
        for flag, value in (('--timezone', 'Not/AZone'), ('--limit', '101'), ('--images', '-1')):
            args = list(self.args)
            args[args.index(flag) + 1] = value
            self.assertEqual(self.invoke(args)[0], 2)
            self.assertFalse(self.f.output.exists())

    def test_timezone_controls_calendar_month_not_host_timezone(self):
        # 2024-02-01 00:30 UTC is still January in Los Angeles.
        stamp = 1706747400
        self.f.make_db('message/message_0.db', [
            f'CREATE TABLE "{self.f.table}"(local_id,server_id,local_type,real_sender_id,create_time,sort_seq,message_content,packed_info_data)',
            'CREATE TABLE Name2Id(user_name)', ('INSERT INTO Name2Id VALUES(?)', (self.f.username,)),
            (f'INSERT INTO "{self.f.table}" VALUES(?,?,?,?,?,?,?,?)',
             (1, 999, 3, 1, stamp, 1, b'', field(3, field(4, self.f.digest.encode()))))])
        folder = self.f.attach.parent.parent / '2024-01' / 'Img'
        folder.mkdir(parents=True)
        (folder / (self.f.digest + '.dat')).write_bytes(self.f.png)
        self.args[self.args.index('--timezone') + 1] = 'America/Los_Angeles'
        code, summary = self.invoke()
        self.assertEqual(code, 0, summary)
        image = self.result()['images'][0]
        self.assertEqual(image['status'], 'decoded')
        self.assertIn('/2024-01/Img/', image['source'])
        self.assertTrue(image['datetime'].startswith('2024-01-31T'))


class ZstdPortabilityTests(unittest.TestCase):
    def test_compressed_text_round_trip_with_system_library(self):
        lib = lr.zstd_library()
        if lib is None:
            self.fail('Install libzstd for this cross-platform integration test')
        raw = 'Synthetic compressed text / 合成文本'.encode()
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = ctypes.c_size_t
        buf = ctypes.create_string_buffer(1024)
        count = lib.ZSTD_compress(buf, len(buf), raw, len(raw), 3)
        self.assertLess(count, len(buf))
        self.assertEqual(lr.unpack_text(buf.raw[:count]), raw.decode())

    def test_missing_zstd_is_unreadable_not_empty_text(self):
        with patch.object(lr, 'zstd_library', return_value=None):
            self.assertIsNone(lr.unpack_text(b'(\xb5/\xfdunsupported'))


if __name__ == '__main__':
    unittest.main()
