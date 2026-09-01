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

RUNTIME_MIGRATION_SCHEMA = "stage4g1-runtime-migration-report-v3"
RUNTIME_EVIDENCE_STORE_SCHEMA = "stage4g1-runtime-evidence-store-v2"
RUNTIME_OUTBOX_SCHEMA_VERSION = 2
_MIGRATION_ROOT = Path(__file__).with_name("migrations_runtime")


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    path: Path


_MIGRATIONS = (
    _Migration(1, "runtime_evidence_outbox", _MIGRATION_ROOT / "0001_runtime_evidence_outbox.sql"),
    _Migration(2, "runtime_occurrence_identity", _MIGRATION_ROOT / "0002_runtime_occurrence_identity.sql"),
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


@dataclass(frozen=True, slots=True)
class RuntimeMigrationReport:
    mode: str
    database: str
    target_schema_state: str
    target_schema_audit_passed: bool
    rehearsal_performed: bool
    rehearsal_audit_passed: bool
    migration_history_valid: bool
    current_version: int
    latest_version: int
    pending_versions: tuple[int, ...]
    source_database_sha256: str
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
            "target_schema_audit_passed": self.target_schema_audit_passed,
            "rehearsal_performed": self.rehearsal_performed,
            "rehearsal_audit_passed": self.rehearsal_audit_passed,
            "migration_history_valid": self.migration_history_valid,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "pending_versions": list(self.pending_versions),
            "source_database_sha256": self.source_database_sha256,
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
    columns = _TABLE_COLUMNS_V1 if version == 1 else _TABLE_COLUMNS_V2
    indexes = _INDEXES_V1 if version == 1 else _INDEXES_V2
    triggers = _TRIGGERS_V1 if version == 1 else _TRIGGERS_V2
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
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name LIKE 'runtime_%'"
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
        if tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()) != ("ok",):
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


def _apply_pending(connection: sqlite3.Connection, current_version: int, applied_at: datetime) -> str:
    created_store_id: str | None = None
    for migration in _MIGRATIONS[current_version:]:
        script = "BEGIN IMMEDIATE;\n" + _migration_sql(migration)
        if migration.version == 2:
            created_store_id = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            script += (
                "\nINSERT INTO runtime_evidence_meta(key,value) VALUES"
                f"('schema','{RUNTIME_EVIDENCE_STORE_SCHEMA}'),"
                f"('runtime_store_id','{created_store_id}');"
            )
        script += (
            "\nINSERT INTO runtime_schema_migration(version,name,checksum,applied_at) VALUES("
            f"{migration.version},'{migration.name}','{_migration_checksum(migration)}','{_utc_text(applied_at)}');\nCOMMIT;"
        )
        try:
            connection.executescript(script)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
    inspected = _inspect_target(connection)
    if inspected.state != "VALID_LATEST_VERSION" or inspected.runtime_store_id is None:
        raise RuntimeMigrationError("runtime migration did not reach latest schema")
    if created_store_id is not None and created_store_id != inspected.runtime_store_id:
        raise RuntimeMigrationError("runtime evidence store identity changed during migration")
    return inspected.runtime_store_id


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
    try:
        connection = sqlite3.connect(checked.as_uri() + "?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise RuntimeMigrationError("cannot open runtime database read-only") from exc
    if _path_identity(checked, "runtime database") != identity:
        connection.close()
        raise RuntimeMigrationError("runtime database was replaced while opening")
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_no_overwrite(source: Path, backup: Path) -> str:
    if backup.exists() or _is_link(backup):
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


def _report(
    *,
    mode: str,
    database: Path,
    inspection: _TargetInspection,
    source_sha: str,
    rehearsal_performed: bool,
    rehearsal_audit_passed: bool,
    database_modified: bool,
    runtime_store_id: str | None,
    would_modify: bool | None = None,
    backup_path: Path | None = None,
    backup_sha256: str | None = None,
) -> RuntimeMigrationReport:
    pending = tuple(item.version for item in _MIGRATIONS if item.version > inspection.current_version)
    return RuntimeMigrationReport(
        mode=mode,
        database=str(database),
        target_schema_state=inspection.state,
        target_schema_audit_passed=inspection.audit_passed,
        rehearsal_performed=rehearsal_performed,
        rehearsal_audit_passed=rehearsal_audit_passed,
        migration_history_valid=inspection.history_valid,
        current_version=inspection.current_version,
        latest_version=RUNTIME_OUTBOX_SCHEMA_VERSION,
        pending_versions=pending,
        source_database_sha256=source_sha,
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
    observed = datetime.now(timezone.utc) if now is None else now
    _utc_text(observed)
    with closing(_read_only_connection(database_path)) as source:
        inspection = _inspect_target(source)
        if not inspection.audit_passed:
            report = _report(
                mode="APPLY" if apply else "DRY_RUN",
                database=database_path,
                inspection=inspection,
                source_sha=source_sha,
                rehearsal_performed=False,
                rehearsal_audit_passed=False,
                database_modified=False,
                runtime_store_id=inspection.runtime_store_id,
            )
            raise RuntimeMigrationError(
                f"runtime migration target is {inspection.state}",
                code="RUNTIME_TARGET_SCHEMA_INVALID",
                report=report,
            )
        try:
            with closing(sqlite3.connect(":memory:")) as rehearsal:
                source.backup(rehearsal)
                _apply_pending(rehearsal, inspection.current_version, observed)
                audit_runtime_evidence_schema(rehearsal)
        except Exception as exc:
            report = _report(
                mode="APPLY" if apply else "DRY_RUN",
                database=database_path,
                inspection=inspection,
                source_sha=source_sha,
                rehearsal_performed=True,
                rehearsal_audit_passed=False,
                database_modified=False,
                runtime_store_id=inspection.runtime_store_id,
            )
            raise RuntimeMigrationError(
                "runtime migration rehearsal failed", code="RUNTIME_REHEARSAL_FAILED", report=report
            ) from exc
    if _path_identity(database_path, "runtime database") != expected_identity or _sha256_file(database_path) != source_sha:
        raise RuntimeMigrationError("runtime database changed during migration rehearsal")
    if not apply:
        return _report(
            mode="DRY_RUN",
            database=database_path,
            inspection=inspection,
            source_sha=source_sha,
            rehearsal_performed=True,
            rehearsal_audit_passed=True,
            database_modified=False,
            runtime_store_id=inspection.runtime_store_id,
        )
    if backup is None:
        raise RuntimeMigrationError("--apply requires an explicit backup path")
    backup_path = _checked_path(backup, "runtime migration backup", must_exist=False)
    if database_path == backup_path:
        raise RuntimeMigrationError("backup path must differ from runtime database")
    backup_sha = _backup_no_overwrite(database_path, backup_path)
    if _path_identity(database_path, "runtime database") != expected_identity:
        raise RuntimeMigrationError("runtime database was replaced before migration apply")
    with closing(sqlite3.connect(database_path, timeout=30.0)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        runtime_store_id = _apply_pending(connection, inspection.current_version, observed)
    if _path_identity(database_path, "runtime database") != expected_identity:
        raise RuntimeMigrationError("runtime database was replaced during migration")
    after_sha = _sha256_file(database_path)
    modified = source_sha != after_sha
    if bool(inspection.current_version < RUNTIME_OUTBOX_SCHEMA_VERSION) != modified:
        raise RuntimeMigrationError("runtime migration modification result is inconsistent")
    with closing(_read_only_connection(database_path)) as verified:
        final_inspection = _inspect_target(verified)
        if audit_runtime_evidence_schema(verified) != runtime_store_id:
            raise RuntimeMigrationError("runtime migration verification failed")
    return _report(
        mode="APPLY",
        database=database_path,
        inspection=final_inspection,
        source_sha=source_sha,
        rehearsal_performed=True,
        rehearsal_audit_passed=True,
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
