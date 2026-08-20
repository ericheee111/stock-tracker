"""Deterministic Big Trend v1 research contract.

The evaluator combines independent evidence families. It intentionally cannot
turn one breakout score, one event, or one model probability into a formal
"main uptrend" state. Outputs are research assessments only and do not contain
orders, position sizes, success probabilities, or performance claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum

from stock_tracker.core.types import Market

from .fingerprint import fingerprint
from .time import ensure_aware, exchange_local_date, to_utc

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_ZERO = Decimal(0)
_ONE = Decimal(1)
_NEG_ONE = Decimal(-1)


class BigTrendContractError(ValueError):
    """Raised when Big Trend evidence is incomplete, ambiguous, or unsafe."""


class BigTrendState(StrEnum):
    NONE = "NONE"
    EMERGING = "EMERGING"
    CONFIRMING = "CONFIRMING"
    TRENDING = "TRENDING"
    MATURE = "MATURE"
    DISTRIBUTING = "DISTRIBUTING"
    BROKEN = "BROKEN"


class BigTrendScope(StrEnum):
    SECTOR = "SECTOR"
    INSTRUMENT = "INSTRUMENT"


class BigTrendActionability(StrEnum):
    DATA_BLOCKED = "DATA_BLOCKED"
    NO_ACTION = "NO_ACTION"
    WATCH_ONLY = "WATCH_ONLY"
    PLAN_ELIGIBLE = "PLAN_ELIGIBLE"
    HOLD_NO_CHASE = "HOLD_NO_CHASE"
    WARNING_TRIM = "WARNING_TRIM"
    CLOSE_RUNNER = "CLOSE_RUNNER"


class BigTrendEvidenceFamily(StrEnum):
    SECTOR_RS_PERSISTENCE = "SECTOR_RS_PERSISTENCE"
    BREADTH_EXPANSION = "BREADTH_EXPANSION"
    TURNOVER_SHARE_TREND = "TURNOVER_SHARE_TREND"
    LEADER_STRENGTH = "LEADER_STRENGTH"
    CORE_STABILITY = "CORE_STABILITY"
    FOLLOWER_DIFFUSION = "FOLLOWER_DIFFUSION"
    EQUITY_VS_SECTOR_RS = "EQUITY_VS_SECTOR_RS"
    TREND_QUALITY = "TREND_QUALITY"
    BREAKOUT_RETENTION = "BREAKOUT_RETENTION"
    PULLBACK_QUALITY = "PULLBACK_QUALITY"
    CATALYST_CONFIRMATION = "CATALYST_CONFIRMATION"
    REGIME_FIT = "REGIME_FIT"
    CROWDING_ACCELERATION = "CROWDING_ACCELERATION"
    DISTRIBUTION_DIVERGENCE = "DISTRIBUTION_DIVERGENCE"


_PARTICIPATION_FAMILIES = frozenset(
    {
        BigTrendEvidenceFamily.SECTOR_RS_PERSISTENCE,
        BigTrendEvidenceFamily.BREADTH_EXPANSION,
        BigTrendEvidenceFamily.TURNOVER_SHARE_TREND,
        BigTrendEvidenceFamily.LEADER_STRENGTH,
        BigTrendEvidenceFamily.CORE_STABILITY,
        BigTrendEvidenceFamily.FOLLOWER_DIFFUSION,
        BigTrendEvidenceFamily.EQUITY_VS_SECTOR_RS,
    }
)
_STRUCTURE_FAMILIES = frozenset(
    {
        BigTrendEvidenceFamily.TREND_QUALITY,
        BigTrendEvidenceFamily.BREAKOUT_RETENTION,
        BigTrendEvidenceFamily.PULLBACK_QUALITY,
    }
)
_CONTEXT_FAMILIES = frozenset(
    {
        BigTrendEvidenceFamily.CATALYST_CONFIRMATION,
        BigTrendEvidenceFamily.REGIME_FIT,
    }
)
_RISK_FAMILIES = frozenset(
    {
        BigTrendEvidenceFamily.CROWDING_ACCELERATION,
        BigTrendEvidenceFamily.DISTRIBUTION_DIVERGENCE,
    }
)
_SUPPORT_FAMILIES = frozenset(BigTrendEvidenceFamily) - _RISK_FAMILIES


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BigTrendContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise BigTrendContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise BigTrendContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, name)


def _require_score(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise BigTrendContractError(
            f"{name} must be Decimal; floats, integers and booleans are forbidden"
        )
    if not value.is_finite() or not _NEG_ONE <= value <= _ONE:
        raise BigTrendContractError(f"{name} must be finite and within [-1, 1]")
    return value


def _require_unit_interval(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise BigTrendContractError(
            f"{name} must be Decimal; floats, integers and booleans are forbidden"
        )
    if not value.is_finite() or not _ZERO <= value <= _ONE:
        raise BigTrendContractError(f"{name} must be finite and within [0, 1]")
    return value


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return _ZERO
    with localcontext(_CONTEXT):
        return +(sum(values, _ZERO) / Decimal(len(values)))


@dataclass(frozen=True, slots=True)
class BigTrendEvidencePoint:
    family: BigTrendEvidenceFamily
    score: Decimal
    observed_at: datetime
    source_snapshot_id: str
    note: str
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.family, BigTrendEvidenceFamily):
            raise BigTrendContractError(
                "family must be BigTrendEvidenceFamily"
            )
        _require_score(self.score, "score")
        ensure_aware(self.observed_at, "observed_at")
        _require_sha256(self.source_snapshot_id, "source_snapshot_id")
        _require_text(self.note, "note")
        object.__setattr__(
            self,
            "evidence_id",
            fingerprint(
                {
                    "schema": "big-trend-evidence-point-v1",
                    "family": self.family,
                    "score": self.score,
                    "observed_at": to_utc(self.observed_at),
                    "source_snapshot_id": self.source_snapshot_id,
                    "note": self.note,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class BigTrendInputSnapshot:
    scope: BigTrendScope
    entity_id: str
    market: Market
    session_date: date
    as_of: datetime
    identity_fact_id: str | None
    taxonomy_id: str | None
    classification_id: str | None
    calendar_snapshot_id: str
    universe_snapshot_id: str
    classification_snapshot_id: str
    raw_bar_snapshot_id: str
    feature_snapshot_id: str
    regime_snapshot_id: str
    event_snapshot_id: str | None
    evidence: tuple[BigTrendEvidencePoint, ...]
    data_quality_blockers: tuple[str, ...] = ()
    tradability_blockers: tuple[str, ...] = ()
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, BigTrendScope):
            raise BigTrendContractError("scope must be BigTrendScope")
        _require_text(self.entity_id, "entity_id")
        if not isinstance(self.market, Market):
            raise BigTrendContractError("market must be Market")
        ensure_aware(self.as_of, "as_of")
        if self.session_date > exchange_local_date(self.as_of, self.market):
            raise BigTrendContractError(
                "session_date cannot follow the as_of exchange date"
            )
        if self.scope is BigTrendScope.INSTRUMENT:
            _require_sha256(self.identity_fact_id, "identity_fact_id")
            if self.taxonomy_id is not None or self.classification_id is not None:
                raise BigTrendContractError(
                    "instrument scope cannot self-assert classification identity"
                )
        else:
            if self.identity_fact_id is not None:
                raise BigTrendContractError(
                    "sector scope cannot carry instrument identity_fact_id"
                )
            _require_text(self.taxonomy_id, "taxonomy_id")
            _require_text(self.classification_id, "classification_id")
            if self.entity_id != f"{self.taxonomy_id}:{self.classification_id}":
                raise BigTrendContractError(
                    "sector entity_id must be taxonomy_id:classification_id"
                )
        for name in (
            "calendar_snapshot_id",
            "universe_snapshot_id",
            "classification_snapshot_id",
            "raw_bar_snapshot_id",
            "feature_snapshot_id",
            "regime_snapshot_id",
        ):
            _require_sha256(getattr(self, name), name)
        _require_optional_sha256(self.event_snapshot_id, "event_snapshot_id")
        if any(
            not isinstance(item, BigTrendEvidencePoint) for item in self.evidence
        ):
            raise BigTrendContractError(
                "evidence must contain BigTrendEvidencePoint values"
            )
        order = tuple((item.family.value, item.evidence_id) for item in self.evidence)
        if order != tuple(sorted(order)):
            raise BigTrendContractError(
                "evidence must be deterministically sorted by family"
            )
        families = tuple(item.family for item in self.evidence)
        if len(set(families)) != len(families):
            raise BigTrendContractError(
                "only one evidence point per family is allowed"
            )
        if any(to_utc(item.observed_at) > to_utc(self.as_of) for item in self.evidence):
            raise BigTrendContractError("future evidence entered Big Trend snapshot")
        for name in ("data_quality_blockers", "tradability_blockers"):
            value = getattr(self, name)
            if value != tuple(sorted(set(value))):
                raise BigTrendContractError(f"{name} must be sorted and unique")
            for blocker in value:
                _require_text(blocker, f"{name} item")
        object.__setattr__(
            self,
            "snapshot_id",
            fingerprint(
                {
                    "schema": "big-trend-input-snapshot-v1",
                    "scope": self.scope,
                    "entity_id": self.entity_id,
                    "market": self.market,
                    "session_date": self.session_date,
                    "as_of": to_utc(self.as_of),
                    "identity_fact_id": self.identity_fact_id,
                    "taxonomy_id": self.taxonomy_id,
                    "classification_id": self.classification_id,
                    "calendar_snapshot_id": self.calendar_snapshot_id,
                    "universe_snapshot_id": self.universe_snapshot_id,
                    "classification_snapshot_id": self.classification_snapshot_id,
                    "raw_bar_snapshot_id": self.raw_bar_snapshot_id,
                    "feature_snapshot_id": self.feature_snapshot_id,
                    "regime_snapshot_id": self.regime_snapshot_id,
                    "event_snapshot_id": self.event_snapshot_id,
                    "evidence_ids": [item.evidence_id for item in self.evidence],
                    "data_quality_blockers": self.data_quality_blockers,
                    "tradability_blockers": self.tradability_blockers,
                }
            ),
        )

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.data_quality_blockers + self.tradability_blockers))
        )


@dataclass(frozen=True, slots=True)
class BigTrendPolicy:
    policy_version: str
    support_threshold: Decimal
    emerging_score: Decimal
    confirming_score: Decimal
    trending_score: Decimal
    emerging_min_families: int
    confirming_min_families: int
    trending_min_families: int
    confirming_min_groups: int
    trending_min_groups: int
    mature_crowding_threshold: Decimal
    distribution_threshold: Decimal
    broken_structure_threshold: Decimal
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        for name in (
            "support_threshold",
            "emerging_score",
            "confirming_score",
            "trending_score",
            "mature_crowding_threshold",
            "distribution_threshold",
        ):
            _require_unit_interval(getattr(self, name), name)
        _require_score(
            self.broken_structure_threshold,
            "broken_structure_threshold",
        )
        if not (
            self.emerging_score <= self.confirming_score <= self.trending_score
        ):
            raise BigTrendContractError(
                "state score thresholds must be monotonic"
            )
        for name in (
            "emerging_min_families",
            "confirming_min_families",
            "trending_min_families",
            "confirming_min_groups",
            "trending_min_groups",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BigTrendContractError(f"{name} must be a positive integer")
        if not (
            self.emerging_min_families
            <= self.confirming_min_families
            <= self.trending_min_families
        ):
            raise BigTrendContractError(
                "family-count thresholds must be monotonic"
            )
        if self.confirming_min_groups > 3 or self.trending_min_groups > 3:
            raise BigTrendContractError("group thresholds cannot exceed three")
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "big-trend-policy-v1",
                    "policy_version": self.policy_version,
                    "support_threshold": self.support_threshold,
                    "emerging_score": self.emerging_score,
                    "confirming_score": self.confirming_score,
                    "trending_score": self.trending_score,
                    "emerging_min_families": self.emerging_min_families,
                    "confirming_min_families": self.confirming_min_families,
                    "trending_min_families": self.trending_min_families,
                    "confirming_min_groups": self.confirming_min_groups,
                    "trending_min_groups": self.trending_min_groups,
                    "mature_crowding_threshold": self.mature_crowding_threshold,
                    "distribution_threshold": self.distribution_threshold,
                    "broken_structure_threshold": self.broken_structure_threshold,
                }
            ),
        )


DEFAULT_BIG_TREND_POLICY = BigTrendPolicy(
    policy_version="big-trend-v1",
    support_threshold=Decimal("0.35"),
    emerging_score=Decimal("0.30"),
    confirming_score=Decimal("0.48"),
    trending_score=Decimal("0.62"),
    emerging_min_families=3,
    confirming_min_families=5,
    trending_min_families=7,
    confirming_min_groups=2,
    trending_min_groups=2,
    mature_crowding_threshold=Decimal("0.72"),
    distribution_threshold=Decimal("0.65"),
    broken_structure_threshold=Decimal("-0.45"),
)


@dataclass(frozen=True, slots=True)
class BigTrendAssessment:
    input_snapshot_id: str
    policy_id: str
    state: BigTrendState
    actionability: BigTrendActionability
    confirmation_score: Decimal
    supportive_family_count: int
    supportive_group_count: int
    maturity_score: Decimal
    distribution_score: Decimal
    reasons: tuple[str, ...]
    assessment_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.input_snapshot_id, "input_snapshot_id")
        _require_sha256(self.policy_id, "policy_id")
        if not isinstance(self.state, BigTrendState):
            raise BigTrendContractError("state must be BigTrendState")
        if not isinstance(self.actionability, BigTrendActionability):
            raise BigTrendContractError(
                "actionability must be BigTrendActionability"
            )
        _require_score(self.confirmation_score, "confirmation_score")
        _require_unit_interval(self.maturity_score, "maturity_score")
        _require_unit_interval(self.distribution_score, "distribution_score")
        for name in ("supportive_family_count", "supportive_group_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BigTrendContractError(f"{name} must be a non-negative integer")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise BigTrendContractError("reasons must be sorted and unique")
        expected_actionability = {
            BigTrendState.NONE: BigTrendActionability.NO_ACTION,
            BigTrendState.EMERGING: BigTrendActionability.WATCH_ONLY,
            BigTrendState.CONFIRMING: BigTrendActionability.PLAN_ELIGIBLE,
            BigTrendState.TRENDING: BigTrendActionability.PLAN_ELIGIBLE,
            BigTrendState.MATURE: BigTrendActionability.HOLD_NO_CHASE,
            BigTrendState.DISTRIBUTING: BigTrendActionability.WARNING_TRIM,
            BigTrendState.BROKEN: BigTrendActionability.CLOSE_RUNNER,
        }
        if self.actionability is not BigTrendActionability.DATA_BLOCKED and (
            self.actionability is not expected_actionability[self.state]
        ):
            raise BigTrendContractError(
                "actionability is inconsistent with Big Trend state"
            )
        if self.actionability is BigTrendActionability.DATA_BLOCKED and (
            self.state is not BigTrendState.NONE
        ):
            raise BigTrendContractError("DATA_BLOCKED assessment must use NONE state")
        object.__setattr__(
            self,
            "assessment_id",
            fingerprint(
                {
                    "schema": "big-trend-assessment-v1",
                    "input_snapshot_id": self.input_snapshot_id,
                    "policy_id": self.policy_id,
                    "state": self.state,
                    "actionability": self.actionability,
                    "confirmation_score": self.confirmation_score,
                    "supportive_family_count": self.supportive_family_count,
                    "supportive_group_count": self.supportive_group_count,
                    "maturity_score": self.maturity_score,
                    "distribution_score": self.distribution_score,
                    "reasons": self.reasons,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class BigTrendTransition:
    previous_assessment_id: str
    previous_state: BigTrendState
    current_assessment_id: str
    current_state: BigTrendState
    transition_reason: str
    transition_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.previous_assessment_id, "previous_assessment_id")
        _require_sha256(self.current_assessment_id, "current_assessment_id")
        if not isinstance(self.previous_state, BigTrendState):
            raise BigTrendContractError("previous_state must be BigTrendState")
        if not isinstance(self.current_state, BigTrendState):
            raise BigTrendContractError("current_state must be BigTrendState")
        _require_text(self.transition_reason, "transition_reason")
        object.__setattr__(
            self,
            "transition_id",
            fingerprint(
                {
                    "schema": "big-trend-transition-v1",
                    "previous_assessment_id": self.previous_assessment_id,
                    "previous_state": self.previous_state,
                    "current_assessment_id": self.current_assessment_id,
                    "current_state": self.current_state,
                    "transition_reason": self.transition_reason,
                }
            ),
        )


def _supportive_groups(
    supportive: set[BigTrendEvidenceFamily],
) -> int:
    return sum(
        bool(supportive & group)
        for group in (
            _PARTICIPATION_FAMILIES,
            _STRUCTURE_FAMILIES,
            _CONTEXT_FAMILIES,
        )
    )


def assess_big_trend(
    snapshot: BigTrendInputSnapshot,
    policy: BigTrendPolicy = DEFAULT_BIG_TREND_POLICY,
) -> BigTrendAssessment:
    """Evaluate one immutable evidence snapshot under a versioned policy."""

    if not isinstance(snapshot, BigTrendInputSnapshot):
        raise BigTrendContractError("snapshot must be BigTrendInputSnapshot")
    if not isinstance(policy, BigTrendPolicy):
        raise BigTrendContractError("policy must be BigTrendPolicy")
    if snapshot.blockers:
        return BigTrendAssessment(
            input_snapshot_id=snapshot.snapshot_id,
            policy_id=policy.policy_id,
            state=BigTrendState.NONE,
            actionability=BigTrendActionability.DATA_BLOCKED,
            confirmation_score=_ZERO,
            supportive_family_count=0,
            supportive_group_count=0,
            maturity_score=_ZERO,
            distribution_score=_ZERO,
            reasons=tuple(f"BLOCKED:{item}" for item in snapshot.blockers),
        )

    by_family = {item.family: item.score for item in snapshot.evidence}
    support_scores = tuple(
        by_family[family]
        for family in sorted(_SUPPORT_FAMILIES, key=lambda item: item.value)
        if family in by_family
    )
    confirmation_score = _mean(support_scores)
    supportive = {
        family
        for family, score in by_family.items()
        if family in _SUPPORT_FAMILIES and score >= policy.support_threshold
    }
    supportive_family_count = len(supportive)
    supportive_group_count = _supportive_groups(supportive)
    maturity_score = max(
        _ZERO,
        by_family.get(BigTrendEvidenceFamily.CROWDING_ACCELERATION, _ZERO),
    )
    distribution_score = max(
        _ZERO,
        by_family.get(BigTrendEvidenceFamily.DISTRIBUTION_DIVERGENCE, _ZERO),
    )
    trend_quality = by_family.get(BigTrendEvidenceFamily.TREND_QUALITY, _ZERO)
    breakout_retention = by_family.get(
        BigTrendEvidenceFamily.BREAKOUT_RETENTION,
        _ZERO,
    )
    participation_supported = bool(supportive & _PARTICIPATION_FAMILIES)
    structure_supported = bool(supportive & _STRUCTURE_FAMILIES)

    reasons: set[str] = {
        f"SUPPORTIVE_FAMILIES:{supportive_family_count}",
        f"SUPPORTIVE_GROUPS:{supportive_group_count}",
    }
    if not participation_supported:
        reasons.add("PARTICIPATION_NOT_CONFIRMED")
    if not structure_supported:
        reasons.add("STRUCTURE_NOT_CONFIRMED")

    broken = (
        trend_quality <= policy.broken_structure_threshold
        and (
            breakout_retention <= policy.broken_structure_threshold
            or distribution_score >= policy.distribution_threshold
        )
    )
    if broken:
        state = BigTrendState.BROKEN
        reasons.add("STRUCTURE_BROKEN")
    elif distribution_score >= policy.distribution_threshold:
        state = BigTrendState.DISTRIBUTING
        reasons.add("DISTRIBUTION_DIVERGENCE_HIGH")
    elif (
        maturity_score >= policy.mature_crowding_threshold
        and supportive_family_count >= policy.confirming_min_families
    ):
        state = BigTrendState.MATURE
        reasons.add("CROWDING_ACCELERATION_HIGH")
    elif (
        confirmation_score >= policy.trending_score
        and supportive_family_count >= policy.trending_min_families
        and supportive_group_count >= policy.trending_min_groups
        and participation_supported
        and structure_supported
    ):
        state = BigTrendState.TRENDING
        reasons.add("MULTI_FAMILY_TREND_CONFIRMED")
    elif (
        confirmation_score >= policy.confirming_score
        and supportive_family_count >= policy.confirming_min_families
        and supportive_group_count >= policy.confirming_min_groups
        and participation_supported
        and structure_supported
    ):
        state = BigTrendState.CONFIRMING
        reasons.add("MULTI_FAMILY_CONFIRMATION_BUILDING")
    elif (
        confirmation_score >= policy.emerging_score
        and supportive_family_count >= policy.emerging_min_families
    ):
        state = BigTrendState.EMERGING
        reasons.add("EARLY_MULTI_FAMILY_EVIDENCE")
    else:
        state = BigTrendState.NONE
        reasons.add("INSUFFICIENT_INDEPENDENT_EVIDENCE")

    actionability = {
        BigTrendState.NONE: BigTrendActionability.NO_ACTION,
        BigTrendState.EMERGING: BigTrendActionability.WATCH_ONLY,
        BigTrendState.CONFIRMING: BigTrendActionability.PLAN_ELIGIBLE,
        BigTrendState.TRENDING: BigTrendActionability.PLAN_ELIGIBLE,
        BigTrendState.MATURE: BigTrendActionability.HOLD_NO_CHASE,
        BigTrendState.DISTRIBUTING: BigTrendActionability.WARNING_TRIM,
        BigTrendState.BROKEN: BigTrendActionability.CLOSE_RUNNER,
    }[state]
    return BigTrendAssessment(
        input_snapshot_id=snapshot.snapshot_id,
        policy_id=policy.policy_id,
        state=state,
        actionability=actionability,
        confirmation_score=confirmation_score,
        supportive_family_count=supportive_family_count,
        supportive_group_count=supportive_group_count,
        maturity_score=maturity_score,
        distribution_score=distribution_score,
        reasons=tuple(sorted(reasons)),
    )


def build_big_trend_transition(
    previous: BigTrendAssessment,
    current: BigTrendAssessment,
) -> BigTrendTransition:
    """Bind a deterministic state transition without implying execution."""

    if not isinstance(previous, BigTrendAssessment):
        raise BigTrendContractError("previous must be BigTrendAssessment")
    if not isinstance(current, BigTrendAssessment):
        raise BigTrendContractError("current must be BigTrendAssessment")
    if current.actionability is BigTrendActionability.DATA_BLOCKED:
        reason = "CURRENT_EVIDENCE_DATA_BLOCKED"
    elif previous.state is current.state:
        reason = "STATE_UNCHANGED"
    elif current.state is BigTrendState.BROKEN:
        reason = "HARD_STRUCTURE_BREAK"
    elif current.state is BigTrendState.DISTRIBUTING:
        reason = "DISTRIBUTION_RISK_INCREASED"
    elif current.state is BigTrendState.MATURE:
        reason = "CROWDING_OR_ACCELERATION_MATURED"
    elif current.state in {BigTrendState.CONFIRMING, BigTrendState.TRENDING}:
        reason = "INDEPENDENT_EVIDENCE_STRENGTHENED"
    elif current.state is BigTrendState.EMERGING:
        reason = "EARLY_EVIDENCE_APPEARED"
    else:
        reason = "INDEPENDENT_EVIDENCE_WEAKENED"
    return BigTrendTransition(
        previous_assessment_id=previous.assessment_id,
        previous_state=previous.state,
        current_assessment_id=current.assessment_id,
        current_state=current.state,
        transition_reason=reason,
    )


__all__ = [
    "DEFAULT_BIG_TREND_POLICY",
    "BigTrendActionability",
    "BigTrendAssessment",
    "BigTrendContractError",
    "BigTrendEvidenceFamily",
    "BigTrendEvidencePoint",
    "BigTrendInputSnapshot",
    "BigTrendPolicy",
    "BigTrendScope",
    "BigTrendState",
    "BigTrendTransition",
    "assess_big_trend",
    "build_big_trend_transition",
]
