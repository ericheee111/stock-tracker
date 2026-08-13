"""Execution-aware fills with halts, limits, T+1 and bounded impact."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from stock_tracker.core.types import Bar, Market

from ..core.calendar import InstrumentSessionState
from ..core.time import exchange_local_date
from .costs import CostBreakdown, CostScheduleBook, estimate_costs
from .market_rules import MarketRuleBook, TradeSide


class ExecutionContractError(RuntimeError):
    """Raised when executable-price semantics are missing or ambiguous."""


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ExecutionContractError(f"{name} must be a boolean")
    return value


def _require_optional_bool(value: object, name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ExecutionContractError(f"{name} must be a boolean or None")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionBar:
    """A Bar plus the execution state that OHLC alone cannot safely imply."""

    bar: Bar
    state: InstrumentSessionState = InstrumentSessionState.OPEN
    locked_limit_up: bool | None = None
    locked_limit_down: bool | None = None

    def __post_init__(self) -> None:
        _require_optional_bool(self.locked_limit_up, "locked_limit_up")
        _require_optional_bool(self.locked_limit_down, "locked_limit_down")
        values = (self.bar.open, self.bar.high, self.bar.low, self.bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ExecutionContractError("bar OHLC must be finite and positive")
        if self.bar.low > min(self.bar.open, self.bar.close, self.bar.high):
            raise ExecutionContractError("bar low is inconsistent with OHLC")
        if self.bar.high < max(self.bar.open, self.bar.close, self.bar.low):
            raise ExecutionContractError("bar high is inconsistent with OHLC")
        if self.state is InstrumentSessionState.OPEN:
            if self.bar.volume <= 0:
                raise ExecutionContractError("OPEN execution bar requires positive volume")
        elif self.bar.volume != 0:
            raise ExecutionContractError("non-OPEN execution bar must have zero volume")

    @property
    def session_date(self) -> date:
        return exchange_local_date(self.bar.timestamp, self.bar.market)

    @property
    def observable(self) -> bool:
        return self.state is InstrumentSessionState.OPEN and self.bar.volume > 0


@dataclass(frozen=True, slots=True)
class Fill:
    side: TradeSide
    symbol: str
    market: Market
    session_index: int
    session_date: date
    timestamp: object
    quantity: int
    reference_price: float
    price: float
    costs: CostBreakdown
    rule_id: str
    cost_schedule_id: str

    @property
    def all_in_unit_price(self) -> float:
        sign = 1.0 if self.side is TradeSide.BUY else -1.0
        explicit = (
            self.costs.commission
            + self.costs.tax
            + self.costs.exchange_fee
            + self.costs.transfer_fee
        ) / self.quantity
        return self.price + sign * explicit


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    executable: bool
    reason: str


class ExecutionEngine:
    """Find deterministic next executable fills under versioned assumptions."""

    def __init__(
        self,
        rules: MarketRuleBook,
        costs: CostScheduleBook,
        *,
        require_verified: bool = True,
    ) -> None:
        _require_bool(require_verified, "require_verified")
        self.rules = rules
        self.costs = costs
        self.require_verified = require_verified

    def decision(
        self,
        execution_bar: ExecutionBar,
        side: TradeSide,
        *,
        acquired_session_index: int | None,
        current_session_index: int,
    ) -> ExecutionDecision:
        bar = execution_bar.bar
        market_rule = self.rules.market_rule(
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        if execution_bar.state is not InstrumentSessionState.OPEN:
            return ExecutionDecision(False, f"SESSION_{execution_bar.state}")
        if bar.volume <= 0:
            return ExecutionDecision(False, "NO_VOLUME")
        if market_rule.sell_t_plus_one and side is TradeSide.SELL:
            if acquired_session_index is None:
                return ExecutionDecision(False, "MISSING_ACQUISITION_SESSION")
            if current_session_index <= acquired_session_index:
                return ExecutionDecision(False, "T_PLUS_ONE")
        if market_rule.price_limit_state_required and (
            execution_bar.locked_limit_up is None
            or execution_bar.locked_limit_down is None
        ):
            return ExecutionDecision(False, "UNKNOWN_PRICE_LIMIT_STATE")
        if side is TradeSide.BUY and execution_bar.locked_limit_up is True:
            return ExecutionDecision(False, "LOCKED_LIMIT_UP")
        if side is TradeSide.SELL and execution_bar.locked_limit_down is True:
            return ExecutionDecision(False, "LOCKED_LIMIT_DOWN")
        return ExecutionDecision(True, "EXECUTABLE")

    def _rounded_quantity(
        self,
        execution_bar: ExecutionBar,
        requested_quantity: int,
    ) -> int:
        if requested_quantity <= 0:
            raise ExecutionContractError("requested_quantity must be positive")
        bar = execution_bar.bar
        market_rule = self.rules.market_rule(
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        instrument_rule = self.rules.instrument_rule(
            bar.symbol,
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        lot_size = (
            instrument_rule.lot_size
            if instrument_rule is not None and instrument_rule.lot_size is not None
            else market_rule.lot_size
        )
        rounded = requested_quantity // lot_size * lot_size
        if rounded <= 0:
            raise ExecutionContractError("quantity is smaller than one tradable lot")
        schedule = self.costs.select(
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        maximum = math.floor(bar.volume * schedule.max_participation_rate)
        maximum = maximum // lot_size * lot_size
        if maximum <= 0:
            raise ExecutionContractError("participation limit allows no tradable lot")
        return min(rounded, maximum)

    def fill_at(
        self,
        bars: Sequence[ExecutionBar],
        index: int,
        *,
        side: TradeSide,
        requested_quantity: int,
        acquired_session_index: int | None = None,
        reference_price: float | None = None,
    ) -> Fill:
        if not 0 <= index < len(bars):
            raise IndexError("execution index out of range")
        execution_bar = bars[index]
        decision = self.decision(
            execution_bar,
            side,
            acquired_session_index=acquired_session_index,
            current_session_index=index,
        )
        if not decision.executable:
            raise ExecutionContractError(decision.reason)
        bar = execution_bar.bar
        base = bar.open if reference_price is None else reference_price
        if not math.isfinite(base) or base <= 0:
            raise ExecutionContractError("reference price must be finite and positive")
        if not bar.low <= base <= bar.high:
            raise ExecutionContractError("reference price must lie inside observed OHLC")
        quantity = self._rounded_quantity(execution_bar, requested_quantity)
        schedule = self.costs.select(
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        estimated_costs = estimate_costs(
            schedule,
            side=side,
            price=base,
            quantity=quantity,
            bar_volume=bar.volume,
        )
        estimated_implicit = (
            estimated_costs.spread_cost
            + estimated_costs.slippage_cost
            + estimated_costs.impact_cost
        )
        direction = 1.0 if side is TradeSide.BUY else -1.0
        modeled = base + direction * estimated_implicit / quantity
        fill_price = min(bar.high, max(bar.low, modeled))
        realized_implicit = abs(fill_price - base) * quantity
        scale = (
            min(1.0, realized_implicit / estimated_implicit)
            if estimated_implicit > 0
            else 0.0
        )
        spread_cost = estimated_costs.spread_cost * scale
        slippage_cost = estimated_costs.slippage_cost * scale
        impact_cost = estimated_costs.impact_cost * scale
        costs = CostBreakdown(
            notional=estimated_costs.notional,
            commission=estimated_costs.commission,
            tax=estimated_costs.tax,
            exchange_fee=estimated_costs.exchange_fee,
            transfer_fee=estimated_costs.transfer_fee,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            impact_cost=impact_cost,
            total=(
                estimated_costs.commission
                + estimated_costs.tax
                + estimated_costs.exchange_fee
                + estimated_costs.transfer_fee
                + spread_cost
                + slippage_cost
                + impact_cost
            ),
        )
        market_rule = self.rules.market_rule(
            bar.market,
            execution_bar.session_date,
            require_verified=self.require_verified,
        )
        return Fill(
            side=side,
            symbol=bar.symbol,
            market=bar.market,
            session_index=index,
            session_date=execution_bar.session_date,
            timestamp=bar.timestamp,
            quantity=quantity,
            reference_price=base,
            price=fill_price,
            costs=costs,
            rule_id=market_rule.rule_id,
            cost_schedule_id=schedule.schedule_id,
        )

    def next_fill(
        self,
        bars: Sequence[ExecutionBar],
        *,
        start_index: int,
        side: TradeSide,
        requested_quantity: int,
        acquired_session_index: int | None = None,
        end_index_exclusive: int | None = None,
    ) -> Fill:
        """Return the first executable fill without skipping unknown-state failures."""

        if start_index < 0:
            raise ExecutionContractError("start_index cannot be negative")
        end = len(bars) if end_index_exclusive is None else min(
            len(bars), end_index_exclusive
        )
        blocked: list[str] = []
        for index in range(start_index, end):
            decision = self.decision(
                bars[index],
                side,
                acquired_session_index=acquired_session_index,
                current_session_index=index,
            )
            if decision.reason == "UNKNOWN_PRICE_LIMIT_STATE":
                raise ExecutionContractError(decision.reason)
            if not decision.executable:
                blocked.append(f"{index}:{decision.reason}")
                continue
            return self.fill_at(
                bars,
                index,
                side=side,
                requested_quantity=requested_quantity,
                acquired_session_index=acquired_session_index,
            )
        detail = ", ".join(blocked) or "no sessions inspected"
        raise ExecutionContractError(f"NO_EXECUTABLE_SESSION ({detail})")


def next_executable_price(
    engine: ExecutionEngine,
    bars: Sequence[ExecutionBar],
    *,
    start_index: int,
    side: TradeSide,
    requested_quantity: int,
    acquired_session_index: int | None = None,
    end_index_exclusive: int | None = None,
) -> float:
    """Convenience wrapper used by labels and backtests."""

    return engine.next_fill(
        bars,
        start_index=start_index,
        side=side,
        requested_quantity=requested_quantity,
        acquired_session_index=acquired_session_index,
        end_index_exclusive=end_index_exclusive,
    ).price
