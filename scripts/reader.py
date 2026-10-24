#!/usr/bin/env python3
"""Read and navigate WeChat screenshots using Windows PowerShell."""
import argparse
import base64
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import uuid


def local_path(value):
    if len(value) > 1 and value[1] == ':' and not re.match(r'^[A-Za-z]:[\\/]', value):
        raise ValueError('Drive-relative Windows paths are unsupported')
    if os.name != 'nt' and value.startswith('\\\\'):
        raise ValueError('Use an absolute drive-letter path, not a UNC path')
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        return Path('/mnt') / value[0].lower() / value[3:].replace('\\', '/')
    return Path(value)


def region(value):
    try:
        parts = [int(x) for x in value.split(',')]
        if len(parts) != 4 or min(parts[:2]) < 0 or min(parts[2:]) <= 0:
            raise ValueError
        return parts
    except ValueError:
        raise argparse.ArgumentTypeError('Expected x,y,width,height with positive size')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    capture = commands.add_parser('capture')
    capture.add_argument('--window', required=True)
    capture.add_argument('--chat', required=True)
    capture.add_argument('--session')
    capture.add_argument('--guard', type=region)
    for name in ('click', 'scroll', 'search-text'):
        action = commands.add_parser(name)
        action.add_argument('--snapshot', required=True)
        if name in ('click', 'scroll'):
            action.add_argument('--x', required=True, type=int)
            action.add_argument('--y', required=True, type=int)
        if name == 'click':
            action.add_argument('--double', action='store_true')
        if name == 'scroll':
            action.add_argument('--steps', required=True, type=int)
        if name == 'search-text':
            action.add_argument('--text', required=True)
    args = vars(parser.parse_args())
    if args['action'] == 'search-text' and (not args['text'] or any(ord(c) < 32 for c in args['text'])):
        parser.error('Search text must be a nonempty line without control characters')
    if args['action'] == 'scroll' and not 1 <= abs(args['steps']) <= 8:
        parser.error('Use 1 to 8 scroll steps per inspected action')
    if args.get('snapshot'):
        args['expected'] = json.loads(local_path(args['snapshot']).read_text(encoding='utf-8'))
    args['capture_id'] = uuid.uuid4().hex[:16]
    payload = base64.b64encode(json.dumps(args, ensure_ascii=False).encode()).decode()
    code = "$payload='" + payload + "'\n" + Path(__file__).with_name('windows.ps1').read_text(encoding='utf-8')
    executable = 'powershell.exe' if os.name == 'nt' else '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand', base64.b64encode(code.encode('utf-16le')).decode()], capture_output=True, timeout=30)
    stdout = result.stdout.decode('utf-8-sig', errors='replace').strip()
    if not stdout:
        raise RuntimeError('No PowerShell result: ' + result.stderr.decode('utf-8', errors='replace')[:500])
    output = json.loads(stdout)
    if result.returncode or not output.get('ok'):
        print(json.dumps(output, ensure_ascii=False))
        return 1
    if output.get('image'):
        try:
            output['local_image'] = str(local_path(output['image']))
            manifest = local_path(output['session']) / 'captures.jsonl'
            duplicate = None
            if manifest.exists():
                for line in manifest.read_text(encoding='utf-8').splitlines():
                    record = json.loads(line)
                    if record.get('image_hash') == output['image_hash']:
                        duplicate = record['image']
            output['identical_capture'] = duplicate
            local_path(output['snapshot']).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
            with manifest.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(output, ensure_ascii=False) + '\n')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            output.update(ok=False, error='Image captured but metadata write failed: ' + str(exc))
            print(json.dumps(output, ensure_ascii=False))
            return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
