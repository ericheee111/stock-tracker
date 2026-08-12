"""SignalManager 编排（§7.7）：扫描 → 评分 → 闸门 → 状态机 → 持久化 → 推送。

- 每个被扫描标的：DQ 评估 → 写 MarketStore + 发布 quote 事件 → 构建 ScanContext
  → 各策略产出候选 → 四分数 → 风险闸门 → 状态机推导 → 入库 + 发布 signal 事件。
- 同时产出基线 WATCH 候选（机会分达标但无策略命中），供雷达展示。
- 组合热度按持仓数量近似（Phase1 简化；#23 预留主题集中度接口）。
"""

from __future__ import annotations

from datetime import datetime

from ..core import types as T
from ..core.config import ConfigBundle
from ..core.eventbus import get_bus
from ..core.store import MarketStore
from ..data_quality.gate import DataQualityGate
from ..features.engine import FeatureEngine
from ..storage.repository import Repository, to_jsonable
from ..strategies.base import SignalCandidate, Strategy
from ..strategies.s1_breakout import S1Breakout
from ..strategies.s2_pullback import S2Pullback
from ..strategies.s3_event import S3Event
from .risk_gate import RiskGate
from .scoring import score as score_signal
from .state_machine import SignalStateMachine


class SignalManager:
    """信号编排器。"""

    def __init__(self, bundle: ConfigBundle, store: MarketStore, repository: Repository,
                 router, feature_engine: FeatureEngine, gate: DataQualityGate) -> None:
        self.bundle = bundle
        self.store = store
        self.repo = repository
        self.router = router
        self.engine = feature_engine
        self.gate = gate
        self.risk_gate = RiskGate(bundle)
        self.sm = SignalStateMachine()
        self.strategies: list[Strategy] = self._build_strategies()
        self._bus = get_bus()

    def _build_strategies(self) -> list[Strategy]:
        sc = self.bundle.strategies
        out: list[Strategy] = []
        if sc.s1.enabled:
            out.append(S1Breakout(sc.s1))
        if sc.s2.enabled:
            out.append(S2Pullback(sc.s2))
        if sc.s3.enabled:
            out.append(S3Event(sc.s3))
        return out

    # ---- 恢复 ----
    def recover(self) -> None:
        """启动时从 SQLite 恢复信号/自选/持仓到进程内存储。"""
        active = (T.SignalState.WATCH, T.SignalState.ARMED_BREAKOUT,
                  T.SignalState.ARMED_PULLBACK, T.SignalState.TRIGGERED,
                  T.SignalState.ACTIVE, T.SignalState.TRIM, T.SignalState.OVEREXTENDED)
        for sig in self.repo.load_signals(list(active)).values():
            self.store.upsert_signal(sig)
        self.store.set_watchlist(self.repo.load_watchlist())
        self.store.set_positions(self.repo.load_positions())

    # ---- 组合热度 ----
    def _portfolio_heat(self) -> float:
        positions = self.store.get_positions()
        if not positions:
            return 0.0
        # Phase1 近似：单股默认占用 10% 预算，封顶 1.0
        return min(1.0, len(positions) * self.bundle.risk.max_single_pct)

    # ---- 单标的扫描 ----
    def scan_symbol(self, symbol: str, quote: T.Quote, bars: list[T.Bar],
                    regime: T.MarketRegime | None, sector: T.SectorSnapshot | None,
                    prev_quote: T.Quote | None = None) -> list[T.Signal]:
        # 1) DQ
        dq, ds = self.gate.evaluate(quote, prev_quote)
        quote.quality = dq
        quote.data_status = ds
        # 2) 写存储 + 发布
        self.store.update_quote(quote)
        self.repo.save_quote(quote)
        self._bus.publish("quote", to_jsonable(quote))

        # 3) 上下文
        ctx = self.engine.build(symbol, quote, bars, regime, sector, dq, self.bundle)

        # 4) 策略候选
        candidates: list[SignalCandidate] = []
        for strat in self.strategies:
            if not strat.enabled or not strat.applies_to(quote.market):
                continue
            try:
                c = strat.evaluate(ctx)
            except Exception:
                continue
            if c is not None:
                candidates.append(c)

        # 5) 基线 WATCH（机会分达标但无策略命中）
        scores0 = score_signal(ctx)
        if not candidates and scores0.opportunity >= 65:
            candidates.append(self._watch_candidate(symbol, quote, scores0))

        # 6) 评分 + 闸门 + 状态机 + 入库
        heat = self._portfolio_heat()
        produced: list[T.Signal] = []
        for cand in candidates:
            scores = score_signal(ctx)
            decision = self.risk_gate.check(cand, scores, ctx, heat)
            existing = self._existing(symbol, cand.strategy_id)
            sig = self.sm.decide(existing, cand, decision, scores, ctx)
            if sig is None:
                continue
            changed = existing is None or existing.state != sig.state
            self.store.upsert_signal(sig)
            self.repo.upsert_signal(sig)
            if changed:
                self.repo.append_signal_history(
                    sig.signal_id, sig.previous_state.value if sig.previous_state else None,
                    sig.state.value, sig.state_changed_at, sig.reason, sig.what_changed)
            self._bus.publish("signal", to_jsonable(sig))
            produced.append(sig)
        return produced

    def _watch_candidate(self, symbol: str, quote: T.Quote, scores: T.ScoreSet) -> SignalCandidate:
        last = quote.last
        return SignalCandidate(
            symbol=symbol, market=quote.market, strategy_id="BASE",
            proposed_state=T.SignalState.WATCH,
            entry_low=round(last * 0.98, 2), entry_high=round(last * 1.02, 2),
            trigger_price=round(last * 1.02, 2), invalidation_price=round(last * 0.95, 2),
            target_1=round(last * 1.05, 2), target_2=round(last * 1.10, 2),
            reward_risk=0.0, reason=f"机会分 {scores.opportunity} 达标，进入观察",
            next_trigger="等待结构/量能确认后升级", half_life_hours=48.0,
        )

    def _existing(self, symbol: str, strategy_id: str) -> T.Signal | None:
        for sig in self.store.get_signals_by_symbol(symbol):
            if sig.strategy_id == strategy_id:
                return sig
        return None

    # ---- 批量扫描（COLD/HOT/WARM） ----
    def scan_pool(self, quotes: list[T.Quote], bars_map: dict[str, list[T.Bar]],
                  regime: T.MarketRegime | None, sectors: dict[str, T.SectorSnapshot]) -> list[T.Signal]:
        out: list[T.Signal] = []
        for q in quotes:
            bars = bars_map.get(q.symbol, [])
            sector = sectors.get(self._sector_of(q.symbol)) if sectors else None
            prev = self.store.get_quote(q.symbol)
            out.extend(self.scan_symbol(q.symbol, q, bars, regime, sector, prev))
        return out

    def _sector_of(self, symbol: str) -> str:
        from ..features import sector as S
        meta = self.store.get_instrument(symbol) or {}
        return S._sector_of(symbol, {symbol: meta})
