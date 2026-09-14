"""Bounded diagnostic input copies, not a Store/PIT verification authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core import types as T
from ..core.market_time import MARKET_SESSION_LABEL_POLICY_V1, market_session_date
from . import indicators as I
from .indicator_diagnostics import MAX_DIAGNOSTIC_BARS, daily_window_diagnostics

INPUT_SCHEMA = "evidence-comparison-input-v1"
MAX_INPUT_BYTES = 512 * 1024
MAX_EVIDENCE_BARS = 80
MAX_MAGNITUDE = 1e100
QUOTE_NUMBERS = ("open", "high", "low", "last", "prev_close", "amount", "turnover")
QUOTE_TIMES = ("timestamp", "received_at", "computed_at")
BAR_NUMBERS = ("open", "high", "low", "close", "amount", "turnover", "adjustment_factor")
SECTOR_NUMBERS = ("score", "relative_strength", "persistence", "crowding")


class EvidenceInputError(ValueError):
    """Stable non-sensitive error code; never echo private input objects."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _number(value: Any, *, optional: bool = False, positive: bool = False) -> Any:
    if value is None:
        if optional:
            return None
        raise EvidenceInputError("INVALID_NUMERIC_INPUT")
    if not I.valid_number(value) or abs(value) > MAX_MAGNITUDE:
        raise EvidenceInputError("INVALID_NUMERIC_INPUT")
    if (positive and value <= 0) or (not positive and value < 0):
        raise EvidenceInputError("INVALID_NUMERIC_RANGE")
    return value


def _text(value: Any, *, empty: bool = True) -> str:
    if type(value) is not str or len(value) > 512 or (not empty and not value.strip()):
        raise EvidenceInputError("INVALID_TEXT")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EvidenceInputError("INVALID_TEXT")
    return value


def _time(value: Any, computed_at: datetime) -> str:
    if type(value) is not datetime:
        raise EvidenceInputError("INVALID_TIMESTAMP")
    if value.tzinfo is not None and value.utcoffset() is not None and value > computed_at:
        raise EvidenceInputError("FUTURE_OBSERVED_TIMESTAMP")
    return value.isoformat()


def _enum(value: Any, enum_type: Any) -> str:
    if type(value) is not enum_type:
        raise EvidenceInputError("INVALID_ENUM_TYPE")
    return value.value


def _identity(symbol: Any, market: Any) -> None:
    if type(market) is not T.Market or type(symbol) is not str or len(symbol) > 40:
        raise EvidenceInputError("INVALID_IDENTITY")
    code, dot, suffix = symbol.rpartition(".")
    if not dot or not code or symbol != symbol.upper() or any(not (c.isascii() and (c.isalnum() or c in ".-_")) for c in code):
        raise EvidenceInputError("INVALID_IDENTITY")
    if suffix not in {T.Market.A: {"SH", "SZ"}, T.Market.HK: {"HK"}, T.Market.US: {"US"}}[market]:
        raise EvidenceInputError("IDENTITY_MARKET_MISMATCH")


def _document(ctx: T.ScanContext, computed_at: datetime) -> dict[str, Any]:
    if type(ctx) is not T.ScanContext:
        raise EvidenceInputError("INVALID_CONTEXT_TYPE")
    if type(computed_at) is not datetime or computed_at.tzinfo is None or computed_at.utcoffset() is None:
        raise EvidenceInputError("AWARE_CAPTURE_CLOCK_REQUIRED")
    _identity(ctx.symbol, ctx.market)
    q = ctx.quote
    if type(q) is not T.Quote or q.symbol != ctx.symbol or q.market is not ctx.market:
        raise EvidenceInputError("QUOTE_IDENTITY_MISMATCH")
    quote: dict[str, Any] = {"symbol": q.symbol, "market": q.market.value,
                             "source": _text(q.source), "data_status": _enum(q.data_status, T.DataStatus)}
    for key in QUOTE_NUMBERS:
        quote[key] = _number(getattr(q, key), optional=True, positive=key in ("open", "high", "low", "last", "prev_close"))
    for key in QUOTE_TIMES:
        quote[key] = _time(getattr(q, key), computed_at)
    if (q.high is not None and q.low is not None
            and (q.high < q.low or any(v is not None and not q.low <= v <= q.high for v in (q.open, q.last)))):
        raise EvidenceInputError("QUOTE_RANGE_INCONSISTENT")
    if type(ctx.recent_bars) not in (list, tuple) or len(ctx.recent_bars) > MAX_DIAGNOSTIC_BARS:
        raise EvidenceInputError("BAR_CONTAINER_OR_LIMIT")
    bars: list[dict[str, Any]] = []
    for b in tuple(ctx.recent_bars):
        if type(b) is not T.Bar or b.symbol != ctx.symbol or b.market is not ctx.market or b.interval != "1d":
            raise EvidenceInputError("BAR_IDENTITY_OR_INTERVAL_MISMATCH")
        if type(b.volume) is not int or not 0 <= b.volume <= 2**53 - 1:
            raise EvidenceInputError("INVALID_VOLUME")
        row = {"symbol": b.symbol, "market": b.market.value, "interval": b.interval,
               "timestamp": _time(b.timestamp, computed_at), "source": _text(b.source, empty=False),
               "volume": b.volume, "quality_status": _enum(b.quality_status, T.DataStatus)}
        for key in BAR_NUMBERS:
            row[key] = _number(getattr(b, key), positive=key in ("open", "high", "low", "close", "adjustment_factor"))
        bars.append(row)
    dq = None
    if ctx.dq is not None:
        if type(ctx.dq) is not T.DataQuality or type(ctx.dq.score) is not int or not 0 <= ctx.dq.score <= 100:
            raise EvidenceInputError("INVALID_DQ")
        if type(ctx.dq.reasons) is not list or len(ctx.dq.reasons) > 32:
            raise EvidenceInputError("INVALID_DQ_REASONS")
        dq = {"status": _enum(ctx.dq.status, T.QualityStatus), "score": ctx.dq.score,
              "reasons": [_text(reason) for reason in tuple(ctx.dq.reasons)]}
    regime: dict[str, Any] | None = None
    if ctx.regime is not None:
        if type(ctx.regime) is not T.MarketRegime:
            raise EvidenceInputError("INVALID_REGIME")
        regime = {"regime": _enum(ctx.regime.regime, T.RegimeState), "market_score": _number(ctx.regime.market_score)}
        if regime["market_score"] > 100:
            raise EvidenceInputError("INVALID_REGIME_RANGE")
    sector: dict[str, Any] | None = None
    if ctx.sector is not None:
        if type(ctx.sector) is not T.SectorSnapshot:
            raise EvidenceInputError("INVALID_SECTOR")
        sector = {"sector": _text(ctx.sector.sector, empty=False), "stage": _enum(ctx.sector.stage, T.SectorStage),
                  "catalyst": _text(ctx.sector.catalyst)}
        for key in SECTOR_NUMBERS:
            sector[key] = _number(getattr(ctx.sector, key))
            if sector[key] > 100:
                raise EvidenceInputError("INVALID_SECTOR_RANGE")
    return {"schema": INPUT_SCHEMA, "symbol": ctx.symbol, "market": ctx.market.value,
            "computed_at": computed_at.astimezone(timezone.utc).isoformat(),
            "quote": quote, "bars": bars, "dq": dq, "regime": regime, "sector": sector,
            "configuration_scope": "FIXED_LEGACY_RECIPE_NO_CFG_FIELDS_READ"}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceInputError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise EvidenceInputError("NONFINITE_JSON")


def _decode(payload: str) -> tuple[T.ScanContext, datetime, dict[str, Any]]:
    if type(payload) is not str or len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
        raise EvidenceInputError("SNAPSHOT_SIZE_OR_TYPE")
    try:
        d = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
        if type(d) is not dict or d.get("schema") != INPUT_SCHEMA:
            raise EvidenceInputError("INPUT_SCHEMA_MISMATCH")
        q = dict(d["quote"])
        q["market"] = T.Market(q["market"])
        q["data_status"] = T.DataStatus(q["data_status"])
        for key in QUOTE_TIMES:
            q[key] = datetime.fromisoformat(q[key])
        bars = []
        for raw in d["bars"]:
            b = dict(raw)
            b["market"] = T.Market(b["market"])
            b["quality_status"] = T.DataStatus(b["quality_status"])
            b["timestamp"] = datetime.fromisoformat(b["timestamp"])
            bars.append(T.Bar(**b))
        dq = None if d["dq"] is None else T.DataQuality(T.QualityStatus(d["dq"]["status"]), d["dq"]["score"], d["dq"]["reasons"])
        reg = None if d["regime"] is None else T.MarketRegime(T.RegimeState(d["regime"]["regime"]), d["regime"]["market_score"])
        sector = None
        if d["sector"] is not None:
            sec = dict(d["sector"])
            sec["stage"] = T.SectorStage(sec["stage"])
            sector = T.SectorSnapshot(**sec)
        ctx = T.ScanContext(symbol=d["symbol"], market=T.Market(d["market"]), quote=T.Quote(**q),
                            recent_bars=bars, dq=dq, regime=reg, sector=sector)
        at = datetime.fromisoformat(d["computed_at"])
        if canonical(_document(ctx, at)) != payload:
            raise EvidenceInputError("NONCANONICAL_OR_UNSUPPORTED_INPUT_FIELDS")
        return ctx, at, d
    except EvidenceInputError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise EvidenceInputError("INVALID_SNAPSHOT_DOCUMENT") from exc


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """Immutable text copy; the digest guarantees reproducibility, not provenance."""

    payload: str

    def __post_init__(self) -> None:
        _decode(self.payload)

    @classmethod
    def capture(cls, ctx: T.ScanContext, computed_at: datetime) -> EvidenceSnapshot:
        return cls(canonical(_document(ctx, computed_at)))

    @property
    def input_id(self) -> str:
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest()

    def select(self) -> tuple[T.ScanContext, dict[str, Any]]:
        """Decode anew for each consumer; never leak original mutable references."""
        ctx, at, _ = _decode(self.payload)
        diagnostic = daily_window_diagnostics(ctx.recent_bars, ctx.symbol, ctx.market, at)
        if diagnostic["status"] == "INVALID_INPUT":
            raise EvidenceInputError("INVALID_DAILY_SERIES:" + ",".join(diagnostic["issues"]))
        cutoff = market_session_date(at, ctx.market, MARKET_SESSION_LABEL_POLICY_V1)
        selected = []
        for bar in ctx.recent_bars:
            naive = bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None
            day = bar.timestamp.date() if naive else market_session_date(bar.timestamp, ctx.market, MARKET_SESSION_LABEL_POLICY_V1)
            if day < cutoff:
                selected.append(bar)
        ctx.recent_bars = selected[-MAX_EVIDENCE_BARS:]
        summary = {key: diagnostic[key] for key in ("input_count", "same_day_excluded", "time_basis", "warnings", "sources")}
        assert ctx.quote is not None
        summary.update(selected_count=len(ctx.recent_bars), older_excluded=max(0, len(selected) - MAX_EVIDENCE_BARS),
                       computed_at=at.isoformat(), input_id=self.input_id,
                       quote_source=ctx.quote.source, quote_source_timestamp=ctx.quote.timestamp.isoformat(),
                       quote_received_at=ctx.quote.received_at.isoformat(),
                       quote_declared_status=ctx.quote.data_status.value,
                       snapshot_kind="BOUNDED_RUNTIME_COPY_NOT_ATOMIC_STORE_OR_PIT")
        return ctx, summary
