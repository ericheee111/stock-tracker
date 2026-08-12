"""信号状态机（§7.4 / PRD #15 / #24.2 / #24.3）。

12 态迁移（架构状态图）。每次迁移记录 previous_state + reason + what_changed。
- next_trigger：由当前态派生人话（PRD #24.2）。
- what_changed：与上次 ScanContext/Signal 的差异（PRD #24.3）。
- freshness：半衰期衰减 exp(-age/half_life)（PRD #15.2）；armed 超时 → EXPIRED。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..core import types as T
from ..strategies.base import SignalCandidate


# 合法迁移表
VALID_TRANSITIONS: dict[T.SignalState, set[T.SignalState]] = {
    T.SignalState.COLD: {T.SignalState.WATCH, T.SignalState.DATA_INVALID},
    T.SignalState.WATCH: {
        T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK,
        T.SignalState.INVALIDATED, T.SignalState.DATA_INVALID, T.SignalState.EXPIRED,
        T.SignalState.COLD,
    },
    T.SignalState.ARMED_BREAKOUT: {
        T.SignalState.TRIGGERED, T.SignalState.INVALIDATED, T.SignalState.DATA_INVALID,
        T.SignalState.EXPIRED, T.SignalState.WATCH, T.SignalState.OVEREXTENDED,
    },
    T.SignalState.ARMED_PULLBACK: {
        T.SignalState.TRIGGERED, T.SignalState.INVALIDATED, T.SignalState.DATA_INVALID,
        T.SignalState.EXPIRED, T.SignalState.WATCH, T.SignalState.OVEREXTENDED,
    },
    T.SignalState.TRIGGERED: {
        T.SignalState.ACTIVE, T.SignalState.INVALIDATED, T.SignalState.OVEREXTENDED,
        T.SignalState.DATA_INVALID,
    },
    T.SignalState.ACTIVE: {
        T.SignalState.TRIM, T.SignalState.EXIT, T.SignalState.INVALIDATED,
        T.SignalState.OVEREXTENDED, T.SignalState.DATA_INVALID,
    },
    T.SignalState.TRIM: {T.SignalState.EXIT, T.SignalState.ACTIVE, T.SignalState.INVALIDATED},
    T.SignalState.OVEREXTENDED: {
        T.SignalState.ACTIVE, T.SignalState.EXIT, T.SignalState.INVALIDATED,
        T.SignalState.DATA_INVALID,
    },
    T.SignalState.EXIT: set(),
    T.SignalState.INVALIDATED: {T.SignalState.WATCH},
    T.SignalState.DATA_INVALID: {T.SignalState.WATCH},
    T.SignalState.EXPIRED: set(),
}


def freshness(state_changed_at: datetime, half_life_hours: float, now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    age_h = (now - state_changed_at).total_seconds() / 3600.0
    hl = max(0.5, half_life_hours)
    return float(__import__("math").exp(-age_h / hl))


def is_expired(state_changed_at: datetime, half_life_hours: float, now: Optional[datetime] = None) -> bool:
    return freshness(state_changed_at, half_life_hours, now) < 0.15


class SignalStateMachine:
    """信号状态机编排。"""

    def signal_id(self, candidate: SignalCandidate) -> str:
        return f"{candidate.symbol}:{candidate.strategy_id}"

    def decide(self, existing: Optional[T.Signal], candidate: SignalCandidate,
               decision, scores: T.ScoreSet, ctx: T.ScanContext,
               now: Optional[datetime] = None) -> Optional[T.Signal]:
        """依据候选、风险结论与既有信号推导新 Signal；返回 None 表示不创建。"""
        now = now or datetime.now()
        dq_block = ctx.dq is not None and ctx.dq.status in (
            T.QualityStatus.INVALID, T.QualityStatus.STALE, T.QualityStatus.DEGRADED)

        # 新信号基础字段
        proposed = candidate.proposed_state
        new_state = proposed

        if dq_block and proposed in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK,
                                     T.SignalState.WATCH):
            new_state = T.SignalState.DATA_INVALID
        elif decision.overextended and proposed in (
                T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK,
                T.SignalState.TRIGGERED, T.SignalState.ACTIVE):
            new_state = T.SignalState.OVEREXTENDED
        elif (proposed in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK)
              and decision.allowed and ctx.quote.last >= candidate.trigger_price):
            new_state = T.SignalState.TRIGGERED
        elif (proposed in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK)
              and not decision.allowed and decision.block_reason):
            new_state = T.SignalState.WATCH  # 硬性阻断 → 降级观察

        # 既有信号迁移
        if existing is not None:
            prev = existing.state
            # 数据异常优先
            if dq_block and prev in (T.SignalState.WATCH, T.SignalState.ARMED_BREAKOUT,
                                     T.SignalState.ARMED_PULLBACK):
                new_state = T.SignalState.DATA_INVALID
            elif decision.overextended and prev in (
                    T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK,
                    T.SignalState.TRIGGERED, T.SignalState.ACTIVE):
                new_state = T.SignalState.OVEREXTENDED
            elif (prev in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK)
                  and decision.allowed and ctx.quote.last >= candidate.trigger_price):
                new_state = T.SignalState.TRIGGERED
            elif (prev in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK)
                  and not decision.allowed and decision.block_reason):
                new_state = T.SignalState.WATCH
            # 过期
            elif (prev in (T.SignalState.ARMED_BREAKOUT, T.SignalState.ARMED_PULLBACK)
                  and is_expired(existing.state_changed_at, candidate.half_life_hours, now)):
                new_state = T.SignalState.EXPIRED
            else:
                new_state = prev  # 维持原态，仅刷新评分/理由

        # 校验合法迁移
        if existing is not None and new_state != existing.state:
            if existing.state in VALID_TRANSITIONS and new_state not in VALID_TRANSITIONS[existing.state]:
                new_state = existing.state  # 非法迁移回退

        # 构建 Signal
        regime_str = ctx.regime.regime.value if ctx.regime else ""
        sector_str = ctx.sector.stage.value if ctx.sector else ""
        reason = candidate.reason
        if decision.block_reason:
            reason = f"{reason} ｜ 风险闸门：{decision.block_reason}"

        sig = T.Signal(
            signal_id=self.signal_id(candidate),
            symbol=candidate.symbol, market=candidate.market,
            strategy_id=candidate.strategy_id,
            state=new_state,
            state_changed_at=now if (existing is None or new_state != existing.state) else existing.state_changed_at,
            previous_state=existing.state if existing else None,
            reason=reason,
            entry_low=candidate.entry_low, entry_high=candidate.entry_high,
            trigger_price=candidate.trigger_price, invalidation_price=candidate.invalidation_price,
            target_1=candidate.target_1, target_2=candidate.target_2,
            reward_risk=candidate.reward_risk,
            freshness=freshness(now if (existing is None or new_state != existing.state)
                                 else existing.state_changed_at, candidate.half_life_hours, now),
            market_regime=regime_str, sector_stage=sector_str,
            next_trigger=candidate.next_trigger,
            what_changed=self._what_changed(existing, new_state, scores, ctx),
            data_status=ctx.quote.data_status if ctx.quote else T.DataStatus.UNKNOWN,
            scores=scores,
        )
        return sig

    def _what_changed(self, existing: Optional[T.Signal], new_state: T.SignalState,
                      scores: T.ScoreSet, ctx: T.ScanContext) -> list[str]:
        changes: list[str] = []
        if existing is None:
            changes.append(f"新建信号，状态 {new_state.value}")
            return changes
        if existing.state != new_state:
            changes.append(f"状态 {existing.state.value} → {new_state.value}")
        if existing.scores is not None:
            if scores.opportunity != existing.scores.opportunity:
                changes.append(f"机会分 {existing.scores.opportunity} → {scores.opportunity}")
            if scores.risk != existing.scores.risk:
                changes.append(f"风险分 {existing.scores.risk} → {scores.risk}")
        if ctx.sector is not None and existing.sector_stage and existing.sector_stage != ctx.sector.stage.value:
            changes.append(f"板块阶段 {existing.sector_stage} → {ctx.sector.stage.value}")
        if not changes:
            changes.append("无显著变化")
        return changes
