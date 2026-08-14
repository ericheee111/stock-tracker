"""Deterministic mapping from runtime signals to product actions."""

from __future__ import annotations

import math

from stock_tracker.core.types import DataStatus, Signal, SignalState

from .types import (
    ActionDecision,
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
)


def _hard(code: str, message: str) -> DecisionBlocker:
    return DecisionBlocker(code, message, BlockerSeverity.HARD, True)


def _require_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise DecisionContractError(f"{name} must be a real boolean")
    return value


def _require_optional_price(name: str, value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise DecisionContractError(f"{name} must be a finite number or None")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise DecisionContractError(f"{name} must be positive and finite")
    return number


def _decision(
    signal: Signal,
    action: ActionState,
    has_position: bool,
    reason: str,
    *,
    actionable: bool = False,
    blockers: tuple[DecisionBlocker, ...] = (),
) -> ActionDecision:
    return ActionDecision(
        action=action,
        source_state=signal.state,
        has_position=has_position,
        actionable=actionable,
        reason=reason,
        data_status=signal.data_status,
        blockers=blockers,
    )


def map_signal_to_action(
    signal: Signal,
    *,
    has_position: bool,
    risk_allowed: bool = True,
    current_price: float | None = None,
    warning_buffer_ratio: float = 0.25,
) -> ActionDecision:
    """Map one signal without turning stale data into a new trade or exit."""

    if not isinstance(signal, Signal):
        raise DecisionContractError("signal must be Signal")
    _require_bool("has_position", has_position)
    _require_bool("risk_allowed", risk_allowed)
    price = _require_optional_price("current_price", current_price)
    if type(warning_buffer_ratio) not in (int, float):
        raise DecisionContractError("warning_buffer_ratio must be numeric")
    warning_ratio = float(warning_buffer_ratio)
    if not math.isfinite(warning_ratio) or not 0 <= warning_ratio <= 1:
        raise DecisionContractError("warning_buffer_ratio must be in [0, 1]")

    if signal.data_status in (DataStatus.STALE, DataStatus.UNKNOWN) or signal.state is SignalState.DATA_INVALID:
        blocker = _hard("DATA_UNAVAILABLE", "Data is stale or unavailable")
        return _decision(
            signal,
            ActionState.DATA_BLOCKED,
            has_position,
            blocker.message,
            blockers=(blocker,),
        )

    # Minimum safe Stage 1 exit baseline: only a LIVE price may trigger it.
    if has_position and signal.data_status is DataStatus.LIVE and price is not None:
        invalidation = float(signal.invalidation_price or 0)
        if invalidation > 0 and price <= invalidation:
            return _decision(
                signal,
                ActionState.EXIT,
                True,
                "Live price breached structural invalidation",
                actionable=True,
            )
        reference = float(signal.entry_high or signal.trigger_price or 0)
        if invalidation > 0 and reference > invalidation:
            warning_level = invalidation + (reference - invalidation) * warning_ratio
            if price <= warning_level:
                return _decision(
                    signal,
                    ActionState.WARNING,
                    True,
                    "Price is approaching structural invalidation",
                )

    state = signal.state
    if state is SignalState.COLD:
        return _decision(
            signal,
            ActionState.WARNING if has_position else ActionState.WATCH,
            has_position,
            "Position has no executable thesis" if has_position else "No executable setup",
        )
    if state is SignalState.WATCH:
        return _decision(
            signal,
            ActionState.WARNING if has_position else ActionState.WATCH,
            has_position,
            "Position thesis needs reassessment" if has_position else "Setup is not complete",
        )
    if state is SignalState.ARMED_PULLBACK:
        return _decision(
            signal,
            ActionState.HOLD if has_position else ActionState.WAIT_PULLBACK,
            has_position,
            "Hold while add-on pullback remains unconfirmed"
            if has_position
            else "Wait for pullback confirmation",
        )
    if state is SignalState.ARMED_BREAKOUT:
        return _decision(
            signal,
            ActionState.HOLD if has_position else ActionState.WAIT_BREAKOUT,
            has_position,
            "Hold while add-on breakout remains unconfirmed"
            if has_position
            else "Wait for breakout confirmation",
        )
    if state is SignalState.TRIGGERED:
        if signal.data_status is not DataStatus.LIVE:
            blocker = _hard("EXECUTION_DATA_NOT_LIVE", "Execution requires LIVE data")
            return _decision(signal, ActionState.DATA_BLOCKED, has_position, blocker.message, blockers=(blocker,))
        if not risk_allowed:
            blocker = _hard("RISK_GATE_BLOCKED", "Risk gate did not pass")
            return _decision(signal, ActionState.AVOID, has_position, blocker.message, blockers=(blocker,))
        if has_position:
            return _decision(signal, ActionState.HOLD, True, "Trigger passed and position already exists")
        return _decision(signal, ActionState.EXECUTABLE, False, "Trigger and risk gate passed", actionable=True)
    if state is SignalState.ACTIVE:
        if has_position:
            return _decision(signal, ActionState.HOLD, True, "Holding thesis remains valid")
        return _decision(signal, ActionState.WATCH, False, "Signal is active but no position is recorded")
    if state is SignalState.TRIM:
        if has_position:
            return _decision(signal, ActionState.TRIM, True, "Risk/reward or trend quality deteriorated", actionable=True)
        return _decision(signal, ActionState.AVOID, False, "No position to trim")
    if state is SignalState.OVEREXTENDED:
        if has_position:
            return _decision(
                signal,
                ActionState.TRIM,
                True,
                "Position is overextended",
                actionable=True,
            )
        return _decision(signal, ActionState.AVOID, False, "Do not chase an overextended price")
    if state in (SignalState.EXIT, SignalState.INVALIDATED):
        if has_position:
            return _decision(signal, ActionState.EXIT, True, "Trading thesis is invalid", actionable=True)
        return _decision(signal, ActionState.AVOID, False, "Candidate plan is invalid")
    if state is SignalState.EXPIRED:
        return _decision(
            signal,
            ActionState.WARNING if has_position else ActionState.AVOID,
            has_position,
            "Holding thesis expired and needs reassessment"
            if has_position
            else "Signal expired",
        )

    raise DecisionContractError(f"Unsupported signal state: {state}")
