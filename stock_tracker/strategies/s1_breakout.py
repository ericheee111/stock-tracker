"""S1 放量突破延续（PRD #10.1）。

候选：价格接近近期高位、相对行业强度高、量能/成交额分位提升、板块处于发酵/启动。
触发：突破关键阻力 + 成交确认 + 板块同步维持强势。
禁止（在 risk_gate 统一处理）：距均线过度扩张、跳空越过合理区、赔率过低、流动性不足。
Phase1 简化：用当日/结构证据判定突破区，给出 ARMED_BREAKOUT 候选。
"""

from __future__ import annotations

from typing import Optional

from ..core import types as T
from .base import SignalCandidate, Strategy


class S1Breakout(Strategy):
    id = "S1"

    def applies_to(self, market: T.Market) -> bool:
        return True

    def evaluate(self, ctx: T.ScanContext) -> Optional[SignalCandidate]:
        if not self.enabled:
            return None
        ev = self._evidence(ctx)
        q = ctx.quote
        params = self.cfg.params

        # 板块/环境友好
        stage_ok = ctx.sector is None or ctx.sector.stage in (
            T.SectorStage.ACCUMULATION, T.SectorStage.LEADING)
        regime_ok = ctx.regime is None or ctx.regime.regime in (
            T.RegimeState.RISK_ON_TREND, T.RegimeState.ROTATION, T.RegimeState.OVERHEATED)

        trend_ok = ev.trend >= 55
        mom_ok = ev.momentum >= 50

        # 突破区：当日接近最高或创近期高（structure 偏高）
        near_high_pct = float(params.get("near_high_pct", 0.03))
        day_chg = (q.last / q.prev_close - 1.0) * 100.0 if q.prev_close > 0 else 0.0
        structure_break = ev.price_structure >= 60
        near_high = q.high > 0 and (q.high - q.last) / q.high <= near_high_pct

        vol_ok = (q.turnover > 1.0) or (q.amount >= 5e8)

        if not (trend_ok and mom_ok and (structure_break or near_high) and vol_ok):
            return None
        if not (stage_ok and regime_ok):
            return None

        # 构建候选
        last = q.last
        entry_low = round(last * 0.985, 2)
        entry_high = round(last * 1.015, 2)
        trigger = round(max(q.high, last * (1 + near_high_pct)), 2)
        invalidation = round(entry_low * 0.97, 2)
        target_1 = round(entry_high * 1.03, 2)
        target_2 = round(entry_high * 1.06, 2)
        rr = self._rr(entry_high, invalidation, target_1)

        sector_name = ctx.sector.sector if ctx.sector else "—"
        reason = (f"放量突破区形成：趋势{ev.trend} 动能{ev.momentum} 结构{ev.price_structure}，"
                  f"板块「{sector_name}」处于{ctx.sector.stage.value if ctx.sector else '—'}")
        next_trigger = (f"放量站上 {trigger:.2f} 且板块维持强势，则升级为可执行；"
                        f"跌破 {invalidation:.2f} 逻辑失效")

        return SignalCandidate(
            symbol=ctx.symbol, market=ctx.market, strategy_id=self.id,
            proposed_state=T.SignalState.ARMED_BREAKOUT,
            entry_low=entry_low, entry_high=entry_high, trigger_price=trigger,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            reward_risk=round(rr, 2), reason=reason, next_trigger=next_trigger,
            half_life_hours=float(params.get("half_life_hours", 48.0)),
        )
