"""Point-in-time corporate-action and adjustment-factor identity contracts.

The module is deliberately provider-neutral and never mutates raw market bars.
A corporate-action snapshot is valid only when one explicit, visible coverage
contract exists and every selected action is bound to the stable instrument
identity that was active on its ex-date.  Adjustment factors are then derived
as a separate, content-addressed view with an explicit basis and convention.

Stage 2B is contract-only: it does not fetch official data, promote evidence
quality, or claim that a real adjusted price history is complete.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Protocol

from stock_tracker.core.types import Market

from .calendar import select_superseding_revision
from .fingerprint import fingerprint
from .point_in_time import PITConflictError, Revision, revision_key
from .time import exchange_local_date, to_utc
from .universe import InstrumentIdentityFact


class CorporateActionContractError(ValueError):
    """Raised when corporate-action or adjustment identity is unsafe."""


class CorporateActionLifecycle(StrEnum):
    ANNOUNCED = "ANNOUNCED"
    EFFECTIVE = "EFFECTIVE"
    CANCELLED = "CANCELLED"


class CorporateActionComponent(StrEnum):
    AUTOMATIC_SHARE_CHANGE = "AUTOMATIC_SHARE_CHANGE"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"


class AdjustmentBasis(StrEnum):
    """Which economic components enter the price multiplier."""

    SHARE_CHANGE_ONLY = "SHARE_CHANGE_ONLY"
    TOTAL_RETURN = "TOTAL_RETURN"


class AdjustmentConvention(StrEnum):
    """Which side of the series is held at raw-price scale."""

    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"


_ADJUSTMENT_POLICY_VERSION = "corporate-action-adjustment-v1"
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}
_ZERO = Decimal(0)
_ONE = Decimal(1)


class _VisibleRecord(Protocol):
    verified: bool
    known_at: datetime
    usable_from: datetime


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CorporateActionContractError(f"{name} must be a boolean")
    return value


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise CorporateActionContractError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise CorporateActionContractError(f"{name} cannot contain surrounding whitespace")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise CorporateActionContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol")
    if symbol != symbol.upper():
        raise CorporateActionContractError("symbol must use canonical uppercase form")
    code, separator, suffix = symbol.rpartition(".")
    if not separator or not code or suffix not in _SYMBOL_SUFFIXES[market]:
        raise CorporateActionContractError("symbol suffix must match market")
    return symbol


def _require_visibility(known_at: datetime, usable_from: datetime) -> None:
    known = to_utc(known_at, "known_at")
    usable = to_utc(usable_from, "usable_from")
    if usable < known:
        raise CorporateActionContractError("usable_from cannot precede known_at")


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if type(value) is not Decimal:
        raise CorporateActionContractError(
            f"{name} must be Decimal; floats, integers and booleans are forbidden"
        )
    if not value.is_finite():
        raise CorporateActionContractError(f"{name} must be finite")
    if positive and value <= 0:
        raise CorporateActionContractError(f"{name} must be positive")
    if non_negative and value < 0:
        raise CorporateActionContractError(f"{name} cannot be negative")
    return value


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise CorporateActionContractError("adjustment denominator cannot be zero")
    with localcontext(_DECIMAL_CONTEXT):
        return +(numerator / denominator)


def _product(values: Iterable[Decimal]) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        result = _ONE
        for value in values:
            result *= value
        return +result


def _visible(
    record: _VisibleRecord,
    cutoff: datetime,
    require_verified: bool,
) -> bool:
    return (
        (record.verified or not require_verified)
        and to_utc(record.known_at) <= cutoff
        and to_utc(record.usable_from) <= cutoff
    )


@dataclass(frozen=True, slots=True)
class CorporateActionCoverage:
    """Per-instrument proof that an action date range was actually surveyed."""

    instrument_id: str
    market: Market
    start_date: date
    end_date: date
    source: str
    action_version: str
    known_at: datetime
    usable_from: datetime
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    complete: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.market, Market):
            raise CorporateActionContractError("market must be Market")
        if self.end_date < self.start_date:
            raise CorporateActionContractError("coverage end_date cannot precede start_date")
        _require_text(self.source, "source")
        _require_text(self.action_version, "action_version")
        _require_visibility(self.known_at, self.usable_from)
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise CorporateActionContractError("coverage revision cannot supersede itself")
        _require_bool(self.verified, "verified")
        _require_bool(self.complete, "complete")
        if (self.verified or self.complete) and not self.source_note:
            raise CorporateActionContractError(
                "verified or complete coverage requires a source note"
            )

    @property
    def coverage_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class CorporateActionFact:
    """One revision of one source-native corporate-action plan.

    ``automatic_share_ratio`` is the number of automatic post-action shares per
    old share.  It includes splits, reverse splits and stock dividends.  Rights
    entitlements are kept separate because they do not automatically increase a
    shareholder's position.
    """

    action_id: str
    instrument_id: str
    identity_fact_id: str
    symbol: str
    market: Market
    ex_date: date
    record_date: date | None
    payment_date: date | None
    share_listing_date: date | None
    lifecycle: CorporateActionLifecycle
    automatic_share_ratio: Decimal | None
    cash_dividend_per_share: Decimal | None
    rights_entitlement_ratio: Decimal | None
    rights_subscription_price: Decimal | None
    currency: str | None
    reference_price: Decimal | None
    reference_price_snapshot_id: str | None
    known_at: datetime
    usable_from: datetime
    source: str
    action_version: str
    revision: Revision
    supersedes_revision: Revision | None
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_text(self.action_id, "action_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        if not isinstance(self.market, Market):
            raise CorporateActionContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if not isinstance(self.lifecycle, CorporateActionLifecycle):
            raise CorporateActionContractError(
                "lifecycle must be CorporateActionLifecycle"
            )
        if self.payment_date is not None and self.payment_date < self.ex_date:
            raise CorporateActionContractError("payment_date cannot precede ex_date")
        if (
            self.share_listing_date is not None
            and self.share_listing_date < self.ex_date
        ):
            raise CorporateActionContractError(
                "share_listing_date cannot precede ex_date"
            )
        _require_visibility(self.known_at, self.usable_from)
        _require_text(self.source, "source")
        _require_text(self.action_version, "action_version")
        revision_key(self.revision)
        if self.supersedes_revision is not None:
            revision_key(self.supersedes_revision)
            if self.supersedes_revision == self.revision:
                raise CorporateActionContractError("action revision cannot supersede itself")
        _require_bool(self.verified, "verified")
        if self.verified and not self.source_note:
            raise CorporateActionContractError("verified action requires a source note")

        terms = (
            self.automatic_share_ratio,
            self.cash_dividend_per_share,
            self.rights_entitlement_ratio,
            self.rights_subscription_price,
            self.currency,
            self.reference_price,
            self.reference_price_snapshot_id,
        )
        if self.lifecycle is CorporateActionLifecycle.CANCELLED:
            if any(value is not None for value in terms):
                raise CorporateActionContractError(
                    "CANCELLED action revision cannot carry economic terms"
                )
            return

        automatic = _require_decimal(
            self.automatic_share_ratio,
            "automatic_share_ratio",
            positive=True,
        )
        cash = _require_decimal(
            self.cash_dividend_per_share,
            "cash_dividend_per_share",
            non_negative=True,
        )
        rights = _require_decimal(
            self.rights_entitlement_ratio,
            "rights_entitlement_ratio",
            non_negative=True,
        )
        if automatic == _ONE and cash == _ZERO and rights == _ZERO:
            raise CorporateActionContractError("corporate action terms cannot be a no-op")
        if automatic != _ONE and self.share_listing_date is None:
            raise CorporateActionContractError(
                "automatic share change requires share_listing_date"
            )

        if rights > 0:
            rights_price = _require_decimal(
                self.rights_subscription_price,
                "rights_subscription_price",
                positive=True,
            )
        elif self.rights_subscription_price is not None:
            raise CorporateActionContractError(
                "rights_subscription_price requires a positive rights ratio"
            )
        else:
            rights_price = None

        monetary = cash > 0 or rights > 0 or self.reference_price is not None
        if monetary:
            if type(self.currency) is not str or _CURRENCY.fullmatch(self.currency) is None:
                raise CorporateActionContractError(
                    "monetary action terms require an uppercase ISO-like currency"
                )
        elif self.currency is not None:
            raise CorporateActionContractError(
                "currency is forbidden when no monetary term is present"
            )

        if self.reference_price is not None:
            reference = _require_decimal(
                self.reference_price,
                "reference_price",
                positive=True,
            )
            _require_sha256(
                self.reference_price_snapshot_id,
                "reference_price_snapshot_id",
            )
            rights_value = rights * (rights_price or _ZERO)
            if reference - cash + rights_value <= 0:
                raise CorporateActionContractError(
                    "corporate action terms produce a non-positive theoretical value"
                )
        elif self.reference_price_snapshot_id is not None:
            raise CorporateActionContractError(
                "reference_price_snapshot_id requires reference_price"
            )

    @property
    def components(self) -> tuple[CorporateActionComponent, ...]:
        if self.lifecycle is CorporateActionLifecycle.CANCELLED:
            return ()
        assert self.automatic_share_ratio is not None
        assert self.cash_dividend_per_share is not None
        assert self.rights_entitlement_ratio is not None
        components: list[CorporateActionComponent] = []
        if self.automatic_share_ratio != _ONE:
            components.append(CorporateActionComponent.AUTOMATIC_SHARE_CHANGE)
        if self.cash_dividend_per_share > 0:
            components.append(CorporateActionComponent.CASH_DIVIDEND)
        if self.rights_entitlement_ratio > 0:
            components.append(CorporateActionComponent.RIGHTS_ISSUE)
        return tuple(components)

    @property
    def fact_id(self) -> str:
        return fingerprint(self)

    @property
    def automatic_position_multiplier(self) -> Decimal:
        if self.lifecycle is CorporateActionLifecycle.CANCELLED:
            raise CorporateActionContractError(
                "cancelled action has no automatic position multiplier"
            )
        assert self.automatic_share_ratio is not None
        return self.automatic_share_ratio

    def backward_price_multiplier(self, basis: AdjustmentBasis) -> Decimal:
        """Return the multiplier applied to prices before the ex-date."""

        if self.lifecycle is not CorporateActionLifecycle.EFFECTIVE:
            raise CorporateActionContractError(
                "only EFFECTIVE actions can produce adjustment factors"
            )
        if not isinstance(basis, AdjustmentBasis):
            raise CorporateActionContractError("basis must be AdjustmentBasis")
        assert self.automatic_share_ratio is not None
        assert self.cash_dividend_per_share is not None
        assert self.rights_entitlement_ratio is not None

        if basis is AdjustmentBasis.SHARE_CHANGE_ONLY:
            return _divide(_ONE, self.automatic_share_ratio)

        cash = self.cash_dividend_per_share
        rights = self.rights_entitlement_ratio
        if cash == 0 and rights == 0 and self.reference_price is None:
            return _divide(_ONE, self.automatic_share_ratio)
        if self.reference_price is None:
            raise CorporateActionContractError(
                "TOTAL_RETURN adjustment requires reference_price for cash or rights terms"
            )
        reference = self.reference_price
        rights_price = self.rights_subscription_price or _ZERO
        numerator = reference - cash + rights * rights_price
        denominator = reference * (self.automatic_share_ratio + rights)
        multiplier = _divide(numerator, denominator)
        if multiplier <= 0:
            raise CorporateActionContractError(
                "corporate action produced a non-positive price multiplier"
            )
        return multiplier


def _coverage_payload(value: CorporateActionCoverage) -> dict[str, object]:
    return {
        "instrument_id": value.instrument_id,
        "market": value.market,
        "start_date": value.start_date,
        "end_date": value.end_date,
        "source": value.source,
        "action_version": value.action_version,
        "usable_from": value.usable_from,
        "verified": value.verified,
        "complete": value.complete,
        "source_note": value.source_note,
    }


def _action_payload(value: CorporateActionFact) -> dict[str, object]:
    return {
        "action_id": value.action_id,
        "instrument_id": value.instrument_id,
        "identity_fact_id": value.identity_fact_id,
        "symbol": value.symbol,
        "market": value.market,
        "ex_date": value.ex_date,
        "record_date": value.record_date,
        "payment_date": value.payment_date,
        "share_listing_date": value.share_listing_date,
        "lifecycle": value.lifecycle,
        "automatic_share_ratio": value.automatic_share_ratio,
        "cash_dividend_per_share": value.cash_dividend_per_share,
        "rights_entitlement_ratio": value.rights_entitlement_ratio,
        "rights_subscription_price": value.rights_subscription_price,
        "currency": value.currency,
        "reference_price": value.reference_price,
        "reference_price_snapshot_id": value.reference_price_snapshot_id,
        "usable_from": value.usable_from,
        "source": value.source,
        "action_version": value.action_version,
        "verified": value.verified,
        "source_note": value.source_note,
    }


@dataclass(frozen=True, slots=True)
class CorporateActionSnapshot:
    instrument_id: str
    market: Market
    start_date: date
    end_date: date
    as_of: datetime
    coverage: CorporateActionCoverage
    actions: tuple[CorporateActionFact, ...]
    identities: tuple[InstrumentIdentityFact, ...]
    snapshot_id: str = field(init=False)
    require_verified: bool = True
    require_complete: bool = True

    def __post_init__(self) -> None:
        cutoff = to_utc(self.as_of, "as_of")
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.market, Market):
            raise CorporateActionContractError("market must be Market")
        if self.end_date < self.start_date:
            raise CorporateActionContractError("snapshot end_date cannot precede start_date")
        _require_bool(self.require_verified, "require_verified")
        _require_bool(self.require_complete, "require_complete")
        if self.coverage.instrument_id != self.instrument_id:
            raise CorporateActionContractError("coverage instrument_id mismatch")
        if self.coverage.market is not self.market:
            raise CorporateActionContractError("coverage market mismatch")
        if not (
            self.coverage.start_date <= self.start_date
            and self.coverage.end_date >= self.end_date
        ):
            raise CorporateActionContractError("coverage does not contain snapshot range")
        if to_utc(self.coverage.known_at) > cutoff or to_utc(self.coverage.usable_from) > cutoff:
            raise CorporateActionContractError("future coverage entered action snapshot")
        if self.require_verified and not self.coverage.verified:
            raise CorporateActionContractError(
                "verified action snapshot requires verified coverage"
            )
        if self.require_complete and not self.coverage.complete:
            raise CorporateActionContractError(
                "complete action snapshot requires complete coverage"
            )

        action_order = tuple(
            (item.ex_date, item.action_id, item.fact_id) for item in self.actions
        )
        if action_order != tuple(sorted(action_order)):
            raise CorporateActionContractError(
                "actions must be sorted by ex_date, action_id and fact_id"
            )
        action_ids = tuple(item.action_id for item in self.actions)
        if len(set(action_ids)) != len(action_ids):
            raise CorporateActionContractError("actions must be unique by action_id")
        identity_ids = tuple(item.fact_id for item in self.identities)
        if identity_ids != tuple(sorted(identity_ids)):
            raise CorporateActionContractError("identities must be sorted by fact_id")
        if len(set(identity_ids)) != len(identity_ids):
            raise CorporateActionContractError("identities must be unique by fact_id")
        identity_by_id = {item.fact_id: item for item in self.identities}

        effective_dates: set[date] = set()
        referenced_identity_ids: set[str] = set()
        for action in self.actions:
            if action.instrument_id != self.instrument_id or action.market is not self.market:
                raise CorporateActionContractError("action instrument identity mismatch")
            if not self.start_date <= action.ex_date <= self.end_date:
                raise CorporateActionContractError("action ex_date is outside snapshot range")
            if (
                action.source != self.coverage.source
                or action.action_version != self.coverage.action_version
            ):
                raise CorporateActionContractError(
                    "action source/version differs from coverage"
                )
            if to_utc(action.known_at) > cutoff or to_utc(action.usable_from) > cutoff:
                raise CorporateActionContractError("future action entered snapshot")
            if self.require_verified and not action.verified:
                raise CorporateActionContractError(
                    "unverified action entered verified snapshot"
                )
            if action.lifecycle is CorporateActionLifecycle.ANNOUNCED:
                raise CorporateActionContractError(
                    "past-range ANNOUNCED action is unresolved; EFFECTIVE or CANCELLED is required"
                )
            if action.lifecycle is CorporateActionLifecycle.EFFECTIVE:
                if action.ex_date in effective_dates:
                    raise CorporateActionContractError(
                        "multiple EFFECTIVE plans share one ex_date; normalize one combined plan"
                    )
                effective_dates.add(action.ex_date)

            identity = identity_by_id.get(action.identity_fact_id)
            if identity is None:
                raise CorporateActionContractError(
                    "action identity_fact_id is missing from snapshot identities"
                )
            referenced_identity_ids.add(identity.fact_id)
            if (
                identity.instrument_id != action.instrument_id
                or identity.market is not action.market
                or identity.symbol != action.symbol
                or not identity.active_on(action.ex_date)
            ):
                raise CorporateActionContractError(
                    "action is not bound to the identity active on its ex_date"
                )
            if to_utc(identity.known_at) > cutoff or to_utc(identity.usable_from) > cutoff:
                raise CorporateActionContractError("future identity entered action snapshot")
            if self.require_verified and not identity.verified:
                raise CorporateActionContractError(
                    "unverified identity entered verified action snapshot"
                )
        if referenced_identity_ids != set(identity_ids):
            raise CorporateActionContractError(
                "snapshot identities must exactly match action identity references"
            )

        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "corporate-action-snapshot-v1",
                    "instrument_id": self.instrument_id,
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": cutoff,
                    "coverage_id": self.coverage.coverage_id,
                    "action_fact_ids": [item.fact_id for item in self.actions],
                    "identity_fact_ids": list(identity_ids),
                    "require_verified": self.require_verified,
                    "require_complete": self.require_complete,
                }
            ),
        )

    @property
    def effective_actions(self) -> tuple[CorporateActionFact, ...]:
        return tuple(
            action
            for action in self.actions
            if action.lifecycle is CorporateActionLifecycle.EFFECTIVE
        )

    @property
    def cancelled_actions(self) -> tuple[CorporateActionFact, ...]:
        return tuple(
            action
            for action in self.actions
            if action.lifecycle is CorporateActionLifecycle.CANCELLED
        )


class CorporateActionBook:
    """Append-only coverage/actions with PIT snapshot selection."""

    def __init__(
        self,
        coverages: Iterable[CorporateActionCoverage],
        actions: Iterable[CorporateActionFact],
        identities: Iterable[InstrumentIdentityFact],
    ) -> None:
        self._coverages = tuple(coverages)
        self._actions = tuple(actions)
        self._identities = tuple(identities)

    def snapshot(
        self,
        instrument_id: str,
        market: Market,
        start_date: date,
        end_date: date,
        as_of: datetime,
        *,
        require_verified: bool = True,
        require_complete: bool = True,
    ) -> CorporateActionSnapshot:
        _require_text(instrument_id, "instrument_id")
        if not isinstance(market, Market):
            raise CorporateActionContractError("market must be Market")
        if end_date < start_date:
            raise CorporateActionContractError("end_date cannot precede start_date")
        _require_bool(require_verified, "require_verified")
        _require_bool(require_complete, "require_complete")
        cutoff = to_utc(as_of, "as_of")
        if end_date > exchange_local_date(as_of, market):
            raise CorporateActionContractError(
                "action snapshot cannot extend beyond the as_of exchange date"
            )

        coverage_groups: dict[
            tuple[str, str], list[CorporateActionCoverage]
        ] = defaultdict(list)
        for coverage in self._coverages:
            if coverage.instrument_id != instrument_id or coverage.market is not market:
                continue
            if not _visible(coverage, cutoff, require_verified):
                continue
            coverage_groups[(coverage.source, coverage.action_version)].append(coverage)
        selected_terminals: list[CorporateActionCoverage] = []
        for records in coverage_groups.values():
            try:
                selected_terminals.append(
                    select_superseding_revision(
                        records,
                        revision_of=lambda item: item.revision,
                        predecessor_of=lambda item: item.supersedes_revision,
                        payload_of=_coverage_payload,
                        identity_of=lambda item: item.coverage_id,
                        known_at_of=lambda item: item.known_at,
                    )
                )
            except PITConflictError as exc:
                raise CorporateActionContractError(str(exc)) from exc
        selected_coverages = [
            coverage
            for coverage in selected_terminals
            if coverage.start_date <= start_date
            and coverage.end_date >= end_date
            and (coverage.complete or not require_complete)
        ]
        if not selected_coverages:
            raise CorporateActionContractError(
                "no visible complete corporate-action coverage contains range"
            )
        if len(selected_coverages) != 1:
            raise CorporateActionContractError(
                "range overlaps multiple corporate-action sources/versions"
            )
        coverage = selected_coverages[0]

        action_groups: dict[str, list[CorporateActionFact]] = defaultdict(list)
        for action in self._actions:
            if action.instrument_id != instrument_id or action.market is not market:
                continue
            if action.source != coverage.source or action.action_version != coverage.action_version:
                continue
            if not _visible(action, cutoff, require_verified):
                continue
            action_groups[action.action_id].append(action)
        selected_terminals: list[CorporateActionFact] = []
        for records in action_groups.values():
            try:
                selected_terminals.append(
                    select_superseding_revision(
                        records,
                        revision_of=lambda item: item.revision,
                        predecessor_of=lambda item: item.supersedes_revision,
                        payload_of=_action_payload,
                        identity_of=lambda item: item.fact_id,
                        known_at_of=lambda item: item.known_at,
                    )
                )
            except PITConflictError as exc:
                raise CorporateActionContractError(str(exc)) from exc
        actions = tuple(
            sorted(
                (
                    action
                    for action in selected_terminals
                    if start_date <= action.ex_date <= end_date
                ),
                key=lambda item: (item.ex_date, item.action_id, item.fact_id),
            )
        )

        all_identities = {item.fact_id: item for item in self._identities}
        identities: list[InstrumentIdentityFact] = []
        for identity_id in sorted({item.identity_fact_id for item in actions}):
            identity = all_identities.get(identity_id)
            if identity is None or not _visible(identity, cutoff, require_verified):
                raise CorporateActionContractError(
                    "selected action references a missing, future or unverified identity"
                )
            identities.append(identity)
        identity_tuple = tuple(identities)
        return CorporateActionSnapshot(
            instrument_id=instrument_id,
            market=market,
            start_date=start_date,
            end_date=end_date,
            as_of=cutoff,
            coverage=coverage,
            actions=actions,
            identities=identity_tuple,
            require_verified=require_verified,
            require_complete=require_complete,
        )


@dataclass(frozen=True, slots=True)
class AdjustmentFactorPoint:
    action_id: str
    action_fact_id: str
    ex_date: date
    automatic_share_effective_date: date
    backward_price_multiplier: Decimal
    forward_price_multiplier: Decimal
    automatic_share_ratio: Decimal
    cash_dividend_per_share: Decimal
    rights_entitlement_ratio: Decimal
    rights_subscription_price: Decimal | None
    currency: str | None
    reference_price_snapshot_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.action_id, "action_id")
        _require_sha256(self.action_fact_id, "action_fact_id")
        if self.automatic_share_effective_date < self.ex_date:
            raise CorporateActionContractError(
                "automatic_share_effective_date cannot precede ex_date"
            )
        if self.reference_price_snapshot_id is not None:
            _require_sha256(
                self.reference_price_snapshot_id,
                "reference_price_snapshot_id",
            )
        backward = _require_decimal(
            self.backward_price_multiplier,
            "backward_price_multiplier",
            positive=True,
        )
        forward = _require_decimal(
            self.forward_price_multiplier,
            "forward_price_multiplier",
            positive=True,
        )
        if forward != _divide(_ONE, backward):
            raise CorporateActionContractError(
                "forward multiplier must be the deterministic reciprocal of backward multiplier"
            )
        _require_decimal(
            self.automatic_share_ratio,
            "automatic_share_ratio",
            positive=True,
        )
        _require_decimal(
            self.cash_dividend_per_share,
            "cash_dividend_per_share",
            non_negative=True,
        )
        rights = _require_decimal(
            self.rights_entitlement_ratio,
            "rights_entitlement_ratio",
            non_negative=True,
        )
        if rights > 0:
            _require_decimal(
                self.rights_subscription_price,
                "rights_subscription_price",
                positive=True,
            )
        elif self.rights_subscription_price is not None:
            raise CorporateActionContractError(
                "rights_subscription_price requires rights entitlement"
            )

    @property
    def factor_id(self) -> str:
        return fingerprint(self)


def _build_factor_points(
    snapshot: CorporateActionSnapshot,
    basis: AdjustmentBasis,
) -> tuple[AdjustmentFactorPoint, ...]:
    factors: list[AdjustmentFactorPoint] = []
    for action in snapshot.effective_actions:
        backward = action.backward_price_multiplier(basis)
        assert action.automatic_share_ratio is not None
        assert action.cash_dividend_per_share is not None
        assert action.rights_entitlement_ratio is not None
        if action.automatic_share_ratio != _ONE:
            assert action.share_listing_date is not None
            share_effective_date = action.share_listing_date
        else:
            share_effective_date = action.ex_date
        factors.append(
            AdjustmentFactorPoint(
                action_id=action.action_id,
                action_fact_id=action.fact_id,
                ex_date=action.ex_date,
                automatic_share_effective_date=share_effective_date,
                backward_price_multiplier=backward,
                forward_price_multiplier=_divide(_ONE, backward),
                automatic_share_ratio=action.automatic_share_ratio,
                cash_dividend_per_share=action.cash_dividend_per_share,
                rights_entitlement_ratio=action.rights_entitlement_ratio,
                rights_subscription_price=action.rights_subscription_price,
                currency=action.currency,
                reference_price_snapshot_id=action.reference_price_snapshot_id,
            )
        )
    return tuple(
        sorted(factors, key=lambda item: (item.ex_date, item.action_id, item.factor_id))
    )


@dataclass(frozen=True, slots=True)
class AdjustmentSeries:
    snapshot: CorporateActionSnapshot = field(repr=False)
    basis: AdjustmentBasis
    convention: AdjustmentConvention
    policy_version: str = _ADJUSTMENT_POLICY_VERSION
    instrument_id: str = field(init=False)
    market: Market = field(init=False)
    start_date: date = field(init=False)
    end_date: date = field(init=False)
    as_of: datetime = field(init=False)
    corporate_action_snapshot_id: str = field(init=False)
    base_date: date = field(init=False)
    factors: tuple[AdjustmentFactorPoint, ...] = field(init=False)
    series_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, CorporateActionSnapshot):
            raise CorporateActionContractError(
                "snapshot must be CorporateActionSnapshot"
            )
        if not self.snapshot.require_verified or not self.snapshot.require_complete:
            raise CorporateActionContractError(
                "formal adjustment series requires verified and complete action snapshot"
            )
        if not isinstance(self.basis, AdjustmentBasis):
            raise CorporateActionContractError("basis must be AdjustmentBasis")
        if not isinstance(self.convention, AdjustmentConvention):
            raise CorporateActionContractError(
                "convention must be AdjustmentConvention"
            )
        _require_text(self.policy_version, "policy_version")

        cutoff = to_utc(self.snapshot.as_of, "as_of")
        base_date = (
            self.snapshot.end_date
            if self.convention is AdjustmentConvention.BACKWARD
            else self.snapshot.start_date
        )
        factors = _build_factor_points(self.snapshot, self.basis)
        for name, value in (
            ("instrument_id", self.snapshot.instrument_id),
            ("market", self.snapshot.market),
            ("start_date", self.snapshot.start_date),
            ("end_date", self.snapshot.end_date),
            ("as_of", cutoff),
            ("corporate_action_snapshot_id", self.snapshot.snapshot_id),
            ("base_date", base_date),
            ("factors", factors),
        ):
            object.__setattr__(self, name, value)

        object.__setattr__(
            self,
            "series_id",
            fingerprint(
                {
                    "schema": "adjustment-series-v1",
                    "instrument_id": self.instrument_id,
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": cutoff,
                    "corporate_action_snapshot_id": self.corporate_action_snapshot_id,
                    "basis": self.basis,
                    "convention": self.convention,
                    "base_date": self.base_date,
                    "factor_ids": [item.factor_id for item in self.factors],
                    "policy_version": self.policy_version,
                }
            ),
        )

    def _require_date(self, session_date: date) -> None:
        if not self.start_date <= session_date <= self.end_date:
            raise CorporateActionContractError(
                "session_date is outside adjustment series range"
            )

    def price_multiplier_for(self, session_date: date) -> Decimal:
        self._require_date(session_date)
        if self.convention is AdjustmentConvention.BACKWARD:
            return _product(
                item.backward_price_multiplier
                for item in self.factors
                if session_date < item.ex_date <= self.base_date
            )
        return _product(
            item.forward_price_multiplier
            for item in self.factors
            if self.base_date < item.ex_date <= session_date
        )

    def automatic_share_multiplier_for(self, session_date: date) -> Decimal:
        """Return split/bonus-share normalization; rights are never automatic."""

        self._require_date(session_date)
        if self.convention is AdjustmentConvention.BACKWARD:
            return _product(
                item.automatic_share_ratio
                for item in self.factors
                if session_date < item.automatic_share_effective_date <= self.base_date
            )
        return _product(
            _divide(_ONE, item.automatic_share_ratio)
            for item in self.factors
            if self.base_date < item.automatic_share_effective_date <= session_date
        )

    def adjust_price(self, raw_price: Decimal, session_date: date) -> Decimal:
        price = _require_decimal(raw_price, "raw_price", positive=True)
        with localcontext(_DECIMAL_CONTEXT):
            return +(price * self.price_multiplier_for(session_date))

    def adjust_automatic_shares(
        self,
        raw_share_quantity: Decimal,
        session_date: date,
    ) -> Decimal:
        quantity = _require_decimal(
            raw_share_quantity,
            "raw_share_quantity",
            non_negative=True,
        )
        with localcontext(_DECIMAL_CONTEXT):
            return +(quantity * self.automatic_share_multiplier_for(session_date))


@dataclass(frozen=True, slots=True)
class AdjustedMarketDataView:
    """Identity binding for a future adjusted-bar view.

    This contract does not contain or mutate bars. It proves only which raw-bar,
    Calendar and corporate-action snapshots plus adjustment policy would be used
    to construct an adjusted view.
    """

    series: AdjustmentSeries = field(repr=False)
    raw_bar_snapshot_id: str
    calendar_snapshot_id: str
    instrument_id: str = field(init=False)
    market: Market = field(init=False)
    start_date: date = field(init=False)
    end_date: date = field(init=False)
    as_of: datetime = field(init=False)
    corporate_action_snapshot_id: str = field(init=False)
    adjustment_series_id: str = field(init=False)
    basis: AdjustmentBasis = field(init=False)
    convention: AdjustmentConvention = field(init=False)
    policy_version: str = field(init=False)
    view_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.series, AdjustmentSeries):
            raise CorporateActionContractError(
                "series must be AdjustmentSeries"
            )
        _require_sha256(self.raw_bar_snapshot_id, "raw_bar_snapshot_id")
        _require_sha256(self.calendar_snapshot_id, "calendar_snapshot_id")
        for name, value in (
            ("instrument_id", self.series.instrument_id),
            ("market", self.series.market),
            ("start_date", self.series.start_date),
            ("end_date", self.series.end_date),
            ("as_of", to_utc(self.series.as_of)),
            (
                "corporate_action_snapshot_id",
                self.series.corporate_action_snapshot_id,
            ),
            ("adjustment_series_id", self.series.series_id),
            ("basis", self.series.basis),
            ("convention", self.series.convention),
            ("policy_version", self.series.policy_version),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "view_id",
            fingerprint(
                {
                    "schema": "adjusted-market-data-view-v1",
                    "instrument_id": self.instrument_id,
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": self.as_of,
                    "raw_bar_snapshot_id": self.raw_bar_snapshot_id,
                    "calendar_snapshot_id": self.calendar_snapshot_id,
                    "corporate_action_snapshot_id": (
                        self.corporate_action_snapshot_id
                    ),
                    "adjustment_series_id": self.adjustment_series_id,
                    "basis": self.basis,
                    "convention": self.convention,
                    "policy_version": self.policy_version,
                }
            ),
        )


def bind_adjusted_market_data_view(
    series: AdjustmentSeries,
    *,
    raw_bar_snapshot_id: str,
    calendar_snapshot_id: str,
) -> AdjustedMarketDataView:
    """Bind a factor series to immutable raw-bar and Calendar snapshot IDs."""

    return AdjustedMarketDataView(
        series=series,
        raw_bar_snapshot_id=raw_bar_snapshot_id,
        calendar_snapshot_id=calendar_snapshot_id,
    )


def build_adjustment_series(
    snapshot: CorporateActionSnapshot,
    *,
    basis: AdjustmentBasis,
    convention: AdjustmentConvention,
) -> AdjustmentSeries:
    """Build a deterministic factor view without modifying raw bars."""

    return AdjustmentSeries(
        snapshot=snapshot,
        basis=basis,
        convention=convention,
    )


__all__ = [
    "AdjustedMarketDataView",
    "AdjustmentBasis",
    "AdjustmentConvention",
    "AdjustmentFactorPoint",
    "AdjustmentSeries",
    "CorporateActionBook",
    "CorporateActionComponent",
    "CorporateActionContractError",
    "CorporateActionCoverage",
    "CorporateActionFact",
    "CorporateActionLifecycle",
    "CorporateActionSnapshot",
    "bind_adjusted_market_data_view",
    "build_adjustment_series",
]
