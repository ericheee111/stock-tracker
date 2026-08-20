"""Unified decision-quality and model-promotion evidence gate.

The gate composes model comparison, point-in-time replay, real outcome evidence,
frozen holdout state, calibration, leakage controls, licensing, and
multiple-testing identity.  It produces an auditable recommendation only; it
cannot write the model registry, deploy an artifact, change runtime weights, or
create orders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from ..core.outcomes import ScoreboardState, StrategyScoreboard
from ..data.bar_artifact import DataTrustTier
from ..models.comparison import (
    ChampionGate,
    ChampionGateConfig,
    ModelEvaluation,
    PromotionDecision,
)
from ..research.replay import ReplayPlan, ReplayPlanState, ReplayPurpose
from .holdout import FrozenHoldoutRecord, HoldoutState

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUST_RANK = {
    DataTrustTier.UNKNOWN: 0,
    DataTrustTier.BEST_EFFORT: 1,
    DataTrustTier.OPERATIONAL_VERIFIED: 2,
    DataTrustTier.RESEARCH_GRADE: 3,
    DataTrustTier.FROZEN_HOLDOUT: 4,
}


class DecisionQualityContractError(ValueError):
    """Raised when decision-quality evidence is malformed or mismatched."""


class ResearchLicenseStatus(StrEnum):
    PENDING = "LICENSE_PENDING"
    CLEARED = "LICENSE_CLEARED"
    REJECTED = "LICENSE_REJECTED"


class DecisionQualityState(StrEnum):
    BLOCKED = "BLOCKED"
    CHALLENGER_DIAGNOSTIC = "CHALLENGER_DIAGNOSTIC"
    PROMOTION_REJECTED = "PROMOTION_REJECTED"
    PROMOTION_ELIGIBLE = "PROMOTION_ELIGIBLE"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DecisionQualityContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise DecisionQualityContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise DecisionQualityContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DecisionQualityContractError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class DecisionQualityPolicy:
    policy_version: str
    minimum_data_trust: DataTrustTier = DataTrustTier.RESEARCH_GRADE
    maximum_holdout_exposures: int = 1
    minimum_recorded_trials: int = 1
    comparison_gate_config: ChampionGateConfig = field(
        default_factory=ChampionGateConfig
    )
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if not isinstance(self.minimum_data_trust, DataTrustTier):
            raise DecisionQualityContractError(
                "minimum_data_trust must be DataTrustTier"
            )
        _require_positive_int(
            self.maximum_holdout_exposures,
            "maximum_holdout_exposures",
        )
        _require_positive_int(
            self.minimum_recorded_trials,
            "minimum_recorded_trials",
        )
        if not isinstance(self.comparison_gate_config, ChampionGateConfig):
            raise DecisionQualityContractError(
                "comparison_gate_config must be ChampionGateConfig"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "decision-quality-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_data_trust": self.minimum_data_trust,
                    "maximum_holdout_exposures": self.maximum_holdout_exposures,
                    "minimum_recorded_trials": self.minimum_recorded_trials,
                    "comparison_gate_config": self.comparison_gate_config,
                }
            ),
        )


DEFAULT_DECISION_QUALITY_POLICY = DecisionQualityPolicy(
    policy_version="decision-quality-gate-v1"
)


@dataclass(frozen=True, slots=True)
class DecisionQualityEvidence:
    strategy_id: str
    market: Market
    horizon_sessions: int
    baseline_model_id: str
    champion_model_id: str
    challenger_model_id: str
    code_id: str
    config_id: str
    dataset_id: str
    feature_set_id: str
    label_id: str
    calibration_id: str
    leakage_audit_id: str
    negative_control_id: str
    experiment_id: str
    registry_snapshot_id: str
    scoreboard_id: str
    replay_plan_id: str
    holdout_record_id: str
    data_trust_tier: DataTrustTier
    license_status: ResearchLicenseStatus
    license_evidence_ids: tuple[str, ...]
    verification_evidence_ids: tuple[str, ...]
    recorded_trials: int
    verified: bool
    complete: bool
    calibration_verified: bool
    leakage_audit_passed: bool
    negative_controls_passed: bool
    synthetic_fixture_only: bool
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "strategy_id",
            "baseline_model_id",
            "champion_model_id",
            "challenger_model_id",
        ):
            _require_text(getattr(self, name), name)
        if not isinstance(self.market, Market):
            raise DecisionQualityContractError("market must be Market")
        _require_positive_int(self.horizon_sessions, "horizon_sessions")
        for name in (
            "code_id",
            "config_id",
            "dataset_id",
            "feature_set_id",
            "label_id",
            "calibration_id",
            "leakage_audit_id",
            "negative_control_id",
            "experiment_id",
            "registry_snapshot_id",
            "scoreboard_id",
            "replay_plan_id",
            "holdout_record_id",
        ):
            _require_sha256(getattr(self, name), name)
        if not isinstance(self.data_trust_tier, DataTrustTier):
            raise DecisionQualityContractError(
                "data_trust_tier must be DataTrustTier"
            )
        if not isinstance(self.license_status, ResearchLicenseStatus):
            raise DecisionQualityContractError(
                "license_status must be ResearchLicenseStatus"
            )
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.license_evidence_ids
        ):
            raise DecisionQualityContractError(
                "license_evidence_ids must contain lowercase SHA-256"
            )
        if self.license_evidence_ids != tuple(
            sorted(set(self.license_evidence_ids))
        ):
            raise DecisionQualityContractError(
                "license_evidence_ids must be sorted and unique"
            )
        if self.license_status is ResearchLicenseStatus.PENDING:
            if self.license_evidence_ids:
                raise DecisionQualityContractError(
                    "pending license status cannot carry clearance evidence"
                )
        elif not self.license_evidence_ids:
            raise DecisionQualityContractError(
                "cleared or rejected license status requires evidence"
            )
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.verification_evidence_ids
        ):
            raise DecisionQualityContractError(
                "verification_evidence_ids must contain lowercase SHA-256"
            )
        if self.verification_evidence_ids != tuple(
            sorted(set(self.verification_evidence_ids))
        ):
            raise DecisionQualityContractError(
                "verification_evidence_ids must be sorted and unique"
            )
        _require_positive_int(self.recorded_trials, "recorded_trials")
        for name in (
            "verified",
            "complete",
            "calibration_verified",
            "leakage_audit_passed",
            "negative_controls_passed",
            "synthetic_fixture_only",
        ):
            _require_bool(getattr(self, name), name)
        if self.verified and not self.verification_evidence_ids:
            raise DecisionQualityContractError(
                "verified decision-quality evidence requires verification IDs"
            )
        if not self.verified and self.verification_evidence_ids:
            raise DecisionQualityContractError(
                "unverified decision-quality evidence cannot carry verification IDs"
            )
        if self.synthetic_fixture_only:
            if self.verified or self.data_trust_tier is not DataTrustTier.BEST_EFFORT:
                raise DecisionQualityContractError(
                    "synthetic evidence must remain unverified BEST_EFFORT"
                )
        elif (
            _TRUST_RANK[self.data_trust_tier]
            >= _TRUST_RANK[DataTrustTier.OPERATIONAL_VERIFIED]
            and not self.verified
        ):
            raise DecisionQualityContractError(
                "operational-or-higher evidence must be verified"
            )
        object.__setattr__(
            self,
            "evidence_id",
            fingerprint(
                {
                    "schema": "decision-quality-evidence-v1",
                    "strategy_id": self.strategy_id,
                    "market": self.market,
                    "horizon_sessions": self.horizon_sessions,
                    "baseline_model_id": self.baseline_model_id,
                    "champion_model_id": self.champion_model_id,
                    "challenger_model_id": self.challenger_model_id,
                    "code_id": self.code_id,
                    "config_id": self.config_id,
                    "dataset_id": self.dataset_id,
                    "feature_set_id": self.feature_set_id,
                    "label_id": self.label_id,
                    "calibration_id": self.calibration_id,
                    "leakage_audit_id": self.leakage_audit_id,
                    "negative_control_id": self.negative_control_id,
                    "experiment_id": self.experiment_id,
                    "registry_snapshot_id": self.registry_snapshot_id,
                    "scoreboard_id": self.scoreboard_id,
                    "replay_plan_id": self.replay_plan_id,
                    "holdout_record_id": self.holdout_record_id,
                    "data_trust_tier": self.data_trust_tier,
                    "license_status": self.license_status,
                    "license_evidence_ids": list(self.license_evidence_ids),
                    "verification_evidence_ids": list(
                        self.verification_evidence_ids
                    ),
                    "recorded_trials": self.recorded_trials,
                    "verified": self.verified,
                    "complete": self.complete,
                    "calibration_verified": self.calibration_verified,
                    "leakage_audit_passed": self.leakage_audit_passed,
                    "negative_controls_passed": self.negative_controls_passed,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class DecisionQualityAssessment:
    evidence: DecisionQualityEvidence
    baseline: ModelEvaluation
    champion: ModelEvaluation
    challenger: ModelEvaluation
    scoreboard: StrategyScoreboard
    replay_plan: ReplayPlan
    holdout: FrozenHoldoutRecord
    policy: DecisionQualityPolicy = DEFAULT_DECISION_QUALITY_POLICY
    baseline_decision: PromotionDecision = field(init=False)
    champion_decision: PromotionDecision = field(init=False)
    structural_blockers: tuple[str, ...] = field(init=False)
    formal_blockers: tuple[str, ...] = field(init=False)
    rejection_reasons: tuple[str, ...] = field(init=False)
    state: DecisionQualityState = field(init=False)
    assessment_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, DecisionQualityEvidence):
            raise DecisionQualityContractError(
                "evidence must be DecisionQualityEvidence"
            )
        for value, name in (
            (self.baseline, "baseline"),
            (self.champion, "champion"),
            (self.challenger, "challenger"),
        ):
            if not isinstance(value, ModelEvaluation):
                raise DecisionQualityContractError(
                    f"{name} must be ModelEvaluation"
                )
        if not isinstance(self.scoreboard, StrategyScoreboard):
            raise DecisionQualityContractError(
                "scoreboard must be StrategyScoreboard"
            )
        if not isinstance(self.replay_plan, ReplayPlan):
            raise DecisionQualityContractError("replay_plan must be ReplayPlan")
        if not isinstance(self.holdout, FrozenHoldoutRecord):
            raise DecisionQualityContractError(
                "holdout must be FrozenHoldoutRecord"
            )
        if not isinstance(self.policy, DecisionQualityPolicy):
            raise DecisionQualityContractError(
                "policy must be DecisionQualityPolicy"
            )

        gate = ChampionGate(self.policy.comparison_gate_config)
        baseline_decision = gate.evaluate(self.baseline, self.challenger)
        champion_decision = gate.evaluate(self.champion, self.challenger)
        structural: set[str] = set()
        formal: set[str] = set()
        rejection: set[str] = set()

        identities = (
            (self.evidence.baseline_model_id, self.baseline.model_id, "BASELINE"),
            (self.evidence.champion_model_id, self.champion.model_id, "CHAMPION"),
            (self.evidence.challenger_model_id, self.challenger.model_id, "CHALLENGER"),
        )
        for expected, actual, label in identities:
            if expected != actual:
                structural.add(f"{label}_MODEL_ID_MISMATCH")
        comparison_ids = {
            self.baseline.comparison_id,
            self.champion.comparison_id,
            self.challenger.comparison_id,
        }
        if len(comparison_ids) != 1:
            structural.add("MODEL_COMPARISON_IDENTITY_MISMATCH")
        if self.evidence.scoreboard_id != self.scoreboard.scoreboard_id:
            structural.add("SCOREBOARD_ID_MISMATCH")
        if self.evidence.replay_plan_id != self.replay_plan.plan_id:
            structural.add("REPLAY_PLAN_ID_MISMATCH")
        if self.evidence.holdout_record_id != self.holdout.record_hash:
            structural.add("HOLDOUT_RECORD_ID_MISMATCH")
        if self.scoreboard.strategy_id != self.evidence.strategy_id:
            structural.add("SCOREBOARD_STRATEGY_MISMATCH")
        if self.scoreboard.market is not self.evidence.market:
            structural.add("SCOREBOARD_MARKET_MISMATCH")
        if self.scoreboard.horizon_sessions != self.evidence.horizon_sessions:
            structural.add("SCOREBOARD_HORIZON_MISMATCH")
        if self.scoreboard.model_id != self.evidence.challenger_model_id:
            structural.add("SCOREBOARD_CHALLENGER_MODEL_MISMATCH")
        if self.evidence.synthetic_fixture_only and (
            self.scoreboard.evidence_tier is not DataTrustTier.BEST_EFFORT
        ):
            structural.add("SYNTHETIC_SCOREBOARD_TIER_MISMATCH")
        if self.replay_plan.request.market is not self.evidence.market:
            structural.add("REPLAY_MARKET_MISMATCH")
        if self.holdout.config_hash != self.evidence.config_id:
            structural.add("HOLDOUT_CONFIG_ID_MISMATCH")
        if self.holdout.data_snapshot_id != self.evidence.dataset_id:
            structural.add("HOLDOUT_DATASET_ID_MISMATCH")

        if self.evidence.synthetic_fixture_only:
            formal.add("SYNTHETIC_FIXTURE_ONLY")
        if not self.evidence.verified:
            formal.add("EVIDENCE_NOT_VERIFIED")
        if not self.evidence.complete:
            formal.add("EVIDENCE_INCOMPLETE")
        if (
            _TRUST_RANK[self.evidence.data_trust_tier]
            < _TRUST_RANK[self.policy.minimum_data_trust]
        ):
            formal.add("T3_NOT_REACHED")
        if self.evidence.license_status is not ResearchLicenseStatus.CLEARED:
            formal.add(self.evidence.license_status.value)
        if not self.evidence.calibration_verified:
            formal.add("CALIBRATION_NOT_VERIFIED")
        if not self.evidence.leakage_audit_passed:
            formal.add("LEAKAGE_AUDIT_FAILED")
        if not self.evidence.negative_controls_passed:
            formal.add("NEGATIVE_CONTROLS_FAILED")
        if self.evidence.recorded_trials < self.policy.minimum_recorded_trials:
            formal.add("MULTIPLE_TESTING_RECORD_INSUFFICIENT")
        if self.scoreboard.state is not ScoreboardState.REAL_EVIDENCE_AVAILABLE:
            formal.add("REAL_OUTCOME_SCOREBOARD_UNAVAILABLE")
        if self.replay_plan.state is not ReplayPlanState.READY:
            formal.add("REPLAY_PLAN_NOT_READY")
        if not self.replay_plan.formal_research_eligible:
            formal.add("FORMAL_PIT_REPLAY_UNAVAILABLE")
        if self.replay_plan.request.purpose not in {
            ReplayPurpose.FORMAL_DECISION,
            ReplayPurpose.FROZEN_HOLDOUT,
        }:
            formal.add("REPLAY_PURPOSE_NOT_FORMAL")
        if self.holdout.state is HoldoutState.COMPROMISED:
            formal.add("FROZEN_HOLDOUT_COMPROMISED")
        elif self.holdout.state is not HoldoutState.EXPOSED:
            formal.add("FROZEN_HOLDOUT_NOT_EXPOSED")
        if self.holdout.exposure_count > self.policy.maximum_holdout_exposures:
            formal.add("FROZEN_HOLDOUT_OVEREXPOSED")

        if not baseline_decision.promoted:
            rejection.update(f"BASELINE:{item}" for item in baseline_decision.reasons)
        if not champion_decision.promoted:
            rejection.update(f"CHAMPION:{item}" for item in champion_decision.reasons)

        structural_tuple = tuple(sorted(structural))
        formal_tuple = tuple(sorted(formal))
        rejection_tuple = tuple(sorted(rejection))
        if structural_tuple:
            state = DecisionQualityState.BLOCKED
        elif self.evidence.synthetic_fixture_only:
            state = DecisionQualityState.CHALLENGER_DIAGNOSTIC
        elif formal_tuple:
            state = DecisionQualityState.BLOCKED
        elif rejection_tuple:
            state = DecisionQualityState.PROMOTION_REJECTED
        else:
            state = DecisionQualityState.PROMOTION_ELIGIBLE
        object.__setattr__(self, "baseline_decision", baseline_decision)
        object.__setattr__(self, "champion_decision", champion_decision)
        object.__setattr__(self, "structural_blockers", structural_tuple)
        object.__setattr__(self, "formal_blockers", formal_tuple)
        object.__setattr__(self, "rejection_reasons", rejection_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "assessment_id",
            fingerprint(
                {
                    "schema": "decision-quality-assessment-v1",
                    "evidence_id": self.evidence.evidence_id,
                    "baseline_decision_id": baseline_decision.decision_id,
                    "champion_decision_id": champion_decision.decision_id,
                    "scoreboard_id": self.scoreboard.scoreboard_id,
                    "replay_plan_id": self.replay_plan.plan_id,
                    "holdout_record_hash": self.holdout.record_hash,
                    "policy_id": self.policy.policy_id,
                    "structural_blockers": structural_tuple,
                    "formal_blockers": formal_tuple,
                    "rejection_reasons": rejection_tuple,
                    "state": state,
                    "writes_model_registry": False,
                    "deploys_model": False,
                    "changes_runtime_weight": False,
                    "creates_order": False,
                }
            ),
        )

    @property
    def writes_model_registry(self) -> bool:
        return False

    @property
    def deploys_model(self) -> bool:
        return False

    @property
    def changes_runtime_weight(self) -> bool:
        return False

    @property
    def creates_order(self) -> bool:
        return False


__all__ = [
    "DEFAULT_DECISION_QUALITY_POLICY",
    "DecisionQualityAssessment",
    "DecisionQualityContractError",
    "DecisionQualityEvidence",
    "DecisionQualityPolicy",
    "DecisionQualityState",
    "ResearchLicenseStatus",
]
