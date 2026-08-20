"""Shadow validation and strategy lifecycle recommendations.

Lifecycle assessment consumes immutable decision-quality and scoreboard evidence.
It recommends a state but never mutates runtime weights, deploys a model, uses a
frozen holdout as a recurring shadow sample, or creates an order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from ..core.outcomes import ScoreboardState, StrategyScoreboard
from ..core.time import to_utc
from ..data.bar_artifact import DataTrustTier
from .decision_quality import DecisionQualityAssessment, DecisionQualityState

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal(0)
_TRUST_RANK = {
    DataTrustTier.UNKNOWN: 0,
    DataTrustTier.BEST_EFFORT: 1,
    DataTrustTier.OPERATIONAL_VERIFIED: 2,
    DataTrustTier.RESEARCH_GRADE: 3,
    DataTrustTier.FROZEN_HOLDOUT: 4,
}


class ShadowLifecycleContractError(ValueError):
    """Raised when shadow or lifecycle evidence is malformed."""


class StrategyLifecycleState(StrEnum):
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    DOWNWEIGHTED = "DOWNWEIGHTED"
    BLOCKED = "BLOCKED"
    RETIRED = "RETIRED"


class ShadowEvidenceState(StrEnum):
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    BLOCKED = "BLOCKED"
    FORMAL_READY = "FORMAL_READY"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ShadowLifecycleContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ShadowLifecycleContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ShadowLifecycleContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShadowLifecycleContractError(
            f"{name} must be a non-negative integer"
        )
    return value


def _require_positive_int(value: object, name: str) -> int:
    result = _require_nonnegative_int(value, name)
    if result == 0:
        raise ShadowLifecycleContractError(f"{name} must be positive")
    return result


def _require_decimal(
    value: object,
    name: str,
    *,
    lower: Decimal | None = None,
) -> Decimal:
    if type(value) is not Decimal:
        raise ShadowLifecycleContractError(
            f"{name} must be Decimal; float, integer and boolean are forbidden"
        )
    if not value.is_finite():
        raise ShadowLifecycleContractError(f"{name} must be finite")
    if lower is not None and value < lower:
        raise ShadowLifecycleContractError(f"{name} is below its lower bound")
    return value


@dataclass(frozen=True, slots=True)
class ShadowLifecyclePolicy:
    policy_version: str
    minimum_shadow_samples: int = 20
    watch_expectancy_floor_r: Decimal = Decimal("0.00")
    downweight_expectancy_floor_r: Decimal = Decimal("-0.15")
    block_expectancy_floor_r: Decimal = Decimal("-0.30")
    maximum_ece_regression: Decimal = Decimal("0.03")
    maximum_drawdown_regression_r: Decimal = Decimal("0.50")
    maximum_average_r_regression: Decimal = Decimal("0.25")
    retire_after_blocked_windows: int = 3
    minimum_data_trust: DataTrustTier = DataTrustTier.OPERATIONAL_VERIFIED
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        _require_positive_int(self.minimum_shadow_samples, "minimum_shadow_samples")
        for name in (
            "watch_expectancy_floor_r",
            "downweight_expectancy_floor_r",
            "block_expectancy_floor_r",
            "maximum_ece_regression",
            "maximum_drawdown_regression_r",
            "maximum_average_r_regression",
        ):
            _require_decimal(getattr(self, name), name)
        if not (
            self.block_expectancy_floor_r
            <= self.downweight_expectancy_floor_r
            <= self.watch_expectancy_floor_r
        ):
            raise ShadowLifecycleContractError(
                "expectancy lifecycle thresholds must be monotonic"
            )
        for name in (
            "maximum_ece_regression",
            "maximum_drawdown_regression_r",
            "maximum_average_r_regression",
        ):
            if getattr(self, name) < _ZERO:
                raise ShadowLifecycleContractError(
                    f"{name} must be non-negative"
                )
        _require_positive_int(
            self.retire_after_blocked_windows,
            "retire_after_blocked_windows",
        )
        if not isinstance(self.minimum_data_trust, DataTrustTier):
            raise ShadowLifecycleContractError(
                "minimum_data_trust must be DataTrustTier"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "shadow-lifecycle-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_shadow_samples": self.minimum_shadow_samples,
                    "watch_expectancy_floor_r": self.watch_expectancy_floor_r,
                    "downweight_expectancy_floor_r": (
                        self.downweight_expectancy_floor_r
                    ),
                    "block_expectancy_floor_r": self.block_expectancy_floor_r,
                    "maximum_ece_regression": self.maximum_ece_regression,
                    "maximum_drawdown_regression_r": (
                        self.maximum_drawdown_regression_r
                    ),
                    "maximum_average_r_regression": (
                        self.maximum_average_r_regression
                    ),
                    "retire_after_blocked_windows": (
                        self.retire_after_blocked_windows
                    ),
                    "minimum_data_trust": self.minimum_data_trust,
                }
            ),
        )


DEFAULT_SHADOW_LIFECYCLE_POLICY = ShadowLifecyclePolicy(
    policy_version="shadow-lifecycle-v1"
)


@dataclass(frozen=True, slots=True)
class ShadowValidationEvidence:
    strategy_id: str
    strategy_version: str
    model_id: str
    market: Market
    horizon_sessions: int
    decision_quality_assessment_id: str
    long_scoreboard_id: str
    recent_scoreboard_id: str
    shadow_snapshot_id: str
    shadow_run_id: str
    data_trust_tier: DataTrustTier
    sample_count: int
    calibration_ece_delta: Decimal
    regime_expectancy_range: Decimal
    max_drawdown_delta_r: Decimal
    out_of_sample: bool
    production_weight_zero: bool
    used_frozen_holdout: bool
    orders_created: bool
    verified: bool
    complete: bool
    synthetic_fixture_only: bool
    verification_evidence_ids: tuple[str, ...]
    blockers: tuple[str, ...] = field(init=False)
    state: ShadowEvidenceState = field(init=False)
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("strategy_id", "strategy_version", "model_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.market, Market):
            raise ShadowLifecycleContractError("market must be Market")
        _require_positive_int(self.horizon_sessions, "horizon_sessions")
        for name in (
            "decision_quality_assessment_id",
            "long_scoreboard_id",
            "recent_scoreboard_id",
            "shadow_snapshot_id",
            "shadow_run_id",
        ):
            _require_sha256(getattr(self, name), name)
        if not isinstance(self.data_trust_tier, DataTrustTier):
            raise ShadowLifecycleContractError(
                "data_trust_tier must be DataTrustTier"
            )
        _require_nonnegative_int(self.sample_count, "sample_count")
        _require_decimal(self.calibration_ece_delta, "calibration_ece_delta")
        _require_decimal(
            self.regime_expectancy_range,
            "regime_expectancy_range",
            lower=_ZERO,
        )
        _require_decimal(self.max_drawdown_delta_r, "max_drawdown_delta_r")
        for name in (
            "out_of_sample",
            "production_weight_zero",
            "used_frozen_holdout",
            "orders_created",
            "verified",
            "complete",
            "synthetic_fixture_only",
        ):
            _require_bool(getattr(self, name), name)
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.verification_evidence_ids
        ):
            raise ShadowLifecycleContractError(
                "verification_evidence_ids must contain lowercase SHA-256"
            )
        if self.verification_evidence_ids != tuple(
            sorted(set(self.verification_evidence_ids))
        ):
            raise ShadowLifecycleContractError(
                "verification_evidence_ids must be sorted and unique"
            )
        if self.verified and not self.verification_evidence_ids:
            raise ShadowLifecycleContractError(
                "verified shadow evidence requires verification IDs"
            )
        if not self.verified and self.verification_evidence_ids:
            raise ShadowLifecycleContractError(
                "unverified shadow evidence cannot carry verification IDs"
            )
        if self.synthetic_fixture_only:
            if self.verified or self.data_trust_tier is not DataTrustTier.BEST_EFFORT:
                raise ShadowLifecycleContractError(
                    "synthetic shadow evidence must remain unverified BEST_EFFORT"
                )
        elif (
            _TRUST_RANK[self.data_trust_tier]
            >= _TRUST_RANK[DataTrustTier.OPERATIONAL_VERIFIED]
            and not self.verified
        ):
            raise ShadowLifecycleContractError(
                "operational-or-higher shadow evidence must be verified"
            )

        blockers: set[str] = set()
        if not self.out_of_sample:
            blockers.add("SHADOW_SAMPLE_NOT_OUT_OF_SAMPLE")
        if not self.production_weight_zero:
            blockers.add("SHADOW_PRODUCTION_WEIGHT_NOT_ZERO")
        if self.used_frozen_holdout:
            blockers.add("FROZEN_HOLDOUT_REUSED_AS_SHADOW")
        if self.orders_created:
            blockers.add("SHADOW_CREATED_ORDERS")
        if not self.complete:
            blockers.add("SHADOW_EVIDENCE_INCOMPLETE")
        if not self.verified:
            blockers.add("SHADOW_EVIDENCE_NOT_VERIFIED")
        blocker_tuple = tuple(sorted(blockers))
        if self.synthetic_fixture_only:
            state = ShadowEvidenceState.DIAGNOSTIC_ONLY
        elif blocker_tuple:
            state = ShadowEvidenceState.BLOCKED
        else:
            state = ShadowEvidenceState.FORMAL_READY
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "evidence_id",
            fingerprint(
                {
                    "schema": "shadow-validation-evidence-v1",
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "model_id": self.model_id,
                    "market": self.market,
                    "horizon_sessions": self.horizon_sessions,
                    "decision_quality_assessment_id": (
                        self.decision_quality_assessment_id
                    ),
                    "long_scoreboard_id": self.long_scoreboard_id,
                    "recent_scoreboard_id": self.recent_scoreboard_id,
                    "shadow_snapshot_id": self.shadow_snapshot_id,
                    "shadow_run_id": self.shadow_run_id,
                    "data_trust_tier": self.data_trust_tier,
                    "sample_count": self.sample_count,
                    "calibration_ece_delta": self.calibration_ece_delta,
                    "regime_expectancy_range": self.regime_expectancy_range,
                    "max_drawdown_delta_r": self.max_drawdown_delta_r,
                    "out_of_sample": self.out_of_sample,
                    "production_weight_zero": self.production_weight_zero,
                    "used_frozen_holdout": self.used_frozen_holdout,
                    "orders_created": self.orders_created,
                    "verified": self.verified,
                    "complete": self.complete,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "verification_evidence_ids": list(
                        self.verification_evidence_ids
                    ),
                    "blockers": blocker_tuple,
                    "state": state,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class StrategyLifecycleAssessment:
    current_state: StrategyLifecycleState
    decision_quality: DecisionQualityAssessment
    long_term_scoreboard: StrategyScoreboard
    recent_scoreboard: StrategyScoreboard
    shadow_evidence: ShadowValidationEvidence
    consecutive_blocked_windows: int = 0
    policy: ShadowLifecyclePolicy = DEFAULT_SHADOW_LIFECYCLE_POLICY
    structural_blockers: tuple[str, ...] = field(init=False)
    reasons: tuple[str, ...] = field(init=False)
    recommended_state: StrategyLifecycleState = field(init=False)
    assessment_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.current_state, StrategyLifecycleState):
            raise ShadowLifecycleContractError(
                "current_state must be StrategyLifecycleState"
            )
        if not isinstance(self.decision_quality, DecisionQualityAssessment):
            raise ShadowLifecycleContractError(
                "decision_quality must be DecisionQualityAssessment"
            )
        if not isinstance(self.long_term_scoreboard, StrategyScoreboard) or not isinstance(
            self.recent_scoreboard,
            StrategyScoreboard,
        ):
            raise ShadowLifecycleContractError(
                "scoreboards must be StrategyScoreboard values"
            )
        if not isinstance(self.shadow_evidence, ShadowValidationEvidence):
            raise ShadowLifecycleContractError(
                "shadow_evidence must be ShadowValidationEvidence"
            )
        _require_nonnegative_int(
            self.consecutive_blocked_windows,
            "consecutive_blocked_windows",
        )
        if not isinstance(self.policy, ShadowLifecyclePolicy):
            raise ShadowLifecycleContractError(
                "policy must be ShadowLifecyclePolicy"
            )
        structural: set[str] = set()
        evidence = self.shadow_evidence
        if evidence.decision_quality_assessment_id != self.decision_quality.assessment_id:
            structural.add("DECISION_QUALITY_ID_MISMATCH")
        if evidence.long_scoreboard_id != self.long_term_scoreboard.scoreboard_id:
            structural.add("LONG_SCOREBOARD_ID_MISMATCH")
        if evidence.recent_scoreboard_id != self.recent_scoreboard.scoreboard_id:
            structural.add("RECENT_SCOREBOARD_ID_MISMATCH")
        for name in (
            "strategy_id",
            "strategy_version",
            "model_id",
            "market",
            "horizon_sessions",
            "evidence_tier",
        ):
            if getattr(self.long_term_scoreboard, name) != getattr(
                self.recent_scoreboard,
                name,
            ):
                structural.add(f"SCOREBOARD_{name.upper()}_MISMATCH")
        if (
            self.long_term_scoreboard.policy.policy_id
            != self.recent_scoreboard.policy.policy_id
        ):
            structural.add("SCOREBOARD_POLICY_MISMATCH")
        if to_utc(self.long_term_scoreboard.as_of) != to_utc(
            self.recent_scoreboard.as_of
        ):
            structural.add("SCOREBOARD_AS_OF_MISMATCH")
        if to_utc(self.long_term_scoreboard.window_end) != to_utc(
            self.recent_scoreboard.window_end
        ):
            structural.add("SCOREBOARD_WINDOW_END_MISMATCH")
        if not (
            to_utc(self.long_term_scoreboard.window_start)
            < to_utc(self.recent_scoreboard.window_start)
            <= to_utc(self.recent_scoreboard.window_end)
        ):
            structural.add("RECENT_WINDOW_NOT_STRICT_SUBWINDOW")
        if evidence.strategy_id != self.long_term_scoreboard.strategy_id:
            structural.add("SHADOW_STRATEGY_ID_MISMATCH")
        if evidence.strategy_version != self.long_term_scoreboard.strategy_version:
            structural.add("SHADOW_STRATEGY_VERSION_MISMATCH")
        if evidence.model_id != self.long_term_scoreboard.model_id:
            structural.add("SHADOW_MODEL_ID_MISMATCH")
        if evidence.market is not self.long_term_scoreboard.market:
            structural.add("SHADOW_MARKET_MISMATCH")
        if evidence.horizon_sessions != self.long_term_scoreboard.horizon_sessions:
            structural.add("SHADOW_HORIZON_MISMATCH")
        if self.recent_scoreboard.metrics is not None and (
            evidence.sample_count != self.recent_scoreboard.metrics.sample_count
        ):
            structural.add("SHADOW_SAMPLE_COUNT_MISMATCH")

        reasons: set[str] = set()
        structural_tuple = tuple(sorted(structural))
        if self.current_state is StrategyLifecycleState.RETIRED:
            recommended = StrategyLifecycleState.RETIRED
            reasons.add("RETIRED_STATE_IS_TERMINAL")
        elif structural_tuple:
            recommended = StrategyLifecycleState.BLOCKED
            reasons.add("STRUCTURAL_EVIDENCE_MISMATCH")
        elif evidence.synthetic_fixture_only:
            recommended = StrategyLifecycleState.SHADOW
            reasons.add("SYNTHETIC_SHADOW_DIAGNOSTIC_ONLY")
        elif evidence.state is not ShadowEvidenceState.FORMAL_READY:
            recommended = StrategyLifecycleState.BLOCKED
            reasons.update(evidence.blockers)
        elif (
            _TRUST_RANK[evidence.data_trust_tier]
            < _TRUST_RANK[self.policy.minimum_data_trust]
        ):
            recommended = StrategyLifecycleState.BLOCKED
            reasons.add("SHADOW_TRUST_TIER_INSUFFICIENT")
        elif self.decision_quality.state is not DecisionQualityState.PROMOTION_ELIGIBLE:
            recommended = (
                StrategyLifecycleState.SHADOW
                if self.current_state is StrategyLifecycleState.SHADOW
                else StrategyLifecycleState.BLOCKED
            )
            reasons.add("DECISION_QUALITY_NOT_PROMOTION_ELIGIBLE")
        elif (
            self.long_term_scoreboard.state
            is not ScoreboardState.REAL_EVIDENCE_AVAILABLE
            or self.recent_scoreboard.state
            is not ScoreboardState.REAL_EVIDENCE_AVAILABLE
            or self.long_term_scoreboard.metrics is None
            or self.recent_scoreboard.metrics is None
        ):
            recommended = (
                StrategyLifecycleState.SHADOW
                if self.current_state is StrategyLifecycleState.SHADOW
                else StrategyLifecycleState.WATCH
            )
            reasons.add("REAL_SCOREBOARD_EVIDENCE_INSUFFICIENT")
        elif evidence.sample_count < self.policy.minimum_shadow_samples:
            recommended = StrategyLifecycleState.SHADOW
            reasons.add("SHADOW_SAMPLE_COUNT_BELOW_MINIMUM")
        else:
            assert self.long_term_scoreboard.metrics is not None
            assert self.recent_scoreboard.metrics is not None
            recent_expectancy = self.recent_scoreboard.metrics.net_expectancy_r
            average_regression = (
                self.long_term_scoreboard.metrics.average_r
                - self.recent_scoreboard.metrics.average_r
            )
            severe = (
                recent_expectancy <= self.policy.block_expectancy_floor_r
                or evidence.calibration_ece_delta
                > self.policy.maximum_ece_regression
                or evidence.max_drawdown_delta_r
                > self.policy.maximum_drawdown_regression_r
            )
            if (
                severe
                and self.current_state is StrategyLifecycleState.BLOCKED
                and self.consecutive_blocked_windows
                >= self.policy.retire_after_blocked_windows
            ):
                recommended = StrategyLifecycleState.RETIRED
                reasons.add("REPEATED_BLOCKED_WINDOWS")
            elif severe:
                recommended = StrategyLifecycleState.BLOCKED
                if recent_expectancy <= self.policy.block_expectancy_floor_r:
                    reasons.add("RECENT_EXPECTANCY_SEVERELY_NEGATIVE")
                if (
                    evidence.calibration_ece_delta
                    > self.policy.maximum_ece_regression
                ):
                    reasons.add("CALIBRATION_REGRESSED")
                if (
                    evidence.max_drawdown_delta_r
                    > self.policy.maximum_drawdown_regression_r
                ):
                    reasons.add("MAX_DRAWDOWN_REGRESSED")
            elif recent_expectancy <= self.policy.downweight_expectancy_floor_r:
                recommended = StrategyLifecycleState.DOWNWEIGHTED
                reasons.add("RECENT_EXPECTANCY_NEGATIVE")
            elif (
                recent_expectancy <= self.policy.watch_expectancy_floor_r
                or average_regression > self.policy.maximum_average_r_regression
            ):
                recommended = StrategyLifecycleState.WATCH
                if recent_expectancy <= self.policy.watch_expectancy_floor_r:
                    reasons.add("RECENT_EXPECTANCY_WEAK")
                if average_regression > self.policy.maximum_average_r_regression:
                    reasons.add("RECENT_AVERAGE_R_REGRESSED")
            else:
                recommended = StrategyLifecycleState.ACTIVE
                reasons.add("REAL_SHADOW_AND_DECISION_QUALITY_GATES_PASSED")
        reason_tuple = tuple(sorted(reasons))
        object.__setattr__(self, "structural_blockers", structural_tuple)
        object.__setattr__(self, "reasons", reason_tuple)
        object.__setattr__(self, "recommended_state", recommended)
        object.__setattr__(
            self,
            "assessment_id",
            fingerprint(
                {
                    "schema": "strategy-lifecycle-assessment-v1",
                    "current_state": self.current_state,
                    "decision_quality_id": self.decision_quality.assessment_id,
                    "long_scoreboard_id": self.long_term_scoreboard.scoreboard_id,
                    "recent_scoreboard_id": self.recent_scoreboard.scoreboard_id,
                    "shadow_evidence_id": self.shadow_evidence.evidence_id,
                    "consecutive_blocked_windows": self.consecutive_blocked_windows,
                    "policy_id": self.policy.policy_id,
                    "structural_blockers": structural_tuple,
                    "reasons": reason_tuple,
                    "recommended_state": recommended,
                    "changes_runtime_state": False,
                    "changes_runtime_weight": False,
                    "deploys_model": False,
                    "creates_order": False,
                }
            ),
        )

    @property
    def changes_runtime_state(self) -> bool:
        return False

    @property
    def changes_runtime_weight(self) -> bool:
        return False

    @property
    def deploys_model(self) -> bool:
        return False

    @property
    def creates_order(self) -> bool:
        return False


__all__ = [
    "DEFAULT_SHADOW_LIFECYCLE_POLICY",
    "ShadowEvidenceState",
    "ShadowLifecycleContractError",
    "ShadowLifecyclePolicy",
    "ShadowValidationEvidence",
    "StrategyLifecycleAssessment",
    "StrategyLifecycleState",
]
