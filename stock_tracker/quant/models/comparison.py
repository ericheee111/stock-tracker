"""Champion/challenger comparison identity and strict promotion gates."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..core.fingerprint import fingerprint
from ..evaluation.metrics import ProbabilityMetrics

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ComparisonContractError(ValueError):
    """Raised when evidence is not comparable or contains invalid metrics."""


@dataclass(frozen=True, slots=True)
class ComparisonIdentity:
    train_dataset_id: str
    calibration_dataset_id: str
    validation_dataset_id: str
    feature_set_version: str
    label_version: str
    market_rule_hash: str
    cost_schedule_hash: str
    calibration_definition: str
    calibration_window: str
    top_k: int
    top_k_definition: str
    random_seed: int

    def __post_init__(self) -> None:
        hashes = (
            self.train_dataset_id,
            self.calibration_dataset_id,
            self.validation_dataset_id,
            self.market_rule_hash,
            self.cost_schedule_hash,
        )
        if any(not _SHA256.fullmatch(value) for value in hashes):
            raise ComparisonContractError("dataset/rule/cost identities must be SHA-256")
        strings = (
            self.feature_set_version,
            self.label_version,
            self.calibration_definition,
            self.calibration_window,
            self.top_k_definition,
        )
        if any(not value for value in strings):
            raise ComparisonContractError("comparison version/definition fields are required")
        if self.top_k <= 0 or self.random_seed < 0:
            raise ComparisonContractError("top_k must be positive and seed non-negative")

    @property
    def comparison_id(self) -> str:
        return fingerprint({"schema": "fair-comparison-v1", "identity": self})


@dataclass(frozen=True, slots=True)
class ModelEvaluation:
    model_id: str
    comparison_id: str
    metrics: ProbabilityMetrics
    score_bucket_rates: tuple[float, ...]
    regime_expectancies: tuple[float, ...]
    time_expectancies: tuple[float, ...]
    max_drawdown: float

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ComparisonContractError("model_id must be non-empty")
        if not _SHA256.fullmatch(self.comparison_id):
            raise ComparisonContractError("comparison_id must be SHA-256")

    def invalid_fields(self) -> tuple[str, ...]:
        values = {
            "brier": self.metrics.brier,
            "logloss": self.metrics.logloss,
            "ece": self.metrics.ece,
            "precision_at_k": self.metrics.precision_at_k,
            "top_k_net_expectancy": self.metrics.top_k_net_expectancy,
            "max_drawdown": self.max_drawdown,
        }
        invalid = [
            name
            for name, value in values.items()
            if value is None or not math.isfinite(float(value))
        ]
        for name, sequence in (
            ("score_bucket_rates", self.score_bucket_rates),
            ("regime_expectancies", self.regime_expectancies),
            ("time_expectancies", self.time_expectancies),
        ):
            if not sequence or any(not math.isfinite(value) for value in sequence):
                invalid.append(name)
        if not 0 <= self.metrics.brier <= 1:
            invalid.append("brier_range")
        if self.metrics.logloss < 0 or self.metrics.ece < 0:
            invalid.append("loss_range")
        if not 0 <= self.metrics.precision_at_k <= 1:
            invalid.append("precision_range")
        if not 0 <= self.max_drawdown <= 1:
            invalid.append("drawdown_range")
        return tuple(sorted(set(invalid)))


@dataclass(frozen=True, slots=True)
class ChampionGateConfig:
    minimum_brier_improvement: float = 0.0
    minimum_logloss_improvement: float = 0.0
    maximum_ece_regression: float = 0.0
    maximum_precision_regression: float = 0.0
    minimum_expectancy_improvement: float = 0.0
    maximum_drawdown_regression: float = 0.0
    monotonic_tolerance: float = 0.0
    maximum_regime_range: float = 0.5
    maximum_time_range: float = 0.5

    def __post_init__(self) -> None:
        values = (
            self.minimum_brier_improvement,
            self.minimum_logloss_improvement,
            self.maximum_ece_regression,
            self.maximum_precision_regression,
            self.minimum_expectancy_improvement,
            self.maximum_drawdown_regression,
            self.monotonic_tolerance,
            self.maximum_regime_range,
            self.maximum_time_range,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ComparisonContractError("promotion thresholds must be finite/non-negative")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promoted: bool
    reasons: tuple[str, ...]
    champion_model_id: str
    challenger_model_id: str
    comparison_id: str
    decision_id: str


class ChampionGate:
    """Promote only when probability quality, ranking and stability all pass."""

    def __init__(self, config: ChampionGateConfig | None = None) -> None:
        self.config = config or ChampionGateConfig()

    def evaluate(
        self,
        champion: ModelEvaluation,
        challenger: ModelEvaluation,
    ) -> PromotionDecision:
        reasons: list[str] = []
        if champion.comparison_id != challenger.comparison_id:
            reasons.append("COMPARISON_IDENTITY_MISMATCH")
        champion_invalid = champion.invalid_fields()
        challenger_invalid = challenger.invalid_fields()
        if champion_invalid:
            reasons.append("INVALID_CHAMPION_METRICS")
        if challenger_invalid:
            reasons.append("INVALID_CHALLENGER_METRICS")
        if not reasons:
            config = self.config
            if not (
                challenger.metrics.brier
                < champion.metrics.brier - config.minimum_brier_improvement
            ):
                reasons.append("BRIER_NOT_IMPROVED")
            if not (
                challenger.metrics.logloss
                < champion.metrics.logloss - config.minimum_logloss_improvement
            ):
                reasons.append("LOGLOSS_NOT_IMPROVED")
            if (
                challenger.metrics.ece
                > champion.metrics.ece + config.maximum_ece_regression
            ):
                reasons.append("ECE_REGRESSED")
            if (
                challenger.metrics.precision_at_k
                < champion.metrics.precision_at_k
                - config.maximum_precision_regression
            ):
                reasons.append("PRECISION_AT_K_REGRESSED")
            assert challenger.metrics.top_k_net_expectancy is not None
            assert champion.metrics.top_k_net_expectancy is not None
            if not (
                challenger.metrics.top_k_net_expectancy
                > champion.metrics.top_k_net_expectancy
                + config.minimum_expectancy_improvement
            ):
                reasons.append("EXPECTANCY_NOT_IMPROVED")
            if challenger.max_drawdown > (
                champion.max_drawdown + config.maximum_drawdown_regression
            ):
                reasons.append("MAX_DRAWDOWN_REGRESSED")
            if not _non_decreasing(
                challenger.score_bucket_rates,
                tolerance=config.monotonic_tolerance,
            ):
                reasons.append("SCORE_BUCKET_NOT_MONOTONIC")
            if _range(challenger.regime_expectancies) > config.maximum_regime_range:
                reasons.append("REGIME_INSTABILITY")
            if _range(challenger.time_expectancies) > config.maximum_time_range:
                reasons.append("TIME_INSTABILITY")
        promoted = not reasons
        comparison_id = (
            champion.comparison_id
            if champion.comparison_id == challenger.comparison_id
            else fingerprint(
                {
                    "champion_comparison_id": champion.comparison_id,
                    "challenger_comparison_id": challenger.comparison_id,
                }
            )
        )
        decision_payload = {
            "schema": "promotion-decision-v1",
            "config": self.config,
            "promoted": promoted,
            "reasons": reasons,
            "champion_model_id": champion.model_id,
            "challenger_model_id": challenger.model_id,
            "comparison_id": comparison_id,
        }
        return PromotionDecision(
            promoted=promoted,
            reasons=tuple(reasons),
            champion_model_id=champion.model_id,
            challenger_model_id=challenger.model_id,
            comparison_id=comparison_id,
            decision_id=fingerprint(decision_payload),
        )


def _range(values: Sequence[float]) -> float:
    return max(values) - min(values)


def _non_decreasing(values: Sequence[float], *, tolerance: float) -> bool:
    return all(
        current + tolerance >= previous
        for previous, current in pairwise(values)
    )
