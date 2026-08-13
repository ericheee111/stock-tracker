"""Capture exact Eastmoney K-line bytes into an immutable T1 artifact store.

This command does not promote data to research grade and does not touch the
production SQLite database.  It is the first Wave 2B.1 ingestion boundary:
fetch exact bytes, persist them content-addressed, parse those same bytes, and
emit a tamper-evident descriptor.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.core.config import load_providers
from stock_tracker.core.types import Market
from stock_tracker.quant.data import capture_market_bars


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _provider(config_path: Path) -> EastmoneyProvider:
    configs = load_providers(str(config_path))
    matches = [config for config in configs if config.name == "eastmoney"]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one eastmoney provider in {config_path}, found {len(matches)}"
        )
    provider = EastmoneyProvider(matches[0])
    if not provider.supports_raw_bars():
        raise RuntimeError("eastmoney provider does not expose raw bar bytes")
    return provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture exact public K-line bytes as a BEST_EFFORT artifact.",
    )
    parser.add_argument("--symbol", required=True, help="Canonical symbol, e.g. 600519.SH")
    parser.add_argument("--market", required=True, choices=[item.value for item in Market])
    parser.add_argument("--interval", default="1d", choices=["1d"])
    parser.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "raw"])
    parser.add_argument("--start", type=_date)
    parser.add_argument("--end", type=_date)
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
    market = Market(args.market)
    provider = _provider(args.providers_config)
    raw = provider.fetch_bars_raw(
        args.symbol,
        market,
        interval=args.interval,
        start=args.start,
        end=args.end,
        adjust=args.adjust,
    )
    captured = capture_market_bars(
        args.output_root,
        raw_bytes=raw,
        parser=provider.parse_bars_strict,
        symbol=args.symbol,
        market=market,
        interval=args.interval,
        retrieved_at=datetime.now(timezone.utc),
        source=provider.name,
        source_dataset="push2his-kline",
        provider_version="push2his-public-endpoint",
        schema_version=provider.KLINE_SCHEMA_VERSION,
        parser_version=provider.KLINE_ADAPTER_VERSION,
        request_parameters={
            "adjustment": args.adjust,
            "requested_start": (
                args.start.date().isoformat() if args.start else None
            ),
            "requested_end": args.end.date().isoformat() if args.end else None,
            "endpoint": provider.KLINE,
            "interval": args.interval,
        },
        known_at_policy="retrieved-at",
        revision_policy="content-addressed-immutable",
        verified=False,
        source_note="public aggregator capture; BEST_EFFORT only",
    )
    print(
        json.dumps(
            {
                "schema": "capture-quant-bars-cli-v1",
                "symbol": args.symbol,
                "market": market.value,
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
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
