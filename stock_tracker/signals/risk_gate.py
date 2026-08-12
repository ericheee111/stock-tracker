"""风险闸门（§7.6 / PRD #14 / #23）。

- 追高惩罚 OverextensionPenalty（#14.3）：现价高于近期低点超阈 → 标记 OVEREXTENDED（禁止追高）。
- 最小 R（#14.2）：reward_risk < min_r_multiple → 不触发。
- 组合热度 Portfolio Heat（#23.2）：总热度 > 上限 → 拦截新增 ACTIVE。
- Regime 禁止：regime ∈ blocked_states → 不触发。
- DQ 闸门（§6）：STALE/INVALID/DEGRADED → 阻断强信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core import types as T
from ..core.config import ConfigBundle
from ..data_quality.gate import blocks_strong_signal
from ..strategies.base import SignalCandidate


@dataclass(slots=True)
class RiskDecision:
    """风险闸门结论。"""

    allowed: bool = True            # 是否允许升级为强信号（TRIGGERED/ACTIVE）
    overextended: bool = False      # 是否标记为禁止追高
    reasons: list = None            # type: ignore[assignment]
    block_reason: Optional[str] = None


class RiskGate:
    """风险闸门。"""

    def __init__(self, bundle: ConfigBundle) -> None:
        self.bundle = bundle
        self.r = bundle.risk

    def check(self, candidate: SignalCandidate, scores: T.ScoreSet, ctx: T.ScanContext,
              portfolio_heat_pct: float = 0.0) -> RiskDecision:
        reasons: list[str] = []
        overextended = False

        # ---- DQ 闸门 ----
        if ctx.dq is not None and blocks_strong_signal(ctx.dq):
            msg = f"数据质量不达标（{ctx.dq.status.value}），禁止强信号"
            reasons.append(msg)
            return RiskDecision(False, False, reasons, msg)

        # ---- Regime 禁止 ----
        if ctx.regime is not None and ctx.regime.regime.value in self.r.regime_blocked_states:
            msg = f"市场环境 {ctx.regime.regime.value} 禁止触发"
            reasons.append(msg)
            return RiskDecision(False, False, reasons, msg)

        # ---- 追高惩罚 ----
        q = ctx.quote
        gain_low = (q.last - q.low) / q.last if (q.last > 0 and q.low > 0) else 0.0
        if gain_low > self.r.overextension_max_gain_from_low_pct:
            overextended = True
            reasons.append(
                f"追高惩罚：现价高于日内低点 {gain_low * 100:.0f}%（阈值 "
                f"{self.r.overextension_max_gain_from_low_pct * 100:.0f}%）"
            )
        # 高于入场区上限超阈
        if candidate.entry_high > 0 and q.last > candidate.entry_high * (1 + self.r.overextension_max_above_entry_pct):
            overextended = True
            reasons.append("现价已高于入场区上限，禁止追高")

        # ---- 最小 R ----
        if candidate.reward_risk < self.r.min_r_multiple:
            msg = f"赔率不足：R={candidate.reward_risk:.2f} < 最小 {self.r.min_r_multiple:.2f}"
            reasons.append(msg)
            return RiskDecision(False, overextended, reasons, msg)

        # ---- 组合热度 ----
        if portfolio_heat_pct > self.r.max_heat_pct:
            msg = (f"组合热度 {portfolio_heat_pct * 100:.0f}% 超上限 "
                   f"{self.r.max_heat_pct * 100:.0f}%，暂停新增")
            reasons.append(msg)
            return RiskDecision(False, overextended, reasons, msg)

        if overextended:
            reasons.append("标记为「强势但禁止追高」（OVEREXTENDED）")
        return RiskDecision(True, overextended, reasons, None)
