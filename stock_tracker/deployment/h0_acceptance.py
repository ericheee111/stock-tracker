"""Hybrid H0 REST/SSE/Portfolio acceptance harness.

The harness is intentionally safe for repository automation:

* the local/server fixture always uses a temporary SQLite database;
* portfolio writes are enabled only after a signed-by-construction fixture
  marker (random fixture identity + explicit boolean) is verified;
* bearer secrets remain request headers and are never rendered in evidence;
* physical two-device acceptance is a separate, explicit client mode.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from urllib.parse import urlsplit

from ..api.handlers import AppContext
from ..api.server import APIServer
from ..api.sse import SSEHub
from ..core.config import load_configs
from ..core.security import PRIVATE_ACCESS_ENV, private_access_value_valid
from ..core.store import MarketStore
from ..storage.db import close_all
from ..storage.repository import Repository

MARKER_PATH = "/.well-known/stock-tracker-h0.json"
MARKER_SCHEMA = "stock-tracker-hybrid-h0-fixture-v1"
REPORT_SCHEMA = "stock-tracker-hybrid-h0-acceptance-v1"
DEFAULT_TIMEOUT_SEC = 8.0


def validate_tailnet_serve_origin(base_url: object) -> str:
    """Return a normalized, tailnet-only Serve origin for real device tests."""

    if type(base_url) is not str or not base_url.strip() or base_url != base_url.strip():
        raise HybridH0AcceptanceError("base URL must be a non-empty trimmed string")
    parsed = urlsplit(base_url)
    hostname = parsed.hostname.rstrip(".").lower() if parsed.hostname else ""
    if parsed.scheme != "https":
        raise HybridH0AcceptanceError("real two-device acceptance requires HTTPS")
    if parsed.username or parsed.password:
        raise HybridH0AcceptanceError("base URL must not contain userinfo")
    if not hostname.endswith(".ts.net") or hostname == "ts.net":
        raise HybridH0AcceptanceError("real two-device acceptance requires a *.ts.net Serve hostname")
    if parsed.port not in (None, 443):
        raise HybridH0AcceptanceError("Hybrid H0 Serve acceptance uses the default HTTPS port 443")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HybridH0AcceptanceError("base URL must be an origin without path, query, or fragment")
    return f"https://{hostname}"


class HybridH0AcceptanceError(RuntimeError):
    """Raised for malformed or unsafe acceptance input."""


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class H0AcceptanceReport:
    scope: str
    base_origin: str
    fixture_id: str
    server_hostname: str | None
    client_hostname: str
    server_tailscale_node_id: str | None
    client_tailscale_node_id: str | None
    device_distinct: bool | None
    checks: tuple[AcceptanceCheck, ...]
    completed_at: str

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": REPORT_SCHEMA,
            "passed": self.passed,
            "scope": self.scope,
            "base_origin": self.base_origin,
            "fixture_id": self.fixture_id,
            "server_hostname": self.server_hostname,
            "client_hostname": self.client_hostname,
            "server_tailscale_node_id": self.server_tailscale_node_id,
            "client_tailscale_node_id": self.client_tailscale_node_id,
            "device_distinct": self.device_distinct,
            "checks": [check.as_dict() for check in self.checks],
            "completed_at": self.completed_at,
            "contains_private_access": False,
            "production_database_modified": False,
        }


class _LocalBus:
    def subscribe(self, callback: Any) -> None:
        self.callback = callback


class _RouterStub:
    def health_list(self) -> list[Any]:
        return []


class _HTTPClient:
    def __init__(
        self,
        base_url: str,
        *,
        private_access: str,
        timeout_sec: float,
        host_header: str | None = None,
        forwarded_for: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise HybridH0AcceptanceError("base URL must use http or https")
        if not parsed.hostname or parsed.username or parsed.password:
            raise HybridH0AcceptanceError("base URL must contain a hostname and no userinfo")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise HybridH0AcceptanceError("base URL must be an origin without path, query, or fragment")
        if not private_access_value_valid(private_access):
            raise HybridH0AcceptanceError("private access value does not meet the server contract")
        if (
            type(timeout_sec) not in (int, float)
            or not math.isfinite(float(timeout_sec))
            or float(timeout_sec) <= 0
        ):
            raise HybridH0AcceptanceError("timeout must be a positive finite number")

        default_port = 443 if parsed.scheme == "https" else 80
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname
        self.port = parsed.port or default_port
        display_host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        self.base_origin = f"{self.scheme}://{display_host}"
        if self.port != default_port:
            self.base_origin += f":{self.port}"
        self.private_access = private_access
        self.timeout_sec = float(timeout_sec)
        self.host_header = host_header or parsed.netloc
        self.forwarded_for = forwarded_for

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.hostname,
                self.port,
                timeout=self.timeout_sec,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self.hostname, self.port, timeout=self.timeout_sec)

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        json_body: dict[str, object] | None = None,
        accept: str = "application/json",
    ) -> tuple[int, dict[str, str], bytes]:
        connection = self._connection()
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self.host_header)
            connection.putheader("Accept", accept)
            connection.putheader("Connection", "close")
            if self.forwarded_for:
                connection.putheader("X-Forwarded-For", self.forwarded_for)
            if authenticated:
                connection.putheader("Authorization", f"Bearer {self.private_access}")
            body: bytes | None = None
            if json_body is not None:
                body = json.dumps(
                    json_body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                connection.putheader("Content-Type", "application/json; charset=utf-8")
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            payload = response.read(1024 * 1024)
            headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, headers, payload
        finally:
            connection.close()

    def sse_probe(self) -> tuple[int, dict[str, str], bytes]:
        connection = self._connection()
        try:
            connection.putrequest("GET", "/api/stream", skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self.host_header)
            connection.putheader("Accept", "text/event-stream")
            if self.forwarded_for:
                connection.putheader("X-Forwarded-For", self.forwarded_for)
            connection.putheader("Authorization", f"Bearer {self.private_access}")
            connection.endheaders()
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            first_line = response.readline(4096)
            return response.status, headers, first_line
        finally:
            connection.close()


def _decode_json(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _error_code(payload: bytes) -> str | None:
    decoded = _decode_json(payload)
    if decoded is None:
        return None
    error = decoded.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _position_symbol(fixture_id: str) -> str:
    digits = int(fixture_id[:12], 16) % 100_000
    return f"6{digits:05d}.SH"


def _completed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report(
    *,
    scope: str,
    client: _HTTPClient,
    fixture_id: str,
    server_hostname: str | None,
    client_hostname: str,
    server_tailscale_node_id: str | None,
    client_tailscale_node_id: str | None,
    device_distinct: bool | None,
    checks: list[AcceptanceCheck],
) -> H0AcceptanceReport:
    return H0AcceptanceReport(
        scope=scope,
        base_origin=client.base_origin,
        fixture_id=fixture_id,
        server_hostname=server_hostname,
        client_hostname=client_hostname,
        server_tailscale_node_id=server_tailscale_node_id,
        client_tailscale_node_id=client_tailscale_node_id,
        device_distinct=device_distinct,
        checks=tuple(checks),
        completed_at=_completed_at(),
    )


def verify_h0_acceptance(
    *,
    base_url: str,
    fixture_id: str,
    private_access: str,
    scope: str,
    require_distinct_devices: bool,
    client_hostname: str | None = None,
    client_tailscale_node_id: str | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    host_header: str | None = None,
    forwarded_for: str | None = None,
) -> H0AcceptanceReport:
    """Verify H0 REST/SSE/Portfolio behavior against a fixture-only Engine.

    Portfolio writes never occur unless the marker schema, fixture identity and
    strict ``allow_portfolio_writes is True`` contract are all satisfied.
    """

    if type(fixture_id) is not str or re.fullmatch(r"[0-9a-f]{32}", fixture_id) is None:
        raise HybridH0AcceptanceError("fixture_id must be exactly 32 lowercase hexadecimal characters")
    if type(scope) is not str or not scope.strip():
        raise HybridH0AcceptanceError("scope is required")
    if type(require_distinct_devices) is not bool:
        raise HybridH0AcceptanceError("require_distinct_devices must be an actual boolean")
    if client_tailscale_node_id is not None and (
        type(client_tailscale_node_id) is not str
        or not client_tailscale_node_id.strip()
        or client_tailscale_node_id != client_tailscale_node_id.strip()
    ):
        raise HybridH0AcceptanceError("client_tailscale_node_id must be a non-empty trimmed string")

    client_name = (client_hostname or socket.gethostname()).strip()
    if not client_name:
        raise HybridH0AcceptanceError("client hostname is empty")
    client = _HTTPClient(
        base_url,
        private_access=private_access,
        timeout_sec=timeout_sec,
        host_header=host_header,
        forwarded_for=forwarded_for,
    )
    checks: list[AcceptanceCheck] = []

    try:
        marker_status, marker_headers, marker_body = client.request("GET", MARKER_PATH)
    except (OSError, http.client.HTTPException) as exc:
        checks.append(AcceptanceCheck("fixture_marker_reachable", False, type(exc).__name__))
        return _report(
            scope=scope,
            client=client,
            fixture_id=fixture_id,
            server_hostname=None,
            client_hostname=client_name,
            server_tailscale_node_id=None,
            client_tailscale_node_id=client_tailscale_node_id,
            device_distinct=None,
            checks=checks,
        )

    marker = _decode_json(marker_body)
    marker_content_type = marker_headers.get("content-type", "")
    marker_valid = (
        marker_status == 200
        and "application/json" in marker_content_type
        and marker is not None
        and marker.get("schema") == MARKER_SCHEMA
        and marker.get("fixture_id") == fixture_id
        and marker.get("fixture_only") is True
        and marker.get("allow_portfolio_writes") is True
        and marker.get("production_database") is False
    )
    checks.append(
        AcceptanceCheck(
            "fixture_marker_verified",
            marker_valid,
            f"HTTP {marker_status}; schema={marker.get('schema') if marker else None}",
        )
    )
    server_hostname = marker.get("server_hostname") if marker else None
    if not isinstance(server_hostname, str) or not server_hostname.strip():
        server_hostname = None
    server_tailscale_node_id = marker.get("server_tailscale_node_id") if marker else None
    if (
        not isinstance(server_tailscale_node_id, str)
        or not server_tailscale_node_id.strip()
    ):
        server_tailscale_node_id = None
    device_distinct = (
        server_tailscale_node_id != client_tailscale_node_id
        if server_tailscale_node_id is not None and client_tailscale_node_id is not None
        else None
    )
    distinct_ok = device_distinct is True if require_distinct_devices else True
    checks.append(
        AcceptanceCheck(
            "distinct_tailscale_node",
            distinct_ok,
            (
                f"required={require_distinct_devices}; distinct={device_distinct}; "
                f"server_node_present={server_tailscale_node_id is not None}; "
                f"client_node_present={client_tailscale_node_id is not None}"
            ),
        )
    )
    if not marker_valid or not distinct_ok:
        return _report(
            scope=scope,
            client=client,
            fixture_id=fixture_id,
            server_hostname=server_hostname,
            client_hostname=client_name,
            server_tailscale_node_id=server_tailscale_node_id,
            client_tailscale_node_id=client_tailscale_node_id,
            device_distinct=device_distinct,
            checks=checks,
        )

    def record(name: str, passed: bool, detail: str) -> bool:
        checks.append(AcceptanceCheck(name, passed, detail))
        return passed

    try:
        status, headers, body = client.request("GET", "/", accept="text/html")
        record(
            "same_origin_static_web",
            status == 200 and "text/html" in headers.get("content-type", "") and bool(body),
            f"HTTP {status}; bytes={len(body)}",
        )

        status, _, body = client.request("GET", "/api/provider_health")
        provider_payload = _decode_json(body)
        record(
            "public_provider_health_rest",
            status == 200 and provider_payload is not None and "providers" in provider_payload,
            f"HTTP {status}",
        )

        status, _, body = client.request("GET", "/api/portfolio")
        record(
            "private_api_rejects_missing_bearer",
            status == 401 and _error_code(body) == "PRIVATE_API_AUTH_REQUIRED",
            f"HTTP {status}; code={_error_code(body)}",
        )

        status, _, body = client.request("GET", "/api/portfolio", authenticated=True)
        portfolio_payload = _decode_json(body)
        record(
            "private_api_accepts_exact_bearer",
            status == 200
            and portfolio_payload is not None
            and portfolio_payload.get("schema_version") == "stage1-v1",
            f"HTTP {status}; schema={portfolio_payload.get('schema_version') if portfolio_payload else None}",
        )

        status, headers, first_line = client.sse_probe()
        record(
            "fetch_stream_sse_authenticated",
            status == 200
            and "text/event-stream" in headers.get("content-type", "")
            and first_line.strip() == b": connected",
            f"HTTP {status}; first_line={first_line.decode('utf-8', errors='replace').strip()!r}",
        )
    except (OSError, http.client.HTTPException) as exc:
        record("read_only_transport", False, type(exc).__name__)

    if not all(check.passed for check in checks):
        return _report(
            scope=scope,
            client=client,
            fixture_id=fixture_id,
            server_hostname=server_hostname,
            client_hostname=client_name,
            server_tailscale_node_id=server_tailscale_node_id,
            client_tailscale_node_id=client_tailscale_node_id,
            device_distinct=device_distinct,
            checks=checks,
        )

    created_position_id: str | None = None
    try:
        profile = {
            "account_equity": 120000.0,
            "available_cash": 60000.0,
            "risk_mode": "BALANCED",
            "per_trade_risk_pct": 0.007,
            "max_position_pct": 0.25,
            "max_portfolio_heat_pct": 0.08,
            "max_sector_pct": 0.40,
            "max_theme_pct": 0.40,
        }
        status, _, body = client.request(
            "PUT",
            "/api/portfolio/profile",
            authenticated=True,
            json_body=profile,
        )
        profile_result = _decode_json(body)
        record(
            "portfolio_profile_put",
            status == 200
            and profile_result is not None
            and profile_result.get("account_equity") == 120000.0,
            f"HTTP {status}",
        )

        position = {
            "symbol": _position_symbol(fixture_id),
            "market": "A",
            "shares": 37,
            "average_cost": 10.5,
            "added_at": _completed_at(),
        }
        status, _, body = client.request(
            "POST",
            "/api/portfolio/positions",
            authenticated=True,
            json_body=position,
        )
        created = _decode_json(body)
        if created is not None and isinstance(created.get("id"), str):
            created_position_id = created["id"]
        record(
            "portfolio_position_post",
            status == 201
            and created_position_id is not None
            and created.get("shares") == 37,
            f"HTTP {status}; created={created_position_id is not None}",
        )

        if created_position_id is not None:
            status, _, body = client.request(
                "PATCH",
                f"/api/portfolio/positions/{created_position_id}",
                authenticated=True,
                json_body={"shares": 13, "average_cost": 10.25},
            )
            patched = _decode_json(body)
            record(
                "portfolio_position_patch",
                status == 200
                and patched is not None
                and patched.get("shares") == 13
                and patched.get("average_cost") == 10.25,
                f"HTTP {status}",
            )

            status, _, body = client.request(
                "DELETE",
                f"/api/portfolio/positions/{created_position_id}",
                authenticated=True,
            )
            deleted = _decode_json(body)
            record(
                "portfolio_position_delete",
                status == 200 and deleted is not None and deleted.get("ok") is True,
                f"HTTP {status}",
            )
            if status == 200:
                created_position_id = None

        status, _, body = client.request("GET", "/api/portfolio", authenticated=True)
        final_portfolio = _decode_json(body)
        final_positions = final_portfolio.get("positions") if final_portfolio else None
        record(
            "portfolio_final_state_clean",
            status == 200 and isinstance(final_positions, list) and not final_positions,
            f"HTTP {status}; count={len(final_positions) if isinstance(final_positions, list) else None}",
        )
    except (OSError, http.client.HTTPException) as exc:
        record("portfolio_crud_transport", False, type(exc).__name__)
    finally:
        if created_position_id is not None:
            try:
                client.request(
                    "DELETE",
                    f"/api/portfolio/positions/{created_position_id}",
                    authenticated=True,
                )
            except (OSError, http.client.HTTPException):
                checks.append(
                    AcceptanceCheck(
                        "portfolio_cleanup_after_failure",
                        False,
                        "cleanup request failed",
                    )
                )

    return _report(
        scope=scope,
        client=client,
        fixture_id=fixture_id,
        server_hostname=server_hostname,
        client_hostname=client_name,
        server_tailscale_node_id=server_tailscale_node_id,
        client_tailscale_node_id=client_tailscale_node_id,
        device_distinct=device_distinct,
        checks=checks,
    )


class TemporaryH0Fixture:
    """Temporary, marker-gated H0 Engine that never opens the production DB."""

    def __init__(
        self,
        *,
        private_access: str | None = None,
        fixture_id: str | None = None,
        server_hostname: str | None = None,
        server_tailscale_node_id: str | None = None,
        web_source: str | Path | None = None,
        config_dir: str | Path | None = None,
    ) -> None:
        self.private_access = private_access or secrets.token_urlsafe(48)
        if not private_access_value_valid(self.private_access):
            raise HybridH0AcceptanceError("fixture private access does not meet the server contract")
        self.fixture_id = fixture_id or uuid.uuid4().hex
        if re.fullmatch(r"[0-9a-f]{32}", self.fixture_id) is None:
            raise HybridH0AcceptanceError(
                "fixture_id must be exactly 32 lowercase hexadecimal characters"
            )
        self.server_hostname = (server_hostname or socket.gethostname()).strip()
        if not self.server_hostname:
            raise HybridH0AcceptanceError("server hostname is empty")
        if server_tailscale_node_id is not None and (
            type(server_tailscale_node_id) is not str
            or not server_tailscale_node_id.strip()
            or server_tailscale_node_id != server_tailscale_node_id.strip()
        ):
            raise HybridH0AcceptanceError(
                "server_tailscale_node_id must be a non-empty trimmed string"
            )
        self.server_tailscale_node_id = server_tailscale_node_id
        root = Path(__file__).resolve().parents[2]
        self.web_source = Path(web_source) if web_source is not None else root / "web"
        self.config_dir = Path(config_dir) if config_dir is not None else root / "config"
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._server: APIServer | None = None
        self._thread: threading.Thread | None = None
        self._previous_access: str | None = None
        self._had_previous_access = False
        self.port: int | None = None
        self.base_url: str | None = None
        self.database_path: str | None = None

    def __enter__(self) -> Self:
        if not self.web_source.is_dir():
            raise HybridH0AcceptanceError(f"web source does not exist: {self.web_source}")
        self._temporary = tempfile.TemporaryDirectory(prefix="stock-tracker-h0-")
        root = Path(self._temporary.name)
        web_root = root / "web"
        shutil.copytree(self.web_source, web_root)
        marker_path = web_root / MARKER_PATH.lstrip("/")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "schema": MARKER_SCHEMA,
            "fixture_id": self.fixture_id,
            "fixture_only": True,
            "allow_portfolio_writes": True,
            "production_database": False,
            "server_hostname": self.server_hostname,
            "server_tailscale_node_id": self.server_tailscale_node_id,
            "created_at": _completed_at(),
        }
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )

        self.database_path = str(root / "h0-acceptance.db")
        bundle = load_configs(str(self.config_dir))
        store = MarketStore()
        repo = Repository(self.database_path)
        hub = SSEHub(_LocalBus())
        ctx = AppContext(
            bundle=bundle,
            store=store,
            repo=repo,
            router=_RouterStub(),
            signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
            sse_hub=hub,
            web_root=str(web_root),
        )

        self._had_previous_access = PRIVATE_ACCESS_ENV in os.environ
        self._previous_access = os.environ.get(PRIVATE_ACCESS_ENV)
        os.environ[PRIVATE_ACCESS_ENV] = self.private_access
        try:
            self._server = APIServer("127.0.0.1", 0, ctx, None)
            self.port = self._server.server_address[1]
            self.base_url = f"http://127.0.0.1:{self.port}"
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name=f"stock-tracker-h0-{self.fixture_id[:8]}",
            )
            self._thread.start()
            return self
        except Exception:
            self._restore_environment()
            close_all()
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            raise

    def _restore_environment(self) -> None:
        if self._had_previous_access:
            assert self._previous_access is not None
            os.environ[PRIVATE_ACCESS_ENV] = self._previous_access
        else:
            os.environ.pop(PRIVATE_ACCESS_ENV, None)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._server is not None:
                self._server.shutdown_wait()
            if self._thread is not None:
                self._thread.join(timeout=5)
        finally:
            close_all()
            self._restore_environment()
            if self._temporary is not None:
                self._temporary.cleanup()
            self._temporary = None
            self._server = None
            self._thread = None
