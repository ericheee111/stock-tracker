"""采集调度（HOT / WARM / COLD 三守护线程，§5 / §14 T5）。

职责：
- 启动 3 个 ``threading.Thread``（守护），各自独立循环，互不阻塞。
- 维护热/温/冷池：
  * HOT  = 持仓标的 + 活跃/已触发信号标的（高频刷新）。
  * WARM = 自选 + 雷达候选（cold_universe 子集，中频刷新 + 信号扫描）。
  * COLD = 全市场快照（批量；降级时回退到 cold_universe）。
- 每 tick：fetch → DQ 标注 → 写 MarketStore → 发布 eventbus → 入库。
  HOT/WARM 额外跑 ``SignalManager.scan_pool``（DQ + 特征 + 策略 + 评分 + 闸门 + 状态机）。
  COLD 额外重算 Regime / Sector 并回写 instruments。
- 异常不触发上游重试风暴：失败由 ProviderRouter 的退避/熔断吸收，池内其余标的继续。

设计约束（PRD #26.7 / §1 不变量）：Collector 是唯一上游访问者；api/features/signals 只读
MarketStore + Repository。
"""

from __future__ import annotations

import threading
from typing import Optional

from ..core import types as T
from ..core.config import ConfigBundle
from ..core.eventbus import get_bus
from ..core.store import MarketStore
from ..data_quality.gate import DataQualityGate
from ..features.engine import FeatureEngine
from ..storage.repository import Repository, to_jsonable
from .router import ProviderRouter


class Scheduler:
    """HOT / WARM / COLD 三线程调度器。"""

    def __init__(self, bundle: ConfigBundle, store: MarketStore, repository: Repository,
                 router: ProviderRouter, feature_engine: FeatureEngine,
                 signal_manager, gate: DataQualityGate, logger) -> None:
        self.bundle = bundle
        self.store = store
        self.repo = repository
        self.router = router
        self.engine = feature_engine
        self.signal_manager = signal_manager
        self.gate = gate
        self.log = logger
        self._bus = get_bus()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._cold_universe: list[str] = list(self.bundle.app.collector.cold_universe or [])
        self._hot_pool: list[str] = []
        self._warm_pool: list[str] = []
        self._seed_pools()

    # ------------------------------------------------------------------ #
    # 池维护
    # ------------------------------------------------------------------ #
    def _seed_pools(self) -> None:
        """初始池：WARM = 自选 + 雷达候选；HOT = 持仓。"""
        wl = list(self.store.get_watchlist().keys())
        radar = self._cold_universe[: self.bundle.app.collector.warm_pool_size]
        self._warm_pool = list(dict.fromkeys(wl + radar))
        self._hot_pool = list(self.store.get_positions().keys())

    def _maintain_pools(self) -> None:
        """动态维护池（每 tick 轻量刷新）。"""
        # HOT：持仓 + 活跃/已触发信号标的
        pos = list(self.store.get_positions().keys())
        active = self.store.active_signal_states()
        sig_syms = [
            sig.symbol for sig in self.store.get_signals().values() if sig.state in active
        ]
        self._hot_pool = list(dict.fromkeys(pos + sig_syms))
        # WARM：自选 + 雷达候选
        wl = list(self.store.get_watchlist().keys())
        radar = self._cold_universe[: self.bundle.app.collector.warm_pool_size]
        self._warm_pool = list(dict.fromkeys(wl + radar))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动三线程；先同步做一次 COLD 预热以尽快填充 MarketStore。"""
        # 预热：先拉一次快照，避免 HOT/WARM 在 regime 为空时首扫质量下降
        try:
            self._run_cold()
        except Exception:  # 预热失败不致命，后台线程会重试
            self.log.exception("Scheduler 预热 COLD 失败（后台将重试）")
        loops = [
            (self._cold_loop, "COLD"),
            (self._warm_loop, "WARM"),
            (self._hot_loop, "HOT"),
        ]
        for target, name in loops:
            t = threading.Thread(target=target, name=f"sched-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.log.info("Scheduler 已启动 %d 个守护线程（HOT/WARM/COLD）", len(self._threads))

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self.log.info("Scheduler 已停止")

    # ------------------------------------------------------------------ #
    # COLD：批量快照 + 市场上下文
    # ------------------------------------------------------------------ #
    def _cold_loop(self) -> None:
        interval = self.bundle.app.collector.cold_interval_sec
        while not self._stop.is_set():
            try:
                self._run_cold()
            except Exception:
                self.log.exception("COLD tick 异常")
            self._publish_health()
            self._stop.wait(interval)

    def _run_cold(self) -> None:
        quotes = self.router.fetch_snapshot(universe=self._cold_universe)
        if not quotes:
            self.log.warning("COLD：快照为空（全部源不可用），跳过本 tick")
            return

        # DQ 标注 + 写 MarketStore + 入库
        stored: list[T.Quote] = []
        for q in quotes:
            prev = self.store.get_quote(q.symbol)
            dq, ds = self.gate.evaluate(q, prev)
            q.quality = dq
            q.data_status = ds
            self.store.update_quote(q)
            stored.append(q)
        self.repo.save_quotes(stored)

        # 维护 instruments（名称/市场/活跃）
        for q in stored:
            meta = {"market": q.market.value, "name": q.name, "is_active": 1}
            self.store.upsert_instrument(q.symbol, meta)
            self.repo.save_instrument(q.symbol, meta)

        # 重算市场上下文（Regime + Sector）
        instruments = self.store.get_instruments()
        regime, sectors = self.engine.build_market_context(stored, instruments)
        self.store.set_regime(regime)
        for sec in sectors.values():
            self.store.update_sector(sec)
        self._bus.publish("regime", to_jsonable(regime))
        self._bus.publish("sector", {k: to_jsonable(v) for k, v in sectors.items()})

        # 持久化 provider 熔断态（重启恢复用）
        for h in self.router.health_list():
            self.repo.save_provider_state(
                h.provider, h.circuit_state.value,
                h.last_success_at.isoformat() if h.last_success_at else None,
                {"latency_p95": h.latency_p95, "error_rate": h.error_rate},
            )

        self.log.info(
            "COLD：更新 %d 标的；regime=%s；sectors=%d",
            len(stored), regime.regime.value, len(sectors),
        )

    # ------------------------------------------------------------------ #
    # HOT / WARM：高频池 + 全信号管线
    # ------------------------------------------------------------------ #
    def _hot_loop(self) -> None:
        interval = self.bundle.app.collector.hot_interval_sec
        while not self._stop.is_set():
            try:
                self._run_pool("HOT", self._hot_pool,
                               self.bundle.app.collector.hot_pool_size)
            except Exception:
                self.log.exception("HOT tick 异常")
            self._publish_health()
            self._stop.wait(interval)

    def _warm_loop(self) -> None:
        interval = self.bundle.app.collector.warm_interval_sec
        while not self._stop.is_set():
            try:
                self._run_pool("WARM", self._warm_pool,
                               self.bundle.app.collector.warm_pool_size)
            except Exception:
                self.log.exception("WARM tick 异常")
            self._publish_health()
            self._stop.wait(interval)

    def _run_pool(self, name: str, pool: list[str], size: int) -> None:
        self._maintain_pools()
        syms = list(dict.fromkeys(pool))[:size]
        if not syms:
            return
        quotes = self.router.fetch_quotes(syms)
        if not quotes:
            return
        regime = self.store.get_regime()
        sectors = self.store.get_sectors()
        bars_map = {s: self.repo.load_recent_bars(s) for s in syms}
        # scan_pool 内部完成 DQ → store → 入库 → publish(quote) → 策略/评分/闸门/状态机
        self.signal_manager.scan_pool(quotes, bars_map, regime, sectors)
        self.log.info("%s：扫描 %d 标的完成", name, len(quotes))

    # ------------------------------------------------------------------ #
    # 健康发布
    # ------------------------------------------------------------------ #
    def _publish_health(self) -> None:
        healths = self.router.health_list()
        for h in healths:
            self.store.update_health(h)
        self._bus.publish("provider_health", [to_jsonable(h) for h in healths])
