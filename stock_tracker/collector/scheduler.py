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
from datetime import datetime, timedelta
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

    def _bar_universe(self) -> list[str]:
        """Return the bounded EOD-bar universe without losing user-critical symbols."""

        watchlist = list(self.store.get_watchlist().keys())
        positions = list(self.store.get_positions().keys())
        active_states = self.store.active_signal_states()
        active_signals = [
            signal.symbol
            for signal in self.store.get_signals().values()
            if signal.state in active_states
        ]
        return list(
            dict.fromkeys(
                [*self._cold_universe, *watchlist, *positions, *active_signals]
            )
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动四线程；先同步做一次 COLD 预热以尽快填充 MarketStore。"""
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
        # BAR：低频 K 线采集线程（首跑全量回填 + 增量追加）；总开关可关闭
        if getattr(self.bundle.app.collector, "bars_enabled", False):
            loops.append((self._run_bars, "BAR"))
        for target, name in loops:
            t = threading.Thread(target=target, name=f"sched-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        total = len(self._threads)
        self.log.info("Scheduler 已启动 %d 个守护线程（HOT/WARM/COLD%s）",
                      total, "/BAR" if total > 3 else "")

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
    # BAR：低频 K 线采集（首跑全量回填 + 增量追加）
    # ------------------------------------------------------------------ #
    def _run_bars(self) -> None:
        """BAR 守护线程入口：首跑全量回填，随后按 bars_interval_sec 周期性增量追加。

        设计要点：
        - 复用 ProviderRouter.fetch_bars（健康检查 / 熔断 / 退避）。
        - 源失败（抛异常）由本层捕获并跳过该标的，不阻塞其余；整轮失败仅记日志。
        - 分批（bar_batch_size）节流 + 批间暂停（bar_batch_pause_sec），避免触发源限频。
        - 每标的仅保留最近 bar_keep_days 根（prune_bars），控制表体积。
        """
        cfg = self.bundle.app.collector
        if not getattr(cfg, "bars_enabled", False):
            self.log.info("BAR：bars_enabled=false，跳过 K 线采集线程")
            return
        # 首跑：全量回填（today - bar_backfill_days 起）
        try:
            self._backfill_bars(cfg)
        except Exception:
            self.log.exception("BAR 首跑全量回填异常（后台循环将重试）")
        # 增量循环
        interval = float(getattr(cfg, "bars_interval_sec", 21600.0))
        while not self._stop.is_set():
            try:
                self._incremental_bars(cfg)
            except Exception:
                self.log.exception("BAR 增量 tick 异常")
            self._stop.wait(interval)

    def _backfill_bars(self, cfg) -> None:
        """首跑全量回填：对每个 universe 标的请求历史 K 线并入库（限制窗口 bar_backfill_days）。"""
        syms = self._bar_universe()
        if not syms:
            return
        start = datetime.now() - timedelta(days=int(getattr(cfg, "bar_backfill_days", 400)))
        batch_size = max(1, int(getattr(cfg, "bar_batch_size", 3)))
        pause = float(getattr(cfg, "bar_batch_pause_sec", 1.5))
        keep = int(getattr(cfg, "bar_keep_days", 260))
        done = 0
        for i in range(0, len(syms), batch_size):
            if self._stop.is_set():
                break
            batch = syms[i:i + batch_size]
            for sym in batch:
                bars = self._fetch_and_store(sym, "1d", start=start, keep=keep)
                done += 1 if bars else 0
            self._stop.wait(pause)
        self.log.info("BAR：首跑全量回填完成（成功 %d/%d 标的）", done, len(syms))

    def _incremental_bars(self, cfg) -> None:
        """增量追加：仅对「最新 K 线日期 < 今日」的标的追加新交易日（避免重复全量拉取）。"""
        syms = self._bar_universe()
        if not syms:
            return
        batch_size = max(1, int(getattr(cfg, "bar_batch_size", 3)))
        pause = float(getattr(cfg, "bar_batch_pause_sec", 1.5))
        keep = int(getattr(cfg, "bar_keep_days", 260))
        today = datetime.now().date()
        done = 0
        for i in range(0, len(syms), batch_size):
            if self._stop.is_set():
                break
            batch = syms[i:i + batch_size]
            for sym in batch:
                # 已是最新（最新 K 线日期 == 今日）→ 跳过
                last = self.repo.load_recent_bars(sym, "1d", n=1)
                if last and last[0].timestamp.date() >= today:
                    continue
                start = (last[0].timestamp if last else None)
                bars = self._fetch_and_store(sym, "1d", start=start, keep=keep)
                done += 1 if bars else 0
            self._stop.wait(pause)
        if done:
            self.log.info("BAR：增量追加完成（更新 %d 标的）", done)

    def _fetch_and_store(self, symbol: str, interval: str, start=None, keep: int = 260) -> int:
        """拉取单标的 K 线 → DQ 过滤 → 入库（保留最近 keep 根）。

        返回本次新写入的 K 线根数。源失败 / 空数据返回 0（不抛、不阻塞其余标的）。
        """
        mkt = T.market_from_symbol(symbol)
        try:
            bars = self.router.fetch_bars(symbol, mkt, interval=interval, start=start)
        except Exception as exc:  # 源不可用：跳过该标的，由 Router 健康/熔断吸收
            self.log.warning("BAR：%s 拉取失败，跳过：%s", symbol, exc)
            return 0
        if not bars:
            return 0
        kept = self._validate_and_keep(bars, keep)
        if not kept:
            return 0
        written = self.repo.save_bars_batch(kept)
        self.repo.prune_bars(symbol, interval, keep)
        return written

    def _validate_and_keep(self, bars: list[T.Bar], keep: int) -> list[T.Bar]:
        """DQ 过滤 + 仅保留最近 keep 根。

        - future-leak（INVALID）直接丢弃；
        - DEGRADED（字段不完整）保留但降权；
        - 按时间升序后取末尾 keep 根。
        """
        ok: list[T.Bar] = []
        for b in bars:
            dq, data_status = self.gate.evaluate_bar(b)
            if dq.status == T.QualityStatus.INVALID:
                continue
            b.quality_status = data_status
            ok.append(b)
        ok.sort(key=lambda x: x.timestamp)
        return ok[-max(1, keep):]

    # ------------------------------------------------------------------ #
    # 健康发布
    # ------------------------------------------------------------------ #
    def _publish_health(self) -> None:
        healths = self.router.health_list()
        for h in healths:
            self.store.update_health(h)
        self._bus.publish("provider_health", [to_jsonable(h) for h in healths])
