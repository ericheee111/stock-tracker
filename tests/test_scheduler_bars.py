"""Scheduler BAR 采集逻辑单元测试（§5 / T02）。

通过 mock 协作对象（store/repo/router/gate）**直接调用** ``_backfill_bars`` / ``_incremental_bars``
（不依赖线程、不发起真实 HTTP），验证：
- 首跑全量：对 ``_cold_universe`` 每个标的发起 ``fetch_bars``，并经 ``save_bars_batch`` + ``prune_bars`` 入库。
- DQ 过滤：future-leak（INVALID）的 Bar 被 ``_validate_and_keep`` 丢弃，不入库。
- 增量仅新：仅对「最新 bar 日期 < 今天」或「无 bar」的标的发起 ``fetch_bars``；已最新的跳过。
"""

import ast
import inspect
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from stock_tracker.collector.scheduler import Scheduler
from stock_tracker.core import types as T
from stock_tracker.core.config import (ConfigBundle, AppConfig, CollectorConfig,
                                       MarketsConfig, StrategiesConfig, RiskConfig)
from stock_tracker.data_quality.gate import DataQualityGate


class _Logger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _Store:
    def __init__(self):
        self.watch = {}
        self.pos = {}
        self.sigs = {}
    def get_watchlist(self): return self.watch
    def get_positions(self): return self.pos
    def get_signals(self): return self.sigs
    def get_signals_by_symbol(self, sym): return []
    def active_signal_states(self): return set()
    def get_regime(self): return None
    def get_sectors(self): return {}
    def get_quote(self, sym): return None
    def update_quote(self, q): pass
    def upsert_instrument(self, s, m): pass


class _Repo:
    def __init__(self):
        self.saved = []
        self.pruned = []
        self._recent = {}  # symbol -> list[Bar]（升序）
    def save_bars_batch(self, bars):
        if bars:
            self.saved.append(bars)
        return len(bars)
    def prune_bars(self, symbol, interval, keep):
        self.pruned.append((symbol, interval, keep))
        return 0
    def load_recent_bars(self, symbol, interval="1d", n=260):
        return self._recent.get(symbol, [])


class _Router:
    def __init__(self):
        self.calls = []
        self.bars_for = {}
    def fetch_bars(self, symbol, market, interval="1d", start=None, end=None, adjust="qfq"):
        self.calls.append((symbol, market, start))
        return self.bars_for.get(symbol, [])


def _bundle(cold_universe, **over):
    cfg = CollectorConfig(
        cold_universe=cold_universe,
        warm_pool_size=over.get("warm_pool_size", 10),
        bars_enabled=over.get("bars_enabled", True),
        bars_interval_sec=over.get("bars_interval_sec", 100.0),
        bar_batch_size=over.get("bar_batch_size", 2),
        bar_batch_pause_sec=over.get("bar_batch_pause_sec", 0.0),
        bar_backfill_days=over.get("bar_backfill_days", 400),
        bar_keep_days=over.get("bar_keep_days", 260),
    )
    return ConfigBundle(app=AppConfig(collector=cfg), markets=MarketsConfig(),
                        strategies=StrategiesConfig(), providers=[], risk=RiskConfig())


def _bar(symbol, ts, close=100.0):
    return T.Bar(symbol=symbol, market=T.market_from_symbol(symbol),
                 timestamp=ts, interval="1d",
                 open=close - 1, high=close + 2, low=close - 2, close=close,
                 volume=1_000_000)


def _scheduler(bundle, store, repo, router):
    sched = Scheduler(bundle, store, repo, router, feature_engine=None,
                      signal_manager=None, gate=DataQualityGate(bundle), logger=_Logger())
    return sched


class TestSchedulerBars(unittest.TestCase):
    def setUp(self):
        self.universe = ["600519.SH", "00700.HK", "AAPL.US"]
        self.bundle = _bundle(self.universe, bar_batch_size=2, bar_keep_days=260,
                              bar_backfill_days=400)
        self.store = _Store()
        self.repo = _Repo()
        self.router = _Router()
        # 每个标的返回 5 根合法 bar
        self.router.bars_for = {s: [_bar(s, datetime(2024, 1, 1 + i)) for i in range(5)]
                                for s in self.universe}
        self.sched = _scheduler(self.bundle, self.store, self.repo, self.router)
        self.cfg = self.bundle.app.collector

    def test_first_run_backfills_all_symbols(self):
        self.sched._backfill_bars(self.cfg)
        # 每个标的都有一次 save_bars_batch
        saved_syms = [b.symbol for batch in self.repo.saved for b in batch]
        for s in self.universe:
            self.assertIn(s, saved_syms)
        # 每次保存 5 根
        for batch in self.repo.saved:
            self.assertEqual(len(batch), 5)
        # 每次保存后都 prune（keep=260）
        self.assertEqual(len(self.repo.pruned), len(self.universe))
        for sym, interval, keep in self.repo.pruned:
            self.assertEqual(interval, "1d")
            self.assertEqual(keep, 260)
        # fetch_bars 对每个标的发起一次（首跑）
        called_syms = [c[0] for c in self.router.calls]
        for s in self.universe:
            self.assertIn(s, called_syms)

    def test_first_run_filters_future_leak_bars(self):
        # 00700.HK 返回含未来 bar → 被过滤，不入库
        future = datetime.now() + timedelta(days=5)
        self.router.bars_for["00700.HK"] = [_bar("00700.HK", future, close=400.0)]
        self.sched._backfill_bars(self.cfg)
        saved_syms = [b.symbol for batch in self.repo.saved for b in batch]
        self.assertNotIn("00700.HK", saved_syms)
        # 其余两个仍入库
        self.assertIn("600519.SH", saved_syms)
        self.assertIn("AAPL.US", saved_syms)

    def test_incremental_only_for_stale_or_missing(self):
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        # 600519.SH 已是最新（今天）→ 跳过；00700.HK 昨天 → 增量；AAPL.US 无 bar → 增量
        self.repo._recent = {
            "600519.SH": [_bar("600519.SH", datetime.combine(today, datetime.min.time()))],
            "00700.HK": [_bar("00700.HK", datetime.combine(yesterday, datetime.min.time()))],
            "AAPL.US": [],
        }
        self.router.calls = []
        self.sched._incremental_bars(self.cfg)
        called = [c[0] for c in self.router.calls]
        self.assertIn("00700.HK", called)
        self.assertIn("AAPL.US", called)
        self.assertNotIn("600519.SH", called)  # 已最新，跳过
        # 00700.HK 增量 start = 昨天日期（不含未来、含当天起点）
        hk_call = [c for c in self.router.calls if c[0] == "00700.HK"][0]
        self.assertIsNotNone(hk_call[2])
        self.assertEqual(hk_call[2].date(), yesterday)

    def test_bar_universe_includes_watchlist_and_positions(self):
        watch_symbol = "MSFT.US"
        position_symbol = "000001.SZ"
        self.store.watch[watch_symbol] = object()
        self.store.pos[position_symbol] = object()
        self.router.bars_for[watch_symbol] = [_bar(watch_symbol, datetime(2024, 1, 2))]
        self.router.bars_for[position_symbol] = [
            _bar(position_symbol, datetime(2024, 1, 2))
        ]

        self.sched._backfill_bars(self.cfg)

        called = {call[0] for call in self.router.calls}
        self.assertIn(watch_symbol, called)
        self.assertIn(position_symbol, called)

    def test_validated_eod_bar_is_never_marked_live(self):
        kept = self.sched._validate_and_keep(
            [_bar("600519.SH", datetime(2024, 1, 2))],
            keep=260,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].quality_status, T.DataStatus.DELAYED)

    def test_scheduler_has_one_definition_per_bar_method(self):
        source = inspect.getsource(Scheduler)
        class_node = ast.parse(source).body[0]
        self.assertIsInstance(class_node, ast.ClassDef)
        names = [
            node.name
            for node in class_node.body
            if isinstance(node, ast.FunctionDef)
        ]
        for method in (
            "_run_bars",
            "_backfill_bars",
            "_incremental_bars",
            "_validate_and_keep",
        ):
            with self.subTest(method=method):
                self.assertEqual(names.count(method), 1)

    def test_disabled_when_bars_enabled_false(self):
        bundle = _bundle(self.universe, bars_enabled=False)
        sched = _scheduler(bundle, self.store, self.repo, self.router)
        sched._run_bars()
        # 禁用时不发起任何 fetch_bars
        self.assertEqual(self.router.calls, [])


if __name__ == "__main__":
    unittest.main()
