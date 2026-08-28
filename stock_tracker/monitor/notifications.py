"""Bounded browser/webhook notification outbox delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from sidecars.xtp.contracts import canonical_json_bytes

from .repository import MonitorRepository, MonitorRepositoryError

_WEBHOOK_URL_ENV = "STOCK_TRACKER_MONITOR_WEBHOOK_URL"
_WEBHOOK_SIGNING_ENV = "STOCK_TRACKER_MONITOR_WEBHOOK_SIGNING_KEY"
_MAX_WEBHOOK_BODY = 16 * 1024
_MAX_ATTEMPTS = 3


class NotificationDeliveryError(RuntimeError):
    """Raised when a configured notification boundary is invalid."""


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None  # noqa: PLR1711, RET501


def _visible_secret(value: str | None, name: str, minimum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not minimum <= len(value) <= 4096:
        raise NotificationDeliveryError(f"{name} is missing or invalid")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise NotificationDeliveryError(f"{name} contains invalid characters")
    return value


def _webhook_url(value: str | None, allowed_origins: tuple[str, ...]) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 2048
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise NotificationDeliveryError("webhook URL is missing or invalid")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise NotificationDeliveryError("webhook URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.fragment
        or parsed.query
    ):
        raise NotificationDeliveryError("webhook URL must use exact HTTPS without query or fragment")
    host = hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise NotificationDeliveryError("webhook URL host is invalid") from exc
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise NotificationDeliveryError("webhook URL must target a global IP")
    origin = f"https://{host}"
    if port not in (None, 443):
        origin += f":{port}"
    if origin not in allowed_origins:
        raise NotificationDeliveryError("webhook origin is not allowlisted")
    return value


class NotificationDispatcher:
    def __init__(
        self,
        repository: MonitorRepository,
        *,
        webhook_enabled: bool = False,
        webhook_allowed_origins: tuple[str, ...] = (),
        browser_publisher: Callable[[str, dict[str, Any]], None] | None = None,
        opener: Any | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        if type(webhook_enabled) is not bool:
            raise NotificationDeliveryError("webhook_enabled must be boolean")
        self.repository = repository
        self.webhook_enabled = webhook_enabled
        self.webhook_allowed_origins = webhook_allowed_origins
        self.browser_publisher = browser_publisher
        self.environ = os.environ if environ is None else environ
        self._opener = opener or urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            urllib_request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirectHandler(),
        )

    def dispatch_pending(self, *, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise NotificationDeliveryError("notification dispatch limit must be 1-100")
        delivered = 0
        failed = 0
        disabled = 0
        for item in self.repository.claim_pending_outbox(limit=limit):
            channel = item["channel"]
            try:
                if channel == "BROWSER":
                    if self.browser_publisher is None:
                        self.repository.mark_outbox(
                            item["outbox_id"],
                            state="DISABLED",
                            attempts=item["attempts"],
                            expected_state="SENDING",
                        )
                        disabled += 1
                        continue
                    try:
                        self.browser_publisher(
                            "monitor.notification",
                            {
                                "schema": "stock-tracker-monitor-browser-notification-v1",
                                "payload": item["payload"],
                            },
                        )
                    except Exception as exc:
                        raise NotificationDeliveryError("browser notification publisher failed") from exc
                    self.repository.mark_outbox(
                        item["outbox_id"],
                        state="DELIVERED",
                        attempts=item["attempts"],
                        expected_state="SENDING",
                    )
                    delivered += 1
                elif channel == "WEBHOOK":
                    if not self.webhook_enabled:
                        self.repository.mark_outbox(
                            item["outbox_id"],
                            state="DISABLED",
                            attempts=item["attempts"],
                            expected_state="SENDING",
                        )
                        disabled += 1
                    else:
                        self._deliver_webhook(item["payload"])
                        self.repository.mark_outbox(
                            item["outbox_id"],
                            state="DELIVERED",
                            attempts=item["attempts"] + 1,
                            expected_state="SENDING",
                        )
                        delivered += 1
                else:
                    raise NotificationDeliveryError("unknown notification channel")
            except (NotificationDeliveryError, MonitorRepositoryError, urllib_error.URLError, OSError):
                attempts = int(item["attempts"]) + 1
                terminal = attempts >= _MAX_ATTEMPTS
                self.repository.mark_outbox(
                    item["outbox_id"],
                    state="FAILED" if terminal else "PENDING",
                    attempts=attempts,
                    next_attempt_at=None
                    if terminal
                    else datetime.now(timezone.utc) + timedelta(seconds=30 * attempts),
                    expected_state="SENDING",
                )
                failed += 1
        return {
            "schema": "stock-tracker-monitor-dispatch-v1",
            "delivered": delivered,
            "failed": failed,
            "disabled": disabled,
            "retry_storm_prevented": True,
            "max_attempts": _MAX_ATTEMPTS,
        }

    def _deliver_webhook(self, payload: dict[str, Any]) -> None:
        url = _webhook_url(
            self.environ.get(_WEBHOOK_URL_ENV),
            self.webhook_allowed_origins,
        )
        signing = _visible_secret(
            self.environ.get(_WEBHOOK_SIGNING_ENV),
            _WEBHOOK_SIGNING_ENV,
            32,
        )
        body = canonical_json_bytes(payload)
        if len(body) > _MAX_WEBHOOK_BODY:
            raise NotificationDeliveryError("webhook payload exceeds size limit")
        signature = hmac.new(signing.encode("utf-8"), body, hashlib.sha256).hexdigest()
        request = urllib_request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "stock-tracker/monitor-webhook-v1",
                "X-Stock-Tracker-Signature": "sha256=" + signature,
            },
            method="POST",
        )
        with self._opener.open(request, timeout=5.0) as response:
            try:
                final = urlparse(response.geturl())
                expected = urlparse(url)
                final_host = final.hostname
                expected_host = expected.hostname
                final_port = final.port
                expected_port = expected.port
            except ValueError as exc:
                raise NotificationDeliveryError("webhook response URL is malformed") from exc
            if (
                final.scheme != expected.scheme
                or final_host != expected_host
                or final_port != expected_port
                or final.path != expected.path
                or final.params != expected.params
                or final.query != expected.query
                or final.fragment != expected.fragment
            ):
                raise NotificationDeliveryError("webhook response URL changed from the request")
            if not 200 <= response.status < 300:
                raise NotificationDeliveryError(f"webhook returned HTTP {response.status}")
            if len(response.read(1025)) > 1024:
                raise NotificationDeliveryError("webhook response exceeds size limit")
