"""SQLite 仓储（CRUD + 重启恢复，§12）。

所有 dataclass ↔ JSON 的互转集中在此，供 API serializers 复用。
线程安全：通过线程本地连接（db.py）实现；仓库方法本身无状态。
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional, get_args, get_origin, get_type_hints

from .db import get_connection
from ..core import types as T
from ..decision.types import RiskMode, UserPortfolioProfile


class RepositoryConflictError(ValueError):
    pass


class RepositoryValidationError(ValueError):
    pass


def _validate_position_values(
    *,
    symbol: object,
    market: object,
    shares: object,
    average_cost: object,
    added_at: object,
) -> tuple[str, T.Market, int, float, datetime]:
    if type(symbol) is not str or not symbol.strip():
        raise RepositoryValidationError("symbol must be a non-empty string")
    normalized_symbol = symbol.strip().upper()
    if symbol != normalized_symbol:
        raise RepositoryValidationError("symbol must use canonical uppercase form")
    if not isinstance(market, T.Market):
        raise RepositoryValidationError("market must be Market")
    code, separator, suffix = normalized_symbol.rpartition(".")
    valid_suffixes = {
        T.Market.A: {"SH", "SZ"},
        T.Market.HK: {"HK"},
        T.Market.US: {"US"},
    }
    if not separator or not code or suffix not in valid_suffixes[market]:
        raise RepositoryValidationError("symbol suffix must match market")
    if type(shares) is not int or shares <= 0:
        raise RepositoryValidationError("shares must be a positive integer")
    if type(average_cost) not in (int, float):
        raise RepositoryValidationError("average_cost must be a finite number")
    normalized_cost = float(average_cost)
    if not math.isfinite(normalized_cost) or normalized_cost <= 0:
        raise RepositoryValidationError(
            "average_cost must be finite and greater than zero"
        )
    if not isinstance(added_at, datetime):
        raise RepositoryValidationError("added_at must be a datetime")
    if added_at.tzinfo is None or added_at.utcoffset() is None:
        raise RepositoryValidationError("added_at must be timezone-aware")
    return normalized_symbol, market, shares, normalized_cost, added_at


# --------------------------------------------------------------------------- #
# 通用 dataclass ↔ JSON
# --------------------------------------------------------------------------- #
def to_jsonable(obj: Any) -> Any:
    """递归将 dataclass/枚举/datetime 转为可 JSON 序列化对象。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if is_dataclass(obj):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    return str(obj)


def _resolve(target: Any) -> Any:
    """解包 Optional[...] 得到内部类型。"""
    origin = get_origin(target)
    if origin is not None and origin.__name__ == "Union":
        args = [a for a in get_args(target) if a is not type(None)]
        return args[0] if args else target
    return target


def from_jsonable(cls: type, d: Any) -> Any:
    """由 dict 重建 dataclass（含嵌套 dataclass / 枚举 / datetime）。"""
    if d is None:
        return None
    if not is_dataclass(cls):
        return d
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        name = f.name
        if name not in d:
            kwargs[name] = None
            continue
        v = d[name]
        if v is None:
            kwargs[name] = None
            continue
        t = _resolve(hints.get(name, type(v)))
        if is_dataclass(t):
            kwargs[name] = from_jsonable(t, v)
        elif isinstance(t, type) and issubclass(t, Enum):
            try:
                kwargs[name] = t(v)
            except ValueError:
                kwargs[name] = None
        elif t is datetime:
            kwargs[name] = datetime.fromisoformat(v) if isinstance(v, str) else v
        else:
            kwargs[name] = v
    return cls(**kwargs)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class Repository:
    """SQLite 仓储。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 触发首次建表
        get_connection(db_path)

    # ---- Quote ----
    def save_quote(self, quote: T.Quote) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "REPLACE INTO quotes_cache(symbol, market, data, updated_at) VALUES (?,?,?,?)",
            (quote.symbol, quote.market.value, json.dumps(to_jsonable(quote), ensure_ascii=False),
             datetime.now().isoformat()),
        )
        conn.commit()

    def save_quotes(self, quotes: list[T.Quote]) -> None:
        conn = get_connection(self.db_path)
        rows = [
            (q.symbol, q.market.value, json.dumps(to_jsonable(q), ensure_ascii=False), datetime.now().isoformat())
            for q in quotes
        ]
        conn.executemany("REPLACE INTO quotes_cache(symbol, market, data, updated_at) VALUES (?,?,?,?)", rows)
        conn.commit()

    def load_quotes(self) -> dict[str, T.Quote]:
        conn = get_connection(self.db_path)
        out: dict[str, T.Quote] = {}
        for row in conn.execute("SELECT data FROM quotes_cache"):
            q = from_jsonable(T.Quote, json.loads(row["data"]))
            if q:
                out[q.symbol] = q
        return out

    # ---- Instruments ----
    def save_instrument(self, symbol: str, meta: dict) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "REPLACE INTO instruments(symbol, market, name, sector, exchange, currency, listing_date, is_active, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                symbol, meta.get("market", ""), meta.get("name"), meta.get("sector"),
                meta.get("exchange"), meta.get("currency"), meta.get("listing_date"),
                int(bool(meta.get("is_active", 1))), datetime.now().isoformat(),
            ),
        )
        conn.commit()

    def load_instruments(self) -> dict[str, dict]:
        conn = get_connection(self.db_path)
        out: dict[str, dict] = {}
        for row in conn.execute("SELECT * FROM instruments"):
            d = dict(row)
            out[d["symbol"]] = d
        return out

    # ---- Bars ----
    def save_bar(self, bar: T.Bar) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "REPLACE INTO bars(symbol, market, timestamp, interval, open, high, low, close, volume, amount, turnover, source, adjustment_factor, quality_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bar.symbol, bar.market.value, bar.timestamp.isoformat(), bar.interval, bar.open, bar.high,
             bar.low, bar.close, bar.volume, bar.amount, bar.turnover, bar.source,
             bar.adjustment_factor, bar.quality_status.value),
        )
        conn.commit()

    def load_recent_bars(self, symbol: str, interval: str = "1d", n: int = 260) -> list[T.Bar]:
        conn = get_connection(self.db_path)
        rows = conn.execute(
            "SELECT * FROM bars WHERE symbol=? AND interval=? ORDER BY timestamp DESC LIMIT ?",
            (symbol, interval, n),
        ).fetchall()
        bars = [from_jsonable(T.Bar, dict(r)) for r in reversed(rows)]
        return [b for b in bars if b is not None]

    def save_bars_batch(self, bars: list[T.Bar]) -> int:
        """批量写入 K 线（单事务、幂等 REPLACE）。

        复用 ``save_bar`` 的字段顺序；一次 ``executemany`` 提交，避免逐条 commit 的
        IO 开销。返回实际写入条数。空列表直接跳过（不建事务）。
        """
        if not bars:
            return 0
        conn = get_connection(self.db_path)
        rows = [
            (bar.symbol, bar.market.value, bar.timestamp.isoformat(), bar.interval, bar.open, bar.high,
             bar.low, bar.close, bar.volume, bar.amount, bar.turnover, bar.source,
             bar.adjustment_factor, bar.quality_status.value)
            for bar in bars
        ]
        conn.executemany(
            "REPLACE INTO bars(symbol, market, timestamp, interval, open, high, low, close, volume, amount, turnover, source, adjustment_factor, quality_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)

    def prune_bars(self, symbol: str, interval: str, keep: int) -> int:
        """仅保留每标的最近 ``keep`` 根 K 线，删除更早的历史（控制表体积）。

        ``keep <= 0`` 视为不裁剪。返回删除行数。
        """
        if keep <= 0:
            return 0
        conn = get_connection(self.db_path)
        cur = conn.execute(
            "DELETE FROM bars WHERE symbol=? AND interval=? AND timestamp < ("
            "SELECT timestamp FROM bars WHERE symbol=? AND interval=? "
            "ORDER BY timestamp DESC LIMIT 1 OFFSET ?)",
            (symbol, interval, symbol, interval, max(0, keep - 1)),
        )
        conn.commit()
        return cur.rowcount

    # ---- Watchlist ----
    def save_watchlist(self, items: list[T.WatchlistItem]) -> None:
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT INTO watchlist(symbol, market, added_at, note) VALUES (?,?,?,?)",
            [(it.symbol, it.market.value, it.added_at.isoformat(), it.note) for it in items],
        )
        conn.commit()

    def load_watchlist(self) -> list[T.WatchlistItem]:
        conn = get_connection(self.db_path)
        return [
            T.WatchlistItem(
                symbol=r["symbol"], market=T.Market(r["market"]),
                added_at=datetime.fromisoformat(r["added_at"]) if r["added_at"] else datetime.now(),
                note=r["note"],
            )
            for r in conn.execute("SELECT * FROM watchlist")
        ]

    # ---- Positions ----
    def save_positions(self, items: list[T.Position]) -> None:
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM positions")
        conn.executemany(
            "INSERT INTO positions(id, symbol, market, shares, cost, added_at, closed_at) VALUES (?,?,?,?,?,?,?)",
            [(p.id, p.symbol, p.market.value, p.shares, p.cost,
              p.added_at.isoformat(), p.closed_at.isoformat() if p.closed_at else None) for p in items],
        )
        conn.commit()

    def load_positions(self) -> list[T.Position]:
        conn = get_connection(self.db_path)
        out = []
        for r in conn.execute("SELECT * FROM positions"):
            out.append(T.Position(
                id=r["id"], symbol=r["symbol"], market=T.Market(r["market"]),
                shares=r["shares"], cost=r["cost"],
                added_at=datetime.fromisoformat(r["added_at"]) if r["added_at"] else datetime.now(),
                closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
            ))
        return out

    def get_position(self, position_id: str) -> Optional[T.Position]:
        conn = get_connection(self.db_path)
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return self._row_to_position(row) if row is not None else None

    def create_position(
        self,
        *,
        symbol: str,
        market: T.Market,
        shares: int,
        average_cost: float,
        added_at: datetime,
        position_id: Optional[str] = None,
    ) -> T.Position:
        symbol, market, shares, average_cost, added_at = _validate_position_values(
            symbol=symbol,
            market=market,
            shares=shares,
            average_cost=average_cost,
            added_at=added_at,
        )
        if position_id is not None and (
            type(position_id) is not str or not position_id.strip()
        ):
            raise RepositoryValidationError(
                "position_id must be a non-empty string or None"
            )
        conn = get_connection(self.db_path)
        position = T.Position(
            id=position_id or f"pos-{uuid.uuid4().hex}",
            symbol=symbol,
            market=market,
            shares=shares,
            cost=average_cost,
            added_at=added_at,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND closed_at IS NULL LIMIT 1",
                (symbol,),
            ).fetchone()
            if duplicate is not None:
                raise RepositoryConflictError(f"active position already exists for {symbol}")
            conn.execute(
                "INSERT INTO positions(id, symbol, market, shares, cost, added_at, closed_at)"
                " VALUES (?,?,?,?,?,?,NULL)",
                (position.id, symbol, market.value, shares, average_cost, added_at.isoformat()),
            )
            conn.commit()
        except RepositoryConflictError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise RepositoryConflictError("position identity already exists") from exc
        return position

    def update_position(
        self,
        position_id: str,
        *,
        shares: Optional[int] = None,
        average_cost: Optional[float] = None,
    ) -> Optional[T.Position]:
        if type(position_id) is not str or not position_id.strip():
            raise RepositoryValidationError("position_id must be a non-empty string")
        if shares is not None and (type(shares) is not int or shares <= 0):
            raise RepositoryValidationError("shares must be a positive integer")
        if average_cost is not None:
            if type(average_cost) not in (int, float):
                raise RepositoryValidationError(
                    "average_cost must be a finite number"
                )
            average_cost = float(average_cost)
            if not math.isfinite(average_cost) or average_cost <= 0:
                raise RepositoryValidationError(
                    "average_cost must be finite and greater than zero"
                )
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM positions WHERE id=?", (position_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            new_shares = row["shares"] if shares is None else shares
            new_cost = row["cost"] if average_cost is None else average_cost
            cursor = conn.execute(
                "UPDATE positions SET shares=?, cost=? WHERE id=?",
                (new_shares, new_cost, position_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        position = self._row_to_position(row)
        position.shares = new_shares
        position.cost = new_cost
        return position

    def delete_position(self, position_id: str) -> bool:
        conn = get_connection(self.db_path)
        cur = conn.execute("DELETE FROM positions WHERE id=?", (position_id,))
        conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_position(row: Any) -> T.Position:
        return T.Position(
            id=row["id"],
            symbol=row["symbol"],
            market=T.Market(row["market"]),
            shares=row["shares"],
            cost=row["cost"],
            added_at=datetime.fromisoformat(row["added_at"]) if row["added_at"] else datetime.now(),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        )

    def load_portfolio_profile(self) -> Optional[UserPortfolioProfile]:
        conn = get_connection(self.db_path)
        row = conn.execute("SELECT * FROM portfolio_profile WHERE id=1").fetchone()
        if row is None:
            return None
        return UserPortfolioProfile(
            account_equity=row["account_equity"],
            available_cash=row["available_cash"],
            risk_mode=RiskMode(row["risk_mode"]),
            per_trade_risk_pct=row["per_trade_risk_pct"],
            max_position_pct=row["max_position_pct"],
            max_portfolio_heat_pct=row["max_portfolio_heat_pct"],
            max_sector_pct=row["max_sector_pct"],
            max_theme_pct=row["max_theme_pct"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_portfolio_profile(self, profile: UserPortfolioProfile) -> UserPortfolioProfile:
        conn = get_connection(self.db_path)
        conn.execute(
            "INSERT INTO portfolio_profile("
            "id, account_equity, available_cash, risk_mode, per_trade_risk_pct,"
            "max_position_pct, max_portfolio_heat_pct, max_sector_pct, max_theme_pct, updated_at)"
            " VALUES (1,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " account_equity=excluded.account_equity, available_cash=excluded.available_cash,"
            " risk_mode=excluded.risk_mode, per_trade_risk_pct=excluded.per_trade_risk_pct,"
            " max_position_pct=excluded.max_position_pct,"
            " max_portfolio_heat_pct=excluded.max_portfolio_heat_pct,"
            " max_sector_pct=excluded.max_sector_pct, max_theme_pct=excluded.max_theme_pct,"
            " updated_at=excluded.updated_at",
            (
                profile.account_equity,
                profile.available_cash,
                profile.risk_mode.value,
                profile.per_trade_risk_pct,
                profile.max_position_pct,
                profile.max_portfolio_heat_pct,
                profile.max_sector_pct,
                profile.max_theme_pct,
                profile.updated_at.isoformat(),
            ),
        )
        conn.commit()
        return profile

    # ---- Signals ----
    def upsert_signal(self, sig: T.Signal) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "REPLACE INTO signals("
            "signal_id, symbol, market, strategy_id, state, state_changed_at, previous_state, reason,"
            "entry_low, entry_high, trigger_price, invalidation_price, target_1, target_2, reward_risk, freshness,"
            "market_regime, sector_stage, next_trigger, what_changed, data_status, scores, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sig.signal_id, sig.symbol, sig.market.value, sig.strategy_id, sig.state.value,
                sig.state_changed_at.isoformat(), sig.previous_state.value if sig.previous_state else None,
                sig.reason, sig.entry_low, sig.entry_high, sig.trigger_price, sig.invalidation_price,
                sig.target_1, sig.target_2, sig.reward_risk, sig.freshness, sig.market_regime,
                sig.sector_stage, sig.next_trigger, json.dumps(sig.what_changed, ensure_ascii=False),
                sig.data_status.value, json.dumps(to_jsonable(sig.scores), ensure_ascii=False) if sig.scores else None,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()

    def load_signals(self, states: Optional[list[T.SignalState]] = None) -> dict[str, T.Signal]:
        conn = get_connection(self.db_path)
        sql = "SELECT * FROM signals"
        params: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            sql += f" WHERE state IN ({placeholders})"
            params = [s.value for s in states]
        out: dict[str, T.Signal] = {}
        for r in conn.execute(sql, params):
            out[r["signal_id"]] = self._row_to_signal(r)
        return out

    def _row_to_signal(self, r) -> Optional[T.Signal]:
        scores = from_jsonable(T.ScoreSet, json.loads(r["scores"])) if r["scores"] else None
        try:
            prev = T.SignalState(r["previous_state"]) if r["previous_state"] else None
        except ValueError:
            prev = None
        return T.Signal(
            signal_id=r["signal_id"], symbol=r["symbol"], market=T.Market(r["market"]),
            strategy_id=r["strategy_id"], state=T.SignalState(r["state"]),
            state_changed_at=datetime.fromisoformat(r["state_changed_at"]) if r["state_changed_at"] else datetime.now(),
            previous_state=prev, reason=r["reason"] or "",
            entry_low=r["entry_low"] or 0.0, entry_high=r["entry_high"] or 0.0,
            trigger_price=r["trigger_price"] or 0.0, invalidation_price=r["invalidation_price"] or 0.0,
            target_1=r["target_1"] or 0.0, target_2=r["target_2"] or 0.0,
            reward_risk=r["reward_risk"] or 0.0, freshness=r["freshness"] if r["freshness"] is not None else 1.0,
            market_regime=r["market_regime"] or "", sector_stage=r["sector_stage"] or "",
            next_trigger=r["next_trigger"] or "",
            what_changed=json.loads(r["what_changed"]) if r["what_changed"] else [],
            data_status=T.DataStatus(r["data_status"]) if r["data_status"] else T.DataStatus.UNKNOWN,
            scores=scores,
        )

    def append_signal_history(self, signal_id: str, from_state: Optional[str], to_state: str,
                              at: datetime, reason: str, what_changed: list[str]) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "INSERT INTO signal_history(signal_id, from_state, to_state, at, reason, what_changed)"
            " VALUES (?,?,?,?,?,?)",
            (signal_id, from_state, to_state, at.isoformat(), reason,
             json.dumps(what_changed, ensure_ascii=False)),
        )
        conn.commit()

    def load_signal_history(self, signal_id: str) -> list[dict]:
        conn = get_connection(self.db_path)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signal_history WHERE signal_id=? ORDER BY id ASC", (signal_id,))]

    # ---- Provider state ----
    def save_provider_state(self, provider: str, circuit_state: str, last_success_at: Optional[str],
                            extra: dict) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "REPLACE INTO provider_state(provider, circuit_state, last_success_at, extra) VALUES (?,?,?,?)",
            (provider, circuit_state, last_success_at, json.dumps(extra, ensure_ascii=False)),
        )
        conn.commit()

    def load_provider_states(self) -> dict[str, dict]:
        conn = get_connection(self.db_path)
        out: dict[str, dict] = {}
        for r in conn.execute("SELECT * FROM provider_state"):
            out[r["provider"]] = {
                "circuit_state": r["circuit_state"],
                "last_success_at": r["last_success_at"],
                "extra": json.loads(r["extra"]) if r["extra"] else {},
            }
        return out

    def recover_provider_states(self) -> None:
        """重启恢复：熔断态重置为 HALF_OPEN（§12）。"""
        conn = get_connection(self.db_path)
        conn.execute("UPDATE provider_state SET circuit_state='HALF_OPEN', last_success_at=NULL")
        conn.commit()

    # ---- Events（S3 占位，#17.5 仅注入） ----
    def save_event(self, event: dict) -> None:
        conn = get_connection(self.db_path)
        conn.execute(
            "INSERT INTO events(symbol, market, event_type, direction, published_at, usable_from, confirmed, weight, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event.get("symbol"), event.get("market"), event.get("event_type"), event.get("direction"),
             event.get("published_at"), event.get("usable_from"), int(bool(event.get("confirmed", False))),
             event.get("weight", 0.0), json.dumps(event.get("payload", {}), ensure_ascii=False),
             datetime.now().isoformat()),
        )
        conn.commit()

    def load_events(self, symbol: Optional[str] = None, limit: int = 50) -> list[dict]:
        conn = get_connection(self.db_path)
        if symbol:
            rows = conn.execute("SELECT * FROM events WHERE symbol=? ORDER BY id DESC LIMIT ?",
                                (symbol, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
