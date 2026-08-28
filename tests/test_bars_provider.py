"""K 线采集（fetch_bars）单元测试（T01）。

验证：
- EastmoneyProvider.fetch_bars：解析 push2his klines；A 股成交量 手×100→股；
  字段顺序（date,开,收,高,低,...）；空/无 data → 返回 []（不抛、不阻塞）。
- TencentProvider.supports_bars：默认 OFF（False），Router 自动跳过。
- ProviderRouter.fetch_bars：仅选 supports_bars() 且市场匹配的源（主 eastmoney），
  失败上抛给调度层吸收。
"""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       StrategiesConfig, RiskConfig, ProviderConfig)
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.collector.router import ProviderRouter

from tests._common import _ROOT


def _em_klines(n: int = 3) -> list[str]:
    base = datetime(2024, 1, 1)
    out = []
    for i in range(n):
        d = base + timedelta(days=i)
        c = 100 + i
        # 顺序：日期,开,收,高,低,成交量,成交额,换手%
        out.append(f"{d.strftime('%Y-%m-%d')},{c - 1},{c},{c + 1},{c - 1},1000,100000,1.5")
    return out


def _em_payload(n: int = 3) -> dict:
    return {"rc": 0, "data": {"code": "600519", "klines": _em_klines(n)}}


def _em_cfg(markets=("a", "hk", "us")):
    return ProviderConfig(name="eastmoney", cls="EastmoneyProvider", markets=list(markets),
                          primary=False, supports_snapshot=True, timeout_ms=3000, max_rps=3)


class TestEastmoneyFetchBars(unittest.TestCase):
    def _patch(self, provider, payload):
        provider._request_research = lambda url: json.dumps(payload).encode("utf-8")
        return provider

    def test_parse_klines_ashare_volume_x100(self):
        p = EastmoneyProvider(_em_cfg())
        self._patch(p, _em_payload(3))
        bars = p.fetch_bars("600519.SH", T.Market.A, interval="1d")
        self.assertEqual(len(bars), 3)
        b0 = bars[0]
        self.assertEqual(b0.symbol, "600519.SH")
        self.assertEqual(b0.market, T.Market.A)
        self.assertEqual(b0.source, "eastmoney")
        self.assertEqual(b0.close, 100.0)
        # A 股成交量：手 ×100 → 股
        self.assertEqual(b0.volume, 1000 * 100)
        # 字段顺序：收(100) 在 开(99) 之后
        self.assertEqual(b0.open, 99.0)
        self.assertEqual(b0.high, 101.0)

    def test_hk_volume_not_multiplied(self):
        p = EastmoneyProvider(_em_cfg())
        self._patch(p, _em_payload(2))
        bars = p.fetch_bars("00700.HK", T.Market.HK, interval="1d")
        self.assertEqual(len(bars), 2)
        # 港/美成交量已是股，不乘 100
        self.assertEqual(bars[0].volume, 1000)

    def test_empty_data_returns_empty_list(self):
        p = EastmoneyProvider(_em_cfg())
        self._patch(p, {"rc": 0, "data": None})
        self.assertEqual(p.fetch_bars("X.SH", T.Market.A), [])

    def test_rc_nonzero_returns_empty(self):
        p = EastmoneyProvider(_em_cfg())
        self._patch(p, {"rc": 1, "data": {"klines": ["2024-01-01,1,2,3,4,5,6,7"]}})
        self.assertEqual(p.fetch_bars("X.SH", T.Market.A), [])

    def test_bad_line_skipped(self):
        p = EastmoneyProvider(_em_cfg())
        payload = {"rc": 0, "data": {"klines": ["short,line", "2024-01-01,1,2,3,0.5,5,6,7"]}}
        self._patch(p, payload)
        bars = p.fetch_bars("X.SH", T.Market.A)
        self.assertEqual(len(bars), 1)


class TestTencentBarsOffByDefault(unittest.TestCase):
    def test_supports_bars_false(self):
        cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a", "hk", "us"],
                             primary=True, timeout_ms=3000, max_rps=5)
        self.assertFalse(TencentProvider(cfg).supports_bars())


class _MockBarsProvider:
    """支持 K 线采集的 Provider 替身，供 Router 路由测试。"""

    def __init__(self, cfg: ProviderConfig, bars: list):
        self.cfg = cfg
        self.name = cfg.name
        self.timeout = 3.0
        self._rl = SimpleNamespace(hits=0)
        self._bars = bars
        self.calls = 0

    def applies_to(self, market: T.Market) -> bool:
        return market in [T.Market(m.upper()) for m in self.cfg.markets]

    def supports_bars(self) -> bool:
        return True

    def fetch_bars(self, symbol, market, interval="1d", start=None, end=None, adjust="qfq"):
        self.calls += 1
        return self._bars


class TestRouterBarsRouting(unittest.TestCase):
    def _router(self, providers):
        bundle = ConfigBundle(app=AppConfig(root_dir=_ROOT), markets=MarketsConfig(),
                               strategies=StrategiesConfig(), providers=providers, risk=RiskConfig())
        return ProviderRouter(bundle, providers)

    def test_selects_supports_bars_provider(self):
        em_cfg = _em_cfg()
        tc_cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a", "hk", "us"],
                                primary=True, timeout_ms=3000, max_rps=5)
        em = _MockBarsProvider(em_cfg, [T.Bar(symbol="600519.SH", market=T.Market.A,
                                              timestamp=datetime(2024, 1, 1), close=100.0)])
        tc = _MockBarsProvider(tc_cfg, [])  # supports_bars 由替身覆盖
        tc.supports_bars = lambda: False
        router = self._router([em, tc])
        bars = router.fetch_bars("600519.SH", T.Market.A)
        self.assertEqual(em.calls, 1)
        self.assertEqual(tc.calls, 0)
        self.assertEqual(len(bars), 1)

    def test_no_bars_provider_raises(self):
        tc_cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a"],
                                primary=True, timeout_ms=3000, max_rps=5)
        tc = _MockBarsProvider(tc_cfg, [])
        tc.supports_bars = lambda: False
        router = self._router([tc])
        with self.assertRaises(RuntimeError):
            router.fetch_bars("600519.SH", T.Market.A)

    def test_bars_failure_propagates(self):
        em_cfg = _em_cfg()
        em = _MockBarsProvider(em_cfg, [])

        def _boom(*a, **k):
            raise ConnectionError("push2his down")
        em.fetch_bars = _boom
        router = self._router([em])
        with self.assertRaises(ConnectionError):
            router.fetch_bars("600519.SH", T.Market.A)


class TestRouterBarsFallback(unittest.TestCase):
    def _router(self, providers):
        bundle = ConfigBundle(app=AppConfig(root_dir=_ROOT), markets=MarketsConfig(),
                               strategies=StrategiesConfig(), providers=providers, risk=RiskConfig())
        return ProviderRouter(bundle, providers)

    def test_falls_back_to_bars_fallback_provider(self):
        # 东财（supports_bars=True，主源）失败时，腾讯（supports_bars=False 但 bars_fallback=True）兜底
        em_cfg = _em_cfg()
        tc_cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a", "hk", "us"],
                                primary=True, timeout_ms=3000, max_rps=5, bars_fallback=True)

        class _FailingBarsProvider(_MockBarsProvider):
            def fetch_bars(self, symbol, market, interval="1d", start=None, end=None, adjust="qfq"):
                self.calls += 1
                raise ConnectionError("push2his down")

        em = _FailingBarsProvider(em_cfg, [])
        tc = _MockBarsProvider(tc_cfg, [T.Bar(symbol="600519.SH", market=T.Market.A,
                                              timestamp=datetime(2024, 1, 1), close=100.0)])
        tc.supports_bars = lambda: False  # 仅经 bars_fallback 参与
        router = self._router([em, tc])
        bars = router.fetch_bars("600519.SH", T.Market.A)
        self.assertEqual(em.calls, 1)     # 主源先尝试
        self.assertEqual(tc.calls, 1)     # 兜底源被调用
        self.assertEqual(len(bars), 1)

    def test_empty_primary_also_tries_fallback(self):
        em_cfg = _em_cfg()
        tc_cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a"],
                                 primary=True, timeout_ms=3000, max_rps=5, bars_fallback=True)
        em = _MockBarsProvider(em_cfg, [])
        tc = _MockBarsProvider(tc_cfg, [T.Bar(symbol="600519.SH", market=T.Market.A,
                                              timestamp=datetime(2024, 1, 1), close=100.0)])
        tc.supports_bars = lambda: False
        router = self._router([em, tc])

        bars = router.fetch_bars("600519.SH", T.Market.A)

        self.assertEqual(em.calls, 1)
        self.assertEqual(tc.calls, 1)
        self.assertEqual(len(bars), 1)

    def test_all_candidates_empty_returns_empty_without_opening_circuit(self):
        em_cfg = _em_cfg()
        tc_cfg = ProviderConfig(name="tencent", cls="TencentProvider", markets=["a"],
                                 primary=True, timeout_ms=3000, max_rps=5, bars_fallback=True)
        em = _MockBarsProvider(em_cfg, [])
        tc = _MockBarsProvider(tc_cfg, [])
        tc.supports_bars = lambda: False
        router = self._router([em, tc])

        self.assertEqual(router.fetch_bars("600519.SH", T.Market.A), [])
        self.assertEqual(router.trackers[em.name].to_provider_health().error_rate, 0.0)
        self.assertEqual(router.trackers[tc.name].to_provider_health().error_rate, 0.0)


if __name__ == "__main__":
    unittest.main()
