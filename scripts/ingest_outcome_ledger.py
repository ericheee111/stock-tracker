#!/usr/bin/env python3
"""Append one terminal SignalOutcome document to the Stage 4F ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.quant.storage import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_RECORD_ROOT,
    OutcomeLedger,
    OutcomeLedgerError,
    read_signal_outcome_json,
)


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


def _validate_paths(
    input_path: Path,
    record_root: Path,
    catalog_path: Path,
) -> tuple[Path, Path, Path]:
    source = _checked_path(input_path, "outcome input")
    root = _checked_path(record_root, "outcome record root")
    catalog = _checked_path(catalog_path, "outcome catalog")
    production = (PROJECT_ROOT / "data" / "stock_tracker.db").resolve(strict=False)
    if source == production or root == production or catalog == production:
        raise OutcomeLedgerError("Stage 4F paths cannot target the production database")
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise OutcomeLedgerError(
            "outcome input must be an existing regular file"
        ) from exc
    if not source.is_file():
        raise OutcomeLedgerError("outcome input must be a regular file")
    if (
        _same_existing_file(source, production, "input/production database")
        or _same_existing_file(catalog, production, "catalog/production database")
    ):
        raise OutcomeLedgerError("Stage 4F paths cannot target the production database")
    if catalog.is_relative_to(root):
        raise OutcomeLedgerError("outcome catalog must be outside the record root")
    if root.exists() and not root.is_dir():
        raise OutcomeLedgerError("outcome record root must be a directory")
    if catalog.exists() and not catalog.is_file():
        raise OutcomeLedgerError("outcome catalog must be a file")
    return source, root, catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append one strictly validated terminal SignalOutcome to the "
            "independent Stage 4F evidence ledger."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--record-root", type=Path, default=DEFAULT_RECORD_ROOT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--recorded-by", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source, record_root, catalog = _validate_paths(
            args.input,
            args.record_root,
            args.catalog,
        )
        outcome = read_signal_outcome_json(source)
        ledger = OutcomeLedger(
            record_root,
            catalog,
            production_database=PROJECT_ROOT / "data" / "stock_tracker.db",
        )
        result = ledger.append(
            outcome,
            recorded_by=args.recorded_by,
        )
        audit = ledger.audit()
        print(
            json.dumps(
                {
                    "schema": "stage4f-outcome-ledger-ingest-cli-v1",
                    "disposition": result.disposition.value,
                    "append_order": result.record.append_order,
                    "record_hash": result.record.record_hash,
                    "outcome_id": outcome.outcome_id,
                    "signal_id": outcome.signal_id,
                    "lane": result.record.lane.value,
                    "outcome_state": outcome.state.value,
                    "origin": outcome.origin.value,
                    "evidence_tier": outcome.evidence_tier.value,
                    "outcome_contract_eligible": outcome.real_scoreboard_eligible,
                    "trusted_ledger_admitted": False,
                    "trusted_outcome_authority_configured": False,
                    "ledger_audit_id": audit.audit_id,
                    "ledger_record_count": audit.record_count,
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
                    "schema": "stage4f-outcome-ledger-ingest-error-v1",
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
