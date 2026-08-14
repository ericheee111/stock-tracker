"""进程内共享存储 MarketStore（带读写锁）。

Collector 是唯一写入上游数据者；api/features/signals 只读此处与 SQLite。
保存：最新 Quote、Signal、Regime、Sector、ProviderHealth、Watchlist、Positions、Instruments。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from . import types as T


class MarketStore:
    """线程安全的进程内最新状态存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, T.Quote] = {}
        self._signals: dict[str, T.Signal] = {}
        self._signals_by_symbol: dict[str, list[str]] = {}
        self._regime: Optional[T.MarketRegime] = None
        self._sectors: dict[str, T.SectorSnapshot] = {}
        self._health: dict[str, T.ProviderHealth] = {}
        self._watchlist: dict[str, T.WatchlistItem] = {}
        self._positions: dict[str, T.Position] = {}
        self._portfolio_profile = None
        self._instruments: dict[str, dict] = {}
        self._last_update: Optional[datetime] = None

    # ---- Quote ----
    def update_quote(self, quote: T.Quote) -> None:
        with self._lock:
            self._quotes[quote.symbol] = quote
            self._last_update = datetime.now()

    def get_quote(self, symbol: str) -> Optional[T.Quote]:
        with self._lock:
            return self._quotes.get(symbol)

    def get_quotes(self) -> dict[str, T.Quote]:
        with self._lock:
            return dict(self._quotes)

    def get_quote_snapshot(self, symbols: list[str]) -> list[T.Quote]:
        with self._lock:
            return [self._quotes[s] for s in symbols if s in self._quotes]

    # ---- Signal ----
    def upsert_signal(self, signal: T.Signal) -> None:
        with self._lock:
            self._signals[signal.signal_id] = signal
            lst = self._signals_by_symbol.setdefault(signal.symbol, [])
            if signal.signal_id not in lst:
                lst.append(signal.signal_id)

    def get_signal(self, signal_id: str) -> Optional[T.Signal]:
        with self._lock:
            return self._signals.get(signal_id)

    def get_signals(self) -> dict[str, T.Signal]:
        with self._lock:
            return dict(self._signals)

    def get_signals_by_symbol(self, symbol: str) -> list[T.Signal]:
        with self._lock:
            return [self._signals[sid] for sid in self._signals_by_symbol.get(symbol, [])]

    def active_signal_states(self) -> tuple:
        return (
            T.SignalState.WATCH, T.SignalState.ARMED_BREAKOUT,
            T.SignalState.ARMED_PULLBACK, T.SignalState.TRIGGERED,
            T.SignalState.ACTIVE, T.SignalState.TRIM,
            T.SignalState.OVEREXTENDED,
        )

    # ---- Regime / Sector ----
    def set_regime(self, regime: T.MarketRegime) -> None:
        with self._lock:
            self._regime = regime

    def get_regime(self) -> Optional[T.MarketRegime]:
        with self._lock:
            return self._regime

    def update_sector(self, sector: T.SectorSnapshot) -> None:
        with self._lock:
            self._sectors[sector.sector] = sector

    def get_sectors(self) -> dict[str, T.SectorSnapshot]:
        with self._lock:
            return dict(self._sectors)

    # ---- ProviderHealth ----
    def update_health(self, health: T.ProviderHealth) -> None:
        with self._lock:
            self._health[health.provider] = health

    def get_health(self) -> dict[str, T.ProviderHealth]:
        with self._lock:
            return dict(self._health)

    # ---- Watchlist / Positions ----
    def set_watchlist(self, items: list[T.WatchlistItem]) -> None:
        with self._lock:
            self._watchlist = {it.symbol: it for it in items}

    def get_watchlist(self) -> dict[str, T.WatchlistItem]:
        with self._lock:
            return dict(self._watchlist)

    def add_watch(self, item: T.WatchlistItem) -> None:
        with self._lock:
            self._watchlist[item.symbol] = item

    def remove_watch(self, symbol: str) -> None:
        with self._lock:
            self._watchlist.pop(symbol, None)

    def set_positions(self, items: list[T.Position]) -> None:
        with self._lock:
            self._positions = {it.id: it for it in items}

    def get_positions(self) -> dict[str, T.Position]:
        with self._lock:
            return dict(self._positions)

    def upsert_position(self, item: T.Position) -> None:
        with self._lock:
            self._positions[item.id] = item

    def remove_position(self, position_id: str) -> None:
        with self._lock:
            self._positions.pop(position_id, None)

    def set_portfolio_profile(self, profile: object) -> None:
        with self._lock:
            self._portfolio_profile = profile

    def get_portfolio_profile(self) -> object:
        with self._lock:
            return self._portfolio_profile

    # ---- Instruments ----
    def upsert_instrument(self, symbol: str, meta: dict) -> None:
        with self._lock:
            existing = self._instruments.get(symbol, {})
            existing.update(meta)
            self._instruments[symbol] = existing

    def get_instrument(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._instruments.get(symbol)

    def get_instruments(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._instruments)

    def get_last_update(self) -> Optional[datetime]:
        with self._lock:
            return self._last_update
