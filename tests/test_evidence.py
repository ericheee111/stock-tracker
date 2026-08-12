"""五大证据族聚合单元测试。

重点验证：
1. 五族分数均在 [0,100]。
2. 五族「去相关」——改变某一族的驱动因子只影响对应族，不污染其他族
   （即同一份价格事实不会被重复计入多个族 → 满足 §7.2 去相关约束）。
"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.features import evidence as E
from tests._common import make_quote, make_bars, make_regime, make_sector


def _evidence(quote=None, bars=None, regime=None, sector=None):
    return E.compute_evidence(
        quote if quote else make_quote(),
        bars if bars is not None else make_bars(),
        regime, sector,
    )


class TestEvidenceBounds(unittest.TestCase):
    def test_all_families_in_range(self):
        ev = _evidence()
        for name in ("trend", "momentum", "relative_strength",
                     "volume_liquidity", "price_structure"):
            val = getattr(ev, name)
            self.assertIsInstance(val, int)
            self.assertTrue(0 <= val <= 100, f"{name}={val} 越界")
        self.assertEqual(set(ev.reasons.keys()), {
            "trend", "momentum", "relative_strength",
            "volume_liquidity", "price_structure",
        })

    def test_empty_bars_fallback(self):
        # 无历史 Bars → 退化为当日涨跌估算，仍应在 [0,100]
        ev = E.compute_evidence(make_quote(), [], None, None)
        for name in ("trend", "momentum", "relative_strength",
                     "volume_liquidity", "price_structure"):
            val = getattr(ev, name)
            self.assertTrue(0 <= val <= 100, f"{name}={val} 越界")


class TestEvidenceNonDoubleCount(unittest.TestCase):
    """去相关 / 不重复计数：每个族的驱动因子独立。"""

    def test_relative_strength_isolated(self):
        base = _evidence(sector=make_sector(rs=50.0))
        better = _evidence(sector=make_sector(rs=90.0))
        # 仅板块相对强度变化 → 只影响 relative_strength，其余四族不变
        self.assertGreater(better.relative_strength, base.relative_strength)
        self.assertEqual(better.trend, base.trend)
        self.assertEqual(better.momentum, base.momentum)
        self.assertEqual(better.volume_liquidity, base.volume_liquidity)
        self.assertEqual(better.price_structure, base.price_structure)

    def test_volume_liquidity_isolated(self):
        q1 = make_quote(turnover=0.5)
        q2 = make_quote(turnover=8.0)
        e1 = _evidence(quote=q1)
        e2 = _evidence(quote=q2)
        self.assertGreater(e2.volume_liquidity, e1.volume_liquidity)
        self.assertEqual(e2.trend, e1.trend)
        self.assertEqual(e2.momentum, e1.momentum)
        self.assertEqual(e2.relative_strength, e1.relative_strength)
        self.assertEqual(e2.price_structure, e1.price_structure)

    def test_price_structure_reflects_intraday_position(self):
        # 日内位置（last 在 [low,high] 的比例）影响 price_structure，
        # 而趋势由 last 相对均线的位置驱动——两者通过 last 关联属正常，
        # 关键去相关点在于「指标不被重复计入多个族」见下。
        q_high = make_quote(open=100.0, high=110.0, low=95.0, last=109.5)
        q_low = make_quote(open=100.0, high=110.0, low=95.0, last=95.5)
        e_high = _evidence(quote=q_high)
        e_low = _evidence(quote=q_low)
        self.assertGreater(e_high.price_structure, e_low.price_structure)

    def test_ma_only_in_trend_not_momentum(self):
        # 去相关结构性验证：MA 只出现在 Trend 族 reason；不出现在 Momentum reason。
        ev = _evidence()
        self.assertIn("MA", ev.reasons["trend"])
        self.assertNotIn("MA", ev.reasons["momentum"])
        self.assertNotIn("均线", ev.reasons["momentum"])

    def test_macd_only_in_momentum_not_trend(self):
        # MACD/hist 只进入 Momentum 族 reason，不污染 Trend 族。
        ev = _evidence()
        self.assertIn("动能", ev.reasons["momentum"])
        self.assertNotIn("MACD", ev.reasons["trend"])
        self.assertNotIn("macd", ev.reasons["trend"])

    def test_ma_and_macd_not_double_counted(self):
        # 综合去相关验证：trend reason 描述 MA20/MA60，绝不提及 MACD/hist。
        ev = _evidence()
        self.assertIn("trend", ev.reasons)
        self.assertIn("momentum", ev.reasons)
        self.assertNotIn("MACD", ev.reasons["trend"])
        self.assertNotIn("macd", ev.reasons["trend"])
        self.assertNotIn("hist", ev.reasons["trend"])


if __name__ == "__main__":
    unittest.main()
