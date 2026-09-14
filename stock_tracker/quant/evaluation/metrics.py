"""Probability, ranking and trading metrics with fail-closed inputs."""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import cast


class MetricContractError(ValueError):
    """Raised when labels, probabilities or returns are not valid evidence."""


METRIC_INPUT_POLICY_ID = "quant-metric-input-v3-ordered-finite-underflow"
MAX_CALIBRATION_BINS = 10000


def _finite_number(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise MetricContractError(f"{name} must be an explicit built-in int or float")
    try:
        number = float(cast(float, value))
    except OverflowError as exc:
        raise MetricContractError(f"{name} is outside finite float range") from exc
    if not math.isfinite(number):
        raise MetricContractError(f"{name} must be finite")
    return number


def _numbers(values: Iterable[int | float], name: str) -> tuple[float, ...]:
    """Published ordered/nonempty input contract, including generator support."""
    if isinstance(values, (str, bytes, bytearray, Mapping, AbstractSet)):
        raise MetricContractError(f"{name} must be an ordered numeric iterable")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise MetricContractError(f"{name} must be an ordered numeric iterable") from exc
    result = tuple(_finite_number(value, name) for value in iterator)
    if not result:
        raise MetricContractError(f"{name} cannot be empty")
    return result


def _finite_result(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise MetricContractError(f"{name} arithmetic is outside finite float range")
    return value


def _finite_quotient(numerator: float, denominator: float, name: str) -> float:
    result = _finite_result(numerator / denominator, name)
    if numerator != 0 and result == 0:
        raise MetricContractError(f"{name} nonzero quotient underflowed to zero")
    return result


def _finite_product(left: float, right: float, name: str) -> float:
    result = _finite_result(left * right, name)
    if left != 0 and right != 0 and result == 0:
        raise MetricContractError(f"{name} nonzero product underflowed to zero")
    return result


def _rank_count(k: int, count: int) -> None:
    if type(k) is not int or not 0 < k <= count:
        raise MetricContractError("k must be an integer in [1, sample_count]")


def binary_labels(values: Iterable[int | float]) -> tuple[int, ...]:
    result: list[int] = []
    for value in _numbers(values, "binary labels"):
        number = _finite_number(value, "binary label")
        if number not in (0, 1):
            raise MetricContractError("binary labels must be exactly 0 or 1")
        result.append(int(number))
    if not result:
        raise MetricContractError("labels cannot be empty")
    return tuple(result)


def probabilities(values: Iterable[float]) -> tuple[float, ...]:
    result = _numbers(values, "probability")
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
    # Keep summation order, but a nonzero error cannot disappear by underflow.
    squared = []
    for label, prob in zip(labels, probs):
        error = prob - label
        term = _finite_result(error ** 2, "Brier squared error")
        if error != 0 and term == 0:
            raise MetricContractError("nonzero Brier squared error underflowed to zero")
        squared.append(term)
    return _finite_quotient(sum(squared), len(labels), "Brier mean")


def log_loss(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    *,
    epsilon: float = 1e-15,
) -> float:
    labels, probs = _paired(y_true, y_prob)
    epsilon = _finite_number(epsilon, "epsilon")
    if not 0 < epsilon < 0.5 or 1.0 - epsilon == 1.0:
        raise MetricContractError("epsilon must be in (0, 0.5) and representably below one after clipping")
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
        raise MetricContractError(f"bins must be an integer in [1, {MAX_CALIBRATION_BINS}]")
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
                mean_probability=_finite_quotient(sum(prob for _, prob in bucket), len(bucket), "calibration mean"),
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
        _finite_product(bucket.count / len(labels),
                        abs(bucket.mean_probability - bucket.observed_rate), "weighted calibration error")
        for bucket in curve
    )


def precision_at_k(
    y_true: Iterable[int | float],
    y_prob: Iterable[float],
    k: int,
) -> float:
    labels, probs = _paired(y_true, y_prob)
    _rank_count(k, len(labels))
    ranked = sorted(range(len(labels)), key=lambda index: (-probs[index], index))[:k]
    return sum(labels[index] for index in ranked) / k


def top_k_net_expectancy(
    returns_r: Sequence[float],
    y_prob: Sequence[float],
    k: int,
    *,
    costs_r: Sequence[float] | None = None,
) -> float:
    # Returns/costs are R multiples, not fractional equity returns. None costs
    # retain the legacy zero-cost diagnostic convention, not a cost-model claim.
    probs = probabilities(y_prob)
    values = _numbers(returns_r, "return R")
    if len(values) != len(probs):
        raise MetricContractError("returns and probabilities must have equal length")
    costs = tuple(0.0 for _ in values) if costs_r is None else _numbers(costs_r, "cost R")
    if len(costs) != len(values):
        raise MetricContractError("costs and returns must have equal length")
    if any(value < 0 for value in costs):
        raise MetricContractError("costs must be nonnegative; rebates require a separate model")
    _rank_count(k, len(values))
    ranked = sorted(range(len(values)), key=lambda index: (-probs[index], index))[:k]
    numerator = _finite_result(sum(values[index] - costs[index] for index in ranked), "net expectancy sum")
    return _finite_quotient(numerator, k, "net expectancy")



def profit_factor(returns: Iterable[float]) -> float:
    """Ratio of positive to negative P&L; no-loss infinity is mathematical only.

    Callers exposing JSON/UI must translate no-loss infinity to an unavailable
    metric with sample context, never a finite or authenticated performance claim.
    """
    values = _numbers(returns, "P&L")
    if not values:
        raise MetricContractError("returns must be non-empty and finite")
    gross_profit = _finite_result(sum(value for value in values if value > 0), "gross profit")
    gross_loss = _finite_result(-sum(value for value in values if value < 0), "gross loss")
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return _finite_quotient(gross_profit, gross_loss, "profit factor")


def max_drawdown(returns: Iterable[float], initial_equity: float = 1.0) -> float:
    """Peak-to-trough fraction from unlevered periodic fractional equity returns.

    A -1 return exhausts capital; it cannot resurrect. Empty input is rejected,
    preserving the stricter already-published nonempty sample contract.
    Positive equity must remain in the normal float range; subnormal scales
    are rejected rather than allowing rounding to hide loss.
    """
    values = _numbers(returns, "fractional return")
    initial_equity = _finite_number(initial_equity, "initial equity")
    if initial_equity < sys.float_info.min:
        raise MetricContractError("initial_equity must be a positive normal float; subnormal scales are unsupported")
    if any(value < -1 for value in values):
        raise MetricContractError("fractional returns cannot be below -1; R multiples need another metric")
    equity = peak = initial_equity
    drawdown = 0.0
    for value in values:
        previous = equity
        equity = _finite_result(equity * (1 + value), "equity")
        if previous > 0 and value > -1 and equity < sys.float_info.min:
            raise MetricContractError("subnormal equity rounding cannot be treated as a reliable drawdown")
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
    if costs_r is not None and returns_r is None:
        raise MetricContractError("costs require matching returns")
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
