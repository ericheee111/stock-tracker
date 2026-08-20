"""Synthetic/offline Stage 2E classification source adapter.

The adapter validates exact bytes and a detached descriptor, parses a strict
source-native taxonomy document, and binds membership rows to Stage 2A identity
candidates.  It never upgrades completeness, verification or a Trust Tier.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from stock_tracker.core.types import Market

from ..core.classification import (
    ClassificationAuthority,
    ClassificationCoverage,
    ClassificationFact,
    ClassificationKind,
    ClassificationMembershipFact,
    ClassificationMembershipState,
    ClassificationTaxonomy,
    HistoricalClassification,
)
from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from .security_universe_adapter import IdentityCandidate

CLASSIFICATION_SOURCE_SCHEMA = "stage3a-classification-source-v1"
CLASSIFICATION_DESCRIPTOR_SCHEMA = "stage3a-classification-descriptor-v1"
CLASSIFICATION_ADAPTER_VERSION = "stage3a-classification-adapter-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ)$")
_CLASSIFICATION_BUNDLE_FACTORY_MARKER = object()


class ClassificationAdapterError(ValueError):
    """Raised when classification source bytes or identity binding are unsafe."""


class ClassificationPublicationGranularity(StrEnum):
    DATE = "DATE"
    SECOND = "SECOND"
    UNKNOWN = "UNKNOWN"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ClassificationAdapterError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ClassificationAdapterError(f"{name} must be a boolean")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClassificationAdapterError(f"{name} must be an integer")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ClassificationAdapterError(f"{name} must be lowercase SHA-256")
    return text


def _parse_date(value: object, name: str) -> date:
    text = _require_text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ClassificationAdapterError(f"{name} must be YYYY-MM-DD") from exc


def _optional_date(value: object, name: str) -> date | None:
    return None if value is None else _parse_date(value, name)


def _parse_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClassificationAdapterError(
            f"{name} must be ISO-8601 datetime"
        ) from exc
    ensure_aware(result, name)
    return to_utc(result)


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ClassificationAdapterError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _fields(
    value: dict[str, object],
    name: str,
    expected: frozenset[str],
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ClassificationAdapterError(
            f"{name} contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise ClassificationAdapterError(
            f"{name} is missing fields: " + ", ".join(missing)
        )


def _strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ClassificationAdapterError("artifact must be strict UTF-8") from exc

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ClassificationAdapterError(
                    f"duplicate JSON field is forbidden: {key}"
                )
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ClassificationAdapterError(
            f"non-finite JSON constant is forbidden: {value}"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except json.JSONDecodeError as exc:
        raise ClassificationAdapterError("artifact is not valid JSON") from exc


def _symbol(value: object, exchange: str) -> str:
    symbol = _require_text(value, "symbol")
    match = _SYMBOL.fullmatch(symbol)
    expected = {"SSE": "SH", "SZSE": "SZ"}.get(exchange)
    if match is None or expected is None or match.group(2) != expected:
        raise ClassificationAdapterError(
            "symbol must be canonical and match exchange"
        )
    return symbol


@dataclass(frozen=True, slots=True)
class ClassificationArtifactDescriptor:
    source: str
    source_dataset: str
    source_version: str
    schema_version: str
    parser_version: str
    retrieved_at: datetime
    artifact_sha256: str
    byte_size: int
    synthetic: bool

    def __post_init__(self) -> None:
        for name in (
            "source",
            "source_dataset",
            "source_version",
            "schema_version",
            "parser_version",
        ):
            _require_text(getattr(self, name), name)
        if self.schema_version != CLASSIFICATION_SOURCE_SCHEMA:
            raise ClassificationAdapterError("descriptor source schema mismatch")
        if self.parser_version != CLASSIFICATION_ADAPTER_VERSION:
            raise ClassificationAdapterError("descriptor parser version mismatch")
        ensure_aware(self.retrieved_at, "retrieved_at")
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        if _require_int(self.byte_size, "byte_size") <= 0:
            raise ClassificationAdapterError("byte_size must be positive")
        _require_bool(self.synthetic, "synthetic")
        if self.synthetic is not True:
            raise ClassificationAdapterError(
                "Stage 2E descriptor is synthetic-only in this slice"
            )

    @property
    def descriptor_id(self) -> str:
        return fingerprint(self)

    def verify(self, raw: bytes) -> None:
        if len(raw) != self.byte_size:
            raise ClassificationAdapterError("artifact byte_size mismatch")
        if hashlib.sha256(raw).hexdigest() != self.artifact_sha256:
            raise ClassificationAdapterError("artifact SHA-256 mismatch")

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, object],
    ) -> ClassificationArtifactDescriptor:
        expected = frozenset(
            {
                "schema",
                "source",
                "source_dataset",
                "source_version",
                "schema_version",
                "parser_version",
                "retrieved_at",
                "artifact_sha256",
                "byte_size",
                "synthetic",
            }
        )
        _fields(value, "descriptor", expected)
        if value["schema"] != CLASSIFICATION_DESCRIPTOR_SCHEMA:
            raise ClassificationAdapterError("unsupported descriptor schema")
        return cls(
            source=_require_text(value["source"], "source"),
            source_dataset=_require_text(
                value["source_dataset"], "source_dataset"
            ),
            source_version=_require_text(value["source_version"], "source_version"),
            schema_version=_require_text(value["schema_version"], "schema_version"),
            parser_version=_require_text(value["parser_version"], "parser_version"),
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            artifact_sha256=_require_sha256(
                value["artifact_sha256"], "artifact_sha256"
            ),
            byte_size=_require_int(value["byte_size"], "byte_size"),
            synthetic=_require_bool(value["synthetic"], "synthetic"),
        )


def read_classification_descriptor(
    path: str | Path,
) -> ClassificationArtifactDescriptor:
    value = _strict_json(Path(path).read_bytes())
    return ClassificationArtifactDescriptor.from_mapping(_object(value, "descriptor"))


@dataclass(frozen=True, slots=True)
class ClassificationSourceDefinition:
    classification_id: str
    name: str
    parent_classification_id: str | None
    effective_from: date
    effective_to: date | None
    revision: str
    supersedes: str | None

    def __post_init__(self) -> None:
        _require_text(self.classification_id, "classification_id")
        _require_text(self.name, "classification name")
        if self.parent_classification_id is not None:
            _require_text(
                self.parent_classification_id,
                "parent_classification_id",
            )
            if self.parent_classification_id == self.classification_id:
                raise ClassificationAdapterError(
                    "classification cannot be its own parent"
                )
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ClassificationAdapterError(
                "classification effective_to cannot precede effective_from"
            )
        _require_text(self.revision, "classification revision")
        if self.supersedes is not None:
            _require_text(self.supersedes, "classification supersedes")
            if self.supersedes == self.revision:
                raise ClassificationAdapterError(
                    "classification revision cannot supersede itself"
                )

    @property
    def candidate_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class ClassificationSourceMembership:
    classification_id: str
    source_security_id: str
    symbol: str
    exchange: str
    effective_from: date
    effective_to: date | None
    state: ClassificationMembershipState
    revision: str
    supersedes: str | None

    def __post_init__(self) -> None:
        _require_text(self.classification_id, "membership classification_id")
        _require_text(self.source_security_id, "source_security_id")
        _require_text(self.exchange, "membership exchange")
        _symbol(self.symbol, self.exchange)
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ClassificationAdapterError(
                "membership effective_to cannot precede effective_from"
            )
        if not isinstance(self.state, ClassificationMembershipState):
            raise ClassificationAdapterError(
                "state must be ClassificationMembershipState"
            )
        _require_text(self.revision, "membership revision")
        if self.supersedes is not None:
            _require_text(self.supersedes, "membership supersedes")
            if self.supersedes == self.revision:
                raise ClassificationAdapterError(
                    "membership revision cannot supersede itself"
                )

    @property
    def candidate_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class ClassificationBindingReport:
    missing_identity: tuple[str, ...]
    inactive_identity: tuple[str, ...]
    symbol_mismatch: tuple[str, ...]
    ambiguous_identity: tuple[str, ...]
    declared_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "missing_identity",
            "inactive_identity",
            "symbol_mismatch",
            "ambiguous_identity",
            "declared_gaps",
        ):
            value = getattr(self, name)
            if value != tuple(sorted(set(value))):
                raise ClassificationAdapterError(
                    f"{name} must be sorted and unique"
                )

    @property
    def has_snapshot_blockers(self) -> bool:
        return bool(
            self.missing_identity
            or self.inactive_identity
            or self.symbol_mismatch
            or self.ambiguous_identity
        )

    @property
    def trust_blocker_codes(self) -> tuple[str, ...]:
        codes = {
            "ADAPTER_UNVERIFIED_INCOMPLETE",
            "LICENSE_PENDING",
            "REAL_HISTORY_COVERAGE_UNPROVEN",
            "T3_NOT_REACHED",
        }
        if self.has_snapshot_blockers:
            codes.add("IDENTITY_BINDING_BLOCKED")
        if self.declared_gaps:
            codes.add("DECLARED_SOURCE_GAPS")
        return tuple(sorted(codes))

    @property
    def report_id(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class ClassificationCandidateBundle:
    descriptor: ClassificationArtifactDescriptor
    taxonomy: ClassificationTaxonomy
    coverage: ClassificationCoverage
    classifications: tuple[ClassificationFact, ...]
    memberships: tuple[ClassificationMembershipFact, ...]
    identities: tuple[IdentityCandidate, ...]
    source_membership_candidate_ids: tuple[str, ...]
    bound_membership_candidate_ids: tuple[str, ...]
    declared_gaps: tuple[str, ...]
    report: ClassificationBindingReport
    _factory_marker: InitVar[object] = None

    def __post_init__(self, _factory_marker: object) -> None:
        if not isinstance(self.descriptor, ClassificationArtifactDescriptor):
            raise ClassificationAdapterError(
                "descriptor must be ClassificationArtifactDescriptor"
            )
        if not isinstance(self.taxonomy, ClassificationTaxonomy):
            raise ClassificationAdapterError(
                "taxonomy must be ClassificationTaxonomy"
            )
        if not isinstance(self.coverage, ClassificationCoverage):
            raise ClassificationAdapterError(
                "coverage must be ClassificationCoverage"
            )
        if not isinstance(self.report, ClassificationBindingReport):
            raise ClassificationAdapterError(
                "report must be ClassificationBindingReport"
            )
        for name in (
            "source_membership_candidate_ids",
            "bound_membership_candidate_ids",
            "declared_gaps",
        ):
            value = getattr(self, name)
            if value != tuple(sorted(set(value))):
                raise ClassificationAdapterError(
                    f"{name} must be sorted and unique"
                )
        if not set(self.bound_membership_candidate_ids).issubset(
            set(self.source_membership_candidate_ids)
        ):
            raise ClassificationAdapterError(
                "bound membership candidate IDs must be source candidates"
            )
        unresolved_groups = (
            self.report.missing_identity,
            self.report.inactive_identity,
            self.report.symbol_mismatch,
            self.report.ambiguous_identity,
        )
        unresolved_ids = tuple(
            sorted(item for group in unresolved_groups for item in group)
        )
        if len(unresolved_ids) != len(set(unresolved_ids)):
            raise ClassificationAdapterError(
                "one source membership cannot have multiple binding outcomes"
            )
        expected_unresolved = tuple(
            sorted(
                set(self.source_membership_candidate_ids)
                - set(self.bound_membership_candidate_ids)
            )
        )
        if unresolved_ids != expected_unresolved:
            raise ClassificationAdapterError(
                "binding report does not cover every unbound source membership"
            )
        if len(self.bound_membership_candidate_ids) != len(self.memberships):
            raise ClassificationAdapterError(
                "bound source membership count differs from normalized facts"
            )
        if self.report.declared_gaps != self.declared_gaps:
            raise ClassificationAdapterError(
                "binding report declared gaps differ from source artifact"
            )
        if self.taxonomy.verified or self.coverage.verified or self.coverage.complete:
            raise ClassificationAdapterError(
                "Stage 2E adapter bundle cannot promote verification/completeness"
            )
        if (
            self.taxonomy.owner != self.descriptor.source
            or self.taxonomy.taxonomy_version != self.descriptor.source_version
            or self.coverage.taxonomy_id != self.taxonomy.taxonomy_id
            or self.coverage.source != self.taxonomy.owner
            or self.coverage.taxonomy_version != self.taxonomy.taxonomy_version
        ):
            raise ClassificationAdapterError(
                "descriptor, taxonomy and coverage stream identities disagree"
            )
        class_order = tuple(
            (item.classification_id, item.fact_id) for item in self.classifications
        )
        if class_order != tuple(sorted(class_order)):
            raise ClassificationAdapterError(
                "classifications must be deterministically sorted"
            )
        if any(
            item.verified
            or item.taxonomy_id != self.taxonomy.taxonomy_id
            or item.source != self.taxonomy.owner
            or item.taxonomy_version != self.taxonomy.taxonomy_version
            for item in self.classifications
        ):
            raise ClassificationAdapterError(
                "classification facts differ from unverified taxonomy stream"
            )
        membership_order = tuple(
            (item.classification_id, item.instrument_id, item.fact_id)
            for item in self.memberships
        )
        if membership_order != tuple(sorted(membership_order)):
            raise ClassificationAdapterError(
                "memberships must be deterministically sorted"
            )
        if any(
            item.verified
            or item.taxonomy_id != self.taxonomy.taxonomy_id
            or item.source != self.taxonomy.owner
            or item.taxonomy_version != self.taxonomy.taxonomy_version
            for item in self.memberships
        ):
            raise ClassificationAdapterError(
                "membership facts differ from unverified taxonomy stream"
            )
        identity_order = tuple(item.candidate_id for item in self.identities)
        if identity_order != tuple(sorted(set(identity_order))):
            raise ClassificationAdapterError(
                "identities must be sorted and unique by candidate_id"
            )
        identity_fact_ids = {item.fact.fact_id for item in self.identities}
        membership_identity_ids = {item.identity_fact_id for item in self.memberships}
        if identity_fact_ids != membership_identity_ids:
            raise ClassificationAdapterError(
                "bundle identities must exactly match membership identity references"
            )
        class_ids = {item.classification_id for item in self.classifications}
        if any(item.classification_id not in class_ids for item in self.memberships):
            raise ClassificationAdapterError(
                "membership references missing classification fact"
            )
        if _factory_marker is not _CLASSIFICATION_BUNDLE_FACTORY_MARKER:
            raise ClassificationAdapterError(
                "ClassificationCandidateBundle must be built by the strict parser"
            )

    @property
    def normalized_dataset_id(self) -> str:
        return fingerprint(
            {
                "schema": "stage3a-classification-normalized-dataset-v1",
                "taxonomy_identity": self.taxonomy.taxonomy_identity,
                "coverage_id": self.coverage.coverage_id,
                "classification_fact_ids": [
                    item.fact_id for item in self.classifications
                ],
                "membership_fact_ids": [item.fact_id for item in self.memberships],
                "identity_candidate_ids": [
                    item.candidate_id for item in self.identities
                ],
                "source_membership_candidate_ids": (
                    self.source_membership_candidate_ids
                ),
                "bound_membership_candidate_ids": (
                    self.bound_membership_candidate_ids
                ),
                "declared_gaps": self.declared_gaps,
                "report_id": self.report.report_id,
            }
        )

    @property
    def bundle_id(self) -> str:
        return fingerprint(
            {
                "schema": "stage3a-classification-candidate-bundle-v1",
                "descriptor_id": self.descriptor.descriptor_id,
                "taxonomy_identity": self.taxonomy.taxonomy_identity,
                "coverage_id": self.coverage.coverage_id,
                "classification_fact_ids": [
                    item.fact_id for item in self.classifications
                ],
                "membership_fact_ids": [item.fact_id for item in self.memberships],
                "identity_candidate_ids": [item.candidate_id for item in self.identities],
                "source_membership_candidate_ids": (
                    self.source_membership_candidate_ids
                ),
                "bound_membership_candidate_ids": (
                    self.bound_membership_candidate_ids
                ),
                "declared_gaps": self.declared_gaps,
                "report_id": self.report.report_id,
            }
        )

    def historical_classification(self) -> HistoricalClassification:
        return HistoricalClassification(
            self.taxonomy,
            (self.coverage,),
            self.classifications,
            self.memberships,
            tuple(item.fact for item in self.identities),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "stage3a-classification-candidate-bundle-v1",
            "bundle_id": self.bundle_id,
            "descriptor_id": self.descriptor.descriptor_id,
            "normalized_dataset_id": self.normalized_dataset_id,
            "taxonomy_identity": self.taxonomy.taxonomy_identity,
            "coverage_id": self.coverage.coverage_id,
            "classification_fact_ids": [
                item.fact_id for item in self.classifications
            ],
            "membership_fact_ids": [item.fact_id for item in self.memberships],
            "source_membership_candidate_ids": list(
                self.source_membership_candidate_ids
            ),
            "bound_membership_candidate_ids": list(
                self.bound_membership_candidate_ids
            ),
            "declared_gaps": list(self.declared_gaps),
            "report_id": self.report.report_id,
            "snapshot_constructible": not self.report.has_snapshot_blockers,
            "complete": False,
            "verified": False,
            "trust_state": "T3_NOT_REACHED",
            "trust_blocker_codes": list(self.report.trust_blocker_codes),
        }


_ROOT_FIELDS = frozenset(
    {
        "schema",
        "synthetic_fixture",
        "taxonomy",
        "coverage",
        "classifications",
        "memberships",
        "declared_gaps",
    }
)
_TAXONOMY_FIELDS = frozenset(
    {
        "taxonomy_id",
        "name",
        "kind",
        "authority",
        "owner",
        "taxonomy_version",
        "commercial_definition",
    }
)
_COVERAGE_FIELDS = frozenset(
    {"start_date", "end_date", "revision", "supersedes"}
)
_CLASSIFICATION_FIELDS = frozenset(
    {
        "classification_id",
        "name",
        "parent_classification_id",
        "effective_from",
        "effective_to",
        "revision",
        "supersedes",
    }
)
_MEMBERSHIP_FIELDS = frozenset(
    {
        "classification_id",
        "source_security_id",
        "symbol",
        "exchange",
        "effective_from",
        "effective_to",
        "state",
        "revision",
        "supersedes",
    }
)


def parse_classification_artifact(
    raw: bytes,
    descriptor: ClassificationArtifactDescriptor,
    identities: tuple[IdentityCandidate, ...],
) -> ClassificationCandidateBundle:
    descriptor.verify(raw)
    if any(not isinstance(item, IdentityCandidate) for item in identities):
        raise ClassificationAdapterError(
            "identities must contain IdentityCandidate values only"
        )
    root = _object(_strict_json(raw), "classification artifact")
    _fields(root, "classification artifact", _ROOT_FIELDS)
    if root["schema"] != CLASSIFICATION_SOURCE_SCHEMA:
        raise ClassificationAdapterError("unsupported classification source schema")
    if _require_bool(root["synthetic_fixture"], "synthetic_fixture") is not True:
        raise ClassificationAdapterError(
            "classification artifact must remain synthetic in this slice"
        )
    taxonomy_row = _object(root["taxonomy"], "taxonomy")
    _fields(taxonomy_row, "taxonomy", _TAXONOMY_FIELDS)
    try:
        kind = ClassificationKind(_require_text(taxonomy_row["kind"], "kind"))
        authority = ClassificationAuthority(
            _require_text(taxonomy_row["authority"], "authority")
        )
    except ValueError as exc:
        raise ClassificationAdapterError("invalid taxonomy enum") from exc
    taxonomy = ClassificationTaxonomy(
        taxonomy_id=_require_text(taxonomy_row["taxonomy_id"], "taxonomy_id"),
        name=_require_text(taxonomy_row["name"], "taxonomy name"),
        kind=kind,
        authority=authority,
        owner=_require_text(taxonomy_row["owner"], "taxonomy owner"),
        taxonomy_version=_require_text(
            taxonomy_row["taxonomy_version"], "taxonomy_version"
        ),
        commercial_definition=_require_bool(
            taxonomy_row["commercial_definition"], "commercial_definition"
        ),
        verified=False,
        source_note="",
    )
    if taxonomy.owner != descriptor.source:
        raise ClassificationAdapterError(
            "taxonomy owner differs from descriptor source"
        )
    if taxonomy.taxonomy_version != descriptor.source_version:
        raise ClassificationAdapterError(
            "taxonomy version differs from descriptor source_version"
        )
    coverage_row = _object(root["coverage"], "coverage")
    _fields(coverage_row, "coverage", _COVERAGE_FIELDS)
    supersedes = (
        None
        if coverage_row["supersedes"] is None
        else _require_text(coverage_row["supersedes"], "coverage supersedes")
    )
    coverage = ClassificationCoverage(
        taxonomy_id=taxonomy.taxonomy_id,
        market=Market.A,
        start_date=_parse_date(coverage_row["start_date"], "coverage start_date"),
        end_date=_parse_date(coverage_row["end_date"], "coverage end_date"),
        source=taxonomy.owner,
        taxonomy_version=taxonomy.taxonomy_version,
        known_at=descriptor.retrieved_at,
        usable_from=descriptor.retrieved_at,
        revision=_require_text(coverage_row["revision"], "coverage revision"),
        supersedes_revision=supersedes,
        verified=False,
        complete=False,
        source_note="",
    )
    definitions_value = root["classifications"]
    if not isinstance(definitions_value, list) or not definitions_value:
        raise ClassificationAdapterError(
            "classifications must be a non-empty array"
        )
    source_definitions: list[ClassificationSourceDefinition] = []
    for index, item in enumerate(definitions_value):
        row = _object(item, f"classifications[{index}]")
        _fields(row, f"classifications[{index}]", _CLASSIFICATION_FIELDS)
        supersedes_value = (
            None
            if row["supersedes"] is None
            else _require_text(row["supersedes"], "classification supersedes")
        )
        source_definitions.append(
            ClassificationSourceDefinition(
                classification_id=_require_text(
                    row["classification_id"], "classification_id"
                ),
                name=_require_text(row["name"], "classification name"),
                parent_classification_id=(
                    None
                    if row["parent_classification_id"] is None
                    else _require_text(
                        row["parent_classification_id"],
                        "parent_classification_id",
                    )
                ),
                effective_from=_parse_date(
                    row["effective_from"], "classification effective_from"
                ),
                effective_to=_optional_date(
                    row["effective_to"], "classification effective_to"
                ),
                revision=_require_text(row["revision"], "classification revision"),
                supersedes=supersedes_value,
            )
        )
    source_definitions.sort(
        key=lambda item: (item.classification_id, item.revision, item.candidate_id)
    )
    definition_keys = [
        (item.classification_id, item.revision) for item in source_definitions
    ]
    if len(set(definition_keys)) != len(definition_keys):
        raise ClassificationAdapterError(
            "duplicate classification/revision is forbidden"
        )
    classification_facts = tuple(
        ClassificationFact(
            taxonomy_id=taxonomy.taxonomy_id,
            classification_id=item.classification_id,
            name=item.name,
            parent_classification_id=item.parent_classification_id,
            effective_from=item.effective_from,
            effective_to=item.effective_to,
            known_at=descriptor.retrieved_at,
            usable_from=descriptor.retrieved_at,
            source=taxonomy.owner,
            taxonomy_version=taxonomy.taxonomy_version,
            revision=item.revision,
            supersedes_revision=item.supersedes,
            verified=False,
            source_note="",
        )
        for item in source_definitions
    )
    classification_ids = {item.classification_id for item in source_definitions}

    memberships_value = root["memberships"]
    if not isinstance(memberships_value, list) or not memberships_value:
        raise ClassificationAdapterError("memberships must be a non-empty array")
    source_memberships: list[ClassificationSourceMembership] = []
    for index, item in enumerate(memberships_value):
        row = _object(item, f"memberships[{index}]")
        _fields(row, f"memberships[{index}]", _MEMBERSHIP_FIELDS)
        classification_id = _require_text(
            row["classification_id"], "membership classification_id"
        )
        if classification_id not in classification_ids:
            raise ClassificationAdapterError(
                "membership references unknown classification_id"
            )
        exchange = _require_text(row["exchange"], "membership exchange")
        try:
            state = ClassificationMembershipState(
                _require_text(row["state"], "membership state")
            )
        except ValueError as exc:
            raise ClassificationAdapterError("invalid membership state") from exc
        source_memberships.append(
            ClassificationSourceMembership(
                classification_id=classification_id,
                source_security_id=_require_text(
                    row["source_security_id"], "source_security_id"
                ),
                symbol=_symbol(row["symbol"], exchange),
                exchange=exchange,
                effective_from=_parse_date(
                    row["effective_from"], "membership effective_from"
                ),
                effective_to=_optional_date(
                    row["effective_to"], "membership effective_to"
                ),
                state=state,
                revision=_require_text(row["revision"], "membership revision"),
                supersedes=(
                    None
                    if row["supersedes"] is None
                    else _require_text(row["supersedes"], "membership supersedes")
                ),
            )
        )
    source_memberships.sort(
        key=lambda item: (
            item.classification_id,
            item.source_security_id,
            item.revision,
            item.candidate_id,
        )
    )
    membership_keys = [
        (item.classification_id, item.source_security_id, item.revision)
        for item in source_memberships
    ]
    if len(set(membership_keys)) != len(membership_keys):
        raise ClassificationAdapterError(
            "duplicate membership/revision is forbidden"
        )

    visible_identities = [
        item
        for item in identities
        if to_utc(item.provenance.known_at) <= to_utc(descriptor.retrieved_at)
        and to_utc(item.provenance.usable_from) <= to_utc(descriptor.retrieved_at)
    ]
    membership_facts: list[ClassificationMembershipFact] = []
    bound_membership_candidate_ids: set[str] = set()
    used_identities: dict[str, IdentityCandidate] = {}
    missing: set[str] = set()
    inactive: set[str] = set()
    mismatch: set[str] = set()
    ambiguous: set[str] = set()
    for item in source_memberships:
        candidates = [
            identity
            for identity in visible_identities
            if identity.exchange == item.exchange
            and identity.source_security_id == item.source_security_id
        ]
        if not candidates:
            missing.add(item.candidate_id)
            continue
        active = [
            identity for identity in candidates if identity.active_on(item.effective_from)
        ]
        if not active:
            inactive.add(item.candidate_id)
            continue
        symbol_matches = [
            identity for identity in active if identity.symbol == item.symbol
        ]
        if not symbol_matches:
            mismatch.add(item.candidate_id)
            continue
        if len(symbol_matches) != 1:
            ambiguous.add(item.candidate_id)
            continue
        identity = symbol_matches[0]
        bound_membership_candidate_ids.add(item.candidate_id)
        used_identities[identity.candidate_id] = identity
        membership_facts.append(
            ClassificationMembershipFact(
                taxonomy_id=taxonomy.taxonomy_id,
                classification_id=item.classification_id,
                instrument_id=identity.instrument_id,
                identity_fact_id=identity.fact.fact_id,
                symbol=identity.symbol,
                market=Market.A,
                effective_from=item.effective_from,
                effective_to=item.effective_to,
                state=item.state,
                known_at=descriptor.retrieved_at,
                usable_from=descriptor.retrieved_at,
                source=taxonomy.owner,
                taxonomy_version=taxonomy.taxonomy_version,
                revision=item.revision,
                supersedes_revision=item.supersedes,
                verified=False,
                source_note="",
            )
        )
    declared_gaps_value = root["declared_gaps"]
    if not isinstance(declared_gaps_value, list):
        raise ClassificationAdapterError("declared_gaps must be an array")
    declared_gaps = tuple(
        sorted(
            {
                _require_text(item, "declared gap")
                for item in declared_gaps_value
            }
        )
    )
    report = ClassificationBindingReport(
        missing_identity=tuple(sorted(missing)),
        inactive_identity=tuple(sorted(inactive)),
        symbol_mismatch=tuple(sorted(mismatch)),
        ambiguous_identity=tuple(sorted(ambiguous)),
        declared_gaps=declared_gaps,
    )
    return ClassificationCandidateBundle(
        descriptor=descriptor,
        taxonomy=taxonomy,
        coverage=coverage,
        classifications=tuple(
            sorted(
                classification_facts,
                key=lambda item: (item.classification_id, item.fact_id),
            )
        ),
        memberships=tuple(
            sorted(
                membership_facts,
                key=lambda item: (
                    item.classification_id,
                    item.instrument_id,
                    item.fact_id,
                ),
            )
        ),
        identities=tuple(
            sorted(used_identities.values(), key=lambda item: item.candidate_id)
        ),
        source_membership_candidate_ids=tuple(
            sorted(item.candidate_id for item in source_memberships)
        ),
        bound_membership_candidate_ids=tuple(
            sorted(bound_membership_candidate_ids)
        ),
        declared_gaps=declared_gaps,
        report=report,
        _factory_marker=_CLASSIFICATION_BUNDLE_FACTORY_MARKER,
    )


__all__ = [
    "CLASSIFICATION_ADAPTER_VERSION",
    "CLASSIFICATION_DESCRIPTOR_SCHEMA",
    "CLASSIFICATION_SOURCE_SCHEMA",
    "ClassificationAdapterError",
    "ClassificationArtifactDescriptor",
    "ClassificationBindingReport",
    "ClassificationCandidateBundle",
    "ClassificationPublicationGranularity",
    "ClassificationSourceDefinition",
    "ClassificationSourceMembership",
    "parse_classification_artifact",
    "read_classification_descriptor",
]
