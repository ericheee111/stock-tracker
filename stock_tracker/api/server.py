"""API HTTP 服务（§9 / T8）。

- ``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``，端口取自 app.toml。
- 静态托管 ``web/``（玻璃拟态驾驶舱前端）。
- REST 端点（§9.1）由 ``handlers`` 实现；``/api/stream`` 走 SSE 长连。
- 所有响应经 ``serializers`` 强制附带 ``data_status`` + ``observed_age_ms``。
- 只读 MarketStore + Repository，不触上游。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..core.network import (
    InvalidOriginError,
    normalize_http_origin,
    require_safe_bind,
)
from ..core.security import (
    MIN_PRIVATE_ACCESS_LENGTH,
    PRIVATE_ACCESS_ENV,
    private_access_value_valid,
)
from ..storage.db import close_all
from . import handlers as H
from .audit import (
    AuditWriteError,
    RemoteAuditLogger,
    new_request_id,
)
from .handlers import AppContext
from .runtime import build_runtime_health

# 路由表：前缀 → handler 函数（GET）
_GET_ROUTES: list[tuple[str, Any]] = [
    ("/api/brief/today", H.get_today_brief),
    ("/api/overview", H.get_overview),
    ("/api/watchlist", H.get_watchlist),
    ("/api/positions", H.get_positions),
    ("/api/radar", H.get_radar),
    ("/api/sectors", H.get_sectors),
    ("/api/markets", H.get_markets),
    ("/api/provider_health", H.get_provider_health),
    ("/api/config", H.get_config),
]

_SIGNAL_RE = re.compile(r"^/api/signal/([^/]+)$")
_QUOTE_RE = re.compile(r"^/api/quote/([^/]+)$")
_PORTFOLIO_POSITION_RE = re.compile(r"^/api/portfolio/positions/([^/]+)$")
_PRIVATE_API_PATHS = frozenset({
    "/api/brief/today",
    "/api/overview",
    "/api/portfolio",
    "/api/portfolio/profile",
    "/api/portfolio/positions",
    "/api/positions",
    "/api/watchlist",
    "/api/watch",
    "/api/watch/remove",
    "/api/events",
    "/api/radar",
    "/api/config",
    "/api/stream",
})
_PRIVATE_API_PREFIXES = (
    "/api/portfolio/positions/",
    "/api/signal/",
)
_MAX_JSON_BODY_BYTES = 64 * 1024
_MAX_OVERSIZE_DRAIN_BYTES = 1024 * 1024
_CORS_ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_CORS_PREFLIGHT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_CORS_ALLOWED_HEADERS = frozenset({"authorization", "content-type", "accept"})
_CORS_ALLOWED_HEADERS_VALUE = "Authorization, Content-Type, Accept"


def _request_host_identity(request_host: str, scheme: str) -> tuple[str, int] | None:
    """Return normalized Host header identity for same-origin detection."""

    if type(request_host) is not str or not request_host:
        return None
    try:
        parsed = urlparse("//" + request_host)
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    normalized = hostname.rstrip(".").lower()
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return normalized, port or (443 if scheme == "https" else 80)


def _private_api_access_allowed(
    *,
    path: str,
    client_host: str,
    request_host: str,
    has_forwarding_headers: bool,
    request_origin: str,
    sec_fetch_site: str,
    authorization: str,
    configured_access: str,
) -> bool:
    is_private = path in _PRIVATE_API_PATHS or any(
        path.startswith(prefix) for prefix in _PRIVATE_API_PREFIXES
    )
    if not is_private:
        return True
    client_is_loopback = False
    try:
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        pass
    parsed_host = urlparse("//" + request_host).hostname if request_host else None
    request_host_is_loopback = parsed_host == "localhost"
    if parsed_host and not request_host_is_loopback:
        try:
            request_host_is_loopback = ipaddress.ip_address(parsed_host).is_loopback
        except ValueError:
            request_host_is_loopback = False
    origin_is_loopback = True
    if request_origin:
        origin_host = urlparse(request_origin).hostname
        origin_is_loopback = origin_host == "localhost"
        if origin_host and not origin_is_loopback:
            try:
                origin_is_loopback = ipaddress.ip_address(origin_host).is_loopback
            except ValueError:
                origin_is_loopback = False
    request_is_cross_site = sec_fetch_site.strip().lower() == "cross-site"
    if (
        client_is_loopback
        and request_host_is_loopback
        and not has_forwarding_headers
        and origin_is_loopback
        and not request_is_cross_site
    ):
        return True
    if not private_access_value_valid(configured_access):
        return False
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False
    return hmac.compare_digest(
        authorization[len(prefix):],
        configured_access,
    )


class APIHandler(BaseHTTPRequestHandler):
    """请求分发。"""

    # 不向 stderr 打印默认日志，改用统一 logger（在 server 中注入）
    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "logger", None) is not None:
            self.server.logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def handle_one_request(self) -> None:
        self._request_id = new_request_id()
        self._audit_write_pending = False
        self._audit_write_path = ""
        super().handle_one_request()

    def _request_identifier(self) -> str:
        value = getattr(self, "_request_id", "")
        if not value:
            value = new_request_id()
            self._request_id = value
        return value

    def _has_forwarding_headers(self) -> bool:
        return any(
            self.headers.get(name)
            for name in (
                "Forwarded",
                "X-Forwarded-For",
                "X-Forwarded-Host",
                "X-Real-IP",
            )
        )

    def _request_host_is_loopback(self) -> bool:
        raw_host = self.headers.get("Host", "")
        try:
            parsed = urlparse("//" + raw_host)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return False
            hostname = parsed.hostname
        except ValueError:
            return False
        if hostname == "localhost":
            return True
        if not hostname:
            return False
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _client_class(self) -> str:
        raw_origin = self.headers.get("Origin", "")
        if self._has_forwarding_headers():
            return "REMOTE_PROXY"
        if raw_origin:
            try:
                normalized = normalize_http_origin(raw_origin)
            except InvalidOriginError:
                return "REMOTE_BROWSER_INVALID_ORIGIN"
            return (
                "LOOPBACK_SAME_ORIGIN"
                if self._request_host_is_loopback()
                and self._origin_is_same_origin(normalized)
                else "REMOTE_BROWSER"
            )
        try:
            client_is_loopback = ipaddress.ip_address(
                self.client_address[0]
            ).is_loopback

        except ValueError:
            return "REMOTE_DIRECT"
        if client_is_loopback and self._request_host_is_loopback():
            return "LOOPBACK_DIRECT"
        return "REMOTE_DIRECT"

    def _is_remote_style(self) -> bool:
        return self._client_class() not in {
            "LOOPBACK_DIRECT",
            "LOOPBACK_SAME_ORIGIN",
        }

    def _audit_origin(self) -> str | None:
        raw_origin = self.headers.get("Origin", "")
        if not raw_origin:
            return None
        try:
            return normalize_http_origin(raw_origin)
        except InvalidOriginError:
            return "INVALID_ORIGIN"

    def _audit_event(
        self,
        *,
        event: str,
        outcome: str,
        path: str,
        status_code: int | None = None,
        required: bool = False,
    ) -> bool:
        audit = getattr(self.server, "audit_logger", None)
        if audit is None or not self._is_remote_style():
            return True
        try:
            audit.record(
                event=event,
                outcome=outcome,
                method=self.command,
                path=path,
                client_class=self._client_class(),
                request_id=self._request_identifier(),
                status_code=status_code,
                origin=self._audit_origin(),
            )
            return True
        except AuditWriteError:
            logger = getattr(self.server, "logger", None)
            if logger is not None:
                logger.exception("Remote audit write failed")
            return not required

    def _begin_remote_write(self, path: str) -> bool:
        if self.command not in {"POST", "PUT", "PATCH", "DELETE"}:
            return True
        if not self._is_remote_style():
            return True
        if not self._audit_event(
            event="REMOTE_WRITE",
            outcome="AUTHORIZED",
            path=path,
            required=True,
        ):
            self._send_json(
                {
                    "error": {
                        "code": "REMOTE_AUDIT_UNAVAILABLE",
                        "message": "remote writes are disabled until the local audit log is writable",
                    }
                },
                status=503,
            )
            return False
        self._audit_write_pending = True
        self._audit_write_path = path
        return True

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            close_all()

    # ---- 辅助 ----
    def _ctx(self) -> AppContext:
        return self.server.ctx

    def _origin_is_same_origin(self, normalized_origin: str) -> bool:
        parsed = urlparse(normalized_origin)
        if not parsed.hostname:
            return False
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_identity = _request_host_identity(
            self.headers.get("Host", ""),
            parsed.scheme,
        )
        return request_identity == (parsed.hostname.rstrip(".").lower(), origin_port)

    def _send_cors_headers(self, *, preflight: bool = False) -> None:
        origin = getattr(self, "_cors_origin", None)
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
        self.send_header(
            "Vary",
            (
                "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"
                if preflight
                else "Origin"
            ),
        )
        if preflight:
            self.send_header(
                "Access-Control-Allow-Methods",
                ", ".join(_CORS_ALLOWED_METHODS),
            )
            self.send_header(
                "Access-Control-Allow-Headers",
                _CORS_ALLOWED_HEADERS_VALUE,
            )
            self.send_header(
                "Access-Control-Max-Age",
                str(getattr(self.server, "cors_max_age_sec", 600)),
            )

    def _prepare_api_request(self, path: str) -> bool:
        """Validate cross-origin API access before authentication or routing."""

        self._cors_origin = None
        if not path.startswith("/api/"):
            return True
        raw_origin = self.headers.get("Origin", "")
        if not raw_origin:
            return True
        try:
            normalized_origin = normalize_http_origin(raw_origin)
        except InvalidOriginError:
            self._audit_event(
                event="CORS",
                outcome="INVALID_ORIGIN",
                path=path,
                status_code=403,
            )
            self._send_json(
                {
                    "error": {
                        "code": "CORS_ORIGIN_INVALID",
                        "message": "request Origin must be a strict HTTP(S) origin",
                    }
                },
                status=403,
            )
            return False
        if self._origin_is_same_origin(normalized_origin):
            return True
        allowed = getattr(self.server, "cors_allowed_origins", frozenset())
        if normalized_origin not in allowed:
            self._audit_event(
                event="CORS",
                outcome="ORIGIN_DENIED",
                path=path,
                status_code=403,
            )
            self._send_json(
                {
                    "error": {
                        "code": "CORS_ORIGIN_DENIED",
                        "message": "request Origin is not allowed",
                    }
                },
                status=403,
            )
            return False
        self._cors_origin = normalized_origin
        return True

    def _private_api_authorized(self, path: str) -> bool:
        return _private_api_access_allowed(
            path=path,
            client_host=self.client_address[0],
            request_host=self.headers.get("Host", ""),
            has_forwarding_headers=any(
                self.headers.get(name)
                for name in (
                    "Forwarded",
                    "X-Forwarded-For",
                    "X-Forwarded-Host",
                    "X-Real-IP",
                )
            ),
            request_origin=self.headers.get("Origin", ""),
            sec_fetch_site=self.headers.get("Sec-Fetch-Site", ""),
            authorization=self.headers.get("Authorization", ""),
            configured_access=os.environ.get(PRIVATE_ACCESS_ENV, ""),
        )

    def _require_private_api(self, path: str) -> bool:
        if self._private_api_authorized(path):
            return True
        configured_value = os.environ.get(PRIVATE_ACCESS_ENV, "")
        configured = private_access_value_valid(configured_value)
        misconfigured = bool(configured_value) and not configured
        code = (
            "PRIVATE_API_AUTH_REQUIRED"
            if configured
            else (
                "PRIVATE_API_MISCONFIGURED"
                if misconfigured
                else "PRIVATE_API_DISABLED"
            )
        )
        message = (
            "private API authorization is required"
            if configured
            else (
                f"private access must contain at least {MIN_PRIVATE_ACCESS_LENGTH} visible characters"
                if misconfigured
                else "private API is local-only until private access is configured"
            )
        )
        self._audit_event(
            event="PRIVATE_API_AUTH",
            outcome=code,
            path=path,
            status_code=401 if configured else 503,
        )
        self._send_json(
            {"error": {"code": code, "message": message}},
            status=401 if configured else 503,
        )
        return False

    def _send_json(self, obj: Any, status: int = 200) -> None:
        if getattr(self, "_audit_write_pending", False):
            self._audit_event(
                event="REMOTE_WRITE",
                outcome="SUCCEEDED" if status < 400 else "REJECTED",
                path=getattr(self, "_audit_write_path", ""),
                status_code=status,
            )
            self._audit_write_pending = False
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_identifier())
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self._request_identifier())
        self.end_headers()
        self.wfile.write(body)

    # ---- OPTIONS ----
    def do_OPTIONS(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send_json(
                {"error": {"code": "NOT_FOUND", "message": "not found"}},
                status=404,
            )
            return
        if not self.headers.get("Origin", ""):
            self._send_json(
                {
                    "error": {
                        "code": "CORS_PREFLIGHT_ORIGIN_REQUIRED",
                        "message": "CORS preflight requires Origin",
                    }
                },
                status=400,
            )
            return
        if not self._prepare_api_request(path):
            return

        requested_method = self.headers.get(
            "Access-Control-Request-Method",
            "",
        ).strip().upper()
        if requested_method not in _CORS_PREFLIGHT_METHODS:
            self._send_json(
                {
                    "error": {
                        "code": "CORS_PREFLIGHT_METHOD_DENIED",
                        "message": "requested method is not allowed",
                    }
                },
                status=403,
            )
            return

        requested_headers_raw = self.headers.get(
            "Access-Control-Request-Headers",
            "",
        )
        requested_headers: set[str] = set()
        if requested_headers_raw:
            for raw_header in requested_headers_raw.split(","):
                header = raw_header.strip().lower()
                if not header or re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", header) is None:
                    self._send_json(
                        {
                            "error": {
                                "code": "CORS_PREFLIGHT_HEADERS_DENIED",
                                "message": "requested headers are malformed",
                            }
                        },
                        status=403,
                    )
                    return
                requested_headers.add(header)
        if not requested_headers.issubset(_CORS_ALLOWED_HEADERS):
            self._send_json(
                {
                    "error": {
                        "code": "CORS_PREFLIGHT_HEADERS_DENIED",
                        "message": "requested headers are not allowed",
                    }
                },
                status=403,
            )
            return

        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_identifier())
        self._send_cors_headers(preflight=True)
        self.end_headers()

    # ---- GET ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        ctx = self._ctx()
        if not self._prepare_api_request(path):
            return
        if path == "/api/runtime/health":
            self._send_json(build_runtime_health(ctx))
            return
        if not self._require_private_api(path):
            return

        # SSE
        if path == "/api/stream":
            self._handle_sse(ctx)
            return

        if path == "/api/portfolio":
            self._call_json(H.get_portfolio, ctx)
            return

        # API 路由
        for prefix, fn in _GET_ROUTES:
            if path == prefix or path.startswith(prefix + "/"):
                try:
                    self._send_json(fn(ctx))
                except Exception:  # noqa: BLE001 - HTTP boundary logs and returns a fixed 500
                    self._internal_error()
                return

        m = _SIGNAL_RE.match(path)
        if m:
            sig = H.get_signal(ctx, m.group(1))
            if sig is None:
                self._send_json({"error": "signal not found"}, status=404)
            else:
                self._send_json(sig)
            return

        # 单标的详情：/api/quote/{symbol}
        m = _QUOTE_RE.match(path)
        if m:
            detail = H.get_quote_detail(ctx, m.group(1))
            if detail is None:
                self._send_json({"error": "invalid symbol"}, status=400)
            else:
                self._send_json(detail)
            return

        if getattr(self.server, "api_only", False):
            if path.startswith("/api/"):
                self._send_json(
                    {"error": {"code": "NOT_FOUND", "message": "not found"}},
                    status=404,
                )
            else:
                self._send_text("Not Found", "text/plain; charset=utf-8", status=404)
            return

        # 静态文件
        self._serve_static(ctx, path)

    # ---- POST ----
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        ctx = self._ctx()
        if not self._prepare_api_request(path):
            return
        if not self._require_private_api(path):
            return
        if not self._begin_remote_write(path):
            return
        if path == "/api/portfolio/positions":
            payload = self._read_strict_json()
            if payload is not None:
                self._call_json(H.post_portfolio_position, ctx, payload, status=201)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}

        if path == "/api/watch":
            action = payload.get("action", "add")
            symbol = payload.get("symbol", "")
            if not symbol:
                self._send_json({"error": "symbol required"}, status=400)
                return
            if action == "remove":
                self._send_json(H.post_watch_remove(ctx, symbol))
            else:
                self._send_json(H.post_watch_add(ctx, symbol, payload.get("market")))
            return
        if path == "/api/events":
            self._send_json(H.post_event_inject(ctx, payload))
            return

        self._send_json({"error": "not found"}, status=404)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if not self._prepare_api_request(path):
            return
        if not self._require_private_api(path):
            return
        if not self._begin_remote_write(path):
            return
        if path != "/api/portfolio/profile":
            self._send_json({"error": {"code": "NOT_FOUND", "message": "not found"}}, status=404)
            return
        payload = self._read_strict_json()
        if payload is not None:
            self._call_json(H.put_portfolio_profile, self._ctx(), payload)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if not self._prepare_api_request(path):
            return
        if not self._require_private_api(path):
            return
        if not self._begin_remote_write(path):
            return
        match = _PORTFOLIO_POSITION_RE.match(path)
        if match is None:
            self._send_json({"error": {"code": "NOT_FOUND", "message": "not found"}}, status=404)
            return
        payload = self._read_strict_json()
        if payload is not None:
            self._call_json(H.patch_portfolio_position, self._ctx(), match.group(1), payload)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not self._prepare_api_request(path):
            return
        if not self._require_private_api(path):
            return
        if not self._begin_remote_write(path):
            return
        match = _PORTFOLIO_POSITION_RE.match(path)
        if match is None:
            self._send_json({"error": {"code": "NOT_FOUND", "message": "not found"}}, status=404)
            return
        self._call_json(H.delete_portfolio_position, self._ctx(), match.group(1))

    def _discard_oversized_body(self, length: int) -> None:
        """Drain a bounded amount so Windows clients can receive the 413 response."""

        remaining = min(length, _MAX_OVERSIZE_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(8192, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        self.close_connection = True

    def _read_strict_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                {"error": {"code": "INVALID_JSON", "message": "invalid Content-Length"}},
                status=400,
            )
            return None
        if length < 0:
            self._send_json(
                {"error": {"code": "INVALID_JSON", "message": "invalid Content-Length"}},
                status=400,
            )
            return None
        if length > _MAX_JSON_BODY_BYTES:
            self._discard_oversized_body(length)
            self._send_json(
                {
                    "error": {
                        "code": "REQUEST_TOO_LARGE",
                        "message": f"JSON request body exceeds {_MAX_JSON_BODY_BYTES} bytes",
                    }
                },
                status=413,
            )
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            text = raw.decode("utf-8")
            payload = json.loads(
                text,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(
                {"error": {"code": "INVALID_JSON", "message": "request body must be valid UTF-8 JSON"}},
                status=400,
            )
            return None
        if type(payload) is not dict:
            self._send_json(
                {"error": {"code": "INVALID_JSON_OBJECT", "message": "JSON body must be an object"}},
                status=400,
            )
            return None
        return payload

    def _call_json(self, fn: Any, *args: Any, status: int = 200) -> None:
        try:
            self._send_json(fn(*args), status=status)
        except H.APIError as exc:
            self._send_json(exc.response(), status=exc.status)
        except Exception:  # noqa: BLE001 - HTTP boundary logs and returns a fixed 500
            self._internal_error()

    def _internal_error(self) -> None:
        logger = getattr(self.server, "logger", None)
        if logger is not None:
            logger.exception("Unhandled API error")
        self._send_json(
            {"error": {"code": "INTERNAL_ERROR", "message": "internal server error"}},
            status=500,
        )

    # ---- 静态托管 ----
    def _serve_static(self, ctx: AppContext, path: str) -> None:
        root = ctx.web_root
        rel = path.lstrip("/")
        if rel == "" or rel == "index.html":
            rel = "index.html"
        # 防目录穿越
        normalized_root = os.path.abspath(root)
        full = os.path.abspath(os.path.normpath(os.path.join(normalized_root, rel)))
        if os.path.commonpath((normalized_root, full)) != normalized_root:
            self._send_text("Forbidden", "text/plain; charset=utf-8", status=403)
            return
        if not os.path.isfile(full):
            # SPA 回退到 index.html
            full = os.path.join(root, "index.html")
            if not os.path.isfile(full):
                self._send_text("Not Found", "text/plain; charset=utf-8", status=404)
                return
        ctype, _ = mimetypes.guess_type(full)
        ctype = ctype or "application/octet-stream"
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_text("Not Found", "text/plain; charset=utf-8", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", self._request_identifier())
        self.end_headers()
        self.wfile.write(data)

    # ---- SSE ----
    def _handle_sse(self, ctx: AppContext) -> None:
        hub = ctx.sse_hub
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Request-ID", self._request_identifier())
        self._send_cors_headers()
        self.end_headers()

        q: queue.Queue = queue.Queue()
        hub.add_client(q)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not getattr(self.server, "_shutdown", None) or not self.server._shutdown.is_set():
                try:
                    topic, payload = q.get(timeout=15)
                except queue.Empty:
                    # 心跳保活
                    try:
                        self.wfile.write(b": hb\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    continue
                data = json.dumps(payload, ensure_ascii=False)
                frame = f"event: {topic}\ndata: {data}\n\n".encode()
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            hub.remove_client(q)


class APIServer(ThreadingHTTPServer):
    """HTTP 服务（持有 AppContext 与全局 shutdown 事件）。"""

    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        ctx: AppContext,
        logger: Any,
        *,
        allow_non_loopback: bool = False,
        api_only: bool = False,
        audit_logger: RemoteAuditLogger | None = None,
    ) -> None:
        safe_host = require_safe_bind(host, allow_non_loopback=allow_non_loopback)
        app_config = getattr(getattr(ctx, "bundle", None), "app", None)
        runtime_config = getattr(app_config, "runtime", None)
        raw_origins = getattr(runtime_config, "cors_allowed_origins", []) or []
        normalized_origins: set[str] = set()
        for raw_origin in raw_origins:
            try:
                normalized_origins.add(normalize_http_origin(raw_origin))
            except InvalidOriginError as exc:
                raise ValueError(f"invalid CORS allowlist origin: {raw_origin!r}") from exc
        raw_max_age = getattr(runtime_config, "cors_max_age_sec", 600)
        if isinstance(raw_max_age, bool) or not isinstance(raw_max_age, int):
            raise TypeError("cors_max_age_sec must be an integer")
        if raw_max_age < 0 or raw_max_age > 86400:
            raise ValueError("cors_max_age_sec must be between 0 and 86400")
        if type(api_only) is not bool:
            raise TypeError("api_only must be an actual boolean")

        resolved_audit = (
            audit_logger
            if audit_logger is not None
            else getattr(ctx, "audit_logger", None)
        )
        if resolved_audit is None:
            raw_database = getattr(getattr(ctx, "repo", None), "db_path", None)
            audit_enabled = getattr(runtime_config, "audit_enabled", False)
            if type(audit_enabled) is not bool:
                raise TypeError("audit_enabled must be an actual boolean")
            if type(raw_database) is str and raw_database and raw_database != ":memory:":
                fallback_path = Path(raw_database).resolve().parent / "remote_access_audit.jsonl"
            else:
                fallback_path = Path(os.devnull)
                audit_enabled = False
            resolved_audit = RemoteAuditLogger(
                fallback_path,
                enabled=audit_enabled,
                max_bytes=getattr(runtime_config, "audit_max_bytes", 5 * 1024 * 1024),
                backup_count=getattr(runtime_config, "audit_backup_count", 3),
            )
        if isinstance(resolved_audit, RemoteAuditLogger) and resolved_audit.enabled:
            resolved_audit.ensure_ready()

        super().__init__((safe_host, port), APIHandler)
        self.ctx = ctx
        self.logger = logger
        self.api_only = api_only
        self.audit_logger = resolved_audit
        self.cors_allowed_origins = frozenset(normalized_origins)
        self.cors_max_age_sec = raw_max_age
        self._shutdown = threading.Event()
        self.daemon_threads = True

    def shutdown_wait(self) -> None:
        self._shutdown.set()
        super().shutdown()
        self.server_close()
