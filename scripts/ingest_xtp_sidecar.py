#!/usr/bin/env python3
"""Ingest read-only XTP sidecar events into the separate local event store."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.collector.xtp_sidecar import load_xtp_sidecar_config
from stock_tracker.monitor.service import MonitorService, MonitorServiceError


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest XTP sidecar events without production DB writes")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "xtp_sidecar.toml",
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--sleep-sec", type=float, default=0.2)
    parser.add_argument("--acknowledge-read-only-ingestion", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.acknowledge_read_only_ingestion:
        raise SystemExit("--acknowledge-read-only-ingestion is required")
    if not 1 <= args.iterations <= 100:
        raise SystemExit("--iterations must be 1-100")
    if not 1 <= args.limit <= 500:
        raise SystemExit("--limit must be 1-500")
    if not 0 <= args.sleep_sec <= 30:
        raise SystemExit("--sleep-sec must be 0-30")
    base = load_xtp_sidecar_config(args.config)
    config = replace(base, enabled=True)
    service = MonitorService(config, project_root=ROOT)
    production_db = ROOT / "data" / "stock_tracker.db"
    before = _sha(production_db)
    runs = []
    try:
        for index in range(args.iterations):
            runs.append(service.poll_once(limit=args.limit))
            if index + 1 < args.iterations and args.sleep_sec:
                time.sleep(args.sleep_sec)
    except MonitorServiceError as exc:
        print(
            json.dumps(
                {
                    "schema": "stock-tracker-xtp-ingestion-v1",
                    "passed": False,
                    "error_code": type(exc).__name__,
                    "contains_account_value": False,
                    "contains_sidecar_access": False,
                    "production_database_modified": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    after = _sha(production_db)
    report = {
        "schema": "stock-tracker-xtp-ingestion-v1",
        "passed": before == after,
        "runs": runs,
        "data_link": service.data_link(),
        "monitor_summary": service.repository.summary(),
        "synthetic_fixture_only": config.backend == "simulator",
        "real_xtp_account_acceptance": "PENDING",
        "local_ingestion_backend": config.backend.upper(),
        "allow_live_decision": False,
        "allow_model_training": False,
        "auto_trade": False,
        "algorithm_account_used": False,
        "contains_account_value": False,
        "contains_sidecar_access": False,
        "production_database_sha256_before": before,
        "production_database_sha256_after": after,
        "production_database_modified": before != after,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
