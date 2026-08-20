"""Deterministic adjusted-market-data materialization.

Stage 2F materializes a separate immutable dataset from exact raw bars, a
Calendar contract, a stable instrument identity, and a formal Stage 2B
``AdjustmentSeries``. Raw ``Bar`` objects are never modified. The module does
not train models, run a backtest, promote trust, or claim correctness on real
securities.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path

from stock_tracker.core.types import Bar, Market

from ..core.corporate_actions import AdjustmentSeries
from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, exchange_local_date, to_utc
from ..core.universe import InstrumentIdentityFact
from .manifest import safe_artifact_path, validate_storage_key

_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_DESCRIPTOR_SCHEMA = "stage2f-adjusted-market-data-descriptor-v1"
_DATASET_SCHEMA = "stage2f-adjusted-market-data-dataset-v1"
_ROW_SCHEMA = "stage2f-adjusted-bar-row-v1"


class AdjustedMarketDataError(ValueError):
    """Raised when adjusted-data materialization is incomplete or ambiguous."""


class RawFieldPolicy(StrEnum):
    PRESERVE_RAW = "PRESERVE_RAW"


class SessionGapPolicy(StrEnum):
    REQUIRE_ALL_OPEN_SESSIONS = "REQUIRE_ALL_OPEN_SESSIONS"
    ALLOW_EXPLICIT_GAPS = "ALLOW_EXPLICIT_GAPS"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AdjustedMarketDataError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise AdjustedMarketDataError(f"{name} must be a boolean")
    return value


def _require_int(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdjustedMarketDataError(f"{name} must be an integer")
    if positive and value <= 0:
        raise AdjustedMarketDataError(f"{name} must be positive")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise AdjustedMarketDataError(f"{name} must be lowercase SHA-256")
    return text


def _decimal_from_number(
    value: object,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise AdjustedMarketDataError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AdjustedMarketDataError(f"{name} is not decimal-convertible") from exc
    if not result.is_finite():
        raise AdjustedMarketDataError(f"{name} must be finite")
    if positive and result <= 0:
        raise AdjustedMarketDataError(f"{name} must be positive")
    if non_negative and result < 0:
        raise AdjustedMarketDataError(f"{name} cannot be negative")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def _parse_decimal_text(value: object, name: str) -> Decimal:
    text = _require_text(value, name)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise AdjustedMarketDataError(f"{name} is not decimal text") from exc
    if not result.is_finite() or _decimal_text(result) != text:
        raise AdjustedMarketDataError(
            f"{name} must be canonical finite decimal text"
        )
    return result


def _parse_date_text(value: object, name: str) -> date:
    text = _require_text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise AdjustedMarketDataError(f"{name} must be YYYY-MM-DD") from exc


def _parse_datetime_text(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdjustedMarketDataError(
            f"{name} must be ISO-8601 datetime"
        ) from exc
    ensure_aware(result, name)
    return to_utc(result)


def _strict_json_text(text: str, name: str) -> object:
    def reject_constant(value: str) -> object:
        raise AdjustedMarketDataError(
            f"non-finite JSON constant {value!r} is forbidden"
        )

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise AdjustedMarketDataError(
                    f"duplicate JSON field is forbidden in {name}: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise AdjustedMarketDataError(f"{name} is not valid JSON") from exc


def _strict_mapping(
    value: object,
    name: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise AdjustedMarketDataError(f"{name} must be a JSON object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        raise AdjustedMarketDataError(
            f"{name} field mismatch; unknown={unknown}, missing={missing}"
        )
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise AdjustedMarketDataError(
                f"immutable adjusted-data path contains different bytes: {path.name}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True, slots=True)
class RawBarSnapshot:
    raw_artifact_id: str
    instrument_id: str
    identity_fact_id: str
    symbol: str
    market: Market
    start_date: date
    end_date: date
    as_of: datetime
    bars: tuple[Bar, ...]
    source_note: str
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        _require_text(self.symbol, "symbol")
        if not isinstance(self.market, Market):
            raise AdjustedMarketDataError("market must be Market")
        if self.end_date < self.start_date:
            raise AdjustedMarketDataError("end_date cannot precede start_date")
        ensure_aware(self.as_of, "as_of")
        _require_text(self.source_note, "source_note")
        if not self.bars:
            raise AdjustedMarketDataError("raw bar snapshot must contain bars")
        if any(not isinstance(bar, Bar) for bar in self.bars):
            raise AdjustedMarketDataError("raw snapshot rows must be Bar")
        timestamps = tuple(to_utc(bar.timestamp) for bar in self.bars)
        if timestamps != tuple(sorted(timestamps)) or len(set(timestamps)) != len(
            timestamps
        ):
            raise AdjustedMarketDataError(
                "raw bars must be strictly chronological and unique"
            )
        row_ids: list[str] = []
        for bar in self.bars:
            if bar.symbol != self.symbol or bar.market is not self.market:
                raise AdjustedMarketDataError("raw bar identity mismatch")
            session_date = exchange_local_date(bar.timestamp, self.market)
            if not self.start_date <= session_date <= self.end_date:
                raise AdjustedMarketDataError("raw bar is outside snapshot range")
            prices = {
                name: _decimal_from_number(
                    getattr(bar, name),
                    name,
                    positive=True,
                )
                for name in ("open", "high", "low", "close")
            }
            if prices["low"] > min(
                prices["open"],
                prices["high"],
                prices["close"],
            ):
                raise AdjustedMarketDataError("raw low is inconsistent with OHLC")
            if prices["high"] < max(
                prices["open"],
                prices["low"],
                prices["close"],
            ):
                raise AdjustedMarketDataError("raw high is inconsistent with OHLC")
            row_ids.append(
                fingerprint(
                    {
                        "schema": "stage2f-raw-bar-row-v1",
                        "symbol": bar.symbol,
                        "market": bar.market,
                        "timestamp": to_utc(bar.timestamp),
                        "session_date": session_date,
                        "interval": bar.interval,
                        "open": prices["open"],
                        "high": prices["high"],
                        "low": prices["low"],
                        "close": prices["close"],
                        "volume": bar.volume,
                        "amount": _decimal_from_number(
                            bar.amount,
                            "amount",
                            non_negative=True,
                        ),
                        "turnover": _decimal_from_number(
                            bar.turnover,
                            "turnover",
                            non_negative=True,
                        ),
                        "source": bar.source,
                        "adjustment_factor": _decimal_from_number(
                            bar.adjustment_factor,
                            "adjustment_factor",
                            positive=True,
                        ),
                    }
                )
            )
        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "stage2f-raw-bar-snapshot-v1",
                    "raw_artifact_id": self.raw_artifact_id,
                    "instrument_id": self.instrument_id,
                    "identity_fact_id": self.identity_fact_id,
                    "symbol": self.symbol,
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": to_utc(self.as_of),
                    "row_ids": row_ids,
                    "source_note": self.source_note,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CalendarMaterializationSnapshot:
    market: Market
    start_date: date
    end_date: date
    as_of: datetime
    open_sessions: tuple[date, ...]
    verified: bool
    complete: bool
    source_note: str
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise AdjustedMarketDataError("market must be Market")
        if self.end_date < self.start_date:
            raise AdjustedMarketDataError("calendar end_date cannot precede start_date")
        ensure_aware(self.as_of, "as_of")
        _require_bool(self.verified, "verified")
        _require_bool(self.complete, "complete")
        if (self.verified or self.complete) and not self.source_note:
            raise AdjustedMarketDataError(
                "verified/complete Calendar snapshot requires source_note"
            )
        if self.open_sessions != tuple(sorted(set(self.open_sessions))):
            raise AdjustedMarketDataError(
                "open_sessions must be sorted and unique"
            )
        if any(
            not self.start_date <= session <= self.end_date
            for session in self.open_sessions
        ):
            raise AdjustedMarketDataError(
                "open session is outside Calendar snapshot range"
            )
        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "stage2f-calendar-materialization-snapshot-v1",
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": to_utc(self.as_of),
                    "open_sessions": self.open_sessions,
                    "verified": self.verified,
                    "complete": self.complete,
                    "source_note": self.source_note,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AdjustedMarketDataPolicy:
    policy_version: str
    session_gap_policy: SessionGapPolicy
    raw_field_policy: RawFieldPolicy = RawFieldPolicy.PRESERVE_RAW
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if not isinstance(self.session_gap_policy, SessionGapPolicy):
            raise AdjustedMarketDataError(
                "session_gap_policy must be SessionGapPolicy"
            )
        if not isinstance(self.raw_field_policy, RawFieldPolicy):
            raise AdjustedMarketDataError("raw_field_policy must be RawFieldPolicy")
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "stage2f-adjusted-market-data-policy-v1",
                    "policy_version": self.policy_version,
                    "session_gap_policy": self.session_gap_policy,
                    "raw_field_policy": self.raw_field_policy,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AdjustedBarRow:
    instrument_id: str
    identity_fact_id: str
    symbol: str
    market: Market
    timestamp: datetime
    session_date: date
    interval: str
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    price_multiplier: Decimal
    automatic_share_multiplier: Decimal
    raw_volume: int
    raw_amount: Decimal
    raw_turnover: Decimal
    raw_source: str
    raw_fields_status: str
    row_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        _require_text(self.symbol, "symbol")
        if not isinstance(self.market, Market):
            raise AdjustedMarketDataError("market must be Market")
        ensure_aware(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", to_utc(self.timestamp))
        if exchange_local_date(self.timestamp, self.market) != self.session_date:
            raise AdjustedMarketDataError(
                "session_date disagrees with timestamp and market timezone"
            )
        _require_text(self.interval, "interval")
        for name in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
            "price_multiplier",
            "automatic_share_multiplier",
            "raw_amount",
            "raw_turnover",
        ):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite():
                raise AdjustedMarketDataError(f"{name} must be finite Decimal")
        if min(self.raw_open, self.raw_high, self.raw_low, self.raw_close) <= 0:
            raise AdjustedMarketDataError("raw OHLC values must be positive")
        if min(
            self.adjusted_open,
            self.adjusted_high,
            self.adjusted_low,
            self.adjusted_close,
        ) <= 0:
            raise AdjustedMarketDataError("adjusted OHLC values must be positive")
        if self.raw_low > min(self.raw_open, self.raw_high, self.raw_close):
            raise AdjustedMarketDataError("raw low is inconsistent with OHLC")
        if self.raw_high < max(self.raw_open, self.raw_low, self.raw_close):
            raise AdjustedMarketDataError("raw high is inconsistent with OHLC")
        if self.adjusted_low > min(
            self.adjusted_open,
            self.adjusted_high,
            self.adjusted_close,
        ):
            raise AdjustedMarketDataError("adjusted low is inconsistent with OHLC")
        if self.adjusted_high < max(
            self.adjusted_open,
            self.adjusted_low,
            self.adjusted_close,
        ):
            raise AdjustedMarketDataError("adjusted high is inconsistent with OHLC")
        if self.price_multiplier <= 0 or self.automatic_share_multiplier <= 0:
            raise AdjustedMarketDataError("adjustment multipliers must be positive")
        _require_int(self.raw_volume, "raw_volume")
        if self.raw_volume < 0:
            raise AdjustedMarketDataError("raw_volume cannot be negative")
        if self.raw_amount < 0 or self.raw_turnover < 0:
            raise AdjustedMarketDataError("raw amount/turnover cannot be negative")
        _require_text(self.raw_source, "raw_source")
        _require_text(self.raw_fields_status, "raw_fields_status")
        object.__setattr__(
            self,
            "row_id",
            fingerprint(
                {
                    "schema": _ROW_SCHEMA,
                    "instrument_id": self.instrument_id,
                    "identity_fact_id": self.identity_fact_id,
                    "symbol": self.symbol,
                    "market": self.market,
                    "timestamp": self.timestamp,
                    "session_date": self.session_date,
                    "interval": self.interval,
                    "raw_open": self.raw_open,
                    "raw_high": self.raw_high,
                    "raw_low": self.raw_low,
                    "raw_close": self.raw_close,
                    "adjusted_open": self.adjusted_open,
                    "adjusted_high": self.adjusted_high,
                    "adjusted_low": self.adjusted_low,
                    "adjusted_close": self.adjusted_close,
                    "price_multiplier": self.price_multiplier,
                    "automatic_share_multiplier": (
                        self.automatic_share_multiplier
                    ),
                    "raw_volume": self.raw_volume,
                    "raw_amount": self.raw_amount,
                    "raw_turnover": self.raw_turnover,
                    "raw_source": self.raw_source,
                    "raw_fields_status": self.raw_fields_status,
                }
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "identity_fact_id": self.identity_fact_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "session_date": self.session_date.isoformat(),
            "interval": self.interval,
            "raw_open": _decimal_text(self.raw_open),
            "raw_high": _decimal_text(self.raw_high),
            "raw_low": _decimal_text(self.raw_low),
            "raw_close": _decimal_text(self.raw_close),
            "adjusted_open": _decimal_text(self.adjusted_open),
            "adjusted_high": _decimal_text(self.adjusted_high),
            "adjusted_low": _decimal_text(self.adjusted_low),
            "adjusted_close": _decimal_text(self.adjusted_close),
            "price_multiplier": _decimal_text(self.price_multiplier),
            "automatic_share_multiplier": _decimal_text(
                self.automatic_share_multiplier
            ),
            "raw_volume": self.raw_volume,
            "raw_amount": _decimal_text(self.raw_amount),
            "raw_turnover": _decimal_text(self.raw_turnover),
            "raw_source": self.raw_source,
            "raw_fields_status": self.raw_fields_status,
            "row_id": self.row_id,
        }


@dataclass(frozen=True, slots=True)
class AdjustedMarketDataDataset:
    raw_snapshot: RawBarSnapshot = field(repr=False)
    calendar_snapshot: CalendarMaterializationSnapshot = field(repr=False)
    identity: InstrumentIdentityFact = field(repr=False)
    series: AdjustmentSeries = field(repr=False)
    policy: AdjustedMarketDataPolicy = field(repr=False)
    explicit_gap_sessions: tuple[date, ...] = ()
    raw_bar_snapshot_id: str = field(init=False)
    calendar_snapshot_id: str = field(init=False)
    corporate_action_snapshot_id: str = field(init=False)
    adjustment_series_id: str = field(init=False)
    identity_fact_id: str = field(init=False)
    instrument_id: str = field(init=False)
    market: Market = field(init=False)
    start_date: date = field(init=False)
    end_date: date = field(init=False)
    as_of: datetime = field(init=False)
    policy_id: str = field(init=False)
    rows: tuple[AdjustedBarRow, ...] = field(init=False)
    gaps: tuple[str, ...] = field(init=False)
    dataset_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.raw_snapshot, RawBarSnapshot):
            raise AdjustedMarketDataError("raw_snapshot must be RawBarSnapshot")
        if not isinstance(
            self.calendar_snapshot,
            CalendarMaterializationSnapshot,
        ):
            raise AdjustedMarketDataError(
                "calendar_snapshot must be CalendarMaterializationSnapshot"
            )
        if not isinstance(self.identity, InstrumentIdentityFact):
            raise AdjustedMarketDataError(
                "identity must be InstrumentIdentityFact"
            )
        if not isinstance(self.series, AdjustmentSeries):
            raise AdjustedMarketDataError("series must be AdjustmentSeries")
        if not isinstance(self.policy, AdjustedMarketDataPolicy):
            raise AdjustedMarketDataError(
                "policy must be AdjustedMarketDataPolicy"
            )
        if not self.calendar_snapshot.verified or not self.calendar_snapshot.complete:
            raise AdjustedMarketDataError(
                "formal materialization requires verified and complete Calendar snapshot"
            )
        if (
            not self.series.snapshot.require_verified
            or not self.series.snapshot.require_complete
        ):
            raise AdjustedMarketDataError(
                "formal materialization requires verified and complete corporate-action snapshot"
            )
        if not self.identity.verified:
            raise AdjustedMarketDataError(
                "formal materialization requires verified identity"
            )
        if self.raw_snapshot.instrument_id != self.identity.instrument_id:
            raise AdjustedMarketDataError("raw snapshot instrument_id mismatch")
        if self.raw_snapshot.identity_fact_id != self.identity.fact_id:
            raise AdjustedMarketDataError("raw snapshot identity_fact_id mismatch")
        if (
            self.raw_snapshot.symbol != self.identity.symbol
            or self.raw_snapshot.market is not self.identity.market
        ):
            raise AdjustedMarketDataError("raw snapshot identity evidence mismatch")
        if (
            self.series.instrument_id != self.identity.instrument_id
            or self.series.market is not self.identity.market
        ):
            raise AdjustedMarketDataError("adjustment series identity mismatch")
        if self.calendar_snapshot.market is not self.identity.market:
            raise AdjustedMarketDataError("Calendar market mismatch")
        if (
            self.raw_snapshot.start_date != self.series.start_date
            or self.raw_snapshot.end_date != self.series.end_date
            or self.calendar_snapshot.start_date != self.series.start_date
            or self.calendar_snapshot.end_date != self.series.end_date
        ):
            raise AdjustedMarketDataError("snapshot date ranges must match exactly")
        series_as_of = to_utc(self.series.as_of)
        if to_utc(self.raw_snapshot.as_of) > series_as_of:
            raise AdjustedMarketDataError(
                "raw snapshot is future relative to series as_of"
            )
        if to_utc(self.calendar_snapshot.as_of) > series_as_of:
            raise AdjustedMarketDataError(
                "Calendar snapshot is future relative to series as_of"
            )
        if (
            to_utc(self.identity.known_at) > series_as_of
            or to_utc(self.identity.usable_from) > series_as_of
        ):
            raise AdjustedMarketDataError(
                "identity is future relative to series as_of"
            )

        explicit_gaps = tuple(sorted(set(self.explicit_gap_sessions)))
        if self.explicit_gap_sessions != explicit_gaps:
            raise AdjustedMarketDataError(
                "explicit_gap_sessions must be sorted and unique"
            )
        bar_dates = tuple(
            exchange_local_date(bar.timestamp, self.raw_snapshot.market)
            for bar in self.raw_snapshot.bars
        )
        if len(set(bar_dates)) != len(bar_dates):
            raise AdjustedMarketDataError(
                "multiple bars per session are unsupported"
            )
        open_sessions = set(self.calendar_snapshot.open_sessions)
        for bar, session_date in zip(
            self.raw_snapshot.bars,
            bar_dates,
            strict=True,
        ):
            if to_utc(bar.timestamp) > series_as_of:
                raise AdjustedMarketDataError(
                    "raw bar timestamp is future relative to series as_of"
                )
            if session_date not in open_sessions:
                raise AdjustedMarketDataError(
                    "bar exists on a Calendar-closed session"
                )
            if not self.identity.active_on(session_date):
                raise AdjustedMarketDataError(
                    "identity is not active on one raw bar session"
                )
        missing_sessions = tuple(sorted(open_sessions - set(bar_dates)))
        if (
            self.policy.session_gap_policy
            is SessionGapPolicy.REQUIRE_ALL_OPEN_SESSIONS
        ):
            if missing_sessions:
                raise AdjustedMarketDataError(
                    "raw snapshot is missing open sessions"
                )
            if explicit_gaps:
                raise AdjustedMarketDataError(
                    "explicit gaps are forbidden by REQUIRE_ALL_OPEN_SESSIONS"
                )
        elif set(missing_sessions) != set(explicit_gaps):
            raise AdjustedMarketDataError(
                "explicit gaps must exactly identify missing open sessions"
            )

        rows: list[AdjustedBarRow] = []
        for bar, session_date in zip(
            self.raw_snapshot.bars,
            bar_dates,
            strict=True,
        ):
            price_multiplier = self.series.price_multiplier_for(session_date)
            share_multiplier = self.series.automatic_share_multiplier_for(
                session_date
            )
            raw_prices = {
                name: _decimal_from_number(
                    getattr(bar, name),
                    name,
                    positive=True,
                )
                for name in ("open", "high", "low", "close")
            }
            with localcontext(_DECIMAL_CONTEXT):
                adjusted = {
                    name: +(value * price_multiplier)
                    for name, value in raw_prices.items()
                }
            rows.append(
                AdjustedBarRow(
                    instrument_id=self.identity.instrument_id,
                    identity_fact_id=self.identity.fact_id,
                    symbol=self.identity.symbol,
                    market=self.identity.market,
                    timestamp=bar.timestamp,
                    session_date=session_date,
                    interval=bar.interval,
                    raw_open=raw_prices["open"],
                    raw_high=raw_prices["high"],
                    raw_low=raw_prices["low"],
                    raw_close=raw_prices["close"],
                    adjusted_open=adjusted["open"],
                    adjusted_high=adjusted["high"],
                    adjusted_low=adjusted["low"],
                    adjusted_close=adjusted["close"],
                    price_multiplier=price_multiplier,
                    automatic_share_multiplier=share_multiplier,
                    raw_volume=bar.volume,
                    raw_amount=_decimal_from_number(
                        bar.amount,
                        "amount",
                        non_negative=True,
                    ),
                    raw_turnover=_decimal_from_number(
                        bar.turnover,
                        "turnover",
                        non_negative=True,
                    ),
                    raw_source=bar.source,
                    raw_fields_status=(
                        "PRESERVED_RAW; volume/amount/turnover were not "
                        "vendor-adjusted"
                    ),
                )
            )
        gaps = tuple(
            f"MISSING_OPEN_SESSION:{item.isoformat()}" for item in explicit_gaps
        )
        derived_values = (
            ("raw_bar_snapshot_id", self.raw_snapshot.snapshot_id),
            ("calendar_snapshot_id", self.calendar_snapshot.snapshot_id),
            (
                "corporate_action_snapshot_id",
                self.series.corporate_action_snapshot_id,
            ),
            ("adjustment_series_id", self.series.series_id),
            ("identity_fact_id", self.identity.fact_id),
            ("instrument_id", self.identity.instrument_id),
            ("market", self.identity.market),
            ("start_date", self.series.start_date),
            ("end_date", self.series.end_date),
            ("as_of", series_as_of),
            ("policy_id", self.policy.policy_id),
            ("rows", tuple(rows)),
            ("gaps", gaps),
        )
        for name, value in derived_values:
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "dataset_id",
            fingerprint(
                {
                    "schema": _DATASET_SCHEMA,
                    "raw_bar_snapshot_id": self.raw_bar_snapshot_id,
                    "calendar_snapshot_id": self.calendar_snapshot_id,
                    "corporate_action_snapshot_id": (
                        self.corporate_action_snapshot_id
                    ),
                    "adjustment_series_id": self.adjustment_series_id,
                    "identity_fact_id": self.identity_fact_id,
                    "instrument_id": self.instrument_id,
                    "market": self.market,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "as_of": self.as_of,
                    "policy_id": self.policy_id,
                    "row_ids": [item.row_id for item in self.rows],
                    "gaps": self.gaps,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AdjustedDatasetArtifact:
    dataset_id: str
    data_sha256: str
    data_key: str
    descriptor_key: str
    byte_length: int
    row_count: int

    def __post_init__(self) -> None:
        _require_sha256(self.dataset_id, "dataset_id")
        _require_sha256(self.data_sha256, "data_sha256")
        validate_storage_key(self.data_key)
        validate_storage_key(self.descriptor_key)
        if self.data_key != f"adjusted-market-data/{self.data_sha256}.jsonl":
            raise AdjustedMarketDataError("data_key does not match data SHA-256")
        if self.descriptor_key != (
            f"adjusted-market-data/descriptors/{self.dataset_id}.json"
        ):
            raise AdjustedMarketDataError(
                "descriptor_key does not match dataset identity"
            )
        _require_int(self.byte_length, "byte_length", positive=True)
        _require_int(self.row_count, "row_count", positive=True)


def materialize_adjusted_market_data(
    *,
    raw_snapshot: RawBarSnapshot,
    calendar_snapshot: CalendarMaterializationSnapshot,
    identity: InstrumentIdentityFact,
    series: AdjustmentSeries,
    policy: AdjustedMarketDataPolicy,
    explicit_gap_sessions: tuple[date, ...] = (),
) -> AdjustedMarketDataDataset:
    """Create a formal immutable adjusted dataset from bound inputs."""

    return AdjustedMarketDataDataset(
        raw_snapshot=raw_snapshot,
        calendar_snapshot=calendar_snapshot,
        identity=identity,
        series=series,
        policy=policy,
        explicit_gap_sessions=explicit_gap_sessions,
    )


def _dataset_descriptor(dataset: AdjustedMarketDataDataset, data_sha256: str, data_key: str, byte_length: int) -> dict[str, object]:
    descriptor_key = f"adjusted-market-data/descriptors/{dataset.dataset_id}.json"
    return {
        "schema": _DESCRIPTOR_SCHEMA,
        "dataset_id": dataset.dataset_id,
        "data_sha256": data_sha256,
        "data_key": data_key,
        "descriptor_key": descriptor_key,
        "byte_length": byte_length,
        "row_count": len(dataset.rows),
        "raw_bar_snapshot_id": dataset.raw_bar_snapshot_id,
        "calendar_snapshot_id": dataset.calendar_snapshot_id,
        "corporate_action_snapshot_id": dataset.corporate_action_snapshot_id,
        "adjustment_series_id": dataset.adjustment_series_id,
        "identity_fact_id": dataset.identity_fact_id,
        "instrument_id": dataset.instrument_id,
        "market": dataset.market.value,
        "start_date": dataset.start_date.isoformat(),
        "end_date": dataset.end_date.isoformat(),
        "as_of": dataset.as_of.isoformat().replace("+00:00", "Z"),
        "policy_id": dataset.policy_id,
        "row_ids": [row.row_id for row in dataset.rows],
        "gaps": list(dataset.gaps),
    }


def write_adjusted_market_data_dataset(
    root: str | Path,
    *,
    dataset: AdjustedMarketDataDataset,
) -> AdjustedDatasetArtifact:
    """Write immutable JSONL rows and a content-bound descriptor."""

    if not isinstance(dataset, AdjustedMarketDataDataset):
        raise AdjustedMarketDataError(
            "dataset must be AdjustedMarketDataDataset"
        )
    data_bytes = b"".join(
        (
            json.dumps(row.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        for row in dataset.rows
    )
    data_sha256 = hashlib.sha256(data_bytes).hexdigest()
    data_key = validate_storage_key(
        f"adjusted-market-data/{data_sha256}.jsonl"
    )
    descriptor = _dataset_descriptor(
        dataset,
        data_sha256,
        data_key,
        len(data_bytes),
    )
    descriptor_key = validate_storage_key(
        _require_text(descriptor["descriptor_key"], "descriptor_key")
    )
    descriptor_bytes = (
        json.dumps(descriptor, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    _atomic_write(safe_artifact_path(root_path, data_key), data_bytes)
    _atomic_write(
        safe_artifact_path(root_path, descriptor_key),
        descriptor_bytes,
    )
    return AdjustedDatasetArtifact(
        dataset_id=dataset.dataset_id,
        data_sha256=data_sha256,
        data_key=data_key,
        descriptor_key=descriptor_key,
        byte_length=len(data_bytes),
        row_count=len(dataset.rows),
    )


_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema",
        "dataset_id",
        "data_sha256",
        "data_key",
        "descriptor_key",
        "byte_length",
        "row_count",
        "raw_bar_snapshot_id",
        "calendar_snapshot_id",
        "corporate_action_snapshot_id",
        "adjustment_series_id",
        "identity_fact_id",
        "instrument_id",
        "market",
        "start_date",
        "end_date",
        "as_of",
        "policy_id",
        "row_ids",
        "gaps",
    }
)
_ROW_FIELDS = frozenset(
    {
        "instrument_id",
        "identity_fact_id",
        "symbol",
        "market",
        "timestamp",
        "session_date",
        "interval",
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
        "price_multiplier",
        "automatic_share_multiplier",
        "raw_volume",
        "raw_amount",
        "raw_turnover",
        "raw_source",
        "raw_fields_status",
        "row_id",
    }
)


def _adjusted_row_from_mapping(value: object) -> AdjustedBarRow:
    item = _strict_mapping(value, "adjusted row", _ROW_FIELDS)
    try:
        market = Market(_require_text(item["market"], "market"))
    except ValueError as exc:
        raise AdjustedMarketDataError("adjusted row market is invalid") from exc
    row = AdjustedBarRow(
        instrument_id=_require_text(item["instrument_id"], "instrument_id"),
        identity_fact_id=_require_sha256(
            item["identity_fact_id"],
            "identity_fact_id",
        ),
        symbol=_require_text(item["symbol"], "symbol"),
        market=market,
        timestamp=_parse_datetime_text(item["timestamp"], "timestamp"),
        session_date=_parse_date_text(item["session_date"], "session_date"),
        interval=_require_text(item["interval"], "interval"),
        raw_open=_parse_decimal_text(item["raw_open"], "raw_open"),
        raw_high=_parse_decimal_text(item["raw_high"], "raw_high"),
        raw_low=_parse_decimal_text(item["raw_low"], "raw_low"),
        raw_close=_parse_decimal_text(item["raw_close"], "raw_close"),
        adjusted_open=_parse_decimal_text(
            item["adjusted_open"],
            "adjusted_open",
        ),
        adjusted_high=_parse_decimal_text(
            item["adjusted_high"],
            "adjusted_high",
        ),
        adjusted_low=_parse_decimal_text(
            item["adjusted_low"],
            "adjusted_low",
        ),
        adjusted_close=_parse_decimal_text(
            item["adjusted_close"],
            "adjusted_close",
        ),
        price_multiplier=_parse_decimal_text(
            item["price_multiplier"],
            "price_multiplier",
        ),
        automatic_share_multiplier=_parse_decimal_text(
            item["automatic_share_multiplier"],
            "automatic_share_multiplier",
        ),
        raw_volume=_require_int(item["raw_volume"], "raw_volume"),
        raw_amount=_parse_decimal_text(item["raw_amount"], "raw_amount"),
        raw_turnover=_parse_decimal_text(
            item["raw_turnover"],
            "raw_turnover",
        ),
        raw_source=_require_text(item["raw_source"], "raw_source"),
        raw_fields_status=_require_text(
            item["raw_fields_status"],
            "raw_fields_status",
        ),
    )
    expected_row_id = _require_sha256(item["row_id"], "row_id")
    if row.row_id != expected_row_id:
        raise AdjustedMarketDataError(
            "adjusted row_id does not match row content"
        )
    return row


def _descriptor_dataset_id(descriptor: dict[str, object]) -> str:
    try:
        market = Market(_require_text(descriptor["market"], "market"))
    except ValueError as exc:
        raise AdjustedMarketDataError("descriptor market is invalid") from exc
    row_ids_value = descriptor["row_ids"]
    gaps_value = descriptor["gaps"]
    if not isinstance(row_ids_value, list):
        raise AdjustedMarketDataError("descriptor row_ids must be an array")
    if not isinstance(gaps_value, list):
        raise AdjustedMarketDataError("descriptor gaps must be an array")
    row_ids = tuple(_require_sha256(item, "row_id") for item in row_ids_value)
    gaps = tuple(_require_text(item, "gap") for item in gaps_value)
    if gaps != tuple(sorted(set(gaps))):
        raise AdjustedMarketDataError(
            "descriptor gaps must be sorted and unique"
        )
    return fingerprint(
        {
            "schema": _DATASET_SCHEMA,
            "raw_bar_snapshot_id": _require_sha256(
                descriptor["raw_bar_snapshot_id"],
                "raw_bar_snapshot_id",
            ),
            "calendar_snapshot_id": _require_sha256(
                descriptor["calendar_snapshot_id"],
                "calendar_snapshot_id",
            ),
            "corporate_action_snapshot_id": _require_sha256(
                descriptor["corporate_action_snapshot_id"],
                "corporate_action_snapshot_id",
            ),
            "adjustment_series_id": _require_sha256(
                descriptor["adjustment_series_id"],
                "adjustment_series_id",
            ),
            "identity_fact_id": _require_sha256(
                descriptor["identity_fact_id"],
                "identity_fact_id",
            ),
            "instrument_id": _require_text(
                descriptor["instrument_id"],
                "instrument_id",
            ),
            "market": market,
            "start_date": _parse_date_text(
                descriptor["start_date"],
                "start_date",
            ),
            "end_date": _parse_date_text(descriptor["end_date"], "end_date"),
            "as_of": _parse_datetime_text(descriptor["as_of"], "as_of"),
            "policy_id": _require_sha256(descriptor["policy_id"], "policy_id"),
            "row_ids": list(row_ids),
            "gaps": gaps,
        }
    )


def load_adjusted_market_data_artifact(
    root: str | Path,
    *,
    descriptor_key: str,
) -> tuple[AdjustedDatasetArtifact, tuple[AdjustedBarRow, ...]]:
    """Load and verify an immutable Stage 2F descriptor and JSONL artifact."""

    root_path = Path(root)
    descriptor_path = safe_artifact_path(root_path, descriptor_key)
    try:
        descriptor_text = descriptor_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdjustedMarketDataError(
            "adjusted descriptor is unreadable"
        ) from exc
    descriptor = _strict_mapping(
        _strict_json_text(descriptor_text, "adjusted descriptor"),
        "adjusted descriptor",
        _DESCRIPTOR_FIELDS,
    )
    if descriptor["schema"] != _DESCRIPTOR_SCHEMA:
        raise AdjustedMarketDataError("unsupported adjusted descriptor schema")
    if descriptor["descriptor_key"] != descriptor_key:
        raise AdjustedMarketDataError(
            "descriptor_key does not match requested path"
        )
    dataset_id = _require_sha256(descriptor["dataset_id"], "dataset_id")
    if _descriptor_dataset_id(descriptor) != dataset_id:
        raise AdjustedMarketDataError(
            "dataset_id does not match descriptor identity content"
        )
    artifact = AdjustedDatasetArtifact(
        dataset_id=dataset_id,
        data_sha256=_require_sha256(
            descriptor["data_sha256"],
            "data_sha256",
        ),
        data_key=_require_text(descriptor["data_key"], "data_key"),
        descriptor_key=descriptor_key,
        byte_length=_require_int(
            descriptor["byte_length"],
            "byte_length",
            positive=True,
        ),
        row_count=_require_int(
            descriptor["row_count"],
            "row_count",
            positive=True,
        ),
    )
    data_path = safe_artifact_path(root_path, artifact.data_key)
    try:
        data_bytes = data_path.read_bytes()
    except OSError as exc:
        raise AdjustedMarketDataError(
            "adjusted JSONL artifact is unreadable"
        ) from exc
    if len(data_bytes) != artifact.byte_length:
        raise AdjustedMarketDataError(
            "adjusted JSONL artifact byte length changed"
        )
    if hashlib.sha256(data_bytes).hexdigest() != artifact.data_sha256:
        raise AdjustedMarketDataError("adjusted JSONL artifact hash changed")
    try:
        data_text = data_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AdjustedMarketDataError(
            "adjusted JSONL artifact must be strict UTF-8"
        ) from exc
    lines = data_text.splitlines()
    if len(lines) != artifact.row_count:
        raise AdjustedMarketDataError("adjusted JSONL row_count changed")
    rows = tuple(
        _adjusted_row_from_mapping(
            _strict_json_text(line, f"adjusted row {index}"),
        )
        for index, line in enumerate(lines)
    )
    row_ids_value = descriptor["row_ids"]
    if not isinstance(row_ids_value, list):
        raise AdjustedMarketDataError("descriptor row_ids must be an array")
    descriptor_row_ids = tuple(
        _require_sha256(item, "row_id") for item in row_ids_value
    )
    if tuple(row.row_id for row in rows) != descriptor_row_ids:
        raise AdjustedMarketDataError(
            "descriptor row_ids do not match adjusted JSONL rows"
        )
    row_order = tuple((row.timestamp, row.row_id) for row in rows)
    if row_order != tuple(sorted(row_order)):
        raise AdjustedMarketDataError(
            "adjusted JSONL rows are not deterministically ordered"
        )
    return artifact, rows


__all__ = [
    "AdjustedBarRow",
    "AdjustedDatasetArtifact",
    "AdjustedMarketDataDataset",
    "AdjustedMarketDataError",
    "AdjustedMarketDataPolicy",
    "CalendarMaterializationSnapshot",
    "RawBarSnapshot",
    "RawFieldPolicy",
    "SessionGapPolicy",
    "load_adjusted_market_data_artifact",
    "materialize_adjusted_market_data",
    "write_adjusted_market_data_dataset",
]
