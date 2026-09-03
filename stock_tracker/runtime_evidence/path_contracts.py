from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import Any

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
    MARKET_EVENT_SEQUENCE_POLICY_V1,
    MarketEventSelection,
    MarketEventSelectionVerification,
    MarketEventSourceRecord,
)

RUNTIME_PATH_CONTRACT_SCHEMA = "stage4g1-runtime-path-contract-v3"
RUNTIME_PATH_PREFIX_SCHEMA = "stage4g1-runtime-path-prefix-v3"
RUNTIME_PATH_RESOLUTION_SCHEMA = "stage4g1-runtime-path-resolution-v2"
RUNTIME_NO_ENTRY_SCHEMA = "stage4g1-runtime-no-entry-evidence-v2"
RUNTIME_EXECUTION_SUMMARY_SCHEMA = "stage4g1-runtime-execution-summary-v3"
RUNTIME_INTERVAL_BOUNDARY_POLICY_V1 = MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 4096
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


class RuntimePathContractError(ValueError):
    """Raised when path, session, execution, or no-entry evidence is malformed."""

    def __init__(self, message: str, *, code: str = "RUNTIME_PATH_CONTRACT_INVALID") -> None:
        super().__init__(message)
        self.code = code


class RuntimeCalendarState(StrEnum):
    OPEN = "OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"


class RuntimeOpenSessionState(StrEnum):
    TRADED = "TRADED"
    SUSPENDED = "SUSPENDED"
    NO_TRADE = "NO_TRADE"
    MISSING_DATA = "MISSING_DATA"


class RuntimeCoverageState(StrEnum):
    COMPLETE_PREFIX = "COMPLETE_PREFIX"
    COMPLETE_SESSION = "COMPLETE_SESSION"
    INCOMPLETE_GAP = "INCOMPLETE_GAP"
    INCOMPLETE_OUT_OF_ORDER = "INCOMPLETE_OUT_OF_ORDER"
    INCOMPLETE_SPARSE = "INCOMPLETE_SPARSE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RuntimePathGranularity(StrEnum):
    TICK = "TICK"
    MINUTE_BAR = "MINUTE_BAR"
    DAILY_BAR = "DAILY_BAR"


class RuntimePathFactKind(StrEnum):
    SESSION = "SESSION"
    POINT = "POINT"


class RuntimePathResolutionState(StrEnum):
    OPEN = "OPEN"
    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"


class RuntimePathTerminalReason(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"


class RuntimePathPendingCode(StrEnum):
    HORIZON_NOT_REACHED = "HORIZON_NOT_REACHED"


class RuntimePathBlockerCode(StrEnum):
    CALENDAR_COVERAGE_GAP = "CALENDAR_COVERAGE_GAP"
    ENTRY_SESSION_MISSING = "ENTRY_SESSION_MISSING"
    ENTRY_SESSION_MISMATCH = "ENTRY_SESSION_MISMATCH"
    ENTRY_SESSION_NOT_TRADED = "ENTRY_SESSION_NOT_TRADED"
    ENTRY_TIME_OUTSIDE_SESSION_WINDOW = "ENTRY_TIME_OUTSIDE_SESSION_WINDOW"
    OPEN_SESSION_INDEX_GAP = "OPEN_SESSION_INDEX_GAP"
    POINT_WITHOUT_SESSION = "POINT_WITHOUT_SESSION"
    POINT_BEFORE_ENTRY_WINDOW = "POINT_BEFORE_ENTRY_WINDOW"
    POINT_OUTSIDE_SESSION_WINDOW = "POINT_OUTSIDE_SESSION_WINDOW"
    WINDOW_BOUNDARY_OVERLAP = "WINDOW_BOUNDARY_OVERLAP"
    OVERLAPPING_PATH_OBSERVATIONS = "OVERLAPPING_PATH_OBSERVATIONS"
    NONTRADED_SESSION_WITH_POINTS = "NONTRADED_SESSION_WITH_POINTS"
    TRADED_SESSION_WITHOUT_POINTS = "TRADED_SESSION_WITHOUT_POINTS"
    POINT_BEYOND_COVERAGE = "POINT_BEYOND_COVERAGE"
    SESSION_PREFIX_NOT_CLOSED = "SESSION_PREFIX_NOT_CLOSED"
    SESSION_GAP = "SESSION_GAP"
    SESSION_OUT_OF_ORDER = "SESSION_OUT_OF_ORDER"
    SESSION_SPARSE = "SESSION_SPARSE"
    MISSING_DATA = "MISSING_DATA"
    INTRABAR_AMBIGUITY = "INTRABAR_AMBIGUITY"
    HORIZON_SESSION_INCOMPLETE = "HORIZON_SESSION_INCOMPLETE"
    ENTRY_BOUNDARY_ORDER_AMBIGUITY = "ENTRY_BOUNDARY_ORDER_AMBIGUITY"


class RuntimeAuthorityKind(StrEnum):
    CALENDAR = "CALENDAR"
    SECURITY_STATUS = "SECURITY_STATUS"
    COVERAGE = "COVERAGE"


class RuntimeNoEntryReason(StrEnum):
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    USER_CANCELLED = "USER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    DATA_INVALID = "DATA_INVALID"


class RuntimeExecutionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class RuntimeFillCompletion(StrEnum):
    ZERO_FILL = "ZERO_FILL"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int = _MAX_TEXT,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > maximum:
        raise RuntimePathContractError(f"{name} must be a safe trimmed string")
    if not value and not allow_empty:
        raise RuntimePathContractError(f"{name} must not be empty")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise RuntimePathContractError(f"{name} contains control characters")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=64)
    if _SHA256.fullmatch(text) is None:
        raise RuntimePathContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimePathContractError(f"{name} must be boolean")
    return value


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimePathContractError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise RuntimePathContractError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise RuntimePathContractError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise RuntimePathContractError(f"{name} must be non-negative")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimePathContractError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_date(value: object, name: str) -> date:
    if type(value) is not date:
        raise RuntimePathContractError(f"{name} must be a date")
    return value


def _require_market(value: object) -> Market:
    if type(value) is not Market:
        raise RuntimePathContractError("market must be the exact Market type")
    return value


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol", maximum=64)
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if symbol != symbol.upper() or not symbol.endswith(suffixes[market]):
        raise RuntimePathContractError("symbol suffix must match market")
    return symbol


def _hash_document(document: dict[str, Any]) -> str:
    try:
        raw = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimePathContractError("path evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _ohlc_output_hash(
    *,
    symbol: str,
    market: Market,
    interval_start: datetime,
    interval_end: datetime,
    high: Decimal,
    low: Decimal,
    close: Decimal,
) -> str:
    return _hash_document(
        {
            "schema": "stage4g1-runtime-projection-ohlc-output-v1",
            "symbol": symbol,
            "market": market.value,
            "interval_start": _utc_text(interval_start),
            "interval_end": _utc_text(interval_end),
            "high": _decimal_text(high),
            "low": _decimal_text(low),
            "close": _decimal_text(close),
        }
    )


@dataclass(frozen=True, slots=True)
class RuntimeSourceReference:
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    source_high_water_append_order: int
    finding_set_digest: str
    sequence_policy_id: str
    selection_id: str
    event_id: str
    source_session_id: str
    symbol: str
    market: Market
    event_type: str
    trading_day: date
    session_label_policy_id: str
    source_record_id: str
    source_append_order: int
    source_record_hash: str
    raw_payload_sha256: str
    parser_id: str
    schema_id: str
    source_time: datetime
    received_at: datetime
    durable_known_at: datetime
    source_reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.source_snapshot_id, "source_snapshot_id")
        _require_sha256(self.source_audit_id, "source_audit_id")
        high_water = _require_int(
            self.source_high_water_append_order,
            "source_high_water_append_order",
        )
        _require_sha256(self.finding_set_digest, "finding_set_digest")
        if self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported market-event sequence policy"
            )
        _require_sha256(self.selection_id, "selection_id")
        _require_sha256(self.event_id, "event_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_text(self.event_type, "event_type", maximum=128)
        _require_date(self.trading_day, "trading_day")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported market session label policy"
            )
        source_time = _require_utc(self.source_time, "source_time")
        received_at = _require_utc(self.received_at, "received_at")
        durable_known_at = _require_utc(self.durable_known_at, "durable_known_at")
        if source_time > durable_known_at or received_at > durable_known_at:
            raise RuntimePathContractError(
                "source or received time cannot exceed durable_known_at"
            )
        object.__setattr__(self, "source_time", source_time)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "durable_known_at", durable_known_at)
        _require_text(self.source_session_id, "source_session_id", maximum=256)
        _require_sha256(self.source_record_id, "source_record_id")
        append_order = _require_int(
            self.source_append_order,
            "source_append_order",
            minimum=1,
        )
        if append_order > high_water:
            raise RuntimePathContractError(
                "source member exceeds its selection high-water"
            )
        _require_sha256(self.source_record_hash, "source_record_hash")
        _require_sha256(self.raw_payload_sha256, "raw_payload_sha256")
        _require_text(self.parser_id, "parser_id", maximum=256)
        _require_text(self.schema_id, "schema_id", maximum=256)
        object.__setattr__(
            self,
            "source_reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @classmethod
    def from_verified_selection(
        cls,
        *,
        record: MarketEventSourceRecord,
        selection: MarketEventSelection,
        verification: MarketEventSelectionVerification,
    ) -> RuntimeSourceReference:
        if (
            type(record) is not MarketEventSourceRecord
            or type(selection) is not MarketEventSelection
            or type(verification) is not MarketEventSelectionVerification
            or verification.selection_id != selection.selection_id
            or verification.snapshot_id != selection.snapshot_id
            or record not in selection.records
        ):
            raise RuntimePathContractError(
                "runtime source member requires an exact verified Selection"
            )
        return cls(
            source_store_id=record.source_store_id,
            source_snapshot_id=selection.snapshot_id,
            source_audit_id=selection.snapshot_audit_id,
            source_high_water_append_order=selection.snapshot_high_water_append_order,
            finding_set_digest=selection.snapshot_finding_set_digest,
            sequence_policy_id=selection.sequence_policy_id,
            selection_id=selection.selection_id,
            event_id=record.event_id,
            source_session_id=record.session_id,
            symbol=record.symbol,
            market=record.market,
            event_type=record.event_type,
            trading_day=record.trading_day,
            session_label_policy_id=record.session_label_policy_id,
            source_record_id=record.source_record_id,
            source_append_order=record.append_order,
            source_record_hash=record.record_content_hash,
            raw_payload_sha256=record.raw_payload_sha256,
            parser_id=record.parser_id,
            schema_id=record.source_schema_id,
            source_time=record.source_time,
            received_at=record.received_at,
            durable_known_at=record.durable_known_at,
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-source-reference-v2",
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "source_high_water_append_order": self.source_high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "sequence_policy_id": self.sequence_policy_id,
            "selection_id": self.selection_id,
            "event_id": self.event_id,
            "source_session_id": self.source_session_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "event_type": self.event_type,
            "trading_day": self.trading_day.isoformat(),
            "session_label_policy_id": self.session_label_policy_id,
            "source_record_id": self.source_record_id,
            "source_append_order": self.source_append_order,
            "source_record_hash": self.source_record_hash,
            "raw_payload_sha256": self.raw_payload_sha256,
            "parser_id": self.parser_id,
            "schema_id": self.schema_id,
            "source_time": _utc_text(self.source_time),
            "received_at": _utc_text(self.received_at),
            "durable_known_at": _utc_text(self.durable_known_at),
        }
        if include_id:
            document["source_reference_id"] = self.source_reference_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeAuthorityFactReference:
    authority_kind: RuntimeAuthorityKind
    authority_store_id: str
    fact_schema: str
    effective_session_date: date
    known_at: datetime
    usable_from: datetime
    source: str
    revision: int
    policy_id: str
    fact_payload_sha256: str
    fact_record_hash: str = field(init=False)
    fact_id: str = field(init=False)
    authority_reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.authority_kind) is not RuntimeAuthorityKind:
            raise RuntimePathContractError(
                "authority_kind must be RuntimeAuthorityKind"
            )
        _require_sha256(self.authority_store_id, "authority_store_id")
        _require_text(self.fact_schema, "fact_schema", maximum=256)
        _require_date(self.effective_session_date, "effective_session_date")
        known_at = _require_utc(self.known_at, "known_at")
        usable_from = _require_utc(self.usable_from, "usable_from")
        if known_at > usable_from:
            raise RuntimePathContractError("authority known_at exceeds usable_from")
        object.__setattr__(self, "known_at", known_at)
        object.__setattr__(self, "usable_from", usable_from)
        _require_text(self.source, "source", maximum=256)
        _require_int(self.revision, "revision", minimum=1)
        _require_sha256(self.policy_id, "policy_id")
        _require_sha256(self.fact_payload_sha256, "fact_payload_sha256")
        object.__setattr__(
            self,
            "fact_record_hash",
            _hash_document(self.fact_document()),
        )
        object.__setattr__(
            self,
            "fact_id",
            _hash_document(
                {
                    "schema": "stage4g1-runtime-authority-fact-id-v1",
                    "authority_kind": self.authority_kind.value,
                    "authority_store_id": self.authority_store_id,
                    "revision": self.revision,
                    "fact_record_hash": self.fact_record_hash,
                }
            ),
        )
        object.__setattr__(
            self,
            "authority_reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def fact_document(self) -> dict[str, Any]:
        return {
            "schema": "stage4g1-runtime-authority-canonical-fact-v1",
            "authority_kind": self.authority_kind.value,
            "authority_store_id": self.authority_store_id,
            "fact_schema": self.fact_schema,
            "effective_session_date": self.effective_session_date.isoformat(),
            "known_at": _utc_text(self.known_at),
            "usable_from": _utc_text(self.usable_from),
            "source": self.source,
            "revision": self.revision,
            "policy_id": self.policy_id,
            "fact_payload_sha256": self.fact_payload_sha256,
        }

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-authority-fact-reference-v2",
            "fact": self.fact_document(),
            "fact_record_hash": self.fact_record_hash,
            "fact_id": self.fact_id,
        }
        if include_id:
            document["authority_reference_id"] = self.authority_reference_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeAuthoritySnapshotBinding:
    authority_kind: RuntimeAuthorityKind
    authority_store_id: str
    authority_audit_id: str
    authority_audited_at: datetime
    high_water_revision: int
    fact_count: int
    fact_manifest_digest: str
    authority_snapshot_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeAuthoritySnapshotBinding requires an exact fact inventory"
        )

    @classmethod
    def from_facts(
        cls,
        *,
        authority_kind: RuntimeAuthorityKind,
        authority_store_id: str,
        authority_audited_at: datetime,
        facts: tuple[RuntimeAuthorityFactReference, ...],
    ) -> RuntimeAuthoritySnapshotBinding:
        if type(authority_kind) is not RuntimeAuthorityKind:
            raise RuntimePathContractError(
                "authority_kind must be RuntimeAuthorityKind"
            )
        _require_sha256(authority_store_id, "authority_store_id")
        audited_at = _require_utc(authority_audited_at, "authority_audited_at")
        if type(facts) is not tuple or not facts or any(
            type(item) is not RuntimeAuthorityFactReference
            or item.authority_kind is not authority_kind
            or item.authority_store_id != authority_store_id
            or item.known_at > audited_at
            or item.usable_from > audited_at
            for item in facts
        ):
            raise RuntimePathContractError(
                "authority snapshot facts are invalid or future-known"
            )
        ordered = tuple(sorted(facts, key=lambda item: (item.revision, item.fact_id)))
        if ordered != facts or len({item.revision for item in facts}) != len(facts):
            raise RuntimePathContractError(
                "authority facts must have unique canonically ordered revisions"
            )
        chain = "0" * 64
        for fact in facts:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-authority-fact-manifest-node-v1",
                    "previous": chain,
                    "fact_id": fact.fact_id,
                    "fact_record_hash": fact.fact_record_hash,
                    "revision": fact.revision,
                }
            )
        fact_manifest_digest = _hash_document(
            {
                "schema": "stage4g1-runtime-authority-fact-manifest-root-v1",
                "fact_count": len(facts),
                "chain_head": chain,
            }
        )
        audit_document = {
            "schema": "stage4g1-runtime-authority-audit-v1",
            "authority_kind": authority_kind.value,
            "authority_store_id": authority_store_id,
            "authority_audited_at": _utc_text(audited_at),
            "high_water_revision": max(item.revision for item in facts),
            "fact_count": len(facts),
            "fact_manifest_digest": fact_manifest_digest,
        }
        self = object.__new__(cls)
        for name, value in {
            "authority_kind": authority_kind,
            "authority_store_id": authority_store_id,
            "authority_audit_id": _hash_document(audit_document),
            "authority_audited_at": audited_at,
            "high_water_revision": max(item.revision for item in facts),
            "fact_count": len(facts),
            "fact_manifest_digest": fact_manifest_digest,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "authority_snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-authority-snapshot-binding-v2",
            "authority_kind": self.authority_kind.value,
            "authority_store_id": self.authority_store_id,
            "authority_audit_id": self.authority_audit_id,
            "authority_audited_at": _utc_text(self.authority_audited_at),
            "high_water_revision": self.high_water_revision,
            "fact_count": self.fact_count,
            "fact_manifest_digest": self.fact_manifest_digest,
        }
        if include_id:
            document["authority_snapshot_id"] = self.authority_snapshot_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeAuthorityFactSelection:
    snapshot: RuntimeAuthoritySnapshotBinding
    facts: tuple[RuntimeAuthorityFactReference, ...]
    selection_policy_id: str
    verification_mode: str
    selection_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeAuthorityFactSelection requires exact snapshot verification"
        )

    @classmethod
    def from_verified_snapshot(
        cls,
        *,
        snapshot: RuntimeAuthoritySnapshotBinding,
        facts: tuple[RuntimeAuthorityFactReference, ...],
        selection_policy_id: str,
    ) -> RuntimeAuthorityFactSelection:
        if type(snapshot) is not RuntimeAuthoritySnapshotBinding:
            raise RuntimePathContractError(
                "authority selection requires RuntimeAuthoritySnapshotBinding"
            )
        _require_sha256(selection_policy_id, "selection_policy_id")
        expected = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=snapshot.authority_kind,
            authority_store_id=snapshot.authority_store_id,
            authority_audited_at=snapshot.authority_audited_at,
            facts=facts,
        )
        if expected != snapshot:
            raise RuntimePathContractError(
                "authority fact inventory disagrees with the exact snapshot"
            )
        self = object.__new__(cls)
        for name, value in {
            "snapshot": snapshot,
            "facts": facts,
            "selection_policy_id": selection_policy_id,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def contains(self, reference: RuntimeAuthorityFactReference) -> bool:
        return any(
            item.fact_id == reference.fact_id
            and item.fact_record_hash == reference.fact_record_hash
            for item in self.facts
        )

    def verify(self) -> None:
        expected_snapshot = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=self.snapshot.authority_kind,
            authority_store_id=self.snapshot.authority_store_id,
            authority_audited_at=self.snapshot.authority_audited_at,
            facts=self.facts,
        )
        if expected_snapshot != self.snapshot or self.verification_mode != "FULL_PREFIX_RESCAN":
            raise RuntimePathContractError(
                "authority fact selection no longer matches its exact snapshot"
            )
        if self.selection_id != _hash_document(self.as_dict(include_id=False)):
            raise RuntimePathContractError("authority fact selection identity mismatch")

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-authority-fact-selection-v1",
            "snapshot": self.snapshot.as_dict(),
            "selection_policy_id": self.selection_policy_id,
            "verification_mode": self.verification_mode,
            "ordered_fact_ids": [item.fact_id for item in self.facts],
            "ordered_fact_record_hashes": [
                item.fact_record_hash for item in self.facts
            ],
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeMarketSourceSnapshotBinding:
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    high_water_append_order: int
    finding_set_digest: str
    sequence_policy_id: str
    selection: MarketEventSelection
    selection_verification: MarketEventSelectionVerification
    source_snapshot_binding_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeMarketSourceSnapshotBinding requires a verified Selection"
        )

    @classmethod
    def from_verified_selection(
        cls,
        *,
        selection: MarketEventSelection,
        verification: MarketEventSelectionVerification,
    ) -> RuntimeMarketSourceSnapshotBinding:
        if (
            type(selection) is not MarketEventSelection
            or type(verification) is not MarketEventSelectionVerification
            or verification.selection_id != selection.selection_id
            or verification.selection_commitment_id
            != selection.commitment.commitment_id
            or verification.membership_witness_id
            != selection.membership_witness.witness_id
            or verification.snapshot_id != selection.snapshot_id
            or verification.snapshot_audit_id != selection.snapshot_audit_id
            or verification.snapshot_high_water_append_order
            != selection.snapshot_high_water_append_order
        ):
            raise RuntimePathContractError(
                "market source binding requires exact selection verification"
            )
        if selection.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported market-event sequence policy"
            )
        self = object.__new__(cls)
        for name, value in {
            "source_store_id": selection.source_store_id,
            "source_snapshot_id": selection.snapshot_id,
            "source_audit_id": selection.snapshot_audit_id,
            "high_water_append_order": selection.snapshot_high_water_append_order,
            "finding_set_digest": selection.snapshot_finding_set_digest,
            "sequence_policy_id": selection.sequence_policy_id,
            "selection": selection,
            "selection_verification": verification,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "source_snapshot_binding_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def verify(self) -> None:
        selection = self.selection
        verification = self.selection_verification
        selection._validate_contents()
        if (
            self.source_store_id != selection.source_store_id
            or self.source_snapshot_id != selection.snapshot_id
            or self.source_audit_id != selection.snapshot_audit_id
            or self.high_water_append_order
            != selection.snapshot_high_water_append_order
            or self.finding_set_digest != selection.snapshot_finding_set_digest
            or self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V1
            or verification.selection_id != selection.selection_id
            or verification.selection_commitment_id
            != selection.commitment.commitment_id
            or verification.membership_witness_id
            != selection.membership_witness.witness_id
            or verification.snapshot_id != selection.snapshot_id
            or verification.snapshot_audit_id != selection.snapshot_audit_id
            or verification.snapshot_high_water_append_order
            != selection.snapshot_high_water_append_order
            or self.source_snapshot_binding_id
            != _hash_document(self.as_dict(include_id=False))
        ):
            raise RuntimePathContractError(
                "market source binding no longer matches its verified Selection"
            )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-market-source-snapshot-binding-v2",
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "high_water_append_order": self.high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "sequence_policy_id": self.sequence_policy_id,
            "selection_id": self.selection.selection_id,
            "selection_commitment_id": self.selection.commitment.commitment_id,
            "selection_membership_witness_id": self.selection.membership_witness.witness_id,
            "selection_verification_id": self.selection_verification.verification_id,
        }
        if include_id:
            document["source_snapshot_binding_id"] = self.source_snapshot_binding_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeProjectionLineage:
    projection_policy_id: str
    source_store_id: str
    source_snapshot_id: str
    source_audit_id: str
    source_high_water_append_order: int
    finding_set_digest: str
    selection_id: str
    selection_verification_id: str
    interval_start: datetime
    interval_end: datetime
    interval_boundary_policy_id: str
    coverage_fact_id: str
    input_event_ids: tuple[str, ...]
    input_source_append_orders: tuple[int, ...]
    input_source_record_ids: tuple[str, ...]
    input_source_record_hashes: tuple[str, ...]
    projection_lineage_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.projection_policy_id, "projection_policy_id")
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.source_snapshot_id, "source_snapshot_id")
        _require_sha256(self.source_audit_id, "source_audit_id")
        high_water = _require_int(
            self.source_high_water_append_order,
            "source_high_water_append_order",
        )
        _require_sha256(self.finding_set_digest, "finding_set_digest")
        _require_sha256(self.selection_id, "selection_id")
        _require_sha256(
            self.selection_verification_id,
            "selection_verification_id",
        )
        start = _require_utc(self.interval_start, "interval_start")
        end = _require_utc(self.interval_end, "interval_end")
        if end <= start:
            raise RuntimePathContractError(
                "projection lineage requires a non-empty interval"
            )
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        if self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported runtime interval boundary policy"
            )
        _require_sha256(self.coverage_fact_id, "coverage_fact_id")
        if type(self.input_event_ids) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_event_ids
        ):
            raise RuntimePathContractError(
                "projection input event IDs must be SHA-256 tuples"
            )
        if type(self.input_source_append_orders) is not tuple or not self.input_source_append_orders:
            raise RuntimePathContractError("projection input append orders are required")
        orders = tuple(
            _require_int(item, "input_source_append_order", minimum=1)
            for item in self.input_source_append_orders
        )
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise RuntimePathContractError(
                "projection input append orders must be unique and increasing"
            )
        if orders[-1] > high_water:
            raise RuntimePathContractError(
                "projection input exceeds source high-water mark"
            )
        if type(self.input_source_record_ids) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_source_record_ids
        ):
            raise RuntimePathContractError(
                "projection input record IDs must be SHA-256 tuples"
            )
        if type(self.input_source_record_hashes) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.input_source_record_hashes
        ):
            raise RuntimePathContractError(
                "projection input record hashes must be SHA-256 tuples"
            )
        if not (
            len(orders)
            == len(self.input_event_ids)
            == len(self.input_source_record_ids)
            == len(self.input_source_record_hashes)
        ):
            raise RuntimePathContractError(
                "projection input lineage columns must have equal length"
            )
        if len(set(self.input_source_record_ids)) != len(orders):
            raise RuntimePathContractError("projection input record IDs must be unique")
        if len(set(self.input_event_ids)) != len(orders):
            raise RuntimePathContractError("projection input event IDs must be unique")
        object.__setattr__(
            self,
            "projection_lineage_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-projection-lineage-v2",
            "projection_policy_id": self.projection_policy_id,
            "source_store_id": self.source_store_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_audit_id": self.source_audit_id,
            "source_high_water_append_order": self.source_high_water_append_order,
            "finding_set_digest": self.finding_set_digest,
            "selection_id": self.selection_id,
            "selection_verification_id": self.selection_verification_id,
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "coverage_fact_id": self.coverage_fact_id,
            "input_event_ids": list(self.input_event_ids),
            "input_source_append_orders": list(self.input_source_append_orders),
            "input_source_record_ids": list(self.input_source_record_ids),
            "input_source_record_hashes": list(self.input_source_record_hashes),
        }
        if include_id:
            document["projection_lineage_id"] = self.projection_lineage_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeProjectionArtifactReference:
    projection_policy_id: str
    created_at: datetime
    durable_known_at: datetime
    symbol: str
    market: Market
    trading_day: date
    interval_start: datetime
    interval_end: datetime
    interval_boundary_policy_id: str
    lineage: RuntimeProjectionLineage
    output_ohlc_sha256: str
    coverage_fact_id: str
    projection_artifact_id: str = field(init=False)

    @classmethod
    def create(
        cls,
        *,
        projection_policy_id: str,
        created_at: datetime,
        durable_known_at: datetime,
        symbol: str,
        market: Market,
        trading_day: date,
        interval_start: datetime,
        interval_end: datetime,
        interval_boundary_policy_id: str,
        lineage: RuntimeProjectionLineage,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        coverage_fact_id: str,
    ) -> RuntimeProjectionArtifactReference:
        return cls(
            projection_policy_id=projection_policy_id,
            created_at=created_at,
            durable_known_at=durable_known_at,
            symbol=symbol,
            market=market,
            trading_day=trading_day,
            interval_start=interval_start,
            interval_end=interval_end,
            interval_boundary_policy_id=interval_boundary_policy_id,
            lineage=lineage,
            output_ohlc_sha256=_ohlc_output_hash(
                symbol=symbol,
                market=market,
                interval_start=interval_start,
                interval_end=interval_end,
                high=high,
                low=low,
                close=close,
            ),
            coverage_fact_id=coverage_fact_id,
        )

    def __post_init__(self) -> None:
        _require_sha256(self.projection_policy_id, "projection_policy_id")
        created_at = _require_utc(self.created_at, "created_at")
        durable_known_at = _require_utc(
            self.durable_known_at,
            "durable_known_at",
        )
        if created_at > durable_known_at:
            raise RuntimePathContractError(
                "projection creation time is outside its causal interval"
            )
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "durable_known_at", durable_known_at)
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_date(self.trading_day, "trading_day")
        start = _require_utc(self.interval_start, "interval_start")
        end = _require_utc(self.interval_end, "interval_end")
        if end <= start or end > durable_known_at or created_at < end:
            raise RuntimePathContractError(
                "projection interval is invalid at durable-known time"
            )
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        if (
            self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1
            or market_session_date(
                end - timedelta(microseconds=1),
                market,
                MARKET_SESSION_LABEL_POLICY_V1,
            )
            != self.trading_day
        ):
            raise RuntimePathContractError(
                "projection interval policy or trading-day label is invalid"
            )
        if type(self.lineage) is not RuntimeProjectionLineage or (
            self.lineage.projection_policy_id != self.projection_policy_id
            or self.lineage.interval_start != start
            or self.lineage.interval_end != end
            or self.lineage.interval_boundary_policy_id
            != self.interval_boundary_policy_id
        ):
            raise RuntimePathContractError(
                "projection artifact and lineage disagree"
            )
        _require_sha256(self.output_ohlc_sha256, "output_ohlc_sha256")
        _require_sha256(self.coverage_fact_id, "coverage_fact_id")
        if self.lineage.coverage_fact_id != self.coverage_fact_id:
            raise RuntimePathContractError(
                "projection coverage reference disagrees with lineage"
            )
        object.__setattr__(
            self,
            "projection_artifact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-projection-artifact-reference-v1",
            "projection_policy_id": self.projection_policy_id,
            "created_at": _utc_text(self.created_at),
            "durable_known_at": _utc_text(self.durable_known_at),
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "lineage": self.lineage.as_dict(),
            "output_ohlc_sha256": self.output_ohlc_sha256,
            "coverage_fact_id": self.coverage_fact_id,
        }
        if include_id:
            document["projection_artifact_id"] = self.projection_artifact_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeSessionEvidence:
    symbol: str
    market: Market
    trading_day: date
    calendar_state: RuntimeCalendarState
    open_session_index: int | None
    open_session_state: RuntimeOpenSessionState | None
    scheduled_open_at: datetime | None
    scheduled_close_at: datetime | None
    coverage_state: RuntimeCoverageState
    session_complete: bool
    coverage_through: datetime | None
    session_label_policy_id: str
    calendar_reference: RuntimeAuthorityFactReference
    security_status_reference: RuntimeAuthorityFactReference | None
    coverage_reference: RuntimeAuthorityFactReference | None
    session_evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_date(self.trading_day, "trading_day")
        if type(self.calendar_state) is not RuntimeCalendarState:
            raise RuntimePathContractError("calendar_state must be RuntimeCalendarState")
        if type(self.coverage_state) is not RuntimeCoverageState:
            raise RuntimePathContractError("coverage_state must be RuntimeCoverageState")
        _require_bool(self.session_complete, "session_complete")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported market session label policy"
            )
        if type(self.calendar_reference) is not RuntimeAuthorityFactReference:
            raise RuntimePathContractError(
                "calendar_reference must be RuntimeAuthorityFactReference"
            )
        if self.calendar_reference.authority_kind is not RuntimeAuthorityKind.CALENDAR:
            raise RuntimePathContractError("calendar_reference kind is invalid")
        security_status = self.security_status_reference
        if security_status is not None and (
            type(security_status) is not RuntimeAuthorityFactReference
            or security_status.authority_kind is not RuntimeAuthorityKind.SECURITY_STATUS
        ):
            raise RuntimePathContractError("security_status_reference kind is invalid")
        coverage_reference = self.coverage_reference
        if coverage_reference is not None and (
            type(coverage_reference) is not RuntimeAuthorityFactReference
            or coverage_reference.authority_kind is not RuntimeAuthorityKind.COVERAGE
        ):
            raise RuntimePathContractError("coverage_reference kind is invalid")
        for reference in (
            self.calendar_reference,
            security_status,
            coverage_reference,
        ):
            if reference is not None and reference.effective_session_date != self.trading_day:
                raise RuntimePathContractError(
                    "authority reference session date disagrees with trading_day"
                )
        scheduled_open = (
            None
            if self.scheduled_open_at is None
            else _require_utc(self.scheduled_open_at, "scheduled_open_at")
        )
        scheduled_close = (
            None
            if self.scheduled_close_at is None
            else _require_utc(self.scheduled_close_at, "scheduled_close_at")
        )
        if (scheduled_open is None) != (scheduled_close is None):
            raise RuntimePathContractError(
                "scheduled session boundaries must be present together"
            )
        if (
            scheduled_open is not None
            and scheduled_close is not None
            and scheduled_close <= scheduled_open
        ):
            raise RuntimePathContractError(
                "scheduled_close_at must be after scheduled_open_at"
            )
        if scheduled_open is not None and scheduled_close is not None and (
            market_session_date(
                scheduled_open,
                market,
                self.session_label_policy_id,
            )
            != self.trading_day
            or market_session_date(
                scheduled_close,
                market,
                self.session_label_policy_id,
            )
            != self.trading_day
        ):
            raise RuntimePathContractError(
                "scheduled session boundaries disagree with market-local trading_day"
            )
        object.__setattr__(self, "scheduled_open_at", scheduled_open)
        object.__setattr__(self, "scheduled_close_at", scheduled_close)
        coverage_through = (
            None
            if self.coverage_through is None
            else _require_utc(self.coverage_through, "coverage_through")
        )
        if (
            coverage_through is not None
            and coverage_reference is not None
            and coverage_through > coverage_reference.usable_from
        ):
            raise RuntimePathContractError(
                "coverage_through cannot exceed coverage authority usable_from"
            )
        object.__setattr__(self, "coverage_through", coverage_through)
        if self.calendar_state is RuntimeCalendarState.MARKET_CLOSED:
            if (
                self.open_session_index is not None
                or self.open_session_state is not None
                or scheduled_open is not None
                or scheduled_close is not None
                or self.coverage_state is not RuntimeCoverageState.NOT_APPLICABLE
                or not self.session_complete
                or coverage_through is not None
                or security_status is not None
                or coverage_reference is not None
            ):
                raise RuntimePathContractError(
                    "MARKET_CLOSED cannot carry an open-session index, status, or market data"
                )
        else:
            _require_int(self.open_session_index, "open_session_index")
            if scheduled_open is None or scheduled_close is None:
                raise RuntimePathContractError(
                    "OPEN calendar day requires scheduled session boundaries"
                )
            if type(self.open_session_state) is not RuntimeOpenSessionState:
                raise RuntimePathContractError(
                    "OPEN calendar day requires RuntimeOpenSessionState"
                )
            if self.coverage_state is RuntimeCoverageState.NOT_APPLICABLE:
                raise RuntimePathContractError(
                    "OPEN calendar day requires a market-data coverage state"
                )
            if coverage_reference is None:
                raise RuntimePathContractError(
                    "OPEN calendar day requires coverage_reference"
                )
            if self.coverage_state in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            } and coverage_through is None:
                raise RuntimePathContractError(
                    "complete coverage requires an explicit coverage_through time"
                )
            if coverage_through is not None and not (
                scheduled_open <= coverage_through <= scheduled_close
            ):
                raise RuntimePathContractError(
                    "coverage_through is outside the scheduled session window"
                )
            if self.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX:
                if (
                    coverage_through is None
                    or self.session_complete
                    or coverage_through >= scheduled_close
                ):
                    raise RuntimePathContractError(
                        "COMPLETE_PREFIX must stop before the scheduled session close"
                    )
            elif self.coverage_state is RuntimeCoverageState.COMPLETE_SESSION and (
                not self.session_complete or coverage_through != scheduled_close
            ):
                raise RuntimePathContractError(
                    "COMPLETE_SESSION must cover the scheduled session close"
                )
            if self.open_session_state in {
                RuntimeOpenSessionState.SUSPENDED,
                RuntimeOpenSessionState.NO_TRADE,
            } and (
                security_status is None
                or self.coverage_state is not RuntimeCoverageState.COMPLETE_SESSION
                or not self.session_complete
            ):
                raise RuntimePathContractError(
                    "SUSPENDED/NO_TRADE require authoritative status and complete-session coverage"
                )
            if self.open_session_state is RuntimeOpenSessionState.MISSING_DATA and self.coverage_state in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            }:
                raise RuntimePathContractError(
                    "MISSING_DATA cannot claim complete market-data coverage"
                )
        object.__setattr__(
            self,
            "session_evidence_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-session-evidence-v2",
            "symbol": self.symbol,
            "market": self.market.value,
            "trading_day": self.trading_day.isoformat(),
            "calendar_state": self.calendar_state.value,
            "open_session_index": self.open_session_index,
            "open_session_state": (
                None if self.open_session_state is None else self.open_session_state.value
            ),
            "scheduled_open_at": (
                None if self.scheduled_open_at is None else _utc_text(self.scheduled_open_at)
            ),
            "scheduled_close_at": (
                None if self.scheduled_close_at is None else _utc_text(self.scheduled_close_at)
            ),
            "coverage_state": self.coverage_state.value,
            "session_complete": self.session_complete,
            "coverage_through": (
                None if self.coverage_through is None else _utc_text(self.coverage_through)
            ),
            "session_label_policy_id": self.session_label_policy_id,
            "calendar_reference": self.calendar_reference.as_dict(),
            "security_status_reference": (
                None
                if self.security_status_reference is None
                else self.security_status_reference.as_dict()
            ),
            "coverage_reference": (
                None
                if self.coverage_reference is None
                else self.coverage_reference.as_dict()
            ),
        }
        if include_id:
            document["session_evidence_id"] = self.session_evidence_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathObservation:
    symbol: str
    market: Market
    open_session_index: int
    interval_start: datetime
    interval_end: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    granularity: RuntimePathGranularity
    interval_boundary_policy_id: str
    source_reference: RuntimeSourceReference | None
    projection_artifact_reference: RuntimeProjectionArtifactReference | None
    path_observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_int(self.open_session_index, "open_session_index")
        interval_start = _require_utc(self.interval_start, "interval_start")
        interval_end = _require_utc(self.interval_end, "interval_end")
        if interval_end < interval_start:
            raise RuntimePathContractError("path interval_end precedes interval_start")
        object.__setattr__(self, "interval_start", interval_start)
        object.__setattr__(self, "interval_end", interval_end)
        high = _require_decimal(self.high, "high", positive=True)
        low = _require_decimal(self.low, "low", positive=True)
        close = _require_decimal(self.close, "close", positive=True)
        if low > min(high, close) or high < max(low, close):
            raise RuntimePathContractError("path OHLC values are inconsistent")
        if type(self.granularity) is not RuntimePathGranularity:
            raise RuntimePathContractError(
                "granularity must be RuntimePathGranularity"
            )
        if self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported runtime interval boundary policy"
            )
        if self.granularity is RuntimePathGranularity.TICK:
            if (
                interval_start != interval_end
                or not (high == low == close)
                or type(self.source_reference) is not RuntimeSourceReference
                or self.projection_artifact_reference is not None
            ):
                raise RuntimePathContractError(
                    "TICK evidence requires one exact source point without projection"
                )
            source_reference = self.source_reference
            if (
                source_reference.symbol != self.symbol
                or source_reference.market is not self.market
                or source_reference.event_type != "TRADE_TICK"
                or source_reference.source_time != interval_start
                or source_reference.trading_day
                != market_session_date(
                    interval_start,
                    market,
                    source_reference.session_label_policy_id,
                )
                or interval_end > source_reference.durable_known_at
            ):
                raise RuntimePathContractError(
                    "TICK observation disagrees with its verified trade-event member"
                )
        else:
            if interval_start == interval_end:
                raise RuntimePathContractError("bar evidence requires a non-zero interval")
            if self.source_reference is not None or type(
                self.projection_artifact_reference
            ) is not RuntimeProjectionArtifactReference:
                raise RuntimePathContractError(
                    "bar evidence requires one immutable projection artifact"
                )
            projection = self.projection_artifact_reference
            if (
                projection.symbol != self.symbol
                or projection.market is not self.market
                or projection.interval_start != interval_start
                or projection.interval_end != interval_end
                or projection.interval_boundary_policy_id
                != self.interval_boundary_policy_id
                or projection.output_ohlc_sha256
                != _ohlc_output_hash(
                    symbol=self.symbol,
                    market=self.market,
                    interval_start=interval_start,
                    interval_end=interval_end,
                    high=high,
                    low=low,
                    close=close,
                )
            ):
                raise RuntimePathContractError(
                    "projection artifact disagrees with the observation output"
                )
        object.__setattr__(
            self,
            "path_observation_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-path-observation-v3",
            "symbol": self.symbol,
            "market": self.market.value,
            "open_session_index": self.open_session_index,
            "interval_start": _utc_text(self.interval_start),
            "interval_end": _utc_text(self.interval_end),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "granularity": self.granularity.value,
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "source_reference": (
                None
                if self.source_reference is None
                else self.source_reference.as_dict()
            ),
            "projection_artifact_reference": (
                None
                if self.projection_artifact_reference is None
                else self.projection_artifact_reference.as_dict()
            ),
        }
        if include_id:
            document["path_observation_id"] = self.path_observation_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeFrozenPathFact:
    collection_store_id: str
    collection_append_order: int
    case_id: str
    collection_fact_id: str
    collection_observed_at: datetime
    kind: RuntimePathFactKind
    session_evidence: RuntimeSessionEvidence | None = None
    path_observation: RuntimePathObservation | None = None
    frozen_fact_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.collection_store_id, "collection_store_id")
        _require_sha256(self.case_id, "case_id")
        _require_int(
            self.collection_append_order,
            "collection_append_order",
            minimum=1,
        )
        _require_sha256(self.collection_fact_id, "collection_fact_id")
        observed_at = _require_utc(
            self.collection_observed_at,
            "collection_observed_at",
        )
        object.__setattr__(self, "collection_observed_at", observed_at)
        if type(self.kind) is not RuntimePathFactKind:
            raise RuntimePathContractError("kind must be RuntimePathFactKind")
        if self.kind is RuntimePathFactKind.SESSION:
            if (
                type(self.session_evidence) is not RuntimeSessionEvidence
                or self.path_observation is not None
            ):
                raise RuntimePathContractError(
                    "SESSION fact must contain exactly one RuntimeSessionEvidence"
                )
            references = tuple(
                item
                for item in (
                    self.session_evidence.calendar_reference,
                    self.session_evidence.security_status_reference,
                    self.session_evidence.coverage_reference,
                )
                if item is not None
            )
            if any(
                item.known_at > observed_at or item.usable_from > observed_at
                for item in references
            ):
                raise RuntimePathContractError(
                    "collection fact predates authority knowledge or usability"
                )
        else:
            if (
                type(self.path_observation) is not RuntimePathObservation
                or self.session_evidence is not None
            ):
                raise RuntimePathContractError(
                    "POINT fact must contain exactly one RuntimePathObservation"
                )
            evidence_known_at = (
                self.path_observation.source_reference.durable_known_at
                if self.path_observation.source_reference is not None
                else self.path_observation.projection_artifact_reference.durable_known_at
                if self.path_observation.projection_artifact_reference is not None
                else observed_at + timedelta(microseconds=1)
            )
            if evidence_known_at > observed_at:
                raise RuntimePathContractError(
                    "collection fact predates the source durable-known time"
                )
        object.__setattr__(
            self,
            "frozen_fact_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def source_reference(self) -> RuntimeSourceReference | None:
        if self.kind is RuntimePathFactKind.SESSION:
            return None
        assert self.path_observation is not None
        return self.path_observation.source_reference

    @property
    def symbol(self) -> str:
        if self.kind is RuntimePathFactKind.SESSION:
            assert self.session_evidence is not None
            return self.session_evidence.symbol
        assert self.path_observation is not None
        return self.path_observation.symbol

    @property
    def market(self) -> Market:
        if self.kind is RuntimePathFactKind.SESSION:
            assert self.session_evidence is not None
            return self.session_evidence.market
        assert self.path_observation is not None
        return self.path_observation.market

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-frozen-path-fact-v2",
            "collection_store_id": self.collection_store_id,
            "collection_append_order": self.collection_append_order,
            "case_id": self.case_id,
            "collection_fact_id": self.collection_fact_id,
            "collection_observed_at": _utc_text(self.collection_observed_at),
            "kind": self.kind.value,
            "session_evidence": (
                None
                if self.session_evidence is None
                else self.session_evidence.as_dict()
            ),
            "path_observation": (
                None
                if self.path_observation is None
                else self.path_observation.as_dict()
            ),
        }
        if include_id:
            document["frozen_fact_id"] = self.frozen_fact_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimePathStoreSnapshotBinding:
    path_store_id: str
    path_snapshot_id: str
    path_audit_id: str
    path_audited_at: datetime
    global_high_water_append_order: int
    record_count: int
    global_manifest_digest: str
    finding_blocker_digest: str
    verification_mode: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimePathStoreSnapshotBinding requires an exact path-store prefix"
        )

    @classmethod
    def from_prefix(
        cls,
        *,
        path_store_id: str,
        path_audited_at: datetime,
        facts: tuple[RuntimeFrozenPathFact, ...],
        finding_blocker_digest: str,
    ) -> RuntimePathStoreSnapshotBinding:
        path_store_id = _require_sha256(path_store_id, "path_store_id")
        audited_at = _require_utc(path_audited_at, "path_audited_at")
        _require_sha256(finding_blocker_digest, "finding_blocker_digest")
        if type(facts) is not tuple or any(
            type(item) is not RuntimeFrozenPathFact for item in facts
        ):
            raise RuntimePathContractError(
                "path snapshot facts must be an exact tuple"
            )
        orders = tuple(item.collection_append_order for item in facts)
        if orders != tuple(range(1, len(facts) + 1)) or any(
            item.collection_store_id != path_store_id
            or item.collection_observed_at > audited_at
            for item in facts
        ):
            raise RuntimePathContractError(
                "path snapshot is not the exact contiguous global prefix"
            )
        if len({item.collection_fact_id for item in facts}) != len(facts) or len(
            {item.frozen_fact_id for item in facts}
        ) != len(facts):
            raise RuntimePathContractError(
                "path snapshot contains duplicate fact identity"
            )
        chain = "0" * 64
        for fact in facts:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-path-store-manifest-node-v1",
                    "previous": chain,
                    "append_order": fact.collection_append_order,
                    "case_id": fact.case_id,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        manifest = _hash_document(
            {
                "schema": "stage4g1-runtime-path-store-manifest-root-v1",
                "record_count": len(facts),
                "chain_head": chain,
            }
        )
        audit_document = {
            "schema": "stage4g1-runtime-path-store-audit-v1",
            "path_store_id": path_store_id,
            "path_audited_at": _utc_text(audited_at),
            "global_high_water_append_order": len(facts),
            "record_count": len(facts),
            "global_manifest_digest": manifest,
            "finding_blocker_digest": finding_blocker_digest,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }
        self = object.__new__(cls)
        for name, value in {
            "path_store_id": path_store_id,
            "path_audit_id": _hash_document(audit_document),
            "path_audited_at": audited_at,
            "global_high_water_append_order": len(facts),
            "record_count": len(facts),
            "global_manifest_digest": manifest,
            "finding_blocker_digest": finding_blocker_digest,
            "verification_mode": "FULL_PREFIX_RESCAN",
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "path_snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-path-store-snapshot-binding-v1",
            "path_store_id": self.path_store_id,
            "path_audit_id": self.path_audit_id,
            "path_audited_at": _utc_text(self.path_audited_at),
            "global_high_water_append_order": self.global_high_water_append_order,
            "record_count": self.record_count,
            "global_manifest_digest": self.global_manifest_digest,
            "finding_blocker_digest": self.finding_blocker_digest,
            "verification_mode": self.verification_mode,
        }
        if include_id:
            document["path_snapshot_id"] = self.path_snapshot_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class RuntimeCaseFactSelection:
    path_store_snapshot: RuntimePathStoreSnapshotBinding
    case_id: str
    symbol: str
    market: Market
    selection_policy_id: str
    facts: tuple[RuntimeFrozenPathFact, ...]
    case_manifest_commitment: str
    verification_mode: str
    finding_blocker_digest: str
    selection_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeCaseFactSelection requires exact path-store verification"
        )

    @classmethod
    def from_verified_snapshot(
        cls,
        *,
        path_store_snapshot: RuntimePathStoreSnapshotBinding,
        all_facts: tuple[RuntimeFrozenPathFact, ...],
        case_id: str,
        symbol: str,
        market: Market,
        selection_policy_id: str,
    ) -> RuntimeCaseFactSelection:
        if type(path_store_snapshot) is not RuntimePathStoreSnapshotBinding:
            raise RuntimePathContractError(
                "case selection requires RuntimePathStoreSnapshotBinding"
            )
        _require_sha256(case_id, "case_id")
        market = _require_market(market)
        _require_symbol(symbol, market)
        _require_sha256(selection_policy_id, "selection_policy_id")
        expected_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=path_store_snapshot.path_store_id,
            path_audited_at=path_store_snapshot.path_audited_at,
            facts=all_facts,
            finding_blocker_digest=path_store_snapshot.finding_blocker_digest,
        )
        if expected_snapshot != path_store_snapshot:
            raise RuntimePathContractError(
                "path-store prefix disagrees with the frozen snapshot"
            )
        selected = tuple(item for item in all_facts if item.case_id == case_id)
        if not selected or any(
            item.symbol != symbol or item.market is not market for item in selected
        ):
            raise RuntimePathContractError(
                "case selection is empty or contains inconsistent facts"
            )
        chain = "0" * 64
        for fact in selected:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-case-selection-node-v1",
                    "previous": chain,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        commitment = _hash_document(
            {
                "schema": "stage4g1-runtime-case-selection-root-v1",
                "case_id": case_id,
                "selection_policy_id": selection_policy_id,
                "fact_count": len(selected),
                "chain_head": chain,
            }
        )
        self = object.__new__(cls)
        for name, value in {
            "path_store_snapshot": path_store_snapshot,
            "case_id": case_id,
            "symbol": symbol,
            "market": market,
            "selection_policy_id": selection_policy_id,
            "facts": selected,
            "case_manifest_commitment": commitment,
            "verification_mode": "FULL_PREFIX_RESCAN",
            "finding_blocker_digest": path_store_snapshot.finding_blocker_digest,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def verify(self) -> None:
        orders = tuple(item.collection_append_order for item in self.facts)
        if orders != tuple(sorted(orders)) or any(
            item.case_id != self.case_id
            or item.symbol != self.symbol
            or item.market is not self.market
            or item.collection_store_id
            != self.path_store_snapshot.path_store_id
            or item.collection_append_order
            > self.path_store_snapshot.global_high_water_append_order
            for item in self.facts
        ):
            raise RuntimePathContractError(
                "case selection facts are outside the verified case query"
            )
        chain = "0" * 64
        for fact in self.facts:
            chain = _hash_document(
                {
                    "schema": "stage4g1-runtime-case-selection-node-v1",
                    "previous": chain,
                    "collection_fact_id": fact.collection_fact_id,
                    "record_hash": fact.frozen_fact_id,
                }
            )
        expected_commitment = _hash_document(
            {
                "schema": "stage4g1-runtime-case-selection-root-v1",
                "case_id": self.case_id,
                "selection_policy_id": self.selection_policy_id,
                "fact_count": len(self.facts),
                "chain_head": chain,
            }
        )
        if (
            self.case_manifest_commitment != expected_commitment
            or self.verification_mode != "FULL_PREFIX_RESCAN"
            or self.finding_blocker_digest
            != self.path_store_snapshot.finding_blocker_digest
        ):
            raise RuntimePathContractError("case selection commitment mismatch")
        if self.selection_id != _hash_document(self.as_dict(include_id=False)):
            raise RuntimePathContractError("case selection identity mismatch")

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-case-fact-selection-v1",
            "path_store_snapshot": self.path_store_snapshot.as_dict(),
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "selection_policy_id": self.selection_policy_id,
            "ordered_case_fact_ids": [
                item.collection_fact_id for item in self.facts
            ],
            "ordered_case_record_hashes": [
                item.frozen_fact_id for item in self.facts
            ],
            "case_manifest_commitment": self.case_manifest_commitment,
            "verification_mode": self.verification_mode,
            "finding_blocker_digest": self.finding_blocker_digest,
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


def _source_reference_matches_record(
    reference: RuntimeSourceReference,
    record: MarketEventSourceRecord,
    binding: RuntimeMarketSourceSnapshotBinding,
) -> bool:
    return (
        reference.source_store_id == binding.source_store_id == record.source_store_id
        and reference.source_snapshot_id == binding.source_snapshot_id
        and reference.source_audit_id == binding.source_audit_id
        and reference.source_high_water_append_order
        == binding.high_water_append_order
        and reference.finding_set_digest == binding.finding_set_digest
        and reference.sequence_policy_id == binding.sequence_policy_id
        and reference.selection_id == binding.selection.selection_id
        and reference.event_id == record.event_id
        and reference.source_session_id == record.session_id
        and reference.symbol == record.symbol
        and reference.market is record.market
        and reference.event_type == record.event_type
        and reference.trading_day == record.trading_day
        and reference.session_label_policy_id == record.session_label_policy_id
        and reference.source_record_id == record.source_record_id
        and reference.source_append_order == record.append_order
        and reference.source_record_hash == record.record_content_hash
        and reference.raw_payload_sha256 == record.raw_payload_sha256
        and reference.parser_id == record.parser_id
        and reference.schema_id == record.source_schema_id
        and reference.source_time == record.source_time
        and reference.received_at == record.received_at
        and reference.durable_known_at == record.durable_known_at
    )


@dataclass(frozen=True, slots=True, init=False)
class RuntimeFrozenPathPrefix:
    case_id: str
    symbol: str
    market: Market
    path_store_snapshot: RuntimePathStoreSnapshotBinding
    case_selection: RuntimeCaseFactSelection
    market_source_snapshot: RuntimeMarketSourceSnapshotBinding
    authority_fact_selections: tuple[RuntimeAuthorityFactSelection, ...]
    frozen_at: datetime
    facts: tuple[RuntimeFrozenPathFact, ...]
    prefix_id: str

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeFrozenPathPrefix requires a verified case selection"
        )

    @classmethod
    def from_verified_case_selection(
        cls,
        *,
        case_selection: RuntimeCaseFactSelection,
        market_source_snapshot: RuntimeMarketSourceSnapshotBinding,
        authority_fact_selections: tuple[RuntimeAuthorityFactSelection, ...],
        frozen_at: datetime,
    ) -> RuntimeFrozenPathPrefix:
        if type(case_selection) is not RuntimeCaseFactSelection:
            raise RuntimePathContractError(
                "case_selection must be RuntimeCaseFactSelection"
            )
        case_selection.verify()
        if type(market_source_snapshot) is not RuntimeMarketSourceSnapshotBinding:
            raise RuntimePathContractError(
                "market_source_snapshot must be a verified source Selection"
            )
        market_source_snapshot.verify()
        if (
            market_source_snapshot.selection.symbol != case_selection.symbol
            or market_source_snapshot.selection.market is not case_selection.market
            or "TRADE_TICK"
            not in market_source_snapshot.selection.allowed_event_types
        ):
            raise RuntimePathContractError(
                "market source Selection does not cover the frozen case identity"
            )
        if type(authority_fact_selections) is not tuple or any(
            type(item) is not RuntimeAuthorityFactSelection
            for item in authority_fact_selections
        ):
            raise RuntimePathContractError(
                "authority_fact_selections must be an exact tuple"
            )
        ordered_selections = tuple(
            sorted(
                authority_fact_selections,
                key=lambda item: (
                    item.snapshot.authority_kind.value,
                    item.snapshot.authority_store_id,
                    item.snapshot.authority_audit_id,
                ),
            )
        )
        if ordered_selections != authority_fact_selections or len(
            {item.snapshot.authority_kind for item in ordered_selections}
        ) != len(ordered_selections):
            raise RuntimePathContractError(
                "authority selections must be canonical and unique by kind"
            )
        for selection in authority_fact_selections:
            selection.verify()
        frozen_at = _require_utc(frozen_at, "frozen_at")
        facts = case_selection.facts
        collection_orders = tuple(item.collection_append_order for item in facts)
        if collection_orders != tuple(sorted(collection_orders)) or len(
            set(collection_orders)
        ) != len(collection_orders):
            raise RuntimePathContractError(
                "frozen path facts must use unique increasing collection order"
            )
        if collection_orders and collection_orders[-1] > (
            case_selection.path_store_snapshot.global_high_water_append_order
        ):
            raise RuntimePathContractError(
                "case fact exceeds path-store high-water mark"
            )
        if any(
            item.collection_store_id
            != case_selection.path_store_snapshot.path_store_id
            or item.case_id != case_selection.case_id
            or item.symbol != case_selection.symbol
            or item.market is not case_selection.market
            or item.collection_observed_at > frozen_at
            for item in facts
        ):
            raise RuntimePathContractError(
                "path prefix fact identity or knowledge time is inconsistent"
            )
        if case_selection.path_store_snapshot.path_audited_at > frozen_at:
            raise RuntimePathContractError(
                "path-store audit is future-known at prefix freeze time"
            )
        authority_selections = {
            item.snapshot.authority_kind: item
            for item in authority_fact_selections
        }
        used_authority_kinds: set[RuntimeAuthorityKind] = set()
        sessions_by_index: dict[int, RuntimeSessionEvidence] = {}
        for fact in facts:
            if fact.session_evidence is None:
                continue
            session = fact.session_evidence
            if session.open_session_index is not None:
                sessions_by_index[session.open_session_index] = session
            for reference in (
                session.calendar_reference,
                session.security_status_reference,
                session.coverage_reference,
            ):
                if reference is None:
                    continue
                used_authority_kinds.add(reference.authority_kind)
                selection = authority_selections.get(reference.authority_kind)
                if (
                    selection is None
                    or reference.authority_store_id
                    != selection.snapshot.authority_store_id
                    or reference.revision
                    > selection.snapshot.high_water_revision
                    or not selection.contains(reference)
                ):
                    raise RuntimePathContractError(
                        "authority fact is absent from its verified store selection"
                    )
                if (
                    reference.known_at > frozen_at
                    or reference.usable_from > frozen_at
                    or selection.snapshot.authority_audited_at > frozen_at
                ):
                    raise RuntimePathContractError(
                        "authority fact or audit was future-known at prefix freeze time"
                    )
        if set(authority_selections) != used_authority_kinds:
            raise RuntimePathContractError(
                "authority selections do not match the referenced authority set"
            )

        selected_records = {
            item.source_record_id: item
            for item in market_source_snapshot.selection.records
        }
        for fact in facts:
            observation = fact.path_observation
            if observation is None:
                continue
            source_reference = observation.source_reference
            if source_reference is not None:
                record = selected_records.get(source_reference.source_record_id)
                session = sessions_by_index.get(observation.open_session_index)
                if (
                    record is None
                    or session is None
                    or record.trading_day != session.trading_day
                    or not _source_reference_matches_record(
                    source_reference,
                    record,
                    market_source_snapshot,
                    )
                ):
                    raise RuntimePathContractError(
                        "path point source member is absent from verified Selection"
                    )
                continue
            projection = observation.projection_artifact_reference
            assert projection is not None
            lineage = projection.lineage
            if (
                lineage.source_store_id
                != market_source_snapshot.source_store_id
                or lineage.source_snapshot_id
                != market_source_snapshot.source_snapshot_id
                or lineage.source_audit_id
                != market_source_snapshot.source_audit_id
                or lineage.source_high_water_append_order
                != market_source_snapshot.high_water_append_order
                or lineage.finding_set_digest
                != market_source_snapshot.finding_set_digest
                or lineage.selection_id
                != market_source_snapshot.selection.selection_id
                or lineage.selection_verification_id
                != market_source_snapshot.selection_verification.verification_id
            ):
                raise RuntimePathContractError(
                    "projection lineage disagrees with frozen market source snapshot"
                )
            session = sessions_by_index.get(observation.open_session_index)
            if (
                session is None
                or session.coverage_reference is None
                or lineage.coverage_fact_id != session.coverage_reference.fact_id
                or projection.trading_day != session.trading_day
            ):
                raise RuntimePathContractError(
                    "projection lineage is not bound to session coverage authority"
                )
            for event_id, append_order, record_id, record_hash in zip(
                lineage.input_event_ids,
                lineage.input_source_append_orders,
                lineage.input_source_record_ids,
                lineage.input_source_record_hashes,
                strict=True,
            ):
                record = selected_records.get(record_id)
                if record is None or (
                    record.event_id != event_id
                    or record.append_order != append_order
                    or record.record_content_hash != record_hash
                    or record.symbol != observation.symbol
                    or record.market is not observation.market
                    or record.event_type != "TRADE_TICK"
                    or record.trading_day != session.trading_day
                    or record.durable_known_at > projection.created_at
                    or not projection.interval_start
                    <= record.source_time
                    < projection.interval_end
                ):
                    raise RuntimePathContractError(
                        "projection input is absent from the verified raw Selection"
                    )
        if any(
            session.coverage_state is RuntimeCoverageState.COMPLETE_SESSION
            for session in sessions_by_index.values()
        ):
            if any(
                not manifest.has_sequence_start_proof
                for manifest in market_source_snapshot.selection.source_session_manifests
            ):
                raise RuntimePathContractError(
                    "COMPLETE_SESSION requires source sequence start capability proof"
                )
            if market_source_snapshot.selection.relevant_findings:
                raise RuntimePathContractError(
                    "COMPLETE_SESSION conflicts with verified source sequence findings"
                )
        selection = market_source_snapshot.selection
        for session in sessions_by_index.values():
            if (
                session.scheduled_open_at is None
                or session.coverage_through is None
                or session.coverage_state
                not in {
                    RuntimeCoverageState.COMPLETE_PREFIX,
                    RuntimeCoverageState.COMPLETE_SESSION,
                }
            ):
                continue
            if (
                selection.start_source_time > session.scheduled_open_at
                or selection.end_source_time <= session.coverage_through
                or selection.snapshot_audit_id
                != market_source_snapshot.source_audit_id
            ):
                raise RuntimePathContractError(
                    "market source Selection does not cover the claimed session interval"
                )
        for values, label in (
            (
                tuple(item.collection_fact_id for item in facts),
                "collection fact",
            ),
            (tuple(item.frozen_fact_id for item in facts), "frozen fact"),
        ):
            if len(values) != len(set(values)):
                raise RuntimePathContractError(
                    f"path prefix contains duplicate {label} identities"
                )
        session_days = tuple(
            item.session_evidence.trading_day
            for item in facts
            if item.session_evidence is not None
        )
        if len(session_days) != len(set(session_days)):
            raise RuntimePathContractError(
                "path prefix contains duplicate calendar-day evidence"
            )
        point_ids = tuple(
            item.path_observation.path_observation_id
            for item in facts
            if item.path_observation is not None
        )
        if len(point_ids) != len(set(point_ids)):
            raise RuntimePathContractError(
                "path prefix contains duplicate path observations"
            )
        self = object.__new__(cls)
        for name, value in {
            "case_id": case_selection.case_id,
            "symbol": case_selection.symbol,
            "market": case_selection.market,
            "path_store_snapshot": case_selection.path_store_snapshot,
            "case_selection": case_selection,
            "market_source_snapshot": market_source_snapshot,
            "authority_fact_selections": authority_fact_selections,
            "frozen_at": frozen_at,
            "facts": facts,
        }.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "prefix_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_PATH_PREFIX_SCHEMA,
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "path_store_snapshot": self.path_store_snapshot.as_dict(),
            "case_selection_id": self.case_selection.selection_id,
            "market_source_snapshot": self.market_source_snapshot.as_dict(),
            "authority_fact_selections": [
                item.as_dict() for item in self.authority_fact_selections
            ],
            "frozen_at": _utc_text(self.frozen_at),
            "facts": [item.as_dict() for item in self.facts],
        }
        if include_id:
            document["prefix_id"] = self.prefix_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathWindow:
    case_id: str
    symbol: str
    market: Market
    entry_filled_at: datetime
    entry_trading_day: date
    entry_session_index: int
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    horizon_sessions: int
    terminal_policy_id: str
    session_label_policy_id: str
    interval_boundary_policy_id: str
    window_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.case_id, "case_id")
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        entry_filled_at = _require_utc(self.entry_filled_at, "entry_filled_at")
        object.__setattr__(self, "entry_filled_at", entry_filled_at)
        _require_date(self.entry_trading_day, "entry_trading_day")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported market session label policy"
            )
        if self.interval_boundary_policy_id != RUNTIME_INTERVAL_BOUNDARY_POLICY_V1:
            raise RuntimePathContractError(
                "unsupported runtime interval boundary policy"
            )
        if market_session_date(
            entry_filled_at,
            market,
            self.session_label_policy_id,
        ) != self.entry_trading_day:
            raise RuntimePathContractError(
                "entry_trading_day disagrees with market-local entry time"
            )
        _require_int(self.entry_session_index, "entry_session_index")
        entry = _require_decimal(self.entry_price, "entry_price", positive=True)
        target = _require_decimal(self.target_price, "target_price", positive=True)
        stop = _require_decimal(self.stop_price, "stop_price", positive=True)
        if not stop < entry < target:
            raise RuntimePathContractError(
                "path window requires stop_price < entry_price < target_price"
            )
        _require_int(self.horizon_sessions, "horizon_sessions", minimum=1)
        _require_sha256(self.terminal_policy_id, "terminal_policy_id")
        object.__setattr__(
            self,
            "window_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def horizon_session_index(self) -> int:
        return self.entry_session_index + self.horizon_sessions

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-path-window-v2",
            "case_id": self.case_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "entry_filled_at": _utc_text(self.entry_filled_at),
            "entry_trading_day": self.entry_trading_day.isoformat(),
            "entry_session_index": self.entry_session_index,
            "entry_price": _decimal_text(self.entry_price),
            "target_price": _decimal_text(self.target_price),
            "stop_price": _decimal_text(self.stop_price),
            "horizon_sessions": self.horizon_sessions,
            "terminal_policy_id": self.terminal_policy_id,
            "session_label_policy_id": self.session_label_policy_id,
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
        }
        if include_id:
            document["window_id"] = self.window_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimePathResolution:
    state: RuntimePathResolutionState
    window_id: str
    prefix_id: str
    terminal_reason: RuntimePathTerminalReason | None
    pending_code: RuntimePathPendingCode | None
    blocker_codes: tuple[RuntimePathBlockerCode, ...]
    first_touch_collection_fact_id: str | None
    first_touch_observation_id: str | None
    effective_session_index: int | None
    resolution_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.state) is not RuntimePathResolutionState:
            raise RuntimePathContractError(
                "state must be RuntimePathResolutionState"
            )
        _require_sha256(self.window_id, "window_id")
        _require_sha256(self.prefix_id, "prefix_id")
        if self.terminal_reason is not None and type(
            self.terminal_reason
        ) is not RuntimePathTerminalReason:
            raise RuntimePathContractError(
                "terminal_reason must be RuntimePathTerminalReason or None"
            )
        if self.pending_code is not None and type(
            self.pending_code
        ) is not RuntimePathPendingCode:
            raise RuntimePathContractError(
                "pending_code must be RuntimePathPendingCode or None"
            )
        if type(self.blocker_codes) is not tuple or any(
            type(item) is not RuntimePathBlockerCode for item in self.blocker_codes
        ):
            raise RuntimePathContractError(
                "blocker_codes must be a tuple of RuntimePathBlockerCode"
            )
        if tuple(sorted(set(self.blocker_codes), key=lambda item: item.value)) != self.blocker_codes:
            raise RuntimePathContractError(
                "blocker_codes must be unique and canonically ordered"
            )
        first_fact = (
            None
            if self.first_touch_collection_fact_id is None
            else _require_sha256(
                self.first_touch_collection_fact_id,
                "first_touch_collection_fact_id",
            )
        )
        first_observation = (
            None
            if self.first_touch_observation_id is None
            else _require_sha256(
                self.first_touch_observation_id,
                "first_touch_observation_id",
            )
        )
        if (first_fact is None) != (first_observation is None):
            raise RuntimePathContractError(
                "first-touch fact and observation identities must be paired"
            )
        if self.effective_session_index is not None:
            _require_int(
                self.effective_session_index,
                "effective_session_index",
            )
        terminal_states = {
            RuntimePathResolutionState.TARGET: RuntimePathTerminalReason.TARGET,
            RuntimePathResolutionState.STOP: RuntimePathTerminalReason.STOP,
            RuntimePathResolutionState.TIMEOUT: RuntimePathTerminalReason.TIMEOUT,
        }
        if self.state in terminal_states:
            if (
                self.terminal_reason is not terminal_states[self.state]
                or self.pending_code is not None
                or self.blocker_codes
                or self.effective_session_index is None
            ):
                raise RuntimePathContractError(
                    "terminal resolution fields are inconsistent"
                )
            if self.state in {
                RuntimePathResolutionState.TARGET,
                RuntimePathResolutionState.STOP,
            } and first_fact is None:
                raise RuntimePathContractError(
                    "barrier resolution requires first-touch evidence"
                )
            if self.state is RuntimePathResolutionState.TIMEOUT and first_fact is not None:
                raise RuntimePathContractError(
                    "TIMEOUT cannot claim first-touch evidence"
                )
        elif self.state is RuntimePathResolutionState.OPEN:
            if (
                self.terminal_reason is not None
                or self.pending_code is not RuntimePathPendingCode.HORIZON_NOT_REACHED
                or self.blocker_codes
                or first_fact is not None
            ):
                raise RuntimePathContractError("OPEN resolution fields are inconsistent")
        else:
            if (
                self.terminal_reason is not None
                or self.pending_code is not None
                or not self.blocker_codes
                or first_fact is not None
            ):
                raise RuntimePathContractError(
                    "BLOCKED resolution fields are inconsistent"
                )
        object.__setattr__(
            self,
            "resolution_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_PATH_RESOLUTION_SCHEMA,
            "state": self.state.value,
            "window_id": self.window_id,
            "prefix_id": self.prefix_id,
            "terminal_reason": (
                None if self.terminal_reason is None else self.terminal_reason.value
            ),
            "pending_code": (
                None if self.pending_code is None else self.pending_code.value
            ),
            "blocker_codes": [item.value for item in self.blocker_codes],
            "first_touch_collection_fact_id": self.first_touch_collection_fact_id,
            "first_touch_observation_id": self.first_touch_observation_id,
            "effective_session_index": self.effective_session_index,
        }
        if include_id:
            document["resolution_id"] = self.resolution_id
        return document


def _blocked_resolution(
    window: RuntimePathWindow,
    prefix: RuntimeFrozenPathPrefix,
    *codes: RuntimePathBlockerCode,
    effective_session_index: int | None = None,
) -> RuntimePathResolution:
    canonical = tuple(sorted(set(codes), key=lambda item: item.value))
    return RuntimePathResolution(
        state=RuntimePathResolutionState.BLOCKED,
        window_id=window.window_id,
        prefix_id=prefix.prefix_id,
        terminal_reason=None,
        pending_code=None,
        blocker_codes=canonical,
        first_touch_collection_fact_id=None,
        first_touch_observation_id=None,
        effective_session_index=effective_session_index,
    )


def _coverage_blocker(
    session: RuntimeSessionEvidence,
) -> RuntimePathBlockerCode | None:
    if session.open_session_state is RuntimeOpenSessionState.MISSING_DATA:
        return RuntimePathBlockerCode.MISSING_DATA
    mapping = {
        RuntimeCoverageState.INCOMPLETE_GAP: RuntimePathBlockerCode.SESSION_GAP,
        RuntimeCoverageState.INCOMPLETE_OUT_OF_ORDER: RuntimePathBlockerCode.SESSION_OUT_OF_ORDER,
        RuntimeCoverageState.INCOMPLETE_SPARSE: RuntimePathBlockerCode.SESSION_SPARSE,
    }
    return mapping.get(session.coverage_state)


def resolve_runtime_path(
    window: RuntimePathWindow,
    prefix: RuntimeFrozenPathPrefix,
) -> RuntimePathResolution:
    """Resolve first-touch TARGET/STOP/TIMEOUT from one immutable request-time prefix.

    This resolver never looks outside ``prefix``.  A later source record with an
    earlier market timestamp therefore cannot rewrite the result for an already
    frozen request.
    """

    if type(window) is not RuntimePathWindow:
        raise RuntimePathContractError("window must be RuntimePathWindow")
    if type(prefix) is not RuntimeFrozenPathPrefix:
        raise RuntimePathContractError("prefix must be RuntimeFrozenPathPrefix")
    if (
        prefix.case_id != window.case_id
        or prefix.symbol != window.symbol
        or prefix.market is not window.market
    ):
        raise RuntimePathContractError(
            "path window and frozen prefix identity are inconsistent",
            code="PATH_PREFIX_IDENTITY_MISMATCH",
        )

    session_facts = tuple(
        item for item in prefix.facts if item.kind is RuntimePathFactKind.SESSION
    )
    point_facts = tuple(
        item for item in prefix.facts if item.kind is RuntimePathFactKind.POINT
    )
    sessions = tuple(
        sorted(
            (item.session_evidence for item in session_facts),
            key=lambda item: item.trading_day if item is not None else date.min,
        )
    )
    sessions = tuple(item for item in sessions if item is not None)
    horizon_index = window.horizon_session_index
    all_relevant_sessions = tuple(
        item for item in sessions if item.trading_day >= window.entry_trading_day
    )
    bounded_sessions: list[RuntimeSessionEvidence] = []
    for session in all_relevant_sessions:
        bounded_sessions.append(session)
        if (
            session.calendar_state is RuntimeCalendarState.OPEN
            and session.open_session_index == horizon_index
        ):
            break
    relevant_sessions = tuple(bounded_sessions)
    entry_session = next(
        (
            item
            for item in relevant_sessions
            if item.trading_day == window.entry_trading_day
        ),
        None,
    )
    if entry_session is None:
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_MISSING,
        )
    if (
        entry_session.calendar_state is not RuntimeCalendarState.OPEN
        or entry_session.open_session_index != window.entry_session_index
    ):
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_MISMATCH,
        )
    if entry_session.open_session_state is not RuntimeOpenSessionState.TRADED:
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_SESSION_NOT_TRADED,
            effective_session_index=window.entry_session_index,
        )
    if (
        entry_session.scheduled_open_at is None
        or entry_session.scheduled_close_at is None
        or not (
            entry_session.scheduled_open_at
            <= window.entry_filled_at
            <= entry_session.scheduled_close_at
        )
    ):
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.ENTRY_TIME_OUTSIDE_SESSION_WINDOW,
            effective_session_index=window.entry_session_index,
        )

    for previous, current in pairwise(relevant_sessions):
        if current.trading_day != previous.trading_day + timedelta(days=1):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.CALENDAR_COVERAGE_GAP,
            )

    open_sessions = tuple(
        item
        for item in relevant_sessions
        if item.calendar_state is RuntimeCalendarState.OPEN
    )
    expected_session_index = window.entry_session_index
    for session in open_sessions:
        if session.open_session_index != expected_session_index:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.OPEN_SESSION_INDEX_GAP,
                effective_session_index=expected_session_index,
            )
        expected_session_index += 1

    session_by_index = {
        item.open_session_index: item
        for item in open_sessions
        if item.open_session_index is not None
    }
    points_by_session: dict[int, list[RuntimeFrozenPathFact]] = {}
    for fact in point_facts:
        observation = fact.path_observation
        assert observation is not None
        index = observation.open_session_index
        if index > horizon_index:
            continue
        session = session_by_index.get(index)
        if session is None:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_WITHOUT_SESSION,
                effective_session_index=index,
            )
        if session.open_session_state is not RuntimeOpenSessionState.TRADED:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.NONTRADED_SESSION_WITH_POINTS,
                effective_session_index=index,
            )
        if (
            session.scheduled_open_at is None
            or session.scheduled_close_at is None
            or observation.interval_start < session.scheduled_open_at
            or observation.interval_end > session.scheduled_close_at
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_OUTSIDE_SESSION_WINDOW,
                effective_session_index=index,
            )
        if (
            session.coverage_state
            in {
                RuntimeCoverageState.COMPLETE_PREFIX,
                RuntimeCoverageState.COMPLETE_SESSION,
            }
            and (
                session.coverage_through is None
                or observation.interval_end > session.coverage_through
            )
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_BEYOND_COVERAGE,
                effective_session_index=index,
            )
        if index < window.entry_session_index:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.POINT_BEFORE_ENTRY_WINDOW,
                effective_session_index=index,
            )
        if index == window.entry_session_index:
            if (
                observation.granularity is RuntimePathGranularity.TICK
                and observation.interval_start == window.entry_filled_at
            ):
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,
                    effective_session_index=index,
                )
            if observation.interval_end <= window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.POINT_BEFORE_ENTRY_WINDOW,
                    effective_session_index=index,
                )
            if observation.interval_start <= window.entry_filled_at:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,
                    effective_session_index=index,
                )
        points_by_session.setdefault(index, []).append(fact)

    max_known_open_index = (
        max(session_by_index) if session_by_index else window.entry_session_index - 1
    )
    for session_index in range(
        window.entry_session_index,
        min(max_known_open_index, horizon_index) + 1,
    ):
        session = session_by_index.get(session_index)
        if session is None:
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.OPEN_SESSION_INDEX_GAP,
                effective_session_index=session_index,
            )
        blocker = _coverage_blocker(session)
        if blocker is not None:
            return _blocked_resolution(
                window,
                prefix,
                blocker,
                effective_session_index=session_index,
            )
        if (
            session_index < max_known_open_index
            and session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
        ):
            return _blocked_resolution(
                window,
                prefix,
                RuntimePathBlockerCode.SESSION_PREFIX_NOT_CLOSED,
                effective_session_index=session_index,
            )
        facts = points_by_session.get(session_index, [])
        if session.open_session_state is RuntimeOpenSessionState.TRADED and not facts:
            eligible_source_records = tuple(
                record
                for record in prefix.market_source_snapshot.selection.records
                if record.symbol == window.symbol
                and record.market is window.market
                and record.event_type == "TRADE_TICK"
                and record.trading_day == session.trading_day
                and (
                    session_index != window.entry_session_index
                    or record.source_time > window.entry_filled_at
                )
            )
            if eligible_source_records:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.TRADED_SESSION_WITHOUT_POINTS,
                    effective_session_index=session_index,
                )
            if session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX:
                return RuntimePathResolution(
                    state=RuntimePathResolutionState.OPEN,
                    window_id=window.window_id,
                    prefix_id=prefix.prefix_id,
                    terminal_reason=None,
                    pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
                    blocker_codes=(),
                    first_touch_collection_fact_id=None,
                    first_touch_observation_id=None,
                    effective_session_index=session_index,
                )
        def point_order_key(
            item: RuntimeFrozenPathFact,
        ) -> tuple[datetime, datetime, int, str]:
            observation = item.path_observation
            if observation is None:
                raise RuntimePathContractError(
                    "point collection contains a non-point fact"
                )
            source_order = (
                observation.source_reference.source_append_order
                if observation.source_reference is not None
                else observation.projection_artifact_reference.lineage.input_source_append_orders[
                    0
                ]
                if observation.projection_artifact_reference is not None
                else 0
            )
            return (
                observation.interval_start,
                observation.interval_end,
                source_order,
                observation.path_observation_id,
            )

        ordered = sorted(facts, key=point_order_key)
        previous_end: datetime | None = None
        for fact in ordered:
            observation = fact.path_observation
            assert observation is not None
            if previous_end is not None and observation.interval_start < previous_end:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.OVERLAPPING_PATH_OBSERVATIONS,
                    effective_session_index=session_index,
                )
            previous_end = observation.interval_end
            target_touched = observation.high >= window.target_price
            stop_touched = observation.low <= window.stop_price
            if target_touched and stop_touched:
                return _blocked_resolution(
                    window,
                    prefix,
                    RuntimePathBlockerCode.INTRABAR_AMBIGUITY,
                    effective_session_index=session_index,
                )
            if target_touched or stop_touched:
                reason = (
                    RuntimePathTerminalReason.TARGET
                    if target_touched
                    else RuntimePathTerminalReason.STOP
                )
                state = (
                    RuntimePathResolutionState.TARGET
                    if target_touched
                    else RuntimePathResolutionState.STOP
                )
                return RuntimePathResolution(
                    state=state,
                    window_id=window.window_id,
                    prefix_id=prefix.prefix_id,
                    terminal_reason=reason,
                    pending_code=None,
                    blocker_codes=(),
                    first_touch_collection_fact_id=fact.collection_fact_id,
                    first_touch_observation_id=observation.path_observation_id,
                    effective_session_index=session_index,
                )

    horizon_session = session_by_index.get(horizon_index)
    if horizon_session is None:
        return RuntimePathResolution(
            state=RuntimePathResolutionState.OPEN,
            window_id=window.window_id,
            prefix_id=prefix.prefix_id,
            terminal_reason=None,
            pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
            blocker_codes=(),
            first_touch_collection_fact_id=None,
            first_touch_observation_id=None,
            effective_session_index=max_known_open_index,
        )
    if (
        not horizon_session.session_complete
        or horizon_session.coverage_state is not RuntimeCoverageState.COMPLETE_SESSION
    ):
        if (
            not horizon_session.session_complete
            and horizon_session.coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
        ):
            return RuntimePathResolution(
                state=RuntimePathResolutionState.OPEN,
                window_id=window.window_id,
                prefix_id=prefix.prefix_id,
                terminal_reason=None,
                pending_code=RuntimePathPendingCode.HORIZON_NOT_REACHED,
                blocker_codes=(),
                first_touch_collection_fact_id=None,
                first_touch_observation_id=None,
                effective_session_index=horizon_index,
            )
        return _blocked_resolution(
            window,
            prefix,
            RuntimePathBlockerCode.HORIZON_SESSION_INCOMPLETE,
            effective_session_index=horizon_index,
        )
    return RuntimePathResolution(
        state=RuntimePathResolutionState.TIMEOUT,
        window_id=window.window_id,
        prefix_id=prefix.prefix_id,
        terminal_reason=RuntimePathTerminalReason.TIMEOUT,
        pending_code=None,
        blocker_codes=(),
        first_touch_collection_fact_id=None,
        first_touch_observation_id=None,
        effective_session_index=horizon_index,
    )


@dataclass(frozen=True, slots=True)
class RuntimeExecutionFragment:
    intent_id: str
    execution_id: str
    source_fact_id: str
    source_callback_id: str
    source_append_order: int
    side: RuntimeExecutionSide
    timestamp: datetime
    durable_known_at: datetime
    quantity: int
    price: Decimal
    explicit_cost: Decimal
    fragment_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        _require_text(self.execution_id, "execution_id", maximum=256)
        _require_sha256(self.source_fact_id, "source_fact_id")
        _require_sha256(self.source_callback_id, "source_callback_id")
        _require_int(self.source_append_order, "source_append_order", minimum=1)
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError("side must be the exact RuntimeExecutionSide type")
        timestamp = _require_utc(self.timestamp, "execution timestamp")
        known_at = _require_utc(self.durable_known_at, "execution durable_known_at")
        if timestamp > known_at:
            raise RuntimePathContractError(
                "execution timestamp cannot exceed durable_known_at"
            )
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "durable_known_at", known_at)
        _require_int(self.quantity, "execution quantity", minimum=1)
        _require_decimal(self.price, "execution price", positive=True)
        _require_decimal(self.explicit_cost, "explicit_cost", nonnegative=True)
        object.__setattr__(
            self,
            "fragment_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-execution-fragment-v2",
            "intent_id": self.intent_id,
            "execution_id": self.execution_id,
            "source_fact_id": self.source_fact_id,
            "source_callback_id": self.source_callback_id,
            "source_append_order": self.source_append_order,
            "side": self.side.value,
            "timestamp": _utc_text(self.timestamp),
            "durable_known_at": _utc_text(self.durable_known_at),
            "quantity": self.quantity,
            "price": _decimal_text(self.price),
            "explicit_cost": _decimal_text(self.explicit_cost),
        }
        if include_id:
            document["fragment_id"] = self.fragment_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeExecutionSummary:
    intent_id: str
    side: RuntimeExecutionSide
    requested_quantity: int
    fragments: tuple[RuntimeExecutionFragment, ...]
    execution_source_store_id: str
    execution_snapshot_id: str
    execution_audit_id: str
    execution_audited_at: datetime
    execution_high_water_append_order: int
    execution_selection_id: str
    membership_verification_id: str
    execution_stream_complete: bool
    native_multi_leg: bool = False
    completion: RuntimeFillCompletion = field(init=False)
    filled_quantity: int = field(init=False)
    quantity_weighted_price: Decimal | None = field(init=False)
    total_explicit_cost: Decimal = field(init=False)
    summary_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        if type(self.side) is not RuntimeExecutionSide:
            raise RuntimePathContractError("side must be the exact RuntimeExecutionSide type")
        _require_int(self.requested_quantity, "requested_quantity", minimum=1)
        for name in (
            "execution_source_store_id",
            "execution_snapshot_id",
            "execution_audit_id",
            "execution_selection_id",
            "membership_verification_id",
        ):
            _require_sha256(getattr(self, name), name)
        execution_audited_at = _require_utc(
            self.execution_audited_at,
            "execution_audited_at",
        )
        object.__setattr__(self, "execution_audited_at", execution_audited_at)
        high_water = _require_int(
            self.execution_high_water_append_order,
            "execution_high_water_append_order",
        )
        if type(self.fragments) is not tuple or any(
            type(item) is not RuntimeExecutionFragment for item in self.fragments
        ):
            raise RuntimePathContractError(
                "fragments must be an exact tuple of RuntimeExecutionFragment"
            )
        _require_bool(self.execution_stream_complete, "execution_stream_complete")
        if _require_bool(self.native_multi_leg, "native_multi_leg"):
            raise RuntimePathContractError(
                "native multi-leg lifecycle is unsupported in Stage 4G v3",
                code="NATIVE_MULTI_LEG_UNSUPPORTED",
            )
        if any(
            item.intent_id != self.intent_id or item.side is not self.side
            for item in self.fragments
        ):
            raise RuntimePathContractError(
                "execution fragments must belong to one intent and side"
            )
        ordered = tuple(
            sorted(
                self.fragments,
                key=lambda item: (
                    item.timestamp,
                    item.execution_id,
                    item.fragment_id,
                ),
            )
        )
        if len({item.execution_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError("execution IDs must be unique")
        if len({item.source_fact_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError(
                "one execution source fact may produce only one fragment"
            )
        if len({item.source_callback_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError(
                "one execution callback may produce only one fragment"
            )
        if any(
            item.source_append_order > high_water
            or item.durable_known_at > execution_audited_at
            for item in ordered
        ):
            raise RuntimePathContractError(
                "execution fragment is outside the frozen execution audit"
            )
        if len({item.fragment_id for item in ordered}) != len(ordered):
            raise RuntimePathContractError("execution fragments must be unique")
        object.__setattr__(self, "fragments", ordered)
        filled_quantity = sum(item.quantity for item in ordered)
        if filled_quantity > self.requested_quantity:
            raise RuntimePathContractError(
                "filled quantity exceeds requested quantity"
            )
        with localcontext(_DECIMAL_CONTEXT):
            total_explicit_cost = sum(
                (item.explicit_cost for item in ordered),
                Decimal(0),
            )
            weighted_price = (
                None
                if filled_quantity == 0
                else sum(
                    (item.price * Decimal(item.quantity) for item in ordered),
                    Decimal(0),
                )
                / Decimal(filled_quantity)
            )
        completion = (
            RuntimeFillCompletion.ZERO_FILL
            if filled_quantity == 0
            else (
                RuntimeFillCompletion.COMPLETE
                if filled_quantity == self.requested_quantity
                and self.execution_stream_complete
                else RuntimeFillCompletion.PARTIAL
            )
        )
        object.__setattr__(self, "filled_quantity", filled_quantity)
        object.__setattr__(self, "quantity_weighted_price", weighted_price)
        object.__setattr__(self, "total_explicit_cost", total_explicit_cost)
        object.__setattr__(self, "completion", completion)
        object.__setattr__(
            self,
            "summary_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def structurally_projectable_to_stage4g_v3(self) -> bool:
        return self.completion is RuntimeFillCompletion.COMPLETE

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_EXECUTION_SUMMARY_SCHEMA,
            "intent_id": self.intent_id,
            "side": self.side.value,
            "requested_quantity": self.requested_quantity,
            "fragments": [item.as_dict() for item in self.fragments],
            "execution_source_store_id": self.execution_source_store_id,
            "execution_snapshot_id": self.execution_snapshot_id,
            "execution_audit_id": self.execution_audit_id,
            "execution_audited_at": _utc_text(self.execution_audited_at),
            "execution_high_water_append_order": self.execution_high_water_append_order,
            "execution_selection_id": self.execution_selection_id,
            "membership_verification_id": self.membership_verification_id,
            "execution_stream_complete": self.execution_stream_complete,
            "native_multi_leg": False,
            "completion": self.completion.value,
            "filled_quantity": self.filled_quantity,
            "quantity_weighted_price": (
                None
                if self.quantity_weighted_price is None
                else _decimal_text(self.quantity_weighted_price)
            ),
            "total_explicit_cost": _decimal_text(self.total_explicit_cost),
            "structurally_projectable_to_stage4g_v3": (
                self.structurally_projectable_to_stage4g_v3
            ),
        }
        if include_id:
            document["summary_id"] = self.summary_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeExecutionEvidenceReference:
    fact_id: str
    fact_kind: str
    source_append_order: int
    known_at: datetime
    membership_verification_id: str
    reference_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.fact_id, "fact_id")
        _require_text(self.fact_kind, "fact_kind", maximum=128)
        _require_int(self.source_append_order, "source_append_order", minimum=1)
        known_at = _require_utc(self.known_at, "known_at")
        object.__setattr__(self, "known_at", known_at)
        _require_sha256(
            self.membership_verification_id,
            "membership_verification_id",
        )
        object.__setattr__(
            self,
            "reference_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-runtime-execution-evidence-reference-v1",
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind,
            "source_append_order": self.source_append_order,
            "known_at": _utc_text(self.known_at),
            "membership_verification_id": self.membership_verification_id,
        }
        if include_id:
            document["reference_id"] = self.reference_id
        return document


@dataclass(frozen=True, slots=True)
class RuntimeNoEntryEvidence:
    reason: RuntimeNoEntryReason
    execution_summary: RuntimeExecutionSummary
    entry_window_start: datetime
    entry_window_end: datetime
    decided_at: datetime
    execution_policy_id: str
    evidence_references: tuple[RuntimeExecutionEvidenceReference, ...]
    evidence_ids: tuple[str, ...]
    entry_window_coverage_complete: bool
    entry_window_coverage_fact_id: str | None = None
    actor_id: str | None = None
    actor_authentication_fact_id: str | None = None
    cancellation_fact_id: str | None = None
    rejection_fact_id: str | None = None
    data_invalidity_fact_id: str | None = None
    no_entry_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.reason) is not RuntimeNoEntryReason:
            raise RuntimePathContractError("reason must be RuntimeNoEntryReason")
        if type(self.execution_summary) is not RuntimeExecutionSummary:
            raise RuntimePathContractError(
                "execution_summary must be RuntimeExecutionSummary"
            )
        if (
            self.execution_summary.completion is not RuntimeFillCompletion.ZERO_FILL
            or not self.execution_summary.execution_stream_complete
        ):
            raise RuntimePathContractError(
                "no-entry evidence requires a complete zero-fill execution summary"
            )
        window_start = _require_utc(self.entry_window_start, "entry_window_start")
        window_end = _require_utc(self.entry_window_end, "entry_window_end")
        decided_at = _require_utc(self.decided_at, "decided_at")
        if window_end < window_start or decided_at < window_start:
            raise RuntimePathContractError("no-entry window times are inconsistent")
        object.__setattr__(self, "entry_window_start", window_start)
        object.__setattr__(self, "entry_window_end", window_end)
        object.__setattr__(self, "decided_at", decided_at)
        _require_sha256(self.execution_policy_id, "execution_policy_id")
        if type(self.evidence_references) is not tuple or any(
            type(item) is not RuntimeExecutionEvidenceReference
            for item in self.evidence_references
        ):
            raise RuntimePathContractError(
                "evidence_references must be an exact tuple"
            )
        references = tuple(
            sorted(
                self.evidence_references,
                key=lambda item: (item.source_append_order, item.fact_id),
            )
        )
        if len({item.fact_id for item in references}) != len(references):
            raise RuntimePathContractError(
                "execution evidence fact identities must be unique"
            )
        if any(
            item.membership_verification_id
            != self.execution_summary.membership_verification_id
            or item.source_append_order
            > self.execution_summary.execution_high_water_append_order
            or item.known_at > decided_at
            for item in references
        ) or self.execution_summary.execution_audited_at > decided_at:
            raise RuntimePathContractError(
                "no-entry decision predates its execution evidence or audit"
            )
        object.__setattr__(self, "evidence_references", references)
        if type(self.evidence_ids) is not tuple:
            raise RuntimePathContractError("evidence_ids must be an exact tuple")
        evidence_ids = tuple(
            sorted(_require_sha256(item, "evidence_id") for item in self.evidence_ids)
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise RuntimePathContractError("evidence_ids must be unique")
        if evidence_ids != tuple(sorted(item.fact_id for item in references)):
            raise RuntimePathContractError(
                "evidence_ids must equal the verified evidence references"
            )
        object.__setattr__(self, "evidence_ids", evidence_ids)
        coverage_complete = _require_bool(
            self.entry_window_coverage_complete,
            "entry_window_coverage_complete",
        )
        coverage_fact = (
            None
            if self.entry_window_coverage_fact_id is None
            else _require_sha256(
                self.entry_window_coverage_fact_id,
                "entry_window_coverage_fact_id",
            )
        )
        if coverage_complete and coverage_fact is None:
            raise RuntimePathContractError(
                "complete entry-window coverage requires an evidence fact"
            )
        actor_id = _require_optional_text(self.actor_id, "actor_id")
        actor_authentication = (
            None
            if self.actor_authentication_fact_id is None
            else _require_sha256(
                self.actor_authentication_fact_id,
                "actor_authentication_fact_id",
            )
        )
        cancellation = (
            None
            if self.cancellation_fact_id is None
            else _require_sha256(self.cancellation_fact_id, "cancellation_fact_id")
        )
        rejection = (
            None
            if self.rejection_fact_id is None
            else _require_sha256(self.rejection_fact_id, "rejection_fact_id")
        )
        invalidity = (
            None
            if self.data_invalidity_fact_id is None
            else _require_sha256(
                self.data_invalidity_fact_id,
                "data_invalidity_fact_id",
            )
        )
        if self.reason is RuntimeNoEntryReason.ENTRY_EXPIRED:
            if (
                decided_at < window_end
                or not coverage_complete
                or coverage_fact is None
                or any(
                    value is not None
                    for value in (
                        actor_id,
                        actor_authentication,
                        cancellation,
                        rejection,
                        invalidity,
                    )
                )
            ):
                raise RuntimePathContractError(
                    "ENTRY_EXPIRED requires complete window coverage, zero fill, and window expiry"
                )
        elif self.reason is RuntimeNoEntryReason.USER_CANCELLED:
            if (
                actor_id is None
                or actor_authentication is None
                or cancellation is None
                or rejection is not None
                or invalidity is not None
            ):
                raise RuntimePathContractError(
                    "USER_CANCELLED requires authenticated actor and cancellation fact"
                )
        elif self.reason is RuntimeNoEntryReason.ORDER_REJECTED:
            if (
                rejection is None
                or actor_id is not None
                or actor_authentication is not None
                or cancellation is not None
                or invalidity is not None
            ):
                raise RuntimePathContractError(
                    "ORDER_REJECTED requires an explicit rejection fact"
                )
        elif (
            invalidity is None
            or actor_id is not None
            or actor_authentication is not None
            or cancellation is not None
            or rejection is not None
        ):
            raise RuntimePathContractError(
                "DATA_INVALID requires an explicit data-invalidity fact"
            )
        required_facts = tuple(
            item
            for item in (
                coverage_fact if coverage_complete else None,
                actor_authentication,
                cancellation,
                rejection,
                invalidity,
            )
            if item is not None
        )
        if any(item not in evidence_ids for item in required_facts):
            raise RuntimePathContractError(
                "reason-specific facts must be present in evidence_ids"
            )
        references_by_fact_id = {item.fact_id: item for item in references}
        expected_fact_kinds = {
            fact_id: fact_kind
            for fact_id, fact_kind in (
                (
                    coverage_fact if coverage_complete else None,
                    "ENTRY_WINDOW_COVERAGE",
                ),
                (actor_authentication, "ACTOR_AUTHENTICATION"),
                (cancellation, "USER_CANCELLATION"),
                (rejection, "ORDER_REJECTION"),
                (invalidity, "DATA_INVALIDITY"),
            )
            if fact_id is not None
        }
        if any(
            references_by_fact_id[fact_id].fact_kind != fact_kind
            for fact_id, fact_kind in expected_fact_kinds.items()
        ):
            raise RuntimePathContractError(
                "reason-specific evidence fact kind is inconsistent"
            )
        object.__setattr__(
            self,
            "no_entry_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": RUNTIME_NO_ENTRY_SCHEMA,
            "reason": self.reason.value,
            "execution_summary": self.execution_summary.as_dict(),
            "entry_window_start": _utc_text(self.entry_window_start),
            "entry_window_end": _utc_text(self.entry_window_end),
            "decided_at": _utc_text(self.decided_at),
            "execution_policy_id": self.execution_policy_id,
            "evidence_references": [
                item.as_dict() for item in self.evidence_references
            ],
            "evidence_ids": list(self.evidence_ids),
            "entry_window_coverage_complete": self.entry_window_coverage_complete,
            "entry_window_coverage_fact_id": self.entry_window_coverage_fact_id,
            "actor_id": self.actor_id,
            "actor_authentication_fact_id": self.actor_authentication_fact_id,
            "cancellation_fact_id": self.cancellation_fact_id,
            "rejection_fact_id": self.rejection_fact_id,
            "data_invalidity_fact_id": self.data_invalidity_fact_id,
        }
        if include_id:
            document["no_entry_id"] = self.no_entry_id
        return document


__all__ = [
    "RUNTIME_EXECUTION_SUMMARY_SCHEMA",
    "RUNTIME_NO_ENTRY_SCHEMA",
    "RUNTIME_PATH_CONTRACT_SCHEMA",
    "RUNTIME_PATH_PREFIX_SCHEMA",
    "RUNTIME_PATH_RESOLUTION_SCHEMA",
    "RuntimeAuthorityFactReference",
    "RuntimeAuthorityKind",
    "RuntimeAuthoritySnapshotBinding",
    "RuntimeCalendarState",
    "RuntimeCoverageState",
    "RuntimeExecutionFragment",
    "RuntimeExecutionSide",
    "RuntimeExecutionSummary",
    "RuntimeFillCompletion",
    "RuntimeFrozenPathFact",
    "RuntimeFrozenPathPrefix",
    "RuntimeMarketSourceSnapshotBinding",
    "RuntimeNoEntryEvidence",
    "RuntimeNoEntryReason",
    "RuntimeOpenSessionState",
    "RuntimePathBlockerCode",
    "RuntimePathContractError",
    "RuntimePathFactKind",
    "RuntimePathGranularity",
    "RuntimePathObservation",
    "RuntimePathPendingCode",
    "RuntimePathResolution",
    "RuntimePathResolutionState",
    "RuntimePathTerminalReason",
    "RuntimePathWindow",
    "RuntimeProjectionLineage",
    "RuntimeSessionEvidence",
    "RuntimeSourceReference",
    "resolve_runtime_path",
]
