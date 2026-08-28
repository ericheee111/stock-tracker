# ruff: noqa: UP045
"""Loopback-only read-only HTTP server for the XTP quote sidecar."""

from __future__ import annotations

import hmac
import ipaddress
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .contracts import XtpSidecarContractError, canonical_json_bytes, validate_access
from .runtime import SidecarRuntime

_MAX_REQUEST_TARGET = 2048
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_AUTH_PREFIX = "Bearer "


class XtpSidecarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        runtime: SidecarRuntime,
        *,
        access_value: str,
        health_public: bool = True,
    ) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise XtpSidecarContractError("sidecar bind host must be a literal loopback IP") from exc
        if address.version != 4 or not address.is_loopback:
            raise XtpSidecarContractError(
                "sidecar must bind to a literal IPv4 loopback address"
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise XtpSidecarContractError(
                "sidecar port must be 0 for an ephemeral test port or 1-65535"
            )
        self.runtime = runtime
        self.access_value = validate_access(access_value)
        self.health_public = health_public is True
        super().__init__((host, port), XtpSidecarRequestHandler)


class XtpSidecarRequestHandler(BaseHTTPRequestHandler):
    server_version = "StockTrackerXtpSidecar/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args

    def _server(self) -> XtpSidecarHTTPServer:
        server = self.server
        if not isinstance(server, XtpSidecarHTTPServer):
            raise TypeError("unexpected server type")
        return server

    def _send_json(self, value: Any, status: int = 200) -> None:
        try:
            body = canonical_json_bytes(value)
        except XtpSidecarContractError:
            body = b'{"error":{"code":"INTERNAL_CONTRACT_ERROR","message":"response contract failed"}}'
            status = 500
        if len(body) > _MAX_RESPONSE_BYTES:
            body = b'{"error":{"code":"RESPONSE_TOO_LARGE","message":"response exceeded limit"}}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_json({"error": {"code": code, "message": message}}, status=status)

    def _authorized(self) -> bool:
        configured = self._server().access_value
        raw = self.headers.get("Authorization", "")
        if not raw.startswith(_AUTH_PREFIX):
            return False
        candidate = raw[len(_AUTH_PREFIX) :]
        try:
            validate_access(candidate)
        except XtpSidecarContractError:
            return False
        return hmac.compare_digest(candidate, configured)

    @staticmethod
    def _single_int(query: dict[str, list[str]], name: str, default: int) -> Optional[int]:
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1 or re.fullmatch(r"[0-9]{1,10}", values[0]) is None:
            return None
        return int(values[0])

    def do_GET(self) -> None:
        if len(self.path) > _MAX_REQUEST_TARGET:
            self._send_error(414, "REQUEST_TARGET_TOO_LONG", "request target exceeded limit")
            return
        parsed = urlparse(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.params:
            self._send_error(400, "INVALID_REQUEST_TARGET", "request target is invalid")
            return
        path = parsed.path
        if path != "/v1/events" and parsed.query:
            self._send_error(400, "UNKNOWN_QUERY_FIELD", "endpoint accepts no query fields")
            return
        runtime = self._server().runtime
        if path == "/v1/health" and self._server().health_public:
            self._send_json(runtime.health())
            return
        if not self._authorized():
            self._send_error(401, "SIDECAR_ACCESS_REQUIRED", "valid sidecar access is required")
            return
        if path == "/v1/health":
            self._send_json(runtime.health())
            return
        if path == "/v1/session":
            self._send_json(runtime.session())
            return
        if path == "/v1/metrics":
            self._send_json(runtime.metrics())
            return
        if path == "/v1/events":
            try:
                query = parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=False,
                    max_num_fields=4,
                )
            except ValueError:
                self._send_error(
                    400,
                    "TOO_MANY_QUERY_FIELDS",
                    "query contains too many fields",
                )
                return
            if set(query) - {"after", "limit"}:
                self._send_error(400, "UNKNOWN_QUERY_FIELD", "query contains unknown field")
                return
            after = self._single_int(query, "after", 0)
            limit = self._single_int(query, "limit", 200)
            if after is None or limit is None:
                self._send_error(400, "INVALID_CURSOR", "after and limit must be bounded integers")
                return
            try:
                payload = runtime.events_after(after, limit)
            except XtpSidecarContractError as exc:
                self._send_error(400, "INVALID_CURSOR", str(exc))
                return
            self._send_json(payload)
            return
        self._send_error(404, "NOT_FOUND", "not found")

    def do_HEAD(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_error(405, "READ_ONLY_SIDECAR", "sidecar exposes GET endpoints only")
