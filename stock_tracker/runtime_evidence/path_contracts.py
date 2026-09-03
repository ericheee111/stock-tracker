from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import Any

from stock_tracker.core.types import Market

RUNTIME_PATH_CONTRACT_SCHEMA = "stage4g1-runtime-path-contract-v1"
RUNTIME_PATH_PREFIX_SCHEMA = "stage4g1-runtime-path-prefix-v1"
RUNTIME_PATH_RESOLUTION_SCHEMA = "stage4g1-runtime-path-resolution-v1"
RUNTIME_NO_ENTRY_SCHEMA = "stage4g1-runtime-no-entry-evidence-v1"
RUNTIME_EXECUTION_SUMMARY_SCHEMA = "stage4g1-runtime-execution-summary-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 4096
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


class RuntimePathContractError(ValueError):
    """Raised when path, session, execution, or no-entry evidence is malformed."""

    def __init__(self, message: str, *, code: str = "RUNTIME_PATH_CONTRACT_INVALID") -> None:
        super().__init__(message)
        self.code = code


class RuntimeCalendarState(StrEnum):
    OPEN = "OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"


class RuntimeOpenSessionState(StrEnum):
    TRADED = "TRADED"
    SUSPENDED = "SUSPENDED"
    NO_TRADE = "NO_TRADE"
    MISSING_DATA = "MISSING_DATA"


class RuntimeCoverageState(StrEnum):
    COMPLETE_PREFIX = "COMPLETE_PREFIX"
    COMPLETE_SESSION = "COMPLETE_SESSION"
    INCOMPLETE_GAP = "INCOMPLETE_GAP"
    INCOMPLETE_OUT_OF_ORDER = "INCOMPLETE_OUT_OF_ORDER"
    INCOMPLETE_SPARSE = "INCOMPLETE_SPARSE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RuntimePathGranularity(StrEnum):
    TICK = "TICK"
    MINUTE_BAR = "MINUTE_BAR"
    DAILY_BAR = "DAILY_BAR"


class RuntimePathFactKind(StrEnum):
    SESSION = "SESSION"
    POINT = "POINT"


class RuntimePathResolutionState(StrEnum):
    OPEN = "OPEN"
    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"


class RuntimePathTerminalReason(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"


class RuntimePathPendingCode(StrEnum):
    HORIZON_NOT_REACHED = "HORIZON_NOT_REACHED"


class RuntimePathBlockerCode(StrEnum):
    CALENDAR_COVERAGE_GAP = "CALENDAR_COVERAGE_GAP"
    ENTRY_SESSION_MISSING = "ENTRY_SESSION_MISSING"
    ENTRY_SESSION_MISMATCH = "ENTRY_SESSION_MISMATCH"
    ENTRY_SESSION_NOT_TRADED = "ENTRY_SESSION_NOT_TRADED"
    ENTRY_TIME_OUTSIDE_SESSION_WINDOW = "ENTRY_TIME_OUTSIDE_SESSION_WINDOW"
    OPEN_SESSION_INDEX_GAP = "OPEN_SESSION_INDEX_GAP"
    POINT_WITHOUT_SESSION = "POINT_WITHOUT_SESSION"
    POINT_BEFORE_ENTRY_WINDOW = "POINT_BEFORE_ENTRY_WINDOW"
    POINT_OUTSIDE_SESSION_WINDOW = "POINT_OUTSIDE_SESSION_WINDOW"
    WINDOW_BOUNDARY_OVERLAP = "WINDOW_BOUNDARY_OVERLAP"
    OVERLAPPING_PATH_OBSERVATIONS = "OVERLAPPING_PATH_OBSERVATIONS"
    NONTRADED_SESSION_WITH_POINTS = "NONTRADED_SESSION_WITH_POINTS"
    TRADED_SESSION_WITHOUT_POINTS = "TRADED_SESSION_WITHOUT_POINTS"
    POINT_BEYOND_COVERAGE = "POINT_BEYOND_COVERAGE"
    SESSION_PREFIX_NOT_CLOSED = "SESSION_PREFIX_NOT_CLOSED"
    SESSION_GAP = "SESSION_GAP"
    SESSION_OUT_OF_ORDER = "SESSION_OUT_OF_ORDER"
    SESSION_SPARSE = "SESSION_SPARSE"
    MISSING_DATA = "MISSING_DATA"
    INTRABAR_AMBIGUITY = "INTRABAR_AMBIGUITY"
    HORIZON_SESSION_INCOMPLETE = "HORIZON_SESSION_INCOMPLETE"


class RuntimeNoEntryReason(StrEnum):
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    USER_CANCELLED = "USER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    DATA_INVALID = "DATA_INVALID"


class RuntimeExecutionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class RuntimeFillCompletion(StrEnum):
    ZERO_FILL = "ZERO_FILL"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int = _MAX_TEXT,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > maximum:
        raise RuntimePathContractError(f"{name} must be a safe trimmed string")
    if not value and not allow_empty:
        raise RuntimePathContractError(f"{name} must not be empty")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise RuntimePathContractError(f"{name} contains control characters")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=64)
    if _SHA256.fullmatch(text) is None:
        raise RuntimePathContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimePathContractError(f"{name} must be boolean")
    return value


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimePathContractError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise RuntimePathContractError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise RuntimePathContractError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise RuntimePathContractError(f"{name} must be non-negative")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimePathContractError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_date(value: object, name: str) -> date:
    if type(value) is not date:
        raise RuntimePathContractError(f"{name} must be a date")
    return value


def _require_market(value: object) -> Market:
    if type(value) is not Market:
        raise RuntimePathContractError("market must be the exact Market type")
    return value


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol", maximum=64)
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if symbol != symbol.upper() or not symbol.endswith(suffixes[market]):
        raise RuntimePathContractError("symbol suffix must match market")
    return symbol


def _hash_document(document: dict[str, Any]) -> str:
    try:
        raw = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimePathContractError("path evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeSourceReference:
    source_store_id: str
    source_session_id: str
    source_record_id: str
    source_append_order: int
    source_record_hash: str
    raw_payload_sha256: str
    parser_id: str
    schema_id: str
    source_time: datetime
    received_at: datetime
    durable_known_at: datetime
    source_reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        source_time = _require_utc(self.source_time, "source_time")
        received_at = _require_utc(self.received_at, "received_at")
        durable_known_at = _require_utc(self.durable_known_at, "durable_known_at")
        if source_time > durable_known_at or received_at > durable_known_at:
            raise RuntimePathContractError(
                "source or received time cannot exceed durable_known_at"
            )
        object.__setattr__(self, "source_time", source_time)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "durable_known_at", durable_known_at)
        _require_sha256(self.source_store_id, "source_store_id")
        _require_text(self.source_session_id, "source_session_id", maximum=256)
        _require_text(self.source_record_id, "source_record_id", maximum=256)
        _require_int(self.source_append_order, "source_append_order", minimum=1)
        _require_sha256(self.source_record_hash, "source_record_hash")
        _require_sha256(self.raw_payload_sha256, "raw_payload_sha256")
        _require_text(self.parser_id, "parser_id", maximum=256)
        _require_text(self.schema_id, "schema_id", maximum=256)
        object.__setattr__(
            self,
            "source_reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-source-reference-v1",
            "source_store_id": self.source_store_id,
            "source_session_id": self.source_session_id,
            "source_record_id": self.source_record_id,
            "source_append_order": self.source_append_order,
            "source_record_hash": self.source_record_hash,
            "raw_payload_sha256": self.raw_payload_sha256,
            "parser_id": self.parser_id,
            "schema_id": self.schema_id,
            "source_time": _utc_text(self.source_time),
            "received_at": _utc_text(self.received_at),
            "durable_known_at": _utc_text(self.durable_known_at),
        }
        if include_id:
            document["source_reference_id"] = self.source_reference_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeSessionEvidence:
    symbol: str
    market: Market
    trading_day: date
    calendar_state: RuntimeCalendarState
    open_session_index: int | None
    open_session_state: RuntimeOpenSessionState | None
    scheduled_open_at: datetime | None
    scheduled_close_at: datetime | None
    coverage_state: RuntimeCoverageState
    session_complete: bool
    coverage_through: datetime | None
    calendar_fact_id: str
    security_status_fact_id: str | None
    coverage_fact_id: str | None
    source_reference: RuntimeSourceReference
    session_evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_date(self.trading_day, "trading_day")
        if type(self.calendar_state) is not RuntimeCalendarState:
            raise RuntimePathContractError("calendar_state must be RuntimeCalendarState")
        if type(self.coverage_state) is not RuntimeCoverageState:
            raise RuntimePathContractError("coverage_state must be RuntimeCoverageState")
        _require_bool(self.session_complete, "session_complete")
        _require_sha256(self.calendar_fact_id, "calendar_fact_id")
        security_status = (
            None
            if self.security_status_fact_id is None
            else _require_sha256(
                self.security_status_fact_id,
                "security_status_fact_id",
            )
        )
        coverage_fact = (
            None
            if self.coverage_fact_id is None
            else _require_sha256(self.coverage_fact_id, "coverage_fact_id")
        )
        if type(self.source_reference) is not RuntimeSourceReference:
            raise RuntimePathContractError(
                "source_reference must be RuntimeSourceReference"
            )
        scheduled_open = (
            None
            if self.scheduled_open_at is None
            else _require_utc(self.scheduled_open_at, "scheduled_open_at")
        )
        scheduled_close = (
            None
            if self.scheduled_close_at is None
            else _require_utc(self.scheduled_close_at, "scheduled_close_at")
        )
        if (scheduled_open is None) != (scheduled_close is None):
            raise RuntimePathContractError(
                "scheduled session boundaries must be present together"
            )
        if (
            scheduled_open is not None
            and scheduled_close is not None
            and scheduled_close <= scheduled_open
        ):
            raise RuntimePathContractError(
                "scheduled_close_at must be after scheduled_open_at"
            )
        object.__setattr__(self, "scheduled_open_at", scheduled_open)
        object.__setattr__(self, "scheduled_close_at", scheduled_close)
        coverage_through = (
            None
            if self.coverage_through is None
            else _require_utc(self.coverage_through, "coverage_through")
        )
        if (
            coverage_through is not None
            and coverage_through > self.source_reference.durable_known_at
        ):
            raise RuntimePathContractError(
                "coverage_through cannot exceed source durable_known_at"
            )
        object.__setattr__(self, "coverage_through", coverage_through)
        if self.calendar_state is RuntimeCalendarState.MARKET_CLOSED:
            if (
                self.open_session_index is not None
                or self.open_session_state is not None
                or scheduled_open is not None
                or scheduled_close is not None
                or self.coverage_state is not RuntimeCoverageState.NOT_APPLICABLE
                or not self.session_complete
                or coverage_through is not None
                or security_status is not None
                or coverage_fact is not None
            ):
                raise RuntimePathContractError(
                    "MARKET_CLOSED cannot carry an open-session index, status, or market data"
                )
        else:
            _require_int(self.open_session_index, "open_session_index")
            if scheduled_open is None or scheduled_close is None:
                raise RuntimePathContractError(
                    "OPEN calendar day requires scheduled session boundaries"
                )
            if type(self.open_session_state) is not RuntimeOpenSessionState:
                raise RuntimePathContractError(
                    "OPEN calendar day requires RuntimeOpenSessionState"
                )
            if self.coverage_state is RuntimeCoverageState.NOT_APPLICABLE:
                raise RuntimePathContractError(
                    "OPEN calendar day requires a market-data coverage state"
                )
            if coverage_fact is None:
                raise RuntimePathContractError(
                    "OPEN calendar day requires coverage_fact_id"
                )
            if self.coverage_state in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            } and coverage_through is None:
                raise RuntimePathContractError(
                    "complete coverage requires an explicit coverage_through time"
                )
            if coverage_through is not None and not (
                scheduled_open <= coverage_through <= scheduled_close
            ):
                raise RuntimePathContractError(
                    "coverage_through is outside the scheduled session window"
                )
            if self.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX:
                if self.session_complete or coverage_through >= scheduled_close:
                    raise RuntimePathContractError(
                        "COMPLETE_PREFIX must stop before the scheduled session close"
                    )
            elif self.coverage_state is RuntimeCoverageState.COMPLETE_SESSION and (
                not self.session_complete or coverage_through != scheduled_close
            ):
                raise RuntimePathContractError(
                    "COMPLETE_SESSION must cover the scheduled session close"
                )
            if self.open_session_state in {
                RuntimeOpenSessionState.SUSPENDED,
                RuntimeOpenSessionState.NO_TRADE,
            } and (
                security_status is None
                or self.coverage_state is not RuntimeCoverageState.COMPLETE_SESSION
                or not self.session_complete
            ):
                raise RuntimePathContractError(
                    "SUSPENDED/NO_TRADE require authoritative status and complete-session coverage"
                )
            if self.open_session_state is RuntimeOpenSessionState.MISSING_DATA and self.coverage_state in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            }:
                raise RuntimePathContractError(
                    "MISSING_DATA cannot claim complete market-data coverage"
                )
        object.__setattr__(
            self,
            "session_evidence_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-session-evidence-v1",
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "calendar_state": self.calendar_state.value,
            "open_session_index": self.open_session_index,
            "open_session_state": (
                None if self.open_session_state is None else self.open_session_state.value
            ),
            "scheduled_open_at": (
                None if self.scheduled_open_at is None else _utc_text(self.scheduled_open_at)
            ),
            "scheduled_close_at": (
                None if self.scheduled_close_at is None else _utc_text(self.scheduled_close_at)
            ),
            "coverage_state": self.coverage_state.value,
            "session_complete": self.session_complete,
            "coverage_through": (
                None if self.coverage_through is None else _utc_text(self.coverage_through)
            ),
            "calendar_fact_id": self.calendar_fact_id,
            "security_status_fact_id": self.security_status_fact_id,
            "coverage_fact_id": self.coverage_fact_id,
            "source_reference": self.source_reference.as_dict(),
        }
        if include_id:
            document["session_evidence_id"] = self.session_evidence_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathObservation:
    symbol: str
    market: Market
    open_session_index: int
    interval_start: datetime
    interval_end: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    granularity: RuntimePathGranularity
    source_reference: RuntimeSourceReference
    path_observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_int(self.open_session_index, "open_session_index")
        interval_start = _require_utc(self.interval_start, "interval_start")
        interval_end = _require_utc(self.interval_end, "interval_end")
        if interval_end < interval_start:
            raise RuntimePathContractError("path interval_end precedes interval_start")
        object.__setattr__(self, "interval_start", interval_start)
        object.__setattr__(self, "interval_end", interval_end)
        high = _require_decimal(self.high, "high", positive=True)
        low = _require_decimal(self.low, "low", positive=True)
        close = _require_decimal(self.close, "close", positive=True)
        if low > min(high, close) or high < max(low, close):
            raise RuntimePathContractError("path OHLC values are inconsistent")
        if type(self.granularity) is not RuntimePathGranularity:
            raise RuntimePathContractError(
                "granularity must be RuntimePathGranularity"
            )
        if type(self.source_reference) is not RuntimeSourceReference:
            raise RuntimePathContractError(
                "source_reference must be RuntimeSourceReference"
            )
        if interval_end > self.source_reference.durable_known_at:
            raise RuntimePathContractError(
                "path interval cannot end after its durable known time"
            )
        if self.granularity is RuntimePathGranularity.TICK:
            if interval_start != interval_end or not (high == low == close):
                raise RuntimePathContractError(
                    "TICK evidence requires a point interval and one exact price"
                )
        elif interval_start == interval_end:
            raise RuntimePathContractError("bar evidence requires a non-zero interval")
        object.__setattr__(
            self,
            "path_observation_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-path-observation-v1",
            "symbol": self.symbol,
            "market": self.market.value,
            "open_session_index": self.open_session_index,
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "granularity": self.granularity.value,
            "source_reference": self.source_reference.as_dict(),
        }
        if include_id:
            document["path_observation_id"] = self.path_observation_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeFrozenPathFact:
    collection_store_id: str
    collection_append_order: int
    collection_fact_id: str
    collection_observed_at: datetime
    kind: RuntimePathFactKind
    session_evidence: RuntimeSessionEvidence | None = None
    path_observation: RuntimePathObservation | None = None
    frozen_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.collection_store_id, "collection_store_id")
        _require_int(
            self.collection_append_order,
            "collection_append_order",
            minimum=1,
        )
        _require_sha256(self.collection_fact_id, "collection_fact_id")
        observed_at = _require_utc(
            self.collection_observed_at,
            "collection_observed_at",
        )
        object.__setattr__(self, "collection_observed_at", observed_at)
        if type(self.kind) is not RuntimePathFactKind:
            raise RuntimePathContractError("kind must be RuntimePathFactKind")
        if self.kind is RuntimePathFactKind.SESSION:
            if (
                type(self.session_evidence) is not RuntimeSessionEvidence
                or self.path_observation is not None
            ):
                raise RuntimePathContractError(
                    "SESSION fact must contain exactly one RuntimeSessionEvidence"
                )
            source = self.session_evidence.source_reference
        else:
            if (
                type(self.path_observation) is not RuntimePathObservation
                or self.session_evidence is not None
            ):
                raise RuntimePathContractError(
                    "POINT fact must contain exactly one RuntimePathObservation"
                )
            source = self.path_observation.source_reference
        if source.durable_known_at > observed_at:
            raise RuntimePathContractError(
                "collection fact predates the source durable-known time"
            )
        object.__setattr__(
            self,
            "frozen_fact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def source_reference(self) -> RuntimeSourceReference:
        if self.kind is RuntimePathFactKind.SESSION:
            assert self.session_evidence is not None
            return self.session_evidence.source_reference
        assert self.path_observation is not None
        return self.path_observation.source_reference

    @property
    def symbol(self) -> str:
        if self.kind is RuntimePathFactKind.SESSION:
            assert self.session_evidence is not None
            return self.session_evidence.symbol
        assert self.path_observation is not None
        return self.path_observation.symbol

    @property
    def market(self) -> Market:
        if self.kind is RuntimePathFactKind.SESSION:
            assert self.session_evidence is not None
            return self.session_evidence.market
        assert self.path_observation is not None
        return self.path_observation.market

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-frozen-path-fact-v1",
            "collection_store_id": self.collection_store_id,
            "collection_append_order": self.collection_append_order,
            "collection_fact_id": self.collection_fact_id,
            "collection_observed_at": _utc_text(self.collection_observed_at),
            "kind": self.kind.value,
            "session_evidence": (
                None
                if self.session_evidence is None
                else self.session_evidence.as_dict()
            ),
            "path_observation": (
                None
                if self.path_observation is None
                else self.path_observation.as_dict()
            ),
        }
        if include_id:
            document["frozen_fact_id"] = self.frozen_fact_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeFrozenPathPrefix:
    case_id: str
    symbol: str
    market: Market
    collection_store_id: str
    source_store_id: str
    frozen_at: datetime
    source_high_water_append_order: int
    collection_high_water_append_order: int
    source_audit_id: str
    collection_audit_id: str
    facts: tuple[RuntimeFrozenPathFact, ...]
    prefix_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.case_id, "case_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_sha256(self.collection_store_id, "collection_store_id")
        _require_sha256(self.source_store_id, "source_store_id")
        frozen_at = _require_utc(self.frozen_at, "frozen_at")
        object.__setattr__(self, "frozen_at", frozen_at)
        _require_int(
            self.source_high_water_append_order,
            "source_high_water_append_order",
        )
        _require_int(
            self.collection_high_water_append_order,
            "collection_high_water_append_order",
        )
        _require_sha256(self.source_audit_id, "source_audit_id")
        _require_sha256(self.collection_audit_id, "collection_audit_id")
        if type(self.facts) is not tuple or any(
            type(item) is not RuntimeFrozenPathFact for item in self.facts
        ):
            raise RuntimePathContractError(
                "facts must be an exact tuple of RuntimeFrozenPathFact"
            )
        collection_orders = tuple(item.collection_append_order for item in self.facts)
        if collection_orders != tuple(sorted(collection_orders)) or len(
            set(collection_orders)
        ) != len(collection_orders):
            raise RuntimePathContractError(
                "frozen path facts must use unique increasing collection order"
            )
        if collection_orders and collection_orders[-1] > self.collection_high_water_append_order:
            raise RuntimePathContractError(
                "path fact exceeds collection high-water mark"
            )
        if any(
            item.source_reference.source_append_order
            > self.source_high_water_append_order
            for item in self.facts
        ):
            raise RuntimePathContractError("path fact exceeds source high-water mark")
        if any(
            item.collection_store_id != self.collection_store_id
            or item.source_reference.source_store_id != self.source_store_id
            or item.symbol != self.symbol
            or item.market is not self.market
            or item.collection_observed_at > frozen_at
            for item in self.facts
        ):
            raise RuntimePathContractError(
                "path prefix fact identity or knowledge time is inconsistent"
            )
        for values, label in (
            (
                tuple(item.collection_fact_id for item in self.facts),
                "collection fact",
            ),
            (tuple(item.frozen_fact_id for item in self.facts), "frozen fact"),
        ):
            if len(values) != len(set(values)):
                raise RuntimePathContractError(
                    f"path prefix contains duplicate {label} identities"
                )
        session_days = tuple(
            item.session_evidence.trading_day
            for item in self.facts
            if item.session_evidence is not None
        )
        if len(session_days) != len(set(session_days)):
            raise RuntimePathContractError(
                "path prefix contains duplicate calendar-day evidence"
            )
        point_ids = tuple(
            item.path_observation.path_observation_id
            for item in self.facts
            if item.path_observation is not None
        )
        if len(point_ids) != len(set(point_ids)):
            raise RuntimePathContractError(
                "path prefix contains duplicate path observations"
            )
        object.__setattr__(
            self,
            "prefix_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_PATH_PREFIX_SCHEMA,
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "collection_store_id": self.collection_store_id,
            "source_store_id": self.source_store_id,
            "frozen_at": _utc_text(self.frozen_at),
            "source_high_water_append_order": self.source_high_water_append_order,
            "collection_high_water_append_order": self.collection_high_water_append_order,
            "source_audit_id": self.source_audit_id,
            "collection_audit_id": self.collection_audit_id,
            "facts": [item.as_dict() for item in self.facts],
        }
        if include_id:
            document["prefix_id"] = self.prefix_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathWindow:
    case_id: str
    symbol: str
    market: Market
    entry_filled_at: datetime
    entry_trading_day: date
    entry_session_index: int
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    horizon_sessions: int
    terminal_policy_id: str
    window_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.case_id, "case_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        entry_filled_at = _require_utc(self.entry_filled_at, "entry_filled_at")
        object.__setattr__(self, "entry_filled_at", entry_filled_at)
        _require_date(self.entry_trading_day, "entry_trading_day")
        _require_int(self.entry_session_index, "entry_session_index")
        entry = _require_decimal(self.entry_price, "entry_price", positive=True)
        target = _require_decimal(self.target_price, "target_price", positive=True)
        stop = _require_decimal(self.stop_price, "stop_price", positive=True)
        if not stop < entry < target:
            raise RuntimePathContractError(
                "path window requires stop_price < entry_price < target_price"
            )
        _require_int(self.horizon_sessions, "horizon_sessions", minimum=1)
        _require_sha256(self.terminal_policy_id, "terminal_policy_id")
        object.__setattr__(
            self,
            "window_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def horizon_session_index(self) -> int:
        return self.entry_session_index + self.horizon_sessions

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-path-window-v1",
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "entry_filled_at": _utc_text(self.entry_filled_at),
            "entry_trading_day": self.entry_trading_day.isoformat(),
            "entry_session_index": self.entry_session_index,
            "entry_price": _decimal_text(self.entry_price),
            "target_price": _decimal_text(self.target_price),
            "stop_price": _decimal_text(self.stop_price),
            "horizon_sessions": self.horizon_sessions,
            "terminal_policy_id": self.terminal_policy_id,
        }
        if include_id:
            document["window_id"] = self.window_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathResolution:
    state: RuntimePathResolutionState
    window_id: str
    prefix_id: str
    terminal_reason: RuntimePathTerminalReason | None
    pending_code: RuntimePathPendingCode | None
    blocker_codes: tuple[RuntimePathBlockerCode, ...]
    first_touch_collection_fact_id: str | None
    first_touch_observation_id: str | None
    effective_session_index: int | None
    resolution_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.state) is not RuntimePathResolutionState:
            raise RuntimePathContractError(
                "state must be RuntimePathResolutionState"
            )
        _require_sha256(self.window_id, "window_id")
        _require_sha256(self.prefix_id, "prefix_id")
        if self.terminal_reason is not None and type(
            self.terminal_reason
        ) is not RuntimePathTerminalReason:
            raise RuntimePathContractError(
                "terminal_reason must be RuntimePathTerminalReason or None"
            )
        if self.pending_code is not None and type(
            self.pending_code
        ) is not RuntimePathPendingCode:
            raise RuntimePathContractError(
                "pending_code must be RuntimePathPendingCode or None"
            )
        if type(self.blocker_codes) is not tuple or any(
            type(item) is not RuntimePathBlockerCode for item in self.blocker_codes
        ):
            raise RuntimePathContractError(
                "blocker_codes must be a tuple of RuntimePathBlockerCode"
            )
        if tuple(sorted(set(self.blocker_codes), key=lambda item: item.value)) != self.blocker_codes:
            raise RuntimePathContractError(
                "blocker_codes must be unique and canonically ordered"
            )
        first_fact = (
            None
            if self.first_touch_collection_fact_id is None
            else _require_sha256(
                self.first_touch_collection_fact_id,
                "first_touch_collection_fact_id",
            )
        )
        first_observation = (
            None
            if self.first_touch_observation_id is None
            else _require_sha256(
                self.first_touch_observation_id,
                "first_touch_observation_id",
            )
        )
        if (first_fact is None) != (first_observation is None):
            raise RuntimePathContractError(
                "first-touch fact and observation identities must be paired"
            )
        if self.effective_session_index is not None:
            _require_int(
                self.effective_session_index,
                "effective_session_index",
            )
        terminal_states = {
            RuntimePathResolutionState.TARGET: RuntimePathTerminalReason.TARGET,
            RuntimePathResolutionState.STOP: RuntimePathTerminalReason.STOP,
            RuntimePathResolutionState.TIMEOUT: RuntimePathTerminalReason.TIMEOUT,
        }
        if self.state in terminal_states:
            if (
                self.terminal_reason is not terminal_states[self.state]
                or self.pending_code is not None
                or self.blocker_codes
                or self.effective_session_index is None
            ):
                raise RuntimePathContractError(
                    "terminal resolution fields are inconsistent"
                )
            if self.state in {
                RuntimePathResolutionState.TARGET,
                RuntimePathResolutionState.STOP,
            } and first_fact is None:
                raise RuntimePathContractError(
                    "barrier resolution requires first-touch evidence"
                )
            if self.state is RuntimePathResolutionState.TIMEOUT and first_fact is not None:
                raise RuntimePathContractError(
                    "TIMEOUT cannot claim first-touch evidence"
                )
        elif self.state is RuntimePathResolutionState.OPEN:
            if (
                self.terminal_reason is not None
                or self.pending_code is not RuntimePathPendingCode.HORIZON_NOT_REACHED
                or self.blocker_codes
                or first_fact is not None
            ):
                raise RuntimePathContractError("OPEN resolution fields are inconsistent")
        else:
            if (
                self.terminal_reason is not None
                or self.pending_code is not None
                or not self.blocker_codes
                or first_fact is not None
            ):
                raise RuntimePathContractError(
                    "BLOCKED resolution fields are inconsistent"
                )
        object.__setattr__(
            self,
            "resolution_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_PATH_RESOLUTION_SCHEMA,
            "state": self.state.value,
            "window_id": self.window_id,
            "prefix_id": self.prefix_id,
            "terminal_reason": (
                None if self.terminal_reason is None else self.terminal_reason.value
            ),
            "pending_code": (
                None if self.pending_code is None else self.pending_code.value
            ),
            "blocker_codes": [item.value for item in self.blocker_codes],
            "first_touch_collection_fact_id": self.first_touch_collection_fact_id,
            "first_touch_observation_id": self.first_touch_observation_id,
            "effective_session_index": self.effective_session_index,
        }
        if include_id:
            document["resolution_id"] = self.resolution_id
        return document


def _blocked_resolution(
    window: RuntimePathWindow,
    prefix: RuntimeFrozenPathPrefix,
    *codes: RuntimePathBlockerCode,
    effective_session_index: int | None = None,
) -> RuntimePathResolution:
    canonical = tuple(sorted(set(codes), key=lambda item: item.value))
    return RuntimePathResolution(
        state=RuntimePathResolutionState.BLOCKED,
        window_id=window.window_id,
        prefix_id=prefix.prefix_id,
        terminal_reason=None,
        pending_code=None,
        blocker_codes=canonical,
        first_touch_collection_fact_id=None,
        first_touch_observation_id=None,
        effective_session_index=effective_session_index,
    )


def _coverage_blocker(
    session: RuntimeSessionEvidence,
) -> RuntimePathBlockerCode | None:
    if session.open_session_state is RuntimeOpenSessionState.MISSING_DATA:
        return RuntimePathBlockerCode.MISSING_DATA
    mapping = {
        RuntimeCoverageState.INCOMPLETE_GAP: RuntimePathBlockerCode.SESSION_GAP,
        RuntimeCoverageState.INCOMPLETE_OUT_OF_ORDER: RuntimePathBlockerCode.SESSION_OUT_OF_ORDER,
        RuntimeCoverageState.INCOMPLETE_SPARSE: RuntimePathBlockerCode.SESSION_SPARSE,
    }
    return mapping.get(session.coverage_state)


def resolve_runtime_path(
    window: RuntimePathWindow,
    prefix: RuntimeFrozenPathPrefix,
) -> RuntimePathResolution:
    """Resolve first-touch TARGET/STOP/TIMEOUT from one immutable request-time prefix.

    This resolver never looks outside ``prefix``.  A later source record with an
    earlier market timestamp therefore cannot rewrite the result for an already
    frozen request.
    """

    if type(window) is not RuntimePathWindow:
        raise RuntimePathContractError("window must be RuntimePathWindow")
    if type(prefix) is not RuntimeFrozenPathPrefix:
        raise RuntimePathContractError("prefix must be RuntimeFrozenPathPrefix")
    if (
        prefix.case_id != window.case_id
        or prefix.symbol != window.symbol
        or prefix.market is not window.market
    ):
        raise RuntimePathContractError(
            "path window and frozen prefix identity are inconsistent",
            code="PATH_PREFIX_IDENTITY_MISMATCH",
        )

    session_facts = tuple(
        item for item in prefix.facts if item.kind is RuntimePathFactKind.SESSION
    )
    point_facts = tuple(
        item for item in prefix.facts if item.kind is RuntimePathFactKind.POINT
    )
    sessions = tuple(
        sorted(
            (item.session_evidence for item in session_facts),
            key=lambda item: item.trading_day if item is not None else date.min,
        )
    )
    sessions = tuple(item for item in sessions if item is not None)
    horizon_index = window.horizon_session_index
    all_relevant_sessions = tuple(
        item for item in sessions if item.trading_day >= window.entry_trading_day
    )
    bounded_sessions: list[RuntimeSessionEvidence] = []
    for session in all_relevant_sessions:
        bounded_sessions.append(session)
        if (
            session.calendar_state is RuntimeCalendarState.OPEN
            and session.open_session_index == horizon_index
        ):
            break
    relevant_sessions = tuple(bounded_sessions)
    entry_session = next(
        (
            item
            for item in relevant_sessions
            if item.trading_day == window.entry_trading_day
        ),
        None,
    )
    if entry_session is None:
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_MISSING,
        )
    if (
        entry_session.calendar_state is not RuntimeCalendarState.OPEN
        or entry_session.open_session_index != window.entry_session_index
    ):
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_MISMATCH,
        )
    if entry_session.open_session_state is not RuntimeOpenSessionState.TRADED:
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_NOT_TRADED,
            effective_session_index=window.entry_session_index,
        )
    if (
        entry_session.scheduled_open_at is None
        or entry_session.scheduled_close_at is None
        or not (
            entry_session.scheduled_open_at
            <= window.entry_filled_at
            <= entry_session.scheduled_close_at
        )
    ):
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_TIME_OUTSIDE_SESSION_WINDOW,
            effective_session_index=window.entry_session_index,
        )

    for previous, current in pairwise(relevant_sessions):
        if current.trading_day != previous.trading_day + timedelta(days=1):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.CALENDAR_COVERAGE_GAP,
            )

    open_sessions = tuple(
        item
        for item in relevant_sessions
        if item.calendar_state is RuntimeCalendarState.OPEN
    )
    expected_session_index = window.entry_session_index
    for session in open_sessions:
        if session.open_session_index != expected_session_index:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.OPEN_SESSION_INDEX_GAP,
                effective_session_index=expected_session_index,
            )
        expected_session_index += 1

    session_by_index = {
        item.open_session_index: item
        for item in open_sessions
        if item.open_session_index is not None
    }
    points_by_session: dict[int, list[RuntimeFrozenPathFact]] = {}
    for fact in point_facts:
        observation = fact.path_observation
        assert observation is not None
        index = observation.open_session_index
        if index > horizon_index:
            continue
        session = session_by_index.get(index)
        if session is None:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_WITHOUT_SESSION,
                effective_session_index=index,
            )
        if session.open_session_state is not RuntimeOpenSessionState.TRADED:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.NONTRADED_SESSION_WITH_POINTS,
                effective_session_index=index,
            )
        if (
            session.scheduled_open_at is None
            or session.scheduled_close_at is None
            or observation.interval_start < session.scheduled_open_at
            or observation.interval_end > session.scheduled_close_at
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_OUTSIDE_SESSION_WINDOW,
                effective_session_index=index,
            )
        if (
            session.coverage_state
            in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            }
            and (
                session.coverage_through is None
                or observation.interval_end > session.coverage_through
            )
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_BEYOND_COVERAGE,
                effective_session_index=index,
            )
        if index < window.entry_session_index:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_BEFORE_ENTRY_WINDOW,
                effective_session_index=index,
            )
        if index == window.entry_session_index:
            if observation.interval_end < window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.POINT_BEFORE_ENTRY_WINDOW,
                    effective_session_index=index,
                )
            if observation.interval_start < window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.WINDOW_BOUNDARY_OVERLAP,
                    effective_session_index=index,
                )
        points_by_session.setdefault(index, []).append(fact)

    max_known_open_index = (
        max(session_by_index) if session_by_index else window.entry_session_index - 1
    )
    for session_index in range(
        window.entry_session_index,
        min(max_known_open_index, horizon_index) + 1,
    ):
        session = session_by_index.get(session_index)
        if session is None:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.OPEN_SESSION_INDEX_GAP,
                effective_session_index=session_index,
            )
        blocker = _coverage_blocker(session)
        if blocker is not None:
            return _blocked_resolution(
                window,
                prefix,
                blocker,
                effective_session_index=session_index,
            )
        if (
            session_index < max_known_open_index
            and session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.SESSION_PREFIX_NOT_CLOSED,
                effective_session_index=session_index,
            )
        facts = points_by_session.get(session_index, [])
        if session.open_session_state is RuntimeOpenSessionState.TRADED and not facts:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.TRADED_SESSION_WITHOUT_POINTS,
                effective_session_index=session_index,
            )
        ordered = sorted(
            facts,
            key=lambda item: (
                item.path_observation.interval_start,
                item.path_observation.interval_end,
                item.source_reference.source_append_order,
                item.path_observation.path_observation_id,
            ),
        )
        previous_end: datetime | None = None
        for fact in ordered:
            observation = fact.path_observation
            assert observation is not None
            if previous_end is not None and observation.interval_start < previous_end:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.OVERLAPPING_PATH_OBSERVATIONS,
                    effective_session_index=session_index,
                )
            previous_end = observation.interval_end
            target_touched = observation.high >= window.target_price
            stop_touched = observation.low <= window.stop_price
            if target_touched and stop_touched:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.INTRABAR_AMBIGUITY,
                    effective_session_index=session_index,
                )
            if target_touched or stop_touched:
                reason = (
                    RuntimePathTerminalReason.TARGET
                    if target_touched
                    else RuntimePathTerminalReason.STOP
                )
                state = (
                    RuntimePathResolutionState.TARGET
                    if target_touched
                    else RuntimePathResolutionState.STOP
                )
                return RuntimePathResolution(
                    state=state,
                    window_id=window.window_id,
                    prefix_id=prefix.prefix_id,
                    terminal_reason=reason,
                    pending_code=None,
                    blocker_codes=(),
                    first_touch_collection_fact_id=fact.collection_fact_id,
                    first_touch_observation_id=observation.path_observation_id,
                    effective_session_index=session_index,
                )

    horizon_session = session_by_index.get(horizon_index)
    if horizon_session is None:
        return RuntimePathResolution(
            state=RuntimePathResolutionState.OPEN,
            window_id=window.window_id,
            prefix_id=prefix.prefix_id,
            terminal_reason=None,
            pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
            blocker_codes=(),
            first_touch_collection_fact_id=None,
            first_touch_observation_id=None,
            effective_session_index=max_known_open_index,
        )
    if (
        not horizon_session.session_complete
        or horizon_session.coverage_state is not RuntimeCoverageState.COMPLETE_SESSION
    ):
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.HORIZON_SESSION_INCOMPLETE,
            effective_session_index=horizon_index,
        )
    return RuntimePathResolution(
        state=RuntimePathResolutionState.TIMEOUT,
        window_id=window.window_id,
        prefix_id=prefix.prefix_id,
        terminal_reason=RuntimePathTerminalReason.TIMEOUT,
        pending_code=None,
        blocker_codes=(),
        first_touch_collection_fact_id=None,
        first_touch_observation_id=None,
        effective_session_index=horizon_index,
    )


@dataclass(frozen=True, slots=True)
class RuntimeExecutionFragment:
    intent_id: str
    execution_id: str
    source_fact_id: str
    side: RuntimeExecutionSide
    timestamp: datetime
    durable_known_at: datetime
    quantity: int
    price: Decimal
    explicit_cost: Decimal
    fragment_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        _require_text(self.execution_id, "execution_id", maximum=256)
        _require_sha256(self.source_fact_id, "source_fact_id")
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError("side must be the exact RuntimeExecutionSide type")
        timestamp = _require_utc(self.timestamp, "execution timestamp")
        known_at = _require_utc(self.durable_known_at, "execution durable_known_at")
        if timestamp > known_at:
            raise RuntimePathContractError(
                "execution timestamp cannot exceed durable_known_at"
            )
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "durable_known_at", known_at)
        _require_int(self.quantity, "execution quantity", minimum=1)
        _require_decimal(self.price, "execution price", positive=True)
        _require_decimal(self.explicit_cost, "explicit_cost", nonnegative=True)
        object.__setattr__(
            self,
            "fragment_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-execution-fragment-v1",
            "intent_id": self.intent_id,
            "execution_id": self.execution_id,
            "source_fact_id": self.source_fact_id,
            "side": self.side.value,
            "timestamp": _utc_text(self.timestamp),
            "durable_known_at": _utc_text(self.durable_known_at),
            "quantity": self.quantity,
            "price": _decimal_text(self.price),
            "explicit_cost": _decimal_text(self.explicit_cost),
        }
        if include_id:
            document["fragment_id"] = self.fragment_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeExecutionSummary:
    intent_id: str
    side: RuntimeExecutionSide
    requested_quantity: int
    fragments: tuple[RuntimeExecutionFragment, ...]
    execution_stream_complete: bool
    execution_stream_audit_id: str | None
    native_multi_leg: bool = False
    completion: RuntimeFillCompletion = field(init=False)
    filled_quantity: int = field(init=False)
    quantity_weighted_price: Decimal | None = field(init=False)
    total_explicit_cost: Decimal = field(init=False)
    summary_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError("side must be the exact RuntimeExecutionSide type")
        _require_int(self.requested_quantity, "requested_quantity", minimum=1)
        if type(self.fragments) is not tuple or any(
            type(item) is not RuntimeExecutionFragment for item in self.fragments
        ):
            raise RuntimePathContractError(
                "fragments must be an exact tuple of RuntimeExecutionFragment"
            )
        _require_bool(self.execution_stream_complete, "execution_stream_complete")
        execution_stream_audit_id = (
            None
            if self.execution_stream_audit_id is None
            else _require_sha256(
                self.execution_stream_audit_id,
                "execution_stream_audit_id",
            )
        )
        if self.execution_stream_complete and execution_stream_audit_id is None:
            raise RuntimePathContractError(
                "complete execution stream requires an audit identity"
            )
        if _require_bool(self.native_multi_leg, "native_multi_leg"):
            raise RuntimePathContractError(
                "native multi-leg lifecycle is unsupported in Stage 4G v3",
                code="NATIVE_MULTI_LEG_UNSUPPORTED",
            )
        if any(
            item.intent_id != self.intent_id or item.side is not self.side
            for item in self.fragments
        ):
            raise RuntimePathContractError(
                "execution fragments must belong to one intent and side"
            )
        ordered = tuple(
            sorted(
                self.fragments,
                key=lambda item: (
                    item.timestamp,
                    item.execution_id,
                    item.fragment_id,
                ),
            )
        )
        if len({item.execution_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError("execution IDs must be unique")
        if len({item.fragment_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError("execution fragments must be unique")
        object.__setattr__(self, "fragments", ordered)
        filled_quantity = sum(item.quantity for item in ordered)
        if filled_quantity > self.requested_quantity:
            raise RuntimePathContractError(
                "filled quantity exceeds requested quantity"
            )
        with localcontext(_DECIMAL_CONTEXT):
            total_explicit_cost = sum(
                (item.explicit_cost for item in ordered),
                Decimal(0),
            )
            weighted_price = (
                None
                if filled_quantity == 0
                else sum(
                    (item.price * Decimal(item.quantity) for item in ordered),
                    Decimal(0),
                )
                / Decimal(filled_quantity)
            )
        completion = (
            RuntimeFillCompletion.ZERO_FILL
            if filled_quantity == 0
            else (
                RuntimeFillCompletion.COMPLETE
                if filled_quantity == self.requested_quantity
                and self.execution_stream_complete
                else RuntimeFillCompletion.PARTIAL
            )
        )
        object.__setattr__(self, "filled_quantity", filled_quantity)
        object.__setattr__(self, "quantity_weighted_price", weighted_price)
        object.__setattr__(self, "total_explicit_cost", total_explicit_cost)
        object.__setattr__(self, "completion", completion)
        object.__setattr__(
            self,
            "summary_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def stage4g_v3_finalizable(self) -> bool:
        return self.completion is RuntimeFillCompletion.COMPLETE

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_EXECUTION_SUMMARY_SCHEMA,
            "intent_id": self.intent_id,
            "side": self.side.value,
            "requested_quantity": self.requested_quantity,
            "fragments": [item.as_dict() for item in self.fragments],
            "execution_stream_complete": self.execution_stream_complete,
            "execution_stream_audit_id": self.execution_stream_audit_id,
            "native_multi_leg": False,
            "completion": self.completion.value,
            "filled_quantity": self.filled_quantity,
            "quantity_weighted_price": (
                None
                if self.quantity_weighted_price is None
                else _decimal_text(self.quantity_weighted_price)
            ),
            "total_explicit_cost": _decimal_text(self.total_explicit_cost),
            "stage4g_v3_finalizable": self.stage4g_v3_finalizable,
        }
        if include_id:
            document["summary_id"] = self.summary_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeNoEntryEvidence:
    reason: RuntimeNoEntryReason
    execution_summary: RuntimeExecutionSummary
    entry_window_start: datetime
    entry_window_end: datetime
    decided_at: datetime
    execution_policy_id: str
    evidence_ids: tuple[str, ...]
    entry_window_coverage_complete: bool
    entry_window_coverage_fact_id: str | None = None
    actor_id: str | None = None
    actor_authentication_fact_id: str | None = None
    cancellation_fact_id: str | None = None
    rejection_fact_id: str | None = None
    data_invalidity_fact_id: str | None = None
    no_entry_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.reason) is not RuntimeNoEntryReason:
            raise RuntimePathContractError("reason must be RuntimeNoEntryReason")
        if type(self.execution_summary) is not RuntimeExecutionSummary:
            raise RuntimePathContractError(
                "execution_summary must be RuntimeExecutionSummary"
            )
        if (
            self.execution_summary.completion is not RuntimeFillCompletion.ZERO_FILL
            or not self.execution_summary.execution_stream_complete
        ):
            raise RuntimePathContractError(
                "no-entry evidence requires a complete zero-fill execution summary"
            )
        window_start = _require_utc(self.entry_window_start, "entry_window_start")
        window_end = _require_utc(self.entry_window_end, "entry_window_end")
        decided_at = _require_utc(self.decided_at, "decided_at")
        if window_end < window_start or decided_at < window_start:
            raise RuntimePathContractError("no-entry window times are inconsistent")
        object.__setattr__(self, "entry_window_start", window_start)
        object.__setattr__(self, "entry_window_end", window_end)
        object.__setattr__(self, "decided_at", decided_at)
        _require_sha256(self.execution_policy_id, "execution_policy_id")
        if type(self.evidence_ids) is not tuple:
            raise RuntimePathContractError("evidence_ids must be an exact tuple")
        evidence_ids = tuple(
            sorted(_require_sha256(item, "evidence_id") for item in self.evidence_ids)
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise RuntimePathContractError("evidence_ids must be unique")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        coverage_complete = _require_bool(
            self.entry_window_coverage_complete,
            "entry_window_coverage_complete",
        )
        coverage_fact = (
            None
            if self.entry_window_coverage_fact_id is None
            else _require_sha256(
                self.entry_window_coverage_fact_id,
                "entry_window_coverage_fact_id",
            )
        )
        if coverage_complete and coverage_fact is None:
            raise RuntimePathContractError(
                "complete entry-window coverage requires an evidence fact"
            )
        actor_id = _require_optional_text(self.actor_id, "actor_id")
        actor_authentication = (
            None
            if self.actor_authentication_fact_id is None
            else _require_sha256(
                self.actor_authentication_fact_id,
                "actor_authentication_fact_id",
            )
        )
        cancellation = (
            None
            if self.cancellation_fact_id is None
            else _require_sha256(self.cancellation_fact_id, "cancellation_fact_id")
        )
        rejection = (
            None
            if self.rejection_fact_id is None
            else _require_sha256(self.rejection_fact_id, "rejection_fact_id")
        )
        invalidity = (
            None
            if self.data_invalidity_fact_id is None
            else _require_sha256(
                self.data_invalidity_fact_id,
                "data_invalidity_fact_id",
            )
        )
        if self.reason is RuntimeNoEntryReason.ENTRY_EXPIRED:
            if (
                decided_at < window_end
                or not coverage_complete
                or coverage_fact is None
                or any(
                    value is not None
                    for value in (
                        actor_id,
                        actor_authentication,
                        cancellation,
                        rejection,
                        invalidity,
                    )
                )
            ):
                raise RuntimePathContractError(
                    "ENTRY_EXPIRED requires complete window coverage, zero fill, and window expiry"
                )
        elif self.reason is RuntimeNoEntryReason.USER_CANCELLED:
            if (
                actor_id is None
                or actor_authentication is None
                or cancellation is None
                or rejection is not None
                or invalidity is not None
            ):
                raise RuntimePathContractError(
                    "USER_CANCELLED requires authenticated actor and cancellation fact"
                )
        elif self.reason is RuntimeNoEntryReason.ORDER_REJECTED:
            if (
                rejection is None
                or actor_id is not None
                or actor_authentication is not None
                or cancellation is not None
                or invalidity is not None
            ):
                raise RuntimePathContractError(
                    "ORDER_REJECTED requires an explicit rejection fact"
                )
        elif (
            invalidity is None
            or actor_id is not None
            or actor_authentication is not None
            or cancellation is not None
            or rejection is not None
        ):
            raise RuntimePathContractError(
                "DATA_INVALID requires an explicit data-invalidity fact"
            )
        required_facts = tuple(
            item
            for item in (
                coverage_fact if coverage_complete else None,
                actor_authentication,
                cancellation,
                rejection,
                invalidity,
            )
            if item is not None
        )
        if any(item not in evidence_ids for item in required_facts):
            raise RuntimePathContractError(
                "reason-specific facts must be present in evidence_ids"
            )
        object.__setattr__(
            self,
            "no_entry_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_NO_ENTRY_SCHEMA,
            "reason": self.reason.value,
            "execution_summary": self.execution_summary.as_dict(),
            "entry_window_start": _utc_text(self.entry_window_start),
            "entry_window_end": _utc_text(self.entry_window_end),
            "decided_at": _utc_text(self.decided_at),
            "execution_policy_id": self.execution_policy_id,
            "evidence_ids": list(self.evidence_ids),
            "entry_window_coverage_complete": self.entry_window_coverage_complete,
            "entry_window_coverage_fact_id": self.entry_window_coverage_fact_id,
            "actor_id": self.actor_id,
            "actor_authentication_fact_id": self.actor_authentication_fact_id,
            "cancellation_fact_id": self.cancellation_fact_id,
            "rejection_fact_id": self.rejection_fact_id,
            "data_invalidity_fact_id": self.data_invalidity_fact_id,
        }
        if include_id:
            document["no_entry_id"] = self.no_entry_id
        return document


__all__ = [
    "RUNTIME_EXECUTION_SUMMARY_SCHEMA",
    "RUNTIME_NO_ENTRY_SCHEMA",
    "RUNTIME_PATH_CONTRACT_SCHEMA",
    "RUNTIME_PATH_PREFIX_SCHEMA",
    "RUNTIME_PATH_RESOLUTION_SCHEMA",
    "RuntimeCalendarState",
    "RuntimeCoverageState",
    "RuntimeExecutionFragment",
    "RuntimeExecutionSide",
    "RuntimeExecutionSummary",
    "RuntimeFillCompletion",
    "RuntimeFrozenPathFact",
    "RuntimeFrozenPathPrefix",
    "RuntimeNoEntryEvidence",
    "RuntimeNoEntryReason",
    "RuntimeOpenSessionState",
    "RuntimePathBlockerCode",
    "RuntimePathContractError",
    "RuntimePathFactKind",
    "RuntimePathGranularity",
    "RuntimePathObservation",
    "RuntimePathPendingCode",
    "RuntimePathResolution",
    "RuntimePathResolutionState",
    "RuntimePathTerminalReason",
    "RuntimePathWindow",
    "RuntimeSessionEvidence",
    "RuntimeSourceReference",
    "resolve_runtime_path",
]
