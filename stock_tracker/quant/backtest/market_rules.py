"""Versioned, date-effective market and instrument execution rules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint


class RuleContractError(ValueError):
    """Raised when a rule set is ambiguous, unverified or internally invalid."""


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RuleContractError(f"{name} must be a boolean")
    return value


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class MarketRule:
    """One immutable market-rule version for an inclusive date interval."""

    rule_id: str
    market: Market
    effective_from: date
    effective_to: date | None
    currency: str
    lot_size: int
    settlement_days: int
    sell_t_plus_one: bool
    price_limit_state_required: bool
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        for name in (
            "sell_t_plus_one",
            "price_limit_state_required",
            "verified",
        ):
            _require_bool(getattr(self, name), name)
        if not self.rule_id or not self.currency:
            raise RuleContractError("rule_id and currency must be non-empty")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise RuleContractError("effective_to cannot precede effective_from")
        if self.lot_size <= 0:
            raise RuleContractError("lot_size must be positive")
        if self.settlement_days < 0:
            raise RuleContractError("settlement_days cannot be negative")
        if self.verified and not self.source_note:
            raise RuleContractError("verified market rules require a source note")

    def applies(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date <= self.effective_to
        )

    @property
    def rule_hash(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class InstrumentRule:
    """Date-effective overrides and restrictions for one instrument."""

    rule_id: str
    symbol: str
    market: Market
    effective_from: date
    effective_to: date | None
    lot_size: int | None
    risk_warning: bool
    newly_listed: bool
    price_limit_up: float | None
    price_limit_down: float | None
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        for name in ("risk_warning", "newly_listed", "verified"):
            _require_bool(getattr(self, name), name)
        if not self.rule_id or not self.symbol:
            raise RuleContractError("instrument rule requires rule_id and symbol")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise RuleContractError("effective_to cannot precede effective_from")
        if self.lot_size is not None and self.lot_size <= 0:
            raise RuleContractError("instrument lot_size must be positive")
        if self.price_limit_up is not None and self.price_limit_up <= 0:
            raise RuleContractError("price_limit_up must be positive")
        if self.price_limit_down is not None and self.price_limit_down <= 0:
            raise RuleContractError("price_limit_down must be positive")
        if (
            self.price_limit_up is not None
            and self.price_limit_down is not None
            and self.price_limit_down >= self.price_limit_up
        ):
            raise RuleContractError("price_limit_down must be below price_limit_up")
        if self.verified and not self.source_note:
            raise RuleContractError("verified instrument rules require a source note")

    def applies(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date <= self.effective_to
        )

    @property
    def rule_hash(self) -> str:
        return fingerprint(self)


def _intervals_overlap(
    left_start: date,
    left_end: date | None,
    right_start: date,
    right_end: date | None,
) -> bool:
    left_max = left_end or date.max
    right_max = right_end or date.max
    return left_start <= right_max and right_start <= left_max


class MarketRuleBook:
    """Fail-closed selection of exactly one rule version per date."""

    def __init__(
        self,
        market_rules: Iterable[MarketRule],
        instrument_rules: Iterable[InstrumentRule] = (),
    ) -> None:
        self.market_rules = tuple(market_rules)
        self.instrument_rules = tuple(instrument_rules)
        self._validate_no_overlap()

    def _validate_no_overlap(self) -> None:
        for index, left in enumerate(self.market_rules):
            for right in self.market_rules[index + 1 :]:
                if left.market is right.market and _intervals_overlap(
                    left.effective_from,
                    left.effective_to,
                    right.effective_from,
                    right.effective_to,
                ):
                    raise RuleContractError(
                        f"overlapping market rules for {left.market}: "
                        f"{left.rule_id}, {right.rule_id}"
                    )
        for index, left in enumerate(self.instrument_rules):
            for right in self.instrument_rules[index + 1 :]:
                if (
                    left.symbol == right.symbol
                    and left.market is right.market
                    and _intervals_overlap(
                        left.effective_from,
                        left.effective_to,
                        right.effective_from,
                        right.effective_to,
                    )
                ):
                    raise RuleContractError(
                        f"overlapping instrument rules for {left.symbol}: "
                        f"{left.rule_id}, {right.rule_id}"
                    )

    def market_rule(
        self,
        market: Market,
        session_date: date,
        *,
        require_verified: bool = True,
    ) -> MarketRule:
        _require_bool(require_verified, "require_verified")
        candidates = [
            rule
            for rule in self.market_rules
            if rule.market is market
            and rule.applies(session_date)
            and (rule.verified or not require_verified)
        ]
        if len(candidates) != 1:
            raise RuleContractError(
                f"expected exactly one market rule for {market} on {session_date}, "
                f"found {len(candidates)}"
            )
        return candidates[0]

    def instrument_rule(
        self,
        symbol: str,
        market: Market,
        session_date: date,
        *,
        require_verified: bool = True,
    ) -> InstrumentRule | None:
        _require_bool(require_verified, "require_verified")
        candidates = [
            rule
            for rule in self.instrument_rules
            if rule.symbol == symbol
            and rule.market is market
            and rule.applies(session_date)
            and (rule.verified or not require_verified)
        ]
        if len(candidates) > 1:
            raise RuleContractError(
                f"ambiguous instrument rules for {symbol} on {session_date}"
            )
        return candidates[0] if candidates else None

    @property
    def rulebook_hash(self) -> str:
        return fingerprint(
            {
                "schema": "market-rulebook-v1",
                "market_rules": sorted(rule.rule_hash for rule in self.market_rules),
                "instrument_rules": sorted(
                    rule.rule_hash for rule in self.instrument_rules
                ),
            }
        )
