"""Plan or explicitly apply stock-tracker quantitative SQLite migrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.quant.storage import (
    MigrationContractError,
    apply_database,
    plan_database,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Checksum-verified quant schema migration. The default is dry-run; "
            "--apply is required to modify the selected database."
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit SQLite database path. No production database is assumed.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply pending migrations. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--json-output",
        help="Optional path for the migration plan/result JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = apply_database(args.database) if args.apply else plan_database(args.database)
    except (MigrationContractError, OSError, ValueError) as exc:
        print(f"quant migration failed: {exc}", file=sys.stderr)
        return 2
    result = plan.as_dict()
    result["mode"] = "APPLY" if args.apply else "DRY_RUN"
    result["database_modified"] = bool(args.apply)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
