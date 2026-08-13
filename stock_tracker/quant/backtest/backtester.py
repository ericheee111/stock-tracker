"""Sequential cash/position ledger using the shared execution engine."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .execution import ExecutionBar, ExecutionContractError, ExecutionEngine, Fill
from .market_rules import TradeSide


class BacktestContractError(RuntimeError):
    """Raised when an order violates portfolio or execution assumptions."""


class LedgerEventType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    REJECT = "REJECT"
    MARK = "MARK"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: str
    position_id: str
    side: TradeSide
    start_index: int
    quantity: int
    end_index_exclusive: int | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.position_id:
            raise BacktestContractError("order_id and position_id must be non-empty")
        if self.start_index < 0 or self.quantity <= 0:
            raise BacktestContractError("order index/quantity are invalid")


@dataclass(frozen=True, slots=True)
class PositionLot:
    position_id: str
    symbol: str
    quantity: int
    entry_fill: Fill


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_type: LedgerEventType
    order_id: str
    position_id: str
    session_index: int | None
    cash_after: float
    quantity: int
    price: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    position_id: str
    entry: Fill
    exit: Fill
    quantity: int
    gross_return: float
    net_return: float
    pnl: float
    holding_sessions: int
    total_cost: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_cash: float
    final_cash: float
    final_equity: float
    events: tuple[LedgerEvent, ...]
    closed_trades: tuple[ClosedTrade, ...]
    open_positions: tuple[PositionLot, ...]
    rejected_orders: int

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_cash - 1


class ExecutionBacktester:
    """Execute chronological order intents without hidden same-bar fills."""

    def __init__(self, engine: ExecutionEngine, *, initial_cash: float) -> None:
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise BacktestContractError("initial_cash must be finite and positive")
        self.engine = engine
        self.initial_cash = initial_cash

    def run(
        self,
        bars: Sequence[ExecutionBar],
        intents: Sequence[OrderIntent],
    ) -> BacktestResult:
        if not bars:
            raise BacktestContractError("bars cannot be empty")
        symbols = {execution_bar.bar.symbol for execution_bar in bars}
        markets = {execution_bar.bar.market for execution_bar in bars}
        if len(symbols) != 1 or len(markets) != 1:
            raise BacktestContractError(
                "this ledger version requires one symbol and one market per run"
            )
        sorted_intents = tuple(
            sorted(intents, key=lambda intent: (intent.start_index, intent.order_id))
        )
        if len({intent.order_id for intent in sorted_intents}) != len(sorted_intents):
            raise BacktestContractError("order_id must be unique")
        cash = self.initial_cash
        positions: dict[str, PositionLot] = {}
        events: list[LedgerEvent] = []
        trades: list[ClosedTrade] = []
        rejected = 0
        last_fill_index: int | None = None
        for intent in sorted_intents:
            try:
                if intent.side is TradeSide.BUY:
                    if intent.position_id in positions:
                        raise BacktestContractError("position_id is already open")
                    fill = self.engine.next_fill(
                        bars,
                        start_index=intent.start_index,
                        side=TradeSide.BUY,
                        requested_quantity=intent.quantity,
                        end_index_exclusive=intent.end_index_exclusive,
                    )
                    if (
                        last_fill_index is not None
                        and fill.session_index < last_fill_index
                    ):
                        raise BacktestContractError("NON_MONOTONIC_FILL_TIME")
                    required = fill.all_in_unit_price * fill.quantity
                    if required > cash + 1e-9:
                        raise BacktestContractError("INSUFFICIENT_CASH")
                    cash -= required
                    positions[intent.position_id] = PositionLot(
                        position_id=intent.position_id,
                        symbol=fill.symbol,
                        quantity=fill.quantity,
                        entry_fill=fill,
                    )
                    events.append(
                        LedgerEvent(
                            LedgerEventType.BUY,
                            intent.order_id,
                            intent.position_id,
                            fill.session_index,
                            cash,
                            fill.quantity,
                            fill.price,
                            "FILLED",
                        )
                    )
                    last_fill_index = fill.session_index
                else:
                    position = positions.get(intent.position_id)
                    if position is None:
                        raise BacktestContractError("POSITION_NOT_OPEN")
                    if intent.quantity != position.quantity:
                        raise BacktestContractError(
                            "SELL_QUANTITY_MUST_EQUAL_POSITION"
                        )
                    fill = self.engine.next_fill(
                        bars,
                        start_index=intent.start_index,
                        side=TradeSide.SELL,
                        requested_quantity=position.quantity,
                        acquired_session_index=position.entry_fill.session_index,
                        end_index_exclusive=intent.end_index_exclusive,
                    )
                    if (
                        last_fill_index is not None
                        and fill.session_index < last_fill_index
                    ):
                        raise BacktestContractError("NON_MONOTONIC_FILL_TIME")
                    proceeds = fill.all_in_unit_price * fill.quantity
                    cash += proceeds
                    entry_outlay = (
                        position.entry_fill.all_in_unit_price * position.quantity
                    )
                    pnl = proceeds - entry_outlay
                    gross_return = fill.price / position.entry_fill.price - 1
                    net_return = pnl / entry_outlay
                    trades.append(
                        ClosedTrade(
                            position_id=intent.position_id,
                            entry=position.entry_fill,
                            exit=fill,
                            quantity=position.quantity,
                            gross_return=gross_return,
                            net_return=net_return,
                            pnl=pnl,
                            holding_sessions=(
                                fill.session_index - position.entry_fill.session_index
                            ),
                            total_cost=(
                                position.entry_fill.costs.total + fill.costs.total
                            ),
                        )
                    )
                    del positions[intent.position_id]
                    events.append(
                        LedgerEvent(
                            LedgerEventType.SELL,
                            intent.order_id,
                            intent.position_id,
                            fill.session_index,
                            cash,
                            fill.quantity,
                            fill.price,
                            "FILLED",
                        )
                    )
                    last_fill_index = fill.session_index
            except (ExecutionContractError, BacktestContractError) as exc:
                rejected += 1
                events.append(
                    LedgerEvent(
                        LedgerEventType.REJECT,
                        intent.order_id,
                        intent.position_id,
                        None,
                        cash,
                        intent.quantity,
                        None,
                        str(exc),
                    )
                )

        last_prices = {bar.bar.symbol: bar.bar.close for bar in bars if bar.observable}
        open_equity = 0.0
        for position in positions.values():
            if position.symbol not in last_prices:
                raise BacktestContractError(
                    f"no observable closing mark for {position.symbol}"
                )
            open_equity += last_prices[position.symbol] * position.quantity
        return BacktestResult(
            initial_cash=self.initial_cash,
            final_cash=cash,
            final_equity=cash + open_equity,
            events=tuple(events),
            closed_trades=tuple(trades),
            open_positions=tuple(sorted(positions.values(), key=lambda item: item.position_id)),
            rejected_orders=rejected,
        )
