from __future__ import annotations

from datetime import datetime

from stock_tracker.core.types import DataStatus

from .types import (
    ActionState,
    DecisionAction,
    DecisionBrief,
    DecisionContractError,
    RankingMode,
)


_CORE_ACTION_PRIORITY = {
    ActionState.EXECUTABLE: 0,
    ActionState.WAIT_PULLBACK: 1,
    ActionState.WAIT_BREAKOUT: 2,
    ActionState.WATCH: 3,
    ActionState.AVOID: 4,
    ActionState.DATA_BLOCKED: 5,
}

_HOLDING_ACTION_PRIORITY = {
    ActionState.EXIT: 0,
    ActionState.TRIM: 1,
    ActionState.WARNING: 2,
    ActionState.HOLD: 3,
    ActionState.DATA_BLOCKED: 4,
    ActionState.PARTIAL_TAKE_PROFIT: 5,
    ActionState.TREND_RUNNER: 6,
}

_DATA_STATUS_PRIORITY = {
    DataStatus.LIVE: 0,
    DataStatus.DELAYED: 1,
    DataStatus.STALE: 2,
    DataStatus.UNKNOWN: 3,
}

_UNKNOWN_SECTORS = {"", "UNKNOWN", "UNCLASSIFIED", "N/A", "BROAD"}


def _validated_actions(name: str, value: object) -> tuple[DecisionAction, ...]:
    if type(value) is not tuple:
        raise DecisionContractError(f"{name} must be a tuple")
    for action in value:
        if not isinstance(action, DecisionAction):
            raise DecisionContractError(f"{name} must contain DecisionAction values")
        if action.action is ActionState.EXECUTABLE and action.data_status in (
            DataStatus.STALE,
            DataStatus.UNKNOWN,
        ):
            raise DecisionContractError(
                "STALE or UNKNOWN data cannot produce an EXECUTABLE action"
            )
    return value


def _core_key(action: DecisionAction) -> tuple:
    return (
        _CORE_ACTION_PRIORITY.get(action.action, 99),
        -action.opportunity,
        -action.timing,
        -action.reward_risk,
        -action.confidence,
        -action.freshness,
        action.risk,
        _DATA_STATUS_PRIORITY[action.data_status],
        action.symbol,
        action.strategy_id,
    )


def _sector_bucket(action: DecisionAction) -> str:
    normalized = action.sector.strip().upper()
    if normalized in _UNKNOWN_SECTORS:
        return f"__UNKNOWN__:{action.symbol}"
    return normalized


def select_core_opportunities(
    candidates: tuple[DecisionAction, ...],
    *,
    max_items: int = 5,
    max_per_sector: int = 2,
) -> tuple[DecisionAction, ...]:
    actions = _validated_actions("candidates", candidates)
    if type(max_items) is not int or not 1 <= max_items <= 5:
        raise DecisionContractError("max_items must be an integer in [1, 5]")
    if type(max_per_sector) is not int or max_per_sector <= 0:
        raise DecisionContractError("max_per_sector must be a positive integer")

    best_by_symbol: dict[str, DecisionAction] = {}
    for candidate in sorted(actions, key=_core_key):
        best_by_symbol.setdefault(candidate.symbol, candidate)

    selected: list[DecisionAction] = []
    sector_counts: dict[str, int] = {}
    for candidate in sorted(best_by_symbol.values(), key=_core_key):
        bucket = _sector_bucket(candidate)
        if sector_counts.get(bucket, 0) >= max_per_sector:
            continue
        selected.append(candidate)
        sector_counts[bucket] = sector_counts.get(bucket, 0) + 1
        if len(selected) == max_items:
            break
    return tuple(selected)


def sort_holding_actions(
    holding_actions: tuple[DecisionAction, ...],
) -> tuple[DecisionAction, ...]:
    actions = _validated_actions("holding_actions", holding_actions)
    return tuple(
        sorted(
            actions,
            key=lambda action: (
                _HOLDING_ACTION_PRIORITY.get(action.action, 99),
                -action.risk,
                action.symbol,
            ),
        )
    )


def ranking_mode_for(actions: tuple[DecisionAction, ...]) -> RankingMode:
    _validated_actions("actions", actions)
    if not actions:
        return RankingMode.RULE_EVIDENCE
    for action in actions:
        if (
            action.trade_plan is None
            or action.trade_plan.calibrated_probability is None
        ):
            return RankingMode.RULE_EVIDENCE
    return RankingMode.CALIBRATED_PROBABILITY


def build_decision_brief(
    *,
    as_of: datetime,
    market_posture: str,
    aggression_level: int,
    core_candidates: tuple[DecisionAction, ...],
    holding_actions: tuple[DecisionAction, ...],
    avoid_reasons: tuple[str, ...],
    data_health: DataStatus,
    max_core_items: int = 5,
    max_per_sector: int = 2,
) -> DecisionBrief:
    core = select_core_opportunities(
        core_candidates,
        max_items=max_core_items,
        max_per_sector=max_per_sector,
    )
    holdings = sort_holding_actions(holding_actions)
    mode = ranking_mode_for(core)
    executable_count = sum(
        action.action is ActionState.EXECUTABLE for action in core
    )
    waiting_count = sum(
        action.action in (ActionState.WAIT_PULLBACK, ActionState.WAIT_BREAKOUT)
        for action in core
    )
    holding_attention_count = sum(
        action.action
        in (ActionState.EXIT, ActionState.TRIM, ActionState.WARNING, ActionState.DATA_BLOCKED)
        for action in holdings
    )
    facts = [
        f"{executable_count} 个新机会达到可执行条件",
        f"{waiting_count} 个新机会等待确认",
        f"{holding_attention_count} 个持仓需要处理",
    ]
    if mode is RankingMode.RULE_EVIDENCE:
        facts.append("校准成功概率尚不可用")

    return DecisionBrief(
        as_of=as_of,
        market_posture=market_posture,
        aggression_level=aggression_level,
        core_opportunities=core,
        holding_actions=holdings,
        avoid_reasons=avoid_reasons,
        data_health=data_health,
        ranking_mode=mode,
        summary_facts=tuple(facts),
        ai_summary=None,
    )
