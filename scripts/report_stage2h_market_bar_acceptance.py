"""Replay Stage 2H captures and emit Stage 2H-2J acceptance/T3 preflight reports."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.hithink_finance import HithinkFinanceProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig
from stock_tracker.quant.data import (
    MarketBarAcceptanceError,
    MarketBarAcceptanceManifest,
    MarketBarAcceptanceState,
    MarketBarParserBinding,
    MarketBarReconciliationPolicy,
    materialize_market_bar_acceptance,
    write_market_bar_acceptance_json,
    write_market_bar_acceptance_markdown,
)


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _checked_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise MarketBarAcceptanceError(
                "acceptance paths cannot traverse symlinks or junctions"
            )
    return absolute


def _validate_paths(
    manifest: Path,
    artifact_root: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    manifest_path = _checked_path(manifest).resolve(strict=True)
    artifact_path = _checked_path(artifact_root).resolve(strict=True)
    output_path = _checked_path(output_dir).resolve(strict=False)
    if not manifest_path.is_file():
        raise MarketBarAcceptanceError("acceptance manifest must be a file")
    if not artifact_path.is_dir():
        raise MarketBarAcceptanceError("artifact root must be a directory")
    if output_path.exists() and not output_path.is_dir():
        raise MarketBarAcceptanceError("acceptance output must be a directory")
    if _overlaps(output_path, artifact_path):
        raise MarketBarAcceptanceError(
            "acceptance output and artifact roots must be separate"
        )
    production_database = (PROJECT_ROOT / "data" / "stock_tracker.db").resolve(
        strict=False
    )
    if production_database in {manifest_path, artifact_path, output_path}:
        raise MarketBarAcceptanceError(
            "Stage 2H paths cannot target the production database"
        )
    return manifest_path, artifact_path, output_path


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
            "Replay exact-raw descriptors and emit fail-closed Stage 2H-2J "
            "acceptance/T3 preflight evidence."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy-version", default="stage2h-market-bar-policy-v1")
    parser.add_argument("--minimum-independent-sources", type=int, default=2)
    parser.add_argument("--price-tolerance-bps", type=int, default=5)
    parser.add_argument("--volume-tolerance-bps", type=int, default=50)
    parser.add_argument("--amount-tolerance-bps", type=int, default=100)
    parser.add_argument("--turnover-tolerance-bps", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path, artifact_root, output_dir = _validate_paths(
            args.manifest,
            args.artifact_root,
            args.output_dir,
        )
        manifest = MarketBarAcceptanceManifest.read_json(manifest_path)
        policy = MarketBarReconciliationPolicy(
            policy_version=args.policy_version,
            minimum_independent_sources=args.minimum_independent_sources,
            price_tolerance_bps=args.price_tolerance_bps,
            volume_tolerance_bps=args.volume_tolerance_bps,
            amount_tolerance_bps=args.amount_tolerance_bps,
            turnover_tolerance_bps=args.turnover_tolerance_bps,
            require_all_open_sessions=True,
            require_license_clearance=True,
        )
        report = materialize_market_bar_acceptance(
            manifest=manifest,
            artifact_root=artifact_root,
            parser_registry=_registry(),
            policy=policy,
        )
        json_output = output_dir / f"{report.report_id}.json"
        markdown_output = output_dir / f"{report.report_id}.md"
        write_market_bar_acceptance_json(report, json_output)
        write_market_bar_acceptance_markdown(report, markdown_output)
        print(
            json.dumps(
                {
                    "schema": "stage2h-market-bar-acceptance-cli-v1",
                    "manifest_id": manifest.manifest_id,
                    "report_id": report.report_id,
                    "policy_id": policy.policy_id,
                    "acceptance_state": report.acceptance_state.value,
                    "t3_preflight_state": report.t3_preflight_state.value,
                    "cases": [
                        {
                            "case_name": item.case.case_name,
                            "case_id": item.case.case_id,
                            "report_id": item.report_id,
                            "reconciliation_report_id": (
                                item.reconciliation.report_id
                            ),
                            "acceptance_state": item.acceptance_state.value,
                            "t3_preflight_state": item.t3_preflight_state.value,
                            "non_synthetic_declared": item.non_synthetic_declared,
                            "missing_assurance_kinds": [
                                value.value
                                for value in item.assurance_coverage.missing_kinds
                            ],
                        }
                        for item in report.cases
                    ],
                    "open_blockers": list(report.open_blockers),
                    "json_output": str(json_output),
                    "markdown_output": str(markdown_output),
                    "trusted_assurance_authority_configured": False,
                    "operational_acceptance_complete": False,
                    "license_clearance_complete": False,
                    "research_grade": False,
                    "t3_reached": False,
                    "production_database_modified": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return (
            2
            if report.acceptance_state is MarketBarAcceptanceState.HARD_BLOCKED
            else 0
        )
    except (MarketBarAcceptanceError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": "stage2h-market-bar-acceptance-cli-error-v1",
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
