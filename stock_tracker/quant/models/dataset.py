"""Immutable, finite model matrices with time and sample identity."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..core.fingerprint import fingerprint
from ..core.time import to_utc
from ..evaluation.metrics import binary_labels
from ..evaluation.walk_forward import TemporalSample


class DatasetContractError(ValueError):
    """Raised when a model matrix loses temporal or feature identity."""


@dataclass(frozen=True, slots=True)
class ModelDataset:
    features: tuple[tuple[float, ...], ...]
    targets: tuple[int, ...]
    sample_ids: tuple[str, ...]
    feature_names: tuple[str, ...]
    signal_times: tuple[datetime, ...]
    label_end_times: tuple[datetime, ...]
    snapshot_id: str

    def __post_init__(self) -> None:
        rows = len(self.features)
        lengths = {
            rows,
            len(self.targets),
            len(self.sample_ids),
            len(self.signal_times),
            len(self.label_end_times),
        }
        if len(lengths) != 1 or rows == 0:
            raise DatasetContractError("all dataset columns must be non-empty/equal")
        if not self.feature_names:
            raise DatasetContractError("feature_names cannot be empty")
        if len(set(self.feature_names)) != len(self.feature_names):
            raise DatasetContractError("feature_names must be unique")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise DatasetContractError("sample_ids must be unique")
        width = len(self.feature_names)
        for row in self.features:
            if len(row) != width:
                raise DatasetContractError("feature row width mismatch")
            if any(not math.isfinite(value) for value in row):
                raise DatasetContractError("features must be finite")
        binary_labels(self.targets)
        keys = []
        for signal_time, label_end, sample_id in zip(
            self.signal_times,
            self.label_end_times,
            self.sample_ids,
        ):
            signal = to_utc(signal_time, "signal_time")
            end = to_utc(label_end, "label_end_time")
            if end < signal:
                raise DatasetContractError("label_end_time cannot precede signal_time")
            keys.append((signal, sample_id))
        if keys != sorted(keys):
            raise DatasetContractError("dataset must be chronological by signal_time/id")
        if len(self.snapshot_id) != 64:
            raise DatasetContractError("snapshot_id must be SHA-256")

    @classmethod
    def from_temporal_samples(
        cls,
        samples: Sequence[TemporalSample],
        *,
        feature_names: Sequence[str],
        snapshot_id: str,
    ) -> ModelDataset:
        return cls(
            features=tuple(tuple(float(value) for value in sample.features) for sample in samples),
            targets=tuple(sample.target for sample in samples),
            sample_ids=tuple(sample.sample_id for sample in samples),
            feature_names=tuple(feature_names),
            signal_times=tuple(sample.signal_time for sample in samples),
            label_end_times=tuple(sample.label_end_time for sample in samples),
            snapshot_id=snapshot_id,
        )

    def subset(self, indices: Sequence[int]) -> ModelDataset:
        selected = tuple(indices)
        if not selected:
            raise DatasetContractError("subset indices cannot be empty")
        if len(set(selected)) != len(selected):
            raise DatasetContractError("subset indices must be unique")
        if any(index < 0 or index >= len(self.features) for index in selected):
            raise DatasetContractError("subset index out of range")
        return ModelDataset(
            features=tuple(self.features[index] for index in selected),
            targets=tuple(self.targets[index] for index in selected),
            sample_ids=tuple(self.sample_ids[index] for index in selected),
            feature_names=self.feature_names,
            signal_times=tuple(self.signal_times[index] for index in selected),
            label_end_times=tuple(self.label_end_times[index] for index in selected),
            snapshot_id=fingerprint(
                {
                    "schema": "model-dataset-subset-v1",
                    "parent": self.dataset_id,
                    "sample_ids": [self.sample_ids[index] for index in selected],
                }
            ),
        )

    @property
    def dataset_id(self) -> str:
        return fingerprint(
            {
                "schema": "model-dataset-v1",
                "snapshot_id": self.snapshot_id,
                "feature_names": self.feature_names,
                "sample_ids": self.sample_ids,
                "features": self.features,
                "targets": self.targets,
                "signal_times": self.signal_times,
                "label_end_times": self.label_end_times,
            }
        )
