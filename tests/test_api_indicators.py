"""API 指标单元测试（§9.1 / T03）。

验证：
- ``_top_opportunities``：每条机会附 ``indicators``（有 K 线时非 None，dict[str,float|None]）。
- ``get_quote_detail``（/api/quote/{symbol}）：结构含 symbol/market/quote/indicators/
  recent_bars(≤30)/bar_count；非法 symbol（无 "."）返回 None。
- 三者均来自同一 ``build_indicators``（纯展示数值，不评分/不加权）。
"""

import unittest
from types import SimpleNamespace

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       StrategiesConfig, RiskConfig)
from stock_tracker.api.handlers import AppContext, _top_opportunities, get_quote_detail

from tests._common import make_quote, make_bars


class _Store:
    def __init__(self, signals, quote):
        self._signals = signals
        self._quote = quote
    def get_signals(self): return self._signals
    def get_quote(self, sym): return self._quote
    def get_signals_by_symbol(self, sym): return []
    def get_regime(self): return None
    def get_sectors(self): return {}
    def get_last_update(self): return None
    def active_signal_states(self): return set()
    def get_watchlist(self): return {}
    def get_positions(self): return []


class _Repo:
    def __init__(self, bars):
        self._bars = bars  # symbol -> list[Bar]
    def load_recent_bars(self, symbol, interval="1d", n=260):
        return self._bars.get(symbol, [])
    def save_bars_batch(self, bars): return len(bars)
    def prune_bars(self, symbol, interval, keep): return 0


def _bundle():
    return ConfigBundle(app=AppConfig(), markets=MarketsConfig(),
                        strategies=StrategiesConfig(), providers=[], risk=RiskConfig())


class TestApiIndicators(unittest.TestCase):
    def _ctx(self):
        sym = "600519.SH"
        signal = T.Signal(
            symbol=sym, market=T.Market.A, strategy_id="s1",
            state=T.SignalState.ARMED_BREAKOUT,
            scores=T.ScoreSet(opportunity=80, timing=70, risk=20, confidence=75),
        )
        bars = make_bars(n=80, symbol=sym, market=T.Market.A)
        store = _Store({sym: signal}, make_quote(symbol=sym, market=T.Market.A))
        repo = _Repo({sym: bars})
        return AppContext(bundle=_bundle(), store=store, repo=repo,
                          router=SimpleNamespace(), signal_manager=SimpleNamespace(),
                          sse_hub=SimpleNamespace(), web_root="web")

    def test_top_opportunities_has_indicators(self):
        out = _top_opportunities(self._ctx(), limit=5)
        self.assertTrue(out, "应至少有一条机会")
        for it in out:
            self.assertIn("indicators", it)
            self.assertIsNotNone(it["indicators"], "有 K 线应产出 indicators")
            self.assertIsInstance(it["indicators"], dict)

    def test_quote_detail_structure(self):
        d = get_quote_detail(self._ctx(), "600519.SH")
        self.assertIsNotNone(d)
        self.assertEqual(d["symbol"], "600519.SH")
        self.assertEqual(d["market"], "A")
        self.assertIn("quote", d)
        self.assertIn("indicators", d)
        self.assertIn("recent_bars", d)
        self.assertIsInstance(d["recent_bars"], list)
        self.assertLessEqual(len(d["recent_bars"]), 30)
        self.assertIn("bar_count", d)
        self.assertGreaterEqual(d["bar_count"], 1)

    def test_quote_detail_invalid_symbol(self):
        self.assertIsNone(get_quote_detail(self._ctx(), "NODOTSYMBOL"))
        self.assertIsNone(get_quote_detail(self._ctx(), "600519.BAD"))
        self.assertIsNone(get_quote_detail(self._ctx(), ".SH"))

    def test_quote_detail_hk_market_derived(self):
        sym = "00700.HK"
        signal = T.Signal(symbol=sym, market=T.Market.HK, strategy_id="s1",
                          state=T.SignalState.ARMED_BREAKOUT,
                          scores=T.ScoreSet(opportunity=80))
        bars = make_bars(n=80, symbol=sym, market=T.Market.HK)
        store = _Store({sym: signal}, make_quote(symbol=sym, market=T.Market.HK))
        repo = _Repo({sym: bars})
        ctx = AppContext(bundle=_bundle(), store=store, repo=repo,
                         router=SimpleNamespace(), signal_manager=SimpleNamespace(),
                         sse_hub=SimpleNamespace(), web_root="web")
        d = get_quote_detail(ctx, sym)
        self.assertIsNotNone(d)
        self.assertEqual(d["market"], "HK")
        self.assertIsNotNone(d["indicators"])

    def test_top_opportunities_never_crosses_sh_sz_identity(self):
        # 同一数字代码的 .SH/.SZ 是不同证券；精确序列缺失时必须返回空指标。
        requested = "000001.SH"
        different_security = "000001.SZ"
        signal = T.Signal(symbol=requested, market=T.Market.A, strategy_id="s1",
                          state=T.SignalState.ARMED_BREAKOUT,
                          scores=T.ScoreSet(opportunity=80))
        bars = make_bars(n=80, symbol=different_security, market=T.Market.A)
        store = _Store({requested: signal},
                       make_quote(symbol=requested, market=T.Market.A))
        repo = _Repo({different_security: bars})
        ctx = AppContext(bundle=_bundle(), store=store, repo=repo,
                         router=SimpleNamespace(), signal_manager=SimpleNamespace(),
                         sse_hub=SimpleNamespace(), web_root="web")
        out = _top_opportunities(ctx, limit=5)
        self.assertTrue(out)
        self.assertIsNone(out[0]["indicators"])


if __name__ == "__main__":
    unittest.main()
