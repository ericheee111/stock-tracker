"""Deterministic multi-source reconciliation for corporate-action candidates.

Stage 2E compares immutable, identity-bound candidate bundles.  Reconciliation
may determine whether a bundle is ready to be sent to an independent verifier;
it never manufactures ``verified``, ``complete``, a Trust Tier, or research-grade
status.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from .corporate_action_adapter import (
    CandidateCorporateAction,
    CorporateActionSourceOwner,
    resolve_corporate_action_candidates,
)
from .corporate_action_extraction import BoundCorporateActionCandidateBundle


class CorporateActionReconciliationError(ValueError):
    """Raised when reconciliation inputs or mappings are ambiguous."""


class LicenseStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    CLEARED_FOR_INTERNAL_RESEARCH = "CLEARED_FOR_INTERNAL_RESEARCH"


class PromotionEligibilityStatus(StrEnum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    ELIGIBLE_FOR_INDEPENDENT_VERIFICATION = (
        "ELIGIBLE_FOR_INDEPENDENT_VERIFICATION"
    )


class ConflictKind(StrEnum):
    LIFECYCLE = "LIFECYCLE"
    EX_DATE = "EX_DATE"
    RECORD_DATE = "RECORD_DATE"
    PAYMENT_DATE = "PAYMENT_DATE"
    SHARE_LISTING_DATE = "SHARE_LISTING_DATE"
    EFFECTIVE_DATE = "EFFECTIVE_DATE"
    ACTION_TYPE = "ACTION_TYPE"
    AUTOMATIC_SHARE_RATIO = "AUTOMATIC_SHARE_RATIO"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    RIGHTS_RATIO = "RIGHTS_RATIO"
    RIGHTS_PRICE = "RIGHTS_PRICE"
    CURRENCY = "CURRENCY"
    REFERENCE_PRICE = "REFERENCE_PRICE"
    REFERENCE_PRICE_EVIDENCE = "REFERENCE_PRICE_EVIDENCE"
    IDENTITY = "IDENTITY"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorporateActionReconciliationError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CorporateActionReconciliationError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class CandidateActionMapping:
    candidate_id: str
    logical_action_id: str
    mapping_policy_version: str
    mapping_note: str
    mapping_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.candidate_id) is not str
            or len(self.candidate_id) != 64
            or any(character not in "0123456789abcdef" for character in self.candidate_id)
        ):
            raise CorporateActionReconciliationError(
                "candidate_id must be lowercase SHA-256"
            )
        _require_text(self.logical_action_id, "logical_action_id")
        _require_text(self.mapping_policy_version, "mapping_policy_version")
        _require_text(self.mapping_note, "mapping_note")
        object.__setattr__(
            self,
            "mapping_id",
            fingerprint(
                {
                    "schema": "stage2e-candidate-action-mapping-v1",
                    "candidate_id": self.candidate_id,
                    "logical_action_id": self.logical_action_id,
                    "mapping_policy_version": self.mapping_policy_version,
                    "mapping_note": self.mapping_note,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CoverageClaimCandidate:
    instrument_id: str
    source_owner: CorporateActionSourceOwner
    source_version: str
    start_date: date
    end_date: date
    known_at: datetime
    usable_from: datetime
    surveyed_source_event_ids: tuple[str, ...]
    coverage_note: str
    license_status: LicenseStatus
    synthetic_fixture: bool = True
    coverage_claim_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        if not isinstance(self.source_owner, CorporateActionSourceOwner):
            raise CorporateActionReconciliationError(
                "source_owner must be CorporateActionSourceOwner"
            )
        _require_text(self.source_version, "source_version")
        if self.end_date < self.start_date:
            raise CorporateActionReconciliationError(
                "coverage end_date cannot precede start_date"
            )
        ensure_aware(self.known_at, "known_at")
        ensure_aware(self.usable_from, "usable_from")
        if to_utc(self.usable_from) < to_utc(self.known_at):
            raise CorporateActionReconciliationError(
                "coverage usable_from cannot precede known_at"
            )
        if self.surveyed_source_event_ids != tuple(
            sorted(set(self.surveyed_source_event_ids))
        ):
            raise CorporateActionReconciliationError(
                "surveyed_source_event_ids must be sorted and unique"
            )
        for item in self.surveyed_source_event_ids:
            _require_text(item, "surveyed source event id")
        _require_text(self.coverage_note, "coverage_note")
        if not isinstance(self.license_status, LicenseStatus):
            raise CorporateActionReconciliationError(
                "license_status must be LicenseStatus"
            )
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionReconciliationError(
                "Stage 2E coverage fixtures are synthetic-only and cannot be relabelled"
            )
        object.__setattr__(
            self,
            "coverage_claim_id",
            fingerprint(
                {
                    "schema": "stage2e-coverage-claim-candidate-v1",
                    "instrument_id": self.instrument_id,
                    "source_owner": self.source_owner,
                    "source_version": self.source_version,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "known_at": to_utc(self.known_at),
                    "usable_from": to_utc(self.usable_from),
                    "surveyed_source_event_ids": self.surveyed_source_event_ids,
                    "coverage_note": self.coverage_note,
                    "license_status": self.license_status,
                    "synthetic_fixture": self.synthetic_fixture,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    policy_version: str
    required_primary_owners: tuple[CorporateActionSourceOwner, ...]
    minimum_independent_sources: int
    require_reference_price_evidence: bool
    require_license_clearance: bool
    require_attachment_evidence: bool = True
    allow_synthetic_eligibility_test: bool = False
    synthetic_fixture: bool = True
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        if self.required_primary_owners != tuple(
            sorted(set(self.required_primary_owners), key=lambda item: item.value)
        ):
            raise CorporateActionReconciliationError(
                "required_primary_owners must be sorted and unique"
            )
        if any(
            not isinstance(owner, CorporateActionSourceOwner)
            for owner in self.required_primary_owners
        ):
            raise CorporateActionReconciliationError(
                "required_primary_owners contains invalid owner"
            )
        if (
            isinstance(self.minimum_independent_sources, bool)
            or not isinstance(self.minimum_independent_sources, int)
            or self.minimum_independent_sources < 1
        ):
            raise CorporateActionReconciliationError(
                "minimum_independent_sources must be a positive integer"
            )
        _require_bool(
            self.require_reference_price_evidence,
            "require_reference_price_evidence",
        )
        _require_bool(self.require_license_clearance, "require_license_clearance")
        _require_bool(
            self.require_attachment_evidence,
            "require_attachment_evidence",
        )
        _require_bool(
            self.allow_synthetic_eligibility_test,
            "allow_synthetic_eligibility_test",
        )
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionReconciliationError(
                "Stage 2E policy fixtures are synthetic-only and cannot be relabelled"
            )
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "stage2e-reconciliation-policy-v1",
                    "policy_version": self.policy_version,
                    "required_primary_owners": self.required_primary_owners,
                    "minimum_independent_sources": self.minimum_independent_sources,
                    "require_reference_price_evidence": (
                        self.require_reference_price_evidence
                    ),
                    "require_license_clearance": self.require_license_clearance,
                    "require_attachment_evidence": self.require_attachment_evidence,
                    "allow_synthetic_eligibility_test": (
                        self.allow_synthetic_eligibility_test
                    ),
                    "synthetic_fixture": self.synthetic_fixture,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationConflict:
    logical_action_id: str
    kind: ConflictKind
    candidate_ids: tuple[str, ...]
    observed_values: tuple[str, ...]
    conflict_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.logical_action_id, "logical_action_id")
        if not isinstance(self.kind, ConflictKind):
            raise CorporateActionReconciliationError("kind must be ConflictKind")
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise CorporateActionReconciliationError(
                "candidate_ids must be sorted and unique"
            )
        if self.observed_values != tuple(sorted(set(self.observed_values))):
            raise CorporateActionReconciliationError(
                "observed_values must be sorted and unique"
            )
        object.__setattr__(
            self,
            "conflict_id",
            fingerprint(
                {
                    "schema": "stage2e-reconciliation-conflict-v1",
                    "logical_action_id": self.logical_action_id,
                    "kind": self.kind,
                    "candidate_ids": self.candidate_ids,
                    "observed_values": self.observed_values,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconciledLogicalAction:
    logical_action_id: str
    instrument_id: str
    candidate_ids: tuple[str, ...]
    source_owners: tuple[CorporateActionSourceOwner, ...]
    conflict_ids: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    action_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.logical_action_id, "logical_action_id")
        _require_text(self.instrument_id, "instrument_id")
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise CorporateActionReconciliationError(
                "candidate_ids must be sorted and unique"
            )
        if self.source_owners != tuple(
            sorted(set(self.source_owners), key=lambda item: item.value)
        ):
            raise CorporateActionReconciliationError(
                "source_owners must be sorted and unique"
            )
        if self.conflict_ids != tuple(sorted(set(self.conflict_ids))):
            raise CorporateActionReconciliationError(
                "conflict_ids must be sorted and unique"
            )
        if self.unresolved_gaps != tuple(sorted(set(self.unresolved_gaps))):
            raise CorporateActionReconciliationError(
                "unresolved_gaps must be sorted and unique"
            )
        object.__setattr__(
            self,
            "action_id",
            fingerprint(
                {
                    "schema": "stage2e-reconciled-logical-action-v1",
                    "logical_action_id": self.logical_action_id,
                    "instrument_id": self.instrument_id,
                    "candidate_ids": self.candidate_ids,
                    "source_owners": self.source_owners,
                    "conflict_ids": self.conflict_ids,
                    "unresolved_gaps": self.unresolved_gaps,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class PromotionEligibility:
    status: PromotionEligibilityStatus
    reasons: tuple[str, ...]
    policy_id: str
    eligibility_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, PromotionEligibilityStatus):
            raise CorporateActionReconciliationError(
                "status must be PromotionEligibilityStatus"
            )
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise CorporateActionReconciliationError(
                "eligibility reasons must be sorted and unique"
            )
        if len(self.policy_id) != 64:
            raise CorporateActionReconciliationError("policy_id must be SHA-256")
        object.__setattr__(
            self,
            "eligibility_id",
            fingerprint(
                {
                    "schema": "stage2e-promotion-eligibility-v1",
                    "status": self.status,
                    "reasons": self.reasons,
                    "policy_id": self.policy_id,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CorporateActionReconciliationReport:
    as_of: datetime
    policy_id: str
    bundle_ids: tuple[str, ...]
    coverage_claim_ids: tuple[str, ...]
    logical_actions: tuple[ReconciledLogicalAction, ...]
    conflicts: tuple[ReconciliationConflict, ...]
    global_gaps: tuple[str, ...]
    eligibility: PromotionEligibility
    synthetic_fixture: bool = True
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        ensure_aware(self.as_of, "as_of")
        for name in ("policy_id",):
            value = getattr(self, name)
            if len(value) != 64:
                raise CorporateActionReconciliationError(f"{name} must be SHA-256")
        for name in ("bundle_ids", "coverage_claim_ids"):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise CorporateActionReconciliationError(
                    f"{name} must be sorted and unique"
                )
        action_order = tuple(
            (item.instrument_id, item.logical_action_id, item.action_id)
            for item in self.logical_actions
        )
        if action_order != tuple(sorted(action_order)):
            raise CorporateActionReconciliationError(
                "logical actions must be deterministically sorted"
            )
        conflict_order = tuple(
            (item.logical_action_id, item.kind.value, item.conflict_id)
            for item in self.conflicts
        )
        if conflict_order != tuple(sorted(conflict_order)):
            raise CorporateActionReconciliationError(
                "conflicts must be deterministically sorted"
            )
        if self.global_gaps != tuple(sorted(set(self.global_gaps))):
            raise CorporateActionReconciliationError(
                "global_gaps must be sorted and unique"
            )
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionReconciliationError(
                "reconciliation report is synthetic-only and cannot be relabelled"
            )
        if self.eligibility.policy_id != self.policy_id:
            raise CorporateActionReconciliationError(
                "eligibility policy identity mismatch"
            )
        object.__setattr__(
            self,
            "report_id",
            fingerprint(
                {
                    "schema": "stage2e-corporate-action-reconciliation-report-v1",
                    "as_of": to_utc(self.as_of),
                    "policy_id": self.policy_id,
                    "bundle_ids": self.bundle_ids,
                    "coverage_claim_ids": self.coverage_claim_ids,
                    "logical_action_ids": [
                        item.action_id for item in self.logical_actions
                    ],
                    "conflict_ids": [item.conflict_id for item in self.conflicts],
                    "global_gaps": self.global_gaps,
                    "eligibility_id": self.eligibility.eligibility_id,
                    "synthetic_fixture": self.synthetic_fixture,
                }
            ),
        )


def _value_text(value: object) -> str:
    if isinstance(value, datetime):
        return to_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return repr(value)


_COMPARE_FIELDS = (
    ("lifecycle", ConflictKind.LIFECYCLE),
    ("ex_date", ConflictKind.EX_DATE),
    ("record_date", ConflictKind.RECORD_DATE),
    ("payment_date", ConflictKind.PAYMENT_DATE),
    ("share_listing_date", ConflictKind.SHARE_LISTING_DATE),
    ("effective_date", ConflictKind.EFFECTIVE_DATE),
    ("action_type", ConflictKind.ACTION_TYPE),
    ("automatic_share_ratio", ConflictKind.AUTOMATIC_SHARE_RATIO),
    ("cash_dividend_per_share", ConflictKind.CASH_DIVIDEND),
    ("rights_entitlement_ratio", ConflictKind.RIGHTS_RATIO),
    ("rights_subscription_price", ConflictKind.RIGHTS_PRICE),
    ("currency", ConflictKind.CURRENCY),
    ("reference_price", ConflictKind.REFERENCE_PRICE),
    ("reference_price_snapshot_id", ConflictKind.REFERENCE_PRICE_EVIDENCE),
    ("identity_fact_id", ConflictKind.IDENTITY),
)


def _resolve_visible_candidates(
    candidates: Iterable[CandidateCorporateAction],
    as_of: datetime,
) -> tuple[CandidateCorporateAction, ...]:
    try:
        return resolve_corporate_action_candidates(candidates, as_of=as_of)
    except Exception as exc:
        raise CorporateActionReconciliationError(str(exc)) from exc


def reconcile_corporate_actions(
    *,
    bundles: Sequence[BoundCorporateActionCandidateBundle],
    action_mappings: Sequence[CandidateActionMapping],
    coverage_claims: Sequence[CoverageClaimCandidate],
    policy: ReconciliationPolicy,
    as_of: datetime,
) -> CorporateActionReconciliationReport:
    """Reconcile bound candidates under an explicit mapping and policy."""

    cutoff = to_utc(as_of, "as_of")
    if not isinstance(policy, ReconciliationPolicy):
        raise CorporateActionReconciliationError(
            "policy must be ReconciliationPolicy"
        )
    bundle_ids = tuple(sorted({bundle.bundle_id for bundle in bundles}))
    if len(bundle_ids) != len(bundles):
        raise CorporateActionReconciliationError("duplicate bundles are forbidden")
    all_candidates_by_id: dict[str, CandidateCorporateAction] = {}
    candidates_by_id: dict[str, CandidateCorporateAction] = {}
    bundle_gaps: set[str] = set()
    for bundle in bundles:
        for candidate in bundle.candidates:
            existing = all_candidates_by_id.get(candidate.candidate_id)
            if existing is not None and existing != candidate:
                raise CorporateActionReconciliationError(
                    "same candidate_id has conflicting payload"
                )
            all_candidates_by_id[candidate.candidate_id] = candidate
        if to_utc(bundle.as_of) > cutoff:
            bundle_gaps.add(f"FUTURE_BUNDLE:{bundle.bundle_id}")
            continue
        bundle_gaps.update(f"BUNDLE_GAP:{gap}" for gap in bundle.gaps)
        for candidate in bundle.candidates:
            if (
                to_utc(candidate.known_at) <= cutoff
                and to_utc(candidate.usable_from) <= cutoff
            ):
                candidates_by_id[candidate.candidate_id] = candidate
            else:
                bundle_gaps.add(f"FUTURE_CANDIDATE:{candidate.candidate_id}")

    mapping_by_candidate: dict[str, CandidateActionMapping] = {}
    logical_to_candidates: dict[str, list[CandidateCorporateAction]] = defaultdict(list)
    for mapping in action_mappings:
        if mapping.mapping_policy_version != policy.policy_version:
            raise CorporateActionReconciliationError(
                "action mapping policy version mismatch"
            )
        if mapping.candidate_id in mapping_by_candidate:
            raise CorporateActionReconciliationError(
                "candidate has multiple logical action mappings"
            )
        candidate = all_candidates_by_id.get(mapping.candidate_id)
        if candidate is None:
            raise CorporateActionReconciliationError(
                "action mapping references missing candidate"
            )
        mapping_by_candidate[mapping.candidate_id] = mapping
        if mapping.candidate_id in candidates_by_id:
            logical_to_candidates[mapping.logical_action_id].append(candidate)
    unmapped = sorted(set(candidates_by_id) - set(mapping_by_candidate))
    bundle_gaps.update(f"UNMAPPED_CANDIDATE:{item}" for item in unmapped)

    conflicts: list[ReconciliationConflict] = []
    reconciled: list[ReconciledLogicalAction] = []
    for logical_action_id, raw_candidates in logical_to_candidates.items():
        terminals = _resolve_visible_candidates(raw_candidates, cutoff)
        action_conflicts: list[ReconciliationConflict] = []
        instrument_ids = {item.instrument_id for item in terminals}
        if len(instrument_ids) != 1:
            action_conflicts.append(
                ReconciliationConflict(
                    logical_action_id=logical_action_id,
                    kind=ConflictKind.IDENTITY,
                    candidate_ids=tuple(
                        sorted(item.candidate_id for item in terminals)
                    ),
                    observed_values=tuple(sorted(instrument_ids)),
                )
            )
            instrument_id = "CONFLICT"
        else:
            instrument_id = next(iter(instrument_ids))
        for field_name, kind in _COMPARE_FIELDS:
            values = {
                _value_text(getattr(candidate, field_name))
                for candidate in terminals
            }
            if len(values) > 1:
                action_conflicts.append(
                    ReconciliationConflict(
                        logical_action_id=logical_action_id,
                        kind=kind,
                        candidate_ids=tuple(
                            sorted(item.candidate_id for item in terminals)
                        ),
                        observed_values=tuple(sorted(values)),
                    )
                )
        conflicts.extend(action_conflicts)
        gaps = {gap for item in terminals for gap in item.gaps}
        action_ids_by_owner: dict[CorporateActionSourceOwner, set[str]] = defaultdict(set)
        for item in terminals:
            action_ids_by_owner[item.source_owner].add(item.action_id)
        if any(len(action_ids) > 1 for action_ids in action_ids_by_owner.values()):
            gaps.add("AMBIGUOUS_LOGICAL_ACTION_MAPPING")
        if not terminals:
            gaps.add("NO_VISIBLE_TERMINAL_CANDIDATE")
        reconciled.append(
            ReconciledLogicalAction(
                logical_action_id=logical_action_id,
                instrument_id=instrument_id,
                candidate_ids=tuple(
                    sorted(item.candidate_id for item in terminals)
                ),
                source_owners=tuple(
                    sorted(
                        {item.source_owner for item in terminals},
                        key=lambda owner: owner.value,
                    )
                ),
                conflict_ids=tuple(
                    sorted(item.conflict_id for item in action_conflicts)
                ),
                unresolved_gaps=tuple(sorted(gaps)),
            )
        )

    visible_claims = [
        claim
        for claim in coverage_claims
        if to_utc(claim.known_at) <= cutoff and to_utc(claim.usable_from) <= cutoff
    ]
    coverage_claim_ids = tuple(
        sorted(claim.coverage_claim_id for claim in visible_claims)
    )
    global_gaps = set(bundle_gaps)
    required_owners = set(policy.required_primary_owners)
    visible_coverage_owners = {claim.source_owner for claim in visible_claims}
    visible_candidate_owners = {
        candidate.source_owner for candidate in candidates_by_id.values()
    }
    for owner in sorted(
        required_owners - visible_coverage_owners,
        key=lambda item: item.value,
    ):
        global_gaps.add(f"MISSING_REQUIRED_PRIMARY_COVERAGE:{owner.value}")
    for owner in sorted(
        required_owners - visible_candidate_owners,
        key=lambda item: item.value,
    ):
        global_gaps.add(f"MISSING_REQUIRED_PRIMARY_CANDIDATE:{owner.value}")
    for action in reconciled:
        if len(action.source_owners) < policy.minimum_independent_sources:
            global_gaps.add(
                f"INSUFFICIENT_INDEPENDENT_SOURCES:{action.logical_action_id}"
            )
        if policy.require_attachment_evidence:
            terminal_candidates = [
                candidates_by_id[candidate_id]
                for candidate_id in action.candidate_ids
                if candidate_id in candidates_by_id
            ]
            if not any(
                "ATTACHMENT" in candidate.source_family.value
                for candidate in terminal_candidates
            ):
                global_gaps.add(
                    f"MISSING_ATTACHMENT_EVIDENCE:{action.logical_action_id}"
                )
        if action.conflict_ids:
            global_gaps.add(f"ACTION_CONFLICT:{action.logical_action_id}")
        if action.unresolved_gaps:
            global_gaps.add(f"ACTION_GAPS:{action.logical_action_id}")
    if not visible_claims:
        global_gaps.add("NO_VISIBLE_COVERAGE_CLAIMS")
    for logical_action_id, raw_candidates in logical_to_candidates.items():
        terminals = _resolve_visible_candidates(raw_candidates, cutoff)
        for candidate in terminals:
            event_date = candidate.ex_date or candidate.effective_date
            matching_claims = [
                claim
                for claim in visible_claims
                if claim.instrument_id == candidate.instrument_id
                and claim.source_owner is candidate.source_owner
                and claim.source_version == candidate.source_version
                and event_date is not None
                and claim.start_date <= event_date <= claim.end_date
                and candidate.action_id in claim.surveyed_source_event_ids
            ]
            if not matching_claims:
                global_gaps.add(
                    "MISSING_COVERAGE_CLAIM:"
                    f"{logical_action_id}:{candidate.candidate_id}"
                )
            elif len(matching_claims) > 1:
                global_gaps.add(
                    "AMBIGUOUS_COVERAGE_CLAIM:"
                    f"{logical_action_id}:{candidate.candidate_id}"
                )
    if policy.require_license_clearance and any(
        claim.license_status is not LicenseStatus.CLEARED_FOR_INTERNAL_RESEARCH
        for claim in visible_claims
    ):
        global_gaps.add("LICENSE_NOT_CLEARED")
    if policy.require_reference_price_evidence:
        for candidate in candidates_by_id.values():
            monetary = (
                (candidate.cash_dividend_per_share or 0) > 0
                or (candidate.rights_entitlement_ratio or 0) > 0
            )
            if monetary and candidate.reference_price_snapshot_id is None:
                global_gaps.add(
                    f"MISSING_REFERENCE_PRICE_EVIDENCE:{candidate.candidate_id}"
                )
    if not policy.allow_synthetic_eligibility_test:
        global_gaps.add("SYNTHETIC_ONLY_NOT_PROMOTABLE")

    reasons = tuple(sorted(global_gaps))
    eligibility = PromotionEligibility(
        status=(
            PromotionEligibilityStatus.ELIGIBLE_FOR_INDEPENDENT_VERIFICATION
            if not reasons
            else PromotionEligibilityStatus.NOT_ELIGIBLE
        ),
        reasons=reasons,
        policy_id=policy.policy_id,
    )
    return CorporateActionReconciliationReport(
        as_of=cutoff,
        policy_id=policy.policy_id,
        bundle_ids=bundle_ids,
        coverage_claim_ids=coverage_claim_ids,
        logical_actions=tuple(
            sorted(
                reconciled,
                key=lambda item: (
                    item.instrument_id,
                    item.logical_action_id,
                    item.action_id,
                ),
            )
        ),
        conflicts=tuple(
            sorted(
                conflicts,
                key=lambda item: (
                    item.logical_action_id,
                    item.kind.value,
                    item.conflict_id,
                ),
            )
        ),
        global_gaps=reasons,
        eligibility=eligibility,
        synthetic_fixture=True,
    )


__all__ = [
    "CandidateActionMapping",
    "ConflictKind",
    "CorporateActionReconciliationError",
    "CorporateActionReconciliationReport",
    "CoverageClaimCandidate",
    "LicenseStatus",
    "PromotionEligibility",
    "PromotionEligibilityStatus",
    "ReconciledLogicalAction",
    "ReconciliationConflict",
    "ReconciliationPolicy",
    "reconcile_corporate_actions",
]
