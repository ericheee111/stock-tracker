"""Probability models, diagnostics, comparison and registry contracts."""

from .baseline import LogisticBaseline, ModelContractError
from .comparison import (
    ChampionGate,
    ChampionGateConfig,
    ComparisonContractError,
    ComparisonIdentity,
    ModelEvaluation,
    PromotionDecision,
)
from .dataset import DatasetContractError, ModelDataset
from .diagnostics import (
    DiagnosticContractError,
    FeatureImportance,
    ablation_indices,
    highly_correlated_pairs,
    permutation_importance,
)
from .horizons import DEFAULT_HORIZONS, HorizonPolicy, horizons_for
from .lightgbm_meta import LightGBMMetaLabelCandidate
from .protocol import ProbabilityModel
from .registry import (
    ModelRegistry,
    RegistryContractError,
    RegistryEvent,
    RegistryEventType,
    registry_event,
)

__all__ = [
    "DEFAULT_HORIZONS",
    "ChampionGate",
    "ChampionGateConfig",
    "ComparisonContractError",
    "ComparisonIdentity",
    "DatasetContractError",
    "DiagnosticContractError",
    "FeatureImportance",
    "HorizonPolicy",
    "LightGBMMetaLabelCandidate",
    "LogisticBaseline",
    "ModelContractError",
    "ModelDataset",
    "ModelEvaluation",
    "ModelRegistry",
    "ProbabilityModel",
    "PromotionDecision",
    "RegistryContractError",
    "RegistryEvent",
    "RegistryEventType",
    "ablation_indices",
    "highly_correlated_pairs",
    "horizons_for",
    "permutation_importance",
    "registry_event",
]
