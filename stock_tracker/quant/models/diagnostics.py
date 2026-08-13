"""Feature correlation, permutation and ablation diagnostics."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from ..evaluation.metrics import brier_score


class DiagnosticContractError(ValueError):
    """Raised when diagnostic matrices or model outputs are invalid."""


@dataclass(frozen=True, slots=True)
class FeatureImportance:
    feature_name: str
    baseline_loss: float
    permuted_loss: float
    importance: float


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise DiagnosticContractError("correlation vectors must have equal length >= 2")
    if any(not math.isfinite(value) for value in (*left, *right)):
        raise DiagnosticContractError("correlation vectors must be finite")
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return 0.0 if denominator <= 1e-12 else numerator / denominator


def highly_correlated_pairs(
    rows: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    *,
    threshold: float = 0.95,
) -> tuple[tuple[str, str, float], ...]:
    if not 0 <= threshold <= 1:
        raise DiagnosticContractError("threshold must be in [0, 1]")
    if not rows or len(rows[0]) != len(feature_names):
        raise DiagnosticContractError("matrix and feature_names are incompatible")
    columns = tuple(tuple(row[index] for row in rows) for index in range(len(feature_names)))
    result: list[tuple[str, str, float]] = []
    for left, right in combinations(range(len(columns)), 2):
        value = _correlation(columns[left], columns[right])
        if abs(value) >= threshold:
            result.append((feature_names[left], feature_names[right], value))
    return tuple(sorted(result, key=lambda item: (-abs(item[2]), item[0], item[1])))


def permutation_importance(
    *,
    rows: Sequence[Sequence[float]],
    targets: Sequence[int],
    feature_names: Sequence[str],
    predict: Callable[[Sequence[Sequence[float]]], Sequence[float]],
    seed: int = 20260813,
) -> tuple[FeatureImportance, ...]:
    if not rows or len(rows) != len(targets):
        raise DiagnosticContractError("rows/targets must be non-empty and equal")
    width = len(feature_names)
    if any(len(row) != width for row in rows):
        raise DiagnosticContractError("row width differs from feature_names")
    baseline = brier_score(targets, predict(rows))
    result: list[FeatureImportance] = []
    for column, name in enumerate(feature_names):
        generator = random.Random(seed + column)
        shuffled = [row[column] for row in rows]
        generator.shuffle(shuffled)
        permuted = [list(row) for row in rows]
        for index, value in enumerate(shuffled):
            permuted[index][column] = value
        loss = brier_score(targets, predict(permuted))
        result.append(
            FeatureImportance(
                feature_name=name,
                baseline_loss=baseline,
                permuted_loss=loss,
                importance=loss - baseline,
            )
        )
    return tuple(sorted(result, key=lambda item: (-item.importance, item.feature_name)))


def ablation_indices(
    feature_names: Sequence[str],
    families: Mapping[str, Sequence[str]],
) -> dict[str, tuple[int, ...]]:
    index = {name: position for position, name in enumerate(feature_names)}
    output: dict[str, tuple[int, ...]] = {}
    for family, members in families.items():
        missing = [name for name in members if name not in index]
        if missing:
            raise DiagnosticContractError(
                f"unknown features in family {family}: {', '.join(missing)}"
            )
        removed = set(members)
        output[family] = tuple(
            position for position, name in enumerate(feature_names) if name not in removed
        )
    return output
