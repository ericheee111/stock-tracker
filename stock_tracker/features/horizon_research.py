"""N4b declared-input weekly facts and purged time-split manifests.

No market calendar is guessed. These objects describe input claims, not an
Authority: a caller cannot turn a source reference into verified PIT evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from ..core.types import Market

ZONES = {Market.A: "Asia/Shanghai", Market.HK: "Asia/Hong_Kong", Market.US: "America/New_York"}


class HorizonResearchError(ValueError):
    pass


class HoldingPurpose(StrEnum):
    SWING = "SWING"
    LONG_TERM = "LONG_TERM"
    SHORT_TERM = "SHORT_TERM"


def _time(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise HorizonResearchError("AWARE_TIME_REQUIRED")
    return value.astimezone(UTC)


def _security(symbol: Any, market: Any) -> None:
    if type(market) is not Market or type(symbol) is not str or not 1 <= len(symbol) <= 40:
        raise HorizonResearchError("SYMBOL_MARKET_MISMATCH")
    code, dot, suffix = symbol.rpartition(".")
    if (not dot or not code or symbol != symbol.upper()
            or any(not (c.isascii() and (c.isalnum() or c in ".-_")) for c in code)
            or suffix not in {Market.A: {"SH", "SZ"}, Market.HK: {"HK"}, Market.US: {"US"}}[market]):
        raise HorizonResearchError("NONCANONICAL_SECURITY")


def _available(known_at: datetime, usable_from: datetime) -> None:
    if _time(known_at) > _time(usable_from):
        raise HorizonResearchError("KNOWN_AFTER_USABLE_FROM")


def _id(value: Any) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise HorizonResearchError("SHA256_REFERENCE_REQUIRED")


def _price(value: Any) -> None:
    if (type(value) is not Decimal or not value.is_finite() or value <= 0
            or len(value.as_tuple().digits) > 28 or abs(value.adjusted()) > 28):
        raise HorizonResearchError("BOUNDED_POSITIVE_DECIMAL_REQUIRED")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DeclaredCalendarDay:
    day: date
    is_open: bool
    close_at: datetime | None
    known_at: datetime
    evidence_id: str
    market: Market = field(kw_only=True)
    usable_from: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.day) is not date or type(self.is_open) is not bool:
            raise HorizonResearchError("CALENDAR_DAY_TYPE")
        if type(self.market) is not Market:
            raise HorizonResearchError("CALENDAR_MARKET_REQUIRED")
        _available(self.known_at, self.usable_from)
        _id(self.evidence_id)
        if self.is_open:
            if _time(self.close_at).astimezone(ZoneInfo(ZONES[self.market])).date() != self.day:
                raise HorizonResearchError("CALENDAR_LOCAL_CLOSE_DATE_MISMATCH")
        elif self.close_at is not None:
            raise HorizonResearchError("CLOSED_DAY_HAS_CLOSE")


@dataclass(frozen=True, slots=True)
class DeclaredDailyObservation:
    symbol: str
    market: Market
    day: date
    close_at: datetime
    known_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    price_basis_id: str
    source_id: str
    usable_from: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.market) is not Market or type(self.day) is not date or type(self.symbol) is not str:
            raise HorizonResearchError("OBSERVATION_IDENTITY_TYPE")
        _security(self.symbol, self.market)
        _available(self.known_at, self.usable_from)
        _id(self.price_basis_id)
        _id(self.source_id)
        if _time(self.known_at) < _time(self.close_at):
            raise HorizonResearchError("DAILY_BAR_KNOWN_BEFORE_CLOSE")
        if self.close_at.astimezone(ZoneInfo(ZONES[self.market])).date() != self.day:
            raise HorizonResearchError("LOCAL_CLOSE_DATE_MISMATCH")
        for v in (self.open, self.high, self.low, self.close):
            _price(v)
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise HorizonResearchError("INVALID_OHLC")
        if type(self.volume) is not int or not 0 <= self.volume <= 2**63 - 1:
            raise HorizonResearchError("INVALID_VOLUME")


def closed_week_facts(
    calendar: tuple[DeclaredCalendarDay, ...], observations: tuple[DeclaredDailyObservation, ...],
    *, symbol: str, market: Market, as_of: datetime,
) -> dict[str, Any]:
    """Aggregate only full local Monday–Sunday schedules known by as_of.

    Zero OPEN days is a distinct result, not an invented zero-price bar. Every
    provided observation must be consumed; unexpected or omitted members fail.
    """
    now = _time(as_of)
    if (type(calendar) is not tuple or type(observations) is not tuple or not calendar
            or len(calendar) > 371 or len(observations) > 371 or type(market) is not Market
            or any(type(d) is not DeclaredCalendarDay for d in calendar)):
        raise HorizonResearchError("BOUNDED_TYPED_WINDOW_REQUIRED")
    _security(symbol, market)
    if len(calendar) % 7 or calendar[0].day.weekday() != 0:
        raise HorizonResearchError("FULL_LOCAL_WEEKS_REQUIRED")
    zone = ZoneInfo(ZONES[market])
    rows: dict[date, DeclaredDailyObservation] = {}
    for item in observations:
        if type(item) is not DeclaredDailyObservation or replace(item) != item:
            raise HorizonResearchError("INVALID_OBSERVATION")
        if item.symbol != symbol or item.market is not market or item.day in rows:
            raise HorizonResearchError("DUPLICATE_OR_WRONG_SECURITY")
        if _time(item.known_at) > now:
            raise HorizonResearchError("FUTURE_KNOWN_OBSERVATION")
        if _time(item.usable_from) > now:
            raise HorizonResearchError("FUTURE_USABLE_OBSERVATION")
        rows[item.day] = item
    if len({r.price_basis_id for r in observations}) > 1 or len({r.source_id for r in observations}) > 1:
        raise HorizonResearchError("MIXED_PRICE_BASIS_OR_SOURCE")
    used: set[date] = set()
    output = []
    for i, item in enumerate(calendar):
        if type(item) is not DeclaredCalendarDay or replace(item) != item:
            raise HorizonResearchError("INVALID_CALENDAR_ITEM")
        if item.day != calendar[0].day + timedelta(days=i):
            raise HorizonResearchError("CALENDAR_MEMBERSHIP_GAP_OR_DUPLICATE")
        if item.market is not market:
            raise HorizonResearchError("CALENDAR_MARKET_MISMATCH")
        if _time(item.known_at) > now:
            raise HorizonResearchError("FUTURE_KNOWN_CALENDAR")
        if _time(item.usable_from) > now:
            raise HorizonResearchError("FUTURE_USABLE_CALENDAR")
        if item.is_open:
            row = rows.get(item.day)
            if row is None or _time(row.close_at) != _time(item.close_at):
                raise HorizonResearchError("OPEN_SESSION_BAR_MISSING_OR_CLOSE_MISMATCH")
            used.add(item.day)
        elif item.day in rows:
            raise HorizonResearchError("BAR_ON_DECLARED_CLOSED_DAY")
    if used != set(rows):
        raise HorizonResearchError("UNCONSUMED_OBSERVATIONS")
    for offset in range(0, len(calendar), 7):
        week = calendar[offset:offset + 7]
        end = datetime.combine(week[-1].day + timedelta(days=1), time(), zone)
        if now < end:
            raise HorizonResearchError("UNFINISHED_LOCAL_WEEK")
        selected = [rows[d.day] for d in week if d.is_open]
        dependencies = (*week, *selected)
        base: dict[str, Any] = {"week_start": week[0].day.isoformat(), "week_end_exclusive": end.isoformat(),
                               "session_dates": [r.day.isoformat() for r in selected],
                               "state": "DECLARED_COMPLETE_WEEK" if selected else "NO_OPEN_SESSIONS",
                               "latest_known_at": max(_time(d.known_at) for d in dependencies).isoformat(),
                               "latest_usable_from": max(_time(d.usable_from) for d in dependencies).isoformat(),
                               "available_at": max(_time(end), *(_time(d.usable_from) for d in dependencies)).isoformat()}
        if selected:
            with localcontext() as ctx:
                ctx.prec = 80
                base.update(open=str(selected[0].open), high=str(max(r.high for r in selected)),
                            low=str(min(r.low for r in selected)), close=str(selected[-1].close),
                            volume=sum(r.volume for r in selected), price_basis_id=selected[0].price_basis_id,
                            source_id=selected[0].source_id)
        output.append(base)
    identity = {"schema": "closed-week-input-v2", "symbol": symbol, "market": market.value,
                "as_of": now.isoformat(), "calendar": [{"day": d.day.isoformat(), "market": d.market.value, "open": d.is_open,
                              "close_at": _time(d.close_at).isoformat() if d.close_at else None,
                              "known_at": _time(d.known_at).isoformat(), "usable_from": _time(d.usable_from).isoformat(),
                              "evidence_id": d.evidence_id} for d in calendar],
                "observations": [{**{k: str(v) for k, v in asdict(r).items()}} for r in observations]}
    result = {"schema": "closed-week-facts-v2", "symbol": symbol, "market": market.value,
              "timezone": ZONES[market], "as_of": now.isoformat(), "weeks": output,
              "input_id": _hash(identity), "input_assurance": "DECLARED_NOT_AUTHORITY_VERIFIED",
              "research_grade": False, "auto_promote": False, "auto_trade": False}
    result["report_id"] = _hash(result)
    return result


@dataclass(frozen=True, slots=True)
class HorizonExperiment:
    purpose: HoldingPurpose
    lookback_sessions: int
    horizon_sessions: int
    review_every_sessions: int
    label_policy_id: str
    exit_policy_id: str
    cost_model_id: str
    price_basis_id: str
    market: Market = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.purpose) is not HoldingPurpose or type(self.market) is not Market:
            raise HorizonResearchError("EXACT_PURPOSE_AND_MARKET_REQUIRED")
        for n in (self.lookback_sessions, self.horizon_sessions, self.review_every_sessions):
            if type(n) is not int or not 1 <= n <= 1260:
                raise HorizonResearchError("EXPLICIT_SESSION_PARAMETER_REQUIRED")
        for value in (self.label_policy_id, self.exit_policy_id, self.cost_model_id, self.price_basis_id):
            _id(value)

    @property
    def definition_id(self) -> str:
        self.__post_init__()
        return _hash({"schema": "horizon-experiment-definition-v2", **asdict(self)})


@dataclass(frozen=True, slots=True)
class ExperimentSample:
    sample_id: str
    episode_id: str
    purpose: HoldingPurpose
    decision_at: datetime
    feature_known_at: datetime
    label_end_at: datetime
    label_known_at: datetime
    source_snapshot_id: str
    definition_id: str
    market: Market = field(kw_only=True)
    feature_usable_from: datetime = field(kw_only=True)
    label_usable_from: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        for value in (self.sample_id, self.episode_id, self.source_snapshot_id, self.definition_id):
            _id(value)
        if type(self.purpose) is not HoldingPurpose or type(self.market) is not Market:
            raise HorizonResearchError("EXACT_PURPOSE_AND_MARKET_REQUIRED")
        if not (_time(self.feature_known_at) <= _time(self.feature_usable_from) <= _time(self.decision_at)
                < _time(self.label_end_at) <= _time(self.label_known_at) <= _time(self.label_usable_from)):
            raise HorizonResearchError("FEATURE_OR_LABEL_LOOKAHEAD")


def purged_experiment_manifest(
    spec: HorizonExperiment, samples: tuple[ExperimentSample, ...], *, train_start: datetime,
    calibration_start: datetime, validation_start: datetime, validation_end: datetime,
    embargo: timedelta, as_of: datetime,
) -> dict[str, Any]:
    """Assign every sample or give a purge reason; no fitting/holdout exposure.

    Embargo is an explicit elapsed-time duration, not mislabeled trading sessions.
    T operations within one episode cannot inflate its independent sample count.
    """
    if type(spec) is not HorizonExperiment or replace(spec) != spec:
        raise HorizonResearchError("INVALID_EXPERIMENT")
    if type(samples) is not tuple or not samples or len(samples) > 10000:
        raise HorizonResearchError("BOUNDED_NONEMPTY_SAMPLES_REQUIRED")
    boundaries = tuple(_time(t) for t in (train_start, calibration_start, validation_start, validation_end))
    if not boundaries[0] < boundaries[1] < boundaries[2] < boundaries[3] <= _time(as_of):
        raise HorizonResearchError("TIME_SPLIT_ORDER")
    if type(embargo) is not timedelta or embargo < timedelta() or embargo >= min(
            boundaries[i + 1] - boundaries[i] for i in range(3)):
        raise HorizonResearchError("INVALID_EXPLICIT_EMBARGO")
    buckets: dict[str, list[str]] = {"train": [], "calibration": [], "validation": []}
    purged: list[dict[str, str]] = []
    episodes: set[str] = set()
    ids: set[str] = set()
    previous = None
    for row in samples:
        if (type(row) is not ExperimentSample or replace(row) != row or row.purpose is not spec.purpose
                or row.definition_id != spec.definition_id or row.market is not spec.market):
            raise HorizonResearchError("SAMPLE_TYPE_OR_COHORT_MISMATCH")
        if row.sample_id in ids or row.episode_id in episodes:
            raise HorizonResearchError("DUPLICATE_SAMPLE_OR_EPISODE")
        stamp = _time(row.decision_at)
        if previous is not None and stamp < previous:
            raise HorizonResearchError("UNORDERED_TIME_SERIES")
        previous = stamp
        ids.add(row.sample_id)
        episodes.add(row.episode_id)
        bucket = next((i for i in range(3) if boundaries[i] <= stamp < boundaries[i + 1]), None)
        if bucket is None:
            reason = "OUTSIDE_EXPERIMENT_WINDOW"
        elif _time(row.label_usable_from) >= boundaries[bucket + 1] - embargo:
            reason = "LABEL_OVERLAP_OR_EMBARGO"
        else:
            buckets[("train", "calibration", "validation")[bucket]].append(row.sample_id)
            continue
        purged.append({"sample_id": row.sample_id, "reason": reason})
    identity = {"spec": asdict(spec), "samples": [{k: str(v) for k, v in asdict(r).items()} for r in samples],
                "boundaries": [t.isoformat() for t in boundaries], "as_of": _time(as_of).isoformat(),
                "embargo_microseconds": embargo // timedelta(microseconds=1)}
    result = {"schema": "horizon-experiment-manifest-v2", "experiment_id": _hash(identity),
              "purpose": spec.purpose.value, "market": spec.market.value, "partitions": buckets, "purged": purged,
              "input_assurance": "DECLARED_NOT_AUTHORITY_VERIFIED", "included_episode_count": sum(map(len, buckets.values())),
              "sample_count": len(samples), "independent_episode_count": len(episodes),
              "ready_for_fixture_comparison": all(buckets.values()), "research_grade": False,
              "holdout_exposed": False, "model_fitted": False, "auto_promote": False, "auto_trade": False}
    return result
