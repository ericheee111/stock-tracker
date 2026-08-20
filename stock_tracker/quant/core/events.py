"""Point-in-time event intelligence contracts.

Events are evidence, not trade instructions.  The contracts separate source
publication metadata from first observation/knowledge, require explicit entity
bindings, resolve revision ancestry before date filtering, and bind any market
confirmation to immutable data snapshots.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from stock_tracker.core.types import Market

from .calendar import select_superseding_revision
from .fingerprint import fingerprint
from .point_in_time import PITConflictError, Revision, revision_key
from .time import ensure_aware, exchange_local_date, to_utc

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}
_ZERO = Decimal(0)
_ONE = Decimal(1)
_NEG_ONE = Decimal(-1)


class EventContractError(ValueError):
    """Raised when event evidence is not point-in-time safe."""


class EventAuthority(StrEnum):
    REGULATOR = "REGULATOR"
    EXCHANGE = "EXCHANGE"
    COMPANY_DISCLOSURE = "COMPANY_DISCLOSURE"
    INDEX_COMPILER = "INDEX_COMPILER"
    OFFICIAL_STATISTICS = "OFFICIAL_STATISTICS"
    SECONDARY_MEDIA = "SECONDARY_MEDIA"
    INTERNAL_RESEARCH = "INTERNAL_RESEARCH"


class EventType(StrEnum):
    POLICY = "POLICY"
    MACRO_DATA = "MACRO_DATA"
    COMPANY_ANNOUNCEMENT = "COMPANY_ANNOUNCEMENT"
    EARNINGS = "EARNINGS"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    INDUSTRY_DATA = "INDUSTRY_DATA"
    INDEX_CHANGE = "INDEX_CHANGE"
    TRADING_STATUS = "TRADING_STATUS"
    RISK_WARNING = "RISK_WARNING"
    TECHNOLOGY_OR_PRODUCT = "TECHNOLOGY_OR_PRODUCT"
    SUPPLY_DEMAND = "SUPPLY_DEMAND"
    OTHER = "OTHER"


class EventLifecycle(StrEnum):
    PROPOSED = "PROPOSED"
    ANNOUNCED = "ANNOUNCED"
    APPROVED = "APPROVED"
    EFFECTIVE = "EFFECTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CORRECTED = "CORRECTED"


class EventEntityKind(StrEnum):
    MARKET = "MARKET"
    INSTRUMENT = "INSTRUMENT"
    CLASSIFICATION = "CLASSIFICATION"


class PublicationGranularity(StrEnum):
    DATE = "DATE"
    SECOND = "SECOND"
    UNKNOWN = "UNKNOWN"


class EventDirection(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise EventContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise EventContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise EventContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, name)


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol")
    if symbol != symbol.upper():
        raise EventContractError("symbol must use uppercase canonical form")
    code, separator, suffix = symbol.rpartition(".")
    if not separator or not code or suffix not in _SYMBOL_SUFFIXES[market]:
        raise EventContractError("symbol suffix must match market")
    return symbol


def _require_visibility(known_at: datetime, usable_from: datetime) -> None:
    known = to_utc(known_at, "known_at")
    usable = to_utc(usable_from, "usable_from")
    if usable < known:
        raise EventContractError("usable_from cannot precede known_at")


def _require_decimal_range(
    value: object,
    name: str,
    lower: Decimal,
    upper: Decimal,
) -> Decimal:
    if type(value) is not Decimal:
        raise EventContractError(
            f"{name} must be Decimal; floats, integers and booleans are forbidden"
        )
    if not value.is_finite() or not lower <= value <= upper:
        raise EventContractError(
            f"{name} must be finite and within [{lower}, {upper}]"
        )
    return value


def _visible(
    known_at: datetime,
    usable_from: datetime,
    verified: bool,
    cutoff: datetime,
    require_verified: bool,
) -> bool:
    return (
        (verified or not require_verified)
        and to_utc(known_at) <= cutoff
        and to_utc(usable_from) <= cutoff
    )


@dataclass(frozen=True, slots=True, order=True)
class EventSourceStream:
    owner: str
    family: str
    version: str
    authority: EventAuthority

    def __post_init__(self) -> None:
        _require_text(self.owner, "source owner")
        _require_text(self.family, "source family")
        _require_text(self.version, "source version")
        if not isinstance(self.authority, EventAuthority):
            raise EventContractError("authority must be EventAuthority")

    @property
    def stream_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class EventEntityBinding:
    kind: EventEntityKind
    market: Market
    entity_id: str
    identity_fact_id: str | None = None
    symbol: str | None = None
    taxonomy_id: str | None = None
    classification_id: str | None = None
    classification_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventEntityKind):
            raise EventContractError("kind must be EventEntityKind")
        if not isinstance(self.market, Market):
            raise EventContractError("market must be Market")
        _require_text(self.entity_id, "entity_id")
        if self.kind is EventEntityKind.MARKET:
            if self.entity_id != self.market.value:
                raise EventContractError(
                    "market binding entity_id must equal market value"
                )
            if any(
                value is not None
                for value in (
                    self.identity_fact_id,
                    self.symbol,
                    self.taxonomy_id,
                    self.classification_id,
                    self.classification_snapshot_id,
                )
            ):
                raise EventContractError(
                    "market binding cannot carry instrument/classification fields"
                )
        elif self.kind is EventEntityKind.INSTRUMENT:
            _require_sha256(self.identity_fact_id, "identity_fact_id")
            _require_symbol(self.symbol, self.market)
            if any(
                value is not None
                for value in (
                    self.taxonomy_id,
                    self.classification_id,
                    self.classification_snapshot_id,
                )
            ):
                raise EventContractError(
                    "instrument binding cannot carry classification fields"
                )
        else:
            _require_text(self.taxonomy_id, "taxonomy_id")
            _require_text(self.classification_id, "classification_id")
            _require_sha256(
                self.classification_snapshot_id,
                "classification_snapshot_id",
            )
            expected = f"{self.taxonomy_id}:{self.classification_id}"
            if self.entity_id != expected:
                raise EventContractError(
                    "classification binding entity_id must be taxonomy:classification"
                )
            if self.identity_fact_id is not None or self.symbol is not None:
                raise EventContractError(
                    "classification binding cannot carry instrument fields"
                )

    @property
    def binding_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class EventCoverage:
    stream: EventSourceStream
    market: Market
    start_date: date
    end_date: date
    known_at: datetime
    usable_from: datetime
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    complete: bool
    source_note: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream, EventSourceStream):
            raise EventContractError("stream must be EventSourceStream")
        if not isinstance(self.market, Market):
            raise EventContractError("market must be Market")
        if self.end_date < self.start_date:
            raise EventContractError("coverage end_date cannot precede start_date")
        _require_visibility(self.known_at, self.usable_from)
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise EventContractError("coverage revision cannot supersede itself")
        _require_bool(self.verified, "verified")
        _require_bool(self.complete, "complete")
        if (self.verified or self.complete) and not self.source_note:
            raise EventContractError(
                "verified/complete coverage requires source_note"
            )

    @property
    def coverage_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class EventFact:
    event_id: str
    stream: EventSourceStream
    market: Market
    event_type: EventType
    lifecycle: EventLifecycle
    event_date: date
    effective_from: date | None
    effective_to: date | None
    title: str
    summary: str
    source_published_at: date | datetime | None
    publication_granularity: PublicationGranularity
    observed_at: datetime
    retrieved_at: datetime
    known_at: datetime
    usable_from: datetime
    entity_bindings: tuple[EventEntityBinding, ...]
    materiality: Decimal
    novelty: Decimal
    surprise: Decimal
    direction: EventDirection
    source_uri: str
    raw_artifact_id: str
    parse_descriptor_id: str
    parser_version: str
    evidence_ids: tuple[str, ...]
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        if not isinstance(self.stream, EventSourceStream):
            raise EventContractError("stream must be EventSourceStream")
        if not isinstance(self.market, Market):
            raise EventContractError("market must be Market")
        if not isinstance(self.event_type, EventType):
            raise EventContractError("event_type must be EventType")
        if not isinstance(self.lifecycle, EventLifecycle):
            raise EventContractError("lifecycle must be EventLifecycle")
        if self.effective_to is not None:
            if self.effective_from is None:
                raise EventContractError(
                    "effective_to requires effective_from"
                )
            if self.effective_to < self.effective_from:
                raise EventContractError(
                    "effective_to cannot precede effective_from"
                )
        _require_text(self.title, "title")
        _require_text(self.summary, "summary")
        if not isinstance(self.publication_granularity, PublicationGranularity):
            raise EventContractError(
                "publication_granularity must be PublicationGranularity"
            )
        for name in ("observed_at", "retrieved_at", "known_at", "usable_from"):
            ensure_aware(getattr(self, name), name)
        if to_utc(self.observed_at) > to_utc(self.retrieved_at):
            raise EventContractError("observed_at cannot follow retrieved_at")
        if to_utc(self.known_at) < to_utc(self.observed_at):
            raise EventContractError("known_at cannot precede observed_at")
        _require_visibility(self.known_at, self.usable_from)
        if self.publication_granularity is PublicationGranularity.DATE:
            if type(self.source_published_at) is not date:
                raise EventContractError(
                    "DATE publication requires date without fabricated time"
                )
            if self.source_published_at > exchange_local_date(
                self.known_at,
                self.market,
            ):
                raise EventContractError(
                    "source publication date cannot follow known_at"
                )
        elif self.publication_granularity is PublicationGranularity.SECOND:
            if not isinstance(self.source_published_at, datetime):
                raise EventContractError(
                    "SECOND publication requires timezone-aware datetime"
                )
            ensure_aware(self.source_published_at, "source_published_at")
            if to_utc(self.source_published_at) > to_utc(self.known_at):
                raise EventContractError(
                    "source publication timestamp cannot follow known_at"
                )
        elif self.source_published_at is not None:
            raise EventContractError(
                "UNKNOWN publication granularity requires null publication"
            )
        if not self.entity_bindings:
            raise EventContractError("event requires at least one entity binding")
        if any(
            not isinstance(item, EventEntityBinding)
            for item in self.entity_bindings
        ):
            raise EventContractError(
                "entity_bindings must contain EventEntityBinding values"
            )
        binding_ids = tuple(item.binding_id for item in self.entity_bindings)
        if binding_ids != tuple(sorted(set(binding_ids))):
            raise EventContractError(
                "entity bindings must be sorted and unique by binding_id"
            )
        if any(item.market is not self.market for item in self.entity_bindings):
            raise EventContractError("event entity binding market mismatch")
        _require_decimal_range(self.materiality, "materiality", _ZERO, _ONE)
        _require_decimal_range(self.novelty, "novelty", _ZERO, _ONE)
        _require_decimal_range(self.surprise, "surprise", _NEG_ONE, _ONE)
        if not isinstance(self.direction, EventDirection):
            raise EventContractError("direction must be EventDirection")
        _require_text(self.source_uri, "source_uri")
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        _require_sha256(self.parse_descriptor_id, "parse_descriptor_id")
        _require_text(self.parser_version, "parser_version")
        for evidence_id in self.evidence_ids:
            _require_sha256(evidence_id, "evidence_id")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise EventContractError("evidence_ids must be sorted and unique")
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise EventContractError("event revision cannot supersede itself")
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise EventContractError("verified event requires source_note")

    @property
    def fact_id(self) -> str:
        return fingerprint(self)

    @property
    def stream_identity(self) -> tuple[str, str]:
        return (self.stream.stream_id, self.event_id)

    @property
    def is_cancelled(self) -> bool:
        return self.lifecycle is EventLifecycle.CANCELLED

    def revision_payload(self) -> dict[str, object]:
        return {
            "stream_id": self.stream.stream_id,
            "market": self.market,
            "event_type": self.event_type,
            "lifecycle": self.lifecycle,
            "event_date": self.event_date,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "title": self.title,
            "summary": self.summary,
            "source_published_at": self.source_published_at,
            "publication_granularity": self.publication_granularity,
            "usable_from": self.usable_from,
            "entity_binding_ids": [
                item.binding_id for item in self.entity_bindings
            ],
            "materiality": self.materiality,
            "novelty": self.novelty,
            "surprise": self.surprise,
            "direction": self.direction,
            "source_uri": self.source_uri,
            "raw_artifact_id": self.raw_artifact_id,
            "parse_descriptor_id": self.parse_descriptor_id,
            "parser_version": self.parser_version,
            "evidence_ids": self.evidence_ids,
            "verified": self.verified,
            "source_note": self.source_note,
        }


@dataclass(frozen=True, slots=True)
class EventMarketConfirmation:
    event_fact_id: str
    evaluated_as_of: datetime
    raw_bar_snapshot_id: str
    feature_snapshot_id: str
    price_response: Decimal
    volume_response: Decimal
    breadth_response: Decimal
    confirmed: bool
    direction: EventDirection
    policy_version: str

    def __post_init__(self) -> None:
        _require_sha256(self.event_fact_id, "event_fact_id")
        ensure_aware(self.evaluated_as_of, "evaluated_as_of")
        _require_sha256(self.raw_bar_snapshot_id, "raw_bar_snapshot_id")
        _require_sha256(self.feature_snapshot_id, "feature_snapshot_id")
        _require_decimal_range(
            self.price_response,
            "price_response",
            _NEG_ONE,
            _ONE,
        )
        _require_decimal_range(
            self.volume_response,
            "volume_response",
            _NEG_ONE,
            _ONE,
        )
        _require_decimal_range(
            self.breadth_response,
            "breadth_response",
            _NEG_ONE,
            _ONE,
        )
        _require_bool(self.confirmed, "confirmed")
        if not isinstance(self.direction, EventDirection):
            raise EventContractError("direction must be EventDirection")
        _require_text(self.policy_version, "policy_version")

    @property
    def confirmation_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class EventEvidenceSnapshot:
    market: Market
    start_date: date
    end_date: date
    as_of: datetime
    required_streams: tuple[EventSourceStream, ...]
    coverages: tuple[EventCoverage, ...]
    events: tuple[EventFact, ...]
    confirmations: tuple[EventMarketConfirmation, ...]
    require_verified: bool = True
    require_complete: bool = True
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        cutoff = to_utc(self.as_of, "as_of")
        if not isinstance(self.market, Market):
            raise EventContractError("market must be Market")
        if self.end_date < self.start_date:
            raise EventContractError("end_date cannot precede start_date")
        if self.end_date > exchange_local_date(self.as_of, self.market):
            raise EventContractError(
                "event snapshot cannot extend beyond as_of exchange date"
            )
        _require_bool(self.require_verified, "require_verified")
        _require_bool(self.require_complete, "require_complete")
        if any(
            not isinstance(item, EventSourceStream)
            for item in self.required_streams
        ):
            raise EventContractError(
                "required_streams must contain EventSourceStream values"
            )
        if any(not isinstance(item, EventCoverage) for item in self.coverages):
            raise EventContractError(
                "coverages must contain EventCoverage values"
            )
        if any(not isinstance(item, EventFact) for item in self.events):
            raise EventContractError("events must contain EventFact values")
        if any(
            not isinstance(item, EventMarketConfirmation)
            for item in self.confirmations
        ):
            raise EventContractError(
                "confirmations must contain EventMarketConfirmation values"
            )
        stream_ids = tuple(item.stream_id for item in self.required_streams)
        if not stream_ids:
            raise EventContractError("required_streams cannot be empty")
        if stream_ids != tuple(sorted(set(stream_ids))):
            raise EventContractError(
                "required_streams must be sorted and unique"
            )
        coverage_order = tuple(
            (item.stream.stream_id, item.coverage_id) for item in self.coverages
        )
        if coverage_order != tuple(sorted(coverage_order)):
            raise EventContractError(
                "coverages must be deterministically sorted"
            )
        if len({item.stream.stream_id for item in self.coverages}) != len(
            self.coverages
        ):
            raise EventContractError(
                "snapshot requires one terminal coverage per source stream"
            )
        if {item.stream.stream_id for item in self.coverages} != set(stream_ids):
            raise EventContractError(
                "snapshot coverage streams differ from required_streams"
            )
        for coverage in self.coverages:
            if coverage.market is not self.market:
                raise EventContractError("coverage market mismatch")
            if not (
                coverage.start_date <= self.start_date
                and coverage.end_date >= self.end_date
            ):
                raise EventContractError(
                    "coverage does not contain snapshot range"
                )
            if to_utc(coverage.known_at) > cutoff or to_utc(coverage.usable_from) > cutoff:
                raise EventContractError("future coverage entered snapshot")
            if self.require_verified and not coverage.verified:
                raise EventContractError(
                    "verified snapshot requires verified coverage"
                )
            if self.require_complete and not coverage.complete:
                raise EventContractError(
                    "complete snapshot requires complete coverage"
                )
        event_order = tuple(
            (
                item.event_date,
                item.stream.stream_id,
                item.event_id,
                item.fact_id,
            )
            for item in self.events
        )
        if event_order != tuple(sorted(event_order)):
            raise EventContractError("events must be deterministically sorted")
        event_stream_ids = set(stream_ids)
        event_fact_ids: set[str] = set()
        event_by_fact: dict[str, EventFact] = {}
        for event in self.events:
            if event.market is not self.market:
                raise EventContractError("event market mismatch")
            if event.stream.stream_id not in event_stream_ids:
                raise EventContractError(
                    "event source stream is not required by snapshot"
                )
            if not self.start_date <= event.event_date <= self.end_date:
                raise EventContractError("event_date is outside snapshot range")
            if to_utc(event.known_at) > cutoff or to_utc(event.usable_from) > cutoff:
                raise EventContractError("future event entered snapshot")
            if self.require_verified and not event.verified:
                raise EventContractError(
                    "unverified event entered verified snapshot"
                )
            if event.fact_id in event_fact_ids:
                raise EventContractError("event facts must be unique")
            event_fact_ids.add(event.fact_id)
            event_by_fact[event.fact_id] = event
        confirmation_order = tuple(
            (item.event_fact_id, item.evaluated_as_of, item.confirmation_id)
            for item in self.confirmations
        )
        if confirmation_order != tuple(sorted(confirmation_order)):
            raise EventContractError(
                "confirmations must be deterministically sorted"
            )
        if len({item.event_fact_id for item in self.confirmations}) != len(
            self.confirmations
        ):
            raise EventContractError(
                "snapshot allows at most one confirmation per event fact"
            )
        for confirmation in self.confirmations:
            event = event_by_fact.get(confirmation.event_fact_id)
            if event is None:
                raise EventContractError(
                    "confirmation references event outside snapshot"
                )
            evaluated_at = to_utc(confirmation.evaluated_as_of)
            if evaluated_at > cutoff:
                raise EventContractError(
                    "future market confirmation entered snapshot"
                )
            if evaluated_at < to_utc(event.usable_from):
                raise EventContractError(
                    "market confirmation cannot precede event usable_from"
                )
            if event.is_cancelled and confirmation.confirmed:
                raise EventContractError(
                    "cancelled event cannot retain positive confirmation"
                )
        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "event-evidence-snapshot-v1",
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": cutoff,
                    "required_stream_ids": list(stream_ids),
                    "coverage_ids": [item.coverage_id for item in self.coverages],
                    "event_fact_ids": [item.fact_id for item in self.events],
                    "confirmation_ids": [
                        item.confirmation_id for item in self.confirmations
                    ],
                    "require_verified": self.require_verified,
                    "require_complete": self.require_complete,
                }
            ),
        )

    @property
    def active_events(self) -> tuple[EventFact, ...]:
        return tuple(item for item in self.events if not item.is_cancelled)

    @property
    def confirmed_events(self) -> tuple[EventFact, ...]:
        confirmed_ids = {
            item.event_fact_id for item in self.confirmations if item.confirmed
        }
        return tuple(
            item
            for item in self.active_events
            if item.fact_id in confirmed_ids
        )

    def events_for_entity(self, binding_id: str) -> tuple[EventFact, ...]:
        _require_sha256(binding_id, "binding_id")
        return tuple(
            item
            for item in self.active_events
            if any(binding.binding_id == binding_id for binding in item.entity_bindings)
        )


class EventBook:
    """Append-only PIT event coverage, revisions and market confirmations."""

    def __init__(
        self,
        coverages: Iterable[EventCoverage],
        events: Iterable[EventFact],
        confirmations: Iterable[EventMarketConfirmation] = (),
    ) -> None:
        self._coverages = tuple(coverages)
        self._events = tuple(events)
        self._confirmations = tuple(confirmations)

    def snapshot(
        self,
        market: Market,
        start_date: date,
        end_date: date,
        as_of: datetime,
        *,
        required_streams: Iterable[EventSourceStream],
        require_verified: bool = True,
        require_complete: bool = True,
    ) -> EventEvidenceSnapshot:
        if not isinstance(market, Market):
            raise EventContractError("market must be Market")
        if end_date < start_date:
            raise EventContractError("end_date cannot precede start_date")
        _require_bool(require_verified, "require_verified")
        _require_bool(require_complete, "require_complete")
        cutoff = to_utc(as_of, "as_of")
        stream_values = tuple(required_streams)
        if not stream_values:
            raise EventContractError("required_streams cannot be empty")
        if any(
            not isinstance(item, EventSourceStream)
            for item in stream_values
        ):
            raise EventContractError(
                "required_streams must contain EventSourceStream values"
            )
        streams = tuple(
            sorted(
                set(stream_values),
                key=lambda item: item.stream_id,
            )
        )
        required_ids = {item.stream_id for item in streams}

        coverage_groups: dict[str, list[EventCoverage]] = defaultdict(list)
        for coverage in self._coverages:
            if (
                coverage.market is market
                and coverage.stream.stream_id in required_ids
                and _visible(
                    coverage.known_at,
                    coverage.usable_from,
                    coverage.verified,
                    cutoff,
                    require_verified,
                )
            ):
                coverage_groups[coverage.stream.stream_id].append(coverage)
        selected_coverages: list[EventCoverage] = []
        for stream in streams:
            rows = coverage_groups.get(stream.stream_id, [])
            if not rows:
                raise EventContractError(
                    f"missing visible coverage for event stream {stream.stream_id}"
                )
            try:
                terminal = select_superseding_revision(
                    rows,
                    revision_of=lambda item: item.revision,
                    predecessor_of=lambda item: item.supersedes_revision,
                    payload_of=lambda item: {
                        "stream_id": item.stream.stream_id,
                        "market": item.market,
                        "start_date": item.start_date,
                        "end_date": item.end_date,
                        "usable_from": item.usable_from,
                        "verified": item.verified,
                        "complete": item.complete,
                        "source_note": item.source_note,
                    },
                    identity_of=lambda item: item.coverage_id,
                    known_at_of=lambda item: item.known_at,
                )
            except PITConflictError as exc:
                raise EventContractError(str(exc)) from exc
            if not (
                terminal.start_date <= start_date
                and terminal.end_date >= end_date
            ):
                raise EventContractError(
                    "terminal event coverage no longer contains snapshot range"
                )
            if require_complete and not terminal.complete:
                raise EventContractError(
                    "complete snapshot requires complete event coverage"
                )
            selected_coverages.append(terminal)

        event_groups: dict[
            tuple[str, str], list[EventFact]
        ] = defaultdict(list)
        for event in self._events:
            if (
                event.market is market
                and event.stream.stream_id in required_ids
                and _visible(
                    event.known_at,
                    event.usable_from,
                    event.verified,
                    cutoff,
                    require_verified,
                )
            ):
                event_groups[event.stream_identity].append(event)
        selected_events: list[EventFact] = []
        for rows in event_groups.values():
            try:
                terminal = select_superseding_revision(
                    rows,
                    revision_of=lambda item: item.revision,
                    predecessor_of=lambda item: item.supersedes_revision,
                    payload_of=lambda item: item.revision_payload(),
                    identity_of=lambda item: item.fact_id,
                    known_at_of=lambda item: item.known_at,
                )
            except PITConflictError as exc:
                raise EventContractError(str(exc)) from exc
            if start_date <= terminal.event_date <= end_date:
                selected_events.append(terminal)
        event_tuple = tuple(
            sorted(
                selected_events,
                key=lambda item: (
                    item.event_date,
                    item.stream.stream_id,
                    item.event_id,
                    item.fact_id,
                ),
            )
        )
        event_fact_ids = {item.fact_id for item in event_tuple}

        confirmations_by_event: dict[
            str, list[EventMarketConfirmation]
        ] = defaultdict(list)
        for confirmation in self._confirmations:
            if (
                confirmation.event_fact_id in event_fact_ids
                and to_utc(confirmation.evaluated_as_of) <= cutoff
            ):
                confirmations_by_event[confirmation.event_fact_id].append(
                    confirmation
                )
        selected_confirmations: list[EventMarketConfirmation] = []
        for rows in confirmations_by_event.values():
            newest_time = max(to_utc(item.evaluated_as_of) for item in rows)
            newest = [
                item
                for item in rows
                if to_utc(item.evaluated_as_of) == newest_time
            ]
            identities = {item.confirmation_id for item in newest}
            if len(identities) != 1:
                raise EventContractError(
                    "market confirmations share evaluated_as_of but disagree"
                )
            selected_confirmations.append(
                min(newest, key=lambda item: item.confirmation_id)
            )

        return EventEvidenceSnapshot(
            market=market,
            start_date=start_date,
            end_date=end_date,
            as_of=cutoff,
            required_streams=streams,
            coverages=tuple(
                sorted(
                    selected_coverages,
                    key=lambda item: (item.stream.stream_id, item.coverage_id),
                )
            ),
            events=event_tuple,
            confirmations=tuple(
                sorted(
                    selected_confirmations,
                    key=lambda item: (
                        item.event_fact_id,
                        item.evaluated_as_of,
                        item.confirmation_id,
                    ),
                )
            ),
            require_verified=require_verified,
            require_complete=require_complete,
        )


__all__ = [
    "EventAuthority",
    "EventBook",
    "EventContractError",
    "EventCoverage",
    "EventDirection",
    "EventEntityBinding",
    "EventEntityKind",
    "EventEvidenceSnapshot",
    "EventFact",
    "EventLifecycle",
    "EventMarketConfirmation",
    "EventSourceStream",
    "EventType",
    "PublicationGranularity",
]
