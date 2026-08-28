"""Append-only partitioned market-event store with separate SQLite metadata.

The production ``data/stock_tracker.db`` is never opened here. Every accepted
callback is persisted as one immutable canonical record file. Partition
manifests and SQLite metadata bind the record hash chain, source/session
identity, sequence findings, and deterministic minute aggregation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sidecars.xtp.contracts import (
    EventEnvelope,
    XtpSidecarContractError,
    canonical_json_bytes,
    strict_json_loads,
    validate_symbol,
)

from .contracts import (
    EventDisposition,
    GapKind,
    IngestionFinding,
    IngestionResult,
    MinuteBarRecord,
    MinuteCompleteness,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EVENT_STORE_SCHEMA = "stock-tracker-market-event-store-v3"
_PREVIOUS_EVENT_STORE_SCHEMA = "stock-tracker-market-event-store-v2"
_RECORD_SCHEMA = "stock-tracker-market-event-record-v1"
_PARTITION_MANIFEST_SCHEMA = "stock-tracker-market-event-partition-v3"
_QUARANTINE_SCHEMA = "stock-tracker-market-event-quarantine-v1"
_ZERO_HASH = "0" * 64
_MAX_QUARANTINE_INPUT_BYTES = 16 * 1024 * 1024


class MarketEventStoreError(RuntimeError):
    """Raised when event storage or metadata integrity fails."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise MarketEventStoreError("refusing to write through a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _safe_child(root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise MarketEventStoreError("invalid storage key")
    candidate_path = Path(relative)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise MarketEventStoreError("storage key escaped root")
    resolved_root = root.resolve(strict=False)
    resolved = (resolved_root / candidate_path).resolve(strict=False)
    if os.path.commonpath((str(resolved_root), str(resolved))) != str(resolved_root):
        raise MarketEventStoreError("storage key escaped root")
    cursor = resolved_root
    for part in candidate_path.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise MarketEventStoreError("storage path contains a symlink")
    return resolved


def _source_time(event: EventEnvelope) -> datetime:
    return event.exchange_timestamp or event.provider_timestamp or event.received_at


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _record_identity(
    event: EventEnvelope,
    previous_record_hash: str,
) -> dict[str, Any]:
    return {
        "schema": _RECORD_SCHEMA,
        "event": event.as_dict(),
        "previous_record_hash": previous_record_hash,
    }


def _record_document(
    event: EventEnvelope,
    previous_record_hash: str,
) -> tuple[dict[str, Any], str]:
    identity = _record_identity(event, previous_record_hash)
    record_hash = _sha256(canonical_json_bytes(identity))
    document = dict(identity)
    document["record_hash"] = record_hash
    return document, record_hash


def _strict_record(raw: bytes) -> tuple[EventEnvelope, str, str]:
    try:
        document = strict_json_loads(raw.rstrip(b"\n"))
    except XtpSidecarContractError as exc:
        raise MarketEventStoreError("stored record is not strict JSON") from exc
    expected = {"schema", "event", "previous_record_hash", "record_hash"}
    if not isinstance(document, dict) or set(document) != expected:
        raise MarketEventStoreError("stored record field set is invalid")
    if document["schema"] != _RECORD_SCHEMA:
        raise MarketEventStoreError("stored record schema is invalid")
    previous = document["previous_record_hash"]
    record_hash = document["record_hash"]
    if (
        type(previous) is not str
        or type(record_hash) is not str
        or len(previous) != 64
        or len(record_hash) != 64
    ):
        raise MarketEventStoreError("stored record hash fields are invalid")
    try:
        event = EventEnvelope.from_dict(document["event"])
    except XtpSidecarContractError as exc:
        raise MarketEventStoreError("stored event contract is invalid") from exc
    identity = {
        "schema": document["schema"],
        "event": document["event"],
        "previous_record_hash": previous,
    }
    if _sha256(canonical_json_bytes(identity)) != record_hash:
        raise MarketEventStoreError("stored record hash mismatch")
    return event, previous, record_hash


class MarketEventStore:
    """Store immutable callback snapshots and queryable integrity metadata."""

    def __init__(
        self,
        root: str | Path,
        metadata_db: str | Path,
        *,
        quarantine_root: str | Path | None = None,
    ) -> None:
        raw_root = Path(root)
        raw_metadata = Path(metadata_db)
        raw_quarantine = Path(
            quarantine_root
            if quarantine_root is not None
            else raw_root.parent / "market-events-quarantine"
        )
        for path, name in (
            (raw_root, "event root"),
            (raw_metadata, "metadata database"),
            (raw_metadata.parent, "metadata parent"),
            (raw_quarantine, "quarantine root"),
        ):
            if path.exists() and path.is_symlink():
                raise MarketEventStoreError(f"{name} must not be a symlink")
        self.root = raw_root.resolve(strict=False)
        self.metadata_db = raw_metadata.resolve(strict=False)
        self.quarantine_root = raw_quarantine.resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_db.parent.mkdir(parents=True, exist_ok=True)
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.metadata_db, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS event_store_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            feed_mode TEXT NOT NULL,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            last_callback_seq INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            append_order INTEGER NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            event_type TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            source_time TEXT NOT NULL,
            received_at TEXT NOT NULL,
            callback_seq INTEGER NOT NULL,
            provider_seq INTEGER,
            partition_key TEXT NOT NULL,
            event_file TEXT NOT NULL UNIQUE,
            event_file_sha256 TEXT NOT NULL,
            previous_record_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            raw_payload_sha256 TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_replay
            ON events(symbol, source_time, callback_seq, event_id);
        CREATE INDEX IF NOT EXISTS idx_events_session_sequence
            ON events(session_id, callback_seq, event_id);
        CREATE INDEX IF NOT EXISTS idx_events_provider_sequence
            ON events(session_id, symbol, provider_seq);
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES events(event_id),
            kind TEXT NOT NULL,
            session_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            expected INTEGER,
            observed INTEGER,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_findings_event ON findings(event_id);
        CREATE TABLE IF NOT EXISTS partitions (
            partition_key TEXT PRIMARY KEY,
            market TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            first_record_hash TEXT NOT NULL,
            last_record_hash TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS minute_bars (
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            minute_start TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            amount REAL NOT NULL,
            event_count INTEGER NOT NULL,
            first_callback_seq INTEGER NOT NULL,
            last_callback_seq INTEGER NOT NULL,
            completeness TEXT NOT NULL,
            source TEXT NOT NULL,
            data_status TEXT NOT NULL,
            PRIMARY KEY(symbol, minute_start, source)
        );
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            accepted INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            quarantined INTEGER NOT NULL DEFAULT 0,
            last_cursor INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS replay_runs (
            replay_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            backend TEXT NOT NULL
        );
        """
        with self._connection() as connection:
            connection.executescript(schema)
            existing = connection.execute(
                "SELECT value FROM event_store_meta WHERE key='schema'"
            ).fetchone()
            existing_schema = None if existing is None else str(existing["value"])
            if existing_schema == _PREVIOUS_EVENT_STORE_SCHEMA:
                self._migrate_v2_to_v3(connection)
                existing_schema = _EVENT_STORE_SCHEMA
            if existing_schema is not None and existing_schema != _EVENT_STORE_SCHEMA:
                raise MarketEventStoreError(
                    "market-event metadata schema mismatch; use a new database path"
                )
            connection.execute(
                "INSERT OR IGNORE INTO event_store_meta(key,value) VALUES('schema',?)",
                (_EVENT_STORE_SCHEMA,),
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_append_order "
                "ON events(append_order)"
            )
            connection.commit()

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "append_order" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN append_order INTEGER")
        rows = connection.execute("SELECT rowid FROM events ORDER BY rowid").fetchall()
        for append_order, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE events SET append_order=? WHERE rowid=?",
                (append_order, row["rowid"]),
            )
        missing = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE append_order IS NULL"
            ).fetchone()[0]
        )
        if missing:
            raise MarketEventStoreError("market-event append-order migration failed")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_append_order "
            "ON events(append_order)"
        )
        connection.execute(
            "UPDATE event_store_meta SET value=? WHERE key='schema'",
            (_EVENT_STORE_SCHEMA,),
        )

    @staticmethod
    def _partition_key(event: EventEnvelope) -> str:
        return (
            f"market={event.market}/trading_day={event.trading_day}/"
            f"symbol={event.symbol}"
        )

    @staticmethod
    def _event_filename(event: EventEnvelope) -> str:
        return f"event-{event.callback_seq:012d}-{event.event_id}.json"

    def _event_file(self, event: EventEnvelope) -> tuple[str, Path]:
        partition = self._partition_key(event)
        relative = f"{partition}/{self._event_filename(event)}"
        return relative, _safe_child(self.root, relative)

    def ingest_dict(self, value: Mapping[str, Any] | bytes) -> IngestionResult:
        if isinstance(value, bytes):
            if not value or len(value) > _MAX_QUARANTINE_INPUT_BYTES:
                raw = value[:_MAX_QUARANTINE_INPUT_BYTES]
            else:
                raw = value
        else:
            try:
                raw = canonical_json_bytes(dict(value))
            except (XtpSidecarContractError, TypeError, ValueError):
                raw = b"{}"
        try:
            payload = strict_json_loads(raw) if isinstance(value, bytes) else value
            if not isinstance(payload, dict):
                raise XtpSidecarContractError("event must be an object")
            return self.append(EventEnvelope.from_dict(payload))
        except (XtpSidecarContractError, TypeError, ValueError) as exc:
            quarantine_id = _sha256(raw)
            descriptor = {
                "schema": _QUARANTINE_SCHEMA,
                "quarantine_id": quarantine_id,
                "raw_sha256": quarantine_id,
                "raw_size": len(raw),
                "error_code": type(exc).__name__,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "raw_payload_persisted": False,
                "contains_account_value": False,
            }
            target = _safe_child(self.quarantine_root, f"{quarantine_id}.json")
            _atomic_write(target, canonical_json_bytes(descriptor) + b"\n")
            return IngestionResult(
                event_id=None,
                disposition=EventDisposition.QUARANTINED,
                partition_key=None,
                findings=(
                    IngestionFinding(
                        GapKind.CONTRACT_VIOLATION,
                        "UNKNOWN",
                        "UNKNOWN",
                        None,
                        None,
                        f"quarantined contract failure: {type(exc).__name__}",
                    ),
                ),
            )

    def _sequence_findings(
        self,
        connection: sqlite3.Connection,
        event: EventEnvelope,
    ) -> list[IngestionFinding]:
        findings: list[IngestionFinding] = []
        callback_row = connection.execute(
            """
            SELECT callback_seq
            FROM events
            WHERE session_id=?
            ORDER BY callback_seq DESC,event_id DESC LIMIT 1
            """,
            (event.session_id,),
        ).fetchone()
        if callback_row is not None:
            last_callback = int(callback_row["callback_seq"])
            if event.callback_seq <= last_callback:
                findings.append(
                    IngestionFinding(
                        GapKind.OUT_OF_ORDER,
                        event.session_id,
                        event.symbol,
                        last_callback + 1,
                        event.callback_seq,
                        "session-local callback sequence did not advance",
                    )
                )
            elif event.callback_seq > last_callback + 1:
                findings.append(
                    IngestionFinding(
                        GapKind.CALLBACK_SEQUENCE,
                        event.session_id,
                        event.symbol,
                        last_callback + 1,
                        event.callback_seq,
                        "local callback sequence contains a gap; it is not an exchange sequence",
                    )
                )

        symbol_row = connection.execute(
            """
            SELECT provider_seq,source_time
            FROM events
            WHERE session_id=? AND symbol=?
            ORDER BY callback_seq DESC,event_id DESC LIMIT 1
            """,
            (event.session_id, event.symbol),
        ).fetchone()
        if symbol_row is None:
            return findings
        previous_provider = symbol_row["provider_seq"]
        if event.provider_seq is not None and previous_provider is not None:
            previous_provider = int(previous_provider)
            if event.provider_seq <= previous_provider:
                findings.append(
                    IngestionFinding(
                        GapKind.OUT_OF_ORDER,
                        event.session_id,
                        event.symbol,
                        previous_provider + 1,
                        event.provider_seq,
                        "provider sequence did not advance",
                    )
                )
            elif event.provider_seq > previous_provider + 1:
                findings.append(
                    IngestionFinding(
                        GapKind.PROVIDER_SEQUENCE,
                        event.session_id,
                        event.symbol,
                        previous_provider + 1,
                        event.provider_seq,
                        "provider sequence contains a gap",
                    )
                )
        previous_time = datetime.fromisoformat(symbol_row["source_time"])
        current_time = _source_time(event).astimezone(timezone.utc)
        if current_time < previous_time:
            findings.append(
                IngestionFinding(
                    GapKind.SOURCE_TIME_REGRESSION,
                    event.session_id,
                    event.symbol,
                    None,
                    None,
                    "source timestamp regressed relative to the previous symbol callback",
                )
            )
        return findings

    def _previous_partition_hash(
        self,
        connection: sqlite3.Connection,
        partition_key: str,
    ) -> str:
        row = connection.execute(
            """
            SELECT record_hash FROM events
            WHERE partition_key=?
            ORDER BY append_order DESC LIMIT 1
            """,
            (partition_key,),
        ).fetchone()
        return _ZERO_HASH if row is None else str(row["record_hash"])

    @staticmethod
    def _next_append_order(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(append_order),0)+1 AS next_order FROM events"
        ).fetchone()
        value = int(row["next_order"])
        if value < 1:
            raise MarketEventStoreError("invalid market-event append order")
        return value

    def append(self, event: EventEnvelope) -> IngestionResult:
        if not isinstance(event, EventEnvelope):
            raise MarketEventStoreError("event must be EventEnvelope")
        try:
            event = EventEnvelope.from_dict(event.as_dict())
        except XtpSidecarContractError as exc:
            raise MarketEventStoreError("event contract is invalid") from exc
        relative, target = self._event_file(event)
        partition_key = self._partition_key(event)
        created_file = False
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT partition_key,event_file,event_file_sha256 FROM events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if duplicate is not None:
                    duplicate_path = _safe_child(self.root, str(duplicate["event_file"]))
                    if (
                        not duplicate_path.is_file()
                        or duplicate_path.is_symlink()
                        or _sha256(duplicate_path.read_bytes())
                        != duplicate["event_file_sha256"]
                    ):
                        raise MarketEventStoreError(
                            "duplicate metadata points to a missing or modified immutable file"
                        )
                    connection.rollback()
                    return IngestionResult(
                        event_id=event.event_id,
                        disposition=EventDisposition.DUPLICATE,
                        partition_key=str(duplicate["partition_key"]),
                        findings=(),
                    )

                session = connection.execute(
                    "SELECT source,feed_mode FROM sessions WHERE session_id=?",
                    (event.session_id,),
                ).fetchone()
                if session is not None and (
                    session["source"] != event.source
                    or session["feed_mode"] != event.feed_mode
                ):
                    raise MarketEventStoreError(
                        "event source/feed_mode does not match the existing session identity"
                    )

                findings = self._sequence_findings(connection, event)
                append_order = self._next_append_order(connection)
                previous_hash = self._previous_partition_hash(connection, partition_key)
                record, record_hash = _record_document(event, previous_hash)
                event_bytes = canonical_json_bytes(record) + b"\n"
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != event_bytes:
                        raise MarketEventStoreError("immutable event file collision")
                else:
                    _atomic_write(target, event_bytes)
                    created_file = True

                now = datetime.now(timezone.utc).isoformat()
                source_time = _source_time(event).astimezone(timezone.utc).isoformat()
                received_at = _iso(event.received_at)
                assert received_at is not None
                connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id,source,feed_mode,first_received_at,last_received_at,
                        event_count,last_callback_seq
                    ) VALUES(?,?,?,?,?,1,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        last_received_at=CASE
                            WHEN excluded.last_received_at>sessions.last_received_at
                            THEN excluded.last_received_at ELSE sessions.last_received_at END,
                        event_count=sessions.event_count+1,
                        last_callback_seq=CASE
                            WHEN excluded.last_callback_seq>sessions.last_callback_seq
                            THEN excluded.last_callback_seq ELSE sessions.last_callback_seq END
                    """,
                    (
                        event.session_id,
                        event.source,
                        event.feed_mode,
                        received_at,
                        received_at,
                        event.callback_seq,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id,append_order,session_id,symbol,market,event_type,trading_day,
                        source_time,received_at,callback_seq,provider_seq,partition_key,
                        event_file,event_file_sha256,previous_record_hash,record_hash,
                        payload_json,raw_payload_sha256,ingested_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id,
                        append_order,
                        event.session_id,
                        event.symbol,
                        event.market,
                        event.event_type,
                        event.trading_day,
                        source_time,
                        received_at,
                        event.callback_seq,
                        event.provider_seq,
                        partition_key,
                        relative,
                        _sha256(event_bytes),
                        previous_hash,
                        record_hash,
                        canonical_json_bytes(event.payload).decode("utf-8"),
                        event.raw_payload_sha256,
                        now,
                    ),
                )
                for finding in findings:
                    connection.execute(
                        """
                        INSERT INTO findings(
                            event_id,kind,session_id,symbol,expected,observed,detail,created_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            event.event_id,
                            finding.kind.value,
                            finding.session_id,
                            finding.symbol,
                            finding.expected,
                            finding.observed,
                            finding.detail,
                            now,
                        ),
                    )
                self._refresh_minute_with_connection(
                    connection,
                    event.symbol,
                    _source_time(event),
                )
                self._refresh_partition_manifest(connection, partition_key)
                connection.commit()
            except Exception:
                connection.rollback()
                if created_file:
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    self._rebuild_manifest_from_committed(partition_key)
                except (OSError, sqlite3.Error, MarketEventStoreError) as recovery_exc:
                    raise MarketEventStoreError(
                        "event append failed and partition manifest recovery also failed"
                    ) from recovery_exc
                raise
        return IngestionResult(
            event_id=event.event_id,
            disposition=EventDisposition.ACCEPTED,
            partition_key=partition_key,
            findings=tuple(findings),
        )

    def _manifest_document(
        self,
        partition_key: str,
        rows: list[sqlite3.Row],
    ) -> dict[str, Any]:
        fields = dict(item.split("=", 1) for item in partition_key.split("/"))
        return {
            "schema": _PARTITION_MANIFEST_SCHEMA,
            "partition_key": partition_key,
            "market": fields["market"],
            "trading_day": fields["trading_day"],
            "symbol": fields["symbol"],
            "event_count": len(rows),
            "first_record_hash": rows[0]["record_hash"] if rows else _ZERO_HASH,
            "last_record_hash": rows[-1]["record_hash"] if rows else _ZERO_HASH,
            "events": [
                {
                    "event_id": row["event_id"],
                    "append_order": row["append_order"],
                    "event_file": row["event_file"],
                    "sha256": row["event_file_sha256"],
                    "callback_seq": row["callback_seq"],
                    "source_time": row["source_time"],
                    "previous_record_hash": row["previous_record_hash"],
                    "record_hash": row["record_hash"],
                }
                for row in rows
            ],
        }

    def _partition_rows(
        self,
        connection: sqlite3.Connection,
        partition_key: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT e.append_order,e.event_id,e.event_file,e.event_file_sha256,
                       e.session_id,e.symbol,e.market,e.event_type,e.trading_day,
                       e.source_time,e.received_at,e.callback_seq,e.provider_seq,
                       e.partition_key,e.previous_record_hash,e.record_hash,
                       e.payload_json,e.raw_payload_sha256,
                       s.source AS session_source,s.feed_mode AS session_feed_mode
                FROM events AS e
                JOIN sessions AS s ON s.session_id=e.session_id
                WHERE e.partition_key=?
                ORDER BY e.append_order
                """,
                (partition_key,),
            ).fetchall()
        )

    def _refresh_partition_manifest(
        self,
        connection: sqlite3.Connection,
        partition_key: str,
    ) -> None:
        rows = self._partition_rows(connection, partition_key)
        if not rows:
            return
        manifest = self._manifest_document(partition_key, rows)
        body = canonical_json_bytes(manifest) + b"\n"
        target = _safe_child(self.root, f"{partition_key}/manifest.json")
        _atomic_write(target, body)
        fields = dict(item.split("=", 1) for item in partition_key.split("/"))
        connection.execute(
            """
            INSERT INTO partitions(
                partition_key,market,trading_day,symbol,event_count,
                first_record_hash,last_record_hash,manifest_sha256,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(partition_key) DO UPDATE SET
                event_count=excluded.event_count,
                first_record_hash=excluded.first_record_hash,
                last_record_hash=excluded.last_record_hash,
                manifest_sha256=excluded.manifest_sha256,
                updated_at=excluded.updated_at
            """,
            (
                partition_key,
                fields["market"],
                fields["trading_day"],
                fields["symbol"],
                len(rows),
                rows[0]["record_hash"],
                rows[-1]["record_hash"],
                _sha256(body),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _rebuild_manifest_from_committed(self, partition_key: str) -> None:
        with self._connection() as connection:
            rows = self._partition_rows(connection, partition_key)
            target = _safe_child(self.root, f"{partition_key}/manifest.json")
            if not rows:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                return
            self._refresh_partition_manifest(connection, partition_key)
            connection.commit()

    @staticmethod
    def _minute_bounds(value: datetime) -> tuple[datetime, datetime]:
        local = value.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
        return local, local + timedelta(minutes=1)

    @staticmethod
    def _cumulative_delta(
        rows: list[tuple[sqlite3.Row, dict[str, Any]]],
        field: str,
    ) -> tuple[float, bool, bool]:
        total = 0.0
        regression = False
        observed = False
        previous_value: float | None = None
        previous_session: str | None = None
        for row, payload in rows:
            raw = payload.get(field)
            if type(raw) not in (int, float) or float(raw) < 0:
                continue
            current = float(raw)
            session = str(row["session_id"])
            observed = True
            if previous_value is not None and previous_session == session:
                if current < previous_value:
                    regression = True
                else:
                    total += current - previous_value
            previous_value = current
            previous_session = session
        return total, regression, observed

    @staticmethod
    def _has_cumulative_value(payload: dict[str, Any], field: str) -> bool:
        value = payload.get(field)
        return type(value) in (int, float) and float(value) >= 0

    def _refresh_minute_with_connection(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        source_time: datetime,
    ) -> None:
        """Rebuild one derived minute atomically with the accepted raw event."""

        start, end = self._minute_bounds(source_time)
        start_utc = start.astimezone(timezone.utc).isoformat()
        end_utc = end.astimezone(timezone.utc).isoformat()
        rows = connection.execute(
            """
            SELECT event_id,event_type,trading_day,source_time,callback_seq,
                   session_id,payload_json
            FROM events
            WHERE symbol=? AND source_time>=? AND source_time<?
              AND event_type IN ('MARKET_DATA','TRADE_TICK')
            ORDER BY source_time,callback_seq,event_id
            """,
            (symbol, start_utc, end_utc),
        ).fetchall()
        parsed: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            last = payload.get("last")
            if type(last) not in (int, float) or float(last) <= 0:
                continue
            parsed.append((row, payload))
        if not parsed:
            return
        prices = [float(payload["last"]) for _, payload in parsed]

        baseline_pair: tuple[sqlite3.Row, dict[str, Any]] | None = None
        baseline_row = connection.execute(
            """
            SELECT event_id,event_type,trading_day,source_time,callback_seq,
                   session_id,payload_json
            FROM events
            WHERE symbol=? AND source_time<?
              AND event_type IN ('MARKET_DATA','TRADE_TICK')
            ORDER BY source_time DESC,callback_seq DESC,event_id DESC LIMIT 1
            """,
            (symbol, start_utc),
        ).fetchone()
        if (
            baseline_row is not None
            and baseline_row["session_id"] == parsed[0][0]["session_id"]
            and baseline_row["trading_day"] == parsed[0][0]["trading_day"]
        ):
            baseline_pair = (baseline_row, json.loads(baseline_row["payload_json"]))

        delta_rows = ([baseline_pair] if baseline_pair is not None else []) + parsed
        volume_delta, volume_regression, volume_observed = self._cumulative_delta(
            delta_rows, "volume"
        )
        amount_delta, amount_regression, amount_observed = self._cumulative_delta(
            delta_rows, "amount"
        )
        volume_baseline = baseline_pair is not None and self._has_cumulative_value(
            baseline_pair[1], "volume"
        )
        amount_baseline = baseline_pair is not None and self._has_cumulative_value(
            baseline_pair[1], "amount"
        )
        first_seq = int(parsed[0][0]["callback_seq"])
        last_seq = int(parsed[-1][0]["callback_seq"])
        finding_rows = connection.execute(
            """
            SELECT DISTINCT finding.kind
            FROM findings AS finding
            JOIN events AS event ON event.event_id=finding.event_id
            WHERE event.symbol=? AND event.source_time>=? AND event.source_time<?
              AND event.event_type IN ('MARKET_DATA','TRADE_TICK')
            """,
            (symbol, start_utc, end_utc),
        ).fetchall()
        kinds = {row["kind"] for row in finding_rows}
        sessions = {str(row["session_id"]) for row, _ in parsed}
        if (
            GapKind.OUT_OF_ORDER.value in kinds
            or GapKind.SOURCE_TIME_REGRESSION.value in kinds
            or volume_regression
            or amount_regression
            or len(sessions) > 1
        ):
            completeness = MinuteCompleteness.INCOMPLETE_OUT_OF_ORDER
        elif (
            GapKind.CALLBACK_SEQUENCE.value in kinds
            or GapKind.PROVIDER_SEQUENCE.value in kinds
        ):
            completeness = MinuteCompleteness.INCOMPLETE_GAP
        elif not volume_baseline or not amount_baseline:
            completeness = MinuteCompleteness.INCOMPLETE_BASELINE
        elif (
            len(parsed) < 2
            or not volume_observed
            or not amount_observed
        ):
            completeness = MinuteCompleteness.INCOMPLETE_SPARSE
        else:
            completeness = MinuteCompleteness.COMPLETE
        bar = MinuteBarRecord(
            symbol=symbol,
            market="A",
            minute_start=start,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=max(0, round(volume_delta)),
            amount=max(0.0, amount_delta),
            event_count=len(parsed),
            first_callback_seq=first_seq,
            last_callback_seq=last_seq,
            completeness=completeness,
        )
        connection.execute(
            """
            INSERT INTO minute_bars(
                symbol,market,minute_start,open,high,low,close,volume,amount,
                event_count,first_callback_seq,last_callback_seq,completeness,source,data_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,minute_start,source) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume,amount=excluded.amount,
                event_count=excluded.event_count,
                first_callback_seq=excluded.first_callback_seq,
                last_callback_seq=excluded.last_callback_seq,
                completeness=excluded.completeness,data_status=excluded.data_status
            """,
            (
                bar.symbol,
                bar.market,
                bar.minute_start.astimezone(timezone.utc).isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.event_count,
                bar.first_callback_seq,
                bar.last_callback_seq,
                bar.completeness.value,
                bar.source,
                bar.data_status,
            ),
        )

    def list_events(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        symbol = validate_symbol(symbol)
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise MarketEventStoreError("replay start must be timezone-aware")
        if end_at.tzinfo is None or end_at.utcoffset() is None:
            raise MarketEventStoreError("replay end must be timezone-aware")
        if end_at < start_at:
            raise MarketEventStoreError("replay end cannot precede start")
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise MarketEventStoreError("replay limit must be 1-100000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id,session_id,symbol,market,event_type,trading_day,
                       source_time,received_at,callback_seq,provider_seq,payload_json,
                       raw_payload_sha256,partition_key,previous_record_hash,record_hash
                FROM events
                WHERE symbol=? AND source_time>=? AND source_time<=?
                ORDER BY source_time,callback_seq,event_id LIMIT ?
                """,
                (
                    symbol,
                    start_at.astimezone(timezone.utc).isoformat(),
                    end_at.astimezone(timezone.utc).isoformat(),
                    limit,
                ),
            ).fetchall()
            output: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                output.append(item)
            return output

    def minute_bars(
        self,
        symbol: str,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        symbol = validate_symbol(symbol)
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise MarketEventStoreError("minute-bar limit must be 1-5000")
        if (start_at is None) != (end_at is None):
            raise MarketEventStoreError(
                "minute-bar replay bounds must be provided together"
            )
        parameters: tuple[Any, ...]
        if start_at is not None and end_at is not None:
            if start_at.tzinfo is None or start_at.utcoffset() is None:
                raise MarketEventStoreError(
                    "minute-bar replay start must be timezone-aware"
                )
            if end_at.tzinfo is None or end_at.utcoffset() is None:
                raise MarketEventStoreError(
                    "minute-bar replay end must be timezone-aware"
                )
            if end_at < start_at:
                raise MarketEventStoreError(
                    "minute-bar replay end cannot precede start"
                )
            start_minute, _ = self._minute_bounds(start_at)
            end_minute, _ = self._minute_bounds(end_at)
            query = """
                SELECT * FROM minute_bars
                WHERE symbol=? AND minute_start>=? AND minute_start<=?
                ORDER BY minute_start DESC LIMIT ?
            """
            parameters = (
                symbol,
                start_minute.astimezone(timezone.utc).isoformat(),
                end_minute.astimezone(timezone.utc).isoformat(),
                limit,
            )
        else:
            query = """
                SELECT * FROM minute_bars WHERE symbol=?
                ORDER BY minute_start DESC LIMIT ?
            """
            parameters = (symbol, limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [dict(row) for row in reversed(rows)]

    def verify_integrity(
        self,
        partition_keys: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        checked_events = 0
        checked_partitions = 0
        errors: list[str] = []
        selected_keys: tuple[str, ...] | None = None
        if partition_keys is not None:
            selected_keys = tuple(sorted(set(partition_keys)))
            if len(selected_keys) > 500:
                raise MarketEventStoreError(
                    "integrity verification is limited to 500 partitions per batch"
                )
            for key in selected_keys:
                if type(key) is not str or not key or len(key) > 512:
                    raise MarketEventStoreError("integrity partition key is invalid")
                _safe_child(self.root, f"{key}/manifest.json")
        with self._connection() as connection:
            if selected_keys is None:
                partitions = connection.execute(
                    "SELECT * FROM partitions ORDER BY partition_key"
                ).fetchall()
                event_partition_keys = {
                    str(row["partition_key"])
                    for row in connection.execute(
                        "SELECT DISTINCT partition_key FROM events"
                    ).fetchall()
                }
                metadata_partition_keys = {
                    str(row["partition_key"]) for row in partitions
                }
                for missing in sorted(
                    event_partition_keys - metadata_partition_keys
                ):
                    errors.append(f"{missing}:partition:MISSING_METADATA")
                for orphan in sorted(
                    metadata_partition_keys - event_partition_keys
                ):
                    errors.append(f"{orphan}:partition:ORPHAN_METADATA")
            elif not selected_keys:
                partitions = []
            else:
                placeholders = ",".join("?" for _ in selected_keys)
                partitions = connection.execute(
                    f"SELECT * FROM partitions WHERE partition_key IN ({placeholders}) "
                    "ORDER BY partition_key",
                    selected_keys,
                ).fetchall()
                found = {str(row["partition_key"]) for row in partitions}
                for missing in set(selected_keys) - found:
                    errors.append(f"{missing}:partition:MISSING_METADATA")
            for partition in partitions:
                partition_key = str(partition["partition_key"])
                rows = self._partition_rows(connection, partition_key)
                expected_previous = _ZERO_HASH
                for row in rows:
                    try:
                        path = _safe_child(self.root, str(row["event_file"]))
                        if not path.is_file() or path.is_symlink():
                            raise MarketEventStoreError("immutable event file is missing")
                        raw = path.read_bytes()
                        if _sha256(raw) != row["event_file_sha256"]:
                            raise MarketEventStoreError("immutable event file SHA mismatch")
                        event, previous, record_hash = _strict_record(raw)
                        if event.event_id != row["event_id"]:
                            raise MarketEventStoreError("event identity mismatch")
                        catalog_matches = (
                            event.source == row["session_source"],
                            event.feed_mode == row["session_feed_mode"],
                            event.session_id == row["session_id"],
                            event.symbol == row["symbol"],
                            event.market == row["market"],
                            event.event_type == row["event_type"],
                            event.trading_day == row["trading_day"],
                            _iso(_source_time(event)) == row["source_time"],
                            _iso(event.received_at) == row["received_at"],
                            event.callback_seq == row["callback_seq"],
                            event.provider_seq == row["provider_seq"],
                            partition_key == row["partition_key"],
                            self._partition_key(event) == partition_key,
                            event.raw_payload_sha256 == row["raw_payload_sha256"],
                            canonical_json_bytes(event.payload).decode("utf-8") == row["payload_json"],
                        )
                        if not all(catalog_matches):
                            raise MarketEventStoreError(
                                "catalog metadata disagrees with immutable event file"
                            )
                        if previous != expected_previous or previous != row["previous_record_hash"]:
                            raise MarketEventStoreError("partition record chain predecessor mismatch")
                        if record_hash != row["record_hash"]:
                            raise MarketEventStoreError("partition record hash mismatch")
                        expected_previous = record_hash
                        checked_events += 1
                    except (MarketEventStoreError, OSError) as exc:
                        errors.append(f"{partition_key}:{row['event_id']}:{type(exc).__name__}")
                manifest_path = _safe_child(self.root, f"{partition_key}/manifest.json")
                try:
                    if not manifest_path.is_file() or manifest_path.is_symlink():
                        raise MarketEventStoreError("partition manifest is missing")
                    manifest_raw = manifest_path.read_bytes()
                    if _sha256(manifest_raw) != partition["manifest_sha256"]:
                        raise MarketEventStoreError("partition manifest SHA mismatch")
                    manifest = strict_json_loads(manifest_raw.rstrip(b"\n"))
                    expected_manifest = self._manifest_document(partition_key, rows)
                    if manifest != expected_manifest:
                        raise MarketEventStoreError("partition manifest content mismatch")
                    fields = dict(
                        item.split("=", 1) for item in partition_key.split("/")
                    )
                    if (
                        partition["market"] != fields.get("market")
                        or partition["trading_day"] != fields.get("trading_day")
                        or partition["symbol"] != fields.get("symbol")
                        or int(partition["event_count"]) != len(rows)
                    ):
                        raise MarketEventStoreError(
                            "partition metadata disagrees with its identity"
                        )
                    if rows:
                        if partition["first_record_hash"] != rows[0]["record_hash"]:
                            raise MarketEventStoreError("partition first hash mismatch")
                        if partition["last_record_hash"] != rows[-1]["record_hash"]:
                            raise MarketEventStoreError("partition last hash mismatch")
                    checked_partitions += 1
                except (
                    MarketEventStoreError,
                    OSError,
                    XtpSidecarContractError,
                ):
                    errors.append(f"{partition_key}:manifest:INTEGRITY_ERROR")
        return {
            "schema": "stock-tracker-market-event-integrity-v1",
            "scope": "FULL" if selected_keys is None else "PARTITIONS",
            "requested_partition_count": (
                None if selected_keys is None else len(selected_keys)
            ),
            "passed": not errors,
            "checked_event_count": checked_events,
            "checked_partition_count": checked_partitions,
            "errors": errors[:100],
            "production_database_modified": False,
        }

    def last_cursor_for_session(self, session_id: str) -> int:
        if type(session_id) is not str or not session_id or len(session_id) > 128:
            raise MarketEventStoreError("session_id is invalid")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_callback_seq FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return 0 if row is None else int(row["last_callback_seq"])

    def start_ingestion_run(self) -> str:
        run_id = f"ing-{uuid.uuid4().hex}"
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO ingestion_runs(run_id,started_at) VALUES(?,?)",
                (run_id, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        return run_id

    def finish_ingestion_run(
        self,
        run_id: str,
        *,
        accepted: int,
        duplicates: int,
        quarantined: int,
        last_cursor: int,
    ) -> None:
        values = (accepted, duplicates, quarantined, last_cursor)
        if any(type(value) is not int or value < 0 for value in values):
            raise MarketEventStoreError("ingestion counters must be non-negative integers")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_runs
                SET completed_at=?,accepted=?,duplicates=?,quarantined=?,last_cursor=?
                WHERE run_id=? AND completed_at IS NULL
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    accepted,
                    duplicates,
                    quarantined,
                    last_cursor,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise MarketEventStoreError("ingestion run is missing or already complete")
            connection.commit()

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            session_count = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            partition_count = int(connection.execute("SELECT COUNT(*) FROM partitions").fetchone()[0])
            finding_count = int(connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0])
            minute_count = int(connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
            last = connection.execute("SELECT MAX(received_at) FROM events").fetchone()[0]
            finding_rows = connection.execute(
                "SELECT kind,COUNT(*) AS n FROM findings GROUP BY kind"
            ).fetchall()
        lag_ms = None
        if last:
            lag_ms = max(
                0,
                int(
                    (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                    * 1000
                ),
            )
        return {
            "schema": _EVENT_STORE_SCHEMA,
            "status": "READY",
            "event_count": event_count,
            "session_count": session_count,
            "partition_count": partition_count,
            "finding_count": finding_count,
            "findings_by_kind": {row["kind"]: row["n"] for row in finding_rows},
            "minute_bar_count": minute_count,
            "last_event_at": last,
            "ingestion_lag_ms": lag_ms,
            "database_path_exposed": False,
            "raw_root_exposed": False,
            "production_database_modified": False,
        }
