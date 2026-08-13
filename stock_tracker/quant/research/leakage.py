"""Adversarial leakage checks and negative controls."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..core.fingerprint import fingerprint
from ..core.time import to_utc
from ..evaluation.metrics import binary_labels, brier_score, probabilities


class LeakageContractError(ValueError):
    """Raised when future availability or control evidence is invalid."""


@dataclass(frozen=True, slots=True)
class FeatureAvailability:
    sample_id: str
    signal_time: datetime
    feature_known_at: datetime
    feature_usable_from: datetime

    def __post_init__(self) -> None:
        signal = to_utc(self.signal_time, "signal_time")
        known = to_utc(self.feature_known_at, "feature_known_at")
        usable = to_utc(self.feature_usable_from, "feature_usable_from")
        if usable < known:
            raise LeakageContractError("feature usable_from cannot precede known_at")
        if not self.sample_id:
            raise LeakageContractError("sample_id must be non-empty")
        if known > signal or usable > signal:
            raise LeakageContractError(
                f"future feature entered sample {self.sample_id}"
            )


@dataclass(frozen=True, slots=True)
class NegativeControlResult:
    baseline_brier: float
    random_feature_brier: float
    randomized_label_brier: float
    future_feature_brier: float
    future_feature_flagged: bool
    suspicious_advantage_detected: bool

    @property
    def evidence_id(self) -> str:
        return fingerprint({"schema": "negative-controls-v1", "result": self})


def randomized_labels(values: Iterable[int | float], *, seed: int) -> tuple[int, ...]:
    labels = list(binary_labels(values))
    random.Random(seed).shuffle(labels)
    return tuple(labels)


def random_feature_probabilities(count: int, *, seed: int) -> tuple[float, ...]:
    if count <= 0:
        raise LeakageContractError("count must be positive")
    generator = random.Random(seed)
    return tuple(generator.random() for _ in range(count))


def assess_negative_controls(
    *,
    y_true: Sequence[int | float],
    baseline_probabilities: Sequence[float],
    future_feature_probabilities: Sequence[float],
    seed: int = 20260813,
    suspicious_brier_threshold: float = 0.01,
    suspicious_advantage: float = 0.1,
) -> NegativeControlResult:
    labels = binary_labels(y_true)
    baseline = probabilities(baseline_probabilities)
    future = probabilities(future_feature_probabilities)
    if len(labels) != len(baseline) or len(labels) != len(future):
        raise LeakageContractError("negative-control arrays must have equal length")
    random_probs = random_feature_probabilities(len(labels), seed=seed)
    shuffled = randomized_labels(labels, seed=seed + 1)
    prevalence = sum(shuffled) / len(shuffled)
    randomized_probs = tuple(prevalence for _ in shuffled)
    baseline_brier = brier_score(labels, baseline)
    random_brier = brier_score(labels, random_probs)
    randomized_brier = brier_score(shuffled, randomized_probs)
    future_brier = brier_score(labels, future)
    flagged = future_brier < suspicious_brier_threshold
    advantage = baseline_brier - future_brier
    suspicious = flagged or advantage > suspicious_advantage
    return NegativeControlResult(
        baseline_brier=baseline_brier,
        random_feature_brier=random_brier,
        randomized_label_brier=randomized_brier,
        future_feature_brier=future_brier,
        future_feature_flagged=flagged,
        suspicious_advantage_detected=suspicious,
    )


def assert_calibration_boundary(
    signal_times: Sequence[datetime],
    label_end_times: Sequence[datetime],
    cutoff: datetime,
) -> None:
    if len(signal_times) != len(label_end_times):
        raise LeakageContractError("signal/label-end arrays must have equal length")
    boundary = to_utc(cutoff, "cutoff")
    for signal, label_end in zip(signal_times, label_end_times):
        if to_utc(signal) <= boundary < to_utc(label_end):
            raise LeakageContractError(
                "calibration boundary includes a signal whose label is unfinished"
            )


def suspicious_metric(value: float, *, name: str) -> None:
    if not math.isfinite(value):
        raise LeakageContractError(f"{name} must be finite")
