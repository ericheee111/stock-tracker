"""Research candidate, experiment and leakage-governance helpers."""

from .candidates import (
    ActionableDecision,
    CandidateContractError,
    CandidateSpec,
    ProbabilityAdvisory,
    risk_gated_action,
)
from .experiments import (
    ExperimentContractError,
    ExperimentEvent,
    ExperimentEventType,
    ExperimentLedger,
    experiment_event,
)
from .leakage import (
    FeatureAvailability,
    LeakageContractError,
    NegativeControlResult,
    assert_calibration_boundary,
    assess_negative_controls,
    random_feature_probabilities,
    randomized_labels,
)

__all__ = [
    "ActionableDecision",
    "CandidateContractError",
    "CandidateSpec",
    "ExperimentContractError",
    "ExperimentEvent",
    "ExperimentEventType",
    "ExperimentLedger",
    "FeatureAvailability",
    "LeakageContractError",
    "NegativeControlResult",
    "ProbabilityAdvisory",
    "assert_calibration_boundary",
    "assess_negative_controls",
    "experiment_event",
    "random_feature_probabilities",
    "randomized_labels",
    "risk_gated_action",
]
