"""Shared security-boundary helpers.

The private API access value is intentionally sourced from the process
environment only. Keep validation in one module so the HTTP server and the
Hybrid H0 bootstrap tooling cannot drift apart.
"""

from __future__ import annotations

PRIVATE_ACCESS_ENV = "STOCK_TRACKER_PRIVATE_ACCESS"
MIN_PRIVATE_ACCESS_LENGTH = 32


def private_access_value_valid(value: object) -> bool:
    """Return whether *value* is a strong, printable bearer secret."""

    return (
        type(value) is str
        and len(value) >= MIN_PRIVATE_ACCESS_LENGTH
        and value == value.strip()
        and not any(ord(char) < 33 or ord(char) == 127 for char in value)
    )
