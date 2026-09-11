"""Real CLI checks of W5 T1/T2; every invocation retains its output separately."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestW5Revision(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("WB2_SELFTEST_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.output = Path(tempfile.mkdtemp(prefix="w5-timer-", dir=parent))

    def cli(self, source):
        suite_dir = self.output / "suite"
        suite_dir.mkdir()
        (suite_dir / "test_sample.py").write_text(textwrap.dedent(source), encoding="utf-8")
        proc = subprocess.run(
            check=False,
            args=[sys.executable, "-m", "qa.wb2.wb2_timed", "--label", "sample",
             "--mode", "discover", "--start-dir", str(suite_dir),
             "--out-dir", str(self.output / "results")], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        (self.output / "timer.stdout.log").write_text(proc.stdout, encoding="utf-8")
        (self.output / "timer.stderr.log").write_text(proc.stderr, encoding="utf-8")
        meta = json.loads((self.output / "results/sample.meta.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (self.output / "results/sample.jsonl").read_text(encoding="utf-8").splitlines()]
        full = (self.output / "results/sample.full-errors.log").read_text(encoding="utf-8")
        reference = subprocess.run(
            check=False,
            args=[sys.executable, '-c',
             ('import unittest,json,sys; '
             's=unittest.TestLoader().discover(sys.argv[1]); '
             'r=unittest.TextTestRunner(verbosity=2).run(s); '
             'print(json.dumps(dict(testsRun=r.testsRun,failures=len(r.failures),errors=len(r.errors),'
             'skipped=len(r.skipped),expected_failures=len(r.expectedFailures),'
             'unexpected_successes=len(r.unexpectedSuccesses),wasSuccessful=r.wasSuccessful())))'),
             str(suite_dir)], cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', timeout=30,
        )
        (self.output / 'reference.stdout.log').write_text(reference.stdout, encoding='utf-8')
        (self.output / 'reference.stderr.log').write_text(reference.stderr, encoding='utf-8')
        self.assertEqual(reference.returncode, 0, reference.stderr)
        expected = json.loads(reference.stdout)
        for key, value in expected.items():
            self.assertEqual(meta[key], value, key)
        return proc, meta, rows, full

    def test_all_subtests_skipped_is_not_validated_cli(self):
        proc, meta, rows, _ = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                def test_sample(self):
                    for i in range(2):
                        with self.subTest(i=i):
                            self.skipTest('reason-' + str(i))
        ''')
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(meta['run_status'], 'NOT_VALIDATED')
        self.assertEqual(rows[0]['outcome'], 'ALL_SUBTESTS_SKIPPED')
        self.assertEqual([s['skip_reason'] for s in rows[0]['subtests']], ['reason-0', 'reason-1'])

    def test_long_subtest_traceback_is_complete_cli(self):
        proc, _, rows, full = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                def test_sample(self):
                    for i in range(2):
                        with self.subTest(i=i):
                            self.fail('x' * 6000 + 'END_OF_COMPLETE_SUBTEST_TRACE_' + str(i))
        ''')
        self.assertEqual(proc.returncode, 1)
        for i, subcase in enumerate(rows[0]['subtests']):
            self.assertIn('END_OF_COMPLETE_SUBTEST_TRACE_' + str(i), full)
            self.assertIn(subcase['subtest_id'], full)
            self.assertLessEqual(len(subcase['traceback_preview']), 2000)
        self.assertEqual(len({s['occurrence_id'] for s in rows[0]['subtests']}), 2)

    def test_mixed_pass_skip_preserves_reasons_and_standard_counts(self):
        proc, meta, rows, _ = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                def test_sample(self):
                    with self.subTest(i=1): self.skipTest('mixed reason')
                    with self.subTest(i=2): self.assertEqual(2, 2)
        ''')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(meta['methods']['PASS'], 1)
        self.assertEqual(meta['subcases']['skipped'], 1)
        self.assertEqual(rows[0]['subtests'][0]['skip_reason'], 'mixed reason')

    def test_repeated_subcase_params_have_unique_occurrence_and_full_errors(self):
        proc, meta, rows, full = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                def test_sample(self):
                    for i in range(3):
                        with self.subTest(label='same'):
                            if i == 0: self.fail('FAIL_END_' + 'a'*5000 + 'TAIL_FAIL')
                            if i == 1: raise ValueError('b'*5000 + 'TAIL_ERROR')
                            self.skipTest('same params skip')
        ''')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(meta['methods']['ERROR'], 1)
        subcases = rows[0]['subtests']
        self.assertEqual(len({s['occurrence_id'] for s in subcases}), 3)
        for token in ('TAIL_FAIL', 'TAIL_ERROR', 'same params skip'):
            self.assertIn(token, full)

    def test_xfail_xpass_and_method_errors_follow_standard_cli(self):
        proc, meta, _, full = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                @unittest.expectedFailure
                def test_expected(self): self.fail('EXPECTED_TAIL')
                @unittest.expectedFailure
                def test_unexpected(self): pass
                def test_error(self): raise RuntimeError('METHOD_ERROR_TAIL')
        ''')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(meta['expected_failures'], 1)
        self.assertEqual(meta['unexpected_successes'], 1)
        self.assertIn('EXPECTED_TAIL', full)
        self.assertIn('METHOD_ERROR_TAIL', full)

    def test_class_error_with_zero_tests_takes_priority(self):
        proc, meta, rows, full = self.cli('''
            import unittest
            class Sample(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise RuntimeError('CLASS_ERROR_TAIL')
                def test_sample(self): pass
        ''')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(meta['run_status'], 'FAILED')
        self.assertEqual(meta['testsRun'], 0)
        self.assertEqual(rows[0]['scope'], 'setUpClass')
        self.assertIn('CLASS_ERROR_TAIL', full)

    def test_module_error_has_scope_identity_and_full_traceback(self):
        proc, meta, rows, full = self.cli('''
            import unittest
            def setUpModule(): raise RuntimeError('MODULE_ERROR_TAIL')
            class Sample(unittest.TestCase):
                def test_sample(self): pass
        ''')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(meta['testsRun'], 0)
        self.assertEqual(rows[0]['scope'], 'setUpModule')
        self.assertIn('MODULE_ERROR_TAIL', full)

    def test_zero_tests_is_not_validated_cli(self):
        proc, meta, _, _ = self.cli('import unittest')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(meta['run_status'], 'NOT_VALIDATED')

    def runner_cli(self, argv, timeout):
        plan = self.output / 'plan.json'
        plan.write_text(json.dumps([{'label': 'control', 'argv': argv,
                                     'timeout': timeout}]), encoding='utf-8')
        ledger = self.output / 'commands.jsonl'
        proc = subprocess.run(
            check=False,
            args=[sys.executable, '-m', 'qa.wb2.wb2_run', '--tag', 'w5',
             '--plan', str(plan), '--cwd', str(ROOT),
             '--commands', str(ledger), '--log-dir', str(self.output / 'logs')],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        (self.output / 'runner.stdout.log').write_text(proc.stdout, encoding='utf-8')
        (self.output / 'runner.stderr.log').write_text(proc.stderr, encoding='utf-8')
        records = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(records[-1]['any_failed'])
        self.assertIsNone(records[0]['exit_code'])
        return records[0]

    def test_runner_missing_executable_real_cli(self):
        record = self.runner_cli([str(self.output / 'missing-executable')], 10)
        self.assertEqual(record['status'], 'RUNNER_ERROR')
        self.assertIn('FileNotFoundError', record['runner_error'])

    def test_runner_timeout_real_cli(self):
        record = self.runner_cli([sys.executable, '-c', 'import time; time.sleep(5)'], 0.1)
        self.assertEqual(record['status'], 'TIMEOUT')
        self.assertIn('TimeoutExpired', record['runner_error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
