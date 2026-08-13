"""Candidate specifications and probability-to-action safety boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint


class CandidateContractError(ValueError):
    """Raised when a model candidate lacks a safe decision boundary."""


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CandidateContractError(f"{name} must be a boolean")
    return value


def _require_probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateContractError(f"{name} must be a finite number in [0, 1]")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise CandidateContractError(f"{name} must be finite and in [0, 1]")
    return result


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    name: str
    model_family: str
    strategy_id: str
    market: Market
    horizon_sessions: int
    feature_set_id: str
    label_version: str
    requires_calibration: bool = True
    probability_is_advisory_only: bool = True

    def __post_init__(self) -> None:
        _require_bool(self.requires_calibration, "requires_calibration")
        _require_bool(
            self.probability_is_advisory_only,
            "probability_is_advisory_only",
        )
        if not self.name or not self.model_family or not self.strategy_id:
            raise CandidateContractError("candidate identity fields are required")
        if isinstance(self.horizon_sessions, bool) or not isinstance(
            self.horizon_sessions,
            int,
        ):
            raise CandidateContractError("horizon_sessions must be an integer")
        if self.horizon_sessions <= 0:
            raise CandidateContractError("horizon_sessions must be positive")
        if len(self.feature_set_id) != 64 or not self.label_version:
            raise CandidateContractError("feature_set_id/label_version are invalid")
        if not self.probability_is_advisory_only:
            raise CandidateContractError(
                "model probability cannot directly control a trade action"
            )

    @property
    def candidate_id(self) -> str:
        return fingerprint({"schema": "candidate-spec-v1", "candidate": self})


@dataclass(frozen=True, slots=True)
class ProbabilityAdvisory:
    probability: float
    calibrated: bool
    calibration_id: str | None
    model_id: str

    def __post_init__(self) -> None:
        _require_probability(self.probability, "probability")
        _require_bool(self.calibrated, "calibrated")
        if not self.model_id:
            raise CandidateContractError("model_id must be non-empty")
        if self.calibrated and (
            self.calibration_id is None or len(self.calibration_id) != 64
        ):
            raise CandidateContractError(
                "calibrated advisory requires calibration_id"
            )


@dataclass(frozen=True, slots=True)
class ActionableDecision:
    actionable: bool
    reasons: tuple[str, ...]
    probability: float


def risk_gated_action(
    advisory: ProbabilityAdvisory,
    *,
    rule_signal_allowed: bool,
    risk_gate_allowed: bool,
    data_quality_allowed: bool,
    minimum_probability: float,
) -> ActionableDecision:
    """Probability is one advisory input; three non-ML gates remain mandatory."""

    for name, value in (
        ("rule_signal_allowed", rule_signal_allowed),
        ("risk_gate_allowed", risk_gate_allowed),
        ("data_quality_allowed", data_quality_allowed),
    ):
        _require_bool(value, name)
    threshold = _require_probability(minimum_probability, "minimum_probability")
    reasons: list[str] = []
    if not advisory.calibrated:
        reasons.append("UNCALIBRATED_PROBABILITY")
    if not rule_signal_allowed:
        reasons.append("RULE_SIGNAL_BLOCKED")
    if not risk_gate_allowed:
        reasons.append("RISK_GATE_BLOCKED")
    if not data_quality_allowed:
        reasons.append("DATA_QUALITY_BLOCKED")
    if advisory.probability < threshold:
        reasons.append("PROBABILITY_BELOW_THRESHOLD")
    return ActionableDecision(
        actionable=not reasons,
        reasons=tuple(reasons),
        probability=advisory.probability,
    )
