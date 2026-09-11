"""WB2 gate runner (measurement only; W5 configurable command-list tool).

Executes a *fixed list* of gate commands as argv lists (never shell-spliced), in a
fully recorded environment, appending an auditable record (argv, cwd, exit code,
interpreter, env whitelist, log sha256) to a JSONL ledger. It never overwrites a
previous attempt's log, never strips/alters the WorkBuddy environment protection,
and returns a non-zero exit if any gate failed.

Two input modes (choose one):
  --plan PATH      JSON plan of {label, argv, timeout?, note?} entries (as before)
  --builtin LIST   comma list of built-in gates (see --list-builtin)

Usage:
    py -3.14 qa/wb2/wb2_run.py --builtin focused-218,runtime,quant,distribution,basedpyright --tag r41 --cwd .
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMED_TOOL = str(HERE / "wb2_timed.py")

ENV_WHITELIST = (
    "PYTHONPATH", "PYTHONHASHSEED", "PYTHONIOENCODING",
    "TEMP", "TMP", "PYTHONPYCACHEPREFIX", "PATH",
)


def _builtin_gates(raw_dir: str) -> dict[str, dict]:
    """Fixed gate command list; full argv lists (argv[0] = interpreter), resolved at
    runtime. No hardcoded drive paths; run as argv lists (never shell-spliced)."""
    py = sys.executable
    return {
        "wb1": {
            "argv": [py, "-m", "unittest", "qa.wb1.test_wb1_clock_boundary",
                     "qa.wb1.test_wb1_identity_lifecycle", "qa.wb1.test_wb1_type_boundary", "-v"],
            "note": "WB1 explicit boundary gate; separate from default tests discover",
        },
        "focused-218": {
            "argv": [py, TIMED_TOOL, "--label", "focused-218", "--mode", "named",
                     "--names", ("tests.test_runtime_evidence,tests.test_runtime_path_contracts,"
                     "tests.test_market_source_snapshot_contracts"),
                     "--out-dir", raw_dir],
            "note": "R4.1 focused suites; counts measured by the current run",
        },
        "runtime": {
            "argv": [py, TIMED_TOOL, "--label", "runtime", "--mode", "discover",
                     "--start-dir", "tests", "--out-dir", raw_dir],
            "note": "R4.1 full runtime discover -s tests",
        },
        "quant": {
            "argv": [py, TIMED_TOOL, "--label", "quant", "--mode", "discover",
                     "--start-dir", "tests_quant", "--out-dir", raw_dir],
            "note": "R4.1 full quant discover -s tests_quant",
        },
        "distribution": {
            "argv": [py, TIMED_TOOL, "--label", "distribution", "--mode", "named",
                     "--names", "tests_quant.test_source_distribution,tests_quant.test_no_tracked_bytecode",
                     "--out-dir", raw_dir],
            "note": "source-distribution gate; actual Git checkout required",
        },
        "basedpyright": {
            "argv": [py, "-m", "basedpyright", "--level", "error",
                     "stock_tracker/runtime_evidence"],
            "note": "full-package basedpyright (standalone gate)",
        },
    }


def _fresh_path(base: Path, tag: str, label: str) -> Path:
    """Never overwrite a prior attempt's log: auto-suffix .2, .3, ..."""
    candidate = base / f"{tag}-{label}.log"
    n = 1
    while candidate.exists():
        n += 1
        candidate = base / f"{tag}-{label}.{n}.log"
    return candidate


def run_entry(argv: list, cwd: Path, env: dict, log_path: Path,
              timeout: int, label: str, tag: str, note: str,
              ledger: Path) -> int:
    started_epoch = time.time()
    # Unify the outcome: status + child_exit_code + runner_error are always set,
    # so the ledger record can be written once regardless of success/failure/
    # timeout/launch error. `proc` is never referenced outside the success branch.
    status = "COMPLETED"
    child_exit_code = None
    runner_error = ""
    try:
        with log_path.open("wb") as fh:
            proc = subprocess.run(
                argv, cwd=str(cwd), env=env, stdout=fh, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
        child_exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        status = "TIMEOUT"
        runner_error = f"TimeoutExpired: {exc}"
        log_path.write_bytes(
            (log_path.read_bytes() if log_path.exists() else b"") +
            f"\n[TIMEOUT] {runner_error}\n".encode("utf-8", errors="replace")
        )
    except Exception as exc:  # noqa: BLE001 - launch/other failure; record, do not crash
        status = "RUNNER_ERROR"
        runner_error = f"{type(exc).__name__}: {exc}"
        log_path.write_bytes(
            (log_path.read_bytes() if log_path.exists() else b"") +
            f"\n[RUNNER_ERROR] {runner_error}\n".encode("utf-8", errors="replace")
        )
    finished_epoch = time.time()
    data = log_path.read_bytes()
    record = {
        "tag": tag,
        "label": label,
        "command": argv,
        "cwd": str(cwd),
        "started_epoch": started_epoch,
        "finished_epoch": finished_epoch,
        "wall_s": round(finished_epoch - started_epoch, 6),
        "status": status,
        "exit_code": child_exit_code,
        "runner_error": runner_error,
        "interpreter": f"Python {platform.python_version()} ({sys.executable})",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "env": {k: env.get(k, "") for k in ENV_WHITELIST},
        "note": note,
        "log": str(log_path),
        "log_bytes": len(data),
        "log_sha256": hashlib.sha256(data).hexdigest(),
    }
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"tag": tag, "label": label, "status": status,
                      "exit_code": child_exit_code, "wall_s": record["wall_s"]},
                     ensure_ascii=False), flush=True)
    # Non-zero for any non-completed run; never fabricate a success.
    if status == "COMPLETED":
        return child_exit_code
    return 124 if status == "TIMEOUT" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default=None)
    parser.add_argument("--builtin", default=None)
    parser.add_argument("--list-builtin", action="store_true")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--log-dir", default=str(HERE / "logs"))
    parser.add_argument("--commands", default=str(HERE / "commands.jsonl"))
    parser.add_argument("--raw-dir", default=str(HERE / "raw"))
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    if args.list_builtin:
        print(json.dumps(sorted(_builtin_gates(str(Path(args.raw_dir).resolve())).keys()), indent=2))
        return 0

    if not args.plan and args.builtin is None:
        parser.error("one of --plan or --builtin is required")

    cwd = Path(args.cwd)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ledger = Path(args.commands)
    # raw-dir is embedded into the timed tool's argv, whose cwd is the *clone*, so
    # it must be resolved to an absolute path here (relative to this tool's cwd).
    raw_dir = str(Path(args.raw_dir).resolve())
    env = dict(os.environ)  # environment passed through unchanged (no shim removal)

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    unknown: list[str] = []
    if args.builtin is not None:
        gates = _builtin_gates(raw_dir)
        plan = []
        for name in args.builtin.split(","):
            name = name.strip()
            if not name:
                continue
            if name not in gates:
                unknown.append(name)
            else:
                plan.append({"label": name, **gates[name]})
    else:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    if unknown:
        print(f"WB2_RUN_NOT_VALIDATED unknown_gates={unknown}")
        return 2
    if not plan:
        print("WB2_RUN_NOT_VALIDATED empty_plan")
        return 2

    planned = len(plan)
    attempted = 0
    completed = 0
    any_failed = False
    for entry in plan:
        label = entry["label"]
        if only and label not in only:
            continue
        attempted += 1
        argv = list(entry["argv"])
        log_path = _fresh_path(log_dir, args.tag, label)
        timeout = entry.get("timeout", 1800)
        rc = run_entry(argv, cwd, env, log_path, timeout, label, args.tag,
                       entry.get("note", ""), ledger)
        completed += 1
        if rc != 0:
            any_failed = True

    if attempted == 0:
        print(f"WB2_RUN_NOT_VALIDATED filter_matched_no_gates only={sorted(only)}")
        return 2

    summary = {"tag": args.tag, "planned": planned, "attempted": attempted,
               "completed": completed, "any_failed": any_failed}
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tag": args.tag, "kind": "batch_summary", **summary},
                            ensure_ascii=False) + "\n")
    print(f"WB2_RUN_BATCH_DONE {args.tag} {json.dumps(summary, ensure_ascii=False)}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
