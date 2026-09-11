"""Explicit init and read-only audit of a manual plan store; no production migration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .domain import PlanningError
from .store import PlanningStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地手工持仓计划库；不连接券商、不自动交易")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="显式创建新的计划库；拒绝覆盖")
    init.add_argument("--database", required=True)
    audit = commands.add_parser("audit", help="只读重放计划日志，不打印持仓内容")
    audit.add_argument("--database", required=True)
    audit.add_argument("--store-id", required=True)
    args = parser.parse_args(argv)
    try:
        store = PlanningStore.create(Path(args.database)) if args.command == "init" else PlanningStore(Path(args.database), args.store_id)
        book = store.read()
        print(json.dumps({"schema": "manual-planning-cli-v1", "store_id": store.store_id,
                          "revision": book["revision"], "command": args.command,
                          "assurance": "MANUAL_UNVERIFIED", "auto_trade": False,
                          "production_database_modified": False}, ensure_ascii=False))
        return 0
    except (PlanningError, OSError) as error:
        print(json.dumps({"error": getattr(error, "code", "PLANNING_IO_ERROR"),
                          "auto_trade": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
