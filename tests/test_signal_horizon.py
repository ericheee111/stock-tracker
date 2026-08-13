"""horizon 派生维度单元测试（普通层，§38 边界之外，不引用 quant）。

验证：
- 各 strategy_id 映射到正确的持仓周期桶（S1/S3→短线，S2/BASE→中线）。
- 未知 / None 回退 MEDIUM。
- 返回的 dict 字段齐全（key/label/span/order）。
- 红线：horizon 模块源码不得出现任何对 stock_tracker.quant 的 import。
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stock_tracker.core import types as T
from stock_tracker.signals import horizon as H


class _Sig:
    """最小 signal-like 对象（仅需 strategy_id 属性）。"""

    def __init__(self, strategy_id):
        self.strategy_id = strategy_id


class TestHorizonForSignal(unittest.TestCase):
    def test_strategy_mapping(self):
        self.assertEqual(H.horizon_for_signal(_Sig("S1"))["key"], "SHORT")
        self.assertEqual(H.horizon_for_signal(_Sig("S3"))["key"], "SHORT")
        self.assertEqual(H.horizon_for_signal(_Sig("S2"))["key"], "MEDIUM")
        self.assertEqual(H.horizon_for_signal(_Sig("BASE"))["key"], "MEDIUM")

    def test_unknown_defaults_medium(self):
        d = H.horizon_for_signal(_Sig("UNKNOWN_X"))
        self.assertEqual(d["key"], "MEDIUM")
        # 真实 Signal 对象未知策略也回退 MEDIUM
        sig = T.Signal(symbol="600519.SH", strategy_id="ZZ")
        self.assertEqual(H.horizon_for_signal(sig)["key"], "MEDIUM")

    def test_none_returns_default(self):
        d = H.horizon_for_signal(None)
        self.assertEqual(d["key"], "MEDIUM")

    def test_dict_fields(self):
        d = H.horizon_for_signal(_Sig("S1"))
        for f in ("key", "label", "span", "order"):
            self.assertIn(f, d)
        self.assertEqual(d["label"], "短线")
        self.assertEqual(d["span"], "几天")
        self.assertEqual(d["order"], 1)

    def test_buckets_present(self):
        for k in ("SHORT", "MEDIUM", "LONG"):
            self.assertIn(k, H.HORIZONS)
        self.assertEqual(H.HORIZONS["LONG"]["span"], "几个月~几年")


class TestNoQuantImport(unittest.TestCase):
    """红线：horizon 模块不得 import stock_tracker.quant（§38 边界）。

    仅检查**真正的 import 语句行**（以 import / from 开头），忽略 docstring
    中对 quant 边界的说明性文字（如「不引用 quant 的任何模块」）。
    """

    def test_source_has_no_quant_import(self):
        with open(os.path.join(os.path.dirname(H.__file__), "horizon.py"),
                  encoding="utf-8") as f:
            lines = f.readlines()
        import_lines = [ln for ln in lines
                        if ln.strip().startswith("import ")
                        or ln.strip().startswith("from ")]
        joined = "\n".join(import_lines)
        self.assertNotIn("stock_tracker.quant", joined)
        self.assertNotIn("from ..quant", joined)
        self.assertNotIn("from .quant", joined)
        self.assertNotIn("import quant", joined)


if __name__ == "__main__":
    unittest.main()
