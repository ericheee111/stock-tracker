"""Probability, ranking and trading metrics with fail-closed inputs."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


class MetricContractError(ValueError):
    """Raised when labels, probabilities or returns are not valid evidence."""


def binary_labels(values: Iterable[int | float]) -> tuple[int, ...]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MetricContractError("binary labels must be numeric 0 or 1")
        if not math.isfinite(float(value)) or value not in (0, 1):
            raise MetricContractError("binary labels must be exactly 0 or 1")
        result.append(int(value))
    if not result:
        raise MetricContractError("labels cannot be empty")
    return tuple(result)


def probabilities(values: Iterable[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result:
        raise MetricContractError("probabilities cannot be empty")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in result):
        raise MetricContractError("probabilities must be finite values in [0, 1]")
    return result


def _paired(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    labels = binary_labels(y_true)
    probs = probabilities(y_prob)
    if len(labels) != len(probs):
        raise MetricContractError("labels and probabilities must have equal length")
    return labels, probs


def brier_score(y_true: Iterable[int | float], y_prob: Iterable[float]) -> float:
    labels, probs = _paired(y_true, y_prob)
    return sum((prob - label) ** 2 for label, prob in zip(labels, probs)) / len(labels)


def log_loss(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    *,
    epsilon: float = 1e-15,
) -> float:
    labels, probs = _paired(y_true, y_prob)
    if not 0 < epsilon < 0.5:
        raise MetricContractError("epsilon must be in (0, 0.5)")
    total = 0.0
    for label, probability in zip(labels, probs):
        clipped = min(1 - epsilon, max(epsilon, probability))
        total -= label * math.log(clipped) + (1 - label) * math.log(1 - clipped)
    return total / len(labels)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_rate: float


def calibration_curve(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    *,
    bins: int = 10,
) -> tuple[CalibrationBin, ...]:
    labels, probs = _paired(y_true, y_prob)
    if bins <= 0:
        raise MetricContractError("bins must be positive")
    buckets: list[list[tuple[int, float]]] = [[] for _ in range(bins)]
    for label, probability in zip(labels, probs):
        index = min(bins - 1, int(probability * bins))
        buckets[index].append((label, probability))
    result: list[CalibrationBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        result.append(
            CalibrationBin(
                lower=index / bins,
                upper=(index + 1) / bins,
                count=len(bucket),
                mean_probability=sum(item[1] for item in bucket) / len(bucket),
                observed_rate=sum(item[0] for item in bucket) / len(bucket),
            )
        )
    return tuple(result)


def expected_calibration_error(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    *,
    bins: int = 10,
) -> float:
    labels, probs = _paired(y_true, y_prob)
    curve = calibration_curve(labels, probs, bins=bins)
    return sum(
        bucket.count / len(labels)
        * abs(bucket.mean_probability - bucket.observed_rate)
        for bucket in curve
    )


def precision_at_k(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    k: int,
) -> float:
    labels, probs = _paired(y_true, y_prob)
    if not 0 < k <= len(labels):
        raise MetricContractError("k must be in [1, sample_count]")
    ranked = sorted(range(len(labels)), key=lambda index: (-probs[index], index))[:k]
    return sum(labels[index] for index in ranked) / k


def top_k_net_expectancy(
    returns_r: Sequence[float],
    y_prob: Sequence[float],
    k: int,
    *,
    costs_r: Sequence[float] | None = None,
) -> float:
    probs = probabilities(y_prob)
    if len(returns_r) != len(probs):
        raise MetricContractError("returns and probabilities must have equal length")
    costs = tuple(0.0 for _ in returns_r) if costs_r is None else tuple(costs_r)
    if len(costs) != len(returns_r):
        raise MetricContractError("costs and returns must have equal length")
    values = tuple(float(value) for value in returns_r)
    if any(not math.isfinite(value) for value in (*values, *costs)):
        raise MetricContractError("returns and costs must be finite")
    if not 0 < k <= len(values):
        raise MetricContractError("k must be in [1, sample_count]")
    ranked = sorted(range(len(values)), key=lambda index: (-probs[index], index))[:k]
    return sum(values[index] - costs[index] for index in ranked) / k


def profit_factor(returns: Iterable[float]) -> float:
    values = tuple(float(value) for value in returns)
    if not values or any(not math.isfinite(value) for value in values):
        raise MetricContractError("returns must be non-empty and finite")
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    return math.inf if gross_loss == 0 and gross_profit > 0 else (
        0.0 if gross_loss == 0 else gross_profit / gross_loss
    )


def max_drawdown(returns: Iterable[float], initial_equity: float = 1.0) -> float:
    values = tuple(float(value) for value in returns)
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise MetricContractError("initial_equity must be finite and positive")
    if any(not math.isfinite(value) for value in values):
        raise MetricContractError("returns must be finite")
    equity = initial_equity
    peak = initial_equity
    drawdown = 0.0
    for value in values:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return drawdown


@dataclass(frozen=True, slots=True)
class ProbabilityMetrics:
    brier: float
    logloss: float
    ece: float
    precision_at_k: float
    top_k_net_expectancy: float | None = None


def probability_metrics(
    y_true: Sequence[int | float],
    y_prob: Sequence[float],
    *,
    k: int,
    returns_r: Sequence[float] | None = None,
    costs_r: Sequence[float] | None = None,
) -> ProbabilityMetrics:
    return ProbabilityMetrics(
        brier=brier_score(y_true, y_prob),
        logloss=log_loss(y_true, y_prob),
        ece=expected_calibration_error(y_true, y_prob),
        precision_at_k=precision_at_k(y_true, y_prob, k),
        top_k_net_expectancy=(
            top_k_net_expectancy(returns_r, y_prob, k, costs_r=costs_r)
            if returns_r is not None
            else None
        ),
    )
