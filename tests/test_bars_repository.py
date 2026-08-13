"""K 线仓储单元测试（T02）。

验证：
- save_bars_batch：单事务幂等 REPLACE；返回写入条数。
- load_recent_bars：默认 n=260（向后兼容：旧调用方不传 n 仍工作）；显式 n 仍生效。
- prune_bars：仅保留每标的最近 keep 根，删除更早历史。
"""

import os
import tempfile
import unittest

from stock_tracker.core import types as T
from stock_tracker.storage.repository import Repository

from tests._common import make_bars


class TestBarsRepository(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.repo = Repository(self.db)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_save_bars_batch_and_load(self):
        bars = make_bars(10)
        written = self.repo.save_bars_batch(bars)
        self.assertEqual(written, 10)
        loaded = self.repo.load_recent_bars(bars[0].symbol)
        self.assertEqual(len(loaded), 10)
        # 时间升序返回
        self.assertLessEqual(loaded[0].timestamp, loaded[-1].timestamp)

    def test_save_bars_batch_idempotent(self):
        bars = make_bars(5)
        self.repo.save_bars_batch(bars)
        self.repo.save_bars_batch(bars)  # REPLACE，不重复累加
        self.assertEqual(len(self.repo.load_recent_bars(bars[0].symbol)), 5)

    def test_save_bars_batch_empty_noop(self):
        self.assertEqual(self.repo.save_bars_batch([]), 0)

    def test_load_recent_bars_default_n_is_260(self):
        # 向后兼容：默认窗口已从 120 提升到 260（旧调用方不传 n 仍工作）
        self.assertEqual(Repository.load_recent_bars.__defaults__[1], 260)

    def test_load_recent_bars_explicit_n(self):
        bars = make_bars(30)
        self.repo.save_bars_batch(bars)
        loaded = self.repo.load_recent_bars(bars[0].symbol, "1d", n=10)
        self.assertEqual(len(loaded), 10)

    def test_prune_keeps_recent_only(self):
        bars = make_bars(30)
        self.repo.save_bars_batch(bars)
        removed = self.repo.prune_bars(bars[0].symbol, "1d", keep=10)
        self.assertEqual(removed, 20)
        kept = self.repo.load_recent_bars(bars[0].symbol)
        self.assertEqual(len(kept), 10)
        # 保留的是最近（最高时间戳）的 10 根
        self.assertEqual(kept[-1].timestamp, bars[-1].timestamp)

    def test_prune_keep_zero_is_noop(self):
        bars = make_bars(5)
        self.repo.save_bars_batch(bars)
        self.assertEqual(self.repo.prune_bars(bars[0].symbol, "1d", keep=0), 0)
        self.assertEqual(len(self.repo.load_recent_bars(bars[0].symbol)), 5)


if __name__ == "__main__":
    unittest.main()
