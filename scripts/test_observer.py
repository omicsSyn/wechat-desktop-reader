"""Observer protocol tests use disposable Python children, never WeChat."""
import copy
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import local_reader


INITIAL = {'ready': True, 'initial': {
    'exists': True, 'minimized': True, 'wechat_foreground': False}}
REPORT = {'completed_on_request': True, 'samples': 3,
          'foreground_samples': 0, 'wechat_activation_events': 0,
          'missing_samples': 0, 'minimized_samples': 3, 'minimize_end_events': 0,
          'final': {'exists': True, 'minimized': True, 'wechat_foreground': False}}


class ObserverTests(unittest.TestCase):
    def test_ready_and_completion_lines_are_kept_separate(self):
        code = (f'import sys; print({json.dumps(INITIAL)!r}, flush=True); '
                f'sys.stdin.readline(); print({json.dumps(REPORT)!r}, flush=True)')
        process, initial = local_reader.start_observer([sys.executable, '-c', code])
        report = local_reader.stop_observer(process)
        self.assertEqual(initial, INITIAL)
        self.assertEqual(report, REPORT)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(local_reader.observation_flags(initial, report), (True, True))

    def test_startup_failures_reap_the_owned_process(self):
        programs = [
            'import time; time.sleep(30)',
            "print('not-json', flush=True)",
            "print('[]', flush=True)",
            "print('{}', flush=True)",
            "print('x' * 17000, flush=True)",
            'raise SystemExit(1)',
        ]
        real_popen = subprocess.Popen
        for code in programs:
            owned = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                owned.append(process)
                return process

            with self.subTest(code=code), patch.object(local_reader.subprocess, 'Popen', launch):
                with self.assertRaises((RuntimeError, ValueError)):
                    local_reader.start_observer([sys.executable, '-c', code],
                                                timeout=0.2, stop_timeout=0.2)
                self.assertIsNotNone(owned[0].poll())
                self.assertTrue(owned[0].stdout.closed)

    def test_hung_shutdown_is_bounded_and_never_verified(self):
        code = (f'import time; print({json.dumps(INITIAL)!r}, flush=True); '
                'time.sleep(30)')
        process, initial = local_reader.start_observer([sys.executable, '-c', code])
        start = time.monotonic()
        report = local_reader.stop_observer(process, timeout=0.2)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNotNone(process.poll())
        self.assertEqual(local_reader.observation_flags(initial, report), (False, False))

    def test_incomplete_or_foreground_observation_is_not_verified(self):
        for key, value in [('completed_on_request', False), ('samples', 0),
                           ('foreground_samples', 1), ('wechat_activation_events', 1),
                           ('missing_samples', 1), ('samples', '3')]:
            report = copy.deepcopy(REPORT)
            report[key] = value
            with self.subTest(key=key):
                self.assertEqual(local_reader.observation_flags(INITIAL, report), (False, False))
        self.assertEqual(local_reader.observation_flags(INITIAL, {}), (False, False))
        self.assertEqual(local_reader.observation_flags({}, REPORT), (False, False))

    def test_restore_is_distinct_from_foreground_activation(self):
        for mutation in ('minimize_end_events', 'minimized_samples', 'final'):
            report = copy.deepcopy(REPORT)
            if mutation == 'final':
                report['final']['minimized'] = False
            else:
                report[mutation] = 1
            self.assertEqual(local_reader.observation_flags(INITIAL, report), (True, False))


if __name__ == '__main__':
    unittest.main()
