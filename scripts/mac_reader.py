"""Experimental macOS file-only reader using caller-supplied, private raw keys.

No process attachment, app launch, window control, or key extraction is performed.
The Windows/WSL implementation remains the separately tested local_reader.py CLI.
"""
import argparse
import ctypes.util
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPOSITORY = Path(__file__).resolve().parent.parent
MAX_KEY_FILE_BYTES = 1024 * 1024


class MacReaderError(ValueError):
    """A bounded validation failure with a message safe for terminal output."""
    def __init__(self, message, code='invalid_input'):
        super().__init__(message)
        self.code = code


def account_databases(account_dir):
    """Inventory only the explicitly selected account's supported database names."""
    root = account_dir / 'db_storage'
    contact = root / 'contact' / 'contact.db'
    shards = sorted(p for p in (root / 'message').glob('message_*.db')
                    if re.fullmatch(r'message_[0-9]+\.db', p.name))
    if not contact.is_file() or not shards:
        raise MacReaderError('Expected contact/contact.db and message/message_N.db are absent')
    selected = [contact] + shards
    for path in selected:
        # Do not follow a database or companion file into a different account.
        for source in (path, Path(str(path) + '-wal'), Path(str(path) + '-journal')):
            if source.is_symlink() or not source.resolve().is_relative_to(account_dir):
                raise MacReaderError('Database paths must stay inside the selected account without symlink files')
        if not path.is_file():
            raise MacReaderError('A selected database is not a regular file')
    return [p.relative_to(root).as_posix() for p in selected]


def unique_json_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise MacReaderError('Duplicate JSON fields in the key file')
        result[name] = value
    return result


def load_keys(path, account_dir, account, relatives):
    """Read an existing 0600/0400 regular file without displaying its contents."""
    if path.resolve().is_relative_to(REPOSITORY) or path.resolve().is_relative_to(account_dir):
        raise MacReaderError('Key file must be outside the repository and source account')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise MacReaderError('Cannot open the private key file; verify its path and permissions') from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)):
            raise MacReaderError('Key file must be owned by this user and have mode 0600 or 0400')
        if not 0 < before.st_size <= MAX_KEY_FILE_BYTES:
            raise MacReaderError('Key file is empty or exceeds 1 MiB')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_KEY_FILE_BYTES + 1)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
            raise MacReaderError('Key file changed during reading')
    finally:
        os.close(fd)
    try:
        secret = json.loads(data, object_pairs_hook=unique_json_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MacReaderError('Key file is not a valid bounded JSON object') from None
    if not isinstance(secret, dict) or type(secret.get('version')) is not int or secret['version'] != 1:
        raise MacReaderError('Key file version must be 1')
    if set(secret) - {'version', 'account_dir', 'account', 'keys', 'image_config'}:
        raise MacReaderError('Unsupported fields in the key file')
    declared = secret.get('account_dir')
    if (not isinstance(declared, str) or not Path(declared).is_absolute()
            or Path(declared).resolve() != account_dir or secret.get('account') != account):
        raise MacReaderError('Key file account and absolute account_dir must match the requested scope')
    keys = secret.get('keys')
    if not isinstance(keys, dict) or set(keys) != set(relatives):
        raise MacReaderError('Key file must cover exactly contact.db and every discovered message_N.db')
    if not all(isinstance(key, str) and re.fullmatch(r'[0-9a-fA-F]{64}', key) for key in keys.values()):
        raise MacReaderError('Each database key must be 32 raw bytes encoded as 64 hexadecimal characters')
    if 'image_config' in secret and (type(secret['image_config']) is not int
                                    or not 0 <= secret['image_config'] < 2 ** 64):
        raise MacReaderError('image_config must be an unsigned 64-bit integer when supplied')
    return secret


def status(account_dir):
    """Environment and selected-path checks only; no key file or chat query."""
    dependencies = {}
    for package in ('apsw', 'cryptography', 'Pillow'):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    zstd = ctypes.util.find_library('zstd') or next((str(p) for p in (
        Path('/opt/homebrew/lib/libzstd.dylib'), Path('/usr/local/lib/libzstd.dylib'))
        if p.is_file()), None)
    report = dict(platform=sys.platform, architecture=platform.machine(),
                  macos_version=platform.mac_ver()[0] or None,
                  experimental=True, live_wechat_tested=False,
                  dependencies=dependencies, zstd_found=bool(zstd),
                  ffmpeg_found=shutil.which('ffmpeg') is not None,
                  key_extraction_supported=False, window_observation_supported=False)
    try:
        report['databases'] = account_databases(account_dir)
        report['layout_found'] = True
    except (MacReaderError, OSError) as exc:
        report['layout_found'] = False
        report['layout_error'] = str(exc) if isinstance(exc, MacReaderError) else type(exc).__name__
    report['ok'] = sys.platform == 'darwin' and all(dependencies.values()) and report['layout_found']
    report['scope'] = 'environment_and_filenames_only_not_key_or_schema_validation'
    return report


def read(args):
    if sys.platform != 'darwin':
        raise MacReaderError('mac_reader.py read requires native macOS Python')
    if not 1 <= args.limit <= 100 or not 0 <= args.images <= 20:
        raise MacReaderError('Use --limit 1..100 and --images 0..20')
    if not args.account or not args.contact:
        raise MacReaderError('Exact account and individual contact names are required')
    try:
        timezone = ZoneInfo(args.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise MacReaderError('Provide a valid IANA timezone for the attachment month folders') from None
    args.account_dir = args.account_dir.expanduser().resolve()
    args.output = args.output.expanduser().absolute()
    if (args.output.resolve().is_relative_to(args.account_dir)
            or args.output.resolve().is_relative_to(REPOSITORY)):
        raise MacReaderError('Output must be outside the account and repository')
    if args.output.exists() or args.output.is_symlink():
        raise MacReaderError('Output directory must not already exist')
    relatives = account_databases(args.account_dir)
    secret = load_keys(args.key_file.expanduser().absolute(), args.account_dir, args.account, relatives)

    # Keep status/help usable before third-party dependencies are installed.
    from local_reader import ContactScopeError, decrypt_image, read_target
    from page_reader import SourceChangedError

    def image_decoder(data):
        # Some Mac caches hold ordinary image bytes under a .dat extension.
        if data.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'wxgf')):
            return data
        if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            return data
        return decrypt_image(data, secret.get('image_config'), args.account)

    previous_umask = os.umask(0o077)
    try:
        for attempt in range(2):
            try:
                result = read_target(args, secret, path_formatter=lambda p: str(p.resolve()),
                                     timezone=timezone, image_decoder=image_decoder)
                break
            except SourceChangedError:
                if attempt or args.output.exists():
                    raise
            except ContactScopeError as exc:
                raise MacReaderError(str(exc), code=exc.code) from None
        result.update(platform='macos', experimental=True, live_wechat_tested=False,
                      account=args.account, account_dir=str(args.account_dir),
                      key_source='user_supplied_private_file', account_identity_source='user_supplied',
                      timezone=args.timezone, ui_automation_used=False,
                      background_verified=None, minimized_verified=None,
                      background_observation={'supported': False, 'reason': 'no_macos_window_observer'})
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        os.umask(previous_umask)
    return dict(ok=True, platform='macos', experimental=True, live_wechat_tested=False,
                ui_automation_used=False, background_verified=None, minimized_verified=None,
                messages=len(result['messages']),
                unreadable_text_messages=sum(item['type'] & 4294967295 == 1 and item['text'] is None
                                             for item in result['messages']),
                images=[{key: item.get(key) for key in ('status', 'size', 'tier')} for item in result['images']],
                result=str(args.output / 'result.json'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('status', help='Check environment and explicitly selected account paths only')
    check.add_argument('--account-dir', type=Path, required=True)
    query = commands.add_parser('read', help='Read one individual contact with pre-existing raw keys')
    query.add_argument('--account-dir', type=Path, required=True)
    query.add_argument('--account', required=True)
    query.add_argument('--contact', required=True)
    query.add_argument('--key-file', type=Path, required=True)
    query.add_argument('--timezone', required=True, help='IANA timezone used to choose attachment month directories')
    query.add_argument('--limit', type=int, default=30)
    query.add_argument('--images', type=int, default=8)
    query.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = status(args.account_dir.expanduser().resolve()) if args.command == 'status' else read(args)
    except MacReaderError as exc:
        report = dict(ok=False, error=type(exc).__name__, code=exc.code, message=str(exc))
    except Exception as exc:
        # Library error strings/tracebacks can include source content. Keep them private.
        report = dict(ok=False, error=type(exc).__name__,
                      message='Read failed; check dependencies, access, keys, database format, and source changes. '
                              'Any partial output is incomplete; retry only in a new output directory.')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
