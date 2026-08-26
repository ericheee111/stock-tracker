"""Metadata-only runtime health contract for Hybrid H1/H2/H3."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .. import __version__
from ..core import types as T
from ..core.timezones import TimezoneResolutionError, market_local_to_utc
from . import serializers as S

RUNTIME_HEALTH_SCHEMA = "hybrid-runtime-v1"


def _machine_timezone():
    value = datetime.now(timezone.utc).astimezone().tzinfo
    return value if value is not None else timezone.utc


def _iso(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=_machine_timezone())
    return value.astimezone(timezone.utc).isoformat()


def _latest_datetime(values: list[object]) -> datetime | None:
    candidates = [value for value in values if isinstance(value, datetime)]
    if not candidates:
        return None

    def comparable(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=_machine_timezone()).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)

    return max(candidates, key=comparable)


def _market_config(ctx: Any, quote: Any) -> Any:
    market = getattr(quote, "market", None)
    markets = getattr(getattr(ctx, "bundle", None), "markets", None)
    if market is T.Market.A:
        return getattr(markets, "a", None)
    if market is T.Market.HK:
        return getattr(markets, "hk", None)
    if market is T.Market.US:
        return getattr(markets, "us", None)
    return None


def _quote_datetime_utc(
    ctx: Any,
    quote: Any,
    value: object,
    *,
    market_time: bool,
) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    if not market_time:
        return value.replace(tzinfo=_machine_timezone()).astimezone(timezone.utc)

    market_config = _market_config(ctx, quote)
    try:
        return market_local_to_utc(
            value,
            timezone_name=getattr(market_config, "timezone", "UTC"),
            fallback_offset_hours=getattr(market_config, "utc_offset_hours", 0),
        )
    except TimezoneResolutionError:
        return None


def _quote_age_ms_now(ctx: Any, quote: Any) -> int:
    """Recompute quote age using current time and configured market timezone."""

    value = getattr(quote, "timestamp", None)
    normalized = None
    if isinstance(value, datetime) and value.year >= 2000:
        normalized = _quote_datetime_utc(ctx, quote, value, market_time=True)
    if normalized is None:
        normalized = _quote_datetime_utc(
            ctx,
            quote,
            getattr(quote, "received_at", None),
            market_time=False,
        )
    if normalized is None:
        return max(0, int(getattr(quote, "observed_age_ms", 0) or 0))
    return max(
        0,
        int((datetime.now(timezone.utc) - normalized).total_seconds() * 1000),
    )


def _data_status(ctx: Any, quotes: list[Any]) -> str:
    if not quotes:
        return "UNKNOWN"
    severity = {"LIVE": 0, "DELAYED": 1, "UNKNOWN": 2, "STALE": 3}
    statuses: set[str] = set()
    for quote in quotes:
        stored = getattr(getattr(quote, "data_status", None), "value", "UNKNOWN")
        recomputed = S.quote_data_status(
            _quote_age_ms_now(ctx, quote),
            _market_config(ctx, quote),
        ).value
        statuses.add(
            max((stored, recomputed), key=lambda value: severity.get(value, 2))
        )
    if "STALE" in statuses:
        return "STALE"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    if "DELAYED" in statuses:
        return "DELAYED"
    return "LIVE"


def _provider_summary(ctx: Any) -> dict[str, int]:
    summary = {"count": 0, "closed": 0, "half_open": 0, "open": 0}
    try:
        healths = list(ctx.router.health_list())
    except Exception:  # noqa: BLE001 - health must remain metadata-only and fail soft
        return summary
    summary["count"] = len(healths)
    for health in healths:
        state = getattr(getattr(health, "circuit_state", None), "value", "").upper()
        if state == "CLOSED":
            summary["closed"] += 1
        elif state == "HALF_OPEN":
            summary["half_open"] += 1
        else:
            # OPEN and unknown values are both conservative failures.
            summary["open"] += 1
    return summary


def _scheduler_state(ctx: Any) -> str:
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None:
        return "NOT_ATTACHED"
    stop = getattr(scheduler, "_stop", None)
    if stop is not None and callable(getattr(stop, "is_set", None)) and stop.is_set():
        return "STOPPED"
    threads = list(getattr(scheduler, "_threads", []) or [])
    if any(
        callable(getattr(thread, "is_alive", None)) and thread.is_alive()
        for thread in threads
    ):
        return "RUNNING"
    return "STARTING"


def _database_state(ctx: Any) -> str:
    path = getattr(getattr(ctx, "repo", None), "db_path", None)
    if path == ":memory:":
        return "READY"
    if type(path) is not str or not path:
        return "UNAVAILABLE"
    if os.path.isfile(path) and os.access(path, os.R_OK):
        return "READY"
    return "MISSING"


def build_runtime_health(ctx: Any) -> dict[str, Any]:
    """Return safe process/runtime metadata without touching an upstream provider."""

    now = datetime.now(timezone.utc)
    runtime = ctx.bundle.app.runtime
    quotes = list(ctx.store.get_quotes().values())
    status = _data_status(ctx, quotes)
    provider_summary = _provider_summary(ctx)
    scheduler_state = _scheduler_state(ctx)
    database_state = _database_state(ctx)

    if status == "STALE":
        overall = "STALE"
    elif (
        status in {"DELAYED", "UNKNOWN"}
        or provider_summary["count"] == 0
        or (
            provider_summary["closed"]
            + provider_summary["half_open"]
            + provider_summary["open"]
            != provider_summary["count"]
        )
        or provider_summary["half_open"] > 0
        or provider_summary["open"] > 0
        or scheduler_state != "RUNNING"
        or database_state != "READY"
    ):
        overall = "DEGRADED"
    else:
        overall = "ONLINE"

    collection_times = [
        _quote_datetime_utc(
            ctx,
            quote,
            getattr(quote, "received_at", None),
            market_time=False,
        )
        for quote in quotes
    ]
    data_times = [
        _quote_datetime_utc(
            ctx,
            quote,
            getattr(quote, "timestamp", None),
            market_time=True,
        )
        for quote in quotes
    ]

    return {
        "schema_version": RUNTIME_HEALTH_SCHEMA,
        "status": overall,
        "engine_id": runtime.engine_id,
        "engine_version": __version__,
        "commit_id": runtime.commit_id,
        "deployment_mode": runtime.deployment_mode,
        "started_at": _iso(getattr(ctx, "started_at", None)),
        "last_heartbeat_at": now.isoformat(),
        "last_collection_at": _iso(_latest_datetime(collection_times)),
        "data_as_of": _iso(_latest_datetime(data_times)),
        "data_status": status,
        "scheduler_state": scheduler_state,
        "provider_summary": provider_summary,
        "database_state": database_state,
        "sse_available": getattr(ctx, "sse_hub", None) is not None,
        "api_major": runtime.api_major,
    }
