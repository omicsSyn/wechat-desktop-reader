"""Synthetic main-entrypoint regressions; no live WeChat or helper execution.

Adapted from the independent release review probes; original retained separately.
"""
import contextlib, ctypes, hashlib, io, json, pathlib, sys, tempfile, types, unittest
from unittest.mock import patch
import apsw
from PIL import Image
import local_reader as lr
from test_page_reader import KEY, encrypt
from test_local_reader import field
from test_observer import INITIAL, REPORT

class LocalPipelineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.account = self.root / 'synthetic_account_suffix'
        self.output = self.root / 'output'
        self.contact = 'synthetic_contact'
        self.username = 'synthetic_peer'
        self.table = 'Msg_' + hashlib.md5(self.username.encode()).hexdigest()
        self.digest = 'a' * 32
        self.args = ['local_reader.py', '--account-dir', str(self.account), '--account', 'synthetic_account', '--contact', self.contact, '--pid', '42', '--hwnd', '99', '--windows-python', 'NOT_EXECUTED', '--limit', '2', '--images', '1', '--require-minimized', '--output', str(self.output)]
        self.make_db('contact/contact.db', ['CREATE TABLE contact(username,nick_name,remark)', ('INSERT INTO contact VALUES(?,?,?)', (self.username, self.contact, ''))])
        cols = 'local_id,server_id,local_type,real_sender_id,create_time,sort_seq,message_content,packed_info_data'
        for i, rows in enumerate([[(1, 101, 1, 1, 1700000400, 1, 'hello'.encode(), b''), (2, 102, 3, 1, 1700000000, 2, b'', field(3, field(4, self.digest.encode()) + field(5, b'b' * 32)))], [(3, 103, 1, 2, 1700000300, 3, '你好'.encode(), b''), (4, 104, 3, 1, 1600000000, 4, b'', b'broken')]]):
            self.make_db(f'message/message_{i}.db', [f'CREATE TABLE "{self.table}"({cols})', 'CREATE TABLE Name2Id(user_name)', ('INSERT INTO Name2Id VALUES(?)', (self.username,)), ('INSERT INTO Name2Id VALUES(?)', ('synthetic_account',))] + [(f'INSERT INTO "{self.table}" VALUES(?,?,?,?,?,?,?,?)', r) for r in rows])
        month = lr.datetime.fromtimestamp(1700000000, lr.TZ).strftime('%Y-%m')
        self.attach = self.account / 'msg' / 'attach' / self.table[4:] / month / 'Img'
        self.attach.mkdir(parents=True)
        b = io.BytesIO()
        Image.new('RGB', (5, 3), (10, 20, 30)).save(b, format='PNG')
        self.png = b.getvalue()
        (self.attach / (self.digest + '_h.dat')).write_bytes(b'bad image')
        (self.attach / (self.digest + '.dat')).write_bytes(bytes((x ^ 123 for x in self.png)))
        (self.attach / ('b' * 32 + '.dat')).write_bytes(bytes((x ^ 123 for x in self.png)))
        self.keys = {str(p.relative_to(self.account / 'db_storage')): KEY.hex() for p in (self.account / 'db_storage').rglob('*.db')}
        self.secret = {'account': 'synthetic_account', 'image_config': 123, 'keys': self.keys}
        self.before = self.hashes()

    def hashes(self):
        return {str(p.relative_to(self.account)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.account.rglob('*') if p.is_file()}

    def make_db(self, rel, statements):
        c = apsw.Connection(':memory:')
        r = ctypes.c_int(80)
        c.filecontrol('main', apsw.SQLITE_FCNTL_RESERVE_BYTES, ctypes.addressof(r))
        c.execute('PRAGMA page_size=4096')
        for s in statements:
            c.execute(*s) if isinstance(s, tuple) else c.execute(s)
        p = self.account / 'db_storage' / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encrypt(c.serialize('main')))
        c.close()

    def run_main(self, source_changed=False, secret=None):
        stdout = io.StringIO()
        real_read = lr.read_target
        calls = []

        def reader(*a):
            calls.append(1)
            if source_changed and len(calls) == 1:
                raise lr.SourceChangedError('synthetic change')
            return real_read(*a)
        with patch.object(sys, 'argv', self.args), patch.object(lr, 'winpath', lambda p: str(p.resolve())), patch.object(lr, 'start_observer', return_value=(object(), INITIAL)), patch.object(lr, 'stop_observer', return_value=REPORT) as stop, patch.object(lr.subprocess, 'run', return_value=types.SimpleNamespace(stdout=json.dumps(secret or self.secret).encode(), returncode=0)) as child, patch.object(lr, 'read_target', side_effect=reader), contextlib.redirect_stdout(stdout):
            lr.main()
        self.assertEqual(stop.call_count, 1)
        self.assertTrue(child.call_args.kwargs['capture_output'])
        self.assertEqual(child.call_args.kwargs['timeout'], 110)
        return (stdout.getvalue(), calls)

    def test_full_main_two_shards_and_fallback(self):
        stdout, calls = self.run_main()
        result = json.loads((self.output / 'result.json').read_text())
        self.assertEqual([m['server_id'] for m in result['messages']], [101, 103])
        self.assertEqual([m['sender'] for m in result['messages']], ['contact', 'self'])
        self.assertEqual([m['text'] for m in result['messages']], ['hello', '你好'])
        self.assertEqual([m['server_id'] for m in result['images']], [102])
        im = result['images'][0]
        self.assertEqual((im['status'], im['tier'], im['size']), ('decoded', 'local_regular', [5, 3]))
        self.assertEqual(im['failed_tiers'][0]['tier'], 'local_h')
        self.assertEqual((self.output / 'image_01.png').read_bytes(), self.png)
        self.assertEqual(self.hashes(), self.before)
        self.assertNotIn(KEY.hex(), stdout)
        self.assertNotIn(KEY.hex(), (self.output / 'result.json').read_text())
        self.assertNotIn('hello', stdout)
        self.assertEqual(set((p.name for p in self.output.iterdir())), {'image_01.png', 'result.json'})

    def test_source_change_retries_once_before_writing(self):
        stdout, calls = self.run_main(True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.hashes(), self.before)

    def test_missing_requested_image_does_not_substitute_decoy(self):
        for suffix in ['_h.dat', '.dat']:
            (self.attach / (self.digest + suffix)).unlink()
        self.run_main()
        result = json.loads((self.output / 'result.json').read_text())
        self.assertEqual(result['images'][0]['status'], 'missing_local_file')
        self.assertEqual(list((p.name for p in self.output.iterdir())), ['result.json'])

    def test_output_in_account_or_repository_blocked_before_child(self):
        for target in [self.account / 'bad_output', pathlib.Path(lr.HERE.parent) / 'BAD_PROBE_OUTPUT']:
            self.args[-1] = str(target)
            with patch.object(sys, 'argv', self.args), patch.object(lr, 'start_observer') as start, self.assertRaises(ValueError):
                lr.main()
            start.assert_not_called()
            self.assertFalse(target.exists())

    def test_symlink_into_account_blocked(self):
        link = self.root / 'link'
        link.symlink_to(self.account, target_is_directory=True)
        self.args[-1] = str(link / 'bad_output')
        with patch.object(sys, 'argv', self.args), patch.object(lr, 'start_observer') as start, self.assertRaises(ValueError):
            lr.main()
        start.assert_not_called()

    def test_malformed_child_missing_keys_is_fail_closed(self):
        secret = dict(self.secret)
        secret['keys'] = {}
        with self.assertRaises(RuntimeError):
            self.run_main(secret=secret)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.hashes(), self.before)

    def test_pixel_limit_failure_falls_back_to_thumbnail(self):
        (self.attach / (self.digest + '_h.dat')).write_bytes(bytes((x ^ 123 for x in self.png)))
        thumb = io.BytesIO()
        Image.new('RGB', (1, 1)).save(thumb, format='PNG')
        (self.attach / (self.digest + '_t.dat')).write_bytes(bytes((x ^ 123 for x in thumb.getvalue())))
        before = self.hashes()
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 1):
            self.run_main()
        result = json.loads((self.output / 'result.json').read_text())
        image = result['images'][0]
        self.assertEqual((image['status'], image['tier'], image['size']), ('decoded', 'thumbnail', [1, 1]))
        self.assertEqual(image['failed_tiers'], [{'tier': 'local_h', 'error': 'DecompressionBombError'}, {'tier': 'local_regular', 'error': 'DecompressionBombError'}])
        self.assertEqual(len(result['messages']), 2)
        self.assertEqual(self.hashes(), before)

    def test_pixel_limit_all_fail_preserves_result(self):
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 1):
            self.run_main()
        result = json.loads((self.output / 'result.json').read_text())
        image = result['images'][0]
        self.assertEqual(image['status'], 'decode_failed')
        self.assertEqual(image['failed_tiers'][-1], {'tier': 'local_regular', 'error': 'DecompressionBombError'})
        self.assertEqual(len(result['messages']), 2)
        self.assertEqual({p.name for p in self.output.iterdir()}, {'result.json'})
        self.assertEqual(self.hashes(), self.before)
if __name__ == '__main__':
    unittest.main(verbosity=2)
