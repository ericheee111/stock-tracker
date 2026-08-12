"""指标纯函数单元测试（边界值：空序列、单值、不足长度、非法输入）。

不依赖任何运行中的服务，纯函数验证。
"""

import math
import unittest

from stock_tracker.features import indicators as I


class TestSMA(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(I.sma([], 5))

    def test_insufficient_length(self):
        self.assertIsNone(I.sma([1, 2], 5))

    def test_non_positive_period(self):
        self.assertIsNone(I.sma([1, 2, 3], 0))
        self.assertIsNone(I.sma([1, 2, 3], -1))

    def test_normal(self):
        self.assertAlmostEqual(I.sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertAlmostEqual(I.sma([1, 2, 3, 4, 5], 3), 4.0)


class TestEMA(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(I.ema([], 5))

    def test_non_positive_period(self):
        self.assertIsNone(I.ema([1, 2, 3], 0))

    def test_short_returns_simple_average(self):
        # 长度不足时退化为简单平均
        self.assertAlmostEqual(I.ema([2.0, 4.0], 5), 3.0)

    def test_normal_monotonic(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        r = I.ema(v, 3)
        self.assertIsNotNone(r)
        # EMA 应介于最小与最大之间
        self.assertTrue(1.0 < r < 8.0)


class TestMACD(unittest.TestCase):
    def test_short(self):
        # 不足 slow 长度返回 (None, None, None)
        self.assertEqual(I.macd([1, 2, 3], 12, 26, 9), (None, None, None))

    def test_normal_returns_three(self):
        # 构造足够长度序列
        v = [float(i) for i in range(1, 40)]
        dif, dea, hist = I.macd(v, 12, 26, 9)
        self.assertIsNotNone(dif)
        self.assertIsNotNone(dea)
        self.assertIsNotNone(hist)

    def test_hist_consistency(self):
        v = [float(i) for i in range(1, 40)]
        dif, dea, hist = I.macd(v, 12, 26, 9)
        if dea is not None:
            self.assertAlmostEqual(hist, dif - dea, places=4)


class TestRSI(unittest.TestCase):
    def test_short(self):
        self.assertIsNone(I.rsi([1, 2, 3], 14))

    def test_all_gains_returns_100(self):
        v = [float(i) for i in range(1, 20)]
        self.assertAlmostEqual(I.rsi(v, 14), 100.0, places=4)

    def test_all_losses_returns_0(self):
        v = [float(20 - i) for i in range(1, 20)]
        self.assertAlmostEqual(I.rsi(v, 14), 0.0, places=4)

    def test_flat_series_returns_100(self):
        # 全平序列：avg_loss=0 → 源码返回 100（避免除零，定义明确的边界行为）
        v = [10.0] * 20
        r = I.rsi(v, 14)
        self.assertAlmostEqual(r, 100.0, places=4)

    def test_mixed_not_extreme(self):
        v = [10.0, 10.5, 10.2, 10.8, 10.1, 11.0, 10.9, 11.2, 11.0, 11.4,
             11.1, 11.6, 11.3, 11.9, 11.5, 12.0, 11.8, 12.2, 12.0, 12.5]
        r = I.rsi(v, 14)
        self.assertIsNotNone(r)
        self.assertTrue(0.0 < r < 100.0)


class TestATR(unittest.TestCase):
    def test_short(self):
        self.assertIsNone(I.atr([1, 2], [1, 2], [1, 2], 14))

    def test_normal(self):
        # ATR 需要 n >= period+1 = 15 个样本
        highs = [float(10 + i) for i in range(20)]
        lows = [float(9 + i) for i in range(20)]
        closes = [float(9.5 + i) for i in range(20)]
        r = I.atr(highs, lows, closes, 14)
        self.assertIsNotNone(r)
        self.assertTrue(r > 0)


class TestROC(unittest.TestCase):
    def test_short(self):
        self.assertIsNone(I.roc([1, 2], 5))

    def test_zero_base(self):
        self.assertIsNone(I.roc([0, 1, 2, 3, 4, 5], 5))

    def test_normal_positive(self):
        v = [100.0, 101, 102, 103, 104, 105]
        r = I.roc(v, 5)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r, 5.0, places=4)


class TestRollingPercentile(unittest.TestCase):
    def test_too_few(self):
        self.assertIsNone(I.rolling_percentile([1.0], 5, 50))

    def test_window_larger_than_data(self):
        v = [1.0, 2.0, 3.0]
        # 窗口大于数据 → 取全部
        self.assertEqual(I.rolling_percentile(v, 10, 50), 2.0)

    def test_normal(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(I.rolling_percentile(v, 5, 100), 5.0)
        self.assertEqual(I.rolling_percentile(v, 5, 0), 1.0)
        self.assertEqual(I.rolling_percentile(v, 5, 50), 3.0)


class TestStdev(unittest.TestCase):
    def test_too_few(self):
        self.assertIsNone(I.stdev([1.0]))
        self.assertIsNone(I.stdev([]))
        self.assertIsNone(I.stdev_pop([]))

    def test_normal(self):
        v = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        s = I.stdev(v)
        self.assertIsNotNone(s)
        # 样本标准差约 2.138
        self.assertAlmostEqual(s, 2.138, places=2)

    def test_pop(self):
        v = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        s = I.stdev_pop(v)
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s, 2.0, places=2)


if __name__ == "__main__":
    unittest.main()
