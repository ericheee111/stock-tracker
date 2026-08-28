#!/usr/bin/env python3
"""Run deterministic Stage 5C XTP shadow acceptance without live credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.market_events.shadow import (
    ShadowThresholds,
    run_shadow_acceptance,
)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run synthetic XTP-vs-reference shadow reconciliation",
    )
    parser.add_argument("--maximum-timestamp-delta-ms", type=int, default=3000)
    parser.add_argument("--maximum-price-difference-bps", type=float, default=8.0)
    parser.add_argument("--maximum-volume-difference-ratio", type=float, default=0.03)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    production = ROOT / "data" / "stock_tracker.db"
    before = _sha(production)
    thresholds = ShadowThresholds(
        maximum_timestamp_delta_ms=args.maximum_timestamp_delta_ms,
        maximum_price_difference_bps=args.maximum_price_difference_bps,
        maximum_volume_difference_ratio=args.maximum_volume_difference_ratio,
    )
    report = run_shadow_acceptance(thresholds=thresholds)
    after = _sha(production)
    report["production_database_sha256_before"] = before
    report["production_database_sha256_after"] = after
    report["production_database_modified"] = before != after
    report["engineering_passed"] = bool(
        report["engineering_passed"] and before == after
    )
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["engineering_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
