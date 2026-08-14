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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import handlers as H
from .handlers import AppContext
from ..storage.db import close_all


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
    "/api/portfolio",
    "/api/portfolio/profile",
    "/api/portfolio/positions",
})
_PRIVATE_API_PREFIXES = ("/api/portfolio/positions/",)
_PRIVATE_ACCESS_ENV = "STOCK_TRACKER_PRIVATE_ACCESS"
_MAX_JSON_BODY_BYTES = 64 * 1024
_MAX_OVERSIZE_DRAIN_BYTES = 1024 * 1024


def _private_api_access_allowed(
    *,
    path: str,
    client_host: str,
    request_host: str,
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
    if client_is_loopback and request_host_is_loopback:
        return True
    if not configured_access:
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
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        if getattr(self.server, "logger", None) is not None:
            self.server.logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            close_all()

    # ---- 辅助 ----
    def _ctx(self) -> AppContext:
        return self.server.ctx

    @staticmethod
    def _is_private_api_path(path: str) -> bool:
        return path in _PRIVATE_API_PATHS or any(
            path.startswith(prefix) for prefix in _PRIVATE_API_PREFIXES
        )

    def _private_api_authorized(self, path: str) -> bool:
        return _private_api_access_allowed(
            path=path,
            client_host=self.client_address[0],
            request_host=self.headers.get("Host", ""),
            authorization=self.headers.get("Authorization", ""),
            configured_access=os.environ.get(_PRIVATE_ACCESS_ENV, ""),
        )

    def _require_private_api(self, path: str) -> bool:
        if self._private_api_authorized(path):
            return True
        configured = bool(os.environ.get(_PRIVATE_ACCESS_ENV, ""))
        self._send_json(
            {
                "error": {
                    "code": (
                        "PRIVATE_API_AUTH_REQUIRED"
                        if configured
                        else "PRIVATE_API_DISABLED"
                    ),
                    "message": (
                        "private API authorization is required"
                        if configured
                        else "private API is local-only until private access is configured"
                    ),
                }
            },
            status=401 if configured else 503,
        )
        return False

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        ctx = self._ctx()
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
                except Exception:
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

        # 静态文件
        self._serve_static(ctx, path)

    # ---- POST ----
    def do_POST(self) -> None:  # noqa: N802
        ctx = self._ctx()
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._require_private_api(path):
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

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._require_private_api(path):
            return
        if path != "/api/portfolio/profile":
            self._send_json({"error": {"code": "NOT_FOUND", "message": "not found"}}, status=404)
            return
        payload = self._read_strict_json()
        if payload is not None:
            self._call_json(H.put_portfolio_profile, self._ctx(), payload)

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._require_private_api(path):
            return
        match = _PORTFOLIO_POSITION_RE.match(path)
        if match is None:
            self._send_json({"error": {"code": "NOT_FOUND", "message": "not found"}}, status=404)
            return
        payload = self._read_strict_json()
        if payload is not None:
            self._call_json(H.patch_portfolio_position, self._ctx(), match.group(1), payload)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._require_private_api(path):
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
        except Exception:
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q: "queue.Queue" = queue.Queue()
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
                frame = f"event: {topic}\ndata: {data}\n\n".encode("utf-8")
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            hub.remove_client(q)


class APIServer(ThreadingHTTPServer):
    """HTTP 服务（持有 AppContext 与全局 shutdown 事件）。"""

    def __init__(self, host: str, port: int, ctx: AppContext, logger) -> None:
        super().__init__((host, port), APIHandler)
        self.ctx = ctx
        self.logger = logger
        self._shutdown = threading.Event()
        self.daemon_threads = True

    def shutdown_wait(self) -> None:
        self._shutdown.set()
        super().shutdown()
        self.server_close()
