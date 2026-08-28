"""Facade for XTP data-link status, event ingestion, monitor rules, and replay."""

from __future__ import annotations

import copy
import math
import queue
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..collector.xtp_sidecar import (
    XtpSidecarClient,
    XtpSidecarClientError,
    XtpSidecarConfig,
    load_xtp_sidecar_config,
)
from ..core.types import DataStatus, QualityStatus, RegimeState, SignalState
from ..decision.types import ActionState
from ..market_events.replay import MarketEventReplay
from ..market_events.store import MarketEventStore, MarketEventStoreError
from .contracts import InboxState, MonitorRule, MonitorValidationError
from .engine import MonitorEngine
from .notifications import NotificationDispatcher
from .repository import MonitorRepository, MonitorRepositoryError


class MonitorServiceError(RuntimeError):
    """Raised when the monitor facade cannot satisfy a strict request."""


_MONITOR_FACT_FIELDS = frozenset(
    {
        "schema",
        "symbol",
        "market",
        "strategy_id",
        "action_state",
        "signal_state",
        "data_status",
        "data_quality",
        "blocker_codes",
        "market_regime",
        "scores",
        "features",
        "market_event",
        "has_position",
        "action_state_mutated",
        "score_mutated",
        "order_created",
    }
)
_MONITOR_SCORE_FIELDS = frozenset({"opportunity", "timing", "risk", "confidence"})
_MONITOR_FEATURE_FIELDS = frozenset(
    {
        "rsi14",
        "roc20",
        "roc60",
        "ann_vol",
        "volume_ratio",
        "pos52w",
        "amplitude",
        "bar_count",
    }
)
_MONITOR_MARKET_EVENT_FIELDS = frozenset(
    {
        "connection_state",
        "feed_mode",
        "latency_p50_ms",
        "latency_p95_ms",
        "duplicate_count",
        "callback_gap_count",
        "provider_gap_count",
        "out_of_order_count",
        "ingestion_lag_ms",
        "last_price",
        "change_pct",
    }
)
_ACTION_VALUES = frozenset(item.value for item in ActionState) | {"NOT_AVAILABLE"}
_SIGNAL_VALUES = frozenset(item.value for item in SignalState)
_DATA_STATUS_VALUES = frozenset(item.value for item in DataStatus)
_QUALITY_VALUES = frozenset(item.value for item in QualityStatus)
_REGIME_VALUES = frozenset(item.value for item in RegimeState) | {"UNKNOWN"}
_RUNTIME_EVENT_TOPICS = frozenset({"regime", "provider_health", "monitor_facts", "quote"})
_RUNTIME_EVENT_QUEUE_SIZE = 1024
_RuntimeEvent = tuple[
    str,
    Any,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]


def _visible_fact_string(value: Any, name: str, *, maximum: int = 128) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise MonitorServiceError(f"{name} is invalid")
    return value


def _fact_number(
    value: Any,
    name: str,
    *,
    nullable: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MonitorServiceError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MonitorServiceError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise MonitorServiceError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise MonitorServiceError(f"{name} exceeds its maximum")
    if integer:
        if not number.is_integer():
            raise MonitorServiceError(f"{name} must be an integer")
        return int(number)
    return number


def _exact_fact_object(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MonitorServiceError(f"{name} contract is invalid")
    return value


def _validated_monitor_facts(payload: Any) -> tuple[str, dict[str, Any]]:
    value = _exact_fact_object(payload, _MONITOR_FACT_FIELDS, "monitor facts")
    if value["schema"] != "stock-tracker-monitor-facts-v1":
        raise MonitorServiceError("monitor facts schema is invalid")
    for name in ("action_state_mutated", "score_mutated", "order_created"):
        if value[name] is not False:
            raise MonitorServiceError(f"monitor facts {name} must remain false")
    if type(value["has_position"]) is not bool:
        raise MonitorServiceError("monitor facts has_position must be boolean")

    symbol = _visible_fact_string(value["symbol"], "monitor symbol", maximum=9)
    if symbol != symbol.upper() or not symbol.endswith((".SH", ".SZ")):
        raise MonitorServiceError("monitor facts symbol must be normalized A-share")
    if value["market"] != "A":
        raise MonitorServiceError("monitor facts market must be A")
    _visible_fact_string(value["strategy_id"], "monitor strategy_id")

    action_state = _visible_fact_string(value["action_state"], "monitor action_state")
    signal_state = _visible_fact_string(value["signal_state"], "monitor signal_state")
    data_status = _visible_fact_string(value["data_status"], "monitor data_status")
    if action_state not in _ACTION_VALUES:
        raise MonitorServiceError("monitor action_state is unsupported")
    if signal_state not in _SIGNAL_VALUES:
        raise MonitorServiceError("monitor signal_state is unsupported")
    if data_status not in _DATA_STATUS_VALUES:
        raise MonitorServiceError("monitor data_status is unsupported")

    quality = _exact_fact_object(
        value["data_quality"], frozenset({"status", "score"}), "monitor data_quality"
    )
    quality_status = _visible_fact_string(
        quality["status"], "monitor data_quality.status"
    )
    if quality_status not in _QUALITY_VALUES:
        raise MonitorServiceError("monitor data quality status is unsupported")
    quality_score = _fact_number(
        quality["score"], "monitor data_quality.score", minimum=0, maximum=100
    )

    regime = _exact_fact_object(
        value["market_regime"], frozenset({"state", "score"}), "monitor market_regime"
    )
    regime_state = _visible_fact_string(regime["state"], "monitor market_regime.state")
    if regime_state not in _REGIME_VALUES:
        raise MonitorServiceError("monitor market regime is unsupported")
    regime_score = _fact_number(
        regime["score"], "monitor market_regime.score", minimum=0, maximum=100
    )

    blockers = value["blocker_codes"]
    if not isinstance(blockers, list) or len(blockers) > 64:
        raise MonitorServiceError("monitor blocker_codes must be a bounded array")
    blocker_codes = [
        _visible_fact_string(item, "monitor blocker code") for item in blockers
    ]
    if len(set(blocker_codes)) != len(blocker_codes):
        raise MonitorServiceError("monitor blocker codes must be unique")

    scores = _exact_fact_object(value["scores"], _MONITOR_SCORE_FIELDS, "monitor scores")
    safe_scores = {
        name: _fact_number(
            scores[name], f"monitor scores.{name}", minimum=0, maximum=100
        )
        for name in sorted(_MONITOR_SCORE_FIELDS)
    }

    features = _exact_fact_object(
        value["features"], _MONITOR_FEATURE_FIELDS, "monitor features"
    )
    safe_features = {
        name: _fact_number(
            features[name],
            f"monitor features.{name}",
            nullable=name != "bar_count",
            minimum=0 if name == "bar_count" else None,
            integer=name == "bar_count",
        )
        for name in sorted(_MONITOR_FEATURE_FIELDS)
    }

    market_event = _exact_fact_object(
        value["market_event"], _MONITOR_MARKET_EVENT_FIELDS, "monitor market_event"
    )
    safe_market_event: dict[str, Any] = {
        "connection_state": _visible_fact_string(
            market_event["connection_state"], "monitor market_event.connection_state"
        ),
        "feed_mode": _visible_fact_string(
            market_event["feed_mode"], "monitor market_event.feed_mode"
        ),
    }
    for name in (
        "latency_p50_ms",
        "latency_p95_ms",
        "duplicate_count",
        "callback_gap_count",
        "provider_gap_count",
        "out_of_order_count",
        "ingestion_lag_ms",
    ):
        safe_market_event[name] = _fact_number(
            market_event[name],
            f"monitor market_event.{name}",
            nullable=True,
            minimum=0,
            integer=name.endswith("count") or name == "ingestion_lag_ms",
        )
    safe_market_event["last_price"] = _fact_number(
        market_event["last_price"],
        "monitor market_event.last_price",
        nullable=True,
        minimum=0,
    )
    safe_market_event["change_pct"] = _fact_number(
        market_event["change_pct"],
        "monitor market_event.change_pct",
        nullable=True,
    )

    return symbol, {
        "action_state": action_state,
        "signal_state": signal_state,
        "data_status": data_status,
        "data_quality": {"status": quality_status, "score": quality_score},
        "blocker_codes": blocker_codes,
        "market_regime": {"state": regime_state, "score": regime_score},
        "scores": safe_scores,
        "features": safe_features,
        "market_event": safe_market_event,
    }


def _normalized_symbol_set(values: Iterable[str], *, maximum: int = 5000) -> frozenset[str]:
    output: set[str] = set()
    for value in values:
        if type(value) is not str or not value or len(value) > 16:
            continue
        normalized = value.strip().upper()
        if normalized.endswith((".SH", ".SZ")) and len(normalized) == 9:
            output.add(normalized)
        if len(output) > maximum:
            raise MonitorServiceError("monitor runtime scope exceeds its bounded maximum")
    return frozenset(output)


class MonitorService:
    """Own separate monitor/event stores; never mutate decision or production data."""

    def __init__(
        self,
        config: XtpSidecarConfig,
        *,
        project_root: str | Path,
        publisher=None,
        access_provider=None,
        sidecar_opener=None,
        webhook_opener=None,
        environ=None,
    ) -> None:
        self.config = config
        self.project_root = Path(project_root).resolve(strict=False)
        self.event_store = MarketEventStore(
            self.project_root / config.event_root,
            self.project_root / config.metadata_db,
            quarantine_root=self.project_root / config.quarantine_root,
        )
        self.repository = MonitorRepository(self.project_root / config.monitor_db)
        kwargs: dict[str, Any] = {}
        if access_provider is not None:
            kwargs["access_provider"] = access_provider
        if sidecar_opener is not None:
            kwargs["opener"] = sidecar_opener
        self.sidecar = XtpSidecarClient(config, **kwargs)
        self.engine = MonitorEngine(self.repository, publisher=publisher)
        self.dispatcher = NotificationDispatcher(
            self.repository,
            webhook_enabled=config.webhook_enabled,
            webhook_allowed_origins=config.webhook_allowed_origins,
            browser_publisher=publisher,
            opener=webhook_opener,
            environ=environ,
        )
        self.replay = MarketEventReplay(self.event_store)
        self._cursor = 0
        self._sidecar_session_id: str | None = None
        self._last_poll: dict[str, Any] | None = None
        self._market_regime: dict[str, Any] = {"state": "UNKNOWN", "score": 0.0}
        self._provider_health: dict[str, Any] = {
            "closed": 0,
            "half_open": 0,
            "open": 0,
        }
        self._notification_stop = threading.Event()
        self._notification_lock = threading.Lock()
        self._notification_thread: threading.Thread | None = None
        self._notification_interval_sec = 1.0
        self._notification_last_error: str | None = None
        self._runtime_event_queue: queue.Queue[_RuntimeEvent] = queue.Queue(
            maxsize=_RUNTIME_EVENT_QUEUE_SIZE
        )
        self._runtime_event_stop = threading.Event()
        self._runtime_event_lock = threading.Lock()
        self._runtime_event_thread: threading.Thread | None = None
        self._runtime_event_enqueued = 0
        self._runtime_event_processed = 0
        self._runtime_event_dropped = 0
        self._runtime_event_last_error: str | None = None

    @classmethod
    def from_project(
        cls,
        project_root: str | Path,
        *,
        config_path: str | Path | None = None,
        **kwargs,
    ) -> MonitorService:
        root = Path(project_root).resolve(strict=False)
        path = (
            root / "config" / "xtp_sidecar.toml"
            if config_path is None
            else Path(config_path)
        )
        config = load_xtp_sidecar_config(path)
        return cls(config, project_root=root, **kwargs)

    def enqueue_runtime_event(
        self,
        topic: str,
        payload: Any,
        *,
        watchlist: Iterable[str] = (),
        positions: Iterable[str] = (),
        universe: Iterable[str] = (),
    ) -> bool:
        """Queue one observational runtime event without blocking the signal pipeline."""

        if topic not in _RUNTIME_EVENT_TOPICS:
            return False
        try:
            item: _RuntimeEvent = (
                topic,
                copy.deepcopy(payload),
                tuple(watchlist),
                tuple(positions),
                tuple(universe),
            )
        except Exception:  # noqa: BLE001 - observational enqueue must fail closed
            with self._runtime_event_lock:
                self._runtime_event_dropped += 1
                self._runtime_event_last_error = "RUNTIME_EVENT_SNAPSHOT_FAILED"
            return False
        try:
            self._runtime_event_queue.put_nowait(item)
        except queue.Full:
            with self._runtime_event_lock:
                self._runtime_event_dropped += 1
                self._runtime_event_last_error = "RUNTIME_EVENT_QUEUE_FULL"
            return False
        with self._runtime_event_lock:
            self._runtime_event_enqueued += 1
        return True

    def start_runtime_event_worker(self) -> bool:
        with self._runtime_event_lock:
            if self._runtime_event_thread is not None and self._runtime_event_thread.is_alive():
                return False
            self._runtime_event_stop.clear()
            thread = threading.Thread(
                target=self._runtime_event_loop,
                name="monitor-runtime-event-worker",
                daemon=True,
            )
            self._runtime_event_thread = thread
            thread.start()
            return True

    def _runtime_event_loop(self) -> None:
        while not self._runtime_event_stop.is_set() or not self._runtime_event_queue.empty():
            try:
                topic, payload, watchlist, positions, universe = self._runtime_event_queue.get(
                    timeout=0.2
                )
            except queue.Empty:
                continue
            try:
                self.observe_eventbus(
                    topic,
                    payload,
                    watchlist=watchlist,
                    positions=positions,
                    universe=universe,
                )
            except Exception as exc:  # noqa: BLE001 - background isolation boundary
                with self._runtime_event_lock:
                    self._runtime_event_last_error = type(exc).__name__
            else:
                with self._runtime_event_lock:
                    self._runtime_event_processed += 1
                    self._runtime_event_last_error = None
            finally:
                self._runtime_event_queue.task_done()

    def stop_runtime_event_worker(self) -> None:
        self._runtime_event_stop.set()
        with self._runtime_event_lock:
            thread = self._runtime_event_thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._runtime_event_lock:
            if thread is not None and thread.is_alive():
                self._runtime_event_last_error = "RUNTIME_EVENT_WORKER_STOP_TIMEOUT"
            elif self._runtime_event_thread is thread:
                self._runtime_event_thread = None

    def _runtime_event_worker_status(self) -> dict[str, Any]:
        with self._runtime_event_lock:
            running = (
                self._runtime_event_thread is not None
                and self._runtime_event_thread.is_alive()
            )
            return {
                "running": running,
                "queue_size": self._runtime_event_queue.qsize(),
                "queue_capacity": _RUNTIME_EVENT_QUEUE_SIZE,
                "enqueued": self._runtime_event_enqueued,
                "processed": self._runtime_event_processed,
                "dropped": self._runtime_event_dropped,
                "last_error_code": self._runtime_event_last_error,
            }

    def start_notification_worker(self, *, interval_sec: float = 1.0) -> bool:
        if (
            isinstance(interval_sec, bool)
            or not isinstance(interval_sec, (int, float))
            or not math.isfinite(float(interval_sec))
            or not 0.1 <= float(interval_sec) <= 60.0
        ):
            raise MonitorServiceError("notification interval_sec must be 0.1-60")
        with self._notification_lock:
            if self._notification_thread is not None and self._notification_thread.is_alive():
                return False
            self._notification_interval_sec = float(interval_sec)
            self._notification_stop.clear()
            thread = threading.Thread(
                target=self._notification_loop,
                name="monitor-notification-dispatcher",
                daemon=True,
            )
            self._notification_thread = thread
            thread.start()
            return True

    def _notification_loop(self) -> None:
        while not self._notification_stop.is_set():
            try:
                self.dispatcher.dispatch_pending()
                self._notification_last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the background boundary alive
                self._notification_last_error = type(exc).__name__
            self._notification_stop.wait(self._notification_interval_sec)

    def stop_notification_worker(self) -> None:
        self._notification_stop.set()
        with self._notification_lock:
            thread = self._notification_thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._notification_lock:
            if self._notification_thread is thread:
                self._notification_thread = None

    def _notification_worker_status(self) -> dict[str, Any]:
        with self._notification_lock:
            running = (
                self._notification_thread is not None
                and self._notification_thread.is_alive()
            )
        return {
            "running": running,
            "last_error_code": self._notification_last_error,
            "webhook_enabled": self.config.webhook_enabled,
        }

    def data_link(self) -> dict[str, Any]:
        event_status = self.event_store.status()
        if not self.config.enabled:
            return {
                "schema": "stock-tracker-monitor-data-link-v2",
                "status": "DISABLED",
                "sidecar": {
                    "enabled": False,
                    "backend": self.config.backend.upper(),
                    "read_only": True,
                    "auto_trade": False,
                    "max_symbols": self.config.max_symbols,
                    "api_version": self.config.expected_api_version,
                    "python_abi": self.config.expected_python_series,
                    "algorithm_account_used": False,
                    "operational_acceptance": "PENDING_USER_ENV_AND_OFFICIAL_SDK",
                },
                "event_store": event_status,
                "last_poll": self._last_poll,
                "runtime_event_worker": self._runtime_event_worker_status(),
                "notification_worker": self._notification_worker_status(),
                "contains_account_value": False,
                "contains_sidecar_access": False,
            }
        try:
            health = self.sidecar.health()
            session = self.sidecar.session()
            metrics = self.sidecar.metrics()
            if not (
                health["session_id"] == session["session_id"] == metrics["session_id"]
                and health["backend"] == session["backend"]
                and health["feed_mode"] == session["feed_mode"]
                and health["connection_state"] == session["connection_state"]
                and health["subscription_count"] == len(session["symbols"])
            ):
                raise XtpSidecarClientError(
                    "XTP sidecar metadata changed during the status snapshot"
                )
            status = "ONLINE" if health["status"] == "OK" else "DEGRADED"
            error = None
        except XtpSidecarClientError as exc:
            health = None
            session = None
            metrics = None
            status = "SIDECAR_OFFLINE"
            error = type(exc).__name__
        return {
            "schema": "stock-tracker-monitor-data-link-v2",
            "status": status,
            "sidecar_health": health,
            "sidecar_session": session,
            "sidecar_metrics": metrics,
            "event_store": event_status,
            "last_poll": self._last_poll,
            "runtime_event_worker": self._runtime_event_worker_status(),
            "notification_worker": self._notification_worker_status(),
            "error_code": error,
            "contains_account_value": False,
            "contains_sidecar_access": False,
        }

    def poll_once(self, *, limit: int | None = None) -> dict[str, Any]:
        if not self.config.enabled:
            raise MonitorServiceError("XTP sidecar is disabled in committed configuration")
        started = datetime.now(timezone.utc)
        self.repository.expire_due(started)
        try:
            health = self.sidecar.health()
            session = self.sidecar.session()
            metrics = self.sidecar.metrics()
            session_id = session["session_id"]
            if not (
                health["session_id"] == session_id == metrics["session_id"]
                and health["backend"] == session["backend"]
                and health["feed_mode"] == session["feed_mode"]
                and health["connection_state"] == session["connection_state"]
                and health["subscription_count"] == len(session["symbols"])
            ):
                raise MonitorServiceError(
                    "XTP sidecar metadata changed during the poll snapshot"
                )
            if session_id != self._sidecar_session_id:
                self._sidecar_session_id = session_id
                self._cursor = self.event_store.last_cursor_for_session(session_id)
            events, response_cursor, has_more = self.sidecar.events(
                after=self._cursor,
                limit=limit,
                expected_session_id=session_id,
                expected_feed_mode=session["feed_mode"],
                expected_symbols=tuple(session["symbols"]),
            )
        except (XtpSidecarClientError, MarketEventStoreError) as exc:
            raise MonitorServiceError(str(exc)) from exc

        run_id = self.event_store.start_ingestion_run()
        accepted = 0
        duplicates = 0
        quarantined = 0
        findings = 0
        evaluations = 0
        processed_cursor = self._cursor
        affected_partitions: set[str] = set()
        universe = _normalized_symbol_set(session["symbols"], maximum=self.config.max_symbols)
        try:
            for event in events:
                result = self.event_store.append(event)
                if result.disposition.value == "ACCEPTED":
                    accepted += 1
                elif result.disposition.value == "DUPLICATE":
                    duplicates += 1
                else:
                    quarantined += 1
                if result.partition_key is not None:
                    affected_partitions.add(result.partition_key)
                findings += len(result.findings)
                lag_ms = max(
                    0,
                    int((datetime.now(timezone.utc) - event.received_at).total_seconds() * 1000),
                )
                facts = {
                    "action_state": "NOT_AVAILABLE",
                    "signal_state": "NOT_AVAILABLE",
                    "data_status": "UNKNOWN",
                    "data_quality": {"status": "DEGRADED", "score": 0.0},
                    "blocker_codes": ["XTP_NOT_PROMOTED_TO_LIVE_DECISION"],
                    "market_regime": dict(self._market_regime),
                    "features": {},
                    "market_event": {
                        "connection_state": health["connection_state"],
                        "feed_mode": health["feed_mode"],
                        "latency_p50_ms": metrics["latency_p50_ms"],
                        "latency_p95_ms": metrics["latency_p95_ms"],
                        "duplicate_count": metrics["duplicate_count"],
                        "callback_gap_count": metrics["callback_gap_count"],
                        "provider_gap_count": metrics["provider_gap_count"],
                        "out_of_order_count": metrics["out_of_order_count"],
                        "ingestion_lag_ms": lag_ms,
                        "last_price": event.payload.get("last"),
                        "change_pct": self._change_pct(event.payload),
                    },
                }
                evaluations += len(
                    self.engine.evaluate_all(
                        symbol=event.symbol,
                        market=event.market,
                        facts=facts,
                        all_market_universe=universe,
                        now=event.received_at,
                    )
                )
                processed_cursor = event.callback_seq
        finally:
            self._cursor = processed_cursor
            self.event_store.finish_ingestion_run(
                run_id,
                accepted=accepted,
                duplicates=duplicates,
                quarantined=quarantined,
                last_cursor=processed_cursor,
            )

        if processed_cursor != response_cursor:
            raise MonitorServiceError("sidecar response cursor was not fully processed")
        dispatch = self.dispatcher.dispatch_pending()
        completed = datetime.now(timezone.utc)
        self._last_poll = {
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_ms": int((completed - started).total_seconds() * 1000),
            "accepted": accepted,
            "duplicates": duplicates,
            "quarantined": quarantined,
            "findings": findings,
            "evaluations": evaluations,
            "next_cursor": response_cursor,
            "has_more": has_more,
            "dispatch": dispatch,
            "integrity": self.event_store.verify_integrity(
                partition_keys=affected_partitions
            ),
            "production_database_modified": False,
        }
        return dict(self._last_poll)

    @staticmethod
    def _change_pct(payload: Mapping[str, Any]) -> float | None:
        last = payload.get("last")
        previous = payload.get("prev_close")
        if (
            type(last) not in (int, float)
            or type(previous) not in (int, float)
            or previous <= 0
        ):
            return None
        return (float(last) / float(previous) - 1.0) * 100.0

    def observe_eventbus(
        self,
        topic: str,
        payload: Any,
        *,
        watchlist: Iterable[str] = (),
        positions: Iterable[str] = (),
        universe: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Observe existing runtime facts without provider calls or state mutation."""

        if type(topic) is not str:
            return []
        if topic == "regime":
            if not isinstance(payload, dict):
                return []
            state = payload.get("regime") or payload.get("state")
            score = payload.get("market_score") or payload.get("score")
            if type(state) is str and type(score) in (int, float):
                self._market_regime = {"state": state, "score": float(score)}
            return []
        if topic == "provider_health":
            rows = payload if isinstance(payload, list) else []
            counts = {"closed": 0, "half_open": 0, "open": 0}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                state = str(row.get("circuit_state", "")).upper()
                if state == "CLOSED":
                    counts["closed"] += 1
                elif state == "HALF_OPEN":
                    counts["half_open"] += 1
                elif state == "OPEN":
                    counts["open"] += 1
            self._provider_health = counts
            return []
        watch = _normalized_symbol_set(watchlist)
        held = _normalized_symbol_set(positions)

        if topic == "monitor_facts":
            try:
                symbol, facts = _validated_monitor_facts(payload)
            except MonitorServiceError:
                return []
            bounded_universe = _normalized_symbol_set((*universe, symbol))
            return [
                item.as_dict()
                for item in self.engine.evaluate_all(
                    symbol=symbol,
                    market="A",
                    facts=facts,
                    watchlist=watch,
                    positions=held,
                    all_market_universe=bounded_universe,
                    now=datetime.now(timezone.utc),
                )
            ]

        if topic != "quote" or not isinstance(payload, dict):
            return []
        symbol = payload.get("symbol")
        market = payload.get("market", "A")
        if type(symbol) is not str or type(market) is not str:
            return []
        symbol = symbol.upper()
        if not symbol.endswith((".SH", ".SZ")) or market.upper() != "A":
            return []
        quality = payload.get("quality")
        quality = quality if isinstance(quality, dict) else {}
        quality_status = quality.get("status", QualityStatus.INVALID.value)
        if quality_status not in _QUALITY_VALUES:
            quality_status = QualityStatus.INVALID.value
        quality_score = quality.get("score", 0)
        if (
            isinstance(quality_score, bool)
            or not isinstance(quality_score, (int, float))
            or not math.isfinite(float(quality_score))
            or not 0 <= float(quality_score) <= 100
        ):
            quality_score = 0.0
        data_status = payload.get("data_status", DataStatus.UNKNOWN.value)
        if data_status not in _DATA_STATUS_VALUES:
            data_status = DataStatus.UNKNOWN.value
        last_price = payload.get("last")
        if (
            isinstance(last_price, bool)
            or not isinstance(last_price, (int, float))
            or not math.isfinite(float(last_price))
            or float(last_price) <= 0
        ):
            last_price = None
        facts = {
            "action_state": "NOT_AVAILABLE",
            "signal_state": "NOT_AVAILABLE",
            "data_status": data_status,
            "data_quality": {
                "status": quality_status,
                "score": float(quality_score),
            },
            "blocker_codes": [],
            "market_regime": dict(self._market_regime),
            "scores": {},
            "features": {},
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
                "last_price": last_price,
                "change_pct": self._change_pct(payload),
            },
        }
        bounded_universe = _normalized_symbol_set((*universe, symbol))
        return [
            item.as_dict()
            for item in self.engine.evaluate_all(
                symbol=symbol,
                market="A",
                facts=facts,
                watchlist=watch,
                positions=held,
                all_market_universe=bounded_universe,
                now=datetime.now(timezone.utc),
            )
        ]

    def create_or_update_rule(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = dict(value)
            payload.pop("version", None)
            payload.pop("created_at", None)
            payload.pop("updated_at", None)
            parsed = MonitorRule.from_dict(payload)
            return self.repository.upsert_rule(parsed)
        except (MonitorValidationError, MonitorRepositoryError, TypeError, ValueError) as exc:
            raise MonitorServiceError(str(exc)) from exc

    def delete_rule(self, rule_id: str) -> bool:
        try:
            return self.repository.delete_rule(rule_id)
        except MonitorRepositoryError as exc:
            raise MonitorServiceError(str(exc)) from exc

    def rules(self) -> list[dict[str, Any]]:
        return [rule.as_dict() for rule in self.repository.list_rules()]

    def inbox(
        self,
        *,
        states: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        parsed: tuple[InboxState, ...] | None = None
        if states:
            try:
                parsed = tuple(InboxState(value) for value in states)
            except ValueError as exc:
                raise MonitorServiceError("unsupported inbox state") from exc
        try:
            return self.repository.list_inbox(states=parsed, limit=limit)
        except MonitorRepositoryError as exc:
            raise MonitorServiceError(str(exc)) from exc

    def transition(
        self,
        inbox_id: str,
        state: str,
        *,
        reason: str,
        snooze_sec: int | None = None,
    ) -> dict[str, Any]:
        try:
            target = InboxState(state)
        except ValueError as exc:
            raise MonitorServiceError("unsupported inbox state") from exc
        if snooze_sec is not None and (
            type(snooze_sec) is not int or not 60 <= snooze_sec <= 604800
        ):
            raise MonitorServiceError("snooze_sec must be 60-604800")
        snoozed_until = None
        if target is InboxState.SNOOZED:
            if snooze_sec is None:
                raise MonitorServiceError("SNOOZED requires snooze_sec")
            snoozed_until = datetime.now(timezone.utc) + timedelta(seconds=snooze_sec)
        elif snooze_sec is not None:
            raise MonitorServiceError("snooze_sec is only valid for SNOOZED")
        try:
            return self.repository.transition(
                inbox_id,
                target,
                reason=reason,
                snoozed_until=snoozed_until,
            )
        except MonitorRepositoryError as exc:
            raise MonitorServiceError(str(exc)) from exc

    def replay_data(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        backend: str = "auto",
        limit: int = 5000,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise MonitorServiceError("monitor replay limit must be 1-5000")
        try:
            result = self.replay.run(
                symbol,
                start_at=start_at,
                end_at=end_at,
                backend=backend,
                limit=limit,
                record_run=False,
            )
            minute_bars = self.event_store.minute_bars(
                symbol,
                start_at=start_at,
                end_at=end_at,
                limit=min(limit, 500),
            )
        except MarketEventStoreError as exc:
            raise MonitorServiceError(str(exc)) from exc
        payload = result.as_dict()
        payload["minute_bars"] = minute_bars
        return payload

    def summary(self) -> dict[str, Any]:
        return {
            "schema": "stock-tracker-monitor-summary-v2",
            "monitor": self.repository.summary(),
            "data_link": self.data_link(),
            "inbox": self.inbox(limit=50),
            "rules": self.rules(),
            "provider_health": dict(self._provider_health),
            "runtime_event_worker": self._runtime_event_worker_status(),
            "notification_worker": self._notification_worker_status(),
            "auto_trade": False,
            "allow_live_decision": False,
            "allow_model_training": False,
            "credential_transport": "PROCESS_ENVIRONMENT_ONLY",
            "account_value_exposed": False,
        }
