"""Execution-aware Target-Before-Stop / Triple Barrier labels."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stock_tracker.core.types import Bar

from ..backtest.execution import (
    ExecutionBar,
    ExecutionContractError,
    ExecutionEngine,
    Fill,
)
from ..backtest.market_rules import TradeSide
from ..core.time import to_utc


class LabelContractError(ValueError):
    """Raised when label semantics are optimistic or incomplete."""


class AmbiguousBarError(LabelContractError):
    """Lower-frequency OHLC cannot determine which barrier occurred first."""


class SameBarPolicy(StrEnum):
    MARK_AMBIGUOUS = "MARK_AMBIGUOUS"
    WORST_CASE = "WORST_CASE"
    LOWER_TIMEFRAME_REQUIRED = "LOWER_TIMEFRAME_REQUIRED"
    BEST_CASE = "BEST_CASE"


class BarrierKind(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"


class LabelOutcome(StrEnum):
    TP_FIRST = "TP_FIRST"
    SL_FIRST = "SL_FIRST"
    TIMEOUT = "TIMEOUT"
    AMBIGUOUS = "AMBIGUOUS"
    BARRIER_BLOCKED_TIMEOUT = "BARRIER_BLOCKED_TIMEOUT"
    ENTRY_UNAVAILABLE = "ENTRY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class TripleBarrierConfig:
    take_profit_atr: float
    stop_loss_atr: float
    horizon_sessions: int
    entry_delay_sessions: int = 1
    same_bar_policy: SameBarPolicy = SameBarPolicy.MARK_AMBIGUOUS

    def __post_init__(self) -> None:
        values = (self.take_profit_atr, self.stop_loss_atr)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise LabelContractError("ATR barrier multiples must be finite and positive")
        if self.horizon_sessions <= 0:
            raise LabelContractError("horizon_sessions must be positive")
        if self.entry_delay_sessions < 0:
            raise LabelContractError("entry_delay_sessions cannot be negative")
        if self.same_bar_policy is SameBarPolicy.BEST_CASE:
            raise LabelContractError("BEST_CASE is forbidden for formal labels")


@dataclass(frozen=True, slots=True)
class TripleBarrierResult:
    label: int
    outcome: LabelOutcome
    entry: Fill | None
    exit: Fill | None
    target_price: float | None
    stop_price: float | None
    first_barrier: BarrierKind | None
    first_barrier_index: int | None
    label_start_time: datetime
    label_end_time: datetime
    mfe: float
    mae: float
    blocked_reasons: tuple[str, ...]
    ambiguous: bool

    def __post_init__(self) -> None:
        if self.label not in {-1, 0, 1}:
            raise LabelContractError("label must be exactly -1, 0 or 1")
        if not math.isfinite(self.mfe) or not math.isfinite(self.mae):
            raise LabelContractError("MFE/MAE must be finite")
        to_utc(self.label_start_time, "label_start_time")
        to_utc(self.label_end_time, "label_end_time")
        if to_utc(self.label_end_time) < to_utc(self.label_start_time):
            raise LabelContractError("label_end_time cannot precede label_start_time")

    @property
    def binary_target(self) -> int:
        """Strict meta-label target: TP is 1, all resolved alternatives are 0."""

        return 1 if self.outcome is LabelOutcome.TP_FIRST else 0


@dataclass(frozen=True, slots=True)
class _BarrierTouch:
    kind: BarrierKind
    reference_price: float
    ambiguous: bool = False


def _touch(
    bar: ExecutionBar,
    *,
    target: float,
    stop: float,
    policy: SameBarPolicy,
    resolution: BarrierKind | None,
) -> _BarrierTouch | None:
    if not bar.observable:
        return None
    raw = bar.bar
    if raw.open >= target:
        return _BarrierTouch(BarrierKind.TAKE_PROFIT, raw.open)
    if raw.open <= stop:
        return _BarrierTouch(BarrierKind.STOP_LOSS, raw.open)
    tp = raw.high >= target
    sl = raw.low <= stop
    if not tp and not sl:
        return None
    if tp and sl:
        if policy is SameBarPolicy.MARK_AMBIGUOUS:
            return _BarrierTouch(BarrierKind.STOP_LOSS, stop, ambiguous=True)
        if policy is SameBarPolicy.WORST_CASE:
            return _BarrierTouch(BarrierKind.STOP_LOSS, stop)
        if policy is SameBarPolicy.LOWER_TIMEFRAME_REQUIRED:
            if resolution is None:
                raise AmbiguousBarError(
                    "same-bar TP/SL requires verified lower-timeframe resolution"
                )
            return _BarrierTouch(
                resolution,
                target if resolution is BarrierKind.TAKE_PROFIT else stop,
            )
        raise LabelContractError("BEST_CASE cannot enter formal labeling")
    if tp:
        return _BarrierTouch(BarrierKind.TAKE_PROFIT, target)
    return _BarrierTouch(BarrierKind.STOP_LOSS, stop)


def _blocked_reason(exc: ExecutionContractError) -> str:
    return str(exc).split(" (", 1)[0]


class TripleBarrierLabeler:
    """Generate labels from a sequence whose indices already mean sessions."""

    def __init__(self, engine: ExecutionEngine, config: TripleBarrierConfig) -> None:
        self.engine = engine
        self.config = config

    def label(
        self,
        bars: Sequence[ExecutionBar],
        *,
        signal_index: int,
        atr: float,
        requested_quantity: int,
        same_bar_resolution: Mapping[int, BarrierKind] | None = None,
    ) -> TripleBarrierResult:
        if not bars:
            raise LabelContractError("bars cannot be empty")
        if not 0 <= signal_index < len(bars):
            raise LabelContractError("signal_index is outside bars")
        if not math.isfinite(atr) or atr <= 0:
            raise LabelContractError("atr must be finite and positive")
        symbols = {bar.bar.symbol for bar in bars}
        markets = {bar.bar.market for bar in bars}
        if len(symbols) != 1 or len(markets) != 1:
            raise LabelContractError("one label sequence must have one symbol and market")
        times = [to_utc(bar.bar.timestamp) for bar in bars]
        if times != sorted(times) or len(set(times)) != len(times):
            raise LabelContractError("bar timestamps must be strictly chronological")

        entry_start = signal_index + self.config.entry_delay_sessions
        if entry_start >= len(bars):
            end_time = bars[-1].bar.timestamp
            return TripleBarrierResult(
                label=0,
                outcome=LabelOutcome.ENTRY_UNAVAILABLE,
                entry=None,
                exit=None,
                target_price=None,
                stop_price=None,
                first_barrier=None,
                first_barrier_index=None,
                label_start_time=bars[signal_index].bar.timestamp,
                label_end_time=end_time,
                mfe=0.0,
                mae=0.0,
                blocked_reasons=("ENTRY_WINDOW_EMPTY",),
                ambiguous=False,
            )
        try:
            entry = self.engine.next_fill(
                bars,
                start_index=entry_start,
                side=TradeSide.BUY,
                requested_quantity=requested_quantity,
            )
        except ExecutionContractError as exc:
            return TripleBarrierResult(
                label=0,
                outcome=LabelOutcome.ENTRY_UNAVAILABLE,
                entry=None,
                exit=None,
                target_price=None,
                stop_price=None,
                first_barrier=None,
                first_barrier_index=None,
                label_start_time=bars[signal_index].bar.timestamp,
                label_end_time=bars[-1].bar.timestamp,
                mfe=0.0,
                mae=0.0,
                blocked_reasons=(_blocked_reason(exc),),
                ambiguous=False,
            )

        target = entry.price + self.config.take_profit_atr * atr
        stop = entry.price - self.config.stop_loss_atr * atr
        if stop <= 0:
            raise LabelContractError("computed stop price is non-positive")
        horizon_end = min(
            len(bars),
            entry.session_index + self.config.horizon_sessions,
        )
        if horizon_end <= entry.session_index:
            raise LabelContractError("horizon contains no sessions")

        pending: _BarrierTouch | None = None
        pending_index: int | None = None
        blocked: list[str] = []
        mfe = 0.0
        mae = 0.0
        resolution = same_bar_resolution or {}
        for index in range(entry.session_index, horizon_end):
            execution_bar = bars[index]
            if execution_bar.observable:
                mfe = max(mfe, execution_bar.bar.high - entry.price)
                mae = min(mae, execution_bar.bar.low - entry.price)
            if pending is None:
                touch = _touch(
                    execution_bar,
                    target=target,
                    stop=stop,
                    policy=self.config.same_bar_policy,
                    resolution=resolution.get(index),
                )
                if touch is None:
                    continue
                if touch.ambiguous:
                    return TripleBarrierResult(
                        label=0,
                        outcome=LabelOutcome.AMBIGUOUS,
                        entry=entry,
                        exit=None,
                        target_price=target,
                        stop_price=stop,
                        first_barrier=None,
                        first_barrier_index=index,
                        label_start_time=entry.timestamp,
                        label_end_time=execution_bar.bar.timestamp,
                        mfe=mfe,
                        mae=mae,
                        blocked_reasons=tuple(blocked),
                        ambiguous=True,
                    )
                pending = touch
                pending_index = index
                reference = touch.reference_price
            else:
                reference = execution_bar.bar.open

            try:
                exit_fill = self.engine.fill_at(
                    bars,
                    index,
                    side=TradeSide.SELL,
                    requested_quantity=entry.quantity,
                    acquired_session_index=entry.session_index,
                    reference_price=reference,
                )
            except ExecutionContractError as exc:
                reason = _blocked_reason(exc)
                if reason in {
                    "UNKNOWN_PRICE_LIMIT_STATE",
                    "MISSING_ACQUISITION_SESSION",
                }:
                    raise
                blocked.append(f"{index}:{reason}")
                continue

            outcome = (
                LabelOutcome.TP_FIRST
                if pending.kind is BarrierKind.TAKE_PROFIT
                else LabelOutcome.SL_FIRST
            )
            return TripleBarrierResult(
                label=1 if outcome is LabelOutcome.TP_FIRST else -1,
                outcome=outcome,
                entry=entry,
                exit=exit_fill,
                target_price=target,
                stop_price=stop,
                first_barrier=pending.kind,
                first_barrier_index=pending_index,
                label_start_time=entry.timestamp,
                label_end_time=exit_fill.timestamp,
                mfe=mfe,
                mae=mae,
                blocked_reasons=tuple(blocked),
                ambiguous=False,
            )

        end_time = bars[horizon_end - 1].bar.timestamp
        return TripleBarrierResult(
            label=0,
            outcome=(
                LabelOutcome.BARRIER_BLOCKED_TIMEOUT
                if pending is not None
                else LabelOutcome.TIMEOUT
            ),
            entry=entry,
            exit=None,
            target_price=target,
            stop_price=stop,
            first_barrier=pending.kind if pending is not None else None,
            first_barrier_index=pending_index,
            label_start_time=entry.timestamp,
            label_end_time=end_time,
            mfe=mfe,
            mae=mae,
            blocked_reasons=tuple(blocked),
            ambiguous=False,
        )


def unsafe_execution_bars(
    bars: Sequence[Bar],
    *,
    explicit_unlocked_limits: bool = True,
) -> tuple[ExecutionBar, ...]:
    """Adapt raw bars for adversarial tests only.

    This helper intentionally does not restore missing exchange sessions. Formal
    production labels must use :class:`CalendarAwareTripleBarrierLabeler`.
    """

    if explicit_unlocked_limits:
        return tuple(
            ExecutionBar(bar, locked_limit_up=False, locked_limit_down=False)
            for bar in bars
        )
    return tuple(ExecutionBar(bar) for bar in bars)
