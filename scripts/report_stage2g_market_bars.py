"""Materialize and reconcile the committed Stage 2G synthetic golden bar pack.

The command is offline and deterministic.  It writes only to caller-selected
artifact/report directories, never touches ``data/stock_tracker.db``, and never
promotes fixture results to verified, research-grade, or investment evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig
from stock_tracker.quant.data import (
    MarketBarCandidateState,
    MarketBarParserBinding,
    MarketBarReconciliationError,
    MarketBarReconciliationPolicy,
    load_market_bar_golden_pack,
    materialize_golden_case,
    write_market_bar_reconciliation_json,
    write_market_bar_reconciliation_markdown,
)
from stock_tracker.quant.data.market_bar_golden import MarketBarGoldenError

DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "tests_quant"
    / "fixtures"
    / "market_bar_golden"
    / "v1"
    / "manifest.json"
)
_CASE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate deterministic Stage 2G cross-source reconciliation reports "
            "from the committed synthetic A/HK/US golden raw payload pack."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Golden case name; repeatable. Omit to process every case.",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "stage2g-market-bar-artifacts",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "stage2g-market-bar-reports",
    )
    parser.add_argument(
        "--policy-version",
        default="stage2g-market-bar-policy-v1",
    )
    parser.add_argument("--minimum-independent-sources", type=int, default=2)
    parser.add_argument("--price-tolerance-bps", type=int, default=5)
    parser.add_argument("--volume-tolerance-bps", type=int, default=50)
    parser.add_argument("--amount-tolerance-bps", type=int, default=100)
    parser.add_argument("--turnover-tolerance-bps", type=int, default=100)
    return parser


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_output_roots(
    *,
    manifest: Path,
    artifact_root: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    manifest_path = manifest.resolve(strict=True)
    fixture_root = manifest_path.parent.resolve(strict=True)
    artifact_input = artifact_root.expanduser()
    output_input = output_dir.expanduser()
    for path, name in (
        (artifact_input, "--artifact-root"),
        (output_input, "--output-dir"),
    ):
        if path.exists() and _is_link(path):
            raise MarketBarGoldenError(f"{name} cannot be a symlink or junction")
    artifact_path = artifact_input.resolve(strict=False)
    output_path = output_input.resolve(strict=False)
    if _overlaps(artifact_path, fixture_root) or _overlaps(output_path, fixture_root):
        raise MarketBarGoldenError(
            "generated output roots cannot overlap the committed golden fixture tree"
        )
    if _overlaps(artifact_path, output_path):
        raise MarketBarGoldenError(
            "--artifact-root and --output-dir must be separate non-overlapping trees"
        )
    production_database = (PROJECT_ROOT / "data" / "stock_tracker.db").resolve(
        strict=False
    )
    if artifact_path == production_database or output_path == production_database:
        raise MarketBarGoldenError("Stage 2G outputs cannot target the production database")
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
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest, artifact_root, output_dir = _validate_output_roots(
            manifest=args.manifest,
            artifact_root=args.artifact_root,
            output_dir=args.output_dir,
        )
        pack = load_market_bar_golden_pack(manifest)
        requested = tuple(args.cases or (item.case_name for item in pack.cases))
        if not requested or len(set(requested)) != len(requested):
            raise MarketBarGoldenError("--case values must be non-empty and unique")
        available = {item.case_name for item in pack.cases}
        unknown = sorted(set(requested) - available)
        if unknown:
            raise MarketBarGoldenError("unknown golden case: " + ", ".join(unknown))
        if any(_CASE_TOKEN.fullmatch(item) is None for item in requested):
            raise MarketBarGoldenError("golden case name is unsafe for report filenames")
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
        registry = _registry()
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, object]] = []
        hard_blocked = False
        for case_name in sorted(requested):
            _, case, report = materialize_golden_case(
                manifest_path=manifest,
                case_name=case_name,
                artifact_root=artifact_root,
                parser_registry=registry,
                policy=policy,
            )
            case_output = output_dir / case_name
            json_output = case_output / f"{report.report_id}.json"
            markdown_output = case_output / f"{report.report_id}.md"
            write_market_bar_reconciliation_json(report, json_output)
            write_market_bar_reconciliation_markdown(report, markdown_output)
            hard_blocked = hard_blocked or (
                report.candidate_state is MarketBarCandidateState.HARD_BLOCKED
            )
            results.append(
                {
                    "case_name": case.case_name,
                    "case_id": case.case_id,
                    "report_id": report.report_id,
                    "candidate_state": report.candidate_state.value,
                    "finding_counts": report.finding_counts,
                    "open_blockers": list(report.open_blockers),
                    "expected_session_count": len(report.expected_open_sessions),
                    "fully_observed_session_count": len(
                        report.coverage.fully_observed_sessions
                    ),
                    "json_output": str(json_output),
                    "markdown_output": str(markdown_output),
                }
            )
        print(
            json.dumps(
                {
                    "schema": "stage2g-market-bar-cli-v1",
                    "pack_id": pack.pack_id,
                    "policy_id": policy.policy_id,
                    "cases": results,
                    "synthetic_fixture_only": True,
                    "source_verification_complete": False,
                    "license_clearance_complete": False,
                    "t3_reached": False,
                    "research_grade": False,
                    "production_database_modified": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2 if hard_blocked else 0
    except (MarketBarGoldenError, MarketBarReconciliationError, OSError) as exc:
        print(
            json.dumps(
                {
                    "schema": "stage2g-market-bar-cli-error-v1",
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
