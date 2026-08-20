from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_tracker.collector.free_stockdb import (
    FreeStockDbContractError,
    FreeStockDbProvider,
)
from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _hash_file(value: str | Path, name: str) -> tuple[Path, str, int]:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise FreeStockDbContractError(f"{name} must not be a symlink")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise FreeStockDbContractError(f"{name} cannot be resolved") from exc
    if not path.is_file():
        raise FreeStockDbContractError(f"{name} must be a regular file")
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    if (
        before.st_size != size
        or after.st_size != size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
    ):
        raise FreeStockDbContractError(f"{name} changed while being hashed")
    if size == 0:
        raise FreeStockDbContractError(f"{name} must not be empty")
    return path, digest.hexdigest(), size


def _binary_inventory(values: list[str]) -> tuple[list[dict[str, object]], str]:
    if not values or len(values) > 20:
        raise FreeStockDbContractError("--binary-path requires 1 to 20 files")
    resolved: list[tuple[Path, str, int]] = [
        _hash_file(value, "binary path") for value in values
    ]
    paths = [item[0] for item in resolved]
    if len(set(paths)) != len(paths):
        raise FreeStockDbContractError("--binary-path values must be unique")
    inventory = sorted(
        (
            {
                "file_name": path.name,
                "byte_size": size,
                "sha256": digest,
            }
            for path, digest, size in resolved
        ),
        key=lambda item: (
            str(item["file_name"]),
            str(item["sha256"]),
            int(item["byte_size"]),
        ),
    )
    encoded = json.dumps(
        inventory,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return inventory, hashlib.sha256(encoded).hexdigest()


def _atomic_create(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FreeStockDbContractError("output path must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
        raise FreeStockDbContractError("output path cannot traverse a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FreeStockDbContractError(
                "output path appeared during atomic create"
            ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a pinned localhost free-stockdb release as a read-only T1 "
            "WARM/COLD sidecar. No production database, training, backtest, "
            "trust promotion, write API, or public redistribution operation is available."
        )
    )
    parser.add_argument("--host", default="127.0.0.1:7899")
    parser.add_argument("--release-version", required=True)
    parser.add_argument(
        "--binary-path",
        action="append",
        required=True,
        help="Repeat for each audited executable/library in the pinned release.",
    )
    parser.add_argument("--data-snapshot-manifest-path", required=True)
    parser.add_argument("--sync-manifest-path", required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--interval", choices=("1d", "1m"), default="1d")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        symbols = tuple(sorted(set(args.symbol)))
        if len(symbols) != len(args.symbol):
            raise FreeStockDbContractError("--symbol values must be unique")
        if len(symbols) > 100:
            raise FreeStockDbContractError("verification accepts at most 100 symbols")
        binary_inventory, binary_identity = _binary_inventory(args.binary_path)
        data_manifest_path, data_manifest_sha256, data_manifest_size = _hash_file(
            args.data_snapshot_manifest_path,
            "data snapshot manifest",
        )
        sync_manifest_path, sync_manifest_sha256, sync_manifest_size = _hash_file(
            args.sync_manifest_path,
            "sync manifest",
        )
        start = datetime.combine(args.start, time.min, tzinfo=_SHANGHAI)
        end = datetime.combine(args.end, time.max, tzinfo=_SHANGHAI)
        cfg = ProviderConfig(
            name="free_stockdb",
            cls="FreeStockDbProvider",
            markets=["a"],
            enabled=True,
            primary=False,
            supports_snapshot=False,
            timeout_ms=5000,
            max_rps=20,
            host=args.host,
            bars_priority=30,
            read_only=True,
            trust_tier="T1_BEST_EFFORT",
            allow_live_decision=False,
            allow_model_training=False,
            allow_public_redistribution=False,
            release_version=args.release_version,
            binary_inventory_sha256=binary_identity,
            data_snapshot_manifest_sha256=data_manifest_sha256,
            sync_manifest_sha256=sync_manifest_sha256,
        )
        provider = FreeStockDbProvider(cfg)
        results: list[dict[str, object]] = []
        for symbol in symbols:
            bars, evidence = provider.fetch_bars_with_evidence(
                symbol,
                T.Market.A,
                interval=args.interval,
                start=start,
                end=end,
                adjust="raw",
            )
            results.append(
                {
                    "symbol": symbol,
                    "bar_count": len(bars),
                    "first_bar_at": (
                        None if not bars else bars[0].timestamp.isoformat()
                    ),
                    "last_bar_at": (
                        None if not bars else bars[-1].timestamp.isoformat()
                    ),
                    "query_contract_valid": True,
                    "evidence": evidence.as_dict(),
                }
            )
        report = {
            "schema": "stage3c-free-stockdb-sidecar-verification-v1",
            "generated_at": datetime.now(tz=_SHANGHAI).isoformat(),
            "operator_declared_release_version": args.release_version,
            "binary_inventory": binary_inventory,
            "binary_inventory_sha256": binary_identity,
            "data_snapshot_manifest": {
                "file_name": data_manifest_path.name,
                "byte_size": data_manifest_size,
                "sha256": data_manifest_sha256,
            },
            "sync_manifest": {
                "file_name": sync_manifest_path.name,
                "byte_size": sync_manifest_size,
                "sha256": sync_manifest_sha256,
            },
            "identity_source": "COMPUTED_FROM_LOCAL_FILES",
            "host": args.host,
            "interval": args.interval,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "symbols": list(symbols),
            "results": results,
            "all_queries_contract_valid": True,
            "all_queries_nonempty": all(item["bar_count"] > 0 for item in results),
            "running_process_identity_attested": False,
            "data_manifest_entries_verified": False,
            "trust_tier": "T1_BEST_EFFORT",
            "role": "WARM_COLD_SIDECAR_POC",
            "license_status": "LICENSE_PENDING",
            "evidence_tier_status": "T3_NOT_REACHED",
            "allow_live_decision": False,
            "allow_model_training": False,
            "allow_public_redistribution": False,
            "production_database_modified": False,
        }
        encoded = (
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        output_candidate = Path(args.output).expanduser()
        if output_candidate.is_symlink():
            raise FreeStockDbContractError("output path must not be a symlink")
        output = output_candidate.absolute()
        _atomic_create(output, encoded)
        print(encoded.decode("utf-8"), end="")
        return 0
    except (FreeStockDbContractError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
