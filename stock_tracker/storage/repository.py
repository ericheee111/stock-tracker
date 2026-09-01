"""SQLite 仓储（CRUD + 重启恢复，§12）。

所有 dataclass ↔ JSON 的互转集中在此，供 API serializers 复用。
线程安全：通过线程本地连接（db.py）实现；仓库方法本身无状态。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

from ..core import types as T
from ..decision.types import RiskMode, UserPortfolioProfile
from ..runtime_evidence.contracts import (
    RuntimeDecisionArtifact,
    RuntimeDecisionDraft,
    RuntimeEvidenceContractError,
    build_runtime_decision_artifact,
    require_utc_clock_value,
    runtime_signal_version_id,
)
from ..runtime_evidence.store import (
    RuntimeArtifactAppendResult,
    RuntimeArtifactAuditReport,
)
from .db import get_connection
from .runtime_migrations import (
    RuntimeMigrationError,
    RuntimeMigrationRequired,
    audit_runtime_evidence_schema,
)


class RepositoryConflictError(ValueError):
    pass


class RepositoryValidationError(ValueError):
    pass


class RuntimeEvidenceUnavailableError(RuntimeError):
    pass


class SignalPersistenceError(RuntimeError):
    pass


class RuntimeDatabaseIntegrityError(RuntimeError):
    def __init__(self, message: str, *, code: str = "RUNTIME_DATABASE_INTEGRITY") -> None:
        super().__init__(message)
        self.code = code


class RuntimeOutboxError(RuntimeDatabaseIntegrityError):
    def __init__(self, message: str, *, code: str = "RUNTIME_OUTBOX_INTEGRITY") -> None:
        super().__init__(message, code=code)


class RuntimeOutboxConflict(RuntimeOutboxError):
    pass


class RuntimeWorkerIntegrityBlocked(RuntimeDatabaseIntegrityError):
    def __init__(self, artifact_id: str, block_code: str) -> None:
        super().__init__(block_code)
        self.artifact_id = artifact_id
        self.block_code = block_code


@dataclass(frozen=True, slots=True)
class RuntimeSignalPersistence:
    evidence_status: str
    artifact_id: str | None
    outbox_append_order: int | None
    quarantine_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeOutboxLease:
    append_order: int
    artifact_id: str
    runtime_signal_id: str
    transition_event_id: str
    decision_content_id: str
    occurrence_dedup_id: str
    payload_json: str
    payload_sha256: str
    retry_count: int
    lease_owner: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.append_order) is not int or self.append_order < 1:
            raise RuntimeOutboxError("lease append_order must be a positive integer")
        _require_runtime_id(self.artifact_id, "lease artifact_id", sha256=True)
        _require_runtime_id(self.runtime_signal_id, "lease runtime_signal_id")
        _require_runtime_id(
            self.transition_event_id,
            "lease transition_event_id",
            sha256=True,
        )
        _require_runtime_id(
            self.decision_content_id,
            "lease decision_content_id",
            sha256=True,
        )
        _require_runtime_id(
            self.occurrence_dedup_id,
            "lease occurrence_dedup_id",
            sha256=True,
        )
        if type(self.payload_json) is not str:
            raise RuntimeOutboxError("lease payload_json must be text")
        if _runtime_sha256_text(self.payload_json) != _require_runtime_id(
            self.payload_sha256,
            "lease payload_sha256",
            sha256=True,
        ):
            raise RuntimeOutboxError("lease payload SHA mismatch")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise RuntimeOutboxError("lease retry_count must be non-negative")
        _require_runtime_id(self.lease_owner, "lease owner")
        object.__setattr__(
            self,
            "lease_expires_at",
            require_utc_clock_value(self.lease_expires_at, "lease_expires_at"),
        )


def _runtime_utc_text(value: object, name: str) -> str:
    return (
        require_utc_clock_value(value, name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _runtime_utc_from_text(value: object, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z") or len(value) > 64:
        raise RuntimeOutboxError(f"{name} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeOutboxError(f"{name} must be ISO-8601") from exc
    if _runtime_utc_text(parsed, name) != value:
        raise RuntimeOutboxError(f"{name} must be canonical UTC")
    return parsed


def _runtime_canonical_json(value: dict[str, Any]) -> str:
    if any(type(key) is not str for key in value):
        raise RuntimeOutboxError("runtime metadata keys must be strings")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeOutboxError("runtime metadata is not canonical JSON") from exc


def _runtime_sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_runtime_id(value: object, name: str, *, sha256: bool = False) -> str:
    if type(value) is not str or value != value.strip() or not value or len(value) > 256:
        raise RuntimeOutboxError(f"{name} must be a safe string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeOutboxError(f"{name} contains control characters")
    if sha256 and (
        len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeOutboxError(f"{name} must be lowercase SHA-256")
    return value


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
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def save_quotes(self, quotes: list[T.Quote]) -> None:
        conn = get_connection(self.db_path)
        rows = [
            (q.symbol, q.market.value, json.dumps(to_jsonable(q), ensure_ascii=False), datetime.now(timezone.utc).isoformat())
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
                int(bool(meta.get("is_active", 1))), datetime.now(timezone.utc).isoformat(),
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
                added_at=datetime.fromisoformat(r["added_at"]) if r["added_at"] else datetime.now(timezone.utc),
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
                added_at=datetime.fromisoformat(r["added_at"]) if r["added_at"] else datetime.now(timezone.utc),
                closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
            ))
        return out

    def get_position(self, position_id: str) -> T.Position | None:
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
        position_id: str | None = None,
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
        shares: int | None = None,
        average_cost: float | None = None,
    ) -> T.Position | None:
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
            added_at=datetime.fromisoformat(row["added_at"]) if row["added_at"] else datetime.now(timezone.utc),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        )

    def load_portfolio_profile(self) -> UserPortfolioProfile | None:
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
    @staticmethod
    def _upsert_signal_connection(
        conn: sqlite3.Connection, sig: T.Signal, updated_at: datetime
    ) -> None:
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
                updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _append_signal_history_connection(
        conn: sqlite3.Connection,
        signal_id: str,
        from_state: str | None,
        to_state: str,
        at: datetime,
        reason: str,
        what_changed: list[str],
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO signal_history(signal_id, from_state, to_state, at, reason, what_changed)"
            " VALUES (?,?,?,?,?,?)",
            (
                signal_id,
                from_state,
                to_state,
                at.isoformat(),
                reason,
                json.dumps(what_changed, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)

    def upsert_signal(self, sig: T.Signal) -> None:
        conn = get_connection(self.db_path)
        self._upsert_signal_connection(conn, sig, datetime.now(timezone.utc))
        conn.commit()

    @staticmethod
    def _runtime_store_id_connection(conn: sqlite3.Connection) -> str:
        try:
            return audit_runtime_evidence_schema(conn)
        except RuntimeMigrationRequired as exc:
            raise RuntimeEvidenceUnavailableError(
                "runtime evidence schema is unavailable"
            ) from exc
        except RuntimeMigrationError as exc:
            raise RuntimeDatabaseIntegrityError(
                "runtime database integrity audit failed"
            ) from exc

    def runtime_evidence_store_id(self) -> str:
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            store_id = self._runtime_store_id_connection(conn)
            self._audit_runtime_outbox_state_connection(conn)
            conn.rollback()
            return store_id
        except (RuntimeEvidenceUnavailableError, RuntimeDatabaseIntegrityError):
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise SignalPersistenceError(
                "runtime evidence store lookup failed"
            ) from exc

    @staticmethod
    def _next_runtime_quarantine_order(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(quarantine_order),0)+1 FROM runtime_outbox_quarantine"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _audit_runtime_outbox_state_connection(conn: sqlite3.Connection) -> None:
        runtime_store_id = audit_runtime_evidence_schema(conn)
        delivery_rows = conn.execute(
            "SELECT * FROM runtime_outbox_delivery ORDER BY artifact_id"
        ).fetchall()
        status_by_artifact: dict[str, str] = {}
        for row in delivery_rows:
            artifact_id = _require_runtime_id(
                row["artifact_id"], "delivery artifact_id", sha256=True
            )
            status = str(row["status"])
            status_by_artifact[artifact_id] = status
            retry_count = row["retry_count"]
            if type(retry_count) is not int or retry_count < 0:
                raise RuntimeOutboxError("runtime retry_count is invalid")
            lease_owner = row["lease_owner"]
            lease_expiry = row["lease_expires_at"]
            next_retry = row["next_retry_at"]
            last_error = row["last_error_code"]
            record_hash = row["delivered_record_hash"]
            audit_id = row["delivered_audit_id"]
            delivered_at = row["delivered_at"]
            if lease_owner is not None:
                _require_runtime_id(lease_owner, "lease_owner")
            if lease_expiry is not None:
                _runtime_utc_from_text(lease_expiry, "lease_expires_at")
            if next_retry is not None:
                _runtime_utc_from_text(next_retry, "next_retry_at")
            if last_error is not None:
                _require_runtime_id(last_error, "last_error_code")
            if record_hash is not None:
                _require_runtime_id(record_hash, "delivered_record_hash", sha256=True)
            if audit_id is not None:
                _require_runtime_id(audit_id, "delivered_audit_id", sha256=True)
            if delivered_at is not None:
                _runtime_utc_from_text(delivered_at, "delivered_at")
            valid = {
                "PENDING": lease_owner is None
                and lease_expiry is None
                and record_hash is None
                and audit_id is None
                and delivered_at is None,
                "LEASED": lease_owner is not None
                and lease_expiry is not None
                and next_retry is None
                and last_error is None
                and record_hash is None
                and audit_id is None
                and delivered_at is None,
                "DELIVERED": lease_owner is None
                and lease_expiry is None
                and next_retry is None
                and last_error is None
                and record_hash is not None
                and audit_id is not None
                and delivered_at is not None,
                "QUARANTINED": lease_owner is None
                and lease_expiry is None
                and next_retry is None
                and last_error is not None
                and record_hash is None
                and audit_id is None
                and delivered_at is None,
            }.get(status, False)
            if not valid:
                raise RuntimeOutboxError("runtime delivery state is inconsistent")

        outbox = conn.execute(
            "SELECT o.*,c.occurrence_order,c.occurrence_dedup_id,"
            "c.decision_content_id,c.upstream_occurrence_id,c.signal_history_id "
            "FROM runtime_transition_outbox o LEFT JOIN runtime_transition_occurrence c "
            "ON c.artifact_id=o.artifact_id ORDER BY o.append_order"
        ).fetchall()
        orphan_occurrence = conn.execute(
            "SELECT COUNT(*) FROM runtime_transition_occurrence c LEFT JOIN "
            "runtime_transition_outbox o ON o.artifact_id=c.artifact_id "
            "WHERE o.artifact_id IS NULL"
        ).fetchone()[0]
        if orphan_occurrence:
            raise RuntimeOutboxError("runtime occurrence relation is incomplete")
        previous_created_at: datetime | None = None
        for expected_order, row in enumerate(outbox, start=1):
            if (
                int(row["append_order"]) != expected_order
                or row["occurrence_order"] is None
                or int(row["occurrence_order"]) != expected_order
            ):
                raise RuntimeOutboxError("runtime occurrence order is not contiguous")
            artifact_id = _require_runtime_id(row["artifact_id"], "artifact_id", sha256=True)
            runtime_signal_id = _require_runtime_id(row["runtime_signal_id"], "runtime_signal_id")
            transition_event_id = _require_runtime_id(
                row["transition_event_id"], "transition_event_id", sha256=True
            )
            decision_content_id = _require_runtime_id(
                row["decision_content_id"], "decision_content_id", sha256=True
            )
            occurrence_dedup_id = _require_runtime_id(
                row["occurrence_dedup_id"], "occurrence_dedup_id", sha256=True
            )
            created_at = _runtime_utc_from_text(row["created_at"], "outbox created_at")
            if previous_created_at is not None and created_at < previous_created_at:
                raise RuntimeOutboxError("runtime outbox clock moved backward")
            previous_created_at = created_at
            if artifact_id not in status_by_artifact:
                raise RuntimeOutboxError("runtime outbox delivery relation is incomplete")
            if status_by_artifact[artifact_id] == "QUARANTINED":
                continue
            payload = str(row["payload_json"])
            if _runtime_sha256_text(payload) != row["payload_sha256"]:
                raise RuntimeOutboxError("runtime outbox payload SHA mismatch")
            try:
                artifact = RuntimeDecisionArtifact.from_json_bytes(payload.encode("utf-8"))
            except RuntimeEvidenceContractError as exc:
                raise RuntimeOutboxError("runtime outbox artifact contract is invalid") from exc
            identity = artifact.identity_dict()
            if (
                artifact.artifact_id != artifact_id
                or identity["runtime_store_id"] != runtime_store_id
                or identity["runtime_signal_id"] != runtime_signal_id
                or identity["transition_event_id"] != transition_event_id
                or identity["decision_content_id"] != decision_content_id
                or identity["occurrence_dedup_id"] != occurrence_dedup_id
                or identity["upstream_occurrence_id"] != row["upstream_occurrence_id"]
                or artifact.to_json_bytes().decode("utf-8") != payload
                or _runtime_utc_from_text(identity["observed_at_utc"], "artifact observed_at")
                != created_at
            ):
                raise RuntimeOutboxError("runtime occurrence disagrees with its artifact")
            history = conn.execute(
                "SELECT * FROM signal_history WHERE id=?",
                (row["signal_history_id"],),
            ).fetchone()
            signal_document = identity["signal_snapshot"]["signal"]
            if history is None:
                raise RuntimeOutboxError("runtime occurrence signal history is missing")
            try:
                history_time = require_utc_clock_value(
                    datetime.fromisoformat(str(history["at"])), "signal history time"
                )
                history_changes = json.loads(str(history["what_changed"]))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeOutboxError("runtime occurrence signal history is invalid") from exc
            if (
                str(history["signal_id"]) != runtime_signal_id
                or history["from_state"] != identity["previous_state"]
                or str(history["to_state"]) != identity["state"]
                or history_time
                != _runtime_utc_from_text(identity["state_changed_at_utc"], "state_changed_at")
                or str(history["reason"]) != signal_document["reason"]
                or history_changes != signal_document["what_changed"]
            ):
                raise RuntimeOutboxError("runtime occurrence signal history disagrees")
        missing_delivery = conn.execute(
            "SELECT COUNT(*) FROM runtime_transition_outbox o "
            "LEFT JOIN runtime_outbox_delivery d ON d.artifact_id=o.artifact_id "
            "WHERE d.artifact_id IS NULL"
        ).fetchone()[0]
        orphan_delivery = conn.execute(
            "SELECT COUNT(*) FROM runtime_outbox_delivery d "
            "LEFT JOIN runtime_transition_outbox o ON o.artifact_id=d.artifact_id "
            "WHERE o.artifact_id IS NULL"
        ).fetchone()[0]
        if missing_delivery or orphan_delivery:
            raise RuntimeOutboxError("runtime outbox delivery relation is incomplete")
        maximum_order = len(outbox)
        for row in conn.execute(
            "SELECT worker_id,last_contiguous_order,updated_at FROM runtime_outbox_cursor"
        ).fetchall():
            _require_runtime_id(row["worker_id"], "cursor worker_id")
            last_order = row["last_contiguous_order"]
            if type(last_order) is not int or not 0 <= last_order <= maximum_order:
                raise RuntimeOutboxError("runtime cursor is outside outbox bounds")
            _runtime_utc_from_text(row["updated_at"], "cursor updated_at")
            terminal_count = sum(
                status_by_artifact[str(item["artifact_id"])]
                in {"DELIVERED", "QUARANTINED"}
                for item in outbox[:last_order]
            )
            if terminal_count != last_order:
                raise RuntimeOutboxError("runtime cursor skipped non-terminal delivery")
        quarantine = conn.execute(
            "SELECT * FROM runtime_outbox_quarantine ORDER BY quarantine_order"
        ).fetchall()
        for expected_order, row in enumerate(quarantine, start=1):
            quarantine_order = int(row["quarantine_order"])
            if quarantine_order != expected_order:
                raise RuntimeOutboxError("runtime quarantine order is not contiguous")
            quarantine_id = _require_runtime_id(
                row["quarantine_id"],
                "quarantine_id",
                sha256=True,
            )
            runtime_signal_id = _require_runtime_id(
                row["runtime_signal_id"],
                "runtime_signal_id",
            )
            reason_code = _require_runtime_id(row["reason_code"], "reason_code")
            quarantined_at = _runtime_utc_from_text(
                row["quarantined_at"],
                "quarantined_at",
            )
            metadata_json = str(row["metadata_json"])
            if _runtime_sha256_text(metadata_json) != row["metadata_sha256"]:
                raise RuntimeOutboxError("runtime quarantine metadata SHA mismatch")
            try:
                metadata = json.loads(metadata_json)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise RuntimeOutboxError("runtime quarantine metadata is invalid") from exc
            if not isinstance(metadata, dict) or _runtime_canonical_json(metadata) != metadata_json:
                raise RuntimeOutboxError("runtime quarantine metadata is not canonical")
            artifact_id_value = row["artifact_id"]
            append_order_value = row["outbox_append_order"]
            if (artifact_id_value is None) != (append_order_value is None):
                raise RuntimeOutboxError("runtime quarantine outbox reference is incomplete")
            artifact_id = None
            append_order = None
            if artifact_id_value is not None:
                artifact_id = _require_runtime_id(
                    artifact_id_value,
                    "quarantine artifact_id",
                    sha256=True,
                )
                if type(append_order_value) is not int or append_order_value < 1:
                    raise RuntimeOutboxError(
                        "quarantine outbox_append_order is invalid"
                    )
                append_order = append_order_value
                linked = conn.execute(
                    "SELECT 1 FROM runtime_transition_outbox WHERE artifact_id=? "
                    "AND append_order=? AND runtime_signal_id=?",
                    (artifact_id, append_order, runtime_signal_id),
                ).fetchone()
                if linked is None:
                    raise RuntimeOutboxError("runtime quarantine outbox reference is invalid")
            identity = _runtime_canonical_json(
                {
                    "schema": "stage4g1-runtime-outbox-quarantine-identity-v1",
                    "runtime_store_id": runtime_store_id,
                    "quarantine_order": quarantine_order,
                    "artifact_id": artifact_id,
                    "outbox_append_order": append_order,
                    "runtime_signal_id": runtime_signal_id,
                    "reason_code": reason_code,
                    "quarantined_at": _runtime_utc_text(
                        quarantined_at,
                        "quarantined_at",
                    ),
                    "metadata_sha256": str(row["metadata_sha256"]),
                }
            )
            if _runtime_sha256_text(identity) != quarantine_id:
                raise RuntimeOutboxError("runtime quarantine identity mismatch")
        block_rows = conn.execute(
            "SELECT * FROM runtime_worker_integrity_block ORDER BY block_order"
        ).fetchall()
        for expected_order, row in enumerate(block_rows, start=1):
            if int(row["block_order"]) != expected_order:
                raise RuntimeOutboxError("runtime worker block order is not contiguous")
            block_id = _require_runtime_id(row["block_id"], "block_id", sha256=True)
            worker_id = _require_runtime_id(row["worker_id"], "blocked worker_id")
            artifact_id = _require_runtime_id(row["artifact_id"], "blocked artifact_id", sha256=True)
            block_code = _require_runtime_id(row["block_code"], "block_code")
            blocked_at = _runtime_utc_from_text(row["blocked_at"], "blocked_at")
            expected_block_id = _runtime_sha256_text(
                _runtime_canonical_json(
                    {
                        "schema": "stage4g1-runtime-worker-integrity-block-v1",
                        "runtime_store_id": runtime_store_id,
                        "block_order": expected_order,
                        "worker_id": worker_id,
                        "artifact_id": artifact_id,
                        "block_code": block_code,
                        "blocked_at": _runtime_utc_text(blocked_at, "blocked_at"),
                    }
                )
            )
            if block_id != expected_block_id:
                raise RuntimeOutboxError("runtime worker block identity mismatch")
            linked = conn.execute(
                "SELECT append_order FROM runtime_transition_outbox WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if linked is None:
                raise RuntimeOutboxError("runtime worker block artifact is missing")
            cursor = conn.execute(
                "SELECT last_contiguous_order FROM runtime_outbox_cursor WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
            if cursor is not None and int(cursor[0]) >= int(linked[0]):
                raise RuntimeOutboxError("runtime worker block cursor advanced")

    @classmethod
    def _append_runtime_quarantine_connection(
        cls,
        conn: sqlite3.Connection,
        *,
        runtime_signal_id: str,
        reason_code: str,
        quarantined_at: datetime,
        metadata: dict[str, Any],
        artifact_id: str | None = None,
        outbox_append_order: int | None = None,
    ) -> str:
        signal_id = _require_runtime_id(runtime_signal_id, "runtime_signal_id")
        reason = _require_runtime_id(reason_code, "reason_code")
        artifact = (
            None
            if artifact_id is None
            else _require_runtime_id(artifact_id, "artifact_id", sha256=True)
        )
        if outbox_append_order is not None and (
            type(outbox_append_order) is not int or outbox_append_order < 1
        ):
            raise RuntimeOutboxError("outbox_append_order must be a positive integer")
        at_text = _runtime_utc_text(quarantined_at, "quarantined_at")
        metadata_json = _runtime_canonical_json(metadata)
        metadata_sha = _runtime_sha256_text(metadata_json)
        quarantine_order = cls._next_runtime_quarantine_order(conn)
        runtime_store_id = cls._runtime_store_id_connection(conn)
        identity = _runtime_canonical_json(
            {
                "schema": "stage4g1-runtime-outbox-quarantine-identity-v1",
                "runtime_store_id": runtime_store_id,
                "quarantine_order": quarantine_order,
                "artifact_id": artifact,
                "outbox_append_order": outbox_append_order,
                "runtime_signal_id": signal_id,
                "reason_code": reason,
                "quarantined_at": at_text,
                "metadata_sha256": metadata_sha,
            }
        )
        quarantine_id = _runtime_sha256_text(identity)
        conn.execute(
            "INSERT INTO runtime_outbox_quarantine("
            "quarantine_order,quarantine_id,artifact_id,outbox_append_order,"
            "runtime_signal_id,reason_code,quarantined_at,metadata_json,metadata_sha256)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                quarantine_order,
                quarantine_id,
                artifact,
                outbox_append_order,
                signal_id,
                reason,
                at_text,
                metadata_json,
                metadata_sha,
            ),
        )
        return quarantine_id

    def persist_signal_decision(
        self,
        sig: T.Signal,
        *,
        changed: bool,
        observed_at: datetime,
        expected_previous_signal_version_id: str | None,
        decision_draft: RuntimeDecisionDraft | None = None,
        quarantine_reason: str | None = None,
    ) -> RuntimeSignalPersistence:
        if type(sig) is not T.Signal:
            raise RuntimeOutboxError("sig must be the exact Signal type")
        if type(changed) is not bool:
            raise RuntimeOutboxError("changed must be boolean")
        observed = require_utc_clock_value(observed_at, "observed_at")
        expected_previous = expected_previous_signal_version_id
        if expected_previous is not None:
            expected_previous = _require_runtime_id(
                expected_previous,
                "expected_previous_signal_version_id",
                sha256=True,
            )
        try:
            target_signal_version_id = runtime_signal_version_id(
                sig,
                allow_legacy_naive_datetime=True,
            )
        except RuntimeEvidenceContractError as exc:
            raise RuntimeOutboxError("runtime signal version is invalid") from exc
        if target_signal_version_id is None:
            raise RuntimeOutboxError("runtime signal version is unavailable")
        if decision_draft is not None and type(decision_draft) is not RuntimeDecisionDraft:
            raise RuntimeOutboxError("decision_draft must be RuntimeDecisionDraft")
        if decision_draft is not None and quarantine_reason is not None:
            raise RuntimeOutboxError("decision_draft and quarantine_reason are mutually exclusive")
        if not changed and (decision_draft is not None or quarantine_reason is not None):
            raise RuntimeOutboxError(
                "runtime evidence may only be attached to an actual signal transition"
            )
        draft_identity: dict[str, Any] | None = None
        if decision_draft is not None:
            draft_identity = decision_draft.identity_dict()
            if (
                draft_identity["runtime_signal_id"] != sig.signal_id
                or draft_identity["signal_version_id"]
                != target_signal_version_id
                or draft_identity["expected_previous_signal_version_id"]
                != expected_previous
                or _runtime_utc_from_text(
                    draft_identity["observed_at_utc"],
                    "draft observed_at",
                )
                != observed
            ):
                raise RuntimeOutboxError(
                    "runtime decision draft disagrees with persistence inputs"
                )
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            runtime_store_id: str | None = None
            if decision_draft is not None or quarantine_reason is not None:
                runtime_store_id = self._runtime_store_id_connection(conn)
                self._audit_runtime_outbox_state_connection(conn)
                self._assert_runtime_clock_not_before_state(conn, observed)
            if draft_identity is not None and (
                draft_identity["runtime_store_id"] != runtime_store_id
            ):
                raise RuntimeOutboxError(
                    "runtime decision draft is bound to a different evidence store"
                )

            current_row = conn.execute(
                "SELECT * FROM signals WHERE signal_id=?",
                (sig.signal_id,),
            ).fetchone()
            current_signal = (
                None if current_row is None else self._row_to_signal(current_row)
            )
            try:
                current_signal_version_id = runtime_signal_version_id(
                    current_signal,
                    allow_legacy_naive_datetime=True,
                )
            except RuntimeEvidenceContractError as exc:
                raise RuntimeOutboxError(
                    "persisted runtime signal version is invalid"
                ) from exc

            existing_occurrence = None
            if decision_draft is not None:
                existing_occurrence = conn.execute(
                    "SELECT o.append_order,o.artifact_id,o.transition_event_id,"
                    "o.payload_json,o.payload_sha256,d.status "
                    "FROM runtime_transition_occurrence c "
                    "JOIN runtime_transition_outbox o ON o.artifact_id=c.artifact_id "
                    "JOIN runtime_outbox_delivery d ON d.artifact_id=o.artifact_id "
                    "WHERE c.occurrence_dedup_id=?",
                    (decision_draft.occurrence_dedup_id,),
                ).fetchone()
            if changed and existing_occurrence is not None:
                if current_signal_version_id != target_signal_version_id:
                    raise RuntimeOutboxConflict(
                        "persisted occurrence disagrees with current signal"
                    )
                persisted_transition_id = _require_runtime_id(
                    existing_occurrence["transition_event_id"],
                    "persisted transition_event_id",
                    sha256=True,
                )
                candidate = build_runtime_decision_artifact(
                    draft=decision_draft,
                    transition_event_id=persisted_transition_id,
                )
                payload_json = candidate.to_json_bytes().decode("utf-8")
                if (
                    str(existing_occurrence["artifact_id"]) != candidate.artifact_id
                    or str(existing_occurrence["payload_json"]) != payload_json
                    or str(existing_occurrence["payload_sha256"])
                    != _runtime_sha256_text(payload_json)
                ):
                    raise RuntimeOutboxConflict(
                        "runtime occurrence dedup identity has payload drift"
                    )
                conn.rollback()
                return RuntimeSignalPersistence(
                    f"OUTBOX_{existing_occurrence['status']}",
                    candidate.artifact_id,
                    int(existing_occurrence["append_order"]),
                    None,
                )

            if changed:
                if current_signal_version_id == target_signal_version_id:
                    conn.rollback()
                    return RuntimeSignalPersistence(
                        "SIGNAL_ALREADY_PERSISTED_WITHOUT_OUTBOX",
                        None,
                        None,
                        None,
                    )
                if current_signal_version_id != expected_previous:
                    raise RuntimeOutboxConflict(
                        "runtime signal changed after the decision snapshot"
                    )
                if current_signal is None:
                    if expected_previous is not None or sig.previous_state is not None:
                        raise RuntimeOutboxConflict(
                            "new runtime signal has an invalid previous-state identity"
                        )
                elif (
                    sig.previous_state is not current_signal.state
                    or (
                        sig.state is current_signal.state
                        and (
                            draft_identity is None
                            or draft_identity["upstream_occurrence_id"] is None
                        )
                    )
                ):
                    raise RuntimeOutboxConflict(
                        "runtime transition disagrees with the persisted previous state"
                    )
            else:
                if current_signal is None:
                    raise RuntimeOutboxConflict(
                        "unchanged runtime signal has no persisted predecessor"
                    )
                if current_signal_version_id != expected_previous:
                    raise RuntimeOutboxConflict(
                        "runtime signal changed after the refresh snapshot"
                    )
                if sig.state is not current_signal.state:
                    raise RuntimeOutboxConflict(
                        "unchanged runtime signal cannot change state"
                    )

            self._upsert_signal_connection(conn, sig, observed)
            history_id: int | None = None
            if changed:
                history_id = self._append_signal_history_connection(
                    conn,
                    sig.signal_id,
                    sig.previous_state.value if sig.previous_state else None,
                    sig.state.value,
                    sig.state_changed_at,
                    sig.reason,
                    sig.what_changed,
                )
            if decision_draft is not None and draft_identity is not None:
                if history_id is None:
                    raise RuntimeOutboxError("runtime occurrence requires signal history")
                transition_event_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
                artifact = build_runtime_decision_artifact(
                    draft=decision_draft,
                    transition_event_id=transition_event_id,
                )
                payload_json = artifact.to_json_bytes().decode("utf-8")
                payload_sha = _runtime_sha256_text(payload_json)
                row = conn.execute(
                    "SELECT COALESCE(MAX(append_order),0)+1 FROM runtime_transition_outbox"
                ).fetchone()
                append_order = int(row[0])
                conn.execute(
                    "INSERT INTO runtime_transition_outbox("
                    "append_order,artifact_id,runtime_signal_id,transition_event_id,"
                    "payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        append_order,
                        artifact.artifact_id,
                        sig.signal_id,
                        transition_event_id,
                        payload_json,
                        payload_sha,
                        _runtime_utc_text(observed, "created_at"),
                    ),
                )
                conn.execute(
                    "INSERT INTO runtime_transition_occurrence("
                    "occurrence_order,occurrence_dedup_id,decision_content_id,artifact_id,"
                    "transition_event_id,upstream_occurrence_id,signal_history_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        append_order,
                        decision_draft.occurrence_dedup_id,
                        decision_draft.decision_content_id,
                        artifact.artifact_id,
                        transition_event_id,
                        draft_identity["upstream_occurrence_id"],
                        history_id,
                        _runtime_utc_text(observed, "created_at"),
                    ),
                )
                conn.execute(
                    "INSERT INTO runtime_outbox_delivery(artifact_id,status,retry_count) "
                    "VALUES(?,'PENDING',0)",
                    (artifact.artifact_id,),
                )
                self._audit_runtime_outbox_state_connection(conn)
                conn.commit()
                return RuntimeSignalPersistence(
                    "OUTBOX_PENDING", artifact.artifact_id, append_order, None
                )
            quarantine_id: str | None = None
            if quarantine_reason is not None:
                quarantine_id = self._append_runtime_quarantine_connection(
                    conn,
                    runtime_signal_id=sig.signal_id,
                    reason_code=quarantine_reason,
                    quarantined_at=observed,
                    metadata={
                        "stage": "ARTIFACT_BUILD",
                        "target_signal_version_id": target_signal_version_id,
                        "expected_previous_signal_version_id": expected_previous,
                    },
                )
                self._audit_runtime_outbox_state_connection(conn)
            conn.commit()
            return RuntimeSignalPersistence(
                "QUARANTINED" if quarantine_id is not None else "NOT_REQUESTED",
                None,
                None,
                quarantine_id,
            )
        except RuntimeEvidenceUnavailableError:
            conn.rollback()
            raise
        except (RuntimeDatabaseIntegrityError, RuntimeOutboxError):
            conn.rollback()
            raise
        except RuntimeMigrationRequired as exc:
            conn.rollback()
            raise RuntimeEvidenceUnavailableError(
                "runtime evidence schema is unavailable"
            ) from exc
        except RuntimeMigrationError as exc:
            conn.rollback()
            raise RuntimeDatabaseIntegrityError(
                "runtime database integrity audit failed"
            ) from exc
        except sqlite3.Error as exc:
            conn.rollback()
            raise SignalPersistenceError("signal durable commit failed") from exc
        except Exception:
            conn.rollback()
            raise

    def load_signals(self, states: list[T.SignalState] | None = None) -> dict[str, T.Signal]:
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

    def _row_to_signal(self, r) -> T.Signal | None:
        scores = from_jsonable(T.ScoreSet, json.loads(r["scores"])) if r["scores"] else None
        try:
            prev = T.SignalState(r["previous_state"]) if r["previous_state"] else None
        except ValueError:
            prev = None
        return T.Signal(
            signal_id=r["signal_id"], symbol=r["symbol"], market=T.Market(r["market"]),
            strategy_id=r["strategy_id"], state=T.SignalState(r["state"]),
            state_changed_at=datetime.fromisoformat(r["state_changed_at"]) if r["state_changed_at"] else datetime.now(timezone.utc),
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

    def append_signal_history(self, signal_id: str, from_state: str | None, to_state: str,
                              at: datetime, reason: str, what_changed: list[str]) -> None:
        conn = get_connection(self.db_path)
        self._append_signal_history_connection(
            conn, signal_id, from_state, to_state, at, reason, what_changed
        )
        conn.commit()

    def load_signal_history(self, signal_id: str) -> list[dict]:
        conn = get_connection(self.db_path)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signal_history WHERE signal_id=? ORDER BY id ASC", (signal_id,))]

    @staticmethod
    def _assert_runtime_clock_not_before_state(
        conn: sqlite3.Connection,
        observed_at: datetime,
    ) -> None:
        observed = require_utc_clock_value(observed_at, "runtime observed_at")
        timestamps = conn.execute(
            "SELECT MAX(value) FROM ("
            "SELECT created_at AS value FROM runtime_transition_outbox "
            "UNION ALL SELECT delivered_at FROM runtime_outbox_delivery "
            "UNION ALL SELECT updated_at FROM runtime_outbox_cursor "
            "UNION ALL SELECT quarantined_at FROM runtime_outbox_quarantine "
            "UNION ALL SELECT blocked_at FROM runtime_worker_integrity_block"
            ") WHERE value IS NOT NULL"
        ).fetchone()[0]
        if timestamps is not None and observed < _runtime_utc_from_text(
            timestamps,
            "latest runtime evidence time",
        ):
            raise RuntimeOutboxError(
                "runtime evidence clock moved backward",
                code="RUNTIME_CLOCK_ROLLBACK",
            )

    @staticmethod
    def _advance_runtime_cursor_connection(
        conn: sqlite3.Connection, worker_id: str, updated_at: datetime
    ) -> int:
        worker = _require_runtime_id(worker_id, "worker_id")
        row = conn.execute(
            "SELECT last_contiguous_order FROM runtime_outbox_cursor WHERE worker_id=?",
            (worker,),
        ).fetchone()
        current = 0 if row is None else int(row[0])
        rows = conn.execute(
            "SELECT o.append_order,d.status FROM runtime_transition_outbox o "
            "JOIN runtime_outbox_delivery d ON d.artifact_id=o.artifact_id "
            "WHERE o.append_order>? ORDER BY o.append_order",
            (current,),
        ).fetchall()
        for item in rows:
            append_order = int(item["append_order"])
            if append_order != current + 1:
                raise RuntimeOutboxError("runtime outbox append order has a gap")
            if str(item["status"]) not in {"DELIVERED", "QUARANTINED"}:
                break
            current = append_order
        conn.execute(
            "INSERT INTO runtime_outbox_cursor(worker_id,last_contiguous_order,updated_at) "
            "VALUES(?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
            "last_contiguous_order=excluded.last_contiguous_order,"
            "updated_at=excluded.updated_at",
            (worker, current, _runtime_utc_text(updated_at, "cursor_updated_at")),
        )
        return current

    @staticmethod
    def _assert_runtime_worker_not_globally_blocked(
        conn: sqlite3.Connection,
    ) -> None:
        blocked = conn.execute(
            "SELECT artifact_id,block_code FROM runtime_worker_integrity_block "
            "ORDER BY block_order LIMIT 1"
        ).fetchone()
        if blocked is not None:
            raise RuntimeWorkerIntegrityBlocked(
                str(blocked["artifact_id"]),
                str(blocked["block_code"]),
            )

    def claim_runtime_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> RuntimeOutboxLease | None:
        worker = _require_runtime_id(worker_id, "worker_id")
        observed = require_utc_clock_value(now, "lease_time")
        if type(lease_seconds) is not int or lease_seconds < 1 or lease_seconds > 3600:
            raise RuntimeOutboxError("lease_seconds must be an integer in [1, 3600]")
        now_text = _runtime_utc_text(observed, "lease_time")
        expires = observed + timedelta(seconds=lease_seconds)
        expires_text = _runtime_utc_text(expires, "lease_expires_at")
        conn = get_connection(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        try:
            audit_runtime_evidence_schema(conn)
            self._assert_runtime_clock_not_before_state(conn, observed)
            self._assert_runtime_worker_not_globally_blocked(conn)
            row = conn.execute(
                "SELECT o.append_order,o.artifact_id,o.runtime_signal_id,"
                "o.transition_event_id,c.decision_content_id,c.occurrence_dedup_id,"
                "o.payload_json,o.payload_sha256,d.retry_count "
                "FROM runtime_transition_outbox o JOIN runtime_outbox_delivery d "
                "ON d.artifact_id=o.artifact_id JOIN runtime_transition_occurrence c "
                "ON c.artifact_id=o.artifact_id WHERE "
                "(d.status='PENDING' AND (d.next_retry_at IS NULL OR d.next_retry_at<=?)) "
                "OR (d.status='LEASED' AND d.lease_expires_at<=?) "
                "ORDER BY o.append_order LIMIT 1",
                (now_text, now_text),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            result = conn.execute(
                "UPDATE runtime_outbox_delivery SET status='LEASED',lease_owner=?,"
                "lease_expires_at=?,next_retry_at=NULL,last_error_code=NULL "
                "WHERE artifact_id=? AND ((status='PENDING' AND "
                "(next_retry_at IS NULL OR next_retry_at<=?)) OR "
                "(status='LEASED' AND lease_expires_at<=?))",
                (worker, expires_text, row["artifact_id"], now_text, now_text),
            )
            if result.rowcount != 1:
                raise RuntimeOutboxError("runtime outbox lease race")
            conn.commit()
            return RuntimeOutboxLease(
                append_order=int(row["append_order"]),
                artifact_id=str(row["artifact_id"]),
                runtime_signal_id=str(row["runtime_signal_id"]),
                transition_event_id=str(row["transition_event_id"]),
                decision_content_id=str(row["decision_content_id"]),
                occurrence_dedup_id=str(row["occurrence_dedup_id"]),
                payload_json=str(row["payload_json"]),
                payload_sha256=str(row["payload_sha256"]),
                retry_count=int(row["retry_count"]),
                lease_owner=worker,
                lease_expires_at=expires,
            )
        except Exception:
            conn.rollback()
            raise

    def mark_runtime_outbox_delivered(
        self,
        lease: RuntimeOutboxLease,
        *,
        append_result: RuntimeArtifactAppendResult,
        audit_report: RuntimeArtifactAuditReport,
        delivered_at: datetime,
    ) -> int:
        if type(lease) is not RuntimeOutboxLease:
            raise RuntimeOutboxError("lease must be RuntimeOutboxLease")
        if type(append_result) is not RuntimeArtifactAppendResult:
            raise RuntimeOutboxError("append_result must be RuntimeArtifactAppendResult")
        if type(audit_report) is not RuntimeArtifactAuditReport:
            raise RuntimeOutboxError("audit_report must be RuntimeArtifactAuditReport")
        record = append_result.record
        if record.artifact.artifact_id != lease.artifact_id:
            raise RuntimeOutboxError("artifact append result does not match lease")
        if audit_report.store_id != record.store_id:
            raise RuntimeOutboxError("artifact audit store identity mismatch")
        audited = dict(zip(audit_report.artifact_ids, audit_report.record_hashes, strict=True))
        if audited.get(lease.artifact_id) != record.record_hash:
            raise RuntimeOutboxError("artifact audit does not cover delivered record")
        delivered = require_utc_clock_value(delivered_at, "delivered_at")
        if audit_report.audited_at < record.stored_at or delivered < audit_report.audited_at:
            raise RuntimeOutboxError("artifact delivery audit time is invalid")
        conn = get_connection(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        try:
            audit_runtime_evidence_schema(conn)
            self._assert_runtime_worker_not_globally_blocked(conn)
            self._audit_runtime_outbox_state_connection(conn)
            self._assert_runtime_clock_not_before_state(conn, delivered)
            result = conn.execute(
                "UPDATE runtime_outbox_delivery SET status='DELIVERED',lease_owner=NULL,"
                "lease_expires_at=NULL,next_retry_at=NULL,last_error_code=NULL,"
                "delivered_record_hash=?,delivered_audit_id=?,delivered_at=? "
                "WHERE artifact_id=? AND status='LEASED' AND lease_owner=? "
                "AND lease_expires_at=? AND retry_count=?",
                (
                    _require_runtime_id(record.record_hash, "record_hash", sha256=True),
                    _require_runtime_id(audit_report.audit_id, "audit_id", sha256=True),
                    _runtime_utc_text(delivered, "delivered_at"),
                    lease.artifact_id,
                    lease.lease_owner,
                    _runtime_utc_text(lease.lease_expires_at, "lease_expires_at"),
                    lease.retry_count,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeOutboxError("runtime outbox delivery lease was lost")
            cursor = self._advance_runtime_cursor_connection(
                conn, lease.lease_owner, delivered
            )
            self._audit_runtime_outbox_state_connection(conn)
            conn.commit()
            return cursor
        except Exception:
            conn.rollback()
            raise

    def mark_runtime_outbox_retry(
        self,
        lease: RuntimeOutboxLease,
        *,
        error_code: str,
        retry_at: datetime,
        observed_at: datetime,
    ) -> None:
        if type(lease) is not RuntimeOutboxLease:
            raise RuntimeOutboxError("lease must be RuntimeOutboxLease")
        error = _require_runtime_id(error_code, "error_code")
        retry = require_utc_clock_value(retry_at, "retry_at")
        observed = require_utc_clock_value(observed_at, "observed_at")
        if retry <= observed:
            raise RuntimeOutboxError("retry_at must be after observed_at")
        conn = get_connection(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        try:
            audit_runtime_evidence_schema(conn)
            self._assert_runtime_worker_not_globally_blocked(conn)
            self._audit_runtime_outbox_state_connection(conn)
            self._assert_runtime_clock_not_before_state(conn, observed)
            result = conn.execute(
                "UPDATE runtime_outbox_delivery SET status='PENDING',lease_owner=NULL,"
                "lease_expires_at=NULL,retry_count=retry_count+1,next_retry_at=?,"
                "last_error_code=? "
                "WHERE artifact_id=? AND status='LEASED' AND lease_owner=? "
                "AND lease_expires_at=? AND retry_count=?",
                (
                    _runtime_utc_text(retry, "retry_at"),
                    error,
                    lease.artifact_id,
                    lease.lease_owner,
                    _runtime_utc_text(lease.lease_expires_at, "lease_expires_at"),
                    lease.retry_count,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeOutboxError("runtime outbox retry lease was lost")
            self._audit_runtime_outbox_state_connection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mark_runtime_worker_integrity_blocked(
        self,
        lease: RuntimeOutboxLease,
        *,
        error_code: str,
        blocked_at: datetime,
    ) -> str:
        if type(lease) is not RuntimeOutboxLease:
            raise RuntimeOutboxError("lease must be RuntimeOutboxLease")
        error = _require_runtime_id(error_code, "error_code")
        observed = require_utc_clock_value(blocked_at, "blocked_at")
        conn = get_connection(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        try:
            runtime_store_id = audit_runtime_evidence_schema(conn)
            self._assert_runtime_clock_not_before_state(conn, observed)
            existing = conn.execute(
                "SELECT block_id,artifact_id,block_code FROM runtime_worker_integrity_block "
                "WHERE worker_id=?",
                (lease.lease_owner,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["artifact_id"]) != lease.artifact_id
                    or str(existing["block_code"]) != error
                ):
                    raise RuntimeOutboxError("runtime worker block identity conflict")
                conn.rollback()
                return str(existing["block_id"])
            leased = conn.execute(
                "SELECT 1 FROM runtime_outbox_delivery WHERE artifact_id=? "
                "AND status='LEASED' AND lease_owner=? AND lease_expires_at=? "
                "AND retry_count=?",
                (
                    lease.artifact_id,
                    lease.lease_owner,
                    _runtime_utc_text(lease.lease_expires_at, "lease_expires_at"),
                    lease.retry_count,
                ),
            )
            if leased is None:
                raise RuntimeOutboxError("runtime outbox blocked lease was lost")
            block_order = int(
                conn.execute(
                    "SELECT COALESCE(MAX(block_order),0)+1 FROM runtime_worker_integrity_block"
                ).fetchone()[0]
            )
            identity = _runtime_canonical_json(
                {
                    "schema": "stage4g1-runtime-worker-integrity-block-v1",
                    "runtime_store_id": runtime_store_id,
                    "block_order": block_order,
                    "worker_id": lease.lease_owner,
                    "artifact_id": lease.artifact_id,
                    "block_code": error,
                    "blocked_at": _runtime_utc_text(observed, "blocked_at"),
                }
            )
            block_id = _runtime_sha256_text(identity)
            conn.execute(
                "INSERT INTO runtime_worker_integrity_block("
                "block_order,block_id,worker_id,artifact_id,block_code,blocked_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    block_order,
                    block_id,
                    lease.lease_owner,
                    lease.artifact_id,
                    error,
                    _runtime_utc_text(observed, "blocked_at"),
                ),
            )
            self._audit_runtime_outbox_state_connection(conn)
            conn.commit()
            return block_id
        except Exception:
            conn.rollback()
            raise

    def mark_runtime_outbox_quarantined(
        self,
        lease: RuntimeOutboxLease,
        *,
        reason_code: str,
        quarantined_at: datetime,
        metadata: dict[str, Any],
    ) -> tuple[str, int]:
        if type(lease) is not RuntimeOutboxLease:
            raise RuntimeOutboxError("lease must be RuntimeOutboxLease")
        observed = require_utc_clock_value(quarantined_at, "quarantined_at")
        conn = get_connection(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        try:
            audit_runtime_evidence_schema(conn)
            self._assert_runtime_worker_not_globally_blocked(conn)
            self._assert_runtime_clock_not_before_state(conn, observed)
            quarantine_id = self._append_runtime_quarantine_connection(
                conn,
                runtime_signal_id=lease.runtime_signal_id,
                reason_code=reason_code,
                quarantined_at=observed,
                metadata=metadata,
                artifact_id=lease.artifact_id,
                outbox_append_order=lease.append_order,
            )
            result = conn.execute(
                "UPDATE runtime_outbox_delivery SET status='QUARANTINED',"
                "lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,"
                "last_error_code=?,delivered_record_hash=NULL,"
                "delivered_audit_id=NULL,delivered_at=NULL "
                "WHERE artifact_id=? AND status='LEASED' AND lease_owner=? "
                "AND lease_expires_at=? AND retry_count=?",
                (
                    reason_code,
                    lease.artifact_id,
                    lease.lease_owner,
                    _runtime_utc_text(lease.lease_expires_at, "lease_expires_at"),
                    lease.retry_count,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeOutboxError("runtime outbox quarantine lease was lost")
            cursor = self._advance_runtime_cursor_connection(
                conn, lease.lease_owner, observed
            )
            self._audit_runtime_outbox_state_connection(conn)
            conn.commit()
            return quarantine_id, cursor
        except Exception:
            conn.rollback()
            raise

    def runtime_outbox_cursor(self, worker_id: str) -> int:
        worker = _require_runtime_id(worker_id, "worker_id")
        conn = get_connection(self.db_path)
        audit_runtime_evidence_schema(conn)
        self._audit_runtime_outbox_state_connection(conn)
        row = conn.execute(
            "SELECT last_contiguous_order FROM runtime_outbox_cursor WHERE worker_id=?",
            (worker,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    # ---- Provider state ----
    def save_provider_state(self, provider: str, circuit_state: str, last_success_at: str | None,
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
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def load_events(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        conn = get_connection(self.db_path)
        if symbol:
            rows = conn.execute("SELECT * FROM events WHERE symbol=? ORDER BY id DESC LIMIT ?",
                                (symbol, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
