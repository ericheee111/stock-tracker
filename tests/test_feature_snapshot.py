"""指标快照单元测试（T03）。

验证 features.feature_snapshot.build_indicators：
- 返回扁平 dict，含全部约定字段（ma5/10/20/60, ema12/26, macd_*, rsi14, atr14,
  roc20/60, pos52w, ann_vol, vol_ratio, amplitude, last_close, bar_count）。
- 仅计算数值，**不评分/不加权/不做证据去相关**（与 scoring/evidence 解耦）。
- pos52w 使用分位排名（最高≈1.0，最低≈0.0），非 rolling_percentile 取值。
- 空/不足长度输入返回全 None 的空壳（bar_count=0），绝不抛异常。
- 各市场年化波动率口径不同（A=242 / HK=244 / US=252 交易日）。
"""

import unittest
from dataclasses import replace

from stock_tracker.core import types as T
from stock_tracker.features.feature_snapshot import build_indicators

from tests._common import make_bars

_KEYS = ["ma5", "ma10", "ma20", "ma60", "ema12", "ema26", "macd_dif", "macd_dea",
         "macd_hist", "rsi14", "atr14", "roc20", "roc60", "pos52w", "ann_vol",
         "vol_ratio", "amplitude", "last_close", "bar_count"]


class TestBuildIndicators(unittest.TestCase):
    def test_returns_all_keys(self):
        ind = build_indicators(make_bars(120), T.Market.A)
        for k in _KEYS:
            self.assertIn(k, ind)
        self.assertEqual(ind["bar_count"], 120)
        self.assertIsNotNone(ind["ma20"])
        self.assertIsNotNone(ind["rsi14"])

    def test_empty_bars_safe(self):
        ind = build_indicators([], T.Market.A)
        self.assertEqual(ind["bar_count"], 0)
        self.assertIsNone(ind["ma20"])
        self.assertIsNone(ind["pos52w"])

    def test_short_bars_partial(self):
        # 不足 60 根：MA60 为 None，但 MA5 等可计算
        ind = build_indicators(make_bars(10), T.Market.A)
        self.assertEqual(ind["bar_count"], 10)
        self.assertIsNone(ind["ma60"])
        self.assertIsNotNone(ind["ma5"])

    def test_pos52w_percentile_rank_highest(self):
        # 单调递增 → 最后一根为最高 → pos52w ≈ 1.0
        ind = build_indicators(make_bars(60, start=100, step=1), T.Market.A)
        self.assertGreaterEqual(ind["pos52w"], 0.9)
        self.assertLessEqual(ind["pos52w"], 1.0)

    def test_pos52w_lowest(self):
        # 单调递减 → 最后一根为最低 → pos52w ≈ 0.0
        bars = make_bars(60, start=200, step=-1)
        ind = build_indicators(bars, T.Market.A)
        self.assertLessEqual(ind["pos52w"], 0.1)
        self.assertGreaterEqual(ind["pos52w"], 0.0)

    def test_market_annualization_differs(self):
        bars = make_bars(120)
        a = build_indicators(bars, T.Market.A)
        us = build_indicators(bars, T.Market.US)
        self.assertNotEqual(a["ann_vol"], us["ann_vol"])

    def test_flat_series_has_neutral_52_week_rank(self):
        bars = make_bars(60, start=100, step=0)
        ind = build_indicators(bars, T.Market.A)
        self.assertEqual(ind["pos52w"], 0.5)

    def test_invalid_bar_is_removed_as_one_aligned_row(self):
        bars = make_bars(20)
        invalid = replace(bars[10], close=0.0, volume=999_999_999)
        bars[10] = invalid
        ind = build_indicators(bars, T.Market.A)
        self.assertEqual(ind["bar_count"], 19)
        # 被剔除的异常成交量不能被误当作今天或历史量比样本。
        self.assertLess(ind["vol_ratio"], 10.0)

    def test_zero_current_volume_produces_zero_volume_ratio(self):
        bars = make_bars(10)
        bars[-1] = replace(bars[-1], volume=0)
        ind = build_indicators(bars, T.Market.A)
        self.assertEqual(ind["vol_ratio"], 0.0)

    def test_no_scoring_fields(self):
        # 指标快照不应包含任何评分/权重字段（解耦硬约束）
        ind = build_indicators(make_bars(120), T.Market.A)
        for forbidden in ("opportunity", "score", "weight", "evidence", "decorrelate"):
            self.assertNotIn(forbidden, ind)


if __name__ == "__main__":
    unittest.main()
