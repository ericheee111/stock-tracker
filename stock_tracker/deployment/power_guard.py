"""Optional Windows sleep guard for active configured trading sessions.

The guard is disabled by default because keeping a personal workstation awake
is a host-level policy choice. When enabled, it requests system availability
only while at least one enabled market is inside a configured session and
releases that request immediately outside those sessions or on shutdown.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..core.timezones import TimezoneResolutionError, utc_to_market_local

ES_SYSTEM_REQUIRED = 0x00000001
ES_CONTINUOUS = 0x80000000


class PowerGuardError(RuntimeError):
    """Raised for an invalid or failed power-guard operation."""


def _market_config(bundle: Any, key: str) -> Any:
    return getattr(getattr(bundle, "markets", None), key, None)


def _session_bounds(session: object) -> tuple[int, int] | None:
    if not isinstance(session, (list, tuple)) or len(session) != 4:
        return None
    if any(type(value) is not int for value in session):
        return None
    start_hour, start_minute, end_hour, end_minute = session
    if not (
        0 <= start_hour <= 23
        and 0 <= end_hour <= 23
        and 0 <= start_minute <= 59
        and 0 <= end_minute <= 59
    ):
        return None
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return (start, end) if start <= end else None


def active_trading_markets(
    bundle: Any,
    *,
    now_utc: datetime | None = None,
) -> tuple[str, ...]:
    """Return enabled markets currently inside a configured local session."""

    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PowerGuardError("now_utc must be timezone-aware")
    enabled = getattr(getattr(bundle, "app", None), "markets_enabled", {}) or {}
    active: list[str] = []
    for key in ("a", "hk", "us"):
        if not enabled.get(key, False):
            continue
        config = _market_config(bundle, key)
        if config is None:
            continue
        try:
            local = utc_to_market_local(
                current,
                timezone_name=getattr(config, "timezone", "UTC"),
                fallback_offset_hours=getattr(config, "utc_offset_hours", 0),
            )
        except TimezoneResolutionError:
            continue
        if local.weekday() >= 5:
            continue
        minute = local.hour * 60 + local.minute
        for session in getattr(config, "trading_hours", ()) or ():
            bounds = _session_bounds(session)
            if bounds is not None and bounds[0] <= minute <= bounds[1]:
                active.append(key.upper())
                break
    return tuple(active)


def windows_set_thread_execution_state(flags: int) -> int:
    """Call the Windows power API lazily so non-Windows imports stay safe."""

    if os.name != "nt":
        raise PowerGuardError("SetThreadExecutionState is available only on Windows")
    if type(flags) is not int:
        raise PowerGuardError("execution-state flags must be an integer")
    import ctypes

    result = ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
    if not result:
        raise PowerGuardError("SetThreadExecutionState returned zero")
    return int(result)


class TradingPowerGuard:
    """Daemon guard that keeps Windows awake only during trading sessions."""

    def __init__(
        self,
        bundle: Any,
        logger: Any,
        *,
        enabled: bool,
        interval_sec: int,
        platform_name: str | None = None,
        set_state: Callable[[int], int] = windows_set_thread_execution_state,
    ) -> None:
        if type(enabled) is not bool:
            raise TypeError("power guard enabled must be an actual boolean")
        if type(interval_sec) is not int or not 15 <= interval_sec <= 3600:
            raise ValueError("power guard interval_sec must be in 15..3600")
        self.bundle = bundle
        self.logger = logger
        self.enabled = enabled
        self.interval_sec = interval_sec
        self.platform_name = platform_name or os.name
        self.set_state = set_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._awake = False
        self._active_markets: tuple[str, ...] = ()
        self._last_error: str | None = None

    def _apply(self, active: tuple[str, ...]) -> None:
        desired_awake = bool(active)
        if desired_awake == self._awake and active == self._active_markets:
            return
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if desired_awake else 0)
        self.set_state(flags)
        self._awake = desired_awake
        self._active_markets = active
        self._last_error = None
        if self.logger is not None:
            self.logger.info(
                "H3 power guard | awake=%s | active_markets=%s",
                desired_awake,
                ",".join(active) or "none",
            )

    def tick(self, *, now_utc: datetime | None = None) -> tuple[str, ...]:
        if not self.enabled or self.platform_name != "nt":
            return ()
        active = active_trading_markets(self.bundle, now_utc=now_utc)
        try:
            self._apply(active)
        except Exception as exc:  # noqa: BLE001 - host API failure must not crash Engine
            self._last_error = type(exc).__name__
            if self.logger is not None:
                self.logger.warning("H3 power guard update failed: %s", self._last_error)
        return active

    def _release(self) -> None:
        if self.platform_name != "nt":
            return
        try:
            self.set_state(ES_CONTINUOUS)
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - shutdown cleanup is best effort
            self._last_error = type(exc).__name__
            if self.logger is not None:
                self.logger.warning("H3 power guard release failed: %s", self._last_error)
        self._awake = False
        self._active_markets = ()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval_sec)
        self._release()

    def start(self) -> bool:
        if not self.enabled or self.platform_name != "nt":
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hybrid-h3-power-guard",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, min(10.0, self.interval_sec / 2)))
        elif self._awake:
            self._release()
        self._thread = None

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": "stock-tracker-hybrid-h3-power-guard-v1",
            "enabled": self.enabled,
            "platform_supported": self.platform_name == "nt",
            "running": bool(self._thread and self._thread.is_alive()),
            "awake_requested": self._awake,
            "active_markets": list(self._active_markets),
            "last_error": self._last_error,
        }


__all__ = [
    "ES_CONTINUOUS",
    "ES_SYSTEM_REQUIRED",
    "PowerGuardError",
    "TradingPowerGuard",
    "active_trading_markets",
    "windows_set_thread_execution_state",
]
