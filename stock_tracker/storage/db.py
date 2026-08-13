"""SQLite 连接管理（线程本地单连接 + 幂等建表）。

- 每个线程独立连接（sqlite3 连接非线程安全）。
- 首次建连时执行 ``schema.sql``（IF NOT EXISTS 幂等）。
- 数据库文件不存在则自动创建（目录一并创建）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
_local = threading.local()
_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _read_schema() -> str:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _init(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")
    with _lock:
        if db_path not in _initialized_paths:
            conn.executescript(_read_schema())
            _initialized_paths.add(db_path)
    return conn


def get_connection(db_path: str) -> sqlite3.Connection:
    """获取当前线程专属连接；切换数据库时先关闭旧连接。"""

    normalized = os.path.abspath(db_path)
    current = getattr(_local, "conn", None)
    current_path = getattr(_local, "path", None)
    if current is None or current_path != normalized:
        if current is not None:
            current.close()
        os.makedirs(os.path.dirname(normalized) or ".", exist_ok=True)
        _local.conn = _init(normalized)
        _local.path = normalized
    return _local.conn


def close_all() -> None:
    """关闭当前线程连接（进程退出时调用）。"""
    if hasattr(_local, "conn"):
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None
        _local.path = None
