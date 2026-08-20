"""Point-in-time industry, index and sector classification contracts.

Runtime convenience mappings are not research identities.  This module binds a
versioned taxonomy, classification definitions, membership revisions and the
instrument identity active on the membership effective date.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from stock_tracker.core.types import Market

from .calendar import select_superseding_revision
from .fingerprint import fingerprint
from .point_in_time import PITConflictError, Revision, revision_key
from .time import exchange_local_date, to_utc
from .universe import InstrumentIdentityFact

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}


class ClassificationContractError(ValueError):
    """Raised when a classification identity is not PIT-safe."""


class ClassificationKind(StrEnum):
    INDUSTRY = "INDUSTRY"
    INDEX = "INDEX"
    THEME = "THEME"
    CUSTOM = "CUSTOM"


class ClassificationAuthority(StrEnum):
    OFFICIAL_REGULATOR = "OFFICIAL_REGULATOR"
    OFFICIAL_INDEX_COMPILER = "OFFICIAL_INDEX_COMPILER"
    SECONDARY_VENDOR = "SECONDARY_VENDOR"
    INTERNAL = "INTERNAL"


class ClassificationMembershipState(StrEnum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ClassificationContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ClassificationContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ClassificationContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_visibility(known_at: datetime, usable_from: datetime) -> None:
    known = to_utc(known_at, "known_at")
    usable = to_utc(usable_from, "usable_from")
    if usable < known:
        raise ClassificationContractError("usable_from cannot precede known_at")


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol")
    if symbol != symbol.upper():
        raise ClassificationContractError("symbol must be uppercase canonical form")
    code, separator, suffix = symbol.rpartition(".")
    if not separator or not code or suffix not in _SYMBOL_SUFFIXES[market]:
        raise ClassificationContractError("symbol suffix must match market")
    return symbol


def _active(start: date, end: date | None, session_date: date) -> bool:
    return start <= session_date and (end is None or session_date <= end)


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


@dataclass(frozen=True, slots=True)
class ClassificationTaxonomy:
    taxonomy_id: str
    name: str
    kind: ClassificationKind
    authority: ClassificationAuthority
    owner: str
    taxonomy_version: str
    commercial_definition: bool
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.taxonomy_id, "taxonomy_id")
        _require_text(self.name, "name")
        if not isinstance(self.kind, ClassificationKind):
            raise ClassificationContractError("kind must be ClassificationKind")
        if not isinstance(self.authority, ClassificationAuthority):
            raise ClassificationContractError(
                "authority must be ClassificationAuthority"
            )
        _require_text(self.owner, "owner")
        _require_text(self.taxonomy_version, "taxonomy_version")
        _require_bool(self.commercial_definition, "commercial_definition")
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise ClassificationContractError(
                "verified taxonomy requires source_note"
            )
        if (
            self.kind is ClassificationKind.THEME
            and self.authority is ClassificationAuthority.OFFICIAL_REGULATOR
        ):
            raise ClassificationContractError(
                "regulatory industry authority cannot be relabelled as theme authority"
            )

    @property
    def taxonomy_identity(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class ClassificationCoverage:
    taxonomy_id: str
    market: Market
    start_date: date
    end_date: date
    source: str
    taxonomy_version: str
    known_at: datetime
    usable_from: datetime
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    complete: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.taxonomy_id, "taxonomy_id")
        if not isinstance(self.market, Market):
            raise ClassificationContractError("market must be Market")
        if self.end_date < self.start_date:
            raise ClassificationContractError(
                "coverage end_date cannot precede start_date"
            )
        _require_text(self.source, "source")
        _require_text(self.taxonomy_version, "taxonomy_version")
        _require_visibility(self.known_at, self.usable_from)
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise ClassificationContractError(
                    "coverage revision cannot supersede itself"
                )
        _require_bool(self.verified, "verified")
        _require_bool(self.complete, "complete")
        if (self.verified or self.complete) and not self.source_note:
            raise ClassificationContractError(
                "verified/complete coverage requires source_note"
            )

    @property
    def coverage_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class ClassificationFact:
    taxonomy_id: str
    classification_id: str
    name: str
    parent_classification_id: str | None
    effective_from: date
    effective_to: date | None
    known_at: datetime
    usable_from: datetime
    source: str
    taxonomy_version: str
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.taxonomy_id, "taxonomy_id")
        _require_text(self.classification_id, "classification_id")
        _require_text(self.name, "name")
        if self.parent_classification_id is not None:
            _require_text(
                self.parent_classification_id,
                "parent_classification_id",
            )
            if self.parent_classification_id == self.classification_id:
                raise ClassificationContractError(
                    "classification cannot be its own parent"
                )
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ClassificationContractError(
                "classification effective_to cannot precede effective_from"
            )
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        _require_text(self.taxonomy_version, "taxonomy_version")
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise ClassificationContractError(
                    "classification revision cannot supersede itself"
                )
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise ClassificationContractError(
                "verified classification requires source_note"
            )

    @property
    def fact_id(self) -> str:
        return fingerprint(self)

    def active_on(self, session_date: date) -> bool:
        return _active(self.effective_from, self.effective_to, session_date)


@dataclass(frozen=True, slots=True)
class ClassificationMembershipFact:
    taxonomy_id: str
    classification_id: str
    instrument_id: str
    identity_fact_id: str
    symbol: str
    market: Market
    effective_from: date
    effective_to: date | None
    state: ClassificationMembershipState
    known_at: datetime
    usable_from: datetime
    source: str
    taxonomy_version: str
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.taxonomy_id, "taxonomy_id")
        _require_text(self.classification_id, "classification_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        if not isinstance(self.market, Market):
            raise ClassificationContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ClassificationContractError(
                "membership effective_to cannot precede effective_from"
            )
        if not isinstance(self.state, ClassificationMembershipState):
            raise ClassificationContractError(
                "state must be ClassificationMembershipState"
            )
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        _require_text(self.taxonomy_version, "taxonomy_version")
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise ClassificationContractError(
                    "membership revision cannot supersede itself"
                )
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise ClassificationContractError(
                "verified membership requires source_note"
            )

    @property
    def fact_id(self) -> str:
        return fingerprint(self)

    def active_on(self, session_date: date) -> bool:
        return _active(self.effective_from, self.effective_to, session_date)


@dataclass(frozen=True, slots=True)
class ClassificationSnapshot:
    taxonomy: ClassificationTaxonomy
    market: Market
    session_date: date
    as_of: datetime
    coverage: ClassificationCoverage
    classifications: tuple[ClassificationFact, ...]
    memberships: tuple[ClassificationMembershipFact, ...]
    identities: tuple[InstrumentIdentityFact, ...]
    require_verified: bool = True
    require_complete: bool = True
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        cutoff = to_utc(self.as_of, "as_of")
        if self.taxonomy.taxonomy_id != self.coverage.taxonomy_id:
            raise ClassificationContractError(
                "taxonomy and coverage taxonomy_id mismatch"
            )
        if self.taxonomy.taxonomy_version != self.coverage.taxonomy_version:
            raise ClassificationContractError(
                "taxonomy and coverage version mismatch"
            )
        if self.market is not self.coverage.market:
            raise ClassificationContractError("coverage market mismatch")
        if not self.coverage.start_date <= self.session_date <= self.coverage.end_date:
            raise ClassificationContractError(
                "coverage does not contain snapshot session_date"
            )
        _require_bool(self.require_verified, "require_verified")
        _require_bool(self.require_complete, "require_complete")
        if to_utc(self.coverage.known_at) > cutoff or to_utc(self.coverage.usable_from) > cutoff:
            raise ClassificationContractError("future coverage entered snapshot")
        if self.require_verified and (
            not self.taxonomy.verified or not self.coverage.verified
        ):
            raise ClassificationContractError(
                "verified snapshot requires verified taxonomy and coverage"
            )
        if self.require_complete and not self.coverage.complete:
            raise ClassificationContractError(
                "complete snapshot requires complete coverage"
            )
        class_order = tuple(
            (item.classification_id, item.fact_id) for item in self.classifications
        )
        if class_order != tuple(sorted(class_order)):
            raise ClassificationContractError(
                "classifications must be deterministically sorted"
            )
        if len({item.classification_id for item in self.classifications}) != len(
            self.classifications
        ):
            raise ClassificationContractError(
                "active classifications must be unique by classification_id"
            )
        membership_order = tuple(
            (
                item.classification_id,
                item.instrument_id,
                item.fact_id,
            )
            for item in self.memberships
        )
        if membership_order != tuple(sorted(membership_order)):
            raise ClassificationContractError(
                "memberships must be deterministically sorted"
            )
        membership_keys = [
            (item.classification_id, item.instrument_id)
            for item in self.memberships
        ]
        if len(set(membership_keys)) != len(membership_keys):
            raise ClassificationContractError(
                "active memberships must be unique by classification/instrument"
            )
        identity_order = tuple(item.fact_id for item in self.identities)
        if identity_order != tuple(sorted(identity_order)):
            raise ClassificationContractError(
                "identities must be sorted by fact_id"
            )
        if len(set(identity_order)) != len(identity_order):
            raise ClassificationContractError(
                "identities must be unique by fact_id"
            )
        classification_ids = {
            item.classification_id for item in self.classifications
        }
        identity_by_id = {item.fact_id: item for item in self.identities}
        referenced_identity_ids: set[str] = set()
        for item in self.classifications:
            if (
                item.taxonomy_id != self.taxonomy.taxonomy_id
                or item.source != self.coverage.source
                or item.taxonomy_version != self.coverage.taxonomy_version
                or not item.active_on(self.session_date)
            ):
                raise ClassificationContractError(
                    "classification does not belong to active snapshot stream"
                )
            if to_utc(item.known_at) > cutoff or to_utc(item.usable_from) > cutoff:
                raise ClassificationContractError(
                    "future classification entered snapshot"
                )
            if self.require_verified and not item.verified:
                raise ClassificationContractError(
                    "unverified classification entered verified snapshot"
                )
        for item in self.memberships:
            if (
                item.taxonomy_id != self.taxonomy.taxonomy_id
                or item.market is not self.market
                or item.source != self.coverage.source
                or item.taxonomy_version != self.coverage.taxonomy_version
                or not item.active_on(self.session_date)
            ):
                raise ClassificationContractError(
                    "membership does not belong to active snapshot stream"
                )
            if item.classification_id not in classification_ids:
                raise ClassificationContractError(
                    "membership references missing active classification"
                )
            if to_utc(item.known_at) > cutoff or to_utc(item.usable_from) > cutoff:
                raise ClassificationContractError("future membership entered snapshot")
            if self.require_verified and not item.verified:
                raise ClassificationContractError(
                    "unverified membership entered verified snapshot"
                )
            identity = identity_by_id.get(item.identity_fact_id)
            if identity is None:
                raise ClassificationContractError(
                    "membership identity_fact_id is missing"
                )
            referenced_identity_ids.add(identity.fact_id)
            if (
                identity.instrument_id != item.instrument_id
                or identity.market is not item.market
                or identity.symbol != item.symbol
                or not identity.active_on(self.session_date)
            ):
                raise ClassificationContractError(
                    "membership is not bound to identity active on session_date"
                )
            if to_utc(identity.known_at) > cutoff or to_utc(identity.usable_from) > cutoff:
                raise ClassificationContractError("future identity entered snapshot")
            if self.require_verified and not identity.verified:
                raise ClassificationContractError(
                    "unverified identity entered verified snapshot"
                )
        if referenced_identity_ids != set(identity_order):
            raise ClassificationContractError(
                "snapshot identities must exactly match membership references"
            )
        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "classification-snapshot-v1",
                    "taxonomy_identity": self.taxonomy.taxonomy_identity,
                    "market": self.market,
                    "session_date": self.session_date,
                    "as_of": cutoff,
                    "coverage_id": self.coverage.coverage_id,
                    "classification_fact_ids": [
                        item.fact_id for item in self.classifications
                    ],
                    "membership_fact_ids": [
                        item.fact_id for item in self.memberships
                    ],
                    "identity_fact_ids": list(identity_order),
                    "require_verified": self.require_verified,
                    "require_complete": self.require_complete,
                }
            ),
        )

    @property
    def included_memberships(self) -> tuple[ClassificationMembershipFact, ...]:
        return tuple(
            item
            for item in self.memberships
            if item.state is ClassificationMembershipState.INCLUDED
        )

    def classification_members(self, classification_id: str) -> tuple[str, ...]:
        _require_text(classification_id, "classification_id")
        return tuple(
            sorted(
                item.symbol
                for item in self.included_memberships
                if item.classification_id == classification_id
            )
        )

    def instrument_classifications(self, instrument_id: str) -> tuple[str, ...]:
        _require_text(instrument_id, "instrument_id")
        return tuple(
            sorted(
                item.classification_id
                for item in self.included_memberships
                if item.instrument_id == instrument_id
            )
        )


@dataclass(frozen=True, slots=True)
class HistoricalClassification:
    taxonomy: ClassificationTaxonomy
    coverages: tuple[ClassificationCoverage, ...]
    classifications: tuple[ClassificationFact, ...]
    memberships: tuple[ClassificationMembershipFact, ...]
    identities: tuple[InstrumentIdentityFact, ...]

    def __init__(
        self,
        taxonomy: ClassificationTaxonomy,
        coverages: Iterable[ClassificationCoverage],
        classifications: Iterable[ClassificationFact],
        memberships: Iterable[ClassificationMembershipFact],
        identities: Iterable[InstrumentIdentityFact],
    ) -> None:
        object.__setattr__(self, "taxonomy", taxonomy)
        object.__setattr__(self, "coverages", tuple(coverages))
        object.__setattr__(self, "classifications", tuple(classifications))
        object.__setattr__(self, "memberships", tuple(memberships))
        object.__setattr__(self, "identities", tuple(identities))

    def snapshot(
        self,
        market: Market,
        session_date: date,
        as_of: datetime,
        *,
        require_verified: bool = True,
        require_complete: bool = True,
    ) -> ClassificationSnapshot:
        if not isinstance(market, Market):
            raise ClassificationContractError("market must be Market")
        _require_bool(require_verified, "require_verified")
        _require_bool(require_complete, "require_complete")
        cutoff = to_utc(as_of, "as_of")
        if session_date > exchange_local_date(as_of, market):
            raise ClassificationContractError(
                "classification snapshot cannot extend beyond as_of exchange date"
            )

        coverage_groups: dict[
            tuple[str, str], list[ClassificationCoverage]
        ] = defaultdict(list)
        for coverage in self.coverages:
            if (
                coverage.taxonomy_id == self.taxonomy.taxonomy_id
                and coverage.market is market
                and _visible(
                    coverage.known_at,
                    coverage.usable_from,
                    coverage.verified,
                    cutoff,
                    require_verified,
                )
            ):
                coverage_groups[(coverage.source, coverage.taxonomy_version)].append(
                    coverage
                )
        terminals: list[ClassificationCoverage] = []
        for rows in coverage_groups.values():
            try:
                terminals.append(
                    select_superseding_revision(
                        rows,
                        revision_of=lambda item: item.revision,
                        predecessor_of=lambda item: item.supersedes_revision,
                        payload_of=lambda item: {
                            "taxonomy_id": item.taxonomy_id,
                            "market": item.market,
                            "start_date": item.start_date,
                            "end_date": item.end_date,
                            "source": item.source,
                            "taxonomy_version": item.taxonomy_version,
                            "usable_from": item.usable_from,
                            "verified": item.verified,
                            "complete": item.complete,
                            "source_note": item.source_note,
                        },
                        identity_of=lambda item: item.coverage_id,
                        known_at_of=lambda item: item.known_at,
                    )
                )
            except PITConflictError as exc:
                raise ClassificationContractError(str(exc)) from exc
        range_coverages = [
            item
            for item in terminals
            if item.start_date <= session_date <= item.end_date
        ]
        if not range_coverages:
            raise ClassificationContractError(
                "no visible classification coverage contains session_date"
            )
        matching_coverages = [
            item
            for item in range_coverages
            if item.complete or not require_complete
        ]
        if not matching_coverages:
            raise ClassificationContractError(
                "complete snapshot requires complete classification coverage"
            )
        if len(matching_coverages) != 1:
            raise ClassificationContractError(
                "multiple classification source/version streams contain session_date"
            )
        coverage = matching_coverages[0]
        if (
            coverage.taxonomy_version != self.taxonomy.taxonomy_version
            or coverage.source != self.taxonomy.owner
        ):
            raise ClassificationContractError(
                "taxonomy metadata differs from selected coverage stream"
            )

        class_groups: dict[str, list[ClassificationFact]] = defaultdict(list)
        for item in self.classifications:
            if (
                item.taxonomy_id == self.taxonomy.taxonomy_id
                and item.source == coverage.source
                and item.taxonomy_version == coverage.taxonomy_version
                and _visible(
                    item.known_at,
                    item.usable_from,
                    item.verified,
                    cutoff,
                    require_verified,
                )
            ):
                class_groups[item.classification_id].append(item)
        selected_classes: list[ClassificationFact] = []
        for rows in class_groups.values():
            try:
                terminal = select_superseding_revision(
                    rows,
                    revision_of=lambda item: item.revision,
                    predecessor_of=lambda item: item.supersedes_revision,
                    payload_of=lambda item: {
                        "taxonomy_id": item.taxonomy_id,
                        "classification_id": item.classification_id,
                        "name": item.name,
                        "parent_classification_id": item.parent_classification_id,
                        "effective_from": item.effective_from,
                        "effective_to": item.effective_to,
                        "usable_from": item.usable_from,
                        "source": item.source,
                        "taxonomy_version": item.taxonomy_version,
                        "verified": item.verified,
                        "source_note": item.source_note,
                    },
                    identity_of=lambda item: item.fact_id,
                    known_at_of=lambda item: item.known_at,
                )
            except PITConflictError as exc:
                raise ClassificationContractError(str(exc)) from exc
            if terminal.active_on(session_date):
                selected_classes.append(terminal)

        membership_groups: dict[
            tuple[str, str], list[ClassificationMembershipFact]
        ] = defaultdict(list)
        for item in self.memberships:
            if (
                item.taxonomy_id == self.taxonomy.taxonomy_id
                and item.market is market
                and item.source == coverage.source
                and item.taxonomy_version == coverage.taxonomy_version
                and _visible(
                    item.known_at,
                    item.usable_from,
                    item.verified,
                    cutoff,
                    require_verified,
                )
            ):
                membership_groups[
                    (item.classification_id, item.instrument_id)
                ].append(item)
        selected_memberships: list[ClassificationMembershipFact] = []
        for rows in membership_groups.values():
            try:
                terminal = select_superseding_revision(
                    rows,
                    revision_of=lambda item: item.revision,
                    predecessor_of=lambda item: item.supersedes_revision,
                    payload_of=lambda item: {
                        "taxonomy_id": item.taxonomy_id,
                        "classification_id": item.classification_id,
                        "instrument_id": item.instrument_id,
                        "identity_fact_id": item.identity_fact_id,
                        "symbol": item.symbol,
                        "market": item.market,
                        "effective_from": item.effective_from,
                        "effective_to": item.effective_to,
                        "state": item.state,
                        "usable_from": item.usable_from,
                        "source": item.source,
                        "taxonomy_version": item.taxonomy_version,
                        "verified": item.verified,
                        "source_note": item.source_note,
                    },
                    identity_of=lambda item: item.fact_id,
                    known_at_of=lambda item: item.known_at,
                )
            except PITConflictError as exc:
                raise ClassificationContractError(str(exc)) from exc
            if terminal.active_on(session_date):
                selected_memberships.append(terminal)

        identities_by_id = {item.fact_id: item for item in self.identities}
        identity_ids = sorted(
            {item.identity_fact_id for item in selected_memberships}
        )
        selected_identities: list[InstrumentIdentityFact] = []
        for identity_id in identity_ids:
            identity = identities_by_id.get(identity_id)
            if identity is None or not _visible(
                identity.known_at,
                identity.usable_from,
                identity.verified,
                cutoff,
                require_verified,
            ):
                raise ClassificationContractError(
                    "selected membership references missing/future identity"
                )
            selected_identities.append(identity)

        return ClassificationSnapshot(
            taxonomy=self.taxonomy,
            market=market,
            session_date=session_date,
            as_of=cutoff,
            coverage=coverage,
            classifications=tuple(
                sorted(
                    selected_classes,
                    key=lambda item: (item.classification_id, item.fact_id),
                )
            ),
            memberships=tuple(
                sorted(
                    selected_memberships,
                    key=lambda item: (
                        item.classification_id,
                        item.instrument_id,
                        item.fact_id,
                    ),
                )
            ),
            identities=tuple(
                sorted(selected_identities, key=lambda item: item.fact_id)
            ),
            require_verified=require_verified,
            require_complete=require_complete,
        )


__all__ = [
    "ClassificationAuthority",
    "ClassificationContractError",
    "ClassificationCoverage",
    "ClassificationFact",
    "ClassificationKind",
    "ClassificationMembershipFact",
    "ClassificationMembershipState",
    "ClassificationSnapshot",
    "ClassificationTaxonomy",
    "HistoricalClassification",
]
