"""Synthetic-only regression tests; no WeChat files, keys, or processes used."""

import ctypes
import contextlib
import hashlib
import hmac
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import apsw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import page_reader as reader


KEY = bytes(range(32))
SALT = bytes(range(100, 116))
PAGE = 4096


def make_plain(rows=3):
    connection = apsw.Connection(":memory:")
    reserve = ctypes.c_int(80)
    assert connection.filecontrol("main", apsw.SQLITE_FCNTL_RESERVE_BYTES,
                                  ctypes.addressof(reserve))
    connection.execute("PRAGMA page_size=4096")
    connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO sample VALUES(?, ?)",
                           ((i, f"synthetic-{i:05d}-" + "x" * 100)
                            for i in range(1, rows + 1)))
    assert connection.serialize("main")[20] == 80
    return connection


def encrypt(plain):
    output = bytearray()
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(x ^ 0x3A for x in SALT), 2, 32)
    for offset in range(0, len(plain), PAGE):
        number = offset // PAGE + 1
        page = plain[offset:offset + PAGE]
        start = 16 if number == 1 else 0
        iv = hashlib.sha256(page + struct.pack("<I", number)).digest()[:16]
        encryptor = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(page[start:4016]) + encryptor.finalize()
        payload = ciphertext + iv
        digest = hmac.new(mac_key, payload + struct.pack("<I", number), hashlib.sha512).digest()
        output.extend((SALT if number == 1 else b"") + payload + digest)
    return bytes(output)


def wal_checksum(data, state, big_endian=False):
    values = struct.unpack((">" if big_endian else "<") + "I" * (len(data) // 4), data)
    a, b = state
    for index in range(0, len(values), 2):
        a = (a + b + values[index]) % (2 ** 32)
        b = (b + a + values[index + 1]) % (2 ** 32)
    return a, b


def make_wal(frames, big_endian=False):
    header = struct.pack(">6I", 0x377F0683 if big_endian else 0x377F0682,
                         3007000, PAGE, 9, 0x12345678, 0xABCDEF01)
    checksum = wal_checksum(header, (0, 0), big_endian)
    output = bytearray(header + struct.pack(">II", *checksum))
    for page_number, db_size, encrypted_page in frames:
        prefix = struct.pack(">II", page_number, db_size)
        checksum = wal_checksum(prefix + encrypted_page, checksum, big_endian)
        output.extend(prefix + header[16:24] + struct.pack(">II", *checksum) + encrypted_page)
    return bytes(output)


def transaction(encrypted, commit=True):
    count = len(encrypted) // PAGE
    return [(i + 1, count if commit and i == count - 1 else 0,
             encrypted[i * PAGE:(i + 1) * PAGE]) for i in range(count)]


def replace_same_size_preserving_mtime(path, content):
    before = path.stat()
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@contextlib.contextmanager
def unpublished_change_times(source):
    """Emulate an open Windows writer whose change time is not yet published.

    Tests restore mtime on disk. POSIX does not permit restoring ctime, so only
    that field is normalized; file identity, size, and mtime are still checked.
    """
    original = reader._signature
    signatures = [source._db_signature, source._wal_signature]

    def signature(value):
        observed = original(value)
        for expected in signatures:
            if expected is not None and observed[:2] == expected[:2]:
                return observed[:4] + (expected[4],)
        return observed

    with mock.patch.object(reader, "_signature", signature):
        yield


class PageReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "synthetic.db"
        self.wal = Path(str(self.path) + "-wal")
        self.plain = make_plain()
        self.addCleanup(self.plain.close)
        self.base = encrypt(self.plain.serialize("main"))
        self.path.write_bytes(self.base)

    def changed(self, value="committed"):
        self.plain.execute("UPDATE sample SET value=? WHERE id=1", (value,))
        return encrypt(self.plain.serialize("main"))

    def value(self, database):
        return database.execute("SELECT value FROM sample WHERE id=1").fetchone()[0]

    def test_reads_valid_database_and_cleans_vfs(self):
        original_vfs = set(apsw.vfsnames())
        before = self.path.stat()
        with reader.open_db(self.path, KEY) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM sample").fetchall(), [(3,)])
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(database.execute("PRAGMA table_info(sample)").fetchall()[0][1], "id")
            self.assertEqual(database.execute("PRAGMA query_only").fetchone(), (1,))
            self.assertEqual(database._source.page(1)[18:20], b"\x01\x01")
            self.assertGreater(database.stats["pages_decrypted"], 0)
        self.assertEqual(set(apsw.vfsnames()), original_vfs)
        self.assertEqual(self.path.read_bytes(), self.base)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_decrypts_only_requested_pages(self):
        larger = make_plain(1200)
        try:
            encrypted = encrypt(larger.serialize("main"))
        finally:
            larger.close()
        self.path.write_bytes(encrypted)
        with reader.open_db(self.path, KEY) as database:
            self.assertEqual(database.execute("SELECT id FROM sample WHERE id=1").fetchone(), (1,))
            self.assertLess(database.stats["db_bytes_read"], len(encrypted) // 2)
            self.assertLessEqual(len(database._source._cache), 32)
            database.validate()
            self.assertLess(database.stats["db_recheck_bytes"], len(encrypted) // 2)
            self.assertEqual(database.stats["snapshot_guarantee"], "guarded_read_not_atomic")

    def test_committed_wal_overlays_and_ignores_uncommitted_tail(self):
        committed = self.changed()
        frames = transaction(committed) + [(2, 0, self.base[PAGE:2 * PAGE])]
        for big_endian in (False, True):
            with self.subTest(big_endian=big_endian):
                self.wal.write_bytes(make_wal(frames, big_endian))
                with reader.open_db(self.path, KEY) as database:
                    self.assertEqual(self.value(database), "committed")
                    self.assertEqual(database.stats["wal_valid_frames"], 3)
                    self.assertEqual(database.stats["wal_committed_frames"], 2)
                    self.assertEqual(database.stats["wal_tail_reason"], "uncommitted_frames")
        self.assertEqual(self.path.read_bytes(), self.base)

    def test_wal_growth_and_latest_commit(self):
        first = self.changed("first")
        self.plain.executemany("INSERT INTO sample VALUES(?, ?)",
                               ((i, "y" * 200) for i in range(4, 250)))
        last = self.changed("last")
        self.wal.write_bytes(make_wal(transaction(first) + transaction(last)))
        with reader.open_db(self.path, KEY) as database:
            self.assertEqual(self.value(database), "last")
            self.assertEqual(database.execute("SELECT count(*) FROM sample").fetchone(), (249,))
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(database.stats["snapshot_pages"], len(last) // PAGE)

    def test_wal_without_commit_is_not_visible(self):
        self.wal.write_bytes(make_wal(transaction(self.changed(), commit=False)))
        with reader.open_db(self.path, KEY) as database:
            self.assertTrue(self.value(database).startswith("synthetic-"))
            self.assertEqual(database.stats["wal_committed_frames"], 0)

    def test_torn_commit_is_not_visible(self):
        first = self.changed("first")
        last = self.changed("last")
        self.wal.write_bytes(make_wal(transaction(first) + transaction(last))[:-100])
        with reader.open_db(self.path, KEY) as database:
            self.assertEqual(self.value(database), "first")
            self.assertEqual(database.stats["wal_tail_reason"], "partial_frame")

    def test_invalid_frame_checksum_or_salt_stops_at_prior_commit(self):
        first = self.changed("first")
        last = self.changed("last")
        good = make_wal(transaction(first) + transaction(last))
        for offset, reason in ((32 + 2 * (PAGE + 24) + 8, "salt_mismatch"),
                               (32 + 2 * (PAGE + 24) + 30, "checksum_mismatch")):
            with self.subTest(reason=reason):
                corrupted = bytearray(good)
                corrupted[offset] ^= 1
                self.wal.write_bytes(corrupted)
                with reader.open_db(self.path, KEY) as database:
                    self.assertEqual(self.value(database), "first")
                    self.assertEqual(database.stats["wal_tail_reason"], reason)

    def test_bad_wal_header_is_rejected(self):
        data = bytearray(make_wal(transaction(self.changed())))
        data[24] ^= 1
        self.wal.write_bytes(data)
        with self.assertRaises(reader.IntegrityError):
            reader.open_db(self.path, KEY)

    def test_wrong_key_and_bad_base_hmac_are_rejected(self):
        with self.assertRaises(reader.IntegrityError):
            reader.open_db(self.path, b"z" * 32)
        for page_number in (1, 2):
            corrupted = bytearray(self.base)
            corrupted[page_number * PAGE - 1] ^= 1
            self.path.write_bytes(corrupted)
            with self.assertRaises(reader.IntegrityError):
                with reader.open_db(self.path, KEY) as database:
                    database.execute("SELECT * FROM sample").fetchall()

    def test_wal_hmac_checked_independently_of_wal_checksum(self):
        corrupted = bytearray(self.changed())
        corrupted[2 * PAGE - 1] ^= 1
        self.wal.write_bytes(make_wal(transaction(bytes(corrupted))))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaises(reader.IntegrityError):
                with reader.open_db(self.path, KEY) as database:
                    database.execute("SELECT * FROM sample").fetchall()
        self.assertEqual(errors.getvalue(), "")

    def test_writes_and_external_access_are_denied(self):
        statements = ["DELETE FROM sample", "CREATE TABLE other(x)",
                      "CREATE TEMP TABLE other(x)", "PRAGMA query_only=OFF",
                      "PRAGMA user_version=42", "PRAGMA journal_mode=WAL",
                      "ATTACH DATABASE ':memory:' AS other", "BEGIN IMMEDIATE",
                      "SELECT load_extension('not-a-library')",
                      "VACUUM INTO 'unexpected.db'"]
        with reader.open_db(self.path, KEY) as database:
            for sql in statements:
                with self.subTest(sql=sql), self.assertRaises(apsw.Error):
                    database.execute(sql).fetchall()
            file = reader._VirtualFile(database._source)
            for method, arguments in ((file.xWrite, (b"bad", 0)),
                                      (file.xTruncate, (0,)), (file.xSync, (0,))):
                with self.assertRaises(apsw.ReadOnlyError):
                    method(*arguments)
            with self.assertRaises(apsw.ReadOnlyError):
                database._vfs.xOpen(str(self.wal), [apsw.SQLITE_OPEN_READONLY, 0])
        self.assertEqual(self.path.read_bytes(), self.base)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_changes_detected_even_for_cached_results(self):
        with self.assertRaises(reader.SourceChangedError):
            with reader.open_db(self.path, KEY) as database:
                self.value(database)
                self.path.write_bytes(self.base)
                self.value(database)

    def test_new_wal_detected_during_cursor_iteration(self):
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        cursor = database.execute("SELECT * FROM sample")
        self.assertEqual(cursor.fetchone()[0], 1)
        self.wal.write_bytes(make_wal(transaction(self.changed())))
        with self.assertRaises(reader.SourceChangedError):
            cursor.fetchall()

    def test_wal_replacement_detected(self):
        data = make_wal(transaction(self.changed()))
        self.wal.write_bytes(data)
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        self.value(database)
        replacement = self.path.parent / "replacement.wal"
        replacement.write_bytes(data)
        replacement.replace(self.wal)
        with self.assertRaises(reader.SourceChangedError):
            self.value(database)

    def test_changes_during_page_read_are_rejected(self):
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        original = os.pread

        def changing_read(descriptor, amount, offset):
            data = original(descriptor, amount, offset)
            current = self.path.stat()
            os.utime(self.path, ns=(current.st_atime_ns, current.st_mtime_ns + 10_000_000))
            return data

        with mock.patch.object(reader.os, "pread", changing_read):
            with self.assertRaises(reader.SourceChangedError):
                database.execute("SELECT * FROM sample").fetchall()

    def test_checksum_matches_a_sqlite_generated_wal(self):
        path = self.path.parent / "sqlite-synthetic.db"
        connection = apsw.Connection(str(path))
        self.addCleanup(connection.close)
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE synthetic(value TEXT)")
        connection.execute("INSERT INTO synthetic VALUES('fixture')")
        data = Path(str(path) + "-wal").read_bytes()
        endian = "<" if struct.unpack_from(">I", data)[0] == 0x377F0682 else ">"
        state = reader._checksum(data[:24], endian)
        self.assertEqual(state, struct.unpack_from(">II", data, 24))
        commits = 0
        for offset in range(32, len(data), 24 + PAGE):
            state = reader._checksum(data[offset:offset + 8], endian, state)
            state = reader._checksum(data[offset + 24:offset + 24 + PAGE], endian, state)
            self.assertEqual(state, struct.unpack_from(">II", data, offset + 16))
            commits += bool(struct.unpack_from(">I", data, offset + 4)[0])
        self.assertGreaterEqual(commits, 2)

    def test_failed_open_closes_source_descriptors(self):
        descriptors = []
        original = os.open

        def record_open(*args, **kwargs):
            descriptor = original(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        with mock.patch.object(reader.os, "open", record_open):
            with self.assertRaises(reader.IntegrityError):
                reader.open_db(self.path, b"z" * 32)
        self.assertTrue(descriptors)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_context_exit_detects_equal_size_base_write_with_restored_mtime(self):
        database = reader.open_db(self.path, KEY)
        with unpublished_change_times(database._source):
            with self.assertRaises(reader.SourceChangedError):
                with database:
                    database.execute("SELECT * FROM sample").fetchall()
                    replace_same_size_preserving_mtime(self.path, self.changed())
                    # Metadata deliberately still matches; exit must compare bytes.
                    database._source.check()
        self.assertGreater(database.stats["db_recheck_bytes"], 0)

    def test_exhaustion_detects_equal_size_checkpoint_of_a_used_base_page(self):
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        cursor = database.execute("SELECT * FROM sample")
        cursor.fetchone()
        with unpublished_change_times(database._source):
            replace_same_size_preserving_mtime(self.path, self.changed("checkpoint"))
            database._source.check()
            with self.assertRaises(reader.SourceChangedError):
                cursor.fetchall()

    def test_wal_generation_change_detected_without_timestamp_change(self):
        data = make_wal(transaction(self.changed()))
        self.wal.write_bytes(data)
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        changed = bytearray(data)
        changed[16] ^= 1
        with unpublished_change_times(database._source):
            replace_same_size_preserving_mtime(self.wal, changed)
            database._source.check()
            with self.assertRaises(reader.SourceChangedError):
                database.validate()
        self.assertGreater(database.stats["wal_recheck_bytes"], 0)

    def test_wal_commit_boundary_change_detected_without_timestamp_change(self):
        data = make_wal(transaction(self.changed()))
        self.wal.write_bytes(data)
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        changed = bytearray(data)
        last_frame = 32 + (len(self.base) // PAGE - 1) * (24 + PAGE)
        changed[last_frame + 4:last_frame + 8] = b"\x00" * 4
        with unpublished_change_times(database._source):
            replace_same_size_preserving_mtime(self.wal, changed)
            database._source.check()
            with self.assertRaises(reader.SourceChangedError):
                database.validate()

    def test_used_wal_page_rechecked_even_away_from_commit_boundary(self):
        large = make_plain(1200)
        try:
            encrypted = encrypt(large.serialize("main"))
        finally:
            large.close()
        data = make_wal(transaction(encrypted))
        self.wal.write_bytes(data)
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        database.execute("SELECT value FROM sample WHERE id=1").fetchall()
        source = database._source
        used_regions = [region for region in source._wal_rechecks
                        if region[0] > 32 and region != source._commit_range]
        self.assertTrue(used_regions)
        changed = bytearray(data)
        changed[used_regions[0][0] + 24 + 100] ^= 1
        with unpublished_change_times(source):
            replace_same_size_preserving_mtime(self.wal, changed)
            source.check()
            with self.assertRaises(reader.SourceChangedError):
                database.validate()

    def test_first_invalid_salt_is_explicit_and_boundary_rechecked(self):
        changed = bytearray(make_wal(transaction(self.changed())))
        changed[32 + 8] ^= 1
        self.wal.write_bytes(changed)
        database = reader.open_db(self.path, KEY)
        self.addCleanup(database.close)
        self.assertTrue(self.value(database).startswith("synthetic-"))
        self.assertEqual(database.stats["wal_valid_frames"], 0)
        self.assertEqual(database.stats["wal_tail_reason"], "salt_mismatch")
        self.assertEqual(database.stats["wal_view"], "base_db_only_no_committed_wal")
        changed[32 + 8] ^= 1
        with unpublished_change_times(database._source):
            replace_same_size_preserving_mtime(self.wal, changed)
            database._source.check()
            with self.assertRaises(reader.SourceChangedError):
                database.validate()

    def test_wal_limit_and_rollback_journal_are_rejected(self):
        self.wal.write_bytes(b"x" * 33)
        with mock.patch.object(reader, "MAX_WAL_BYTES", 32):
            with self.assertRaises(reader.UnsupportedFormatError):
                reader.open_db(self.path, KEY)
        self.wal.unlink()
        Path(str(self.path) + "-journal").write_bytes(b"pending")
        with self.assertRaises(reader.UnsupportedFormatError):
            reader.open_db(self.path, KEY)

    def test_key_format_and_failed_open_cleanup(self):
        before = set(apsw.vfsnames())
        for key in (KEY + SALT, KEY.hex(), b"short"):
            with self.assertRaises(ValueError):
                reader.open_db(self.path, key)
        with self.assertRaises(reader.IntegrityError):
            reader.open_db(self.path, b"z" * 32)
        self.assertEqual(set(apsw.vfsnames()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
