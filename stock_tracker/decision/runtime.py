"""Runtime adapters for the Stage 1 Today Action product slice.

The functions in this module consume already-collected runtime facts.  They do
not fetch market data, mutate the database, call an LLM, or claim calibrated
probabilities.  Invalid or incomplete inputs are downgraded rather than being
silently coerced into executable actions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from stock_tracker.core import types as T

from .action_mapper import map_signal_to_action
from .position_sizing import size_position
from .trade_plan import build_trade_plan
from .types import (
    ActionDecision,
    ActionState,
    BlockerSeverity,
    DecisionAction,
    DecisionBlocker,
    DecisionContractError,
    ProbabilityEvidenceLevel,
    RiskMode,
    TradePlan,
    UserPortfolioProfile,
)


_OPENING_ACTIONS = {
    ActionState.EXECUTABLE,
    ActionState.WAIT_PULLBACK,
    ActionState.WAIT_BREAKOUT,
}


@dataclass(frozen=True, slots=True)
class RuntimeDecisionRecord:
    """One product action plus the runtime facts needed by API serialization."""

    action: DecisionAction
    signal: T.Signal | None
    quote: T.Quote | None
    action_decision: ActionDecision | None
    name: str
    hard_blockers: tuple[DecisionBlocker, ...]
    soft_blockers: tuple[DecisionBlocker, ...]


def _hard(code: str, message: str, *, recoverable: bool = True) -> DecisionBlocker:
    return DecisionBlocker(code, message, BlockerSeverity.HARD, recoverable)


def _soft(code: str, message: str, *, recoverable: bool = True) -> DecisionBlocker:
    return DecisionBlocker(code, message, BlockerSeverity.SOFT, recoverable)


def _merge_blockers(
    *groups: tuple[DecisionBlocker, ...],
) -> tuple[DecisionBlocker, ...]:
    seen: set[tuple[str, str, BlockerSeverity]] = set()
    merged: list[DecisionBlocker] = []
    for group in groups:
        for blocker in group:
            identity = (blocker.code, blocker.message, blocker.severity)
            if identity not in seen:
                seen.add(identity)
                merged.append(blocker)
    return tuple(merged)


def _score(name: str, value: object) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise DecisionContractError(f"{name} must be an integer in [0, 100]")
    return value


def _finite_ratio(name: str, value: object) -> float:
    if type(value) not in (int, float):
        raise DecisionContractError(f"{name} must be a finite ratio")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise DecisionContractError(f"{name} must be in [0, 1]")
    return number


def _positive_price(name: str, value: object) -> float:
    if type(value) not in (int, float):
        raise DecisionContractError(f"{name} must be a finite price")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise DecisionContractError(f"{name} must be positive and finite")
    return number


def _signal_plan_prices(
    signal: T.Signal,
    *,
    no_chase_pct: float,
) -> tuple[float, float, float, float, float, float, float, float]:
    low = _positive_price("entry_low", signal.entry_low)
    high = _positive_price("entry_high", signal.entry_high)
    trigger = _positive_price("trigger_price", signal.trigger_price)
    invalidation = _positive_price("invalidation_price", signal.invalidation_price)
    target_1 = _positive_price("target_1", signal.target_1)
    target_2 = _positive_price("target_2", signal.target_2)
    reward_risk = _positive_price("reward_risk", signal.reward_risk)
    pct = _finite_ratio("no_chase_pct", no_chase_pct)
    no_chase = max(high, trigger) * (1.0 + pct)
    return low, high, trigger, no_chase, invalidation, target_1, target_2, reward_risk


def _entry_reference(
    action: ActionState,
    *,
    quote: T.Quote | None,
    entry_high: float,
    trigger_price: float,
    no_chase_above: float,
    invalidation_price: float,
) -> float:
    if action is ActionState.WAIT_PULLBACK:
        return entry_high
    if action is ActionState.WAIT_BREAKOUT:
        return trigger_price
    if action is ActionState.EXECUTABLE and quote is not None:
        last = quote.last
        if type(last) in (int, float):
            current = float(last)
            if (
                math.isfinite(current)
                and invalidation_price < current <= no_chase_above
            ):
                return current
    return trigger_price


def _soft_blockers_for(
    action: ActionState,
    data_status: T.DataStatus,
    signal_state: T.SignalState,
) -> tuple[DecisionBlocker, ...]:
    blockers: list[DecisionBlocker] = []
    if action is ActionState.WAIT_PULLBACK:
        blockers.append(
            _soft("WAIT_PULLBACK_CONFIRMATION", "尚未完成回踩确认")
        )
    elif action is ActionState.WAIT_BREAKOUT:
        blockers.append(
            _soft("WAIT_BREAKOUT_CONFIRMATION", "尚未完成突破确认")
        )
    if data_status is T.DataStatus.DELAYED:
        blockers.append(
            _soft("DATA_DELAYED", "行情存在延迟，不可按实时成交条件理解")
        )
    if signal_state is T.SignalState.OVEREXTENDED:
        blockers.append(
            _soft("NO_CHASE_OVEREXTENDED", "价格过度扩张，禁止追高")
        )
    return tuple(blockers)


def build_signal_record(
    signal: T.Signal,
    *,
    quote: T.Quote | None,
    data_status: T.DataStatus,
    has_position: bool,
    profile: UserPortfolioProfile | None,
    current_portfolio_heat_pct: float,
    current_sector_exposure_pct: float,
    current_theme_exposure_pct: float,
    sector: str,
    as_of: datetime,
    no_chase_pct: float,
    lot_size: int | None = None,
    external_hard_blockers: tuple[DecisionBlocker, ...] = (),
) -> RuntimeDecisionRecord:
    """Build one deterministic decision record from a runtime signal."""

    if not isinstance(signal, T.Signal):
        raise DecisionContractError("signal must be Signal")
    if not isinstance(data_status, T.DataStatus):
        raise DecisionContractError("data_status must be DataStatus")
    if type(has_position) is not bool:
        raise DecisionContractError("has_position must be a real boolean")
    if profile is not None and not isinstance(profile, UserPortfolioProfile):
        raise DecisionContractError("profile must be UserPortfolioProfile or None")
    if type(sector) is not str:
        raise DecisionContractError("sector must be a string")
    if type(external_hard_blockers) is not tuple:
        raise DecisionContractError("external_hard_blockers must be a tuple")
    for blocker in external_hard_blockers:
        if not isinstance(blocker, DecisionBlocker):
            raise DecisionContractError(
                "external_hard_blockers must contain DecisionBlocker values"
            )
        if blocker.severity is not BlockerSeverity.HARD:
            raise DecisionContractError(
                "external_hard_blockers must contain only HARD blockers"
            )

    effective_signal = replace(signal, data_status=data_status)
    current_price = quote.last if quote is not None else None
    decision = map_signal_to_action(
        effective_signal,
        has_position=has_position,
        current_price=current_price,
    )
    hard = _merge_blockers(
        tuple(
            blocker
            for blocker in decision.blockers
            if blocker.severity is BlockerSeverity.HARD
        ),
        external_hard_blockers,
    )
    soft = _soft_blockers_for(decision.action, data_status, signal.state)
    effective_action = decision.action
    plan: TradePlan | None = None

    scores = signal.scores or T.ScoreSet()
    opportunity = _score("opportunity", scores.opportunity)
    timing = _score("timing", scores.timing)
    risk = _score("risk", scores.risk)
    confidence = _score("confidence", scores.confidence)
    freshness = _finite_ratio("freshness", signal.freshness)

    if effective_action in _OPENING_ACTIONS:
        try:
            (
                entry_low,
                entry_high,
                trigger_price,
                no_chase_above,
                invalidation_price,
                target_1,
                target_2,
                reward_risk,
            ) = _signal_plan_prices(signal, no_chase_pct=no_chase_pct)
        except DecisionContractError as exc:
            hard = _merge_blockers(
                hard,
                (_hard("INVALID_TRADE_PLAN", str(exc), recoverable=False),),
            )
            effective_action = (
                ActionState.DATA_BLOCKED
                if data_status in (T.DataStatus.STALE, T.DataStatus.UNKNOWN)
                else ActionState.AVOID
            )
        else:
            entry_reference = _entry_reference(
                effective_action,
                quote=quote,
                entry_high=entry_high,
                trigger_price=trigger_price,
                no_chase_above=no_chase_above,
                invalidation_price=invalidation_price,
            )
            position_size = None
            if profile is not None:
                try:
                    position_size = size_position(
                        profile,
                        market=signal.market,
                        entry_price=entry_reference,
                        invalidation_price=invalidation_price,
                        current_portfolio_heat_pct=current_portfolio_heat_pct,
                        current_sector_exposure_pct=current_sector_exposure_pct,
                        current_theme_exposure_pct=current_theme_exposure_pct,
                        lot_size=lot_size,
                        hard_blockers=hard,
                    )
                except DecisionContractError as exc:
                    hard = _merge_blockers(
                        hard,
                        (_hard("POSITION_SIZE_UNAVAILABLE", str(exc)),),
                    )
                else:
                    if not position_size.allowed:
                        hard = _merge_blockers(hard, position_size.blockers)
                        effective_action = (
                            ActionState.DATA_BLOCKED
                            if any(
                                blocker.code
                                in {"DATA_UNAVAILABLE", "EXECUTION_DATA_NOT_LIVE"}
                                for blocker in hard
                            )
                            else ActionState.AVOID
                        )
            aggressive_multiplier = None
            aggressive_action = None
            if (
                soft
                and not hard
                and data_status is T.DataStatus.LIVE
                and profile is not None
                and profile.risk_mode is not RiskMode.CONSERVATIVE
                and effective_action
                in (ActionState.WAIT_PULLBACK, ActionState.WAIT_BREAKOUT)
            ):
                aggressive_multiplier = 0.5
                aggressive_action = ActionState.EXECUTABLE
            plan = build_trade_plan(
                symbol=signal.symbol,
                market=signal.market,
                strategy_id=signal.strategy_id,
                action=effective_action,
                entry_low=entry_low,
                entry_high=entry_high,
                trigger_price=trigger_price,
                no_chase_above=no_chase_above,
                invalidation_price=invalidation_price,
                target_1=target_1,
                target_2=target_2,
                reward_risk=reward_risk,
                next_trigger=signal.next_trigger,
                position_size=position_size,
                calibrated_probability=None,
                probability_evidence_level=ProbabilityEvidenceLevel.INSUFFICIENT,
                data_status=data_status,
                as_of=as_of,
                hard_blockers=hard,
                soft_blockers=soft,
                aggressive_risk_budget_multiplier=aggressive_multiplier,
                aggressive_action=aggressive_action,
            )
            effective_action = plan.action

    reward_risk_value = (
        plan.reward_risk
        if plan is not None
        else max(0.0, float(signal.reward_risk or 0.0))
    )
    reason = decision.reason
    if hard and effective_action in (ActionState.AVOID, ActionState.DATA_BLOCKED):
        reason = hard[0].message
    action = DecisionAction(
        symbol=signal.symbol,
        market=signal.market,
        action=effective_action,
        strategy_id=signal.strategy_id,
        opportunity=opportunity,
        timing=timing,
        risk=risk,
        confidence=confidence,
        reward_risk=reward_risk_value,
        freshness=freshness,
        sector=sector,
        reason=reason,
        trade_plan=plan,
        data_status=data_status,
    )
    name = (
        quote.name
        if quote is not None and type(quote.name) is str and quote.name.strip()
        else signal.symbol
    )
    return RuntimeDecisionRecord(
        action=action,
        signal=effective_signal,
        quote=quote,
        action_decision=decision,
        name=name,
        hard_blockers=hard,
        soft_blockers=soft,
    )


def build_unbound_position_record(
    position: T.Position,
    *,
    quote: T.Quote | None,
    data_status: T.DataStatus,
    sector: str,
) -> RuntimeDecisionRecord:
    """Represent a holding that has no active signal without inventing a thesis."""

    blocker = _hard(
        "POSITION_THESIS_MISSING",
        "持仓未绑定有效交易计划，暂无法给出结构化退出判断",
    )
    action = DecisionAction(
        symbol=position.symbol,
        market=position.market,
        action=ActionState.DATA_BLOCKED,
        strategy_id="UNBOUND_POSITION",
        opportunity=0,
        timing=0,
        risk=100,
        confidence=0,
        reward_risk=0.0,
        freshness=0.0,
        sector=sector,
        reason=blocker.message,
        trade_plan=None,
        data_status=data_status,
    )
    name = (
        quote.name
        if quote is not None and type(quote.name) is str and quote.name.strip()
        else position.symbol
    )
    return RuntimeDecisionRecord(
        action=action,
        signal=None,
        quote=quote,
        action_decision=None,
        name=name,
        hard_blockers=(blocker,),
        soft_blockers=(),
    )
