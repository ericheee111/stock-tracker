"""Strict timezone helpers used by every quantitative data boundary."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from itertools import pairwise
from zoneinfo import ZoneInfo

from stock_tracker.core.types import Market


class TimeContractError(ValueError):
    """Raised when a timestamp would make point-in-time semantics ambiguous."""


_MARKET_TIMEZONES: dict[Market, str] = {
    Market.A: "Asia/Shanghai",
    Market.HK: "Asia/Hong_Kong",
    Market.US: "America/New_York",
}


def ensure_aware(value: datetime, name: str = "timestamp") -> datetime:
    """Return *value* after rejecting naive or invalid timezone information."""

    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeContractError(f"{name} must include an explicit timezone")
    return value


def to_utc(value: datetime, name: str = "timestamp") -> datetime:
    """Convert an explicitly-aware timestamp to UTC."""

    return ensure_aware(value, name).astimezone(timezone.utc)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(timezone.utc)


def market_timezone(market: Market | str) -> ZoneInfo:
    """Return the canonical exchange-local timezone for a supported market."""

    try:
        normalized = market if isinstance(market, Market) else Market(str(market).upper())
    except ValueError as exc:
        raise TimeContractError(f"unsupported market: {market!r}") from exc
    return ZoneInfo(_MARKET_TIMEZONES[normalized])


def exchange_local_date(value: datetime, market: Market | str) -> date:
    """Map an instant to its exchange-local civil date."""

    return ensure_aware(value).astimezone(market_timezone(market)).date()


def require_exchange_timezone(
    value: datetime,
    market: Market | str,
    name: str = "timestamp",
) -> datetime:
    """Require that *value* is represented in the exchange's named ZoneInfo."""

    aware = ensure_aware(value, name)
    expected = market_timezone(market)
    actual_key = getattr(aware.tzinfo, "key", None)
    if actual_key != expected.key:
        raise TimeContractError(
            f"{name} must use exchange timezone {expected.key}, "
            f"got {actual_key or aware.tzinfo}"
        )
    return aware


def assert_monotonic(
    values: Iterable[datetime],
    *,
    strictly: bool = False,
    name: str = "timestamps",
) -> tuple[datetime, ...]:
    """Validate an aware, chronological timestamp sequence."""

    normalized = tuple(to_utc(value, name) for value in values)
    for previous, current in pairwise(normalized):
        invalid = current <= previous if strictly else current < previous
        if invalid:
            qualifier = "strictly " if strictly else ""
            raise TimeContractError(f"{name} must be {qualifier}monotonic")
    return normalized
