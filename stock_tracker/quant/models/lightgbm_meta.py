"""Optional LightGBM meta-label challenger with deterministic defaults."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.fingerprint import fingerprint
from .baseline import ModelContractError
from .dataset import ModelDataset


@dataclass(slots=True)
class LightGBMMetaLabelCandidate:
    params: dict[str, Any] = field(
        default_factory=lambda: {
            "objective": "binary",
            "n_estimators": 200,
            "learning_rate": 0.03,
            "num_leaves": 15,
            "max_depth": -1,
            "min_child_samples": 30,
            "subsample": 1.0,
            "colsample_bytree": 1.0,
            "reg_alpha": 0.0,
            "reg_lambda": 1.0,
            "random_state": 20260813,
            "n_jobs": 1,
            "verbosity": -1,
        }
    )
    _model: Any = None
    _feature_names: tuple[str, ...] = ()
    _training_dataset_id: str | None = None

    def fit(self, dataset: ModelDataset) -> LightGBMMetaLabelCandidate:
        try:
            from lightgbm import LGBMClassifier
        except ImportError as exc:
            raise ModelContractError(
                "LightGBM is optional and unavailable; Logistic remains Champion"
            ) from exc
        model = LGBMClassifier(**self.params)
        model.fit(list(dataset.features), list(dataset.targets))
        self._model = model
        self._feature_names = dataset.feature_names
        self._training_dataset_id = dataset.dataset_id
        return self

    def predict_proba(
        self,
        features: Sequence[Sequence[float]],
    ) -> tuple[float, ...]:
        if self._model is None:
            raise ModelContractError("LightGBM candidate is not fitted")
        if any(len(row) != len(self._feature_names) for row in features):
            raise ModelContractError("feature width differs from fitted candidate")
        output = self._model.predict_proba(list(features))
        return tuple(float(row[1]) for row in output)

    @property
    def model_id(self) -> str:
        if self._model is None or self._training_dataset_id is None:
            raise ModelContractError("LightGBM candidate is not fitted")
        booster = self._model.booster_
        model_text = booster.model_to_string()
        return fingerprint(
            {
                "schema": "lightgbm-meta-label-v1",
                "params": self.params,
                "feature_names": self._feature_names,
                "training_dataset_id": self._training_dataset_id,
                "booster_text": model_text,
            }
        )
