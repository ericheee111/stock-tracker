"""Dependency-light Logistic Regression production baseline."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..core.fingerprint import fingerprint
from .dataset import DatasetContractError, ModelDataset


class ModelContractError(ValueError):
    """Raised when a model is unfitted, divergent or receives invalid data."""


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp_value = math.exp(-value)
        return 1 / (1 + exp_value)
    exp_value = math.exp(value)
    return exp_value / (1 + exp_value)


@dataclass(slots=True)
class LogisticBaseline:
    learning_rate: float = 0.05
    max_iter: int = 10_000
    l2: float = 1e-3
    tolerance: float = 1e-9
    coefficients: tuple[float, ...] = ()
    intercept: float = 0.0
    means: tuple[float, ...] = ()
    scales: tuple[float, ...] = ()
    feature_names: tuple[str, ...] = ()
    training_dataset_id: str | None = None
    iterations: int = 0
    fitted: bool = False

    def __post_init__(self) -> None:
        if self.learning_rate <= 0 or self.max_iter <= 0:
            raise ModelContractError("learning_rate/max_iter must be positive")
        if self.l2 < 0 or self.tolerance <= 0:
            raise ModelContractError("l2 must be non-negative and tolerance positive")

    @staticmethod
    def _normalization(
        rows: Sequence[Sequence[float]],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        width = len(rows[0])
        means = tuple(sum(row[column] for row in rows) / len(rows) for column in range(width))
        scales: list[float] = []
        for column, mean in enumerate(means):
            variance = sum((row[column] - mean) ** 2 for row in rows) / len(rows)
            scale = math.sqrt(variance)
            scales.append(scale if scale > 1e-12 else 1.0)
        return means, tuple(scales)

    @staticmethod
    def _transform_rows(
        rows: Sequence[Sequence[float]],
        means: Sequence[float],
        scales: Sequence[float],
    ) -> tuple[tuple[float, ...], ...]:
        width = len(means)
        result: list[tuple[float, ...]] = []
        for row in rows:
            if len(row) != width:
                raise ModelContractError("feature width differs from fitted model")
            values = tuple(float(value) for value in row)
            if any(not math.isfinite(value) for value in values):
                raise ModelContractError("features must be finite")
            result.append(
                tuple(
                    (values[column] - means[column]) / scales[column]
                    for column in range(width)
                )
            )
        return tuple(result)

    def fit(self, dataset: ModelDataset) -> LogisticBaseline:
        if not isinstance(dataset, ModelDataset):
            raise TypeError("LogisticBaseline.fit requires ModelDataset")
        if len(set(dataset.targets)) != 2:
            raise ModelContractError("Logistic baseline requires both target classes")
        means, scales = self._normalization(dataset.features)
        rows = self._transform_rows(dataset.features, means, scales)
        width = len(dataset.feature_names)
        coefficients = [0.0] * width
        positive_rate = sum(dataset.targets) / len(dataset.targets)
        intercept = math.log(positive_rate / (1 - positive_rate))
        previous_loss = math.inf
        iteration = 0
        for iteration in range(1, self.max_iter + 1):
            logits = tuple(
                intercept + sum(weight * value for weight, value in zip(coefficients, row))
                for row in rows
            )
            probabilities = tuple(_sigmoid(value) for value in logits)
            errors = tuple(
                probability - target
                for probability, target in zip(probabilities, dataset.targets)
            )
            grad_intercept = sum(errors) / len(rows)
            gradients = [
                sum(error * row[column] for error, row in zip(errors, rows)) / len(rows)
                + self.l2 * coefficients[column]
                for column in range(width)
            ]
            intercept -= self.learning_rate * grad_intercept
            for column in range(width):
                coefficients[column] -= self.learning_rate * gradients[column]
            loss = -sum(
                target * math.log(max(probability, 1e-15))
                + (1 - target) * math.log(max(1 - probability, 1e-15))
                for target, probability in zip(dataset.targets, probabilities)
            ) / len(rows) + self.l2 * sum(value * value for value in coefficients) / 2
            if not math.isfinite(loss):
                raise ModelContractError("Logistic training diverged")
            if abs(previous_loss - loss) <= self.tolerance:
                break
            previous_loss = loss
        self.coefficients = tuple(coefficients)
        self.intercept = intercept
        self.means = means
        self.scales = scales
        self.feature_names = dataset.feature_names
        self.training_dataset_id = dataset.dataset_id
        self.iterations = iteration
        self.fitted = True
        return self

    def decision_function(
        self,
        features: Sequence[Sequence[float]],
    ) -> tuple[float, ...]:
        if not self.fitted:
            raise ModelContractError("Logistic baseline is not fitted")
        rows = self._transform_rows(features, self.means, self.scales)
        return tuple(
            self.intercept
            + sum(weight * value for weight, value in zip(self.coefficients, row))
            for row in rows
        )

    def predict_proba(
        self,
        features: Sequence[Sequence[float]],
    ) -> tuple[float, ...]:
        return tuple(_sigmoid(value) for value in self.decision_function(features))

    @property
    def model_id(self) -> str:
        if not self.fitted or self.training_dataset_id is None:
            raise ModelContractError("Logistic baseline is not fitted")
        return fingerprint(
            {
                "schema": "logistic-baseline-v1",
                "parameters": {
                    "learning_rate": self.learning_rate,
                    "max_iter": self.max_iter,
                    "l2": self.l2,
                    "tolerance": self.tolerance,
                },
                "coefficients": self.coefficients,
                "intercept": self.intercept,
                "means": self.means,
                "scales": self.scales,
                "feature_names": self.feature_names,
                "training_dataset_id": self.training_dataset_id,
                "iterations": self.iterations,
            }
        )

    def as_dict(self) -> dict[str, object]:
        if not self.fitted:
            raise ModelContractError("Logistic baseline is not fitted")
        return {
            "schema": "logistic-baseline-v1",
            "learning_rate": self.learning_rate,
            "max_iter": self.max_iter,
            "l2": self.l2,
            "tolerance": self.tolerance,
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "means": list(self.means),
            "scales": list(self.scales),
            "feature_names": list(self.feature_names),
            "training_dataset_id": self.training_dataset_id,
            "iterations": self.iterations,
            "model_id": self.model_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> LogisticBaseline:
        if value.get("schema") != "logistic-baseline-v1":
            raise DatasetContractError("unsupported Logistic model schema")
        model = cls(
            learning_rate=float(value["learning_rate"]),
            max_iter=int(value["max_iter"]),
            l2=float(value["l2"]),
            tolerance=float(value["tolerance"]),
        )
        model.coefficients = tuple(float(item) for item in value["coefficients"])
        model.intercept = float(value["intercept"])
        model.means = tuple(float(item) for item in value["means"])
        model.scales = tuple(float(item) for item in value["scales"])
        model.feature_names = tuple(str(item) for item in value["feature_names"])
        model.training_dataset_id = str(value["training_dataset_id"])
        model.iterations = int(value["iterations"])
        model.fitted = True
        if value.get("model_id") != model.model_id:
            raise ModelContractError("model_id does not match Logistic model content")
        return model
