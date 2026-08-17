"""Deterministic, offline A-share security identity and universe adapter.

The adapter accepts already-captured JSON bytes plus a checksum-bound descriptor.
It never fetches data and it can only emit unverified, incomplete candidate facts.
Provider-specific fields that the core snapshot contract cannot yet represent
(names, boards, publication granularity and intraday status intervals) stay in
the candidate envelopes instead of being discarded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from stock_tracker.core.types import Market
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.point_in_time import Revision, revision_key
from stock_tracker.quant.core.time import exchange_local_date, to_utc
from stock_tracker.quant.core.universe import (
    HistoricalUniverse,
    InstrumentIdentityFact,
    ListingState,
    RiskDesignation,
    SecurityStatusFact,
    SecurityType,
    TradingState,
    UniverseCoverage,
    UniverseMembershipFact,
    UniverseMembershipState,
)


ADAPTER_VERSION = "security-universe-adapter-v1"
SOURCE_SCHEMA = "a-share-security-universe-source-v1"
DESCRIPTOR_SCHEMA = "a-share-security-universe-descriptor-v1"
OUTPUT_SCHEMA = "a-share-security-universe-candidate-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[0-9]{6}\.(SH|SZ)$")
_UNIVERSE_BY_EXCHANGE = {
    "SSE": "A_SHARE_SSE_ALL",
    "SZSE": "A_SHARE_SZSE_ALL",
}
_SUFFIX_BY_EXCHANGE = {"SSE": "SH", "SZSE": "SZ"}
_TRUST_FIELDS = frozenset({"complete", "verified", "trust_tier"})


class SecurityUniverseAdapterError(ValueError):
    pass


class PublishedGranularity(StrEnum):
    DATE = "DATE"
    SECOND = "SECOND"
    UNKNOWN = "UNKNOWN"


class CoverageKind(StrEnum):
    CURRENT_ANCHOR = "CURRENT_ANCHOR"
    HISTORICAL_EVENTS = "HISTORICAL_EVENTS"


class StatusScope(StrEnum):
    DAILY = "DAILY"
    INTRADAY = "INTRADAY"


class MembershipReason(StrEnum):
    LISTED = "LISTED"
    RELISTED = "RELISTED"
    DELISTED = "DELISTED"
    TYPE_CHANGE = "TYPE_CHANGE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNKNOWN = "UNKNOWN"


class SourceListingState(StrEnum):
    PRE_LISTING = "PRE_LISTING"
    LISTED = "LISTED"
    DELISTING = "DELISTING"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


class SourceTradingState(StrEnum):
    TRADABLE = "TRADABLE"
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    RESUMED = "RESUMED"
    UNKNOWN = "UNKNOWN"


class SourceRiskDesignation(StrEnum):
    NORMAL = "NORMAL"
    ST = "ST"
    STAR_ST = "STAR_ST"
    RISK_WARNING = "RISK_WARNING"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


def _require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise SecurityUniverseAdapterError(f"{name} must be a JSON object")
    return cast(dict[str, Any], value)


def _require_fields(
    value: Mapping[str, object],
    name: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise SecurityUniverseAdapterError(
            f"{name} is missing fields: {', '.join(missing)}"
        )
    if unknown:
        raise SecurityUniverseAdapterError(
            f"{name} contains unknown fields: {', '.join(unknown)}"
        )


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SecurityUniverseAdapterError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise SecurityUniverseAdapterError(f"{name} must be a boolean")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SecurityUniverseAdapterError(f"{name} must be an integer")
    return value


def _require_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise SecurityUniverseAdapterError(f"{name} must be a JSON array")
    return cast(list[object], value)


def _require_texts(value: object, name: str) -> tuple[str, ...]:
    result = tuple(_require_text(item, f"{name} item") for item in _require_list(value, name))
    if len(result) != len(set(result)):
        raise SecurityUniverseAdapterError(f"{name} must not contain duplicates")
    return tuple(sorted(result))


def _parse_date(value: object, name: str) -> date:
    text = _require_text(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise SecurityUniverseAdapterError(f"{name} must be an ISO-8601 date") from exc
    if parsed.isoformat() != text:
        raise SecurityUniverseAdapterError(f"{name} must be a canonical ISO-8601 date")
    return parsed


def _parse_optional_date(value: object, name: str) -> date | None:
    return None if value is None else _parse_date(value, name)


def _parse_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text)
        return to_utc(parsed, name)
    except (TypeError, ValueError) as exc:
        raise SecurityUniverseAdapterError(
            f"{name} must be a timezone-aware ISO-8601 datetime"
        ) from exc


def _parse_revision(value: object, name: str) -> Revision:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SecurityUniverseAdapterError(
            f"{name} must be a non-empty string or an integer"
        )
    try:
        revision_key(value)
    except TypeError as exc:
        raise SecurityUniverseAdapterError(str(exc)) from exc
    return value


def _parse_enum(enum_type: type[StrEnum], value: object, name: str) -> StrEnum:
    text = _require_text(value, name)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise SecurityUniverseAdapterError(f"{name} has an unsupported value: {text}") from exc


def _strict_json(raw: bytes, name: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SecurityUniverseAdapterError(f"{name} must be strict UTF-8") from exc

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SecurityUniverseAdapterError(f"{name} contains duplicate key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise SecurityUniverseAdapterError(f"{name} contains non-finite number: {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=invalid_constant,
        )
    except json.JSONDecodeError as exc:
        raise SecurityUniverseAdapterError(f"{name} is not valid JSON") from exc
    return _require_object(value, name)


def _reject_trust_claims(value: object, path: str = "artifact") -> None:
    if isinstance(value, dict):
        claims = sorted(_TRUST_FIELDS.intersection(value))
        if claims:
            raise SecurityUniverseAdapterError(
                f"{path} cannot set adapter trust fields: {', '.join(claims)}"
            )
        for key, item in value.items():
            _reject_trust_claims(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_trust_claims(item, f"{path}[{index}]")


def _jsonable(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def stable_instrument_id(exchange: str, source_security_id: str) -> str:
    """Map one source-native permanent identity without consulting its symbol."""

    if exchange not in _UNIVERSE_BY_EXCHANGE:
        raise SecurityUniverseAdapterError("exchange must be SSE or SZSE")
    source_id = _require_text(source_security_id, "source_security_id")
    if any(ord(character) < 33 for character in source_id):
        raise SecurityUniverseAdapterError("source_security_id contains control characters")
    return f"CN:{exchange}:{source_id}"


def _require_symbol(value: object, exchange: str) -> str:
    symbol = _require_text(value, "symbol")
    match = _SYMBOL.fullmatch(symbol)
    if match is None or match.group(1) != _SUFFIX_BY_EXCHANGE[exchange]:
        raise SecurityUniverseAdapterError("symbol must be canonical and match exchange")
    return symbol


@dataclass(frozen=True, slots=True)
class SecurityUniverseArtifactDescriptor:
    source: str
    source_dataset: str
    source_version: str
    exchange: str
    schema_version: str
    parser_version: str
    retrieved_at: datetime
    artifact_sha256: str
    byte_size: int
    synthetic: bool

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "SecurityUniverseArtifactDescriptor":
        _require_fields(
            value,
            "descriptor",
            required={
                "schema",
                "source",
                "source_dataset",
                "source_version",
                "exchange",
                "schema_version",
                "parser_version",
                "retrieved_at",
                "artifact_sha256",
                "byte_size",
                "synthetic",
            },
        )
        if value["schema"] != DESCRIPTOR_SCHEMA:
            raise SecurityUniverseAdapterError("unsupported descriptor schema")
        exchange = _require_text(value["exchange"], "exchange")
        if exchange not in _UNIVERSE_BY_EXCHANGE:
            raise SecurityUniverseAdapterError("descriptor exchange must be SSE or SZSE")
        schema_version = _require_text(value["schema_version"], "schema_version")
        parser_version = _require_text(value["parser_version"], "parser_version")
        if schema_version != SOURCE_SCHEMA:
            raise SecurityUniverseAdapterError("descriptor source schema version mismatch")
        if parser_version != ADAPTER_VERSION:
            raise SecurityUniverseAdapterError("descriptor parser version mismatch")
        artifact_sha256 = _require_text(value["artifact_sha256"], "artifact_sha256")
        if _SHA256.fullmatch(artifact_sha256) is None:
            raise SecurityUniverseAdapterError("artifact_sha256 must be lowercase SHA-256")
        byte_size = _require_int(value["byte_size"], "byte_size")
        if byte_size < 0:
            raise SecurityUniverseAdapterError("byte_size cannot be negative")
        return cls(
            source=_require_text(value["source"], "source"),
            source_dataset=_require_text(value["source_dataset"], "source_dataset"),
            source_version=_require_text(value["source_version"], "source_version"),
            exchange=exchange,
            schema_version=schema_version,
            parser_version=parser_version,
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            artifact_sha256=artifact_sha256,
            byte_size=byte_size,
            synthetic=_require_bool(value["synthetic"], "synthetic"),
        )

    @property
    def universe_id(self) -> str:
        return _UNIVERSE_BY_EXCHANGE[self.exchange]

    @property
    def artifact_id(self) -> str:
        return self.artifact_sha256

    def verify_bytes(self, raw: bytes) -> None:
        if len(raw) != self.byte_size:
            raise SecurityUniverseAdapterError("artifact byte_size does not match descriptor")
        if hashlib.sha256(raw).hexdigest() != self.artifact_sha256:
            raise SecurityUniverseAdapterError("artifact SHA-256 does not match descriptor")

    def as_dict(self) -> dict[str, object]:
        result = cast(dict[str, object], _jsonable(self))
        result["schema"] = DESCRIPTOR_SCHEMA
        return result


@dataclass(frozen=True, slots=True)
class CandidateProvenance:
    source_published_at: str | None
    source_published_granularity: PublishedGranularity
    observed_at: datetime
    retrieved_at: datetime
    known_at: datetime
    usable_from: datetime
    revision: Revision
    supersedes: Revision | None
    source_uri: str
    evidence_ids: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        retrieved_at: datetime,
        name: str,
        allow_synthetic_backdating: bool = False,
    ) -> "CandidateProvenance":
        _require_fields(
            value,
            name,
            required={
                "source_published_at",
                "source_published_granularity",
                "observed_at",
                "known_at",
                "usable_from",
                "revision",
                "supersedes",
                "source_uri",
                "evidence_ids",
            },
        )
        granularity = cast(
            PublishedGranularity,
            _parse_enum(
                PublishedGranularity,
                value["source_published_granularity"],
                f"{name}.source_published_granularity",
            ),
        )
        published_value = value["source_published_at"]
        if granularity is PublishedGranularity.UNKNOWN:
            if published_value is not None:
                raise SecurityUniverseAdapterError(
                    f"{name}.source_published_at must be null when granularity is UNKNOWN"
                )
            published: str | None = None
        elif granularity is PublishedGranularity.DATE:
            published = _parse_date(
                published_value,
                f"{name}.source_published_at",
            ).isoformat()
        else:
            published_datetime = _parse_datetime(
                published_value,
                f"{name}.source_published_at",
            )
            published = published_datetime.isoformat()
        observed_at = _parse_datetime(value["observed_at"], f"{name}.observed_at")
        known_at = _parse_datetime(value["known_at"], f"{name}.known_at")
        usable_from = _parse_datetime(value["usable_from"], f"{name}.usable_from")
        if usable_from < known_at:
            raise SecurityUniverseAdapterError(f"{name}.usable_from cannot precede known_at")
        if observed_at > retrieved_at:
            raise SecurityUniverseAdapterError(f"{name}.observed_at cannot follow retrieved_at")
        if known_at != observed_at:
            raise SecurityUniverseAdapterError(
                f"{name}.known_at must equal observed_at without independently bound earlier-time evidence"
            )
        if not allow_synthetic_backdating and observed_at != retrieved_at:
            raise SecurityUniverseAdapterError(
                f"{name}.observed_at must equal descriptor retrieved_at for non-synthetic candidate data"
            )
        if granularity is PublishedGranularity.DATE:
            assert published is not None
            published_date = date.fromisoformat(published)
            if published_date > exchange_local_date(known_at, Market.A):
                raise SecurityUniverseAdapterError(
                    f"{name}.source_published_at date cannot follow known_at"
                )
        elif granularity is PublishedGranularity.SECOND:
            assert published is not None
            if datetime.fromisoformat(published) > known_at:
                raise SecurityUniverseAdapterError(
                    f"{name}.source_published_at cannot follow known_at"
                )
        revision = _parse_revision(value["revision"], f"{name}.revision")
        supersedes_value = value["supersedes"]
        supersedes = (
            None
            if supersedes_value is None
            else _parse_revision(supersedes_value, f"{name}.supersedes")
        )
        if supersedes == revision:
            raise SecurityUniverseAdapterError(f"{name}.supersedes cannot equal revision")
        return cls(
            source_published_at=published,
            source_published_granularity=granularity,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            known_at=known_at,
            usable_from=usable_from,
            revision=revision,
            supersedes=supersedes,
            source_uri=_require_text(value["source_uri"], f"{name}.source_uri"),
            evidence_ids=_require_texts(value["evidence_ids"], f"{name}.evidence_ids"),
        )

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _jsonable(self))


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    source_security_id: str
    instrument_id: str
    exchange: str
    symbol: str
    name: str
    security_type: SecurityType
    board: str | None
    effective_from: date
    effective_to: date | None
    provenance: CandidateProvenance
    fact: InstrumentIdentityFact

    @property
    def candidate_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def active_on(self, value: date) -> bool:
        return self.effective_from <= value and (
            self.effective_to is None or value <= self.effective_to
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result = {
            "source_security_id": self.source_security_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "name": self.name,
            "market": Market.A.value,
            "security_type": self.security_type.value,
            "board": self.board,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            **self.provenance.as_dict(),
            "verified": False,
            "core_fact_id": self.fact.fact_id,
        }
        if include_id:
            result["candidate_id"] = self.candidate_id
        return result


@dataclass(frozen=True, slots=True)
class StatusCandidate:
    source_security_id: str
    instrument_id: str
    exchange: str
    symbol: str
    session_date: date
    scope: StatusScope
    effective_start: date | datetime
    effective_end: date | datetime | None
    listing_state: SourceListingState
    trading_state: SourceTradingState
    risk_designation: SourceRiskDesignation
    reason_code: str | None
    provenance: CandidateProvenance
    fact: SecurityStatusFact | None

    @property
    def candidate_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result = {
            "source_security_id": self.source_security_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "market": Market.A.value,
            "session_date": self.session_date.isoformat(),
            "scope": self.scope.value,
            "effective_start": self.effective_start.isoformat(),
            "effective_end": (
                self.effective_end.isoformat() if self.effective_end is not None else None
            ),
            "listing_state": self.listing_state.value,
            "trading_state": self.trading_state.value,
            "risk_designation": self.risk_designation.value,
            "reason_code": self.reason_code,
            **self.provenance.as_dict(),
            "verified": False,
            "core_compatible": self.fact is not None,
            "core_fact_id": self.fact.fact_id if self.fact is not None else None,
        }
        if include_id:
            result["candidate_id"] = self.candidate_id
        return result


@dataclass(frozen=True, slots=True)
class MembershipCandidate:
    source_security_id: str
    instrument_id: str
    exchange: str
    universe_id: str
    symbol: str
    effective_date: date
    state: UniverseMembershipState
    reason: MembershipReason
    evidence_ids: tuple[str, ...]
    provenance: CandidateProvenance
    fact: UniverseMembershipFact

    @property
    def candidate_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result = {
            "source_security_id": self.source_security_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "universe_id": self.universe_id,
            "symbol": self.symbol,
            "market": Market.A.value,
            "effective_date": self.effective_date.isoformat(),
            "state": self.state.value,
            "reason": self.reason.value,
            "evidence_ids": list(self.evidence_ids),
            **self.provenance.as_dict(),
            "verified": False,
            "core_fact_id": self.fact.fact_id,
        }
        if include_id:
            result["candidate_id"] = self.candidate_id
        return result


@dataclass(frozen=True, slots=True)
class UniverseCoverageReport:
    current_anchor_only: bool
    unclosed_delistings: tuple[str, ...]
    missing_listing_event: tuple[str, ...]
    missing_identity: tuple[str, ...]
    missing_daily_session_status: tuple[str, ...]
    missing_exclusion_reason: tuple[str, ...]
    quantity_continuity_gaps: tuple[str, ...]
    unparsed_attachments: tuple[str, ...]
    cross_source_conflicts: tuple[str, ...]

    @property
    def has_snapshot_blockers(self) -> bool:
        return bool(
            self.missing_identity
            or self.missing_daily_session_status
            or self.cross_source_conflicts
        )

    @property
    def trust_blocker_codes(self) -> tuple[str, ...]:
        codes = {
            "ADAPTER_UNVERIFIED_INCOMPLETE",
            "SOURCE_SECURITY_ID_STABILITY_UNPROVEN",
            "UPSTREAM_RAW_PROVENANCE_INCOMPLETE",
        }
        if self.current_anchor_only:
            codes.add("CURRENT_ANCHOR_ONLY")
        if self.unclosed_delistings:
            codes.add("UNCLOSED_DELISTINGS")
        if self.missing_listing_event:
            codes.add("MISSING_LISTING_EVENT")
        if self.missing_identity:
            codes.add("MISSING_IDENTITY")
        if self.missing_daily_session_status:
            codes.add("MISSING_DAILY_SESSION_STATUS")
        if self.missing_exclusion_reason:
            codes.add("MISSING_EXCLUSION_REASON")
        if self.quantity_continuity_gaps:
            codes.add("QUANTITY_CONTINUITY_GAPS")
        if self.unparsed_attachments:
            codes.add("UNPARSED_ATTACHMENTS")
        if self.cross_source_conflicts:
            codes.add("CROSS_SOURCE_CONFLICTS")
        return tuple(sorted(codes))

    @property
    def has_trust_blockers(self) -> bool:
        return bool(self.trust_blocker_codes)

    @property
    def report_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result = cast(dict[str, object], _jsonable(self))
        result["complete"] = False
        result["verified"] = False
        result["trust_ceiling"] = "T2_CANDIDATE_EVIDENCE"
        result["trust_state"] = "T3_NOT_REACHED"
        result["has_snapshot_blockers"] = self.has_snapshot_blockers
        result["has_trust_blockers"] = self.has_trust_blockers
        result["trust_blocker_codes"] = list(self.trust_blocker_codes)
        if include_id:
            result["coverage_report_id"] = self.report_id
        return result


@dataclass(frozen=True, slots=True)
class SecurityUniverseCandidateBundle:
    descriptor: SecurityUniverseArtifactDescriptor
    coverage_kind: CoverageKind
    required_session_dates: tuple[date, ...]
    coverage: UniverseCoverage
    identities: tuple[IdentityCandidate, ...]
    statuses: tuple[StatusCandidate, ...]
    memberships: tuple[MembershipCandidate, ...]
    coverage_report: UniverseCoverageReport

    @property
    def core_identities(self) -> tuple[InstrumentIdentityFact, ...]:
        return tuple(item.fact for item in self.identities)

    @property
    def core_statuses(self) -> tuple[SecurityStatusFact, ...]:
        return tuple(item.fact for item in self.statuses if item.fact is not None)

    @property
    def core_memberships(self) -> tuple[UniverseMembershipFact, ...]:
        return tuple(item.fact for item in self.memberships)

    def historical_universe(self) -> HistoricalUniverse:
        return HistoricalUniverse(
            (self.coverage,),
            self.core_identities,
            self.core_statuses,
            self.core_memberships,
        )

    @property
    def bundle_id(self) -> str:
        return fingerprint(self.as_dict(include_id=False))

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": OUTPUT_SCHEMA,
            "artifact": self.descriptor.as_dict(),
            "candidate_state": (
                "SYNTHETIC_FIXTURE" if self.descriptor.synthetic else "REAL_SOURCE_CANDIDATE"
            ),
            "trust_ceiling": "T2_CANDIDATE_EVIDENCE",
            "trust_state": "T3_NOT_REACHED",
            "coverage_kind": self.coverage_kind.value,
            "required_session_dates": [item.isoformat() for item in self.required_session_dates],
            "coverage": _jsonable(self.coverage),
            "instrument_identities": [item.as_dict() for item in self.identities],
            "security_statuses": [item.as_dict() for item in self.statuses],
            "universe_memberships": [item.as_dict() for item in self.memberships],
            "coverage_report": self.coverage_report.as_dict(),
        }
        if include_id:
            result["bundle_id"] = self.bundle_id
        return result

    def write_outputs(self, output_dir: str | Path) -> tuple[Path, ...]:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        payloads = {
            "candidate_bundle.json": json.dumps(
                self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            "instrument_identities.jsonl": _jsonl(
                item.as_dict() for item in self.identities
            ),
            "security_statuses.jsonl": _jsonl(item.as_dict() for item in self.statuses),
            "universe_memberships.jsonl": _jsonl(
                item.as_dict() for item in self.memberships
            ),
            "coverage.json": json.dumps(
                _jsonable(self.coverage), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            "coverage_report.json": json.dumps(
                self.coverage_report.as_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        }
        written: list[Path] = []
        for name, content in payloads.items():
            path = destination / name
            _atomic_write(path, content)
            written.append(path)
        return tuple(written)


def _jsonl(records: Iterable[Mapping[str, object]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for record in records
    )


def _atomic_write(path: Path, content: str) -> None:
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


def read_security_universe_descriptor(
    path: str | Path,
) -> SecurityUniverseArtifactDescriptor:
    raw = Path(path).read_bytes()
    return SecurityUniverseArtifactDescriptor.from_mapping(
        _strict_json(raw, "descriptor")
    )


def _parse_provenance(
    value: object,
    descriptor: SecurityUniverseArtifactDescriptor,
    name: str,
) -> CandidateProvenance:
    return CandidateProvenance.from_mapping(
        _require_object(value, name),
        retrieved_at=descriptor.retrieved_at,
        name=name,
        allow_synthetic_backdating=descriptor.synthetic,
    )


def _core_listing(value: SourceListingState) -> ListingState | None:
    if value is SourceListingState.UNKNOWN:
        return None
    return ListingState(value.value)


def _core_trading(value: SourceTradingState) -> TradingState:
    if value is SourceTradingState.RESUMED:
        return TradingState.TRADABLE
    return TradingState(value.value)


def _core_risk(value: SourceRiskDesignation) -> RiskDesignation:
    return RiskDesignation(value.value)


def _parse_identity(
    value: object,
    descriptor: SecurityUniverseArtifactDescriptor,
    index: int,
) -> IdentityCandidate:
    name = f"identities[{index}]"
    row = _require_object(value, name)
    _require_fields(
        row,
        name,
        required={
            "source_security_id",
            "symbol",
            "name",
            "security_type",
            "board",
            "effective_from",
            "effective_to",
            "provenance",
        },
    )
    source_security_id = _require_text(
        row["source_security_id"], f"{name}.source_security_id"
    )
    instrument_id = stable_instrument_id(descriptor.exchange, source_security_id)
    symbol = _require_symbol(row["symbol"], descriptor.exchange)
    security_type = cast(
        SecurityType,
        _parse_enum(SecurityType, row["security_type"], f"{name}.security_type"),
    )
    effective_from = _parse_date(row["effective_from"], f"{name}.effective_from")
    effective_to = _parse_optional_date(row["effective_to"], f"{name}.effective_to")
    if effective_to is not None and effective_to < effective_from:
        raise SecurityUniverseAdapterError(f"{name}.effective_to cannot precede start")
    provenance = _parse_provenance(row["provenance"], descriptor, f"{name}.provenance")
    fact = InstrumentIdentityFact(
        instrument_id=instrument_id,
        symbol=symbol,
        market=Market.A,
        exchange=descriptor.exchange,
        security_type=security_type,
        effective_from=effective_from,
        effective_to=effective_to,
        known_at=provenance.known_at,
        usable_from=provenance.usable_from,
        source=descriptor.source,
        revision=provenance.revision,
        verified=False,
        source_note="candidate only; identity provenance retained by adapter envelope",
    )
    return IdentityCandidate(
        source_security_id=source_security_id,
        instrument_id=instrument_id,
        exchange=descriptor.exchange,
        symbol=symbol,
        name=_require_text(row["name"], f"{name}.name"),
        security_type=security_type,
        board=_optional_text(row["board"], f"{name}.board"),
        effective_from=effective_from,
        effective_to=effective_to,
        provenance=provenance,
        fact=fact,
    )


def _parse_effective_status_time(
    value: object,
    *,
    scope: StatusScope,
    name: str,
) -> date | datetime:
    return _parse_date(value, name) if scope is StatusScope.DAILY else _parse_datetime(value, name)


def _parse_status(
    value: object,
    descriptor: SecurityUniverseArtifactDescriptor,
    index: int,
) -> StatusCandidate:
    name = f"statuses[{index}]"
    row = _require_object(value, name)
    _require_fields(
        row,
        name,
        required={
            "source_security_id",
            "symbol",
            "session_date",
            "scope",
            "effective_start",
            "effective_end",
            "listing_state",
            "trading_state",
            "risk_designation",
            "reason_code",
            "provenance",
        },
    )
    source_security_id = _require_text(
        row["source_security_id"], f"{name}.source_security_id"
    )
    instrument_id = stable_instrument_id(descriptor.exchange, source_security_id)
    symbol = _require_symbol(row["symbol"], descriptor.exchange)
    session_date = _parse_date(row["session_date"], f"{name}.session_date")
    scope = cast(
        StatusScope,
        _parse_enum(StatusScope, row["scope"], f"{name}.scope"),
    )
    effective_start = _parse_effective_status_time(
        row["effective_start"], scope=scope, name=f"{name}.effective_start"
    )
    effective_end_value = row["effective_end"]
    effective_end = (
        None
        if effective_end_value is None
        else _parse_effective_status_time(
            effective_end_value,
            scope=scope,
            name=f"{name}.effective_end",
        )
    )
    if effective_end is not None and effective_end < effective_start:
        raise SecurityUniverseAdapterError(f"{name}.effective_end cannot precede start")
    if scope is StatusScope.DAILY:
        if effective_start != session_date or (
            effective_end is not None and effective_end != session_date
        ):
            raise SecurityUniverseAdapterError(
                f"{name} DAILY effective range must equal session_date"
            )
    else:
        assert isinstance(effective_start, datetime)
        if exchange_local_date(effective_start, Market.A) != session_date:
            raise SecurityUniverseAdapterError(
                f"{name} INTRADAY effective_start must match session_date"
            )
        if isinstance(effective_end, datetime) and exchange_local_date(
            effective_end, Market.A
        ) != session_date:
            raise SecurityUniverseAdapterError(
                f"{name} INTRADAY effective_end must match session_date"
            )
    listing_state = cast(
        SourceListingState,
        _parse_enum(
            SourceListingState,
            row["listing_state"],
            f"{name}.listing_state",
        ),
    )
    trading_state = cast(
        SourceTradingState,
        _parse_enum(
            SourceTradingState,
            row["trading_state"],
            f"{name}.trading_state",
        ),
    )
    risk_designation = cast(
        SourceRiskDesignation,
        _parse_enum(
            SourceRiskDesignation,
            row["risk_designation"],
            f"{name}.risk_designation",
        ),
    )
    provenance = _parse_provenance(row["provenance"], descriptor, f"{name}.provenance")
    core_listing = _core_listing(listing_state)
    fact = None
    if scope is StatusScope.DAILY and core_listing is not None:
        fact = SecurityStatusFact(
            instrument_id=instrument_id,
            symbol=symbol,
            market=Market.A,
            session_date=session_date,
            listing_state=core_listing,
            trading_state=_core_trading(trading_state),
            risk_designation=_core_risk(risk_designation),
            known_at=provenance.known_at,
            usable_from=provenance.usable_from,
            source=descriptor.source,
            revision=provenance.revision,
            verified=False,
            source_note="candidate only; exact status interval retained by adapter envelope",
        )
    return StatusCandidate(
        source_security_id=source_security_id,
        instrument_id=instrument_id,
        exchange=descriptor.exchange,
        symbol=symbol,
        session_date=session_date,
        scope=scope,
        effective_start=effective_start,
        effective_end=effective_end,
        listing_state=listing_state,
        trading_state=trading_state,
        risk_designation=risk_designation,
        reason_code=_optional_text(row["reason_code"], f"{name}.reason_code"),
        provenance=provenance,
        fact=fact,
    )


def _parse_membership(
    value: object,
    descriptor: SecurityUniverseArtifactDescriptor,
    index: int,
) -> MembershipCandidate:
    name = f"memberships[{index}]"
    row = _require_object(value, name)
    _require_fields(
        row,
        name,
        required={
            "source_security_id",
            "symbol",
            "effective_date",
            "state",
            "reason",
            "evidence_ids",
            "provenance",
        },
    )
    source_security_id = _require_text(
        row["source_security_id"], f"{name}.source_security_id"
    )
    instrument_id = stable_instrument_id(descriptor.exchange, source_security_id)
    symbol = _require_symbol(row["symbol"], descriptor.exchange)
    state = cast(
        UniverseMembershipState,
        _parse_enum(
            UniverseMembershipState,
            row["state"],
            f"{name}.state",
        ),
    )
    reason = cast(
        MembershipReason,
        _parse_enum(MembershipReason, row["reason"], f"{name}.reason"),
    )
    evidence_ids = _require_texts(row["evidence_ids"], f"{name}.evidence_ids")
    provenance = _parse_provenance(row["provenance"], descriptor, f"{name}.provenance")
    effective_date = _parse_date(row["effective_date"], f"{name}.effective_date")
    fact = UniverseMembershipFact(
        universe_id=descriptor.universe_id,
        instrument_id=instrument_id,
        symbol=symbol,
        market=Market.A,
        effective_date=effective_date,
        state=state,
        known_at=provenance.known_at,
        usable_from=provenance.usable_from,
        source=descriptor.source,
        universe_version=descriptor.source_version,
        revision=provenance.revision,
        verified=False,
        reason=reason.value,
        source_note="candidate only; explicit membership event",
    )
    return MembershipCandidate(
        source_security_id=source_security_id,
        instrument_id=instrument_id,
        exchange=descriptor.exchange,
        universe_id=descriptor.universe_id,
        symbol=symbol,
        effective_date=effective_date,
        state=state,
        reason=reason,
        evidence_ids=evidence_ids,
        provenance=provenance,
        fact=fact,
    )


def _revision_identity(value: Revision) -> tuple[int, int | str]:
    return revision_key(value)


def _validate_rows(
    records: Iterable[object],
    *,
    kind: str,
    logical_key: Any,
) -> None:
    seen: set[str] = set()
    groups: dict[tuple[object, ...], set[str]] = defaultdict(set)
    for record in records:
        record_dict = cast(Any, record).as_dict(include_id=False)
        candidate_id = fingerprint(record_dict)
        if candidate_id in seen:
            raise SecurityUniverseAdapterError(f"duplicate {kind} row")
        seen.add(candidate_id)
        provenance = cast(Any, record).provenance
        group_key = (
            *logical_key(record),
            provenance.known_at,
            _revision_identity(provenance.revision),
        )
        groups[group_key].add(candidate_id)
    if any(len(payloads) > 1 for payloads in groups.values()):
        raise SecurityUniverseAdapterError(
            f"{kind} rows conflict at the same known_at and revision"
        )


def _current_identities(
    identities: Iterable[IdentityCandidate],
) -> tuple[IdentityCandidate, ...]:
    groups: dict[tuple[str, date], list[IdentityCandidate]] = defaultdict(list)
    for identity in identities:
        groups[(identity.instrument_id, identity.effective_from)].append(identity)
    selected: list[IdentityCandidate] = []
    for records in groups.values():
        records.sort(
            key=lambda item: (
                item.provenance.known_at,
                _revision_identity(item.provenance.revision),
                item.candidate_id,
            )
        )
        selected.append(records[-1])
    return tuple(sorted(selected, key=lambda item: (item.instrument_id, item.effective_from)))


def _ranges_overlap(
    left_start: date,
    left_end: date | None,
    right_start: date,
    right_end: date | None,
) -> bool:
    return (left_end is None or right_start <= left_end) and (
        right_end is None or left_start <= right_end
    )


def _validate_identity_intervals(identities: Iterable[IdentityCandidate]) -> None:
    current = _current_identities(identities)
    by_instrument: dict[str, list[IdentityCandidate]] = defaultdict(list)
    by_symbol: dict[str, list[IdentityCandidate]] = defaultdict(list)
    for identity in current:
        by_instrument[identity.instrument_id].append(identity)
        by_symbol[identity.symbol].append(identity)
    for records in by_instrument.values():
        records.sort(key=lambda item: item.effective_from)
        for left, right in zip(records, records[1:]):
            if _ranges_overlap(
                left.effective_from,
                left.effective_to,
                right.effective_from,
                right.effective_to,
            ):
                raise SecurityUniverseAdapterError(
                    "one instrument has overlapping identity effective ranges"
                )
    for records in by_symbol.values():
        records.sort(key=lambda item: item.effective_from)
        for left, right in zip(records, records[1:]):
            if left.instrument_id != right.instrument_id and _ranges_overlap(
                left.effective_from,
                left.effective_to,
                right.effective_from,
                right.effective_to,
            ):
                raise SecurityUniverseAdapterError(
                    "one symbol overlaps multiple stable instrument identities"
                )


def _parse_evidence(
    value: object,
) -> tuple[dict[str, tuple[tuple[str, ...], ...]], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    evidence = _require_object(value, "coverage_evidence")
    _require_fields(
        evidence,
        "coverage_evidence",
        required={
            "delisting_closures",
            "quantity_continuity",
            "unparsed_attachments",
            "cross_source_conflicts",
        },
    )
    closures: dict[str, tuple[tuple[str, ...], ...]] = {}
    for index, item in enumerate(_require_list(evidence["delisting_closures"], "delisting_closures")):
        name = f"delisting_closures[{index}]"
        row = _require_object(item, name)
        _require_fields(
            row,
            name,
            required={
                "source_security_id",
                "listing_announcement_ids",
                "delisting_announcement_ids",
                "exchange_delisted_list_ids",
                "chinaclear_termination_ids",
            },
        )
        source_id = _require_text(row["source_security_id"], f"{name}.source_security_id")
        if source_id in closures:
            raise SecurityUniverseAdapterError("duplicate delisting closure identity")
        closures[source_id] = (
            _require_texts(row["listing_announcement_ids"], f"{name}.listing_announcement_ids"),
            _require_texts(row["delisting_announcement_ids"], f"{name}.delisting_announcement_ids"),
            _require_texts(row["exchange_delisted_list_ids"], f"{name}.exchange_delisted_list_ids"),
            _require_texts(row["chinaclear_termination_ids"], f"{name}.chinaclear_termination_ids"),
        )
    continuity_gaps: list[str] = []
    seen_periods: set[str] = set()
    for index, item in enumerate(_require_list(evidence["quantity_continuity"], "quantity_continuity")):
        name = f"quantity_continuity[{index}]"
        row = _require_object(item, name)
        _require_fields(
            row,
            name,
            required={
                "period",
                "begin_count",
                "listings",
                "relistings",
                "delistings",
                "scope_changes",
                "end_count",
            },
        )
        period = _require_text(row["period"], f"{name}.period")
        if period in seen_periods:
            raise SecurityUniverseAdapterError("duplicate quantity continuity period")
        seen_periods.add(period)
        begin = _require_int(row["begin_count"], f"{name}.begin_count")
        listings = _require_int(row["listings"], f"{name}.listings")
        relistings = _require_int(row["relistings"], f"{name}.relistings")
        delistings = _require_int(row["delistings"], f"{name}.delistings")
        scope_changes = _require_int(row["scope_changes"], f"{name}.scope_changes")
        end = _require_int(row["end_count"], f"{name}.end_count")
        if any(item < 0 for item in (begin, listings, relistings, delistings, end)):
            raise SecurityUniverseAdapterError("quantity counts cannot be negative")
        expected = begin + listings + relistings - delistings + scope_changes
        if expected != end:
            continuity_gaps.append(f"{period}:expected={expected}:reported={end}")
    return (
        closures,
        tuple(sorted(continuity_gaps)),
        _require_texts(evidence["unparsed_attachments"], "unparsed_attachments"),
        _require_texts(evidence["cross_source_conflicts"], "cross_source_conflicts"),
    )


def _latest_memberships_on(
    memberships: Iterable[MembershipCandidate],
    session_date: date,
) -> dict[str, MembershipCandidate]:
    eligible = [item for item in memberships if item.effective_date <= session_date]
    latest: dict[str, MembershipCandidate] = {}
    for item in eligible:
        current = latest.get(item.instrument_id)
        if current is None or item.effective_date > current.effective_date:
            latest[item.instrument_id] = item
    return latest


def _build_report(
    *,
    exchange: str,
    coverage_kind: CoverageKind,
    required_session_dates: tuple[date, ...],
    identities: tuple[IdentityCandidate, ...],
    statuses: tuple[StatusCandidate, ...],
    memberships: tuple[MembershipCandidate, ...],
    closures: Mapping[str, tuple[tuple[str, ...], ...]],
    continuity_gaps: tuple[str, ...],
    unparsed_attachments: tuple[str, ...],
    declared_conflicts: tuple[str, ...],
) -> UniverseCoverageReport:
    missing_identity: set[str] = set()
    missing_status: set[str] = set()
    conflicts = set(declared_conflicts)
    current_identities = _current_identities(identities)
    identity_by_id: dict[str, list[IdentityCandidate]] = defaultdict(list)
    for identity in current_identities:
        identity_by_id[identity.instrument_id].append(identity)

    for membership in memberships:
        active = [
            item
            for item in identity_by_id.get(membership.instrument_id, ())
            if item.active_on(membership.effective_date)
        ]
        if not active:
            missing_identity.add(
                f"{membership.instrument_id}@{membership.effective_date.isoformat()}"
            )
        elif all(item.symbol != membership.symbol for item in active):
            conflicts.add(
                f"membership-symbol:{membership.instrument_id}@{membership.effective_date.isoformat()}"
            )

    daily_statuses = [
        item for item in statuses if item.scope is StatusScope.DAILY and item.fact is not None
    ]
    for session_date in required_session_dates:
        for instrument_id, membership in _latest_memberships_on(
            memberships, session_date
        ).items():
            identity_anchor = (
                session_date
                if membership.state is UniverseMembershipState.INCLUDED
                else membership.effective_date
            )
            active = [
                item
                for item in identity_by_id.get(instrument_id, ())
                if item.active_on(identity_anchor)
            ]
            if not active:
                missing_identity.add(f"{instrument_id}@{identity_anchor.isoformat()}")
                continue
            if membership.state is UniverseMembershipState.INCLUDED:
                matching = [
                    item
                    for item in daily_statuses
                    if item.instrument_id == instrument_id
                    and item.session_date == session_date
                    and item.symbol == active[-1].symbol
                ]
                if not matching:
                    missing_status.add(f"{instrument_id}@{session_date.isoformat()}")
                if membership.symbol != active[-1].symbol:
                    conflicts.add(
                        f"snapshot-symbol:{instrument_id}@{session_date.isoformat()}"
                    )
            else:
                exit_statuses = [
                    item
                    for item in daily_statuses
                    if item.instrument_id == instrument_id
                    and item.session_date <= membership.effective_date
                ]
                if not exit_statuses:
                    missing_status.add(
                        f"{instrument_id}@exit:{membership.effective_date.isoformat()}"
                    )

    included_reasons: dict[str, set[MembershipReason]] = defaultdict(set)
    for membership in memberships:
        if membership.state is UniverseMembershipState.INCLUDED:
            included_reasons[membership.instrument_id].add(membership.reason)
    missing_listing = tuple(
        sorted(
            instrument_id
            for instrument_id in identity_by_id
            if not included_reasons[instrument_id].intersection(
                {MembershipReason.LISTED, MembershipReason.RELISTED}
            )
        )
    )
    missing_exclusion_reason = tuple(
        sorted(
            f"{item.instrument_id}@{item.effective_date.isoformat()}"
            for item in memberships
            if item.state is UniverseMembershipState.EXCLUDED
            and item.reason is MembershipReason.UNKNOWN
        )
    )
    delisted_source_ids = {
        item.source_security_id
        for item in memberships
        if item.state is UniverseMembershipState.EXCLUDED
        and item.reason is MembershipReason.DELISTED
    }
    delisted_source_ids.update(
        item.source_security_id
        for item in statuses
        if item.listing_state is SourceListingState.DELISTED
    )
    unclosed = tuple(
        sorted(
            stable_instrument_id(exchange, source_id)
            for source_id in delisted_source_ids
            if source_id not in closures or any(not evidence for evidence in closures[source_id])
        )
    )
    return UniverseCoverageReport(
        current_anchor_only=coverage_kind is CoverageKind.CURRENT_ANCHOR,
        unclosed_delistings=unclosed,
        missing_listing_event=missing_listing,
        missing_identity=tuple(sorted(missing_identity)),
        missing_daily_session_status=tuple(sorted(missing_status)),
        missing_exclusion_reason=missing_exclusion_reason,
        quantity_continuity_gaps=continuity_gaps,
        unparsed_attachments=unparsed_attachments,
        cross_source_conflicts=tuple(sorted(conflicts)),
    )


def parse_security_universe_artifact(
    raw: bytes,
    descriptor: SecurityUniverseArtifactDescriptor | Mapping[str, object],
) -> SecurityUniverseCandidateBundle:
    if not isinstance(descriptor, SecurityUniverseArtifactDescriptor):
        descriptor = SecurityUniverseArtifactDescriptor.from_mapping(descriptor)
    descriptor.verify_bytes(raw)
    document = _strict_json(raw, "artifact")
    _reject_trust_claims(document)
    _require_fields(
        document,
        "artifact",
        required={
            "schema",
            "source",
            "source_dataset",
            "source_version",
            "exchange",
            "universe_id",
            "coverage",
            "identities",
            "statuses",
            "memberships",
            "coverage_evidence",
        },
    )
    expected = {
        "schema": descriptor.schema_version,
        "source": descriptor.source,
        "source_dataset": descriptor.source_dataset,
        "source_version": descriptor.source_version,
        "exchange": descriptor.exchange,
        "universe_id": descriptor.universe_id,
    }
    mismatches = [key for key, value in expected.items() if document[key] != value]
    if mismatches:
        raise SecurityUniverseAdapterError(
            "artifact/descriptor source or version mismatch: " + ", ".join(mismatches)
        )

    coverage_value = _require_object(document["coverage"], "coverage")
    _require_fields(
        coverage_value,
        "coverage",
        required={
            "start_date",
            "end_date",
            "coverage_kind",
            "required_session_dates",
            "provenance",
        },
    )
    start_date = _parse_date(coverage_value["start_date"], "coverage.start_date")
    end_date = _parse_date(coverage_value["end_date"], "coverage.end_date")
    if end_date < start_date:
        raise SecurityUniverseAdapterError("coverage end_date cannot precede start_date")
    coverage_kind = cast(
        CoverageKind,
        _parse_enum(
            CoverageKind,
            coverage_value["coverage_kind"],
            "coverage.coverage_kind",
        ),
    )
    required_session_dates = tuple(
        sorted(
            _parse_date(item, "coverage.required_session_dates item")
            for item in _require_list(
                coverage_value["required_session_dates"],
                "coverage.required_session_dates",
            )
        )
    )
    if len(required_session_dates) != len(set(required_session_dates)):
        raise SecurityUniverseAdapterError("required_session_dates contains duplicates")
    if any(not start_date <= item <= end_date for item in required_session_dates):
        raise SecurityUniverseAdapterError(
            "required_session_dates must stay inside coverage range"
        )
    coverage_provenance = _parse_provenance(
        coverage_value["provenance"], descriptor, "coverage.provenance"
    )
    coverage = UniverseCoverage(
        universe_id=descriptor.universe_id,
        market=Market.A,
        start_date=start_date,
        end_date=end_date,
        source=descriptor.source,
        universe_version=descriptor.source_version,
        known_at=coverage_provenance.known_at,
        usable_from=coverage_provenance.usable_from,
        revision=coverage_provenance.revision,
        verified=False,
        complete=False,
        source_note="candidate only; completeness and verification require reconciliation",
    )

    identities = tuple(
        sorted(
            (
                _parse_identity(item, descriptor, index)
                for index, item in enumerate(
                    _require_list(document["identities"], "identities")
                )
            ),
            key=lambda item: (
                item.instrument_id,
                item.effective_from,
                item.provenance.known_at,
                _revision_identity(item.provenance.revision),
                item.candidate_id,
            ),
        )
    )
    statuses = tuple(
        sorted(
            (
                _parse_status(item, descriptor, index)
                for index, item in enumerate(_require_list(document["statuses"], "statuses"))
            ),
            key=lambda item: (
                item.instrument_id,
                item.session_date,
                item.scope.value,
                item.effective_start,
                item.provenance.known_at,
                _revision_identity(item.provenance.revision),
                item.candidate_id,
            ),
        )
    )
    memberships = tuple(
        sorted(
            (
                _parse_membership(item, descriptor, index)
                for index, item in enumerate(
                    _require_list(document["memberships"], "memberships")
                )
            ),
            key=lambda item: (
                item.instrument_id,
                item.effective_date,
                item.provenance.known_at,
                _revision_identity(item.provenance.revision),
                item.candidate_id,
            ),
        )
    )
    _validate_rows(
        identities,
        kind="identity",
        logical_key=lambda item: (item.instrument_id, item.effective_from),
    )
    _validate_rows(
        statuses,
        kind="status",
        logical_key=lambda item: (
            item.instrument_id,
            item.session_date,
            item.scope,
            item.effective_start,
        ),
    )
    _validate_rows(
        memberships,
        kind="membership",
        logical_key=lambda item: (item.instrument_id, item.effective_date),
    )
    _validate_identity_intervals(identities)
    closures, continuity_gaps, unparsed, declared_conflicts = _parse_evidence(
        document["coverage_evidence"]
    )
    report = _build_report(
        exchange=descriptor.exchange,
        coverage_kind=coverage_kind,
        required_session_dates=required_session_dates,
        identities=identities,
        statuses=statuses,
        memberships=memberships,
        closures=closures,
        continuity_gaps=continuity_gaps,
        unparsed_attachments=unparsed,
        declared_conflicts=declared_conflicts,
    )
    return SecurityUniverseCandidateBundle(
        descriptor=descriptor,
        coverage_kind=coverage_kind,
        required_session_dates=required_session_dates,
        coverage=coverage,
        identities=identities,
        statuses=statuses,
        memberships=memberships,
        coverage_report=report,
    )
