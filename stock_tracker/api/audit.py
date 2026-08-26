"""Metadata-only append-only audit for remote-style API boundaries.

The logger accepts a fixed, bounded metadata contract. Request bodies, bearer
values, portfolio facts, symbols, client IP addresses, database paths, and
arbitrary exception text cannot be serialized through this interface.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_SCHEMA = "stock-tracker-remote-write-audit-v1"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_KNOWN_ROUTES = frozenset(
    {
        "/api/brief/today",
        "/api/config",
        "/api/events",
        "/api/overview",
        "/api/portfolio",
        "/api/portfolio/positions",
        "/api/portfolio/profile",
        "/api/positions",
        "/api/provider_health",
        "/api/radar",
        "/api/runtime/health",
        "/api/sectors",
        "/api/stream",
        "/api/watch",
        "/api/watch/remove",
        "/api/watchlist",
    }
)
_DYNAMIC_ROUTE_PATTERNS = (
    (re.compile(r"^/api/portfolio/positions/[^/]+$"), "/api/portfolio/positions/{position_id}"),
    (re.compile(r"^/api/signal/[^/]+$"), "/api/signal/{signal_id}"),
    (re.compile(r"^/api/quote/[^/]+$"), "/api/quote/{symbol}"),
)


class AuditWriteError(RuntimeError):
    """Raised when a required audit record cannot be durably appended."""


RemoteAuditError = AuditWriteError


def new_request_id() -> str:
    """Return a non-secret correlation identifier."""

    return uuid.uuid4().hex


def make_request_id() -> str:
    """Compatibility alias for request handlers."""

    return new_request_id()


def route_template(method: object, path: object) -> str:
    """Return a bounded route identity without dynamic/user-controlled values."""

    normalized_method = str(method or "").upper()
    if type(path) is not str or not path.startswith("/"):
        return "INVALID_ROUTE"
    route = path.split("?", 1)[0].split("#", 1)[0]
    if len(route) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in route):
        return "INVALID_ROUTE"
    for pattern, template in _DYNAMIC_ROUTE_PATTERNS:
        if pattern.fullmatch(route):
            return template
    if route in _KNOWN_ROUTES:
        return route
    return "/api/{unclassified-write}" if normalized_method in _WRITE_METHODS else "/api/{other}"


def template_route(method: object, path: object) -> str:
    return route_template(method, path)


def _bounded_text(value: object, *, default: str, maximum: int = 256) -> str:
    if type(value) is not str:
        return default
    cleaned = "".join(char for char in value if 32 <= ord(char) < 127).strip()
    return (cleaned or default)[:maximum]


def _status(value: object) -> int:
    if type(value) is int and 0 <= value <= 999:
        return value
    return 0


class RemoteAuditLogger:
    """Thread-safe rotating JSONL writer for bounded local audit metadata."""

    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
    ) -> None:
        if type(enabled) is not bool:
            raise TypeError("audit enabled must be an actual boolean")
        if type(max_bytes) is not int or not 64 * 1024 <= max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("audit max_bytes must be in 64 KiB..1 GiB")
        if type(backup_count) is not int or not 1 <= backup_count <= 20:
            raise ValueError("audit backup_count must be in 1..20")
        self.path = Path(path)
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()

    def _assert_no_symlink(self) -> None:
        candidates = (self.path, *self.path.parents)
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_symlink():
                    raise AuditWriteError("remote audit path must not traverse a symlink")
            except OSError as exc:
                raise AuditWriteError("cannot inspect remote audit path") from exc

    def _rotate(self, incoming_bytes: int) -> None:
        if not self.path.exists():
            return
        self._assert_no_symlink()
        try:
            current_size = self.path.stat().st_size
        except OSError as exc:
            raise AuditWriteError("cannot stat remote audit log") from exc
        if current_size + incoming_bytes <= self.max_bytes:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        try:
            if oldest.exists():
                if oldest.is_symlink():
                    raise AuditWriteError("remote audit backup must not be a symlink")
                oldest.unlink()
            for index in range(self.backup_count - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                target = self.path.with_name(f"{self.path.name}.{index + 1}")
                if source.exists():
                    if source.is_symlink() or target.is_symlink():
                        raise AuditWriteError("remote audit backup must not be a symlink")
                    os.replace(source, target)
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except AuditWriteError:
            raise
        except OSError as exc:
            raise AuditWriteError("cannot rotate remote audit log") from exc

    def ensure_ready(self) -> None:
        """Verify that the configured append-only path is locally writable."""

        if not self.enabled:
            raise AuditWriteError("remote audit is disabled")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._assert_no_symlink()
            with self.path.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_no_symlink()
        except AuditWriteError:
            raise
        except OSError as exc:
            raise AuditWriteError("remote audit log is not writable") from exc

    def writable(self) -> bool:
        try:
            self.ensure_ready()
        except AuditWriteError:
            return False
        return True

    def record(self, **values: Any) -> dict[str, object]:
        """Append one bounded metadata record; unknown keywords are discarded."""

        if not self.enabled:
            raise AuditWriteError("remote audit is disabled")
        request_id = _bounded_text(
            values.get("request_id"),
            default=new_request_id(),
            maximum=64,
        )
        method = _bounded_text(values.get("method"), default="UNKNOWN", maximum=16).upper()
        raw_path = values.get("route", values.get("path", "INVALID_ROUTE"))
        record: dict[str, object] = {
            "schema": AUDIT_SCHEMA,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "event": _bounded_text(values.get("event"), default="REMOTE_API", maximum=64).upper(),
            "method": method,
            "route": route_template(method, raw_path),
            "client_boundary": _bounded_text(
                values.get("client_boundary", values.get("client_class")),
                default="UNKNOWN",
                maximum=64,
            ),
            "origin": _bounded_text(values.get("origin"), default="NONE", maximum=512),
            "outcome": _bounded_text(values.get("outcome"), default="UNKNOWN", maximum=64).upper(),
            "status": _status(values.get("status", values.get("status_code"))),
            "code": _bounded_text(values.get("code"), default="NONE", maximum=128),
        }
        payload = (
            json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._assert_no_symlink()
                self._rotate(len(payload))
                with self.path.open("ab") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._assert_no_symlink()
            except AuditWriteError:
                raise
            except OSError as exc:
                raise AuditWriteError("cannot append remote audit record") from exc
        return record


AuditLogger = RemoteAuditLogger


__all__ = [
    "AUDIT_SCHEMA",
    "AuditLogger",
    "AuditWriteError",
    "RemoteAuditError",
    "RemoteAuditLogger",
    "make_request_id",
    "new_request_id",
    "route_template",
    "template_route",
]
