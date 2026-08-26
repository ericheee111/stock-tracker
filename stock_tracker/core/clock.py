"""Operational trading-session checks using configured market timezones.

The runtime clock prefers the standard-library ``zoneinfo`` database and uses
``core.timezones`` fallbacks when a Windows Python installation lacks IANA data.
Formal PIT research calendars remain separate and stricter.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import types as T
from .config import ConfigBundle, MarketConfig
from .timezones import TimezoneResolutionError, utc_to_market_local


def _market_local_now(market_cfg: MarketConfig) -> datetime:
    """Convert current UTC into the configured market-local clock."""

    try:
        return utc_to_market_local(
            datetime.now(timezone.utc),
            timezone_name=market_cfg.timezone,
            fallback_offset_hours=market_cfg.utc_offset_hours,
        )
    except TimezoneResolutionError:
        return datetime.now(timezone.utc)


def _in_session(market_cfg: MarketConfig, dt: datetime) -> bool:
    """Return whether market-local ``dt`` is inside a configured weekday session."""

    if dt.weekday() >= 5:
        return False
    cur_min = dt.hour * 60 + dt.minute
    for session in market_cfg.trading_hours:
        if not isinstance(session, (list, tuple)) or len(session) != 4:
            continue
        if any(type(value) is not int for value in session):
            continue
        start_hour, start_minute, end_hour, end_minute = session
        if not (
            0 <= start_hour <= 23
            and 0 <= end_hour <= 23
            and 0 <= start_minute <= 59
            and 0 <= end_minute <= 59
        ):
            continue
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start <= cur_min <= end:
            return True
    return False


def is_trading_now(bundle: ConfigBundle, market: T.Market) -> bool:
    """Return whether ``market`` is currently inside a configured session."""

    market_config = _market_cfg(bundle, market)
    return _in_session(market_config, _market_local_now(market_config))


def session_of(bundle: ConfigBundle, market: T.Market) -> str:
    """Return ``TRADING``, ``CLOSED``, or ``WEEKEND``."""

    market_config = _market_cfg(bundle, market)
    now_local = _market_local_now(market_config)
    if now_local.weekday() >= 5:
        return "WEEKEND"
    return "TRADING" if _in_session(market_config, now_local) else "CLOSED"


def market_open_status(bundle: ConfigBundle) -> dict[str, str]:
    """Return enabled A/HK/US market-session states."""

    output: dict[str, str] = {}
    for key, market in (("a", T.Market.A), ("hk", T.Market.HK), ("us", T.Market.US)):
        if bundle.app.markets_enabled.get(key, False):
            output[key] = session_of(bundle, market)
        else:
            output[key] = "DISABLED"
    return output


def _market_cfg(bundle: ConfigBundle, market: T.Market) -> MarketConfig:
    return {
        "A": bundle.markets.a,
        "HK": bundle.markets.hk,
        "US": bundle.markets.us,
    }[market.value]
