from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.storage.runtime_migrations import (
    RuntimeMigrationError,
    migrate_runtime_database,
    report_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args(argv)
    try:
        report = migrate_runtime_database(
            args.database,
            apply=args.apply,
            backup=args.backup,
        )
    except RuntimeMigrationError as exc:
        if exc.report is not None:
            print(report_json(exc.report), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    print(report_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
