"""单标的详情 API 单元测试（T03）。

验证 handlers.get_quote_detail（/api/quote/{symbol}）：
- 聚合实时报价 + 历史 K 线 + 展示指标；返回 indicators / recent_bars / bar_count。
- 无效 symbol（无 "."）→ 返回 None（服务端据此 400）。
- 仅展示数值，indicators 与 features.build_indicators 同源（纯展示，不评分）。
- 服务端路由正则 _QUOTE_RE 正确捕获 symbol。
"""

import unittest
from types import SimpleNamespace

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       StrategiesConfig, RiskConfig, ProviderConfig)
from stock_tracker.api.handlers import AppContext, get_quote_detail
from stock_tracker.api.server import _QUOTE_RE

from tests._common import _ROOT, make_quote, make_bars


class _FakeStore:
    def __init__(self, quote=None):
        self._q = quote

    def get_quote(self, symbol):
        return self._q

    def get_signals_by_symbol(self, symbol):
        return []

    def get_signals(self):
        return {}


class _FakeRepo:
    def __init__(self, bars):
        self._bars = bars

    def load_recent_bars(self, symbol, interval="1d", n=260):
        return self._bars


def _ctx(symbol: str, bars) -> AppContext:
    bundle = ConfigBundle(
        app=AppConfig(root_dir=_ROOT), markets=MarketsConfig(),
        strategies=StrategiesConfig(), providers=[ProviderConfig(name="tencent")],
        risk=RiskConfig())
    q = make_quote(symbol=symbol)
    return AppContext(bundle=bundle, store=_FakeStore(quote=q), repo=_FakeRepo(bars),
                      router=None, signal_manager=None, sse_hub=SimpleNamespace(),
                      web_root="web")


class TestGetQuoteDetail(unittest.TestCase):
    def test_returns_indicators_and_recent_bars(self):
        symbol = "600519.SH"
        bars = make_bars(80)
        d = get_quote_detail(_ctx(symbol, bars), symbol)
        self.assertIsNotNone(d)
        self.assertEqual(d["symbol"], symbol)
        self.assertIsNotNone(d["quote"])
        self.assertIsNotNone(d["indicators"])
        self.assertIn("ma20", d["indicators"])
        self.assertIn("pos52w", d["indicators"])
        # recent_bars 最多 30 根用于展示，并保留来源/复权/质量元数据。
        self.assertLessEqual(len(d["recent_bars"]), 30)
        self.assertEqual(d["bar_count"], 80)
        latest = d["recent_bars"][-1]
        self.assertIn("source", latest)
        self.assertIn("adjustment_factor", latest)
        self.assertIn("quality_status", latest)

    def test_invalid_symbol_returns_none(self):
        self.assertIsNone(get_quote_detail(_ctx("BADSYMBOL", []), "BADSYMBOL"))

    def test_no_data_returns_dict(self):
        # 有 quote 但无 K 线：仍返回（indicators 为 None）
        d = get_quote_detail(_ctx("600519.SH", []), "600519.SH")
        self.assertIsNotNone(d)
        self.assertIsNone(d["indicators"])
        self.assertEqual(d["bar_count"], 0)

    def test_route_regex_captures_symbol(self):
        m = _QUOTE_RE.match("/api/quote/00700.HK")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "00700.HK")
        # 非 quote 路由不匹配
        self.assertIsNone(_QUOTE_RE.match("/api/overview"))


if __name__ == "__main__":
    unittest.main()
