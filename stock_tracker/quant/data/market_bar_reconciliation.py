"""Deterministic cross-source reconciliation for captured market-bar artifacts.

Stage 2G compares exact-raw captures only after each capture has been parsed from
its own immutable bytes.  The report can prove structural consistency and expose
coverage gaps; it cannot manufacture source verification, licence clearance, a
Trust Tier, or T3 research readiness.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from stock_tracker.core.types import Bar, Market

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, exchange_local_date, to_utc
from .bar_artifact import CapturedBarArtifact, validate_captured_market_bars
from .manifest import DataKind, ManifestContractError

REPORT_SCHEMA = "stage2g-market-bar-reconciliation-v1"
DEFAULT_POLICY_VERSION = "stage2g-market-bar-policy-v1"
LICENSE_PENDING = "LICENSE_PENDING"
T3_NOT_REACHED = "T3_NOT_REACHED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRICE_FIELDS = frozenset({"OPEN", "HIGH", "LOW", "CLOSE"})
_FORBIDDEN_PROMOTION_FIELDS = frozenset(
    {"verified", "complete", "trust_tier", "research_grade", "t3_achieved"}
)


class MarketBarReconciliationError(ValueError):
    """Raised when Stage 2G inputs or serialized evidence violate the contract."""


class MarketBarField(StrEnum):
    OPEN = "OPEN"
    HIGH = "HIGH"
    LOW = "LOW"
    CLOSE = "CLOSE"
    VOLUME = "VOLUME"
    AMOUNT = "AMOUNT"
    TURNOVER = "TURNOVER"


class MarketBarLicenseStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    CLEARED_FOR_INTERNAL_RESEARCH = "CLEARED_FOR_INTERNAL_RESEARCH"


class MarketBarFindingSeverity(StrEnum):
    HARD_BLOCK = "HARD_BLOCK"
    TRUST_BLOCK = "TRUST_BLOCK"
    WARNING = "WARNING"
    INFO = "INFO"


class MarketBarComparisonState(StrEnum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class MarketBarCandidateState(StrEnum):
    HARD_BLOCKED = "HARD_BLOCKED"
    STRUCTURALLY_CONSTRUCTIBLE = "STRUCTURALLY_CONSTRUCTIBLE"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBarReconciliationError(f"{name} must be a non-empty trimmed string")
    if len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MarketBarReconciliationError(f"{name} contains invalid characters")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MarketBarReconciliationError(f"{name} must be lowercase SHA-256")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise MarketBarReconciliationError(f"{name} must be boolean")
    return value


def _require_nonnegative_int(value: object, name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise MarketBarReconciliationError(f"{name} must be an integer in 0..{maximum}")
    return value


def _canonical_texts(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(_require_text(value, f"{name} item") for value in values)
    return tuple(sorted(set(result)))


def _session_date(bar: Bar) -> date:
    if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
        return bar.timestamp.date()
    return exchange_local_date(bar.timestamp, bar.market)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise MarketBarReconciliationError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketBarReconciliationError(f"{name} must be finite numeric") from exc
    if not result.is_finite():
        raise MarketBarReconciliationError(f"{name} must be finite numeric")
    return result


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _delta_bps(left: Decimal, right: Decimal) -> Decimal:
    denominator = max(abs(left), abs(right))
    if denominator == 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 34
        return abs(left - right) / denominator * Decimal(10_000)


@dataclass(frozen=True, slots=True)
class MarketBarPoint:
    session_date: date
    open: str
    high: str
    low: str
    close: str
    volume: int
    amount: str
    turnover: str
    row_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise MarketBarReconciliationError("session_date must be date")
        prices = {
            name: _decimal(getattr(self, name), name)
            for name in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in prices.values()):
            raise MarketBarReconciliationError("market-bar prices must be positive")
        if prices["low"] > min(prices.values()) or prices["high"] < max(
            prices.values()
        ):
            raise MarketBarReconciliationError("market-bar OHLC values are inconsistent")
        if type(self.volume) is not int or self.volume < 0:
            raise MarketBarReconciliationError("market-bar volume must be non-negative integer")
        amount = _decimal(self.amount, "amount")
        turnover = _decimal(self.turnover, "turnover")
        if amount < 0 or turnover < 0:
            raise MarketBarReconciliationError(
                "market-bar amount/turnover must be non-negative"
            )
        normalized_values = {
            "open": _decimal_text(prices["open"]),
            "high": _decimal_text(prices["high"]),
            "low": _decimal_text(prices["low"]),
            "close": _decimal_text(prices["close"]),
            "amount": _decimal_text(amount),
            "turnover": _decimal_text(turnover),
        }
        for name, value in normalized_values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "row_id",
            fingerprint(
                {
                    "schema": "stage2g-market-bar-point-v1",
                    "session_date": self.session_date,
                    "open": self.open,
                    "high": self.high,
                    "low": self.low,
                    "close": self.close,
                    "volume": self.volume,
                    "amount": self.amount,
                    "turnover": self.turnover,
                }
            ),
        )

    @classmethod
    def from_bar(cls, bar: Bar) -> MarketBarPoint:
        if not isinstance(bar, Bar):
            raise MarketBarReconciliationError("captured row must be Bar")
        return cls(
            session_date=_session_date(bar),
            open=_decimal_text(_decimal(bar.open, "open")),
            high=_decimal_text(_decimal(bar.high, "high")),
            low=_decimal_text(_decimal(bar.low, "low")),
            close=_decimal_text(_decimal(bar.close, "close")),
            volume=bar.volume,
            amount=_decimal_text(_decimal(bar.amount, "amount")),
            turnover=_decimal_text(_decimal(bar.turnover, "turnover")),
        )

    def value(self, field_name: MarketBarField) -> object:
        return {
            MarketBarField.OPEN: self.open,
            MarketBarField.HIGH: self.high,
            MarketBarField.LOW: self.low,
            MarketBarField.CLOSE: self.close,
            MarketBarField.VOLUME: self.volume,
            MarketBarField.AMOUNT: self.amount,
            MarketBarField.TURNOVER: self.turnover,
        }[field_name]

    def as_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "session_date": self.session_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "turnover": self.turnover,
        }


def _bar_field(bar: MarketBarPoint, field_name: MarketBarField) -> object:
    return bar.value(field_name)


@dataclass(frozen=True, slots=True)
class MarketBarSeriesEvidence:
    captured: CapturedBarArtifact = field(repr=False)
    source_family: str
    adjustment: str
    comparable_fields: tuple[MarketBarField, ...]
    license_status: MarketBarLicenseStatus
    synthetic_fixture: bool
    market: Market = field(init=False)
    symbol: str = field(init=False)
    interval: str = field(init=False)
    source: str = field(init=False)
    raw_artifact_id: str = field(init=False)
    capture_id: str = field(init=False)
    normalized_dataset_id: str = field(init=False)
    artifact_retrieved_at: datetime = field(init=False)
    artifact_validation_state: str = field(init=False)
    rows: tuple[MarketBarPoint, ...] = field(init=False)
    series_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.captured, CapturedBarArtifact):
            raise MarketBarReconciliationError("captured must be CapturedBarArtifact")
        try:
            validate_captured_market_bars(self.captured)
        except ManifestContractError as exc:
            raise MarketBarReconciliationError(
                "captured market-bar identity is invalid"
            ) from exc
        _require_text(self.source_family, "source_family")
        _require_text(self.adjustment, "adjustment")
        if not isinstance(self.license_status, MarketBarLicenseStatus):
            raise MarketBarReconciliationError("license_status is invalid")
        if self.license_status is MarketBarLicenseStatus.CLEARED_FOR_INTERNAL_RESEARCH:
            raise MarketBarReconciliationError(
                "Stage 2G series cannot self-declare licence clearance"
            )
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        capture_synthetic = self.captured.request_parameters.get(
            "synthetic_fixture",
            False,
        )
        _require_bool(capture_synthetic, "captured request synthetic_fixture")
        if self.synthetic_fixture is not capture_synthetic:
            raise MarketBarReconciliationError(
                "synthetic_fixture must match the immutable capture request evidence"
            )
        artifact = self.captured.artifact
        if artifact.kind is not DataKind.MARKET_BARS:
            raise MarketBarReconciliationError("series requires a MARKET_BARS artifact")
        if self.source_family != artifact.source:
            raise MarketBarReconciliationError(
                "Stage 2G source_family must equal the captured source identity"
            )
        market = artifact.market
        if market is None:
            raise MarketBarReconciliationError("market-bar artifact is missing market")
        if self.captured.request_parameters.get("adjustment") != self.adjustment:
            raise MarketBarReconciliationError(
                "series adjustment differs from captured request parameters"
            )
        if not self.comparable_fields or any(
            not isinstance(item, MarketBarField) for item in self.comparable_fields
        ):
            raise MarketBarReconciliationError("comparable_fields is invalid")
        canonical_fields = tuple(sorted(set(self.comparable_fields), key=lambda item: item.value))
        if self.comparable_fields != canonical_fields:
            raise MarketBarReconciliationError(
                "comparable_fields must be sorted and unique"
            )
        if not _PRICE_FIELDS.issubset({item.value for item in self.comparable_fields}):
            raise MarketBarReconciliationError(
                "every market-bar series must expose comparable OHLC fields"
            )
        first = self.captured.bars[0]
        points = tuple(MarketBarPoint.from_bar(bar) for bar in self.captured.bars)
        point_dates = tuple(item.session_date for item in points)
        if point_dates != tuple(sorted(set(point_dates))):
            raise MarketBarReconciliationError(
                "market-bar points must be strictly chronological and unique"
            )
        derived = {
            "market": market,
            "symbol": first.symbol,
            "interval": first.interval,
            "source": artifact.source,
            "raw_artifact_id": artifact.artifact_id,
            "capture_id": self.captured.capture_id,
            "normalized_dataset_id": self.captured.normalized_dataset_id,
            "artifact_retrieved_at": to_utc(artifact.retrieved_at),
            "artifact_validation_state": (
                "SOURCE_DECLARED_VERIFIED"
                if artifact.verified
                else "NOT_INDEPENDENTLY_VERIFIED"
            ),
            "rows": points,
        }
        for name, value in derived.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "series_id",
            fingerprint(
                {
                    "schema": "stage2g-market-bar-series-evidence-v2",
                    "capture_id": self.capture_id,
                    "raw_artifact_id": self.raw_artifact_id,
                    "normalized_dataset_id": self.normalized_dataset_id,
                    "source_family": self.source_family,
                    "market": self.market,
                    "symbol": self.symbol,
                    "interval": self.interval,
                    "adjustment": self.adjustment,
                    "comparable_fields": self.comparable_fields,
                    "license_status": self.license_status,
                    "synthetic_fixture": self.synthetic_fixture,
                    "row_ids": [item.row_id for item in points],
                }
            ),
        )

    def bars_by_session(self) -> dict[date, MarketBarPoint]:
        return {item.session_date: item for item in self.rows}

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "stage2g-market-bar-series-evidence-v2",
            "series_id": self.series_id,
            "capture_id": self.capture_id,
            "raw_artifact_id": self.raw_artifact_id,
            "normalized_dataset_id": self.normalized_dataset_id,
            "source": self.source,
            "source_family": self.source_family,
            "market": self.market.value,
            "symbol": self.symbol,
            "interval": self.interval,
            "adjustment": self.adjustment,
            "row_count": len(self.rows),
            "row_ids": [item.row_id for item in self.rows],
            "comparable_fields": [item.value for item in self.comparable_fields],
            "license_status": self.license_status.value,
            "synthetic_fixture": self.synthetic_fixture,
            "artifact_validation_state": self.artifact_validation_state,
        }


@dataclass(frozen=True, slots=True)
class MarketBarReconciliationPolicy:
    policy_version: str = DEFAULT_POLICY_VERSION
    minimum_independent_sources: int = 2
    price_tolerance_bps: int = 5
    volume_tolerance_bps: int = 50
    amount_tolerance_bps: int = 100
    turnover_tolerance_bps: int = 100
    require_all_open_sessions: bool = True
    require_license_clearance: bool = True
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if (
            type(self.minimum_independent_sources) is not int
            or not 2 <= self.minimum_independent_sources <= 10
        ):
            raise MarketBarReconciliationError(
                "minimum_independent_sources must be an integer in 2..10"
            )
        for name in (
            "price_tolerance_bps",
            "volume_tolerance_bps",
            "amount_tolerance_bps",
            "turnover_tolerance_bps",
        ):
            _require_nonnegative_int(getattr(self, name), name, maximum=100_000)
        _require_bool(self.require_all_open_sessions, "require_all_open_sessions")
        _require_bool(self.require_license_clearance, "require_license_clearance")
        if self.require_all_open_sessions is not True:
            raise MarketBarReconciliationError(
                "Stage 2G policy cannot disable open-session coverage"
            )
        if self.require_license_clearance is not True:
            raise MarketBarReconciliationError(
                "Stage 2G policy cannot disable licence clearance"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "stage2g-market-bar-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_independent_sources": self.minimum_independent_sources,
                    "price_tolerance_bps": self.price_tolerance_bps,
                    "volume_tolerance_bps": self.volume_tolerance_bps,
                    "amount_tolerance_bps": self.amount_tolerance_bps,
                    "turnover_tolerance_bps": self.turnover_tolerance_bps,
                    "require_all_open_sessions": self.require_all_open_sessions,
                    "require_license_clearance": self.require_license_clearance,
                }
            ),
        )

    def tolerance_for(self, field_name: MarketBarField) -> int:
        if field_name in {
            MarketBarField.OPEN,
            MarketBarField.HIGH,
            MarketBarField.LOW,
            MarketBarField.CLOSE,
        }:
            return self.price_tolerance_bps
        if field_name is MarketBarField.VOLUME:
            return self.volume_tolerance_bps
        if field_name is MarketBarField.AMOUNT:
            return self.amount_tolerance_bps
        return self.turnover_tolerance_bps

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "minimum_independent_sources": self.minimum_independent_sources,
            "price_tolerance_bps": self.price_tolerance_bps,
            "volume_tolerance_bps": self.volume_tolerance_bps,
            "amount_tolerance_bps": self.amount_tolerance_bps,
            "turnover_tolerance_bps": self.turnover_tolerance_bps,
            "require_all_open_sessions": self.require_all_open_sessions,
            "require_license_clearance": self.require_license_clearance,
        }


@dataclass(frozen=True, slots=True)
class MarketBarFinding:
    code: str
    severity: MarketBarFindingSeverity
    scope: str
    message: str
    subject_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.code, "finding code")
        if not isinstance(self.severity, MarketBarFindingSeverity):
            raise MarketBarReconciliationError("finding severity is invalid")
        _require_text(self.scope, "finding scope")
        _require_text(self.message, "finding message")
        object.__setattr__(self, "subject_ids", _canonical_texts(self.subject_ids, "subject_ids"))
        object.__setattr__(
            self,
            "evidence_ids",
            _canonical_texts(self.evidence_ids, "evidence_ids"),
        )
        object.__setattr__(self, "details", _canonical_texts(self.details, "details"))

    @property
    def finding_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.value,
            "scope": self.scope,
            "message": self.message,
            "subject_ids": list(self.subject_ids),
            "evidence_ids": list(self.evidence_ids),
            "details": list(self.details),
        }
        if include_id:
            result["finding_id"] = self.finding_id
        return result


@dataclass(frozen=True, slots=True)
class MarketBarFieldComparison:
    field: MarketBarField
    values: tuple[tuple[str, str], ...]
    comparable: bool
    maximum_delta_bps: str | None
    tolerance_bps: int
    conflict: bool

    def __post_init__(self) -> None:
        if not isinstance(self.field, MarketBarField):
            raise MarketBarReconciliationError("comparison field is invalid")
        if self.values != tuple(sorted(self.values)) or len({key for key, _ in self.values}) != len(
            self.values
        ):
            raise MarketBarReconciliationError("comparison values must be sorted by unique series ID")
        _require_bool(self.comparable, "comparable")
        _require_bool(self.conflict, "conflict")
        _require_nonnegative_int(self.tolerance_bps, "tolerance_bps", maximum=100_000)
        if self.comparable:
            if len(self.values) < 2 or self.maximum_delta_bps is None:
                raise MarketBarReconciliationError(
                    "comparable field requires at least two values and a delta"
                )
            delta = _decimal(self.maximum_delta_bps, "maximum_delta_bps")
            if delta < 0:
                raise MarketBarReconciliationError("maximum_delta_bps cannot be negative")
            if self.conflict != (delta > Decimal(self.tolerance_bps)):
                raise MarketBarReconciliationError("field conflict disagrees with tolerance")
        elif self.maximum_delta_bps is not None or self.conflict:
            raise MarketBarReconciliationError(
                "non-comparable field cannot carry a delta or conflict"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field.value,
            "values": [
                {"series_id": series_id, "value": value}
                for series_id, value in self.values
            ],
            "comparable": self.comparable,
            "maximum_delta_bps": self.maximum_delta_bps,
            "tolerance_bps": self.tolerance_bps,
            "conflict": self.conflict,
        }


@dataclass(frozen=True, slots=True)
class MarketBarSessionComparison:
    session_date: date
    fields: tuple[MarketBarFieldComparison, ...]
    state: MarketBarComparisonState
    comparison_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise MarketBarReconciliationError("session_date must be date")
        if self.fields != tuple(sorted(self.fields, key=lambda item: item.field.value)):
            raise MarketBarReconciliationError("comparison fields must be sorted")
        if not isinstance(self.state, MarketBarComparisonState):
            raise MarketBarReconciliationError("comparison state is invalid")
        expected_state = (
            MarketBarComparisonState.CONFLICT
            if any(item.conflict for item in self.fields)
            else MarketBarComparisonState.MATCH
            if any(item.comparable for item in self.fields)
            else MarketBarComparisonState.NOT_COMPARABLE
        )
        if self.state is not expected_state:
            raise MarketBarReconciliationError("comparison state disagrees with field evidence")
        object.__setattr__(
            self,
            "comparison_id",
            fingerprint(
                {
                    "schema": "stage2g-market-bar-session-comparison-v1",
                    "session_date": self.session_date,
                    "fields": [item.as_dict() for item in self.fields],
                    "state": self.state,
                }
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "comparison_id": self.comparison_id,
            "session_date": self.session_date.isoformat(),
            "state": self.state.value,
            "fields": [item.as_dict() for item in self.fields],
        }


@dataclass(frozen=True, slots=True)
class MarketBarCoverageMetrics:
    expected_sessions: tuple[date, ...]
    observed_union_sessions: tuple[date, ...]
    fully_observed_sessions: tuple[date, ...]
    missing_by_series: tuple[tuple[str, tuple[date, ...]], ...]
    unexpected_by_series: tuple[tuple[str, tuple[date, ...]], ...]

    def __post_init__(self) -> None:
        for name in (
            "expected_sessions",
            "observed_union_sessions",
            "fully_observed_sessions",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise MarketBarReconciliationError(f"{name} must be sorted and unique")
        for name in ("missing_by_series", "unexpected_by_series"):
            values = getattr(self, name)
            if values != tuple(sorted(values)) or len({key for key, _ in values}) != len(values):
                raise MarketBarReconciliationError(f"{name} must be sorted by unique series ID")
            for series_id, sessions in values:
                _require_sha256(series_id, "series_id")
                if sessions != tuple(sorted(set(sessions))):
                    raise MarketBarReconciliationError(
                        f"{name} sessions must be sorted and unique"
                    )

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_session_count": len(self.expected_sessions),
            "observed_union_session_count": len(self.observed_union_sessions),
            "fully_observed_session_count": len(self.fully_observed_sessions),
            "expected_sessions": [item.isoformat() for item in self.expected_sessions],
            "observed_union_sessions": [
                item.isoformat() for item in self.observed_union_sessions
            ],
            "fully_observed_sessions": [
                item.isoformat() for item in self.fully_observed_sessions
            ],
            "missing_by_series": {
                series_id: [item.isoformat() for item in sessions]
                for series_id, sessions in self.missing_by_series
            },
            "unexpected_by_series": {
                series_id: [item.isoformat() for item in sessions]
                for series_id, sessions in self.unexpected_by_series
            },
        }


def _field_comparison(
    field_name: MarketBarField,
    session_rows: tuple[tuple[MarketBarSeriesEvidence, MarketBarPoint], ...],
    policy: MarketBarReconciliationPolicy,
) -> MarketBarFieldComparison:
    comparable = len(session_rows) >= 2 and all(
        field_name in series.comparable_fields for series, _ in session_rows
    )
    values = tuple(
        sorted(
            (
                series.series_id,
                _decimal_text(_decimal(_bar_field(bar, field_name), field_name.value)),
            )
            for series, bar in session_rows
        )
    )
    tolerance = policy.tolerance_for(field_name)
    if not comparable:
        return MarketBarFieldComparison(
            field=field_name,
            values=values,
            comparable=False,
            maximum_delta_bps=None,
            tolerance_bps=tolerance,
            conflict=False,
        )
    deltas = [
        _delta_bps(_decimal(left[1], "left value"), _decimal(right[1], "right value"))
        for left, right in combinations(values, 2)
    ]
    maximum = max(deltas, default=Decimal(0))
    return MarketBarFieldComparison(
        field=field_name,
        values=values,
        comparable=True,
        maximum_delta_bps=_decimal_text(maximum),
        tolerance_bps=tolerance,
        conflict=maximum > Decimal(tolerance),
    )


def _comparison_for_session(
    session_date: date,
    session_rows: tuple[tuple[MarketBarSeriesEvidence, MarketBarPoint], ...],
    policy: MarketBarReconciliationPolicy,
) -> MarketBarSessionComparison:
    fields = tuple(
        sorted(
            (
                _field_comparison(field_name, session_rows, policy)
                for field_name in MarketBarField
            ),
            key=lambda item: item.field.value,
        )
    )
    state = (
        MarketBarComparisonState.CONFLICT
        if any(item.conflict for item in fields)
        else MarketBarComparisonState.MATCH
        if any(item.comparable for item in fields)
        else MarketBarComparisonState.NOT_COMPARABLE
    )
    return MarketBarSessionComparison(
        session_date=session_date,
        fields=fields,
        state=state,
    )


@dataclass(frozen=True, slots=True)
class MarketBarReconciliationReport:
    as_of: datetime
    policy: MarketBarReconciliationPolicy
    calendar_snapshot_id: str
    expected_open_sessions: tuple[date, ...]
    series: tuple[MarketBarSeriesEvidence, ...]
    findings: tuple[MarketBarFinding, ...] = field(init=False)
    comparisons: tuple[MarketBarSessionComparison, ...] = field(init=False)
    coverage: MarketBarCoverageMetrics = field(init=False)
    open_blockers: tuple[str, ...] = field(init=False)
    license_status: str = field(init=False)
    evidence_tier_status: str = field(init=False, default=T3_NOT_REACHED)
    candidate_state: MarketBarCandidateState = field(init=False)

    def __post_init__(self) -> None:
        cutoff = to_utc(ensure_aware(self.as_of, "as_of"))
        if not isinstance(self.policy, MarketBarReconciliationPolicy):
            raise MarketBarReconciliationError("policy is invalid")
        calendar_id = _require_sha256(self.calendar_snapshot_id, "calendar_snapshot_id")
        if any(type(item) is not date for item in self.expected_open_sessions):
            raise MarketBarReconciliationError(
                "expected_open_sessions must contain date values"
            )
        sessions = tuple(sorted(set(self.expected_open_sessions)))
        if not sessions or sessions != self.expected_open_sessions:
            raise MarketBarReconciliationError(
                "expected_open_sessions must be non-empty, sorted and unique"
            )
        raw_series = tuple(self.series)
        if not raw_series or any(
            not isinstance(item, MarketBarSeriesEvidence) for item in raw_series
        ):
            raise MarketBarReconciliationError("series must contain MarketBarSeriesEvidence")
        ordered_series = tuple(sorted(raw_series, key=lambda item: item.series_id))
        if len({item.series_id for item in ordered_series}) != len(ordered_series):
            raise MarketBarReconciliationError("series IDs must be unique")
        object.__setattr__(self, "as_of", cutoff)
        object.__setattr__(self, "calendar_snapshot_id", calendar_id)
        object.__setattr__(self, "series", ordered_series)

        findings: list[MarketBarFinding] = []
        first = ordered_series[0]
        identity = (first.market, first.symbol, first.interval, first.adjustment)
        latest_allowed_session = exchange_local_date(cutoff, first.market)
        if any(session >= latest_allowed_session for session in sessions):
            findings.append(
                MarketBarFinding(
                    code="CALENDAR_SESSION_NOT_FINAL_AS_OF",
                    severity=MarketBarFindingSeverity.HARD_BLOCK,
                    scope="CALENDAR",
                    message=(
                        "daily-bar reconciliation requires every expected session "
                        "to precede the reconciliation local date"
                    ),
                    evidence_ids=(calendar_id,),
                )
            )
        findings.append(
            MarketBarFinding(
                code="CALENDAR_BINDING_NOT_INDEPENDENTLY_VERIFIED",
                severity=MarketBarFindingSeverity.TRUST_BLOCK,
                scope="CALENDAR",
                message="Stage 2G binds a Calendar snapshot ID but does not verify its authority",
                evidence_ids=(calendar_id,),
            )
        )
        findings.extend(
            (
                MarketBarFinding(
                    code="RECONCILIATION_POLICY_NOT_INDEPENDENTLY_APPROVED",
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="POLICY",
                    message="Stage 2G policy identity is caller-selected, not independently approved",
                    evidence_ids=(self.policy.policy_id,),
                ),
                MarketBarFinding(
                    code="SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED",
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="REPORT",
                    message="distinct source labels are not independent-source attestation",
                    subject_ids=tuple(item.series_id for item in ordered_series),
                ),
                MarketBarFinding(
                    code="MARKET_BAR_FIELD_UNIT_POLICY_UNVERIFIED",
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="REPORT",
                    message="volume/amount/turnover unit equivalence needs external evidence",
                    subject_ids=tuple(item.series_id for item in ordered_series),
                ),
                MarketBarFinding(
                    code="ADJUSTMENT_POLICY_EQUIVALENCE_UNVERIFIED",
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="REPORT",
                    message="same adjustment label does not prove vendor adjustment equivalence",
                    subject_ids=tuple(item.series_id for item in ordered_series),
                ),
            )
        )
        for item in ordered_series:
            if (item.market, item.symbol, item.interval, item.adjustment) != identity:
                findings.append(
                    MarketBarFinding(
                        code="MARKET_BAR_SERIES_IDENTITY_MISMATCH",
                        severity=MarketBarFindingSeverity.HARD_BLOCK,
                        scope="REPORT",
                        message="all compared series must share market/symbol/interval/adjustment",
                        subject_ids=(item.series_id,),
                    )
                )
            if item.artifact_retrieved_at > cutoff:
                findings.append(
                    MarketBarFinding(
                        code="MARKET_BAR_ARTIFACT_NOT_VISIBLE_AS_OF",
                        severity=MarketBarFindingSeverity.HARD_BLOCK,
                        scope=item.series_id,
                        message="captured artifact was retrieved after reconciliation as_of",
                        evidence_ids=(item.raw_artifact_id,),
                    )
                )
            if item.artifact_validation_state != "SOURCE_DECLARED_VERIFIED":
                findings.append(
                    MarketBarFinding(
                        code="MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED",
                        severity=MarketBarFindingSeverity.TRUST_BLOCK,
                        scope=item.series_id,
                        message="raw capture has no independent source verification evidence",
                        evidence_ids=(item.raw_artifact_id,),
                    )
                )
            if item.synthetic_fixture:
                findings.append(
                    MarketBarFinding(
                        code="SYNTHETIC_MARKET_BAR_EVIDENCE",
                        severity=MarketBarFindingSeverity.TRUST_BLOCK,
                        scope=item.series_id,
                        message="synthetic golden payload proves parser/reconciliation contracts only",
                        evidence_ids=(item.capture_id,),
                    )
                )
            else:
                findings.append(
                    MarketBarFinding(
                        code="LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED",
                        severity=MarketBarFindingSeverity.TRUST_BLOCK,
                        scope=item.series_id,
                        message="non-synthetic capture still lacks independent provenance attestation",
                        evidence_ids=(item.capture_id,),
                    )
                )

        source_counts = Counter(item.source_family for item in ordered_series)
        for source_family, count in sorted(source_counts.items()):
            if count > 1:
                findings.append(
                    MarketBarFinding(
                        code="DUPLICATE_SOURCE_FAMILY_NOT_INDEPENDENT",
                        severity=MarketBarFindingSeverity.TRUST_BLOCK,
                        scope=source_family,
                        message="multiple captures from one source family are not independent corroboration",
                        subject_ids=tuple(
                            item.series_id
                            for item in ordered_series
                            if item.source_family == source_family
                        ),
                    )
                )
        if len(source_counts) < self.policy.minimum_independent_sources:
            findings.append(
                MarketBarFinding(
                    code="INSUFFICIENT_INDEPENDENT_MARKET_BAR_SOURCES",
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="REPORT",
                    message="market-bar evidence has fewer independent sources than policy requires",
                    subject_ids=tuple(item.series_id for item in ordered_series),
                    details=(
                        f"observed={len(source_counts)}",
                        f"required={self.policy.minimum_independent_sources}",
                    ),
                )
            )

        maps = {item.series_id: item.bars_by_session() for item in ordered_series}
        expected = set(sessions)
        observed_union = set().union(*(set(value) for value in maps.values()))
        fully_observed = set(sessions)
        for value in maps.values():
            fully_observed.intersection_update(value)
        missing_by_series = tuple(
            (series_id, tuple(sorted(expected - set(values))))
            for series_id, values in sorted(maps.items())
        )
        unexpected_by_series = tuple(
            (series_id, tuple(sorted(set(values) - expected)))
            for series_id, values in sorted(maps.items())
        )
        coverage = MarketBarCoverageMetrics(
            expected_sessions=sessions,
            observed_union_sessions=tuple(sorted(observed_union)),
            fully_observed_sessions=tuple(sorted(fully_observed)),
            missing_by_series=missing_by_series,
            unexpected_by_series=unexpected_by_series,
        )
        for series_id, missing in missing_by_series:
            if missing:
                findings.append(
                    MarketBarFinding(
                        code="MARKET_BAR_OPEN_SESSION_COVERAGE_GAP",
                        severity=(
                            MarketBarFindingSeverity.HARD_BLOCK
                            if self.policy.require_all_open_sessions
                            else MarketBarFindingSeverity.TRUST_BLOCK
                        ),
                        scope=series_id,
                        message="captured series is missing expected Calendar-open sessions",
                        details=tuple(item.isoformat() for item in missing),
                    )
                )
        for series_id, unexpected in unexpected_by_series:
            if unexpected:
                findings.append(
                    MarketBarFinding(
                        code="MARKET_BAR_ON_CALENDAR_CLOSED_SESSION",
                        severity=MarketBarFindingSeverity.HARD_BLOCK,
                        scope=series_id,
                        message="captured series contains bars outside expected open sessions",
                        details=tuple(item.isoformat() for item in unexpected),
                    )
                )

        comparisons: list[MarketBarSessionComparison] = []
        if all((item.market, item.symbol, item.interval, item.adjustment) == identity for item in ordered_series):
            for session in sorted(fully_observed):
                comparison = _comparison_for_session(
                    session,
                    tuple((item, maps[item.series_id][session]) for item in ordered_series),
                    self.policy,
                )
                comparisons.append(comparison)
                for field_comparison in comparison.fields:
                    if field_comparison.conflict:
                        findings.append(
                            MarketBarFinding(
                                code=f"MARKET_BAR_{field_comparison.field.value}_CONFLICT",
                                severity=MarketBarFindingSeverity.HARD_BLOCK,
                                scope=session.isoformat(),
                                message="cross-source market-bar values exceed policy tolerance",
                                subject_ids=tuple(
                                    series_id
                                    for series_id, _ in field_comparison.values
                                ),
                                details=(
                                    f"maximum_delta_bps={field_comparison.maximum_delta_bps}",
                                    f"tolerance_bps={field_comparison.tolerance_bps}",
                                ),
                            )
                        )
                    elif not field_comparison.comparable:
                        findings.append(
                            MarketBarFinding(
                                code=f"MARKET_BAR_{field_comparison.field.value}_NOT_COMPARABLE",
                                severity=MarketBarFindingSeverity.INFO,
                                scope=session.isoformat(),
                                message="at least one source did not declare this field comparable",
                                subject_ids=tuple(
                                    series_id
                                    for series_id, _ in field_comparison.values
                                ),
                            )
                        )

        license_status = LICENSE_PENDING
        if self.policy.require_license_clearance:
            findings.append(
                MarketBarFinding(
                    code=LICENSE_PENDING,
                    severity=MarketBarFindingSeverity.TRUST_BLOCK,
                    scope="REPORT",
                    message="Stage 2G has no trusted licence-clearance authority",
                    subject_ids=tuple(item.series_id for item in ordered_series),
                )
            )
        findings.append(
            MarketBarFinding(
                code=T3_NOT_REACHED,
                severity=MarketBarFindingSeverity.TRUST_BLOCK,
                scope="REPORT",
                message="Stage 2G reconciliation does not itself promote data to T3",
                evidence_ids=(calendar_id,),
            )
        )
        canonical_findings = tuple(
            sorted(
                {item.finding_id: item for item in findings}.values(),
                key=lambda item: item.finding_id,
            )
        )
        blockers = tuple(
            sorted(
                {
                    item.code
                    for item in canonical_findings
                    if item.severity
                    in {
                        MarketBarFindingSeverity.HARD_BLOCK,
                        MarketBarFindingSeverity.TRUST_BLOCK,
                    }
                }
            )
        )
        state = (
            MarketBarCandidateState.HARD_BLOCKED
            if any(
                item.severity is MarketBarFindingSeverity.HARD_BLOCK
                for item in canonical_findings
            )
            else MarketBarCandidateState.STRUCTURALLY_CONSTRUCTIBLE
        )
        object.__setattr__(self, "findings", canonical_findings)
        object.__setattr__(self, "comparisons", tuple(comparisons))
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "open_blockers", blockers)
        object.__setattr__(self, "license_status", license_status)
        object.__setattr__(self, "candidate_state", state)

    @property
    def finding_counts(self) -> dict[str, int]:
        counts = Counter(item.severity.value for item in self.findings)
        return {
            severity.value: counts.get(severity.value, 0)
            for severity in MarketBarFindingSeverity
        }

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": REPORT_SCHEMA,
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "policy": self.policy.as_dict(),
            "calendar_snapshot_id": self.calendar_snapshot_id,
            "expected_open_sessions": [
                item.isoformat() for item in self.expected_open_sessions
            ],
            "series": [item.as_dict() for item in self.series],
            "comparisons": [item.as_dict() for item in self.comparisons],
            "coverage": self.coverage.as_dict(),
            "findings": [item.as_dict() for item in self.findings],
            "finding_counts": self.finding_counts,
            "open_blockers": list(self.open_blockers),
            "license_status": self.license_status,
            "evidence_tier_status": self.evidence_tier_status,
            "candidate_state": self.candidate_state.value,
        }

    @property
    def report_id(self) -> str:
        return fingerprint(self._identity_payload())

    def as_dict(self) -> dict[str, object]:
        payload = {**self._identity_payload(), "report_id": self.report_id}
        validate_market_bar_report_payload(payload)
        return payload


def validate_market_bar_report_payload(value: object, path: str = "report") -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(_FORBIDDEN_PROMOTION_FIELDS.intersection(value))
        if forbidden:
            raise MarketBarReconciliationError(
                f"{path} cannot contain promotion fields: {', '.join(forbidden)}"
            )
        for key, item in value.items():
            if type(key) is not str:
                raise MarketBarReconciliationError(f"{path} keys must be strings")
            validate_market_bar_report_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_market_bar_report_payload(item, f"{path}[{index}]")


def reconcile_market_bars(
    *,
    as_of: datetime,
    calendar_snapshot_id: str,
    expected_open_sessions: Iterable[date],
    series: Iterable[MarketBarSeriesEvidence],
    policy: MarketBarReconciliationPolicy | None = None,
) -> MarketBarReconciliationReport:
    return MarketBarReconciliationReport(
        as_of=as_of,
        policy=policy or MarketBarReconciliationPolicy(),
        calendar_snapshot_id=calendar_snapshot_id,
        expected_open_sessions=tuple(expected_open_sessions),
        series=tuple(series),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise MarketBarReconciliationError(
                "immutable report path already contains different content"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_market_bar_reconciliation_json(
    report: MarketBarReconciliationReport,
    path: str | Path,
) -> None:
    if not isinstance(report, MarketBarReconciliationReport):
        raise MarketBarReconciliationError("report is invalid")
    _atomic_write_text(
        Path(path),
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def render_market_bar_reconciliation_markdown(
    report: MarketBarReconciliationReport,
) -> str:
    if not isinstance(report, MarketBarReconciliationReport):
        raise MarketBarReconciliationError("report is invalid")
    lines = [
        "# Stage 2G Market-Bar Reconciliation",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Candidate state: `{report.candidate_state.value}`",
        f"- Evidence tier: `{report.evidence_tier_status}`",
        f"- License status: `{report.license_status}`",
        f"- Series: `{len(report.series)}`",
        f"- Expected sessions: `{len(report.expected_open_sessions)}`",
        f"- Fully observed sessions: `{len(report.coverage.fully_observed_sessions)}`",
        "",
        "## Open blockers",
        "",
    ]
    lines.extend(f"- `{item}`" for item in report.open_blockers)
    lines.extend(["", "## Findings", ""])
    for item in report.findings:
        lines.append(
            f"- **{item.severity.value}** `{item.code}` — {item.message}"
        )
    lines.extend(
        [
            "",
            "> This report does not claim source verification, T3 research grade, or investment performance.",
            "",
        ]
    )
    return "\n".join(lines)


def write_market_bar_reconciliation_markdown(
    report: MarketBarReconciliationReport,
    path: str | Path,
) -> None:
    _atomic_write_text(Path(path), render_market_bar_reconciliation_markdown(report))


__all__ = [
    "DEFAULT_POLICY_VERSION",
    "LICENSE_PENDING",
    "REPORT_SCHEMA",
    "T3_NOT_REACHED",
    "MarketBarCandidateState",
    "MarketBarComparisonState",
    "MarketBarCoverageMetrics",
    "MarketBarField",
    "MarketBarFieldComparison",
    "MarketBarFinding",
    "MarketBarFindingSeverity",
    "MarketBarLicenseStatus",
    "MarketBarPoint",
    "MarketBarReconciliationError",
    "MarketBarReconciliationPolicy",
    "MarketBarReconciliationReport",
    "MarketBarSeriesEvidence",
    "MarketBarSessionComparison",
    "reconcile_market_bars",
    "render_market_bar_reconciliation_markdown",
    "validate_market_bar_report_payload",
    "write_market_bar_reconciliation_json",
    "write_market_bar_reconciliation_markdown",
]
