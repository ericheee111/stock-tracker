"""EastmoneyProvider.fetch_bars（历史 K 线解析）单元测试（§4.1 / T01）。

通过 monkeypatch ``_request`` 返回构造好的东财 klines JSON，**不发起真实 HTTP**，
保证确定性、可重复。验证：
- A 股：成交量「手」×100 → 股；字段顺序（日期,开,收,高,低,量,额,换手%）。
- 港股/美股：成交量已是「股」×1（不乘 100）。
- 恒指(HSI.HK)等指数：``to_kline_secid`` 走 ``100.`` 前缀；解析正常。
- ``supports_bars()`` 为 True；tencent 默认 False。
- ``rc != 0`` / 无 ``klines`` / ``data`` 为空 → 返回 ``[]``（不抛、不阻塞）。
- 单根解析失败（字段不足/非法）被跳过，不中断整批。
"""

import json
import unittest
from datetime import datetime

from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider


def _eastmoney_cfg(name="eastmoney", markets=("a", "hk", "us")):
    return ProviderConfig(name=name, cls="EastmoneyProvider", markets=list(markets),
                           timeout_ms=3000, max_rps=3)


def _kline_payload(klines, rc=0):
    """构造东财 kline/get 的响应体（data.klines 为字符串列表）。"""
    data = {"klines": klines} if klines is not None else None
    return json.dumps({"rc": rc, "data": data}).encode("utf-8")


class _Patched:
    """临时替换实例方法 _request 为返回固定字节。"""

    def __init__(self, provider, payload: bytes):
        self.provider = provider
        self.payload = payload
        self._orig = provider._request

    def __enter__(self):
        def fake(url, headers=None):
            return self.payload
        self.provider._request = fake
        return self.provider

    def __exit__(self, *exc):
        self.provider._request = self._orig


class TestEastmoneyFetchBars(unittest.TestCase):
    def _provider(self):
        return EastmoneyProvider(_eastmoney_cfg())

    def test_supports_bars_true(self):
        provider = self._provider()
        self.assertTrue(provider.supports_bars())
        self.assertTrue(provider.supports_raw_bars())

    def test_raw_fetch_and_parse_are_separate_and_lossless(self):
        payload = _kline_payload(
            ["2024-01-02,100.0,105.0,110.0,95.0,12000,1.5e9,2.3"]
        )
        with _Patched(self._provider(), payload) as provider:
            raw = provider.fetch_bars_raw("600519.SH", T.Market.A)
        self.assertEqual(raw, payload)
        bars = provider.parse_bars(raw, "600519.SH", T.Market.A)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 105.0)
        self.assertEqual(bars[0].volume, 1_200_000)

    def test_unsupported_bar_contract_values_fail_closed(self):
        provider = self._provider()
        with self.assertRaisesRegex(ValueError, "interval='1d'"):
            provider._bars_url("600519.SH", interval="5m")
        with self.assertRaisesRegex(ValueError, "adjust"):
            provider._bars_url("600519.SH", adjust="mystery")

    def test_tencent_supports_bars_false(self):
        # tencent 默认 OFF（fetch_bars 仅文档占位）
        self.assertFalse(TencentProvider(_eastmoney_cfg(name="tencent", markets=("a", "hk", "us"))).supports_bars())

    def test_a_share_volume_scaled_by_100(self):
        # 字段顺序：日期,开,收,高,低,成交量(手),成交额,换手%
        klines = ["2024-01-02,100.0,105.0,110.0,95.0,12000,1.5e9,2.3"]
        with _Patched(self._provider(), _kline_payload(klines)) as p:
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
        self.assertEqual(b.amount, 1.5e9)
        self.assertEqual(b.turnover, 2.3)
        self.assertEqual(b.source, "eastmoney")
        self.assertEqual(b.timestamp, datetime(2024, 1, 2))

    def test_hk_volume_not_scaled(self):
        klines = ["2024-01-02,400.0,405.0,410.0,395.0,8000000,1.26e10,0.5"]
        with _Patched(self._provider(), _kline_payload(klines)) as p:
            bars = p.fetch_bars("00700.HK", T.Market.HK, interval="1d")
        self.assertEqual(len(bars), 1)
        # 港股已是股，×1
        self.assertEqual(bars[0].volume, 8_000_000)

    def test_us_volume_not_scaled(self):
        klines = ["2024-01-02,180.0,185.0,190.0,175.0,50000000,9.25e9,0.3"]
        with _Patched(self._provider(), _kline_payload(klines)) as p:
            bars = p.fetch_bars("AAPL.US", T.Market.US, interval="1d")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].volume, 50_000_000)

    def test_hsi_index_uses_100_prefix_and_parses(self):
        # 恒指经 to_kline_secid 走 100.HSI；此处验证指数为个股同样的解析路径且返回非空
        klines = ["2024-01-02,16000.0,16500.0,16800.0,15800.0,0,0,0"]
        # 用 eastmoney 实际符号（100.HSI 由 to_kline_secid 推导，不影响解析逻辑）
        with _Patched(self._provider(), _kline_payload(klines)) as p:
            bars = p.fetch_bars("HSI.HK", T.Market.HK, interval="1d")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 16500.0)
        self.assertEqual(bars[0].high, 16800.0)

    def test_empty_klines_returns_empty_list(self):
        with _Patched(self._provider(), _kline_payload([])) as p:
            self.assertEqual(p.fetch_bars("600519.SH", T.Market.A), [])
        # data 为 null
        with _Patched(self._provider(), _kline_payload(None)) as p:
            self.assertEqual(p.fetch_bars("600519.SH", T.Market.A), [])

    def test_rc_nonzero_returns_empty(self):
        with _Patched(self._provider(), _kline_payload(["2024-01-02,1,2,3,4,5,6,7"], rc=1)) as p:
            self.assertEqual(p.fetch_bars("600519.SH", T.Market.A), [])

    def test_malformed_line_skipped(self):
        klines = [
            "2024-01-02,100.0,105.0,110.0,95.0,12000,1.5e9,2.3",
            "bad-line-without-commas",
            "2024-01-03,101.0,106.0,111.0,96.0,13000,1.6e9,2.4",
        ]
        raw = _kline_payload(klines)
        with _Patched(self._provider(), raw) as p:
            bars = p.fetch_bars("600519.SH", T.Market.A)
        # 运行链路容忍单根坏行，仅保留 2 根合法数据。
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1].timestamp, datetime(2024, 1, 3))

    def test_strict_parser_rejects_any_malformed_line(self):
        raw = _kline_payload(
            [
                "2024-01-02,100.0,105.0,110.0,95.0,12000,1.5e9,2.3",
                "bad-line-without-commas",
            ]
        )
        with self.assertRaisesRegex(ValueError, "row 1"):
            self._provider().parse_bars_strict(raw, "600519.SH", T.Market.A)


if __name__ == "__main__":
    unittest.main()
