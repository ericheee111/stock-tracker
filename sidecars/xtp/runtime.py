# ruff: noqa: UP006, UP031, UP035, UP045
"""In-memory read-only runtime and deterministic XTP simulator backend."""

from __future__ import annotations

import math
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional

from .contracts import (
    EVENTS_RESPONSE_SCHEMA,
    HEALTH_SCHEMA,
    METRICS_SCHEMA,
    SESSION_SCHEMA,
    EventEnvelope,
    XtpSidecarContractError,
    trading_day_for,
    validate_symbol_list,
)

_ERROR_CODE_RE = re.compile(r"^[A-Z0-9._:-]{1,128}$")


def _safe_error_code(value: object) -> str:
    text = str(value).strip().upper()
    return text if _ERROR_CODE_RE.fullmatch(text) is not None else "DISCONNECTED"


class SidecarRuntime:
    """Thread-safe session, event-buffer, and metrics state."""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        backend: str = "simulator",
        api_version: str = "2.2.50.8",
        max_events: int = 20000,
    ) -> None:
        self.symbols = tuple(validate_symbol_list(symbols, maximum=20))
        if backend not in {"simulator", "xtp"}:
            raise XtpSidecarContractError("backend must be simulator or xtp")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 100:
            raise XtpSidecarContractError("max_events must be an integer >= 100")
        self.backend = backend
        self.api_version = api_version
        self.session_id = "xtp-%s" % uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc)
        self.connected_at: Optional[datetime] = None
        self.last_event_at: Optional[datetime] = None
        self.connection_state = "STARTING"
        self.feed_mode = "SIMULATOR" if backend == "simulator" else "LEVEL1"
        self._max_events = max_events
        self._events: Deque[EventEnvelope] = deque(maxlen=max_events)
        self._event_ids: set[str] = set()
        self._lock = threading.RLock()
        self._callback_seq = 0
        self._provider_seq_by_symbol: Dict[str, int] = {}
        self._last_callback_seq = 0
        self._last_provider_seq_by_symbol: Dict[str, int] = {}
        self._latencies_ms: Deque[float] = deque(maxlen=4096)
        self.callback_count = 0
        self.duplicate_count = 0
        self.callback_gap_count = 0
        self.provider_gap_count = 0
        self.out_of_order_count = 0
        self.reconnect_count = 0
        self.disconnect_count = 0
        self.dropped_buffer_count = 0
        self.last_error_code: Optional[str] = None
        self.last_error_at: Optional[datetime] = None

    def mark_connected(self, *, feed_mode: Optional[str] = None) -> None:
        if feed_mode is not None and feed_mode not in {"SIMULATOR", "LEVEL1", "LEVEL2"}:
            raise XtpSidecarContractError("invalid feed mode")
        with self._lock:
            self.connection_state = "CONNECTED"
            self.connected_at = datetime.now(timezone.utc)
            if feed_mode is not None:
                self.feed_mode = feed_mode

    def mark_disconnected(self, code: str = "DISCONNECTED") -> None:
        safe_code = _safe_error_code(code)
        with self._lock:
            self.connection_state = "DISCONNECTED"
            self.disconnect_count += 1
            self.last_error_code = safe_code
            self.last_error_at = datetime.now(timezone.utc)

    def mark_reconnecting(self) -> None:
        with self._lock:
            self.connection_state = "RECONNECTING"
            self.reconnect_count += 1

    def next_callback_seq(self) -> int:
        with self._lock:
            self._callback_seq += 1
            return self._callback_seq

    def next_provider_seq(self, symbol: str) -> int:
        with self._lock:
            value = self._provider_seq_by_symbol.get(symbol, 0) + 1
            self._provider_seq_by_symbol[symbol] = value
            return value

    def append(self, event: EventEnvelope) -> bool:
        if not isinstance(event, EventEnvelope):
            raise XtpSidecarContractError("event must be EventEnvelope")
        event = EventEnvelope.from_dict(event.as_dict())
        with self._lock:
            if event.session_id != self.session_id:
                raise XtpSidecarContractError("event session_id does not match runtime")
            if event.feed_mode != self.feed_mode:
                raise XtpSidecarContractError("event feed_mode does not match runtime")
            if event.symbol not in self.symbols:
                raise XtpSidecarContractError("event symbol is not subscribed")
            self.callback_count += 1
            if event.event_id in self._event_ids:
                self.duplicate_count += 1
                return False
            if self._last_callback_seq and event.callback_seq <= self._last_callback_seq:
                self.out_of_order_count += 1
            elif self._last_callback_seq and event.callback_seq > self._last_callback_seq + 1:
                self.callback_gap_count += event.callback_seq - self._last_callback_seq - 1
            self._last_callback_seq = max(self._last_callback_seq, event.callback_seq)
            if event.provider_seq is not None:
                previous = self._last_provider_seq_by_symbol.get(event.symbol)
                if previous is not None:
                    if event.provider_seq <= previous:
                        self.out_of_order_count += 1
                    elif event.provider_seq > previous + 1:
                        self.provider_gap_count += event.provider_seq - previous - 1
                self._last_provider_seq_by_symbol[event.symbol] = max(
                    previous or 0, event.provider_seq
                )
            if len(self._events) == self._max_events:
                removed = self._events[0]
                self._event_ids.discard(removed.event_id)
                self.dropped_buffer_count += 1
            self._events.append(event)
            self._event_ids.add(event.event_id)
            self.last_event_at = event.received_at.astimezone(timezone.utc)
            reference = event.exchange_timestamp or event.provider_timestamp
            if reference is not None:
                latency = max(
                    0.0,
                    (event.received_at - reference).total_seconds() * 1000.0,
                )
                if math.isfinite(latency):
                    self._latencies_ms.append(latency)
            return True

    def events_after(self, after: int, limit: int) -> Dict[str, Any]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise XtpSidecarContractError("after must be an integer >= 0")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise XtpSidecarContractError("limit must be between 1 and 500")
        with self._lock:
            oldest_cursor = self._events[0].callback_seq if self._events else 0
            cursor_lost = bool(self._events and after < oldest_cursor - 1)
            selected = [event for event in self._events if event.callback_seq > after]
            selected = selected[:limit]
            next_cursor = after if not selected else selected[-1].callback_seq
            return {
                "schema": EVENTS_RESPONSE_SCHEMA,
                "session_id": self.session_id,
                "after": after,
                "oldest_cursor": oldest_cursor,
                "next_cursor": next_cursor,
                "cursor_lost": cursor_lost,
                "has_more": any(event.callback_seq > next_cursor for event in self._events),
                "events": [event.as_dict() for event in selected],
            }

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return round(ordered[lower], 3)
        weight = position - lower
        return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": HEALTH_SCHEMA,
                "status": "OK" if self.connection_state == "CONNECTED" else "DEGRADED",
                "backend": self.backend.upper(),
                "read_only": True,
                "auto_trade": False,
                "allow_live_decision": False,
                "allow_model_training": False,
                "api_version": self.api_version,
                "python_abi_expected": "3.9",
                "session_id": self.session_id,
                "connection_state": self.connection_state,
                "feed_mode": self.feed_mode,
                "subscription_count": len(self.symbols),
                "started_at": self.started_at.isoformat(),
                "last_event_at": None if self.last_event_at is None else self.last_event_at.isoformat(),
            }

    def session(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": SESSION_SCHEMA,
                "session_id": self.session_id,
                "backend": self.backend.upper(),
                "feed_mode": self.feed_mode,
                "connection_state": self.connection_state,
                "started_at": self.started_at.isoformat(),
                "connected_at": None if self.connected_at is None else self.connected_at.isoformat(),
                "symbols": list(self.symbols),
                "account_identifier_present": False,
                "algorithm_account_used": False,
            }

    def metrics(self) -> Dict[str, Any]:
        with self._lock:
            values = list(self._latencies_ms)
            return {
                "schema": METRICS_SCHEMA,
                "session_id": self.session_id,
                "callback_count": self.callback_count,
                "duplicate_count": self.duplicate_count,
                "callback_gap_count": self.callback_gap_count,
                "provider_gap_count": self.provider_gap_count,
                "out_of_order_count": self.out_of_order_count,
                "reconnect_count": self.reconnect_count,
                "disconnect_count": self.disconnect_count,
                "dropped_buffer_count": self.dropped_buffer_count,
                "latency_p50_ms": self._percentile(values, 0.50),
                "latency_p95_ms": self._percentile(values, 0.95),
                "last_error_code": self.last_error_code,
                "last_error_at": None if self.last_error_at is None else self.last_error_at.isoformat(),
            }


class SimulatorBackend:
    """Deterministic feed used for engineering tests and local UI development."""

    def __init__(
        self,
        runtime: SidecarRuntime,
        *,
        interval_sec: float = 0.2,
        seed_price: float = 10.0,
    ) -> None:
        if interval_sec <= 0:
            raise XtpSidecarContractError("interval_sec must be positive")
        self.runtime = runtime
        self.interval_sec = float(interval_sec)
        self.seed_price = float(seed_price)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ticks = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.runtime.mark_connected(feed_mode="SIMULATOR")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="xtp-simulator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self.runtime.mark_disconnected("SIMULATOR_STOPPED")

    def emit_once(self, *, now: Optional[datetime] = None) -> List[EventEnvelope]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise XtpSidecarContractError("simulator time must be timezone-aware")
        events: List[EventEnvelope] = []
        self._ticks += 1
        for index, symbol in enumerate(self.runtime.symbols):
            callback_seq = self.runtime.next_callback_seq()
            provider_seq = self.runtime.next_provider_seq(symbol)
            drift = ((self._ticks + index) % 17 - 8) * 0.002
            last = round(self.seed_price + index * 0.37 + drift, 3)
            payload = {
                "last": last,
                "open": round(last - 0.05, 3),
                "high": round(last + 0.08, 3),
                "low": round(last - 0.09, 3),
                "prev_close": round(last - 0.02, 3),
                "volume": 1000 + self._ticks * 10 + index,
                "amount": round(last * (1000 + self._ticks * 10 + index), 3),
                "simulator": True,
            }
            exchange_time = current - timedelta(milliseconds=6 + index)
            event = EventEnvelope.create(
                feed_mode="SIMULATOR",
                symbol=symbol,
                event_type="MARKET_DATA",
                trading_day=trading_day_for(exchange_time),
                exchange_timestamp=exchange_time,
                provider_timestamp=exchange_time,
                received_at=current,
                session_id=self.runtime.session_id,
                callback_seq=callback_seq,
                provider_seq=provider_seq,
                payload=payload,
            )
            self.runtime.append(event)
            events.append(event)
        return events

    def inject_reconnect(self) -> None:
        self.runtime.mark_disconnected("SIMULATED_LINK_DROP")
        self.runtime.mark_reconnecting()
        self.runtime.mark_connected(feed_mode="SIMULATOR")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.emit_once()
            self._stop.wait(self.interval_sec)
