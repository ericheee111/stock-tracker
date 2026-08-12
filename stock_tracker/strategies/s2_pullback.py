"""S2 趋势回踩（PRD #10.2）。

候选：MA20/MA60 趋势向上；个股/行业相对强度仍高；回撤到支撑区（MA/前高/平台）；
回踩阶段成交缩小。触发：支撑附近止跌 + 重新放量/反包。比直接追突破更有赔率优势。
Phase1 简化：用趋势向上 + 价格回踩至日内偏下/接近支撑 + 量能不放大 → ARMED_PULLBACK。
"""

from __future__ import annotations

from typing import Optional

from ..core import types as T
from .base import SignalCandidate, Strategy


class S2Pullback(Strategy):
    id = "S2"

    def applies_to(self, market: T.Market) -> bool:
        return True

    def evaluate(self, ctx: T.ScanContext) -> Optional[SignalCandidate]:
        if not self.enabled:
            return None
        ev = self._evidence(ctx)
        q = ctx.quote
        params = self.cfg.params

        trend_up = ev.trend >= 55
        rs_ok = ev.relative_strength >= 52
        # 回踩：价格处于日内偏下（last 低于中段）但仍为正/未破位
        day_chg = (q.last / q.prev_close - 1.0) * 100.0 if q.prev_close > 0 else 0.0
        pullback_depth = float(params.get("pullback_depth_pct", 0.06))
        near_support = q.high > 0 and (q.last - q.low) / (q.high - q.low + 1e-9) <= 0.5

        # 量能：回踩缩量（换手不极端高）
        vol_shrank = (q.turnover == 0.0) or (q.turnover <= 3.0)

        if not (trend_up and rs_ok and near_support and vol_shrank):
            return None
        # 回踩不应是破位大跌
        if day_chg < -pullback_depth * 100.0:
            return None

        last = q.last
        support = q.low if q.low > 0 else last * 0.97
        entry_low = round(support * 0.995, 2)
        entry_high = round(last * 1.01, 2)
        trigger = round(max(q.high, last * 1.012), 2)
        invalidation = round(support * 0.97, 2)
        target_1 = round(entry_high * 1.04, 2)
        target_2 = round(entry_high * 1.08, 2)
        rr = self._rr(entry_high, invalidation, target_1)

        sector_name = ctx.sector.sector if ctx.sector else "—"
        reason = (f"趋势回踩：趋势{ev.trend} 相对强度{ev.relative_strength}，"
                  f"回踩至支撑区（量能收缩），板块「{sector_name}」")
        next_trigger = (f"支撑 {support:.2f} 附近缩量止跌，或放量站上 {trigger:.2f} 确认，则升级为可执行；"
                        f"跌破 {invalidation:.2f} 失效")

        return SignalCandidate(
            symbol=ctx.symbol, market=ctx.market, strategy_id=self.id,
            proposed_state=T.SignalState.ARMED_PULLBACK,
            entry_low=entry_low, entry_high=entry_high, trigger_price=trigger,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            reward_risk=round(rr, 2), reason=reason, next_trigger=next_trigger,
            half_life_hours=float(params.get("half_life_hours", 48.0)),
        )
