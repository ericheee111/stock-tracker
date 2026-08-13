"""Date-effective transaction-cost schedules and deterministic estimates."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from .market_rules import (
    RuleContractError,
    TradeSide,
    _intervals_overlap,
    _require_bool,
)


@dataclass(frozen=True, slots=True)
class CostSchedule:
    """Immutable fee, spread, slippage and impact assumptions."""

    schedule_id: str
    market: Market
    effective_from: date
    effective_to: date | None
    commission_bps: float
    minimum_commission: float
    sell_tax_bps: float
    exchange_fee_bps: float
    transfer_fee_bps: float
    half_spread_bps: float
    slippage_bps: float
    impact_coefficient: float
    max_participation_rate: float
    verified: bool
    source_note: str

    def __post_init__(self) -> None:
        _require_bool(self.verified, "verified")
        if not self.schedule_id:
            raise RuleContractError("schedule_id must be non-empty")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise RuleContractError("effective_to cannot precede effective_from")
        numeric = (
            self.commission_bps,
            self.minimum_commission,
            self.sell_tax_bps,
            self.exchange_fee_bps,
            self.transfer_fee_bps,
            self.half_spread_bps,
            self.slippage_bps,
            self.impact_coefficient,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise RuleContractError("cost parameters must be finite and non-negative")
        if not 0 < self.max_participation_rate <= 1:
            raise RuleContractError("max_participation_rate must be in (0, 1]")
        if self.verified and not self.source_note:
            raise RuleContractError("verified cost schedules require a source note")

    def applies(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date <= self.effective_to
        )

    @property
    def schedule_hash(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    notional: float
    commission: float
    tax: float
    exchange_fee: float
    transfer_fee: float
    spread_cost: float
    slippage_cost: float
    impact_cost: float
    total: float

    def __post_init__(self) -> None:
        values = (
            self.notional,
            self.commission,
            self.tax,
            self.exchange_fee,
            self.transfer_fee,
            self.spread_cost,
            self.slippage_cost,
            self.impact_cost,
            self.total,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise RuleContractError("cost breakdown must be finite and non-negative")

    @property
    def effective_bps(self) -> float:
        return 0.0 if self.notional == 0 else self.total / self.notional * 10_000


class CostScheduleBook:
    """Select one verified cost schedule for a market and trading date."""

    def __init__(self, schedules: Iterable[CostSchedule]) -> None:
        self.schedules = tuple(schedules)
        for index, left in enumerate(self.schedules):
            for right in self.schedules[index + 1 :]:
                if left.market is right.market and _intervals_overlap(
                    left.effective_from,
                    left.effective_to,
                    right.effective_from,
                    right.effective_to,
                ):
                    raise RuleContractError(
                        f"overlapping cost schedules for {left.market}: "
                        f"{left.schedule_id}, {right.schedule_id}"
                    )

    def select(
        self,
        market: Market,
        session_date: date,
        *,
        require_verified: bool = True,
    ) -> CostSchedule:
        _require_bool(require_verified, "require_verified")
        candidates = [
            schedule
            for schedule in self.schedules
            if schedule.market is market
            and schedule.applies(session_date)
            and (schedule.verified or not require_verified)
        ]
        if len(candidates) != 1:
            raise RuleContractError(
                f"expected one cost schedule for {market} on {session_date}, "
                f"found {len(candidates)}"
            )
        return candidates[0]

    @property
    def schedule_book_hash(self) -> str:
        return fingerprint(
            {
                "schema": "cost-schedule-book-v1",
                "schedules": sorted(
                    schedule.schedule_hash for schedule in self.schedules
                ),
            }
        )


def estimate_costs(
    schedule: CostSchedule,
    *,
    side: TradeSide,
    price: float,
    quantity: int,
    bar_volume: int,
) -> CostBreakdown:
    """Estimate costs without hiding spread/slippage inside the fill price."""

    if not math.isfinite(price) or price <= 0:
        raise RuleContractError("price must be finite and positive")
    if quantity <= 0:
        raise RuleContractError("quantity must be positive")
    if bar_volume <= 0:
        raise RuleContractError("bar_volume must be positive")
    participation = quantity / bar_volume
    if participation > schedule.max_participation_rate + 1e-12:
        raise RuleContractError("quantity exceeds configured participation limit")
    notional = price * quantity
    commission = max(
        schedule.minimum_commission,
        notional * schedule.commission_bps / 10_000,
    )
    tax = notional * schedule.sell_tax_bps / 10_000 if side is TradeSide.SELL else 0.0
    exchange_fee = notional * schedule.exchange_fee_bps / 10_000
    transfer_fee = notional * schedule.transfer_fee_bps / 10_000
    spread_cost = notional * schedule.half_spread_bps / 10_000
    slippage_cost = notional * schedule.slippage_bps / 10_000
    impact_bps = schedule.impact_coefficient * math.sqrt(participation) * 10_000
    impact_cost = notional * impact_bps / 10_000
    total = (
        commission
        + tax
        + exchange_fee
        + transfer_fee
        + spread_cost
        + slippage_cost
        + impact_cost
    )
    return CostBreakdown(
        notional=notional,
        commission=commission,
        tax=tax,
        exchange_fee=exchange_fee,
        transfer_fee=transfer_fee,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        impact_cost=impact_cost,
        total=total,
    )
