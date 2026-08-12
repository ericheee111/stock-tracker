"""策略触发单元测试（§7.5 / PRD #10）。

验证：
- S1 放量突破：满足趋势/动能/突破区/量能/板块/环境 → 产出 ARMED_BREAKOUT；不满足 → None。
- S2 趋势回踩：趋势向上 + 相对强度够 + 回踩至支撑 + 缩量 → ARMED_PULLBACK；非回踩 → None。
- S3 事件驱动：仅接受注入正面事件（direction=positive 且 weight≥阈值）；无事件/负面/权重不足 → None（禁北向实时，#17.5）。
"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.core.config import StrategyConfig
from stock_tracker.strategies.s1_breakout import S1Breakout
from stock_tracker.strategies.s2_pullback import S2Pullback
from stock_tracker.strategies.s3_event import S3Event, inject_event, clear_events

from tests._common import make_quote, make_bars, make_regime, make_sector


def _ctx(quote, bars, regime=None, sector=None):
    return T.ScanContext(
        symbol=quote.symbol, market=quote.market, quote=quote,
        recent_bars=bars, regime=regime, sector=sector, dq=None, cfg=None,
    )


def _s1_quote(**over):
    # 接近近期高位、放量、温和上涨
    return make_quote(open=147.0, high=149.0, low=140.0, close=148.0, last=148.0,
                      prev_close=145.0, turnover=2.0, amount=1e9, **over)


def _s2_quote(**over):
    # 回踩：与上行 bars 末值一致（约 148），日内偏下、缩量、温和下跌
    return make_quote(open=143.0, high=148.0, low=135.0, close=140.0, last=140.0,
                      prev_close=145.0, turnover=1.5, amount=1e9, **over)


class TestS1Breakout(unittest.TestCase):
    def setUp(self):
        self.s = S1Breakout(StrategyConfig(enabled=True))

    def test_triggers_breakout(self):
        bars = make_bars(n=25, start=100, step=2)  # 陡峭上行，保证趋势/动能
        q = _s1_quote()
        ctx = _ctx(q, bars, make_regime(T.RegimeState.RISK_ON_TREND, 70),
                  make_sector(T.SectorStage.LEADING, 65, rs=70))
        cand = self.s.evaluate(ctx)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.proposed_state, T.SignalState.ARMED_BREAKOUT)
        self.assertGreater(cand.trigger_price, cand.entry_low)
        self.assertGreater(cand.trigger_price, q.last)  # 突破触发价高于现价

    def test_no_trigger_when_flat(self):
        # 横盘 + 环境不利 → 不触发
        bars = make_bars(n=25, start=100, step=0.01)
        q = make_quote(last=100.0, high=100.1, low=99.9, prev_close=100.0, turnover=0.5, amount=1e7)
        ctx = _ctx(q, bars, make_regime(T.RegimeState.RISK_OFF, 30),
                  make_sector(T.SectorStage.DECLINE, 30, rs=20))
        self.assertIsNone(self.s.evaluate(ctx))

    def test_disabled(self):
        s = S1Breakout(StrategyConfig(enabled=False))
        self.assertIsNone(s.evaluate(_ctx(_s1_quote(), make_bars())))


class TestS2Pullback(unittest.TestCase):
    def setUp(self):
        self.s = S2Pullback(StrategyConfig(enabled=True))

    def test_triggers_pullback(self):
        bars = make_bars(n=25, start=100, step=1)  # 趋势向上（末值 124，低于 quote 现价 → 趋势分达标）
        q = _s2_quote()
        ctx = _ctx(q, bars, make_regime(T.RegimeState.ROTATION, 55),
                  make_sector(T.SectorStage.ACCUMULATION, 60, rs=75))
        cand = self.s.evaluate(ctx)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.proposed_state, T.SignalState.ARMED_PULLBACK)
        self.assertGreater(cand.trigger_price, q.last)

    def test_no_trigger_when_not_pullback(self):
        # 当日强势上涨、价格贴近高位 → 不满足回踩（near_support 失败）
        bars = make_bars(n=25, start=100, step=2)
        q = make_quote(open=146.0, high=152.0, low=140.0, close=150.0, last=150.0,
                       prev_close=145.0, turnover=3.5, amount=1e9)
        ctx = _ctx(q, bars, make_regime(T.RegimeState.ROTATION, 55),
                  make_sector(T.SectorStage.LEADING, 65, rs=75))
        self.assertIsNone(self.s.evaluate(ctx))

    def test_disabled(self):
        s = S2Pullback(StrategyConfig(enabled=False))
        self.assertIsNone(s.evaluate(_ctx(_s2_quote(), make_bars())))


class TestS3Event(unittest.TestCase):
    def setUp(self):
        clear_events()
        self.s = S3Event(StrategyConfig(enabled=True))

    def tearDown(self):
        clear_events()

    def test_no_event_no_trigger(self):
        ctx = _ctx(make_quote(), make_bars())
        self.assertIsNone(self.s.evaluate(ctx))

    def test_positive_injected_event_triggers(self):
        inject_event("600519.SH", {"direction": "positive", "weight": 0.5,
                                    "event_type": "季报", "published_at": "2026-08-12"})
        ctx = _ctx(make_quote(symbol="600519.SH"), make_bars())
        cand = self.s.evaluate(ctx)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.proposed_state, T.SignalState.ARMED_BREAKOUT)
        self.assertIn("事件", cand.reason)

    def test_negative_direction_no_trigger(self):
        inject_event("600519.SH", {"direction": "negative", "weight": 0.8,
                                    "event_type": "利空", "published_at": "2026-08-12"})
        ctx = _ctx(make_quote(symbol="600519.SH"), make_bars())
        self.assertIsNone(self.s.evaluate(ctx))

    def test_low_weight_no_trigger(self):
        inject_event("600519.SH", {"direction": "positive", "weight": 0.1,
                                    "event_type": "弱事件", "published_at": "2026-08-12"})
        ctx = _ctx(make_quote(symbol="600519.SH"), make_bars())
        self.assertIsNone(self.s.evaluate(ctx))

    def test_disabled(self):
        clear_events()
        s = S3Event(StrategyConfig(enabled=False))
        inject_event("600519.SH", {"direction": "positive", "weight": 0.5,
                                    "event_type": "季报", "published_at": "2026-08-12"})
        self.assertIsNone(s.evaluate(_ctx(make_quote(symbol="600519.SH"), make_bars())))


if __name__ == "__main__":
    unittest.main()
