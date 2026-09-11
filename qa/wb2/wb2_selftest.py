"""Self-tests for the WB2-W5 fixed timing recorder (wb2_timed.TimingResult).

Each scenario is run under BOTH the fixed TimingResult and a standard
``unittest.TextTestRunner`` (the reference behaviour of ``python -m unittest``) and
asserted to agree. Expected outcomes come from the frozen W5 spec, not from the
tool's own output.

Run:
    py -3.14 qa/wb2/wb2_selftest.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wb2_timed import TimingResult, classify_run


def _reference(case_cls: type) -> unittest.TestResult:
    suite = unittest.TestLoader().loadTestsFromTestCase(case_cls)
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
    return runner.run(suite)


def _run_timing(case_cls: type):
    suite = unittest.TestLoader().loadTestsFromTestCase(case_cls)
    sink: list = []
    result = TimingResult(sink)
    suite.run(result)
    return result, sink


def _by_method(sink: list) -> dict:
    return {row["method"]: row["outcome"] for row in sink if row["scope"] == "test"}


def _make(name: str, methods: dict) -> type:
    methods.setdefault("__module__", "wb2_selftest")
    return type(name, (unittest.TestCase,), methods)


class TestClassification(unittest.TestCase):
    def assert_scenario(self, case_cls: type, expected: dict, expect_successful: bool) -> None:
        result, sink = _run_timing(case_cls)
        ref = _reference(case_cls)
        by_method = _by_method(sink)
        self.assertEqual(by_method, expected, f"sink rows={sink}")
        self.assertEqual(result.wasSuccessful(), expect_successful)
        self.assertEqual(result.wasSuccessful(), ref.wasSuccessful())
        self.assertEqual(
            (len(result.failures), len(result.errors), len(result.skipped),
             len(result.expectedFailures), len(result.unexpectedSuccesses)),
            (len(ref.failures), len(ref.errors), len(ref.skipped),
             len(ref.expectedFailures), len(ref.unexpectedSuccesses)),
        )

    def test_01_pass(self):
        self.assert_scenario(_make("C1", {"test_pass": lambda s: None}),
                             {"test_pass": "PASS"}, True)

    def test_02_fail(self):
        self.assert_scenario(_make("C2", {"test_fail": lambda s: s.fail("boom")}),
                             {"test_fail": "FAIL"}, False)

    def test_03_error(self):
        def test_err(s):
            raise ValueError("unexpected error")
        self.assert_scenario(_make("C3", {"test_err": test_err}),
                             {"test_err": "ERROR"}, False)

    def test_04_skip(self):
        def test_skip(s):
            s.skipTest("reason")
        self.assert_scenario(_make("C4", {"test_skip": test_skip}),
                             {"test_skip": "SKIP"}, True)

    def test_05_xfail(self):
        @unittest.expectedFailure
        def test_xfail(s):
            s.fail("expected")
        self.assert_scenario(_make("C5", {"test_xfail": test_xfail}),
                             {"test_xfail": "EXPECTED_FAILURE"}, True)

    def test_06_xpass(self):
        @unittest.expectedFailure
        def test_xpass(s):
            return None
        self.assert_scenario(_make("C6", {"test_xpass": test_xpass}),
                             {"test_xpass": "UNEXPECTED_SUCCESS"}, False)

    def test_07_subtest_failure(self):
        def test_subfail(s):
            for i in (1, 2):
                with s.subTest(i=i):
                    if i == 2:
                        s.fail(f"subtest {i}")
        case = _make("C7", {"test_subfail": test_subfail})
        result, sink = _run_timing(case)
        ref = _reference(case)
        self.assertEqual(_by_method(sink), {"test_subfail": "FAIL"})
        self.assertFalse(result.wasSuccessful())
        # WB2-03: no double count vs standard unittest
        self.assertEqual(len(result.failures), len(ref.failures))
        self.assertEqual(len(result.errors), len(ref.errors))
        row = next(r for r in sink if r["scope"] == "test")
        self.assertTrue(any(st["outcome"] == "FAIL" for st in row["subtests"]))
        self.assertTrue(any(st["outcome"] == "PASS" for st in row["subtests"]))
        self.assertTrue(row["subtests"][0]["parent_id"].endswith("test_subfail"))

    def test_08_subtest_error(self):
        def test_suberr(s):
            with s.subTest(i=1):
                raise ValueError("sub error")
        case = _make("C8", {"test_suberr": test_suberr})
        result, sink = _run_timing(case)
        ref = _reference(case)
        self.assertEqual(_by_method(sink), {"test_suberr": "ERROR"})
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), len(ref.errors))
        row = next(r for r in sink if r["scope"] == "test")
        self.assertEqual(row["subtests"][0]["outcome"], "ERROR")

    def test_09_subtest_pass(self):
        def test_subpass(s):
            for i in (1, 2):
                with s.subTest(i=i):
                    pass
        self.assert_scenario(_make("C9", {"test_subpass": test_subpass}),
                             {"test_subpass": "PASS"}, True)

    def test_09b_subtest_identity_keeps_params(self):
        def test_case(s):
            for label in ("a", "b"):
                with s.subTest(label=label):
                    s.assertEqual(1, 1)
        _result, sink = _run_timing(_make("C9b", {"test_case": test_case}))
        row = next(r for r in sink if r["scope"] == "test")
        ids = {st["subtest_id"] for st in row["subtests"]}
        self.assertEqual(len(ids), 2, "subtest ids must be distinct")
        for st in row["subtests"]:
            self.assertIn("label", st["params"])
            self.assertIn(st["params"]["label"], st["subtest_id"])

    def test_09c_skip_plus_pass_subtest_is_ok(self):
        def test_case(s):
            for label in ("skipped", "pass"):
                with s.subTest(label=label):
                    if label == "skipped":
                        s.skipTest("synthetic skip")
                    s.assertEqual(2, 2)
        result, sink = _run_timing(_make("C9c", {"test_case": test_case}))
        # parent must NOT be marked SKIP; the run has a real success branch
        self.assertEqual(_by_method(sink), {"test_case": "PASS"})
        self.assertTrue(result.wasSuccessful())
        row = next(r for r in sink if r["scope"] == "test")
        outcomes = sorted(st["outcome"] for st in row["subtests"])
        self.assertEqual(outcomes, ["PASS", "SKIP"])
        status, code = classify_run(result.testsRun, successes=1, failures=0, errors=0,
                                    unexpected_successes=0, skipped=0, was_successful=True)
        self.assertEqual((status, code), ("OK", 0))

    def test_10_setup_failure(self):
        cls = _make("C10", {"test_x": lambda s: None})
        cls.setUp = lambda s: (_ for _ in ()).throw(ValueError("setUp boom"))  # type: ignore[assignment]
        self.assert_scenario(cls, {"test_x": "ERROR"}, False)

    def test_11_teardown_failure(self):
        cls = _make("C11", {"test_x": lambda s: None})
        cls.tearDown = lambda s: (_ for _ in ()).throw(ValueError("tearDown boom"))  # type: ignore[assignment]
        result, sink = _run_timing(cls)
        self.assertEqual(_by_method(sink), {"test_x": "ERROR"})
        self.assertFalse(result.wasSuccessful())


class TestFixtureScopeErrors(unittest.TestCase):
    def test_setupClass_failure_scope_row(self):
        cls = _make("C12", {"test_x": lambda s: None})

        @classmethod
        def bad_setup(k):
            raise ValueError("setUpClass boom")
        cls.setUpClass = bad_setup  # type: ignore[assignment]

        result, sink = _run_timing(cls)
        scope_rows = [r for r in sink if r["scope"] != "test"]
        self.assertTrue(scope_rows, "expected a scope row for setUpClass failure")
        row = scope_rows[0]
        self.assertEqual(row["outcome"], "ERROR")
        self.assertIn("setUpClass", row["test_id"])
        # no test-method rows may be emitted for a class whose setUpClass failed
        self.assertEqual(_by_method(sink), {})
        self.assertFalse(result.wasSuccessful())
        self.assertTrue(result.full_records, "full traceback must be captured")
        # WB2-03: failure takes priority over zero-tests (testsRun==0, errors==1)
        status, code = classify_run(result.testsRun, successes=0, failures=0, errors=1,
                                    unexpected_successes=0, skipped=0, was_successful=False)
        self.assertEqual((status, code), ("FAILED", 1))

    def test_tearDownClass_failure_scope_row(self):
        cls = _make("C13", {"test_x": lambda s: None})

        @classmethod
        def bad_teardown(k):
            raise ValueError("tearDownClass boom")
        cls.tearDownClass = bad_teardown  # type: ignore[assignment]

        result, sink = _run_timing(cls)
        scope_rows = [r for r in sink if r["scope"] != "test"]
        self.assertTrue(scope_rows)
        self.assertEqual(scope_rows[0]["outcome"], "ERROR")
        self.assertIn("tearDownClass", scope_rows[0]["test_id"])
        self.assertEqual(_by_method(sink), {"test_x": "PASS"})
        self.assertFalse(result.wasSuccessful())

    def test_module_fixture_error_no_active_test(self):
        # unittest reports setUpModule/setUpModule failures via a synthetic holder
        # that never goes through startTest/stopTest; exercise that exact path.
        from unittest.suite import _ErrorHolder

        holder = _ErrorHolder("setUpModule (some.module)")
        sink: list = []
        result = TimingResult(sink)
        try:
            raise ValueError("setUpModule boom")
        except ValueError:
            result.addError(holder, sys.exc_info())
        rows = [r for r in sink if r["scope"] != "test"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["outcome"], "ERROR")
        self.assertEqual(rows[0]["scope"], "setUpModule")
        self.assertIn("setUpModule boom", rows[0]["error_text"])


class TestRunClassification(unittest.TestCase):
    def test_zero_tests_not_validated(self):
        status, code = classify_run(0, successes=0, failures=0, errors=0,
                                    unexpected_successes=0, skipped=0, was_successful=True)
        self.assertEqual((status, code), ("NOT_VALIDATED", 2))

    def test_all_skip_not_validated(self):
        status, code = classify_run(3, successes=0, failures=0, errors=0,
                                    unexpected_successes=0, skipped=3, was_successful=True)
        self.assertEqual((status, code), ("NOT_VALIDATED", 2))

    def test_partial_skip_is_ok(self):
        status, code = classify_run(3, successes=2, failures=0, errors=0,
                                    unexpected_successes=0, skipped=1, was_successful=True)
        self.assertEqual((status, code), ("OK", 0))

    def test_xpass_is_failed(self):
        status, code = classify_run(1, successes=0, failures=0, errors=0,
                                    unexpected_successes=1, skipped=0, was_successful=False)
        self.assertEqual((status, code), ("FAILED", 1))


class TestFullTracebackSidecar(unittest.TestCase):
    def test_error_full_traceback_captured(self):
        def test_err(s):
            raise RuntimeError("full-traceback-probe")
        result, _sink = _run_timing(_make("C14", {"test_err": test_err}))
        self.assertTrue(result.full_records)
        rec = result.full_records[0]
        self.assertIn("full-traceback-probe", rec["full_text"])
        self.assertIn("RuntimeError", rec["full_text"])


class TestGateRunnerCLI(unittest.TestCase):
    """Real-CLI exit tests for wb2_run.py (WB2-01 / WB2-02)."""

    RUNNER = os.path.join(_HERE, "wb2_run.py")
    CLONE = os.path.abspath(os.path.join(_HERE, "..", ".."))

    def setUp(self):
        parent = os.environ.get("WB2_SELFTEST_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.output = tempfile.mkdtemp(prefix="wb2-selftest-", dir=parent)

    def _run(self, *argv, timeout=30):
        return subprocess.run(
            check=False,
            args=[sys.executable, "-B", self.RUNNER, *argv],
            cwd=self.CLONE, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )

    def test_01_unknown_builtin_is_nonzero(self):
        p = self._run("--builtin", "misspelled-gate", "--tag", "cli-unknown",
                      "--log-dir", os.path.join(self.output, "cli-logs"),
                      "--commands", os.path.join(self.output, "cli-commands.jsonl"),
                      "--raw-dir", os.path.join(self.output, "cli-raw"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("NOT_VALIDATED", p.stdout)

    def test_02_empty_plan_is_nonzero(self):
        p = self._run("--builtin", "", "--tag", "cli-empty",
                      "--log-dir", os.path.join(self.output, "cli-logs"),
                      "--commands", os.path.join(self.output, "cli-commands.jsonl"),
                      "--raw-dir", os.path.join(self.output, "cli-raw"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("NOT_VALIDATED", p.stdout)

    def test_03_filter_empty_is_nonzero(self):
        p = self._run("--builtin", "distribution", "--only", "no-such-gate",
                      "--tag", "cli-filter",
                      "--log-dir", os.path.join(self.output, "cli-logs"),
                      "--commands", os.path.join(self.output, "cli-commands.jsonl"),
                      "--raw-dir", os.path.join(self.output, "cli-raw"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("NOT_VALIDATED", p.stdout)

    def test_04_missing_executable_records_and_nonzero(self):
        # run_entry must not raise UnboundLocalError; it records RUNNER_ERROR + null exit.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wb2_run_under_test", os.path.join(_HERE, "wb2_run.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ledger = os.path.join(self.output, "cli-missing-commands.jsonl")
        missing = os.path.join(self.output, "nonexistent-program.exe")
        code = module.run_entry([missing], self.CLONE, dict(os.environ),
                                Path(os.path.join(self.output, "cli-missing.log")),
                                5, "missing-exe", "cli", "synthetic", Path(ledger))
        self.assertNotEqual(code, 0)
        self.assertTrue(os.path.exists(ledger), "ledger must be written even on launch failure")
        with open(ledger, encoding="utf-8") as fh:
            rec = json.loads(fh.read().strip().splitlines()[-1])
        self.assertEqual(rec["status"], "RUNNER_ERROR")
        self.assertIsNone(rec["exit_code"])
        self.assertIn("FileNotFoundError", rec["runner_error"])

    def test_05_timeout_records_and_nonzero(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wb2_run_under_test2", os.path.join(_HERE, "wb2_run.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ledger = os.path.join(self.output, "cli-timeout-commands.jsonl")
        code = module.run_entry(
            [sys.executable, "-c", "import time; time.sleep(5)"], self.CLONE,
            dict(os.environ), Path(os.path.join(self.output, "cli-timeout.log")),
            0.05, "timeout", "cli", "synthetic", Path(ledger))
        self.assertNotEqual(code, 0)
        self.assertTrue(os.path.exists(ledger), "ledger must be written on timeout")
        with open(ledger, encoding="utf-8") as fh:
            rec = json.loads(fh.read().strip().splitlines()[-1])
        self.assertEqual(rec["status"], "TIMEOUT")
        self.assertIsNone(rec["exit_code"])
        self.assertIn("TimeoutExpired", rec["runner_error"])


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
