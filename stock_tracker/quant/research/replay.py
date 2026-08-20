"""Point-in-time replay planning and execution evidence.

Replay never reads the current runtime SQLite state implicitly.  Every required
snapshot must be provided, purpose-qualified, visible at the target time, and
bound to an explicit data lane and trust tier.  Missing or weak evidence yields
``BLOCKED`` rather than a best-effort reconstruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from ..data.bar_artifact import DataTrustTier

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUST_RANK = {
    DataTrustTier.BEST_EFFORT: 1,
    DataTrustTier.OPERATIONAL_VERIFIED: 2,
    DataTrustTier.RESEARCH_GRADE: 3,
    DataTrustTier.FROZEN_HOLDOUT: 4,
}


class ReplayContractError(ValueError):
    """Raised when replay evidence or execution claims are inconsistent."""


class ReplayPurpose(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    FORMAL_DECISION = "FORMAL_DECISION"
    FROZEN_HOLDOUT = "FROZEN_HOLDOUT"


class ReplayDependencyKind(StrEnum):
    CALENDAR = "CALENDAR"
    UNIVERSE = "UNIVERSE"
    SECURITY_STATUS = "SECURITY_STATUS"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    CLASSIFICATION = "CLASSIFICATION"
    EVENT = "EVENT"
    RAW_BAR = "RAW_BAR"
    FEATURE = "FEATURE"
    MODEL = "MODEL"
    CONFIG = "CONFIG"
    MARKET_RULE = "MARKET_RULE"
    COST_SCHEDULE = "COST_SCHEDULE"
    PORTFOLIO = "PORTFOLIO"
    DECISION_POLICY = "DECISION_POLICY"


class ReplayDataLane(StrEnum):
    RESEARCH = "RESEARCH"
    RUNTIME = "RUNTIME"
    SHADOW = "SHADOW"


class ReplayPlanState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"


class ReplayExecutionState(StrEnum):
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    MISMATCH = "MISMATCH"
    FAILED = "FAILED"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ReplayContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ReplayContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ReplayContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_symbol(symbol: object, market: Market) -> str:
    value = _require_text(symbol, "symbol_at_target")
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if value != value.upper() or not value.endswith(suffixes[market]):
        raise ReplayContractError("symbol suffix must match market")
    return value


@dataclass(frozen=True, slots=True)
class ReplayDependency:
    kind: ReplayDependencyKind
    snapshot_id: str
    snapshot_as_of: datetime
    known_at: datetime
    created_at: datetime
    valid_from: datetime
    valid_to: datetime | None
    trust_tier: DataTrustTier
    lane: ReplayDataLane
    verified: bool
    complete: bool
    synthetic_fixture_only: bool
    source_name: str
    provenance_ids: tuple[str, ...]
    dependency_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReplayDependencyKind):
            raise ReplayContractError("kind must be ReplayDependencyKind")
        _require_sha256(self.snapshot_id, "snapshot_id")
        for name in ("snapshot_as_of", "known_at", "created_at", "valid_from"):
            ensure_aware(getattr(self, name), name)
        if self.valid_to is not None:
            ensure_aware(self.valid_to, "valid_to")
            if to_utc(self.valid_to) < to_utc(self.valid_from):
                raise ReplayContractError("valid_to cannot precede valid_from")
        if to_utc(self.known_at) > to_utc(self.snapshot_as_of):
            raise ReplayContractError("known_at cannot follow snapshot_as_of")
        if to_utc(self.created_at) < to_utc(self.known_at):
            raise ReplayContractError("created_at cannot precede known_at")
        if not isinstance(self.trust_tier, DataTrustTier):
            raise ReplayContractError("trust_tier must be DataTrustTier")
        if not isinstance(self.lane, ReplayDataLane):
            raise ReplayContractError("lane must be ReplayDataLane")
        for name in ("verified", "complete", "synthetic_fixture_only"):
            _require_bool(getattr(self, name), name)
        _require_text(self.source_name, "source_name")
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.provenance_ids
        ):
            raise ReplayContractError(
                "provenance_ids must contain lowercase SHA-256"
            )
        if self.provenance_ids != tuple(sorted(set(self.provenance_ids))):
            raise ReplayContractError(
                "provenance_ids must be sorted and unique"
            )
        if self.verified and not self.provenance_ids:
            raise ReplayContractError(
                "verified dependency requires provenance evidence"
            )
        if self.verified and _TRUST_RANK[self.trust_tier] < _TRUST_RANK[
            DataTrustTier.OPERATIONAL_VERIFIED
        ]:
            raise ReplayContractError(
                "verified dependency cannot remain BEST_EFFORT"
            )
        if self.synthetic_fixture_only and (
            self.verified or self.trust_tier is not DataTrustTier.BEST_EFFORT
        ):
            raise ReplayContractError(
                "synthetic dependency must remain unverified BEST_EFFORT"
            )
        if self.source_name == "free_stockdb" and (
            self.lane is not ReplayDataLane.SHADOW
            or self.trust_tier is not DataTrustTier.BEST_EFFORT
            or self.verified
            or self.complete
        ):
            raise ReplayContractError(
                "free_stockdb dependency must remain incomplete T1 shadow evidence"
            )
        object.__setattr__(
            self,
            "dependency_id",
            fingerprint(
                {
                    "schema": "replay-dependency-v1",
                    "kind": self.kind,
                    "snapshot_id": self.snapshot_id,
                    "snapshot_as_of": to_utc(self.snapshot_as_of),
                    "known_at": to_utc(self.known_at),
                    "created_at": to_utc(self.created_at),
                    "valid_from": to_utc(self.valid_from),
                    "valid_to": (
                        None if self.valid_to is None else to_utc(self.valid_to)
                    ),
                    "trust_tier": self.trust_tier,
                    "lane": self.lane,
                    "verified": self.verified,
                    "complete": self.complete,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "source_name": self.source_name,
                    "provenance_ids": list(self.provenance_ids),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    policy_version: str
    purpose: ReplayPurpose
    required_kinds: tuple[ReplayDependencyKind, ...]
    minimum_trust: DataTrustTier
    require_verified: bool
    require_complete: bool
    allow_synthetic: bool
    allowed_lanes: tuple[ReplayDataLane, ...]
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if not isinstance(self.purpose, ReplayPurpose):
            raise ReplayContractError("purpose must be ReplayPurpose")
        if any(
            not isinstance(item, ReplayDependencyKind)
            for item in self.required_kinds
        ):
            raise ReplayContractError(
                "required_kinds must contain ReplayDependencyKind values"
            )
        expected_kinds = tuple(sorted(set(self.required_kinds), key=lambda item: item.value))
        if not self.required_kinds or self.required_kinds != expected_kinds:
            raise ReplayContractError(
                "required_kinds must be non-empty, sorted and unique"
            )
        if not isinstance(self.minimum_trust, DataTrustTier):
            raise ReplayContractError("minimum_trust must be DataTrustTier")
        for name in ("require_verified", "require_complete", "allow_synthetic"):
            _require_bool(getattr(self, name), name)
        if any(not isinstance(item, ReplayDataLane) for item in self.allowed_lanes):
            raise ReplayContractError(
                "allowed_lanes must contain ReplayDataLane values"
            )
        expected_lanes = tuple(sorted(set(self.allowed_lanes), key=lambda item: item.value))
        if not self.allowed_lanes or self.allowed_lanes != expected_lanes:
            raise ReplayContractError(
                "allowed_lanes must be non-empty, sorted and unique"
            )
        if self.purpose is not ReplayPurpose.DIAGNOSTIC and (
            self.allow_synthetic or ReplayDataLane.RUNTIME in self.allowed_lanes
        ):
            raise ReplayContractError(
                "formal replay policy cannot allow synthetic or runtime lane"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "replay-policy-v1",
                    "policy_version": self.policy_version,
                    "purpose": self.purpose,
                    "required_kinds": [item.value for item in self.required_kinds],
                    "minimum_trust": self.minimum_trust,
                    "require_verified": self.require_verified,
                    "require_complete": self.require_complete,
                    "allow_synthetic": self.allow_synthetic,
                    "allowed_lanes": [item.value for item in self.allowed_lanes],
                }
            ),
        )


_ALL_REQUIRED_KINDS = tuple(
    sorted(ReplayDependencyKind, key=lambda item: item.value)
)

FORMAL_REPLAY_POLICY = ReplayPolicy(
    policy_version="formal-decision-replay-v1",
    purpose=ReplayPurpose.FORMAL_DECISION,
    required_kinds=_ALL_REQUIRED_KINDS,
    minimum_trust=DataTrustTier.RESEARCH_GRADE,
    require_verified=True,
    require_complete=True,
    allow_synthetic=False,
    allowed_lanes=(ReplayDataLane.RESEARCH,),
)

FROZEN_HOLDOUT_REPLAY_POLICY = ReplayPolicy(
    policy_version="frozen-holdout-replay-v1",
    purpose=ReplayPurpose.FROZEN_HOLDOUT,
    required_kinds=_ALL_REQUIRED_KINDS,
    minimum_trust=DataTrustTier.FROZEN_HOLDOUT,
    require_verified=True,
    require_complete=True,
    allow_synthetic=False,
    allowed_lanes=(ReplayDataLane.RESEARCH,),
)

DIAGNOSTIC_REPLAY_POLICY = ReplayPolicy(
    policy_version="diagnostic-replay-v1",
    purpose=ReplayPurpose.DIAGNOSTIC,
    required_kinds=_ALL_REQUIRED_KINDS,
    minimum_trust=DataTrustTier.BEST_EFFORT,
    require_verified=False,
    require_complete=False,
    allow_synthetic=True,
    allowed_lanes=tuple(sorted(ReplayDataLane, key=lambda item: item.value)),
)


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    target_at: datetime
    requested_at: datetime
    purpose: ReplayPurpose
    market: Market
    instrument_id: str
    identity_fact_id: str
    symbol_at_target: str
    expected_decision_snapshot_id: str | None
    policy: ReplayPolicy
    dependencies: tuple[ReplayDependency, ...]
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        ensure_aware(self.target_at, "target_at")
        ensure_aware(self.requested_at, "requested_at")
        if to_utc(self.requested_at) < to_utc(self.target_at):
            raise ReplayContractError("requested_at cannot precede target_at")
        if not isinstance(self.purpose, ReplayPurpose):
            raise ReplayContractError("purpose must be ReplayPurpose")
        if not isinstance(self.market, Market):
            raise ReplayContractError("market must be Market")
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        _require_symbol(self.symbol_at_target, self.market)
        if self.expected_decision_snapshot_id is not None:
            _require_sha256(
                self.expected_decision_snapshot_id,
                "expected_decision_snapshot_id",
            )
        if not isinstance(self.policy, ReplayPolicy):
            raise ReplayContractError("policy must be ReplayPolicy")
        if self.policy.purpose is not self.purpose:
            raise ReplayContractError("request purpose must match replay policy")
        if any(
            not isinstance(item, ReplayDependency) for item in self.dependencies
        ):
            raise ReplayContractError(
                "dependencies must contain ReplayDependency values"
            )
        normalized = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (item.kind.value, item.dependency_id),
            )
        )
        kinds = tuple(item.kind for item in normalized)
        if len(set(kinds)) != len(kinds):
            raise ReplayContractError(
                "replay request cannot contain duplicate dependency kinds"
            )
        object.__setattr__(self, "dependencies", normalized)
        object.__setattr__(
            self,
            "request_id",
            fingerprint(
                {
                    "schema": "replay-request-v1",
                    "target_at": to_utc(self.target_at),
                    "requested_at": to_utc(self.requested_at),
                    "purpose": self.purpose,
                    "market": self.market,
                    "instrument_id": self.instrument_id,
                    "identity_fact_id": self.identity_fact_id,
                    "symbol_at_target": self.symbol_at_target,
                    "expected_decision_snapshot_id": (
                        self.expected_decision_snapshot_id
                    ),
                    "policy_id": self.policy.policy_id,
                    "dependency_ids": [
                        item.dependency_id for item in normalized
                    ],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    request: ReplayRequest
    dependency_ids: tuple[str, ...] = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    state: ReplayPlanState = field(init=False)
    formal_research_eligible: bool = field(init=False)
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, ReplayRequest):
            raise ReplayContractError("request must be ReplayRequest")
        dependency_by_kind = {
            dependency.kind: dependency
            for dependency in self.request.dependencies
        }
        blockers: set[str] = set()
        for kind in self.request.policy.required_kinds:
            if kind not in dependency_by_kind:
                blockers.add(f"MISSING_DEPENDENCY:{kind.value}")
        if (
            self.request.purpose is not ReplayPurpose.DIAGNOSTIC
            and self.request.expected_decision_snapshot_id is None
        ):
            blockers.add("EXPECTED_DECISION_SNAPSHOT_MISSING")
        for dependency in self.request.dependencies:
            kind = dependency.kind.value
            if to_utc(dependency.snapshot_as_of) != to_utc(
                self.request.target_at
            ):
                blockers.add(f"AS_OF_MISMATCH:{kind}")
            if to_utc(dependency.known_at) > to_utc(self.request.target_at):
                blockers.add(f"FUTURE_KNOWN_AT:{kind}")
            if to_utc(dependency.created_at) > to_utc(self.request.requested_at):
                blockers.add(f"FUTURE_CREATED_AT:{kind}")
            if to_utc(self.request.target_at) < to_utc(dependency.valid_from):
                blockers.add(f"NOT_YET_VALID:{kind}")
            if (
                dependency.valid_to is not None
                and to_utc(self.request.target_at) > to_utc(dependency.valid_to)
            ):
                blockers.add(f"EXPIRED_DEPENDENCY:{kind}")
            if _TRUST_RANK[dependency.trust_tier] < _TRUST_RANK[
                self.request.policy.minimum_trust
            ]:
                blockers.add(f"TRUST_TIER_INSUFFICIENT:{kind}")
            if self.request.policy.require_verified and not dependency.verified:
                blockers.add(f"UNVERIFIED_DEPENDENCY:{kind}")
            if self.request.policy.require_complete and not dependency.complete:
                blockers.add(f"INCOMPLETE_DEPENDENCY:{kind}")
            if (
                dependency.synthetic_fixture_only
                and not self.request.policy.allow_synthetic
            ):
                blockers.add(f"SYNTHETIC_DEPENDENCY:{kind}")
            if dependency.lane not in self.request.policy.allowed_lanes:
                blockers.add(f"FORBIDDEN_DATA_LANE:{kind}:{dependency.lane.value}")
            if (
                dependency.source_name == "free_stockdb"
                and self.request.purpose is not ReplayPurpose.DIAGNOSTIC
            ):
                blockers.add(f"SIDECAR_FORBIDDEN_FOR_FORMAL_REPLAY:{kind}")
        blocker_tuple = tuple(sorted(blockers))
        state = ReplayPlanState.BLOCKED if blocker_tuple else ReplayPlanState.READY
        formal = state is ReplayPlanState.READY and self.request.purpose in {
            ReplayPurpose.FORMAL_DECISION,
            ReplayPurpose.FROZEN_HOLDOUT,
        }
        dependency_ids = tuple(
            item.dependency_id for item in self.request.dependencies
        )
        object.__setattr__(self, "dependency_ids", dependency_ids)
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "formal_research_eligible", formal)
        object.__setattr__(
            self,
            "plan_id",
            fingerprint(
                {
                    "schema": "replay-plan-v1",
                    "request_id": self.request.request_id,
                    "dependency_ids": list(dependency_ids),
                    "blockers": blocker_tuple,
                    "state": state,
                    "formal_research_eligible": formal,
                }
            ),
        )


def build_replay_plan(request: ReplayRequest) -> ReplayPlan:
    """Build a deterministic plan; blockers are evidence, not exceptions."""

    return ReplayPlan(request=request)


@dataclass(frozen=True, slots=True)
class ReplayExecutionResult:
    plan: ReplayPlan
    executed_at: datetime
    executor_version: str
    observed_dependency_ids: tuple[str, ...]
    output_decision_snapshot_id: str | None
    error_code: str | None
    production_database_modified: bool
    state: ReplayExecutionState = field(init=False)
    decision_matches_expected: bool | None = field(init=False)
    uses_current_runtime_state: bool = field(init=False, default=False)
    result_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ReplayPlan):
            raise ReplayContractError("plan must be ReplayPlan")
        ensure_aware(self.executed_at, "executed_at")
        if to_utc(self.executed_at) < to_utc(self.plan.request.requested_at):
            raise ReplayContractError(
                "executed_at cannot precede replay request"
            )
        _require_text(self.executor_version, "executor_version")
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.observed_dependency_ids
        ):
            raise ReplayContractError(
                "observed_dependency_ids must contain lowercase SHA-256"
            )
        if self.observed_dependency_ids != tuple(
            sorted(set(self.observed_dependency_ids))
        ):
            raise ReplayContractError(
                "observed_dependency_ids must be sorted and unique"
            )
        if self.output_decision_snapshot_id is not None:
            _require_sha256(
                self.output_decision_snapshot_id,
                "output_decision_snapshot_id",
            )
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        _require_bool(
            self.production_database_modified,
            "production_database_modified",
        )
        if self.production_database_modified:
            raise ReplayContractError(
                "replay must not modify the production database"
            )

        if self.plan.state is ReplayPlanState.BLOCKED:
            if (
                self.observed_dependency_ids
                or self.output_decision_snapshot_id is not None
                or self.error_code is not None
            ):
                raise ReplayContractError(
                    "blocked replay plan cannot claim execution evidence"
                )
            state = ReplayExecutionState.BLOCKED
            matches: bool | None = None
        else:
            if self.observed_dependency_ids != tuple(
                sorted(self.plan.dependency_ids)
            ):
                raise ReplayContractError(
                    "execution must bind the exact planned dependencies"
                )
            if (self.output_decision_snapshot_id is None) == (
                self.error_code is None
            ):
                raise ReplayContractError(
                    "ready replay requires exactly one output or error"
                )
            if self.error_code is not None:
                state = ReplayExecutionState.FAILED
                matches = None
            else:
                expected = self.plan.request.expected_decision_snapshot_id
                if expected is None:
                    state = ReplayExecutionState.COMPLETED
                    matches = None
                else:
                    matches = self.output_decision_snapshot_id == expected
                    state = (
                        ReplayExecutionState.COMPLETED
                        if matches
                        else ReplayExecutionState.MISMATCH
                    )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "decision_matches_expected", matches)
        object.__setattr__(
            self,
            "result_id",
            fingerprint(
                {
                    "schema": "replay-execution-result-v1",
                    "plan_id": self.plan.plan_id,
                    "executed_at": to_utc(self.executed_at),
                    "executor_version": self.executor_version,
                    "observed_dependency_ids": list(
                        self.observed_dependency_ids
                    ),
                    "output_decision_snapshot_id": (
                        self.output_decision_snapshot_id
                    ),
                    "error_code": self.error_code,
                    "production_database_modified": False,
                    "uses_current_runtime_state": False,
                    "state": state,
                    "decision_matches_expected": matches,
                }
            ),
        )


__all__ = [
    "DIAGNOSTIC_REPLAY_POLICY",
    "FORMAL_REPLAY_POLICY",
    "FROZEN_HOLDOUT_REPLAY_POLICY",
    "ReplayContractError",
    "ReplayDataLane",
    "ReplayDependency",
    "ReplayDependencyKind",
    "ReplayExecutionResult",
    "ReplayExecutionState",
    "ReplayPlan",
    "ReplayPlanState",
    "ReplayPolicy",
    "ReplayPurpose",
    "ReplayRequest",
    "build_replay_plan",
]
