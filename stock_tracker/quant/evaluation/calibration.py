"""Strictly temporal Platt and isotonic probability calibration."""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..core.fingerprint import fingerprint
from ..core.time import to_utc
from .metrics import MetricContractError, binary_labels


class CalibrationContractError(ValueError):
    """Raised when calibration would include unfinished/future labels."""


class CalibrationMethod(StrEnum):
    PLATT = "PLATT"
    ISOTONIC = "ISOTONIC"


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    sample_id: str
    signal_time: datetime
    label_end_time: datetime
    raw_score: float
    target: int

    def __post_init__(self) -> None:
        if not self.sample_id:
            raise CalibrationContractError("sample_id must be non-empty")
        signal = to_utc(self.signal_time, "signal_time")
        label_end = to_utc(self.label_end_time, "label_end_time")
        if label_end < signal:
            raise CalibrationContractError("label_end_time cannot precede signal_time")
        if not math.isfinite(self.raw_score):
            raise CalibrationContractError("raw_score must be finite")
        binary_labels((self.target,))


def select_completed_rows(
    rows: Iterable[CalibrationRow],
    *,
    cutoff: datetime,
    minimum_signal_time: datetime | None = None,
) -> tuple[CalibrationRow, ...]:
    """Select rows by actual label completion, never signal timestamp alone."""

    boundary = to_utc(cutoff, "cutoff")
    minimum = to_utc(minimum_signal_time) if minimum_signal_time is not None else None
    selected = [
        row
        for row in rows
        if to_utc(row.label_end_time) <= boundary
        and (minimum is None or to_utc(row.signal_time) >= minimum)
    ]
    selected.sort(key=lambda row: (to_utc(row.label_end_time), row.sample_id))
    if not selected:
        raise CalibrationContractError("no completed labels are available for calibration")
    return tuple(selected)


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp_value = math.exp(-value)
        return 1 / (1 + exp_value)
    exp_value = math.exp(value)
    return exp_value / (1 + exp_value)


@dataclass(slots=True)
class PlattCalibrator:
    slope: float = 0.0
    intercept: float = 0.0
    fitted: bool = False

    def fit(
        self,
        scores: Sequence[float],
        targets: Sequence[int | float],
        *,
        learning_rate: float = 0.05,
        max_iter: int = 5000,
        tolerance: float = 1e-10,
        l2: float = 1e-6,
    ) -> PlattCalibrator:
        labels = binary_labels(targets)
        values = tuple(float(value) for value in scores)
        if len(labels) != len(values) or not values:
            raise CalibrationContractError("scores and targets must be non-empty/equal")
        if any(not math.isfinite(value) for value in values):
            raise CalibrationContractError("scores must be finite")
        if len(set(labels)) != 2:
            raise CalibrationContractError("Platt calibration requires both classes")
        if learning_rate <= 0 or max_iter <= 0 or tolerance <= 0 or l2 < 0:
            raise CalibrationContractError("invalid Platt optimizer settings")
        slope = 0.0
        positive_rate = sum(labels) / len(labels)
        intercept = math.log(positive_rate / (1 - positive_rate))
        previous_loss = math.inf
        for _ in range(max_iter):
            probabilities = tuple(_sigmoid(slope * x + intercept) for x in values)
            grad_slope = (
                sum((probability - label) * x for x, label, probability in zip(values, labels, probabilities))
                / len(values)
                + l2 * slope
            )
            grad_intercept = sum(
                probability - label for label, probability in zip(labels, probabilities)
            ) / len(values)
            slope -= learning_rate * grad_slope
            intercept -= learning_rate * grad_intercept
            loss = -sum(
                label * math.log(max(probability, 1e-15))
                + (1 - label) * math.log(max(1 - probability, 1e-15))
                for label, probability in zip(labels, probabilities)
            ) / len(labels) + l2 * slope * slope / 2
            if abs(previous_loss - loss) <= tolerance:
                break
            previous_loss = loss
        if not math.isfinite(slope) or not math.isfinite(intercept):
            raise CalibrationContractError("Platt calibration diverged")
        self.slope = slope
        self.intercept = intercept
        self.fitted = True
        return self

    def predict(self, scores: Iterable[float]) -> tuple[float, ...]:
        if not self.fitted:
            raise CalibrationContractError("Platt calibrator is not fitted")
        values = tuple(float(value) for value in scores)
        if any(not math.isfinite(value) for value in values):
            raise CalibrationContractError("scores must be finite")
        return tuple(_sigmoid(self.slope * value + self.intercept) for value in values)

    @property
    def model_id(self) -> str:
        if not self.fitted:
            raise CalibrationContractError("Platt calibrator is not fitted")
        return fingerprint(
            {
                "schema": "platt-calibrator-v1",
                "slope": self.slope,
                "intercept": self.intercept,
            }
        )


@dataclass(slots=True)
class IsotonicCalibrator:
    thresholds: tuple[float, ...] = ()
    values: tuple[float, ...] = ()
    fitted: bool = False

    def fit(
        self,
        scores: Sequence[float],
        targets: Sequence[int | float],
    ) -> IsotonicCalibrator:
        labels = binary_labels(targets)
        raw = tuple(float(value) for value in scores)
        if len(labels) != len(raw) or not raw:
            raise CalibrationContractError("scores and targets must be non-empty/equal")
        if any(not math.isfinite(value) for value in raw):
            raise CalibrationContractError("scores must be finite")
        ordered = sorted(zip(raw, labels), key=lambda item: item[0])
        grouped: list[list[float]] = []
        for score, label in ordered:
            if grouped and grouped[-1][1] == score:
                grouped[-1][2] += label
                grouped[-1][3] += 1
                grouped[-1][4] = score
            else:
                grouped.append([score, score, float(label), 1.0, score])
        blocks: list[list[float]] = []
        for low, high, total, weight, threshold in grouped:
            blocks.append([low, high, total, weight, threshold])
            while len(blocks) >= 2:
                left = blocks[-2]
                right = blocks[-1]
                if left[2] / left[3] <= right[2] / right[3]:
                    break
                blocks[-2:] = [[
                    left[0],
                    right[1],
                    left[2] + right[2],
                    left[3] + right[3],
                    right[4],
                ]]
        self.thresholds = tuple(block[1] for block in blocks)
        self.values = tuple(block[2] / block[3] for block in blocks)
        self.fitted = True
        return self

    def predict(self, scores: Iterable[float]) -> tuple[float, ...]:
        if not self.fitted:
            raise CalibrationContractError("isotonic calibrator is not fitted")
        result: list[float] = []
        for raw in scores:
            score = float(raw)
            if not math.isfinite(score):
                raise CalibrationContractError("scores must be finite")
            index = bisect.bisect_left(self.thresholds, score)
            index = min(index, len(self.values) - 1)
            result.append(self.values[index])
        return tuple(result)

    @property
    def model_id(self) -> str:
        if not self.fitted:
            raise CalibrationContractError("isotonic calibrator is not fitted")
        return fingerprint(
            {
                "schema": "isotonic-calibrator-v1",
                "thresholds": self.thresholds,
                "values": self.values,
            }
        )


def fit_temporal_calibrator(
    rows: Iterable[CalibrationRow],
    *,
    cutoff: datetime,
    method: CalibrationMethod,
) -> PlattCalibrator | IsotonicCalibrator:
    selected = select_completed_rows(rows, cutoff=cutoff)
    scores = tuple(row.raw_score for row in selected)
    targets = tuple(row.target for row in selected)
    if method is CalibrationMethod.PLATT:
        return PlattCalibrator().fit(scores, targets)
    if method is CalibrationMethod.ISOTONIC:
        return IsotonicCalibrator().fit(scores, targets)
    raise MetricContractError(f"unsupported calibration method: {method}")
