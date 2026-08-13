"""serialize_signal 附带 horizon 字段测试。

验证：serialize_signal 输出必含非空的 ``horizon`` 维度（几天/几周/几个月~几年），
且映射与 signals.horizon 一致。
"""

import unittest

from stock_tracker.core import types as T
from stock_tracker.api import serializers as S


class TestSerializeSignalHorizon(unittest.TestCase):
    def test_horizon_present_and_nonempty(self):
        sig = T.Signal(symbol="600519.SH", market=T.Market.A,
                       strategy_id="S1", state=T.SignalState.ACTIVE)
        d = S.serialize_signal(sig)
        self.assertIn("horizon", d)
        h = d["horizon"]
        self.assertIsInstance(h, dict)
        self.assertTrue(h.get("key"))
        self.assertEqual(h["key"], "SHORT")

    def test_horizon_default_medium(self):
        sig = T.Signal(symbol="000001.SZ", market=T.Market.A,
                       strategy_id="BASE", state=T.SignalState.WATCH)
        d = S.serialize_signal(sig)
        self.assertEqual(d["horizon"]["key"], "MEDIUM")

    def test_horizon_long_exists(self):
        # LONG 桶为扩展点（当前无策略映射），但通过 horizon_for_key 可取到。
        from stock_tracker.signals.horizon import horizon_for_key
        self.assertEqual(horizon_for_key("LONG")["span"], "几个月~几年")


if __name__ == "__main__":
    unittest.main()
