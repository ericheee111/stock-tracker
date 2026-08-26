"""Operational timezone resolution with deterministic standard-library fallbacks.

``zoneinfo`` remains authoritative when IANA data is available. Windows Python
installations do not always bundle that database, so a narrowly scoped
US/Eastern fallback is provided for runtime freshness and power-management
metadata. Formal PIT research calendars keep their independent contracts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_US_EASTERN_NAMES = frozenset({"America/New_York", "US/Eastern"})


class TimezoneResolutionError(ValueError):
    """Raised when an operational timezone input cannot be interpreted safely."""


def _first_weekday_date(year: int, month: int, weekday: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7)


def _us_eastern_transition_dates(year: int) -> tuple[date, date]:
    second_sunday_march = _first_weekday_date(year, 3, 6) + timedelta(days=7)
    first_sunday_november = _first_weekday_date(year, 11, 6)
    return second_sunday_march, first_sunday_november


def _us_eastern_offset_for_local(value: datetime) -> timedelta:
    """Return the UTC offset for one naive US/Eastern wall-clock timestamp.

    During the repeated 01:00 hour at the November transition, ``fold=0`` is
    interpreted as daylight time and ``fold=1`` as standard time, matching the
    PEP 495 convention.
    """

    local = value.replace(tzinfo=None)
    start_date, end_date = _us_eastern_transition_dates(local.year)
    if local.date() < start_date or local.date() > end_date:
        return timedelta(hours=-5)
    if start_date < local.date() < end_date:
        return timedelta(hours=-4)
    if local.date() == start_date:
        return timedelta(hours=-5 if local.hour < 2 else -4)
    if local.hour < 1:
        return timedelta(hours=-4)
    if local.hour >= 2:
        return timedelta(hours=-5)
    return timedelta(hours=-5 if local.fold else -4)


def _us_eastern_offset_for_utc(value: datetime) -> timedelta:
    current = value.astimezone(timezone.utc)
    start_date, end_date = _us_eastern_transition_dates(current.year)
    start_utc = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        7,
        tzinfo=timezone.utc,
    )
    end_utc = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        6,
        tzinfo=timezone.utc,
    )
    return timedelta(hours=-4 if start_utc <= current < end_utc else -5)


def _fixed_timezone(offset_hours: object) -> timezone:
    if isinstance(offset_hours, bool) or not isinstance(offset_hours, (int, float)):
        raise TimezoneResolutionError("UTC offset must be numeric")
    try:
        return timezone(timedelta(hours=float(offset_hours)))
    except (OverflowError, ValueError) as exc:
        raise TimezoneResolutionError("UTC offset is outside the supported range") from exc


def resolve_zoneinfo(name: object) -> ZoneInfo | None:
    """Resolve a trimmed IANA timezone name, returning ``None`` if unavailable."""

    if type(name) is not str or not name or name != name.strip():
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def market_local_to_utc(
    value: datetime,
    *,
    timezone_name: object,
    fallback_offset_hours: object,
) -> datetime:
    """Interpret a market-local timestamp and return an aware UTC value."""

    if not isinstance(value, datetime):
        raise TimezoneResolutionError("value must be datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    zone = resolve_zoneinfo(timezone_name)
    if zone is not None:
        return value.replace(tzinfo=zone).astimezone(timezone.utc)
    if timezone_name in _US_EASTERN_NAMES:
        offset = _us_eastern_offset_for_local(value)
        return value.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
    return value.replace(tzinfo=_fixed_timezone(fallback_offset_hours)).astimezone(
        timezone.utc
    )


def utc_to_market_local(
    value: datetime,
    *,
    timezone_name: object,
    fallback_offset_hours: object,
) -> datetime:
    """Convert an aware timestamp to an aware market-local value."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TimezoneResolutionError("UTC value must be timezone-aware")
    current = value.astimezone(timezone.utc)
    zone = resolve_zoneinfo(timezone_name)
    if zone is not None:
        return current.astimezone(zone)
    if timezone_name in _US_EASTERN_NAMES:
        return current.astimezone(timezone(_us_eastern_offset_for_utc(current)))
    return current.astimezone(_fixed_timezone(fallback_offset_hours))


__all__ = [
    "TimezoneResolutionError",
    "market_local_to_utc",
    "resolve_zoneinfo",
    "utc_to_market_local",
]
