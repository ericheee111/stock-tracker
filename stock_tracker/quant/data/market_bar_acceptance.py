"""Stage 2H-2J exact-raw market-bar acceptance and T3 preflight.

The acceptance layer consumes already captured exact-raw market-bar descriptors,
replays the pinned parsers, runs the Stage 2G field/session reconciliation, and
records which external assurance declarations are present.  Declarations are
not approval authority: this module intentionally has no trusted closure
registry and therefore never promotes data to T3 or research grade.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from stock_tracker.core.types import Market, market_from_symbol

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from .bar_artifact import load_captured_market_bars
from .manifest import ManifestContractError, validate_storage_key
from .market_bar_golden import MarketBarParserBinding
from .market_bar_reconciliation import (
    MarketBarCandidateState,
    MarketBarField,
    MarketBarLicenseStatus,
    MarketBarReconciliationError,
    MarketBarReconciliationPolicy,
    MarketBarReconciliationReport,
    MarketBarSeriesEvidence,
    reconcile_market_bars,
)

MANIFEST_SCHEMA = "stage2h-market-bar-acceptance-manifest-v1"
REPORT_SCHEMA = "stage2h-market-bar-acceptance-report-v1"
DEFAULT_ACCEPTANCE_VERSION = "stage2h-market-bar-acceptance-v1"
NO_TRUSTED_ASSURANCE_AUTHORITY = "NO_TRUSTED_MARKET_BAR_ASSURANCE_AUTHORITY"
SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING = (
    "SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING"
)
CORPORATE_ACTION_REFERENCE_MISSING = "CORPORATE_ACTION_REFERENCE_MISSING"
SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED = (
    "SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED"
)
CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED = (
    "CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CASE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


class MarketBarAcceptanceError(ValueError):
    """Raised when a Stage 2H-2J acceptance input violates its contract."""


class MarketBarAssuranceKind(StrEnum):
    CALENDAR_AUTHORITY = "CALENDAR_AUTHORITY"
    RECONCILIATION_POLICY_APPROVAL = "RECONCILIATION_POLICY_APPROVAL"
    SOURCE_FAMILY_INDEPENDENCE = "SOURCE_FAMILY_INDEPENDENCE"
    FIELD_UNIT_POLICY = "FIELD_UNIT_POLICY"
    ADJUSTMENT_EQUIVALENCE = "ADJUSTMENT_EQUIVALENCE"
    ARTIFACT_ATTESTATION = "ARTIFACT_ATTESTATION"
    LIVE_PROVENANCE = "LIVE_PROVENANCE"
    LICENSE_APPROVAL = "LICENSE_APPROVAL"
    SECURITY_STATUS_UNIVERSE_BINDING = "SECURITY_STATUS_UNIVERSE_BINDING"
    CORPORATE_ACTION_BINDING = "CORPORATE_ACTION_BINDING"
    T3_PROMOTION_DECISION = "T3_PROMOTION_DECISION"


class MarketBarAcceptanceState(StrEnum):
    HARD_BLOCKED = "HARD_BLOCKED"
    SYNTHETIC_CONTRACT_ONLY = "SYNTHETIC_CONTRACT_ONLY"
    NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE = (
        "NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE"
    )


class MarketBarT3PreflightState(StrEnum):
    HARD_BLOCKED = "HARD_BLOCKED"
    EVIDENCE_PACKAGE_INCOMPLETE = "EVIDENCE_PACKAGE_INCOMPLETE"
    PENDING_INDEPENDENT_AUTHORITY = "PENDING_INDEPENDENT_AUTHORITY"


_BLOCKER_ASSURANCE_KIND: dict[str, MarketBarAssuranceKind] = {
    "CALENDAR_BINDING_NOT_INDEPENDENTLY_VERIFIED": (
        MarketBarAssuranceKind.CALENDAR_AUTHORITY
    ),
    "RECONCILIATION_POLICY_NOT_INDEPENDENTLY_APPROVED": (
        MarketBarAssuranceKind.RECONCILIATION_POLICY_APPROVAL
    ),
    "SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED": (
        MarketBarAssuranceKind.SOURCE_FAMILY_INDEPENDENCE
    ),
    "MARKET_BAR_FIELD_UNIT_POLICY_UNVERIFIED": (
        MarketBarAssuranceKind.FIELD_UNIT_POLICY
    ),
    "ADJUSTMENT_POLICY_EQUIVALENCE_UNVERIFIED": (
        MarketBarAssuranceKind.ADJUSTMENT_EQUIVALENCE
    ),
    "MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED": (
        MarketBarAssuranceKind.ARTIFACT_ATTESTATION
    ),
    "LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED": (
        MarketBarAssuranceKind.LIVE_PROVENANCE
    ),
    "SYNTHETIC_MARKET_BAR_EVIDENCE": MarketBarAssuranceKind.LIVE_PROVENANCE,
    "LICENSE_PENDING": MarketBarAssuranceKind.LICENSE_APPROVAL,
    SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED: (
        MarketBarAssuranceKind.SECURITY_STATUS_UNIVERSE_BINDING
    ),
    CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED: (
        MarketBarAssuranceKind.CORPORATE_ACTION_BINDING
    ),
    "T3_NOT_REACHED": MarketBarAssuranceKind.T3_PROMOTION_DECISION,
}
_REQUIRED_ASSURANCE_KINDS = tuple(sorted(set(_BLOCKER_ASSURANCE_KIND.values()), key=str))
_SOURCE_SCOPED_ASSURANCE_KINDS = frozenset(
    {
        MarketBarAssuranceKind.SOURCE_FAMILY_INDEPENDENCE,
        MarketBarAssuranceKind.FIELD_UNIT_POLICY,
        MarketBarAssuranceKind.ADJUSTMENT_EQUIVALENCE,
        MarketBarAssuranceKind.ARTIFACT_ATTESTATION,
        MarketBarAssuranceKind.LIVE_PROVENANCE,
        MarketBarAssuranceKind.LICENSE_APPROVAL,
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBarAcceptanceError(f"{name} must be a non-empty trimmed string")
    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MarketBarAcceptanceError(f"{name} contains invalid characters")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MarketBarAcceptanceError(f"{name} must be lowercase SHA-256")
    return text


def _optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise MarketBarAcceptanceError(f"{name} must be boolean")
    return value


def _strict_json(raw: bytes, name: str) -> object:
    if not isinstance(raw, bytes) or not raw:
        raise MarketBarAcceptanceError(f"{name} must be non-empty UTF-8 JSON")

    def pairs_hook(pairs):
        output: dict[str, Any] = {}
        for key, value in pairs:
            if type(key) is not str:
                raise MarketBarAcceptanceError(f"{name} keys must be strings")
            if key in output:
                raise MarketBarAcceptanceError(f"{name} contains duplicate JSON keys")
            output[key] = value
        return output

    def reject_constant(value: str):
        raise MarketBarAcceptanceError(f"{name} contains non-finite token: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise MarketBarAcceptanceError(f"{name} must use UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise MarketBarAcceptanceError(f"{name} is invalid JSON") from exc


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketBarAcceptanceError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise MarketBarAcceptanceError(f"{name} must be an array")
    return cast(list[Any], value)


def _fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise MarketBarAcceptanceError(f"{name} contains unknown field: {unknown[0]}")
    if missing:
        raise MarketBarAcceptanceError(f"{name} is missing field: {missing[0]}")


def _parse_datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketBarAcceptanceError(f"{name} must be ISO-8601 datetime") from exc
    ensure_aware(parsed, name)
    return to_utc(parsed)


def _parse_date(value: object, name: str) -> date:
    text = _text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise MarketBarAcceptanceError(f"{name} must be ISO date") from exc


def _canonical_texts(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(_text(value, f"{name} item") for value in values)
    if len(result) != len(set(result)):
        raise MarketBarAcceptanceError(f"{name} must not contain duplicates")
    return tuple(sorted(result))


def _canonical_sha256s(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(_sha256(value, f"{name} item") for value in values)
    if len(result) != len(set(result)):
        raise MarketBarAcceptanceError(f"{name} must not contain duplicates")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class MarketBarAssuranceDeclaration:
    kind: MarketBarAssuranceKind
    source_owner: str
    source_version: str
    known_at: datetime
    usable_from: datetime
    markets: tuple[Market, ...]
    sources: tuple[str, ...]
    evidence_artifact_ids: tuple[str, ...]
    synthetic: bool
    details: tuple[str, ...] = ()
    declaration_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MarketBarAssuranceKind):
            raise MarketBarAcceptanceError("assurance kind is invalid")
        _text(self.source_owner, "assurance source_owner")
        _text(self.source_version, "assurance source_version")
        known_at = to_utc(ensure_aware(self.known_at, "assurance known_at"))
        usable_from = to_utc(ensure_aware(self.usable_from, "assurance usable_from"))
        if known_at > usable_from:
            raise MarketBarAcceptanceError("assurance known_at cannot exceed usable_from")
        markets = tuple(sorted(set(self.markets), key=lambda item: item.value))
        if not markets or any(not isinstance(item, Market) for item in markets):
            raise MarketBarAcceptanceError("assurance markets must contain Market values")
        sources = _canonical_texts(self.sources, "assurance sources")
        if any(_CASE_TOKEN.fullmatch(item) is None for item in sources):
            raise MarketBarAcceptanceError("assurance sources contain an unsafe token")
        if self.kind in _SOURCE_SCOPED_ASSURANCE_KINDS and not sources:
            raise MarketBarAcceptanceError(
                f"{self.kind.value} assurance must name its covered sources"
            )
        if (
            self.kind is MarketBarAssuranceKind.SOURCE_FAMILY_INDEPENDENCE
            and len(sources) < 2
        ):
            raise MarketBarAcceptanceError(
                "SOURCE_FAMILY_INDEPENDENCE assurance requires at least two sources"
            )
        evidence_ids = _canonical_sha256s(
            self.evidence_artifact_ids,
            "assurance evidence_artifact_ids",
        )
        if not evidence_ids:
            raise MarketBarAcceptanceError(
                "assurance declaration requires evidence_artifact_ids"
            )
        _boolean(self.synthetic, "assurance synthetic")
        details = _canonical_texts(self.details, "assurance details")
        object.__setattr__(self, "known_at", known_at)
        object.__setattr__(self, "usable_from", usable_from)
        object.__setattr__(self, "markets", markets)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "evidence_artifact_ids", evidence_ids)
        object.__setattr__(self, "details", details)
        object.__setattr__(self, "declaration_id", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "stage2i-market-bar-assurance-declaration-v1",
            "kind": self.kind.value,
            "source_owner": self.source_owner,
            "source_version": self.source_version,
            "known_at": self.known_at,
            "usable_from": self.usable_from,
            "markets": [item.value for item in self.markets],
            "sources": list(self.sources),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "synthetic": self.synthetic,
            "details": list(self.details),
        }

    def covers(self, market: Market, sources: Iterable[str], as_of: datetime) -> bool:
        required_sources = set(sources)
        return (
            market in self.markets
            and (not self.sources or required_sources.issubset(self.sources))
            and self.usable_from <= to_utc(as_of)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "known_at": self.known_at.isoformat().replace("+00:00", "Z"),
            "usable_from": self.usable_from.isoformat().replace("+00:00", "Z"),
            "declaration_id": self.declaration_id,
        }

    def write_json(self, path: str | Path) -> None:
        _atomic_write_text(
            Path(path),
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarketBarAssuranceDeclaration:
        _fields(
            value,
            {
                "schema",
                "kind",
                "source_owner",
                "source_version",
                "known_at",
                "usable_from",
                "markets",
                "sources",
                "evidence_artifact_ids",
                "synthetic",
                "details",
                "declaration_id",
            },
            "assurance declaration",
        )
        if value.get("schema") != "stage2i-market-bar-assurance-declaration-v1":
            raise MarketBarAcceptanceError("unsupported assurance declaration schema")
        try:
            kind = MarketBarAssuranceKind(
                _text(value.get("kind"), "assurance kind")
            )
            markets = tuple(
                Market(_text(item, "assurance market"))
                for item in _array(value.get("markets"), "markets")
            )
        except ValueError as exc:
            raise MarketBarAcceptanceError(
                "assurance declaration enum value is invalid"
            ) from exc
        declaration = cls(
            kind=kind,
            source_owner=_text(value.get("source_owner"), "source_owner"),
            source_version=_text(value.get("source_version"), "source_version"),
            known_at=_parse_datetime(value.get("known_at"), "known_at"),
            usable_from=_parse_datetime(value.get("usable_from"), "usable_from"),
            markets=markets,
            sources=tuple(
                _text(item, "assurance source")
                for item in _array(value.get("sources"), "sources")
            ),
            evidence_artifact_ids=tuple(
                _sha256(item, "assurance evidence_artifact_id")
                for item in _array(
                    value.get("evidence_artifact_ids"),
                    "evidence_artifact_ids",
                )
            ),
            synthetic=_boolean(value.get("synthetic"), "synthetic"),
            details=tuple(
                _text(item, "assurance detail")
                for item in _array(value.get("details"), "details")
            ),
        )
        if declaration.declaration_id != _sha256(
            value.get("declaration_id"),
            "declaration_id",
        ):
            raise MarketBarAcceptanceError(
                "declaration_id does not match assurance content"
            )
        return declaration


def load_market_bar_assurance_declaration(
    path: str | Path,
) -> MarketBarAssuranceDeclaration:
    value = _object(
        _strict_json(Path(path).read_bytes(), "assurance declaration"),
        "assurance declaration",
    )
    return MarketBarAssuranceDeclaration.from_dict(value)


@dataclass(frozen=True, slots=True)
class MarketBarCaptureReference:
    source: str
    descriptor_key: str
    parser_binding_id: str

    def __post_init__(self) -> None:
        if _CASE_TOKEN.fullmatch(_text(self.source, "capture source")) is None:
            raise MarketBarAcceptanceError("capture source is unsafe")
        try:
            object.__setattr__(
                self,
                "descriptor_key",
                validate_storage_key(self.descriptor_key),
            )
        except ManifestContractError as exc:
            raise MarketBarAcceptanceError("capture descriptor_key is invalid") from exc
        _sha256(self.parser_binding_id, "parser_binding_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "descriptor_key": self.descriptor_key,
            "parser_binding_id": self.parser_binding_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarketBarCaptureReference:
        _fields(
            value,
            {"source", "descriptor_key", "parser_binding_id"},
            "capture reference",
        )
        return cls(
            source=_text(value.get("source"), "capture source"),
            descriptor_key=_text(value.get("descriptor_key"), "descriptor_key"),
            parser_binding_id=_sha256(
                value.get("parser_binding_id"),
                "parser_binding_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketBarAuxiliaryBindings:
    stage2_reconciliation_report_id: str | None = None
    corporate_action_report_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage2_reconciliation_report_id",
            _optional_sha256(
                self.stage2_reconciliation_report_id,
                "stage2_reconciliation_report_id",
            ),
        )
        object.__setattr__(
            self,
            "corporate_action_report_id",
            _optional_sha256(
                self.corporate_action_report_id,
                "corporate_action_report_id",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "stage2_reconciliation_report_id": self.stage2_reconciliation_report_id,
            "corporate_action_report_id": self.corporate_action_report_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarketBarAuxiliaryBindings:
        _fields(
            value,
            {"stage2_reconciliation_report_id", "corporate_action_report_id"},
            "auxiliary bindings",
        )
        return cls(
            stage2_reconciliation_report_id=_optional_sha256(
                value.get("stage2_reconciliation_report_id"),
                "stage2_reconciliation_report_id",
            ),
            corporate_action_report_id=_optional_sha256(
                value.get("corporate_action_report_id"),
                "corporate_action_report_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketBarAcceptanceCase:
    case_name: str
    market: Market
    symbol: str
    interval: str
    adjustment: str
    as_of: datetime
    expected_open_sessions: tuple[date, ...]
    calendar_snapshot_id: str
    captures: tuple[MarketBarCaptureReference, ...]
    comparable_fields: tuple[MarketBarField, ...]
    assurance_declaration_ids: tuple[str, ...]
    auxiliary_bindings: MarketBarAuxiliaryBindings = field(
        default_factory=MarketBarAuxiliaryBindings
    )
    case_id: str = field(init=False)

    def __post_init__(self) -> None:
        if _CASE_TOKEN.fullmatch(_text(self.case_name, "case_name")) is None:
            raise MarketBarAcceptanceError("case_name is unsafe")
        if not isinstance(self.market, Market):
            raise MarketBarAcceptanceError("case market must be Market")
        if self.market is not Market.A:
            raise MarketBarAcceptanceError(
                "Stage 2H acceptance currently supports A shares only"
            )
        _text(self.symbol, "case symbol")
        if market_from_symbol(self.symbol) is not self.market:
            raise MarketBarAcceptanceError("case symbol suffix disagrees with market")
        _text(self.interval, "case interval")
        if self.interval != "1d":
            raise MarketBarAcceptanceError("Stage 2H supports interval='1d' only")
        _text(self.adjustment, "case adjustment")
        if self.adjustment != "qfq":
            raise MarketBarAcceptanceError(
                "Stage 2H acceptance currently supports adjustment='qfq' only"
            )
        cutoff = to_utc(ensure_aware(self.as_of, "case as_of"))
        sessions = tuple(sorted(set(self.expected_open_sessions)))
        if not sessions or sessions != self.expected_open_sessions:
            raise MarketBarAcceptanceError(
                "expected_open_sessions must be non-empty, sorted and unique"
            )
        if any(type(item) is not date for item in sessions):
            raise MarketBarAcceptanceError(
                "expected_open_sessions must contain date values"
            )
        calendar_id = _sha256(self.calendar_snapshot_id, "calendar_snapshot_id")
        captures = tuple(sorted(self.captures, key=lambda item: item.source))
        if len(captures) < 2:
            raise MarketBarAcceptanceError("acceptance case requires at least two captures")
        if any(not isinstance(item, MarketBarCaptureReference) for item in captures):
            raise MarketBarAcceptanceError("captures must contain capture references")
        if len({item.source for item in captures}) != len(captures):
            raise MarketBarAcceptanceError("capture sources must be unique")
        fields = tuple(sorted(set(self.comparable_fields), key=lambda item: item.value))
        if fields != self.comparable_fields or not fields:
            raise MarketBarAcceptanceError(
                "comparable_fields must be non-empty, sorted and unique"
            )
        if any(not isinstance(item, MarketBarField) for item in fields):
            raise MarketBarAcceptanceError("comparable_fields contains invalid field")
        declarations = _canonical_sha256s(
            self.assurance_declaration_ids,
            "assurance_declaration_ids",
        )
        if not isinstance(self.auxiliary_bindings, MarketBarAuxiliaryBindings):
            raise MarketBarAcceptanceError("auxiliary_bindings is invalid")
        object.__setattr__(self, "as_of", cutoff)
        object.__setattr__(self, "calendar_snapshot_id", calendar_id)
        object.__setattr__(self, "captures", captures)
        object.__setattr__(self, "assurance_declaration_ids", declarations)
        object.__setattr__(self, "case_id", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "stage2h-market-bar-acceptance-case-v1",
            "case_name": self.case_name,
            "market": self.market.value,
            "symbol": self.symbol,
            "interval": self.interval,
            "adjustment": self.adjustment,
            "as_of": self.as_of,
            "expected_open_sessions": self.expected_open_sessions,
            "calendar_snapshot_id": self.calendar_snapshot_id,
            "captures": [item.as_dict() for item in self.captures],
            "comparable_fields": [item.value for item in self.comparable_fields],
            "assurance_declaration_ids": list(self.assurance_declaration_ids),
            "auxiliary_bindings": self.auxiliary_bindings.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "expected_open_sessions": [
                item.isoformat() for item in self.expected_open_sessions
            ],
            "case_id": self.case_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarketBarAcceptanceCase:
        _fields(
            value,
            {
                "schema",
                "case_name",
                "market",
                "symbol",
                "interval",
                "adjustment",
                "as_of",
                "expected_open_sessions",
                "calendar_snapshot_id",
                "captures",
                "comparable_fields",
                "assurance_declaration_ids",
                "auxiliary_bindings",
                "case_id",
            },
            "acceptance case",
        )
        if value.get("schema") != "stage2h-market-bar-acceptance-case-v1":
            raise MarketBarAcceptanceError("unsupported acceptance case schema")
        try:
            market = Market(_text(value.get("market"), "market"))
            comparable_fields = tuple(
                MarketBarField(_text(item, "comparable field"))
                for item in _array(
                    value.get("comparable_fields"),
                    "comparable_fields",
                )
            )
        except ValueError as exc:
            raise MarketBarAcceptanceError("acceptance case enum value is invalid") from exc
        case = cls(
            case_name=_text(value.get("case_name"), "case_name"),
            market=market,
            symbol=_text(value.get("symbol"), "symbol"),
            interval=_text(value.get("interval"), "interval"),
            adjustment=_text(value.get("adjustment"), "adjustment"),
            as_of=_parse_datetime(value.get("as_of"), "as_of"),
            expected_open_sessions=tuple(
                _parse_date(item, "expected_open_session")
                for item in _array(
                    value.get("expected_open_sessions"),
                    "expected_open_sessions",
                )
            ),
            calendar_snapshot_id=_sha256(
                value.get("calendar_snapshot_id"),
                "calendar_snapshot_id",
            ),
            captures=tuple(
                MarketBarCaptureReference.from_dict(
                    _object(item, "capture reference")
                )
                for item in _array(value.get("captures"), "captures")
            ),
            comparable_fields=comparable_fields,
            assurance_declaration_ids=tuple(
                _sha256(item, "assurance_declaration_id")
                for item in _array(
                    value.get("assurance_declaration_ids"),
                    "assurance_declaration_ids",
                )
            ),
            auxiliary_bindings=MarketBarAuxiliaryBindings.from_dict(
                _object(value.get("auxiliary_bindings"), "auxiliary_bindings")
            ),
        )
        if case.case_id != _sha256(value.get("case_id"), "case_id"):
            raise MarketBarAcceptanceError("case_id does not match case content")
        return case


@dataclass(frozen=True, slots=True)
class MarketBarAcceptanceManifest:
    acceptance_version: str
    created_at: datetime
    cases: tuple[MarketBarAcceptanceCase, ...]
    assurance_declarations: tuple[MarketBarAssuranceDeclaration, ...]
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.acceptance_version, "acceptance_version")
        created_at = to_utc(ensure_aware(self.created_at, "created_at"))
        cases = tuple(sorted(self.cases, key=lambda item: item.case_name))
        if not cases or any(not isinstance(item, MarketBarAcceptanceCase) for item in cases):
            raise MarketBarAcceptanceError("manifest requires acceptance cases")
        if len({item.case_name for item in cases}) != len(cases):
            raise MarketBarAcceptanceError("manifest case names must be unique")
        if len({item.case_id for item in cases}) != len(cases):
            raise MarketBarAcceptanceError("manifest case IDs must be unique")
        if any(item.as_of > created_at for item in cases):
            raise MarketBarAcceptanceError("manifest created_at cannot precede case as_of")
        declarations = tuple(
            sorted(self.assurance_declarations, key=lambda item: item.declaration_id)
        )
        if any(
            not isinstance(item, MarketBarAssuranceDeclaration)
            for item in declarations
        ):
            raise MarketBarAcceptanceError(
                "assurance_declarations contains invalid item"
            )
        if len({item.declaration_id for item in declarations}) != len(declarations):
            raise MarketBarAcceptanceError("assurance declaration IDs must be unique")
        if any(
            item.known_at > created_at or item.usable_from > created_at
            for item in declarations
        ):
            raise MarketBarAcceptanceError(
                "manifest cannot contain assurance evidence from after created_at"
            )
        available = {item.declaration_id for item in declarations}
        referenced = {
            declaration_id
            for case in cases
            for declaration_id in case.assurance_declaration_ids
        }
        missing = sorted(referenced - available)
        if missing:
            raise MarketBarAcceptanceError(
                "case references missing assurance declaration: " + missing[0]
            )
        unreferenced = sorted(available - referenced)
        if unreferenced:
            raise MarketBarAcceptanceError(
                "manifest contains unreferenced assurance declaration: "
                + unreferenced[0]
            )
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "assurance_declarations", declarations)
        object.__setattr__(self, "manifest_id", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": MANIFEST_SCHEMA,
            "acceptance_version": self.acceptance_version,
            "created_at": self.created_at,
            "cases": [item.as_dict() for item in self.cases],
            "assurance_declarations": [
                item.as_dict() for item in self.assurance_declarations
            ],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "manifest_id": self.manifest_id,
        }

    def write_json(self, path: str | Path) -> None:
        _atomic_write_text(
            Path(path),
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )

    @classmethod
    def read_json(cls, path: str | Path) -> MarketBarAcceptanceManifest:
        value = _object(
            _strict_json(Path(path).read_bytes(), "acceptance manifest"),
            "acceptance manifest",
        )
        _fields(
            value,
            {
                "schema",
                "acceptance_version",
                "created_at",
                "cases",
                "assurance_declarations",
                "manifest_id",
            },
            "acceptance manifest",
        )
        if value.get("schema") != MANIFEST_SCHEMA:
            raise MarketBarAcceptanceError("unsupported acceptance manifest schema")
        manifest = cls(
            acceptance_version=_text(
                value.get("acceptance_version"),
                "acceptance_version",
            ),
            created_at=_parse_datetime(value.get("created_at"), "created_at"),
            cases=tuple(
                MarketBarAcceptanceCase.from_dict(_object(item, "acceptance case"))
                for item in _array(value.get("cases"), "cases")
            ),
            assurance_declarations=tuple(
                MarketBarAssuranceDeclaration.from_dict(
                    _object(item, "assurance declaration")
                )
                for item in _array(
                    value.get("assurance_declarations"),
                    "assurance_declarations",
                )
            ),
        )
        if manifest.manifest_id != _sha256(value.get("manifest_id"), "manifest_id"):
            raise MarketBarAcceptanceError(
                "manifest_id does not match acceptance manifest content"
            )
        return manifest


@dataclass(frozen=True, slots=True)
class MarketBarAssuranceCoverage:
    required_kinds: tuple[MarketBarAssuranceKind, ...]
    declared_kinds: tuple[MarketBarAssuranceKind, ...]
    missing_kinds: tuple[MarketBarAssuranceKind, ...]
    declaration_ids: tuple[str, ...]
    synthetic_declaration_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("required_kinds", "declared_kinds", "missing_kinds"):
            values = getattr(self, name)
            if any(not isinstance(item, MarketBarAssuranceKind) for item in values):
                raise MarketBarAcceptanceError(
                    f"{name} contains invalid assurance kind"
                )
        required = tuple(sorted(set(self.required_kinds), key=str))
        declared = tuple(sorted(set(self.declared_kinds), key=str))
        missing = tuple(sorted(set(self.missing_kinds), key=str))
        if required != self.required_kinds:
            raise MarketBarAcceptanceError("required assurance kinds must be canonical")
        if declared != self.declared_kinds:
            raise MarketBarAcceptanceError("declared assurance kinds must be canonical")
        if not set(declared).issubset(required):
            raise MarketBarAcceptanceError(
                "declared assurance kinds must be a subset of required kinds"
            )
        if missing != self.missing_kinds or set(missing) != set(required) - set(declared):
            raise MarketBarAcceptanceError("missing assurance kinds are inconsistent")
        declaration_ids = _canonical_sha256s(
            self.declaration_ids,
            "declaration_ids",
        )
        synthetic_ids = _canonical_sha256s(
            self.synthetic_declaration_ids,
            "synthetic_declaration_ids",
        )
        if not set(synthetic_ids).issubset(declaration_ids):
            raise MarketBarAcceptanceError(
                "synthetic declaration IDs must be a subset of declaration IDs"
            )
        object.__setattr__(self, "declaration_ids", declaration_ids)
        object.__setattr__(self, "synthetic_declaration_ids", synthetic_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "required_kinds": [item.value for item in self.required_kinds],
            "declared_kinds": [item.value for item in self.declared_kinds],
            "missing_kinds": [item.value for item in self.missing_kinds],
            "declaration_ids": list(self.declaration_ids),
            "synthetic_declaration_ids": list(self.synthetic_declaration_ids),
            "trusted_authority_configured": False,
        }


@dataclass(frozen=True, slots=True)
class MarketBarAcceptanceCaseReport:
    case: MarketBarAcceptanceCase = field(repr=False)
    reconciliation: MarketBarReconciliationReport = field(repr=False)
    assurance_declarations: tuple[MarketBarAssuranceDeclaration, ...] = field(
        repr=False
    )
    assurance_coverage: MarketBarAssuranceCoverage = field(repr=False)
    non_synthetic_declared: bool = field(init=False)
    open_blockers: tuple[str, ...] = field(init=False)
    acceptance_state: MarketBarAcceptanceState = field(init=False)
    t3_preflight_state: MarketBarT3PreflightState = field(init=False)
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.case, MarketBarAcceptanceCase):
            raise MarketBarAcceptanceError("case report case is invalid")
        if not isinstance(self.reconciliation, MarketBarReconciliationReport):
            raise MarketBarAcceptanceError("case reconciliation is invalid")
        declarations = tuple(
            sorted(self.assurance_declarations, key=lambda item: item.declaration_id)
        )
        if any(
            not isinstance(item, MarketBarAssuranceDeclaration)
            for item in declarations
        ):
            raise MarketBarAcceptanceError(
                "case report assurance declarations are invalid"
            )
        if len({item.declaration_id for item in declarations}) != len(declarations):
            raise MarketBarAcceptanceError(
                "case report assurance declaration IDs must be unique"
            )
        if tuple(item.declaration_id for item in declarations) != (
            self.case.assurance_declaration_ids
        ):
            raise MarketBarAcceptanceError(
                "case report assurance declarations disagree with the case"
            )
        if not isinstance(self.assurance_coverage, MarketBarAssuranceCoverage):
            raise MarketBarAcceptanceError("assurance coverage is invalid")
        object.__setattr__(self, "assurance_declarations", declarations)
        if self.reconciliation.as_of != self.case.as_of:
            raise MarketBarAcceptanceError(
                "reconciliation as_of disagrees with acceptance case"
            )
        if self.reconciliation.calendar_snapshot_id != self.case.calendar_snapshot_id:
            raise MarketBarAcceptanceError(
                "reconciliation Calendar identity disagrees with acceptance case"
            )
        if self.reconciliation.expected_open_sessions != self.case.expected_open_sessions:
            raise MarketBarAcceptanceError(
                "reconciliation sessions disagree with acceptance case"
            )
        identity = self.reconciliation.series[0]
        if (
            identity.market is not self.case.market
            or identity.symbol != self.case.symbol
            or identity.interval != self.case.interval
            or identity.adjustment != self.case.adjustment
        ):
            raise MarketBarAcceptanceError(
                "reconciliation identity disagrees with acceptance case"
            )
        reconciliation_sources = tuple(item.source for item in self.reconciliation.series)
        reference_sources = tuple(item.source for item in self.case.captures)
        if (
            len(reconciliation_sources) != len(reference_sources)
            or set(reconciliation_sources) != set(reference_sources)
        ):
            raise MarketBarAcceptanceError(
                "reconciliation sources disagree with acceptance capture references"
            )
        expected_coverage = _coverage_for_case(
            self.case,
            {item.declaration_id: item for item in declarations},
            reconciliation_sources,
        )
        if self.assurance_coverage != expected_coverage:
            raise MarketBarAcceptanceError(
                "case assurance coverage is not derived from its declarations"
            )
        non_synthetic_declared = all(
            not item.synthetic_fixture for item in self.reconciliation.series
        )
        blockers = set(self.reconciliation.open_blockers)
        missing_auxiliary_references: set[str] = set()
        if self.case.auxiliary_bindings.stage2_reconciliation_report_id is None:
            missing_auxiliary_references.add(
                SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING
            )
        if self.case.auxiliary_bindings.corporate_action_report_id is None:
            missing_auxiliary_references.add(CORPORATE_ACTION_REFERENCE_MISSING)
        blockers.update(missing_auxiliary_references)
        blockers.update(
            {
                SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED,
                CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED,
                NO_TRUSTED_ASSURANCE_AUTHORITY,
            }
        )
        if self.reconciliation.candidate_state is MarketBarCandidateState.HARD_BLOCKED:
            state = MarketBarAcceptanceState.HARD_BLOCKED
            preflight = MarketBarT3PreflightState.HARD_BLOCKED
        elif not non_synthetic_declared:
            state = MarketBarAcceptanceState.SYNTHETIC_CONTRACT_ONLY
            preflight = MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE
        else:
            state = (
                MarketBarAcceptanceState
                .NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
            )
            package_incomplete = bool(
                self.assurance_coverage.missing_kinds
                or missing_auxiliary_references
            )
            preflight = (
                MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE
                if package_incomplete
                else MarketBarT3PreflightState.PENDING_INDEPENDENT_AUTHORITY
            )
        object.__setattr__(
            self,
            "non_synthetic_declared",
            non_synthetic_declared,
        )
        object.__setattr__(self, "open_blockers", tuple(sorted(blockers)))
        object.__setattr__(self, "acceptance_state", state)
        object.__setattr__(self, "t3_preflight_state", preflight)
        object.__setattr__(self, "report_id", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "stage2h-market-bar-acceptance-case-report-v1",
            "case_id": self.case.case_id,
            "reconciliation_report_id": self.reconciliation.report_id,
            "assurance_declaration_ids": [
                item.declaration_id for item in self.assurance_declarations
            ],
            "assurance_coverage": self.assurance_coverage.as_dict(),
            "non_synthetic_declared": self.non_synthetic_declared,
            "open_blockers": list(self.open_blockers),
            "acceptance_state": self.acceptance_state.value,
            "t3_preflight_state": self.t3_preflight_state.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "case": self.case.as_dict(),
            "reconciliation": self.reconciliation.as_dict(),
            "report_id": self.report_id,
        }


@dataclass(frozen=True, slots=True)
class MarketBarAcceptanceReport:
    manifest: MarketBarAcceptanceManifest = field(repr=False)
    policy: MarketBarReconciliationPolicy = field(repr=False)
    cases: tuple[MarketBarAcceptanceCaseReport, ...]
    acceptance_state: MarketBarAcceptanceState = field(init=False)
    t3_preflight_state: MarketBarT3PreflightState = field(init=False)
    open_blockers: tuple[str, ...] = field(init=False)
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, MarketBarAcceptanceManifest):
            raise MarketBarAcceptanceError("acceptance report manifest is invalid")
        if not isinstance(self.policy, MarketBarReconciliationPolicy):
            raise MarketBarAcceptanceError("acceptance report policy is invalid")
        cases = tuple(sorted(self.cases, key=lambda item: item.case.case_name))
        if not cases or any(
            not isinstance(item, MarketBarAcceptanceCaseReport) for item in cases
        ):
            raise MarketBarAcceptanceError("acceptance report requires case reports")
        if len({item.case.case_id for item in cases}) != len(cases):
            raise MarketBarAcceptanceError("acceptance report case IDs must be unique")
        if {item.case.case_id for item in cases} != {
            item.case_id for item in self.manifest.cases
        }:
            raise MarketBarAcceptanceError(
                "acceptance report cases disagree with manifest"
            )
        declarations_by_id = {
            item.declaration_id: item for item in self.manifest.assurance_declarations
        }
        for item in cases:
            if item.reconciliation.policy.policy_id != self.policy.policy_id:
                raise MarketBarAcceptanceError(
                    "case reconciliation policy disagrees with acceptance report policy"
                )
            expected_coverage = _coverage_for_case(
                item.case,
                declarations_by_id,
                tuple(series.source for series in item.reconciliation.series),
            )
            if item.assurance_coverage != expected_coverage:
                raise MarketBarAcceptanceError(
                    "case assurance coverage is not derived from the manifest"
                )
        if any(
            item.acceptance_state is MarketBarAcceptanceState.HARD_BLOCKED
            for item in cases
        ):
            state = MarketBarAcceptanceState.HARD_BLOCKED
        elif any(
            item.acceptance_state is MarketBarAcceptanceState.SYNTHETIC_CONTRACT_ONLY
            for item in cases
        ):
            state = MarketBarAcceptanceState.SYNTHETIC_CONTRACT_ONLY
        else:
            state = (
                MarketBarAcceptanceState
                .NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
            )
        if any(
            item.t3_preflight_state is MarketBarT3PreflightState.HARD_BLOCKED
            for item in cases
        ):
            preflight = MarketBarT3PreflightState.HARD_BLOCKED
        elif any(
            item.t3_preflight_state
            is MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE
            for item in cases
        ):
            preflight = MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE
        else:
            preflight = MarketBarT3PreflightState.PENDING_INDEPENDENT_AUTHORITY
        blockers = tuple(
            sorted({code for item in cases for code in item.open_blockers})
        )
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "acceptance_state", state)
        object.__setattr__(self, "t3_preflight_state", preflight)
        object.__setattr__(self, "open_blockers", blockers)
        object.__setattr__(self, "report_id", fingerprint(self._identity_payload()))

    @property
    def finding_counts(self) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for item in self.cases:
            counter.update(item.reconciliation.finding_counts)
        return dict(sorted(counter.items()))

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": REPORT_SCHEMA,
            "manifest_id": self.manifest.manifest_id,
            "acceptance_version": self.manifest.acceptance_version,
            "policy_id": self.policy.policy_id,
            "case_report_ids": [item.report_id for item in self.cases],
            "acceptance_state": self.acceptance_state.value,
            "t3_preflight_state": self.t3_preflight_state.value,
            "open_blockers": list(self.open_blockers),
            "trusted_assurance_authority_configured": False,
            "research_grade": False,
            "t3_reached": False,
            "license_clearance_complete": False,
            "production_database_modified": False,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "cases": [item.as_dict() for item in self.cases],
            "assurance_declarations": [
                item.as_dict() for item in self.manifest.assurance_declarations
            ],
            "finding_counts": self.finding_counts,
            "report_id": self.report_id,
        }


def _coverage_for_case(
    case: MarketBarAcceptanceCase,
    declarations_by_id: Mapping[str, MarketBarAssuranceDeclaration],
    sources: tuple[str, ...],
) -> MarketBarAssuranceCoverage:
    declarations = tuple(
        declarations_by_id[item] for item in case.assurance_declaration_ids
    )
    source_set = set(sources)
    visible = tuple(
        item
        for item in declarations
        if case.market in item.markets and item.usable_from <= case.as_of
    )
    relevant = tuple(
        item
        for item in visible
        if (
            not item.sources
            or bool(source_set.intersection(item.sources))
            or source_set.issubset(item.sources)
        )
    )
    declared: set[MarketBarAssuranceKind] = set()
    for kind in _REQUIRED_ASSURANCE_KINDS:
        candidates = tuple(
            item
            for item in visible
            if item.kind is kind and not item.synthetic
        )
        if kind is MarketBarAssuranceKind.SOURCE_FAMILY_INDEPENDENCE:
            if any(source_set.issubset(item.sources) for item in candidates):
                declared.add(kind)
            continue
        if kind in _SOURCE_SCOPED_ASSURANCE_KINDS:
            covered_sources = set().union(*(set(item.sources) for item in candidates))
            if source_set.issubset(covered_sources):
                declared.add(kind)
            continue
        if any(
            not item.sources or source_set.issubset(item.sources)
            for item in candidates
        ):
            declared.add(kind)
    declared_kinds = tuple(sorted(declared, key=str))
    missing_kinds = tuple(
        item for item in _REQUIRED_ASSURANCE_KINDS if item not in declared
    )
    return MarketBarAssuranceCoverage(
        required_kinds=_REQUIRED_ASSURANCE_KINDS,
        declared_kinds=declared_kinds,
        missing_kinds=missing_kinds,
        declaration_ids=tuple(item.declaration_id for item in relevant),
        synthetic_declaration_ids=tuple(
            item.declaration_id for item in relevant if item.synthetic
        ),
    )


def materialize_market_bar_acceptance(
    *,
    manifest: MarketBarAcceptanceManifest,
    artifact_root: str | Path,
    parser_registry: Mapping[str, MarketBarParserBinding],
    policy: MarketBarReconciliationPolicy | None = None,
) -> MarketBarAcceptanceReport:
    if not isinstance(manifest, MarketBarAcceptanceManifest):
        raise MarketBarAcceptanceError("manifest is invalid")
    selected_policy = policy or MarketBarReconciliationPolicy()
    declarations_by_id = {
        item.declaration_id: item for item in manifest.assurance_declarations
    }
    case_reports: list[MarketBarAcceptanceCaseReport] = []
    for case in manifest.cases:
        series: list[MarketBarSeriesEvidence] = []
        for reference in case.captures:
            binding = parser_registry.get(reference.source)
            if binding is None:
                raise MarketBarAcceptanceError(
                    f"no parser binding for source: {reference.source}"
                )
            if not isinstance(binding, MarketBarParserBinding):
                raise MarketBarAcceptanceError("parser registry item is invalid")
            if binding.source != reference.source:
                raise MarketBarAcceptanceError(
                    "parser binding source differs from capture reference"
                )
            if binding.binding_id != reference.parser_binding_id:
                raise MarketBarAcceptanceError(
                    "parser binding ID differs from acceptance manifest"
                )
            try:
                captured = load_captured_market_bars(
                    artifact_root,
                    descriptor_key=reference.descriptor_key,
                    parser=binding.parser,
                )
            except (ManifestContractError, OSError, ValueError) as exc:
                raise MarketBarAcceptanceError(
                    f"captured market-bar descriptor failed validation: {reference.source}"
                ) from exc
            if captured.artifact.source != reference.source:
                raise MarketBarAcceptanceError(
                    "captured artifact source differs from reference"
                )
            if captured.artifact.schema_version != binding.schema_version:
                raise MarketBarAcceptanceError(
                    "captured artifact schema differs from parser binding"
                )
            if captured.parser_version != binding.parser_version:
                raise MarketBarAcceptanceError(
                    "captured parser version differs from parser binding"
                )
            capture_synthetic = captured.request_parameters.get("synthetic_fixture")
            if type(capture_synthetic) is not bool:
                raise MarketBarAcceptanceError(
                    "captured synthetic_fixture must be explicit boolean"
                )
            evidence = MarketBarSeriesEvidence(
                captured=captured,
                source_family=reference.source,
                adjustment=case.adjustment,
                comparable_fields=case.comparable_fields,
                license_status=MarketBarLicenseStatus.PENDING,
                synthetic_fixture=capture_synthetic,
            )
            if (
                evidence.market is not case.market
                or evidence.symbol != case.symbol
                or evidence.interval != case.interval
                or evidence.adjustment != case.adjustment
            ):
                raise MarketBarAcceptanceError(
                    "captured series identity disagrees with acceptance case"
                )
            series.append(evidence)
        try:
            reconciliation = reconcile_market_bars(
                as_of=case.as_of,
                calendar_snapshot_id=case.calendar_snapshot_id,
                expected_open_sessions=case.expected_open_sessions,
                series=series,
                policy=selected_policy,
            )
        except MarketBarReconciliationError as exc:
            raise MarketBarAcceptanceError(
                f"market-bar reconciliation failed for case {case.case_name}"
            ) from exc
        sources = tuple(item.source for item in reconciliation.series)
        case_declarations = tuple(
            declarations_by_id[item]
            for item in case.assurance_declaration_ids
        )
        coverage = _coverage_for_case(case, declarations_by_id, sources)
        case_reports.append(
            MarketBarAcceptanceCaseReport(
                case=case,
                reconciliation=reconciliation,
                assurance_declarations=case_declarations,
                assurance_coverage=coverage,
            )
        )
    return MarketBarAcceptanceReport(
        manifest=manifest,
        policy=selected_policy,
        cases=tuple(case_reports),
    )


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_output_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise MarketBarAcceptanceError(
                "acceptance output path cannot traverse a symlink or junction"
            )
    return absolute


def _atomic_write_text(path: Path, text: str) -> None:
    path = _checked_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise MarketBarAcceptanceError(
                "immutable acceptance path already contains different content"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_market_bar_acceptance_markdown(
    report: MarketBarAcceptanceReport,
) -> str:
    if not isinstance(report, MarketBarAcceptanceReport):
        raise MarketBarAcceptanceError("acceptance report is invalid")
    lines = [
        "# Stage 2H-2J Market-Bar Acceptance and T3 Preflight",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Manifest ID: `{report.manifest.manifest_id}`",
        f"- Acceptance state: `{report.acceptance_state.value}`",
        f"- T3 preflight: `{report.t3_preflight_state.value}`",
        f"- Cases: `{len(report.cases)}`",
        "- Trusted assurance authority configured: `false`",
        "- Research grade: `false`",
        "- Production database modified: `false`",
        "",
        "## Cases",
        "",
    ]
    for item in report.cases:
        lines.extend(
            [
                f"### {item.case.case_name}",
                "",
                f"- State: `{item.acceptance_state.value}`",
                f"- T3 preflight: `{item.t3_preflight_state.value}`",
                (
                    "- Non-synthetic declared: `"
                    f"{str(item.non_synthetic_declared).lower()}`"
                ),
                f"- Reconciliation report: `{item.reconciliation.report_id}`",
                f"- Missing assurance kinds: `{len(item.assurance_coverage.missing_kinds)}`",
                "",
            ]
        )
    lines.extend(["## Open blockers", ""])
    lines.extend(f"- `{item}`" for item in report.open_blockers)
    lines.extend(
        [
            "",
            "> Declarations are review inputs, not approval authority. This report never promotes data to T3, research grade, or investment evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def write_market_bar_acceptance_json(
    report: MarketBarAcceptanceReport,
    path: str | Path,
) -> None:
    if not isinstance(report, MarketBarAcceptanceReport):
        raise MarketBarAcceptanceError("acceptance report is invalid")
    _atomic_write_text(
        Path(path),
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )


def write_market_bar_acceptance_markdown(
    report: MarketBarAcceptanceReport,
    path: str | Path,
) -> None:
    _atomic_write_text(Path(path), render_market_bar_acceptance_markdown(report))


__all__ = [
    "CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED",
    "CORPORATE_ACTION_REFERENCE_MISSING",
    "DEFAULT_ACCEPTANCE_VERSION",
    "MANIFEST_SCHEMA",
    "NO_TRUSTED_ASSURANCE_AUTHORITY",
    "REPORT_SCHEMA",
    "SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED",
    "SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING",
    "MarketBarAcceptanceCase",
    "MarketBarAcceptanceCaseReport",
    "MarketBarAcceptanceError",
    "MarketBarAcceptanceManifest",
    "MarketBarAcceptanceReport",
    "MarketBarAcceptanceState",
    "MarketBarAssuranceCoverage",
    "MarketBarAssuranceDeclaration",
    "MarketBarAssuranceKind",
    "MarketBarAuxiliaryBindings",
    "MarketBarCaptureReference",
    "MarketBarT3PreflightState",
    "load_market_bar_assurance_declaration",
    "materialize_market_bar_acceptance",
    "render_market_bar_acceptance_markdown",
    "write_market_bar_acceptance_json",
    "write_market_bar_acceptance_markdown",
]
