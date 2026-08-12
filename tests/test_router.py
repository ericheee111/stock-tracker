"""ProviderRouter + HealthTracker 单元测试（§4.2 / PRD #26.7）。

验证：
- HealthTracker 熔断状态机：CLOSED →（连续失败达阈值）→ OPEN →（窗口过）→ HALF_OPEN →（成功）→ CLOSED。
- 指数退避：连续熔断时 open 窗口随 backoff_level 增大。
- ProviderRouter.select：优先 primary；OPEN 源被排除；按健康评分选优。
- cross_check：跨源偏差计算正确（供 health.cross_source_deviation）。
- record_outcome：成功/失败正确计入 tracker。
- HealthTracker.to_provider_health：输出 ProviderHealth 且含 circuit_state。
"""

import os
import time
import unittest
from types import SimpleNamespace

from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, MarketsConfig,
                                       StrategiesConfig, RiskConfig, ProviderConfig)
from stock_tracker.collector.router import ProviderRouter
from stock_tracker.data_quality.health import HealthTracker

from tests._common import _ROOT


def _cfg(name, primary=False, markets=("a", "hk", "us"), threshold=3, base=1.0, mx=60.0):
    return ProviderConfig(name=name, cls="X", primary=primary,
                           markets=list(markets), circuit_fail_threshold=threshold,
                           backoff_base_sec=base, backoff_max_sec=mx)


class MockProvider:
    """轻量 Provider 替身，仅暴露 Router / record_outcome 所需字段。"""

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.timeout = 3.0
        self._rl = SimpleNamespace(hits=0)

    def applies_to(self, market: T.Market) -> bool:
        return market in [T.Market(m.upper()) for m in self.cfg.markets]

    def supports_snapshot(self) -> bool:
        return self.cfg.supports_snapshot


def _bundle_with(providers) -> ConfigBundle:
    return ConfigBundle(
        app=AppConfig(root_dir=os.path.join(_ROOT, "data")),
        markets=MarketsConfig(),
        strategies=StrategiesConfig(),
        providers=providers,
        risk=RiskConfig(),
    )


class TestHealthTrackerCircuit(unittest.TestCase):
    def setUp(self):
        self.tr = HealthTracker(_cfg("tencent", threshold=3, base=1.0))

    def test_starts_closed(self):
        self.assertEqual(self.tr.circuit, T.CircuitState.CLOSED)
        self.assertTrue(self.tr.can_try())

    def test_success_stays_closed(self):
        for _ in range(5):
            self.tr.record_success(20.0)
        self.assertEqual(self.tr.circuit, T.CircuitState.CLOSED)
        self.assertEqual(self.tr._consecutive_fail, 0)

    def test_failures_open_circuit(self):
        for _ in range(3):
            self.tr.record_failure(False)
        self.assertEqual(self.tr.circuit, T.CircuitState.OPEN)

    def test_open_blocks_try_within_window(self):
        for _ in range(3):
            self.tr.record_failure(False)
        self.assertFalse(self.tr.can_try())
        self.assertEqual(self.tr._backoff_level, 1)

    def test_half_open_then_closed(self):
        for _ in range(3):
            self.tr.record_failure(False)
        # 模拟时间流逝（窗口已过）
        self.tr._open_until = 0.0
        self.assertTrue(self.tr.can_try())
        self.assertEqual(self.tr.circuit, T.CircuitState.HALF_OPEN)
        self.tr.record_success(15.0)
        self.assertEqual(self.tr.circuit, T.CircuitState.CLOSED)

    def test_backoff_grows(self):
        for _ in range(3):
            self.tr.record_failure(False)
        first_window = self.tr._open_until
        # 回到 HALF_OPEN 并再次熔断
        self.tr._open_until = 0.0
        self.tr.can_try()  # → HALF_OPEN
        for _ in range(3):
            self.tr.record_failure(False)
        self.assertGreater(self.tr._backoff_level, 1)
        # 第二次 open 窗口应 >= 第一次（指数退避，封顶 backoff_max）
        self.assertGreaterEqual(self.tr._open_until, first_window)

    def test_to_provider_health(self):
        h = self.tr.to_provider_health()
        self.assertIsInstance(h, T.ProviderHealth)
        self.assertEqual(h.provider, "tencent")
        self.assertEqual(h.circuit_state, T.CircuitState.CLOSED)


class TestProviderRouterSelect(unittest.TestCase):
    def setUp(self):
        self.p_primary = MockProvider(_cfg("tencent", primary=True, markets=("a", "hk", "us"), threshold=3))
        self.p_backup = MockProvider(_cfg("sina", primary=False, markets=("a",), threshold=3))
        self.router = ProviderRouter(
            _bundle_with([self.p_primary.cfg, self.p_backup.cfg]),
            [self.p_primary, self.p_backup],
        )

    def test_select_prefers_primary(self):
        sel = self.router.select(T.Market.A, "quote")
        self.assertEqual(sel.name, "tencent")

    def test_open_provider_excluded(self):
        # 把 primary 打爆到 OPEN
        for _ in range(3):
            self.router.record_outcome("tencent", False, 3000.0, False)
        sel = self.router.select(T.Market.A, "quote")
        self.assertEqual(sel.name, "sina")  # 主源熔断 → 备用

    def test_snapshot_requires_supports(self):
        # 两个都不支持快照 → 返回 None
        self.assertIsNone(self.router.select(None, "snapshot"))

    def test_record_success_clears_error(self):
        for _ in range(3):
            self.router.record_outcome("sina", False, 3000.0, False)
        self.assertEqual(self.router.trackers["sina"].circuit, T.CircuitState.OPEN)
        self.router.record_outcome("sina", True, 20.0, False)
        self.assertEqual(self.router.trackers["sina"].circuit, T.CircuitState.CLOSED)


class TestCrossCheck(unittest.TestCase):
    def test_cross_check_deviation(self):
        bundle = _bundle_with([_cfg("tencent", primary=True), _cfg("sina", primary=False)])
        router = ProviderRouter(bundle, [MockProvider(bundle.providers[0]),
                                         MockProvider(bundle.providers[1])])
        q1 = T.Quote(symbol="600519.SH", market=T.Market.A, timestamp=__import__("datetime").datetime.now(),
                     last=100.0)
        q2 = T.Quote(symbol="600519.SH", market=T.Market.A, timestamp=__import__("datetime").datetime.now(),
                     last=110.0)
        q1.source = "tencent"
        q2.source = "sina"
        dev = router.cross_check("600519.SH", q1, q2)
        self.assertAlmostEqual(dev, 0.10, places=4)
        # 偏差写入 primary 的 tracker
        self.assertAlmostEqual(router.trackers["tencent"].cross_source_deviation, 0.10, places=4)

    def test_health_list(self):
        bundle = _bundle_with([_cfg("tencent")])
        router = ProviderRouter(bundle, [MockProvider(bundle.providers[0])])
        healths = router.health_list()
        self.assertEqual(len(healths), 1)
        self.assertEqual(healths[0].provider, "tencent")


if __name__ == "__main__":
    unittest.main()
