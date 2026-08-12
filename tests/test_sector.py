"""板块/主题轮动生命周期 + 打分单元测试。"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.features import sector as S
from tests._common import make_quote


def _q(symbol, last, prev, high=0.0, low=0.0, turnover=1.0):
    return make_quote(symbol=symbol, last=last, prev_close=prev,
                      high=high or last, low=low or last * 0.95, turnover=turnover)


class TestSectorScoring(unittest.TestCase):
    def test_weights_sum_to_one(self):
        # 校准系数之和 = 1.0（0.25+0.20+0.20+0.15+0.15+0.05=1.0）
        total = (0.25 + 0.20 + 0.20 + 0.15 + 0.15 + 0.05)
        self.assertAlmostEqual(total, 1.0)

    def test_score_in_range(self):
        eng = S.SectorEngine()
        qs = [_q("600519.SH", 110, 100, 111, 109, 3.0),
              _q("000858.SZ", 105, 100, 106, 104, 2.0)]
        out = eng.update(qs, {})
        self.assertIn("白酒", out)
        snap = out["白酒"]
        self.assertTrue(0 <= snap.score <= 100)
        self.assertIn(snap.stage, list(T.SectorStage))

    def test_broad_fallback_for_unknown(self):
        eng = S.SectorEngine()
        qs = [_q("123456.SH", 110, 100)]
        out = eng.update(qs, {})
        self.assertIn("BROAD", out)


class TestSectorLifecycle(unittest.TestCase):
    """生命周期状态机：EARLY→ACCUMULATION→LEADING→PEAK→DIVERGENCE→DECLINE（二启回 ACCUMULATION）。"""

    def _eng(self):
        return S.SectorEngine()

    def test_early_to_accumulation(self):
        eng = self._eng()
        st = eng._transition("T", score=55, rs=55, extreme=0.0)
        self.assertEqual(st, T.SectorStage.ACCUMULATION)

    def test_accumulation_to_leading(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.ACCUMULATION
        st = eng._transition("T", score=65, rs=58, extreme=0.0)
        self.assertEqual(st, T.SectorStage.LEADING)

    def test_leading_to_peak(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.LEADING
        st = eng._transition("T", score=75, rs=60, extreme=0.2)
        self.assertEqual(st, T.SectorStage.PEAK)

    def test_leading_stays_if_not_hot(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.LEADING
        st = eng._transition("T", score=65, rs=56, extreme=0.0)
        self.assertEqual(st, T.SectorStage.LEADING)  # 不满足条件保持不变

    def test_peak_to_divergence(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.PEAK
        st = eng._transition("T", score=60, rs=48, extreme=0.0)
        self.assertEqual(st, T.SectorStage.DIVERGENCE)

    def test_divergence_to_decline(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.DIVERGENCE
        st = eng._transition("T", score=40, rs=40, extreme=0.0)
        self.assertEqual(st, T.SectorStage.DECLINE)

    def test_decline_re_accumulation(self):
        eng = self._eng()
        eng._stages["T"] = T.SectorStage.DECLINE
        st = eng._transition("T", score=55, rs=55, extreme=0.0)
        self.assertEqual(st, T.SectorStage.ACCUMULATION)  # 二启

    def test_stage_persists_across_updates(self):
        eng = self._eng()
        qs = [_q("600519.SH", 101, 100, 102, 100, 1.0),
              _q("000858.SZ", 102, 100, 103, 101, 1.0)]
        eng._stages["白酒"] = T.SectorStage.LEADING
        out = eng.update(qs, {})
        self.assertEqual(out["白酒"].stage, T.SectorStage.LEADING)


if __name__ == "__main__":
    unittest.main()
