"""Causal, dependency-light Alpha158-style price/volume feature set.

This module intentionally does not claim numerical equivalence with Microsoft
Qlib.  It freezes 158 transparent, causal features and replaces Qlib's default
next-period-return label with the project's execution-aware labels elsewhere.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .context import FeatureContext
from .metadata import (
    FeatureDefinition,
    FeatureFamily,
    FeatureSetDefinition,
)

_EPSILON = 1e-12
_WINDOWS = (5, 10, 20, 30, 60)
_WINDOW_FEATURES = (
    "roc",
    "ma_ratio",
    "std",
    "beta",
    "rsqr",
    "residual",
    "max_ratio",
    "min_ratio",
    "qtlu",
    "qtld",
    "rank",
    "rsv",
    "imax",
    "imin",
    "imxd",
    "corr_pv",
    "corr_pr",
    "cntp",
    "cntn",
    "cntd",
    "sump",
    "sumn",
    "sumd",
    "vma",
    "vstd",
    "wvma",
    "vsump",
    "vsumd",
    "turnover_mean",
)
_KBAR_NAMES = (
    "kmid",
    "klen",
    "kmid2",
    "kup",
    "kup2",
    "klow",
    "klow2",
    "ksft",
    "ksft2",
)
_EXTRA_NAMES = ("volume_z20", "amount_z20", "turnover_z20", "close_position_60")


class FeatureComputationError(ValueError):
    """Raised when the requested causal lookback is unavailable or invalid."""


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _std(values: Sequence[float]) -> float:
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return 0.0 if denominator <= _EPSILON else numerator / denominator


def _regression(values: Sequence[float]) -> tuple[float, float, float]:
    x_values = tuple(float(index) for index in range(len(values)))
    x_mean = _mean(x_values)
    y_mean = _mean(values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    slope = 0.0 if denominator <= _EPSILON else sum(
        (x - x_mean) * (y - y_mean) for x, y in zip(x_values, values)
    ) / denominator
    intercept = y_mean - slope * x_mean
    fitted = tuple(intercept + slope * x for x in x_values)
    total = sum((value - y_mean) ** 2 for value in values)
    residual = sum((value - estimate) ** 2 for value, estimate in zip(values, fitted))
    rsqr = 0.0 if total <= _EPSILON else max(0.0, 1 - residual / total)
    return slope, rsqr, values[-1] - fitted[-1]


def _changes(values: Sequence[float]) -> tuple[float, ...]:
    return tuple(
        0.0 if abs(previous) <= _EPSILON else current / previous - 1
        for previous, current in pairwise(values)
    )


def _zscore_last(values: Sequence[float]) -> float:
    deviation = _std(values)
    return 0.0 if deviation <= _EPSILON else (values[-1] - _mean(values)) / deviation


def feature_names() -> tuple[str, ...]:
    names = list(_KBAR_NAMES)
    for window in _WINDOWS:
        names.extend(f"{name}_{window}" for name in _WINDOW_FEATURES)
    names.extend(_EXTRA_NAMES)
    result = tuple(names)
    if len(result) != 158 or len(set(result)) != 158:
        raise AssertionError("Alpha158-style feature inventory must be exactly 158")
    return result


def _family(name: str) -> FeatureFamily:
    if name.startswith(("corr", "cnt", "sum", "roc", "rank", "rsv")):
        return FeatureFamily.MOMENTUM
    if name.startswith(("std", "klen", "kup", "klow", "ksft")):
        return FeatureFamily.VOLATILITY
    if name.startswith(("v", "turnover", "amount")):
        return FeatureFamily.VOLUME_LIQUIDITY
    if name.startswith(("ma", "beta", "rsqr", "residual", "imax", "imin", "imxd")):
        return FeatureFamily.TREND
    return FeatureFamily.PRICE_STRUCTURE


def alpha158_style_definition() -> FeatureSetDefinition:
    definitions = []
    for name in feature_names():
        suffix = name.rsplit("_", 1)[-1]
        lookback = int(suffix) if suffix.isdigit() else (20 if "20" in name else 60)
        definitions.append(
            FeatureDefinition(
                name=name,
                family=_family(name),
                lookback_sessions=lookback,
                causal=True,
                description=f"Causal Alpha158-style feature {name}",
                formula_version="stock-tracker-alpha158-style-v1",
            )
        )
    return FeatureSetDefinition(
        name="stock-tracker-alpha158-style",
        version="1.0.0",
        features=tuple(definitions),
        qlib_revision=None,
        numerically_equivalent_to_qlib=False,
    )


@dataclass(frozen=True, slots=True)
class FeatureVector:
    names: tuple[str, ...]
    values: tuple[float, ...]
    context_id: str
    feature_set_id: str

    def __post_init__(self) -> None:
        if len(self.names) != 158 or len(self.values) != 158:
            raise FeatureComputationError("feature vector must contain exactly 158 values")
        if any(not math.isfinite(value) for value in self.values):
            raise FeatureComputationError("feature values must be finite")

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


class Alpha158Style:
    """Compute a frozen 158-feature vector using only bars at/before ``as_of``."""

    definition = alpha158_style_definition()

    def compute(self, context: FeatureContext) -> FeatureVector:
        if len(context.bars) < max(_WINDOWS):
            raise FeatureComputationError("Alpha158-style features require 60 sessions")
        bars = context.bars
        opens = tuple(float(bar.open) for bar in bars)
        highs = tuple(float(bar.high) for bar in bars)
        lows = tuple(float(bar.low) for bar in bars)
        closes = tuple(float(bar.close) for bar in bars)
        volumes = tuple(float(bar.volume) for bar in bars)
        amounts = tuple(float(bar.amount) for bar in bars)
        turnovers = tuple(float(bar.turnover) for bar in bars)
        if any(
            not math.isfinite(value)
            for series in (opens, highs, lows, closes, volumes, amounts, turnovers)
            for value in series
        ):
            raise FeatureComputationError("input bar values must be finite")
        if any(value <= 0 for value in (*opens, *highs, *lows, *closes)):
            raise FeatureComputationError("OHLC inputs must be positive")
        if any(value < 0 for value in (*volumes, *amounts, *turnovers)):
            raise FeatureComputationError("volume/amount/turnover cannot be negative")

        open_now, high_now, low_now, close_now = (
            opens[-1],
            highs[-1],
            lows[-1],
            closes[-1],
        )
        span = max(high_now - low_now, _EPSILON)
        values: list[float] = [
            (close_now - open_now) / open_now,
            (high_now - low_now) / open_now,
            (close_now - open_now) / span,
            (high_now - max(open_now, close_now)) / open_now,
            (high_now - max(open_now, close_now)) / span,
            (min(open_now, close_now) - low_now) / open_now,
            (min(open_now, close_now) - low_now) / span,
            (2 * close_now - high_now - low_now) / open_now,
            (2 * close_now - high_now - low_now) / span,
        ]

        for window in _WINDOWS:
            price = closes[-window:]
            volume = volumes[-window:]
            turnover = turnovers[-window:]
            returns = _changes(price)
            volume_changes = _changes(tuple(value + 1.0 for value in volume))
            price_mean = _mean(price)
            price_std = _std(price)
            normalized = tuple(value / price[0] - 1 for value in price)
            beta, rsqr, residual = _regression(normalized)
            high_value = max(price)
            low_value = min(price)
            q75 = _quantile(price, 0.75)
            q25 = _quantile(price, 0.25)
            imax = max(range(window), key=lambda index: price[index]) / max(1, window - 1)
            imin = min(range(window), key=lambda index: price[index]) / max(1, window - 1)
            positive = tuple(value for value in returns if value > 0)
            negative = tuple(-value for value in returns if value < 0)
            absolute_sum = sum(abs(value) for value in returns)
            positive_volume = sum(value for value in volume_changes if value > 0)
            negative_volume = -sum(value for value in volume_changes if value < 0)
            volume_move = positive_volume + negative_volume
            volume_mean = _mean(volume)
            weighted_price = (
                sum(p * v for p, v in zip(price, volume)) / sum(volume)
                if sum(volume) > _EPSILON
                else price_mean
            )
            values.extend(
                (
                    close_now / price[0] - 1,
                    close_now / price_mean - 1,
                    price_std / max(abs(price_mean), _EPSILON),
                    beta,
                    rsqr,
                    residual,
                    close_now / high_value - 1,
                    close_now / low_value - 1,
                    close_now / q75 - 1,
                    close_now / q25 - 1,
                    sum(value <= close_now for value in price) / window,
                    (close_now - low_value) / max(high_value - low_value, _EPSILON),
                    imax,
                    imin,
                    imax - imin,
                    _correlation(price, volume),
                    _correlation(returns, volume_changes),
                    len(positive) / max(1, len(returns)),
                    len(negative) / max(1, len(returns)),
                    (len(positive) - len(negative)) / max(1, len(returns)),
                    sum(positive) / max(absolute_sum, _EPSILON),
                    sum(negative) / max(absolute_sum, _EPSILON),
                    (sum(positive) - sum(negative)) / max(absolute_sum, _EPSILON),
                    volume[-1] / max(volume_mean, _EPSILON) - 1,
                    _std(volume) / max(abs(volume_mean), _EPSILON),
                    weighted_price / price_mean - 1,
                    positive_volume / max(volume_move, _EPSILON),
                    (positive_volume - negative_volume) / max(volume_move, _EPSILON),
                    _mean(turnover),
                )
            )

        values.extend(
            (
                _zscore_last(volumes[-20:]),
                _zscore_last(amounts[-20:]),
                _zscore_last(turnovers[-20:]),
                (close_now - min(closes[-60:]))
                / max(max(closes[-60:]) - min(closes[-60:]), _EPSILON),
            )
        )
        names = feature_names()
        if len(values) != len(names):
            raise AssertionError(f"feature count mismatch: {len(values)} != {len(names)}")
        return FeatureVector(
            names=names,
            values=tuple(values),
            context_id=context.context_id,
            feature_set_id=self.definition.feature_set_id,
        )
