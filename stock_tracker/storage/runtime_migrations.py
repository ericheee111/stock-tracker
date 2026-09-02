from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_MIGRATION_SCHEMA = "stage4g1-runtime-migration-report-v4"
RUNTIME_EVIDENCE_STORE_SCHEMA = "stage4g1-runtime-evidence-store-v2"
RUNTIME_OUTBOX_SCHEMA_VERSION = 3
_MIGRATION_ROOT = Path(__file__).with_name("migrations_runtime")


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    path: Path


_MIGRATIONS = (
    _Migration(1, "runtime_evidence_outbox", _MIGRATION_ROOT / "0001_runtime_evidence_outbox.sql"),
    _Migration(2, "runtime_occurrence_identity", _MIGRATION_ROOT / "0002_runtime_occurrence_identity.sql"),
    _Migration(
        3,
        "runtime_delivery_binding_and_integrity",
        _MIGRATION_ROOT / "0003_runtime_delivery_binding_and_integrity.sql",
    ),
)
_TABLE_COLUMNS_V1 = {
    "runtime_schema_migration": (
        ("version", "INTEGER", 0, 1, 0),
        ("name", "TEXT", 1, 0, 0),
        ("checksum", "TEXT", 1, 0, 0),
        ("applied_at", "TEXT", 1, 0, 0),
    ),
    "runtime_transition_outbox": (
        ("append_order", "INTEGER", 0, 1, 0),
        ("artifact_id", "TEXT", 1, 0, 0),
        ("runtime_signal_id", "TEXT", 1, 0, 0),
        ("transition_event_id", "TEXT", 1, 0, 0),
        ("payload_json", "TEXT", 1, 0, 0),
        ("payload_sha256", "TEXT", 1, 0, 0),
        ("created_at", "TEXT", 1, 0, 0),
    ),
    "runtime_outbox_delivery": (
        ("artifact_id", "TEXT", 0, 1, 0),
        ("status", "TEXT", 1, 0, 0),
        ("lease_owner", "TEXT", 0, 0, 0),
        ("lease_expires_at", "TEXT", 0, 0, 0),
        ("retry_count", "INTEGER", 1, 0, 0),
        ("next_retry_at", "TEXT", 0, 0, 0),
        ("last_error_code", "TEXT", 0, 0, 0),
        ("delivered_record_hash", "TEXT", 0, 0, 0),
        ("delivered_audit_id", "TEXT", 0, 0, 0),
        ("delivered_at", "TEXT", 0, 0, 0),
    ),
    "runtime_outbox_cursor": (
        ("worker_id", "TEXT", 0, 1, 0),
        ("last_contiguous_order", "INTEGER", 1, 0, 0),
        ("updated_at", "TEXT", 1, 0, 0),
    ),
    "runtime_outbox_quarantine": (
        ("quarantine_order", "INTEGER", 0, 1, 0),
        ("quarantine_id", "TEXT", 1, 0, 0),
        ("artifact_id", "TEXT", 0, 0, 0),
        ("outbox_append_order", "INTEGER", 0, 0, 0),
        ("runtime_signal_id", "TEXT", 1, 0, 0),
        ("reason_code", "TEXT", 1, 0, 0),
        ("quarantined_at", "TEXT", 1, 0, 0),
        ("metadata_json", "TEXT", 1, 0, 0),
        ("metadata_sha256", "TEXT", 1, 0, 0),
    ),
}
_TABLE_COLUMNS_V2 = {
    **_TABLE_COLUMNS_V1,
    "runtime_evidence_meta": (
        ("key", "TEXT", 0, 1, 0),
        ("value", "TEXT", 1, 0, 0),
    ),
    "runtime_transition_occurrence": (
        ("occurrence_order", "INTEGER", 0, 1, 0),
        ("occurrence_dedup_id", "TEXT", 1, 0, 0),
        ("decision_content_id", "TEXT", 1, 0, 0),
        ("artifact_id", "TEXT", 1, 0, 0),
        ("transition_event_id", "TEXT", 1, 0, 0),
        ("upstream_occurrence_id", "TEXT", 0, 0, 0),
        ("signal_history_id", "INTEGER", 1, 0, 0),
        ("created_at", "TEXT", 1, 0, 0),
    ),
    "runtime_worker_integrity_block": (
        ("block_order", "INTEGER", 0, 1, 0),
        ("block_id", "TEXT", 1, 0, 0),
        ("worker_id", "TEXT", 1, 0, 0),
        ("artifact_id", "TEXT", 1, 0, 0),
        ("block_code", "TEXT", 1, 0, 0),
        ("blocked_at", "TEXT", 1, 0, 0),
    ),
}
_TABLE_COLUMNS_V3 = {
    **_TABLE_COLUMNS_V2,
    "runtime_transition_occurrence": (
        *_TABLE_COLUMNS_V2["runtime_transition_occurrence"],
        ("signal_history_sha256", "TEXT", 1, 0, 0),
        ("occurrence_append_order", "INTEGER", 1, 0, 0),
        ("previous_occurrence_hash", "TEXT", 1, 0, 0),
        ("occurrence_hash", "TEXT", 1, 0, 0),
    ),
    "runtime_outbox_delivery": (
        *_TABLE_COLUMNS_V2["runtime_outbox_delivery"],
        ("delivered_artifact_store_id", "TEXT", 0, 0, 0),
        ("delivered_append_order", "INTEGER", 0, 0, 0),
    ),
    "runtime_artifact_delivery_binding": (
        ("binding_order", "INTEGER", 0, 1, 0),
        ("delivery_binding_id", "TEXT", 1, 0, 0),
        ("runtime_store_id", "TEXT", 1, 0, 0),
        ("artifact_store_id", "TEXT", 1, 0, 0),
        ("bound_at", "TEXT", 1, 0, 0),
    ),
    "runtime_artifact_integrity_event": (
        ("event_order", "INTEGER", 0, 1, 0),
        ("integrity_event_id", "TEXT", 1, 0, 0),
        ("delivery_binding_id", "TEXT", 1, 0, 0),
        ("artifact_id", "TEXT", 0, 0, 0),
        ("worker_id", "TEXT", 1, 0, 0),
        ("block_code", "TEXT", 1, 0, 0),
        ("blocked_at", "TEXT", 1, 0, 0),
    ),
}
_INDEXES_V1 = {
    "idx_runtime_transition_signal": (
        "runtime_transition_outbox",
        ("runtime_signal_id", "append_order"),
        0,
    ),
    "idx_runtime_outbox_delivery_ready": (
        "runtime_outbox_delivery",
        ("status", "next_retry_at", "lease_expires_at"),
        0,
    ),
    "idx_runtime_outbox_quarantine_artifact": (
        "runtime_outbox_quarantine",
        ("artifact_id", "outbox_append_order"),
        0,
    ),
}
_INDEXES_V2 = {
    **_INDEXES_V1,
    "idx_runtime_occurrence_decision": (
        "runtime_transition_occurrence",
        ("decision_content_id", "occurrence_order"),
        0,
    ),
}
_INDEXES_V3 = {
    **_INDEXES_V2,
    "idx_runtime_occurrence_append_order": (
        "runtime_transition_occurrence",
        ("occurrence_append_order",),
        1,
    ),
}
_UNIQUE_CONSTRAINTS_V1 = {
    "runtime_schema_migration": {("name",)},
    "runtime_transition_outbox": {("artifact_id",), ("transition_event_id",)},
    "runtime_outbox_quarantine": {("quarantine_id",)},
}
_UNIQUE_CONSTRAINTS_V2 = {
    **_UNIQUE_CONSTRAINTS_V1,
    "runtime_transition_occurrence": {
        ("occurrence_dedup_id",),
        ("artifact_id",),
        ("transition_event_id",),
        ("signal_history_id",),
    },
    "runtime_worker_integrity_block": {("block_id",), ("worker_id",)},
}
_UNIQUE_CONSTRAINTS_V3 = {
    **_UNIQUE_CONSTRAINTS_V2,
    "runtime_artifact_delivery_binding": {
        ("delivery_binding_id",),
        ("runtime_store_id",),
        ("artifact_store_id",),
    },
    "runtime_artifact_integrity_event": {("integrity_event_id",)},
}


def _append_only_trigger(name: str, table: str, message: str) -> tuple[str, str]:
    return (
        table,
        (
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{message}'); END"
        ),
    )


_TRIGGERS_V1 = {
    "runtime_schema_migration_no_update": _append_only_trigger(
        "runtime_schema_migration_no_update", "runtime_schema_migration", "runtime_schema_migration is append-only"
    ),
    "runtime_schema_migration_no_delete": (
        "runtime_schema_migration",
        (
            "CREATE TRIGGER runtime_schema_migration_no_delete BEFORE DELETE ON runtime_schema_migration "
            "BEGIN SELECT RAISE(ABORT, 'runtime_schema_migration is append-only'); END"
        ),
    ),
    "runtime_transition_outbox_no_update": _append_only_trigger(
        "runtime_transition_outbox_no_update", "runtime_transition_outbox", "runtime_transition_outbox payload is immutable"
    ),
    "runtime_transition_outbox_no_delete": (
        "runtime_transition_outbox",
        (
            "CREATE TRIGGER runtime_transition_outbox_no_delete BEFORE DELETE ON runtime_transition_outbox "
            "BEGIN SELECT RAISE(ABORT, 'runtime_transition_outbox payload is immutable'); END"
        ),
    ),
    "runtime_outbox_quarantine_no_update": _append_only_trigger(
        "runtime_outbox_quarantine_no_update", "runtime_outbox_quarantine", "runtime_outbox_quarantine is append-only"
    ),
    "runtime_outbox_quarantine_no_delete": (
        "runtime_outbox_quarantine",
        (
            "CREATE TRIGGER runtime_outbox_quarantine_no_delete BEFORE DELETE ON runtime_outbox_quarantine "
            "BEGIN SELECT RAISE(ABORT, 'runtime_outbox_quarantine is append-only'); END"
        ),
    ),
}
_TRIGGERS_V2 = {
    **_TRIGGERS_V1,
    "runtime_evidence_meta_no_update": _append_only_trigger(
        "runtime_evidence_meta_no_update", "runtime_evidence_meta", "runtime_evidence_meta is append-only"
    ),
    "runtime_evidence_meta_no_delete": (
        "runtime_evidence_meta",
        (
            "CREATE TRIGGER runtime_evidence_meta_no_delete BEFORE DELETE ON runtime_evidence_meta "
            "BEGIN SELECT RAISE(ABORT, 'runtime_evidence_meta is append-only'); END"
        ),
    ),
    "runtime_transition_occurrence_no_update": _append_only_trigger(
        "runtime_transition_occurrence_no_update", "runtime_transition_occurrence", "runtime_transition_occurrence is append-only"
    ),
    "runtime_transition_occurrence_no_delete": (
        "runtime_transition_occurrence",
        (
            "CREATE TRIGGER runtime_transition_occurrence_no_delete BEFORE DELETE ON runtime_transition_occurrence "
            "BEGIN SELECT RAISE(ABORT, 'runtime_transition_occurrence is append-only'); END"
        ),
    ),
    "runtime_worker_integrity_block_no_update": _append_only_trigger(
        "runtime_worker_integrity_block_no_update", "runtime_worker_integrity_block", "runtime_worker_integrity_block is append-only"
    ),
    "runtime_worker_integrity_block_no_delete": (
        "runtime_worker_integrity_block",
        (
            "CREATE TRIGGER runtime_worker_integrity_block_no_delete BEFORE DELETE ON runtime_worker_integrity_block "
            "BEGIN SELECT RAISE(ABORT, 'runtime_worker_integrity_block is append-only'); END"
        ),
    ),
}
_TRIGGERS_V3 = {
    **_TRIGGERS_V2,
    "runtime_artifact_delivery_binding_no_update": _append_only_trigger(
        "runtime_artifact_delivery_binding_no_update",
        "runtime_artifact_delivery_binding",
        "runtime_artifact_delivery_binding is append-only",
    ),
    "runtime_artifact_delivery_binding_no_delete": (
        "runtime_artifact_delivery_binding",
        (
            "CREATE TRIGGER runtime_artifact_delivery_binding_no_delete "
            "BEFORE DELETE ON runtime_artifact_delivery_binding BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifact_delivery_binding is append-only'); END"
        ),
    ),
    "runtime_artifact_integrity_event_no_update": _append_only_trigger(
        "runtime_artifact_integrity_event_no_update",
        "runtime_artifact_integrity_event",
        "runtime_artifact_integrity_event is append-only",
    ),
    "runtime_artifact_integrity_event_no_delete": (
        "runtime_artifact_integrity_event",
        (
            "CREATE TRIGGER runtime_artifact_integrity_event_no_delete "
            "BEFORE DELETE ON runtime_artifact_integrity_event BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifact_integrity_event is append-only'); END"
        ),
    ),
    "runtime_bound_signal_history_no_update": (
        "signal_history",
        (
            "CREATE TRIGGER runtime_bound_signal_history_no_update BEFORE UPDATE ON "
            "signal_history WHEN EXISTS ( SELECT 1 FROM runtime_transition_occurrence "
            "WHERE signal_history_id = OLD.id ) BEGIN SELECT RAISE(ABORT, "
            "'runtime-bound signal_history is immutable'); END"
        ),
    ),
    "runtime_bound_signal_history_no_delete": (
        "signal_history",
        (
            "CREATE TRIGGER runtime_bound_signal_history_no_delete BEFORE DELETE ON "
            "signal_history WHEN EXISTS ( SELECT 1 FROM runtime_transition_occurrence "
            "WHERE signal_history_id = OLD.id ) BEGIN SELECT RAISE(ABORT, "
            "'runtime-bound signal_history is immutable'); END"
        ),
    ),
}

_FOREIGN_KEYS_V1 = {
    "runtime_outbox_delivery": {
        (
            "artifact_id",
            "runtime_transition_outbox",
            "artifact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
    },
}
_FOREIGN_KEYS_V2 = {
    **_FOREIGN_KEYS_V1,
    "runtime_transition_occurrence": {
        (
            "artifact_id",
            "runtime_transition_outbox",
            "artifact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
        (
            "signal_history_id",
            "signal_history",
            "id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
    "runtime_worker_integrity_block": {
        (
            "artifact_id",
            "runtime_transition_outbox",
            "artifact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
    },
}
_FOREIGN_KEYS_V3 = {
    **_FOREIGN_KEYS_V2,
    "runtime_artifact_integrity_event": {
        (
            "delivery_binding_id",
            "runtime_artifact_delivery_binding",
            "delivery_binding_id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
        (
            "artifact_id",
            "runtime_transition_outbox",
            "artifact_id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    },
}


@dataclass(frozen=True, slots=True)
class RuntimeMigrationReport:
    mode: str
    database: str
    target_schema_state: str
    target_state_audit_passed: bool
    target_latest_schema_ready: bool
    rehearsal_performed: bool
    rehearsal_latest_schema_passed: bool
    migration_history_valid: bool
    current_version: int
    latest_version: int
    pending_versions: tuple[int, ...]
    source_database_sha256: str
    source_wal_sha256: str | None
    source_logical_sha256: str
    migration_block_code: str | None
    would_modify: bool
    database_modified: bool
    runtime_store_id: str | None
    backup_path: str | None
    backup_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RUNTIME_MIGRATION_SCHEMA,
            "mode": self.mode,
            "database": self.database,
            "target_schema_state": self.target_schema_state,
            "target_state_audit_passed": self.target_state_audit_passed,
            "target_latest_schema_ready": self.target_latest_schema_ready,
            "rehearsal_performed": self.rehearsal_performed,
            "rehearsal_latest_schema_passed": self.rehearsal_latest_schema_passed,
            "migration_history_valid": self.migration_history_valid,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "pending_versions": list(self.pending_versions),
            "source_database_sha256": self.source_database_sha256,
            "source_wal_sha256": self.source_wal_sha256,
            "source_logical_sha256": self.source_logical_sha256,
            "migration_block_code": self.migration_block_code,
            "would_modify": self.would_modify,
            "database_modified": self.database_modified,
            "runtime_store_id": self.runtime_store_id,
            "backup_path": self.backup_path,
            "backup_sha256": self.backup_sha256,
            "auto_trade": False,
            "production_database_modified": self.database_modified,
        }


class RuntimeMigrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RUNTIME_MIGRATION_INVALID",
        report: RuntimeMigrationReport | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.report = report


class RuntimeMigrationRequired(RuntimeMigrationError):
    pass


@dataclass(frozen=True, slots=True)
class _TargetInspection:
    state: str
    audit_passed: bool
    history_valid: bool
    current_version: int
    runtime_store_id: str | None


def _migration_sql(migration: _Migration) -> str:
    try:
        return migration.path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeMigrationError("runtime migration SQL is unavailable") from exc


def _migration_checksum(migration: _Migration) -> str:
    normalized = _migration_sql(migration).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeMigrationError("migration time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_from_text(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z") or len(value) > 64:
        raise RuntimeMigrationError("migration history time must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeMigrationError("migration history time must be ISO-8601") from exc
    if _utc_text(parsed) != value:
        raise RuntimeMigrationError("migration history time must be canonical UTC")
    return parsed


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str, int, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]), int(row[6]))
        for row in connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
    )


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        raise RuntimeMigrationError("runtime SQL definition is missing")
    return " ".join(value.lower().split()).rstrip(";")


def _expected_history(version: int) -> list[tuple[int, str, str]]:
    return [(item.version, item.name, _migration_checksum(item)) for item in _MIGRATIONS[:version]]


def _validate_version_schema(connection: sqlite3.Connection, version: int) -> str | None:
    columns = {
        1: _TABLE_COLUMNS_V1,
        2: _TABLE_COLUMNS_V2,
        3: _TABLE_COLUMNS_V3,
    }[version]
    indexes = {1: _INDEXES_V1, 2: _INDEXES_V2, 3: _INDEXES_V3}[version]
    unique_constraints = {
        1: _UNIQUE_CONSTRAINTS_V1,
        2: _UNIQUE_CONSTRAINTS_V2,
        3: _UNIQUE_CONSTRAINTS_V3,
    }[version]
    triggers = {1: _TRIGGERS_V1, 2: _TRIGGERS_V2, 3: _TRIGGERS_V3}[version]
    foreign_keys = {
        1: _FOREIGN_KEYS_V1,
        2: _FOREIGN_KEYS_V2,
        3: _FOREIGN_KEYS_V3,
    }[version]
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'runtime_%'"
        ).fetchall()
    }
    if tables != set(columns):
        code = "RUNTIME_SCHEMA_PARTIAL" if set(columns) - tables else "RUNTIME_SCHEMA_INVALID"
        raise RuntimeMigrationError("runtime evidence table set is invalid", code=code)
    for table, expected in columns.items():
        if _table_columns(connection, table) != expected:
            raise RuntimeMigrationError(f"runtime table schema is invalid: {table}")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name LIKE 'runtime_%' LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeMigrationError("runtime evidence views are forbidden")
    actual_triggers = {
        str(row[0]): (str(row[1]), _normalized_sql(row[2]))
        for row in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'runtime_%'"
        ).fetchall()
    }
    expected_triggers = {name: (table, _normalized_sql(sql)) for name, (table, sql) in triggers.items()}
    if actual_triggers != expected_triggers:
        raise RuntimeMigrationError("runtime evidence trigger set is invalid")
    actual_indexes: dict[str, tuple[str, tuple[str, ...], int]] = {}
    for table in tables:
        for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if str(row[3]) != "c":
                continue
            name = str(row[1])
            actual_indexes[name] = (
                table,
                tuple(str(item[1]) for item in connection.execute(
                    "SELECT seqno,name FROM pragma_index_info(?) ORDER BY seqno", (name,)
                ).fetchall()),
                int(row[2]),
            )
    if actual_indexes != indexes:
        raise RuntimeMigrationError("runtime evidence index set is invalid")
    for table in tables:
        actual_uniques = {
            tuple(
                str(item[1])
                for item in connection.execute(
                    "SELECT seqno,name FROM pragma_index_info(?) ORDER BY seqno",
                    (str(row[1]),),
                ).fetchall()
            )
            for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
            if str(row[3]) == "u"
        }
        if actual_uniques != unique_constraints.get(table, set()):
            raise RuntimeMigrationError(
                f"runtime evidence unique constraints are invalid: {table}"
            )
        actual_foreign_keys = {
            (
                str(row[3]),
                str(row[2]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
            )
            for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        if actual_foreign_keys != foreign_keys.get(table, set()):
            raise RuntimeMigrationError(
                f"runtime evidence foreign key set is invalid: {table}"
            )
    if version == 1:
        return None
    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key,value FROM runtime_evidence_meta ORDER BY key").fetchall()
    }
    if set(metadata) != {"schema", "runtime_store_id"} or metadata.get("schema") != RUNTIME_EVIDENCE_STORE_SCHEMA:
        raise RuntimeMigrationError("runtime evidence metadata is invalid")
    store_id = metadata["runtime_store_id"]
    if len(store_id) != 64 or any(c not in "0123456789abcdef" for c in store_id):
        raise RuntimeMigrationError("runtime evidence store identity is invalid")
    return store_id


def _inspect_target(connection: sqlite3.Connection) -> _TargetInspection:
    try:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            return _TargetInspection("INVALID", False, False, 0, None)
        if tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()) != ("ok",):
            return _TargetInspection("INVALID", False, False, 0, None)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return _TargetInspection("INVALID", False, False, 0, None)
        runtime_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'runtime_%'"
            ).fetchall()
        }
        if not runtime_tables:
            return _TargetInspection("UNINITIALIZED", True, True, 0, None)
        if "runtime_schema_migration" not in runtime_tables:
            return _TargetInspection("PARTIALLY_MIGRATED", False, False, 0, None)
        rows = connection.execute(
            "SELECT version,name,checksum,applied_at FROM runtime_schema_migration "
            "ORDER BY version"
        ).fetchall()
        versions = [int(row[0]) for row in rows]
        if not versions:
            return _TargetInspection("PARTIALLY_MIGRATED", False, False, 0, None)
        if any(version < 1 or version > RUNTIME_OUTBOX_SCHEMA_VERSION for version in versions):
            return _TargetInspection("UNKNOWN_VERSION", False, False, max(versions), None)
        current = versions[-1]
        if [tuple(row)[:3] for row in rows] != _expected_history(current):
            return _TargetInspection("INVALID", False, False, current, None)
        try:
            applied_times = [_utc_from_text(row[3]) for row in rows]
        except RuntimeMigrationError:
            return _TargetInspection("INVALID", False, False, current, None)
        if applied_times != sorted(applied_times):
            return _TargetInspection("INVALID", False, False, current, None)
        try:
            store_id = _validate_version_schema(connection, current)
        except RuntimeMigrationError as exc:
            state = "PARTIALLY_MIGRATED" if exc.code == "RUNTIME_SCHEMA_PARTIAL" else "INVALID"
            return _TargetInspection(state, False, True, current, None)
        state = "VALID_LATEST_VERSION" if current == RUNTIME_OUTBOX_SCHEMA_VERSION else "VALID_CURRENT_VERSION"
        return _TargetInspection(state, True, True, current, store_id)
    except sqlite3.Error:
        return _TargetInspection("INVALID", False, False, 0, None)


def audit_runtime_evidence_schema(connection: sqlite3.Connection) -> str:
    if not connection.in_transaction:
        connection.execute("PRAGMA foreign_keys=ON")
    inspection = _inspect_target(connection)
    if inspection.state in {"UNINITIALIZED", "VALID_CURRENT_VERSION"}:
        raise RuntimeMigrationRequired(
            "runtime evidence migration is not at the latest version", code="RUNTIME_MIGRATION_REQUIRED"
        )
    if inspection.state != "VALID_LATEST_VERSION" or inspection.runtime_store_id is None:
        raise RuntimeMigrationError(
            f"runtime evidence target is {inspection.state}", code="RUNTIME_DATABASE_INTEGRITY"
        )
    return inspection.runtime_store_id


def runtime_evidence_schema_ready(connection: sqlite3.Connection) -> bool:
    try:
        audit_runtime_evidence_schema(connection)
    except RuntimeMigrationRequired:
        return False
    return True


def _execute_script_in_transaction(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                connection.execute(statement)
    if buffer.strip():
        raise RuntimeMigrationError("runtime migration SQL is incomplete")


def _apply_pending_in_transaction(
    connection: sqlite3.Connection,
    current_version: int,
    applied_at: datetime,
) -> str:
    if not connection.in_transaction:
        raise RuntimeMigrationError("runtime migration requires an explicit transaction")
    inspection = _inspect_target(connection)
    block_code = _migration_block_code(connection, inspection)
    if block_code is not None:
        raise RuntimeMigrationError(
            "legacy Runtime Evidence v1 data cannot be upgraded automatically",
            code=block_code,
        )
    created_store_id: str | None = None
    for migration in _MIGRATIONS[current_version:]:
        _execute_script_in_transaction(connection, _migration_sql(migration))
        if migration.version == 2:
            created_store_id = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            connection.execute(
                "INSERT INTO runtime_evidence_meta(key,value) VALUES(?,?),(?,?)",
                (
                    "schema",
                    RUNTIME_EVIDENCE_STORE_SCHEMA,
                    "runtime_store_id",
                    created_store_id,
                ),
            )
        connection.execute(
            "INSERT INTO runtime_schema_migration(version,name,checksum,applied_at) "
            "VALUES(?,?,?,?)",
            (
                migration.version,
                migration.name,
                _migration_checksum(migration),
                _utc_text(applied_at),
            ),
        )
    inspected = _inspect_target(connection)
    if inspected.state != "VALID_LATEST_VERSION" or inspected.runtime_store_id is None:
        raise RuntimeMigrationError("runtime migration did not reach latest schema")
    if created_store_id is not None and created_store_id != inspected.runtime_store_id:
        raise RuntimeMigrationError("runtime evidence store identity changed during migration")
    return inspected.runtime_store_id


def _apply_pending(
    connection: sqlite3.Connection,
    current_version: int,
    applied_at: datetime,
) -> str:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        store_id = _apply_pending_in_transaction(
            connection,
            current_version,
            applied_at,
        )
        connection.commit()
        return store_id
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def _checked_path(value: str | Path, name: str, *, must_exist: bool) -> Path:
    absolute = Path(value).expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise RuntimeMigrationError(f"{name} cannot traverse a link")
    try:
        resolved = absolute.resolve(strict=must_exist)
    except OSError as exc:
        raise RuntimeMigrationError(f"cannot resolve {name}") from exc
    if must_exist and (not resolved.is_file() or _is_link(resolved)):
        raise RuntimeMigrationError(f"{name} must be a regular non-link file")
    return resolved


def _path_identity(path: Path, name: str) -> tuple[int, int]:
    try:
        status = path.stat()
    except OSError as exc:
        raise RuntimeMigrationError(f"cannot inspect {name}") from exc
    return int(status.st_dev), int(status.st_ino)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    checked = _checked_path(path, "runtime database", must_exist=True)
    identity = _path_identity(checked, "runtime database")
    immutable = _wal_sha256(checked) is None
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    try:
        connection = sqlite3.connect(checked.as_uri() + query, uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise RuntimeMigrationError("cannot open runtime database read-only") from exc
    if _path_identity(checked, "runtime database") != identity:
        connection.close()
        raise RuntimeMigrationError("runtime database was replaced while opening")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeMigrationError("runtime database foreign keys are disabled")
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wal_sha256(path: Path) -> str | None:
    wal = Path(str(path) + "-wal")
    return _sha256_file(wal) if wal.is_file() else None


def _logical_database_sha256(connection: sqlite3.Connection) -> str:
    try:
        return hashlib.sha256(connection.serialize()).hexdigest()
    except (AttributeError, sqlite3.Error) as exc:
        raise RuntimeMigrationError(
            "cannot serialize the logical runtime database snapshot"
        ) from exc


def _deserialize_writable_rehearsal(
    connection: sqlite3.Connection,
    serialized: bytes,
) -> None:
    writable = bytearray(serialized)
    if len(writable) < 100 or bytes(writable[:16]) != b"SQLite format 3\x00":
        raise RuntimeMigrationError("runtime rehearsal snapshot is invalid")
    writable[18:20] = b"\x01\x01"
    connection.deserialize(bytes(writable))


def _migration_block_code(
    connection: sqlite3.Connection,
    inspection: _TargetInspection,
) -> str | None:
    if inspection.current_version not in {1, 2}:
        return None
    tables = [
        "runtime_transition_outbox",
        "runtime_outbox_delivery",
        "runtime_outbox_cursor",
        "runtime_outbox_quarantine",
    ]
    if inspection.current_version == 2:
        tables.extend(
            [
                "runtime_transition_occurrence",
                "runtime_worker_integrity_block",
            ]
        )
    if any(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) > 0
        for table in tables
    ):
        return "LEGACY_RUNTIME_EVIDENCE_REQUIRES_EXPORT"
    return None


def _backup_connection_no_overwrite(
    source_connection: sqlite3.Connection,
    backup: Path,
) -> str:
    if backup.exists() or _is_link(backup):
        raise RuntimeMigrationError("runtime migration backup target must not exist")
    backup.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=backup.parent, prefix=f".{backup.name}.backup-", suffix=".db"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.commit()
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temporary, backup)
        except FileExistsError as exc:
            raise RuntimeMigrationError("runtime migration backup target appeared") from exc
        return _sha256_file(backup)
    finally:
        if temporary.exists():
            temporary.unlink()


def _backup_no_overwrite(source: Path, backup: Path) -> str:
    with closing(_read_only_connection(source)) as source_connection:
        return _backup_connection_no_overwrite(source_connection, backup)


def _report(
    *,
    mode: str,
    database: Path,
    inspection: _TargetInspection,
    source_sha: str,
    source_wal_sha: str | None,
    source_logical_sha: str,
    rehearsal_performed: bool,
    rehearsal_latest_schema_passed: bool,
    database_modified: bool,
    runtime_store_id: str | None,
    migration_block_code: str | None = None,
    would_modify: bool | None = None,
    backup_path: Path | None = None,
    backup_sha256: str | None = None,
) -> RuntimeMigrationReport:
    pending = tuple(item.version for item in _MIGRATIONS if item.version > inspection.current_version)
    return RuntimeMigrationReport(
        mode=mode,
        database=str(database),
        target_schema_state=inspection.state,
        target_state_audit_passed=inspection.audit_passed,
        target_latest_schema_ready=(
            inspection.state == "VALID_LATEST_VERSION"
            and inspection.audit_passed
        ),
        rehearsal_performed=rehearsal_performed,
        rehearsal_latest_schema_passed=rehearsal_latest_schema_passed,
        migration_history_valid=inspection.history_valid,
        current_version=inspection.current_version,
        latest_version=RUNTIME_OUTBOX_SCHEMA_VERSION,
        pending_versions=pending,
        source_database_sha256=source_sha,
        source_wal_sha256=source_wal_sha,
        source_logical_sha256=source_logical_sha,
        migration_block_code=migration_block_code,
        would_modify=bool(pending) if would_modify is None else would_modify,
        database_modified=database_modified,
        runtime_store_id=runtime_store_id,
        backup_path=None if backup_path is None else str(backup_path),
        backup_sha256=backup_sha256,
    )


def migrate_runtime_database(
    database: str | Path,
    *,
    apply: bool = False,
    backup: str | Path | None = None,
    now: datetime | None = None,
) -> RuntimeMigrationReport:
    if type(apply) is not bool:
        raise RuntimeMigrationError("apply must be boolean")
    database_path = _checked_path(database, "runtime database", must_exist=True)
    expected_identity = _path_identity(database_path, "runtime database")
    source_sha = _sha256_file(database_path)
    source_wal_sha = _wal_sha256(database_path)
    observed = datetime.now(timezone.utc) if now is None else now
    _utc_text(observed)
    with closing(_read_only_connection(database_path)) as source:
        inspection = _inspect_target(source)
        source_logical_sha = _logical_database_sha256(source)
        migration_block_code = _migration_block_code(source, inspection)
        if not inspection.audit_passed or migration_block_code is not None:
            report = _report(
                mode="APPLY" if apply else "DRY_RUN",
                database=database_path,
                inspection=inspection,
                source_sha=source_sha,
                source_wal_sha=source_wal_sha,
                source_logical_sha=source_logical_sha,
                rehearsal_performed=False,
                rehearsal_latest_schema_passed=False,
                database_modified=False,
                runtime_store_id=inspection.runtime_store_id,
                migration_block_code=migration_block_code,
            )
            raise RuntimeMigrationError(
                (
                    "legacy Runtime Evidence requires explicit export"
                    if migration_block_code is not None
                    else f"runtime migration target is {inspection.state}"
                ),
                code=migration_block_code or "RUNTIME_TARGET_SCHEMA_INVALID",
                report=report,
            )
        try:
            with closing(sqlite3.connect(":memory:")) as rehearsal:
                _deserialize_writable_rehearsal(rehearsal, source.serialize())
                _apply_pending(rehearsal, inspection.current_version, observed)
                audit_runtime_evidence_schema(rehearsal)
        except Exception as exc:
            report = _report(
                mode="APPLY" if apply else "DRY_RUN",
                database=database_path,
                inspection=inspection,
                source_sha=source_sha,
                source_wal_sha=source_wal_sha,
                source_logical_sha=source_logical_sha,
                rehearsal_performed=True,
                rehearsal_latest_schema_passed=False,
                database_modified=False,
                runtime_store_id=inspection.runtime_store_id,
            )
            raise RuntimeMigrationError(
                "runtime migration rehearsal failed",
                code="RUNTIME_REHEARSAL_FAILED",
                report=report,
            ) from exc
    with closing(_read_only_connection(database_path)) as unchanged_source:
        unchanged_logical_sha = _logical_database_sha256(unchanged_source)
    if (
        _path_identity(database_path, "runtime database") != expected_identity
        or _sha256_file(database_path) != source_sha
        or _wal_sha256(database_path) != source_wal_sha
        or unchanged_logical_sha != source_logical_sha
    ):
        raise RuntimeMigrationError("runtime database changed during migration rehearsal")
    if not apply:
        return _report(
            mode="DRY_RUN",
            database=database_path,
            inspection=inspection,
            source_sha=source_sha,
            source_wal_sha=source_wal_sha,
            source_logical_sha=source_logical_sha,
            rehearsal_performed=True,
            rehearsal_latest_schema_passed=True,
            database_modified=False,
            runtime_store_id=inspection.runtime_store_id,
        )
    if backup is None:
        raise RuntimeMigrationError("--apply requires an explicit backup path")
    backup_path = _checked_path(backup, "runtime migration backup", must_exist=False)
    if database_path == backup_path:
        raise RuntimeMigrationError("backup path must differ from runtime database")

    backup_sha: str | None = None
    runtime_store_id: str | None = None
    final_inspection: _TargetInspection | None = None
    try:
        connection = sqlite3.connect(database_path, timeout=30.0)
    except sqlite3.Error as exc:
        raise RuntimeMigrationError("cannot open runtime database for migration") from exc
    with closing(connection):
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            locked_inspection = _inspect_target(connection)
            locked_logical_sha = _logical_database_sha256(connection)
            if (
                locked_inspection != inspection
                or locked_logical_sha != source_logical_sha
                or _path_identity(database_path, "runtime database")
                != expected_identity
            ):
                raise RuntimeMigrationError(
                    "runtime database changed before the exclusive migration lock"
                )
            with closing(_read_only_connection(database_path)) as locked_source:
                if _logical_database_sha256(locked_source) != source_logical_sha:
                    raise RuntimeMigrationError(
                        "runtime database changed before the locked backup"
                    )
                backup_sha = _backup_connection_no_overwrite(
                    locked_source,
                    backup_path,
                )
            runtime_store_id = _apply_pending_in_transaction(
                connection,
                inspection.current_version,
                observed,
            )
            final_inspection = _inspect_target(connection)
            if (
                final_inspection.state != "VALID_LATEST_VERSION"
                or final_inspection.runtime_store_id != runtime_store_id
            ):
                raise RuntimeMigrationError(
                    "runtime migration failed its in-transaction verification"
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
    if (
        runtime_store_id is None
        or final_inspection is None
        or backup_sha is None
        or _path_identity(database_path, "runtime database") != expected_identity
    ):
        raise RuntimeMigrationError("runtime migration did not produce a verified result")
    with closing(_read_only_connection(database_path)) as verified:
        after_logical_sha = _logical_database_sha256(verified)
        verified_inspection = _inspect_target(verified)
        if audit_runtime_evidence_schema(verified) != runtime_store_id:
            raise RuntimeMigrationError("runtime migration verification failed")
    modified = source_logical_sha != after_logical_sha
    if bool(inspection.current_version < RUNTIME_OUTBOX_SCHEMA_VERSION) != modified:
        raise RuntimeMigrationError("runtime migration modification result is inconsistent")
    if verified_inspection != final_inspection:
        raise RuntimeMigrationError("runtime migration post-commit schema changed")
    return _report(
        mode="APPLY",
        database=database_path,
        inspection=verified_inspection,
        source_sha=source_sha,
        source_wal_sha=source_wal_sha,
        source_logical_sha=source_logical_sha,
        rehearsal_performed=True,
        rehearsal_latest_schema_passed=True,
        database_modified=modified,
        runtime_store_id=runtime_store_id,
        would_modify=inspection.current_version < RUNTIME_OUTBOX_SCHEMA_VERSION,
        backup_path=backup_path,
        backup_sha256=backup_sha,
    )


def report_json(report: RuntimeMigrationReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)


__all__ = [
    "RUNTIME_EVIDENCE_STORE_SCHEMA",
    "RUNTIME_MIGRATION_SCHEMA",
    "RUNTIME_OUTBOX_SCHEMA_VERSION",
    "RuntimeMigrationError",
    "RuntimeMigrationReport",
    "RuntimeMigrationRequired",
    "audit_runtime_evidence_schema",
    "migrate_runtime_database",
    "report_json",
    "runtime_evidence_schema_ready",
]
