"""四分数聚合单元测试（§7.3 / PRD #11）。

验证：
- 四个分数均在 [0,100]。
- success_probability 为 None（Phase1 不伪装概率）。
- 权重归一（opportunity 公式系数和为 1，且含风险惩罚项）。
- 超买惩罚 OverextensionPenalty 生效（P0 反 FOMO）：追高 → 风险↑ → 机会↓。
- DQ 分数影响置信度。
"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.features import evidence as E
from stock_tracker.signals import scoring as SC
from tests._common import (make_quote, make_bars, make_regime, make_sector,
                            make_ctx)


class TestScoreBounds(unittest.TestCase):
    def test_all_in_range(self):
        ctx = make_ctx(quote=make_quote(), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        ss = SC.score(ctx)
        for name in ("opportunity", "timing", "risk", "confidence"):
            val = getattr(ss, name)
            self.assertIsInstance(val, int)
            self.assertTrue(0 <= val <= 100, f"{name}={val} 越界")

    def test_success_probability_none(self):
        ctx = make_ctx(quote=make_quote(), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        ss = SC.score(ctx)
        self.assertIsNone(ss.success_probability)


class TestWeightsNormalized(unittest.TestCase):
    def test_opportunity_recomputation_matches(self):
        """回归验证：opportunity 与文档公式（含风险惩罚）一致。"""
        q = make_quote()
        bars = make_bars()
        regime = make_regime(state=T.RegimeState.ROTATION, score=55.0)
        sector = make_sector(stage=T.SectorStage.LEADING, score=65.0, rs=60.0)
        ctx = make_ctx(quote=q, bars=bars, regime=regime, sector=sector)
        ss = SC.score(ctx)
        ev = E.compute_evidence(q, bars, regime, sector)

        rs = ev.relative_strength
        trend_mom = (ev.trend + ev.momentum) / 2.0
        sector_ctx = sector.score
        catalyst = 70.0 if (sector is not None and sector.catalyst) else 50.0
        vol = ev.volume_liquidity
        structure = ev.price_structure
        regime_fit = regime.market_score
        persistence = sector.persistence

        # 复刻 _compute_risk
        risk = 50.0
        if q.high > q.low and q.last > 0:
            gain_low = (q.last - q.low) / q.last
            risk += SC._clamp(gain_low * 200.0 - 20.0, -10.0, 30.0)
        if sector is not None:
            risk += SC._clamp(sector.crowding * 0.3, 0.0, 20.0)
        if regime is not None:
            risk += {"RISK_OFF": 15.0, "OVERHEATED": 10.0, "PANIC_REBOUND": 8.0,
                     "ROTATION": 0.0, "RISK_ON_TREND": -5.0}.get(regime.regime.value, 0.0)
        if q.prev_close > 0 and q.high > q.low:
            rng = (q.high - q.low) / q.prev_close * 100.0
            risk += SC._clamp(rng - 2.0, 0.0, 20.0)
        risk = SC._clamp(risk)

        expected = (0.20 * rs + 0.15 * trend_mom + 0.15 * sector_ctx
                    + 0.15 * catalyst + 0.10 * vol + 0.10 * structure
                    + 0.10 * regime_fit + 0.05 * persistence)
        expected -= max(0.0, (risk - 60.0)) * 0.3
        expected = SC._clamp(expected)

        self.assertEqual(ss.opportunity, int(round(expected)))
        self.assertEqual(ss.risk, int(round(risk)))


class TestOverextensionPenalty(unittest.TestCase):
    """P0 反 FOMO：追高惩罚。

    两组仅 low 不同（last/均线/动量完全相同），overextended 组的日内涨幅
    （last 相对 low）极大 → 风险↑；尽管其 price_structure 也更高，但风险惩罚
    （(risk-60)*0.3）占主导 → 机会分更低。
    """

    def _score(self, low):
        q = make_quote(open=100.0, high=110.0, low=low, last=102.0,
                       prev_close=100.0, turnover=2.0, amount=1e9)
        ctx = make_ctx(quote=q, bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        return SC.score(ctx)

    def test_risk_higher_when_overextended(self):
        normal = self._score(low=100.0)
        over = self._score(low=20.0)
        self.assertGreater(over.risk, normal.risk)
        self.assertGreaterEqual(over.risk, 70)  # 惩罚项已激活

    def test_opportunity_lower_when_overextended(self):
        normal = self._score(low=100.0)
        over = self._score(low=20.0)
        self.assertLess(over.opportunity, normal.opportunity)

    def test_high_risk_flagged_in_negative_reasons(self):
        over = self._score(low=20.0)
        joined = " ".join(over.negative_reasons)
        self.assertIn("风险", joined)


class TestDQInfluencesConfidence(unittest.TestCase):
    def test_low_dq_lowers_confidence(self):
        q = make_quote()
        bars = make_bars()
        regime = make_regime()
        sector = make_sector()
        ctx_good = make_ctx(quote=q, bars=bars, regime=regime, sector=sector,
                             dq=T.DataQuality(T.QualityStatus.VALID, 100, []))
        ctx_bad = make_ctx(quote=q, bars=bars, regime=regime, sector=sector,
                            dq=T.DataQuality(T.QualityStatus.VALID, 30, ["质量下降"]))
        good = SC.score(ctx_good)
        bad = SC.score(ctx_bad)
        self.assertLess(bad.confidence, good.confidence)


if __name__ == "__main__":
    unittest.main()
