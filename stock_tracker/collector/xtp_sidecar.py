"""Read-only client for the isolated loopback XTP quote sidecar."""

from __future__ import annotations

import ipaddress
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse

import tomllib

from sidecars.xtp.contracts import (
    ENV_SIDECAR_ACCESS,
    EVENTS_RESPONSE_SCHEMA,
    HEALTH_SCHEMA,
    METRICS_SCHEMA,
    SESSION_SCHEMA,
    EventEnvelope,
    XtpSidecarContractError,
    strict_json_loads,
    validate_access,
    validate_symbol,
)

_MAX_BODY_BYTES = 8 * 1024 * 1024
_XTP_FIELDS = frozenset(
    {
        "enabled",
        "backend",
        "bind_host",
        "bind_port",
        "max_symbols",
        "expected_api_version",
        "expected_python_series",
        "read_only",
        "allow_live_decision",
        "allow_model_training",
        "allow_public_redistribution",
        "auto_trade",
        "level2_required",
    }
)
_IPC_FIELDS = frozenset(
    {
        "health_public",
        "max_events_per_response",
        "max_response_bytes",
        "request_timeout_ms",
    }
)
_EVENT_STORE_FIELDS = frozenset({"root", "metadata_db", "quarantine_root"})
_MONITOR_FIELDS = frozenset(
    {"database", "webhook_enabled", "webhook_allowed_origins"}
)
_SECRET_FIELD_PARTS = (
    "password",
    "secret",
    "token",
    "access_key",
    "api_key",
    "quote_user",
    "quote_access",
    "quote_server",
    "client_id",
    "account",
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ALLOWED_CONNECTION_STATES = frozenset(
    {"STARTING", "CONNECTED", "RECONNECTING", "DISCONNECTED"}
)
_ALLOWED_FEED_MODES = frozenset({"SIMULATOR", "LEVEL1", "LEVEL2"})
_MAX_FUTURE_SECONDS = 120


class XtpSidecarClientError(RuntimeError):
    """Raised when the local XTP sidecar violates transport or data contracts."""


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


@dataclass(frozen=True, slots=True)
class XtpSidecarConfig:
    enabled: bool
    backend: str
    bind_host: str
    bind_port: int
    max_symbols: int
    expected_api_version: str
    expected_python_series: str
    read_only: bool
    allow_live_decision: bool
    allow_model_training: bool
    allow_public_redistribution: bool
    auto_trade: bool
    level2_required: bool
    health_public: bool
    max_events_per_response: int
    max_response_bytes: int
    request_timeout_ms: int
    event_root: str
    metadata_db: str
    quarantine_root: str
    monitor_db: str
    webhook_enabled: bool
    webhook_allowed_origins: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise XtpSidecarClientError("xtp.enabled must be boolean")
        if self.backend not in {"simulator", "xtp"}:
            raise XtpSidecarClientError("xtp.backend must be simulator or xtp")
        try:
            host = ipaddress.ip_address(self.bind_host)
        except ValueError as exc:
            raise XtpSidecarClientError("xtp.bind_host must be a literal loopback IP") from exc
        if host.version != 4 or not host.is_loopback:
            raise XtpSidecarClientError(
                "xtp.bind_host must remain a literal IPv4 loopback address"
            )
        if type(self.bind_port) is not int or not 1 <= self.bind_port <= 65535:
            raise XtpSidecarClientError("xtp.bind_port must be between 1 and 65535")
        if type(self.max_symbols) is not int or not 1 <= self.max_symbols <= 20:
            raise XtpSidecarClientError("xtp.max_symbols must be between 1 and 20")
        if self.expected_api_version != "2.2.50.8":
            raise XtpSidecarClientError("unexpected XTP API version")
        if self.expected_python_series != "3.9":
            raise XtpSidecarClientError("XTP sidecar Python series must stay 3.9")
        for name in (
            "read_only",
            "allow_live_decision",
            "allow_model_training",
            "allow_public_redistribution",
            "auto_trade",
            "level2_required",
            "health_public",
            "webhook_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise XtpSidecarClientError(f"{name} must be boolean")
        if self.read_only is not True:
            raise XtpSidecarClientError("XTP sidecar must remain read_only")
        if self.allow_live_decision or self.allow_model_training or self.allow_public_redistribution:
            raise XtpSidecarClientError("XTP sidecar trust/use gates must remain false")
        if self.auto_trade:
            raise XtpSidecarClientError("auto_trade must remain false")
        if type(self.max_events_per_response) is not int or not 1 <= self.max_events_per_response <= 500:
            raise XtpSidecarClientError("ipc.max_events_per_response must be 1-500")
        if type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= _MAX_BODY_BYTES:
            raise XtpSidecarClientError("ipc.max_response_bytes is outside the allowed range")
        if type(self.request_timeout_ms) is not int or not 250 <= self.request_timeout_ms <= 30000:
            raise XtpSidecarClientError("ipc.request_timeout_ms is outside the allowed range")
        storage_values = (
            (self.event_root, "event_store.root"),
            (self.metadata_db, "event_store.metadata_db"),
            (self.quarantine_root, "event_store.quarantine_root"),
            (self.monitor_db, "monitor.database"),
        )
        normalized_storage: dict[str, Path] = {}
        for value, name in storage_values:
            if type(value) is not str or not value.strip() or value != value.strip():
                raise XtpSidecarClientError(
                    f"{name} must be a non-empty relative path"
                )
            candidate = Path(value)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise XtpSidecarClientError(f"{name} must stay inside the project")
            normalized = Path(candidate.as_posix().casefold())
            normalized_storage[name] = normalized
        production_db = Path("data/stock_tracker.db")
        if normalized_storage["event_store.metadata_db"] == production_db:
            raise XtpSidecarClientError("event store must not use the production database")
        if normalized_storage["monitor.database"] == production_db:
            raise XtpSidecarClientError("monitor store must not use the production database")
        if normalized_storage["event_store.metadata_db"] == normalized_storage["monitor.database"]:
            raise XtpSidecarClientError("event and monitor databases must be separate")
        event_root = normalized_storage["event_store.root"]
        quarantine_root = normalized_storage["event_store.quarantine_root"]
        if (
            event_root == quarantine_root
            or event_root in quarantine_root.parents
            or quarantine_root in event_root.parents
        ):
            raise XtpSidecarClientError("event and quarantine roots must not overlap")
        if not isinstance(self.webhook_allowed_origins, tuple):
            raise XtpSidecarClientError("monitor.webhook_allowed_origins must be an array")
        normalized_origins = tuple(
            _normalize_https_origin(value) for value in self.webhook_allowed_origins
        )
        if normalized_origins != self.webhook_allowed_origins:
            raise XtpSidecarClientError(
                "monitor.webhook_allowed_origins must be normalized exact HTTPS origins"
            )
        if len(set(normalized_origins)) != len(normalized_origins):
            raise XtpSidecarClientError(
                "monitor.webhook_allowed_origins must be unique"
            )

    @property
    def origin(self) -> str:
        return f"http://{self.bind_host}:{self.bind_port}"


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise XtpSidecarClientError(f"{name} must be a TOML table")
    return value


def _require_exact_section(
    value: dict[str, Any],
    *,
    name: str,
    allowed: frozenset[str],
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise XtpSidecarClientError(f"unknown {name} field: {min(unknown)}")
    for field in value:
        lowered = field.lower()
        if any(part in lowered for part in _SECRET_FIELD_PARTS):
            raise XtpSidecarClientError(
                f"secret/account field is forbidden in committed XTP config: {field}"
            )


def _normalize_https_origin(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise XtpSidecarClientError("webhook origin must be a trimmed HTTPS origin")
    if len(value) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise XtpSidecarClientError("webhook origin contains invalid characters")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise XtpSidecarClientError("webhook origin is malformed") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise XtpSidecarClientError("webhook origin must be an exact HTTPS origin")
    host = hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise XtpSidecarClientError("webhook origin host is invalid") from exc
    return f"https://{host}" + ("" if port in (None, 443) else f":{port}")


def load_xtp_sidecar_config(path: str | Path) -> XtpSidecarConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise XtpSidecarClientError("cannot load XTP sidecar config") from exc
    allowed_sections = {"xtp", "ipc", "event_store", "monitor"}
    unknown_sections = sorted(set(document) - allowed_sections)
    if unknown_sections:
        raise XtpSidecarClientError(
            f"unknown XTP config section: {unknown_sections[0]}"
        )
    xtp = _section(document, "xtp")
    ipc = _section(document, "ipc")
    event_store = _section(document, "event_store")
    monitor = _section(document, "monitor")
    _require_exact_section(xtp, name="xtp", allowed=_XTP_FIELDS)
    _require_exact_section(ipc, name="ipc", allowed=_IPC_FIELDS)
    _require_exact_section(
        event_store,
        name="event_store",
        allowed=_EVENT_STORE_FIELDS,
    )
    _require_exact_section(monitor, name="monitor", allowed=_MONITOR_FIELDS)
    origins = monitor.get("webhook_allowed_origins", [])
    if not isinstance(origins, list) or any(type(value) is not str for value in origins):
        raise XtpSidecarClientError("monitor.webhook_allowed_origins must be a string array")
    return XtpSidecarConfig(
        enabled=xtp.get("enabled", False),
        backend=xtp.get("backend", "simulator"),
        bind_host=xtp.get("bind_host", "127.0.0.1"),
        bind_port=xtp.get("bind_port", 17991),
        max_symbols=xtp.get("max_symbols", 20),
        expected_api_version=xtp.get("expected_api_version", "2.2.50.8"),
        expected_python_series=xtp.get("expected_python_series", "3.9"),
        read_only=xtp.get("read_only", True),
        allow_live_decision=xtp.get("allow_live_decision", False),
        allow_model_training=xtp.get("allow_model_training", False),
        allow_public_redistribution=xtp.get("allow_public_redistribution", False),
        auto_trade=xtp.get("auto_trade", False),
        level2_required=xtp.get("level2_required", False),
        health_public=ipc.get("health_public", True),
        max_events_per_response=ipc.get("max_events_per_response", 500),
        max_response_bytes=ipc.get("max_response_bytes", _MAX_BODY_BYTES),
        request_timeout_ms=ipc.get("request_timeout_ms", 3000),
        event_root=event_store.get("root", "data/market-events"),
        metadata_db=event_store.get("metadata_db", "data/market_events.db"),
        quarantine_root=event_store.get("quarantine_root", "data/market-events-quarantine"),
        monitor_db=monitor.get("database", "data/monitor.db"),
        webhook_enabled=monitor.get("webhook_enabled", False),
        webhook_allowed_origins=tuple(origins),
    )


def _require_exact_keys(
    payload: Any,
    expected: frozenset[str],
    name: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise XtpSidecarClientError(f"{name} must be an object")
    unknown = set(payload) - expected
    missing = expected - set(payload)
    if unknown:
        raise XtpSidecarClientError(f"{name} contains unknown field: {min(unknown)}")
    if missing:
        raise XtpSidecarClientError(f"{name} is missing field: {min(missing)}")
    return payload


def _require_nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise XtpSidecarClientError(f"{name} must be a non-negative integer")
    return value


def _require_finite_nonnegative(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise XtpSidecarClientError(f"{name} must be numeric")
    number = float(value)
    if not (number >= 0 and number < float("inf")):
        raise XtpSidecarClientError(f"{name} must be finite and non-negative")
    return number


def _require_identifier(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise XtpSidecarClientError(f"{name} is invalid")
    return value


def _require_aware_time(value: Any, name: str, *, nullable: bool = False) -> datetime | None:
    if value is None:
        if nullable:
            return None
        raise XtpSidecarClientError(f"{name} must be timezone-aware ISO 8601")
    if type(value) is not str or not value or len(value) > 64:
        raise XtpSidecarClientError(f"{name} must be timezone-aware ISO 8601")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise XtpSidecarClientError(f"{name} must be timezone-aware ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise XtpSidecarClientError(f"{name} must be timezone-aware ISO 8601")
    utc = parsed.astimezone(timezone.utc)
    if (utc - datetime.now(timezone.utc)).total_seconds() > _MAX_FUTURE_SECONDS:
        raise XtpSidecarClientError(f"{name} is too far in the future")
    return utc


class XtpSidecarClient:
    """Strict loopback client used only by explicit ingestion/acceptance tools."""

    def __init__(
        self,
        config: XtpSidecarConfig,
        *,
        access_provider=lambda: os.environ.get(ENV_SIDECAR_ACCESS),
        opener: Any | None = None,
    ) -> None:
        self.config = config
        self._access_provider = access_provider
        self._opener = opener or urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            urllib_request.HTTPHandler(),
            urllib_request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirectHandler(),
        )

    def _access(self) -> str:
        try:
            return validate_access(self._access_provider())
        except XtpSidecarContractError as exc:
            raise XtpSidecarClientError(str(exc)) from exc

    def _request(self, path: str, *, public: bool = False) -> Any:
        if type(path) is not str or not path.startswith("/v1/") or "?" in path.split("/v1/", 1)[0]:
            raise XtpSidecarClientError("invalid sidecar path")
        url = self.config.origin + path
        try:
            parsed = urlparse(url)
            parsed_hostname = parsed.hostname
            parsed_port = parsed.port
        except ValueError as exc:
            raise XtpSidecarClientError("sidecar request URL is malformed") from exc
        if parsed_hostname is None:
            raise XtpSidecarClientError("sidecar request escaped loopback")
        try:
            address = ipaddress.ip_address(parsed_hostname)
        except ValueError as exc:
            raise XtpSidecarClientError("sidecar request host is invalid") from exc
        if address.version != 4 or not address.is_loopback:
            raise XtpSidecarClientError("sidecar request escaped IPv4 loopback")
        headers = {"Accept": "application/json", "User-Agent": "stock-tracker/xtp-sidecar-client-v1"}
        if not public:
            headers["Authorization"] = "Bearer " + self._access()
        request = urllib_request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(
                request,
                timeout=self.config.request_timeout_ms / 1000.0,
            ) as response:
                try:
                    final = urlparse(response.geturl())
                    final_hostname = final.hostname
                    final_port = final.port
                except ValueError as exc:
                    raise XtpSidecarClientError("sidecar response URL is malformed") from exc
                if (
                    final.scheme != parsed.scheme
                    or final_hostname != parsed_hostname
                    or final_port != parsed_port
                    or final.path != parsed.path
                    or final.params != parsed.params
                    or final.query != parsed.query
                    or final.fragment
                ):
                    raise XtpSidecarClientError(
                        "sidecar response URL changed from the requested loopback endpoint"
                    )
                if response.status != 200:
                    raise XtpSidecarClientError(
                        f"sidecar returned HTTP {response.status}"
                    )
                content_type = response.headers.get("Content-Type", "").lower()
                if "json" not in content_type:
                    raise XtpSidecarClientError("sidecar response is not JSON")
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib_error.HTTPError as exc:
            raise XtpSidecarClientError(f"sidecar returned HTTP {exc.code}") from exc
        except (urllib_error.URLError, OSError, TimeoutError) as exc:
            raise XtpSidecarClientError("sidecar transport failed") from exc
        if len(raw) > self.config.max_response_bytes:
            raise XtpSidecarClientError("sidecar response exceeds size limit")
        try:
            return strict_json_loads(raw)
        except XtpSidecarContractError as exc:
            raise XtpSidecarClientError(str(exc)) from exc

    def health(self) -> dict[str, Any]:
        payload = _require_exact_keys(
            self._request("/v1/health", public=self.config.health_public),
            frozenset(
                {
                    "schema",
                    "status",
                    "backend",
                    "read_only",
                    "auto_trade",
                    "allow_live_decision",
                    "allow_model_training",
                    "api_version",
                    "python_abi_expected",
                    "session_id",
                    "connection_state",
                    "feed_mode",
                    "subscription_count",
                    "started_at",
                    "last_event_at",
                }
            ),
            "sidecar health",
        )
        if payload["schema"] != HEALTH_SCHEMA:
            raise XtpSidecarClientError("invalid sidecar health schema")
        if payload["read_only"] is not True or payload["auto_trade"] is not False:
            raise XtpSidecarClientError("sidecar health violates read-only contract")
        if payload["allow_live_decision"] is not False or payload["allow_model_training"] is not False:
            raise XtpSidecarClientError("sidecar health violates evidence gates")
        if payload["api_version"] != self.config.expected_api_version:
            raise XtpSidecarClientError("sidecar API version mismatch")
        if payload["python_abi_expected"] != self.config.expected_python_series:
            raise XtpSidecarClientError("sidecar Python ABI contract mismatch")
        if payload["status"] not in {"OK", "DEGRADED"}:
            raise XtpSidecarClientError("sidecar health status is invalid")
        if payload["backend"] != self.config.backend.upper():
            raise XtpSidecarClientError("sidecar backend does not match configured backend")
        if payload["connection_state"] not in _ALLOWED_CONNECTION_STATES:
            raise XtpSidecarClientError("sidecar connection state is invalid")
        if payload["feed_mode"] not in _ALLOWED_FEED_MODES:
            raise XtpSidecarClientError("sidecar feed mode is invalid")
        _require_identifier(payload["session_id"], "sidecar health session_id")
        _require_aware_time(payload["started_at"], "sidecar health started_at")
        _require_aware_time(
            payload["last_event_at"],
            "sidecar health last_event_at",
            nullable=True,
        )
        subscription_count = _require_nonnegative_int(
            payload["subscription_count"], "subscription_count"
        )
        if subscription_count > self.config.max_symbols:
            raise XtpSidecarClientError("sidecar subscription count exceeds configured maximum")
        return payload

    def session(self) -> dict[str, Any]:
        payload = _require_exact_keys(
            self._request("/v1/session"),
            frozenset(
                {
                    "schema",
                    "session_id",
                    "backend",
                    "feed_mode",
                    "connection_state",
                    "started_at",
                    "connected_at",
                    "symbols",
                    "account_identifier_present",
                    "algorithm_account_used",
                }
            ),
            "sidecar session",
        )
        if payload["schema"] != SESSION_SCHEMA:
            raise XtpSidecarClientError("invalid sidecar session schema")
        if payload["algorithm_account_used"] is not False:
            raise XtpSidecarClientError("algorithm account must not be used")
        if payload["account_identifier_present"] is not False:
            raise XtpSidecarClientError("sidecar session must not expose account identity")
        _require_identifier(payload["session_id"], "sidecar session_id")
        if payload["backend"] != self.config.backend.upper():
            raise XtpSidecarClientError("sidecar session backend does not match configuration")
        if payload["feed_mode"] not in _ALLOWED_FEED_MODES:
            raise XtpSidecarClientError("sidecar session feed_mode is invalid")
        if payload["connection_state"] not in _ALLOWED_CONNECTION_STATES:
            raise XtpSidecarClientError("sidecar session connection_state is invalid")
        started_at = _require_aware_time(payload["started_at"], "sidecar session started_at")
        connected_at = _require_aware_time(
            payload["connected_at"],
            "sidecar session connected_at",
            nullable=True,
        )
        if payload["connection_state"] == "CONNECTED" and connected_at is None:
            raise XtpSidecarClientError("connected sidecar session is missing connected_at")
        if connected_at is not None and started_at is not None and connected_at < started_at:
            raise XtpSidecarClientError("sidecar connected_at precedes started_at")
        symbols = payload["symbols"]
        if (
            not isinstance(symbols, list)
            or not symbols
            or len(symbols) > self.config.max_symbols
            or len(set(symbols)) != len(symbols)
        ):
            raise XtpSidecarClientError("sidecar session symbol list is invalid")
        try:
            normalized_symbols = [validate_symbol(symbol) for symbol in symbols]
        except XtpSidecarContractError as exc:
            raise XtpSidecarClientError("sidecar session contains an invalid symbol") from exc
        if normalized_symbols != symbols:
            raise XtpSidecarClientError("sidecar session symbols are not normalized")
        return payload

    def metrics(self) -> dict[str, Any]:
        payload = _require_exact_keys(
            self._request("/v1/metrics"),
            frozenset(
                {
                    "schema",
                    "session_id",
                    "callback_count",
                    "duplicate_count",
                    "callback_gap_count",
                    "provider_gap_count",
                    "out_of_order_count",
                    "reconnect_count",
                    "disconnect_count",
                    "dropped_buffer_count",
                    "latency_p50_ms",
                    "latency_p95_ms",
                    "last_error_code",
                    "last_error_at",
                }
            ),
            "sidecar metrics",
        )
        if payload["schema"] != METRICS_SCHEMA:
            raise XtpSidecarClientError("invalid sidecar metrics schema")
        _require_identifier(payload["session_id"], "sidecar metrics session_id")
        for name in (
            "callback_count",
            "duplicate_count",
            "callback_gap_count",
            "provider_gap_count",
            "out_of_order_count",
            "reconnect_count",
            "disconnect_count",
            "dropped_buffer_count",
        ):
            _require_nonnegative_int(payload[name], name)
        p50 = _require_finite_nonnegative(payload["latency_p50_ms"], "latency_p50_ms")
        p95 = _require_finite_nonnegative(payload["latency_p95_ms"], "latency_p95_ms")
        if p95 < p50:
            raise XtpSidecarClientError("sidecar latency percentiles are inconsistent")
        error_code = payload["last_error_code"]
        error_at = payload["last_error_at"]
        if error_code is None:
            if error_at is not None:
                raise XtpSidecarClientError("sidecar last_error_at requires last_error_code")
        else:
            _require_identifier(error_code, "sidecar last_error_code")
            _require_aware_time(error_at, "sidecar last_error_at")
        return payload

    def events(
        self,
        *,
        after: int = 0,
        limit: int | None = None,
        expected_session_id: str | None = None,
        expected_feed_mode: str | None = None,
        expected_symbols: tuple[str, ...] | None = None,
    ) -> tuple[list[EventEnvelope], int, bool]:
        if type(after) is not int or after < 0:
            raise XtpSidecarClientError("after must be an integer >= 0")
        if expected_session_id is not None:
            _require_identifier(expected_session_id, "expected_session_id")
        if expected_feed_mode is not None and expected_feed_mode not in _ALLOWED_FEED_MODES:
            raise XtpSidecarClientError("expected_feed_mode is invalid")
        expected_symbol_set: frozenset[str] | None = None
        if expected_symbols is not None:
            if (
                type(expected_symbols) is not tuple
                or not expected_symbols
                or len(expected_symbols) > self.config.max_symbols
                or len(set(expected_symbols)) != len(expected_symbols)
            ):
                raise XtpSidecarClientError("expected_symbols is invalid")
            try:
                normalized_symbols = tuple(
                    validate_symbol(symbol) for symbol in expected_symbols
                )
            except XtpSidecarContractError as exc:
                raise XtpSidecarClientError(
                    "expected_symbols contains an invalid symbol"
                ) from exc
            if normalized_symbols != expected_symbols:
                raise XtpSidecarClientError("expected_symbols is not normalized")
            expected_symbol_set = frozenset(normalized_symbols)
        effective_limit = self.config.max_events_per_response if limit is None else limit
        if type(effective_limit) is not int or not 1 <= effective_limit <= self.config.max_events_per_response:
            raise XtpSidecarClientError("limit is outside the configured range")
        path = "/v1/events?" + urlencode({"after": after, "limit": effective_limit})
        payload = _require_exact_keys(
            self._request(path),
            frozenset(
                {
                    "schema",
                    "session_id",
                    "after",
                    "oldest_cursor",
                    "next_cursor",
                    "cursor_lost",
                    "has_more",
                    "events",
                }
            ),
            "sidecar events response",
        )
        if payload["schema"] != EVENTS_RESPONSE_SCHEMA:
            raise XtpSidecarClientError("invalid sidecar events schema")
        response_after = _require_nonnegative_int(payload["after"], "events after")
        if response_after != after:
            raise XtpSidecarClientError("sidecar response cursor does not match request")
        oldest_cursor = _require_nonnegative_int(payload["oldest_cursor"], "oldest_cursor")
        next_cursor = _require_nonnegative_int(payload["next_cursor"], "next_cursor")
        cursor_lost = payload["cursor_lost"]
        has_more = payload["has_more"]
        if type(cursor_lost) is not bool or type(has_more) is not bool:
            raise XtpSidecarClientError("invalid sidecar cursor booleans")
        if cursor_lost:
            raise XtpSidecarClientError(
                f"sidecar cursor was evicted; oldest available cursor is {oldest_cursor}"
            )
        if next_cursor < after:
            raise XtpSidecarClientError("invalid sidecar cursor contract")
        events_value = payload["events"]
        if not isinstance(events_value, list) or len(events_value) > effective_limit:
            raise XtpSidecarClientError("sidecar events must be a bounded array")
        try:
            events = [EventEnvelope.from_dict(value) for value in events_value]
        except XtpSidecarContractError as exc:
            raise XtpSidecarClientError(str(exc)) from exc
        previous = after
        response_session = _require_identifier(
            payload["session_id"],
            "sidecar response session_id",
        )
        if expected_session_id is not None and response_session != expected_session_id:
            raise XtpSidecarClientError("sidecar session changed during event fetch")
        if events and oldest_cursor > events[0].callback_seq:
            raise XtpSidecarClientError("sidecar oldest cursor exceeds the first returned event")
        for event in events:
            if event.session_id != response_session:
                raise XtpSidecarClientError("event session identity mismatch")
            if expected_feed_mode is not None and event.feed_mode != expected_feed_mode:
                raise XtpSidecarClientError(
                    "event feed_mode does not match the session snapshot"
                )
            if expected_symbol_set is not None and event.symbol not in expected_symbol_set:
                raise XtpSidecarClientError(
                    "event symbol is absent from the session subscription"
                )
            if event.callback_seq <= previous:
                raise XtpSidecarClientError("sidecar events are not strictly ordered")
            previous = event.callback_seq
        if events and events[-1].callback_seq != next_cursor:
            raise XtpSidecarClientError("sidecar cursor does not match last event")
        if not events and next_cursor != after:
            raise XtpSidecarClientError("empty sidecar response advanced the cursor")
        if not events and has_more:
            raise XtpSidecarClientError("empty sidecar response cannot advertise more events")
        return events, next_cursor, has_more


__all__ = [
    "XtpSidecarClient",
    "XtpSidecarClientError",
    "XtpSidecarConfig",
    "load_xtp_sidecar_config",
]
