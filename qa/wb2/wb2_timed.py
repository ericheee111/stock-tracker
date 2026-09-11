"""WB2 per-test timing recorder (measurement only, WB2-W5 fixed).

Result semantics follow the frozen W5 spec (WORKBUDDY-NEXT-WAVE.md §3). Compared to
the WB2-1a7286d version this fixes:

* WB2-T01  a failing subTest no longer records the parent as PASS; addSubTest now
           records the parent id, subtest params and the subtest fail/error/skip.
* expectedFailure is no longer mis-recorded as FAIL (addExpectedFailure no longer
           writes into the failure bucket).
* unexpectedSuccess (XPASS) is reported as UNEXPECTED_SUCCESS (still fails the run).
* setUpClass / setUpModule / tearDown errors are captured with a scope row and full
           traceback even though unittest never calls startTest/stopTest for them.
* 0 tests and all-skipped runs are NOT_VALIDATED (distinct exit codes), never PASS.
* Full stderr/traceback is written to a sidecar log; the inline row only holds a
           truncated preview (never presented as "complete").
* A repeated --label never overwrites a previous attempt's raw files.

Usage (run from repository root):
    py -3.14 qa/wb2/wb2_timed.py --label focused --mode named \
        --names tests.test_runtime_evidence,tests.test_runtime_path_contracts,tests.test_market_source_snapshot_contracts
    py -3.14 qa/wb2/wb2_timed.py --label runtime --mode discover --start-dir tests
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
import time
import traceback
import unittest
from pathlib import Path


def _format_tb(err) -> str:
    if err is None:
        return ""
    if isinstance(err, str):
        return err
    try:
        return "".join(traceback.format_exception(*err))
    except Exception:  # noqa: BLE001 - diagnostic formatter must preserve the original error
        return repr(err)


def _parse_scope(test_id: str) -> str:
    head = test_id.split("(", 1)[0].strip()
    head = head.split(" ")[0]
    for scope in ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule"):
        if head == scope:
            return scope
    if head in ("<module>", "_FailedTest"):
        return "module"
    return "unknown"


class TimingResult(unittest.TestResult):
    """Records per-test wall/CPU, outcome, subtest results and full tracebacks.

    The authoritative pass/fail state mirrors ``unittest.TextTestResult`` (what
    ``python -m unittest`` actually uses): a failing subtest promotes its parent to
    FAIL/ERROR and makes ``wasSuccessful()`` False.
    """

    def __init__(self, sink: list) -> None:
        super().__init__()
        self.sink = sink
        self.full_records: list[dict] = []
        self._wall0 = 0.0
        self._cpu0 = 0.0
        self._last_end: float | None = None
        self.run_wall = 0.0
        self.run_cpu = 0.0
        self._run_wall0 = 0.0
        self._run_cpu0 = 0.0
        self._active_test = None
        self._case_failures: list[str] = []
        self._case_errors: list[str] = []
        self._case_skips: list[str] = []
        self._case_expected_failures: list[str] = []
        self._case_unexpected_success: list[str] = []
        self._case_subtests: list[dict] = []
        self._gap = None

    # ---- run level -------------------------------------------------------
    def startTestRun(self) -> None:
        self._run_wall0 = time.perf_counter()
        self._run_cpu0 = time.process_time()

    def stopTestRun(self) -> None:
        self.run_wall = time.perf_counter() - self._run_wall0
        self.run_cpu = time.process_time() - self._run_cpu0

    # ---- helpers ---------------------------------------------------------
    def _begin_case(self, test) -> None:
        self._active_test = test
        self._wall0 = time.perf_counter()
        self._cpu0 = time.process_time()
        self._gap = None if self._last_end is None else self._wall0 - self._last_end
        self._case_failures = []
        self._case_errors = []
        self._case_skips = []
        self._case_expected_failures = []
        self._case_unexpected_success = []
        self._case_subtests = []
        self._case_subtest_failed = False
        self._case_subtest_errored = False

    def _emit_scope_row(self, test, outcome: str, text: str) -> None:
        """Emit a row for a module/class fixture error or skip (no startTest)."""
        test_id = test.id()
        now = time.perf_counter()
        self._last_end = now
        self.sink.append(
            {
                "test_id": test_id,
                "module": "",
                "class": "",
                "method": "",
                "scope": _parse_scope(test_id),
                "outcome": outcome,
                "wall_s": 0.0,
                "cpu_s": 0.0,
                "gap_before_s": None,
                "skip_reason": text if outcome == "SKIP" else "",
                "failure_text": text if outcome == "FAIL" else "",
                "error_text": text if outcome == "ERROR" else "",
                "full_traceback_index": len(self.full_records),
                "subtests": [],
            }
        )
        self.full_records.append(
            {"test_id": test_id, "kind": outcome, "full_text": text}
        )

    def _emit_case_row(self, test, wall: float, cpu: float) -> None:
        test_id = test.id()
        if self._case_errors or self._case_subtest_errored:
            outcome = "ERROR"
        elif self._case_failures or self._case_subtest_failed:
            outcome = "FAIL"
        elif self._case_unexpected_success:
            outcome = "UNEXPECTED_SUCCESS"
        elif self._case_skips:
            outcome = "SKIP"
        elif self._case_expected_failures:
            outcome = "EXPECTED_FAILURE"
        elif self._case_subtests and all(s["outcome"] == "SKIP" for s in self._case_subtests):
            outcome = "ALL_SUBTESTS_SKIPPED"
        else:
            outcome = "PASS"

        parts = test_id.split(".")
        module = ".".join(parts[:-2]) if len(parts) >= 3 else test_id
        cls = parts[-2] if len(parts) >= 2 else ""
        method = parts[-1]

        error_text = "\n".join(self._case_errors)
        failure_text = "\n".join(self._case_failures)
        expected_text = "\n".join(self._case_expected_failures)
        unexpected_text = "\n".join(self._case_unexpected_success)
        full_index = len(self.full_records)
        self.full_records.append(
            {
                "test_id": test_id,
                "kind": outcome,
                "full_text": "\n\n".join(
                    x for x in (error_text, failure_text, expected_text, unexpected_text) if x
                ),
            }
        )

        self.sink.append(
            {
                "test_id": test_id,
                "module": module,
                "class": cls,
                "method": method,
                "scope": "test",
                "outcome": outcome,
                "wall_s": round(wall, 6),
                "cpu_s": round(cpu, 6),
                "gap_before_s": None if self._gap is None else round(self._gap, 6),
                "skip_reason": "; ".join(self._case_skips),
                "failure_text": failure_text[:4000],
                "error_text": error_text[:4000],
                "expected_failure_text": expected_text[:4000],
                "unexpected_success_text": unexpected_text[:4000],
                "full_traceback_index": full_index,
                "subtests": self._case_subtests,
            }
        )

    # ---- case level ------------------------------------------------------
    def startTest(self, test) -> None:
        self._begin_case(test)
        super().startTest(test)

    def stopTest(self, test) -> None:
        super().stopTest(test)
        end = time.perf_counter()
        wall = end - self._wall0
        cpu = time.process_time() - self._cpu0
        self._last_end = end
        self._emit_case_row(test, wall, cpu)
        self._active_test = None

    # ---- outcome hooks ---------------------------------------------------
    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        if self._active_test is None:
            self._emit_scope_row(test, "FAIL", _format_tb(err))
        else:
            self._case_failures.append(_format_tb(err))

    def addError(self, test, err) -> None:
        super().addError(test, err)
        if self._active_test is None:
            self._emit_scope_row(test, "ERROR", _format_tb(err))
        else:
            self._case_errors.append(_format_tb(err))

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        if isinstance(test, unittest.case._SubTest):
            # A skipped *subtest*: record it as a subcase of the parent; do NOT mark
            # the parent test as skipped (its other subtests may have run).
            self._record_subcase(self._active_test, test, "SKIP", skip_reason=str(reason))
            return
        if self._active_test is None:
            self._emit_scope_row(test, "SKIP", str(reason))
        else:
            self._case_skips.append(str(reason))

    def addExpectedFailure(self, test, err) -> None:
        super().addExpectedFailure(test, err)
        self._case_expected_failures.append(_format_tb(err))

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self._case_unexpected_success.append("UNEXPECTED_SUCCESS (XPASS)")

    def addSubTest(self, test, subtest, err) -> None:
        # super() maintains the standard failures/errors counts exactly once
        # (Python 3.14's TestResult.addSubTest already appends to failures/errors).
        super().addSubTest(test, subtest, err)
        if err is None:
            outcome = "PASS"
        elif issubclass(err[0], test.failureException):
            outcome = "FAIL"
            self._case_subtest_failed = True
        else:
            outcome = "ERROR"
            self._case_subtest_errored = True
        self._record_subcase(test, subtest, outcome, full_text=_format_tb(err))

    def _record_subcase(self, test, subtest, outcome, *, full_text="", skip_reason=""):
        ordinal = len(self._case_subtests) + 1
        occurrence_id = f"{test.id()}::subcase[{ordinal}]"
        full_index = len(self.full_records)
        self.full_records.append({
            "test_id": subtest.id(), "parent_id": test.id(),
            "occurrence_id": occurrence_id, "kind": outcome,
            "full_text": full_text or skip_reason,
        })
        params = {k: repr(v) for k, v in subtest.params.items()}
        self._case_subtests.append(
            {
                "parent_id": test.id(),
                "subtest_id": subtest.id(),
                "occurrence_id": occurrence_id,
                "params": params,
                "outcome": outcome,
                "skip_reason": skip_reason,
                "full_traceback_index": full_index,
                "traceback_preview": full_text[:2000],
            }
        )


def classify_run(tests_run: int, *, successes: int, failures: int, errors: int,
                 unexpected_successes: int, skipped: int, was_successful: bool,
                 unvalidated_methods: int = 0):
    """Pure gate-status function (frozen W5 semantics). Testable without running a suite.

    Failure/error/unexpected-success takes priority over the zero-tests guard, so a
    class/module setUp failure (testsRun==0, errors==1) is FAILED, not NOT_VALIDATED.
    """
    if failures or errors or unexpected_successes:
        return "FAILED", 1
    if tests_run == 0:
        return "NOT_VALIDATED", 2
    if successes == 0 and skipped + unvalidated_methods >= tests_run:
        return "NOT_VALIDATED", 2  # all skipped
    if was_successful:
        return "OK", 0
    return "FAILED", 1


def build_suite(args) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if args.mode == "discover":
        # Mirror `python -m unittest discover -s <start_dir> -p <pattern>` exactly:
        # unittest uses start_dir itself as top_level_dir (tree has no tests/__init__.py).
        return loader.discover(start_dir=args.start_dir, pattern=args.pattern)
    names: list[str] = []
    for chunk in args.names:
        names.extend([n.strip() for n in chunk.split(",") if n.strip()])
    return loader.loadTestsFromNames(names)


def _resolve_outputs(args) -> tuple[str, Path, Path, Path, Path]:
    """Never overwrite a previous attempt: auto-suffix a monotonic counter."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = args.label
    attempt = 1
    while (out_dir / f"{label}.jsonl").exists():
        attempt += 1
        label = f"{args.label}.{attempt}"
    jsonl = out_dir / f"{label}.jsonl"
    csv_path = out_dir / f"{label}.csv"
    meta_path = out_dir / f"{label}.meta.json"
    full_path = out_dir / f"{label}.full-errors.log"
    return label, jsonl, csv_path, meta_path, full_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "raw"))
    parser.add_argument("--mode", choices=["discover", "named"], default="discover")
    parser.add_argument("--start-dir", default="tests")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--names", action="append", default=[])
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    root = os.getcwd()
    if root not in sys.path:
        sys.path.insert(0, root)

    label, jsonl, csv_path, meta_path, full_path = _resolve_outputs(args)

    suite = build_suite(args)
    sink: list = []
    result = TimingResult(sink)

    started = time.time()
    run_wall0 = time.perf_counter()
    run_cpu0 = time.process_time()
    suite.run(result)
    run_wall = time.perf_counter() - run_wall0
    run_cpu = time.process_time() - run_cpu0
    finished = time.time()

    prefix = args.start_dir.replace("/", ".").replace("\\", ".")
    for row in sink:
        if row["scope"] == "test":
            parts = row["test_id"].split(".")
            if (args.mode == "discover" and not Path(args.start_dir).is_absolute()
                    and all(p.isidentifier() for p in prefix.split("."))
                    and len(parts) >= 3 and "." not in row["module"]):
                row["test_id_canonical"] = f"{prefix}.{row['test_id']}"
                row["module"] = f"{prefix}.{row['module']}"
            else:
                row["test_id_canonical"] = row["test_id"]
        else:
            row["test_id_canonical"] = row["test_id"]

    with jsonl.open("w", encoding="utf-8") as fh:
        for row in sink:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "test_id_canonical", "test_id", "module", "class", "method", "scope",
                "outcome", "wall_s", "cpu_s", "gap_before_s", "skip_reason",
            ],
        )
        writer.writeheader()
        for row in sink:
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})

    # Full tracebacks as a sidecar (never truncated, never claimed complete in-row).
    with full_path.open("w", encoding="utf-8") as fh:
        for idx, rec in enumerate(result.full_records):
            fh.write(f"===== [{idx}] {rec['kind']} :: {rec['test_id']} =====\n")
            if "occurrence_id" in rec:
                fh.write(f"parent={rec['parent_id']} occurrence={rec['occurrence_id']}\n")
            fh.write(rec["full_text"].rstrip() + "\n\n")

    measured = sum(r.get("wall_s", 0.0) for r in sink)
    measured_cpu = sum(r.get("cpu_s", 0.0) for r in sink)
    failures = [r for r in sink if r["outcome"] == "FAIL"]
    errors = [r for r in sink if r["outcome"] == "ERROR"]
    skips = [r for r in sink if r["outcome"] == "SKIP"]
    xfail = [r for r in sink if r["outcome"] == "EXPECTED_FAILURE"]
    xpass = [r for r in sink if r["outcome"] == "UNEXPECTED_SUCCESS"]
    successes = [r for r in sink if r["outcome"] == "PASS"]
    unvalidated = [r for r in sink if r["outcome"] == "ALL_SUBTESTS_SKIPPED"]

    tests_run = result.testsRun
    was_successful = result.wasSuccessful()
    run_status, process_exit = classify_run(
        tests_run,
        successes=len(successes),
        failures=len(failures),
        errors=len(errors),
        unexpected_successes=len(xpass),
        skipped=len(skips),
        was_successful=was_successful,
        unvalidated_methods=len(unvalidated),
    )

    meta = {
        "label": label,
        "note": args.note,
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor_count": os.cpu_count(),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", ""),
        "mode": args.mode,
        "start_dir": args.start_dir,
        "pattern": args.pattern,
        "suite_count_test_cases": suite.countTestCases(),
        "records": len(sink),
        "started_epoch": started,
        "finished_epoch": finished,
        "run_wall_s": round(run_wall, 6),
        "run_cpu_s": round(run_cpu, 6),
        "sum_test_wall_s": round(measured, 6),
        "sum_test_cpu_s": round(measured_cpu, 6),
        "harness_gap_s": round(run_wall - measured, 6),
        "testsRun": tests_run,
        "successes": len(successes),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "methods": {outcome: sum(r["scope"] == "test" and r["outcome"] == outcome for r in sink)
                    for outcome in ("PASS", "FAIL", "ERROR", "SKIP", "EXPECTED_FAILURE",
                                    "UNEXPECTED_SUCCESS", "ALL_SUBTESTS_SKIPPED")},
        "scope_records": sum(r["scope"] != "test" for r in sink),
        "not_validated_reason": "ALL_SUBTESTS_SKIPPED" if unvalidated and run_status == "NOT_VALIDATED" else None,
        "wasSuccessful": was_successful,
        "run_status": run_status,
        "process_exit_code": process_exit,
        "failing_test_ids": sorted(r["test_id_canonical"] for r in failures),
        "erroring_test_ids": sorted(r["test_id_canonical"] for r in errors),
        "skipped_test_ids": sorted(r["test_id_canonical"] for r in skips),
        "expected_failure_test_ids": sorted(r["test_id_canonical"] for r in xfail),
        "unexpected_success_test_ids": sorted(r["test_id_canonical"] for r in xpass),
        "subcases": {
            "total": sum(len(r["subtests"]) for r in sink if r["scope"] == "test"),
            "passed": sum(1 for r in sink for s in r["subtests"] if s["outcome"] == "PASS"),
            "failed": sum(1 for r in sink for s in r["subtests"] if s["outcome"] == "FAIL"),
            "errored": sum(1 for r in sink for s in r["subtests"] if s["outcome"] == "ERROR"),
            "skipped": sum(1 for r in sink for s in r["subtests"] if s["outcome"] == "SKIP"),
        },
        "jsonl": str(jsonl),
        "csv": str(csv_path),
        "full_traceback_log": str(full_path),
        "raw_exit_code": process_exit,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    top = sorted(sink, key=lambda r: r.get("wall_s", 0.0), reverse=True)[:20]
    print(f"WB2_TIMED label={label} tests={tests_run} status={run_status} "
          f"exit={process_exit} wall={run_wall:.3f}s cpu={run_cpu:.3f}s "
          f"method_pass={len(successes)} fail={len(result.failures)} err={len(result.errors)} skip={len(result.skipped)} "
          f"xfail={len(xfail)} xpass={len(xpass)}")
    for f in failures:
        print(f"WB2_FAIL {f['test_id_canonical']}")
    for e in errors:
        print(f"WB2_ERROR {e['test_id_canonical']}")
    print("WB2_TOP20")
    for r in top:
        print(f"  {r.get('wall_s',0.0):9.3f}s cpu={r.get('cpu_s',0.0):9.3f}s "
              f"{r['outcome']:18s} {r.get('test_id_canonical', r['test_id'])}")
    print(f"WB2_TIMED_DONE {jsonl}")
    return process_exit


if __name__ == "__main__":
    raise SystemExit(main())
