"""Minimal probability-model protocol used by research orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .dataset import ModelDataset


@runtime_checkable
class ProbabilityModel(Protocol):
    """Models must expose deterministic fitting, probability and identity."""

    def fit(self, dataset: ModelDataset) -> ProbabilityModel: ...

    def predict_proba(
        self,
        features: Sequence[Sequence[float]],
    ) -> tuple[float, ...]: ...

    @property
    def model_id(self) -> str: ...
