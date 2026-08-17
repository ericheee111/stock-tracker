"""Point-in-time security identity, status and historical-universe contracts.

The contracts in this module are intentionally provider-neutral.  They do not
fetch data or infer missing membership.  A research snapshot is produced only
when one visible, complete universe coverage/version exists and every selected
membership has a visible instrument identity and security status for the target
session.  Absence is therefore not silently treated as exclusion.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TypeVar

from stock_tracker.core.types import Market

from .calendar import CalendarSnapshot, CalendarStatus
from .fingerprint import fingerprint
from .point_in_time import PITConflictError, Revision, revision_key
from .time import to_utc


class UniverseContractError(ValueError):
    """Raised when a historical security universe is incomplete or unsafe."""


class SecurityType(StrEnum):
    COMMON_EQUITY = "COMMON_EQUITY"
    PREFERRED_EQUITY = "PREFERRED_EQUITY"
    ETF = "ETF"
    FUND = "FUND"
    BOND = "BOND"
    INDEX = "INDEX"
    OTHER = "OTHER"


class ListingState(StrEnum):
    PRE_LISTING = "PRE_LISTING"
    LISTED = "LISTED"
    DELISTING = "DELISTING"
    DELISTED = "DELISTED"


class TradingState(StrEnum):
    TRADABLE = "TRADABLE"
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class RiskDesignation(StrEnum):
    NORMAL = "NORMAL"
    ST = "ST"
    STAR_ST = "STAR_ST"
    RISK_WARNING = "RISK_WARNING"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class UniverseMembershipState(StrEnum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"


_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise UniverseContractError(f"{name} must be a boolean")
    return value


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise UniverseContractError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise UniverseContractError(f"{name} cannot contain surrounding whitespace")
    return value


def _require_symbol(symbol: object, market: Market) -> str:
    value = _require_text(symbol, "symbol")
    if value != value.upper():
        raise UniverseContractError("symbol must use canonical uppercase form")
    code, separator, suffix = value.rpartition(".")
    if not separator or not code or suffix not in _SYMBOL_SUFFIXES[market]:
        raise UniverseContractError("symbol suffix must match market")
    return value


def _require_visibility(known_at: datetime, usable_from: datetime) -> None:
    known = to_utc(known_at, "known_at")
    usable = to_utc(usable_from, "usable_from")
    if usable < known:
        raise UniverseContractError("usable_from cannot precede known_at")


def _require_date_range(start: date, end: date | None, name: str) -> None:
    if end is not None and end < start:
        raise UniverseContractError(f"{name} end cannot precede start")


TRevisionFact = TypeVar(
    "TRevisionFact",
    "UniverseCoverage",
    "InstrumentIdentityFact",
    "SecurityStatusFact",
    "UniverseMembershipFact",
)


def _select_revision(records: Iterable[TRevisionFact]) -> TRevisionFact:
    candidates = tuple(records)
    if not candidates:
        raise LookupError("no visible revision")
    newest_known_at = max(to_utc(record.known_at) for record in candidates)
    newest = [
        record
        for record in candidates
        if to_utc(record.known_at) == newest_known_at
    ]
    highest_revision = max(revision_key(record.revision) for record in newest)
    finalists = [
        record
        for record in newest
        if revision_key(record.revision) == highest_revision
    ]
    identities = {fingerprint(record) for record in finalists}
    if len(identities) != 1:
        raise PITConflictError(
            "universe/security revisions share known_at and revision but disagree"
        )
    return min(finalists, key=fingerprint)


@dataclass(frozen=True, slots=True)
class UniverseCoverage:
    universe_id: str
    market: Market
    start_date: date
    end_date: date
    source: str
    universe_version: str
    known_at: datetime
    usable_from: datetime
    revision: Revision
    verified: bool
    complete: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.universe_id, "universe_id")
        if not isinstance(self.market, Market):
            raise UniverseContractError("market must be Market")
        _require_date_range(self.start_date, self.end_date, "coverage")
        _require_text(self.source, "source")
        _require_text(self.universe_version, "universe_version")
        _require_visibility(self.known_at, self.usable_from)
        revision_key(self.revision)
        _require_bool(self.verified, "verified")
        _require_bool(self.complete, "complete")
        if self.verified and not self.source_note:
            raise UniverseContractError("verified coverage requires a source note")
        if self.complete and not self.source_note:
            raise UniverseContractError("complete coverage requires a source note")

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class InstrumentIdentityFact:
    instrument_id: str
    symbol: str
    market: Market
    exchange: str
    security_type: SecurityType
    effective_from: date
    effective_to: date | None
    known_at: datetime
    usable_from: datetime
    source: str
    revision: Revision
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.market, Market):
            raise UniverseContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        _require_text(self.exchange, "exchange")
        if not isinstance(self.security_type, SecurityType):
            raise UniverseContractError("security_type must be SecurityType")
        _require_date_range(self.effective_from, self.effective_to, "identity")
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        revision_key(self.revision)
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise UniverseContractError("verified identity requires a source note")

    def active_on(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date <= self.effective_to
        )

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class SecurityStatusFact:
    instrument_id: str
    symbol: str
    market: Market
    session_date: date
    listing_state: ListingState
    trading_state: TradingState
    risk_designation: RiskDesignation
    known_at: datetime
    usable_from: datetime
    source: str
    revision: Revision
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.market, Market):
            raise UniverseContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if not isinstance(self.listing_state, ListingState):
            raise UniverseContractError("listing_state must be ListingState")
        if not isinstance(self.trading_state, TradingState):
            raise UniverseContractError("trading_state must be TradingState")
        if not isinstance(self.risk_designation, RiskDesignation):
            raise UniverseContractError("risk_designation must be RiskDesignation")
        if (
            self.listing_state in {ListingState.PRE_LISTING, ListingState.DELISTED}
            and self.trading_state is TradingState.TRADABLE
        ):
            raise UniverseContractError(
                "PRE_LISTING or DELISTED security cannot be TRADABLE"
            )
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        revision_key(self.revision)
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise UniverseContractError("verified security status requires a source note")

    @property
    def is_listed(self) -> bool:
        return self.listing_state in {ListingState.LISTED, ListingState.DELISTING}

    @property
    def is_tradable(self) -> bool:
        return self.is_listed and self.trading_state is TradingState.TRADABLE

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class UniverseMembershipFact:
    universe_id: str
    instrument_id: str
    symbol: str
    market: Market
    effective_date: date
    state: UniverseMembershipState
    known_at: datetime
    usable_from: datetime
    source: str
    universe_version: str
    revision: Revision
    verified: bool
    reason: str
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.universe_id, "universe_id")
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.market, Market):
            raise UniverseContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if not isinstance(self.state, UniverseMembershipState):
            raise UniverseContractError("state must be UniverseMembershipState")
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        _require_text(self.universe_version, "universe_version")
        revision_key(self.revision)
        _require_bool(self.verified, "verified")
        _require_text(self.reason, "reason")
        if self.verified and not self.source_note:
            raise UniverseContractError("verified membership requires a source note")

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    universe_id: str
    market: Market
    session_date: date
    as_of: datetime
    coverage: UniverseCoverage
    memberships: tuple[UniverseMembershipFact, ...]
    identities: tuple[InstrumentIdentityFact, ...]
    statuses: tuple[SecurityStatusFact, ...]
    security_status_snapshot_id: str
    snapshot_id: str
    require_verified: bool = True
    require_complete: bool = True

    def __post_init__(self) -> None:
        cutoff = to_utc(self.as_of, "as_of")
        _require_bool(self.require_verified, "require_verified")
        _require_bool(self.require_complete, "require_complete")
        if self.require_verified and not self.coverage.verified:
            raise UniverseContractError("verified snapshot requires verified coverage")
        if self.require_complete and not self.coverage.complete:
            raise UniverseContractError("complete snapshot requires complete coverage")
        if self.coverage.universe_id != self.universe_id:
            raise UniverseContractError("coverage universe_id mismatch")
        if self.coverage.market is not self.market:
            raise UniverseContractError("coverage market mismatch")
        if not self.coverage.start_date <= self.session_date <= self.coverage.end_date:
            raise UniverseContractError("session_date is outside universe coverage")
        if (
            to_utc(self.coverage.known_at) > cutoff
            or to_utc(self.coverage.usable_from) > cutoff
        ):
            raise UniverseContractError("future or unusable coverage entered snapshot")
        membership_ids = tuple(item.instrument_id for item in self.memberships)
        identity_ids = tuple(item.instrument_id for item in self.identities)
        status_ids = tuple(item.instrument_id for item in self.statuses)
        if membership_ids != tuple(sorted(membership_ids)):
            raise UniverseContractError("memberships must be sorted by instrument_id")
        if len(set(membership_ids)) != len(membership_ids):
            raise UniverseContractError("memberships must be unique by instrument_id")
        if identity_ids != membership_ids or status_ids != membership_ids:
            raise UniverseContractError(
                "every selected membership requires one identity and one status"
            )
        included_symbols = tuple(
            item.symbol
            for item in self.memberships
            if item.state is UniverseMembershipState.INCLUDED
        )
        if len(set(included_symbols)) != len(included_symbols):
            raise UniverseContractError(
                "one session cannot include multiple instruments under the same symbol"
            )
        for membership, identity, status in zip(
            self.memberships,
            self.identities,
            self.statuses,
        ):
            if membership.universe_id != self.universe_id:
                raise UniverseContractError("membership universe_id mismatch")
            if membership.market is not self.market:
                raise UniverseContractError("membership market mismatch")
            if membership.source != self.coverage.source:
                raise UniverseContractError("membership source differs from coverage")
            if membership.universe_version != self.coverage.universe_version:
                raise UniverseContractError("membership version differs from coverage")
            if identity.market is not self.market or status.market is not self.market:
                raise UniverseContractError("identity/status market mismatch")
            if (
                identity.instrument_id != membership.instrument_id
                or status.instrument_id != membership.instrument_id
            ):
                raise UniverseContractError("identity/status instrument_id mismatch")
            if membership.effective_date > self.session_date:
                raise UniverseContractError("future membership entered snapshot")
            if membership.state is UniverseMembershipState.INCLUDED:
                if identity.symbol != membership.symbol or status.symbol != membership.symbol:
                    raise UniverseContractError("identity/status symbol mismatch")
                if not identity.active_on(self.session_date):
                    raise UniverseContractError(
                        "included instrument identity is inactive on session_date"
                    )
                if status.session_date != self.session_date:
                    raise UniverseContractError(
                        "included membership requires target-session security status"
                    )
            else:
                if identity.symbol != membership.symbol:
                    raise UniverseContractError(
                        "excluded membership symbol must match its exit identity"
                    )
                if not identity.active_on(membership.effective_date):
                    raise UniverseContractError(
                        "excluded membership requires identity active on exclusion date"
                    )
                if status.session_date > membership.effective_date:
                    raise UniverseContractError(
                        "excluded membership cannot use post-exclusion security status"
                    )
            for record in (membership, identity, status):
                if self.require_verified and not record.verified:
                    raise UniverseContractError(
                        "unverified fact entered verified universe snapshot"
                    )
                if to_utc(record.known_at) > cutoff or to_utc(record.usable_from) > cutoff:
                    raise UniverseContractError("future or unusable fact entered snapshot")
            if (
                membership.state is UniverseMembershipState.INCLUDED
                and not status.is_listed
            ):
                raise UniverseContractError(
                    "included member cannot be PRE_LISTING or DELISTED"
                )
        if len(self.security_status_snapshot_id) != 64:
            raise UniverseContractError("security_status_snapshot_id must be SHA-256")
        if len(self.snapshot_id) != 64:
            raise UniverseContractError("snapshot_id must be SHA-256")
        expected_status_id = fingerprint(
            {
                "schema": "security-status-snapshot-v1",
                "market": self.market,
                "session_date": self.session_date,
                "as_of": cutoff,
                "status_fact_ids": [status.fact_id for status in self.statuses],
                "require_verified": self.require_verified,
            }
        )
        if self.security_status_snapshot_id != expected_status_id:
            raise UniverseContractError(
                "security_status_snapshot_id does not match snapshot content"
            )
        expected_snapshot_id = fingerprint(
            {
                "schema": "historical-universe-snapshot-v1",
                "universe_id": self.universe_id,
                "market": self.market,
                "session_date": self.session_date,
                "as_of": cutoff,
                "coverage_fact_id": self.coverage.fact_id,
                "membership_fact_ids": [item.fact_id for item in self.memberships],
                "identity_fact_ids": [item.fact_id for item in self.identities],
                "security_status_snapshot_id": self.security_status_snapshot_id,
                "require_verified": self.require_verified,
                "require_complete": self.require_complete,
            }
        )
        if self.snapshot_id != expected_snapshot_id:
            raise UniverseContractError("snapshot_id does not match snapshot content")

    @property
    def member_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                item.symbol
                for item in self.memberships
                if item.state is UniverseMembershipState.INCLUDED
            )
        )

    @property
    def tradable_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                membership.symbol
                for membership, status in zip(self.memberships, self.statuses)
                if membership.state is UniverseMembershipState.INCLUDED
                and status.is_tradable
            )
        )

    @property
    def delisted_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                status.symbol
                for status in self.statuses
                if status.listing_state is ListingState.DELISTED
            )
        )

    @property
    def delisted_instrument_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                status.instrument_id
                for status in self.statuses
                if status.listing_state is ListingState.DELISTED
            )
        )


class HistoricalUniverse:
    """Append-only identity/status/membership facts with PIT snapshot selection."""

    def __init__(
        self,
        coverages: Iterable[UniverseCoverage],
        identities: Iterable[InstrumentIdentityFact],
        statuses: Iterable[SecurityStatusFact],
        memberships: Iterable[UniverseMembershipFact],
    ) -> None:
        self._coverages = tuple(coverages)
        self._identities = tuple(identities)
        self._statuses = tuple(statuses)
        self._memberships = tuple(memberships)

    @staticmethod
    def _visible(record: object, cutoff: datetime, require_verified: bool) -> bool:
        verified = getattr(record, "verified")
        return (
            (verified or not require_verified)
            and to_utc(getattr(record, "known_at")) <= cutoff
            and to_utc(getattr(record, "usable_from")) <= cutoff
        )

    def snapshot(
        self,
        universe_id: str,
        market: Market,
        session_date: date,
        as_of: datetime,
        *,
        require_verified: bool = True,
        require_complete: bool = True,
    ) -> UniverseSnapshot:
        _require_text(universe_id, "universe_id")
        if not isinstance(market, Market):
            raise UniverseContractError("market must be Market")
        _require_bool(require_verified, "require_verified")
        _require_bool(require_complete, "require_complete")
        cutoff = to_utc(as_of, "as_of")

        coverage_groups: dict[tuple[str, str], list[UniverseCoverage]] = defaultdict(list)
        for coverage in self._coverages:
            if coverage.universe_id != universe_id or coverage.market is not market:
                continue
            if not coverage.start_date <= session_date <= coverage.end_date:
                continue
            if not self._visible(coverage, cutoff, require_verified):
                continue
            if require_complete and not coverage.complete:
                continue
            coverage_groups[(coverage.source, coverage.universe_version)].append(
                coverage
            )
        selected_coverages = [
            _select_revision(group) for group in coverage_groups.values()
        ]
        if not selected_coverages:
            raise UniverseContractError(
                "no visible complete universe coverage contains session_date"
            )
        if len(selected_coverages) != 1:
            raise UniverseContractError(
                "request overlaps multiple universe sources/versions"
            )
        coverage = selected_coverages[0]

        membership_revisions: dict[
            tuple[str, date], list[UniverseMembershipFact]
        ] = defaultdict(list)
        for membership in self._memberships:
            if membership.universe_id != universe_id or membership.market is not market:
                continue
            if membership.source != coverage.source:
                continue
            if membership.universe_version != coverage.universe_version:
                continue
            if membership.effective_date > session_date:
                continue
            if not self._visible(membership, cutoff, require_verified):
                continue
            membership_revisions[(membership.instrument_id, membership.effective_date)].append(
                membership
            )
        selected_events = [
            _select_revision(group) for group in membership_revisions.values()
        ]
        latest_membership: dict[str, UniverseMembershipFact] = {}
        for membership in selected_events:
            current = latest_membership.get(membership.instrument_id)
            if current is None or membership.effective_date > current.effective_date:
                latest_membership[membership.instrument_id] = membership
        if not latest_membership:
            raise UniverseContractError("universe snapshot has no membership history")

        wanted_instrument_ids = set(latest_membership)
        identity_revisions: dict[
            tuple[str, date], list[InstrumentIdentityFact]
        ] = defaultdict(list)
        for identity in self._identities:
            membership = latest_membership.get(identity.instrument_id)
            if membership is None or identity.market is not market:
                continue
            identity_anchor = (
                session_date
                if membership.state is UniverseMembershipState.INCLUDED
                else membership.effective_date
            )
            if identity.effective_from > identity_anchor:
                continue
            if not identity.active_on(identity_anchor):
                continue
            if not self._visible(identity, cutoff, require_verified):
                continue
            identity_revisions[(identity.instrument_id, identity.effective_from)].append(identity)
        selected_identity_events = [
            _select_revision(group) for group in identity_revisions.values()
        ]
        latest_identity: dict[str, InstrumentIdentityFact] = {}
        for identity in selected_identity_events:
            current = latest_identity.get(identity.instrument_id)
            if current is None or identity.effective_from > current.effective_from:
                latest_identity[identity.instrument_id] = identity

        status_revisions: dict[
            tuple[str, date], list[SecurityStatusFact]
        ] = defaultdict(list)
        for status in self._statuses:
            membership = latest_membership.get(status.instrument_id)
            if membership is None or status.market is not market:
                continue
            if membership.state is UniverseMembershipState.INCLUDED:
                if status.session_date != session_date:
                    continue
            elif status.session_date > membership.effective_date:
                continue
            if not self._visible(status, cutoff, require_verified):
                continue
            status_revisions[(status.instrument_id, status.session_date)].append(status)
        selected_status_events = [
            _select_revision(group) for group in status_revisions.values()
        ]
        latest_status: dict[str, SecurityStatusFact] = {}
        for status in selected_status_events:
            current = latest_status.get(status.instrument_id)
            if current is None or status.session_date > current.session_date:
                latest_status[status.instrument_id] = status

        missing_identity = sorted(wanted_instrument_ids - set(latest_identity))
        missing_status = sorted(wanted_instrument_ids - set(latest_status))
        if missing_identity:
            raise UniverseContractError(
                "membership lacks visible instrument identity: "
                + ", ".join(missing_identity)
            )
        if missing_status:
            raise UniverseContractError(
                "membership lacks visible security status: "
                + ", ".join(missing_status)
            )

        instrument_ids = tuple(sorted(wanted_instrument_ids))
        memberships = tuple(latest_membership[item] for item in instrument_ids)
        identities = tuple(latest_identity[item] for item in instrument_ids)
        statuses = tuple(latest_status[item] for item in instrument_ids)
        security_status_snapshot_id = fingerprint(
            {
                "schema": "security-status-snapshot-v1",
                "market": market,
                "session_date": session_date,
                "as_of": cutoff,
                "status_fact_ids": [status.fact_id for status in statuses],
                "require_verified": require_verified,
            }
        )
        snapshot_id = fingerprint(
            {
                "schema": "historical-universe-snapshot-v1",
                "universe_id": universe_id,
                "market": market,
                "session_date": session_date,
                "as_of": cutoff,
                "coverage_fact_id": coverage.fact_id,
                "membership_fact_ids": [item.fact_id for item in memberships],
                "identity_fact_ids": [item.fact_id for item in identities],
                "security_status_snapshot_id": security_status_snapshot_id,
                "require_verified": require_verified,
                "require_complete": require_complete,
            }
        )
        return UniverseSnapshot(
            universe_id=universe_id,
            market=market,
            session_date=session_date,
            as_of=cutoff,
            coverage=coverage,
            memberships=memberships,
            identities=identities,
            statuses=statuses,
            security_status_snapshot_id=security_status_snapshot_id,
            snapshot_id=snapshot_id,
            require_verified=require_verified,
            require_complete=require_complete,
        )


@dataclass(frozen=True, slots=True)
class ResearchIdentitySnapshot:
    market: Market
    session_date: date
    as_of: datetime
    calendar_snapshot_id: str
    calendar_version: str
    universe_snapshot_id: str
    universe_id: str
    universe_version: str
    security_status_snapshot_id: str
    member_symbols: tuple[str, ...]
    tradable_symbols: tuple[str, ...]
    snapshot_id: str

    def __post_init__(self) -> None:
        to_utc(self.as_of, "as_of")
        for name in (
            "calendar_snapshot_id",
            "universe_snapshot_id",
            "security_status_snapshot_id",
            "snapshot_id",
        ):
            if len(getattr(self, name)) != 64:
                raise UniverseContractError(f"{name} must be SHA-256")
        if tuple(sorted(self.member_symbols)) != self.member_symbols:
            raise UniverseContractError("member_symbols must be sorted")
        if not set(self.tradable_symbols).issubset(set(self.member_symbols)):
            raise UniverseContractError("tradable_symbols must be members")
        expected_snapshot_id = fingerprint(
            {
                "schema": "research-identity-snapshot-v1",
                "market": self.market,
                "session_date": self.session_date,
                "as_of": to_utc(self.as_of),
                "calendar_snapshot_id": self.calendar_snapshot_id,
                "calendar_version": self.calendar_version,
                "universe_snapshot_id": self.universe_snapshot_id,
                "universe_id": self.universe_id,
                "universe_version": self.universe_version,
                "security_status_snapshot_id": self.security_status_snapshot_id,
                "member_symbols": self.member_symbols,
                "tradable_symbols": self.tradable_symbols,
            }
        )
        if self.snapshot_id != expected_snapshot_id:
            raise UniverseContractError(
                "research identity snapshot_id does not match content"
            )


def build_research_identity_snapshot(
    calendar: CalendarSnapshot,
    universe: UniverseSnapshot,
) -> ResearchIdentitySnapshot:
    """Bind one open calendar session to one complete PIT universe snapshot."""

    if not universe.require_verified or not universe.require_complete:
        raise UniverseContractError(
            "research identity requires verified, complete universe policy"
        )
    if not calendar.coverage.verified or any(not day.verified for day in calendar.days):
        raise UniverseContractError("research identity requires verified calendar facts")
    if calendar.market is not universe.market:
        raise UniverseContractError("calendar and universe market mismatch")
    if to_utc(calendar.as_of) != to_utc(universe.as_of):
        raise UniverseContractError("calendar and universe as_of must match")
    matching_days = [
        day for day in calendar.days if day.session_date == universe.session_date
    ]
    if len(matching_days) != 1:
        raise UniverseContractError("universe session_date is not in calendar snapshot")
    if matching_days[0].status is not CalendarStatus.OPEN:
        raise UniverseContractError("research identity requires an OPEN session")
    payload = {
        "schema": "research-identity-snapshot-v1",
        "market": universe.market,
        "session_date": universe.session_date,
        "as_of": to_utc(universe.as_of),
        "calendar_snapshot_id": calendar.snapshot_id,
        "calendar_version": calendar.coverage.calendar_version,
        "universe_snapshot_id": universe.snapshot_id,
        "universe_id": universe.universe_id,
        "universe_version": universe.coverage.universe_version,
        "security_status_snapshot_id": universe.security_status_snapshot_id,
        "member_symbols": universe.member_symbols,
        "tradable_symbols": universe.tradable_symbols,
    }
    return ResearchIdentitySnapshot(
        market=universe.market,
        session_date=universe.session_date,
        as_of=to_utc(universe.as_of),
        calendar_snapshot_id=calendar.snapshot_id,
        calendar_version=calendar.coverage.calendar_version,
        universe_snapshot_id=universe.snapshot_id,
        universe_id=universe.universe_id,
        universe_version=universe.coverage.universe_version,
        security_status_snapshot_id=universe.security_status_snapshot_id,
        member_symbols=universe.member_symbols,
        tradable_symbols=universe.tradable_symbols,
        snapshot_id=fingerprint(payload),
    )
