"""Network binding safety helpers for local-first deployment."""

from __future__ import annotations

import ipaddress


class UnsafeBindError(ValueError):
    """Raised when a non-loopback bind lacks explicit acknowledgement."""


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
