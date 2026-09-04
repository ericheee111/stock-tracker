from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import Any

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
    MARKET_EVENT_SEQUENCE_POLICY_V2,
    DecodedTradeTick,
    FindingResolutionState,
    MarketEventSelection,
    MarketEventSelectionVerification,
    MarketEventSequenceFindingKind,
    MarketEventSourceRecord,
    StructuralFixture,
    validate_selection_verification,
)

RUNTIME_PATH_CONTRACT_SCHEMA = "stage4g1-runtime-path-contract-v4"
RUNTIME_PATH_PREFIX_SCHEMA = "stage4g1-runtime-path-prefix-v4"
RUNTIME_PATH_RESOLUTION_SCHEMA = "stage4g1-runtime-path-resolution-v3"
RUNTIME_NO_ENTRY_SCHEMA = "stage4g1-runtime-no-entry-evidence-v3"
RUNTIME_EXECUTION_SUMMARY_SCHEMA = "stage4g1-runtime-execution-summary-v4"
RUNTIME_INTERVAL_BOUNDARY_POLICY_V1 = MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 4096
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


class RuntimePathContractError(ValueError):
    """Raised when path, session, execution, or no-entry evidence is malformed."""

    def __init__(
        self, message: str, *, code: str = "RUNTIME_PATH_CONTRACT_INVALID"
    ) -> None:
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


class RuntimeSecurityStatus(StrEnum):
    TRADABLE = "TRADABLE"
    SUSPENDED = "SUSPENDED"
    NO_TRADE = "NO_TRADE"
    UNKNOWN = "UNKNOWN"


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
    UNPROJECTED_SOURCE_MEMBER = "UNPROJECTED_SOURCE_MEMBER"
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
    ENTRY_BOUNDARY_ORDER_AMBIGUITY = "ENTRY_BOUNDARY_ORDER_AMBIGUITY"


class RuntimeAuthorityKind(StrEnum):
    CALENDAR = "CALENDAR"
    SECURITY_STATUS = "SECURITY_STATUS"
    COVERAGE = "COVERAGE"


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
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
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


def _ohlc_output_hash(
    *,
    symbol: str,
    market: Market,
    interval_start: datetime,
    interval_end: datetime,
    high: Decimal,
    low: Decimal,
    close: Decimal,
) -> str:
    return _hash_document(
        {
            "schema": "stage4g1-runtime-projection-ohlc-output-v1",
            "symbol": symbol,
            "market": market.value,
            "interval_start": _utc_text(interval_start),
            "interval_end": _utc_text(interval_end),
            "high": _decimal_text(high),
            "low": _decimal_text(low),
            "close": _decimal_text(close),
        }
    )


@dataclass(frozen=True, slots=True)
class RuntimeSourceReference(StructuralFixture):
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    source_high_water_append_order: int
    finding_set_digest: str
    sequence_policy_id: str
    selection_id: str
    selection_verification_id: str
    event_id: str
    source_session_id: str
    symbol: str
    market: Market
    event_type: str
    trading_day: date
    session_label_policy_id: str
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
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.source_snapshot_id, "source_snapshot_id")
        _require_sha256(self.source_audit_id, "source_audit_id")
        high_water = _require_int(
            self.source_high_water_append_order,
            "source_high_water_append_order",
        )
        _require_sha256(self.finding_set_digest, "finding_set_digest")
        if self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V2:
            raise RuntimePathContractError("unsupported market-event sequence policy")
        _require_sha256(self.selection_id, "selection_id")
        _require_sha256(self.selection_verification_id, "selection_verification_id")
        _require_sha256(self.event_id, "event_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_text(self.event_type, "event_type", maximum=128)
        _require_date(self.trading_day, "trading_day")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError("unsupported market session label policy")
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
        _require_text(self.source_session_id, "source_session_id", maximum=256)
        _require_sha256(self.source_record_id, "source_record_id")
        append_order = _require_int(
            self.source_append_order,
            "source_append_order",
            minimum=1,
        )
        if append_order > high_water:
            raise RuntimePathContractError(
                "source member exceeds its selection high-water"
            )
        _require_sha256(self.source_record_hash, "source_record_hash")
        _require_sha256(self.raw_payload_sha256, "raw_payload_sha256")
        _require_text(self.parser_id, "parser_id", maximum=256)
        _require_text(self.schema_id, "schema_id", maximum=256)
        object.__setattr__(
            self,
            "source_reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @classmethod
    def from_verified_selection(
        cls,
        *,
        record: MarketEventSourceRecord,
        selection: MarketEventSelection,
        verification: MarketEventSelectionVerification,
    ) -> RuntimeSourceReference:
        validate_selection_verification(selection, verification)
        if (
            type(record) is not MarketEventSourceRecord
            or type(selection) is not MarketEventSelection
            or type(verification) is not MarketEventSelectionVerification
            or verification.selection_id != selection.selection_id
            or verification.snapshot_id != selection.snapshot_id
            or record not in selection.records
        ):
            raise RuntimePathContractError(
                "runtime source member requires an exact verified Selection"
            )
        return cls(
            source_store_id=record.source_store_id,
            source_snapshot_id=selection.snapshot_id,
            source_audit_id=selection.snapshot_audit_id,
            source_high_water_append_order=selection.snapshot_high_water_append_order,
            finding_set_digest=selection.snapshot_finding_set_digest,
            sequence_policy_id=selection.sequence_policy_id,
            selection_id=selection.selection_id,
            selection_verification_id=verification.verification_id,
            event_id=record.event_id,
            source_session_id=record.session_id,
            symbol=record.symbol,
            market=record.market,
            event_type=record.event_type,
            trading_day=record.trading_day,
            session_label_policy_id=record.session_label_policy_id,
            source_record_id=record.source_record_id,
            source_append_order=record.append_order,
            source_record_hash=record.record_content_hash,
            raw_payload_sha256=record.raw_payload_sha256,
            parser_id=record.parser_id,
            schema_id=record.source_schema_id,
            source_time=record.source_time,
            received_at=record.received_at,
            durable_known_at=record.durable_known_at,
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-source-reference-v3",
            "selection_verification_id": self.selection_verification_id,
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "source_high_water_append_order": self.source_high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "sequence_policy_id": self.sequence_policy_id,
            "selection_id": self.selection_id,
            "event_id": self.event_id,
            "source_session_id": self.source_session_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "event_type": self.event_type,
            "trading_day": self.trading_day.isoformat(),
            "session_label_policy_id": self.session_label_policy_id,
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


class TradingSessionSegmentKind(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    OPEN_AUCTION = "OPEN_AUCTION"
    CLOSE_AUCTION = "CLOSE_AUCTION"
    BREAK = "BREAK"
    HALT = "HALT"


TRADING_SEGMENT_POLICY_V1 = "stage4g1-fixture-trading-segments-v1"
TRADE_PROJECTION_POLICY_V1 = "stage4g1-fixture-trade-ohlc-v1"
PATH_PROJECTION_POLICY_V1 = "stage4g1-exact-trade-consumption-v1"


@dataclass(frozen=True, slots=True)
class TradingSessionSegment(StructuralFixture):
    kind: TradingSessionSegmentKind
    start: datetime
    end: datetime
    price_events_allowed: bool
    execution_allowed: bool
    segment_policy_id: str
    calendar_fact_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not TradingSessionSegmentKind:
            raise RuntimePathContractError("typed segment kind is required")
        start = _require_utc(self.start, "segment start")
        end = _require_utc(self.end, "segment end")
        if end <= start:
            raise RuntimePathContractError("segment interval must be non-empty")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        _require_bool(self.price_events_allowed, "price_events_allowed")
        _require_bool(self.execution_allowed, "execution_allowed")
        if self.segment_policy_id != TRADING_SEGMENT_POLICY_V1:
            raise RuntimePathContractError("unsupported segment policy")
        if self.kind in {
            TradingSessionSegmentKind.BREAK,
            TradingSessionSegmentKind.HALT,
        } and (self.price_events_allowed or self.execution_allowed):
            raise RuntimePathContractError(
                "BREAK/HALT cannot carry normal price or execution events"
            )
        if self.execution_allowed and not self.price_events_allowed:
            raise RuntimePathContractError("execution requires observable price events")
        if self.calendar_fact_id is not None:
            _require_sha256(self.calendar_fact_id, "calendar_fact_id")

    def as_dict(self, *, include_binding: bool = True) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": "stage4g1-trading-session-segment-v1",
            "assurance": self.assurance.value,
            "kind": self.kind.value,
            "start": _utc_text(self.start),
            "end": _utc_text(self.end),
            "price_events_allowed": self.price_events_allowed,
            "execution_allowed": self.execution_allowed,
            "segment_policy_id": self.segment_policy_id,
        }
        if include_binding:
            document["calendar_fact_id"] = self.calendar_fact_id
        return document


@dataclass(frozen=True, slots=True)
class CalendarSessionFact(StructuralFixture):
    symbol: str
    market: Market
    trading_day: date
    calendar_state: RuntimeCalendarState
    open_session_index: int | None
    segments: tuple[TradingSessionSegment, ...]
    calendar_policy_id: str
    session_label_policy_id: str = MARKET_SESSION_LABEL_POLICY_V1
    calendar_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_symbol(self.symbol, _require_market(self.market))
        _require_date(self.trading_day, "trading_day")
        _require_sha256(self.calendar_policy_id, "calendar_policy_id")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError("unsupported Calendar session label policy")
        if type(self.calendar_state) is not RuntimeCalendarState:
            raise RuntimePathContractError("typed calendar state is required")
        if type(self.segments) is not tuple or any(
            type(item) is not TradingSessionSegment for item in self.segments
        ):
            raise RuntimePathContractError("typed calendar segments are required")
        if self.calendar_state is RuntimeCalendarState.MARKET_CLOSED:
            if self.open_session_index is not None or self.segments:
                raise RuntimePathContractError(
                    "MARKET_CLOSED cannot contain open segments/index"
                )
        else:
            _require_int(self.open_session_index, "open_session_index")
            if not self.segments:
                raise RuntimePathContractError("OPEN requires exact calendar segments")
        for segment in self.segments:
            segment.__post_init__()
            if any(
                market_session_date(value, self.market, MARKET_SESSION_LABEL_POLICY_V1)
                != self.trading_day
                for value in (segment.start, segment.end - timedelta(microseconds=1))
            ):
                raise RuntimePathContractError("calendar segment trading-day mismatch")
        if any(left.end != right.start for left, right in pairwise(self.segments)):
            raise RuntimePathContractError(
                "calendar segments must partition the session without gaps or overlap"
            )
        fact_id = _hash_document(self.as_dict(include_id=False, include_bindings=False))
        if any(item.calendar_fact_id not in (None, fact_id) for item in self.segments):
            raise RuntimePathContractError("segment belongs to another calendar fact")
        object.__setattr__(self, "calendar_fact_id", fact_id)
        object.__setattr__(
            self,
            "segments",
            tuple(replace(item, calendar_fact_id=fact_id) for item in self.segments),
        )

    @property
    def scheduled_open_at(self) -> datetime | None:
        return self.segments[0].start if self.segments else None

    @property
    def scheduled_close_at(self) -> datetime | None:
        return self.segments[-1].end if self.segments else None

    def permits_price_interval(self, start: datetime, end: datetime) -> bool:
        return any(
            item.price_events_allowed
            and item.start <= start < item.end
            and (end == start or end <= item.end)
            for item in self.segments
        )

    def as_dict(
        self, *, include_id: bool = True, include_bindings: bool = True
    ) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-calendar-session-fact-v1",
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "calendar_state": self.calendar_state.value,
            "open_session_index": self.open_session_index,
            "calendar_policy_id": self.calendar_policy_id,
            "session_label_policy_id": self.session_label_policy_id,
            "segments": [
                item.as_dict(include_binding=include_bindings) for item in self.segments
            ],
        }
        if include_id:
            document["calendar_fact_id"] = self.calendar_fact_id
        return document


@dataclass(frozen=True, slots=True)
class SecurityStatusFact(StructuralFixture):
    symbol: str
    market: Market
    trading_day: date
    status: RuntimeSecurityStatus
    status_policy_id: str
    status_reason: str
    security_status_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_symbol(self.symbol, _require_market(self.market))
        _require_date(self.trading_day, "trading_day")
        if type(self.status) is not RuntimeSecurityStatus:
            raise RuntimePathContractError("typed security status is required")
        _require_sha256(self.status_policy_id, "status_policy_id")
        _require_text(self.status_reason, "status_reason")
        object.__setattr__(
            self,
            "security_status_fact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-security-status-fact-v1",
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "status": self.status.value,
            "status_policy_id": self.status_policy_id,
            "status_reason": self.status_reason,
        }
        if include_id:
            document["security_status_fact_id"] = self.security_status_fact_id
        return document


@dataclass(frozen=True, slots=True)
class SourceCoverageFact(StructuralFixture):
    calendar_fact: CalendarSessionFact
    selection: MarketEventSelection
    verification: MarketEventSelectionVerification
    coverage_through: datetime
    coverage_policy_id: str
    coverage_state: RuntimeCoverageState = field(init=False)
    session_complete: bool = field(init=False)
    segment_coverage: tuple[tuple[TradingSessionSegment, datetime, bool], ...] = field(
        init=False
    )
    coverage_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.calendar_fact) is not CalendarSessionFact:
            raise RuntimePathContractError("coverage requires a typed Calendar fact")
        validate_selection_verification(self.selection, self.verification)
        calendar = self.calendar_fact
        if replace(calendar) != calendar:
            raise RuntimePathContractError("coverage calendar fact was mutated")
        if calendar.calendar_state is not RuntimeCalendarState.OPEN:
            raise RuntimePathContractError(
                "market-closed day cannot claim source coverage"
            )
        if (
            self.selection.symbol != calendar.symbol
            or self.selection.market is not calendar.market
            or self.selection.allowed_event_types != ("TRADE_TICK",)
        ):
            raise RuntimePathContractError(
                "coverage query must match calendar symbol and trade event type"
            )
        through = _require_utc(self.coverage_through, "coverage_through")
        _require_sha256(self.coverage_policy_id, "coverage_policy_id")
        opening, closing = calendar.scheduled_open_at, calendar.scheduled_close_at
        assert opening is not None and closing is not None
        if (
            not opening <= through <= closing
            or through > self.selection.snapshot_audited_at
        ):
            raise RuntimePathContractError(
                "coverage_through is outside calendar/audit bounds"
            )
        object.__setattr__(self, "coverage_through", through)
        intervals = tuple(
            (item.start, min(item.end, through))
            for item in calendar.segments
            if item.kind is not TradingSessionSegmentKind.BREAK and item.start < through
        )
        covered = bool(intervals) and all(
            self.selection.covers_interval(start, end) for start, end in intervals
        )
        relevant = tuple(
            item
            for item in self.selection.relevant_findings
            if item.resolution_state is FindingResolutionState.UNRESOLVED
            and item.intersects(
                calendar.symbol, calendar.market, opening, through, ("TRADE_TICK",)
            )
        )
        if relevant:
            state = (
                RuntimeCoverageState.INCOMPLETE_OUT_OF_ORDER
                if any(
                    item.kind
                    in {
                        MarketEventSequenceFindingKind.OUT_OF_ORDER,
                        MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION,
                    }
                    for item in relevant
                )
                else RuntimeCoverageState.INCOMPLETE_GAP
            )
        elif not covered:
            state = RuntimeCoverageState.INCOMPLETE_GAP
        else:
            state = (
                RuntimeCoverageState.COMPLETE_SESSION
                if through == closing
                else RuntimeCoverageState.COMPLETE_PREFIX
            )
        segment_coverage = tuple(
            (
                segment,
                min(max(through, segment.start), segment.end),
                self.selection.covers_interval(segment.start, min(through, segment.end))
                if through > segment.start
                else False,
            )
            for segment in calendar.segments
            if segment.kind is not TradingSessionSegmentKind.BREAK
        )
        object.__setattr__(self, "segment_coverage", segment_coverage)
        object.__setattr__(self, "coverage_state", state)
        object.__setattr__(
            self, "session_complete", state is RuntimeCoverageState.COMPLETE_SESSION
        )
        object.__setattr__(
            self, "coverage_fact_id", _hash_document(self.as_dict(include_id=False))
        )

    def proves_interval(self, start: datetime, end: datetime) -> bool:
        return (
            self.calendar_fact.permits_price_interval(start, end)
            and end <= self.coverage_through
            and self.selection.covers_interval(start, end)
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-source-coverage-fact-v1",
            "symbol": self.calendar_fact.symbol,
            "market": self.calendar_fact.market.value,
            "trading_day": self.calendar_fact.trading_day.isoformat(),
            "coverage_start": _utc_text(self.calendar_fact.segments[0].start),
            "source_store_id": self.selection.source_store_id,
            "source_snapshot_id": self.selection.snapshot_id,
            "source_audit_id": self.selection.snapshot_audit_id,
            "subscription_manifests": [
                item.as_dict() for item in self.selection.source_session_manifests
            ],
            "finding_digest": self.selection.relevant_finding_set_digest,
            "segment_coverage": [
                {
                    "schema": "stage4g1-source-segment-coverage-v1",
                    "segment": segment.as_dict(),
                    "coverage_through": _utc_text(through),
                    "covered_prefix": covered,
                    "segment_complete": covered and through == segment.end,
                    "selection_id": self.selection.selection_id,
                    "verification_id": self.verification.verification_id,
                }
                for segment, through, covered in self.segment_coverage
            ],
            "calendar_fact": self.calendar_fact.as_dict(),
            "selection_id": self.selection.selection_id,
            "selection_verification_id": self.verification.verification_id,
            "coverage_policy_id": self.coverage_policy_id,
            "coverage_through": _utc_text(self.coverage_through),
            "coverage_state": self.coverage_state.value,
            "session_complete": self.session_complete,
        }
        if include_id:
            document["coverage_fact_id"] = self.coverage_fact_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeAuthorityFactReference(StructuralFixture):
    authority_store_id: str
    fact: CalendarSessionFact | SecurityStatusFact | SourceCoverageFact
    known_at: datetime
    usable_from: datetime
    source: str
    fact_revision: int
    authority_append_order: int
    previous_authority_record_hash: str
    policy_id: str
    authority_kind: RuntimeAuthorityKind = field(init=False)
    fact_schema: str = field(init=False)
    effective_session_date: date = field(init=False)
    fact_payload_sha256: str = field(init=False)
    fact_record_hash: str = field(init=False)
    fact_id: str = field(init=False)
    authority_reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        kinds = {
            CalendarSessionFact: RuntimeAuthorityKind.CALENDAR,
            SecurityStatusFact: RuntimeAuthorityKind.SECURITY_STATUS,
            SourceCoverageFact: RuntimeAuthorityKind.COVERAGE,
        }
        if type(self.fact) not in kinds or replace(self.fact) != self.fact:
            raise RuntimePathContractError(
                "authority requires an exact, valid typed business fact"
            )
        _require_sha256(self.authority_store_id, "authority_store_id")
        known_at = _require_utc(self.known_at, "known_at")
        usable_from = _require_utc(self.usable_from, "usable_from")
        if known_at > usable_from:
            raise RuntimePathContractError("authority known_at exceeds usable_from")
        if (
            isinstance(self.fact, SourceCoverageFact)
            and known_at < self.fact.selection.snapshot_audited_at
        ):
            raise RuntimePathContractError(
                "coverage cannot be known before its selection audit"
            )
        object.__setattr__(self, "known_at", known_at)
        object.__setattr__(self, "usable_from", usable_from)
        _require_text(self.source, "source", maximum=256)
        _require_int(self.fact_revision, "fact_revision", minimum=1)
        _require_int(self.authority_append_order, "authority_append_order", minimum=1)
        _require_sha256(
            self.previous_authority_record_hash, "previous_authority_record_hash"
        )
        _require_sha256(self.policy_id, "policy_id")
        document = self.fact.as_dict()
        calendar = (
            self.fact.calendar_fact
            if isinstance(self.fact, SourceCoverageFact)
            else self.fact
        )
        object.__setattr__(self, "authority_kind", kinds[type(self.fact)])
        object.__setattr__(self, "fact_schema", document["schema"])
        object.__setattr__(self, "effective_session_date", calendar.trading_day)
        object.__setattr__(self, "fact_payload_sha256", _hash_document(document))
        object.__setattr__(
            self, "fact_record_hash", _hash_document(self.fact_document())
        )
        object.__setattr__(
            self,
            "fact_id",
            _hash_document(
                {
                    "schema": "stage4g1-runtime-authority-fact-id-v2",
                    "authority_kind": self.authority_kind.value,
                    "authority_store_id": self.authority_store_id,
                    "authority_append_order": self.authority_append_order,
                    "fact_record_hash": self.fact_record_hash,
                }
            ),
        )
        object.__setattr__(
            self,
            "authority_reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def fact_document(self) -> dict[str, Any]:
        return {
            "schema": "stage4g1-runtime-authority-canonical-fact-v2",
            "assurance": self.assurance.value,
            "authority_kind": self.authority_kind.value,
            "authority_store_id": self.authority_store_id,
            "fact_schema": self.fact_schema,
            "effective_session_date": self.effective_session_date.isoformat(),
            "known_at": _utc_text(self.known_at),
            "usable_from": _utc_text(self.usable_from),
            "source": self.source,
            "fact_revision": self.fact_revision,
            "authority_append_order": self.authority_append_order,
            "previous_authority_record_hash": self.previous_authority_record_hash,
            "policy_id": self.policy_id,
            "fact_payload_sha256": self.fact_payload_sha256,
            "business_fact": self.fact.as_dict(),
        }

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-authority-fact-reference-v3",
            "fact": self.fact_document(),
            "fact_record_hash": self.fact_record_hash,
            "fact_id": self.fact_id,
        }
        if include_id:
            document["authority_reference_id"] = self.authority_reference_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeAuthoritySnapshotBinding(StructuralFixture):
    authority_kind: RuntimeAuthorityKind
    authority_store_id: str
    authority_audit_id: str
    authority_audited_at: datetime
    authority_high_water_append_order: int
    fact_count: int
    fact_manifest_digest: str
    authority_snapshot_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeAuthoritySnapshotBinding requires an exact fact inventory"
        )

    @classmethod
    def from_facts(
        cls,
        *,
        authority_kind: RuntimeAuthorityKind,
        authority_store_id: str,
        authority_audited_at: datetime,
        facts: tuple[RuntimeAuthorityFactReference, ...],
    ) -> RuntimeAuthoritySnapshotBinding:
        if type(authority_kind) is not RuntimeAuthorityKind:
            raise RuntimePathContractError(
                "authority_kind must be RuntimeAuthorityKind"
            )
        _require_sha256(authority_store_id, "authority_store_id")
        audited_at = _require_utc(authority_audited_at, "authority_audited_at")
        if (
            type(facts) is not tuple
            or not facts
            or any(
                type(item) is not RuntimeAuthorityFactReference
                or item.authority_kind is not authority_kind
                or item.authority_store_id != authority_store_id
                or item.known_at > audited_at
                or item.usable_from > audited_at
                for item in facts
            )
        ):
            raise RuntimePathContractError(
                "authority snapshot facts are invalid or future-known"
            )
        chain = "0" * 64
        previous_record = "0" * 64
        previous_known: datetime | None = None
        for order, fact in enumerate(facts, 1):
            if (
                fact.authority_append_order != order
                or fact.previous_authority_record_hash != previous_record
                or (previous_known is not None and fact.known_at < previous_known)
                or replace(fact) != fact
            ):
                raise RuntimePathContractError(
                    "authority append chain is not a contiguous exact prefix"
                )
            previous_record = fact.fact_record_hash
            previous_known = fact.known_at
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-authority-fact-manifest-node-v2",
                    "previous": chain,
                    "fact_id": fact.fact_id,
                    "fact_record_hash": fact.fact_record_hash,
                    "authority_append_order": fact.authority_append_order,
                }
            )
        fact_manifest_digest = _hash_document(
            {
                "schema": "stage4g1-runtime-authority-fact-manifest-root-v2",
                "fact_count": len(facts),
                "chain_head": chain,
            }
        )
        audit_document = {
            "schema": "stage4g1-runtime-authority-audit-v2",
            "authority_kind": authority_kind.value,
            "authority_store_id": authority_store_id,
            "authority_audited_at": _utc_text(audited_at),
            "authority_high_water_append_order": len(facts),
            "fact_count": len(facts),
            "fact_manifest_digest": fact_manifest_digest,
        }
        self = object.__new__(cls)
        for name, value in {
            "authority_kind": authority_kind,
            "authority_store_id": authority_store_id,
            "authority_audit_id": _hash_document(audit_document),
            "authority_audited_at": audited_at,
            "authority_high_water_append_order": len(facts),
            "fact_count": len(facts),
            "fact_manifest_digest": fact_manifest_digest,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "authority_snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-authority-snapshot-binding-v3",
            "authority_kind": self.authority_kind.value,
            "authority_store_id": self.authority_store_id,
            "authority_audit_id": self.authority_audit_id,
            "authority_audited_at": _utc_text(self.authority_audited_at),
            "authority_high_water_append_order": self.authority_high_water_append_order,
            "fact_count": self.fact_count,
            "fact_manifest_digest": self.fact_manifest_digest,
        }
        if include_id:
            document["authority_snapshot_id"] = self.authority_snapshot_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeAuthorityFactSelection(StructuralFixture):
    snapshot: RuntimeAuthoritySnapshotBinding
    facts: tuple[RuntimeAuthorityFactReference, ...]
    selection_policy_id: str
    verification_mode: str
    selection_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeAuthorityFactSelection requires exact snapshot verification"
        )

    @classmethod
    def from_verified_snapshot(
        cls,
        *,
        snapshot: RuntimeAuthoritySnapshotBinding,
        facts: tuple[RuntimeAuthorityFactReference, ...],
        selection_policy_id: str,
    ) -> RuntimeAuthorityFactSelection:
        if type(snapshot) is not RuntimeAuthoritySnapshotBinding:
            raise RuntimePathContractError(
                "authority selection requires RuntimeAuthoritySnapshotBinding"
            )
        _require_sha256(selection_policy_id, "selection_policy_id")
        expected = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=snapshot.authority_kind,
            authority_store_id=snapshot.authority_store_id,
            authority_audited_at=snapshot.authority_audited_at,
            facts=facts,
        )
        if expected != snapshot:
            raise RuntimePathContractError(
                "authority fact inventory disagrees with the exact snapshot"
            )
        self = object.__new__(cls)
        for name, value in {
            "snapshot": snapshot,
            "facts": facts,
            "selection_policy_id": selection_policy_id,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def contains(self, reference: RuntimeAuthorityFactReference) -> bool:
        return any(
            item.fact_id == reference.fact_id
            and item.fact_record_hash == reference.fact_record_hash
            for item in self.facts
        )

    def verify(self) -> None:
        expected_snapshot = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=self.snapshot.authority_kind,
            authority_store_id=self.snapshot.authority_store_id,
            authority_audited_at=self.snapshot.authority_audited_at,
            facts=self.facts,
        )
        if (
            expected_snapshot != self.snapshot
            or self.verification_mode != "FULL_PREFIX_RESCAN"
        ):
            raise RuntimePathContractError(
                "authority fact selection no longer matches its exact snapshot"
            )
        if self.selection_id != _hash_document(self.as_dict(include_id=False)):
            raise RuntimePathContractError("authority fact selection identity mismatch")

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-authority-fact-selection-v2",
            "snapshot": self.snapshot.as_dict(),
            "selection_policy_id": self.selection_policy_id,
            "verification_mode": self.verification_mode,
            "ordered_fact_ids": [item.fact_id for item in self.facts],
            "ordered_fact_record_hashes": [
                item.fact_record_hash for item in self.facts
            ],
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeMarketSourceSnapshotBinding(StructuralFixture):
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    high_water_append_order: int
    finding_set_digest: str
    sequence_policy_id: str
    selection: MarketEventSelection
    selection_verification: MarketEventSelectionVerification
    source_snapshot_binding_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeMarketSourceSnapshotBinding requires a verified Selection"
        )

    @classmethod
    def from_verified_selection(
        cls,
        *,
        selection: MarketEventSelection,
        verification: MarketEventSelectionVerification,
    ) -> RuntimeMarketSourceSnapshotBinding:
        validate_selection_verification(selection, verification)
        if (
            type(selection) is not MarketEventSelection
            or type(verification) is not MarketEventSelectionVerification
            or verification.selection_id != selection.selection_id
            or verification.selection_commitment_id
            != selection.commitment.commitment_id
            or verification.membership_witness_id
            != selection.membership_witness.witness_id
            or verification.snapshot_id != selection.snapshot_id
            or verification.snapshot_audit_id != selection.snapshot_audit_id
            or verification.snapshot_high_water_append_order
            != selection.snapshot_high_water_append_order
        ):
            raise RuntimePathContractError(
                "market source binding requires exact selection verification"
            )
        if selection.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V2:
            raise RuntimePathContractError("unsupported market-event sequence policy")
        self = object.__new__(cls)
        for name, value in {
            "source_store_id": selection.source_store_id,
            "source_snapshot_id": selection.snapshot_id,
            "source_audit_id": selection.snapshot_audit_id,
            "high_water_append_order": selection.snapshot_high_water_append_order,
            "finding_set_digest": selection.snapshot_finding_set_digest,
            "sequence_policy_id": selection.sequence_policy_id,
            "selection": selection,
            "selection_verification": verification,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "source_snapshot_binding_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def verify(self) -> None:
        selection = self.selection
        verification = self.selection_verification
        validate_selection_verification(selection, verification)
        if (
            self.source_store_id != selection.source_store_id
            or self.source_snapshot_id != selection.snapshot_id
            or self.source_audit_id != selection.snapshot_audit_id
            or self.high_water_append_order
            != selection.snapshot_high_water_append_order
            or self.finding_set_digest != selection.snapshot_finding_set_digest
            or self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V2
            or verification.selection_id != selection.selection_id
            or verification.selection_commitment_id
            != selection.commitment.commitment_id
            or verification.membership_witness_id
            != selection.membership_witness.witness_id
            or verification.snapshot_id != selection.snapshot_id
            or verification.snapshot_audit_id != selection.snapshot_audit_id
            or verification.snapshot_high_water_append_order
            != selection.snapshot_high_water_append_order
            or self.source_snapshot_binding_id
            != _hash_document(self.as_dict(include_id=False))
        ):
            raise RuntimePathContractError(
                "market source binding no longer matches its verified Selection"
            )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-market-source-snapshot-binding-v3",
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "high_water_append_order": self.high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "sequence_policy_id": self.sequence_policy_id,
            "selection_id": self.selection.selection_id,
            "selection_commitment_id": self.selection.commitment.commitment_id,
            "selection_membership_witness_id": self.selection.membership_witness.witness_id,
            "selection_verification_id": self.selection_verification.verification_id,
        }
        if include_id:
            document["source_snapshot_binding_id"] = self.source_snapshot_binding_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeProjectionLineage(StructuralFixture):
    projection_policy_id: str
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    source_high_water_append_order: int
    finding_set_digest: str
    selection_id: str
    selection_verification_id: str
    interval_start: datetime
    interval_end: datetime
    interval_boundary_policy_id: str
    coverage_fact_id: str
    input_event_ids: tuple[str, ...]
    input_source_append_orders: tuple[int, ...]
    input_source_record_ids: tuple[str, ...]
    input_source_record_hashes: tuple[str, ...]
    projection_lineage_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.projection_policy_id != TRADE_PROJECTION_POLICY_V1:
            raise RuntimePathContractError("unsupported projection policy")
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.source_snapshot_id, "source_snapshot_id")
        _require_sha256(self.source_audit_id, "source_audit_id")
        high_water = _require_int(
            self.source_high_water_append_order,
            "source_high_water_append_order",
        )
        _require_sha256(self.finding_set_digest, "finding_set_digest")
        _require_sha256(self.selection_id, "selection_id")
        _require_sha256(
            self.selection_verification_id,
            "selection_verification_id",
        )
        start = _require_utc(self.interval_start, "interval_start")
        end = _require_utc(self.interval_end, "interval_end")
        if end <= start:
            raise RuntimePathContractError(
                "projection lineage requires a non-empty interval"
            )
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        if self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported runtime interval boundary policy"
            )
        _require_sha256(self.coverage_fact_id, "coverage_fact_id")
        if type(self.input_event_ids) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_event_ids
        ):
            raise RuntimePathContractError(
                "projection input event IDs must be SHA-256 tuples"
            )
        if (
            type(self.input_source_append_orders) is not tuple
            or not self.input_source_append_orders
        ):
            raise RuntimePathContractError(
                "projection input append orders are required"
            )
        orders = tuple(
            _require_int(item, "input_source_append_order", minimum=1)
            for item in self.input_source_append_orders
        )
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise RuntimePathContractError(
                "projection input append orders must be unique and increasing"
            )
        if orders[-1] > high_water:
            raise RuntimePathContractError(
                "projection input exceeds source high-water mark"
            )
        if type(self.input_source_record_ids) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_source_record_ids
        ):
            raise RuntimePathContractError(
                "projection input record IDs must be SHA-256 tuples"
            )
        if type(self.input_source_record_hashes) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_source_record_hashes
        ):
            raise RuntimePathContractError(
                "projection input record hashes must be SHA-256 tuples"
            )
        if not (
            len(orders)
            == len(self.input_event_ids)
            == len(self.input_source_record_ids)
            == len(self.input_source_record_hashes)
        ):
            raise RuntimePathContractError(
                "projection input lineage columns must have equal length"
            )
        if len(set(self.input_source_record_ids)) != len(orders):
            raise RuntimePathContractError("projection input record IDs must be unique")
        if len(set(self.input_event_ids)) != len(orders):
            raise RuntimePathContractError("projection input event IDs must be unique")
        object.__setattr__(
            self,
            "projection_lineage_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-projection-lineage-v3",
            "projection_policy_id": self.projection_policy_id,
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "source_high_water_append_order": self.source_high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "selection_id": self.selection_id,
            "selection_verification_id": self.selection_verification_id,
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "coverage_fact_id": self.coverage_fact_id,
            "input_event_ids": list(self.input_event_ids),
            "input_source_append_orders": list(self.input_source_append_orders),
            "input_source_record_ids": list(self.input_source_record_ids),
            "input_source_record_hashes": list(self.input_source_record_hashes),
        }
        if include_id:
            document["projection_lineage_id"] = self.projection_lineage_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeProjectionArtifactReference(StructuralFixture):
    selection: MarketEventSelection
    verification: MarketEventSelectionVerification
    decoded_ticks: tuple[DecodedTradeTick, ...]
    projection_policy_id: str
    coverage_fact: RuntimeAuthorityFactReference
    interval_start: datetime
    interval_end: datetime
    created_at: datetime
    durable_known_at: datetime
    symbol: str = field(init=False)
    market: Market = field(init=False)
    trading_day: date = field(init=False)
    high: Decimal = field(init=False)
    low: Decimal = field(init=False)
    close: Decimal = field(init=False)
    interval_boundary_policy_id: str = field(init=False)
    lineage: RuntimeProjectionLineage = field(init=False)
    output_ohlc_sha256: str = field(init=False)
    coverage_fact_id: str = field(init=False)
    projection_artifact_id: str = field(init=False)

    @classmethod
    def create(
        cls,
        *,
        selection: MarketEventSelection,
        verification: MarketEventSelectionVerification,
        decoded_ticks: tuple[DecodedTradeTick, ...],
        projection_policy_id: str,
        coverage_fact: RuntimeAuthorityFactReference,
        interval_start: datetime,
        interval_end: datetime,
        created_at: datetime,
        durable_known_at: datetime,
    ) -> RuntimeProjectionArtifactReference:
        return cls(
            selection,
            verification,
            decoded_ticks,
            projection_policy_id,
            coverage_fact,
            interval_start,
            interval_end,
            created_at,
            durable_known_at,
        )

    def __post_init__(self) -> None:
        validate_selection_verification(self.selection, self.verification)
        if self.projection_policy_id != TRADE_PROJECTION_POLICY_V1:
            raise RuntimePathContractError("unsupported trade projection policy")
        start, end = (
            _require_utc(self.interval_start, "interval_start"),
            _require_utc(self.interval_end, "interval_end"),
        )
        created, known = (
            _require_utc(self.created_at, "created_at"),
            _require_utc(self.durable_known_at, "durable_known_at"),
        )
        if not start < end <= created <= known:
            raise RuntimePathContractError(
                "projection interval/creation/durable causal order is invalid"
            )
        for name, value in (
            ("interval_start", start),
            ("interval_end", end),
            ("created_at", created),
            ("durable_known_at", known),
        ):
            object.__setattr__(self, name, value)
        selection = self.selection
        if (
            selection.start_source_time != start
            or selection.end_source_time != end
            or selection.allowed_event_types != ("TRADE_TICK",)
        ):
            raise RuntimePathContractError(
                "projection requires the exact half-open trade interval Selection"
            )
        if (
            not selection.records
            or type(self.decoded_ticks) is not tuple
            or not self.decoded_ticks
        ):
            raise RuntimePathContractError("empty Selection cannot produce a bar")
        if any(type(item) is not DecodedTradeTick for item in self.decoded_ticks):
            raise RuntimePathContractError("projection requires typed decoded ticks")
        for tick in self.decoded_ticks:
            tick.validate()
            if tick.selection != selection or tick.verification != self.verification:
                raise RuntimePathContractError(
                    "projection tick belongs to another verified Selection"
                )
        ordered = tuple(
            sorted(
                self.decoded_ticks,
                key=lambda tick: (tick.record.source_time, tick.record.append_order),
            )
        )
        if ordered != self.decoded_ticks:
            raise RuntimePathContractError(
                "decoded ticks must be ordered by source time and append order"
            )
        if len(ordered) != len(selection.records) or {
            item.record.source_record_id for item in ordered
        } != {item.source_record_id for item in selection.records}:
            raise RuntimePathContractError(
                "projection must consume every interval member exactly once"
            )
        reference = self.coverage_fact
        if (
            type(reference) is not RuntimeAuthorityFactReference
            or reference.authority_kind is not RuntimeAuthorityKind.COVERAGE
            or type(reference.fact) is not SourceCoverageFact
            or replace(reference) != reference
        ):
            raise RuntimePathContractError(
                "projection requires a typed coverage authority fact"
            )
        coverage = reference.fact
        if (
            coverage.selection.snapshot_id != selection.snapshot_id
            or coverage.selection.snapshot_audit_id != selection.snapshot_audit_id
            or not coverage.proves_interval(start, end)
            or not selection.covers_interval(start, end)
        ):
            raise RuntimePathContractError("projection lacks complete segment coverage")
        if created < max(
            selection.snapshot_audited_at,
            reference.usable_from,
            *(tick.record.durable_known_at for tick in ordered),
        ):
            raise RuntimePathContractError(
                "projection was created before its input/coverage/audit known-at"
            )
        high, low, close = (
            max(tick.price for tick in ordered),
            min(tick.price for tick in ordered),
            ordered[-1].price,
        )
        day = ordered[0].record.trading_day
        if any(tick.record.trading_day != day for tick in ordered):
            raise RuntimePathContractError("projection cannot cross trading days")
        for name, value in {
            "symbol": selection.symbol,
            "market": selection.market,
            "trading_day": day,
            "high": high,
            "low": low,
            "close": close,
            "interval_boundary_policy_id": RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
            "coverage_fact_id": reference.fact_id,
            "output_ohlc_sha256": _ohlc_output_hash(
                symbol=selection.symbol,
                market=selection.market,
                interval_start=start,
                interval_end=end,
                high=high,
                low=low,
                close=close,
            ),
            "lineage": RuntimeProjectionLineage(
                projection_policy_id=self.projection_policy_id,
                source_store_id=selection.source_store_id,
                source_snapshot_id=selection.snapshot_id,
                source_audit_id=selection.snapshot_audit_id,
                source_high_water_append_order=selection.snapshot_high_water_append_order,
                finding_set_digest=selection.snapshot_finding_set_digest,
                selection_id=selection.selection_id,
                selection_verification_id=self.verification.verification_id,
                interval_start=start,
                interval_end=end,
                interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                coverage_fact_id=reference.fact_id,
                input_event_ids=tuple(item.record.event_id for item in ordered),
                input_source_append_orders=tuple(
                    item.record.append_order for item in ordered
                ),
                input_source_record_ids=tuple(
                    item.record.source_record_id for item in ordered
                ),
                input_source_record_hashes=tuple(
                    item.record.record_content_hash for item in ordered
                ),
            ),
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "projection_artifact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-projection-artifact-reference-v2",
            "projection_policy_id": self.projection_policy_id,
            "created_at": _utc_text(self.created_at),
            "durable_known_at": _utc_text(self.durable_known_at),
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "lineage": self.lineage.as_dict(),
            "decoder_policy_id": self.decoded_ticks[0].decoder_policy_id,
            "ordered_decoded_tick_ids": [
                item.decoded_tick_id for item in self.decoded_ticks
            ],
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "output_ohlc_sha256": self.output_ohlc_sha256,
            "coverage_fact_id": self.coverage_fact_id,
        }
        if include_id:
            document["projection_artifact_id"] = self.projection_artifact_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeSessionEvidence(StructuralFixture):
    calendar_reference: RuntimeAuthorityFactReference
    security_status_reference: RuntimeAuthorityFactReference | None = None
    coverage_reference: RuntimeAuthorityFactReference | None = None
    symbol: str = field(init=False)
    market: Market = field(init=False)
    trading_day: date = field(init=False)
    calendar_state: RuntimeCalendarState = field(init=False)
    open_session_index: int | None = field(init=False)
    open_session_state: RuntimeOpenSessionState | None = field(init=False)
    scheduled_open_at: datetime | None = field(init=False)
    scheduled_close_at: datetime | None = field(init=False)
    coverage_state: RuntimeCoverageState = field(init=False)
    session_complete: bool = field(init=False)
    coverage_through: datetime | None = field(init=False)
    session_label_policy_id: str = field(init=False)
    segments: tuple[TradingSessionSegment, ...] = field(init=False)
    session_evidence_id: str = field(init=False)

    @classmethod
    def from_typed_authority_facts(
        cls,
        *,
        calendar_reference: RuntimeAuthorityFactReference,
        security_status_reference: RuntimeAuthorityFactReference | None = None,
        coverage_reference: RuntimeAuthorityFactReference | None = None,
    ) -> RuntimeSessionEvidence:
        return cls(calendar_reference, security_status_reference, coverage_reference)

    def __post_init__(self) -> None:
        references = (
            self.calendar_reference,
            self.security_status_reference,
            self.coverage_reference,
        )
        for reference in references:
            if reference is not None and (
                type(reference) is not RuntimeAuthorityFactReference
                or replace(reference) != reference
            ):
                raise RuntimePathContractError(
                    "session requires valid typed authority references"
                )
        calendar = self.calendar_reference.fact
        if type(calendar) is not CalendarSessionFact:
            raise RuntimePathContractError("calendar_reference kind is invalid")
        for reference in references:
            if (
                reference is not None
                and reference.effective_session_date != calendar.trading_day
            ):
                raise RuntimePathContractError(
                    "authority reference session date mismatch"
                )
        status: RuntimeOpenSessionState | None = None
        through: datetime | None = None
        state = RuntimeCoverageState.NOT_APPLICABLE
        complete = True
        if calendar.calendar_state is RuntimeCalendarState.MARKET_CLOSED:
            if (
                self.security_status_reference is not None
                or self.coverage_reference is not None
            ):
                raise RuntimePathContractError(
                    "MARKET_CLOSED cannot carry status/coverage facts"
                )
        else:
            if (
                self.security_status_reference is None
                or self.coverage_reference is None
            ):
                raise RuntimePathContractError(
                    "OPEN requires typed security status and coverage facts"
                )
            security, coverage = (
                self.security_status_reference.fact,
                self.coverage_reference.fact,
            )
            if (
                type(security) is not SecurityStatusFact
                or type(coverage) is not SourceCoverageFact
            ):
                raise RuntimePathContractError(
                    "session status/coverage authority kinds are invalid"
                )
            if (
                security.symbol != calendar.symbol
                or security.market is not calendar.market
                or coverage.calendar_fact != calendar
            ):
                raise RuntimePathContractError(
                    "authority business facts do not describe the same session"
                )
            status = {
                RuntimeSecurityStatus.TRADABLE: RuntimeOpenSessionState.TRADED,
                RuntimeSecurityStatus.SUSPENDED: RuntimeOpenSessionState.SUSPENDED,
                RuntimeSecurityStatus.NO_TRADE: RuntimeOpenSessionState.NO_TRADE,
                RuntimeSecurityStatus.UNKNOWN: RuntimeOpenSessionState.MISSING_DATA,
            }[security.status]
            through = coverage.coverage_through
            state, complete = coverage.coverage_state, coverage.session_complete
            if status in {
                RuntimeOpenSessionState.SUSPENDED,
                RuntimeOpenSessionState.NO_TRADE,
            }:
                if not complete:
                    raise RuntimePathContractError(
                        "SUSPENDED/NO_TRADE require complete-session coverage"
                    )
                if any(
                    item.trading_day == calendar.trading_day
                    for item in coverage.selection.records
                ):
                    raise RuntimePathContractError(
                        "SUSPENDED/NO_TRADE contradict selected trade events"
                    )
            if status is RuntimeOpenSessionState.MISSING_DATA and state in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            }:
                raise RuntimePathContractError(
                    "MISSING_DATA cannot claim complete market-data coverage"
                )
        for name, value in {
            "symbol": calendar.symbol,
            "market": calendar.market,
            "trading_day": calendar.trading_day,
            "calendar_state": calendar.calendar_state,
            "open_session_index": calendar.open_session_index,
            "open_session_state": status,
            "scheduled_open_at": calendar.scheduled_open_at,
            "scheduled_close_at": calendar.scheduled_close_at,
            "coverage_state": state,
            "session_complete": complete,
            "coverage_through": through,
            "session_label_policy_id": MARKET_SESSION_LABEL_POLICY_V1,
            "segments": calendar.segments,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self, "session_evidence_id", _hash_document(self.as_dict(include_id=False))
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-session-evidence-v3",
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "calendar_state": self.calendar_state.value,
            "open_session_index": self.open_session_index,
            "open_session_state": None
            if self.open_session_state is None
            else self.open_session_state.value,
            "scheduled_open_at": None
            if self.scheduled_open_at is None
            else _utc_text(self.scheduled_open_at),
            "scheduled_close_at": None
            if self.scheduled_close_at is None
            else _utc_text(self.scheduled_close_at),
            "segments": [item.as_dict() for item in self.segments],
            "coverage_state": self.coverage_state.value,
            "session_complete": self.session_complete,
            "coverage_through": None
            if self.coverage_through is None
            else _utc_text(self.coverage_through),
            "session_label_policy_id": self.session_label_policy_id,
            "calendar_reference": self.calendar_reference.as_dict(),
            "security_status_reference": None
            if self.security_status_reference is None
            else self.security_status_reference.as_dict(),
            "coverage_reference": None
            if self.coverage_reference is None
            else self.coverage_reference.as_dict(),
        }
        if include_id:
            document["session_evidence_id"] = self.session_evidence_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathObservation(StructuralFixture):
    calendar_fact: CalendarSessionFact
    granularity: RuntimePathGranularity
    decoded_trade_tick: DecodedTradeTick | None = None
    projection_artifact_reference: RuntimeProjectionArtifactReference | None = None
    symbol: str = field(init=False)
    market: Market = field(init=False)
    open_session_index: int = field(init=False)
    interval_start: datetime = field(init=False)
    interval_end: datetime = field(init=False)
    high: Decimal = field(init=False)
    low: Decimal = field(init=False)
    close: Decimal = field(init=False)
    interval_boundary_policy_id: str = field(init=False)
    source_reference: RuntimeSourceReference | None = field(init=False)
    path_observation_id: str = field(init=False)

    @classmethod
    def from_decoded_trade_tick(
        cls, *, decoded_tick: DecodedTradeTick, calendar_fact: CalendarSessionFact
    ) -> RuntimePathObservation:
        return cls(calendar_fact, RuntimePathGranularity.TICK, decoded_tick)

    @classmethod
    def from_projection(
        cls,
        *,
        projection: RuntimeProjectionArtifactReference,
        calendar_fact: CalendarSessionFact,
        granularity: RuntimePathGranularity = RuntimePathGranularity.MINUTE_BAR,
    ) -> RuntimePathObservation:
        return cls(calendar_fact, granularity, projection_artifact_reference=projection)

    def __post_init__(self) -> None:
        if (
            type(self.calendar_fact) is not CalendarSessionFact
            or replace(self.calendar_fact) != self.calendar_fact
        ):
            raise RuntimePathContractError(
                "observation requires a valid typed calendar fact"
            )
        calendar = self.calendar_fact
        if calendar.calendar_state is not RuntimeCalendarState.OPEN:
            raise RuntimePathContractError(
                "price observation cannot exist on MARKET_CLOSED day"
            )
        source: RuntimeSourceReference | None = None
        if self.granularity is RuntimePathGranularity.TICK:
            tick = self.decoded_trade_tick
            if (
                type(tick) is not DecodedTradeTick
                or self.projection_artifact_reference is not None
            ):
                raise RuntimePathContractError(
                    "TICK requires exactly one decoded trade tick"
                )
            tick.validate()
            symbol, market = tick.record.symbol, tick.record.market
            start = end = tick.record.source_time
            high = low = close = tick.price
            source = RuntimeSourceReference.from_verified_selection(
                record=tick.record,
                selection=tick.selection,
                verification=tick.verification,
            )
        else:
            projection = self.projection_artifact_reference
            if (
                self.granularity
                not in {
                    RuntimePathGranularity.MINUTE_BAR,
                    RuntimePathGranularity.DAILY_BAR,
                }
                or type(projection) is not RuntimeProjectionArtifactReference
                or self.decoded_trade_tick is not None
                or replace(projection) != projection
            ):
                raise RuntimePathContractError(
                    "bar requires exactly one valid deterministic projection"
                )
            symbol, market = projection.symbol, projection.market
            start, end = projection.interval_start, projection.interval_end
            high, low, close = projection.high, projection.low, projection.close
        if (
            symbol != calendar.symbol
            or market is not calendar.market
            or not calendar.permits_price_interval(start, end)
        ):
            raise RuntimePathContractError(
                "price event is outside an allowed calendar segment (BREAK/HALT excluded)"
            )
        index = _require_int(calendar.open_session_index, "open_session_index")
        for name, value in {
            "symbol": symbol,
            "market": market,
            "open_session_index": index,
            "interval_start": start,
            "interval_end": end,
            "high": high,
            "low": low,
            "close": close,
            "source_reference": source,
            "interval_boundary_policy_id": RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self, "path_observation_id", _hash_document(self.as_dict(include_id=False))
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-path-observation-v4",
            "symbol": self.symbol,
            "market": self.market.value,
            "open_session_index": self.open_session_index,
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "granularity": self.granularity.value,
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "calendar_fact_id": self.calendar_fact.calendar_fact_id,
            "decoded_tick_id": None
            if self.decoded_trade_tick is None
            else self.decoded_trade_tick.decoded_tick_id,
            "source_reference": None
            if self.source_reference is None
            else self.source_reference.as_dict(),
            "projection_artifact_reference": None
            if self.projection_artifact_reference is None
            else self.projection_artifact_reference.as_dict(),
        }
        if include_id:
            document["path_observation_id"] = self.path_observation_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeFrozenPathFact(StructuralFixture):
    collection_store_id: str
    collection_append_order: int
    case_id: str
    collection_fact_id: str
    collection_observed_at: datetime
    kind: RuntimePathFactKind
    session_evidence: RuntimeSessionEvidence | None = None
    path_observation: RuntimePathObservation | None = None
    frozen_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.collection_store_id, "collection_store_id")
        _require_sha256(self.case_id, "case_id")
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
            references = tuple(
                item
                for item in (
                    self.session_evidence.calendar_reference,
                    self.session_evidence.security_status_reference,
                    self.session_evidence.coverage_reference,
                )
                if item is not None
            )
            if any(
                item.known_at > observed_at or item.usable_from > observed_at
                for item in references
            ):
                raise RuntimePathContractError(
                    "collection fact predates authority knowledge or usability"
                )
        else:
            if (
                type(self.path_observation) is not RuntimePathObservation
                or self.session_evidence is not None
            ):
                raise RuntimePathContractError(
                    "POINT fact must contain exactly one RuntimePathObservation"
                )
            evidence_known_at = (
                self.path_observation.source_reference.durable_known_at
                if self.path_observation.source_reference is not None
                else self.path_observation.projection_artifact_reference.durable_known_at
                if self.path_observation.projection_artifact_reference is not None
                else observed_at + timedelta(microseconds=1)
            )
            if evidence_known_at > observed_at:
                raise RuntimePathContractError(
                    "collection fact predates the source durable-known time"
                )
        object.__setattr__(
            self,
            "frozen_fact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def source_reference(self) -> RuntimeSourceReference | None:
        if self.kind is RuntimePathFactKind.SESSION:
            return None
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
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-frozen-path-fact-v3",
            "collection_store_id": self.collection_store_id,
            "collection_append_order": self.collection_append_order,
            "case_id": self.case_id,
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


@dataclass(frozen=True, slots=True, init=False)
class RuntimePathStoreSnapshotBinding(StructuralFixture):
    path_store_id: str
    path_snapshot_id: str
    path_audit_id: str
    path_audited_at: datetime
    global_high_water_append_order: int
    record_count: int
    global_manifest_digest: str
    finding_blocker_digest: str
    verification_mode: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimePathStoreSnapshotBinding requires an exact path-store prefix"
        )

    @classmethod
    def from_prefix(
        cls,
        *,
        path_store_id: str,
        path_audited_at: datetime,
        facts: tuple[RuntimeFrozenPathFact, ...],
        finding_blocker_digest: str,
    ) -> RuntimePathStoreSnapshotBinding:
        path_store_id = _require_sha256(path_store_id, "path_store_id")
        audited_at = _require_utc(path_audited_at, "path_audited_at")
        _require_sha256(finding_blocker_digest, "finding_blocker_digest")
        if type(facts) is not tuple or any(
            type(item) is not RuntimeFrozenPathFact for item in facts
        ):
            raise RuntimePathContractError("path snapshot facts must be an exact tuple")
        orders = tuple(item.collection_append_order for item in facts)
        if orders != tuple(range(1, len(facts) + 1)) or any(
            item.collection_store_id != path_store_id
            or item.collection_observed_at > audited_at
            for item in facts
        ):
            raise RuntimePathContractError(
                "path snapshot is not the exact contiguous global prefix"
            )
        if len({item.collection_fact_id for item in facts}) != len(facts) or len(
            {item.frozen_fact_id for item in facts}
        ) != len(facts):
            raise RuntimePathContractError(
                "path snapshot contains duplicate fact identity"
            )
        chain = "0" * 64
        for fact in facts:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-path-store-manifest-node-v1",
                    "previous": chain,
                    "append_order": fact.collection_append_order,
                    "case_id": fact.case_id,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        manifest = _hash_document(
            {
                "schema": "stage4g1-runtime-path-store-manifest-root-v1",
                "record_count": len(facts),
                "chain_head": chain,
            }
        )
        audit_document = {
            "schema": "stage4g1-runtime-path-store-audit-v1",
            "path_store_id": path_store_id,
            "path_audited_at": _utc_text(audited_at),
            "global_high_water_append_order": len(facts),
            "record_count": len(facts),
            "global_manifest_digest": manifest,
            "finding_blocker_digest": finding_blocker_digest,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }
        self = object.__new__(cls)
        for name, value in {
            "path_store_id": path_store_id,
            "path_audit_id": _hash_document(audit_document),
            "path_audited_at": audited_at,
            "global_high_water_append_order": len(facts),
            "record_count": len(facts),
            "global_manifest_digest": manifest,
            "finding_blocker_digest": finding_blocker_digest,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "path_snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-path-store-snapshot-binding-v1",
            "path_store_id": self.path_store_id,
            "path_audit_id": self.path_audit_id,
            "path_audited_at": _utc_text(self.path_audited_at),
            "global_high_water_append_order": self.global_high_water_append_order,
            "record_count": self.record_count,
            "global_manifest_digest": self.global_manifest_digest,
            "finding_blocker_digest": self.finding_blocker_digest,
            "verification_mode": self.verification_mode,
        }
        if include_id:
            document["path_snapshot_id"] = self.path_snapshot_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeCaseFactSelection(StructuralFixture):
    path_store_snapshot: RuntimePathStoreSnapshotBinding
    case_id: str
    symbol: str
    market: Market
    selection_policy_id: str
    facts: tuple[RuntimeFrozenPathFact, ...]
    case_manifest_commitment: str
    verification_mode: str
    finding_blocker_digest: str
    selection_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeCaseFactSelection requires exact path-store verification"
        )

    @classmethod
    def from_verified_snapshot(
        cls,
        *,
        path_store_snapshot: RuntimePathStoreSnapshotBinding,
        all_facts: tuple[RuntimeFrozenPathFact, ...],
        case_id: str,
        symbol: str,
        market: Market,
        selection_policy_id: str,
    ) -> RuntimeCaseFactSelection:
        if type(path_store_snapshot) is not RuntimePathStoreSnapshotBinding:
            raise RuntimePathContractError(
                "case selection requires RuntimePathStoreSnapshotBinding"
            )
        _require_sha256(case_id, "case_id")
        market = _require_market(market)
        _require_symbol(symbol, market)
        _require_sha256(selection_policy_id, "selection_policy_id")
        expected_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=path_store_snapshot.path_store_id,
            path_audited_at=path_store_snapshot.path_audited_at,
            facts=all_facts,
            finding_blocker_digest=path_store_snapshot.finding_blocker_digest,
        )
        if expected_snapshot != path_store_snapshot:
            raise RuntimePathContractError(
                "path-store prefix disagrees with the frozen snapshot"
            )
        selected = tuple(item for item in all_facts if item.case_id == case_id)
        if not selected or any(
            item.symbol != symbol or item.market is not market for item in selected
        ):
            raise RuntimePathContractError(
                "case selection is empty or contains inconsistent facts"
            )
        chain = "0" * 64
        for fact in selected:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-case-selection-node-v1",
                    "previous": chain,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        commitment = _hash_document(
            {
                "schema": "stage4g1-runtime-case-selection-root-v1",
                "case_id": case_id,
                "selection_policy_id": selection_policy_id,
                "fact_count": len(selected),
                "chain_head": chain,
            }
        )
        self = object.__new__(cls)
        for name, value in {
            "path_store_snapshot": path_store_snapshot,
            "case_id": case_id,
            "symbol": symbol,
            "market": market,
            "selection_policy_id": selection_policy_id,
            "facts": selected,
            "case_manifest_commitment": commitment,
            "verification_mode": "FULL_PREFIX_RESCAN",
            "finding_blocker_digest": path_store_snapshot.finding_blocker_digest,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def verify(self) -> None:
        orders = tuple(item.collection_append_order for item in self.facts)
        if orders != tuple(sorted(orders)) or any(
            item.case_id != self.case_id
            or item.symbol != self.symbol
            or item.market is not self.market
            or item.collection_store_id != self.path_store_snapshot.path_store_id
            or item.collection_append_order
            > self.path_store_snapshot.global_high_water_append_order
            for item in self.facts
        ):
            raise RuntimePathContractError(
                "case selection facts are outside the verified case query"
            )
        chain = "0" * 64
        for fact in self.facts:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-case-selection-node-v1",
                    "previous": chain,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        expected_commitment = _hash_document(
            {
                "schema": "stage4g1-runtime-case-selection-root-v1",
                "case_id": self.case_id,
                "selection_policy_id": self.selection_policy_id,
                "fact_count": len(self.facts),
                "chain_head": chain,
            }
        )
        if (
            self.case_manifest_commitment != expected_commitment
            or self.verification_mode != "FULL_PREFIX_RESCAN"
            or self.finding_blocker_digest
            != self.path_store_snapshot.finding_blocker_digest
        ):
            raise RuntimePathContractError("case selection commitment mismatch")
        if self.selection_id != _hash_document(self.as_dict(include_id=False)):
            raise RuntimePathContractError("case selection identity mismatch")

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-case-fact-selection-v2",
            "path_store_snapshot": self.path_store_snapshot.as_dict(),
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "selection_policy_id": self.selection_policy_id,
            "ordered_case_fact_ids": [item.collection_fact_id for item in self.facts],
            "ordered_case_record_hashes": [item.frozen_fact_id for item in self.facts],
            "case_manifest_commitment": self.case_manifest_commitment,
            "verification_mode": self.verification_mode,
            "finding_blocker_digest": self.finding_blocker_digest,
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


def _source_reference_matches_record(
    reference: RuntimeSourceReference,
    record: MarketEventSourceRecord,
    binding: RuntimeMarketSourceSnapshotBinding,
) -> bool:
    return (
        reference.source_store_id == binding.source_store_id == record.source_store_id
        and reference.source_snapshot_id == binding.source_snapshot_id
        and reference.source_audit_id == binding.source_audit_id
        and reference.source_high_water_append_order == binding.high_water_append_order
        and reference.finding_set_digest == binding.finding_set_digest
        and reference.sequence_policy_id == binding.sequence_policy_id
        and reference.event_id == record.event_id
        and reference.source_session_id == record.session_id
        and reference.symbol == record.symbol
        and reference.market is record.market
        and reference.event_type == record.event_type
        and reference.trading_day == record.trading_day
        and reference.session_label_policy_id == record.session_label_policy_id
        and reference.source_record_id == record.source_record_id
        and reference.source_append_order == record.append_order
        and reference.source_record_hash == record.record_content_hash
        and reference.raw_payload_sha256 == record.raw_payload_sha256
        and reference.parser_id == record.parser_id
        and reference.schema_id == record.source_schema_id
        and reference.source_time == record.source_time
        and reference.received_at == record.received_at
        and reference.durable_known_at == record.durable_known_at
    )


@dataclass(frozen=True, slots=True)
class PathProjectionManifest(StructuralFixture):
    selection: MarketEventSelection
    verification: MarketEventSelectionVerification
    observations: tuple[RuntimePathObservation, ...]
    path_projection_policy_id: str = PATH_PROJECTION_POLICY_V1
    consumed_source_record_ids: tuple[str, ...] = field(init=False)
    consumed_decoded_tick_ids: tuple[str, ...] = field(init=False)
    produced_path_observation_ids: tuple[str, ...] = field(init=False)
    interval_coverage: tuple[tuple[datetime, datetime], ...] = field(init=False)
    mapping_commitment: str = field(init=False)
    projection_manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        validate_selection_verification(self.selection, self.verification)
        if self.path_projection_policy_id != PATH_PROJECTION_POLICY_V1:
            raise RuntimePathContractError("unsupported path projection policy")
        if type(self.observations) is not tuple:
            raise RuntimePathContractError(
                "manifest observations must be an exact tuple"
            )
        mappings: list[dict[str, Any]] = []
        source_ids: list[str] = []
        tick_ids: list[str] = []
        for point in self.observations:
            if type(point) is not RuntimePathObservation or replace(point) != point:
                raise RuntimePathContractError(
                    "manifest observation is invalid or mutated"
                )
            ticks = (
                (point.decoded_trade_tick,)
                if point.decoded_trade_tick is not None
                else point.projection_artifact_reference.decoded_ticks
                if point.projection_artifact_reference is not None
                else ()
            )
            for tick in ticks:
                if tick.record not in self.selection.records:
                    raise RuntimePathContractError(
                        "path input is outside verified source Selection"
                    )
                source_ids.append(tick.record.source_record_id)
                tick_ids.append(tick.decoded_tick_id)
                mappings.append(
                    {
                        "source_record_id": tick.record.source_record_id,
                        "decoded_tick_id": tick.decoded_tick_id,
                        "path_observation_id": point.path_observation_id,
                        "interval_start": _utc_text(point.interval_start),
                        "interval_end": _utc_text(point.interval_end),
                    }
                )
        if len(source_ids) != len(set(source_ids)):
            raise RuntimePathContractError("source member was consumed more than once")
        for name, value in {
            "consumed_source_record_ids": tuple(source_ids),
            "consumed_decoded_tick_ids": tuple(tick_ids),
            "produced_path_observation_ids": tuple(
                item.path_observation_id for item in self.observations
            ),
            "interval_coverage": tuple(
                (item.interval_start, item.interval_end) for item in self.observations
            ),
            "mapping_commitment": _hash_document(
                {"schema": "stage4g1-path-projection-mapping-v1", "mappings": mappings}
            ),
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "projection_manifest_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-path-projection-manifest-v1",
            "path_projection_policy_id": self.path_projection_policy_id,
            "selection_id": self.selection.selection_id,
            "selection_verification_id": self.verification.verification_id,
            "consumed_source_record_ids": list(self.consumed_source_record_ids),
            "consumed_decoded_tick_ids": list(self.consumed_decoded_tick_ids),
            "produced_path_observation_ids": list(self.produced_path_observation_ids),
            "interval_coverage": [
                [_utc_text(start), _utc_text(end)]
                for start, end in self.interval_coverage
            ],
            "mapping_commitment": self.mapping_commitment,
        }
        if include_id:
            document["projection_manifest_id"] = self.projection_manifest_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeFrozenPathPrefix(StructuralFixture):
    case_id: str
    symbol: str
    market: Market
    path_store_snapshot: RuntimePathStoreSnapshotBinding
    case_selection: RuntimeCaseFactSelection
    market_source_snapshot: RuntimeMarketSourceSnapshotBinding
    authority_fact_selections: tuple[RuntimeAuthorityFactSelection, ...]
    frozen_at: datetime
    facts: tuple[RuntimeFrozenPathFact, ...]
    projection_manifest: PathProjectionManifest
    prefix_id: str

    def __init__(self) -> None:
        raise TypeError("RuntimeFrozenPathPrefix requires a verified case selection")

    @classmethod
    def from_verified_case_selection(
        cls,
        *,
        case_selection: RuntimeCaseFactSelection,
        market_source_snapshot: RuntimeMarketSourceSnapshotBinding,
        authority_fact_selections: tuple[RuntimeAuthorityFactSelection, ...],
        frozen_at: datetime,
    ) -> RuntimeFrozenPathPrefix:
        if type(case_selection) is not RuntimeCaseFactSelection:
            raise RuntimePathContractError(
                "case_selection must be RuntimeCaseFactSelection"
            )
        case_selection.verify()
        if type(market_source_snapshot) is not RuntimeMarketSourceSnapshotBinding:
            raise RuntimePathContractError(
                "market_source_snapshot must be a verified source Selection"
            )
        market_source_snapshot.verify()
        if (
            market_source_snapshot.selection.symbol != case_selection.symbol
            or market_source_snapshot.selection.market is not case_selection.market
            or "TRADE_TICK" not in market_source_snapshot.selection.allowed_event_types
        ):
            raise RuntimePathContractError(
                "market source Selection does not cover the frozen case identity"
            )
        if type(authority_fact_selections) is not tuple or any(
            type(item) is not RuntimeAuthorityFactSelection
            for item in authority_fact_selections
        ):
            raise RuntimePathContractError(
                "authority_fact_selections must be an exact tuple"
            )
        ordered_selections = tuple(
            sorted(
                authority_fact_selections,
                key=lambda item: (
                    item.snapshot.authority_kind.value,
                    item.snapshot.authority_store_id,
                    item.snapshot.authority_audit_id,
                ),
            )
        )
        if ordered_selections != authority_fact_selections or len(
            {item.snapshot.authority_kind for item in ordered_selections}
        ) != len(ordered_selections):
            raise RuntimePathContractError(
                "authority selections must be canonical and unique by kind"
            )
        for selection in authority_fact_selections:
            selection.verify()
        frozen_at = _require_utc(frozen_at, "frozen_at")
        facts = case_selection.facts
        collection_orders = tuple(item.collection_append_order for item in facts)
        if collection_orders != tuple(sorted(collection_orders)) or len(
            set(collection_orders)
        ) != len(collection_orders):
            raise RuntimePathContractError(
                "frozen path facts must use unique increasing collection order"
            )
        if collection_orders and collection_orders[-1] > (
            case_selection.path_store_snapshot.global_high_water_append_order
        ):
            raise RuntimePathContractError(
                "case fact exceeds path-store high-water mark"
            )
        if any(
            item.collection_store_id != case_selection.path_store_snapshot.path_store_id
            or item.case_id != case_selection.case_id
            or item.symbol != case_selection.symbol
            or item.market is not case_selection.market
            or item.collection_observed_at > frozen_at
            for item in facts
        ):
            raise RuntimePathContractError(
                "path prefix fact identity or knowledge time is inconsistent"
            )
        if case_selection.path_store_snapshot.path_audited_at > frozen_at:
            raise RuntimePathContractError(
                "path-store audit is future-known at prefix freeze time"
            )
        if market_source_snapshot.selection.snapshot_audited_at > frozen_at:
            raise RuntimePathContractError(
                "market-source audit is future-known at prefix freeze time"
            )
        authority_selections = {
            item.snapshot.authority_kind: item for item in authority_fact_selections
        }
        used_authority_kinds: set[RuntimeAuthorityKind] = set()
        sessions_by_index: dict[int, RuntimeSessionEvidence] = {}
        for fact in facts:
            if fact.session_evidence is None:
                continue
            session = fact.session_evidence
            if replace(session) != session:
                raise RuntimePathContractError("derived session was mutated")
            coverage_reference = session.coverage_reference
            if coverage_reference is not None:
                coverage = coverage_reference.fact
                if (
                    type(coverage) is not SourceCoverageFact
                    or coverage.selection.snapshot_id
                    != market_source_snapshot.source_snapshot_id
                ):
                    raise RuntimePathContractError(
                        "session coverage is outside frozen market-source snapshot"
                    )
            if session.open_session_index is not None:
                sessions_by_index[session.open_session_index] = session
            for reference in (
                session.calendar_reference,
                session.security_status_reference,
                session.coverage_reference,
            ):
                if reference is None:
                    continue
                used_authority_kinds.add(reference.authority_kind)
                selection = authority_selections.get(reference.authority_kind)
                if (
                    selection is None
                    or reference.authority_store_id
                    != selection.snapshot.authority_store_id
                    or reference.authority_append_order
                    > selection.snapshot.authority_high_water_append_order
                    or not selection.contains(reference)
                ):
                    raise RuntimePathContractError(
                        "authority fact is absent from its verified store selection"
                    )
                if (
                    reference.known_at > frozen_at
                    or reference.usable_from > frozen_at
                    or selection.snapshot.authority_audited_at > frozen_at
                ):
                    raise RuntimePathContractError(
                        "authority fact or audit was future-known at prefix freeze time"
                    )
        if set(authority_selections) != used_authority_kinds:
            raise RuntimePathContractError(
                "authority selections do not match the referenced authority set"
            )

        selected_records = {
            item.source_record_id: item
            for item in market_source_snapshot.selection.records
        }
        for fact in facts:
            observation = fact.path_observation
            if observation is None:
                continue
            session = sessions_by_index.get(observation.open_session_index)
            if (
                session is None
                or observation.calendar_fact != session.calendar_reference.fact
            ):
                raise RuntimePathContractError(
                    "observation is outside exact session Calendar fact"
                )
            source_reference = observation.source_reference
            if source_reference is not None:
                record = selected_records.get(source_reference.source_record_id)
                session = sessions_by_index.get(observation.open_session_index)
                if (
                    record is None
                    or session is None
                    or record.trading_day != session.trading_day
                    or not _source_reference_matches_record(
                        source_reference,
                        record,
                        market_source_snapshot,
                    )
                ):
                    raise RuntimePathContractError(
                        "path point source member is absent from verified Selection"
                    )
                continue
            projection = observation.projection_artifact_reference
            assert projection is not None
            lineage = projection.lineage
            if (
                lineage.source_store_id != market_source_snapshot.source_store_id
                or lineage.source_snapshot_id
                != market_source_snapshot.source_snapshot_id
                or lineage.source_audit_id != market_source_snapshot.source_audit_id
                or lineage.source_high_water_append_order
                != market_source_snapshot.high_water_append_order
                or lineage.finding_set_digest
                != market_source_snapshot.finding_set_digest
            ):
                raise RuntimePathContractError(
                    "projection lineage disagrees with frozen market source snapshot"
                )
            expected_interval_records = tuple(
                item
                for item in market_source_snapshot.selection.records
                if projection.interval_start
                <= item.source_time
                < projection.interval_end
            )
            if projection.selection.records != expected_interval_records:
                raise RuntimePathContractError(
                    "projection omitted an eligible frozen interval member"
                )
            session = sessions_by_index.get(observation.open_session_index)
            if (
                session is None
                or session.coverage_reference is None
                or lineage.coverage_fact_id != session.coverage_reference.fact_id
                or projection.trading_day != session.trading_day
            ):
                raise RuntimePathContractError(
                    "projection lineage is not bound to session coverage authority"
                )
            for event_id, append_order, record_id, record_hash in zip(
                lineage.input_event_ids,
                lineage.input_source_append_orders,
                lineage.input_source_record_ids,
                lineage.input_source_record_hashes,
                strict=True,
            ):
                record = selected_records.get(record_id)
                if record is None or (
                    record.event_id != event_id
                    or record.append_order != append_order
                    or record.record_content_hash != record_hash
                    or record.symbol != observation.symbol
                    or record.market is not observation.market
                    or record.event_type != "TRADE_TICK"
                    or record.trading_day != session.trading_day
                    or record.durable_known_at > projection.created_at
                    or not projection.interval_start
                    <= record.source_time
                    < projection.interval_end
                ):
                    raise RuntimePathContractError(
                        "projection input is absent from the verified raw Selection"
                    )
        if any(
            session.coverage_state is RuntimeCoverageState.COMPLETE_SESSION
            for session in sessions_by_index.values()
        ):
            if any(
                not manifest.has_sequence_start_proof
                for manifest in market_source_snapshot.selection.source_session_manifests
            ):
                raise RuntimePathContractError(
                    "COMPLETE_SESSION requires source sequence start capability proof"
                )
            if market_source_snapshot.selection.relevant_findings:
                raise RuntimePathContractError(
                    "COMPLETE_SESSION conflicts with verified source sequence findings"
                )
        selection = market_source_snapshot.selection
        for session in sessions_by_index.values():
            if (
                session.scheduled_open_at is None
                or session.coverage_through is None
                or session.coverage_state
                not in {
                    RuntimeCoverageState.COMPLETE_PREFIX,
                    RuntimeCoverageState.COMPLETE_SESSION,
                }
            ):
                continue
            if (
                selection.start_source_time > session.scheduled_open_at
                or selection.end_source_time < session.coverage_through
                or selection.snapshot_audit_id != market_source_snapshot.source_audit_id
            ):
                raise RuntimePathContractError(
                    "market source Selection does not cover the claimed session interval"
                )
        for values, label in (
            (
                tuple(item.collection_fact_id for item in facts),
                "collection fact",
            ),
            (tuple(item.frozen_fact_id for item in facts), "frozen fact"),
        ):
            if len(values) != len(set(values)):
                raise RuntimePathContractError(
                    f"path prefix contains duplicate {label} identities"
                )
        session_days = tuple(
            item.session_evidence.trading_day
            for item in facts
            if item.session_evidence is not None
        )
        if len(session_days) != len(set(session_days)):
            raise RuntimePathContractError(
                "path prefix contains duplicate calendar-day evidence"
            )
        point_ids = tuple(
            item.path_observation.path_observation_id
            for item in facts
            if item.path_observation is not None
        )
        if len(point_ids) != len(set(point_ids)):
            raise RuntimePathContractError(
                "path prefix contains duplicate path observations"
            )
        self = object.__new__(cls)
        for name, value in {
            "case_id": case_selection.case_id,
            "symbol": case_selection.symbol,
            "market": case_selection.market,
            "path_store_snapshot": case_selection.path_store_snapshot,
            "case_selection": case_selection,
            "market_source_snapshot": market_source_snapshot,
            "authority_fact_selections": authority_fact_selections,
            "frozen_at": frozen_at,
            "facts": facts,
            "projection_manifest": PathProjectionManifest(
                market_source_snapshot.selection,
                market_source_snapshot.selection_verification,
                tuple(
                    item.path_observation
                    for item in facts
                    if item.path_observation is not None
                ),
            ),
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "prefix_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": RUNTIME_PATH_PREFIX_SCHEMA,
            "projection_manifest": self.projection_manifest.as_dict(),
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "path_store_snapshot": self.path_store_snapshot.as_dict(),
            "case_selection_id": self.case_selection.selection_id,
            "market_source_snapshot": self.market_source_snapshot.as_dict(),
            "authority_fact_selections": [
                item.as_dict() for item in self.authority_fact_selections
            ],
            "frozen_at": _utc_text(self.frozen_at),
            "facts": [item.as_dict() for item in self.facts],
        }
        if include_id:
            document["prefix_id"] = self.prefix_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathWindow(StructuralFixture):
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
    session_label_policy_id: str
    interval_boundary_policy_id: str
    window_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.case_id, "case_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        entry_filled_at = _require_utc(self.entry_filled_at, "entry_filled_at")
        object.__setattr__(self, "entry_filled_at", entry_filled_at)
        _require_date(self.entry_trading_day, "entry_trading_day")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError("unsupported market session label policy")
        if self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported runtime interval boundary policy"
            )
        if (
            market_session_date(
                entry_filled_at,
                market,
                self.session_label_policy_id,
            )
            != self.entry_trading_day
        ):
            raise RuntimePathContractError(
                "entry_trading_day disagrees with market-local entry time"
            )
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
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-path-window-v2",
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
            "session_label_policy_id": self.session_label_policy_id,
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
        }
        if include_id:
            document["window_id"] = self.window_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathResolution(StructuralFixture):
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
            raise RuntimePathContractError("state must be RuntimePathResolutionState")
        _require_sha256(self.window_id, "window_id")
        _require_sha256(self.prefix_id, "prefix_id")
        if (
            self.terminal_reason is not None
            and type(self.terminal_reason) is not RuntimePathTerminalReason
        ):
            raise RuntimePathContractError(
                "terminal_reason must be RuntimePathTerminalReason or None"
            )
        if (
            self.pending_code is not None
            and type(self.pending_code) is not RuntimePathPendingCode
        ):
            raise RuntimePathContractError(
                "pending_code must be RuntimePathPendingCode or None"
            )
        if type(self.blocker_codes) is not tuple or any(
            type(item) is not RuntimePathBlockerCode for item in self.blocker_codes
        ):
            raise RuntimePathContractError(
                "blocker_codes must be a tuple of RuntimePathBlockerCode"
            )
        if (
            tuple(sorted(set(self.blocker_codes), key=lambda item: item.value))
            != self.blocker_codes
        ):
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
            if (
                self.state
                in {
                    RuntimePathResolutionState.TARGET,
                    RuntimePathResolutionState.STOP,
                }
                and first_fact is None
            ):
                raise RuntimePathContractError(
                    "barrier resolution requires first-touch evidence"
                )
            if (
                self.state is RuntimePathResolutionState.TIMEOUT
                and first_fact is not None
            ):
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
                raise RuntimePathContractError(
                    "OPEN resolution fields are inconsistent"
                )
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
            "assurance": self.assurance.value,
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
    expected_prefix = RuntimeFrozenPathPrefix.from_verified_case_selection(
        case_selection=prefix.case_selection,
        market_source_snapshot=prefix.market_source_snapshot,
        authority_fact_selections=prefix.authority_fact_selections,
        frozen_at=prefix.frozen_at,
    )
    if expected_prefix != prefix:
        raise RuntimePathContractError("frozen path prefix was mutated")
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
        or not any(
            segment.execution_allowed
            and segment.start <= window.entry_filled_at < segment.end
            for segment in entry_session.segments
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
        if session.coverage_state in {
            RuntimeCoverageState.COMPLETE_PREFIX,
            RuntimeCoverageState.COMPLETE_SESSION,
        } and (
            session.coverage_through is None
            or observation.interval_end > session.coverage_through
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
            if (
                observation.granularity is RuntimePathGranularity.TICK
                and observation.interval_start == window.entry_filled_at
            ):
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,
                    effective_session_index=index,
                )
            if observation.interval_end <= window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.POINT_BEFORE_ENTRY_WINDOW,
                    effective_session_index=index,
                )
            if observation.interval_start <= window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,
                    effective_session_index=index,
                )
        points_by_session.setdefault(index, []).append(fact)

    max_known_open_index = (
        max(session_by_index) if session_by_index else window.entry_session_index - 1
    )
    manifest = prefix.projection_manifest
    if replace(manifest) != manifest:
        raise RuntimePathContractError("projection manifest was mutated")
    consumed = set(manifest.consumed_source_record_ids)
    horizon_session_for_membership = session_by_index.get(horizon_index)
    source_cutoff = (
        horizon_session_for_membership.scheduled_close_at
        if horizon_session_for_membership is not None
        else prefix.market_source_snapshot.selection.end_source_time
    )
    assert source_cutoff is not None
    if any(
        window.entry_filled_at <= record.source_time < source_cutoff
        and record.source_record_id not in consumed
        for record in prefix.market_source_snapshot.selection.records
    ):
        return _blocked_resolution(
            window, prefix, RuntimePathBlockerCode.UNPROJECTED_SOURCE_MEMBER
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
            eligible_source_records = tuple(
                record
                for record in prefix.market_source_snapshot.selection.records
                if record.symbol == window.symbol
                and record.market is window.market
                and record.event_type == "TRADE_TICK"
                and record.trading_day == session.trading_day
                and (
                    session_index != window.entry_session_index
                    or record.source_time > window.entry_filled_at
                )
            )
            if eligible_source_records:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.TRADED_SESSION_WITHOUT_POINTS,
                    effective_session_index=session_index,
                )
            if session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX:
                return RuntimePathResolution(
                    state=RuntimePathResolutionState.OPEN,
                    window_id=window.window_id,
                    prefix_id=prefix.prefix_id,
                    terminal_reason=None,
                    pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
                    blocker_codes=(),
                    first_touch_collection_fact_id=None,
                    first_touch_observation_id=None,
                    effective_session_index=session_index,
                )

        def point_order_key(
            item: RuntimeFrozenPathFact,
        ) -> tuple[datetime, datetime, int, str]:
            observation = item.path_observation
            if observation is None:
                raise RuntimePathContractError(
                    "point collection contains a non-point fact"
                )
            source_order = (
                observation.source_reference.source_append_order
                if observation.source_reference is not None
                else observation.projection_artifact_reference.lineage.input_source_append_orders[
                    0
                ]
                if observation.projection_artifact_reference is not None
                else 0
            )
            return (
                observation.interval_start,
                observation.interval_end,
                source_order,
                observation.path_observation_id,
            )

        ordered = sorted(facts, key=point_order_key)
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
        if (
            not horizon_session.session_complete
            and horizon_session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
        ):
            return RuntimePathResolution(
                state=RuntimePathResolutionState.OPEN,
                window_id=window.window_id,
                prefix_id=prefix.prefix_id,
                terminal_reason=None,
                pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
                blocker_codes=(),
                first_touch_collection_fact_id=None,
                first_touch_observation_id=None,
                effective_session_index=horizon_index,
            )
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
class RuntimeExecutionFragment(StructuralFixture):
    intent_id: str
    execution_id: str
    source_fact_id: str
    source_callback_id: str
    source_append_order: int
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
        _require_sha256(self.source_callback_id, "source_callback_id")
        _require_int(self.source_append_order, "source_append_order", minimum=1)
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError(
                "side must be the exact RuntimeExecutionSide type"
            )
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
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-execution-fragment-v2",
            "intent_id": self.intent_id,
            "execution_id": self.execution_id,
            "source_fact_id": self.source_fact_id,
            "source_callback_id": self.source_callback_id,
            "source_append_order": self.source_append_order,
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
class RuntimeExecutionSummary(StructuralFixture):
    intent_id: str
    side: RuntimeExecutionSide
    requested_quantity: int
    fragments: tuple[RuntimeExecutionFragment, ...]
    execution_source_store_id: str
    execution_snapshot_id: str
    execution_audit_id: str
    execution_audited_at: datetime
    execution_high_water_append_order: int
    execution_selection_id: str
    membership_verification_id: str
    execution_stream_complete: bool
    native_multi_leg: bool = False
    completion: RuntimeFillCompletion = field(init=False)
    filled_quantity: int = field(init=False)
    quantity_weighted_price: Decimal | None = field(init=False)
    total_explicit_cost: Decimal = field(init=False)
    summary_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError(
                "side must be the exact RuntimeExecutionSide type"
            )
        _require_int(self.requested_quantity, "requested_quantity", minimum=1)
        for name in (
            "execution_source_store_id",
            "execution_snapshot_id",
            "execution_audit_id",
            "execution_selection_id",
            "membership_verification_id",
        ):
            _require_sha256(getattr(self, name), name)
        execution_audited_at = _require_utc(
            self.execution_audited_at,
            "execution_audited_at",
        )
        object.__setattr__(self, "execution_audited_at", execution_audited_at)
        high_water = _require_int(
            self.execution_high_water_append_order,
            "execution_high_water_append_order",
        )
        if type(self.fragments) is not tuple or any(
            type(item) is not RuntimeExecutionFragment for item in self.fragments
        ):
            raise RuntimePathContractError(
                "fragments must be an exact tuple of RuntimeExecutionFragment"
            )
        _require_bool(self.execution_stream_complete, "execution_stream_complete")
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
        if len({item.source_fact_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError(
                "one execution source fact may produce only one fragment"
            )
        if len({item.source_callback_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError(
                "one execution callback may produce only one fragment"
            )
        if any(
            item.source_append_order > high_water
            or item.durable_known_at > execution_audited_at
            for item in ordered
        ):
            raise RuntimePathContractError(
                "execution fragment is outside the frozen execution audit"
            )
        if len({item.fragment_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError("execution fragments must be unique")
        object.__setattr__(self, "fragments", ordered)
        filled_quantity = sum(item.quantity for item in ordered)
        if filled_quantity > self.requested_quantity:
            raise RuntimePathContractError("filled quantity exceeds requested quantity")
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
    def structurally_projectable_to_stage4g_v3(self) -> bool:
        return self.completion is RuntimeFillCompletion.COMPLETE

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": RUNTIME_EXECUTION_SUMMARY_SCHEMA,
            "intent_id": self.intent_id,
            "side": self.side.value,
            "requested_quantity": self.requested_quantity,
            "fragments": [item.as_dict() for item in self.fragments],
            "execution_source_store_id": self.execution_source_store_id,
            "execution_snapshot_id": self.execution_snapshot_id,
            "execution_audit_id": self.execution_audit_id,
            "execution_audited_at": _utc_text(self.execution_audited_at),
            "execution_high_water_append_order": self.execution_high_water_append_order,
            "execution_selection_id": self.execution_selection_id,
            "membership_verification_id": self.membership_verification_id,
            "execution_stream_complete": self.execution_stream_complete,
            "native_multi_leg": False,
            "completion": self.completion.value,
            "filled_quantity": self.filled_quantity,
            "quantity_weighted_price": (
                None
                if self.quantity_weighted_price is None
                else _decimal_text(self.quantity_weighted_price)
            ),
            "total_explicit_cost": _decimal_text(self.total_explicit_cost),
            "structurally_projectable_to_stage4g_v3": (
                self.structurally_projectable_to_stage4g_v3
            ),
        }
        if include_id:
            document["summary_id"] = self.summary_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeExecutionEvidenceReference(StructuralFixture):
    fact_id: str
    fact_kind: str
    source_append_order: int
    known_at: datetime
    membership_verification_id: str
    reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.fact_id, "fact_id")
        _require_text(self.fact_kind, "fact_kind", maximum=128)
        _require_int(self.source_append_order, "source_append_order", minimum=1)
        known_at = _require_utc(self.known_at, "known_at")
        object.__setattr__(self, "known_at", known_at)
        _require_sha256(
            self.membership_verification_id,
            "membership_verification_id",
        )
        object.__setattr__(
            self,
            "reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-runtime-execution-evidence-reference-v1",
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind,
            "source_append_order": self.source_append_order,
            "known_at": _utc_text(self.known_at),
            "membership_verification_id": self.membership_verification_id,
        }
        if include_id:
            document["reference_id"] = self.reference_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeNoEntryEvidence(StructuralFixture):
    reason: RuntimeNoEntryReason
    execution_summary: RuntimeExecutionSummary
    entry_window_start: datetime
    entry_window_end: datetime
    decided_at: datetime
    execution_policy_id: str
    evidence_references: tuple[RuntimeExecutionEvidenceReference, ...]
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
        if type(self.evidence_references) is not tuple or any(
            type(item) is not RuntimeExecutionEvidenceReference
            for item in self.evidence_references
        ):
            raise RuntimePathContractError("evidence_references must be an exact tuple")
        references = tuple(
            sorted(
                self.evidence_references,
                key=lambda item: (item.source_append_order, item.fact_id),
            )
        )
        if len({item.fact_id for item in references}) != len(references):
            raise RuntimePathContractError(
                "execution evidence fact identities must be unique"
            )
        if (
            any(
                item.membership_verification_id
                != self.execution_summary.membership_verification_id
                or item.source_append_order
                > self.execution_summary.execution_high_water_append_order
                or item.known_at > decided_at
                for item in references
            )
            or self.execution_summary.execution_audited_at > decided_at
        ):
            raise RuntimePathContractError(
                "no-entry decision predates its execution evidence or audit"
            )
        object.__setattr__(self, "evidence_references", references)
        if type(self.evidence_ids) is not tuple:
            raise RuntimePathContractError("evidence_ids must be an exact tuple")
        evidence_ids = tuple(
            sorted(_require_sha256(item, "evidence_id") for item in self.evidence_ids)
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise RuntimePathContractError("evidence_ids must be unique")
        if evidence_ids != tuple(sorted(item.fact_id for item in references)):
            raise RuntimePathContractError(
                "evidence_ids must equal the verified evidence references"
            )
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
        references_by_fact_id = {item.fact_id: item for item in references}
        expected_fact_kinds = {
            fact_id: fact_kind
            for fact_id, fact_kind in (
                (
                    coverage_fact if coverage_complete else None,
                    "ENTRY_WINDOW_COVERAGE",
                ),
                (actor_authentication, "ACTOR_AUTHENTICATION"),
                (cancellation, "USER_CANCELLATION"),
                (rejection, "ORDER_REJECTION"),
                (invalidity, "DATA_INVALIDITY"),
            )
            if fact_id is not None
        }
        if any(
            references_by_fact_id[fact_id].fact_kind != fact_kind
            for fact_id, fact_kind in expected_fact_kinds.items()
        ):
            raise RuntimePathContractError(
                "reason-specific evidence fact kind is inconsistent"
            )
        object.__setattr__(
            self,
            "no_entry_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": RUNTIME_NO_ENTRY_SCHEMA,
            "reason": self.reason.value,
            "execution_summary": self.execution_summary.as_dict(),
            "entry_window_start": _utc_text(self.entry_window_start),
            "entry_window_end": _utc_text(self.entry_window_end),
            "decided_at": _utc_text(self.decided_at),
            "execution_policy_id": self.execution_policy_id,
            "evidence_references": [
                item.as_dict() for item in self.evidence_references
            ],
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
    "PATH_PROJECTION_POLICY_V1",
    "RUNTIME_EXECUTION_SUMMARY_SCHEMA",
    "RUNTIME_NO_ENTRY_SCHEMA",
    "RUNTIME_PATH_CONTRACT_SCHEMA",
    "RUNTIME_PATH_PREFIX_SCHEMA",
    "RUNTIME_PATH_RESOLUTION_SCHEMA",
    "TRADE_PROJECTION_POLICY_V1",
    "TRADING_SEGMENT_POLICY_V1",
    "CalendarSessionFact",
    "PathProjectionManifest",
    "RuntimeAuthorityFactReference",
    "RuntimeAuthorityKind",
    "RuntimeAuthoritySnapshotBinding",
    "RuntimeCalendarState",
    "RuntimeCoverageState",
    "RuntimeExecutionFragment",
    "RuntimeExecutionSide",
    "RuntimeExecutionSummary",
    "RuntimeFillCompletion",
    "RuntimeFrozenPathFact",
    "RuntimeFrozenPathPrefix",
    "RuntimeMarketSourceSnapshotBinding",
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
    "RuntimeProjectionLineage",
    "RuntimeSecurityStatus",
    "RuntimeSessionEvidence",
    "RuntimeSourceReference",
    "SecurityStatusFact",
    "SourceCoverageFact",
    "TradingSessionSegment",
    "TradingSessionSegmentKind",
    "resolve_runtime_path",
]
