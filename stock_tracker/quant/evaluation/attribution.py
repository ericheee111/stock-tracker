"""Deterministic outcome attribution and same-cohort version comparison.

Attribution is deliberately descriptive rather than causal: it only classifies
facts already bound to an immutable :class:`SignalOutcome`.  Version comparison
requires identical market, horizon, evidence tier, cohort, and evaluation
window.  No object in this module changes strategy weights, deploys a model, or
creates an order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from ..core.fingerprint import fingerprint
from ..core.outcomes import (
    OutcomeEvidenceOrigin,
    OutcomeState,
    OutcomeTerminalReason,
    ScoreboardState,
    SignalOutcome,
    StrategyScoreboard,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal(0)
_ONE = Decimal(1)


class AttributionContractError(ValueError):
    """Raised when attribution or comparison evidence is inconsistent."""


class AttributionCategory(StrEnum):
    ENTRY_NOT_FILLED = "ENTRY_NOT_FILLED"
    DATA_INVALID = "DATA_INVALID"
    TARGET_CAPTURED = "TARGET_CAPTURED"
    STOP_LOSS = "STOP_LOSS"
    TIMEOUT = "TIMEOUT"
    MANUAL_EXIT = "MANUAL_EXIT"
    TRAILING_STOP = "TRAILING_STOP"
    BROKEN_TREND = "BROKEN_TREND"
    COST_DRAG = "COST_DRAG"
    LARGE_ADVERSE_EXCURSION = "LARGE_ADVERSE_EXCURSION"
    EARLY_EXIT_OPPORTUNITY_COST = "EARLY_EXIT_OPPORTUNITY_COST"
    FAVORABLE_EXCURSION_UNCAPTURED = "FAVORABLE_EXCURSION_UNCAPTURED"


class AttributionSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AttributionState(StrEnum):
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    FORMAL_READY = "FORMAL_READY"


class VersionComparisonState(StrEnum):
    BLOCKED = "BLOCKED"
    CANDIDATE_BETTER = "CANDIDATE_BETTER"
    CANDIDATE_NOT_BETTER = "CANDIDATE_NOT_BETTER"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AttributionContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise AttributionContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_decimal(
    value: object,
    name: str,
    *,
    lower: Decimal | None = None,
    upper: Decimal | None = None,
) -> Decimal:
    if type(value) is not Decimal:
        raise AttributionContractError(
            f"{name} must be Decimal; float, integer and boolean are forbidden"
        )
    if not value.is_finite():
        raise AttributionContractError(f"{name} must be finite")
    if lower is not None and value < lower:
        raise AttributionContractError(f"{name} is below its lower bound")
    if upper is not None and value > upper:
        raise AttributionContractError(f"{name} is above its upper bound")
    return value


@dataclass(frozen=True, slots=True)
class OutcomeAttributionPolicy:
    policy_version: str
    cost_drag_threshold_r: Decimal = Decimal("0.20")
    large_mae_threshold_r: Decimal = Decimal("-0.80")
    early_exit_gap_threshold_r: Decimal = Decimal("0.75")
    uncaptured_mfe_threshold_r: Decimal = Decimal("1.00")
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        _require_decimal(
            self.cost_drag_threshold_r,
            "cost_drag_threshold_r",
            lower=_ZERO,
        )
        _require_decimal(
            self.large_mae_threshold_r,
            "large_mae_threshold_r",
            upper=_ZERO,
        )
        _require_decimal(
            self.early_exit_gap_threshold_r,
            "early_exit_gap_threshold_r",
            lower=_ZERO,
        )
        _require_decimal(
            self.uncaptured_mfe_threshold_r,
            "uncaptured_mfe_threshold_r",
            lower=_ZERO,
        )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "outcome-attribution-policy-v1",
                    "policy_version": self.policy_version,
                    "cost_drag_threshold_r": self.cost_drag_threshold_r,
                    "large_mae_threshold_r": self.large_mae_threshold_r,
                    "early_exit_gap_threshold_r": self.early_exit_gap_threshold_r,
                    "uncaptured_mfe_threshold_r": self.uncaptured_mfe_threshold_r,
                }
            ),
        )


DEFAULT_OUTCOME_ATTRIBUTION_POLICY = OutcomeAttributionPolicy(
    policy_version="outcome-attribution-v1"
)


@dataclass(frozen=True, slots=True)
class AttributionFinding:
    category: AttributionCategory
    severity: AttributionSeverity
    outcome_id: str
    metric_value_r: Decimal | None
    detail: str
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.category, AttributionCategory):
            raise AttributionContractError("category must be AttributionCategory")
        if not isinstance(self.severity, AttributionSeverity):
            raise AttributionContractError("severity must be AttributionSeverity")
        _require_sha256(self.outcome_id, "outcome_id")
        if self.metric_value_r is not None:
            _require_decimal(self.metric_value_r, "metric_value_r")
        _require_text(self.detail, "detail")
        object.__setattr__(
            self,
            "finding_id",
            fingerprint(
                {
                    "schema": "attribution-finding-v1",
                    "category": self.category,
                    "severity": self.severity,
                    "outcome_id": self.outcome_id,
                    "metric_value_r": self.metric_value_r,
                    "detail": self.detail,
                }
            ),
        )


def _finding(
    outcome: SignalOutcome,
    category: AttributionCategory,
    severity: AttributionSeverity,
    detail: str,
    metric_value_r: Decimal | None = None,
) -> AttributionFinding:
    return AttributionFinding(
        category=category,
        severity=severity,
        outcome_id=outcome.outcome_id,
        metric_value_r=metric_value_r,
        detail=detail,
    )


def _terminal_finding(outcome: SignalOutcome) -> AttributionFinding:
    if outcome.state is OutcomeState.NO_ENTRY:
        category = (
            AttributionCategory.DATA_INVALID
            if outcome.terminal_reason is OutcomeTerminalReason.DATA_INVALID
            else AttributionCategory.ENTRY_NOT_FILLED
        )
        severity = (
            AttributionSeverity.CRITICAL
            if category is AttributionCategory.DATA_INVALID
            else AttributionSeverity.WARNING
        )
        return _finding(outcome, category, severity, "entry did not produce a fill")
    if outcome.state is OutcomeState.OPEN:
        raise AttributionContractError("open outcome has no terminal attribution")
    mapping = {
        OutcomeTerminalReason.TARGET: (
            AttributionCategory.TARGET_CAPTURED,
            AttributionSeverity.INFO,
        ),
        OutcomeTerminalReason.STOP: (
            AttributionCategory.STOP_LOSS,
            AttributionSeverity.WARNING,
        ),
        OutcomeTerminalReason.TIMEOUT: (
            AttributionCategory.TIMEOUT,
            AttributionSeverity.WARNING,
        ),
        OutcomeTerminalReason.MANUAL: (
            AttributionCategory.MANUAL_EXIT,
            AttributionSeverity.WARNING,
        ),
        OutcomeTerminalReason.TRAILING_STOP: (
            AttributionCategory.TRAILING_STOP,
            AttributionSeverity.INFO,
        ),
        OutcomeTerminalReason.BROKEN_TREND: (
            AttributionCategory.BROKEN_TREND,
            AttributionSeverity.WARNING,
        ),
        OutcomeTerminalReason.DATA_INVALID: (
            AttributionCategory.DATA_INVALID,
            AttributionSeverity.CRITICAL,
        ),
    }
    if outcome.terminal_reason not in mapping:
        raise AttributionContractError("complete outcome has unsupported terminal reason")
    category, severity = mapping[outcome.terminal_reason]
    return _finding(
        outcome,
        category,
        severity,
        f"terminal reason was {outcome.terminal_reason.value}",
        None if outcome.metrics is None else outcome.metrics.realized_r,
    )


@dataclass(frozen=True, slots=True)
class OutcomeAttribution:
    outcome: SignalOutcome
    policy: OutcomeAttributionPolicy = DEFAULT_OUTCOME_ATTRIBUTION_POLICY
    findings: tuple[AttributionFinding, ...] = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    state: AttributionState = field(init=False)
    attribution_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SignalOutcome):
            raise AttributionContractError("outcome must be SignalOutcome")
        if not isinstance(self.policy, OutcomeAttributionPolicy):
            raise AttributionContractError("policy must be OutcomeAttributionPolicy")
        if self.outcome.state is OutcomeState.OPEN:
            raise AttributionContractError("open outcome cannot be attributed as terminal")

        findings: list[AttributionFinding] = [_terminal_finding(self.outcome)]
        if self.outcome.metrics is not None:
            metrics = self.outcome.metrics
            risk = self.outcome.risk_per_share
            if risk is None:
                raise AttributionContractError("complete outcome is missing derived risk")
            quantity = self.outcome.entry_fill.quantity if self.outcome.entry_fill else 0
            if quantity <= 0:
                raise AttributionContractError("complete outcome is missing entry quantity")
            cost_drag_r = metrics.total_cost / (risk * Decimal(quantity))
            if cost_drag_r >= self.policy.cost_drag_threshold_r:
                findings.append(
                    _finding(
                        self.outcome,
                        AttributionCategory.COST_DRAG,
                        AttributionSeverity.WARNING,
                        "total transaction cost consumed a material share of initial risk",
                        cost_drag_r,
                    )
                )
            if metrics.mae_r <= self.policy.large_mae_threshold_r:
                findings.append(
                    _finding(
                        self.outcome,
                        AttributionCategory.LARGE_ADVERSE_EXCURSION,
                        AttributionSeverity.WARNING,
                        "adverse excursion approached or exceeded the configured risk limit",
                        metrics.mae_r,
                    )
                )
            capture_gap = metrics.mfe_r - metrics.realized_r
            early_reasons = {
                OutcomeTerminalReason.MANUAL,
                OutcomeTerminalReason.TRAILING_STOP,
                OutcomeTerminalReason.BROKEN_TREND,
            }
            if (
                self.outcome.terminal_reason in early_reasons
                and capture_gap >= self.policy.early_exit_gap_threshold_r
            ):
                findings.append(
                    _finding(
                        self.outcome,
                        AttributionCategory.EARLY_EXIT_OPPORTUNITY_COST,
                        AttributionSeverity.WARNING,
                        "realized R materially lagged the maximum favorable excursion",
                        capture_gap,
                    )
                )
            elif capture_gap >= self.policy.uncaptured_mfe_threshold_r:
                findings.append(
                    _finding(
                        self.outcome,
                        AttributionCategory.FAVORABLE_EXCURSION_UNCAPTURED,
                        AttributionSeverity.INFO,
                        "a material favorable excursion was not retained at exit",
                        capture_gap,
                    )
                )

        blockers: set[str] = set()
        if not self.outcome.real_scoreboard_eligible:
            blockers.add("OUTCOME_NOT_FORMAL_SCOREBOARD_ELIGIBLE")
        if self.outcome.origin is not OutcomeEvidenceOrigin.LIVE_OBSERVED:
            blockers.add(f"ORIGIN:{self.outcome.origin.value}")
        blocker_tuple = tuple(sorted(blockers))
        state = (
            AttributionState.FORMAL_READY
            if not blocker_tuple
            else AttributionState.DIAGNOSTIC_ONLY
        )
        finding_tuple = tuple(sorted(findings, key=lambda item: item.finding_id))
        object.__setattr__(self, "findings", finding_tuple)
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "attribution_id",
            fingerprint(
                {
                    "schema": "outcome-attribution-v1",
                    "outcome_id": self.outcome.outcome_id,
                    "policy_id": self.policy.policy_id,
                    "finding_ids": [item.finding_id for item in finding_tuple],
                    "blockers": blocker_tuple,
                    "state": state,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class VersionComparisonPolicy:
    policy_version: str
    minimum_average_r_improvement: Decimal = Decimal("0.10")
    minimum_recent_expectancy_improvement: Decimal = Decimal("0.00")
    maximum_drawdown_regression_r: Decimal = Decimal("0.00")
    maximum_win_rate_regression: Decimal = Decimal("0.00")
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        for name in (
            "minimum_average_r_improvement",
            "minimum_recent_expectancy_improvement",
            "maximum_drawdown_regression_r",
            "maximum_win_rate_regression",
        ):
            _require_decimal(getattr(self, name), name, lower=_ZERO)
        if self.maximum_win_rate_regression > _ONE:
            raise AttributionContractError("maximum_win_rate_regression cannot exceed one")
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "version-comparison-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_average_r_improvement": self.minimum_average_r_improvement,
                    "minimum_recent_expectancy_improvement": (
                        self.minimum_recent_expectancy_improvement
                    ),
                    "maximum_drawdown_regression_r": self.maximum_drawdown_regression_r,
                    "maximum_win_rate_regression": self.maximum_win_rate_regression,
                }
            ),
        )


DEFAULT_VERSION_COMPARISON_POLICY = VersionComparisonPolicy(
    policy_version="strategy-version-comparison-v1"
)


@dataclass(frozen=True, slots=True)
class StrategyVersionComparison:
    baseline: StrategyScoreboard
    candidate: StrategyScoreboard
    policy: VersionComparisonPolicy = DEFAULT_VERSION_COMPARISON_POLICY
    blockers: tuple[str, ...] = field(init=False)
    reasons: tuple[str, ...] = field(init=False)
    average_r_delta: Decimal | None = field(init=False)
    recent_expectancy_delta: Decimal | None = field(init=False)
    max_drawdown_delta_r: Decimal | None = field(init=False)
    win_rate_delta: Decimal | None = field(init=False)
    state: VersionComparisonState = field(init=False)
    comparison_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.baseline, StrategyScoreboard) or not isinstance(
            self.candidate,
            StrategyScoreboard,
        ):
            raise AttributionContractError(
                "baseline and candidate must be StrategyScoreboard values"
            )
        if not isinstance(self.policy, VersionComparisonPolicy):
            raise AttributionContractError("policy must be VersionComparisonPolicy")
        blockers: set[str] = set()
        if self.baseline.strategy_id != self.candidate.strategy_id:
            blockers.add("STRATEGY_ID_MISMATCH")
        if self.baseline.strategy_version == self.candidate.strategy_version:
            blockers.add("STRATEGY_VERSION_NOT_DIFFERENT")
        for name in (
            "market",
            "horizon_sessions",
            "evidence_tier",
            "cohort_id",
            "window_start",
            "window_end",
            "as_of",
        ):
            if getattr(self.baseline, name) != getattr(self.candidate, name):
                blockers.add(f"{name.upper()}_MISMATCH")
        if self.baseline.policy.policy_id != self.candidate.policy.policy_id:
            blockers.add("SCOREBOARD_POLICY_MISMATCH")
        if self.baseline.state is not ScoreboardState.REAL_EVIDENCE_AVAILABLE:
            blockers.add("BASELINE_REAL_EVIDENCE_UNAVAILABLE")
        if self.candidate.state is not ScoreboardState.REAL_EVIDENCE_AVAILABLE:
            blockers.add("CANDIDATE_REAL_EVIDENCE_UNAVAILABLE")
        if self.baseline.metrics is None or self.candidate.metrics is None:
            blockers.add("SCOREBOARD_METRICS_MISSING")

        average_delta: Decimal | None = None
        recent_delta: Decimal | None = None
        drawdown_delta: Decimal | None = None
        win_rate_delta: Decimal | None = None
        reasons: set[str] = set()
        if not blockers:
            assert self.baseline.metrics is not None
            assert self.candidate.metrics is not None
            average_delta = (
                self.candidate.metrics.average_r - self.baseline.metrics.average_r
            )
            recent_delta = (
                self.candidate.metrics.recent_weighted_expectancy_r
                - self.baseline.metrics.recent_weighted_expectancy_r
            )
            drawdown_delta = (
                self.candidate.metrics.max_drawdown_r
                - self.baseline.metrics.max_drawdown_r
            )
            win_rate_delta = (
                self.candidate.metrics.win_rate - self.baseline.metrics.win_rate
            )
            if average_delta < self.policy.minimum_average_r_improvement:
                reasons.add("AVERAGE_R_NOT_IMPROVED")
            if recent_delta < self.policy.minimum_recent_expectancy_improvement:
                reasons.add("RECENT_EXPECTANCY_NOT_IMPROVED")
            if drawdown_delta > self.policy.maximum_drawdown_regression_r:
                reasons.add("MAX_DRAWDOWN_REGRESSED")
            if win_rate_delta < -self.policy.maximum_win_rate_regression:
                reasons.add("WIN_RATE_REGRESSED")

        blocker_tuple = tuple(sorted(blockers))
        reason_tuple = tuple(sorted(reasons))
        if blocker_tuple:
            state = VersionComparisonState.BLOCKED
        elif reason_tuple:
            state = VersionComparisonState.CANDIDATE_NOT_BETTER
        else:
            state = VersionComparisonState.CANDIDATE_BETTER
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "reasons", reason_tuple)
        object.__setattr__(self, "average_r_delta", average_delta)
        object.__setattr__(self, "recent_expectancy_delta", recent_delta)
        object.__setattr__(self, "max_drawdown_delta_r", drawdown_delta)
        object.__setattr__(self, "win_rate_delta", win_rate_delta)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "comparison_id",
            fingerprint(
                {
                    "schema": "strategy-version-comparison-v1",
                    "baseline_scoreboard_id": self.baseline.scoreboard_id,
                    "candidate_scoreboard_id": self.candidate.scoreboard_id,
                    "policy_id": self.policy.policy_id,
                    "blockers": blocker_tuple,
                    "reasons": reason_tuple,
                    "average_r_delta": average_delta,
                    "recent_expectancy_delta": recent_delta,
                    "max_drawdown_delta_r": drawdown_delta,
                    "win_rate_delta": win_rate_delta,
                    "state": state,
                    "changes_runtime_weight": False,
                    "deploys_model": False,
                }
            ),
        )

    @property
    def changes_runtime_weight(self) -> bool:
        return False

    @property
    def deploys_model(self) -> bool:
        return False


__all__ = [
    "DEFAULT_OUTCOME_ATTRIBUTION_POLICY",
    "DEFAULT_VERSION_COMPARISON_POLICY",
    "AttributionCategory",
    "AttributionContractError",
    "AttributionFinding",
    "AttributionSeverity",
    "AttributionState",
    "OutcomeAttribution",
    "OutcomeAttributionPolicy",
    "StrategyVersionComparison",
    "VersionComparisonPolicy",
    "VersionComparisonState",
]
