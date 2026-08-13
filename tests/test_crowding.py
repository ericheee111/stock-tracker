"""crowding_for（§24.6 拥挤度/追高风险仪表，普通层）单元测。

验证：
- 高位 + 远离 MA20 + 放量 + 动量加速 → 高分 / 追高；
- 低位 + 贴近 MA20 + 地量 → 低分 / 安全；
- 指标缺失 / None → 安全降级，不抛异常；
- 部分字段（仅 pos52w）/ NaN → 安全跳过；
- 返回 dict 字段齐全；
- 源码不引用 stock_tracker.quant（严守 §38 边界）。
"""
import os
import unittest

import stock_tracker.signals.crowding as C


class TestCrowding(unittest.TestCase):
    def test_high_crowding(self):
        ind = {"pos52w": 0.98, "ma20": 100.0, "last_close": 110.0, "atr14": 3.0,
               "vol_ratio": 4.0, "roc20": 25.0, "roc60": 10.0}
        c = C.crowding_for(ind)
        self.assertGreaterEqual(c["score"], 75)
        self.assertEqual(c["level_key"], "OVEREXT")
        self.assertEqual(c["level"], "追高")
        self.assertTrue(c["factors"])

    def test_low_crowding_safe(self):
        ind = {"pos52w": 0.10, "ma20": 100.0, "last_close": 99.0, "atr14": 3.0,
               "vol_ratio": 0.8, "roc20": -2.0, "roc60": -5.0}
        c = C.crowding_for(ind)
        self.assertLess(c["score"], 25)
        self.assertEqual(c["level_key"], "SAFE")
        self.assertEqual(c["level"], "安全")

    def test_mid_crowded(self):
        # pos52w 0.6→24 分；距MA20 +1.67ATR→~16.7；量比1.5→~3.3；roc20 5→~1.7 ≈ 45 → 关注
        ind = {"pos52w": 0.6, "ma20": 100.0, "last_close": 105.0, "atr14": 3.0,
               "vol_ratio": 1.5, "roc20": 5.0}
        c = C.crowding_for(ind)
        self.assertGreaterEqual(c["score"], 25)
        self.assertLess(c["score"], 50)
        self.assertEqual(c["level_key"], "WATCH")

    def test_no_indicators_safe(self):
        c = C.crowding_for(None)
        self.assertEqual(c["score"], 0)
        self.assertEqual(c["level_key"], "SAFE")
        self.assertEqual(c["factors"], ["暂无指标，无法评估拥挤度"])

    def test_partial_indicators_only_pos52w(self):
        c = C.crowding_for({"pos52w": 0.5})
        self.assertAlmostEqual(c["score"], 20)  # 0.5 * 40
        self.assertEqual(c["level_key"], "SAFE")  # 20 < 25

    def test_keys_present(self):
        c = C.crowding_for({"pos52w": 0.9})
        for k in ("score", "level", "level_key", "color", "factors"):
            self.assertIn(k, c)

    def test_no_quant_import(self):
        # 仅扫描真实 import 语句，避开 docstring 中出于"边界说明"而出现的
        # ``stock_tracker.quant`` 字面量（历史 test_signal_horizon 同因修复）。
        path = os.path.join(os.path.dirname(C.__file__), "crowding.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        import_lines = [ln for ln in src.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        joined = "\n".join(import_lines)
        self.assertNotIn("stock_tracker.quant", joined)
        self.assertNotIn("from ..quant", joined)
        self.assertNotIn("from .quant", joined)
        self.assertNotIn("import quant", joined)

    def test_does_not_raise_on_nan(self):
        ind = {"pos52w": float("nan"), "ma20": 100.0, "last_close": 101.0, "atr14": 3.0}
        c = C.crowding_for(ind)  # pos52w NaN 被跳过，仅距MA20 贡献少量分
        self.assertIn("score", c)
        self.assertEqual(c["level_key"], "SAFE")


if __name__ == "__main__":
    unittest.main()
