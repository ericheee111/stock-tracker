"""Deterministic event replay with optional offline DuckDB execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sidecars.xtp.contracts import canonical_json_bytes, validate_symbol

from .store import MarketEventStore, MarketEventStoreError

_BACKEND_VALUES = frozenset({"auto", "python", "duckdb"})


@dataclass(frozen=True, slots=True)
class ReplayResult:
    replay_id: str
    symbol: str
    start_at: datetime
    end_at: datetime
    rows: tuple[dict[str, Any], ...]
    backend_requested: str
    backend_used: str
    duckdb_available: bool
    integrity_verified: bool
    integrity_partition_count: int
    synthetic_fixture_only: bool = False
    production_database_modified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "stock-tracker-market-event-replay-v2",
            "replay_id": self.replay_id,
            "symbol": self.symbol,
            "start_at": self.start_at.astimezone(timezone.utc).isoformat(),
            "end_at": self.end_at.astimezone(timezone.utc).isoformat(),
            "row_count": len(self.rows),
            "backend_requested": self.backend_requested,
            "backend_used": self.backend_used,
            "duckdb_available": self.duckdb_available,
            "integrity_verified": self.integrity_verified,
            "integrity_partition_count": self.integrity_partition_count,
            "rows": list(self.rows),
            "synthetic_fixture_only": self.synthetic_fixture_only,
            "production_database_modified": self.production_database_modified,
        }


class MarketEventReplay:
    """Replay normalized local events without arbitrary SQL or network extensions."""

    def __init__(self, store: MarketEventStore) -> None:
        if not isinstance(store, MarketEventStore):
            raise TypeError("store must be MarketEventStore")
        self.store = store

    @staticmethod
    def _require_bounds(start_at: datetime, end_at: datetime) -> None:
        for value, name in ((start_at, "start_at"), (end_at, "end_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise MarketEventStoreError(f"{name} must be timezone-aware")
        if end_at < start_at:
            raise MarketEventStoreError("replay end cannot precede start")

    @staticmethod
    def _identity(
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        rows: list[dict[str, Any]],
        backend_used: str,
    ) -> str:
        identity = {
            "schema": "stock-tracker-market-event-replay-identity-v2",
            "symbol": symbol,
            "start_at": start_at.astimezone(timezone.utc).isoformat(),
            "end_at": end_at.astimezone(timezone.utc).isoformat(),
            "backend_used": backend_used,
            "event_ids": [row["event_id"] for row in rows],
            "record_hashes": [row["record_hash"] for row in rows],
        }
        return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()

    def run(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
        backend: str = "auto",
        limit: int = 10000,
        synthetic_fixture_only: bool = False,
        record_run: bool = True,
    ) -> ReplayResult:
        symbol = validate_symbol(symbol)
        self._require_bounds(start_at, end_at)
        if backend not in _BACKEND_VALUES:
            raise MarketEventStoreError("backend must be auto, python, or duckdb")
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise MarketEventStoreError("replay limit must be 1-100000")
        if type(synthetic_fixture_only) is not bool:
            raise MarketEventStoreError("synthetic_fixture_only must be boolean")
        if type(record_run) is not bool:
            raise MarketEventStoreError("record_run must be boolean")
        duckdb_available = importlib.util.find_spec("duckdb") is not None
        if backend == "duckdb" and not duckdb_available:
            raise MarketEventStoreError("DuckDB is not installed")
        backend_used = (
            "duckdb" if backend == "duckdb" or (backend == "auto" and duckdb_available) else "python"
        )
        base_rows = self.store.list_events(
            symbol,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        partition_keys = tuple(
            sorted({str(row["partition_key"]) for row in base_rows})
        )
        for offset in range(0, len(partition_keys), 500):
            integrity = self.store.verify_integrity(
                partition_keys=partition_keys[offset : offset + 500]
            )
            if not integrity["passed"]:
                raise MarketEventStoreError(
                    "replay source partition integrity verification failed"
                )
        rows = self._duckdb_rows(base_rows, limit) if backend_used == "duckdb" else base_rows
        replay_id = self._identity(symbol, start_at, end_at, rows, backend_used)
        if record_run:
            self._record_run(
                replay_id,
                symbol,
                start_at,
                end_at,
                len(rows),
                backend_used,
            )
        return ReplayResult(
            replay_id=replay_id,
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
            rows=tuple(rows),
            backend_requested=backend,
            backend_used=backend_used,
            duckdb_available=duckdb_available,
            integrity_verified=True,
            integrity_partition_count=len(partition_keys),
            synthetic_fixture_only=synthetic_fixture_only,
        )

    @staticmethod
    def _duckdb_rows(
        rows: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MarketEventStoreError("DuckDB is not installed") from exc
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE replay_events(
                    event_id VARCHAR,
                    session_id VARCHAR,
                    symbol VARCHAR,
                    market VARCHAR,
                    event_type VARCHAR,
                    trading_day VARCHAR,
                    source_time VARCHAR,
                    received_at VARCHAR,
                    callback_seq BIGINT,
                    provider_seq BIGINT,
                    raw_payload_sha256 VARCHAR,
                    partition_key VARCHAR,
                    previous_record_hash VARCHAR,
                    record_hash VARCHAR,
                    payload_json VARCHAR
                )
                """
            )
            values = [
                (
                    row["event_id"],
                    row["session_id"],
                    row["symbol"],
                    row["market"],
                    row["event_type"],
                    row["trading_day"],
                    row["source_time"],
                    row["received_at"],
                    row["callback_seq"],
                    row["provider_seq"],
                    row["raw_payload_sha256"],
                    row["partition_key"],
                    row["previous_record_hash"],
                    row["record_hash"],
                    json.dumps(
                        row["payload"],
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
                for row in rows
            ]
            if values:
                connection.executemany(
                    """
                    INSERT INTO replay_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
            records = connection.execute(
                """
                SELECT * FROM replay_events
                ORDER BY source_time,callback_seq,event_id LIMIT ?
                """,
                [limit],
            ).fetchall()
            names = [column[0] for column in connection.description]
            output: list[dict[str, Any]] = []
            for record in records:
                row = dict(zip(names, record, strict=True))
                row["payload"] = json.loads(row.pop("payload_json"))
                output.append(row)
            return output
        finally:
            connection.close()

    def _record_run(
        self,
        replay_id: str,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        count: int,
        backend: str,
    ) -> None:
        connection = sqlite3.connect(self.store.metadata_db)
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO replay_runs(
                    replay_id,created_at,symbol,start_at,end_at,row_count,backend
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    replay_id,
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    start_at.astimezone(timezone.utc).isoformat(),
                    end_at.astimezone(timezone.utc).isoformat(),
                    count,
                    backend,
                ),
            )
            connection.commit()
        finally:
            connection.close()
