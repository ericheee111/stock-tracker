"""Versioned feature metadata and family-level governance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.fingerprint import fingerprint


class FeatureFamily(StrEnum):
    PRICE_STRUCTURE = "PRICE_STRUCTURE"
    TREND = "TREND"
    MOMENTUM = "MOMENTUM"
    VOLATILITY = "VOLATILITY"
    VOLUME_LIQUIDITY = "VOLUME_LIQUIDITY"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"
    CROSS_SECTIONAL = "CROSS_SECTIONAL"
    EVENT = "EVENT"
    MARKET_CONTEXT = "MARKET_CONTEXT"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    family: FeatureFamily
    lookback_sessions: int
    causal: bool
    description: str
    formula_version: str

    def __post_init__(self) -> None:
        if not self.name or not self.description or not self.formula_version:
            raise ValueError("feature name/description/version must be non-empty")
        if self.lookback_sessions <= 0:
            raise ValueError("lookback_sessions must be positive")
        if not self.causal:
            raise ValueError("formal feature definitions must be causal")

    @property
    def feature_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class FeatureSetDefinition:
    name: str
    version: str
    features: tuple[FeatureDefinition, ...]
    qlib_revision: str | None = None
    numerically_equivalent_to_qlib: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.features:
            raise ValueError("feature set name/version/features are required")
        names = [feature.name for feature in self.features]
        if len(set(names)) != len(names):
            raise ValueError("feature names must be unique")
        if self.numerically_equivalent_to_qlib and not self.qlib_revision:
            raise ValueError("Qlib equivalence requires an exact pinned revision")

    @property
    def feature_set_id(self) -> str:
        return fingerprint(
            {
                "schema": "feature-set-v1",
                "name": self.name,
                "version": self.version,
                "features": [feature.feature_id for feature in self.features],
                "qlib_revision": self.qlib_revision,
                "numerically_equivalent_to_qlib": self.numerically_equivalent_to_qlib,
            }
        )
