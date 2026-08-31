from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_MIGRATION_SCHEMA = "stage4g1-runtime-migration-history-v1"
RUNTIME_OUTBOX_SCHEMA_VERSION = 1
_MIGRATION_ROOT = Path(__file__).with_name("migrations_runtime")
_MIGRATION_FILE = _MIGRATION_ROOT / "0001_runtime_evidence_outbox.sql"
_MIGRATION_NAME = "runtime_evidence_outbox"
_RUNTIME_TABLES = {
    "runtime_schema_migration",
    "runtime_transition_outbox",
    "runtime_outbox_delivery",
    "runtime_outbox_cursor",
    "runtime_outbox_quarantine",
}
_EXPECTED_COLUMNS = {
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
_EXPECTED_EXPLICIT_INDEXES = {
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
_EXPECTED_TRIGGERS = {
    "runtime_schema_migration_no_update": (
        "runtime_schema_migration",
        (
            "CREATE TRIGGER runtime_schema_migration_no_update "
            "BEFORE UPDATE ON runtime_schema_migration BEGIN "
            "SELECT RAISE(ABORT, 'runtime_schema_migration is append-only'); END"
        ),
    ),
    "runtime_schema_migration_no_delete": (
        "runtime_schema_migration",
        (
            "CREATE TRIGGER runtime_schema_migration_no_delete "
            "BEFORE DELETE ON runtime_schema_migration BEGIN "
            "SELECT RAISE(ABORT, 'runtime_schema_migration is append-only'); END"
        ),
    ),
    "runtime_transition_outbox_no_update": (
        "runtime_transition_outbox",
        (
            "CREATE TRIGGER runtime_transition_outbox_no_update "
            "BEFORE UPDATE ON runtime_transition_outbox BEGIN "
            "SELECT RAISE(ABORT, 'runtime_transition_outbox payload is immutable'); END"
        ),
    ),
    "runtime_transition_outbox_no_delete": (
        "runtime_transition_outbox",
        (
            "CREATE TRIGGER runtime_transition_outbox_no_delete "
            "BEFORE DELETE ON runtime_transition_outbox BEGIN "
            "SELECT RAISE(ABORT, 'runtime_transition_outbox payload is immutable'); END"
        ),
    ),
    "runtime_outbox_quarantine_no_update": (
        "runtime_outbox_quarantine",
        (
            "CREATE TRIGGER runtime_outbox_quarantine_no_update "
            "BEFORE UPDATE ON runtime_outbox_quarantine BEGIN "
            "SELECT RAISE(ABORT, 'runtime_outbox_quarantine is append-only'); END"
        ),
    ),
    "runtime_outbox_quarantine_no_delete": (
        "runtime_outbox_quarantine",
        (
            "CREATE TRIGGER runtime_outbox_quarantine_no_delete "
            "BEFORE DELETE ON runtime_outbox_quarantine BEGIN "
            "SELECT RAISE(ABORT, 'runtime_outbox_quarantine is append-only'); END"
        ),
    ),
}


class RuntimeMigrationError(RuntimeError):
    pass


class RuntimeMigrationRequired(RuntimeMigrationError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeMigrationReport:
    mode: str
    database: str
    current_version: int
    pending_versions: tuple[int, ...]
    database_modified: bool
    schema_audit_passed: bool
    backup_path: str | None
    backup_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RUNTIME_MIGRATION_SCHEMA,
            "mode": self.mode,
            "database": self.database,
            "current_version": self.current_version,
            "pending_versions": list(self.pending_versions),
            "database_modified": self.database_modified,
            "schema_audit_passed": self.schema_audit_passed,
            "backup_path": self.backup_path,
            "backup_sha256": self.backup_sha256,
            "auto_trade": False,
            "production_database_modified": self.database_modified,
        }


def _migration_sql() -> str:
    try:
        return _MIGRATION_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeMigrationError("runtime migration SQL is unavailable") from exc


def _migration_checksum() -> str:
    normalized = _migration_sql().replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeMigrationError("migration time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]), int(row[6]))
        for row in connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
    )


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        raise RuntimeMigrationError("runtime trigger SQL definition is missing")
    return " ".join(value.lower().split()).rstrip(";")


def audit_runtime_evidence_schema(connection: sqlite3.Connection) -> None:
    try:
        quick_check = tuple(
            str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
        )
        if quick_check != ("ok",):
            raise RuntimeMigrationError("runtime database integrity check failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'runtime_%'"
            ).fetchall()
        }
        if tables != _RUNTIME_TABLES:
            raise RuntimeMigrationRequired("runtime evidence migration is not applied")
        for table, expected in _EXPECTED_COLUMNS.items():
            if _table_columns(connection, table) != expected:
                raise RuntimeMigrationError(f"runtime table schema is invalid: {table}")
        views = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'runtime_%'"
            ).fetchall()
        )
        if views:
            raise RuntimeMigrationError("runtime evidence views are forbidden")
        triggers = {
            str(row[0]): (str(row[1]), _normalized_sql(row[2]))
            for row in connection.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name LIKE 'runtime_%'"
            ).fetchall()
        }
        expected_triggers = {
            name: (table, _normalized_sql(sql))
            for name, (table, sql) in _EXPECTED_TRIGGERS.items()
        }
        if triggers != expected_triggers:
            raise RuntimeMigrationError("runtime evidence trigger set is invalid")
        explicit_indexes: dict[str, tuple[str, tuple[str, ...], int]] = {}
        for table in _RUNTIME_TABLES:
            for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
                if str(row[3]) != "c":
                    continue
                name = str(row[1])
                columns = tuple(
                    str(item[1])
                    for item in connection.execute(
                        "SELECT seqno,name FROM pragma_index_info(?) ORDER BY seqno",
                        (name,),
                    ).fetchall()
                )
                explicit_indexes[name] = (table, columns, int(row[2]))
        if explicit_indexes != _EXPECTED_EXPLICIT_INDEXES:
            raise RuntimeMigrationError("runtime evidence index set is invalid")
        rows = connection.execute(
            "SELECT version,name,checksum FROM runtime_schema_migration ORDER BY version"
        ).fetchall()
        expected_rows = [
            (RUNTIME_OUTBOX_SCHEMA_VERSION, _MIGRATION_NAME, _migration_checksum())
        ]
        if [tuple(row) for row in rows] != expected_rows:
            raise RuntimeMigrationError("runtime migration history is invalid")
    except RuntimeMigrationError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeMigrationError("runtime evidence schema audit failed") from exc


def runtime_evidence_schema_ready(connection: sqlite3.Connection) -> bool:
    try:
        audit_runtime_evidence_schema(connection)
    except RuntimeMigrationRequired:
        return False
    return True


def _applied_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_schema_migration'"
    ).fetchone()
    if table is None:
        return 0
    rows = connection.execute(
        "SELECT version,name,checksum FROM runtime_schema_migration ORDER BY version"
    ).fetchall()
    if not rows:
        raise RuntimeMigrationError("runtime migration history is empty")
    expected = (RUNTIME_OUTBOX_SCHEMA_VERSION, _MIGRATION_NAME, _migration_checksum())
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise RuntimeMigrationError("runtime migration history is invalid")
    return int(rows[-1][0])


def _apply_connection(connection: sqlite3.Connection, applied_at: datetime) -> None:
    current = _applied_version(connection)
    if current == RUNTIME_OUTBOX_SCHEMA_VERSION:
        audit_runtime_evidence_schema(connection)
        return
    if current != 0:
        raise RuntimeMigrationError("unsupported runtime migration version")
    script = (
        "BEGIN IMMEDIATE;\n"
        + _migration_sql()
        + "\nINSERT INTO runtime_schema_migration(version,name,checksum,applied_at) VALUES("
        + str(RUNTIME_OUTBOX_SCHEMA_VERSION)
        + ","
        + _sql_literal(_MIGRATION_NAME)
        + ","
        + _sql_literal(_migration_checksum())
        + ","
        + _sql_literal(_utc_text(applied_at))
        + ");\nCOMMIT;"
    )
    try:
        connection.executescript(script)
        audit_runtime_evidence_schema(connection)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise RuntimeMigrationError("runtime database must be a regular file")
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise RuntimeMigrationError("cannot open runtime database read-only") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_no_overwrite(source: Path, backup: Path) -> str:
    if backup.exists() or backup.is_symlink():
        raise RuntimeMigrationError("runtime migration backup target must not exist")
    backup.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=backup.parent, prefix=f".{backup.name}.backup-", suffix=".db"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(_read_only_connection(source)) as source_connection, closing(
            sqlite3.connect(temporary)
        ) as target_connection:
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


def migrate_runtime_database(
    database: str | Path,
    *,
    apply: bool = False,
    backup: str | Path | None = None,
    now: datetime | None = None,
) -> RuntimeMigrationReport:
    if type(apply) is not bool:
        raise RuntimeMigrationError("apply must be boolean")
    database_path = Path(database).expanduser().resolve(strict=True)
    observed = datetime.now(timezone.utc) if now is None else now
    _utc_text(observed)
    with closing(_read_only_connection(database_path)) as source:
        current = _applied_version(source)
        pending = () if current == RUNTIME_OUTBOX_SCHEMA_VERSION else (1,)
        if not apply:
            with closing(sqlite3.connect(":memory:")) as rehearsal:
                source.backup(rehearsal)
                _apply_connection(rehearsal, observed)
            return RuntimeMigrationReport(
                mode="DRY_RUN",
                database=str(database_path),
                current_version=current,
                pending_versions=pending,
                database_modified=False,
                schema_audit_passed=True,
                backup_path=None,
                backup_sha256=None,
            )
    if backup is None:
        raise RuntimeMigrationError("--apply requires an explicit backup path")
    backup_path = Path(backup).expanduser().resolve(strict=False)
    if database_path == backup_path:
        raise RuntimeMigrationError("backup path must differ from runtime database")
    backup_sha = _backup_no_overwrite(database_path, backup_path)
    with closing(sqlite3.connect(database_path, timeout=30.0)) as connection:
        _apply_connection(connection, observed)
    return RuntimeMigrationReport(
        mode="APPLY",
        database=str(database_path),
        current_version=RUNTIME_OUTBOX_SCHEMA_VERSION,
        pending_versions=(),
        database_modified=bool(pending),
        schema_audit_passed=True,
        backup_path=str(backup_path),
        backup_sha256=backup_sha,
    )


def report_json(report: RuntimeMigrationReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)


__all__ = [
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
