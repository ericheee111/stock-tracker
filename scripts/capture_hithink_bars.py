#!/usr/bin/env python3
"""Capture exact HiThink Finance daily-bar bytes as a T1 artifact.

The command is read-only with respect to the production SQLite database. It
explicitly activates the otherwise disabled capture adapter for this process
only and requires its credential in the documented environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.collector.hithink_finance import HithinkFinanceProvider
from stock_tracker.core.config import load_providers
from stock_tracker.core.types import Market
from stock_tracker.quant.data import capture_market_bars


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_SHANGHAI)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _provider(config_path: Path) -> HithinkFinanceProvider:
    configs = load_providers(str(config_path))
    matches = [config for config in configs if config.name == "hithink_finance"]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one hithink_finance provider in {config_path}, "
            f"found {len(matches)}"
        )
    # The committed application config stays disabled. Running this dedicated
    # capture command is the explicit activation boundary for this process only.
    return HithinkFinanceProvider(replace(matches[0], enabled=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture exact official HiThink A-share daily-bar bytes as a "
            "BEST_EFFORT artifact."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Canonical A-share symbol")
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument("--adjust", default="raw", choices=["raw", "qfq", "hfq"])
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/quant-artifacts"),
        help="Artifact-store root; production SQLite is never modified.",
    )
    parser.add_argument(
        "--providers-config",
        type=Path,
        default=Path("config/providers.toml"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = _provider(args.providers_config)
    request_parameters = provider.historical_request_parameters(
        args.symbol,
        Market.A,
        "1d",
        args.start,
        args.end,
        args.adjust,
    )
    raw = provider.fetch_bars_raw(
        args.symbol,
        Market.A,
        interval="1d",
        start=args.start,
        end=args.end,
        adjust=args.adjust,
    )
    captured = capture_market_bars(
        args.output_root,
        raw_bytes=raw,
        parser=provider.parse_bars_strict,
        symbol=args.symbol,
        market=Market.A,
        interval="1d",
        retrieved_at=datetime.now(timezone.utc),
        source=provider.name,
        source_dataset=provider.SOURCE_DATASET,
        provider_version=provider.PROVIDER_VERSION,
        schema_version=provider.HISTORICAL_SCHEMA_VERSION,
        parser_version=provider.HISTORICAL_ADAPTER_VERSION,
        request_parameters={
            "endpoint": provider.HISTORICAL_ENDPOINT,
            "requested_start": args.start.date().isoformat(),
            "requested_end": args.end.date().isoformat(),
            "adjustment": args.adjust,
            "upstream_adjustment": request_parameters["adjust"],
            "interval": "1d",
            "offset": 0,
        },
        known_at_policy="retrieved-at",
        revision_policy="content-addressed-immutable",
        verified=False,
        source_note=(
            "official HiThink REST capture; BEST_EFFORT pending license, "
            "coverage, revision and cross-source PIT review"
        ),
    )
    print(
        json.dumps(
            {
                "schema": "capture-hithink-bars-cli-v1",
                "symbol": args.symbol,
                "market": Market.A.value,
                "row_count": captured.artifact.row_count,
                "trust_tier": captured.trust_tier.value,
                "artifact_id": captured.artifact.artifact_id,
                "capture_id": captured.capture_id,
                "normalized_dataset_id": captured.normalized_dataset_id,
                "request_parameters": captured.request_parameters,
                "storage_key": captured.artifact.storage_key,
                "descriptor_key": captured.descriptor_key,
                "production_database_modified": False,
                "research_grade": False,
                "credential_in_output": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
