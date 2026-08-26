"""Network binding safety helpers for local-first deployment."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class UnsafeBindError(ValueError):
    """Raised when a non-loopback bind lacks explicit acknowledgement."""


class InvalidOriginError(ValueError):
    """Raised when a configured/request Origin is not a strict HTTP(S) origin."""


def is_loopback_host(host: object) -> bool:
    """Return whether *host* is an explicit loopback address/name."""

    if type(host) is not str:
        return False
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_safe_bind(host: object, *, allow_non_loopback: bool) -> str:
    """Validate a bind host and fail closed for LAN/public listeners."""

    if type(host) is not str or not host.strip() or host != host.strip():
        raise UnsafeBindError(
            "server host must be a non-empty string without surrounding whitespace"
        )
    if type(allow_non_loopback) is not bool:
        raise UnsafeBindError("allow_non_loopback must be an actual boolean")
    if not is_loopback_host(host) and not allow_non_loopback:
        raise UnsafeBindError(
            "refusing non-loopback bind; pass --allow-non-loopback only for an "
            "explicitly reviewed PURE_CLOUD_EXPERIMENTAL deployment"
        )
    return host


def normalize_http_origin(value: object) -> str:
    """Return a canonical HTTP(S) origin or fail closed.

    Origins are metadata, not arbitrary URLs. This helper rejects userinfo,
    paths, queries, fragments, whitespace/control characters and non-HTTP
    schemes so CORS and browser runtime configuration share one exact shape.
    """

    if type(value) is not str or not value or value != value.strip():
        raise InvalidOriginError("origin must be a non-empty trimmed string")
    if value.lower() == "null" or "\\" in value:
        raise InvalidOriginError("null/backslash origins are not allowed")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise InvalidOriginError("origin must not contain whitespace or control characters")

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise InvalidOriginError("origin scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidOriginError("origin must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InvalidOriginError("origin must not contain path, query, or fragment")
    hostname = parsed.hostname
    if not hostname:
        raise InvalidOriginError("origin must contain a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidOriginError("origin contains an invalid port") from exc

    normalized_host = hostname.rstrip(".").lower()
    if not normalized_host:
        raise InvalidOriginError("origin hostname must not be empty")
    if scheme == "http" and not is_loopback_host(normalized_host):
        raise InvalidOriginError("remote origins must use https; http is loopback-only")
    try:
        normalized_host = normalized_host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidOriginError("origin hostname is not valid IDNA") from exc
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"

    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in (None, default_port) else f":{port}"
    return f"{scheme}://{normalized_host}{port_suffix}"
