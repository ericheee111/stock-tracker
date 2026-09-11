"""B1a-c physical byte ledger; deliberately not a semantic/coverage authority.

Only explicit initialize() creates a store. Opening requires its expected ID.
Event and Transport bytes share one publication order. A durable intent precedes
file publication so recovery never guesses the owner of an orphan. Pure market
contracts and the Stage 3F store remain unchanged. No production default path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

STORE_SCHEMA = "b1-physical-store-v1"
RECORD_SCHEMA = "b1-physical-record-v1"
SCOPE = "PHYSICAL_BYTES_ONLY"
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGE = 256
MAX_PAGE_BYTES = 8 * 1024 * 1024
ZERO = "0" * 64
APP_ID = 0x53544231


class PhysicalStoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StreamKind(str, Enum):
    EVENT = "EVENT"
    TRANSPORT = "TRANSPORT"


def _fail(code: str) -> NoReturn:
    raise PhysicalStoreError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PhysicalStoreError("INVALID_SHA256")
    return value


def _integer(value: object, minimum: int, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PhysicalStoreError("INVALID_INTEGER")
    return value


def _time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PhysicalStoreError("AWARE_TIME_REQUIRED")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise PhysicalStoreError("INVALID_TIME_ENCODING")
    try:
        result = _time(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError) as exc:
        raise PhysicalStoreError("INVALID_TIME_ENCODING") from exc
    if _stamp(result) != value:
        _fail("NON_CANONICAL_TIME")
    return result


def _text(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise PhysicalStoreError("INVALID_IDENTIFIER")
    if re.fullmatch(r"[A-Za-z0-9_./:-]+", value) is None:
        _fail("INVALID_IDENTIFIER")
    return value


def _json_shape(value: Any, depth: int = 0) -> None:
    if depth > 32:
        _fail("JSON_DEPTH_LIMIT")
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is list:
        for item in value:
            _json_shape(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_shape(item, depth + 1)
        return
    _fail("UNSUPPORTED_JSON_VALUE")  # Prices/decimal values are strings, never floats.


def canonical_bytes(value: Any) -> bytes:
    try:
        _json_shape(value)
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PhysicalStoreError("INVALID_CANONICAL_JSON") from exc
    if len(data) > MAX_BYTES:
        _fail("RECORD_SIZE_LIMIT")
    return data


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _decode(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > MAX_BYTES:
        _fail("RECORD_SIZE_LIMIT")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
        if type(value) is not dict or canonical_bytes(value) != data:
            _fail("NON_CANONICAL_JSON")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PhysicalStoreError("INVALID_CANONICAL_JSON") from exc


@dataclass(frozen=True)
class EvidenceInput:
    stream: StreamKind
    record_key: str
    partition_key: str
    source_schema: str
    observed_at: datetime
    payload_json: str

    def __post_init__(self) -> None:
        if type(self.stream) is not StreamKind:
            _fail("INVALID_STREAM")
        _hash(self.record_key)
        _text(self.partition_key)
        _text(self.source_schema)
        _time(self.observed_at)
        if type(self.payload_json) is not str or len(self.payload_json) > MAX_BYTES:
            _fail("INVALID_PAYLOAD")
        try:
            _decode(self.payload_json.encode("utf-8"))
        except UnicodeError as exc:
            raise PhysicalStoreError("INVALID_UTF8_PAYLOAD") from exc

    def document(self) -> dict[str, Any]:
        self.__post_init__()
        return {"stream": self.stream.value, "record_key": self.record_key,
                "partition_key": self.partition_key, "source_schema": self.source_schema,
                "observed_at": _stamp(self.observed_at),
                "payload": _decode(self.payload_json.encode("utf-8"))}

    @property
    def request_id(self) -> str:
        return _sha(canonical_bytes(self.document()))

    @classmethod
    def from_document(cls, value: dict[str, Any]) -> EvidenceInput:
        if type(value) is not dict or set(value) != {"stream", "record_key", "partition_key", "source_schema", "observed_at", "payload"}:
            _fail("INPUT_SCHEMA_MISMATCH")
        try:
            return cls(StreamKind(value["stream"]), value["record_key"], value["partition_key"],
                       value["source_schema"], _parse_time(value["observed_at"]),
                       canonical_bytes(value["payload"]).decode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise PhysicalStoreError("INPUT_SCHEMA_MISMATCH") from exc


@dataclass(frozen=True)
class PhysicalRecord:
    content: bytes
    store_id: str = field(init=False)
    append_order: int = field(init=False)
    input: EvidenceInput = field(init=False)
    recorded_at: datetime = field(init=False)
    previous_global_hash: str = field(init=False)
    previous_partition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        names = ("store_id", "append_order", "input", "recorded_at",
                 "previous_global_hash", "previous_partition_hash")
        for name, value in zip(names, self._decode_fields(self.content), strict=True):
            object.__setattr__(self, name, value)

    @classmethod
    def decode(cls, data: bytes) -> PhysicalRecord:
        return cls(content=data)

    @property
    def content_hash(self) -> str:
        return _sha(self.content)

    @property
    def storage_key(self) -> str:
        return f"records/{self.append_order:020d}-{self.content_hash}.json"

    @property
    def record_id(self) -> str:
        return _sha(canonical_bytes({"schema": "b1-physical-record-identity-v1",
                    "store_id": self.store_id, "append_order": self.append_order,
                    "request_id": self.input.request_id, "storage_key": self.storage_key,
                    "content_hash": self.content_hash, "file_sha256": self.content_hash}))

    @staticmethod
    def _decode_fields(data: bytes) -> tuple[Any, ...]:
        value = _decode(data)
        fields = {"schema", "store_id", "append_order", "input", "recorded_at",
                  "previous_global_hash", "previous_partition_hash"}
        if set(value) != fields or value["schema"] != RECORD_SCHEMA:
            _fail("RECORD_SCHEMA_MISMATCH")
        if type(value["input"]) is not dict:
            _fail("INPUT_SCHEMA_MISMATCH")
        inp = EvidenceInput.from_document(value["input"])
        recorded = _parse_time(value["recorded_at"])
        if recorded < _time(inp.observed_at):
            _fail("FUTURE_OBSERVATION")
        return (_hash(value["store_id"]), _integer(value["append_order"], 1), inp,
                recorded, _hash(value["previous_global_hash"]),
                _hash(value["previous_partition_hash"]))


@dataclass(frozen=True)
class PhysicalSnapshot:
    store_id: str
    high_water: int
    global_head: str

    def __post_init__(self) -> None:
        _hash(self.store_id)
        _integer(self.high_water, 0)
        _hash(self.global_head)
        if self.high_water == 0 and self.global_head != ZERO:
            _fail("INVALID_EMPTY_SNAPSHOT")

    @property
    def snapshot_id(self) -> str:
        return _sha(canonical_bytes({"schema": "b1-physical-snapshot-v1", "store_id": self.store_id,
                    "high_water": self.high_water, "global_head": self.global_head}))


@dataclass(frozen=True)
class PhysicalAudit:
    snapshot: PhysicalSnapshot
    audited_at: datetime
    pending_order: int | None
    file_count: int
    byte_count: int

    @property
    def ready(self) -> bool:
        return self.pending_order is None

    def to_dict(self) -> dict[str, Any]:
        return {"scope": SCOPE, "store_id": self.snapshot.store_id,
                "snapshot_id": self.snapshot.snapshot_id, "high_water": self.snapshot.high_water,
                "global_head": self.snapshot.global_head, "audited_at": _stamp(self.audited_at),
                "pending_order": self.pending_order, "file_count": self.file_count,
                "byte_count": self.byte_count, "ready": self.ready,
                "market_coverage_verified": False, "trusted_admission": False,
                "auto_trade": False, "power_loss_certified": False}


@dataclass(frozen=True)
class PhysicalPage:
    snapshot: PhysicalSnapshot
    after_order: int
    records: tuple[PhysicalRecord, ...]
    has_more: bool


SQL = (
    "CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL UNIQUE, schema_id TEXT NOT NULL, created_at TEXT NOT NULL, durability TEXT NOT NULL) STRICT",
    "CREATE TABLE intents (append_order INTEGER PRIMARY KEY CHECK(append_order>0), request_id TEXT NOT NULL UNIQUE, stream TEXT NOT NULL CHECK(stream IN ('EVENT','TRANSPORT')), record_key TEXT NOT NULL, partition_key TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, storage_key TEXT NOT NULL UNIQUE, record_id TEXT NOT NULL UNIQUE, recorded_at TEXT NOT NULL, content BLOB NOT NULL CHECK(length(content)<=2097152), UNIQUE(stream,record_key)) STRICT",
    "CREATE TABLE publications (append_order INTEGER PRIMARY KEY REFERENCES intents(append_order) ON DELETE RESTRICT, published_at TEXT NOT NULL) STRICT",
    "CREATE INDEX intents_partition ON intents(stream,partition_key,append_order)",
    "CREATE TRIGGER intents_order BEFORE INSERT ON intents BEGIN SELECT CASE WHEN NEW.append_order != COALESCE((SELECT MAX(append_order) FROM intents),0)+1 THEN RAISE(ABORT,'NONCONTIGUOUS_INTENT') END; SELECT CASE WHEN EXISTS(SELECT 1 FROM intents i LEFT JOIN publications p USING(append_order) WHERE p.append_order IS NULL) THEN RAISE(ABORT,'PENDING_INTENT') END; END",
    "CREATE TRIGGER publications_order BEFORE INSERT ON publications BEGIN SELECT CASE WHEN NEW.append_order != COALESCE((SELECT MAX(append_order) FROM publications),0)+1 THEN RAISE(ABORT,'NONCONTIGUOUS_PUBLICATION') END; END",
) + tuple(f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END"
          for table in ("meta", "intents", "publications") for op in ("UPDATE", "DELETE"))


def _schema_inventory(conn: sqlite3.Connection) -> tuple[Any, ...]:
    objects = tuple(tuple(r) for r in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))
    tables = tuple((name, tuple(tuple(r) for r in conn.execute(f"PRAGMA table_xinfo('{name}')")),
                    tuple(tuple(r) for r in conn.execute(f"PRAGMA foreign_key_list('{name}')")))
                   for name in ("meta", "intents", "publications"))
    indexes = tuple((row[1], tuple(tuple(r) for r in conn.execute(f"PRAGMA index_xinfo('{row[1]}')")))
                    for row in objects if row[0] == "index")
    return objects, tables, indexes


def _expected_schema() -> tuple[Any, ...]:
    conn = sqlite3.connect(":memory:")
    try:
        for statement in SQL:
            conn.execute(statement)
        return _schema_inventory(conn)
    finally:
        conn.close()


EXPECTED_SCHEMA = _expected_schema()


def _lstat(path: Path, *, directory: bool, allow_link_count: int = 1) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        _fail("LINK_OR_REPARSE_POINT")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            _fail("DIRECTORY_REQUIRED")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink > allow_link_count:
        _fail("UNSAFE_REGULAR_FILE")
    return info


def _root_path(root: str | Path) -> Path:
    path = Path(os.path.abspath(root))
    for parent in reversed(path.parents):
        _lstat(parent, directory=True)
    return path


def _sync_directory(path: Path) -> None:
    # Python has no portable Windows directory flush. No power-loss claim is made.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class PhysicalEvidenceStore:
    """Private local root; expected Store ID and physical re-audit on every use."""

    def __init__(self, root: str | Path, expected_store_id: str, *,
                 clock: Callable[[], datetime] | None = None,
                 fault: Callable[[str], None] | None = None) -> None:
        self.root = _root_path(root)
        self.store_id = _hash(expected_store_id)
        self.catalog = self.root / "catalog.sqlite3"
        self.records = self.root / "records"
        self.staging = self.root / "staging"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fault = fault or (lambda _: None)
        self._identities: dict[Path, tuple[int, int]] = {}
        try:
            for path in (self.root, self.records, self.staging, self.catalog):
                info = _lstat(path, directory=path != self.catalog)
                self._identities[path] = (info.st_dev, info.st_ino)
        except OSError as exc:
            raise PhysicalStoreError("STORE_NOT_INITIALIZED") from exc

    @classmethod
    def initialize(cls, root: str | Path, *, clock: Callable[[], datetime] | None = None) -> PhysicalEvidenceStore:
        now = _time((clock or (lambda: datetime.now(UTC)))())
        path = _root_path(root)
        if path.exists() or path.is_symlink():
            _fail("INITIALIZATION_REQUIRES_NEW_ROOT")
        conn: sqlite3.Connection | None = None
        temp = path / (".initializing-" + secrets.token_hex(12))
        try:
            path.mkdir(mode=0o700)
            (path / "records").mkdir(mode=0o700)
            (path / "staging").mkdir(mode=0o700)
            store_id = _sha(secrets.token_bytes(32))
            conn = sqlite3.connect(temp)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in SQL:
                conn.execute(statement)
            conn.execute(f"PRAGMA application_id={APP_ID}")
            conn.execute("PRAGMA user_version=1")
            conn.execute("INSERT INTO meta VALUES(1,?,?,?,?)",
                         (store_id, STORE_SCHEMA, _stamp(now), "FILE_FSYNC_PLATFORM_NAMESPACE"))
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                _fail("INITIALIZATION_INTEGRITY_FAILED")
            conn.close()
            conn = None
            with temp.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.link(temp, path / "catalog.sqlite3")
            temp.unlink()
            _sync_directory(path)
            _sync_directory(path.parent)
            result = cls(path, store_id, clock=clock)
            result.audit()
            return result
        except (OSError, sqlite3.Error) as exc:
            # Keep incomplete root for explicit operator inspection; never reset it.
            raise PhysicalStoreError("INITIALIZATION_FAILED") from exc
        finally:
            if conn is not None:
                conn.close()

    def _paths(self) -> None:
        _root_path(self.root)
        for path, expected in self._identities.items():
            info = _lstat(path, directory=path != self.catalog)
            if (info.st_dev, info.st_ino) != expected:
                _fail("STORE_PATH_REPLACED")
        allowed = {"records", "staging", "catalog.sqlite3", "catalog.sqlite3-journal"}
        journal = self.root / "catalog.sqlite3-journal"
        if journal.exists() or journal.is_symlink():
            _lstat(journal, directory=False)
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.name not in allowed:
                    _fail("UNEXPECTED_ROOT_ENTRY")

    def _schema(self, conn: sqlite3.Connection) -> datetime:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID or conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            _fail("SCHEMA_VERSION_MISMATCH")
        if _schema_inventory(conn) != EXPECTED_SCHEMA:
            _fail("SCHEMA_INVENTORY_MISMATCH")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1 or conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            _fail("FOREIGN_KEY_INTEGRITY")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete" or conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
            _fail("UNSUPPORTED_SQLITE_PROFILE")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            _fail("SQLITE_INTEGRITY")
        rows = conn.execute("SELECT * FROM meta").fetchall()
        if len(rows) != 1 or tuple(rows[0])[:3] != (1, self.store_id, STORE_SCHEMA) or rows[0][4] != "FILE_FSYNC_PLATFORM_NAMESPACE":
            _fail("STORE_IDENTITY_MISMATCH")
        return _parse_time(rows[0][3])

    @contextmanager
    def _session(self, *, commit: bool) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            self._paths()
            conn = sqlite3.connect(self.catalog.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            self._paths()
            self._schema(conn)
            yield conn
            self._paths()
            if commit:
                try:
                    conn.commit()
                except sqlite3.Error as exc:
                    raise PhysicalStoreError("COMMIT_OUTCOME_UNKNOWN_RETRY_EXACT_INPUT") from exc
            else:
                conn.rollback()
        except (OSError, sqlite3.Error) as exc:
            raise PhysicalStoreError("PHYSICAL_STORE_IO_OR_SQL_ERROR") from exc
        finally:
            if conn is not None:
                conn.close()  # An uncommitted transaction rolls back; published files are kept.

    def _record(self, row: sqlite3.Row) -> PhysicalRecord:
        record = PhysicalRecord.decode(row["content"])
        expected = (record.append_order, record.input.request_id, record.input.stream.value,
                    record.input.record_key, record.input.partition_key, record.content_hash,
                    record.storage_key, record.record_id, _stamp(record.recorded_at), record.content)
        actual = tuple(row[k] for k in ("append_order", "request_id", "stream", "record_key", "partition_key",
                                      "content_hash", "storage_key", "record_id", "recorded_at", "content"))
        if actual != expected or record.store_id != self.store_id:
            _fail("CATALOG_RECORD_DISAGREEMENT")
        return record

    def _stage_path(self, record: PhysicalRecord) -> Path:
        return self.staging / (Path(record.storage_key).name + ".tmp")

    def _file_bytes(self, path: Path, *, links: int = 1) -> bytes:
        info = _lstat(path, directory=False, allow_link_count=links)
        if info.st_size > MAX_BYTES:
            _fail("FILE_SIZE_LIMIT")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                _fail("FILE_REPLACED_DURING_READ")
            data = handle.read(MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = _lstat(path, directory=False, allow_link_count=links)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) or len(data) != info.st_size:
            _fail("FILE_CHANGED_DURING_READ")
        return data

    def _audit(self, conn: sqlite3.Connection) -> PhysicalAudit:
        created = self._schema(conn)
        order = committed = files = byte_count = 0
        previous = head = ZERO
        latest_time = created
        pending: PhysicalRecord | None = None
        for order, row in enumerate(conn.execute("SELECT i.*,p.published_at FROM intents i LEFT JOIN publications p USING(append_order) ORDER BY i.append_order"), start=1):
            record = self._record(row)
            if record.append_order != order or record.previous_global_hash != previous or record.recorded_at < latest_time:
                _fail("GLOBAL_CHAIN_OR_CLOCK_MISMATCH")
            pred = conn.execute("SELECT content_hash FROM intents WHERE stream=? AND partition_key=? AND append_order<? ORDER BY append_order DESC LIMIT 1",
                                (record.input.stream.value, record.input.partition_key, order)).fetchone()
            if record.previous_partition_hash != (pred[0] if pred else ZERO):
                _fail("PARTITION_CHAIN_MISMATCH")
            previous = record.content_hash
            latest_time = record.recorded_at
            final = self.root / record.storage_key
            stage = self._stage_path(record)
            if row["published_at"] is None:
                if pending is not None:
                    _fail("MULTIPLE_PENDING_INTENTS")
                pending = record
                if final.exists() and self._file_bytes(final, links=2 if stage.exists() and os.path.samefile(stage, final) else 1) != record.content:
                    _fail("PENDING_FILE_CONFLICT")
            else:
                if pending is not None or order != committed + 1:
                    _fail("PUBLICATION_PREFIX_GAP")
                published = _parse_time(row["published_at"])
                if published < latest_time:
                    _fail("PUBLICATION_CLOCK_MISMATCH")
                latest_time = published
                if self._file_bytes(final) != record.content:
                    _fail("COMMITTED_FILE_MISMATCH")
                if stage.exists():
                    _fail("COMMITTED_STAGING_RESIDUE")
                committed += 1
                head = record.content_hash
        with os.scandir(self.records) as entries:
            for entry in entries:
                row = conn.execute("SELECT append_order FROM intents WHERE storage_key=?", ("records/" + entry.name,)).fetchone()
                if row is None:
                    _fail("UNKNOWN_RECORD_FILE")
                _lstat(Path(entry.path), directory=False, allow_link_count=2 if pending is not None and row[0] == pending.append_order else 1)
                files += 1
                byte_count += entry.stat(follow_symlinks=False).st_size
        with os.scandir(self.staging) as entries:
            for entry in entries:
                if pending is None or Path(entry.path) != self._stage_path(pending):
                    _fail("UNKNOWN_STAGING_FILE")
                _lstat(Path(entry.path), directory=False, allow_link_count=2)
        now = _time(self.clock())
        if now < latest_time:
            _fail("STORE_CLOCK_ROLLBACK")
        return PhysicalAudit(PhysicalSnapshot(self.store_id, committed, head), now,
                             pending.append_order if pending else None, files, byte_count)

    def audit(self) -> PhysicalAudit:
        with self._session(commit=False) as conn:
            return self._audit(conn)

    def _publish(self, record: PhysicalRecord) -> None:
        final = self.root / record.storage_key
        stage = self._stage_path(record)
        if final.exists():
            linked = stage.exists() and os.path.samefile(final, stage)
            if self._file_bytes(final, links=2 if linked else 1) != record.content:
                _fail("IMMUTABLE_FILE_CONFLICT")
            if stage.exists():
                _lstat(stage, directory=False, allow_link_count=2 if linked else 1)
                stage.unlink()  # Only the exact pending intent's transient name.
        else:
            if stage.exists():
                _lstat(stage, directory=False)
                stage.unlink()  # A killed writer's partial private staging file, not evidence.
            with stage.open("xb") as handle:
                handle.write(record.content)
                handle.flush()
                os.fsync(handle.fileno())
            self.fault("temp_synced")
            try:
                os.link(stage, final)
            except FileExistsError:
                if self._file_bytes(final) != record.content:
                    _fail("IMMUTABLE_FILE_CONFLICT")
            except OSError as exc:
                raise PhysicalStoreError("NO_OVERWRITE_PUBLICATION_UNAVAILABLE") from exc
            self.fault("linked_before_staging_cleanup")
            _lstat(stage, directory=False, allow_link_count=2)
            stage.unlink()
        if self._file_bytes(final) != record.content:
            _fail("PUBLISHED_FILE_MISMATCH")
        _sync_directory(self.records)
        _sync_directory(self.staging)
        self.fault("file_published")

    def _finish_pending(self, conn: sqlite3.Connection, row: sqlite3.Row) -> PhysicalRecord:
        record = self._record(row)
        self._publish(record)
        published = _time(self.clock())
        previous = conn.execute("SELECT published_at FROM publications ORDER BY append_order DESC LIMIT 1").fetchone()
        if published < record.recorded_at or (previous and published < _parse_time(previous[0])):
            _fail("STORE_CLOCK_ROLLBACK")
        conn.execute("INSERT INTO publications VALUES(?,?)", (record.append_order, _stamp(published)))
        self.fault("publication_before_commit")
        self._audit(conn)
        return record

    def append(self, value: EvidenceInput) -> PhysicalRecord:
        if type(value) is not EvidenceInput:
            _fail("EXACT_INPUT_REQUIRED")
        document = value.document()
        req_id = value.request_id
        with self._session(commit=True) as conn:
            audit = self._audit(conn)
            old = conn.execute("SELECT i.*,p.published_at FROM intents i LEFT JOIN publications p USING(append_order) WHERE stream=? AND record_key=?",
                               (value.stream.value, value.record_key)).fetchone()
            if old:
                if old["request_id"] != req_id:
                    _fail("IDEMPOTENCY_CONFLICT")
                if old["published_at"] is not None:
                    return self._record(old)
            else:
                if not audit.ready:
                    _fail("RECOVERY_REQUIRED")
                now = _time(self.clock())
                if now < audit.audited_at or now < _time(value.observed_at):
                    _fail("FUTURE_OBSERVATION_OR_CLOCK_ROLLBACK")
                pred = conn.execute("SELECT content_hash FROM intents WHERE stream=? AND partition_key=? ORDER BY append_order DESC LIMIT 1",
                                    (value.stream.value, value.partition_key)).fetchone()
                data = canonical_bytes({"schema": RECORD_SCHEMA, "store_id": self.store_id,
                    "append_order": audit.snapshot.high_water + 1, "input": document,
                    "recorded_at": _stamp(now), "previous_global_hash": audit.snapshot.global_head,
                    "previous_partition_hash": pred[0] if pred else ZERO})
                rec = PhysicalRecord.decode(data)
                conn.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (rec.append_order, req_id, value.stream.value, value.record_key,
                              value.partition_key, rec.content_hash, rec.storage_key, rec.record_id,
                              _stamp(rec.recorded_at), rec.content))
                self.fault("intent_before_commit")
        self.fault("intent_committed")
        with self._session(commit=True) as conn:
            self._audit(conn)
            row = conn.execute("SELECT i.*,p.published_at FROM intents i LEFT JOIN publications p USING(append_order) WHERE request_id=?", (req_id,)).fetchone()
            if row is None:
                _fail("DURABLE_INTENT_DISAPPEARED")
            result = self._record(row) if row["published_at"] is not None else self._finish_pending(conn, row)
        self.fault("catalog_committed")
        return result

    def recover(self) -> PhysicalRecord | None:
        with self._session(commit=True) as conn:
            self._audit(conn)
            row = conn.execute("SELECT i.* FROM intents i LEFT JOIN publications p USING(append_order) WHERE p.append_order IS NULL").fetchone()
            result = self._finish_pending(conn, row) if row is not None else None
        self.fault("catalog_committed")
        return result

    def snapshot(self) -> PhysicalSnapshot:
        audit = self.audit()
        if not audit.ready:
            _fail("RECOVERY_REQUIRED")
        return audit.snapshot

    def read_page(self, snapshot: PhysicalSnapshot, *, after_order: int = 0,
                  limit: int = 128) -> PhysicalPage:
        if type(snapshot) is not PhysicalSnapshot:
            _fail("EXACT_SNAPSHOT_REQUIRED")
        snapshot.__post_init__()
        _integer(after_order, 0, snapshot.high_water)
        _integer(limit, 1, MAX_PAGE)
        with self._session(commit=False) as conn:
            audit = self._audit(conn)
            if not audit.ready:
                _fail("RECOVERY_REQUIRED")
            if snapshot.store_id != self.store_id or snapshot.high_water > audit.snapshot.high_water:
                _fail("SNAPSHOT_STORE_OR_HIGH_WATER_MISMATCH")
            row = conn.execute("SELECT content_hash FROM intents WHERE append_order=?", (snapshot.high_water,)).fetchone()
            if snapshot.global_head != (row[0] if row else ZERO):
                _fail("SNAPSHOT_HEAD_MISMATCH")
            rows = conn.execute("SELECT i.* FROM intents i JOIN publications p USING(append_order) WHERE i.append_order>? AND i.append_order<=? ORDER BY i.append_order LIMIT ?",
                                (after_order, snapshot.high_water, limit + 1))
            records: list[PhysicalRecord] = []
            byte_count = 0
            has_more = False
            for row in rows:
                record = self._record(row)
                if len(records) == limit or byte_count + len(record.content) > MAX_PAGE_BYTES:
                    has_more = True
                    break
                records.append(record)
                byte_count += len(record.content)
            return PhysicalPage(snapshot, after_order, tuple(records), has_more)
