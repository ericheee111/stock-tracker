"""Cross-market isolation contracts for A-share, HK, and US expansion.

Market profiles bind independent calendars, universes, rules, costs, datasets,
models, calibration, and scoreboards.  Cross-market reuse is forbidden by
default and may only enter a zero-weight, no-order shadow lane with explicit
target-market validation evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from stock_tracker.core.types import Market

from ..data.bar_artifact import DataTrustTier
from .fingerprint import fingerprint

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUST_RANK = {
    DataTrustTier.UNKNOWN: 0,
    DataTrustTier.BEST_EFFORT: 1,
    DataTrustTier.OPERATIONAL_VERIFIED: 2,
    DataTrustTier.RESEARCH_GRADE: 3,
    DataTrustTier.FROZEN_HOLDOUT: 4,
}


class MarketIsolationContractError(ValueError):
    """Raised when a market profile or transfer request is unsafe."""


class MarketAccessScope(StrEnum):
    A_DOMESTIC = "A_DOMESTIC"
    HK_CONNECT = "HK_CONNECT"
    HK_BROAD = "HK_BROAD"
    US_CASH = "US_CASH"


class SettlementCurrency(StrEnum):
    CNY = "CNY"
    HKD = "HKD"
    USD = "USD"


class MarketIsolationState(StrEnum):
    BLOCKED = "BLOCKED"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    ISOLATED = "ISOLATED"


class CrossMarketTransferKind(StrEnum):
    FEATURE_DEFINITION = "FEATURE_DEFINITION"
    LABEL_DEFINITION = "LABEL_DEFINITION"
    MODEL_ARTIFACT = "MODEL_ARTIFACT"
    SCORE_THRESHOLD = "SCORE_THRESHOLD"
    CALIBRATION = "CALIBRATION"
    STRATEGY_RULE = "STRATEGY_RULE"


class CrossMarketTransferState(StrEnum):
    BLOCKED = "BLOCKED"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    SHADOW_ONLY = "SHADOW_ONLY"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketIsolationContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise MarketIsolationContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MarketIsolationContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MarketIsolationContractError(f"{name} must be a positive integer")
    return value


_EXPECTED_SCOPE_MARKET = {
    MarketAccessScope.A_DOMESTIC: Market.A,
    MarketAccessScope.HK_CONNECT: Market.HK,
    MarketAccessScope.HK_BROAD: Market.HK,
    MarketAccessScope.US_CASH: Market.US,
}
_EXPECTED_CURRENCY = {
    Market.A: SettlementCurrency.CNY,
    Market.HK: SettlementCurrency.HKD,
    Market.US: SettlementCurrency.USD,
}
_EXPECTED_TIMEZONE = {
    Market.A: "Asia/Shanghai",
    Market.HK: "Asia/Hong_Kong",
    Market.US: "America/New_York",
}


@dataclass(frozen=True, slots=True)
class MarketResearchProfile:
    market: Market
    access_scope: MarketAccessScope
    currency: SettlementCurrency
    timezone_name: str
    horizons: tuple[int, ...]
    config_id: str
    calendar_snapshot_id: str
    universe_snapshot_id: str
    market_rule_id: str
    cost_schedule_id: str
    data_snapshot_id: str
    feature_policy_id: str
    label_policy_id: str
    model_id: str
    calibration_id: str
    scoreboard_id: str
    trust_tier: DataTrustTier
    verified: bool
    complete: bool
    synthetic_fixture_only: bool
    provenance_ids: tuple[str, ...]
    profile_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise MarketIsolationContractError("market must be Market")
        if not isinstance(self.access_scope, MarketAccessScope):
            raise MarketIsolationContractError(
                "access_scope must be MarketAccessScope"
            )
        if _EXPECTED_SCOPE_MARKET[self.access_scope] is not self.market:
            raise MarketIsolationContractError(
                "access_scope does not belong to market"
            )
        if not isinstance(self.currency, SettlementCurrency):
            raise MarketIsolationContractError(
                "currency must be SettlementCurrency"
            )
        if self.currency is not _EXPECTED_CURRENCY[self.market]:
            raise MarketIsolationContractError("currency does not match market")
        timezone_name = _require_text(self.timezone_name, "timezone_name")
        if timezone_name != _EXPECTED_TIMEZONE[self.market]:
            raise MarketIsolationContractError("timezone does not match market")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in self.horizons
        ):
            raise MarketIsolationContractError(
                "horizons must contain positive integers"
            )
        if not self.horizons or self.horizons != tuple(sorted(set(self.horizons))):
            raise MarketIsolationContractError(
                "horizons must be non-empty, sorted and unique"
            )
        for name in (
            "config_id",
            "calendar_snapshot_id",
            "universe_snapshot_id",
            "market_rule_id",
            "cost_schedule_id",
            "data_snapshot_id",
            "feature_policy_id",
            "label_policy_id",
            "model_id",
            "calibration_id",
            "scoreboard_id",
        ):
            _require_sha256(getattr(self, name), name)
        if not isinstance(self.trust_tier, DataTrustTier):
            raise MarketIsolationContractError("trust_tier must be DataTrustTier")
        for name in ("verified", "complete", "synthetic_fixture_only"):
            _require_bool(getattr(self, name), name)
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.provenance_ids
        ):
            raise MarketIsolationContractError(
                "provenance_ids must contain lowercase SHA-256"
            )
        if self.provenance_ids != tuple(sorted(set(self.provenance_ids))):
            raise MarketIsolationContractError(
                "provenance_ids must be sorted and unique"
            )
        if self.verified and not self.provenance_ids:
            raise MarketIsolationContractError(
                "verified market profile requires provenance IDs"
            )
        if not self.verified and self.provenance_ids:
            raise MarketIsolationContractError(
                "unverified market profile cannot carry provenance IDs"
            )
        if self.synthetic_fixture_only:
            if self.verified or self.trust_tier is not DataTrustTier.BEST_EFFORT:
                raise MarketIsolationContractError(
                    "synthetic profile must remain unverified BEST_EFFORT"
                )
        elif (
            _TRUST_RANK[self.trust_tier]
            >= _TRUST_RANK[DataTrustTier.OPERATIONAL_VERIFIED]
            and not self.verified
        ):
            raise MarketIsolationContractError(
                "operational-or-higher profile must be verified"
            )
        object.__setattr__(
            self,
            "profile_id",
            fingerprint(
                {
                    "schema": "market-research-profile-v1",
                    "market": self.market,
                    "access_scope": self.access_scope,
                    "currency": self.currency,
                    "timezone_name": self.timezone_name,
                    "horizons": self.horizons,
                    "config_id": self.config_id,
                    "calendar_snapshot_id": self.calendar_snapshot_id,
                    "universe_snapshot_id": self.universe_snapshot_id,
                    "market_rule_id": self.market_rule_id,
                    "cost_schedule_id": self.cost_schedule_id,
                    "data_snapshot_id": self.data_snapshot_id,
                    "feature_policy_id": self.feature_policy_id,
                    "label_policy_id": self.label_policy_id,
                    "model_id": self.model_id,
                    "calibration_id": self.calibration_id,
                    "scoreboard_id": self.scoreboard_id,
                    "trust_tier": self.trust_tier,
                    "verified": self.verified,
                    "complete": self.complete,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "provenance_ids": list(self.provenance_ids),
                }
            ),
        )

    @property
    def scope_key(self) -> tuple[Market, MarketAccessScope]:
        return (self.market, self.access_scope)


@dataclass(frozen=True, slots=True)
class MarketIsolationPolicy:
    policy_version: str
    minimum_trust: DataTrustTier = DataTrustTier.OPERATIONAL_VERIFIED
    required_scopes: tuple[MarketAccessScope, ...] = (
        MarketAccessScope.A_DOMESTIC,
        MarketAccessScope.HK_CONNECT,
        MarketAccessScope.US_CASH,
    )
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if not isinstance(self.minimum_trust, DataTrustTier):
            raise MarketIsolationContractError(
                "minimum_trust must be DataTrustTier"
            )
        if any(
            not isinstance(item, MarketAccessScope) for item in self.required_scopes
        ):
            raise MarketIsolationContractError(
                "required_scopes must contain MarketAccessScope values"
            )
        expected = tuple(sorted(set(self.required_scopes), key=lambda item: item.value))
        if not self.required_scopes or self.required_scopes != expected:
            raise MarketIsolationContractError(
                "required_scopes must be non-empty, sorted and unique"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "market-isolation-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_trust": self.minimum_trust,
                    "required_scopes": [item.value for item in self.required_scopes],
                }
            ),
        )


DEFAULT_MARKET_ISOLATION_POLICY = MarketIsolationPolicy(
    policy_version="market-isolation-v1",
    required_scopes=tuple(
        sorted(
            (
                MarketAccessScope.A_DOMESTIC,
                MarketAccessScope.HK_CONNECT,
                MarketAccessScope.US_CASH,
            ),
            key=lambda item: item.value,
        )
    ),
)


_ISOLATED_ID_FIELDS = (
    "config_id",
    "calendar_snapshot_id",
    "universe_snapshot_id",
    "market_rule_id",
    "cost_schedule_id",
    "data_snapshot_id",
    "feature_policy_id",
    "label_policy_id",
    "model_id",
    "calibration_id",
    "scoreboard_id",
)


@dataclass(frozen=True, slots=True)
class MarketIsolationBundle:
    profiles: tuple[MarketResearchProfile, ...]
    policy: MarketIsolationPolicy = DEFAULT_MARKET_ISOLATION_POLICY
    blockers: tuple[str, ...] = field(init=False)
    state: MarketIsolationState = field(init=False)
    bundle_id: str = field(init=False)

    def __post_init__(self) -> None:
        if any(not isinstance(item, MarketResearchProfile) for item in self.profiles):
            raise MarketIsolationContractError(
                "profiles must contain MarketResearchProfile values"
            )
        if not isinstance(self.policy, MarketIsolationPolicy):
            raise MarketIsolationContractError(
                "policy must be MarketIsolationPolicy"
            )
        normalized = tuple(
            sorted(
                self.profiles,
                key=lambda item: (item.market.value, item.access_scope.value),
            )
        )
        if len({item.scope_key for item in normalized}) != len(normalized):
            raise MarketIsolationContractError("market access scopes must be unique")
        object.__setattr__(self, "profiles", normalized)
        blockers: set[str] = set()
        present_scopes = {item.access_scope for item in normalized}
        for scope in self.policy.required_scopes:
            if scope not in present_scopes:
                blockers.add(f"MISSING_SCOPE:{scope.value}")
        for profile in normalized:
            if not profile.verified:
                blockers.add(f"PROFILE_UNVERIFIED:{profile.access_scope.value}")
            if not profile.complete:
                blockers.add(f"PROFILE_INCOMPLETE:{profile.access_scope.value}")
            if (
                _TRUST_RANK[profile.trust_tier]
                < _TRUST_RANK[self.policy.minimum_trust]
            ):
                blockers.add(
                    f"PROFILE_TRUST_INSUFFICIENT:{profile.access_scope.value}"
                )
        for field_name in _ISOLATED_ID_FIELDS:
            values: dict[str, list[str]] = {}
            for profile in normalized:
                values.setdefault(getattr(profile, field_name), []).append(
                    profile.access_scope.value
                )
            for identity, scopes in values.items():
                if len(scopes) > 1:
                    blockers.add(
                        f"SHARED_{field_name.upper()}:"
                        + ",".join(sorted(scopes))
                        + f":{identity}"
                    )
        blocker_tuple = tuple(sorted(blockers))
        if any(item.synthetic_fixture_only for item in normalized):
            state = MarketIsolationState.DIAGNOSTIC_ONLY
        elif blocker_tuple:
            state = MarketIsolationState.BLOCKED
        else:
            state = MarketIsolationState.ISOLATED
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "bundle_id",
            fingerprint(
                {
                    "schema": "market-isolation-bundle-v1",
                    "profile_ids": [item.profile_id for item in normalized],
                    "policy_id": self.policy.policy_id,
                    "blockers": blocker_tuple,
                    "state": state,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CrossMarketTransferRequest:
    source: MarketResearchProfile
    target: MarketResearchProfile
    transfer_kind: CrossMarketTransferKind
    target_validation_id: str | None
    approval_evidence_ids: tuple[str, ...]
    shadow_only: bool
    production_weight_zero: bool
    orders_created: bool
    synthetic_fixture_only: bool
    blockers: tuple[str, ...] = field(init=False)
    state: CrossMarketTransferState = field(init=False)
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, MarketResearchProfile) or not isinstance(
            self.target,
            MarketResearchProfile,
        ):
            raise MarketIsolationContractError(
                "source and target must be MarketResearchProfile values"
            )
        if self.source.market is self.target.market:
            raise MarketIsolationContractError(
                "cross-market transfer requires different markets"
            )
        if not isinstance(self.transfer_kind, CrossMarketTransferKind):
            raise MarketIsolationContractError(
                "transfer_kind must be CrossMarketTransferKind"
            )
        if self.target_validation_id is not None:
            _require_sha256(self.target_validation_id, "target_validation_id")
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.approval_evidence_ids
        ):
            raise MarketIsolationContractError(
                "approval_evidence_ids must contain lowercase SHA-256"
            )
        if self.approval_evidence_ids != tuple(
            sorted(set(self.approval_evidence_ids))
        ):
            raise MarketIsolationContractError(
                "approval_evidence_ids must be sorted and unique"
            )
        for name in (
            "shadow_only",
            "production_weight_zero",
            "orders_created",
            "synthetic_fixture_only",
        ):
            _require_bool(getattr(self, name), name)
        expected_synthetic = (
            self.source.synthetic_fixture_only
            or self.target.synthetic_fixture_only
        )
        if self.synthetic_fixture_only is not expected_synthetic:
            raise MarketIsolationContractError(
                "transfer synthetic flag must match source/target provenance"
            )
        blockers: set[str] = set()
        if self.target_validation_id is None:
            blockers.add("TARGET_MARKET_VALIDATION_MISSING")
        if not self.approval_evidence_ids:
            blockers.add("TRANSFER_APPROVAL_EVIDENCE_MISSING")
        if not self.shadow_only:
            blockers.add("CROSS_MARKET_PRODUCTION_REUSE_FORBIDDEN")
        if not self.production_weight_zero:
            blockers.add("TRANSFER_PRODUCTION_WEIGHT_NOT_ZERO")
        if self.orders_created:
            blockers.add("TRANSFER_CREATED_ORDERS")
        if not self.source.complete or not self.target.complete:
            blockers.add("SOURCE_OR_TARGET_PROFILE_INCOMPLETE")
        if not self.source.verified or not self.target.verified:
            blockers.add("SOURCE_OR_TARGET_PROFILE_UNVERIFIED")
        if any(
            _TRUST_RANK[profile.trust_tier]
            < _TRUST_RANK[DataTrustTier.OPERATIONAL_VERIFIED]
            for profile in (self.source, self.target)
        ):
            blockers.add("SOURCE_OR_TARGET_PROFILE_TRUST_INSUFFICIENT")
        blocker_tuple = tuple(sorted(blockers))
        if self.synthetic_fixture_only:
            state = CrossMarketTransferState.DIAGNOSTIC_ONLY
        elif blocker_tuple:
            state = CrossMarketTransferState.BLOCKED
        else:
            state = CrossMarketTransferState.SHADOW_ONLY
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "request_id",
            fingerprint(
                {
                    "schema": "cross-market-transfer-request-v1",
                    "source_profile_id": self.source.profile_id,
                    "target_profile_id": self.target.profile_id,
                    "transfer_kind": self.transfer_kind,
                    "target_validation_id": self.target_validation_id,
                    "approval_evidence_ids": list(self.approval_evidence_ids),
                    "shadow_only": self.shadow_only,
                    "production_weight_zero": self.production_weight_zero,
                    "orders_created": self.orders_created,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "blockers": blocker_tuple,
                    "state": state,
                    "allows_production_reuse": False,
                    "deploys_model": False,
                    "changes_runtime_weight": False,
                    "creates_order": False,
                }
            ),
        )

    @property
    def allows_production_reuse(self) -> bool:
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
    "DEFAULT_MARKET_ISOLATION_POLICY",
    "CrossMarketTransferKind",
    "CrossMarketTransferRequest",
    "CrossMarketTransferState",
    "MarketAccessScope",
    "MarketIsolationBundle",
    "MarketIsolationContractError",
    "MarketIsolationPolicy",
    "MarketIsolationState",
    "MarketResearchProfile",
    "SettlementCurrency",
]
