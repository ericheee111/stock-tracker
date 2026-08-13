"""K 线 DQ 单元测试（T02）。

验证 DataQualityGate.evaluate_bar（与实时 Quote 的 evaluate 解耦）：
- future-leak 硬阻断：时间戳来自未来 → (INVALID, UNKNOWN)，理由含 future-leak。
- 完整性（非严格）：价格缺失/≤0 或 成交量<0 → (DEGRADED, STALE)。
- 正常日线 → (VALID, DELAYED)，明确它是 EOD 数据而非盘中实时行情。
注意：不修改既有 evaluate（实时 Quote 闸门保持不变）。
"""

import unittest
from datetime import datetime, timedelta

from stock_tracker.core import types as T
from stock_tracker.data_quality.gate import DataQualityGate


def _bar(**over) -> T.Bar:
    return T.Bar(
        symbol=over.get("symbol", "600519.SH"),
        market=over.get("market", T.Market.A),
        timestamp=over.get("timestamp", datetime.now()),
        interval="1d",
        open=over.get("open", 100.0),
        high=over.get("high", 110.0),
        low=over.get("low", 95.0),
        close=over.get("close", 105.0),
        volume=over.get("volume", 1_000_000),
    )


def _gate() -> DataQualityGate:
    # evaluate_bar 仅用 datetime.now() 与 bar 字段，不访问 bundle
    return DataQualityGate(bundle=None)


class TestEvaluateBar(unittest.TestCase):
    def test_future_leak_invalid(self):
        b = _bar(timestamp=datetime.now() + timedelta(days=1))
        dq, ds = _gate().evaluate_bar(b)
        self.assertEqual(dq.status, T.QualityStatus.INVALID)
        self.assertEqual(ds, T.DataStatus.UNKNOWN)
        self.assertIn("future-leak", dq.reasons[0])

    def test_zero_open_degraded(self):
        b = _bar(open=0.0)
        dq, ds = _gate().evaluate_bar(b)
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertEqual(ds, T.DataStatus.STALE)

    def test_negative_volume_degraded(self):
        b = _bar(volume=-5)
        dq, ds = _gate().evaluate_bar(b)
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertEqual(ds, T.DataStatus.STALE)

    def test_valid_bar(self):
        b = _bar()
        dq, ds = _gate().evaluate_bar(b)
        self.assertEqual(dq.status, T.QualityStatus.VALID)
        self.assertEqual(ds, T.DataStatus.DELAYED)
        self.assertEqual(dq.score, 100)

    def test_degraded_still_returned_not_discarded(self):
        # DEGRADED 不应被当作硬阻断（调度层保留降权入库）
        b = _bar(open=0.0)
        dq, _ = _gate().evaluate_bar(b)
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)


if __name__ == "__main__":
    unittest.main()
