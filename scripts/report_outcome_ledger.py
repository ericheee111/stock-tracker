#!/usr/bin/env python3
"""Audit the Stage 4F ledger and emit an exact-cohort Scoreboard snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.core.types import Market
from stock_tracker.quant.core.outcomes import OutcomeScoreboardPolicy
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.storage import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_RECORD_ROOT,
    OutcomeLedger,
    OutcomeLedgerError,
    write_outcome_scoreboard_json,
    write_outcome_scoreboard_markdown,
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


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_path(path: Path, name: str) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise OutcomeLedgerError(f"{name} cannot traverse a symlink or junction")
    return absolute.resolve(strict=False)


def _same_existing_file(left: Path, right: Path, name: str) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right)
    except OSError as exc:
        raise OutcomeLedgerError(f"cannot verify {name} file identity") from exc


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_paths(
    record_root: Path,
    catalog_path: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    root = _checked_path(record_root, "outcome record root")
    catalog = _checked_path(catalog_path, "outcome catalog")
    output = _checked_path(output_dir, "outcome report root")
    production = (PROJECT_ROOT / "data" / "stock_tracker.db").resolve(strict=False)
    if production in {root, catalog, output}:
        raise OutcomeLedgerError("Stage 4F paths cannot target the production database")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise OutcomeLedgerError(
            "outcome record root must be an existing directory"
        ) from exc
    try:
        catalog = catalog.resolve(strict=True)
    except OSError as exc:
        raise OutcomeLedgerError(
            "outcome catalog must be an existing regular file"
        ) from exc
    if (
        _same_existing_file(root, production, "record root/production database")
        or _same_existing_file(catalog, production, "catalog/production database")
        or _same_existing_file(output, production, "report root/production database")
    ):
        raise OutcomeLedgerError("Stage 4F paths cannot target the production database")
    if not root.is_dir():
        raise OutcomeLedgerError("outcome record root must be a directory")
    if not catalog.is_file():
        raise OutcomeLedgerError("outcome catalog must be a file")
    if output.exists() and not output.is_dir():
        raise OutcomeLedgerError("outcome report root must be a directory")
    if catalog.is_relative_to(root):
        raise OutcomeLedgerError("outcome catalog must be outside the record root")
    if _overlaps(output, root) or output == catalog or catalog.is_relative_to(output):
        raise OutcomeLedgerError(
            "outcome report root must be separate from records and catalog"
        )
    return root, catalog, output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit an append-only Stage 4F outcome ledger and materialize one "
            "exact-cohort Strategy Scoreboard snapshot."
        )
    )
    parser.add_argument("--record-root", type=Path, default=DEFAULT_RECORD_ROOT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--market", choices=[item.value for item in Market], required=True)
    parser.add_argument("--horizon-sessions", type=int, required=True)
    parser.add_argument("--model-id")
    parser.add_argument(
        "--evidence-tier",
        choices=[item.value for item in DataTrustTier],
        required=True,
    )
    parser.add_argument("--window-start", type=_aware_datetime, required=True)
    parser.add_argument("--window-end", type=_aware_datetime, required=True)
    parser.add_argument("--as-of", type=_aware_datetime, required=True)
    parser.add_argument(
        "--policy-version",
        default="stage4f-outcome-scoreboard-policy-v1",
    )
    parser.add_argument("--minimum-real-samples", type=int, default=30)
    parser.add_argument("--minimum-bucket-samples", type=int, default=5)
    parser.add_argument("--recent-window", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record_root, catalog, output_dir = _validate_paths(
            args.record_root,
            args.catalog,
            args.output_dir,
        )
        policy = OutcomeScoreboardPolicy(
            policy_version=args.policy_version,
            minimum_real_samples=args.minimum_real_samples,
            minimum_bucket_samples=args.minimum_bucket_samples,
            recent_window=args.recent_window,
        )
        ledger = OutcomeLedger(
            record_root,
            catalog,
            production_database=PROJECT_ROOT / "data" / "stock_tracker.db",
        )
        snapshot = ledger.materialize_scoreboard(
            strategy_id=args.strategy_id,
            strategy_version=args.strategy_version,
            market=Market(args.market),
            horizon_sessions=args.horizon_sessions,
            model_id=args.model_id,
            evidence_tier=DataTrustTier(args.evidence_tier),
            window_start=args.window_start,
            window_end=args.window_end,
            as_of=args.as_of,
            policy=policy,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        json_output = output_dir / f"{snapshot.snapshot_id}.json"
        markdown_output = output_dir / f"{snapshot.snapshot_id}.md"
        write_outcome_scoreboard_json(snapshot, json_output)
        write_outcome_scoreboard_markdown(snapshot, markdown_output)
        scoreboard = snapshot.scoreboard
        print(
            json.dumps(
                {
                    "schema": "stage4f-outcome-ledger-report-cli-v1",
                    "snapshot_id": snapshot.snapshot_id,
                    "ledger_audit_id": snapshot.audit.audit_id,
                    "ledger_record_count": snapshot.audit.record_count,
                    "candidate_record_count": len(
                        snapshot.candidate_record_hashes
                    ),
                    "outcome_contract_eligible_count": (
                        snapshot.outcome_contract_eligible_count
                    ),
                    "trusted_admitted_record_count": len(snapshot.record_hashes),
                    "scoreboard_id": scoreboard.scoreboard_id,
                    "scoreboard_state": scoreboard.state.value,
                    "eligible_real_sample_count": len(
                        scoreboard.eligible_outcome_ids
                    ),
                    "admission_blockers": list(snapshot.admission_blockers),
                    "scoreboard_blockers": list(scoreboard.blockers),
                    "blockers": sorted(
                        set(snapshot.admission_blockers).union(scoreboard.blockers)
                    ),
                    "metrics_available": scoreboard.metrics is not None,
                    "trusted_outcome_authority_configured": False,
                    "json_output": str(json_output),
                    "markdown_output": str(markdown_output),
                    "investment_performance_claim": False,
                    "production_database_modified": False,
                    "auto_promote_model": False,
                    "auto_change_strategy_weight": False,
                    "auto_trade": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, OutcomeLedgerError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": "stage4f-outcome-ledger-report-error-v1",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "investment_performance_claim": False,
                    "production_database_modified": False,
                    "auto_trade": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
