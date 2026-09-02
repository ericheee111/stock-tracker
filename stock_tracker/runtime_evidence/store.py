from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .contracts import (
    RuntimeDecisionArtifact,
    RuntimeEvidenceContractError,
    SystemUtcClock,
    UtcClock,
    require_utc_clock_value,
)

RUNTIME_ARTIFACT_STORE_SCHEMA = "stage4g1-runtime-artifact-store-v2"
RUNTIME_ARTIFACT_RECORD_SCHEMA = "stage4g1-runtime-artifact-record-v1"
RUNTIME_ARTIFACT_AUDIT_SCHEMA = "stage4g1-runtime-artifact-audit-v2"
DEFAULT_RUNTIME_ARTIFACT_CATALOG = Path("data/runtime-decision-artifacts.db")
DEFAULT_RUNTIME_ARTIFACT_ROOT = Path("data/runtime-decision-artifacts")
_ZERO_HASH = "0" * 64
_SQLITE_HEADER = b"SQLite format 3\x00"
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_META_COLUMNS = (
    ("key", "TEXT", 0, 1, 0),
    ("value", "TEXT", 1, 0, 0),
)
_ARTIFACT_COLUMNS = (
    ("append_order", "INTEGER", 0, 1, 0),
    ("artifact_id", "TEXT", 1, 0, 0),
    ("transition_event_id", "TEXT", 1, 0, 0),
    ("runtime_signal_id", "TEXT", 1, 0, 0),
    ("stored_at", "TEXT", 1, 0, 0),
    ("previous_record_hash", "TEXT", 1, 0, 0),
    ("record_hash", "TEXT", 1, 0, 0),
    ("record_file", "TEXT", 1, 0, 0),
    ("record_file_sha256", "TEXT", 1, 0, 0),
    ("payload_sha256", "TEXT", 1, 0, 0),
)
_EXPECTED_TRIGGERS = {
    "runtime_artifact_meta_no_update": (
        "runtime_artifact_meta",
        (
            "CREATE TRIGGER runtime_artifact_meta_no_update "
            "BEFORE UPDATE ON runtime_artifact_meta BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifact_meta is append-only'); END"
        ),
    ),
    "runtime_artifact_meta_no_delete": (
        "runtime_artifact_meta",
        (
            "CREATE TRIGGER runtime_artifact_meta_no_delete "
            "BEFORE DELETE ON runtime_artifact_meta BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifact_meta is append-only'); END"
        ),
    ),
    "runtime_artifacts_no_update": (
        "runtime_artifacts",
        (
            "CREATE TRIGGER runtime_artifacts_no_update "
            "BEFORE UPDATE ON runtime_artifacts BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifacts is append-only'); END"
        ),
    ),
    "runtime_artifacts_no_delete": (
        "runtime_artifacts",
        (
            "CREATE TRIGGER runtime_artifacts_no_delete "
            "BEFORE DELETE ON runtime_artifacts BEGIN "
            "SELECT RAISE(ABORT, 'runtime_artifacts is append-only'); END"
        ),
    ),
}
_CATALOG_SCHEMA_SQL = (
    "CREATE TABLE runtime_artifact_meta ("
    "key TEXT PRIMARY KEY,value TEXT NOT NULL);"
    + _EXPECTED_TRIGGERS["runtime_artifact_meta_no_update"][1]
    + ";"
    + _EXPECTED_TRIGGERS["runtime_artifact_meta_no_delete"][1]
    + ";"
    "CREATE TABLE runtime_artifacts ("
    "append_order INTEGER PRIMARY KEY CHECK(append_order > 0),"
    "artifact_id TEXT NOT NULL UNIQUE,"
    "transition_event_id TEXT NOT NULL UNIQUE,"
    "runtime_signal_id TEXT NOT NULL,"
    "stored_at TEXT NOT NULL,"
    "previous_record_hash TEXT NOT NULL,"
    "record_hash TEXT NOT NULL UNIQUE,"
    "record_file TEXT NOT NULL UNIQUE,"
    "record_file_sha256 TEXT NOT NULL,"
    "payload_sha256 TEXT NOT NULL);"
    "CREATE INDEX idx_runtime_artifacts_signal "
    "ON runtime_artifacts(runtime_signal_id,append_order);"
    + _EXPECTED_TRIGGERS["runtime_artifacts_no_update"][1]
    + ";"
    + _EXPECTED_TRIGGERS["runtime_artifacts_no_delete"][1]
    + ";"
)


class RuntimeArtifactFailureClass(StrEnum):
    ROW_PERMANENT = "ROW_PERMANENT"
    ROW_TRANSIENT = "ROW_TRANSIENT"
    STORE_INTEGRITY_BLOCK = "STORE_INTEGRITY_BLOCK"


class RuntimeArtifactStoreError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "ARTIFACT_STORE_INTEGRITY_FAILURE",
        failure_class: RuntimeArtifactFailureClass = (
            RuntimeArtifactFailureClass.STORE_INTEGRITY_BLOCK
        ),
        safe_to_quarantine: bool = False,
    ) -> None:
        if type(code) is not str or not code or code != code.strip():
            raise TypeError("code must be a trimmed non-empty string")
        if type(failure_class) is not RuntimeArtifactFailureClass:
            raise TypeError("failure_class must be RuntimeArtifactFailureClass")
        if type(safe_to_quarantine) is not bool:
            raise TypeError("safe_to_quarantine must be boolean")
        super().__init__(message)
        self.code = code
        self.failure_class = failure_class
        self.safe_to_quarantine = safe_to_quarantine


class RuntimeArtifactAppendDisposition(StrEnum):
    APPENDED = "APPENDED"
    IDEMPOTENT = "IDEMPOTENT"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RuntimeArtifactStoreError(f"{name} must be lowercase SHA-256")
    return value


def _utc_text(value: object, name: str) -> str:
    return (
        require_utc_clock_value(value, name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_from_text(value: object, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z") or len(value) > 64:
        raise RuntimeArtifactStoreError(f"{name} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeArtifactStoreError(f"{name} must be ISO-8601") from exc
    if _utc_text(parsed, name) != value:
        raise RuntimeArtifactStoreError(f"{name} must be canonical UTC")
    return parsed


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeArtifactStoreError("artifact record is not canonical JSON") from exc
    if not raw or len(raw) > _MAX_RECORD_BYTES:
        raise RuntimeArtifactStoreError("artifact record exceeds the size bound")
    return raw


def _strict_json_loads(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_RECORD_BYTES:
        raise RuntimeArtifactStoreError("artifact record bytes are invalid")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeArtifactStoreError("artifact record has duplicate keys")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise RuntimeArtifactStoreError(f"artifact record has non-finite {token}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeArtifactStoreError("artifact record is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise RuntimeArtifactStoreError("artifact record must be a JSON object")
    if _canonical_json_bytes(value) != raw:
        raise RuntimeArtifactStoreError("artifact record JSON is not canonical")
    return value


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_path(path: Path, name: str) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise RuntimeArtifactStoreError(f"{name} cannot traverse a link")
    return absolute.resolve(strict=False)


def _path_identity(path: Path, name: str) -> tuple[int, int]:
    try:
        status = path.stat()
    except OSError as exc:
        raise RuntimeArtifactStoreError(f"cannot inspect {name}") from exc
    return int(status.st_dev), int(status.st_ino)


def _same_existing_file(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right)
    except OSError as exc:
        raise RuntimeArtifactStoreError("cannot compare store path identities") from exc


def _safe_record_path(root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise RuntimeArtifactStoreError("artifact record path is invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeArtifactStoreError("artifact record path escaped root")
    candidate = (root / relative_path).resolve(strict=False)
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        raise RuntimeArtifactStoreError("artifact record path escaped root")
    cursor = root
    for part in relative_path.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and _is_link(cursor):
            raise RuntimeArtifactStoreError("artifact record path contains a link")
    return candidate


def _atomic_write_no_overwrite(path: Path, raw: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_parent = _checked_path(path.parent, "artifact record parent")
    if checked_parent != path.parent.resolve(strict=False) or _is_link(path.parent):
        raise RuntimeArtifactStoreError("artifact record parent identity changed")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.publish-", suffix=".tmp"
    )
    created = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
            created = True
        except FileExistsError:
            if not path.is_file() or _is_link(path) or path.read_bytes() != raw:
                raise RuntimeArtifactStoreError(
                    "immutable artifact record collision",
                    code="ARTIFACT_ID_COLLISION",
                )
        except OSError as exc:
            raise RuntimeArtifactStoreError(
                "cannot atomically publish artifact",
                code="ARTIFACT_RECORD_PUBLICATION_IO",
                failure_class=RuntimeArtifactFailureClass.ROW_TRANSIENT,
            ) from exc
        return created
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]), int(row[6]))
        for row in connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
    )


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        raise RuntimeArtifactStoreError("artifact catalog SQL definition is missing")
    return " ".join(value.lower().split()).rstrip(";")


def _validate_catalog_schema(
    connection: sqlite3.Connection,
    *,
    expected_source_runtime_store_id: str | None = None,
) -> tuple[str, str, str]:
    try:
        journal_row = connection.execute("PRAGMA journal_mode").fetchone()
        journal = "" if journal_row is None else str(journal_row[0]).lower()
        if journal != "delete":
            raise RuntimeArtifactStoreError("artifact catalog journal mode must be DELETE")
        quick_check = tuple(
            str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
        )
        if quick_check != ("ok",):
            raise RuntimeArtifactStoreError("artifact catalog integrity check failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if tables != {"runtime_artifact_meta", "runtime_artifacts"}:
            raise RuntimeArtifactStoreError(
                "artifact catalog table set is invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        if _table_columns(connection, "runtime_artifact_meta") != _META_COLUMNS:
            raise RuntimeArtifactStoreError(
                "artifact metadata columns are invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        if _table_columns(connection, "runtime_artifacts") != _ARTIFACT_COLUMNS:
            raise RuntimeArtifactStoreError(
                "artifact record columns are invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        views = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if views:
            raise RuntimeArtifactStoreError(
                "artifact catalog views are forbidden",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        triggers = {
            str(row[0]): (str(row[1]), _normalized_sql(row[2]))
            for row in connection.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master "
                "WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_triggers = {
            name: (table, _normalized_sql(sql))
            for name, (table, sql) in _EXPECTED_TRIGGERS.items()
        }
        if triggers != expected_triggers:
            raise RuntimeArtifactStoreError(
                "artifact catalog trigger set is invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        explicit_indexes: dict[str, tuple[str, ...]] = {}
        for table in tables:
            for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
                if str(row[3]) != "c":
                    continue
                name = str(row[1])
                explicit_indexes[name] = tuple(
                    str(item[1])
                    for item in connection.execute(
                        "SELECT seqno,name FROM pragma_index_info(?) ORDER BY seqno",
                        (name,),
                    ).fetchall()
                )
        if explicit_indexes != {
            "idx_runtime_artifacts_signal": ("runtime_signal_id", "append_order")
        }:
            raise RuntimeArtifactStoreError(
                "artifact catalog index set is invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        unique_constraints = {
            table: {
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
            for table in tables
        }
        if unique_constraints != {
            "runtime_artifact_meta": set(),
            "runtime_artifacts": {
                ("artifact_id",),
                ("transition_event_id",),
                ("record_hash",),
                ("record_file",),
            },
        }:
            raise RuntimeArtifactStoreError(
                "artifact catalog unique constraints are invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key,value FROM runtime_artifact_meta ORDER BY key"
            ).fetchall()
        }
        if set(metadata) != {"schema", "store_id", "source_runtime_store_id"}:
            raise RuntimeArtifactStoreError(
                "artifact catalog metadata is invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        if metadata["schema"] != RUNTIME_ARTIFACT_STORE_SCHEMA:
            raise RuntimeArtifactStoreError(
                "artifact catalog schema identity is invalid",
                code="ARTIFACT_STORE_SCHEMA_MISMATCH",
            )
        store_id = _require_sha256(metadata["store_id"], "artifact store identity")
        source_runtime_store_id = _require_sha256(
            metadata["source_runtime_store_id"],
            "source runtime store identity",
        )
        if (
            expected_source_runtime_store_id is not None
            and source_runtime_store_id != expected_source_runtime_store_id
        ):
            raise RuntimeArtifactStoreError(
                "artifact store is bound to a different runtime evidence store",
                code="ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH",
            )
        return metadata["schema"], store_id, source_runtime_store_id
    except RuntimeArtifactStoreError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeArtifactStoreError(
            "artifact catalog schema audit failed",
            code="ARTIFACT_STORE_SCHEMA_MISMATCH",
        ) from exc


@dataclass(frozen=True, slots=True)
class RuntimeArtifactRecord:
    append_order: int
    store_id: str
    artifact: RuntimeDecisionArtifact
    stored_at: datetime
    previous_record_hash: str
    record_hash: str
    record_file: str
    record_file_sha256: str

    @classmethod
    def create(
        cls,
        *,
        append_order: int,
        store_id: str,
        artifact: RuntimeDecisionArtifact,
        stored_at: datetime,
        previous_record_hash: str,
    ) -> RuntimeArtifactRecord:
        identity = {
            "schema": RUNTIME_ARTIFACT_RECORD_SCHEMA,
            "append_order": append_order,
            "store_id": store_id,
            "stored_at": _utc_text(stored_at, "stored_at"),
            "previous_record_hash": previous_record_hash,
            "artifact": artifact.as_dict(),
        }
        record_hash = _sha256(_canonical_json_bytes(identity))
        relative = f"records/{artifact.artifact_id[:2]}/{artifact.artifact_id}.json"
        document = {**identity, "record_hash": record_hash}
        raw = _canonical_json_bytes(document)
        return cls(
            append_order=append_order,
            store_id=store_id,
            artifact=artifact,
            stored_at=require_utc_clock_value(stored_at, "stored_at"),
            previous_record_hash=previous_record_hash,
            record_hash=record_hash,
            record_file=relative,
            record_file_sha256=_sha256(raw),
        )

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "schema": RUNTIME_ARTIFACT_RECORD_SCHEMA,
                "append_order": self.append_order,
                "store_id": self.store_id,
                "stored_at": _utc_text(self.stored_at, "stored_at"),
                "previous_record_hash": self.previous_record_hash,
                "artifact": self.artifact.as_dict(),
                "record_hash": self.record_hash,
            }
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> RuntimeArtifactRecord:
        document = _strict_json_loads(raw)
        expected = {
            "schema",
            "append_order",
            "store_id",
            "stored_at",
            "previous_record_hash",
            "artifact",
            "record_hash",
        }
        if set(document) != expected or document["schema"] != RUNTIME_ARTIFACT_RECORD_SCHEMA:
            raise RuntimeArtifactStoreError("artifact record fields are invalid")
        append_order = document["append_order"]
        if type(append_order) is not int or append_order < 1:
            raise RuntimeArtifactStoreError("artifact append order is invalid")
        store_id = str(document["store_id"])
        previous = str(document["previous_record_hash"])
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in (store_id, previous)
        ):
            raise RuntimeArtifactStoreError("artifact record hash field is invalid")
        try:
            artifact = RuntimeDecisionArtifact.from_dict(document["artifact"])
        except RuntimeEvidenceContractError as exc:
            raise RuntimeArtifactStoreError("stored artifact contract is invalid") from exc
        stored_at = _utc_from_text(document["stored_at"], "stored_at")
        record = cls.create(
            append_order=append_order,
            store_id=store_id,
            artifact=artifact,
            stored_at=stored_at,
            previous_record_hash=previous,
        )
        if record.record_hash != document["record_hash"]:
            raise RuntimeArtifactStoreError("artifact record hash mismatch")
        if record.to_json_bytes() != raw:
            raise RuntimeArtifactStoreError("artifact record is not canonical")
        return record


@dataclass(frozen=True, slots=True)
class RuntimeArtifactAppendResult:
    disposition: RuntimeArtifactAppendDisposition
    record: RuntimeArtifactRecord


@dataclass(frozen=True, slots=True)
class RuntimeArtifactAuditReport:
    store_id: str
    source_runtime_store_id: str
    audited_at: datetime
    record_hashes: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    first_record_hash: str
    last_record_hash: str
    audit_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("store_id", self.store_id),
            ("source_runtime_store_id", self.source_runtime_store_id),
        ):
            _require_sha256(value, name)
        audited_at = require_utc_clock_value(self.audited_at, "audited_at")
        if type(self.record_hashes) is not tuple or type(self.artifact_ids) is not tuple:
            raise RuntimeArtifactStoreError("artifact audit identities must be tuples")
        if len(self.record_hashes) != len(self.artifact_ids):
            raise RuntimeArtifactStoreError("artifact audit identity counts differ")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise RuntimeArtifactStoreError("artifact audit contains duplicate artifact IDs")
        for value in (*self.record_hashes, *self.artifact_ids):
            if type(value) is not str or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise RuntimeArtifactStoreError("artifact audit contains invalid SHA-256")
        expected_first = _ZERO_HASH if not self.record_hashes else self.record_hashes[0]
        expected_last = _ZERO_HASH if not self.record_hashes else self.record_hashes[-1]
        if self.first_record_hash != expected_first or self.last_record_hash != expected_last:
            raise RuntimeArtifactStoreError("artifact audit boundaries are invalid")
        identity = {
            "schema": RUNTIME_ARTIFACT_AUDIT_SCHEMA,
            "store_id": self.store_id,
            "source_runtime_store_id": self.source_runtime_store_id,
            "audited_at": _utc_text(audited_at, "audited_at"),
            "record_hashes": list(self.record_hashes),
            "artifact_ids": list(self.artifact_ids),
            "first_record_hash": self.first_record_hash,
            "last_record_hash": self.last_record_hash,
        }
        object.__setattr__(self, "audit_id", _sha256(_canonical_json_bytes(identity)))

    @property
    def record_count(self) -> int:
        return len(self.record_hashes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RUNTIME_ARTIFACT_AUDIT_SCHEMA,
            "store_id": self.store_id,
            "source_runtime_store_id": self.source_runtime_store_id,
            "audited_at": _utc_text(self.audited_at, "audited_at"),
            "record_count": self.record_count,
            "record_hashes": list(self.record_hashes),
            "artifact_ids": list(self.artifact_ids),
            "first_record_hash": self.first_record_hash,
            "last_record_hash": self.last_record_hash,
            "audit_id": self.audit_id,
            "integrity_state": "PASSED",
            "production_database_modified": False,
            "auto_trade": False,
            "trusted_outcome_admission": False,
            "investment_performance_claim": False,
        }


class RuntimeArtifactStore:
    def __init__(
        self,
        record_root: str | Path = DEFAULT_RUNTIME_ARTIFACT_ROOT,
        catalog_path: str | Path = DEFAULT_RUNTIME_ARTIFACT_CATALOG,
        *,
        source_runtime_store_id: str,
        production_database: str | Path | None = None,
        forbidden_paths: Iterable[str | Path] = (),
        clock: UtcClock | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[2]
        source_runtime_store = _require_sha256(
            source_runtime_store_id,
            "source_runtime_store_id",
        )
        root = _checked_path(Path(record_root), "artifact record root")
        catalog = _checked_path(Path(catalog_path), "artifact catalog")
        production = _checked_path(
            Path(
                production_database
                if production_database is not None
                else project_root / "data" / "stock_tracker.db"
            ),
            "production database",
        )
        forbidden = tuple(
            _checked_path(Path(item), "forbidden artifact store path")
            for item in forbidden_paths
        )
        if root == catalog or catalog.is_relative_to(root) or root.is_relative_to(catalog):
            raise RuntimeArtifactStoreError("artifact catalog and record root must be separate")
        if production == root or production.is_relative_to(root):
            raise RuntimeArtifactStoreError("artifact record root contains production database")
        for target in forbidden:
            if (
                root == target
                or root.is_relative_to(target)
                or target.is_relative_to(root)
                or catalog == target
                or catalog.is_relative_to(target)
            ):
                raise RuntimeArtifactStoreError("artifact store overlaps a forbidden path")
        for target in (production, *forbidden):
            if catalog == target:
                raise RuntimeArtifactStoreError("artifact store overlaps a forbidden path")
            if _same_existing_file(catalog, target):
                raise RuntimeArtifactStoreError("artifact catalog aliases a forbidden database")
        root.mkdir(parents=True, exist_ok=True)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        if _is_link(root) or _is_link(catalog.parent):
            raise RuntimeArtifactStoreError("artifact store paths must not be links")
        self.record_root = root
        self.catalog_path = catalog
        self.source_runtime_store_id = source_runtime_store
        self.production_database = production
        self.forbidden_paths = forbidden
        self.clock = clock or SystemUtcClock()
        self._lock = threading.RLock()
        self._record_root_identity = _path_identity(root, "artifact record root")
        self._catalog_identity: tuple[int, int] | None = None
        self._initialize()
        self._catalog_identity = _path_identity(catalog, "artifact catalog")
        with self._connection() as connection:
            _schema, self.store_id, bound_runtime_store_id = _validate_catalog_schema(
                connection,
                expected_source_runtime_store_id=self.source_runtime_store_id,
            )
            if bound_runtime_store_id != self.source_runtime_store_id:
                raise RuntimeArtifactStoreError(
                    "artifact store runtime binding changed",
                    code="ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH",
                )

    def _initialize(self) -> None:
        if self.catalog_path.exists():
            if not self.catalog_path.is_file() or _is_link(self.catalog_path):
                raise RuntimeArtifactStoreError("artifact catalog must be a regular file")
            with closing(sqlite3.connect(self.catalog_path, timeout=30.0)) as connection:
                connection.row_factory = sqlite3.Row
                _validate_catalog_schema(
                    connection,
                    expected_source_runtime_store_id=self.source_runtime_store_id,
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.catalog_path.parent,
            prefix=f".{self.catalog_path.name}.init-",
            suffix=".db",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary, timeout=30.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(_CATALOG_SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO runtime_artifact_meta(key,value) VALUES('schema',?)",
                    (RUNTIME_ARTIFACT_STORE_SCHEMA,),
                )
                connection.execute(
                    "INSERT INTO runtime_artifact_meta(key,value) VALUES('store_id',?)",
                    (_sha256(secrets.token_bytes(32)),),
                )
                connection.execute(
                    "INSERT INTO runtime_artifact_meta(key,value) "
                    "VALUES('source_runtime_store_id',?)",
                    (self.source_runtime_store_id,),
                )
                connection.commit()
                _validate_catalog_schema(
                    connection,
                    expected_source_runtime_store_id=self.source_runtime_store_id,
                )
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.catalog_path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RuntimeArtifactStoreError("cannot publish artifact catalog") from exc
            with closing(sqlite3.connect(self.catalog_path, timeout=30.0)) as connection:
                connection.row_factory = sqlite3.Row
                _validate_catalog_schema(
                    connection,
                    expected_source_runtime_store_id=self.source_runtime_store_id,
                )
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(temporary) + suffix)
                if candidate.exists() and not _is_link(candidate):
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

    def _assert_identities(self) -> None:
        if _checked_path(self.record_root, "artifact record root") != self.record_root:
            raise RuntimeArtifactStoreError("artifact record root identity changed")
        if _path_identity(self.record_root, "artifact record root") != self._record_root_identity:
            raise RuntimeArtifactStoreError(
                "artifact record root was replaced",
                code="ARTIFACT_STORE_IDENTITY_REPLACED",
            )
        if not self.catalog_path.is_file() or _is_link(self.catalog_path):
            raise RuntimeArtifactStoreError("artifact catalog must remain a regular file")
        if self._catalog_identity is not None and _path_identity(
            self.catalog_path, "artifact catalog"
        ) != self._catalog_identity:
            raise RuntimeArtifactStoreError(
                "artifact catalog was replaced",
                code="ARTIFACT_STORE_IDENTITY_REPLACED",
            )
        for target in (self.production_database, *self.forbidden_paths):
            if _same_existing_file(self.catalog_path, target):
                raise RuntimeArtifactStoreError("artifact catalog aliases a forbidden path")
        try:
            with self.catalog_path.open("rb") as stream:
                header = stream.read(len(_SQLITE_HEADER))
        except OSError as exc:
            raise RuntimeArtifactStoreError("cannot read artifact catalog") from exc
        if header != _SQLITE_HEADER:
            raise RuntimeArtifactStoreError(
                "artifact catalog is not SQLite",
                code="ARTIFACT_STORE_IDENTITY_REPLACED",
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._assert_identities()
        try:
            connection = sqlite3.connect(self.catalog_path, timeout=30.0)
        except sqlite3.Error as exc:
            raise RuntimeArtifactStoreError(
                "cannot open artifact catalog",
                code="ARTIFACT_CATALOG_OPEN_FAILED",
                failure_class=(
                    RuntimeArtifactFailureClass.ROW_TRANSIENT
                    if isinstance(exc, sqlite3.OperationalError)
                    else RuntimeArtifactFailureClass.STORE_INTEGRITY_BLOCK
                ),
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            _validate_catalog_schema(
                connection,
                expected_source_runtime_store_id=self.source_runtime_store_id,
            )
            yield connection
            self._assert_identities()
        except RuntimeArtifactStoreError:
            raise
        except sqlite3.Error as exc:
            raise RuntimeArtifactStoreError(
                "artifact catalog operation failed",
                code="ARTIFACT_CATALOG_OPERATION_FAILED",
                failure_class=(
                    RuntimeArtifactFailureClass.ROW_TRANSIENT
                    if isinstance(exc, sqlite3.OperationalError)
                    else RuntimeArtifactFailureClass.STORE_INTEGRITY_BLOCK
                ),
            ) from exc
        finally:
            connection.close()

    def _record_from_row(self, row: sqlite3.Row) -> RuntimeArtifactRecord:
        path = _safe_record_path(self.record_root, str(row["record_file"]))
        if not path.is_file() or _is_link(path):
            raise RuntimeArtifactStoreError("artifact record file is missing")
        raw = path.read_bytes()
        if _sha256(raw) != str(row["record_file_sha256"]):
            raise RuntimeArtifactStoreError("artifact record file SHA mismatch")
        record = RuntimeArtifactRecord.from_json_bytes(raw)
        artifact_payload_sha = _sha256(record.artifact.to_json_bytes())
        values = (
            record.append_order == int(row["append_order"]),
            record.store_id == self.store_id,
            record.artifact.artifact_id == str(row["artifact_id"]),
            record.artifact.identity_dict()["transition_event_id"]
            == str(row["transition_event_id"]),
            record.artifact.identity_dict()["runtime_signal_id"]
            == str(row["runtime_signal_id"]),
            _utc_text(record.stored_at, "stored_at") == str(row["stored_at"]),
            record.previous_record_hash == str(row["previous_record_hash"]),
            record.record_hash == str(row["record_hash"]),
            record.record_file == str(row["record_file"]),
            artifact_payload_sha == str(row["payload_sha256"]),
        )
        if not all(values):
            raise RuntimeArtifactStoreError(
                "artifact catalog disagrees with record file",
                code="ARTIFACT_STORE_CATALOG_RECORD_MISMATCH",
            )
        return record

    def _validate_state(
        self,
        connection: sqlite3.Connection,
        audited_at: datetime,
        *,
        allowed_orphan: Path | None = None,
    ) -> tuple[RuntimeArtifactRecord, ...]:
        _schema, store_id, source_runtime_store_id = _validate_catalog_schema(
            connection,
            expected_source_runtime_store_id=self.source_runtime_store_id,
        )
        if store_id != self.store_id:
            raise RuntimeArtifactStoreError(
                "artifact store identity changed",
                code="ARTIFACT_STORE_IDENTITY_REPLACED",
            )
        if source_runtime_store_id != self.source_runtime_store_id:
            raise RuntimeArtifactStoreError(
                "artifact store runtime binding changed",
                code="ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH",
            )
        rows = connection.execute(
            "SELECT * FROM runtime_artifacts ORDER BY append_order"
        ).fetchall()
        records: list[RuntimeArtifactRecord] = []
        expected_previous = _ZERO_HASH
        previous_stored_at: datetime | None = None
        expected_files: set[Path] = set()
        for expected_order, row in enumerate(rows, start=1):
            record = self._record_from_row(row)
            if (
                record.artifact.identity_dict()["runtime_store_id"]
                != self.source_runtime_store_id
            ):
                raise RuntimeArtifactStoreError(
                    "artifact record belongs to a different runtime evidence store",
                    code="ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH",
                )
            if record.append_order != expected_order:
                raise RuntimeArtifactStoreError("artifact append order is not contiguous")
            if record.previous_record_hash != expected_previous:
                raise RuntimeArtifactStoreError(
                    "artifact record hash chain is broken",
                    code="ARTIFACT_STORE_HASH_CHAIN_FAILURE",
                )
            if record.stored_at > audited_at:
                raise RuntimeArtifactStoreError(
                    "artifact was stored after audit time",
                    code="ARTIFACT_STORE_CLOCK_ROLLBACK",
                )
            if previous_stored_at is not None and record.stored_at < previous_stored_at:
                raise RuntimeArtifactStoreError(
                    "artifact store time is not monotonic",
                    code="ARTIFACT_STORE_CLOCK_ROLLBACK",
                )
            artifact_observed = _utc_from_text(
                record.artifact.identity_dict()["observed_at_utc"], "observed_at_utc"
            )
            if record.stored_at < artifact_observed:
                raise RuntimeArtifactStoreError(
                    "artifact stored_at precedes observation",
                    code="ARTIFACT_STORE_CLOCK_ROLLBACK",
                )
            expected_previous = record.record_hash
            previous_stored_at = record.stored_at
            expected_files.add(_safe_record_path(self.record_root, record.record_file))
            records.append(record)
        actual_files = {
            path.resolve(strict=False)
            for path in self.record_root.rglob("*.json")
            if path.is_file() and not _is_link(path)
        }
        unexpected = actual_files - expected_files
        if allowed_orphan is not None:
            unexpected.discard(allowed_orphan.resolve(strict=False))
        if expected_files - actual_files or unexpected:
            raise RuntimeArtifactStoreError(
                "artifact record inventory mismatch",
                code="ARTIFACT_STORE_INVENTORY_MISMATCH",
            )
        return tuple(records)

    def append(self, artifact: RuntimeDecisionArtifact) -> RuntimeArtifactAppendResult:
        if type(artifact) is not RuntimeDecisionArtifact:
            raise RuntimeArtifactStoreError(
                "artifact must be RuntimeDecisionArtifact",
                code="ROW_ARTIFACT_TYPE_INVALID",
                failure_class=RuntimeArtifactFailureClass.ROW_PERMANENT,
            )
        try:
            artifact = RuntimeDecisionArtifact.from_json_bytes(artifact.to_json_bytes())
        except RuntimeEvidenceContractError as exc:
            raise RuntimeArtifactStoreError(
                "artifact contract is invalid",
                code="ROW_ARTIFACT_CONTRACT_INVALID",
                failure_class=RuntimeArtifactFailureClass.ROW_PERMANENT,
                safe_to_quarantine=True,
            ) from exc
        if artifact.identity_dict()["runtime_store_id"] != self.source_runtime_store_id:
            raise RuntimeArtifactStoreError(
                "artifact belongs to a different runtime evidence store",
                code="ROW_RUNTIME_STORE_ID_MISMATCH",
                failure_class=RuntimeArtifactFailureClass.ROW_PERMANENT,
                safe_to_quarantine=True,
            )
        created_path: Path | None = None
        created_raw: bytes | None = None
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = require_utc_clock_value(self.clock.now(), "stored_at")
                target = _safe_record_path(
                    self.record_root,
                    f"records/{artifact.artifact_id[:2]}/{artifact.artifact_id}.json",
                )
                records = self._validate_state(
                    connection,
                    observed,
                    allowed_orphan=target if target.exists() else None,
                )
                existing = next(
                    (record for record in records if record.artifact.artifact_id == artifact.artifact_id),
                    None,
                )
                if existing is not None:
                    if existing.artifact != artifact:
                        raise RuntimeArtifactStoreError(
                            "artifact identity conflict",
                            code="ARTIFACT_ID_COLLISION",
                        )
                    connection.rollback()
                    return RuntimeArtifactAppendResult(
                        RuntimeArtifactAppendDisposition.IDEMPOTENT, existing
                    )
                if records and observed < records[-1].stored_at:
                    raise RuntimeArtifactStoreError(
                        "artifact store clock moved backward",
                        code="ARTIFACT_STORE_CLOCK_ROLLBACK",
                    )
                expected_previous = _ZERO_HASH if not records else records[-1].record_hash
                if target.exists():
                    if not target.is_file() or _is_link(target):
                        raise RuntimeArtifactStoreError("orphan artifact record is invalid")
                    raw = target.read_bytes()
                    record = RuntimeArtifactRecord.from_json_bytes(raw)
                    if (
                        record.append_order != len(records) + 1
                        or record.store_id != self.store_id
                        or record.artifact != artifact
                        or record.previous_record_hash != expected_previous
                        or record.record_file
                        != f"records/{artifact.artifact_id[:2]}/{artifact.artifact_id}.json"
                        or record.stored_at > observed
                    ):
                        raise RuntimeArtifactStoreError(
                            "orphan artifact record identity mismatch",
                            code="ARTIFACT_ID_COLLISION",
                        )
                else:
                    record = RuntimeArtifactRecord.create(
                        append_order=len(records) + 1,
                        store_id=self.store_id,
                        artifact=artifact,
                        stored_at=observed,
                        previous_record_hash=expected_previous,
                    )
                    raw = record.to_json_bytes()
                    if _atomic_write_no_overwrite(target, raw):
                        created_path = target
                        created_raw = raw
                identity = artifact.identity_dict()
                connection.execute(
                    "INSERT INTO runtime_artifacts("
                    "append_order,artifact_id,transition_event_id,runtime_signal_id,"
                    "stored_at,previous_record_hash,record_hash,record_file,"
                    "record_file_sha256,payload_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.append_order,
                        artifact.artifact_id,
                        identity["transition_event_id"],
                        identity["runtime_signal_id"],
                        _utc_text(record.stored_at, "stored_at"),
                        record.previous_record_hash,
                        record.record_hash,
                        record.record_file,
                        record.record_file_sha256,
                        _sha256(artifact.to_json_bytes()),
                    ),
                )
                self._validate_state(connection, observed)
                connection.commit()
                return RuntimeArtifactAppendResult(
                    RuntimeArtifactAppendDisposition.APPENDED, record
                )
            except Exception:
                connection.rollback()
                if (
                    created_path is not None
                    and created_raw is not None
                    and created_path.is_file()
                    and not _is_link(created_path)
                    and created_path.read_bytes() == created_raw
                ):
                    existing_row = connection.execute(
                        "SELECT 1 FROM runtime_artifacts WHERE artifact_id=?",
                        (artifact.artifact_id,),
                    ).fetchone()
                    if existing_row is None:
                        created_path.unlink()
                raise

    def audit(self) -> RuntimeArtifactAuditReport:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                audited_at = require_utc_clock_value(self.clock.now(), "audited_at")
                records = self._validate_state(connection, audited_at)
                connection.rollback()
            except Exception:
                connection.rollback()
                raise
        hashes = tuple(record.record_hash for record in records)
        return RuntimeArtifactAuditReport(
            store_id=self.store_id,
            source_runtime_store_id=self.source_runtime_store_id,
            audited_at=audited_at,
            record_hashes=hashes,
            artifact_ids=tuple(record.artifact.artifact_id for record in records),
            first_record_hash=_ZERO_HASH if not hashes else hashes[0],
            last_record_hash=_ZERO_HASH if not hashes else hashes[-1],
        )

    def get(self, artifact_id: str) -> RuntimeArtifactRecord:
        if type(artifact_id) is not str or len(artifact_id) != 64:
            raise RuntimeArtifactStoreError("artifact_id is invalid")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = require_utc_clock_value(self.clock.now(), "audited_at")
                records = self._validate_state(connection, observed)
                connection.rollback()
            except Exception:
                connection.rollback()
                raise
        record = next(
            (item for item in records if item.artifact.artifact_id == artifact_id), None
        )
        if record is None:
            raise RuntimeArtifactStoreError("runtime artifact does not exist")
        return record


__all__ = [
    "DEFAULT_RUNTIME_ARTIFACT_CATALOG",
    "DEFAULT_RUNTIME_ARTIFACT_ROOT",
    "RUNTIME_ARTIFACT_AUDIT_SCHEMA",
    "RUNTIME_ARTIFACT_RECORD_SCHEMA",
    "RUNTIME_ARTIFACT_STORE_SCHEMA",
    "RuntimeArtifactAppendDisposition",
    "RuntimeArtifactAppendResult",
    "RuntimeArtifactAuditReport",
    "RuntimeArtifactFailureClass",
    "RuntimeArtifactRecord",
    "RuntimeArtifactStore",
    "RuntimeArtifactStoreError",
]
