"""TencentProvider.fetch_bars（兜底 K 线解析）单元测试（T01/T04）。

通过 monkeypatch ``_request`` 返回构造好的腾讯 kline JSON，**不发起真实 HTTP**。
验证：
- A 股：成交量「手」×100 → 股；列表顺序 [日期,开,收,高,低,量]。
- 港股/美股：成交量已是「股」×1；美股经 ``.OQ`` 后缀查询码。
- ``supports_bars()`` 仍为 False（默认 OFF 契约，仅经 ``bars_fallback`` 兜底）。
- 空 / 无 data → 返回 ``[]``（不抛、不阻塞）。
- 单行解析失败（非列表 / 字段不足）被跳过，不中断整批。
"""

import json
import unittest
from datetime import datetime

from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig
from stock_tracker.collector.tencent import TencentProvider


def _tc_cfg(name="tencent", markets=("a", "hk", "us")):
    return ProviderConfig(name=name, cls="TencentProvider", markets=list(markets),
                          primary=True, timeout_ms=3000, max_rps=5)


def _kline_payload(prov_sym, rows):
    return json.dumps({"code": 0, "msg": "", "data": {prov_sym: {"qfqday": rows}}}).encode("utf-8")


class _Patched:
    """临时替换实例方法 ``_request`` 为返回固定字节。"""

    def __init__(self, provider, payload: bytes):
        self.provider = provider
        self.payload = payload
        self._orig = provider._request

    def __enter__(self):
        self.provider._request = lambda url, headers=None: self.payload
        return self.provider

    def __exit__(self, *exc):
        self.provider._request = self._orig


class TestTencentFetchBars(unittest.TestCase):
    def _provider(self):
        return TencentProvider(_tc_cfg())

    def test_supports_bars_false_contract(self):
        # 默认 OFF 契约保持：supports_bars=False；仅经 bars_fallback 配置兜底
        self.assertFalse(self._provider().supports_bars())

    def test_ashare_volume_scaled_by_100(self):
        # 列表顺序：日期,开,收,高,低,成交量(手)
        rows = [["2024-01-02", "100.0", "105.0", "110.0", "95.0", "12000"]]
        with _Patched(self._provider(), _kline_payload("sh600519", rows)) as p:
            bars = p.fetch_bars("600519.SH", T.Market.A, interval="1d")
        self.assertEqual(len(bars), 1)
        b = bars[0]
        self.assertEqual(b.symbol, "600519.SH")
        self.assertEqual(b.market, T.Market.A)
        self.assertEqual(b.open, 100.0)
        self.assertEqual(b.close, 105.0)
        self.assertEqual(b.high, 110.0)
        self.assertEqual(b.low, 95.0)
        # A 股手→股：12000 * 100 = 1_200_000
        self.assertEqual(b.volume, 1_200_000)
        self.assertEqual(b.source, "tencent")
        self.assertEqual(b.timestamp, datetime(2024, 1, 2))

    def test_hk_volume_not_scaled(self):
        rows = [["2024-01-02", "400.0", "405.0", "410.0", "395.0", "8000000"]]
        with _Patched(self._provider(), _kline_payload("hk00700", rows)) as p:
            bars = p.fetch_bars("00700.HK", T.Market.HK, interval="1d")
        self.assertEqual(len(bars), 1)
        # 港股已是股，×1
        self.assertEqual(bars[0].volume, 8_000_000)

    def test_us_uses_oq_suffix_and_not_scaled(self):
        # 美股经 us+CODE.OQ 查询；data 节点键为 usAAPL.OQ
        rows = [["2024-01-02", "180.0", "185.0", "190.0", "175.0", "50000000"]]
        with _Patched(self._provider(), _kline_payload("usAAPL.OQ", rows)) as p:
            bars = p.fetch_bars("AAPL.US", T.Market.US, interval="1d")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].market, T.Market.US)
        self.assertEqual(bars[0].volume, 50_000_000)

    def test_unsupported_contract_values_fail_closed(self):
        provider = self._provider()
        with self.assertRaisesRegex(ValueError, "interval='1d'"):
            provider.fetch_bars("600519.SH", T.Market.A, interval="5m")
        with self.assertRaisesRegex(ValueError, "adjust='qfq'"):
            provider.fetch_bars("600519.SH", T.Market.A, adjust="raw")

    def test_empty_data_returns_empty_list(self):
        with _Patched(self._provider(), json.dumps({"code": 0, "msg": "", "data": {}}).encode()) as p:
            self.assertEqual(p.fetch_bars("600519.SH", T.Market.A), [])

    def test_malformed_row_skipped(self):
        rows = [
            ["2024-01-02", "100.0", "105.0", "110.0", "95.0", "12000"],
            "not-a-list",  # 非列表 → 跳过
            ["2024-01-03", "101.0", "106.0", "111.0", "96.0", "13000"],
        ]
        with _Patched(self._provider(), _kline_payload("sh600519", rows)) as p:
            bars = p.fetch_bars("600519.SH", T.Market.A)
        # 仅 2 根合法（中间坏行被跳过）
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1].timestamp, datetime(2024, 1, 3))


if __name__ == "__main__":
    unittest.main()
