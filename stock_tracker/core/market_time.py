from __future__ import annotations

from datetime import date, datetime

from stock_tracker.core.timezones import utc_to_market_local
from stock_tracker.core.types import Market

MARKET_SESSION_LABEL_POLICY_V1 = "stage4g1-market-session-label-v1"

_MARKET_TIMEZONE = {
    Market.A: ("Asia/Shanghai", 8),
    Market.HK: ("Asia/Hong_Kong", 8),
    Market.US: ("America/New_York", -5),
}


def market_session_date(
    timestamp: datetime,
    market: Market,
    session_label_policy_id: str,
) -> date:
    if type(timestamp) is not datetime or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if type(market) is not Market:
        raise ValueError("market must be the exact Market type")
    if session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
        raise ValueError("unsupported market session label policy")
    timezone_name, fallback_offset = _MARKET_TIMEZONE[market]
    return utc_to_market_local(
        timestamp,
        timezone_name=timezone_name,
        fallback_offset_hours=fallback_offset,
    ).date()


__all__ = ["MARKET_SESSION_LABEL_POLICY_V1", "market_session_date"]
