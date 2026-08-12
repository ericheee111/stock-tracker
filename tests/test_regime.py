"""市场环境状态机（5 态）单元测试。"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.features import regime as R
from tests._common import make_quote


def _q(last, prev, high, low, symbol="600519.SH"):
    return make_quote(symbol=symbol, last=last, prev_close=prev,
                      high=high, low=low, open=prev)


class TestBuildRegime(unittest.TestCase):
    def test_empty(self):
        reg = R.build_regime([])
        self.assertEqual(reg.regime, T.RegimeState.ROTATION)
        self.assertEqual(reg.market_score, 50.0)

    def test_overheated(self):
        # 普涨 + 极端股多 → OVERHEATED
        quotes = [
            _q(110, 100, 111, 99), _q(112, 100, 113, 99),
            _q(108, 100, 109, 99), _q(115, 100, 116, 99),
            _q(120, 100, 121, 99), _q(111, 100, 112, 99),
            _q(109, 100, 110, 99), _q(113, 100, 114, 99),
            _q(117, 100, 118, 99), _q(106, 100, 107, 99),
        ]
        reg = R.build_regime(quotes)
        self.assertEqual(reg.regime, T.RegimeState.OVERHEATED)
        self.assertTrue(0 <= reg.market_score <= 100)

    def test_risk_off(self):
        # 大跌 + 高波动 + 窄幅上涨 → RISK_OFF（非恐慌反弹）
        quotes = [
            _q(90, 100, 91, 88), _q(89, 100, 90, 87),
            _q(91, 100, 92, 88), _q(88, 100, 89, 86),
            _q(92, 100, 93, 89), _q(87, 100, 88, 85),
            _q(90, 100, 91, 88), _q(86, 100, 87, 84),
            _q(93, 100, 94, 90), _q(85, 100, 86, 83),
        ]
        reg = R.build_regime(quotes)
        self.assertEqual(reg.regime, T.RegimeState.RISK_OFF)

    def test_panic_rebound(self):
        # 大跌（avg<-3%）+ 高波动 + 部分反弹（breadth>0.3） → PANIC_REBOUND
        # 注意避开 5% 边界的浮点噪声：上涨股用 +3%，下跌股用 -12%。
        quotes = [
            _q(103, 100, 109, 101), _q(103, 100, 109, 101),
            _q(103, 100, 109, 101), _q(103, 100, 109, 101),
            _q(88, 100, 90, 85), _q(88, 100, 90, 85),
            _q(88, 100, 90, 85), _q(88, 100, 90, 85),
            _q(88, 100, 90, 85), _q(88, 100, 90, 85),
        ]
        reg = R.build_regime(quotes)
        self.assertEqual(reg.regime, T.RegimeState.PANIC_REBOUND)

    def test_risk_on_trend(self):
        # 中强普涨（无股 >5% 触发过热边界） → RISK_ON_TREND
        quotes = [
            _q(102, 100, 103, 101), _q(103, 100, 104, 101),
            _q(101, 100, 102, 100), _q(104, 100, 105, 102),
            _q(102, 100, 103, 101), _q(103, 100, 104, 101),
            _q(101, 100, 102, 100), _q(103, 100, 104, 101),
            _q(104, 100, 105, 102), _q(102, 100, 103, 101),
        ]
        reg = R.build_regime(quotes)
        self.assertEqual(reg.regime, T.RegimeState.RISK_ON_TREND)

    def test_rotation_default(self):
        # 温和、无明显方向 → ROTATION
        quotes = [_q(100, 100, 101, 99), _q(101, 100, 102, 100)]
        reg = R.build_regime(quotes)
        self.assertEqual(reg.regime, T.RegimeState.ROTATION)

    def test_market_score_range(self):
        for st in (T.RegimeState.RISK_ON_TREND, T.RegimeState.ROTATION,
                   T.RegimeState.RISK_OFF, T.RegimeState.PANIC_REBOUND,
                   T.RegimeState.OVERHEATED):
            quotes = [_q(100, 100, 101, 99)]
            reg = R.build_regime(quotes)
            self.assertTrue(0 <= reg.market_score <= 100)


if __name__ == "__main__":
    unittest.main()
