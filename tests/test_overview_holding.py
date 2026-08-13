"""get_overview 返回 holding_signals（收市态面板数据契约）测试。

验证：
- overview 含 ``holding_signals`` 键，且**仅含活跃态**信号（WATCH/ARMED/TRIGGERED/
  ACTIVE/TRIM/OVEREXTENDED；EXIT/COLD/INVALIDATED 等不出现）。
- 每项含 horizon 维度（key 非空），并补发 name（Signal 无 name 字段）。
- 不取前 12，呈现完整活跃持仓跨度。
"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       StrategiesConfig, RiskConfig, ProviderConfig)
from stock_tracker.api.handlers import AppContext, get_overview

from tests._common import _ROOT, make_quote


class _FakeStore:
    def __init__(self, signals, quotes=None):
        self._signals = {s.signal_id: s for s in signals}
        self._quotes = quotes or {}

    def get_signals(self):
        return dict(self._signals)

    def get_signals_by_symbol(self, symbol):
        return [s for s in self._signals.values() if s.symbol == symbol]

    def get_quote(self, symbol):
        return self._quotes.get(symbol)

    def get_quotes(self):
        return dict(self._quotes)

    def get_regime(self):
        return None

    def active_signal_states(self):
        return (T.SignalState.WATCH, T.SignalState.ARMED_BREAKOUT,
                T.SignalState.ARMED_PULLBACK, T.SignalState.TRIGGERED,
                T.SignalState.ACTIVE, T.SignalState.TRIM, T.SignalState.OVEREXTENDED)


class _FakeRepo:
    def load_recent_bars(self, symbol, interval="1d", n=260):
        return []


def _make_signal(sid, symbol, strategy_id, state):
    return T.Signal(signal_id=sid, symbol=symbol, market=T.Market.A,
                    strategy_id=strategy_id, state=state,
                    state_changed_at=datetime.now(),
                    scores=T.ScoreSet(opportunity=70))


class TestOverviewHolding(unittest.TestCase):
    def _ctx(self, signals):
        bundle = ConfigBundle(
            app=AppConfig(root_dir=_ROOT, markets_enabled={"a": True, "hk": True, "us": True}),
            markets=MarketsConfig(), strategies=StrategiesConfig(),
            providers=[ProviderConfig(name="tencent", primary=True, markets=["a", "hk", "us"])],
            risk=RiskConfig())
        store = _FakeStore(signals,
                           quotes={s.symbol: make_quote(symbol=s.symbol) for s in signals})
        return AppContext(bundle=bundle, store=store, repo=_FakeRepo(),
                          router=SimpleNamespace(health_list=lambda: []),
                          signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
                          sse_hub=SimpleNamespace(), web_root="web")

    def test_holding_signals_present_and_filtered(self):
        signals = [
            _make_signal("s1", "600519.SH", "S1", T.SignalState.ACTIVE),
            _make_signal("s2", "000001.SZ", "BASE", T.SignalState.WATCH),
            _make_signal("s3", "300750.SZ", "S2", T.SignalState.EXIT),  # 非活跃态
            _make_signal("s4", "000002.SZ", "S1", T.SignalState.COLD),  # 非活跃态
        ]
        ov = get_overview(self._ctx(signals))
        self.assertIn("holding_signals", ov)
        hs = ov["holding_signals"]
        syms = [s["symbol"] for s in hs]
        self.assertIn("600519.SH", syms)
        self.assertIn("000001.SZ", syms)
        self.assertNotIn("300750.SZ", syms)
        self.assertNotIn("000002.SZ", syms)

    def test_each_has_horizon_and_name(self):
        signals = [_make_signal("s1", "600519.SH", "S1", T.SignalState.ACTIVE)]
        ov = get_overview(self._ctx(signals))
        hs = ov["holding_signals"]
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0]["horizon"]["key"], "SHORT")
        self.assertTrue(hs[0].get("name"))  # 补发名称（Signal 无 name 字段）

    def test_holding_signals_carry_crowding_and_indicators(self):
        signals = [_make_signal("s1", "600519.SH", "S1", T.SignalState.ACTIVE)]
        ov = get_overview(self._ctx(signals))
        hs = ov["holding_signals"]
        self.assertEqual(len(hs), 1)
        # §24.6 拥挤度仪表已挂载（即便无 K 线退化为安全档，契约要求字段存在）
        self.assertIn("crowding", hs[0])
        self.assertIsInstance(hs[0]["crowding"], dict)
        self.assertIn("score", hs[0]["crowding"])
        self.assertIn("level_key", hs[0]["crowding"])
        # indicators 键存在（§24.6 同源展示指标）
        self.assertIn("indicators", hs[0])

    def test_top_opportunities_carry_crowding(self):
        signals = [_make_signal("s1", "600519.SH", "S1", T.SignalState.ACTIVE)]
        ov = get_overview(self._ctx(signals))
        top = ov["top_opportunities"]
        self.assertTrue(top)
        self.assertIn("crowding", top[0])
        self.assertIsInstance(top[0]["crowding"], dict)
        self.assertIn("score", top[0]["crowding"])


if __name__ == "__main__":
    unittest.main()
