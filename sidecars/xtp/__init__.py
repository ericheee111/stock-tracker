"""Read-only XTP quote sidecar package.

The package is deliberately compatible with CPython 3.9 and contains no order,
trader, or algorithm-order surface. The stock-tracker core communicates with it
only through the loopback JSON protocol defined in :mod:`sidecars.xtp.contracts`.
"""

from .contracts import (
    EVENT_SCHEMA,
    HEALTH_SCHEMA,
    METRICS_SCHEMA,
    SESSION_SCHEMA,
    EventEnvelope,
    XtpSidecarContractError,
)

__all__ = [
    "EVENT_SCHEMA",
    "HEALTH_SCHEMA",
    "METRICS_SCHEMA",
    "SESSION_SCHEMA",
    "EventEnvelope",
    "XtpSidecarContractError",
]
