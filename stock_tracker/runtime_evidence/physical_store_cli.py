"""Explicit local B1 physical storage operations; no provider, worker or trading.

python -m stock_tracker.runtime_evidence.physical_store_cli --help
No production/default root. init/append/recover mutate only with --apply.
Audit reserves the SQLite writer and can trigger SQLite's own hot-journal
recovery; it is not a forensic, guaranteed-zero-filesystem-write reader.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from stock_tracker.runtime_evidence.physical_store import (
    MAX_BYTES,
    SCOPE,
    EvidenceInput,
    PhysicalEvidenceStore,
    PhysicalStoreError,
    _decode,
    _root_path,
)


def _base(status: str) -> dict[str, Any]:
    return {"schema": "b1-physical-cli-v1", "scope": SCOPE, "status": status,
            "auto_trade": False, "trusted_admission": False,
            "market_coverage_verified": False, "power_loss_certified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "audit", "recover", "append", "page"):
        item = sub.add_parser(command)
        item.add_argument("--root", required=True, type=Path)
        if command != "init":
            item.add_argument("--store-id", required=True)
        if command in ("init", "recover", "append"):
            item.add_argument("--apply", action="store_true")
        if command == "append":
            item.add_argument("--input", required=True, type=Path,
                              help="strict canonical EvidenceInput document; decimal strings, not floats")
        if command == "page":
            item.add_argument("--after-order", type=int, default=0)
            item.add_argument("--limit", type=int, default=128)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            root = _root_path(args.root)
            if root.exists() or root.is_symlink():
                raise PhysicalStoreError("INITIALIZATION_REQUIRES_NEW_ROOT")
            if not args.apply:
                result = _base("INITIALIZATION_PLAN")
                result.update(mode="DRY_RUN", would_create=True, record_publications_modified=False)
            else:
                store = PhysicalEvidenceStore.initialize(root)
                result = {**_base("INITIALIZED"), **store.audit().to_dict(), "mode": "APPLY"}
        else:
            store = PhysicalEvidenceStore(args.root, args.store_id)
            if args.command == "append":
                with args.input.open("rb") as handle:
                    value = EvidenceInput.from_document(_decode(handle.read(MAX_BYTES + 1)))
                if not args.apply:
                    result = _base("INPUT_VALIDATED_NOT_APPENDED")
                    result.update(mode="DRY_RUN", request_id=value.request_id,
                                  record_publications_modified=False)
                else:
                    record = store.append(value)
                    result = _base("PUBLISHED")
                    result.update(mode="APPLY", append_order=record.append_order,
                                  record_id=record.record_id, storage_key=record.storage_key,
                                  file_sha256=record.content_hash)
            elif args.command == "page":
                page = store.read_page(store.snapshot(), after_order=args.after_order, limit=args.limit)
                result = _base("PHYSICAL_PAGE")
                result.update(store_id=store.store_id, high_water=page.snapshot.high_water,
                              snapshot_id=page.snapshot.snapshot_id, has_more=page.has_more,
                              records=[{"append_order": r.append_order, "record_id": r.record_id,
                                        "stream": r.input.stream.value, "storage_key": r.storage_key,
                                        "file_sha256": r.content_hash} for r in page.records])
            else:
                if args.command == "recover" and args.apply:
                    store.recover()
                audit = store.audit()
                result = {**_base("READY" if audit.ready else "RECOVERY_REQUIRED"), **audit.to_dict(),
                          "mode": "APPLY" if args.command == "recover" and args.apply else "AUDIT",
                          "sqlite_hot_journal_recovery_possible": True}
                print(json.dumps(result, sort_keys=True, ensure_ascii=False))
                return 0 if audit.ready else 2
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (PhysicalStoreError, OSError) as exc:
        result = _base("BLOCKED")
        result["error_code"] = exc.code if isinstance(exc, PhysicalStoreError) else "LOCAL_IO_ERROR"
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
