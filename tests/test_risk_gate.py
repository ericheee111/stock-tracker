"""风险闸门单元测试（§7.6 / PRD #14 / #23）。

验证：
- STALE / INVALID / DEGRADED 数据质量必须阻断强信号。
- 市场环境禁止态阻断。
- 追高惩罚（OverextensionPenalty）标记 OVEREXTENDED。
- 最小 R（赔率不足）阻断。
- 组合热度超限阻断。
- 正常路径放行。
"""

import os
import unittest

from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.signals.risk_gate import RiskGate
from stock_tracker.strategies.base import SignalCandidate
from tests._common import make_quote, make_bars, make_regime, make_sector, make_ctx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLE = load_configs(os.path.join(_ROOT, "config"))


def _candidate(reward_risk: float = 3.0, proposed=T.SignalState.ARMED_BREAKOUT,
               entry_high: float = 105.0) -> SignalCandidate:
    return SignalCandidate(
        symbol="600519.SH", market=T.Market.A, strategy_id="S1",
        proposed_state=proposed, entry_low=99.0, entry_high=entry_high,
        trigger_price=106.0, invalidation_price=98.0, reward_risk=reward_risk,
        reason="测试候选",
    )


def _gate() -> RiskGate:
    return RiskGate(_BUNDLE)


class TestDQBlocksStrongSignal(unittest.TestCase):
    def setUp(self):
        self.gate = _gate()

    def _ctx(self, status):
        dq = T.DataQuality(status, 100 if status == T.QualityStatus.VALID else 40,
                           ["测试原因"])
        return make_ctx(quote=make_quote(), bars=make_bars(),
                        regime=make_regime(), sector=make_sector(), dq=dq)

    def test_stale_blocks(self):
        dec = self.gate.check(_candidate(), _scores(), self._ctx(T.QualityStatus.STALE))
        self.assertFalse(dec.allowed)
        self.assertIsNotNone(dec.block_reason)
        self.assertIn("数据质量", dec.block_reason)

    def test_invalid_blocks(self):
        dec = self.gate.check(_candidate(), _scores(), self._ctx(T.QualityStatus.INVALID))
        self.assertFalse(dec.allowed)

    def test_degraded_blocks(self):
        dec = self.gate.check(_candidate(), _scores(), self._ctx(T.QualityStatus.DEGRADED))
        self.assertFalse(dec.allowed)

    def test_valid_passes_dq(self):
        dec = self.gate.check(_candidate(), _scores(), self._ctx(T.QualityStatus.VALID))
        # DQ 不阻断，但可能因其他规则（如 R / 热度）而拦截，此处仅验证 DQ 未触发 block
        self.assertNotIn("数据质量", dec.reasons)


def _scores() -> T.ScoreSet:
    return T.ScoreSet(opportunity=70, timing=70, risk=40, confidence=70,
                      success_probability=None, positive_reasons=[], negative_reasons=[])


class TestRegimeBlock(unittest.TestCase):
    def test_blocked_regime(self):
        bundle = load_configs(os.path.join(_ROOT, "config"))
        bundle.risk.regime_blocked_states = ["RISK_OFF"]
        gate = RiskGate(bundle)
        ctx = make_ctx(quote=make_quote(), bars=make_bars(),
                       regime=make_regime(state=T.RegimeState.RISK_OFF),
                       sector=make_sector())
        dec = gate.check(_candidate(), _scores(), ctx)
        self.assertFalse(dec.allowed)
        self.assertIn("市场环境", dec.block_reason)


class TestOverextension(unittest.TestCase):
    def test_marked_overextended(self):
        gate = _gate()
        # last 远高于日内低点 → 追高惩罚触发
        q = make_quote(open=100.0, high=200.0, low=100.0, last=195.0,
                       prev_close=100.0)
        ctx = make_ctx(quote=q, bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        dec = gate.check(_candidate(reward_risk=3.0), _scores(), ctx)
        self.assertTrue(dec.overextended)
        self.assertIn("追高", dec.reasons[0])  # 第一条为追高说明

    def test_not_overextended_normal(self):
        gate = _gate()
        q = make_quote(open=100.0, high=110.0, low=100.0, last=102.0,
                       prev_close=100.0)
        ctx = make_ctx(quote=q, bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        dec = gate.check(_candidate(reward_risk=3.0), _scores(), ctx)
        self.assertFalse(dec.overextended)


class TestMinR(unittest.TestCase):
    def test_low_r_blocks(self):
        gate = _gate()
        ctx = make_ctx(quote=make_quote(), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        dec = gate.check(_candidate(reward_risk=0.5), _scores(), ctx)
        self.assertFalse(dec.allowed)
        self.assertIn("赔率", dec.block_reason)


class TestPortfolioHeat(unittest.TestCase):
    def test_high_heat_blocks(self):
        gate = _gate()
        ctx = make_ctx(quote=make_quote(), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        dec = gate.check(_candidate(reward_risk=3.0), _scores(), ctx,
                         portfolio_heat_pct=0.50)
        self.assertFalse(dec.allowed)
        self.assertIn("热度", dec.block_reason)


class TestAllowedPath(unittest.TestCase):
    def test_all_good_allows(self):
        gate = _gate()
        q = make_quote(open=100.0, high=110.0, low=100.0, last=102.0,
                       prev_close=100.0)
        ctx = make_ctx(quote=q, bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        dec = gate.check(_candidate(reward_risk=3.0), _scores(), ctx,
                         portfolio_heat_pct=0.0)
        self.assertTrue(dec.allowed)
        self.assertFalse(dec.overextended)


if __name__ == "__main__":
    unittest.main()
