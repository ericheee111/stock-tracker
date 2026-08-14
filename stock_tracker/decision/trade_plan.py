from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from stock_tracker.core.types import DataStatus, Market

from .types import (
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    PlanVariant,
    PositionSizeResult,
    ProbabilityEvidenceLevel,
    TradePlan,
)


def _positive_number(name: str, value: object, *, allow_zero: bool = False) -> float:
    if type(value) not in (int, float):
        raise DecisionContractError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        comparator = "non-negative" if allow_zero else "greater than zero"
        raise DecisionContractError(f"{name} must be finite and {comparator}")
    return number


def _blocker_tuple(
    name: str,
    value: object,
    severity: BlockerSeverity,
) -> tuple[DecisionBlocker, ...]:
    if type(value) is not tuple:
        raise DecisionContractError(f"{name} must be a tuple")
    for blocker in value:
        if not isinstance(blocker, DecisionBlocker):
            raise DecisionContractError(f"{name} must contain DecisionBlocker values")
        if blocker.severity is not severity:
            raise DecisionContractError(f"{name} contains the wrong blocker severity")
    return value


def _merge_blockers(
    first: tuple[DecisionBlocker, ...],
    second: tuple[DecisionBlocker, ...],
) -> tuple[DecisionBlocker, ...]:
    seen: set[tuple[str, str]] = set()
    merged: list[DecisionBlocker] = []
    for blocker in (*first, *second):
        identity = (blocker.code, blocker.message)
        if identity not in seen:
            seen.add(identity)
            merged.append(blocker)
    return tuple(merged)


def _force_blocked_position(
    position_size: PositionSizeResult | None,
    hard_blockers: tuple[DecisionBlocker, ...],
) -> PositionSizeResult | None:
    if position_size is None:
        return None
    if not isinstance(position_size, PositionSizeResult):
        raise DecisionContractError("position_size must be PositionSizeResult or None")
    if not hard_blockers and position_size.allowed:
        return position_size
    blockers = _merge_blockers(position_size.blockers, hard_blockers)
    return replace(
        position_size,
        allowed=False,
        shares=0,
        actual_risk_amount=0.0,
        actual_risk_pct=0.0,
        position_value=0.0,
        position_pct=0.0,
        limiting_factors=tuple(dict.fromkeys((*position_size.limiting_factors, "HARD_BLOCKER"))),
        blockers=blockers,
    )


def build_trade_plan(
    *,
    symbol: str,
    market: Market,
    strategy_id: str,
    action: ActionState,
    entry_low: float,
    entry_high: float,
    trigger_price: float,
    no_chase_above: float,
    invalidation_price: float,
    target_1: float,
    target_2: float,
    reward_risk: float,
    next_trigger: str,
    position_size: PositionSizeResult | None,
    calibrated_probability: float | None,
    probability_evidence_level: ProbabilityEvidenceLevel,
    data_status: DataStatus,
    as_of: datetime,
    hard_blockers: tuple[DecisionBlocker, ...] = (),
    soft_blockers: tuple[DecisionBlocker, ...] = (),
    balanced_risk_budget_multiplier: float = 1.0,
    aggressive_risk_budget_multiplier: float | None = None,
    aggressive_action: ActionState | None = None,
) -> TradePlan:
    if type(symbol) is not str or not symbol.strip():
        raise DecisionContractError("symbol must be a non-empty string")
    if not isinstance(market, Market):
        raise DecisionContractError("market must be Market")
    if type(strategy_id) is not str or not strategy_id.strip():
        raise DecisionContractError("strategy_id must be a non-empty string")
    if not isinstance(action, ActionState):
        raise DecisionContractError("action must be ActionState")

    low = _positive_number("entry_low", entry_low)
    high = _positive_number("entry_high", entry_high)
    trigger = _positive_number("trigger_price", trigger_price)
    no_chase = _positive_number("no_chase_above", no_chase_above)
    invalidation = _positive_number("invalidation_price", invalidation_price)
    first_target = _positive_number("target_1", target_1)
    second_target = _positive_number("target_2", target_2)
    rr = _positive_number("reward_risk", reward_risk)
    if low > high:
        raise DecisionContractError("entry_low cannot exceed entry_high")
    if invalidation >= low:
        raise DecisionContractError("invalidation_price must be below the long entry zone")
    if no_chase < high:
        raise DecisionContractError("no_chase_above cannot be below the entry zone")
    if trigger > no_chase:
        raise DecisionContractError("trigger_price cannot exceed no_chase_above")
    if first_target <= high or second_target < first_target:
        raise DecisionContractError("long targets must be ordered above the entry zone")
    if type(next_trigger) is not str:
        raise DecisionContractError("next_trigger must be a string")

    hard = _blocker_tuple("hard_blockers", hard_blockers, BlockerSeverity.HARD)
    soft = _blocker_tuple("soft_blockers", soft_blockers, BlockerSeverity.SOFT)
    if position_size is not None and not isinstance(position_size, PositionSizeResult):
        raise DecisionContractError("position_size must be PositionSizeResult or None")
    if position_size is not None:
        if not math.isclose(
            position_size.invalidation_price,
            invalidation,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise DecisionContractError(
                "position_size invalidation_price must match the trade plan"
            )
        if not low <= position_size.entry_price <= no_chase:
            raise DecisionContractError(
                "position_size entry_price must be between entry_low and no_chase_above"
            )
    if position_size is not None and not position_size.allowed:
        position_hard = tuple(
            blocker
            for blocker in position_size.blockers
            if blocker.severity is BlockerSeverity.HARD
        )
        if not position_hard:
            position_hard = (
                DecisionBlocker(
                    "POSITION_SIZE_BLOCKED",
                    "Position sizing did not permit an opening position",
                    BlockerSeverity.HARD,
                ),
            )
        hard = _merge_blockers(hard, position_hard)
    if data_status in (DataStatus.STALE, DataStatus.UNKNOWN):
        hard = _merge_blockers(
            hard,
            (
                DecisionBlocker(
                    "DATA_NOT_ACTIONABLE",
                    "Stale or unknown data cannot authorize a new position",
                    BlockerSeverity.HARD,
                ),
            ),
        )

    balanced_multiplier = _positive_number(
        "balanced_risk_budget_multiplier", balanced_risk_budget_multiplier
    )
    if balanced_multiplier > 1.0:
        raise DecisionContractError("balanced_risk_budget_multiplier must be <= 1")
    aggressive_multiplier: float | None = None
    resolved_aggressive_action = action
    if aggressive_action is not None:
        if not isinstance(aggressive_action, ActionState):
            raise DecisionContractError("aggressive_action must be ActionState or None")
        if aggressive_action not in {
            ActionState.EXECUTABLE,
            ActionState.WAIT_PULLBACK,
            ActionState.WAIT_BREAKOUT,
            ActionState.WATCH,
            ActionState.AVOID,
            ActionState.DATA_BLOCKED,
        }:
            raise DecisionContractError(
                "aggressive_action must use a new-opportunity action"
            )
        resolved_aggressive_action = aggressive_action
    if aggressive_risk_budget_multiplier is not None:
        aggressive_multiplier = _positive_number(
            "aggressive_risk_budget_multiplier", aggressive_risk_budget_multiplier
        )
        if aggressive_multiplier > 1.0:
            raise DecisionContractError("aggressive_risk_budget_multiplier must be <= 1")
        if not soft and not hard:
            raise DecisionContractError("aggressive plan requires at least one soft blocker")
        if not hard and aggressive_multiplier >= balanced_multiplier:
            raise DecisionContractError(
                "aggressive risk budget must be lower than the balanced risk budget"
            )
        if (
            resolved_aggressive_action is ActionState.EXECUTABLE
            and data_status is not DataStatus.LIVE
        ):
            raise DecisionContractError(
                "aggressive EXECUTABLE plan requires LIVE data"
            )
    elif aggressive_action is not None:
        raise DecisionContractError(
            "aggressive_action requires aggressive_risk_budget_multiplier"
        )

    effective_action = action
    if hard and action is ActionState.EXECUTABLE:
        effective_action = (
            ActionState.DATA_BLOCKED
            if any(blocker.code == "DATA_NOT_ACTIONABLE" for blocker in hard)
            else ActionState.AVOID
        )
    balanced = PlanVariant(
        name="BALANCED",
        action=effective_action,
        risk_budget_multiplier=balanced_multiplier,
        note="Standard confirmation and account constraints apply",
    )
    aggressive = None
    if soft and not hard and aggressive_multiplier is not None:
        aggressive = PlanVariant(
            name="AGGRESSIVE",
            action=resolved_aggressive_action,
            risk_budget_multiplier=aggressive_multiplier,
            note="Soft blockers remain; use the reduced risk budget",
        )

    return TradePlan(
        symbol=symbol,
        market=market,
        strategy_id=strategy_id,
        action=effective_action,
        entry_low=low,
        entry_high=high,
        trigger_price=trigger,
        no_chase_above=no_chase,
        invalidation_price=invalidation,
        target_1=first_target,
        target_2=second_target,
        reward_risk=rr,
        next_trigger=next_trigger,
        position_size=_force_blocked_position(position_size, hard),
        balanced_plan=balanced,
        aggressive_plan=aggressive,
        hard_blockers=hard,
        soft_blockers=soft,
        calibrated_probability=calibrated_probability,
        probability_evidence_level=probability_evidence_level,
        data_status=data_status,
        as_of=as_of,
    )
