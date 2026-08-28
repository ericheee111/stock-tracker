"""Capture exact provider K-line bytes into an immutable T1 artifact store.

This command does not promote data to research grade and does not touch the
production SQLite database.  It supports explicit Eastmoney or Tencent raw
capture, persists exact response bytes before parsing, then emits a tamper-
evident descriptor for the same deterministic parser/version contract.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import load_providers
from stock_tracker.core.types import Market
from stock_tracker.quant.data import capture_market_bars

RawBarProvider: TypeAlias = EastmoneyProvider | TencentProvider
_PROVIDER_CLASSES: dict[str, type[RawBarProvider]] = {
    "eastmoney": EastmoneyProvider,
    "tencent": TencentProvider,
}
_PROVIDER_DATASETS = {
    "eastmoney": "push2his-kline",
    "tencent": "web-ifzq-fqkline",
}
_PROVIDER_VERSIONS = {
    "eastmoney": "push2his-public-endpoint",
    "tencent": "web-ifzq-public-endpoint",
}


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _provider(config_path: Path, provider_name: str = "eastmoney") -> RawBarProvider:
    provider_class = _PROVIDER_CLASSES.get(provider_name)
    if provider_class is None:
        raise RuntimeError(f"unsupported raw-bar provider: {provider_name}")
    configs = load_providers(str(config_path))
    matches = [config for config in configs if config.name == provider_name]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {provider_name} provider in {config_path}, "
            f"found {len(matches)}"
        )
    provider = provider_class(matches[0])
    if not provider.supports_raw_bars():
        raise RuntimeError(f"{provider_name} provider does not expose raw bar bytes")
    return provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture exact public K-line bytes as a BEST_EFFORT artifact.",
    )
    parser.add_argument(
        "--provider",
        default="eastmoney",
        choices=sorted(_PROVIDER_CLASSES),
        help="Exact-raw source. Tencent currently supports qfq only.",
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
    provider = _provider(args.providers_config, args.provider)
    if not provider.applies_to(market):
        raise RuntimeError(
            f"{args.provider} is not configured for market {market.value}"
        )
    if not provider.supports_adjustment(args.adjust):
        raise RuntimeError(
            f"{args.provider} cannot honestly provide adjustment={args.adjust}"
        )
    strict_parser = provider.parse_bars_strict
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
        parser=strict_parser,
        symbol=args.symbol,
        market=market,
        interval=args.interval,
        retrieved_at=datetime.now(timezone.utc),
        source=provider.name,
        source_dataset=_PROVIDER_DATASETS[args.provider],
        provider_version=_PROVIDER_VERSIONS[args.provider],
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
            "synthetic_fixture": False,
        },
        known_at_policy="retrieved-at",
        revision_policy="content-addressed-immutable",
        verified=False,
        source_note="public aggregator capture; BEST_EFFORT only",
    )
    print(
        json.dumps(
            {
                "schema": "capture-quant-bars-cli-v2",
                "provider": args.provider,
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
