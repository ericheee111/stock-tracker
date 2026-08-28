"""Deterministic synthetic XTP shadow-reconciliation engineering evidence.

The module deliberately does not call a live account or promote any source. It
compares bounded, frozen observations using explicit tolerances and preserves
conflicts as conflicts. The output cannot be interpreted as strategy accuracy,
PIT research evidence, or permission to trade.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sidecars.xtp.contracts import canonical_json_bytes, validate_symbol

_FIXTURE_AS_OF = datetime(2026, 8, 27, 2, 30, tzinfo=timezone.utc)
_REFERENCE_SOURCES = ("tencent", "eastmoney", "hithink_finance", "free_stockdb")


class ShadowContractError(ValueError):
    """Raised when shadow evidence is malformed or overclaims its scope."""


class ComparisonStatus(StrEnum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    NON_OVERLAPPING_FREQUENCY = "NON_OVERLAPPING_FREQUENCY"


@dataclass(frozen=True, slots=True)
class ShadowThresholds:
    maximum_timestamp_delta_ms: int = 3000
    maximum_price_difference_bps: float = 8.0
    maximum_volume_difference_ratio: float = 0.03

    def __post_init__(self) -> None:
        if (
            type(self.maximum_timestamp_delta_ms) is not int
            or not 0 <= self.maximum_timestamp_delta_ms <= 60000
        ):
            raise ShadowContractError(
                "maximum_timestamp_delta_ms must be an integer in [0, 60000]"
            )
        for value, name, maximum in (
            (
                self.maximum_price_difference_bps,
                "maximum_price_difference_bps",
                1000.0,
            ),
            (
                self.maximum_volume_difference_ratio,
                "maximum_volume_difference_ratio",
                1.0,
            ),
        ):
            if type(value) not in (int, float):
                raise ShadowContractError(f"{name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or not 0 <= number <= maximum:
                raise ShadowContractError(f"{name} is outside the allowed range")


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    source: str
    symbol: str
    board: str
    scenario: str
    observed_at: datetime | None
    last: float | None
    cumulative_volume: int | None
    frequency: str
    available: bool = True
    synthetic_fixture: bool = True

    def __post_init__(self) -> None:
        if self.source not in {"xtp", *_REFERENCE_SOURCES}:
            raise ShadowContractError("unsupported shadow source")
        validate_symbol(self.symbol)
        if self.board not in {"SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"}:
            raise ShadowContractError("unsupported A-share board")
        if type(self.scenario) is not str or not self.scenario.strip():
            raise ShadowContractError("scenario is required")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ShadowContractError("observed_at must be timezone-aware")
        if self.last is not None:
            if type(self.last) not in (int, float):
                raise ShadowContractError("last must be numeric or null")
            number = float(self.last)
            if not math.isfinite(number) or number <= 0:
                raise ShadowContractError("last must be positive and finite")
        if self.cumulative_volume is not None and (
            type(self.cumulative_volume) is not int
            or self.cumulative_volume < 0
        ):
            raise ShadowContractError(
                "cumulative_volume must be a non-negative integer or null"
            )
        if self.frequency not in {"SNAPSHOT", "DAILY"}:
            raise ShadowContractError("frequency must be SNAPSHOT or DAILY")
        if type(self.available) is not bool or type(self.synthetic_fixture) is not bool:
            raise ShadowContractError("availability and fixture flags must be booleans")
        if not self.synthetic_fixture:
            raise ShadowContractError(
                "this engineering fixture builder cannot relabel data as live evidence"
            )
        if not self.available and any(
            value is not None
            for value in (self.observed_at, self.last, self.cumulative_volume)
        ):
            raise ShadowContractError(
                "unavailable observation must not contain market values"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "board": self.board,
            "scenario": self.scenario,
            "observed_at": None
            if self.observed_at is None
            else self.observed_at.astimezone(timezone.utc).isoformat(),
            "last": self.last,
            "cumulative_volume": self.cumulative_volume,
            "frequency": self.frequency,
            "available": self.available,
            "synthetic_fixture": self.synthetic_fixture,
        }


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    symbol: str
    board: str
    scenario: str
    reference_source: str
    status: ComparisonStatus
    timestamp_delta_ms: int | None
    price_difference_bps: float | None
    volume_difference_ratio: float | None
    conflict_reasons: tuple[str, ...]
    source_winner: None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "board": self.board,
            "scenario": self.scenario,
            "reference_source": self.reference_source,
            "status": self.status.value,
            "timestamp_delta_ms": self.timestamp_delta_ms,
            "price_difference_bps": self.price_difference_bps,
            "volume_difference_ratio": self.volume_difference_ratio,
            "conflict_reasons": list(self.conflict_reasons),
            "source_winner": None,
        }


def _price_bps(left: float, right: float) -> float:
    midpoint = (left + right) / 2.0
    if midpoint <= 0:
        raise ShadowContractError("price midpoint must be positive")
    return round(abs(left - right) / midpoint * 10000.0, 4)


def _volume_ratio(left: int, right: int) -> float:
    denominator = max(left, right, 1)
    return round(abs(left - right) / denominator, 6)


def compare_observations(
    xtp: ShadowObservation,
    reference: ShadowObservation,
    thresholds: ShadowThresholds,
) -> ShadowComparison:
    if xtp.source != "xtp":
        raise ShadowContractError("left observation must be XTP")
    if reference.source not in _REFERENCE_SOURCES:
        raise ShadowContractError("right observation must be a reference source")
    if xtp.symbol != reference.symbol or xtp.board != reference.board:
        raise ShadowContractError("shadow observations refer to different instruments")
    if xtp.scenario != reference.scenario:
        raise ShadowContractError("shadow observations refer to different scenarios")
    if not reference.available:
        return ShadowComparison(
            xtp.symbol,
            xtp.board,
            xtp.scenario,
            reference.source,
            ComparisonStatus.SOURCE_UNAVAILABLE,
            None,
            None,
            None,
            ("REFERENCE_SOURCE_UNAVAILABLE",),
        )
    if reference.frequency != xtp.frequency:
        return ShadowComparison(
            xtp.symbol,
            xtp.board,
            xtp.scenario,
            reference.source,
            ComparisonStatus.NON_OVERLAPPING_FREQUENCY,
            None,
            None,
            None,
            ("NON_OVERLAPPING_FREQUENCY",),
        )
    if (
        xtp.observed_at is None
        or reference.observed_at is None
        or xtp.last is None
        or reference.last is None
        or xtp.cumulative_volume is None
        or reference.cumulative_volume is None
    ):
        raise ShadowContractError(
            "available overlapping observations require timestamp, price and volume"
        )
    timestamp_delta = abs(
        int(
            (
                xtp.observed_at.astimezone(timezone.utc)
                - reference.observed_at.astimezone(timezone.utc)
            ).total_seconds()
            * 1000
        )
    )
    price_difference = _price_bps(float(xtp.last), float(reference.last))
    volume_difference = _volume_ratio(
        xtp.cumulative_volume,
        reference.cumulative_volume,
    )
    reasons: list[str] = []
    if timestamp_delta > thresholds.maximum_timestamp_delta_ms:
        reasons.append("TIMESTAMP_DELTA_EXCEEDED")
    if price_difference > thresholds.maximum_price_difference_bps:
        reasons.append("PRICE_DIFFERENCE_EXCEEDED")
    if volume_difference > thresholds.maximum_volume_difference_ratio:
        reasons.append("VOLUME_DIFFERENCE_EXCEEDED")
    status = ComparisonStatus.CONFLICT if reasons else ComparisonStatus.MATCH
    return ShadowComparison(
        xtp.symbol,
        xtp.board,
        xtp.scenario,
        reference.source,
        status,
        timestamp_delta,
        price_difference,
        volume_difference,
        tuple(reasons),
    )


def representative_symbols() -> tuple[tuple[str, str], ...]:
    """Return 64 deterministic A-share symbols spanning four listing boards."""

    symbols: list[tuple[str, str]] = []
    symbols.extend((f"600{index:03d}.SH", "SH_MAIN") for index in range(1, 17))
    symbols.extend((f"000{index:03d}.SZ", "SZ_MAIN") for index in range(1, 17))
    symbols.extend((f"300{index:03d}.SZ", "CHINEXT") for index in range(1, 17))
    symbols.extend((f"688{index:03d}.SH", "STAR") for index in range(1, 17))
    return tuple(symbols)


def _scenario(index: int) -> str:
    scenarios = (
        "NORMAL_TRADING",
        "SUSPENDED",
        "ST_SECURITY",
        "LIMIT_UP",
        "LIMIT_DOWN",
        "DUPLICATE_CALLBACK",
        "OUT_OF_ORDER_CALLBACK",
        "RECONNECT_RECOVERY",
        "LUNCH_BREAK",
        "MARKET_OPEN",
        "MARKET_CLOSE",
        "PROVIDER_SEQUENCE_UNAVAILABLE",
        "STALE_REFERENCE",
        "REFERENCE_CONFLICT",
        "LOW_LIQUIDITY",
        "HIGH_LIQUIDITY",
    )
    return scenarios[index % len(scenarios)]


def build_shadow_fixture() -> tuple[
    tuple[ShadowObservation, ...],
    tuple[ShadowObservation, ...],
    tuple[dict[str, Any], ...],
]:
    xtp_rows: list[ShadowObservation] = []
    references: list[ShadowObservation] = []
    scenarios: list[dict[str, Any]] = []
    for index, (symbol, board) in enumerate(representative_symbols()):
        scenario = _scenario(index)
        observed_at = _FIXTURE_AS_OF + timedelta(milliseconds=index * 7)
        base_price = round(8.0 + index * 0.37, 3)
        volume = 100000 + index * 1377
        xtp_rows.append(
            ShadowObservation(
                "xtp",
                symbol,
                board,
                scenario,
                observed_at,
                base_price,
                volume,
                "SNAPSHOT",
            )
        )
        # Tencent is the closest snapshot reference. Some cases intentionally
        # contain a conflict to prove the reconciler does not choose a winner.
        tencent_price = (
            round(base_price * 1.0025, 3)
            if scenario == "REFERENCE_CONFLICT"
            else round(base_price * 1.0002, 3)
        )
        tencent_time = observed_at + (
            timedelta(seconds=8)
            if scenario == "STALE_REFERENCE"
            else timedelta(milliseconds=400 + index % 5 * 30)
        )
        tencent_volume = (
            int(volume * 0.90)
            if scenario == "REFERENCE_CONFLICT"
            else volume + index % 17
        )
        references.append(
            ShadowObservation(
                "tencent",
                symbol,
                board,
                scenario,
                tencent_time,
                tencent_price,
                tencent_volume,
                "SNAPSHOT",
            )
        )
        # Eastmoney is intentionally unavailable for suspended fixture rows.
        if scenario == "SUSPENDED":
            references.append(
                ShadowObservation(
                    "eastmoney",
                    symbol,
                    board,
                    scenario,
                    None,
                    None,
                    None,
                    "SNAPSHOT",
                    available=False,
                )
            )
        else:
            references.append(
                ShadowObservation(
                    "eastmoney",
                    symbol,
                    board,
                    scenario,
                    observed_at + timedelta(milliseconds=700),
                    round(base_price * 0.9998, 3),
                    volume + index % 23,
                    "SNAPSHOT",
                )
            )
        # HiThink and free-stockdb remain daily references and therefore are
        # reported as non-overlapping, not silently compared as live snapshots.
        for source in ("hithink_finance", "free_stockdb"):
            references.append(
                ShadowObservation(
                    source,
                    symbol,
                    board,
                    scenario,
                    _FIXTURE_AS_OF.replace(hour=0, minute=0, second=0, microsecond=0),
                    base_price,
                    volume,
                    "DAILY",
                )
            )
        scenarios.append(
            {
                "symbol": symbol,
                "board": board,
                "scenario": scenario,
                "duplicate_callback_count": 1
                if scenario == "DUPLICATE_CALLBACK"
                else 0,
                "out_of_order_count": 1
                if scenario == "OUT_OF_ORDER_CALLBACK"
                else 0,
                "reconnect_count": 1
                if scenario == "RECONNECT_RECOVERY"
                else 0,
                "provider_sequence_available": scenario
                != "PROVIDER_SEQUENCE_UNAVAILABLE",
                "session_transition": scenario
                in {"RECONNECT_RECOVERY", "MARKET_OPEN", "MARKET_CLOSE"},
            }
        )
    return tuple(xtp_rows), tuple(references), tuple(scenarios)


def run_shadow_acceptance(
    *,
    thresholds: ShadowThresholds | None = None,
) -> dict[str, Any]:
    effective = thresholds or ShadowThresholds()
    xtp_rows, reference_rows, scenario_rows = build_shadow_fixture()
    reference_index = {
        (row.symbol, row.source): row for row in reference_rows
    }
    comparisons: list[ShadowComparison] = []
    for xtp in xtp_rows:
        for source in _REFERENCE_SOURCES:
            comparisons.append(
                compare_observations(
                    xtp,
                    reference_index[(xtp.symbol, source)],
                    effective,
                )
            )
    status_counts = {
        status.value: sum(1 for item in comparisons if item.status is status)
        for status in ComparisonStatus
    }
    board_counts = {
        board: sum(1 for row in xtp_rows if row.board == board)
        for board in ("SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR")
    }
    scenario_names = {row["scenario"] for row in scenario_rows}
    required_scenarios = {
        "SUSPENDED",
        "ST_SECURITY",
        "LIMIT_UP",
        "LIMIT_DOWN",
        "DUPLICATE_CALLBACK",
        "OUT_OF_ORDER_CALLBACK",
        "RECONNECT_RECOVERY",
        "LUNCH_BREAK",
        "MARKET_OPEN",
        "MARKET_CLOSE",
        "PROVIDER_SEQUENCE_UNAVAILABLE",
        "STALE_REFERENCE",
        "REFERENCE_CONFLICT",
    }
    checks = {
        "representative_symbol_count": 50 <= len(xtp_rows) <= 100,
        "all_four_boards_present": all(count > 0 for count in board_counts.values()),
        "required_scenarios_present": required_scenarios.issubset(scenario_names),
        "conflicts_preserved": status_counts[ComparisonStatus.CONFLICT.value] > 0,
        "unavailable_preserved": status_counts[
            ComparisonStatus.SOURCE_UNAVAILABLE.value
        ]
        > 0,
        "daily_sources_not_mislabeled_live": status_counts[
            ComparisonStatus.NON_OVERLAPPING_FREQUENCY.value
        ]
        == len(xtp_rows) * 2,
        "no_source_winner": all(item.source_winner is None for item in comparisons),
        "duplicate_fixture_present": any(
            row["duplicate_callback_count"] > 0 for row in scenario_rows
        ),
        "out_of_order_fixture_present": any(
            row["out_of_order_count"] > 0 for row in scenario_rows
        ),
        "reconnect_fixture_present": any(
            row["reconnect_count"] > 0 for row in scenario_rows
        ),
        "sequence_unavailable_is_explicit": any(
            row["provider_sequence_available"] is False for row in scenario_rows
        ),
    }
    identity = {
        "schema": "stock-tracker-xtp-shadow-fixture-identity-v1",
        "as_of": _FIXTURE_AS_OF.isoformat(),
        "thresholds": {
            "maximum_timestamp_delta_ms": effective.maximum_timestamp_delta_ms,
            "maximum_price_difference_bps": effective.maximum_price_difference_bps,
            "maximum_volume_difference_ratio": effective.maximum_volume_difference_ratio,
        },
        "xtp": [row.as_dict() for row in xtp_rows],
        "references": [row.as_dict() for row in reference_rows],
        "scenarios": list(scenario_rows),
    }
    fixture_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return {
        "schema": "stock-tracker-xtp-shadow-acceptance-v1",
        "fixture_id": fixture_id,
        "fixture_as_of": _FIXTURE_AS_OF.isoformat(),
        "engineering_passed": all(checks.values()),
        "checks": checks,
        "symbol_count": len(xtp_rows),
        "board_counts": board_counts,
        "scenario_count": len(scenario_names),
        "comparison_count": len(comparisons),
        "comparison_status_counts": status_counts,
        "comparisons": [item.as_dict() for item in comparisons],
        "scenario_evidence": list(scenario_rows),
        "synthetic_fixture_only": True,
        "operational_live_account_pending": True,
        "stock_test_account_registration": "USER_REPORTED_NOT_MACHINE_VERIFIED",
        "algorithm_test_account_registration": "USER_REPORTED_NOT_MACHINE_VERIFIED",
        "algorithm_account_used": False,
        "no_real_strategy_claim": True,
        "allow_live_decision": False,
        "allow_model_training": False,
        "allow_public_redistribution": False,
        "auto_trade": False,
        "source_promotion_performed": False,
        "production_database_modified": False,
        "evidence_tier_status": "T3_NOT_REACHED",
    }


__all__ = [
    "ComparisonStatus",
    "ShadowComparison",
    "ShadowContractError",
    "ShadowObservation",
    "ShadowThresholds",
    "build_shadow_fixture",
    "compare_observations",
    "representative_symbols",
    "run_shadow_acceptance",
]
