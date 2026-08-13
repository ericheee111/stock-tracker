"""Reproducibility evidence for training and research runs."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .fingerprint import fingerprint
from .time import to_utc, utc_now


def set_reproducible(seed: int) -> None:
    """Seed Python and NumPy when available."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except ImportError:
        pass


def installed_versions(names: tuple[str, ...]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "UNAVAILABLE"
    return versions


@dataclass(frozen=True, slots=True)
class ReproducibilityRecord:
    config_hash: str
    data_snapshot_ids: tuple[str, ...]
    code_version: str
    random_seed: int
    trained_at: datetime
    library_versions: dict[str, str] = field(default_factory=dict)
    runtime: dict[str, str] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.config_hash) != 64:
            raise ValueError("config_hash must be SHA-256")
        if any(len(value) != 64 for value in self.data_snapshot_ids):
            raise ValueError("each data_snapshot_id must be SHA-256")
        if not self.code_version:
            raise ValueError("code_version must be non-empty")
        if self.random_seed < 0:
            raise ValueError("random_seed cannot be negative")
        to_utc(self.trained_at, "trained_at")

    @property
    def record_id(self) -> str:
        return fingerprint(self)

    @classmethod
    def build(
        cls,
        *,
        config: Any,
        data_snapshot_ids: tuple[str, ...],
        code_version: str,
        random_seed: int,
        trained_at: datetime | None = None,
        packages: tuple[str, ...] = (
            "numpy",
            "pandas",
            "scikit-learn",
            "scipy",
            "lightgbm",
        ),
        notes: dict[str, Any] | None = None,
    ) -> ReproducibilityRecord:
        return cls(
            config_hash=fingerprint(config),
            data_snapshot_ids=tuple(sorted(data_snapshot_ids)),
            code_version=code_version,
            random_seed=random_seed,
            trained_at=to_utc(trained_at or utc_now()),
            library_versions=installed_versions(packages),
            runtime={
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
            notes=dict(notes or {}),
        )
