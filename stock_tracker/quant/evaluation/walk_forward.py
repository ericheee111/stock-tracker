"""Purged expanding/rolling walk-forward splits for overlapping labels."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..core.fingerprint import fingerprint
from ..core.time import to_utc
from .metrics import binary_labels


class WalkForwardContractError(ValueError):
    """Raised when temporal splits could leak or cannot satisfy the policy."""


class WalkForwardMode(StrEnum):
    EXPANDING = "EXPANDING"
    ROLLING = "ROLLING"


@dataclass(frozen=True, slots=True)
class TemporalSample:
    sample_id: str
    signal_time: datetime
    label_end_time: datetime
    features: tuple[float, ...]
    target: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sample_id:
            raise WalkForwardContractError("sample_id must be non-empty")
        signal = to_utc(self.signal_time, "signal_time")
        label_end = to_utc(self.label_end_time, "label_end_time")
        if label_end < signal:
            raise WalkForwardContractError("label_end_time cannot precede signal_time")
        binary_labels((self.target,))
        if not self.features:
            raise WalkForwardContractError("features cannot be empty")


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    mode: WalkForwardMode
    minimum_train_samples: int
    validation_samples: int
    step_samples: int
    gap_samples: int = 0
    embargo_samples: int = 0
    rolling_train_samples: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_train_samples <= 0:
            raise WalkForwardContractError("minimum_train_samples must be positive")
        if self.validation_samples <= 0 or self.step_samples <= 0:
            raise WalkForwardContractError(
                "validation_samples and step_samples must be positive"
            )
        if self.gap_samples < 0 or self.embargo_samples < 0:
            raise WalkForwardContractError("gap/embargo samples cannot be negative")
        if self.mode is WalkForwardMode.ROLLING:
            if (
                self.rolling_train_samples is None
                or self.rolling_train_samples < self.minimum_train_samples
            ):
                raise WalkForwardContractError(
                    "rolling mode requires rolling_train_samples >= minimum_train_samples"
                )
        elif self.rolling_train_samples is not None:
            raise WalkForwardContractError(
                "rolling_train_samples is only valid for rolling mode"
            )


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_index: int
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]
    gap_indices: tuple[int, ...]
    embargo_indices: tuple[int, ...]
    validation_start: datetime
    validation_end: datetime
    fold_id: str

    def __post_init__(self) -> None:
        partitions = (
            set(self.train_indices),
            set(self.validation_indices),
            set(self.purged_indices),
            set(self.gap_indices),
            set(self.embargo_indices),
        )
        for index, left in enumerate(partitions):
            for right in partitions[index + 1 :]:
                if left & right:
                    raise WalkForwardContractError("fold partitions must be disjoint")
        if not self.train_indices or not self.validation_indices:
            raise WalkForwardContractError("fold needs train and validation samples")
        to_utc(self.validation_start, "validation_start")
        to_utc(self.validation_end, "validation_end")


def _ordered(samples: Sequence[TemporalSample]) -> tuple[TemporalSample, ...]:
    result = tuple(samples)
    if not result:
        raise WalkForwardContractError("samples cannot be empty")
    keys = [(to_utc(sample.signal_time), sample.sample_id) for sample in result]
    if keys != sorted(keys) or len({sample.sample_id for sample in result}) != len(result):
        raise WalkForwardContractError(
            "samples must be uniquely identified and ordered by signal_time/sample_id"
        )
    width = len(result[0].features)
    if any(len(sample.features) != width for sample in result):
        raise WalkForwardContractError("all samples must share one feature width")
    return result


def build_walk_forward(
    samples: Sequence[TemporalSample],
    config: WalkForwardConfig,
) -> tuple[WalkForwardFold, ...]:
    """Build folds and purge train labels that overlap validation information."""

    ordered = _ordered(samples)
    folds: list[WalkForwardFold] = []
    validation_start_index = config.minimum_train_samples + config.gap_samples
    fold_index = 0
    while validation_start_index + config.validation_samples <= len(ordered):
        validation_end_index = validation_start_index + config.validation_samples
        validation_indices = tuple(range(validation_start_index, validation_end_index))
        validation_start_time = to_utc(ordered[validation_start_index].signal_time)

        nominal_train_end = validation_start_index - config.gap_samples
        if config.mode is WalkForwardMode.ROLLING:
            assert config.rolling_train_samples is not None
            nominal_train_start = max(
                0,
                nominal_train_end - config.rolling_train_samples,
            )
        else:
            nominal_train_start = 0
        nominal_train = tuple(range(nominal_train_start, nominal_train_end))
        gap_indices = tuple(range(nominal_train_end, validation_start_index))
        embargo_end_index = min(
            len(ordered),
            validation_end_index + config.embargo_samples,
        )
        embargo_indices = tuple(range(validation_end_index, embargo_end_index))

        train: list[int] = []
        purged: list[int] = []
        for index in nominal_train:
            if to_utc(ordered[index].label_end_time) >= validation_start_time:
                purged.append(index)
            else:
                train.append(index)
        next_validation_start = max(
            validation_start_index + config.step_samples,
            embargo_end_index,
        )
        if len(train) < config.minimum_train_samples:
            validation_start_index = next_validation_start
            continue

        identity = {
            "schema": "walk-forward-fold-v1",
            "config": config,
            "fold_index": fold_index,
            "train_ids": [ordered[index].sample_id for index in train],
            "validation_ids": [
                ordered[index].sample_id for index in validation_indices
            ],
            "purged_ids": [ordered[index].sample_id for index in purged],
            "gap_ids": [ordered[index].sample_id for index in gap_indices],
            "embargo_ids": [ordered[index].sample_id for index in embargo_indices],
        }
        folds.append(
            WalkForwardFold(
                fold_index=fold_index,
                train_indices=tuple(train),
                validation_indices=validation_indices,
                purged_indices=tuple(purged),
                gap_indices=gap_indices,
                embargo_indices=embargo_indices,
                validation_start=ordered[validation_start_index].signal_time,
                validation_end=ordered[validation_end_index - 1].label_end_time,
                fold_id=fingerprint(identity),
            )
        )
        validation_start_index = next_validation_start
        fold_index += 1
    if not folds:
        raise WalkForwardContractError("no fold satisfies the configured policy")
    return tuple(folds)


def assert_no_label_overlap(
    samples: Sequence[TemporalSample],
    fold: WalkForwardFold,
) -> None:
    validation_start = min(
        to_utc(samples[index].signal_time) for index in fold.validation_indices
    )
    for index in fold.train_indices:
        if to_utc(samples[index].label_end_time) >= validation_start:
            raise WalkForwardContractError(
                f"train sample {samples[index].sample_id} overlaps validation"
            )
