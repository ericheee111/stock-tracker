"""Probability, ranking and trading metrics with fail-closed inputs."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass


class MetricContractError(ValueError):
    """Raised when labels, probabilities or returns are not valid evidence."""


METRIC_INPUT_POLICY_ID = "quant-metric-input-v2-exact-finite"
MAX_CALIBRATION_BINS = 10_000


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        raise MetricContractError(f"{name} must be an explicit built-in int or float")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetricContractError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise MetricContractError(f"{name} must be finite")
    return number


def _numbers(values: Iterable[int | float], name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping, AbstractSet)):
        raise MetricContractError(f"{name} must be an ordered numeric iterable")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise MetricContractError(f"{name} must be an ordered numeric iterable") from exc
    result = tuple(_number(value, name) for value in iterator)
    if not result:
        raise MetricContractError(f"{name} cannot be empty")
    return result


def _result(value: float) -> float:
    if not math.isfinite(value):
        raise MetricContractError("metric arithmetic overflow; no finite result")
    return value


def _k(k: int, count: int) -> None:
    if type(k) is not int or not 0 < k <= count:
        raise MetricContractError("k must be an exact integer in [1, sample_count]")


def binary_labels(values: Iterable[int | float]) -> tuple[int, ...]:
    numbers = _numbers(values, "binary labels")
    if any(value not in (0, 1) for value in numbers):
        raise MetricContractError("binary labels must be exactly 0 or 1")
    return tuple(int(value) for value in numbers)


def probabilities(values: Iterable[float]) -> tuple[float, ...]:
    result = _numbers(values, "probabilities")
    if any(not 0 <= value <= 1 for value in result):
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
    epsilon = _number(epsilon, "epsilon")
    if not 0 < epsilon < 0.5 or not 0 < 1.0 - epsilon < 1:
        raise MetricContractError("epsilon must represent an interior float in (0, 0.5)")
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
    if type(bins) is not int or not 0 < bins <= MAX_CALIBRATION_BINS:
        raise MetricContractError("bins must be an exact integer in [1, 10000]")
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
    _k(k, len(labels))
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
    values = _numbers(returns_r, "returns in R")
    if len(values) != len(probs):
        raise MetricContractError("returns and probabilities must have equal length")
    costs = tuple(0.0 for _ in values) if costs_r is None else _numbers(costs_r, "costs in R")
    if len(costs) != len(values) or any(cost < 0 for cost in costs):
        raise MetricContractError("costs must be nonnegative and match returns; rebates need a separate contract")
    _k(k, len(values))
    ranked = sorted(range(len(values)), key=lambda index: (-probs[index], index))[:k]
    return _result(sum(_result(values[index] - costs[index]) for index in ranked) / k)


def profit_factor(returns: Iterable[float]) -> float:
    """Gross profits/losses. Legacy all-winner infinity is not JSON/performance evidence."""
    values = _numbers(returns, "returns")
    gross_profit = _result(sum(value for value in values if value > 0))
    gross_loss = _result(-sum(value for value in values if value < 0))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return _result(gross_profit / gross_loss)


def max_drawdown(returns: Iterable[float], initial_equity: float = 1.0) -> float:
    """Compounded simple-return drawdown, not R multiples; no cash flows or leverage."""
    values = _numbers(returns, "simple returns")
    equity = _number(initial_equity, "initial equity")
    if equity <= 0 or any(value < -1 for value in values):
        raise MetricContractError("positive equity and simple returns >= -1 required")
    peak = equity
    drawdown = 0.0
    for value in values:
        equity = _result(equity * (1 + value))
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return _result(drawdown)


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
    if returns_r is None and costs_r is not None:
        raise MetricContractError("costs cannot be supplied without matching returns")
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
