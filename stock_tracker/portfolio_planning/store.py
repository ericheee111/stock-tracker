"""Explicitly initialized, bounded, append-only manual planning command log.

This is not an Authority Store: local mutation with full access is outside the
trust claim. Schema, hash chain and deterministic replay detect inconsistency.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import (
    MAX_COMMANDS,
    PlanningError,
    canonical,
    clock,
    digest,
    empty_book,
    fields,
    identifier,
    reduce_command,
    require,
    when,
)

DDL = (
    "CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE commands (sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL)",
    "CREATE TRIGGER metadata_no_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END",
    "CREATE TRIGGER metadata_no_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END",
    "CREATE TRIGGER commands_no_update BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'immutable command'); END",
    "CREATE TRIGGER commands_no_delete BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'immutable command'); END",
)
STORE_SCHEMA = "manual-planning-sqlite-v1"


def _schema(conn: sqlite3.Connection) -> tuple[Any, ...]:
    return tuple(tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))


def _expected_schema() -> tuple[Any, ...]:
    with sqlite3.connect(":memory:") as conn:
        for sql in DDL:
            conn.execute(sql)
        result = _schema(conn)
    conn.close()
    return result


EXPECTED_SCHEMA = _expected_schema()


def safe_path(path: Path, *, must_exist: bool) -> Path:
    require(path.is_absolute(), "ABSOLUTE_PATH_REQUIRED", "计划库路径必须为绝对路径")
    require(path.name.lower() not in ("stock_tracker.db", "stock_tracker.db-wal", "stock_tracker.db-shm"),
            "PROTECTED_DATABASE", "不可复用生产数据库")
    require(".." not in path.parts, "UNSAFE_PATH", "路径不允许父目录跳转")
    for part in (path, *path.parents):
        require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(),
                "UNSAFE_PATH", "计划库及父目录不能是链接")
    require(path.parent.is_dir(), "PARENT_MISSING", "请先显式创建本地私有目录")
    if must_exist:
        require(path.is_file(), "PLANNER_NOT_INITIALIZED", "计划库尚未显式初始化", 503)
        require(path.stat().st_nlink == 1, "UNSAFE_PATH", "计划库不能是硬链接")
    for suffix in ("-journal", "-wal", "-shm"):
        sibling = Path(str(path) + suffix)
        require(not sibling.is_symlink() and not getattr(sibling, "is_junction", lambda: False)(), "UNSAFE_PATH", "SQLite侧文件不允许链接")
        if sibling.exists():
            require(sibling.is_file() and sibling.stat().st_nlink == 1, "UNSAFE_PATH", "SQLite侧文件不安全")
    return path


def _load(text: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "CORRUPT_LOG", "记录包含重复字段", 503)
            result[key] = value
        return result
    def reject(value):
        raise PlanningError("CORRUPT_LOG", "记录包含非有限数值", status=503)
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
        require(type(value) is dict and canonical(value) == text, "CORRUPT_LOG", "记录不是规范对象", 503)
        return value
    except (ValueError, TypeError) as exc:
        if isinstance(exc, PlanningError):
            raise
        raise PlanningError("CORRUPT_LOG", "记录解析失败", status=503) from exc


class PlanningStore:
    def __init__(self, path: Path, expected_store_id: str, *, fault: Callable[[str], None] | None = None):
        self.path = safe_path(Path(path), must_exist=True)
        self.store_id = identifier(expected_store_id, "expected_store_id")
        self._file_id = (self.path.stat().st_dev, self.path.stat().st_ino)
        self._fault = fault or (lambda _: None)
        self.read()

    @classmethod
    def create(cls, path: Path) -> PlanningStore:
        target = safe_path(Path(path), must_exist=False)
        require(not target.exists(), "STORE_EXISTS", "拒绝覆盖现有路径", 409)
        store_id = uuid.uuid4().hex
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.init")
        try:
            with tmp.open("xb"):
                pass
            conn = sqlite3.connect(tmp, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                for sql in DDL:
                    conn.execute(sql)
                conn.executemany("INSERT INTO metadata VALUES (?,?)", (("schema", STORE_SCHEMA), ("store_id", store_id)))
                conn.execute("PRAGMA user_version=1")
                conn.commit()
                require(_schema(conn) == EXPECTED_SCHEMA and conn.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                        "SCHEMA_MISMATCH", "初始化验证失败", 503)
            finally:
                conn.close()
            with tmp.open("r+b") as f:
                os.fsync(f.fileno())
            os.link(tmp, target)  # Atomic no-overwrite publication; no replace fallback.
        except OSError as exc:
            raise PlanningError("INITIALIZATION_FAILED", "无法不可覆盖地发布计划库", status=503) from exc
        finally:
            if tmp.exists():
                tmp.unlink()
        return cls(target, store_id)

    def _check_identity(self) -> None:
        safe_path(self.path, must_exist=True)
        stat = self.path.stat()
        require((stat.st_dev, stat.st_ino) == self._file_id,
                "STORE_REPLACED", "计划库文件身份已变化", 503)

    @contextmanager
    def _connection(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        self._check_identity()
        conn = None
        try:
            conn = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check_identity()
            require(_schema(conn) == EXPECTED_SCHEMA, "SCHEMA_MISMATCH", "计划库Schema不一致", 503)
            require(conn.execute("PRAGMA user_version").fetchone()[0] == 1, "SCHEMA_MISMATCH", "不支持的版本", 503)
            require(conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete", "SCHEMA_MISMATCH", "不支持的journal模式", 503)
            require(dict(conn.execute("SELECT name,value FROM metadata")) == {"schema": STORE_SCHEMA, "store_id": self.store_id},
                    "STORE_IDENTITY_MISMATCH", "Store ID不一致", 503)
            require(conn.execute("PRAGMA quick_check").fetchall() == [("ok",)], "CORRUPT_LOG", "SQLite完整性检查失败", 503)
            yield conn
            self._check_identity()
        except sqlite3.Error as exc:
            raise PlanningError("PLANNING_STORAGE_ERROR", "计划库暂不可用；未确认写入请以相同命令ID重试", status=503) from exc
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _replay(self, conn: sqlite3.Connection) -> tuple[dict[str, Any], str, datetime | None]:
        book, previous, last_time = empty_book(), "0" * 64, None
        require(conn.execute("SELECT count(*) FROM commands").fetchone()[0] <= MAX_COMMANDS, "CAPACITY_LIMIT", "超出受支持日志容量", 503)
        for row in conn.execute("SELECT sequence,command_id,request_hash,record_json,record_hash FROM commands ORDER BY sequence"):
            sequence, cid, request_hash, raw, record_hash = row
            require(len(raw.encode("utf-8")) <= 128 * 1024, "CORRUPT_LOG", "记录超限", 503)
            record = _load(raw)
            fields(record, {"schema", "store_id", "sequence", "previous_hash", "recorded_at", "command", "parents"})
            require(record["schema"] == STORE_SCHEMA and record["store_id"] == self.store_id
                    and sequence == book["revision"] + 1 and record["sequence"] == sequence
                    and record["previous_hash"] == previous and digest(record) == record_hash,
                    "CORRUPT_LOG", "命令链不一致", 503)
            command = record["command"]
            require(type(command) is dict and command.get("command_id") == cid and digest(command) == request_hash,
                    "CORRUPT_LOG", "命令身份不一致", 503)
            timestamp = when(record["recorded_at"], "recorded_at")
            require(last_time is None or timestamp >= last_time, "CLOCK_ROLLBACK", "历史时钟回退", 503)
            book = reduce_command(book, command, record["parents"], timestamp)
            previous, last_time = record_hash, timestamp
        return book, previous, last_time

    def read(self) -> dict[str, Any]:
        with self._connection(write=False) as conn:
            book, _, _ = self._replay(conn)
            return book

    def apply(self, command: dict[str, Any], parents: dict[str, Any], now: datetime) -> dict[str, Any]:
        timestamp = clock(now)
        fields(command, {"command_id", "expected_revision", "kind", "data"})
        cid = identifier(command["command_id"], "command_id")
        command = _load(canonical(command))  # Freeze nested mutable request data before the transaction.
        request_hash = digest(command)
        require(len(canonical(command).encode("utf-8")) <= 32 * 1024, "COMMAND_TOO_LARGE", "命令超限")
        with self._connection(write=True) as conn:
            book, previous, last_time = self._replay(conn)
            existing = conn.execute("SELECT sequence,request_hash FROM commands WHERE command_id=?", (cid,)).fetchone()
            if existing:
                require(existing[1] == request_hash, "IDEMPOTENCY_CONFLICT", "相同ID对应不同命令", 409)
                return {"book": book, "command_revision": existing[0], "idempotent": True}
            require(last_time is None or timestamp >= last_time, "CLOCK_ROLLBACK", "本机时钟回退，暂停写入", 409)
            next_book = reduce_command(book, command, parents, timestamp)
            # Store only the parent used by this command, not an unrelated private portfolio dump.
            pid = command["data"].get("position_id")
            used_parents = {pid: parents[pid]} if pid in parents else {}
            record = {"schema": STORE_SCHEMA, "store_id": self.store_id, "sequence": next_book["revision"],
                      "previous_hash": previous, "recorded_at": timestamp.isoformat(), "command": command, "parents": used_parents}
            raw = canonical(record)
            conn.execute("INSERT INTO commands VALUES (?,?,?,?,?)", (next_book["revision"], cid, request_hash, raw, digest(record)))
            self._fault("before_commit")
            conn.commit()
            self._fault("after_commit")
            return {"book": next_book, "command_revision": next_book["revision"], "idempotent": False}
