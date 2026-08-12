"""数据质量闸门单元测试（§6 / PRD #5.2 / #5.4）。

验证：
- VALID：新鲜、完整、无异常 → 得分 100。
- STALE：observed_age_ms 超 stale 阈值。
- DEGRADED：连续 tick 重复（疑似停更）/ 停牌（量为 0 无波动）/ 跨源偏差过大。
- INVALID（future-leak 硬阻断）：computed_at < timestamp → 分数 0（最重要，禁止泄漏未来）。
- INVALID：last<=0 / 时间戳缺失（<2000 年）。
- blocks_strong_signal：INVALID/STALE/DEGRADED 阻断，VALID 不阻断。
"""

import os
import unittest
from datetime import datetime, timedelta

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       MarketConfig, StrategiesConfig, RiskConfig)
from stock_tracker.data_quality.gate import DataQualityGate, blocks_strong_signal

from tests._common import _ROOT, now_ts


def _bundle(stale_ms: int = 60000, delayed_ms: int = 15000) -> ConfigBundle:
    return ConfigBundle(
        app=AppConfig(root_dir=os.path.join(_ROOT, "data")),
        markets=MarketsConfig(
            a=MarketConfig(stale_ms=stale_ms, delayed_ms=delayed_ms),
            hk=MarketConfig(stale_ms=stale_ms, delayed_ms=delayed_ms),
            us=MarketConfig(stale_ms=stale_ms, delayed_ms=delayed_ms),
        ),
        strategies=StrategiesConfig(),
        providers=[],
        risk=RiskConfig(),
    )


def _quote(**over) -> T.Quote:
    base = dict(
        symbol="600519.SH", market=T.Market.A,
        timestamp=over.get("timestamp", now_ts()),
        open=100.0, high=110.0, low=95.0, close=105.0, last=105.0,
        prev_close=100.0, volume=1_000_000, amount=5e8, turnover=2.0,
        computed_at=over.get("computed_at", datetime.now()),
        observed_age_ms=over.get("observed_age_ms", 10_000),
    )
    base.update(over)
    return T.Quote(**base)


class TestValidAndDegraded(unittest.TestCase):
    def setUp(self):
        self.gate = DataQualityGate(_bundle())

    def test_valid_fresh_complete(self):
        dq, ds = self.gate.evaluate(_quote())
        self.assertEqual(dq.status, T.QualityStatus.VALID)
        self.assertEqual(dq.score, 100)
        self.assertEqual(ds, T.DataStatus.LIVE)

    def test_stale_by_age(self):
        dq, ds = self.gate.evaluate(_quote(observed_age_ms=120_000))  # > stale 60s
        self.assertEqual(dq.status, T.QualityStatus.STALE)
        self.assertEqual(ds, T.DataStatus.STALE)
        self.assertLess(dq.score, 100)

    def test_delayed_by_age(self):
        dq, ds = self.gate.evaluate(_quote(observed_age_ms=30_000))  # > delayed 15s, < stale
        self.assertEqual(ds, T.DataStatus.DELAYED)
        # 仅降权，仍为 VALID
        self.assertEqual(dq.status, T.QualityStatus.VALID)

    def test_dedup_degraded(self):
        prev = _quote()
        cur = _quote(timestamp=prev.timestamp, last=prev.last)
        dq, _ = self.gate.evaluate(cur, prev=prev)
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertTrue(any("停更" in r for r in dq.reasons))

    def test_halt_degraded(self):
        q = _quote(volume=0, high=105.0, low=105.0, last=105.0, close=105.0)
        dq, _ = self.gate.evaluate(q)
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)
        self.assertTrue(any("停牌" in r for r in dq.reasons))

    def test_cross_source_deviation_degraded(self):
        dq, _ = self.gate.evaluate(_quote(), deviation=0.05)  # > 1% 容忍
        self.assertEqual(dq.status, T.QualityStatus.DEGRADED)


class TestInvalidHardBlock(unittest.TestCase):
    def setUp(self):
        self.gate = DataQualityGate(_bundle())

    def test_future_leak_hard_block(self):
        # computed_at 早于源时间戳 → 未来数据泄漏，硬阻断
        ts = datetime.now()
        q = _quote(timestamp=ts, computed_at=ts - timedelta(seconds=10))
        dq, ds = self.gate.evaluate(q)
        self.assertEqual(dq.status, T.QualityStatus.INVALID)
        self.assertEqual(dq.score, 0)
        self.assertEqual(ds, T.DataStatus.UNKNOWN)
        self.assertTrue(any("future-leak" in r for r in dq.reasons))

    def test_future_timestamp_invalid(self):
        # computed_at 足够靠后，确保命中「时间戳来自未来」分支（而非 future-leak 分支）
        q = _quote(timestamp=datetime.now() + timedelta(seconds=300),
                   computed_at=datetime.now() + timedelta(seconds=400))
        dq, _ = self.gate.evaluate(q)
        self.assertEqual(dq.status, T.QualityStatus.INVALID)
        self.assertTrue(any("未来" in r for r in dq.reasons))

    def test_nonpositive_price_invalid(self):
        dq, _ = self.gate.evaluate(_quote(last=0.0))
        self.assertEqual(dq.status, T.QualityStatus.INVALID)

    def test_missing_timestamp_invalid(self):
        q = _quote(timestamp=datetime(1990, 1, 1))
        dq, _ = self.gate.evaluate(q)
        self.assertEqual(dq.status, T.QualityStatus.INVALID)
        self.assertTrue(any("时间戳缺失" in r for r in dq.reasons))


class TestBlockStrongSignal(unittest.TestCase):
    def test_blocking_statuses(self):
        for st in (T.QualityStatus.INVALID, T.QualityStatus.STALE, T.QualityStatus.DEGRADED):
            dq = T.DataQuality(st, 50, [])
            self.assertTrue(blocks_strong_signal(dq), st)

    def test_valid_not_blocking(self):
        dq = T.DataQuality(T.QualityStatus.VALID, 100, [])
        self.assertFalse(blocks_strong_signal(dq))


if __name__ == "__main__":
    unittest.main()
