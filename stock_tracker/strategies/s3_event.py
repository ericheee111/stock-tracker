"""S3 事件驱动延续（PRD #10.3 / #9 / #17.5）。

**Phase1 轻量占位**：仅接受**注入/占位事件**，绝不接入盘中实时北向净流入作强信号
（PRD #17.5 硬规则）。事件经 ``inject_event`` 注入（如盘后披露、季报、政策落地），
且需 direction=positive、weight ≥ event_min_weight 才可能产出候选。

默认无任何注入事件 → ``evaluate`` 返回 ``None``（不误触发）。北向实时流在 Phase1 完全不接入。
"""

from __future__ import annotations

from typing import Optional

from ..core import types as T
from .base import SignalCandidate, Strategy

# 注入事件存储（仅占位/注入，绝不含实时北向）
_INJECTED: dict[str, list[dict]] = {}


def inject_event(symbol: str, event: dict) -> None:
    """注入一个事件（外部/盘后）。event 含 direction/weight/published_at 等。"""
    _INJECTED.setdefault(symbol, []).append(event)


def clear_events(symbol: Optional[str] = None) -> None:
    if symbol is None:
        _INJECTED.clear()
    else:
        _INJECTED.pop(symbol, None)


class S3Event(Strategy):
    id = "S3"

    def applies_to(self, market: T.Market) -> bool:
        return True

    def evaluate(self, ctx: T.ScanContext) -> Optional[SignalCandidate]:
        if not self.enabled:
            return None
        events = _INJECTED.get(ctx.symbol, [])
        min_w = float(self.cfg.params.get("event_min_weight", 0.30))
        # 仅取正面、已确认、权重达标的注入事件
        qual = [e for e in events
                if e.get("direction") == "positive"
                and float(e.get("weight", 0.0)) >= min_w]
        if not qual:
            return None  # 无有效注入事件 → 不触发（#17.5 禁北向实时）

        ev = self._evidence(ctx)
        q = ctx.quote
        last = q.last
        entry_low = round(last * 0.97, 2)
        entry_high = round(last * 1.03, 2)
        trigger = round(last * 1.02, 2)
        invalidation = round(last * 0.95, 2)
        target_1 = round(entry_high * 1.05, 2)
        target_2 = round(entry_high * 1.10, 2)
        rr = self._rr(entry_high, invalidation, target_1)

        cat = qual[0].get("event_type", "事件")
        reason = f"事件驱动延续：检测到注入正面事件「{cat}」（占位，非北向实时）"
        next_trigger = (f"事件后 1–3 日承接确认、回踩不破 {invalidation:.2f}，"
                        f"放量站上 {trigger:.2f} 则触发")

        return SignalCandidate(
            symbol=ctx.symbol, market=ctx.market, strategy_id=self.id,
            proposed_state=T.SignalState.ARMED_BREAKOUT,
            entry_low=entry_low, entry_high=entry_high, trigger_price=trigger,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            reward_risk=round(rr, 2), reason=reason, next_trigger=next_trigger,
            half_life_hours=float(self.cfg.params.get("half_life_hours", 72.0)),
        )
