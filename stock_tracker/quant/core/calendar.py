"""Point-in-time exchange calendars and calendar-aligned bar sequences."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TypeVar

from stock_tracker.core.types import Bar, DataStatus, Market

from .fingerprint import fingerprint
from .point_in_time import PITConflictError, Revision, revision_key
from .time import (
    TimeContractError,
    exchange_local_date,
    require_exchange_timezone,
    to_utc,
)


class CalendarContractError(ValueError):
    """Raised when calendar coverage or an aligned sequence is unsafe."""


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CalendarContractError(f"{name} must be a boolean")
    return value


def _normalize_visibility(record: object) -> None:
    known_at = getattr(record, "known_at")
    usable_from = getattr(record, "usable_from")
    effective_usable_from = known_at if usable_from is None else usable_from
    known = to_utc(known_at, "known_at")
    usable = to_utc(effective_usable_from, "usable_from")
    if usable < known:
        raise CalendarContractError("usable_from cannot precede known_at")
    object.__setattr__(record, "usable_from", effective_usable_from)


def _visible_at(record: object, cutoff: datetime) -> bool:
    usable_from = getattr(record, "usable_from") or getattr(record, "known_at")
    return (
        to_utc(getattr(record, "known_at")) <= cutoff
        and to_utc(usable_from) <= cutoff
    )


class CalendarStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class SessionKind(StrEnum):
    REGULAR = "REGULAR"
    HALF_DAY = "HALF_DAY"
    SPECIAL = "SPECIAL"


class InstrumentSessionState(StrEnum):
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    VCM_HALT = "VCM_HALT"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


def _dates(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise CalendarContractError("coverage end cannot precede start")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


@dataclass(frozen=True, slots=True)
class CalendarDay:
    market: Market
    session_date: date
    status: CalendarStatus
    open_time: datetime | None
    close_time: datetime | None
    session_kind: SessionKind
    known_at: datetime
    source: str
    revision: Revision
    calendar_version: str
    verified: bool
    source_note: str
    supersedes_revision: Revision | None = None
    usable_from: datetime | None = None

    def __post_init__(self) -> None:
        _require_bool(self.verified, "verified")
        if not self.source or not self.calendar_version:
            raise CalendarContractError("calendar source and version must be non-empty")
        if self.verified and not self.source_note:
            raise CalendarContractError("verified calendar days require a source note")
        _normalize_visibility(self)
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise CalendarContractError("calendar revision cannot supersede itself")
        if self.status is CalendarStatus.OPEN:
            if self.open_time is None or self.close_time is None:
                raise CalendarContractError("OPEN day requires open_time and close_time")
            try:
                require_exchange_timezone(self.open_time, self.market, "open_time")
                require_exchange_timezone(self.close_time, self.market, "close_time")
                open_date = exchange_local_date(self.open_time, self.market)
                close_date = exchange_local_date(self.close_time, self.market)
            except TimeContractError as exc:
                raise CalendarContractError(str(exc)) from exc
            if open_date != self.session_date:
                raise CalendarContractError("open_time does not match session_date")
            if close_date != self.session_date:
                raise CalendarContractError("close_time does not match session_date")
            if self.close_time <= self.open_time:
                raise CalendarContractError("close_time must follow open_time")
        elif self.open_time is not None or self.close_time is not None:
            raise CalendarContractError("CLOSED day cannot carry trading times")

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class CalendarCoverage:
    market: Market
    start_date: date
    end_date: date
    source: str
    calendar_version: str
    known_at: datetime
    revision: Revision
    verified: bool
    source_note: str
    usable_from: datetime | None = None

    def __post_init__(self) -> None:
        _require_bool(self.verified, "verified")
        _dates(self.start_date, self.end_date)
        if not self.source or not self.calendar_version:
            raise CalendarContractError("coverage source and version must be non-empty")
        if self.verified and not self.source_note:
            raise CalendarContractError("verified coverage requires a source note")
        _normalize_visibility(self)
        revision_key(self.revision)

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class InstrumentSessionStatus:
    symbol: str
    market: Market
    session_date: date
    status: InstrumentSessionState
    known_at: datetime
    source: str
    revision: Revision
    reference_price: float | None
    share_factor: float
    verified: bool
    source_note: str
    usable_from: datetime | None = None

    def __post_init__(self) -> None:
        _require_bool(self.verified, "verified")
        if not self.symbol or not self.source:
            raise CalendarContractError("instrument status requires symbol and source")
        if self.status is InstrumentSessionState.OPEN:
            raise CalendarContractError("no-bar status cannot be OPEN")
        if self.reference_price is not None and self.reference_price <= 0:
            raise CalendarContractError("reference_price must be positive")
        if self.share_factor <= 0:
            raise CalendarContractError("share_factor must be explicitly positive")
        if self.verified and not self.source_note:
            raise CalendarContractError("verified instrument status requires source note")
        _normalize_visibility(self)
        revision_key(self.revision)

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


TRevisionFact = TypeVar(
    "TRevisionFact",
    CalendarDay,
    CalendarCoverage,
    InstrumentSessionStatus,
)
TGraphRevision = TypeVar("TGraphRevision")


def select_superseding_revision(
    records: Iterable[TGraphRevision],
    *,
    revision_of: Callable[[TGraphRevision], Hashable],
    predecessor_of: Callable[[TGraphRevision], Hashable | None],
    payload_of: Callable[[TGraphRevision], object],
    identity_of: Callable[[TGraphRevision], str],
    known_at_of: Callable[[TGraphRevision], datetime],
) -> TGraphRevision:
    """Select a terminal revision using only an explicit supersedes graph.

    Every visible node is validated before selection. Cycles and missing
    predecessors therefore fail closed even when their payload is identical to
    the selected terminal. Revision string/numeric ordering is never used as a
    substitute for ancestry.
    """

    candidates = tuple(records)
    if not candidates:
        raise LookupError("no visible revision")
    grouped: dict[Hashable, list[TGraphRevision]] = defaultdict(list)
    for record in candidates:
        grouped[revision_of(record)].append(record)

    representatives: dict[Hashable, TGraphRevision] = {}
    for revision, values in grouped.items():
        semantics = {
            (
                predecessor_of(value),
                fingerprint(payload_of(value)),
                to_utc(known_at_of(value)),
            )
            for value in values
        }
        if len(semantics) != 1:
            raise PITConflictError(
                f"revision {revision!r} maps to conflicting calendar semantics"
            )
        representatives[revision] = min(values, key=identity_of)

    for revision, record in representatives.items():
        predecessor = predecessor_of(record)
        if predecessor is not None and predecessor not in representatives:
            raise PITConflictError(
                f"calendar revision {revision!r} references missing predecessor {predecessor!r}"
            )

    visiting: set[Hashable] = set()
    visited: set[Hashable] = set()

    def visit(revision: Hashable) -> None:
        if revision in visiting:
            raise PITConflictError("calendar revision graph contains a cycle")
        if revision in visited:
            return
        visiting.add(revision)
        predecessor = predecessor_of(representatives[revision])
        if predecessor is not None:
            visit(predecessor)
        visiting.remove(revision)
        visited.add(revision)

    for revision in representatives:
        visit(revision)

    superseded = {
        predecessor_of(record)
        for record in representatives.values()
        if predecessor_of(record) is not None
    }
    terminals = [
        record
        for revision, record in representatives.items()
        if revision not in superseded
    ]
    if not terminals:
        raise PITConflictError("calendar revision graph has no terminal revision")
    if len(terminals) != 1:
        raise PITConflictError(
            "calendar revision graph has multiple terminal revisions without one explicit ancestry"
        )
    return terminals[0]


def _select_revision(records: Iterable[TRevisionFact]) -> TRevisionFact:
    candidates = tuple(records)
    if not candidates:
        raise LookupError("no visible revision")
    if all(isinstance(record, CalendarDay) for record in candidates) and any(
        isinstance(record, CalendarDay) and record.supersedes_revision is not None
        for record in candidates
    ):
        return select_superseding_revision(
            candidates,
            revision_of=lambda record: record.revision,
            predecessor_of=lambda record: record.supersedes_revision,
            payload_of=lambda record: {
                "status": record.status,
                "open_time": record.open_time,
                "close_time": record.close_time,
                "session_kind": record.session_kind,
            },
            identity_of=lambda record: record.fact_id,
            known_at_of=lambda record: record.known_at,
        )
    newest_known_at = max(to_utc(record.known_at) for record in candidates)
    newest = [
        record for record in candidates if to_utc(record.known_at) == newest_known_at
    ]
    highest = max(revision_key(record.revision) for record in newest)
    finalists = [record for record in newest if revision_key(record.revision) == highest]
    identities = {fingerprint(record) for record in finalists}
    if len(identities) != 1:
        raise PITConflictError(
            "calendar/status revisions share known_at and revision but disagree"
        )
    return min(finalists, key=fingerprint)


@dataclass(frozen=True, slots=True)
class CalendarSnapshot:
    market: Market
    as_of: datetime
    coverage: CalendarCoverage
    days: tuple[CalendarDay, ...]
    snapshot_id: str
    require_verified: bool = True

    def __post_init__(self) -> None:
        cutoff = to_utc(self.as_of, "as_of")
        _require_bool(self.require_verified, "require_verified")
        if self.require_verified and not self.coverage.verified:
            raise CalendarContractError(
                "verified snapshot requires verified calendar coverage"
            )
        if not _visible_at(self.coverage, cutoff):
            raise CalendarContractError("future or unusable calendar coverage entered snapshot")
        expected = _dates(self.coverage.start_date, self.coverage.end_date)
        actual = tuple(day.session_date for day in self.days)
        if actual != expected:
            raise CalendarContractError("calendar coverage must include every civil date")
        for day in self.days:
            if self.require_verified and not day.verified:
                raise CalendarContractError(
                    "unverified day entered verified calendar snapshot"
                )
            if day.market is not self.market:
                raise CalendarContractError("calendar day market mismatch")
            if day.source != self.coverage.source:
                raise CalendarContractError("calendar day source differs from coverage")
            if day.calendar_version != self.coverage.calendar_version:
                raise CalendarContractError("calendar day version differs from coverage")
            if not _visible_at(day, cutoff):
                raise CalendarContractError(
                    "future or unusable calendar revision entered snapshot"
                )
        expected_snapshot_id = fingerprint(
            {
                "schema": "calendar-snapshot-v1",
                "as_of": cutoff,
                "coverage": self.coverage.fact_id,
                "days": [day.fact_id for day in self.days],
                "require_verified": self.require_verified,
            }
        )
        if self.snapshot_id != expected_snapshot_id:
            raise CalendarContractError(
                "calendar snapshot_id does not match snapshot content"
            )

    @property
    def open_days(self) -> tuple[CalendarDay, ...]:
        return tuple(day for day in self.days if day.status is CalendarStatus.OPEN)

    @property
    def open_dates(self) -> tuple[date, ...]:
        return tuple(day.session_date for day in self.open_days)

    def _require_date(self, value: date) -> None:
        if not self.coverage.start_date <= value <= self.coverage.end_date:
            raise CalendarContractError("date is outside one calendar coverage/version")

    def advance(self, value: date, sessions: int) -> date:
        """Advance by exchange sessions, counting the starting session as index zero."""

        if sessions < 0:
            raise CalendarContractError("sessions cannot be negative")
        self._require_date(value)
        try:
            index = self.open_dates.index(value)
        except ValueError as exc:
            raise CalendarContractError("advance requires an OPEN session date") from exc
        target = index + sessions
        if target >= len(self.open_dates):
            raise CalendarContractError("advance would cross calendar coverage")
        return self.open_dates[target]

    def session_distance(self, start: date, end: date) -> int:
        self._require_date(start)
        self._require_date(end)
        try:
            return self.open_dates.index(end) - self.open_dates.index(start)
        except ValueError as exc:
            raise CalendarContractError("session_distance requires OPEN dates") from exc


@dataclass(frozen=True, slots=True)
class CalendarAlignedBars:
    symbol: str
    market: Market
    bars: tuple[Bar, ...]
    session_dates: tuple[date, ...]
    session_states: tuple[InstrumentSessionState, ...]
    placeholder_dates: tuple[date, ...]
    calendar_as_of: datetime
    calendar_snapshot_id: str
    calendar_version: str
    instrument_status_snapshot_id: str
    coverage_start: date
    coverage_end: date

    def __post_init__(self) -> None:
        lengths = {len(self.bars), len(self.session_dates), len(self.session_states)}
        if len(lengths) != 1:
            raise CalendarContractError("aligned bars, dates and states must have equal length")
        if not self.bars:
            raise CalendarContractError("aligned bars cannot be empty")
        if len(self.calendar_snapshot_id) != 64:
            raise CalendarContractError("calendar_snapshot_id must be SHA-256")
        if len(self.instrument_status_snapshot_id) != 64:
            raise CalendarContractError("status_snapshot_id must be SHA-256")
        if tuple(sorted(self.session_dates)) != self.session_dates:
            raise CalendarContractError("session_dates must be chronological")
        if len(set(self.session_dates)) != len(self.session_dates):
            raise CalendarContractError("session_dates must be unique")
        placeholders = set(self.placeholder_dates)
        if not placeholders.issubset(set(self.session_dates)):
            raise CalendarContractError("placeholder_dates must belong to session_dates")
        for bar, session_date, state in zip(
            self.bars,
            self.session_dates,
            self.session_states,
        ):
            if bar.symbol != self.symbol or bar.market is not self.market:
                raise CalendarContractError("bar identity differs from aligned sequence")
            if exchange_local_date(bar.timestamp, self.market) != session_date:
                raise CalendarContractError("bar timestamp differs from declared session date")
            if session_date in placeholders:
                if state is InstrumentSessionState.OPEN or bar.volume != 0:
                    raise CalendarContractError("placeholder must be non-OPEN with zero volume")
            elif state is not InstrumentSessionState.OPEN:
                raise CalendarContractError("real bar must have OPEN session state")

    def is_observable(self, index: int) -> bool:
        return self.session_states[index] is InstrumentSessionState.OPEN


class TradingCalendar:
    """Append-only calendar/status records with PIT snapshot selection."""

    def __init__(
        self,
        coverages: Iterable[CalendarCoverage],
        days: Iterable[CalendarDay],
        statuses: Iterable[InstrumentSessionStatus] = (),
    ) -> None:
        self._coverages = tuple(coverages)
        self._days = tuple(days)
        self._statuses = tuple(statuses)

    def snapshot(
        self,
        market: Market,
        start: date,
        end: date,
        as_of: datetime,
        *,
        require_verified: bool = True,
    ) -> CalendarSnapshot:
        _require_bool(require_verified, "require_verified")
        cutoff = to_utc(as_of, "as_of")
        matching = [
            coverage
            for coverage in self._coverages
            if coverage.market is market
            and coverage.start_date <= start
            and coverage.end_date >= end
            and _visible_at(coverage, cutoff)
            and (coverage.verified or not require_verified)
        ]
        if not matching:
            raise CalendarContractError("no visible calendar coverage contains request")
        selected_by_contract: dict[tuple[str, str], list[CalendarCoverage]] = defaultdict(list)
        for coverage in matching:
            selected_by_contract[(coverage.source, coverage.calendar_version)].append(coverage)
        selected_coverages = [
            _select_revision(group) for group in selected_by_contract.values()
        ]
        if len(selected_coverages) != 1:
            raise CalendarContractError("request overlaps multiple calendar sources/versions")
        base_coverage = selected_coverages[0]
        effective = replace(base_coverage, start_date=start, end_date=end)

        groups: dict[date, list[CalendarDay]] = defaultdict(list)
        for day in self._days:
            if day.market is not market or not start <= day.session_date <= end:
                continue
            if day.source != effective.source or day.calendar_version != effective.calendar_version:
                continue
            if not _visible_at(day, cutoff):
                continue
            if require_verified and not day.verified:
                continue
            groups[day.session_date].append(day)
        selected_days: list[CalendarDay] = []
        for session_date in _dates(start, end):
            if session_date not in groups:
                raise CalendarContractError(
                    f"calendar coverage is incomplete at {session_date.isoformat()}"
                )
            selected_days.append(_select_revision(groups[session_date]))
        snapshot_id = fingerprint(
            {
                "schema": "calendar-snapshot-v1",
                "as_of": cutoff,
                "coverage": effective.fact_id,
                "days": [day.fact_id for day in selected_days],
                "require_verified": require_verified,
            }
        )
        return CalendarSnapshot(
            market=market,
            as_of=cutoff,
            coverage=effective,
            days=tuple(selected_days),
            snapshot_id=snapshot_id,
            require_verified=require_verified,
        )

    def _status_map(
        self,
        symbol: str,
        market: Market,
        dates: Iterable[date],
        as_of: datetime,
        *,
        require_verified: bool,
        extra_statuses: Iterable[InstrumentSessionStatus] = (),
    ) -> tuple[dict[date, InstrumentSessionStatus], str]:
        cutoff = to_utc(as_of, "as_of")
        wanted = set(dates)
        groups: dict[date, list[InstrumentSessionStatus]] = defaultdict(list)
        for status in (*self._statuses, *tuple(extra_statuses)):
            if status.symbol != symbol or status.market is not market:
                continue
            if status.session_date not in wanted or not _visible_at(status, cutoff):
                continue
            if require_verified and not status.verified:
                continue
            groups[status.session_date].append(status)
        selected = {day: _select_revision(records) for day, records in groups.items()}
        snapshot_id = fingerprint(
            {
                "schema": "instrument-status-snapshot-v1",
                "symbol": symbol,
                "market": market,
                "as_of": cutoff,
                "statuses": [selected[key].fact_id for key in sorted(selected)],
                "require_verified": require_verified,
            }
        )
        return selected, snapshot_id

    def align_bars(
        self,
        *,
        symbol: str,
        market: Market,
        bars: Iterable[Bar],
        start: date,
        end: date,
        as_of: datetime,
        require_verified: bool = True,
        extra_statuses: Iterable[InstrumentSessionStatus] = (),
    ) -> CalendarAlignedBars:
        snapshot = self.snapshot(
            market,
            start,
            end,
            as_of,
            require_verified=require_verified,
        )
        by_date: dict[date, Bar] = {}
        for bar in bars:
            if bar.symbol != symbol or bar.market is not market:
                raise CalendarContractError("input bar identity mismatch")
            session_date = exchange_local_date(bar.timestamp, market)
            if session_date in by_date:
                raise CalendarContractError(f"duplicate bar for {session_date}")
            if session_date not in snapshot.open_dates:
                raise CalendarContractError("bar appears on a CLOSED or uncovered date")
            by_date[session_date] = bar

        statuses, status_snapshot_id = self._status_map(
            symbol,
            market,
            snapshot.open_dates,
            as_of,
            require_verified=require_verified,
            extra_statuses=extra_statuses,
        )
        output: list[Bar] = []
        states: list[InstrumentSessionState] = []
        placeholders: list[date] = []
        previous_close: float | None = None
        for day in snapshot.open_days:
            real = by_date.get(day.session_date)
            status = statuses.get(day.session_date)
            if real is not None and status is not None:
                raise CalendarContractError("same session has both a real bar and no-bar status")
            if real is not None:
                if real.volume <= 0:
                    raise CalendarContractError(
                        "zero-volume market-open bars require explicit no-bar status"
                    )
                output.append(real)
                states.append(InstrumentSessionState.OPEN)
                previous_close = real.close
                continue
            if status is None:
                raise CalendarContractError(
                    f"missing bar at {day.session_date} has no verified explanation"
                )
            reference = status.reference_price or previous_close
            if reference is None or reference <= 0:
                raise CalendarContractError(
                    "first placeholder session requires an explicit reference price"
                )
            if day.close_time is None:
                raise CalendarContractError("OPEN day unexpectedly lacks close_time")
            placeholder = Bar(
                symbol=symbol,
                market=market,
                timestamp=day.close_time,
                interval="1d",
                open=reference,
                high=reference,
                low=reference,
                close=reference,
                volume=0,
                amount=0.0,
                turnover=0.0,
                source=f"{status.source}:placeholder",
                adjustment_factor=status.share_factor,
                quality_status=DataStatus.STALE,
            )
            output.append(placeholder)
            states.append(status.status)
            placeholders.append(day.session_date)
            previous_close = reference

        session_dates = snapshot.open_dates
        return CalendarAlignedBars(
            symbol=symbol,
            market=market,
            bars=tuple(output),
            session_dates=session_dates,
            session_states=tuple(states),
            placeholder_dates=tuple(placeholders),
            calendar_as_of=to_utc(as_of),
            calendar_snapshot_id=snapshot.snapshot_id,
            calendar_version=snapshot.coverage.calendar_version,
            instrument_status_snapshot_id=status_snapshot_id,
            coverage_start=start,
            coverage_end=end,
        )
