"""Build one Stage 2H exact-raw market-bar acceptance manifest.

The command is offline. It re-verifies already captured exact-raw descriptors,
binds the exact parser/schema versions and writes a content-addressed acceptance
manifest. It never opens the production SQLite database and never promotes data
to T3 or research grade.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.hithink_finance import HithinkFinanceProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig
from stock_tracker.core.types import Market
from stock_tracker.quant.data import (
    DEFAULT_MARKET_BAR_ACCEPTANCE_VERSION,
    MarketBarAcceptanceCase,
    MarketBarAcceptanceError,
    MarketBarAcceptanceManifest,
    MarketBarAssuranceDeclaration,
    MarketBarAuxiliaryBindings,
    MarketBarCaptureReference,
    MarketBarField,
    MarketBarParserBinding,
    load_captured_market_bars,
    load_market_bar_assurance_declaration,
)
from stock_tracker.quant.data.manifest import ManifestContractError

_DEFAULT_FIELDS = tuple(
    sorted(
        (
            MarketBarField.OPEN,
            MarketBarField.HIGH,
            MarketBarField.LOW,
            MarketBarField.CLOSE,
            MarketBarField.VOLUME,
        ),
        key=lambda item: item.value,
    )
)


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "datetime must use ISO-8601 and include a timezone"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _capture_reference(value: str) -> tuple[str, str]:
    source, separator, descriptor_key = value.partition("=")
    if separator != "=" or not source or not descriptor_key:
        raise argparse.ArgumentTypeError(
            "capture must use SOURCE=DESCRIPTOR_KEY"
        )
    return source, descriptor_key


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise MarketBarAcceptanceError(
                "acceptance paths cannot traverse symlinks or junctions"
            )
    return absolute


def _validate_paths(artifact_root: Path, output: Path) -> tuple[Path, Path]:
    artifact_path = _checked_path(artifact_root).resolve(strict=True)
    output_path = _checked_path(output).resolve(strict=False)
    if not artifact_path.is_dir():
        raise MarketBarAcceptanceError("artifact root must be a directory")
    if output_path.is_relative_to(artifact_path):
        raise MarketBarAcceptanceError(
            "acceptance manifest cannot be written inside the artifact store"
        )
    production_database = (PROJECT_ROOT / "data" / "stock_tracker.db").resolve(
        strict=False
    )
    if artifact_path == production_database or output_path == production_database:
        raise MarketBarAcceptanceError(
            "Stage 2H paths cannot target the production database"
        )
    if output_path.exists() and not output_path.is_file():
        raise MarketBarAcceptanceError("acceptance manifest output must be a file")
    return artifact_path, output_path


def _load_assurance_declaration(path: Path) -> MarketBarAssuranceDeclaration:
    declaration_path = _checked_path(path).resolve(strict=True)
    if not declaration_path.is_file():
        raise MarketBarAcceptanceError(
            "assurance declaration path must be a regular file"
        )
    return load_market_bar_assurance_declaration(declaration_path)


def _registry() -> dict[str, MarketBarParserBinding]:
    eastmoney = EastmoneyProvider(
        ProviderConfig(
            name="eastmoney",
            cls="EastmoneyProvider",
            markets=["a", "hk", "us"],
            max_rps=100,
        )
    )
    tencent = TencentProvider(
        ProviderConfig(
            name="tencent",
            cls="TencentProvider",
            markets=["a", "hk", "us"],
            max_rps=100,
        )
    )
    hithink = HithinkFinanceProvider(
        ProviderConfig(
            name="hithink_finance",
            cls="HithinkFinanceProvider",
            markets=["a"],
            enabled=True,
            primary=False,
            supports_snapshot=False,
            max_rps=100,
            read_only=True,
            trust_tier="T1_BEST_EFFORT",
            allow_live_decision=False,
            allow_model_training=False,
            allow_public_redistribution=False,
        ),
        credential_provider=lambda: "stage2h-offline-parser-binding",
    )
    return {
        "eastmoney": MarketBarParserBinding(
            source="eastmoney",
            schema_version=eastmoney.KLINE_SCHEMA_VERSION,
            parser_version=eastmoney.KLINE_ADAPTER_VERSION,
            parser=eastmoney.parse_bars_strict,
        ),
        "tencent": MarketBarParserBinding(
            source="tencent",
            schema_version=tencent.KLINE_SCHEMA_VERSION,
            parser_version=tencent.KLINE_ADAPTER_VERSION,
            parser=tencent.parse_bars_strict,
        ),
        "hithink_finance": MarketBarParserBinding(
            source="hithink_finance",
            schema_version=hithink.HISTORICAL_SCHEMA_VERSION,
            parser_version=hithink.HISTORICAL_ADAPTER_VERSION,
            parser=hithink.parse_bars_strict,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Stage 2H acceptance manifest from verified exact-raw "
            "capture descriptors."
        )
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--market",
        required=True,
        choices=[item.value for item in Market],
    )
    parser.add_argument("--interval", default="1d", choices=["1d"])
    parser.add_argument("--adjustment", default="qfq", choices=["qfq"])
    parser.add_argument("--as-of", type=_aware_datetime, required=True)
    parser.add_argument(
        "--created-at",
        type=_aware_datetime,
        default=None,
    )
    parser.add_argument("--calendar-snapshot-id", required=True)
    parser.add_argument(
        "--open-session",
        type=_date,
        action="append",
        dest="open_sessions",
        required=True,
    )
    parser.add_argument(
        "--capture",
        type=_capture_reference,
        action="append",
        dest="captures",
        required=True,
        help="Repeat SOURCE=DESCRIPTOR_KEY; at least two unique sources are required.",
    )
    parser.add_argument(
        "--comparable-field",
        action="append",
        choices=[item.value for item in MarketBarField],
        dest="comparable_fields",
    )
    parser.add_argument(
        "--assurance-declaration",
        type=Path,
        action="append",
        dest="assurance_declarations",
    )
    parser.add_argument("--stage2-reconciliation-report-id")
    parser.add_argument("--corporate-action-report-id")
    parser.add_argument(
        "--acceptance-version",
        default=DEFAULT_MARKET_BAR_ACCEPTANCE_VERSION,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifact_root, output = _validate_paths(args.artifact_root, args.output)
        market = Market(args.market)
        if market is not Market.A:
            raise MarketBarAcceptanceError(
                "Stage 2H acceptance currently supports A shares only"
            )
        if len(args.open_sessions) != len(set(args.open_sessions)):
            raise MarketBarAcceptanceError("open sessions must not contain duplicates")
        if args.comparable_fields and len(args.comparable_fields) != len(
            set(args.comparable_fields)
        ):
            raise MarketBarAcceptanceError(
                "comparable fields must not contain duplicates"
            )
        registry = _registry()
        references: list[MarketBarCaptureReference] = []
        seen_sources: set[str] = set()
        for source, descriptor_key in args.captures:
            if source in seen_sources:
                raise MarketBarAcceptanceError(
                    "capture sources must be unique"
                )
            binding = registry.get(source)
            if binding is None:
                raise MarketBarAcceptanceError(
                    f"unsupported acceptance capture source: {source}"
                )
            captured = load_captured_market_bars(
                artifact_root,
                descriptor_key=descriptor_key,
                parser=binding.parser,
            )
            if captured.artifact.source != source:
                raise MarketBarAcceptanceError(
                    "capture source differs from descriptor artifact"
                )
            if captured.artifact.schema_version != binding.schema_version:
                raise MarketBarAcceptanceError(
                    "capture schema differs from parser binding"
                )
            if captured.parser_version != binding.parser_version:
                raise MarketBarAcceptanceError(
                    "capture parser version differs from parser binding"
                )
            first = captured.bars[0]
            if (
                first.symbol != args.symbol
                or first.market is not market
                or first.interval != args.interval
                or captured.request_parameters.get("adjustment") != args.adjustment
            ):
                raise MarketBarAcceptanceError(
                    "capture identity differs from requested acceptance case"
                )
            references.append(
                MarketBarCaptureReference(
                    source=source,
                    descriptor_key=descriptor_key,
                    parser_binding_id=binding.binding_id,
                )
            )
            seen_sources.add(source)
        declarations = tuple(
            _load_assurance_declaration(path)
            for path in (args.assurance_declarations or ())
        )
        fields = (
            tuple(
                sorted(
                    (MarketBarField(item) for item in args.comparable_fields),
                    key=lambda item: item.value,
                )
            )
            if args.comparable_fields
            else _DEFAULT_FIELDS
        )
        case = MarketBarAcceptanceCase(
            case_name=args.case_name,
            market=market,
            symbol=args.symbol,
            interval=args.interval,
            adjustment=args.adjustment,
            as_of=args.as_of,
            expected_open_sessions=tuple(args.open_sessions),
            calendar_snapshot_id=args.calendar_snapshot_id,
            captures=tuple(references),
            comparable_fields=fields,
            assurance_declaration_ids=tuple(
                item.declaration_id for item in declarations
            ),
            auxiliary_bindings=MarketBarAuxiliaryBindings(
                stage2_reconciliation_report_id=(
                    args.stage2_reconciliation_report_id
                ),
                corporate_action_report_id=args.corporate_action_report_id,
            ),
        )
        manifest = MarketBarAcceptanceManifest(
            acceptance_version=args.acceptance_version,
            created_at=args.created_at or datetime.now(timezone.utc),
            cases=(case,),
            assurance_declarations=declarations,
        )
        manifest.write_json(output)
        print(
            json.dumps(
                {
                    "schema": "stage2h-market-bar-manifest-builder-v1",
                    "manifest_id": manifest.manifest_id,
                    "case_id": case.case_id,
                    "case_name": case.case_name,
                    "capture_count": len(case.captures),
                    "assurance_declaration_count": len(declarations),
                    "output": str(output),
                    "trusted_assurance_authority_configured": False,
                    "research_grade": False,
                    "t3_reached": False,
                    "production_database_modified": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ManifestContractError,
        MarketBarAcceptanceError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema": "stage2h-market-bar-manifest-builder-error-v1",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "production_database_modified": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
