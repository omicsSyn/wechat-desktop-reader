# Image decryption adapted from fanyuantaier/wechatauto-replica media.py
# (Apache-2.0), commit d322c4a9b1124228820bab363e5676016f188fc6.
# Changes: no package imports, key persistence, UI, network, or arbitrary hash
# fallback; explicit scope, structural attachment lookup, full image decoding.
"""Scoped read-only validation runner; output is limited to an exact named chat."""
import argparse, ctypes, ctypes.util, hashlib, io, json, os, pathlib, re, selectors, struct, subprocess, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from page_reader import open_db, SourceChangedError
HERE = pathlib.Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')


class MissingImageKeyError(ValueError):
    """This image format requires an independently supplied image key."""


class ContactScopeError(ValueError):
    """A contact selection failure whose fixed message contains no chat data."""
    MESSAGES = {
        'contact_not_found': 'No exact contact match; verify the requested nickname or remark',
        'contact_ambiguous': 'Multiple exact contact matches; select a unique nickname or remark',
        'individual_contact_required': 'An individual contact is required; group chats are unsupported',
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


def zstd_library():
    """Find system libzstd, including native Apple Silicon/Intel Homebrew."""
    candidates = [ctypes.util.find_library('zstd')]
    if sys.platform == 'darwin':
        candidates.extend(['/opt/homebrew/lib/libzstd.dylib',
                           '/usr/local/lib/libzstd.dylib'])
    for candidate in candidates:
        if candidate:
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                pass
    return None


def stop_observer(process, timeout=5):
    """Stop only this run's observer; an incomplete report cannot verify state."""
    try:
        output, _ = process.communicate(b'stop\n', timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=timeout)
        return {'completed_on_request': False, 'error': 'observer_shutdown_timeout'}
    if process.returncode:
        return {'completed_on_request': False, 'error': 'observer_failed'}
    try:
        report = json.loads(output)
    except (ValueError, UnicodeError):
        report = None
    if not isinstance(report, dict):
        return {'completed_on_request': False, 'error': 'invalid_observer_report'}
    return report


def start_observer(command, timeout=10, stop_timeout=5):
    """Wait for a bounded readiness line under WSL, cleaning up on every failure."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0)
    try:
        deadline = time.monotonic() + timeout
        line = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not line.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError('Observer startup timed out')
                # Reading one byte avoids consuming the later completion report.
                chunk = os.read(process.stdout.fileno(), 1)
                if not chunk or len(line) >= 16384:
                    raise RuntimeError('Observer readiness line is absent or oversized')
                line.extend(chunk)
        initial = json.loads(line)
        state = initial.get('initial') if isinstance(initial, dict) else None
        if (not isinstance(initial, dict) or initial.get('ready') is not True or not isinstance(state, dict)
                or state.get('exists') is not True
                or not all(isinstance(state.get(k), bool)
                           for k in ('minimized', 'wechat_foreground'))):
            raise RuntimeError('Observer could not verify the requested window')
        return process, initial
    except BaseException:
        stop_observer(process, timeout=stop_timeout)
        raise


def observation_flags(initial, monitor):
    """Treat missing, malformed, and incomplete observation data as unverified."""
    try:
        first, final = initial['initial'], monitor['final']
        counts = [monitor[k] for k in ('samples', 'foreground_samples',
                  'wechat_activation_events', 'missing_samples',
                  'minimized_samples', 'minimize_end_events')]
        if not all(type(value) is int and value >= 0 for value in counts):
            return False, False
        background = bool(monitor.get('completed_on_request') is True
                          and monitor['samples'] > 0
                          and monitor['foreground_samples'] == 0
                          and monitor['wechat_activation_events'] == 0
                          and monitor['missing_samples'] == 0
                          and first.get('exists') is True
                          and first.get('wechat_foreground') is False
                          and final.get('exists') is True
                          and final.get('wechat_foreground') is False)
        minimized = bool(background and first.get('minimized') is True
                         and final.get('minimized') is True
                         and monitor['minimized_samples'] == monitor['samples']
                         and monitor['minimize_end_events'] == 0)
        return background, minimized
    except (KeyError, TypeError, AttributeError):
        return False, False

def winpath(path):
    """Map mounted-drive or WSL-hosted helper paths without shell interpolation."""
    value = str(path.resolve())
    if re.match(r'^/mnt/[a-z]/', value):
        return value[5].upper() + ':\\' + value[7:].replace('/', '\\')
    result = subprocess.run(['wslpath', '-w', value], capture_output=True,
                            text=True, check=True, timeout=5)
    converted = result.stdout.strip()
    if not converted.startswith('\\\\'):
        raise ValueError('Windows mapping is unavailable for this helper path')
    return converted


def unpack_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.startswith(b'(\xb5/\xfd'):
        lib = zstd_library()
        if lib is None:
            return None
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        target = ctypes.create_string_buffer(8 * 1024 * 1024)
        n = lib.ZSTD_decompress(target, len(target), value, len(value))
        if lib.ZSTD_isError(n):
            return None
        value = target.raw[:n]
    try:
        return value.decode('utf-8').strip('\x00')
    except UnicodeDecodeError:
        return None

def protobuf_fields(data):
    """Parse protobuf wire fields without searching arbitrary binary strings."""
    if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
        raise ValueError('Invalid packed image metadata')

    def integer(pos):
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError('Truncated protobuf integer')
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return (value, pos)
        raise ValueError('Oversized protobuf integer')
    pos = 0
    fields = []
    while pos < len(data):
        tag, pos = integer(pos)
        field, wire = (tag >> 3, tag & 7)
        if not 0 < field < 1 << 29:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            value, pos = integer(pos)
        elif wire in (1, 5):
            size = {1: 8, 5: 4}[wire]
            value = data[pos:pos + size]
            pos += size
        elif wire == 2:
            size, pos = integer(pos)
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError('Unsupported protobuf wire type')
        if pos > len(data):
            raise ValueError('Truncated protobuf value')
        fields.append((field, wire, value))
    return fields

def attachment_name(packed):
    """Read the observed WeChat 4.x ImageInfo.filename field, protobuf path 3.4.

    Schema evidence: weixin-omni/omni-bot-sdk-oss packed_info_data_img2.proto.
    The same field was checked against a local 4.1.15.55 image attachment.
    Unknown layouts and duplicate fields fail explicitly; XML md5 and other
    hashes are not interchangeable with this local cache filename.
    """
    outer = [value for field, wire, value in protobuf_fields(packed) if field == 3 and wire == 2]
    if len(outer) != 1:
        raise ValueError('Unsupported image metadata layout')
    names = [value for field, wire, value in protobuf_fields(outer[0]) if field == 4 and wire == 2]
    if len(names) != 1 or not re.fullmatch(b'[0-9a-fA-F]{32}', names[0]):
        raise ValueError('Missing or ambiguous local image filename')
    return names[0].decode().lower()

def decrypt_image(data, config, account):
    if not data:
        raise ValueError('Empty local image')
    if data.startswith(b'\x07\x08V2\x08\x07'):
        if config is None:
            raise MissingImageKeyError('V2 image requires image_config')
        if len(data) < 31:
            raise ValueError('Truncated V2 image')
        aes_size, xor_size = struct.unpack_from('<II', data, 6)
        block = (aes_size // 16 + 1) * 16
        if 15 + block > len(data) - xor_size:
            raise ValueError('Invalid image segment lengths')
        key = hashlib.md5((str(config) + account).encode()).hexdigest()[:16].encode()
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        first = dec.update(data[15:15 + block]) + dec.finalize()
        padding = first[-1]
        if not 1 <= padding <= 16 or first[-padding:] != bytes([padding]) * padding:
            raise ValueError('Image padding/key validation failed')
        first = first[:-padding]
        if len(first) != aes_size:
            raise ValueError('Image AES length mismatch')
        end = len(data) - xor_size
        return first + data[15 + block:end] + bytes((b ^ config & 255 for b in data[end:]))
    if data.startswith(b'\x07\x08\x05V\x02\x05'):
        if config is None:
            raise MissingImageKeyError('This image requires image_config')
        return bytes((b ^ config & 255 for b in data[22:]))
    for signature in (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF89a'):
        candidate = data[0] ^ signature[0]
        if bytes((b ^ candidate for b in data[:len(signature)])) == signature:
            return bytes((b ^ candidate for b in data))
    raise ValueError('Unsupported local image format')

def decode_image(data):
    converted = False
    if data.startswith(b'wxgf'):
        pos = data.find(b'\x00\x00\x00\x01')
        if pos < 0:
            raise ValueError('Missing HEVC stream')
        r = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'hevc', '-i', 'pipe:0', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], input=data[pos:], capture_output=True, timeout=30)
        if r.returncode or not r.stdout:
            raise ValueError('HEVC image decode failed')
        data = r.stdout
        converted = True
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        size = list(im.size)
        fmt = im.format.lower()
    ext = {'jpeg': 'jpg', 'png': 'png', 'gif': 'gif', 'webp': 'webp'}.get(fmt)
    if not ext:
        raise ValueError('Unsupported decoded image type')
    return (data, ext, size, converted)

def read_target(args, secret, *, path_formatter=None, timezone=TZ, image_decoder=None):
    # Windows keeps its existing defaults; macOS supplies only these adapters.
    path_formatter = path_formatter or winpath
    image_decoder = image_decoder or (lambda data: decrypt_image(data, secret['image_config'], args.account))
    root = args.account_dir / 'db_storage'
    keys = {name: bytes.fromhex(value) for name, value in secret['keys'].items()}
    stats = {}
    with open_db(root / 'contact/contact.db', keys['contact/contact.db']) as db:
        matches = db.execute('SELECT username,nick_name,remark FROM contact WHERE nick_name=? OR remark=?', (args.contact, args.contact)).fetchall()
    stats['contact/contact.db'] = dict(db.stats)
    if not matches:
        raise ContactScopeError('contact_not_found')
    if len(matches) != 1:
        raise ContactScopeError('contact_ambiguous')
    username, nickname, remark = matches[0]
    if not isinstance(username, str) or '@chatroom' in username:
        raise ContactScopeError('individual_contact_required')
    table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
    allrows = []
    image_rows = []
    found_shards = []
    for rel in sorted(keys):
        if not re.fullmatch('message/message_\\d+\\.db', rel):
            continue
        with open_db(root / rel, keys[rel]) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                found_shards.append(rel)
                columns = 'local_id,server_id,local_type,real_sender_id,create_time,sort_seq,CAST(message_content AS BLOB),CAST(packed_info_data AS BLOB)'
                rawrows = db.execute(f'SELECT {columns} FROM "{table}" ORDER BY create_time DESC,sort_seq DESC LIMIT ?', (args.limit,)).fetchall()
                rawimages = db.execute(f'SELECT {columns} FROM "{table}" WHERE (local_type & 4294967295)=3 ORDER BY create_time DESC,sort_seq DESC LIMIT ?', (args.images,)).fetchall()
                for rows, dst in ((rawrows, allrows), (rawimages, image_rows)):
                    for row in rows:
                        item = dict(zip(('local_id', 'server_id', 'type', 'sender_id', 'timestamp', 'sort_seq', 'content', 'packed'), row))
                        sender = db.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (item['sender_id'],)).fetchone()
                        item['sender'] = 'contact' if sender and sender[0] == username else 'self' if sender and sender[0] == args.account else 'unresolved'
                        item['shard'] = rel
                        dst.append(item)
        stats[rel] = dict(db.stats)
    allrows = sorted(allrows, key=lambda r: (r['timestamp'], r['sort_seq']), reverse=True)[:args.limit]
    image_rows = sorted(image_rows, key=lambda r: (r['timestamp'], r['sort_seq']), reverse=True)[:args.images]
    result = {'contact': args.contact, 'matched_name': remark or nickname, 'username': username, 'shards': found_shards, 'messages': [], 'images': [], 'database_stats': stats}
    for row in allrows:
        item = {k: row[k] for k in ('shard', 'local_id', 'server_id', 'type', 'sender', 'timestamp')}
        item['datetime'] = datetime.fromtimestamp(row['timestamp'], timezone).isoformat()
        item['text'] = unpack_text(row['content']) if row['type'] & 4294967295 == 1 else None
        result['messages'].append(item)
    folder = args.output
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    attach = args.account_dir / 'msg' / 'attach' / table[4:]
    for index, row in enumerate(image_rows, 1):
        meta = {k: row[k] for k in ('shard', 'local_id', 'server_id', 'sender', 'timestamp')}
        meta['datetime'] = datetime.fromtimestamp(row['timestamp'], timezone).isoformat()
        date_dir = datetime.fromtimestamp(row['timestamp'], timezone).strftime('%Y-%m')
        try:
            digest = attachment_name(row['packed'])
        except ValueError:
            meta['status'] = 'unsupported_attachment_metadata'
            result['images'].append(meta)
            continue
        hits = []
        for suffix, tier in (('_h.dat', 'local_h'), ('.dat', 'local_regular'), ('_t.dat', 'thumbnail')):
            path = attach / date_dir / 'Img' / (digest + suffix)
            if path.is_file():
                hits.append((digest, tier, path))
        if not hits:
            meta['status'] = 'missing_local_file'
            result['images'].append(meta)
            continue
        order = {'local_h': 0, 'local_regular': 1, 'thumbnail': 2}
        hits.sort(key=lambda hit: order[hit[1]])
        attempts = []
        for digest, tier, path in hits:
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(args.account_dir.resolve()):
                    raise ValueError('Image path escapes the account or is a symlink')
                before = path.stat()
                if before.st_size > 64 * 1024 * 1024:
                    raise ValueError('Image exceeds bounded read size')
                encrypted = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('Image changed during read')
                decrypted = image_decoder(encrypted)
                image, ext, size, converted = decode_image(decrypted)
                output = folder / f'image_{index:02d}.{ext}'
                output.write_bytes(image)
                meta.update(status='decoded', association='packed_info_field_3_4', tier=tier, size=size, converted=converted, path=path_formatter(output), source=path_formatter(path), sha256=hashlib.sha256(image).hexdigest())
                break
            except (ValueError, OSError, subprocess.TimeoutExpired, Image.DecompressionBombError) as exc:
                attempts.append({'tier': tier, 'error': type(exc).__name__})
        if 'status' not in meta:
            meta['status'] = ('missing_image_key' if attempts and all(
                item['error'] == 'MissingImageKeyError' for item in attempts) else 'decode_failed')
        if attempts:
            meta['failed_tiers'] = attempts
        result['images'].append(meta)
    return result

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-dir', type=pathlib.Path, required=True)
    p.add_argument('--account', required=True)
    p.add_argument('--contact', required=True)
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--hwnd', type=int, required=True)
    p.add_argument('--windows-python', required=True,
                   help='WSL path to a 64-bit Windows Python executable')
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--images', type=int, default=8)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--require-minimized', action='store_true')
    args = p.parse_args()
    if args.output.resolve().is_relative_to(args.account_dir.resolve()):
        raise ValueError('Output must be outside the source account')
    if args.output.resolve().is_relative_to(HERE.parent):
        raise ValueError('Output must be outside the skill repository')
    if args.output.exists():
        raise ValueError('Output directory already exists')
    if not 1 <= args.limit <= 100 or not 1 <= args.images <= 20:
        raise ValueError('Query bounds exceeded')
    observer, initial = start_observer([args.windows_python, winpath(HERE / '_window_observer.py'), str(args.pid), str(args.hwnd)])
    result = None
    try:
        if args.require_minimized and (not initial['initial']['minimized'] or initial['initial']['wechat_foreground']):
            raise RuntimeError('WeChat must already be minimized and in the background')
        relatives = ['contact/contact.db'] + [str(p.relative_to(args.account_dir / 'db_storage')) for p in sorted((args.account_dir / 'db_storage' / 'message').glob('message_[0-9]*.db')) if re.fullmatch('message_\\d+\\.db', p.name)]
        r = subprocess.run([args.windows_python, winpath(HERE / '_windows_keys.py'), str(args.pid), args.account, winpath(args.account_dir / 'db_storage')], input=json.dumps({'databases': relatives}).encode(), capture_output=True, timeout=110)
        secret = json.loads(r.stdout)
        if r.returncode or secret.get('account') != args.account or set(secret.get('keys', {})) != set(relatives):
            raise RuntimeError('Not all requested database keys validated; do not report complete coverage')
        for attempt in range(2):
            try:
                result = read_target(args, secret)
                break
            except SourceChangedError:
                if attempt:
                    raise
    finally:
        monitor = stop_observer(observer)
    if result is not None:
        result['background_observation'] = {'initial': initial, 'report': monitor}
        result['background_verified'], result['minimized_verified'] = observation_flags(initial, monitor)
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'ok': True, 'background_verified': result['background_verified'], 'minimized_verified': result['minimized_verified'], 'messages': len(result['messages']), 'images': [{'status': r['status'], 'size': r.get('size'), 'tier': r.get('tier')} for r in result['images']], 'shards': result['shards'], 'observation': result['background_observation'], 'result': winpath(args.output / 'result.json')}, ensure_ascii=False))
if __name__ == '__main__':
    main()
