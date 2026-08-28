"""Separate SQLite persistence for monitor rules, inbox, and notifications."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sidecars.xtp.contracts import canonical_json_bytes, validate_symbol

from .contracts import (
    InboxState,
    MonitorRule,
    MonitorValidationError,
    state_transition_allowed,
)

_MONITOR_SCHEMA = "stock-tracker-monitor-store-v3"
_PREVIOUS_MONITOR_SCHEMA = "stock-tracker-monitor-store-v2"
_RULE_SNAPSHOT_SCHEMA = "stock-tracker-monitor-rule-snapshot-v1"
_TERMINAL_INBOX_STATES = (
    InboxState.INVALIDATED.value,
    InboxState.EXPIRED.value,
    InboxState.RESOLVED.value,
)


class MonitorRepositoryError(RuntimeError):
    """Raised when monitor persistence or lifecycle integrity fails."""


@dataclass(frozen=True, slots=True)
class MonitorTriggerWriteResult:
    """Atomic trigger-write outcome used by the monitor engine."""

    inbox: dict[str, Any] | None
    suppressed: bool
    reason: str | None = None


def _rule_snapshot(
    rule: MonitorRule,
    *,
    historical_exact: bool,
    migration_source: str | None = None,
) -> dict[str, Any]:
    if not isinstance(rule, MonitorRule):
        raise MonitorRepositoryError("rule snapshot requires MonitorRule")
    if type(historical_exact) is not bool:
        raise MonitorRepositoryError("historical_exact must be boolean")
    snapshot: dict[str, Any] = {
        "schema": _RULE_SNAPSHOT_SCHEMA,
        "historical_exact": historical_exact,
        "rule": rule.as_dict(),
    }
    if migration_source is not None:
        if (
            type(migration_source) is not str
            or not migration_source
            or len(migration_source) > 128
        ):
            raise MonitorRepositoryError("migration_source is invalid")
        snapshot["migration_source"] = migration_source
    return snapshot


def _parse_rule_snapshot(value: str) -> dict[str, Any]:
    try:
        snapshot = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MonitorRepositoryError("stored monitor rule snapshot is invalid") from exc
    required = {"schema", "historical_exact", "rule"}
    optional = {"migration_source"}
    if (
        not isinstance(snapshot, dict)
        or required - set(snapshot)
        or set(snapshot) - required - optional
        or snapshot.get("schema") != _RULE_SNAPSHOT_SCHEMA
        or type(snapshot.get("historical_exact")) is not bool
    ):
        raise MonitorRepositoryError("stored monitor rule snapshot is invalid")
    if "migration_source" in snapshot and (
        type(snapshot["migration_source"]) is not str
        or not snapshot["migration_source"]
        or len(snapshot["migration_source"]) > 128
    ):
        raise MonitorRepositoryError("stored monitor rule snapshot migration source is invalid")
    try:
        rule = MonitorRule.from_dict(snapshot["rule"])
    except (TypeError, ValueError, MonitorValidationError) as exc:
        raise MonitorRepositoryError("stored monitor rule snapshot rule is invalid") from exc
    snapshot["rule"] = rule.as_dict()
    return snapshot


class MonitorRepository:
    def __init__(self, database: str | Path) -> None:
        raw_database = Path(database)
        if raw_database.exists() and raw_database.is_symlink():
            raise MonitorRepositoryError("monitor database must not be a symlink")
        if raw_database.parent.exists() and raw_database.parent.is_symlink():
            raise MonitorRepositoryError("monitor database parent must not be a symlink")
        self.database = raw_database.resolve(strict=False)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS monitor_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitor_rules (
            rule_id TEXT PRIMARY KEY,
            rule_json TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitor_inbox (
            inbox_id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL REFERENCES monitor_rules(rule_id),
            rule_version INTEGER NOT NULL,
            rule_snapshot_json TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            severity TEXT NOT NULL,
            state TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            first_triggered_at TEXT NOT NULL,
            last_triggered_at TEXT NOT NULL,
            trigger_count INTEGER NOT NULL,
            snoozed_until TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_monitor_inbox_state
            ON monitor_inbox(state,last_triggered_at DESC);
        CREATE INDEX IF NOT EXISTS idx_monitor_inbox_dedup
            ON monitor_inbox(rule_id,symbol,dedup_key,last_triggered_at DESC);
        CREATE TABLE IF NOT EXISTS monitor_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id TEXT NOT NULL REFERENCES monitor_inbox(inbox_id),
            previous_state TEXT NOT NULL,
            next_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            changed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            outbox_id TEXT PRIMARY KEY,
            inbox_id TEXT NOT NULL REFERENCES monitor_inbox(inbox_id),
            channel TEXT NOT NULL,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_state
            ON notification_outbox(state,next_attempt_at);
        """
        with self._connection() as connection:
            connection.executescript(schema)
            existing = connection.execute(
                "SELECT value FROM monitor_meta WHERE key='schema'"
            ).fetchone()
            existing_schema = None if existing is None else str(existing["value"])
            if existing_schema == _PREVIOUS_MONITOR_SCHEMA:
                self._migrate_v2_to_v3(connection)
                existing_schema = _MONITOR_SCHEMA
            if existing_schema is not None and existing_schema != _MONITOR_SCHEMA:
                raise MonitorRepositoryError(
                    "monitor database schema mismatch; use a new database path"
                )
            connection.execute(
                "INSERT OR IGNORE INTO monitor_meta(key,value) VALUES('schema',?)",
                (_MONITOR_SCHEMA,),
            )
            self._ensure_snapshot_triggers(connection)
            connection.commit()

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(monitor_inbox)").fetchall()
        }
        if "rule_version" not in columns:
            connection.execute("ALTER TABLE monitor_inbox ADD COLUMN rule_version INTEGER")
        if "rule_snapshot_json" not in columns:
            connection.execute("ALTER TABLE monitor_inbox ADD COLUMN rule_snapshot_json TEXT")

        rules: dict[str, MonitorRule] = {}
        for row in connection.execute(
            "SELECT rule_id,rule_json FROM monitor_rules ORDER BY rule_id"
        ).fetchall():
            try:
                rule = MonitorRule.from_dict(json.loads(row["rule_json"]))
            except (TypeError, ValueError, json.JSONDecodeError, MonitorValidationError) as exc:
                raise MonitorRepositoryError("cannot migrate invalid v2 monitor rule") from exc
            rules[str(row["rule_id"])] = rule
            connection.execute(
                "UPDATE monitor_rules SET rule_json=? WHERE rule_id=?",
                (
                    canonical_json_bytes(rule.as_dict()).decode("utf-8"),
                    rule.rule_id,
                ),
            )

        for row in connection.execute(
            """
            SELECT inbox_id,rule_id FROM monitor_inbox
            WHERE rule_version IS NULL OR rule_snapshot_json IS NULL
            ORDER BY inbox_id
            """
        ).fetchall():
            rule = rules.get(str(row["rule_id"]))
            if rule is None:
                raise MonitorRepositoryError(
                    "cannot migrate monitor inbox without its current rule"
                )
            snapshot = _rule_snapshot(
                rule,
                historical_exact=False,
                migration_source="V2_CURRENT_RULE",
            )
            connection.execute(
                """
                UPDATE monitor_inbox
                SET rule_version=?,rule_snapshot_json=?
                WHERE inbox_id=?
                """,
                (
                    rule.version,
                    canonical_json_bytes(snapshot).decode("utf-8"),
                    row["inbox_id"],
                ),
            )
        missing = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM monitor_inbox
                WHERE rule_version IS NULL OR rule_snapshot_json IS NULL
                """
            ).fetchone()[0]
        )
        if missing:
            raise MonitorRepositoryError("monitor rule-snapshot migration failed")
        connection.execute(
            "UPDATE monitor_meta SET value=? WHERE key='schema'",
            (_MONITOR_SCHEMA,),
        )

    @staticmethod
    def _ensure_snapshot_triggers(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS monitor_inbox_snapshot_required
            BEFORE INSERT ON monitor_inbox
            WHEN NEW.rule_version IS NULL OR NEW.rule_snapshot_json IS NULL
            BEGIN
                SELECT RAISE(ABORT,'monitor inbox rule snapshot is required');
            END;
            DROP TRIGGER IF EXISTS monitor_inbox_snapshot_immutable;
            CREATE TRIGGER monitor_inbox_snapshot_immutable
            BEFORE UPDATE OF rule_version,rule_snapshot_json ON monitor_inbox
            WHEN NEW.rule_version IS NOT OLD.rule_version
              OR NEW.rule_snapshot_json IS NOT OLD.rule_snapshot_json
            BEGIN
                SELECT RAISE(ABORT,'monitor inbox rule snapshot is immutable');
            END;
            """
        )

    def upsert_rule(self, rule: MonitorRule) -> dict[str, Any]:
        if not isinstance(rule, MonitorRule):
            raise MonitorRepositoryError("rule must be MonitorRule")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT rule_json FROM monitor_rules WHERE rule_id=?",
                (rule.rule_id,),
            ).fetchone()
            now = datetime.now(timezone.utc)
            if row is None:
                stored = replace(
                    rule,
                    version=1,
                    updated_at=now,
                )
            else:
                try:
                    previous = MonitorRule.from_dict(json.loads(row["rule_json"]))
                except (TypeError, ValueError, json.JSONDecodeError, MonitorValidationError) as exc:
                    raise MonitorRepositoryError("stored monitor rule is invalid") from exc
                stored = replace(
                    rule,
                    version=previous.version + 1,
                    created_at=previous.created_at,
                    updated_at=now,
                )
            body = canonical_json_bytes(stored.as_dict()).decode("utf-8")
            connection.execute(
                """
                INSERT INTO monitor_rules(rule_id,rule_json,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    rule_json=excluded.rule_json,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    stored.rule_id,
                    body,
                    1 if stored.enabled else 0,
                    stored.created_at.isoformat(),
                    stored.updated_at.isoformat(),
                ),
            )
            connection.commit()
        return stored.as_dict()

    def delete_rule(self, rule_id: str) -> bool:
        if type(rule_id) is not str or not rule_id:
            raise MonitorRepositoryError("rule_id is required")
        with self._lock, self._connection() as connection:
            history_count = connection.execute(
                "SELECT COUNT(*) FROM monitor_inbox WHERE rule_id=?",
                (rule_id,),
            ).fetchone()[0]
            if history_count:
                raise MonitorRepositoryError(
                    "rule has immutable inbox history and cannot be deleted; disable it instead"
                )
            cursor = connection.execute("DELETE FROM monitor_rules WHERE rule_id=?", (rule_id,))
            connection.commit()
            return cursor.rowcount > 0

    def get_rule(self, rule_id: str) -> MonitorRule | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT rule_json FROM monitor_rules WHERE rule_id=?",
                (rule_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return MonitorRule.from_dict(json.loads(row["rule_json"]))
        except (json.JSONDecodeError, MonitorValidationError) as exc:
            raise MonitorRepositoryError("stored monitor rule is invalid") from exc

    def list_rules(self, *, enabled_only: bool = False) -> list[MonitorRule]:
        query = "SELECT rule_json FROM monitor_rules"
        parameters: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY updated_at DESC,rule_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        rules: list[MonitorRule] = []
        for row in rows:
            try:
                rules.append(MonitorRule.from_dict(json.loads(row["rule_json"])))
            except (json.JSONDecodeError, MonitorValidationError) as exc:
                raise MonitorRepositoryError("stored monitor rule is invalid") from exc
        return rules

    def latest_trigger(
        self,
        rule_id: str,
        symbol: str,
        dedup_key: str,
    ) -> dict[str, Any] | None:
        symbol = validate_symbol(symbol)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT inbox_id,state,last_triggered_at,updated_at,trigger_count
                FROM monitor_inbox
                WHERE rule_id=? AND symbol=? AND dedup_key=?
                ORDER BY last_triggered_at DESC LIMIT 1
                """,
                (rule_id, symbol, dedup_key),
            ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _insert_inbox_with_connection(
        connection: sqlite3.Connection,
        *,
        rule: MonitorRule,
        symbol: str,
        market: str,
        title: str,
        summary: str,
        evidence_json: str,
        dedup_key: str,
        timestamp: str,
    ) -> str:
        """Insert one inbox item and its outbox rows in the caller transaction."""

        inbox_id = f"mon-{uuid.uuid4().hex}"
        rule_snapshot_json = canonical_json_bytes(
            _rule_snapshot(rule, historical_exact=True)
        ).decode("utf-8")
        connection.execute(
            """
            INSERT INTO monitor_inbox(
                inbox_id,rule_id,rule_version,rule_snapshot_json,symbol,market,
                severity,state,title,summary,evidence_json,dedup_key,
                first_triggered_at,last_triggered_at,trigger_count,snoozed_until,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                inbox_id,
                rule.rule_id,
                rule.version,
                rule_snapshot_json,
                symbol,
                market,
                rule.severity.value,
                InboxState.NEW.value,
                title,
                summary,
                evidence_json,
                dedup_key,
                timestamp,
                timestamp,
                1,
                None,
                timestamp,
            ),
        )
        for channel in rule.notification_channels:
            outbox_id = f"out-{uuid.uuid4().hex}"
            payload = {
                "schema": "stock-tracker-monitor-notification-v1",
                "inbox_id": inbox_id,
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "symbol": symbol,
                "market": market,
                "severity": rule.severity.value,
                "title": title,
                "summary": summary,
                "triggered_at": timestamp,
                "portfolio_details_included": False,
            }
            connection.execute(
                """
                INSERT INTO notification_outbox(
                    outbox_id,inbox_id,channel,state,payload_json,attempts,
                    next_attempt_at,created_at,updated_at
                ) VALUES(?,?,?,'PENDING',?,0,?,?,?)
                """,
                (
                    outbox_id,
                    inbox_id,
                    channel,
                    canonical_json_bytes(payload).decode("utf-8"),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        return inbox_id

    def create_inbox(
        self,
        *,
        rule: MonitorRule,
        symbol: str,
        market: str,
        title: str,
        summary: str,
        evidence: dict[str, Any],
        dedup_key: str,
        triggered_at: datetime,
    ) -> dict[str, Any]:
        symbol = validate_symbol(symbol)
        if market != "A":
            raise MonitorRepositoryError("monitor inbox currently supports A shares only")
        for value, name, maximum in (
            (title, "title", 160),
            (summary, "summary", 1000),
            (dedup_key, "dedup_key", 128),
        ):
            if type(value) is not str or not value.strip() or value != value.strip():
                raise MonitorRepositoryError(f"{name} must be non-empty and trimmed")
            if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise MonitorRepositoryError(f"{name} is invalid")
        if triggered_at.tzinfo is None or triggered_at.utcoffset() is None:
            raise MonitorRepositoryError("triggered_at must be timezone-aware")
        timestamp = triggered_at.astimezone(timezone.utc).isoformat()
        evidence_json = canonical_json_bytes(evidence).decode("utf-8")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inbox_id = self._insert_inbox_with_connection(
                connection,
                rule=rule,
                symbol=symbol,
                market=market,
                title=title,
                summary=summary,
                evidence_json=evidence_json,
                dedup_key=dedup_key,
                timestamp=timestamp,
            )
            connection.commit()
        result = self.get_inbox(inbox_id)
        if result is None:
            raise MonitorRepositoryError("monitor inbox event disappeared")
        return result

    def record_trigger(
        self,
        *,
        rule: MonitorRule,
        symbol: str,
        market: str,
        title: str,
        summary: str,
        evidence: dict[str, Any],
        dedup_key: str,
        triggered_at: datetime,
        suppress_window_sec: int = 0,
    ) -> MonitorTriggerWriteResult:
        """Create a new inbox event or append one trigger to an active event."""

        symbol = validate_symbol(symbol)
        if market != "A":
            raise MonitorRepositoryError("monitor inbox currently supports A shares only")
        for value, name, maximum in (
            (title, "title", 160),
            (summary, "summary", 1000),
            (dedup_key, "dedup_key", 128),
        ):
            if type(value) is not str or not value.strip() or value != value.strip():
                raise MonitorRepositoryError(f"{name} must be non-empty and trimmed")
            if len(value) > maximum or any(
                ord(char) < 32 or ord(char) == 127 for char in value
            ):
                raise MonitorRepositoryError(f"{name} is invalid")
        if triggered_at.tzinfo is None or triggered_at.utcoffset() is None:
            raise MonitorRepositoryError("triggered_at must be timezone-aware")
        if (
            type(suppress_window_sec) is not int
            or not 0 <= suppress_window_sec <= 604800
        ):
            raise MonitorRepositoryError(
                "suppress_window_sec must be an integer in 0..604800"
            )
        timestamp = triggered_at.astimezone(timezone.utc).isoformat()
        evidence_json = canonical_json_bytes(evidence).decode("utf-8")
        inbox_id: str | None = None
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT inbox_id,state,snoozed_until,rule_version,last_triggered_at
                FROM monitor_inbox
                WHERE rule_id=? AND symbol=? AND dedup_key=?
                ORDER BY last_triggered_at DESC LIMIT 1
                """,
                (rule.rule_id, symbol, dedup_key),
            ).fetchone()
            if row is None or row["state"] in _TERMINAL_INBOX_STATES:
                inbox_id = self._insert_inbox_with_connection(
                    connection,
                    rule=rule,
                    symbol=symbol,
                    market=market,
                    title=title,
                    summary=summary,
                    evidence_json=evidence_json,
                    dedup_key=dedup_key,
                    timestamp=timestamp,
                )
                connection.commit()
            else:
                if int(row["rule_version"]) != rule.version:
                    raise MonitorRepositoryError(
                        "monitor dedup identity collided across rule versions"
                    )
                try:
                    previous_triggered_at = datetime.fromisoformat(
                        str(row["last_triggered_at"])
                    )
                except ValueError as exc:
                    raise MonitorRepositoryError(
                        "stored monitor trigger time is invalid"
                    ) from exc
                if (
                    previous_triggered_at.tzinfo is None
                    or previous_triggered_at.utcoffset() is None
                ):
                    raise MonitorRepositoryError(
                        "stored monitor trigger time must be timezone-aware"
                    )
                elapsed = (
                    triggered_at.astimezone(timezone.utc)
                    - previous_triggered_at.astimezone(timezone.utc)
                ).total_seconds()
                if elapsed < 0:
                    connection.rollback()
                    return MonitorTriggerWriteResult(
                        inbox=None,
                        suppressed=True,
                        reason="OUT_OF_ORDER_TRIGGER_SUPPRESSED",
                    )
                if elapsed < suppress_window_sec:
                    connection.rollback()
                    return MonitorTriggerWriteResult(
                        inbox=None,
                        suppressed=True,
                        reason="COOLDOWN_OR_DUPLICATE_SUPPRESSED",
                    )
                next_state = row["state"]
                snoozed_until = row["snoozed_until"]
                if (
                    next_state == InboxState.SNOOZED.value
                    and snoozed_until is not None
                    and snoozed_until <= timestamp
                ):
                    next_state = InboxState.NEW.value
                    snoozed_until = None
                connection.execute(
                    """
                    UPDATE monitor_inbox
                    SET severity=?,state=?,title=?,summary=?,evidence_json=?,
                        last_triggered_at=?,trigger_count=trigger_count+1,
                        snoozed_until=?,updated_at=?
                    WHERE inbox_id=?
                    """,
                    (
                        rule.severity.value,
                        next_state,
                        title,
                        summary,
                        evidence_json,
                        timestamp,
                        snoozed_until,
                        timestamp,
                        row["inbox_id"],
                    ),
                )
                for channel in rule.notification_channels:
                    outbox_id = f"out-{uuid.uuid4().hex}"
                    payload = {
                        "schema": "stock-tracker-monitor-notification-v1",
                        "inbox_id": row["inbox_id"],
                        "rule_id": rule.rule_id,
                        "rule_version": rule.version,
                        "symbol": symbol,
                        "market": market,
                        "severity": rule.severity.value,
                        "title": title,
                        "summary": summary,
                        "triggered_at": timestamp,
                        "portfolio_details_included": False,
                    }
                    connection.execute(
                        """
                        INSERT INTO notification_outbox(
                            outbox_id,inbox_id,channel,state,payload_json,attempts,
                            next_attempt_at,created_at,updated_at
                        ) VALUES(?,?,?,'PENDING',?,0,?,?,?)
                        """,
                        (
                            outbox_id,
                            row["inbox_id"],
                            channel,
                            canonical_json_bytes(payload).decode("utf-8"),
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                connection.commit()
                inbox_id = str(row["inbox_id"])
        if inbox_id is None:
            raise MonitorRepositoryError(
                "monitor trigger did not produce an inbox identity"
            )
        result = self.get_inbox(inbox_id)
        if result is None:
            raise MonitorRepositoryError("monitor inbox event disappeared")
        return MonitorTriggerWriteResult(
            inbox=result,
            suppressed=False,
        )

    def get_inbox(self, inbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_inbox WHERE inbox_id=?",
                (inbox_id,),
            ).fetchone()
        return None if row is None else self._inbox_row(row)

    @staticmethod
    def _inbox_row(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        try:
            evidence = json.loads(output.pop("evidence_json"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise MonitorRepositoryError("stored monitor evidence is invalid") from exc
        if not isinstance(evidence, dict):
            raise MonitorRepositoryError("stored monitor evidence must be an object")
        snapshot = _parse_rule_snapshot(output.pop("rule_snapshot_json"))
        rule_version = int(output["rule_version"])
        if rule_version != int(snapshot["rule"]["version"]):
            raise MonitorRepositoryError(
                "stored monitor rule version does not match its snapshot"
            )
        evidence_version = evidence.get("rule_version")
        if evidence_version is not None and (
            type(evidence_version) is not int or evidence_version != rule_version
        ):
            raise MonitorRepositoryError(
                "stored monitor evidence version does not match its snapshot"
            )
        evidence_hash = evidence.get("rule_snapshot_sha256")
        if evidence_hash is not None:
            expected_hash = hashlib.sha256(
                canonical_json_bytes(snapshot["rule"])
            ).hexdigest()
            if type(evidence_hash) is not str or evidence_hash != expected_hash:
                raise MonitorRepositoryError(
                    "stored monitor evidence hash does not match its snapshot"
                )
        output["evidence"] = evidence
        output["rule_snapshot"] = snapshot
        return output

    def list_inbox(
        self,
        *,
        states: tuple[InboxState, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MonitorRepositoryError("inbox limit must be 1-1000")
        query = "SELECT * FROM monitor_inbox"
        parameters: list[Any] = []
        if states:
            if any(not isinstance(state, InboxState) for state in states):
                raise MonitorRepositoryError("inbox state filter is invalid")
            placeholders = ",".join("?" for _ in states)
            query += f" WHERE state IN ({placeholders})"
            parameters.extend(state.value for state in states)
        query += " ORDER BY last_triggered_at DESC,inbox_id LIMIT ?"
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._inbox_row(row) for row in rows]

    def transition(
        self,
        inbox_id: str,
        target: InboxState,
        *,
        reason: str,
        snoozed_until: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(target, InboxState):
            raise MonitorRepositoryError("target state is invalid")
        if type(reason) is not str or not reason.strip() or reason != reason.strip() or len(reason) > 500:
            raise MonitorRepositoryError("transition reason is invalid")
        if snoozed_until is not None and (
            snoozed_until.tzinfo is None or snoozed_until.utcoffset() is None
        ):
            raise MonitorRepositoryError("snoozed_until must be timezone-aware")
        if target is InboxState.SNOOZED and snoozed_until is None:
            raise MonitorRepositoryError("SNOOZED requires snoozed_until")
        if target is not InboxState.SNOOZED and snoozed_until is not None:
            raise MonitorRepositoryError("snoozed_until is only valid for SNOOZED")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM monitor_inbox WHERE inbox_id=?",
                (inbox_id,),
            ).fetchone()
            if row is None:
                raise MonitorRepositoryError("monitor inbox event not found")
            current = InboxState(row["state"])
            if not state_transition_allowed(current, target):
                raise MonitorRepositoryError(
                    f"invalid monitor transition {current.value}->{target.value}"
                )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE monitor_inbox
                SET state=?,snoozed_until=?,updated_at=?
                WHERE inbox_id=?
                """,
                (
                    target.value,
                    None if snoozed_until is None else snoozed_until.astimezone(timezone.utc).isoformat(),
                    now,
                    inbox_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO monitor_transitions(
                    inbox_id,previous_state,next_state,reason,changed_at
                ) VALUES(?,?,?,?,?)
                """,
                (inbox_id, current.value, target.value, reason, now),
            )
            connection.commit()
        result = self.get_inbox(inbox_id)
        if result is None:
            raise MonitorRepositoryError("monitor inbox event disappeared")
        return result

    def expire_due(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise MonitorRepositoryError("expiry clock must be timezone-aware")
        changed = 0
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT inbox_id,state FROM monitor_inbox
                WHERE state=? AND snoozed_until IS NOT NULL AND snoozed_until<=?
                """,
                (InboxState.SNOOZED.value, current.astimezone(timezone.utc).isoformat()),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE monitor_inbox SET state=?,snoozed_until=NULL,updated_at=? WHERE inbox_id=?",
                    (InboxState.NEW.value, current.isoformat(), row["inbox_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO monitor_transitions(
                        inbox_id,previous_state,next_state,reason,changed_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        row["inbox_id"],
                        row["state"],
                        InboxState.NEW.value,
                        "snooze elapsed",
                        current.isoformat(),
                    ),
                )
                changed += 1
            connection.commit()
        return changed

    def expire_rule_events(
        self,
        rule_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise MonitorRepositoryError("expiry clock must be timezone-aware")
        timestamp = current.astimezone(timezone.utc).isoformat()
        changed = 0
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT inbox_id,state FROM monitor_inbox
                WHERE rule_id=? AND state NOT IN (?,?,?)
                """,
                (rule_id, *_TERMINAL_INBOX_STATES),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE monitor_inbox
                    SET state=?,snoozed_until=NULL,updated_at=? WHERE inbox_id=?
                    """,
                    (InboxState.EXPIRED.value, timestamp, row["inbox_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO monitor_transitions(
                        inbox_id,previous_state,next_state,reason,changed_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        row["inbox_id"],
                        row["state"],
                        InboxState.EXPIRED.value,
                        "monitor rule expired",
                        timestamp,
                    ),
                )
                changed += 1
            connection.commit()
        return changed

    def outbox(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MonitorRepositoryError("outbox limit must be 1-1000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def claim_pending_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 200,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Atomically lease due outbox rows so concurrent dispatchers cannot duplicate-send."""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise MonitorRepositoryError("outbox clock must be timezone-aware")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MonitorRepositoryError("outbox limit must be 1-1000")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise MonitorRepositoryError("outbox lease_seconds must be 5-300")
        current_utc = current.astimezone(timezone.utc)
        timestamp = current_utc.isoformat()
        lease_until = (current_utc + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_outbox
                SET state='PENDING',next_attempt_at=NULL,updated_at=?
                WHERE state='SENDING' AND next_attempt_at IS NOT NULL
                  AND next_attempt_at<=?
                """,
                (timestamp, timestamp),
            )
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE state='PENDING'
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                ORDER BY created_at,outbox_id LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE notification_outbox
                    SET state='SENDING',next_attempt_at=?,updated_at=?
                    WHERE outbox_id=? AND state='PENDING'
                    """,
                    (lease_until, timestamp, row["outbox_id"]),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise MonitorRepositoryError("outbox lease allocation raced")
            connection.commit()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise MonitorRepositoryError("stored notification payload is invalid") from exc
            if not isinstance(payload, dict):
                raise MonitorRepositoryError("stored notification payload must be an object")
            item["payload"] = payload
            item["state"] = "SENDING"
            item["next_attempt_at"] = lease_until
            output.append(item)
        return output

    def pending_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise MonitorRepositoryError("outbox clock must be timezone-aware")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MonitorRepositoryError("outbox limit must be 1-1000")
        timestamp = current.astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE state='PENDING'
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                ORDER BY created_at,outbox_id LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def mark_outbox(
        self,
        outbox_id: str,
        *,
        state: str,
        attempts: int,
        next_attempt_at: datetime | None = None,
        expected_state: str | None = None,
    ) -> None:
        allowed_states = {"PENDING", "SENDING", "DELIVERED", "FAILED", "DISABLED"}
        if state not in allowed_states:
            raise MonitorRepositoryError("outbox state is invalid")
        if expected_state is not None and expected_state not in allowed_states:
            raise MonitorRepositoryError("expected outbox state is invalid")
        if type(attempts) is not int or not 0 <= attempts <= 5:
            raise MonitorRepositoryError("outbox attempts is invalid")
        if next_attempt_at is not None and (
            next_attempt_at.tzinfo is None or next_attempt_at.utcoffset() is None
        ):
            raise MonitorRepositoryError("next_attempt_at must be timezone-aware")
        query = """
            UPDATE notification_outbox
            SET state=?,attempts=?,next_attempt_at=?,updated_at=?
            WHERE outbox_id=?
        """
        parameters: list[Any] = [
            state,
            attempts,
            None
            if next_attempt_at is None
            else next_attempt_at.astimezone(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            outbox_id,
        ]
        if expected_state is not None:
            query += " AND state=?"
            parameters.append(expected_state)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(query, parameters)
            if cursor.rowcount != 1:
                connection.rollback()
                raise MonitorRepositoryError("outbox item is missing or changed state")
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with self._connection() as connection:
            rules = int(connection.execute("SELECT COUNT(*) FROM monitor_rules").fetchone()[0])
            enabled = int(
                connection.execute("SELECT COUNT(*) FROM monitor_rules WHERE enabled=1").fetchone()[0]
            )
            inbox_rows = connection.execute(
                "SELECT state,COUNT(*) AS n FROM monitor_inbox GROUP BY state"
            ).fetchall()
            outbox_rows = connection.execute(
                "SELECT state,COUNT(*) AS n FROM notification_outbox GROUP BY state"
            ).fetchall()
        return {
            "schema": _MONITOR_SCHEMA,
            "status": "READY",
            "rule_count": rules,
            "enabled_rule_count": enabled,
            "inbox_by_state": {row["state"]: row["n"] for row in inbox_rows},
            "outbox_by_state": {row["state"]: row["n"] for row in outbox_rows},
            "database_path_exposed": False,
            "auto_trade": False,
            "score_mutation": False,
            "production_database_modified": False,
        }
