"""Read SQLCipher 4 pages through a read-only, in-memory APSW VFS.

Only the encrypted WAL (at most 64 MiB) and a bounded cache of requested
plaintext pages are retained. No decrypted database, journal, or temporary file
is created. The source files are never locked or written. Source metadata is
checked during reads. On cursor exhaustion and successful context exit, used
ciphertext pages and relevant WAL boundaries are reread and compared by digest,
including when source timestamps are unchanged. Callers must discard partial
results and reopen after any change error. Without cooperation from the writer,
this is a guarded read rather than an operating-system-enforced atomic snapshot.

The supported format is a 32-byte raw key, 4096-byte pages, a 16-byte page-one
salt, and 80 reserved bytes containing the IV and SHA-512 HMAC. Password keys,
explicit-salt plaintext headers, other cipher formats, and rollback-journal
recovery are not supported. Every page returned to SQLite is authenticated;
unrequested pages are not a full-database integrity check.

When WAL scanning stops before a valid commit, the view contains base-database
pages only. In particular, a salt mismatch at the first frame is recorded as
such; it does not establish that the database reflects the latest application
state. The reader never treats discarded WAL content as a committed update.

WAL parsing follows https://www.sqlite.org/fileformat2.html#walformat. The
cryptographic layout was cross-checked against Apache-2.0 wechatauto db.py at
commit d322c4a9b1124228820bab363e5676016f188fc6; this implementation does not
import that module or its process/key-discovery functionality.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import os
from pathlib import Path
import stat
import struct
import uuid

import apsw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


PAGE_SIZE = 4096
RESERVE_SIZE = 80
MAX_WAL_BYTES = 64 * 1024 * 1024
_CACHE_PAGES = 32
_FRAME_SIZE = 24 + PAGE_SIZE
_SQLITE_HEADER = b"SQLite format 3\x00"


class ReaderError(Exception):
    """The encrypted database cannot be read safely with this reader."""


class IntegrityError(ReaderError):
    """A cipher page or WAL header failed an integrity check."""


class SourceChangedError(ReaderError):
    """Source files changed; discard results and open a new reader."""


class UnsupportedFormatError(ReaderError):
    """The source requires an unsupported format or recovery operation."""


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _path_signature(path: Path) -> tuple[int, ...] | None:
    try:
        return _signature(path.stat())
    except FileNotFoundError:
        return None


def _checksum(data: bytes | memoryview, endian: str,
              state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Compute the SQLite rolling checksum with the WAL's byte order."""
    first, second = state
    for left, right in struct.iter_unpack(endian + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


class _Source:
    def __init__(self, path: os.PathLike[str] | str, raw_key: bytes):
        if not isinstance(raw_key, bytes) or len(raw_key) != 32:
            raise ValueError("raw_key must be exactly 32 bytes")
        self.path = Path(path).absolute()
        self.wal_path = Path(str(self.path) + "-wal")
        self.journal_path = Path(str(self.path) + "-journal")
        self._fd = None
        self._wal_fd = None
        self._closed = False
        self._invalid = False
        self._failure = None
        self._key = raw_key
        self._mac_key = b""
        self._wal = b""
        self._overlay = {}
        self._cache = OrderedDict()
        self._base_digests = {}
        self._wal_rechecks = {}
        self._commit_range = None
        self.stats = dict(db_bytes_read=0, wal_bytes_read=0,
                          sqlite_bytes_requested=0, pages_decrypted=0,
                          page_cache_hits=0, wal_valid_frames=0,
                          wal_committed_frames=0, wal_overlay_pages=0,
                          wal_ignored_tail_bytes=0, wal_tail_reason="none",
                          wal_view="absent_or_empty", db_recheck_bytes=0,
                          wal_recheck_bytes=0, content_rechecks=0,
                          snapshot_guarantee="guarded_read_not_atomic")
        try:
            self._db_signature = _path_signature(self.path)
            self._wal_signature = _path_signature(self.wal_path)
            self._journal_signature = _path_signature(self.journal_path)
            if self._db_signature is None:
                raise FileNotFoundError("The encrypted database does not exist")
            if self._journal_signature and self._journal_signature[2]:
                raise UnsupportedFormatError("Nonempty rollback journal requires recovery")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            self._fd = os.open(self.path, flags)
            db_stat = os.fstat(self._fd)
            if not stat.S_ISREG(db_stat.st_mode):
                raise UnsupportedFormatError("The source must be a regular file")
            self.check()
            self._base_pages, remainder = divmod(db_stat.st_size, PAGE_SIZE)
            if not self._base_pages or remainder:
                raise UnsupportedFormatError("Database size is not a positive multiple of 4096")
            self._page_count = self._base_pages
            first_page = self._read_base(1)
            self._salt = first_page[:16]
            mac_salt = bytes(value ^ 0x3A for value in self._salt)
            self._mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, 32)
            self._decrypt(first_page, 1)
            if self._wal_signature:
                if self._wal_signature[2] > MAX_WAL_BYTES:
                    raise UnsupportedFormatError("Encrypted WAL exceeds the 64 MiB limit")
                self._wal_fd = os.open(self.wal_path, flags)
                if not stat.S_ISREG(os.fstat(self._wal_fd).st_mode):
                    raise UnsupportedFormatError("The WAL must be a regular file")
                self.check()
                self._wal = os.pread(self._wal_fd, self._wal_signature[2], 0)
                self.stats["wal_bytes_read"] = len(self._wal)
                if len(self._wal) != self._wal_signature[2]:
                    raise SourceChangedError("WAL changed while being captured")
                self.check()
                self._parse_wal()
            self.stats["db_file_bytes"] = db_stat.st_size
            self.stats["snapshot_pages"] = self._page_count
            self.page(1)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        """Validate paths and open descriptors, including replacement/removal."""
        if self._closed:
            raise ReaderError("Reader is closed")
        if self._failure is not None:
            raise self._failure
        if self._invalid:
            raise SourceChangedError("Source changed; discard results and reopen")
        try:
            changed = (
                _path_signature(self.path) != self._db_signature
                or _signature(os.fstat(self._fd)) != self._db_signature
                or _path_signature(self.wal_path) != self._wal_signature
                or _path_signature(self.journal_path) != self._journal_signature
                or (self._wal_fd is not None and
                    _signature(os.fstat(self._wal_fd)) != self._wal_signature)
            )
        except OSError as error:
            self._invalid = True
            raise SourceChangedError("Source cannot be revalidated") from error
        if changed:
            self._invalid = True
            raise SourceChangedError("Source changed; discard results and reopen")

    def _read_base(self, page_number):
        data = os.pread(self._fd, PAGE_SIZE, (page_number - 1) * PAGE_SIZE)
        self.stats["db_bytes_read"] += len(data)
        if len(data) != PAGE_SIZE:
            self.check()
            raise IntegrityError("Requested database page is missing")
        digest = hashlib.sha256(data).digest()
        previous = self._base_digests.get(page_number)
        if previous is not None and not hmac.compare_digest(previous, digest):
            self._invalid = True
            raise SourceChangedError("Previously read database page changed")
        self._base_digests[page_number] = digest
        return data

    def _remember_wal_range(self, offset, size):
        self._wal_rechecks[(offset, size)] = hashlib.sha256(
            self._wal[offset:offset + size]).digest()

    def _recheck_range(self, descriptor, offset, size, expected, counter):
        try:
            content = os.pread(descriptor, size, offset)
        except OSError as error:
            self._invalid = True
            raise SourceChangedError("Source content cannot be revalidated") from error
        self.stats[counter] += len(content)
        if len(content) != size or not hmac.compare_digest(
                hashlib.sha256(content).digest(), expected):
            self._invalid = True
            raise SourceChangedError("Source ciphertext changed during guarded read")

    def revalidate(self):
        """Reread used pages and WAL boundaries without scanning the database."""
        self.check()
        self.stats["content_rechecks"] += 1
        for (offset, size), digest in sorted(self._wal_rechecks.items()):
            self._recheck_range(self._wal_fd, offset, size, digest, "wal_recheck_bytes")
        for page_number, digest in sorted(self._base_digests.items()):
            self._recheck_range(self._fd, (page_number - 1) * PAGE_SIZE, PAGE_SIZE,
                                digest, "db_recheck_bytes")
        # Bracket the database rereads with the captured WAL generation and
        # commit boundary. This can detect a checkpoint/reset during the check,
        # but does not claim an atomic snapshot or exclude every possible race.
        for region in ((0, 32), self._commit_range):
            if region in self._wal_rechecks:
                self._recheck_range(self._wal_fd, *region,
                                    self._wal_rechecks[region], "wal_recheck_bytes")
        self.check()

    def _decrypt(self, page, page_number):
        start = 16 if page_number == 1 else 0
        if len(page) != PAGE_SIZE:
            raise IntegrityError("Encrypted page has the wrong length")
        if page_number == 1 and page[:16] != self._salt:
            raise IntegrityError("Page-one salt differs from the database salt")
        expected = hmac.new(self._mac_key, page[start:4032], hashlib.sha512)
        expected.update(struct.pack("<I", page_number))
        if not hmac.compare_digest(expected.digest(), page[4032:]):
            raise IntegrityError(f"Page {page_number} HMAC verification failed")
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(page[4016:4032]))
        decryptor = cipher.decryptor()
        plain = decryptor.update(page[start:4016]) + decryptor.finalize()
        if page_number == 1:
            plain = _SQLITE_HEADER + plain
            if (plain[16:18] != b"\x10\x00" or plain[20] != RESERVE_SIZE
                    or plain[21:24] != b"\x40\x20\x20"
                    or plain[18] not in (1, 2) or plain[19] not in (1, 2)):
                raise UnsupportedFormatError("Decrypted SQLite header has unsupported settings")
        self.stats["pages_decrypted"] += 1
        return plain + bytes(RESERVE_SIZE)

    def _parse_wal(self):
        if not self._wal:
            return
        if len(self._wal) < 32:
            raise IntegrityError("WAL header is incomplete")
        magic, version, page_size = struct.unpack_from(">III", self._wal)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
            raise UnsupportedFormatError("WAL header has unsupported magic or version")
        if page_size != PAGE_SIZE:
            raise UnsupportedFormatError("WAL page size is not 4096")
        endian = "<" if magic == 0x377F0682 else ">"
        checksum = _checksum(self._wal[:24], endian)
        if checksum != struct.unpack_from(">II", self._wal, 24):
            raise IntegrityError("WAL header checksum verification failed")
        self._remember_wal_range(0, 32)
        salt = self._wal[16:24]
        frames = []
        last_commit = 0
        offset = 32
        view = memoryview(self._wal)
        while offset + _FRAME_SIZE <= len(self._wal):
            page_number, db_size = struct.unpack_from(">II", view, offset)
            if view[offset + 8:offset + 16] != salt:
                self.stats["wal_tail_reason"] = "salt_mismatch"
                break
            if not 1 <= page_number <= 0xFFFFFFFE or db_size > 0xFFFFFFFE:
                self.stats["wal_tail_reason"] = "invalid_page_number"
                break
            checksum = _checksum(view[offset:offset + 8], endian, checksum)
            checksum = _checksum(view[offset + 24:offset + _FRAME_SIZE], endian, checksum)
            if checksum != struct.unpack_from(">II", view, offset + 16):
                self.stats["wal_tail_reason"] = "checksum_mismatch"
                break
            frames.append((page_number, db_size, offset + 24))
            if db_size:
                last_commit = len(frames)
            offset += _FRAME_SIZE
        if self.stats["wal_tail_reason"] == "none":
            if offset < len(self._wal):
                self.stats["wal_tail_reason"] = "partial_frame"
            elif len(frames) > last_commit:
                self.stats["wal_tail_reason"] = "uncommitted_frames"
        if last_commit:
            self._commit_range = (frames[last_commit - 1][2] - 24, _FRAME_SIZE)
            self._remember_wal_range(*self._commit_range)
        if len(frames) > last_commit:
            self._remember_wal_range(frames[-1][2] - 24, _FRAME_SIZE)
        if offset < len(self._wal):
            self._remember_wal_range(offset, min(_FRAME_SIZE, len(self._wal) - offset))
        previous_size = self._base_pages
        for page_number, db_size, page_offset in frames[:last_commit]:
            self._overlay[page_number] = page_offset
            if db_size:
                if db_size < previous_size:
                    self._overlay = {number: position for number, position in
                                     self._overlay.items() if number <= db_size}
                previous_size = db_size
                self._page_count = db_size
        self._overlay = {number: position for number, position in self._overlay.items()
                         if number <= self._page_count}
        self.stats.update(wal_valid_frames=len(frames),
                          wal_committed_frames=last_commit,
                          wal_overlay_pages=len(self._overlay),
                          wal_ignored_tail_bytes=len(self._wal) - 32 - last_commit * _FRAME_SIZE,
                          wal_view=("committed_wal_overlay" if last_commit else
                                    "base_db_only_no_committed_wal"))

    def page(self, page_number):
        self.check()
        if not 1 <= page_number <= self._page_count:
            raise IntegrityError("Requested page is outside the committed database")
        if page_number in self._cache:
            self.stats["page_cache_hits"] += 1
            self._cache.move_to_end(page_number)
            return self._cache[page_number]
        if page_number in self._overlay:
            offset = self._overlay[page_number]
            encrypted = self._wal[offset:offset + PAGE_SIZE]
            self._remember_wal_range(offset - 24, _FRAME_SIZE)
        else:
            encrypted = self._read_base(page_number)
        plain = self._decrypt(encrypted, page_number)
        if page_number == 1:
            header = bytearray(plain)
            # SQLite must not inspect real WAL/journal files for this virtual view.
            header[18:20] = b"\x01\x01"
            if self.stats["wal_committed_frames"]:
                header[28:32] = struct.pack(">I", self._page_count)
                header[92:96] = header[24:28]
            plain = bytes(header)
        self.check()
        self._cache[page_number] = plain
        if len(self._cache) > _CACHE_PAGES:
            self._cache.popitem(last=False)
        return plain

    def read(self, amount, offset):
        self.check()
        self.stats["sqlite_bytes_requested"] += amount
        if amount < 0 or offset < 0:
            raise apsw.IOError("Invalid virtual read range")
        remaining = min(amount, max(0, self._page_count * PAGE_SIZE - offset))
        output = bytearray()
        while remaining:
            number, within_page = divmod(offset, PAGE_SIZE)
            take = min(remaining, PAGE_SIZE - within_page)
            output.extend(self.page(number + 1)[within_page:within_page + take])
            remaining -= take
            offset += take
        self.check()
        # APSW converts a short return into SQLITE_IOERR_SHORT_READ and pads it.
        return bytes(output)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cache.clear()
        self._overlay.clear()
        self._base_digests.clear()
        self._wal_rechecks.clear()
        self._wal = b""
        self._key = self._mac_key = b""
        for descriptor in (self._fd, self._wal_fd):
            if descriptor is not None:
                os.close(descriptor)
        self._fd = self._wal_fd = None


class _VirtualFile:
    def __init__(self, source):
        self.source = source

    def excepthook(self, error_type, error, traceback):
        # The wrapper raises the retained error. Do not let APSW duplicate a
        # callback traceback into application logs, which may contain SQL text.
        pass

    def xRead(self, amount, offset):
        return self._guard(self.source.read, amount, offset)

    def _guard(self, operation, *arguments):
        # APSW recognizes its own error types at the C VFS boundary. Preserve
        # the specific reader error for the public Python wrapper to re-raise.
        try:
            return operation(*arguments)
        except ReaderError as error:
            self.source._failure = error
            raise apsw.IOError(str(error)) from error

    def xFileSize(self):
        self._guard(self.source.check)
        return self.source._page_count * PAGE_SIZE

    def xClose(self):
        pass

    def xLock(self, level):
        self._guard(self.source.check)
        if level > apsw.SQLITE_LOCK_SHARED:
            raise apsw.ReadOnlyError("The virtual database is read-only")

    def xUnlock(self, level):
        pass

    def xCheckReservedLock(self):
        return False

    def xFileControl(self, operation, pointer):
        return False

    def xSectorSize(self):
        return PAGE_SIZE

    def xDeviceCharacteristics(self):
        return 0

    def xWrite(self, data, offset):
        raise apsw.ReadOnlyError("The virtual database is read-only")

    def xTruncate(self, size):
        raise apsw.ReadOnlyError("The virtual database is read-only")

    def xSync(self, flags):
        raise apsw.ReadOnlyError("The virtual database is read-only")


class _ReadOnlyVFS(apsw.VFS):
    def __init__(self, source):
        self.source = source
        self.name = "cipher_page_reader_" + uuid.uuid4().hex
        self.filename = "/" + self.name + ".db"
        super().__init__(self.name, "")

    def xFullPathname(self, name):
        return str(name)

    def excepthook(self, error_type, error, traceback):
        pass

    def xOpen(self, name, flags):
        filename = name.filename() if isinstance(name, apsw.URIFilename) else name
        if (filename != self.filename or not flags[0] & apsw.SQLITE_OPEN_MAIN_DB
                or not flags[0] & apsw.SQLITE_OPEN_READONLY
                or flags[0] & (apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE)):
            raise apsw.ReadOnlyError("Only the virtual read-only main database can be opened")
        flags[1] = apsw.SQLITE_OPEN_READONLY
        return _VirtualFile(self.source)

    def xAccess(self, name, flags):
        return False

    def xDelete(self, name, syncdir):
        raise apsw.ReadOnlyError("The virtual database is read-only")


_READ_PRAGMAS = frozenset({
    "application_id", "collation_list", "compile_options", "database_list",
    "encoding", "freelist_count", "page_count", "page_size", "query_only",
    "schema_version", "user_version",
})
_ARGUMENT_PRAGMAS = frozenset({
    "foreign_key_check", "foreign_key_list", "index_info", "index_list",
    "index_xinfo", "integrity_check", "quick_check", "table_info", "table_xinfo",
    "table_list",
})


def _authorize(action, first, second, database, trigger):
    if action in (apsw.SQLITE_SELECT, apsw.SQLITE_READ, apsw.SQLITE_RECURSIVE):
        return apsw.SQLITE_OK
    if action == apsw.SQLITE_FUNCTION:
        return (apsw.SQLITE_DENY if (second or "").lower() in
                {"load_extension", "readfile", "writefile"} else apsw.SQLITE_OK)
    if action == apsw.SQLITE_PRAGMA:
        name = (first or "").lower()
        if name in _ARGUMENT_PRAGMAS or (name in _READ_PRAGMAS and second is None):
            return apsw.SQLITE_OK
    return apsw.SQLITE_DENY


class _Cursor:
    """Check source changes even when SQLite satisfies a query from its cache."""

    def __init__(self, cursor, source):
        self._cursor = cursor
        self._source = source

    def __iter__(self):
        return self

    def __next__(self):
        self._source.check()
        try:
            row = next(self._cursor)
        except StopIteration:
            self._source.revalidate()
            raise
        except apsw.Error as error:
            if self._source._failure is not None:
                raise self._source._failure from error
            raise
        self._source.check()
        return row

    def fetchone(self):
        return next(self, None)

    def fetchall(self):
        return list(self)

    def getdescription(self):
        return self._cursor.getdescription()

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()


class ReadOnlyDatabase:
    """A context-managed connection with execute(), stats, and close().

    Read SQL is supported; writes, ATTACH, extension loading, transaction
    commands, and mutating PRAGMAs are denied. Cursor rows are tuples. If any
    SourceChangedError occurs, discard every row obtained from this instance.
    Use fetchall() when rows must not escape before the final change check.
    """

    def __init__(self, path, raw_key):
        self._source = _Source(path, raw_key)
        self._vfs = None
        self._connection = None
        self._closed = False
        try:
            self._vfs = _ReadOnlyVFS(self._source)
            self._connection = apsw.Connection(
                self._vfs.filename, flags=apsw.SQLITE_OPEN_READONLY,
                vfs=self._vfs.name, statementcachesize=0)
            self._connection.execute("PRAGMA temp_store=MEMORY")
            self._connection.execute("PRAGMA cache_size=-2048")
            self._connection.execute("PRAGMA mmap_size=0")
            self._connection.execute("PRAGMA query_only=ON")
            self._connection.enableloadextension(False)
            self._connection.setauthorizer(_authorize)
            self._source.check()
        except BaseException:
            self.close()
            raise

    @property
    def stats(self):
        """Return counters and status only, without paths, keys, or row content."""
        return dict(self._source.stats)

    def validate(self):
        """Recheck ciphertext before consuming rows from a partial iteration."""
        self._source.revalidate()

    def execute(self, sql, bindings=None):
        self._source.check()
        cursor = self._connection.cursor()
        try:
            if bindings is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, bindings)
            self._source.check()
            return _Cursor(cursor, self._source)
        except BaseException as error:
            try:
                cursor.close()
            except apsw.Error:
                pass
            if self._source._failure is not None:
                raise self._source._failure from error
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._connection is not None:
                self._connection.close(True)
        finally:
            if self._vfs is not None:
                self._vfs.unregister()
            self._source.close()

    def __enter__(self):
        self._source.check()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self._source.revalidate()
        finally:
            self.close()


def open_db(path: os.PathLike[str] | str, raw_key: bytes) -> ReadOnlyDatabase:
    """Open a guarded read-only view; close it or use it as a context manager."""
    return ReadOnlyDatabase(path, raw_key)
