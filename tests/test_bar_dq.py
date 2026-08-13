"""evaluate_bar（K 线入库前 DQ）单元测试（§6 / T02）。

验证：
- future-leak 硬阻断：bar.timestamp 显著领先 now → (INVALID, UNKNOWN)，原因含 future-leak。
- 完整性（非严格）：o/h/l/c 任一 ≤0 或 volume<0 → (DEGRADED, STALE)。
- 正常日线 → (VALID, DELAYED)，明确它是 EOD 数据而非盘中实时行情。
- 绝不改动既有 ``evaluate``（此处仅验证 evaluate_bar 行为）。
"""

import unittest
from datetime import datetime, timedelta

from stock_tracker.core import types as T
from stock_tracker.data_quality.gate import DataQualityGate


def _bar(ts=None, **over):
    return T.Bar(
        symbol="X.SH", market=T.Market.A,
        timestamp=ts or datetime.now(),
        open=over.get("open", 100.0),
        high=over.get("high", 110.0),
        low=over.get("low", 95.0),
        close=over.get("close", 105.0),
        volume=over.get("volume", 1_000_000),
    )


class TestEvaluateBar(unittest.TestCase):
    def setUp(self):
        # evaluate_bar 自包含，不依赖 bundle；传 None 即可（不触发 evaluate 路径）
        self.gate = DataQualityGate(None)

    def test_valid(self):
        dq, ds = self.gate.evaluate_bar(_bar())
        self.assertEqual(dq.status, T.QualityStatus.VALID)
        self.assertEqual(ds, T.DataStatus.DELAYED)
        self.assertEqual(dq.score, 100)

    def test_future_leak_invalid(self):
        future = datetime.now() + timedelta(seconds=200)
        dq, ds = self.gate.evaluate_bar(_bar(ts=future))
        self.assertEqual(dq.status, T.QualityStatus.INVALID)
        self.assertEqual(ds, T.DataStatus.UNKNOWN)
        self.assertIn("future-leak", dq.reasons[0])

    def test_completeness_open_zero(self):
        dq, ds = self.gate.evaluate_bar(_bar(open=0.0))
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertEqual(ds, T.DataStatus.STALE)

    def test_completeness_close_negative(self):
        dq, ds = self.gate.evaluate_bar(_bar(close=-5.0))
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertEqual(ds, T.DataStatus.STALE)

    def test_completeness_volume_negative(self):
        dq, ds = self.gate.evaluate_bar(_bar(volume=-1))
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertEqual(ds, T.DataStatus.STALE)

    def test_future_leak_within_drift_allowed(self):
        # 正常时钟漂移（<120s）不算未来泄漏
        slightly_ahead = datetime.now() + timedelta(seconds=30)
        dq, ds = self.gate.evaluate_bar(_bar(ts=slightly_ahead))
        self.assertEqual(dq.status, T.QualityStatus.VALID)


if __name__ == "__main__":
    unittest.main()
