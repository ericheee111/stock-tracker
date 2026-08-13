"""Train-only and point-in-time cross-sectional normalization."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..core.fingerprint import fingerprint


class NormalizationContractError(ValueError):
    """Raised when normalization has not been fitted on an identified train set."""


@dataclass(slots=True)
class TrainOnlyStandardizer:
    means: tuple[float, ...] = ()
    scales: tuple[float, ...] = ()
    lower_bounds: tuple[float, ...] = ()
    upper_bounds: tuple[float, ...] = ()
    training_dataset_id: str | None = None
    fitted: bool = False

    def fit(
        self,
        rows: Sequence[Sequence[float]],
        *,
        training_dataset_id: str,
        winsor_quantile: float = 0.01,
    ) -> TrainOnlyStandardizer:
        if len(training_dataset_id) != 64:
            raise NormalizationContractError("training_dataset_id must be SHA-256")
        if not rows or not rows[0]:
            raise NormalizationContractError("training rows cannot be empty")
        if not 0 <= winsor_quantile < 0.5:
            raise NormalizationContractError("winsor_quantile must be in [0, 0.5)")
        width = len(rows[0])
        values = tuple(tuple(float(item) for item in row) for row in rows)
        if any(len(row) != width for row in values):
            raise NormalizationContractError("training rows must have equal width")
        if any(not math.isfinite(item) for row in values for item in row):
            raise NormalizationContractError("training values must be finite")
        lowers: list[float] = []
        uppers: list[float] = []
        clipped_columns: list[tuple[float, ...]] = []
        for column in range(width):
            ordered = sorted(row[column] for row in values)
            lower_index = min(len(ordered) - 1, int(winsor_quantile * len(ordered)))
            upper_index = max(
                0,
                len(ordered) - 1 - int(winsor_quantile * len(ordered)),
            )
            lower = ordered[lower_index]
            upper = ordered[upper_index]
            lowers.append(lower)
            uppers.append(upper)
            clipped_columns.append(
                tuple(min(upper, max(lower, row[column])) for row in values)
            )
        means = tuple(sum(column) / len(column) for column in clipped_columns)
        scales: list[float] = []
        for column, mean in zip(clipped_columns, means):
            variance = sum((value - mean) ** 2 for value in column) / len(column)
            scale = math.sqrt(variance)
            scales.append(scale if scale > 1e-12 else 1.0)
        self.means = means
        self.scales = tuple(scales)
        self.lower_bounds = tuple(lowers)
        self.upper_bounds = tuple(uppers)
        self.training_dataset_id = training_dataset_id
        self.fitted = True
        return self

    def transform(
        self,
        rows: Sequence[Sequence[float]],
    ) -> tuple[tuple[float, ...], ...]:
        if not self.fitted:
            raise NormalizationContractError("standardizer is not fitted")
        width = len(self.means)
        output: list[tuple[float, ...]] = []
        for row in rows:
            if len(row) != width:
                raise NormalizationContractError("row width differs from fitted transform")
            values = tuple(float(value) for value in row)
            if any(not math.isfinite(value) for value in values):
                raise NormalizationContractError("values must be finite")
            clipped = tuple(
                min(self.upper_bounds[index], max(self.lower_bounds[index], value))
                for index, value in enumerate(values)
            )
            output.append(
                tuple(
                    (value - self.means[index]) / self.scales[index]
                    for index, value in enumerate(clipped)
                )
            )
        return tuple(output)

    @property
    def transform_id(self) -> str:
        if not self.fitted or self.training_dataset_id is None:
            raise NormalizationContractError("standardizer is not fitted")
        return fingerprint(
            {
                "schema": "train-only-standardizer-v1",
                "means": self.means,
                "scales": self.scales,
                "lower_bounds": self.lower_bounds,
                "upper_bounds": self.upper_bounds,
                "training_dataset_id": self.training_dataset_id,
            }
        )


def point_in_time_rank(values: Mapping[str, float]) -> dict[str, float]:
    """Map one same-timestamp cross-section to deterministic percentile ranks."""

    if not values:
        raise NormalizationContractError("cross-section cannot be empty")
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise NormalizationContractError("cross-sectional values must be finite")
    ordered = sorted((float(value), symbol) for symbol, value in values.items())
    groups: dict[float, list[int]] = {}
    for index, (value, _) in enumerate(ordered):
        groups.setdefault(value, []).append(index)
    denominator = max(1, len(ordered) - 1)
    result: dict[str, float] = {}
    for value, indices in groups.items():
        average_index = sum(indices) / len(indices)
        rank = average_index / denominator
        for index in indices:
            result[ordered[index][1]] = rank
    return result
