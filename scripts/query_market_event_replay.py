#!/usr/bin/env python3
"""Query deterministic local market-event replay without touching production SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.collector.xtp_sidecar import load_xtp_sidecar_config
from stock_tracker.market_events.replay import MarketEventReplay
from stock_tracker.market_events.store import MarketEventStore


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must be timezone-aware ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("time must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query local XTP market-event replay")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, type=_time)
    parser.add_argument("--end", required=True, type=_time)
    parser.add_argument("--backend", choices=("auto", "python", "duckdb"), default="auto")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "xtp_sidecar.toml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_xtp_sidecar_config(args.config)
    store = MarketEventStore(
        ROOT / config.event_root,
        ROOT / config.metadata_db,
        quarantine_root=ROOT / config.quarantine_root,
    )
    result = MarketEventReplay(store).run(
        args.symbol,
        start_at=args.start,
        end_at=args.end,
        backend=args.backend,
        limit=args.limit,
    )
    payload = result.as_dict()
    payload["minute_bars"] = store.minute_bars(
        args.symbol,
        start_at=args.start,
        end_at=args.end,
    )
    payload["allow_live_decision"] = False
    payload["allow_model_training"] = False
    payload["auto_trade"] = False
    payload["production_database_modified"] = False
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
