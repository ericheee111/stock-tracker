"""行情源归一化单元测试（§4.1）。

验证：
- Tencent（GBK、~ 分隔）：A 股手→股、万元→元换算；港股本币额、不乘 100；字段映射；时间 14 位/斜杠两种格式。
- Eastmoney（JSON）：单票 ×100 整数价；快照布局；secid 推导 A/SH。
- Sina（CSV + Referer）：字段顺序与单位；符号解析；异常行（字段不足）抛错。
- 异常 body 容错（parse 不崩）。

注意：这里直接调用各 Provider 的 normalize，避免发起真实 HTTP（确定性、可重复）。
"""

import os
import unittest

from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig
from stock_tracker.collector.tencent import TencentProvider, _parse_body
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.sina import SinaProvider, _resolve_symbol as sina_resolve

from tests._common import _ROOT


def _tencent_body_a(last="105.00", prev="100.00", open_="101.00", vol_hand="10000",
                    high="106.00", low="95.00", amount_yuan="500000000",
                    turnover="2.0", dt="20260812150000"):
    parts = ["x"] * 39
    parts[0] = "v_sh600519"
    parts[1] = "贵州茅台"
    parts[3] = last
    parts[4] = prev
    parts[5] = open_
    parts[6] = vol_hand
    parts[30] = dt
    parts[33] = high
    parts[34] = low
    parts[35] = f"{last}/{vol_hand}/{amount_yuan}"   # A 股 f[35]=最新/量/额(元)
    parts[37] = str(float(amount_yuan) / 10000.0)     # 万元
    parts[38] = turnover
    return "~".join(parts)


def _tencent_body_hk(last="400.0", prev="390.0", open_="395.0", vol_shares="1000000",
                     high="410.0", low="380.0", amount_hkd="12600000000",
                     turnover="0.5", dt="2026/08/12 16:00:00"):
    parts = ["x"] * 39
    parts[0] = "v_hk00700"
    parts[1] = "腾讯控股"
    parts[3] = last
    parts[4] = prev
    parts[5] = open_
    parts[6] = vol_shares
    parts[30] = dt
    parts[33] = high
    parts[34] = low
    parts[35] = last                      # 港股 f[35] 单值（无 /）
    parts[37] = amount_hkd                 # 港股 f[37] 完整本币额
    parts[38] = turnover
    return "~".join(parts)


def _tencent_cfg(name="tencent", markets=("a", "hk", "us")):
    return ProviderConfig(name=name, cls="TencentProvider", markets=list(markets))


class TestTencentNormalize(unittest.TestCase):
    def setUp(self):
        self.p = TencentProvider(_tencent_cfg())

    def test_a_share_mapping_and_scaling(self):
        q = self.p.normalize(("sh600519", _tencent_body_a()), T.Market.A)
        self.assertEqual(q.symbol, "600519.SH")
        self.assertEqual(q.market, T.Market.A)
        self.assertEqual(q.name, "贵州茅台")
        self.assertAlmostEqual(q.last, 105.0, places=2)
        self.assertAlmostEqual(q.prev_close, 100.0, places=2)
        self.assertAlmostEqual(q.open, 101.0, places=2)
        self.assertAlmostEqual(q.high, 106.0, places=2)
        self.assertAlmostEqual(q.low, 95.0, places=2)
        # 手 → 股
        self.assertEqual(q.volume, 1_000_000)
        # 额：f[35] 第三段为元（5e8）
        self.assertAlmostEqual(q.amount, 500_000_000.0, places=2)
        self.assertAlmostEqual(q.turnover, 2.0, places=4)

    def test_a_share_amount_falls_back_to_wan(self):
        # f[35] 第三段(额元)为 0 → 回退到 f[37](万元)×10000
        parts = ["x"] * 39
        parts[0] = "v_sh600519"; parts[1] = "贵州茅台"
        parts[3] = "105.00"; parts[4] = "100.00"; parts[5] = "101.00"; parts[6] = "10000"
        parts[30] = "20260812150000"; parts[33] = "106.00"; parts[34] = "95.00"
        parts[35] = "105.00/10000/0"   # 第三段(额元) = 0
        parts[37] = "50000"            # 万元 → 5e8 元
        parts[38] = "2.0"
        body = "~".join(parts)
        q = self.p.normalize(("sh600519", body), T.Market.A)
        self.assertAlmostEqual(q.amount, 500_000_000.0, places=2)

    def test_a_share_14digit_timestamp(self):
        q = self.p.normalize(("sh600519", _tencent_body_a(dt="20260812153000")), T.Market.A)
        self.assertEqual(q.timestamp.year, 2026)
        self.assertEqual(q.timestamp.month, 8)
        self.assertEqual(q.timestamp.day, 12)

    def test_hk_no_volume_scaling(self):
        q = self.p.normalize(("hk00700", _tencent_body_hk()), T.Market.HK)
        self.assertEqual(q.symbol, "00700.HK")
        self.assertEqual(q.market, T.Market.HK)
        self.assertEqual(q.name, "腾讯控股")
        self.assertAlmostEqual(q.last, 400.0, places=2)
        # 港股成交量单位为股，不乘 100
        self.assertEqual(q.volume, 1_000_000)
        # 港股额为完整本币（HKD），不折算万元→元
        self.assertAlmostEqual(q.amount, 12_600_000_000.0, places=2)

    def test_hk_slash_timestamp(self):
        q = self.p.normalize(("hk00700", _tencent_body_hk(dt="2026/08/12 16:00:00")), T.Market.HK)
        self.assertEqual(q.timestamp.year, 2026)
        self.assertEqual(q.timestamp.hour, 16)

    def test_symbol_resolution(self):
        self.assertEqual(TencentProvider._resolve_symbol("sh600519"), "600519.SH")
        self.assertEqual(TencentProvider._resolve_symbol("sz000001"), "000001.SZ")
        self.assertEqual(TencentProvider._resolve_symbol("hk00700"), "00700.HK")
        self.assertEqual(TencentProvider._resolve_symbol("usAAPL"), "AAPL.US")

    def test_malformed_body_does_not_crash(self):
        # 空/极短 body：缺失字段回落默认 0，不抛异常
        q = _parse_body("", T.Market.A)
        self.assertAlmostEqual(q.last, 0.0, places=4)
        self.assertEqual(q.volume, 0)
        self.assertEqual(q.symbol, "")

    def test_last_invalid_field_is_none(self):
        # 源返回 "--"（停牌/无成交）等不可解析字段 → 解析为 None，而非 0.0。
        # 0.0 会被数据质量闸门误判为「非法价格」(last<=0 → INVALID)，且前端会把
        # 缺失渲染成 "0.00"；None 才是正确的「无数据」语义（修复 Bug D）。
        parts = ["x"] * 39
        parts[0] = "v_sh600519"; parts[1] = "贵州茅台"
        parts[3] = "--"; parts[4] = "100.00"; parts[5] = "101.00"
        parts[6] = "0"; parts[30] = "20260812150000"
        parts[33] = "106.00"; parts[34] = "95.00"
        parts[35] = "--/0/0"; parts[37] = "0"; parts[38] = "0.0"
        body = "~".join(parts)
        q = _parse_body(body, T.Market.A)
        self.assertIsNone(q.last)                       # 关键：缺失价格 → None
        self.assertAlmostEqual(q.prev_close, 100.0, places=2)  # 有效字段仍正常
        self.assertAlmostEqual(q.high, 106.0, places=2)
        self.assertAlmostEqual(q.low, 95.0, places=2)


class TestEastmoneyNormalize(unittest.TestCase):
    def setUp(self):
        self.p = EastmoneyProvider(ProviderConfig(
            name="eastmoney", cls="EastmoneyProvider",
            markets=["a", "hk", "us"], supports_snapshot=True))

    def test_single_layout_x100(self):
        payload = {"f43": 10500, "f44": 11000, "f45": 9500, "f46": 10100,
                   "f57": "600519", "f58": "贵州茅台", "f60": 10000}
        q = self.p.normalize(payload, T.Market.A)
        self.assertEqual(q.symbol, "600519.SH")
        self.assertAlmostEqual(q.last, 105.0, places=2)
        self.assertAlmostEqual(q.high, 110.0, places=2)
        self.assertAlmostEqual(q.low, 95.0, places=2)
        self.assertAlmostEqual(q.open, 101.0, places=2)
        self.assertAlmostEqual(q.prev_close, 100.0, places=2)

    def test_snapshot_layout(self):
        item = {"f12": "600519", "f13": 1, "f2": 105.0, "f15": 110.0, "f16": 95.0,
                "f17": 101.0, "f18": 100.0, "f5": 10000.0, "f6": 5e8, "f8": 2.0}
        q = self.p.normalize(item, T.Market.A)
        self.assertEqual(q.symbol, "600519.SH")
        self.assertAlmostEqual(q.last, 105.0, places=2)
        self.assertEqual(q.volume, 1_000_000)  # 手 → 股
        self.assertAlmostEqual(q.amount, 5e8, places=2)
        self.assertAlmostEqual(q.turnover, 2.0, places=4)

    def test_supports_snapshot(self):
        self.assertTrue(self.p.supports_snapshot())


class TestSinaNormalize(unittest.TestCase):
    def setUp(self):
        self.p = SinaProvider(ProviderConfig(name="sina", cls="SinaProvider", markets=["a"]))

    def test_csv_mapping(self):
        body = ("贵州茅台,101.0,100.0,105.0,110.0,95.0,105.0,105.0,"
                "1000000,500000000,2026-08-12,16:00:00")
        q = self.p.normalize(("sh600519", body), T.Market.A)
        self.assertEqual(q.symbol, "600519.SH")
        self.assertEqual(q.market, T.Market.A)
        self.assertEqual(q.name, "贵州茅台")
        self.assertAlmostEqual(q.open, 101.0, places=2)
        self.assertAlmostEqual(q.prev_close, 100.0, places=2)
        self.assertAlmostEqual(q.last, 105.0, places=2)
        self.assertAlmostEqual(q.high, 110.0, places=2)
        self.assertAlmostEqual(q.low, 95.0, places=2)
        self.assertEqual(q.volume, 1_000_000)
        self.assertAlmostEqual(q.amount, 500_000_000.0, places=2)
        self.assertEqual(q.timestamp.year, 2026)
        self.assertEqual(q.timestamp.hour, 16)

    def test_symbol_resolution(self):
        self.assertEqual(sina_resolve("sh600519"), "600519.SH")
        self.assertEqual(sina_resolve("sz000001"), "000001.SZ")

    def test_insufficient_fields_raises(self):
        # 字段不足 12 → 抛 ValueError（fetch_quotes 外层会吞掉该行）
        with self.assertRaises(ValueError):
            self.p.normalize(("sh600519", "贵州茅台,1,2"), T.Market.A)


if __name__ == "__main__":
    unittest.main()
