"""SignalManager 编排（§7.7）：扫描 → 评分 → 闸门 → 状态机 → 持久化 → 推送。

- 每个被扫描标的：DQ 评估 → 写 MarketStore + 发布 quote 事件 → 构建 ScanContext
  → 各策略产出候选 → 四分数 → 风险闸门 → 状态机推导 → 入库 + 发布 signal 事件。
- 同时产出基线 WATCH 候选（机会分达标但无策略命中），供雷达展示。
- 组合热度按持仓数量近似（Phase1 简化；#23 预留主题集中度接口）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from ..core import types as T
from ..core.config import ConfigBundle
from ..core.eventbus import get_bus
from ..core.store import MarketStore
from ..data_quality.gate import DataQualityGate
from ..decision.action_mapper import map_signal_to_action
from ..decision.types import DecisionContractError
from ..features.engine import FeatureEngine
from ..features.feature_snapshot import build_indicators
from ..runtime_evidence.contracts import (
    RuntimeEvidenceContractError,
    SystemUtcClock,
    UtcClock,
    build_runtime_decision_draft,
    require_utc_clock_value,
    runtime_signal_version_id,
)
from ..storage.repository import (
    Repository,
    RuntimeDatabaseIntegrityError,
    RuntimeEvidenceUnavailableError,
    RuntimeOutboxConflict,
    RuntimeOutboxError,
    RuntimeSignalPersistence,
    SignalPersistenceError,
    to_jsonable,
)
from ..strategies.base import SignalCandidate, Strategy
from ..strategies.s1_breakout import S1Breakout
from ..strategies.s2_pullback import S2Pullback
from ..strategies.s3_event import S3Event
from .risk_gate import RiskGate
from .scoring import score as score_signal
from .state_machine import SignalStateMachine


def _price_usable(q: T.Quote) -> bool:
    """价格字段是否可用于策略/特征计算（无 None 且为正）。"""
    return all(p is not None and p > 0 for p in
               (q.last, q.prev_close, q.open, q.high, q.low))


@dataclass(frozen=True, slots=True)
class _SignalPersistenceOutcome:
    status: str
    artifact_id: str | None
    error_code: str | None


class SignalManager:
    """信号编排器。"""

    def __init__(self, bundle: ConfigBundle, store: MarketStore, repository: Repository,
                 router, feature_engine: FeatureEngine, gate: DataQualityGate,
                 *, clock: UtcClock | None = None) -> None:
        self.bundle = bundle
        self.store = store
        self.repo = repository
        self.router = router
        self.engine = feature_engine
        self.gate = gate
        self.clock = clock or SystemUtcClock()
        self.risk_gate = RiskGate(bundle)
        self.sm = SignalStateMachine()
        self.strategies: list[Strategy] = self._build_strategies()
        self._bus = get_bus()
        self._observational_errors: deque[str] = deque(maxlen=256)

    def _record_observational_error(self, code: str) -> None:
        self._observational_errors.append(code)

    @property
    def observational_errors(self) -> tuple[str, ...]:
        return tuple(self._observational_errors)

    def _publish_runtime_evidence(
        self,
        *,
        signal_id: str,
        status: str,
        artifact_id: str | None,
        error_code: str | None,
    ) -> None:
        try:
            self._bus.publish(
                "runtime_evidence",
                {
                    "schema": "stage4g1-runtime-evidence-status-v1",
                    "runtime_signal_id": signal_id,
                    "status": status,
                    "artifact_id": artifact_id,
                    "error_code": error_code,
                    "auto_trade": False,
                    "stage4g_case_opened": False,
                },
            )
        except Exception:  # noqa: BLE001 - observational status must not break decisions
            self._record_observational_error(
                "RUNTIME_EVIDENCE_EVENT_PUBLISH_FAILED"
            )
            return

    def _persist_signal_with_runtime_evidence(
        self,
        *,
        signal: T.Signal,
        existing: T.Signal | None,
        changed: bool,
        quote: T.Quote,
        bars: list[T.Bar],
        dq: T.DataQuality,
        decision_requested_at: datetime,
    ) -> _SignalPersistenceOutcome | None:
        observed_at = require_utc_clock_value(self.clock.now(), "observed_at")
        try:
            expected_previous_signal_version_id = runtime_signal_version_id(
                existing,
                allow_legacy_naive_datetime=True,
            )
        except RuntimeEvidenceContractError as exc:
            self._publish_runtime_evidence(
                signal_id=signal.signal_id,
                status="SIGNAL_PERSISTENCE_FAILED",
                artifact_id=None,
                error_code=exc.code,
            )
            return None

        decision_draft = None
        build_error: RuntimeEvidenceContractError | None = None
        evidence_unavailable: RuntimeEvidenceUnavailableError | None = None
        if changed:
            try:
                runtime_store_id = self.repo.runtime_evidence_store_id()
                decision_draft = build_runtime_decision_draft(
                    runtime_store_id=runtime_store_id,
                    signal=signal,
                    previous_signal=existing,
                    quote=quote,
                    bars=bars,
                    data_quality=dq,
                    bundle=self.bundle,
                    decision_requested_at=decision_requested_at,
                    observed_at=observed_at,
                    instrument_metadata=self.store.get_instrument(signal.symbol) or {},
                )
            except RuntimeEvidenceContractError as exc:
                build_error = exc
            except RuntimeEvidenceUnavailableError as exc:
                evidence_unavailable = exc
            except (
                RuntimeDatabaseIntegrityError,
                RuntimeOutboxConflict,
                RuntimeOutboxError,
                SignalPersistenceError,
            ) as exc:
                self._publish_runtime_evidence(
                    signal_id=signal.signal_id,
                    status="SIGNAL_PERSISTENCE_FAILED",
                    artifact_id=None,
                    error_code=type(exc).__name__,
                )
                return None

        if evidence_unavailable is not None:
            try:
                persisted = self.repo.persist_signal_decision(
                    signal,
                    changed=changed,
                    observed_at=observed_at,
                    expected_previous_signal_version_id=expected_previous_signal_version_id,
                )
            except (
                RuntimeDatabaseIntegrityError,
                RuntimeOutboxConflict,
                RuntimeOutboxError,
                SignalPersistenceError,
            ) as core_error:
                self._publish_runtime_evidence(
                    signal_id=signal.signal_id,
                    status="SIGNAL_PERSISTENCE_FAILED",
                    artifact_id=None,
                    error_code=type(core_error).__name__,
                )
                return None
            return _SignalPersistenceOutcome(
                "UNAVAILABLE",
                None,
                "RUNTIME_EVIDENCE_UNAVAILABLE",
            )

        try:
            persisted: RuntimeSignalPersistence = self.repo.persist_signal_decision(
                signal,
                changed=changed,
                observed_at=observed_at,
                expected_previous_signal_version_id=expected_previous_signal_version_id,
                decision_draft=decision_draft,
                quarantine_reason=None if build_error is None else build_error.code,
            )
        except RuntimeEvidenceUnavailableError:
            try:
                persisted = self.repo.persist_signal_decision(
                    signal,
                    changed=changed,
                    observed_at=observed_at,
                    expected_previous_signal_version_id=expected_previous_signal_version_id,
                )
            except (
                RuntimeDatabaseIntegrityError,
                RuntimeOutboxConflict,
                RuntimeOutboxError,
                SignalPersistenceError,
            ) as core_error:
                self._publish_runtime_evidence(
                    signal_id=signal.signal_id,
                    status="SIGNAL_PERSISTENCE_FAILED",
                    artifact_id=None,
                    error_code=type(core_error).__name__,
                )
                return None
            return _SignalPersistenceOutcome(
                "UNAVAILABLE",
                None,
                "RUNTIME_EVIDENCE_UNAVAILABLE",
            )
        except (
            RuntimeDatabaseIntegrityError,
            RuntimeOutboxConflict,
            RuntimeOutboxError,
            SignalPersistenceError,
        ) as error:
            self._publish_runtime_evidence(
                signal_id=signal.signal_id,
                status="SIGNAL_PERSISTENCE_FAILED",
                artifact_id=None,
                error_code=type(error).__name__,
            )
            return None
        evidence_status = persisted.evidence_status
        error_code = None if build_error is None else build_error.code
        if evidence_status == "SIGNAL_ALREADY_PERSISTED_WITHOUT_OUTBOX":
            evidence_status = "UNAVAILABLE"
            error_code = "SIGNAL_ALREADY_PERSISTED_WITHOUT_OUTBOX"
        return _SignalPersistenceOutcome(
            evidence_status,
            persisted.artifact_id,
            error_code,
        )

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
        self.store.set_portfolio_profile(self.repo.load_portfolio_profile())

    # ---- 组合热度 ----
    def _portfolio_heat(self) -> float:
        positions = self.store.get_positions()
        if not positions:
            return 0.0
        # Phase1 近似：单股默认占用 10% 预算，封顶 1.0
        return min(1.0, len(positions) * self.bundle.risk.max_single_pct)

    def _has_open_position(self, symbol: str) -> bool:
        return any(
            position.symbol == symbol and position.closed_at is None
            for position in self.store.get_positions().values()
        )

    def _monitor_facts_payload(
        self,
        *,
        signal: T.Signal,
        quote: T.Quote,
        bars: list[T.Bar],
        regime: T.MarketRegime | None,
        decision,
        scores: T.ScoreSet,
        dq: T.DataQuality,
    ) -> dict:
        """Build a metadata-only fact snapshot without mutating decision state."""

        has_position = self._has_open_position(signal.symbol)
        blocker_codes: list[str] = []
        try:
            action = map_signal_to_action(
                signal,
                has_position=has_position,
                risk_allowed=decision.allowed,
                current_price=quote.last,
            )
            action_state = action.action.value
            blocker_codes.extend(blocker.code for blocker in action.blockers)
        except DecisionContractError:
            action_state = "NOT_AVAILABLE"
            blocker_codes.append("MONITOR_ACTION_MAPPING_FAILED")
        if not decision.allowed and "RISK_GATE_BLOCKED" not in blocker_codes:
            blocker_codes.append("RISK_GATE_BLOCKED")

        indicators = build_indicators(bars, quote.market)
        change_pct = None
        if (
            type(quote.last) in (int, float)
            and type(quote.prev_close) in (int, float)
            and quote.prev_close > 0
        ):
            change_pct = (float(quote.last) / float(quote.prev_close) - 1.0) * 100.0
        return {
            "schema": "stock-tracker-monitor-facts-v1",
            "symbol": signal.symbol,
            "market": signal.market.value,
            "strategy_id": signal.strategy_id,
            "action_state": action_state,
            "signal_state": signal.state.value,
            "data_status": signal.data_status.value,
            "data_quality": {
                "status": dq.status.value,
                "score": dq.score,
            },
            "blocker_codes": blocker_codes,
            "market_regime": {
                "state": regime.regime.value if regime is not None else "UNKNOWN",
                "score": regime.market_score if regime is not None else 0.0,
            },
            "scores": {
                "opportunity": scores.opportunity,
                "timing": scores.timing,
                "risk": scores.risk,
                "confidence": scores.confidence,
            },
            "features": {
                "rsi14": indicators.get("rsi14"),
                "roc20": indicators.get("roc20"),
                "roc60": indicators.get("roc60"),
                "ann_vol": indicators.get("ann_vol"),
                "volume_ratio": indicators.get("vol_ratio"),
                "pos52w": indicators.get("pos52w"),
                "amplitude": indicators.get("amplitude"),
                "bar_count": indicators.get("bar_count", 0),
            },
            "market_event": {
                "connection_state": "NOT_APPLICABLE",
                "feed_mode": "RUNTIME_PROVIDER",
                "latency_p50_ms": None,
                "latency_p95_ms": None,
                "duplicate_count": None,
                "callback_gap_count": None,
                "provider_gap_count": None,
                "out_of_order_count": None,
                "ingestion_lag_ms": None,
                "last_price": quote.last,
                "change_pct": change_pct,
            },
            "has_position": has_position,
            "action_state_mutated": False,
            "score_mutated": False,
            "order_created": False,
        }

    def _publish_monitor_facts(
        self,
        *,
        signal: T.Signal,
        quote: T.Quote,
        bars: list[T.Bar],
        regime: T.MarketRegime | None,
        decision,
        scores: T.ScoreSet,
        dq: T.DataQuality,
    ) -> None:
        """Keep optional Monitor telemetry from affecting the signal pipeline."""

        try:
            payload = self._monitor_facts_payload(
                signal=signal,
                quote=quote,
                bars=bars,
                regime=regime,
                decision=decision,
                scores=scores,
                dq=dq,
            )
        except Exception:  # noqa: BLE001 - observational telemetry boundary
            self._record_observational_error("MONITOR_FACT_BUILD_FAILED")
            return
        try:
            self._bus.publish("monitor_facts", payload)
        except Exception:  # noqa: BLE001 - observational transport boundary
            self._record_observational_error("MONITOR_EVENT_PUBLISH_FAILED")

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

        # 3) 价格不可用（缺失/非正）→ 数据无效，跳过策略/评分/状态机管线。
        #    否则下游特征/策略对 None 价格做除法/比较会抛 TypeError，且不应基于
        #    无效行情产生信号（与 DQ 闸门 INVALID 语义一致）。
        if not _price_usable(quote):
            return []

        # 4) 上下文
        ctx = self.engine.build(symbol, quote, bars, regime, sector, dq, self.bundle)

        # 4) 策略候选
        candidates: list[SignalCandidate] = []
        for strat in self.strategies:
            if not strat.enabled or not strat.applies_to(quote.market):
                continue
            try:
                c = strat.evaluate(ctx)
            except Exception:  # noqa: BLE001, S112 - isolate one strategy failure
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
            decision_requested_at = require_utc_clock_value(
                self.clock.now(), "decision_requested_at"
            )
            scores = score_signal(ctx)
            decision = self.risk_gate.check(cand, scores, ctx, heat)
            existing = self._existing(symbol, cand.strategy_id)
            state_machine_now = decision_requested_at
            if (
                existing is not None
                and (
                    existing.state_changed_at.tzinfo is None
                    or existing.state_changed_at.utcoffset() is None
                )
            ):
                state_machine_now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
            sig = self.sm.decide(
                existing, cand, decision, scores, ctx, now=state_machine_now
            )
            if sig is None:
                continue
            changed = existing is None or existing.state != sig.state
            persistence = self._persist_signal_with_runtime_evidence(
                signal=sig,
                existing=existing,
                changed=changed,
                quote=quote,
                bars=bars,
                dq=dq,
                decision_requested_at=decision_requested_at,
            )
            if persistence is None:
                continue
            self.store.upsert_signal(sig)
            try:
                self._bus.publish("signal", to_jsonable(sig))
            except Exception:  # noqa: BLE001 - durable state survives transport
                self._record_observational_error("SIGNAL_EVENT_PUBLISH_FAILED")
            self._publish_runtime_evidence(
                signal_id=sig.signal_id,
                status=persistence.status,
                artifact_id=persistence.artifact_id,
                error_code=persistence.error_code,
            )
            self._publish_monitor_facts(
                signal=sig,
                quote=quote,
                bars=bars,
                regime=regime,
                decision=decision,
                scores=scores,
                dq=dq,
            )
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
