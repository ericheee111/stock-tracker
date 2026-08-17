from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Protocol, cast

from stock_tracker.core.types import Market
from stock_tracker.quant.core.calendar import (
    CalendarStatus,
    select_superseding_revision,
)
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.point_in_time import PITConflictError, revision_key
from stock_tracker.quant.core.time import exchange_local_date, to_utc
from stock_tracker.quant.core.universe import UniverseMembershipState

from .calendar_adapter import (
    CalendarAdapterError,
    CalendarCandidateDocument,
    CalendarSourceFamily,
    CandidateCalendarFact,
    Exchange,
    NoticeType,
    load_calendar_parse_descriptor,
    parse_calendar_from_descriptor,
)
from .security_universe_adapter import (
    IdentityCandidate,
    MembershipCandidate,
    SecurityUniverseAdapterError,
    SecurityUniverseCandidateBundle,
    SourceListingState,
    SourceRiskDesignation,
    SourceTradingState,
    StatusCandidate,
    StatusScope,
    UniverseCoverageReport,
    parse_security_universe_artifact,
    read_security_universe_descriptor,
)

RECONCILIATION_SCHEMA = "stage2-reconciliation-report-v1"
DEFAULT_RECONCILIATION_POLICY_VERSION = "stage2-reconciliation-policy-v1"
LICENSE_PENDING = "LICENSE_PENDING"
T3_NOT_REACHED = "T3_NOT_REACHED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_FORBIDDEN_PROMOTION_FIELDS = frozenset(
    {
        "verified",
        "complete",
        "trust_tier",
        "t2_achieved",
        "t3_achieved",
        "research_grade",
    }
)
_BASE_INHERITED_BLOCKERS = frozenset({LICENSE_PENDING, T3_NOT_REACHED})
_BASE_SECURITY_TRUST_BLOCKERS = frozenset(
    {
        "ADAPTER_UNVERIFIED_INCOMPLETE",
        "SOURCE_SECURITY_ID_STABILITY_UNPROVEN",
        "UPSTREAM_RAW_PROVENANCE_INCOMPLETE",
    }
)
_EVIDENCE_KIND_BY_BLOCKER = {
    "ADAPTER_UNVERIFIED_INCOMPLETE": "ADAPTER_VALIDATION",
    "SOURCE_SECURITY_ID_STABILITY_UNPROVEN": "SOURCE_ID_STABILITY_CONTRACT",
    "UPSTREAM_RAW_PROVENANCE_INCOMPLETE": "UPSTREAM_RAW_CHAIN",
    LICENSE_PENDING: "LICENSE_APPROVAL",
    T3_NOT_REACHED: "T3_PROMOTION_DECISION",
}


class ReconciliationContractError(ValueError):
    pass


class FindingSeverity(StrEnum):
    HARD_BLOCK = "HARD_BLOCK"
    TRUST_BLOCK = "TRUST_BLOCK"
    WARNING = "WARNING"
    INFO = "INFO"


class BlockerStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED_WITH_EVIDENCE = "CLOSED_WITH_EVIDENCE"


class ClosureEvidenceKind(StrEnum):
    GENERAL_VALIDATION = "GENERAL_VALIDATION"
    ADAPTER_VALIDATION = "ADAPTER_VALIDATION"
    SOURCE_ID_STABILITY_CONTRACT = "SOURCE_ID_STABILITY_CONTRACT"
    UPSTREAM_RAW_CHAIN = "UPSTREAM_RAW_CHAIN"
    LICENSE_APPROVAL = "LICENSE_APPROVAL"
    T3_PROMOTION_DECISION = "T3_PROMOTION_DECISION"


class ReconciliationInputError(ReconciliationContractError):
    def __init__(self, code: str, severity: FindingSeverity, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.severity = severity


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ReconciliationContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ReconciliationContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ReconciliationContractError(f"{name} must be a boolean")
    return value


def _sorted_texts(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(_require_text(value, f"{name} item") for value in values)
    if len(result) != len(set(result)):
        raise ReconciliationContractError(f"{name} must not contain duplicates")
    return tuple(sorted(result))


def _canonical_texts(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(_require_text(value, f"{name} item") for value in values)
    return tuple(sorted(set(result)))


def _finding_sort_key(value: Finding) -> tuple[str, ...]:
    return (
        value.severity.value,
        value.code,
        value.scope,
        value.message,
        *value.subject_ids,
        *value.evidence_ids,
        *value.details,
    )


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: FindingSeverity
    scope: str
    message: str
    subject_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.code, "finding code")
        if not isinstance(self.severity, FindingSeverity):
            raise ReconciliationContractError("finding severity is invalid")
        _require_text(self.scope, "finding scope")
        _require_text(self.message, "finding message")
        object.__setattr__(
            self,
            "subject_ids",
            _canonical_texts(self.subject_ids, "subject_ids"),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _canonical_texts(self.evidence_ids, "evidence_ids"),
        )
        object.__setattr__(self, "details", _canonical_texts(self.details, "details"))

    @property
    def finding_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.value,
            "scope": self.scope,
            "message": self.message,
            "subject_ids": list(self.subject_ids),
            "evidence_ids": list(self.evidence_ids),
            "details": list(self.details),
        }
        if include_id:
            result["finding_id"] = self.finding_id
        return result


@dataclass(frozen=True, slots=True)
class ExternalClosureEvidence:
    evidence_id: str
    kind: ClosureEvidenceKind
    supported_blocker_codes: tuple[str, ...]
    source_owner: str
    source_version: str
    synthetic: bool
    independently_approved: bool
    upstream_raw_artifact_ids: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        if not isinstance(self.kind, ClosureEvidenceKind):
            raise ReconciliationContractError("closure evidence kind is invalid")
        object.__setattr__(
            self,
            "supported_blocker_codes",
            _sorted_texts(self.supported_blocker_codes, "supported_blocker_codes"),
        )
        _require_text(self.source_owner, "source_owner")
        _require_text(self.source_version, "source_version")
        _require_bool(self.synthetic, "synthetic")
        _require_bool(self.independently_approved, "independently_approved")
        object.__setattr__(
            self,
            "upstream_raw_artifact_ids",
            _sorted_texts(self.upstream_raw_artifact_ids, "upstream_raw_artifact_ids"),
        )
        object.__setattr__(self, "details", _sorted_texts(self.details, "details"))
        if self.evidence_id != fingerprint(self._identity_payload()):
            raise ReconciliationContractError(
                "evidence_id does not match external closure evidence content"
            )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "stage2-external-closure-evidence-v1",
            "kind": self.kind.value,
            "supported_blocker_codes": list(self.supported_blocker_codes),
            "source_owner": self.source_owner,
            "source_version": self.source_version,
            "synthetic": self.synthetic,
            "independently_approved": self.independently_approved,
            "upstream_raw_artifact_ids": list(self.upstream_raw_artifact_ids),
            "details": list(self.details),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "evidence_id": self.evidence_id}


@dataclass(frozen=True, slots=True)
class BlockerClosureRequest:
    code: str
    closing_evidence_ids: tuple[str, ...]
    closing_reason: str
    policy_version: str

    def __post_init__(self) -> None:
        _require_text(self.code, "closure code")
        if not self.closing_evidence_ids:
            raise ReconciliationContractError(
                "CLOSED_WITH_EVIDENCE requires closing_evidence_ids"
            )
        object.__setattr__(
            self,
            "closing_evidence_ids",
            _sorted_texts(self.closing_evidence_ids, "closing_evidence_ids"),
        )
        _require_text(self.closing_reason, "closing_reason")
        _require_text(self.policy_version, "closure policy_version")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "closing_evidence_ids": list(self.closing_evidence_ids),
            "closing_reason": self.closing_reason,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class InheritedTrustBlocker:
    code: str
    status: BlockerStatus
    closing_evidence_ids: tuple[str, ...] = ()
    closing_reason: str | None = None
    policy_version: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.code, "blocker code")
        if not isinstance(self.status, BlockerStatus):
            raise ReconciliationContractError("blocker status is invalid")
        if self.status is BlockerStatus.OPEN:
            if self.closing_evidence_ids or self.closing_reason is not None or self.policy_version is not None:
                raise ReconciliationContractError("OPEN blocker cannot carry closing evidence")
            return
        if not self.closing_evidence_ids:
            raise ReconciliationContractError(
                "CLOSED_WITH_EVIDENCE requires closing_evidence_ids"
            )
        object.__setattr__(
            self,
            "closing_evidence_ids",
            _sorted_texts(self.closing_evidence_ids, "closing_evidence_ids"),
        )
        _require_text(self.closing_reason, "closing_reason")
        _require_text(self.policy_version, "policy_version")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "status": self.status.value}
        if self.status is BlockerStatus.CLOSED_WITH_EVIDENCE:
            result.update(
                {
                    "closing_evidence_ids": list(self.closing_evidence_ids),
                    "closing_reason": self.closing_reason,
                    "policy_version": self.policy_version,
                }
            )
        return result


def _classify_calendar_error(message: str) -> tuple[str, FindingSeverity]:
    lowered = message.lower()
    if "redirect" in lowered or "official https" in lowered:
        return "CALENDAR_REDIRECT_OWNER_DOMAIN_VIOLATION", FindingSeverity.HARD_BLOCK
    if "raw artifact" in lowered or "hash changed" in lowered or "byte length" in lowered:
        return "CALENDAR_RAW_ARTIFACT_MISMATCH", FindingSeverity.HARD_BLOCK
    if "parse descriptor" in lowered:
        return "CALENDAR_PARSE_DESCRIPTOR_MISMATCH", FindingSeverity.HARD_BLOCK
    if "descriptor" in lowered:
        return "CALENDAR_RAW_DESCRIPTOR_MISMATCH", FindingSeverity.HARD_BLOCK
    if "parser version" in lowered or "parser_version" in lowered:
        return "CALENDAR_PARSER_VERSION_MISMATCH", FindingSeverity.HARD_BLOCK
    if "binding" in lowered or "provenance" in lowered or "identity mismatch" in lowered:
        return "CALENDAR_RAW_PARSE_BINDING_MISMATCH", FindingSeverity.HARD_BLOCK
    return "CALENDAR_INPUT_CONTRACT_INVALID", FindingSeverity.HARD_BLOCK


@dataclass(frozen=True, slots=True)
class CalendarReconciliationInput:
    parse_descriptor_id: str
    parse_descriptor_key: str
    raw_descriptor_id: str
    raw_descriptor_key: str
    raw_artifact_id: str
    parser_version: str
    source_owner: Exchange
    source_family: CalendarSourceFamily
    source_version: str
    document: CalendarCandidateDocument

    def __post_init__(self) -> None:
        for name in (
            "parse_descriptor_id",
            "raw_descriptor_id",
            "raw_artifact_id",
        ):
            _require_sha256(getattr(self, name), name)
        for name in (
            "parse_descriptor_key",
            "raw_descriptor_key",
            "parser_version",
            "source_version",
        ):
            _require_text(getattr(self, name), name)
        if self.document.parser_version != self.parser_version:
            raise ReconciliationContractError("calendar document parser version mismatch")
        provenance = self.document.provenance
        if provenance.raw_artifact_id != self.raw_artifact_id:
            raise ReconciliationContractError("calendar document raw artifact mismatch")
        if provenance.source_owner is not self.source_owner:
            raise ReconciliationContractError("calendar document source owner mismatch")
        if provenance.source_family is not self.source_family:
            raise ReconciliationContractError("calendar document source family mismatch")
        if provenance.source_version != self.source_version:
            raise ReconciliationContractError("calendar document source version mismatch")

    @classmethod
    def from_parse_descriptor(
        cls,
        root: str | Path,
        parse_descriptor_key: str,
    ) -> CalendarReconciliationInput:
        try:
            document = parse_calendar_from_descriptor(
                root,
                parse_descriptor_key=parse_descriptor_key,
            )
            descriptor, capture, _ = load_calendar_parse_descriptor(
                root,
                parse_descriptor_key=parse_descriptor_key,
            )
        except (CalendarAdapterError, OSError, ValueError) as exc:
            code, severity = _classify_calendar_error(str(exc))
            raise ReconciliationInputError(code, severity, str(exc)) from exc
        return cls(
            parse_descriptor_id=descriptor.parse_descriptor_id,
            parse_descriptor_key=descriptor.parse_descriptor_key,
            raw_descriptor_id=descriptor.raw_descriptor_id,
            raw_descriptor_key=descriptor.raw_descriptor_key,
            raw_artifact_id=descriptor.raw_artifact_id,
            parser_version=descriptor.parser_version,
            source_owner=capture.source_owner,
            source_family=capture.source_family,
            source_version=capture.source_version,
            document=document,
        )

    @property
    def input_id(self) -> str:
        return fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id_basis_schema": "stage2-calendar-input-v1",
            "parse_descriptor_id": self.parse_descriptor_id,
            "parse_descriptor_key": self.parse_descriptor_key,
            "raw_descriptor_id": self.raw_descriptor_id,
            "raw_descriptor_key": self.raw_descriptor_key,
            "raw_artifact_id": self.raw_artifact_id,
            "parser_version": self.parser_version,
            "source_owner": self.source_owner.value,
            "source_family": self.source_family.value,
            "source_version": self.source_version,
            "document_id": self.document.document_id,
            "candidate_fact_ids": sorted(fact.candidate_id for fact in self.document.facts),
            "gaps": sorted(self.document.gaps),
        }


@dataclass(frozen=True, slots=True)
class SecurityUniverseReconciliationInput:
    normalized_artifact_id: str
    descriptor_id: str
    bundle_id: str
    coverage_report_id: str
    source: str
    source_dataset: str
    source_version: str
    parser_version: str
    schema_version: str
    exchange: str
    universe_id: str
    synthetic: bool
    bundle: SecurityUniverseCandidateBundle

    def __post_init__(self) -> None:
        for name in (
            "normalized_artifact_id",
            "descriptor_id",
            "bundle_id",
            "coverage_report_id",
        ):
            _require_sha256(getattr(self, name), name)
        for name in (
            "source",
            "source_dataset",
            "source_version",
            "parser_version",
            "schema_version",
            "exchange",
            "universe_id",
        ):
            _require_text(getattr(self, name), name)
        _require_bool(self.synthetic, "synthetic")
        descriptor = self.bundle.descriptor
        expected = (
            descriptor.artifact_id,
            self.bundle.bundle_id,
            self.bundle.coverage_report.report_id,
            descriptor.source,
            descriptor.source_dataset,
            descriptor.source_version,
            descriptor.parser_version,
            descriptor.schema_version,
            descriptor.exchange,
            descriptor.universe_id,
            descriptor.synthetic,
        )
        actual = (
            self.normalized_artifact_id,
            self.bundle_id,
            self.coverage_report_id,
            self.source,
            self.source_dataset,
            self.source_version,
            self.parser_version,
            self.schema_version,
            self.exchange,
            self.universe_id,
            self.synthetic,
        )
        if actual != expected:
            raise ReconciliationContractError("security input identity disagrees with bundle")
        if self.descriptor_id != fingerprint(descriptor.as_dict()):
            raise ReconciliationContractError("security descriptor_id mismatch")

    @classmethod
    def from_bundle(
        cls,
        bundle: SecurityUniverseCandidateBundle,
    ) -> SecurityUniverseReconciliationInput:
        descriptor = bundle.descriptor
        return cls(
            normalized_artifact_id=descriptor.artifact_id,
            descriptor_id=fingerprint(descriptor.as_dict()),
            bundle_id=bundle.bundle_id,
            coverage_report_id=bundle.coverage_report.report_id,
            source=descriptor.source,
            source_dataset=descriptor.source_dataset,
            source_version=descriptor.source_version,
            parser_version=descriptor.parser_version,
            schema_version=descriptor.schema_version,
            exchange=descriptor.exchange,
            universe_id=descriptor.universe_id,
            synthetic=descriptor.synthetic,
            bundle=bundle,
        )

    @classmethod
    def from_artifact_files(
        cls,
        artifact_path: str | Path,
        descriptor_path: str | Path,
    ) -> SecurityUniverseReconciliationInput:
        try:
            descriptor = read_security_universe_descriptor(descriptor_path)
            bundle = parse_security_universe_artifact(Path(artifact_path).read_bytes(), descriptor)
        except (SecurityUniverseAdapterError, OSError, ValueError) as exc:
            message = str(exc)
            lowered = message.lower()
            code = (
                "SECURITY_NORMALIZED_ARTIFACT_MISMATCH"
                if "sha-256" in lowered or "byte_size" in lowered
                else "SECURITY_DESCRIPTOR_MISMATCH"
                if "descriptor" in lowered
                else "SECURITY_INPUT_CONTRACT_INVALID"
            )
            raise ReconciliationInputError(code, FindingSeverity.HARD_BLOCK, message) from exc
        return cls.from_bundle(bundle)

    @property
    def input_id(self) -> str:
        return fingerprint(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id_basis_schema": "stage2-security-universe-input-v1",
            "normalized_artifact_id": self.normalized_artifact_id,
            "descriptor_id": self.descriptor_id,
            "bundle_id": self.bundle_id,
            "coverage_report_id": self.coverage_report_id,
            "source": self.source,
            "source_dataset": self.source_dataset,
            "source_version": self.source_version,
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "exchange": self.exchange,
            "universe_id": self.universe_id,
            "synthetic": self.synthetic,
            "required_session_dates": [
                value.isoformat() for value in self.bundle.required_session_dates
            ],
            "identity_candidate_ids": sorted(item.candidate_id for item in self.bundle.identities),
            "status_candidate_ids": sorted(item.candidate_id for item in self.bundle.statuses),
            "membership_candidate_ids": sorted(
                item.candidate_id for item in self.bundle.memberships
            ),
            "inherited_trust_blocker_codes": list(
                self.bundle.coverage_report.trust_blocker_codes
            ),
        }


@dataclass(frozen=True, slots=True)
class CoverageMetrics:
    calendar_expected_civil_dates: int
    calendar_observed_civil_dates: int
    calendar_missing_civil_dates: tuple[str, ...]
    calendar_open_dates: tuple[str, ...]
    security_bundle_count: int
    identity_candidate_count: int
    status_candidate_count: int
    membership_candidate_count: int
    included_membership_count: int
    excluded_membership_count: int
    required_status_count: int
    observed_required_status_count: int
    missing_required_status_ids: tuple[str, ...]
    unclosed_delisted_instrument_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "calendar_expected_civil_dates",
            "calendar_observed_civil_dates",
            "security_bundle_count",
            "identity_candidate_count",
            "status_candidate_count",
            "membership_candidate_count",
            "included_membership_count",
            "excluded_membership_count",
            "required_status_count",
            "observed_required_status_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReconciliationContractError(f"{field_name} must be a non-negative integer")
        for field_name in (
            "calendar_missing_civil_dates",
            "calendar_open_dates",
            "missing_required_status_ids",
            "unclosed_delisted_instrument_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_texts(getattr(self, field_name), field_name),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "calendar_expected_civil_dates": self.calendar_expected_civil_dates,
            "calendar_observed_civil_dates": self.calendar_observed_civil_dates,
            "calendar_missing_civil_dates": list(self.calendar_missing_civil_dates),
            "calendar_open_dates": list(self.calendar_open_dates),
            "security_bundle_count": self.security_bundle_count,
            "identity_candidate_count": self.identity_candidate_count,
            "status_candidate_count": self.status_candidate_count,
            "membership_candidate_count": self.membership_candidate_count,
            "included_membership_count": self.included_membership_count,
            "excluded_membership_count": self.excluded_membership_count,
            "required_status_count": self.required_status_count,
            "observed_required_status_count": self.observed_required_status_count,
            "missing_required_status_ids": list(self.missing_required_status_ids),
            "unclosed_delisted_instrument_ids": list(
                self.unclosed_delisted_instrument_ids
            ),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    reconciliation_policy_version: str
    as_of: datetime
    calendar_inputs: tuple[CalendarReconciliationInput, ...]
    security_universe_inputs: tuple[SecurityUniverseReconciliationInput, ...]
    closure_requests: tuple[BlockerClosureRequest, ...] = ()
    external_closure_evidence: tuple[ExternalClosureEvidence, ...] = ()
    additional_findings: tuple[Finding, ...] = ()
    findings: tuple[Finding, ...] = field(init=False)
    inherited_trust_blockers: tuple[InheritedTrustBlocker, ...] = field(init=False)
    coverage_metrics: CoverageMetrics = field(init=False)
    unresolved_gaps: tuple[str, ...] = field(init=False)
    license_status: str = field(init=False, default=LICENSE_PENDING)
    evidence_tier_status: str = field(init=False, default=T3_NOT_REACHED)

    def __post_init__(self) -> None:
        policy = _require_text(
            self.reconciliation_policy_version,
            "reconciliation_policy_version",
        )
        cutoff = to_utc(self.as_of, "as_of")
        calendars = tuple(sorted(self.calendar_inputs, key=lambda item: item.input_id))
        securities = tuple(
            sorted(self.security_universe_inputs, key=lambda item: item.input_id)
        )
        if not calendars:
            raise ReconciliationContractError("report requires at least one Calendar input")
        if not securities:
            raise ReconciliationContractError("report requires at least one Security/Universe input")
        if len({item.input_id for item in calendars}) != len(calendars):
            raise ReconciliationContractError("report Calendar inputs must be unique by input_id")
        if len({item.input_id for item in securities}) != len(securities):
            raise ReconciliationContractError(
                "report Security/Universe inputs must be unique by input_id"
            )
        requests = tuple(
            sorted(
                self.closure_requests,
                key=lambda item: (
                    item.code,
                    item.policy_version,
                    item.closing_reason,
                    item.closing_evidence_ids,
                ),
            )
        )
        evidence = tuple(sorted(self.external_closure_evidence, key=lambda item: item.evidence_id))
        additional = tuple(sorted(self.additional_findings, key=_finding_sort_key))
        object.__setattr__(self, "reconciliation_policy_version", policy)
        object.__setattr__(self, "as_of", cutoff)
        object.__setattr__(self, "calendar_inputs", calendars)
        object.__setattr__(self, "security_universe_inputs", securities)
        object.__setattr__(self, "closure_requests", requests)
        object.__setattr__(self, "external_closure_evidence", evidence)
        object.__setattr__(self, "additional_findings", additional)

        findings, blockers, coverage, unresolved = _derive_reconciliation_components(
            calendar_inputs=calendars,
            security_universe_inputs=securities,
            as_of=cutoff,
            reconciliation_policy_version=policy,
            closure_requests=requests,
            external_closure_evidence=evidence,
            additional_findings=additional,
        )
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "inherited_trust_blockers", blockers)
        object.__setattr__(self, "coverage_metrics", coverage)
        object.__setattr__(self, "unresolved_gaps", unresolved)

    @property
    def blocker_closures(self) -> tuple[InheritedTrustBlocker, ...]:
        return tuple(
            item
            for item in self.inherited_trust_blockers
            if item.status is BlockerStatus.CLOSED_WITH_EVIDENCE
        )

    @property
    def open_inherited_blockers(self) -> tuple[str, ...]:
        return tuple(
            item.code
            for item in self.inherited_trust_blockers
            if item.status is BlockerStatus.OPEN
        )

    @property
    def finding_counts(self) -> dict[str, int]:
        counter = Counter(item.severity.value for item in self.findings)
        return {severity.value: counter.get(severity.value, 0) for severity in FindingSeverity}

    @property
    def has_hard_blocks(self) -> bool:
        return any(item.severity is FindingSeverity.HARD_BLOCK for item in self.findings)

    @property
    def has_trust_blocks(self) -> bool:
        return bool(self.open_inherited_blockers) or any(
            item.severity is FindingSeverity.TRUST_BLOCK for item in self.findings
        )

    @property
    def candidate_snapshot_state(self) -> str:
        return "HARD_BLOCKED" if self.has_hard_blocks else "STRUCTURALLY_CONSTRUCTIBLE"

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": RECONCILIATION_SCHEMA,
            "reconciliation_policy_version": self.reconciliation_policy_version,
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "calendar_inputs": [item.as_dict() for item in self.calendar_inputs],
            "security_universe_inputs": [
                item.as_dict() for item in self.security_universe_inputs
            ],
            "closure_requests": [item.as_dict() for item in self.closure_requests],
            "external_closure_evidence": [
                item.as_dict() for item in self.external_closure_evidence
            ],
            "additional_findings": [item.as_dict() for item in self.additional_findings],
            "findings": [item.as_dict() for item in self.findings],
            "inherited_trust_blockers": [
                item.as_dict() for item in self.inherited_trust_blockers
            ],
            "blocker_closures": [item.as_dict() for item in self.blocker_closures],
            "coverage_metrics": self.coverage_metrics.as_dict(),
            "unresolved_gaps": list(self.unresolved_gaps),
            "license_status": self.license_status,
            "evidence_tier_status": self.evidence_tier_status,
            "candidate_snapshot_state": self.candidate_snapshot_state,
            "has_hard_blocks": self.has_hard_blocks,
            "has_trust_blocks": self.has_trust_blocks,
            "finding_counts": self.finding_counts,
        }

    @property
    def report_id(self) -> str:
        return fingerprint(self._identity_payload())

    def as_dict(self) -> dict[str, object]:
        payload = {**self._identity_payload(), "report_id": self.report_id}
        validate_reconciliation_output_payload(payload)
        return payload


def validate_reconciliation_output_payload(value: object, path: str = "report") -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(_FORBIDDEN_PROMOTION_FIELDS.intersection(value))
        if forbidden:
            raise ReconciliationContractError(
                f"{path} cannot contain promotion fields: {', '.join(forbidden)}"
            )
        for key, item in value.items():
            if type(key) is not str:
                raise ReconciliationContractError(f"{path} keys must be strings")
            validate_reconciliation_output_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_reconciliation_output_payload(item, f"{path}[{index}]")


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _fact_payload(value: CandidateCalendarFact) -> tuple[object, ...]:
    return (value.status, value.session_kind, value.open_time, value.close_time)


class _VisibleProvenance(Protocol):
    known_at: datetime
    usable_from: datetime


def _visible(provenance: _VisibleProvenance, cutoff: datetime) -> bool:
    return (
        to_utc(provenance.known_at) <= cutoff
        and to_utc(provenance.usable_from) <= cutoff
    )


def _resolve_calendar_stream(
    pairs: list[tuple[CalendarReconciliationInput, CandidateCalendarFact]],
    *,
    exchange: Exchange,
    civil_date: date,
    source_family: CalendarSourceFamily,
    source_version: str,
) -> tuple[CandidateCalendarFact | None, list[Finding]]:
    """Resolve one authority/version stream only through explicit revision ancestry."""

    findings: list[Finding] = []
    facts = [fact for _, fact in pairs]
    try:
        selected = select_superseding_revision(
            facts,
            revision_of=lambda fact: fact.revision_id,
            predecessor_of=lambda fact: fact.supersedes_revision_id,
            payload_of=_fact_payload,
            identity_of=lambda fact: fact.candidate_id,
            known_at_of=lambda fact: fact.known_at,
        )
    except PITConflictError as exc:
        message = str(exc)
        lowered = message.lower()
        code = (
            "CALENDAR_REVISION_CYCLE"
            if "cycle" in lowered or "no terminal" in lowered
            else "CALENDAR_REVISION_PREDECESSOR_MISSING"
            if "missing predecessor" in lowered
            else "CALENDAR_REVISION_BRANCH_CONFLICT"
            if "terminal" in lowered
            else "CALENDAR_REVISION_CONFLICT"
        )
        findings.append(
            Finding(
                code,
                FindingSeverity.HARD_BLOCK,
                (
                    f"calendar:{exchange.value}:"
                    f"{source_family.value}:{source_version}"
                ),
                message,
                subject_ids=tuple(fact.candidate_id for fact in facts),
                details=(civil_date.isoformat(),),
            )
        )
        return None, findings

    distinct_payloads = {_fact_payload(fact) for fact in facts}
    if len(distinct_payloads) > 1:
        explicit_over_inferred = selected.notice_type is not NoticeType.ANNUAL and any(
            fact.notice_type is NoticeType.ANNUAL
            and "WEEKDAY_OPEN_BASELINE_INFERRED" in item.document.gaps
            for item, fact in pairs
        )
        findings.append(
            Finding(
                (
                    "CALENDAR_EXPLICIT_REVISION_OVERRIDES_INFERENCE"
                    if explicit_over_inferred
                    else "CALENDAR_EXPLICIT_REVISION_CHAIN"
                ),
                FindingSeverity.WARNING,
                (
                    f"calendar:{exchange.value}:"
                    f"{source_family.value}:{source_version}"
                ),
                "Shared supersedes-graph resolver selects the terminal Calendar revision; revision audit is retained.",
                subject_ids=tuple(fact.candidate_id for fact in facts),
                details=(civil_date.isoformat(), selected.revision_id),
            )
        )
    return selected, findings


def _analyze_calendar(
    inputs: tuple[CalendarReconciliationInput, ...],
    cutoff: datetime,
) -> tuple[
    list[Finding],
    set[str],
    set[date],
    set[date],
    set[date],
    dict[Exchange, set[date]],
    dict[Exchange, set[date]],
]:
    findings: list[Finding] = []
    gaps: set[str] = set()
    expected_dates: set[date] = set()
    observed_dates: set[date] = set()
    open_dates: set[date] = set()
    observed_dates_by_exchange: dict[Exchange, set[date]] = defaultdict(set)
    open_dates_by_exchange: dict[Exchange, set[date]] = defaultdict(set)
    by_exchange: dict[Exchange, list[CalendarReconciliationInput]] = defaultdict(list)
    for item in inputs:
        by_exchange[item.source_owner].append(item)
        gaps.update(item.document.gaps)
        provenance = item.document.provenance
        expected = set(_date_range(provenance.effective_from, provenance.effective_to))
        actual = {fact.civil_date for fact in item.document.facts}
        visible_actual = {
            fact.civil_date for fact in item.document.facts if _visible(fact, cutoff)
        }
        expected_dates.update(expected)
        observed_dates.update(visible_actual)
        observed_dates_by_exchange[item.source_owner].update(visible_actual)
        missing = sorted(expected - actual)
        if missing:
            findings.append(
                Finding(
                    "CALENDAR_CIVIL_DATE_GAP",
                    FindingSeverity.HARD_BLOCK,
                    f"calendar:{item.source_owner.value}",
                    "Calendar input does not cover every civil date in its effective range.",
                    subject_ids=(item.input_id,),
                    details=tuple(value.isoformat() for value in missing),
                )
            )
        for fact in item.document.facts:
            if fact.usable_from < fact.known_at:
                findings.append(
                    Finding(
                        "CALENDAR_KNOWN_USABLE_CONFLICT",
                        FindingSeverity.HARD_BLOCK,
                        f"calendar:{item.source_owner.value}",
                        "Calendar fact has usable_from before known_at.",
                        subject_ids=(fact.candidate_id,),
                    )
                )
            if not _visible(fact, cutoff):
                findings.append(
                    Finding(
                        "CALENDAR_FACT_NOT_VISIBLE_AS_OF",
                        FindingSeverity.WARNING,
                        f"calendar:{item.source_owner.value}",
                        "A later calendar correction is excluded at this report as_of.",
                        subject_ids=(fact.candidate_id,),
                    )
                )

        for gap in item.document.gaps:
            severity = (
                FindingSeverity.WARNING
                if gap == "WEEKDAY_OPEN_BASELINE_INFERRED"
                else FindingSeverity.TRUST_BLOCK
            )
            findings.append(
                Finding(
                    gap,
                    severity,
                    f"calendar:{item.source_owner.value}",
                    "Calendar adapter gap remains unresolved in reconciliation.",
                    subject_ids=(item.input_id,),
                )
            )

    for exchange, records in by_exchange.items():
        by_family: dict[CalendarSourceFamily, list[CalendarReconciliationInput]] = defaultdict(list)
        for item in records:
            by_family[item.source_family].append(item)
        for family, family_records in by_family.items():
            if len({item.source_version for item in family_records}) > 1:
                findings.append(
                    Finding(
                        "CALENDAR_SOURCE_VERSION_MIXING",
                        FindingSeverity.HARD_BLOCK,
                        f"calendar:{exchange.value}:{family.value}",
                        "One Calendar source family contains multiple source versions.",
                        subject_ids=tuple(item.input_id for item in family_records),
                    )
                )
            if len({item.parser_version for item in family_records}) > 1:
                findings.append(
                    Finding(
                        "CALENDAR_PARSER_VERSION_MISMATCH",
                        FindingSeverity.HARD_BLOCK,
                        f"calendar:{exchange.value}:{family.value}",
                        "One Calendar source family contains multiple parser versions.",
                        subject_ids=tuple(item.input_id for item in family_records),
                    )
                )

        dated: dict[date, list[tuple[CalendarReconciliationInput, CandidateCalendarFact]]] = (
            defaultdict(list)
        )
        for item in records:
            for fact in item.document.facts:
                dated[fact.civil_date].append((item, fact))
        for civil_date, pairs in dated.items():
            visible_pairs = [(item, fact) for item, fact in pairs if _visible(fact, cutoff)]
            if not visible_pairs:
                continue
            by_stream: dict[
                tuple[CalendarSourceFamily, str],
                list[tuple[CalendarReconciliationInput, CandidateCalendarFact]],
            ] = defaultdict(list)
            for pair in visible_pairs:
                by_stream[
                    (pair[0].source_family, pair[0].source_version)
                ].append(pair)

            selected_streams: list[
                tuple[CalendarSourceFamily, str, CandidateCalendarFact]
            ] = []
            for (source_family, source_version), stream_pairs in by_stream.items():
                selected, stream_findings = _resolve_calendar_stream(
                    stream_pairs,
                    exchange=exchange,
                    civil_date=civil_date,
                    source_family=source_family,
                    source_version=source_version,
                )
                findings.extend(stream_findings)
                if selected is not None:
                    selected_streams.append(
                        (source_family, source_version, selected)
                    )
            if not selected_streams:
                continue

            stream_payloads = {
                _fact_payload(fact) for _, _, fact in selected_streams
            }
            if len(stream_payloads) > 1:
                code = (
                    "CALENDAR_OPEN_CLOSED_CONFLICT"
                    if len({fact.status for _, _, fact in selected_streams}) > 1
                    else "CALENDAR_SESSION_CONFLICT"
                )
                findings.append(
                    Finding(
                        code,
                        FindingSeverity.HARD_BLOCK,
                        f"calendar:{exchange.value}",
                        "Independent Calendar source-family/version streams disagree after each revision chain is resolved.",
                        subject_ids=tuple(
                            fact.candidate_id for _, _, fact in selected_streams
                        ),
                        details=(
                            civil_date.isoformat(),
                            *(
                                f"{family.value}/{version}"
                                for family, version, _ in sorted(
                                    selected_streams,
                                    key=lambda item: (
                                        item[0].value,
                                        item[1],
                                        item[2].candidate_id,
                                    ),
                                )
                            ),
                        ),
                    )
                )
                continue

            selected = min(
                (fact for _, _, fact in selected_streams),
                key=lambda fact: fact.candidate_id,
            )
            if selected.status is CalendarStatus.OPEN:
                open_dates.add(civil_date)
                open_dates_by_exchange[exchange].add(civil_date)
    return (
        findings,
        gaps,
        expected_dates,
        observed_dates,
        open_dates,
        observed_dates_by_exchange,
        open_dates_by_exchange,
    )


def _visible_security_candidates(
    item: SecurityUniverseReconciliationInput,
    cutoff: datetime,
) -> tuple[
    tuple[IdentityCandidate, ...],
    tuple[StatusCandidate, ...],
    tuple[MembershipCandidate, ...],
]:
    """Project one immutable bundle to what was actually visible at ``cutoff``."""

    if to_utc(item.bundle.descriptor.retrieved_at) > cutoff:
        return (), (), ()
    identities = tuple(
        candidate
        for candidate in item.bundle.identities
        if _visible(candidate.provenance, cutoff)
    )
    statuses = tuple(
        candidate
        for candidate in item.bundle.statuses
        if _visible(candidate.provenance, cutoff)
    )
    memberships = tuple(
        candidate
        for candidate in item.bundle.memberships
        if _visible(candidate.provenance, cutoff)
    )
    return identities, statuses, memberships


def _report_findings(
    item: SecurityUniverseReconciliationInput,
    report: UniverseCoverageReport,
) -> list[Finding]:
    findings: list[Finding] = []
    hard_fields = {
        "MISSING_IDENTITY": report.missing_identity,
        "MISSING_DAILY_SESSION_STATUS": report.missing_daily_session_status,
        "CROSS_SOURCE_CONFLICTS": report.cross_source_conflicts,
    }
    trust_fields = {
        "UNCLOSED_DELISTINGS": report.unclosed_delistings,
        "MISSING_LISTING_EVENT": report.missing_listing_event,
        "MISSING_EXCLUSION_REASON": report.missing_exclusion_reason,
        "QUANTITY_CONTINUITY_GAPS": report.quantity_continuity_gaps,
        "UNPARSED_ATTACHMENTS": report.unparsed_attachments,
    }
    for code, details in hard_fields.items():
        if details:
            findings.append(
                Finding(
                    code,
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{item.universe_id}",
                    "Security/Universe coverage report contains a candidate snapshot blocker.",
                    subject_ids=(item.coverage_report_id,),
                    details=tuple(details),
                )
            )
    for code, details in trust_fields.items():
        if details:
            findings.append(
                Finding(
                    code,
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{item.universe_id}",
                    "Security/Universe coverage evidence remains incomplete.",
                    subject_ids=(item.coverage_report_id,),
                    details=tuple(details),
                )
            )
    if report.current_anchor_only:
        findings.extend(
            (
                Finding(
                    "CURRENT_ANCHOR_USED_AS_HISTORY",
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{item.universe_id}",
                    "A current securities anchor cannot establish historical Universe membership.",
                    subject_ids=(item.bundle_id,),
                ),
                Finding(
                    "ABSENCE_CANNOT_PROVE_EXCLUDED",
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{item.universe_id}",
                    "Absence from a current anchor is not an EXCLUDED membership event.",
                    subject_ids=(item.bundle_id,),
                ),
            )
        )
    return findings


def _intervals_overlap(
    left_start: date,
    left_end: date | None,
    right_start: date,
    right_end: date | None,
) -> bool:
    return (left_end is None or right_start <= left_end) and (
        right_end is None or left_start <= right_end
    )


def _latest_identities(
    records: Iterable[tuple[SecurityUniverseReconciliationInput, IdentityCandidate]],
) -> list[tuple[SecurityUniverseReconciliationInput, IdentityCandidate]]:
    groups: dict[
        tuple[str, str, date],
        list[tuple[SecurityUniverseReconciliationInput, IdentityCandidate]],
    ] = defaultdict(list)
    for item, candidate in records:
        groups[(item.input_id, candidate.instrument_id, candidate.effective_from)].append(
            (item, candidate)
        )
    selected = []
    for values in groups.values():
        values.sort(
            key=lambda pair: (
                pair[1].provenance.known_at,
                revision_key(pair[1].provenance.revision),
                pair[1].candidate_id,
            )
        )
        selected.append(values[-1])
    semantic: dict[
        tuple[str, str, str, str, date, date | None],
        tuple[SecurityUniverseReconciliationInput, IdentityCandidate],
    ] = {}
    for pair in selected:
        candidate = pair[1]
        key = (
            candidate.instrument_id,
            candidate.source_security_id,
            candidate.exchange,
            candidate.symbol,
            candidate.effective_from,
            candidate.effective_to,
        )
        current = semantic.get(key)
        if current is None or candidate.candidate_id < current[1].candidate_id:
            semantic[key] = pair
    return list(semantic.values())


def _membership_snapshot(
    item: SecurityUniverseReconciliationInput,
    session_date: date,
    cutoff: datetime,
) -> dict[str, MembershipCandidate]:
    visible = [
        candidate
        for candidate in item.bundle.memberships
        if candidate.effective_date <= session_date and _visible(candidate.provenance, cutoff)
    ]
    latest: dict[str, MembershipCandidate] = {}
    for candidate in visible:
        current = latest.get(candidate.instrument_id)
        key = (
            candidate.effective_date,
            candidate.provenance.known_at,
            revision_key(candidate.provenance.revision),
            candidate.candidate_id,
        )
        if current is None:
            latest[candidate.instrument_id] = candidate
            continue
        current_key = (
            current.effective_date,
            current.provenance.known_at,
            revision_key(current.provenance.revision),
            current.candidate_id,
        )
        if key > current_key:
            latest[candidate.instrument_id] = candidate
    return latest


def _intraday_statuses_overlap(left: StatusCandidate, right: StatusCandidate) -> bool:
    left_start = cast(datetime, left.effective_start)
    right_start = cast(datetime, right.effective_start)
    left_end = cast(datetime | None, left.effective_end)
    right_end = cast(datetime | None, right.effective_end)
    return (left_end is None or right_start < left_end) and (
        right_end is None or left_start < right_end
    )


def _status_conflicts(
    records: list[tuple[SecurityUniverseReconciliationInput, StatusCandidate]],
    cutoff: datetime,
) -> list[Finding]:
    findings: list[Finding] = []
    groups: dict[
        tuple[str, date, StatusScope],
        list[tuple[SecurityUniverseReconciliationInput, StatusCandidate]],
    ] = defaultdict(list)
    for item, status in records:
        groups[(status.instrument_id, status.session_date, status.scope)].append((item, status))
    for (instrument_id, session_date, scope), values in groups.items():
        visible = [pair for pair in values if _visible(pair[1].provenance, cutoff)]
        future = [pair for pair in values if not _visible(pair[1].provenance, cutoff)]
        visible_payloads = {
            (
                status.listing_state,
                status.trading_state,
                status.risk_designation,
                status.effective_start,
                status.effective_end,
            )
            for _, status in visible
        }
        if future and visible and any(
            (
                status.listing_state,
                status.trading_state,
                status.risk_designation,
                status.effective_start,
                status.effective_end,
            )
            not in visible_payloads
            for _, status in future
        ):
            findings.append(
                Finding(
                    "STATUS_FUTURE_CORRECTION_EXCLUDED",
                    FindingSeverity.WARNING,
                    f"status:{instrument_id}",
                    "A future status correction is excluded at this report as_of.",
                    subject_ids=tuple(status.candidate_id for _, status in future),
                    details=(session_date.isoformat(),),
                )
            )
        if len(visible) < 2:
            continue
        if scope is StatusScope.INTRADAY:
            for index, (_, left) in enumerate(visible):
                for _, right in visible[index + 1 :]:
                    if not _intraday_statuses_overlap(left, right):
                        continue
                    subjects = (left.candidate_id, right.candidate_id)
                    details = (session_date.isoformat(), "INTRADAY")
                    risks = {left.risk_designation, right.risk_designation}
                    if SourceRiskDesignation.UNKNOWN in risks and len(risks) > 1:
                        findings.append(
                            Finding(
                                "STATUS_UNKNOWN_CANNOT_BE_SILENTLY_RESOLVED",
                                FindingSeverity.TRUST_BLOCK,
                                f"status:{instrument_id}",
                                "UNKNOWN risk designation cannot be replaced by another source's missing/default interpretation.",
                                subject_ids=subjects,
                                details=details,
                            )
                        )
                    if (
                        left.trading_state != right.trading_state
                        or left.listing_state != right.listing_state
                    ):
                        findings.append(
                            Finding(
                                "STATUS_INTRADAY_SUSPENSION_CONFLICT",
                                FindingSeverity.HARD_BLOCK,
                                f"status:{instrument_id}",
                                "Overlapping intraday status intervals disagree.",
                                subject_ids=subjects,
                                details=details,
                            )
                        )
                    elif left.risk_designation != right.risk_designation:
                        findings.append(
                            Finding(
                                "STATUS_INTRADAY_RISK_CONFLICT",
                                FindingSeverity.TRUST_BLOCK,
                                f"status:{instrument_id}",
                                "Overlapping intraday intervals disagree on risk designation.",
                                subject_ids=subjects,
                                details=details,
                            )
                        )
            continue
        risks = {status.risk_designation for _, status in visible}
        listings = {status.listing_state for _, status in visible}
        tradings = {status.trading_state for _, status in visible}
        subjects = tuple(status.candidate_id for _, status in visible)
        if SourceRiskDesignation.UNKNOWN in risks and len(risks) > 1:
            findings.append(
                Finding(
                    "STATUS_UNKNOWN_CANNOT_BE_SILENTLY_RESOLVED",
                    FindingSeverity.TRUST_BLOCK,
                    f"status:{instrument_id}",
                    "UNKNOWN risk designation cannot be replaced by another source's missing/default interpretation.",
                    subject_ids=subjects,
                    details=(session_date.isoformat(),),
                )
            )
        if {SourceRiskDesignation.ST, SourceRiskDesignation.STAR_ST}.issubset(risks):
            findings.append(
                Finding(
                    "STATUS_ST_STAR_ST_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"status:{instrument_id}",
                    "Sources disagree between ST and *ST for the same status interval.",
                    subject_ids=subjects,
                    details=(session_date.isoformat(),),
                )
            )
        trading_open = bool(
            tradings.intersection({SourceTradingState.TRADABLE, SourceTradingState.RESUMED})
        )
        trading_closed = bool(
            tradings.intersection({SourceTradingState.SUSPENDED, SourceTradingState.HALTED})
        )
        if trading_open and trading_closed:
            findings.append(
                Finding(
                    "STATUS_SUSPENSION_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"status:{instrument_id}",
                    "Sources disagree on tradable versus suspended/halted state.",
                    subject_ids=subjects,
                    details=(session_date.isoformat(), scope.value),
                )
            )
        if SourceListingState.DELISTED in listings and listings.intersection(
            {SourceListingState.LISTED, SourceListingState.DELISTING}
        ):
            findings.append(
                Finding(
                    "STATUS_DELISTING_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"status:{instrument_id}",
                    "Sources disagree on listed/delisting versus delisted state.",
                    subject_ids=subjects,
                    details=(session_date.isoformat(),),
                )
            )
        elif SourceListingState.DELISTING in listings and SourceListingState.LISTED in listings:
            findings.append(
                Finding(
                    "STATUS_DELISTING_PERIOD_CONFLICT",
                    FindingSeverity.TRUST_BLOCK,
                    f"status:{instrument_id}",
                    "Sources disagree on the delisting-period state.",
                    subject_ids=subjects,
                    details=(session_date.isoformat(),),
                )
            )
    return findings


def _analyze_security(
    inputs: tuple[SecurityUniverseReconciliationInput, ...],
    cutoff: datetime,
    calendar_observed_dates_by_exchange: Mapping[Exchange, set[date]],
    calendar_open_dates_by_exchange: Mapping[Exchange, set[date]],
) -> tuple[list[Finding], set[str], int, int, set[str], set[str]]:
    findings: list[Finding] = []
    inherited: set[str] = set()
    required_status_keys: set[str] = set()
    observed_status_keys: set[str] = set()
    missing_status_ids: set[str] = set()
    unclosed_delisted: set[str] = set()

    identities: list[tuple[SecurityUniverseReconciliationInput, IdentityCandidate]] = []
    statuses: list[tuple[SecurityUniverseReconciliationInput, StatusCandidate]] = []
    memberships: list[tuple[SecurityUniverseReconciliationInput, MembershipCandidate]] = []
    visible_inputs: list[SecurityUniverseReconciliationInput] = []
    included_at_session: dict[
        tuple[str, date, str],
        list[MembershipCandidate],
    ] = defaultdict(list)
    membership_at_session: dict[
        tuple[str, date, str],
        list[tuple[SecurityUniverseReconciliationInput, MembershipCandidate]],
    ] = defaultdict(list)
    cutoff_session_date = exchange_local_date(cutoff, Market.A)
    for item in inputs:
        report = item.bundle.coverage_report
        inherited.update(_BASE_SECURITY_TRUST_BLOCKERS)
        if report.current_anchor_only:
            inherited.add("CURRENT_ANCHOR_ONLY")
        expected_universe = f"A_SHARE_{item.exchange}_ALL"
        if item.universe_id != expected_universe:
            findings.append(
                Finding(
                    "PREMATURE_A_SHARE_ALL_UNION",
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{item.universe_id}",
                    "SSE and SZSE must remain separate; reconciliation cannot create A_SHARE_ALL.",
                    subject_ids=(item.bundle_id,),
                )
            )
        visible_identities, visible_statuses, visible_memberships = _visible_security_candidates(
            item,
            cutoff,
        )
        descriptor_visible = to_utc(item.bundle.descriptor.retrieved_at) <= cutoff
        if not descriptor_visible:
            inherited.add("SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF")
            findings.append(
                Finding(
                    "SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF",
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{item.universe_id}",
                    "Security/Universe artifact was retrieved after the reconciliation as_of.",
                    subject_ids=(item.input_id,),
                    details=(item.bundle.descriptor.retrieved_at.isoformat(),),
                )
            )
            continue
        visible_inputs.append(item)
        all_candidates_visible = (
            len(visible_identities) == len(item.bundle.identities)
            and len(visible_statuses) == len(item.bundle.statuses)
            and len(visible_memberships) == len(item.bundle.memberships)
        )
        if all_candidates_visible:
            inherited.update(report.trust_blocker_codes)
            findings.extend(_report_findings(item, report))
            unclosed_delisted.update(report.unclosed_delistings)
        else:
            inherited.add("SECURITY_COVERAGE_NOT_AS_OF_STABLE")
            findings.append(
                Finding(
                    "SECURITY_COVERAGE_NOT_AS_OF_STABLE",
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{item.universe_id}",
                    "Bundle-global coverage evidence includes candidates not yet usable at this as_of.",
                    subject_ids=(item.coverage_report_id,),
                )
            )
        identities.extend((item, value) for value in visible_identities)
        statuses.extend((item, value) for value in visible_statuses)
        memberships.extend((item, value) for value in visible_memberships)
        exchange = Exchange(item.exchange)
        calendar_observed_dates = calendar_observed_dates_by_exchange.get(exchange, set())
        calendar_open_dates = calendar_open_dates_by_exchange.get(exchange, set())
        for session_date in (
            value
            for value in item.bundle.required_session_dates
            if value <= cutoff_session_date
        ):
            if session_date not in calendar_observed_dates:
                findings.append(
                    Finding(
                        "REQUIRED_SESSION_CALENDAR_MISSING",
                        FindingSeverity.HARD_BLOCK,
                        f"universe:{item.universe_id}",
                        "A required Universe session is absent from Calendar evidence.",
                        subject_ids=(item.bundle_id,),
                        details=(session_date.isoformat(),),
                    )
                )
            elif session_date not in calendar_open_dates:
                findings.append(
                    Finding(
                        "REQUIRED_SESSION_CALENDAR_CLOSED",
                        FindingSeverity.HARD_BLOCK,
                        f"universe:{item.universe_id}",
                        "A required Universe session is not OPEN in visible Calendar evidence.",
                        subject_ids=(item.bundle_id,),
                        details=(session_date.isoformat(),),
                    )
                )

            snapshot = _membership_snapshot(item, session_date, cutoff)
            visible_daily_statuses = [
                status
                for status in visible_statuses
                if status.scope is StatusScope.DAILY and status.fact is not None
            ]
            included_symbols: dict[str, str] = {}
            for instrument_id, membership in snapshot.items():
                membership_at_session[
                    (item.universe_id, session_date, instrument_id)
                ].append((item, membership))
                anchor = (
                    session_date
                    if membership.state is UniverseMembershipState.INCLUDED
                    else membership.effective_date
                )
                active = [
                    identity
                    for identity in visible_identities
                    if identity.instrument_id == instrument_id and identity.active_on(anchor)
                ]
                if not active:
                    findings.append(
                        Finding(
                            "MEMBERSHIP_WITHOUT_REQUIRED_IDENTITY",
                            FindingSeverity.HARD_BLOCK,
                            f"universe:{item.universe_id}",
                            "Membership has no visible identity at its required anchor date.",
                            subject_ids=(membership.candidate_id,),
                            details=(anchor.isoformat(),),
                        )
                    )
                if membership.state is UniverseMembershipState.INCLUDED:
                    requirement_key = f"{instrument_id}@{session_date.isoformat()}"
                    required_status_keys.add(requirement_key)
                    included_at_session[
                        (item.universe_id, session_date, membership.symbol)
                    ].append(membership)
                    prior = included_symbols.setdefault(membership.symbol, instrument_id)
                    if prior != instrument_id:
                        findings.append(
                            Finding(
                                "SYMBOL_OVERLAPS_INCLUDED_INSTRUMENTS",
                                FindingSeverity.HARD_BLOCK,
                                f"universe:{item.universe_id}",
                                "One target session includes the same symbol under multiple instrument IDs.",
                                subject_ids=(membership.candidate_id,),
                                details=(session_date.isoformat(), membership.symbol, prior),
                            )
                        )
                    matching = [
                        status
                        for status in visible_daily_statuses
                        if status.instrument_id == instrument_id
                        and status.session_date == session_date
                    ]
                    if matching:
                        observed_status_keys.add(requirement_key)
                        if any(
                            status.listing_state is SourceListingState.DELISTED
                            for status in matching
                        ):
                            findings.append(
                                Finding(
                                    "INCLUDED_WITH_DELISTED_STATUS",
                                    FindingSeverity.HARD_BLOCK,
                                    f"universe:{item.universe_id}",
                                    "An INCLUDED member is DELISTED on the target session.",
                                    subject_ids=tuple(status.candidate_id for status in matching),
                                    details=(instrument_id, session_date.isoformat()),
                                )
                            )
                    else:
                        missing_id = f"{instrument_id}@{session_date.isoformat()}"
                        missing_status_ids.add(missing_id)
                        findings.append(
                            Finding(
                                "INCLUDED_WITHOUT_TARGET_SESSION_STATUS",
                                FindingSeverity.HARD_BLOCK,
                                f"universe:{item.universe_id}",
                                "INCLUDED membership lacks target-session daily status.",
                                subject_ids=(membership.candidate_id,),
                                details=(missing_id,),
                            )
                        )
                else:
                    requirement_key = (
                        f"{instrument_id}@exit:{membership.effective_date.isoformat()}"
                    )
                    required_status_keys.add(requirement_key)
                    matching = [
                        status
                        for status in visible_daily_statuses
                        if status.instrument_id == instrument_id
                        and status.session_date <= membership.effective_date
                    ]
                    if matching:
                        observed_status_keys.add(requirement_key)
                    else:
                        missing_id = requirement_key
                        missing_status_ids.add(missing_id)
                        findings.append(
                            Finding(
                                "EXCLUDED_WITHOUT_EXIT_OR_LAST_VISIBLE_STATUS",
                                FindingSeverity.HARD_BLOCK,
                                f"universe:{item.universe_id}",
                                "EXCLUDED membership lacks status on or before its exclusion date.",
                                subject_ids=(membership.candidate_id,),
                                details=(missing_id,),
                            )
                        )

    for (universe_id, session_date, instrument_id), records in membership_at_session.items():
        states = {membership.state for _, membership in records}
        reasons = {membership.reason for _, membership in records}
        if len(states) > 1:
            findings.append(
                Finding(
                    "UNIVERSE_MEMBERSHIP_STATE_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{universe_id}",
                    "Sources disagree on INCLUDED versus EXCLUDED membership for the same instrument/session.",
                    subject_ids=tuple(membership.candidate_id for _, membership in records),
                    details=(session_date.isoformat(), instrument_id),
                )
            )
        elif len(reasons) > 1:
            findings.append(
                Finding(
                    "UNIVERSE_MEMBERSHIP_REASON_CONFLICT",
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{universe_id}",
                    "Sources agree on membership state but disagree on its event reason.",
                    subject_ids=tuple(membership.candidate_id for _, membership in records),
                    details=(session_date.isoformat(), instrument_id, *(reason.value for reason in reasons)),
                )
            )

    for (universe_id, session_date, symbol), records in included_at_session.items():
        instrument_ids = {item.instrument_id for item in records}
        if len(instrument_ids) > 1:
            findings.append(
                Finding(
                    "SYMBOL_OVERLAPS_INCLUDED_INSTRUMENTS",
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{universe_id}",
                    "One target session includes the same symbol under multiple instrument IDs.",
                    subject_ids=tuple(item.candidate_id for item in records),
                    details=(session_date.isoformat(), symbol, *instrument_ids),
                )
            )

    latest_identities = _latest_identities(identities)
    by_instrument: dict[str, list[IdentityCandidate]] = defaultdict(list)
    by_symbol: dict[str, list[IdentityCandidate]] = defaultdict(list)
    source_id_mapping: dict[tuple[str, str], set[str]] = defaultdict(set)
    instrument_source_ids: dict[str, set[str]] = defaultdict(set)
    for _, identity in latest_identities:
        by_instrument[identity.instrument_id].append(identity)
        by_symbol[identity.symbol].append(identity)
        source_id_mapping[(identity.exchange, identity.source_security_id)].add(
            identity.instrument_id
        )
        instrument_source_ids[identity.instrument_id].add(identity.source_security_id)
        if (identity.exchange == "SSE" and not identity.symbol.endswith(".SH")) or (
            identity.exchange == "SZSE" and not identity.symbol.endswith(".SZ")
        ):
            findings.append(
                Finding(
                    "IDENTITY_MARKET_EXCHANGE_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"identity:{identity.instrument_id}",
                    "Identity symbol suffix conflicts with its exchange.",
                    subject_ids=(identity.candidate_id,),
                )
            )
    for instrument_id, source_ids in instrument_source_ids.items():
        if len(source_ids) > 1:
            findings.append(
                Finding(
                    "INSTRUMENT_ID_INSTABILITY",
                    FindingSeverity.HARD_BLOCK,
                    f"identity:{instrument_id}",
                    "One instrument_id maps to multiple source_security_id values.",
                    details=tuple(source_ids),
                )
            )
    for key, instrument_ids in source_id_mapping.items():
        if len(instrument_ids) > 1:
            findings.append(
                Finding(
                    "SOURCE_SECURITY_ID_IDENTITY_CONFLICT",
                    FindingSeverity.HARD_BLOCK,
                    f"identity:{key[0]}:{key[1]}",
                    "One source_security_id maps to multiple instrument IDs.",
                    details=tuple(instrument_ids),
                )
            )
    for instrument_id, records in by_instrument.items():
        records.sort(key=lambda value: (value.effective_from, value.candidate_id))
        symbols = {value.symbol for value in records}
        if len(symbols) > 1:
            findings.append(
                Finding(
                    "IDENTITY_SYMBOL_CHANGE",
                    FindingSeverity.INFO,
                    f"identity:{instrument_id}",
                    "Stable instrument identity changes symbol across non-overlapping intervals.",
                    subject_ids=tuple(value.candidate_id for value in records),
                    details=tuple(symbols),
                )
            )
        for left, right in pairwise(records):
            if _intervals_overlap(
                left.effective_from,
                left.effective_to,
                right.effective_from,
                right.effective_to,
            ):
                severity = (
                    FindingSeverity.HARD_BLOCK
                    if left.symbol != right.symbol
                    else FindingSeverity.TRUST_BLOCK
                )
                code = (
                    "SAME_INSTRUMENT_INCOMPATIBLE_SYMBOL_OVERLAP"
                    if left.symbol != right.symbol
                    else "IDENTITY_INTERVAL_OVERLAP"
                )
                findings.append(
                    Finding(
                        code,
                        severity,
                        f"identity:{instrument_id}",
                        "One instrument has incompatible overlapping identity intervals.",
                        subject_ids=(left.candidate_id, right.candidate_id),
                    )
                )
    for symbol, records in by_symbol.items():
        records.sort(key=lambda value: (value.effective_from, value.candidate_id))
        for left, right in pairwise(records):
            if left.instrument_id == right.instrument_id:
                continue
            if _intervals_overlap(
                left.effective_from,
                left.effective_to,
                right.effective_from,
                right.effective_to,
            ):
                findings.append(
                    Finding(
                        "SYMBOL_IDENTITY_INTERVAL_OVERLAP",
                        FindingSeverity.HARD_BLOCK,
                        f"identity:{symbol}",
                        "One symbol overlaps different stable instrument identities.",
                        subject_ids=(left.candidate_id, right.candidate_id),
                    )
                )
            else:
                findings.append(
                    Finding(
                        "LEGITIMATE_NON_OVERLAP_CODE_REUSE",
                        FindingSeverity.INFO,
                        f"identity:{symbol}",
                        "Different instrument IDs reuse one symbol in non-overlapping intervals.",
                        subject_ids=(left.candidate_id, right.candidate_id),
                    )
                )

    membership_by_instrument: dict[str, list[MembershipCandidate]] = defaultdict(list)
    for _, membership in memberships:
        membership_by_instrument[membership.instrument_id].append(membership)
        if not membership.evidence_ids or not membership.provenance.evidence_ids:
            findings.append(
                Finding(
                    "MISSING_SOURCE_EVIDENCE_IDS",
                    FindingSeverity.TRUST_BLOCK,
                    f"universe:{membership.universe_id}",
                    "Membership lacks explicit source evidence IDs.",
                    subject_ids=(membership.candidate_id,),
                )
            )
    for instrument_id, records in membership_by_instrument.items():
        records.sort(key=lambda value: (value.effective_date, value.candidate_id))
        for left, right in pairwise(records):
            if (
                left.state is UniverseMembershipState.EXCLUDED
                and right.state is UniverseMembershipState.INCLUDED
            ):
                findings.append(
                    Finding(
                        "RELISTING_CONTINUITY_AMBIGUOUS",
                        FindingSeverity.TRUST_BLOCK,
                        f"identity:{instrument_id}",
                        "Relisting continuity requires independent stable-identity evidence.",
                        subject_ids=(left.candidate_id, right.candidate_id),
                    )
                )

    findings.extend(_status_conflicts(statuses, cutoff))
    for _, status in statuses:
        if not status.provenance.evidence_ids:
            findings.append(
                Finding(
                    "MISSING_SOURCE_EVIDENCE_IDS",
                    FindingSeverity.TRUST_BLOCK,
                    f"status:{status.instrument_id}",
                    "Status candidate lacks source evidence IDs.",
                    subject_ids=(status.candidate_id,),
                )
            )

    versions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for item in visible_inputs:
        versions[(item.source, item.exchange, item.universe_id)].add(item.source_version)
    for key, source_versions in versions.items():
        if len(source_versions) > 1:
            findings.append(
                Finding(
                    "UNIVERSE_SOURCE_VERSION_MIXING",
                    FindingSeverity.HARD_BLOCK,
                    f"universe:{key[2]}",
                    "One source identity contains multiple Universe source versions.",
                    details=tuple(source_versions),
                )
            )
    synthetic_inputs = tuple(item for item in visible_inputs if item.synthetic)
    if synthetic_inputs:
        inherited.add("SYNTHETIC_EVIDENCE_NOT_CORROBORATION")
        findings.append(
            Finding(
                "SYNTHETIC_EVIDENCE_NOT_CORROBORATION",
                FindingSeverity.TRUST_BLOCK,
                "provenance",
                "Synthetic fixtures cannot serve as independent corroboration or Trust evidence.",
                subject_ids=tuple(item.input_id for item in synthetic_inputs),
            )
        )
    return (
        findings,
        inherited,
        len(required_status_keys),
        len(observed_status_keys),
        missing_status_ids,
        unclosed_delisted,
    )


def _evaluate_blockers(
    codes: set[str],
    requests: tuple[BlockerClosureRequest, ...],
    evidence: tuple[ExternalClosureEvidence, ...],
    policy_version: str,
) -> tuple[tuple[InheritedTrustBlocker, ...], list[Finding]]:
    request_by_code: dict[str, BlockerClosureRequest] = {}
    for request in requests:
        if request.code in request_by_code:
            raise ReconciliationContractError("duplicate blocker closure request")
        request_by_code[request.code] = request
    evidence_by_id = {item.evidence_id: item for item in evidence}
    if len(evidence_by_id) != len(evidence):
        raise ReconciliationContractError("duplicate external closure evidence ID")
    blockers: list[InheritedTrustBlocker] = []
    rejected: list[Finding] = []
    for code in sorted(codes):
        request = request_by_code.get(code)
        if request is None:
            blockers.append(InheritedTrustBlocker(code, BlockerStatus.OPEN))
            continue
        reasons: list[str] = [
            "Stage 2A has no trusted external closure authority; self-asserted approval cannot close Trust blockers"
        ]
        if request.policy_version != policy_version:
            reasons.append("closure policy version does not match report policy")
        records = [evidence_by_id.get(item) for item in request.closing_evidence_ids]
        if any(item is None for item in records):
            reasons.append("closing evidence ID is not present in the external evidence registry")
        present = [cast(ExternalClosureEvidence, item) for item in records if item is not None]
        required_kind = _EVIDENCE_KIND_BY_BLOCKER.get(code)
        for item in present:
            if item.synthetic:
                reasons.append(f"{item.evidence_id}: synthetic evidence cannot close Trust")
            if not item.independently_approved:
                reasons.append(f"{item.evidence_id}: evidence lacks independent approval")
            if code not in item.supported_blocker_codes:
                reasons.append(f"{item.evidence_id}: evidence does not support blocker code")
            if required_kind is not None and item.kind.value != required_kind:
                reasons.append(f"{item.evidence_id}: wrong evidence kind for blocker")
            if (
                code == "UPSTREAM_RAW_PROVENANCE_INCOMPLETE"
                and not item.upstream_raw_artifact_ids
            ):
                reasons.append(f"{item.evidence_id}: upstream raw artifact IDs are missing")
        if reasons:
            blockers.append(InheritedTrustBlocker(code, BlockerStatus.OPEN))
            rejected.append(
                Finding(
                    "BLOCKER_CLOSURE_REJECTED",
                    FindingSeverity.TRUST_BLOCK,
                    f"blocker:{code}",
                    "Inherited blocker closure request did not satisfy the evidence contract.",
                    evidence_ids=request.closing_evidence_ids,
                    details=tuple(
                        sorted(
                            set(
                                reasons
                                + [
                                    f"requested_closing_reason={request.closing_reason}",
                                    f"requested_policy_version={request.policy_version}",
                                ]
                            )
                        )
                    ),
                )
            )
        else:
            blockers.append(
                InheritedTrustBlocker(
                    code,
                    BlockerStatus.CLOSED_WITH_EVIDENCE,
                    request.closing_evidence_ids,
                    request.closing_reason,
                    request.policy_version,
                )
            )
    unknown_requests = sorted(set(request_by_code) - codes)
    if unknown_requests:
        raise ReconciliationContractError(
            "closure request references non-inherited blocker: "
            + ", ".join(unknown_requests)
        )
    return tuple(blockers), rejected


def _derive_reconciliation_components(
    *,
    calendar_inputs: tuple[CalendarReconciliationInput, ...],
    security_universe_inputs: tuple[SecurityUniverseReconciliationInput, ...],
    as_of: datetime,
    reconciliation_policy_version: str,
    closure_requests: tuple[BlockerClosureRequest, ...],
    external_closure_evidence: tuple[ExternalClosureEvidence, ...],
    additional_findings: tuple[Finding, ...],
) -> tuple[
    tuple[Finding, ...],
    tuple[InheritedTrustBlocker, ...],
    CoverageMetrics,
    tuple[str, ...],
]:
    policy_version = _require_text(
        reconciliation_policy_version,
        "reconciliation_policy_version",
    )
    cutoff = to_utc(as_of, "as_of")
    calendars = tuple(sorted(calendar_inputs, key=lambda item: item.input_id))
    securities = tuple(sorted(security_universe_inputs, key=lambda item: item.input_id))

    (
        findings,
        calendar_gaps,
        expected_dates,
        observed_dates,
        open_dates,
        observed_dates_by_exchange,
        open_dates_by_exchange,
    ) = _analyze_calendar(calendars, cutoff)
    (
        security_findings,
        inherited,
        required_status_count,
        observed_status_count,
        missing_status_ids,
        unclosed_delisted,
    ) = _analyze_security(
        securities,
        cutoff,
        observed_dates_by_exchange,
        open_dates_by_exchange,
    )
    findings.extend(security_findings)
    findings.extend(additional_findings)
    inherited.update(_BASE_INHERITED_BLOCKERS)
    inherited.update(
        gap for gap in calendar_gaps if gap != "WEEKDAY_OPEN_BASELINE_INFERRED"
    )
    blockers, closure_findings = _evaluate_blockers(
        inherited,
        closure_requests,
        external_closure_evidence,
        policy_version,
    )
    findings.extend(closure_findings)
    for blocker in blockers:
        if blocker.status is BlockerStatus.OPEN:
            findings.append(
                Finding(
                    "INHERITED_TRUST_BLOCKER_OPEN",
                    FindingSeverity.TRUST_BLOCK,
                    f"blocker:{blocker.code}",
                    "Inherited Trust blocker remains OPEN; report generation cannot close it.",
                    details=(blocker.code,),
                )
            )

    unique_findings = {item.finding_id: item for item in findings}
    all_findings = tuple(sorted(unique_findings.values(), key=_finding_sort_key))
    missing_dates = tuple(
        value.isoformat() for value in sorted(expected_dates - observed_dates)
    )

    visible_security = [
        (source, *_visible_security_candidates(source, cutoff))
        for source in securities
        if to_utc(source.bundle.descriptor.retrieved_at) <= cutoff
    ]
    visible_identities = [
        candidate
        for _, identities, _, _ in visible_security
        for candidate in identities
    ]
    visible_statuses = [
        candidate
        for _, _, statuses, _ in visible_security
        for candidate in statuses
    ]
    visible_memberships = [
        candidate
        for _, _, _, memberships in visible_security
        for candidate in memberships
    ]
    coverage = CoverageMetrics(
        calendar_expected_civil_dates=len(expected_dates),
        calendar_observed_civil_dates=len(observed_dates),
        calendar_missing_civil_dates=missing_dates,
        calendar_open_dates=tuple(value.isoformat() for value in sorted(open_dates)),
        security_bundle_count=len(visible_security),
        identity_candidate_count=len(visible_identities),
        status_candidate_count=len(visible_statuses),
        membership_candidate_count=len(visible_memberships),
        included_membership_count=sum(
            item.state is UniverseMembershipState.INCLUDED
            for item in visible_memberships
        ),
        excluded_membership_count=sum(
            item.state is UniverseMembershipState.EXCLUDED
            for item in visible_memberships
        ),
        required_status_count=required_status_count,
        observed_required_status_count=observed_status_count,
        missing_required_status_ids=tuple(missing_status_ids),
        unclosed_delisted_instrument_ids=tuple(unclosed_delisted),
    )
    unresolved = set(calendar_gaps)
    unresolved.update(
        blocker.code for blocker in blockers if blocker.status is BlockerStatus.OPEN
    )
    unresolved.update(
        finding.code
        for finding in all_findings
        if finding.severity in {FindingSeverity.HARD_BLOCK, FindingSeverity.TRUST_BLOCK}
    )
    unresolved.update({LICENSE_PENDING, T3_NOT_REACHED})
    return all_findings, blockers, coverage, tuple(sorted(unresolved))


def reconcile_stage2(
    *,
    calendar_inputs: Iterable[CalendarReconciliationInput],
    security_universe_inputs: Iterable[SecurityUniverseReconciliationInput],
    as_of: datetime,
    reconciliation_policy_version: str = DEFAULT_RECONCILIATION_POLICY_VERSION,
    closure_requests: Iterable[BlockerClosureRequest] = (),
    external_closure_evidence: Iterable[ExternalClosureEvidence] = (),
    additional_findings: Iterable[Finding] = (),
) -> ReconciliationReport:
    return ReconciliationReport(
        reconciliation_policy_version=reconciliation_policy_version,
        as_of=as_of,
        calendar_inputs=tuple(calendar_inputs),
        security_universe_inputs=tuple(security_universe_inputs),
        closure_requests=tuple(closure_requests),
        external_closure_evidence=tuple(external_closure_evidence),
        additional_findings=tuple(additional_findings),
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_reconciliation_json(report: ReconciliationReport, path: str | Path) -> None:
    payload = report.as_dict()
    _atomic_write_text(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def render_reconciliation_markdown(report: ReconciliationReport) -> str:
    counts = report.finding_counts
    lines = [
        "# Stage 2A Reconciliation Summary",
        "",
        f"- report_id: `{report.report_id}`",
        f"- policy_version: `{report.reconciliation_policy_version}`",
        f"- as_of: `{report.as_of.isoformat().replace('+00:00', 'Z')}`",
        f"- candidate_snapshot_state: `{report.candidate_snapshot_state}`",
        f"- HARD_BLOCK: {counts[FindingSeverity.HARD_BLOCK.value]}",
        f"- TRUST_BLOCK: {counts[FindingSeverity.TRUST_BLOCK.value]}",
        f"- WARNING: {counts[FindingSeverity.WARNING.value]}",
        f"- INFO: {counts[FindingSeverity.INFO.value]}",
        f"- license_status: `{report.license_status}`",
        f"- evidence_tier_status: `{report.evidence_tier_status}`",
        "",
        "## Input identities",
        "",
    ]
    for item in report.calendar_inputs:
        lines.append(
            f"- Calendar `{item.source_owner.value}` parse `{item.parse_descriptor_id}` raw `{item.raw_artifact_id}` parser `{item.parser_version}`"
        )
    for item in report.security_universe_inputs:
        lines.append(
            f"- Universe `{item.universe_id}` bundle `{item.bundle_id}` coverage `{item.coverage_report_id}` parser `{item.parser_version}`"
        )
    lines.extend(("", "## Inherited blockers", ""))
    for blocker in report.inherited_trust_blockers:
        suffix = (
            ""
            if blocker.status is BlockerStatus.OPEN
            else " evidence=" + ",".join(blocker.closing_evidence_ids)
        )
        lines.append(f"- `{blocker.code}`: `{blocker.status.value}`{suffix}")
    lines.extend(("", "## Findings", ""))
    for finding in report.findings:
        lines.append(
            f"- `{finding.severity.value}` `{finding.code}` [{finding.scope}]: {finding.message}"
        )
    lines.extend(
        (
            "",
            "Report generation success means only that reconciliation completed. It does not mean Trust passed.",
            "",
        )
    )
    return "\n".join(lines)


def write_reconciliation_markdown(report: ReconciliationReport, path: str | Path) -> None:
    _atomic_write_text(Path(path), render_reconciliation_markdown(report))
