from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any, Protocol

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market

MARKET_EVENT_SOURCE_RECORD_SCHEMA = "stage4g1-market-event-source-record-v4"
MARKET_EVENT_STORE_AUDIT_SCHEMA = "stage4g1-market-event-store-audit-v5"
MARKET_EVENT_STORE_SNAPSHOT_SCHEMA = "stage4g1-market-event-store-snapshot-v5"
MARKET_EVENT_SELECTION_SCHEMA = "stage4g1-market-event-selection-v5"
MARKET_EVENT_READ_PORT_SCHEMA = "stage4g1-market-event-read-port-v4"
MARKET_EVENT_SEQUENCE_POLICY_V3 = "stage4g1-market-event-sequence-policy-v3"
FIXTURE_TRADE_TICK_SCHEMA_V1 = "stage4g1-fixture-trade-tick-v1"
FIXTURE_TRADE_DECODER_POLICY_V1 = "stage4g1-fixture-trade-decoder-v1"
MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1 = (
    "stage4g1-market-event-left-closed-right-open-v1"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64
_MAX_TEXT = 4096
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_SELECTION_RECORDS = 10_000
_AUDIT_CHUNK_SIZE = 4096
_SAFE_STORAGE_KEY = re.compile(r"^[A-Za-z0-9._/-]+$")
_DRIVE_QUALIFIED = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class EvidenceAssurance(StrEnum):
    STRUCTURAL_FIXTURE = "STRUCTURAL_FIXTURE"
    STORE_RESCANNED = "STORE_RESCANNED"
    PIT_CANDIDATE = "PIT_CANDIDATE"
    TRUSTED_ADMITTED = "TRUSTED_ADMITTED"


class StructuralFixture:
    """Pure reference algorithms never issue physical-store or admission receipts."""

    __slots__ = ()

    @property
    def assurance(self) -> EvidenceAssurance:
        return EvidenceAssurance.STRUCTURAL_FIXTURE


class FindingImpactScope(StrEnum):
    SOURCE_SESSION_GLOBAL = "SOURCE_SESSION_GLOBAL"
    CONNECTION_EPOCH_GLOBAL = "CONNECTION_EPOCH_GLOBAL"
    SYMBOL_SESSION = "SYMBOL_SESSION"
    SYMBOL_CONNECTION_EPOCH = "SYMBOL_CONNECTION_EPOCH"


class FindingResolutionState(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    RESOLVED = "RESOLVED"


class CoverageOrigin(StrEnum):
    LIVE = "LIVE"
    REPLAY = "REPLAY"
    BACKFILL = "BACKFILL"


class SelectionCompleteness(StrEnum):
    COVERED_RECORDS = "COVERED_RECORDS"
    ZERO_EVENT_PROVEN = "ZERO_EVENT_PROVEN"
    EMPTY_NOT_PROVEN = "EMPTY_NOT_PROVEN"
    INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
    BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY = "BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY"


class MarketEventSourceContractError(ValueError):
    """Raised when a Market Event Store snapshot cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MARKET_EVENT_SOURCE_CONTRACT_INVALID",
    ) -> None:
        super().__init__(message)
        self.code = code


class MarketEventStoreAuditScope(StrEnum):
    FULL_PREFIX = "FULL_PREFIX"


class MarketEventSequenceFindingKind(StrEnum):
    CALLBACK_SEQUENCE = "CALLBACK_SEQUENCE"
    PROVIDER_SEQUENCE = "PROVIDER_SEQUENCE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    SOURCE_TIME_REGRESSION = "SOURCE_TIME_REGRESSION"
    SOURCE_CLOCK_SKEW = "SOURCE_CLOCK_SKEW"
    DURABILITY_DELAY = "DURABILITY_DELAY"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"


class MarketEventCallbackSequenceScope(StrEnum):
    SESSION = "SESSION"
    CONNECTION_EPOCH = "CONNECTION_EPOCH"


class MarketEventProviderSequenceScope(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    SESSION = "SESSION"
    CONNECTION_EPOCH = "CONNECTION_EPOCH"
    SYMBOL_SESSION = "SYMBOL_SESSION"
    SYMBOL_CONNECTION_EPOCH = "SYMBOL_CONNECTION_EPOCH"


class MarketEventSelectionVerificationMode(StrEnum):
    FULL_PREFIX_RESCAN = "FULL_PREFIX_RESCAN"


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int = _MAX_TEXT,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > maximum:
        raise MarketEventSourceContractError(f"{name} must be a safe trimmed string")
    if not value and not allow_empty:
        raise MarketEventSourceContractError(f"{name} must not be empty")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise MarketEventSourceContractError(f"{name} contains control characters")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=64)
    if _SHA256.fullmatch(text) is None:
        raise MarketEventSourceContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise MarketEventSourceContractError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MarketEventSourceContractError(
            f"{name} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_date(value: object, name: str) -> date:
    if type(value) is not date:
        raise MarketEventSourceContractError(f"{name} must be a date")
    return value


def _require_market(value: object) -> Market:
    if type(value) is not Market:
        raise MarketEventSourceContractError("market must be the exact Market type")
    return value


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol", maximum=64)
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if symbol != symbol.upper() or not symbol.endswith(suffixes[market]):
        raise MarketEventSourceContractError("symbol suffix must match market")
    return symbol


def _require_storage_key(value: object, name: str) -> str:
    key = _require_text(value, name, maximum=1024)
    if (
        _SAFE_STORAGE_KEY.fullmatch(key) is None
        or "\\" in key
        or key.startswith("/")
        or _DRIVE_QUALIFIED.match(key) is not None
        or ":" in key
    ):
        raise MarketEventSourceContractError(f"{name} is not a safe POSIX storage key")
    segments = key.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or segment.endswith((".", " "))
        or segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED
        for segment in segments
    ):
        raise MarketEventSourceContractError(f"{name} is not a safe POSIX storage key")
    return key


def _record_storage_key(append_order: int, event_id: str) -> str:
    return f"records/{append_order:020d}-{event_id}.json"


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise MarketEventSourceContractError(
            "source snapshot is not canonical JSON"
        ) from exc
    if not raw or len(raw) > _MAX_PAYLOAD_BYTES:
        raise MarketEventSourceContractError("source snapshot exceeds its size bound")
    return raw


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_PAYLOAD_BYTES:
        raise MarketEventSourceContractError("payload JSON bytes are invalid")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise MarketEventSourceContractError(
                    "payload JSON keys are invalid or duplicated"
                )
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise MarketEventSourceContractError(
            f"payload JSON contains non-finite token {token}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MarketEventSourceContractError(
            "payload is not strict UTF-8 JSON"
        ) from exc
    if type(value) is not dict:
        raise MarketEventSourceContractError("payload JSON must be an object")
    if _canonical_json_bytes(value) != raw:
        raise MarketEventSourceContractError("payload JSON is not canonical")
    return value


def _hash_document(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketEventSubscriptionManifest(StructuralFixture):
    market: Market
    symbols: tuple[str, ...]
    event_types: tuple[str, ...]
    subscription_scope_id: str = field(init=False)
    subscription_manifest_root: str = field(init=False)
    symbol_or_universe_snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_market(self.market)
        if type(self.symbols) is not tuple or not self.symbols:
            raise MarketEventSourceContractError(
                "subscription symbols must be a non-empty tuple"
            )
        for symbol in self.symbols:
            _require_symbol(symbol, self.market)
        if self.symbols != tuple(sorted(set(self.symbols))):
            raise MarketEventSourceContractError(
                "subscription symbols must be unique and canonical"
            )
        if type(self.event_types) is not tuple or not self.event_types:
            raise MarketEventSourceContractError(
                "subscription event types must be a non-empty tuple"
            )
        for event_type in self.event_types:
            _require_text(event_type, "subscribed_event_type", maximum=128)
        if self.event_types != tuple(sorted(set(self.event_types))):
            raise MarketEventSourceContractError(
                "subscription event types must be unique and canonical"
            )
        identity = _hash_document(self.as_dict())
        object.__setattr__(self, "subscription_scope_id", identity)
        object.__setattr__(self, "subscription_manifest_root", identity)
        object.__setattr__(
            self,
            "symbol_or_universe_snapshot_id",
            _hash_document(
                {
                    "schema": "stage4g1-fixture-subscription-universe-v1",
                    "market": self.market.value,
                    "symbols": list(self.symbols),
                }
            ),
        )

    def membership_witness(self, symbol: str, market: Market) -> str:
        if market is not self.market or symbol not in self.symbols:
            raise MarketEventSourceContractError("symbol is absent from subscription")
        return _hash_document(
            {
                "schema": "stage4g1-fixture-subscription-membership-v1",
                "subscription_manifest_root": self.subscription_manifest_root,
                "symbol": symbol,
                "market": market.value,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage4g1-subscription-manifest-v1",
            "assurance": self.assurance.value,
            "market": self.market.value,
            "symbols": list(self.symbols),
            "event_types": list(self.event_types),
        }


@dataclass(frozen=True, slots=True)
class SourceClockPolicy(StructuralFixture):
    clock_policy_id: str = "stage4g1-fixture-source-clock-v1"
    maximum_source_ahead_of_received: timedelta = field(
        init=False, default=timedelta(seconds=2)
    )
    maximum_received_to_durable_delay: timedelta = field(
        init=False, default=timedelta(minutes=5)
    )
    maximum_source_regression: timedelta = field(init=False, default=timedelta(0))
    timezone_precision: str = field(init=False, default="UTC_MICROSECONDS")
    violation_handling: str = field(init=False, default="IMPACT_FINDING_BLOCK")

    def __post_init__(self) -> None:
        if (
            type(self.clock_policy_id) is not str
            or self.clock_policy_id != "stage4g1-fixture-source-clock-v1"
        ):
            raise MarketEventSourceContractError("unsupported source clock policy")


@dataclass(frozen=True, slots=True)
class MarketEventSourceSessionManifest(StructuralFixture):
    source_store_id: str
    session_id: str
    connection_epoch: int
    reconnect_epoch: int
    collector_started_at: datetime
    coverage_start: datetime
    expected_first_callback_seq: int | None
    callback_sequence_scope: MarketEventCallbackSequenceScope
    provider_sequence_available: bool
    provider_sequence_scope: MarketEventProviderSequenceScope
    sequence_policy_id: str
    subscription: MarketEventSubscriptionManifest
    subscription_activated_at: datetime
    coverage_through: datetime
    coverage_origin: CoverageOrigin
    queue_overflow_count: int
    dropped_callback_count: int
    first_callback_seq: int | None
    last_callback_seq: int | None
    replay_selection: MarketEventSelection | None = None
    replay_verification: MarketEventSelectionVerification | None = None
    clock_policy_id: str = "stage4g1-fixture-source-clock-v1"
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        SourceClockPolicy(self.clock_policy_id)
        _require_sha256(self.source_store_id, "source_store_id")
        _require_text(self.session_id, "session_id", maximum=256)
        _require_int(self.connection_epoch, "connection_epoch", minimum=1)
        _require_int(self.reconnect_epoch, "reconnect_epoch")
        collector_started_at = _require_utc(
            self.collector_started_at,
            "collector_started_at",
        )
        coverage_start = _require_utc(self.coverage_start, "coverage_start")
        activated_at = _require_utc(
            self.subscription_activated_at, "subscription_activated_at"
        )
        through = _require_utc(self.coverage_through, "coverage_through")
        if through < coverage_start:
            raise MarketEventSourceContractError("coverage interval is reversed")
        if type(self.subscription) is not MarketEventSubscriptionManifest:
            raise MarketEventSourceContractError(
                "typed subscription manifest is required"
            )
        if replace(self.subscription) != self.subscription:
            raise MarketEventSourceContractError("subscription manifest was mutated")
        if type(self.coverage_origin) is not CoverageOrigin:
            raise MarketEventSourceContractError(
                "coverage_origin must be CoverageOrigin"
            )
        if self.coverage_origin is CoverageOrigin.LIVE and not (
            collector_started_at <= activated_at <= coverage_start
        ):
            raise MarketEventSourceContractError(
                "LIVE coverage cannot predate collector or subscription activation"
            )
        if self.coverage_origin is CoverageOrigin.LIVE:
            if (
                self.replay_selection is not None
                or self.replay_verification is not None
            ):
                raise MarketEventSourceContractError(
                    "LIVE cannot carry replay membership"
                )
        else:
            if self.replay_selection is None or self.replay_verification is None:
                raise MarketEventSourceContractError(
                    "replay/backfill requires exact selection membership"
                )
            validate_selection_verification(
                self.replay_selection, self.replay_verification
            )
            replay = self.replay_selection
            if (
                len(self.subscription.symbols) != 1
                or replay.symbol != self.subscription.symbols[0]
                or replay.market is not self.subscription.market
                or replay.allowed_event_types != self.subscription.event_types
                or replay.start_source_time > coverage_start
                or replay.end_source_time < through
            ):
                raise MarketEventSourceContractError(
                    "replay/backfill does not prove the coverage interval"
                )
        object.__setattr__(self, "collector_started_at", collector_started_at)
        object.__setattr__(self, "coverage_start", coverage_start)
        object.__setattr__(self, "subscription_activated_at", activated_at)
        object.__setattr__(self, "coverage_through", through)
        _require_int(self.queue_overflow_count, "queue_overflow_count")
        _require_int(self.dropped_callback_count, "dropped_callback_count")
        if (self.first_callback_seq is None) != (self.last_callback_seq is None):
            raise MarketEventSourceContractError(
                "first/last callback sequence must be paired"
            )
        if self.first_callback_seq is not None:
            first = _require_int(
                self.first_callback_seq, "first_callback_seq", minimum=1
            )
            last = _require_int(self.last_callback_seq, "last_callback_seq", minimum=1)
            if last < first:
                raise MarketEventSourceContractError(
                    "callback sequence interval is reversed"
                )
        if self.expected_first_callback_seq is not None:
            _require_int(
                self.expected_first_callback_seq,
                "expected_first_callback_seq",
                minimum=1,
            )
        if type(self.callback_sequence_scope) is not MarketEventCallbackSequenceScope:
            raise MarketEventSourceContractError(
                "callback_sequence_scope must be MarketEventCallbackSequenceScope"
            )
        if type(self.provider_sequence_available) is not bool:
            raise MarketEventSourceContractError(
                "provider_sequence_available must be boolean"
            )
        if type(self.provider_sequence_scope) is not MarketEventProviderSequenceScope:
            raise MarketEventSourceContractError(
                "provider_sequence_scope must be MarketEventProviderSequenceScope"
            )
        if self.provider_sequence_available != (
            self.provider_sequence_scope
            is not MarketEventProviderSequenceScope.UNAVAILABLE
        ):
            raise MarketEventSourceContractError(
                "provider sequence availability and scope disagree"
            )
        if self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V3:
            raise MarketEventSourceContractError(
                "unsupported market-event sequence policy"
            )
        object.__setattr__(
            self,
            "manifest_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    @property
    def has_sequence_start_proof(self) -> bool:
        return self.expected_first_callback_seq is not None and (
            self.first_callback_seq == self.expected_first_callback_seq
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-source-session-manifest-v3",
            "clock_policy_id": self.clock_policy_id,
            "source_store_id": self.source_store_id,
            "session_id": self.session_id,
            "connection_epoch": self.connection_epoch,
            "reconnect_epoch": self.reconnect_epoch,
            "collector_started_at": _utc_text(self.collector_started_at),
            "coverage_start": _utc_text(self.coverage_start),
            "coverage_through": _utc_text(self.coverage_through),
            "subscription": self.subscription.as_dict(),
            "subscription_scope_id": self.subscription.subscription_scope_id,
            "subscription_manifest_root": self.subscription.subscription_manifest_root,
            "symbol_or_universe_snapshot_id": self.subscription.symbol_or_universe_snapshot_id,
            "subscription_activated_at": _utc_text(self.subscription_activated_at),
            "coverage_origin": self.coverage_origin.value,
            "queue_overflow_count": self.queue_overflow_count,
            "dropped_callback_count": self.dropped_callback_count,
            "first_callback_seq": self.first_callback_seq,
            "last_callback_seq": self.last_callback_seq,
            "replay_selection_id": None
            if self.replay_selection is None
            else self.replay_selection.selection_id,
            "replay_verification_id": None
            if self.replay_verification is None
            else self.replay_verification.verification_id,
            "expected_first_callback_seq": self.expected_first_callback_seq,
            "callback_sequence_scope": self.callback_sequence_scope.value,
            "provider_sequence_available": self.provider_sequence_available,
            "provider_sequence_scope": self.provider_sequence_scope.value,
            "sequence_policy_id": self.sequence_policy_id,
        }
        if include_id:
            document["manifest_id"] = self.manifest_id
        return document


@dataclass(frozen=True, slots=True)
class MarketEventSourceRecord(StructuralFixture):
    source_store_id: str
    append_order: int
    event_id: str
    session_id: str
    source_session_manifest_id: str
    connection_epoch: int
    reconnect_epoch: int
    source: str
    feed_mode: str
    symbol: str
    market: Market
    event_type: str
    trading_day: date
    session_label_policy_id: str
    source_time: datetime
    received_at: datetime
    durable_known_at: datetime
    callback_seq: int
    provider_seq: int | None
    partition_key: str
    previous_global_record_hash: str
    previous_partition_record_hash: str
    raw_payload_sha256: str
    payload_json: str
    payload_sha256: str
    parser_id: str
    source_schema_id: str
    record_storage_key: str
    record_file_sha256: str
    record_content_hash: str
    source_record_id: str = field(init=False)

    def __post_init__(self) -> None:
        source_store_id = _require_sha256(self.source_store_id, "source_store_id")
        append_order = _require_int(self.append_order, "append_order", minimum=1)
        event_id = _require_sha256(self.event_id, "event_id")
        _require_text(self.session_id, "session_id", maximum=256)
        _require_sha256(
            self.source_session_manifest_id,
            "source_session_manifest_id",
        )
        _require_int(self.connection_epoch, "connection_epoch", minimum=1)
        _require_int(self.reconnect_epoch, "reconnect_epoch")
        _require_text(self.source, "source", maximum=128)
        _require_text(self.feed_mode, "feed_mode", maximum=128)
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_text(self.event_type, "event_type", maximum=128)
        _require_date(self.trading_day, "trading_day")
        if self.session_label_policy_id != MARKET_SESSION_LABEL_POLICY_V1:
            raise MarketEventSourceContractError(
                "unsupported market session label policy"
            )
        source_time = _require_utc(self.source_time, "source_time")
        received_at = _require_utc(self.received_at, "received_at")
        durable_known_at = _require_utc(self.durable_known_at, "durable_known_at")
        if source_time > durable_known_at or received_at > durable_known_at:
            raise MarketEventSourceContractError(
                "source or received time exceeds durable_known_at"
            )
        object.__setattr__(self, "source_time", source_time)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "durable_known_at", durable_known_at)
        if (
            market_session_date(
                source_time,
                market,
                self.session_label_policy_id,
            )
            != self.trading_day
        ):
            raise MarketEventSourceContractError(
                "trading_day disagrees with market-local source_time"
            )
        _require_int(self.callback_seq, "callback_seq", minimum=1)
        if self.provider_seq is not None:
            _require_int(self.provider_seq, "provider_seq")
        partition_key = _require_text(
            self.partition_key,
            "partition_key",
            maximum=512,
        )
        expected_partition = (
            f"market={market.value}/trading_day={self.trading_day.isoformat()}/"
            f"symbol={self.symbol}"
        )
        if partition_key != expected_partition:
            raise MarketEventSourceContractError(
                "partition_key disagrees with market/day/symbol identity"
            )
        previous_global = _require_sha256(
            self.previous_global_record_hash,
            "previous_global_record_hash",
        )
        previous_partition = _require_sha256(
            self.previous_partition_record_hash,
            "previous_partition_record_hash",
        )
        _require_sha256(self.raw_payload_sha256, "raw_payload_sha256")
        payload_json = _require_text(
            self.payload_json,
            "payload_json",
            maximum=_MAX_PAYLOAD_BYTES,
        )
        payload_raw = payload_json.encode("utf-8")
        _strict_json_object(payload_raw)
        if hashlib.sha256(payload_raw).hexdigest() != _require_sha256(
            self.payload_sha256,
            "payload_sha256",
        ):
            raise MarketEventSourceContractError("payload SHA mismatch")
        _require_text(self.parser_id, "parser_id", maximum=256)
        _require_text(self.source_schema_id, "source_schema_id", maximum=256)
        storage_key = _require_storage_key(
            self.record_storage_key,
            "record_storage_key",
        )
        if storage_key != _record_storage_key(append_order, event_id):
            raise MarketEventSourceContractError(
                "record_storage_key is not the deterministic immutable record location"
            )
        file_sha = _require_sha256(self.record_file_sha256, "record_file_sha256")
        record_identity = self._record_identity(
            source_store_id=source_store_id,
            append_order=append_order,
            event_id=event_id,
            previous_global_record_hash=previous_global,
            previous_partition_record_hash=previous_partition,
        )
        expected_content_hash = _hash_document(record_identity)
        if self.record_content_hash != expected_content_hash:
            raise MarketEventSourceContractError("record_content_hash mismatch")
        if file_sha != expected_content_hash:
            raise MarketEventSourceContractError(
                "record_file_sha256 must equal canonical record content hash"
            )
        object.__setattr__(
            self,
            "source_record_id",
            _hash_document(
                {
                    "schema": "stage4g1-market-event-source-record-id-v3",
                    "source_store_id": source_store_id,
                    "append_order": append_order,
                    "event_id": event_id,
                    "record_content_hash": expected_content_hash,
                    "record_storage_key": storage_key,
                    "record_file_sha256": file_sha,
                }
            ),
        )

    def _record_identity(
        self,
        *,
        source_store_id: str | None = None,
        append_order: int | None = None,
        event_id: str | None = None,
        previous_global_record_hash: str | None = None,
        previous_partition_record_hash: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": MARKET_EVENT_SOURCE_RECORD_SCHEMA,
            "source_store_id": self.source_store_id
            if source_store_id is None
            else source_store_id,
            "append_order": self.append_order if append_order is None else append_order,
            "event_id": self.event_id if event_id is None else event_id,
            "session_id": self.session_id,
            "source_session_manifest_id": self.source_session_manifest_id,
            "connection_epoch": self.connection_epoch,
            "reconnect_epoch": self.reconnect_epoch,
            "source": self.source,
            "feed_mode": self.feed_mode,
            "symbol": self.symbol,
            "market": self.market.value,
            "event_type": self.event_type,
            "trading_day": self.trading_day.isoformat(),
            "session_label_policy_id": self.session_label_policy_id,
            "source_time": _utc_text(self.source_time),
            "received_at": _utc_text(self.received_at),
            "durable_known_at": _utc_text(self.durable_known_at),
            "callback_seq": self.callback_seq,
            "provider_seq": self.provider_seq,
            "partition_key": self.partition_key,
            "previous_global_record_hash": self.previous_global_record_hash
            if previous_global_record_hash is None
            else previous_global_record_hash,
            "previous_partition_record_hash": self.previous_partition_record_hash
            if previous_partition_record_hash is None
            else previous_partition_record_hash,
            "raw_payload_sha256": self.raw_payload_sha256,
            "payload_sha256": self.payload_sha256,
            "payload_json": self.payload_json,
            "parser_id": self.parser_id,
            "source_schema_id": self.source_schema_id,
        }

    @classmethod
    def create(
        cls,
        *,
        source_store_id: str,
        append_order: int,
        event_id: str,
        session_id: str,
        source_session_manifest_id: str,
        connection_epoch: int,
        reconnect_epoch: int,
        source: str,
        feed_mode: str,
        symbol: str,
        market: Market,
        event_type: str,
        trading_day: date,
        session_label_policy_id: str,
        source_time: datetime,
        received_at: datetime,
        durable_known_at: datetime,
        callback_seq: int,
        provider_seq: int | None,
        partition_key: str,
        previous_global_record_hash: str,
        previous_partition_record_hash: str,
        raw_payload_sha256: str,
        payload: dict[str, Any],
        parser_id: str,
        source_schema_id: str,
    ) -> MarketEventSourceRecord:
        if type(payload) is not dict:
            raise MarketEventSourceContractError("payload must be an exact dict")
        payload_raw = _canonical_json_bytes(payload)
        payload_sha = hashlib.sha256(payload_raw).hexdigest()
        provisional = object.__new__(cls)
        for name, value in {
            "source_store_id": source_store_id,
            "append_order": append_order,
            "event_id": event_id,
            "session_id": session_id,
            "source_session_manifest_id": source_session_manifest_id,
            "connection_epoch": connection_epoch,
            "reconnect_epoch": reconnect_epoch,
            "source": source,
            "feed_mode": feed_mode,
            "symbol": symbol,
            "market": market,
            "event_type": event_type,
            "trading_day": trading_day,
            "session_label_policy_id": session_label_policy_id,
            "source_time": source_time,
            "received_at": received_at,
            "durable_known_at": durable_known_at,
            "callback_seq": callback_seq,
            "provider_seq": provider_seq,
            "partition_key": partition_key,
            "previous_global_record_hash": previous_global_record_hash,
            "previous_partition_record_hash": previous_partition_record_hash,
            "raw_payload_sha256": raw_payload_sha256,
            "payload_json": payload_raw.decode("utf-8"),
            "payload_sha256": payload_sha,
            "parser_id": parser_id,
            "source_schema_id": source_schema_id,
            "record_storage_key": _record_storage_key(append_order, event_id),
        }.items():
            object.__setattr__(provisional, name, value)
        identity = provisional._record_identity()
        return cls(
            source_store_id=source_store_id,
            append_order=append_order,
            event_id=event_id,
            session_id=session_id,
            source_session_manifest_id=source_session_manifest_id,
            connection_epoch=connection_epoch,
            reconnect_epoch=reconnect_epoch,
            source=source,
            feed_mode=feed_mode,
            symbol=symbol,
            market=market,
            event_type=event_type,
            trading_day=trading_day,
            session_label_policy_id=session_label_policy_id,
            source_time=source_time,
            received_at=received_at,
            durable_known_at=durable_known_at,
            callback_seq=callback_seq,
            provider_seq=provider_seq,
            partition_key=partition_key,
            previous_global_record_hash=previous_global_record_hash,
            previous_partition_record_hash=previous_partition_record_hash,
            raw_payload_sha256=raw_payload_sha256,
            payload_json=payload_raw.decode("utf-8"),
            payload_sha256=payload_sha,
            parser_id=parser_id,
            source_schema_id=source_schema_id,
            record_storage_key=_record_storage_key(append_order, event_id),
            record_file_sha256=_hash_document(identity),
            record_content_hash=_hash_document(identity),
        )

    def payload(self) -> dict[str, Any]:
        return dict(_strict_json_object(self.payload_json.encode("utf-8")))

    def record_content_bytes(self) -> bytes:
        """Frozen on-disk bytes; catalog identity and the file's own SHA stay outside."""
        return _canonical_json_bytes(self._record_identity())

    def inventory_leaf(self) -> dict[str, Any]:
        return {
            "schema": "stage4g1-market-event-inventory-leaf-v1",
            "source_store_id": self.source_store_id,
            "append_order": self.append_order,
            "event_id": self.event_id,
            "record_content_hash": self.record_content_hash,
            "record_storage_key": self.record_storage_key,
            "record_file_sha256": self.record_file_sha256,
            "source_record_id": self.source_record_id,
        }

    @property
    def inventory_leaf_id(self) -> str:
        return _hash_document(self.inventory_leaf())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._record_identity(),
            "assurance": self.assurance.value,
            "payload_json": self.payload_json,
            "record_storage_key": self.record_storage_key,
            "record_file_sha256": self.record_file_sha256,
            "record_content_hash": self.record_content_hash,
            "source_record_id": self.source_record_id,
        }


@dataclass(frozen=True, slots=True)
class MarketEventSequenceFinding(StructuralFixture):
    sequence_policy_id: str
    kind: MarketEventSequenceFindingKind
    source_store_id: str
    event_id: str
    session_id: str
    symbol: str
    observed_append_order: int
    expected_sequence: int | None
    observed_sequence: int | None
    detail_code: str
    market: Market
    impact_scope: FindingImpactScope
    connection_epoch: int
    reconnect_epoch: int
    impact_append_order_start: int
    impact_append_order_end: int
    impact_source_time_start: datetime
    impact_source_time_end: datetime
    affected_symbol: str | None
    affected_event_types: tuple[str, ...]
    resolution_state: FindingResolutionState = FindingResolutionState.UNRESOLVED
    replay_resolution_id: str | None = None
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V3:
            raise MarketEventSourceContractError(
                "unsupported market-event sequence policy"
            )
        if type(self.kind) is not MarketEventSequenceFindingKind:
            raise MarketEventSourceContractError(
                "kind must be MarketEventSequenceFindingKind"
            )
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.event_id, "event_id")
        _require_text(self.session_id, "session_id", maximum=256)
        _require_text(self.symbol, "symbol", maximum=64)
        _require_int(
            self.observed_append_order,
            "observed_append_order",
            minimum=1,
        )
        if self.expected_sequence is not None:
            _require_int(self.expected_sequence, "expected_sequence")
        if self.observed_sequence is not None:
            _require_int(self.observed_sequence, "observed_sequence")
        _require_text(self.detail_code, "detail_code", maximum=256)
        _require_market(self.market)
        if type(self.impact_scope) is not FindingImpactScope:
            raise MarketEventSourceContractError(
                "typed finding impact scope is required"
            )
        _require_int(self.connection_epoch, "connection_epoch", minimum=1)
        _require_int(self.reconnect_epoch, "reconnect_epoch")
        start_order = _require_int(
            self.impact_append_order_start, "impact_append_order_start", minimum=1
        )
        end_order = _require_int(
            self.impact_append_order_end, "impact_append_order_end", minimum=start_order
        )
        if not start_order <= self.observed_append_order <= end_order:
            raise MarketEventSourceContractError(
                "finding carrier is outside append impact"
            )
        start = _require_utc(self.impact_source_time_start, "impact_source_time_start")
        end = _require_utc(self.impact_source_time_end, "impact_source_time_end")
        if end <= start:
            raise MarketEventSourceContractError(
                "finding impact interval must be non-empty"
            )
        object.__setattr__(self, "impact_source_time_start", start)
        object.__setattr__(self, "impact_source_time_end", end)
        symbol_scoped = self.impact_scope in {
            FindingImpactScope.SYMBOL_SESSION,
            FindingImpactScope.SYMBOL_CONNECTION_EPOCH,
        }
        if symbol_scoped:
            _require_symbol(self.affected_symbol, self.market)
        elif self.affected_symbol is not None:
            raise MarketEventSourceContractError(
                "global finding cannot narrow affected symbol"
            )
        if (
            type(self.affected_event_types) is not tuple
            or not self.affected_event_types
            or self.affected_event_types
            != tuple(sorted(set(self.affected_event_types)))
        ):
            raise MarketEventSourceContractError(
                "finding event types must be canonical"
            )
        for event_type in self.affected_event_types:
            _require_text(event_type, "affected_event_type")
        if type(self.resolution_state) is not FindingResolutionState:
            raise MarketEventSourceContractError("typed finding resolution is required")
        if self.resolution_state is FindingResolutionState.RESOLVED:
            _require_sha256(self.replay_resolution_id, "replay_resolution_id")
        elif self.replay_resolution_id is not None:
            raise MarketEventSourceContractError(
                "unresolved finding cannot carry a resolution"
            )
        object.__setattr__(
            self,
            "finding_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-sequence-finding-v3",
            "sequence_policy_id": self.sequence_policy_id,
            "kind": self.kind.value,
            "source_store_id": self.source_store_id,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "observed_append_order": self.observed_append_order,
            "expected_sequence": self.expected_sequence,
            "observed_sequence": self.observed_sequence,
            "detail_code": self.detail_code,
            "market": self.market.value,
            "impact_scope": self.impact_scope.value,
            "connection_epoch": self.connection_epoch,
            "reconnect_epoch": self.reconnect_epoch,
            "impact_append_order_start": self.impact_append_order_start,
            "impact_append_order_end": self.impact_append_order_end,
            "impact_source_time_start": _utc_text(self.impact_source_time_start),
            "impact_source_time_end": _utc_text(self.impact_source_time_end),
            "affected_symbol": self.affected_symbol,
            "affected_event_types": list(self.affected_event_types),
            "resolution_state": self.resolution_state.value,
            "replay_resolution_id": self.replay_resolution_id,
        }
        if include_id:
            document["finding_id"] = self.finding_id
        return document

    def intersects(
        self,
        symbol: str,
        market: Market,
        start: datetime,
        end: datetime,
        event_types: tuple[str, ...],
    ) -> bool:
        return (
            self.market is market
            and (self.affected_symbol is None or self.affected_symbol == symbol)
            and self.impact_source_time_start < end
            and start < self.impact_source_time_end
            and bool(set(event_types).intersection(self.affected_event_types))
        )

    @classmethod
    def from_record(
        cls,
        *,
        record: MarketEventSourceRecord,
        manifest: MarketEventSourceSessionManifest,
        kind: MarketEventSequenceFindingKind,
        expected_sequence: int | None,
        observed_sequence: int | None,
        detail_code: str,
        scope_manifests: tuple[MarketEventSourceSessionManifest, ...] = (),
    ) -> MarketEventSequenceFinding:
        if detail_code.startswith("PROVIDER_"):
            scope = {
                MarketEventProviderSequenceScope.SESSION: FindingImpactScope.SOURCE_SESSION_GLOBAL,
                MarketEventProviderSequenceScope.CONNECTION_EPOCH: FindingImpactScope.CONNECTION_EPOCH_GLOBAL,
                MarketEventProviderSequenceScope.SYMBOL_SESSION: FindingImpactScope.SYMBOL_SESSION,
                MarketEventProviderSequenceScope.SYMBOL_CONNECTION_EPOCH: FindingImpactScope.SYMBOL_CONNECTION_EPOCH,
            }.get(manifest.provider_sequence_scope)
            if scope is None:
                raise MarketEventSourceContractError(
                    "provider finding requires sequence capability"
                )
        elif kind is MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION:
            scope = FindingImpactScope.SYMBOL_SESSION
        else:
            scope = (
                FindingImpactScope.SOURCE_SESSION_GLOBAL
                if manifest.callback_sequence_scope
                is MarketEventCallbackSequenceScope.SESSION
                else FindingImpactScope.CONNECTION_EPOCH_GLOBAL
            )
        impact_manifests = tuple(
            item
            for item in scope_manifests
            if item.source_store_id == record.source_store_id
            and item.session_id == record.session_id
            and (
                scope
                in {
                    FindingImpactScope.SOURCE_SESSION_GLOBAL,
                    FindingImpactScope.SYMBOL_SESSION,
                }
                or (
                    item.connection_epoch == record.connection_epoch
                    and item.reconnect_epoch == record.reconnect_epoch
                )
            )
        ) or (manifest,)
        return cls(
            sequence_policy_id=manifest.sequence_policy_id,
            kind=kind,
            source_store_id=record.source_store_id,
            event_id=record.event_id,
            session_id=record.session_id,
            symbol=record.symbol,
            observed_append_order=record.append_order,
            expected_sequence=expected_sequence,
            observed_sequence=observed_sequence,
            detail_code=detail_code,
            market=record.market,
            impact_scope=scope,
            connection_epoch=record.connection_epoch,
            reconnect_epoch=record.reconnect_epoch,
            impact_append_order_start=1,
            impact_append_order_end=record.append_order,
            impact_source_time_start=min(
                item.coverage_start for item in impact_manifests
            ),
            impact_source_time_end=max(
                item.coverage_through for item in impact_manifests
            ),
            affected_symbol=(
                record.symbol
                if scope
                in {
                    FindingImpactScope.SYMBOL_SESSION,
                    FindingImpactScope.SYMBOL_CONNECTION_EPOCH,
                }
                else None
            ),
            affected_event_types=tuple(
                sorted(
                    {
                        event_type
                        for item in impact_manifests
                        for event_type in item.subscription.event_types
                    }
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketEventPartitionHead(StructuralFixture):
    partition_key: str
    event_count: int
    first_record_hash: str
    last_record_hash: str
    manifest_sha256: str
    partition_head_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.partition_key, "partition_key", maximum=512)
        event_count = _require_int(self.event_count, "event_count")
        first = _require_sha256(self.first_record_hash, "first_record_hash")
        last = _require_sha256(self.last_record_hash, "last_record_hash")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if event_count == 0:
            if first != _ZERO_HASH or last != _ZERO_HASH:
                raise MarketEventSourceContractError(
                    "empty partition must use zero hash boundaries"
                )
        elif first == _ZERO_HASH or last == _ZERO_HASH:
            raise MarketEventSourceContractError(
                "non-empty partition cannot use zero hash boundaries"
            )
        object.__setattr__(
            self,
            "partition_head_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-partition-head-v2",
            "partition_key": self.partition_key,
            "event_count": self.event_count,
            "first_record_hash": self.first_record_hash,
            "last_record_hash": self.last_record_hash,
            "manifest_sha256": self.manifest_sha256,
        }
        if include_id:
            document["partition_head_id"] = self.partition_head_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class MarketEventInventoryVerification(StructuralFixture):
    source_store_id: str
    catalog_schema_fingerprint: str
    high_water_append_order: int
    record_count: int
    inventory_root: str
    verification_mode: MarketEventSelectionVerificationMode
    verification_id: str

    def __init__(self) -> None:
        raise TypeError(
            "MarketEventInventoryVerification requires an exact prefix scan"
        )

    @classmethod
    def create_from_prefix(
        cls,
        *,
        source_store_id: str,
        catalog_schema_fingerprint: str,
        records: Iterable[MarketEventSourceRecord],
    ) -> MarketEventInventoryVerification:
        source_store_id = _require_sha256(source_store_id, "source_store_id")
        catalog_schema_fingerprint = _require_sha256(
            catalog_schema_fingerprint,
            "catalog_schema_fingerprint",
        )
        inventory_chain = _ZERO_HASH
        event_ids: set[str] = set()
        source_record_ids: set[str] = set()
        storage_keys: set[str] = set()
        append_orders: set[int] = set()
        record_count = 0
        for record in records:
            if type(record) is not MarketEventSourceRecord:
                raise MarketEventSourceContractError(
                    "inventory records must be MarketEventSourceRecord"
                )
            record_count += 1
            if (
                record.source_store_id != source_store_id
                or record.append_order != record_count
            ):
                raise MarketEventSourceContractError(
                    "inventory is not the exact contiguous store prefix"
                )
            duplicate = (
                record.event_id in event_ids
                or record.source_record_id in source_record_ids
                or record.record_storage_key in storage_keys
                or record.append_order in append_orders
            )
            if duplicate:
                raise MarketEventSourceContractError(
                    "inventory contains duplicate event, record, storage, or append identity"
                )
            event_ids.add(record.event_id)
            source_record_ids.add(record.source_record_id)
            storage_keys.add(record.record_storage_key)
            append_orders.add(record.append_order)
            inventory_chain = _rolling_digest(
                inventory_chain,
                "stage4g1-market-event-inventory-node-v1",
                record.inventory_leaf(),
            )
        self = object.__new__(cls)
        values: dict[str, object] = {
            "source_store_id": source_store_id,
            "catalog_schema_fingerprint": catalog_schema_fingerprint,
            "high_water_append_order": record_count,
            "record_count": record_count,
            "inventory_root": _hash_document(
                {
                    "schema": "stage4g1-market-event-inventory-root-v1",
                    "record_count": record_count,
                    "chain_head": inventory_chain,
                }
            ),
            "verification_mode": MarketEventSelectionVerificationMode.FULL_PREFIX_RESCAN,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "verification_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-inventory-verification-v2",
            "source_store_id": self.source_store_id,
            "catalog_schema_fingerprint": self.catalog_schema_fingerprint,
            "high_water_append_order": self.high_water_append_order,
            "record_count": self.record_count,
            "inventory_root": self.inventory_root,
            "verification_mode": self.verification_mode.value,
        }
        if include_id:
            document["verification_id"] = self.verification_id
        return document


@dataclass(frozen=True, slots=True)
class _PrefixScan:
    record_count: int
    high_water_append_order: int
    first_global_record_hash: str
    last_global_record_hash: str
    global_chain_head: str
    partition_count: int
    partition_head_root: str
    finding_count: int
    finding_set_digest: str
    chunk_count: int
    chunk_manifest_root: str
    source_session_manifest_count: int
    source_session_manifest_root: str
    selected_records: tuple[MarketEventSourceRecord, ...]
    selected_findings: tuple[MarketEventSequenceFinding, ...]
    source_session_manifests: tuple[MarketEventSourceSessionManifest, ...]
    coverage_verified_manifest_ids: tuple[str, ...]


def _rolling_digest(previous: str, schema: str, document: dict[str, Any]) -> str:
    return _hash_document(
        {
            "schema": schema,
            "previous": previous,
            "document": document,
        }
    )


def _expected_sequence_findings(
    record: MarketEventSourceRecord,
    manifest: MarketEventSourceSessionManifest,
    callback_max: dict[tuple[object, ...], int],
    provider_max: dict[tuple[object, ...], int],
    symbol_heads: dict[tuple[str, str], MarketEventSourceRecord],
    scope_manifests: tuple[MarketEventSourceSessionManifest, ...],
) -> tuple[MarketEventSequenceFinding, ...]:
    findings: list[MarketEventSequenceFinding] = []
    callback_key: tuple[object, ...] = (record.session_id,)
    if (
        manifest.callback_sequence_scope
        is MarketEventCallbackSequenceScope.CONNECTION_EPOCH
    ):
        callback_key += (record.connection_epoch, record.reconnect_epoch)
    previous_callback = callback_max.get(callback_key)
    expected_callback = (
        manifest.expected_first_callback_seq
        if previous_callback is None
        else previous_callback + 1
    )
    if expected_callback is not None:
        if record.callback_seq < expected_callback:
            findings.append(
                MarketEventSequenceFinding.from_record(
                    record=record,
                    manifest=manifest,
                    scope_manifests=scope_manifests,
                    kind=MarketEventSequenceFindingKind.OUT_OF_ORDER,
                    expected_sequence=expected_callback,
                    observed_sequence=record.callback_seq,
                    detail_code="CALLBACK_SEQUENCE_NOT_ADVANCED",
                )
            )
        elif record.callback_seq > expected_callback:
            findings.append(
                MarketEventSequenceFinding.from_record(
                    record=record,
                    manifest=manifest,
                    scope_manifests=scope_manifests,
                    kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
                    expected_sequence=expected_callback,
                    observed_sequence=record.callback_seq,
                    detail_code="CALLBACK_SEQUENCE_GAP",
                )
            )
    callback_max[callback_key] = max(
        record.callback_seq,
        callback_max.get(callback_key, record.callback_seq),
    )

    if manifest.provider_sequence_available:
        if record.provider_seq is None:
            raise MarketEventSourceContractError(
                "provider sequence is required by the source session manifest"
            )
        provider_key: tuple[object, ...] = (record.session_id,)
        if manifest.provider_sequence_scope in {
            MarketEventProviderSequenceScope.SYMBOL_SESSION,
            MarketEventProviderSequenceScope.SYMBOL_CONNECTION_EPOCH,
        }:
            provider_key += (record.symbol,)
        if manifest.provider_sequence_scope in {
            MarketEventProviderSequenceScope.CONNECTION_EPOCH,
            MarketEventProviderSequenceScope.SYMBOL_CONNECTION_EPOCH,
        }:
            provider_key += (record.connection_epoch, record.reconnect_epoch)
        previous_provider = provider_max.get(provider_key)
        if previous_provider is not None:
            if record.provider_seq <= previous_provider:
                findings.append(
                    MarketEventSequenceFinding.from_record(
                        record=record,
                        manifest=manifest,
                        scope_manifests=scope_manifests,
                        kind=MarketEventSequenceFindingKind.OUT_OF_ORDER,
                        expected_sequence=previous_provider + 1,
                        observed_sequence=record.provider_seq,
                        detail_code="PROVIDER_SEQUENCE_NOT_ADVANCED",
                    )
                )
            elif record.provider_seq > previous_provider + 1:
                findings.append(
                    MarketEventSequenceFinding.from_record(
                        record=record,
                        manifest=manifest,
                        scope_manifests=scope_manifests,
                        kind=MarketEventSequenceFindingKind.PROVIDER_SEQUENCE,
                        expected_sequence=previous_provider + 1,
                        observed_sequence=record.provider_seq,
                        detail_code="PROVIDER_SEQUENCE_GAP",
                    )
                )
        provider_max[provider_key] = max(
            record.provider_seq,
            provider_max.get(provider_key, record.provider_seq),
        )
    elif record.provider_seq is not None:
        raise MarketEventSourceContractError(
            "provider sequence is unavailable in the source session manifest"
        )

    clock = SourceClockPolicy(manifest.clock_policy_id)
    for violated, kind in (
        (
            record.source_time
            > record.received_at + clock.maximum_source_ahead_of_received,
            MarketEventSequenceFindingKind.SOURCE_CLOCK_SKEW,
        ),
        (
            record.durable_known_at - record.received_at
            > clock.maximum_received_to_durable_delay,
            MarketEventSequenceFindingKind.DURABILITY_DELAY,
        ),
    ):
        if violated:
            findings.append(
                MarketEventSequenceFinding.from_record(
                    record=record,
                    manifest=manifest,
                    scope_manifests=scope_manifests,
                    kind=kind,
                    expected_sequence=None,
                    observed_sequence=None,
                    detail_code=kind.value,
                )
            )
    previous_symbol = symbol_heads.get((record.session_id, record.symbol))
    if (
        previous_symbol is not None
        and record.source_time + clock.maximum_source_regression
        < previous_symbol.source_time
    ):
        findings.append(
            MarketEventSequenceFinding.from_record(
                record=record,
                manifest=manifest,
                scope_manifests=scope_manifests,
                kind=MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION,
                expected_sequence=None,
                observed_sequence=None,
                detail_code="SOURCE_TIME_REGRESSION",
            )
        )
    symbol_key = (record.session_id, record.symbol)
    if previous_symbol is None or (
        record.callback_seq,
        record.event_id,
    ) > (
        previous_symbol.callback_seq,
        previous_symbol.event_id,
    ):
        symbol_heads[symbol_key] = record
    return tuple(findings)


def _scan_prefix(
    *,
    records: Iterable[MarketEventSourceRecord],
    partition_heads: Iterable[MarketEventPartitionHead],
    findings: Iterable[MarketEventSequenceFinding],
    source_session_manifests: Iterable[MarketEventSourceSessionManifest],
    source_store_id: str,
    audited_at: datetime,
    sequence_policy_id: str,
    selection_query: tuple[
        str,
        Market,
        datetime,
        datetime,
        tuple[str, ...],
    ]
    | None = None,
    selection_limit: int = _MAX_SELECTION_RECORDS,
) -> _PrefixScan:
    if sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V3:
        raise MarketEventSourceContractError("unsupported market-event sequence policy")
    manifests = tuple(source_session_manifests)
    ordered_manifests = tuple(
        sorted(
            manifests,
            key=lambda item: (
                item.session_id,
                item.connection_epoch,
                item.reconnect_epoch,
                item.manifest_id,
            ),
        )
    )
    if manifests != ordered_manifests or any(
        type(item) is not MarketEventSourceSessionManifest
        or replace(item) != item
        or item.source_store_id != source_store_id
        or item.sequence_policy_id != sequence_policy_id
        for item in manifests
    ):
        raise MarketEventSourceContractError(
            "source session manifests must be canonical and belong to the store"
        )
    if len(
        {
            (
                item.source_store_id,
                item.session_id,
                item.connection_epoch,
                item.reconnect_epoch,
            )
            for item in manifests
        }
    ) != len(manifests):
        raise MarketEventSourceContractError("source session manifests must be unique")
    provider_capability_by_session: dict[
        str,
        tuple[
            bool,
            MarketEventProviderSequenceScope,
            MarketEventCallbackSequenceScope,
            Market,
        ],
    ] = {}
    for manifest in manifests:
        capability = (
            manifest.provider_sequence_available,
            manifest.provider_sequence_scope,
            manifest.callback_sequence_scope,
            manifest.subscription.market,
        )
        previous_capability = provider_capability_by_session.setdefault(
            manifest.session_id,
            capability,
        )
        if previous_capability != capability:
            raise MarketEventSourceContractError(
                "provider sequence capability changes within one source session"
            )
    manifest_by_id = {item.manifest_id: item for item in manifests}
    manifest_chain = _ZERO_HASH
    for manifest in manifests:
        manifest_chain = _rolling_digest(
            manifest_chain,
            "stage4g1-market-event-source-session-manifest-node-v1",
            manifest.as_dict(),
        )

    supplied_findings = iter(findings)
    expected_previous = _ZERO_HASH
    previous_known_at: datetime | None = None
    partition_state: dict[str, tuple[int, str, str]] = {}
    callback_max: dict[tuple[object, ...], int] = {}
    provider_max: dict[tuple[object, ...], int] = {}
    symbol_heads: dict[tuple[str, str], MarketEventSourceRecord] = {}
    event_ids: set[str] = set()
    source_record_ids: set[str] = set()
    storage_keys: set[str] = set()
    append_orders: set[int] = set()
    finding_chain = _ZERO_HASH
    manifest_sequences: dict[str, list[int]] = {}
    finding_count = 0
    chunk_chain = _ZERO_HASH
    chunk_manifest_chain = _ZERO_HASH
    chunk_record_count = 0
    chunk_count = 0
    first_hash = _ZERO_HASH
    selected_records: list[MarketEventSourceRecord] = []
    selected_findings: list[MarketEventSequenceFinding] = []
    record_count = 0
    for record in records:
        if type(record) is not MarketEventSourceRecord or replace(record) != record:
            raise MarketEventSourceContractError(
                "audit records must be MarketEventSourceRecord"
            )
        record_count += 1
        if (
            record.source_store_id != source_store_id
            or record.append_order != record_count
            or record.previous_global_record_hash != expected_previous
            or record.durable_known_at > audited_at
            or (
                previous_known_at is not None
                and record.durable_known_at < previous_known_at
            )
        ):
            raise MarketEventSourceContractError(
                "audited global prefix identity is invalid"
            )
        manifest = manifest_by_id.get(record.source_session_manifest_id)
        if manifest is None or (
            manifest.session_id != record.session_id
            or manifest.connection_epoch != record.connection_epoch
            or manifest.reconnect_epoch != record.reconnect_epoch
        ):
            raise MarketEventSourceContractError(
                "source record is not bound to its exact session manifest"
            )
        manifest_sequences.setdefault(manifest.manifest_id, []).append(
            record.callback_seq
        )
        if (
            record.market is not manifest.subscription.market
            or record.symbol not in manifest.subscription.symbols
            or record.event_type not in manifest.subscription.event_types
            or record.source_time < manifest.coverage_start
            or record.source_time >= manifest.coverage_through
        ):
            raise MarketEventSourceContractError(
                "record is outside subscribed coverage"
            )
        if (
            record.event_id in event_ids
            or record.source_record_id in source_record_ids
            or record.record_storage_key in storage_keys
            or record.append_order in append_orders
        ):
            raise MarketEventSourceContractError(
                "audited prefix contains duplicate inventory identity"
            )
        event_ids.add(record.event_id)
        source_record_ids.add(record.source_record_id)
        storage_keys.add(record.record_storage_key)
        append_orders.add(record.append_order)
        previous_partition = partition_state.get(record.partition_key)
        expected_partition_previous = (
            _ZERO_HASH if previous_partition is None else previous_partition[2]
        )
        if record.previous_partition_record_hash != expected_partition_previous:
            raise MarketEventSourceContractError(
                "audited partition hash chain is invalid"
            )
        partition_state[record.partition_key] = (
            1 if previous_partition is None else previous_partition[0] + 1,
            record.record_content_hash
            if previous_partition is None
            else previous_partition[1],
            record.record_content_hash,
        )
        record_selected = False
        if selection_query is not None:
            symbol, market, start, end, allowed_event_types = selection_query
            record_selected = (
                record.symbol == symbol
                and record.market is market
                and record.event_type in allowed_event_types
                and start <= record.source_time < end
            )
            if record_selected:
                selected_records.append(record)
                if len(selected_records) > selection_limit:
                    raise MarketEventSourceContractError(
                        "selection exceeds its strict record limit"
                    )
        for expected_finding in _expected_sequence_findings(
            record,
            manifest,
            callback_max,
            provider_max,
            symbol_heads,
            manifests,
        ):
            supplied = next(supplied_findings, None)
            if supplied != expected_finding:
                raise MarketEventSourceContractError(
                    "supplied sequence findings do not equal the expected prefix findings"
                )
            finding_count += 1
            finding_chain = _rolling_digest(
                finding_chain,
                "stage4g1-market-event-finding-chain-node-v1",
                expected_finding.as_dict(),
            )
            if selection_query is not None and expected_finding.intersects(
                *selection_query
            ):
                selected_findings.append(expected_finding)
        if record_count == 1:
            first_hash = record.record_content_hash
        expected_previous = record.record_content_hash
        previous_known_at = record.durable_known_at
        chunk_chain = _rolling_digest(
            chunk_chain,
            "stage4g1-market-event-record-chunk-node-v1",
            record.inventory_leaf(),
        )
        chunk_record_count += 1
        if chunk_record_count == _AUDIT_CHUNK_SIZE:
            chunk_count += 1
            chunk_manifest_chain = _rolling_digest(
                chunk_manifest_chain,
                "stage4g1-market-event-chunk-manifest-node-v1",
                {
                    "chunk_index": chunk_count,
                    "record_count": chunk_record_count,
                    "chunk_chain_head": chunk_chain,
                },
            )
            chunk_chain = _ZERO_HASH
            chunk_record_count = 0
    if any(item.coverage_through > audited_at for item in manifests):
        raise MarketEventSourceContractError("coverage cannot exceed audit known time")
    for manifest in manifests:
        actual = manifest_sequences.get(manifest.manifest_id, [])
        if manifest.first_callback_seq is not None and (
            not actual
            or manifest.first_callback_seq != min(actual)
            or manifest.last_callback_seq != max(actual)
        ):
            raise MarketEventSourceContractError(
                "declared first/last callback bounds disagree with exact prefix"
            )
    if next(supplied_findings, None) is not None:
        raise MarketEventSourceContractError(
            "supplied sequence findings contain fabricated or duplicate entries"
        )
    if chunk_record_count:
        chunk_count += 1
        chunk_manifest_chain = _rolling_digest(
            chunk_manifest_chain,
            "stage4g1-market-event-chunk-manifest-node-v1",
            {
                "chunk_index": chunk_count,
                "record_count": chunk_record_count,
                "chunk_chain_head": chunk_chain,
            },
        )
    partition_chain = _ZERO_HASH
    partition_count = 0
    previous_partition_key: str | None = None
    for head in partition_heads:
        if type(head) is not MarketEventPartitionHead:
            raise MarketEventSourceContractError(
                "partition heads must be MarketEventPartitionHead"
            )
        if (
            previous_partition_key is not None
            and head.partition_key <= previous_partition_key
        ):
            raise MarketEventSourceContractError(
                "partition heads must be unique and canonically ordered"
            )
        expected = partition_state.get(head.partition_key)
        if expected is None or (
            head.event_count != expected[0]
            or head.first_record_hash != expected[1]
            or head.last_record_hash != expected[2]
        ):
            raise MarketEventSourceContractError(
                "partition head disagrees with the audited prefix"
            )
        partition_count += 1
        previous_partition_key = head.partition_key
        partition_chain = _rolling_digest(
            partition_chain,
            "stage4g1-market-event-partition-head-node-v1",
            head.as_dict(),
        )
    if partition_count != len(partition_state):
        raise MarketEventSourceContractError(
            "partition inventory disagrees with the audited prefix"
        )
    return _PrefixScan(
        record_count=record_count,
        high_water_append_order=record_count,
        first_global_record_hash=first_hash,
        last_global_record_hash=expected_previous,
        global_chain_head=expected_previous,
        partition_count=partition_count,
        partition_head_root=_hash_document(
            {
                "schema": "stage4g1-market-event-partition-head-root-v1",
                "partition_count": partition_count,
                "chain_head": partition_chain,
            }
        ),
        finding_count=finding_count,
        finding_set_digest=_hash_document(
            {
                "schema": "stage4g1-market-event-finding-set-root-v1",
                "finding_count": finding_count,
                "chain_head": finding_chain,
            }
        ),
        chunk_count=chunk_count,
        chunk_manifest_root=_hash_document(
            {
                "schema": "stage4g1-market-event-chunk-manifest-root-v1",
                "chunk_count": chunk_count,
                "chain_head": chunk_manifest_chain,
            }
        ),
        source_session_manifest_count=len(manifests),
        source_session_manifest_root=_hash_document(
            {
                "schema": "stage4g1-market-event-source-session-manifest-root-v1",
                "manifest_count": len(manifests),
                "chain_head": manifest_chain,
            }
        ),
        selected_records=tuple(selected_records),
        selected_findings=tuple(selected_findings),
        source_session_manifests=manifests,
        coverage_verified_manifest_ids=tuple(
            item.manifest_id
            for item in manifests
            if (
                (
                    not manifest_sequences.get(item.manifest_id)
                    and item.first_callback_seq is None
                    and item.last_callback_seq is None
                )
                or (
                    bool(manifest_sequences.get(item.manifest_id))
                    and item.first_callback_seq
                    == min(manifest_sequences[item.manifest_id])
                    and item.last_callback_seq
                    == max(manifest_sequences[item.manifest_id])
                )
            )
        ),
    )


class MarketEventTransportKind(StrEnum):
    CONNECTED = "CONNECTED"
    SUBSCRIPTION_ACKNOWLEDGED = "SUBSCRIPTION_ACKNOWLEDGED"
    HEARTBEAT = "HEARTBEAT"
    CALLBACK_WATERMARK = "CALLBACK_WATERMARK"
    QUEUE_WATERMARK = "QUEUE_WATERMARK"
    DISCONNECTED = "DISCONNECTED"
    SESSION_CLOSED = "SESSION_CLOSED"
    REPLAY_STARTED = "REPLAY_STARTED"
    REPLAY_COMPLETED = "REPLAY_COMPLETED"
    BACKFILL_STARTED = "BACKFILL_STARTED"
    BACKFILL_COMPLETED = "BACKFILL_COMPLETED"


class TransportCoverageState(StrEnum):
    INCOMPLETE_LIFECYCLE = "INCOMPLETE_LIFECYCLE"
    MIXED_COVERAGE_PROVEN = "MIXED_COVERAGE_PROVEN"
    INCOMPLETE_INTERVAL_GAP = "INCOMPLETE_INTERVAL_GAP"
    LIVE_COVERAGE_PROVEN = "LIVE_COVERAGE_PROVEN"
    REPLAY_COVERAGE_PROVEN = "REPLAY_COVERAGE_PROVEN"
    BACKFILL_COVERAGE_PROVEN = "BACKFILL_COVERAGE_PROVEN"
    INCOMPLETE_DISCONNECTED = "INCOMPLETE_DISCONNECTED"
    INCOMPLETE_HEARTBEAT_GAP = "INCOMPLETE_HEARTBEAT_GAP"
    INCOMPLETE_SUBSCRIPTION_ACK = "INCOMPLETE_SUBSCRIPTION_ACK"
    INCOMPLETE_QUEUE_LOSS = "INCOMPLETE_QUEUE_LOSS"
    INCOMPLETE_NO_FINAL_WATERMARK = "INCOMPLETE_NO_FINAL_WATERMARK"
    INCOMPLETE_CLOCK_POLICY = "INCOMPLETE_CLOCK_POLICY"
    INCOMPLETE_REPLAY_JOB = "INCOMPLETE_REPLAY_JOB"
    INCOMPLETE_EVENT_BINDING = "INCOMPLETE_EVENT_BINDING"


@dataclass(frozen=True, slots=True)
class TransportLivenessPolicy(StructuralFixture):
    liveness_policy_id: str = "stage4g1-fixture-transport-liveness-v2"
    maximum_heartbeat_gap: timedelta = field(init=False, default=timedelta(minutes=5))
    maximum_watermark_gap: timedelta = field(init=False, default=timedelta(minutes=5))
    require_subscription_ack: bool = field(init=False, default=True)
    require_clean_session_close_for_complete_session: bool = field(
        init=False, default=True
    )
    allow_callback_activity_as_heartbeat: bool = field(init=False, default=False)
    required_transport_kinds: tuple[MarketEventTransportKind, ...] = field(
        init=False,
        default=(
            MarketEventTransportKind.CONNECTED,
            MarketEventTransportKind.SUBSCRIPTION_ACKNOWLEDGED,
            MarketEventTransportKind.HEARTBEAT,
            MarketEventTransportKind.CALLBACK_WATERMARK,
            MarketEventTransportKind.QUEUE_WATERMARK,
        ),
    )
    clock_policy_id: str = field(init=False, default="stage4g1-fixture-source-clock-v1")

    def __post_init__(self) -> None:
        if (
            type(self.liveness_policy_id) is not str
            or self.liveness_policy_id != "stage4g1-fixture-transport-liveness-v2"
        ):
            raise MarketEventSourceContractError(
                "unsupported transport liveness policy"
            )


@dataclass(frozen=True, slots=True)
class MarketEventTransportRecord(StructuralFixture):
    source_store_id: str
    transport_stream_id: str
    transport_append_order: int
    previous_transport_record_hash: str
    session_id: str
    connection_epoch: int
    reconnect_epoch: int
    kind: MarketEventTransportKind
    observed_at: datetime
    durable_known_at: datetime
    subscription_scope_id: str | None
    callback_high_water: int | None
    provider_high_water: int | None
    queue_overflow_count: int
    dropped_callback_count: int
    canonical_payload: str
    job_input_selection: MarketEventSelection | None = None
    job_input_verification: MarketEventSelectionVerification | None = None
    job_output_high_water: int | None = None
    job_output_chain_head: str | None = None
    record_hash: str = field(init=False)
    record_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "source_store_id",
            "transport_stream_id",
            "previous_transport_record_hash",
        ):
            _require_sha256(getattr(self, name), name)
        _require_text(self.session_id, "session_id", maximum=256)
        _require_int(self.transport_append_order, "transport_append_order", minimum=1)
        _require_int(self.connection_epoch, "connection_epoch", minimum=1)
        _require_int(self.reconnect_epoch, "reconnect_epoch")
        for name in (
            "callback_high_water",
            "provider_high_water",
            "job_output_high_water",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_int(value, name)
        for name in ("queue_overflow_count", "dropped_callback_count"):
            _require_int(getattr(self, name), name)
        for name in ("subscription_scope_id", "job_output_chain_head"):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        if type(self.kind) is not MarketEventTransportKind:
            raise MarketEventSourceContractError("typed transport kind required")
        observed = _require_utc(self.observed_at, "observed_at")
        durable = _require_utc(self.durable_known_at, "durable_known_at")
        if observed > durable:
            raise MarketEventSourceContractError(
                "transport observed time exceeds durable time"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "durable_known_at", durable)
        if type(self.canonical_payload) is not str or self.canonical_payload != "{}":
            raise MarketEventSourceContractError(
                "fixture transport payload schema only permits canonical empty object"
            )
        job = self.kind.value.startswith(("REPLAY_", "BACKFILL_"))
        if job:
            if self.job_input_selection is None or self.job_input_verification is None:
                raise MarketEventSourceContractError(
                    "job requires typed input selection and verification"
                )
            validate_selection_verification(
                self.job_input_selection, self.job_input_verification
            )
            if self.job_input_selection.snapshot_audited_at > observed:
                raise MarketEventSourceContractError("job predates exact input audit")
            if self.job_output_high_water is None or self.job_output_chain_head is None:
                raise MarketEventSourceContractError(
                    "job requires output prefix boundary"
                )
        elif any(
            value is not None
            for value in (
                self.job_input_selection,
                self.job_input_verification,
                self.job_output_high_water,
                self.job_output_chain_head,
            )
        ):
            raise MarketEventSourceContractError(
                "LIVE transport cannot carry replay job fields"
            )
        object.__setattr__(
            self, "record_hash", _hash_document(self.as_dict(include_id=False))
        )
        object.__setattr__(
            self,
            "record_id",
            _hash_document(
                {
                    "schema": "stage4g1-transport-record-id-v1",
                    "record_hash": self.record_hash,
                }
            ),
        )

    @property
    def epoch_key(self) -> tuple[str, int, int]:
        return self.session_id, self.connection_epoch, self.reconnect_epoch

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-transport-record-v1",
            "assurance": self.assurance.value,
            "source_store_id": self.source_store_id,
            "transport_stream_id": self.transport_stream_id,
            "transport_append_order": self.transport_append_order,
            "previous_transport_record_hash": self.previous_transport_record_hash,
            "session_id": self.session_id,
            "connection_epoch": self.connection_epoch,
            "reconnect_epoch": self.reconnect_epoch,
            "kind": self.kind.value,
            "observed_at": _utc_text(self.observed_at),
            "durable_known_at": _utc_text(self.durable_known_at),
            "subscription_scope_id": self.subscription_scope_id,
            "callback_high_water": self.callback_high_water,
            "provider_high_water": self.provider_high_water,
            "queue_overflow_count": self.queue_overflow_count,
            "dropped_callback_count": self.dropped_callback_count,
            "canonical_payload": self.canonical_payload,
            "job_input_selection_id": self.job_input_selection.selection_id
            if self.job_input_selection
            else None,
            "job_input_snapshot_id": self.job_input_selection.snapshot_id
            if self.job_input_selection
            else None,
            "job_input_high_water": self.job_input_selection.snapshot_high_water_append_order
            if self.job_input_selection
            else None,
            "job_input_verification_id": self.job_input_verification.verification_id
            if self.job_input_verification
            else None,
            "job_output_high_water": self.job_output_high_water,
            "job_output_chain_head": self.job_output_chain_head,
        }
        if include_id:
            document.update(record_hash=self.record_hash, record_id=self.record_id)
        return document


@dataclass(frozen=True, slots=True)
class MarketEventTransportSnapshot(StructuralFixture):
    source_store_id: str
    records: tuple[MarketEventTransportRecord, ...]
    audited_at: datetime
    audit_id: str = field(init=False)
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.source_store_id, "source_store_id")
        audited = _require_utc(self.audited_at, "transport audited_at")
        object.__setattr__(self, "audited_at", audited)
        if type(self.records) is not tuple:
            raise MarketEventSourceContractError(
                "transport records must be an exact tuple"
            )
        previous = _ZERO_HASH
        durable: datetime | None = None
        heads: dict[tuple[str, int, int], MarketEventTransportRecord] = {}
        watermarks: dict[tuple[tuple[str, int, int], str], int] = {}
        starts: set[tuple[str, int, int]] = set()
        closes: set[tuple[str, int, int]] = set()
        stream: str | None = None
        for order, record in enumerate(self.records, 1):
            if (
                type(record) is not MarketEventTransportRecord
                or replace(record) != record
            ):
                raise MarketEventSourceContractError("invalid transport record")
            if (
                record.source_store_id != self.source_store_id
                or record.transport_append_order != order
                or record.previous_transport_record_hash != previous
                or record.durable_known_at > audited
                or (durable is not None and record.durable_known_at < durable)
                or (stream is not None and record.transport_stream_id != stream)
            ):
                raise MarketEventSourceContractError(
                    "transport store/prefix/known-time identity mismatch"
                )
            head = heads.get(record.epoch_key)
            for name in ("callback_high_water", "provider_high_water"):
                value = getattr(record, name)
                key = record.epoch_key, name
                if value is not None:
                    if value < watermarks.get(key, value):
                        raise MarketEventSourceContractError(
                            "transport watermark regression"
                        )
                    watermarks[key] = value
            if head is not None and (
                record.observed_at < head.observed_at
                or record.queue_overflow_count < head.queue_overflow_count
                or record.dropped_callback_count < head.dropped_callback_count
                or (
                    head.callback_high_water is not None
                    and record.callback_high_water is not None
                    and record.callback_high_water < head.callback_high_water
                )
            ):
                raise MarketEventSourceContractError(
                    "transport clock/counters/watermarks are not monotonic"
                )
            if record.epoch_key in closes:
                raise MarketEventSourceContractError(
                    "transport lifecycle branches after close"
                )
            if record.kind in {
                MarketEventTransportKind.CONNECTED,
                MarketEventTransportKind.REPLAY_STARTED,
                MarketEventTransportKind.BACKFILL_STARTED,
            }:
                if record.epoch_key in starts:
                    raise MarketEventSourceContractError(
                        "transport lifecycle has duplicate start/branch"
                    )
                starts.add(record.epoch_key)
            if record.kind in {
                MarketEventTransportKind.SESSION_CLOSED,
                MarketEventTransportKind.REPLAY_COMPLETED,
                MarketEventTransportKind.BACKFILL_COMPLETED,
            }:
                closes.add(record.epoch_key)
            heads[record.epoch_key] = record
            previous, durable, stream = (
                record.record_hash,
                record.durable_known_at,
                record.transport_stream_id,
            )
        object.__setattr__(
            self,
            "audit_id",
            _hash_document(
                {
                    "schema": "stage4g1-transport-audit-v1",
                    **self.as_dict(include_id=False),
                }
            ),
        )
        object.__setattr__(
            self,
            "snapshot_id",
            _hash_document(
                {
                    "schema": "stage4g1-transport-snapshot-id-v1",
                    "audit_id": self.audit_id,
                }
            ),
        )

    @classmethod
    def from_records(
        cls,
        *,
        source_store_id: str,
        records: tuple[MarketEventTransportRecord, ...],
        audited_at: datetime,
    ) -> MarketEventTransportSnapshot:
        return cls(source_store_id, records, audited_at)

    @property
    def high_water_append_order(self) -> int:
        return len(self.records)

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-transport-snapshot-v1",
            "assurance": self.assurance.value,
            "source_store_id": self.source_store_id,
            "audited_at": _utc_text(self.audited_at),
            "record_count": len(self.records),
            "high_water_append_order": len(self.records),
            "chain_head": self.records[-1].record_hash if self.records else _ZERO_HASH,
            "ordered_record_ids": [r.record_id for r in self.records],
            "session_epoch_inventory": sorted({r.epoch_key for r in self.records}),
            "acknowledgements": [
                r.record_id
                for r in self.records
                if r.kind is MarketEventTransportKind.SUBSCRIPTION_ACKNOWLEDGED
            ],
            "epoch_liveness_inventory": [
                _transport_epoch_summary(
                    tuple(r for r in self.records if r.epoch_key == key)
                )
                for key in sorted({r.epoch_key for r in self.records})
            ],
        }
        if include_id:
            document.update(audit_id=self.audit_id, snapshot_id=self.snapshot_id)
        return document


def _transport_epoch_summary(
    rows: tuple[MarketEventTransportRecord, ...],
) -> dict[str, Any]:
    def intervals(kind: MarketEventTransportKind) -> list[list[str]]:
        times = [r.observed_at for r in rows if r.kind is kind]
        return [[_utc_text(left), _utc_text(right)] for left, right in pairwise(times)]

    starts = [r for r in rows if r.kind is MarketEventTransportKind.CONNECTED]
    acknowledgements = [
        r for r in rows if r.kind is MarketEventTransportKind.SUBSCRIPTION_ACKNOWLEDGED
    ]
    ends = [
        r
        for r in rows
        if r.kind
        in {
            MarketEventTransportKind.DISCONNECTED,
            MarketEventTransportKind.SESSION_CLOSED,
        }
    ]
    end = _utc_text(ends[0].observed_at) if ends else None
    return {
        "epoch": rows[0].epoch_key if rows else None,
        "connected_intervals": [[_utc_text(r.observed_at), end] for r in starts],
        "subscribed_intervals": [
            [r.subscription_scope_id, _utc_text(r.observed_at), end]
            for r in acknowledgements
        ],
        "heartbeat_intervals": intervals(MarketEventTransportKind.HEARTBEAT),
        "callback_watermark_intervals": intervals(
            MarketEventTransportKind.CALLBACK_WATERMARK
        ),
        "queue_watermark_intervals": intervals(
            MarketEventTransportKind.QUEUE_WATERMARK
        ),
        "counter_start": [rows[0].queue_overflow_count, rows[0].dropped_callback_count]
        if rows
        else None,
        "counter_end": [rows[-1].queue_overflow_count, rows[-1].dropped_callback_count]
        if rows
        else None,
        "clean_close": bool(ends)
        and ends[0].kind is MarketEventTransportKind.SESSION_CLOSED,
        "watermark_bindings": [
            [r.record_id, r.callback_high_water, r.provider_high_water]
            for r in rows
            if r.kind is MarketEventTransportKind.CALLBACK_WATERMARK
        ],
    }


@dataclass(frozen=True, slots=True, init=False)
class MarketEventStoreAudit(StructuralFixture):
    source_store_id: str
    source_schema_id: str
    scope: MarketEventStoreAuditScope
    sequence_policy_id: str
    high_water_append_order: int
    record_count: int
    audited_at: datetime
    catalog_schema_fingerprint: str
    inventory_verification_id: str
    inventory_root: str
    global_chain_head: str
    partition_count: int
    partition_head_root: str
    finding_count: int
    finding_set_digest: str
    chunk_count: int
    chunk_manifest_root: str
    source_session_manifest_count: int
    source_session_manifest_root: str
    first_global_record_hash: str
    last_global_record_hash: str
    audit_id: str
    transport_snapshot: MarketEventTransportSnapshot | None

    def __init__(self) -> None:
        raise TypeError("MarketEventStoreAudit must be created from a verified prefix")

    @classmethod
    def create_from_prefix(
        cls,
        *,
        source_store_id: str,
        source_schema_id: str,
        sequence_policy_id: str,
        audited_at: datetime,
        catalog_schema_fingerprint: str,
        inventory_verification: MarketEventInventoryVerification,
        records: Iterable[MarketEventSourceRecord],
        partition_heads: Iterable[MarketEventPartitionHead],
        findings: Iterable[MarketEventSequenceFinding],
        source_session_manifests: Iterable[MarketEventSourceSessionManifest],
        transport_snapshot: MarketEventTransportSnapshot | None = None,
    ) -> MarketEventStoreAudit:
        source_store_id = _require_sha256(source_store_id, "source_store_id")
        source_schema_id = _require_text(
            source_schema_id,
            "source_schema_id",
            maximum=256,
        )
        audited_at = _require_utc(audited_at, "audited_at")
        if transport_snapshot is not None and (
            type(transport_snapshot) is not MarketEventTransportSnapshot
            or replace(transport_snapshot) != transport_snapshot
            or transport_snapshot.source_store_id != source_store_id
            or transport_snapshot.audited_at > audited_at
        ):
            raise MarketEventSourceContractError(
                "transport store/audit does not match Event freeze"
            )
        catalog_schema_fingerprint = _require_sha256(
            catalog_schema_fingerprint,
            "catalog_schema_fingerprint",
        )
        if type(inventory_verification) is not MarketEventInventoryVerification:
            raise MarketEventSourceContractError(
                "an exact catalog inventory verification is required"
            )
        records = tuple(records)
        expected_inventory = MarketEventInventoryVerification.create_from_prefix(
            source_store_id=source_store_id,
            catalog_schema_fingerprint=catalog_schema_fingerprint,
            records=records,
        )
        if inventory_verification != expected_inventory:
            raise MarketEventSourceContractError(
                "catalog inventory verification disagrees with the audited prefix"
            )
        scan = _scan_prefix(
            records=records,
            partition_heads=partition_heads,
            findings=findings,
            source_session_manifests=source_session_manifests,
            source_store_id=source_store_id,
            audited_at=audited_at,
            sequence_policy_id=sequence_policy_id,
        )
        self = object.__new__(cls)
        values: dict[str, object] = {
            "source_store_id": source_store_id,
            "transport_snapshot": transport_snapshot,
            "source_schema_id": source_schema_id,
            "scope": MarketEventStoreAuditScope.FULL_PREFIX,
            "sequence_policy_id": sequence_policy_id,
            "high_water_append_order": scan.high_water_append_order,
            "record_count": scan.record_count,
            "audited_at": audited_at,
            "catalog_schema_fingerprint": catalog_schema_fingerprint,
            "inventory_verification_id": inventory_verification.verification_id,
            "inventory_root": inventory_verification.inventory_root,
            "global_chain_head": scan.global_chain_head,
            "partition_count": scan.partition_count,
            "partition_head_root": scan.partition_head_root,
            "finding_count": scan.finding_count,
            "finding_set_digest": scan.finding_set_digest,
            "chunk_count": scan.chunk_count,
            "chunk_manifest_root": scan.chunk_manifest_root,
            "source_session_manifest_count": scan.source_session_manifest_count,
            "source_session_manifest_root": scan.source_session_manifest_root,
            "first_global_record_hash": scan.first_global_record_hash,
            "last_global_record_hash": scan.last_global_record_hash,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self, "audit_id", _hash_document(self.as_dict(include_id=False))
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": MARKET_EVENT_STORE_AUDIT_SCHEMA,
            "transport_snapshot_id": self.transport_snapshot.snapshot_id
            if self.transport_snapshot
            else None,
            "transport_audit_id": self.transport_snapshot.audit_id
            if self.transport_snapshot
            else None,
            "transport_high_water": self.transport_snapshot.high_water_append_order
            if self.transport_snapshot
            else None,
            "source_store_id": self.source_store_id,
            "source_schema_id": self.source_schema_id,
            "scope": self.scope.value,
            "sequence_policy_id": self.sequence_policy_id,
            "high_water_append_order": self.high_water_append_order,
            "record_count": self.record_count,
            "audited_at": _utc_text(self.audited_at),
            "catalog_schema_fingerprint": self.catalog_schema_fingerprint,
            "inventory_verification_id": self.inventory_verification_id,
            "inventory_root": self.inventory_root,
            "global_chain_head": self.global_chain_head,
            "partition_count": self.partition_count,
            "partition_head_root": self.partition_head_root,
            "finding_count": self.finding_count,
            "finding_set_digest": self.finding_set_digest,
            "chunk_count": self.chunk_count,
            "chunk_manifest_root": self.chunk_manifest_root,
            "source_session_manifest_count": self.source_session_manifest_count,
            "source_session_manifest_root": self.source_session_manifest_root,
            "first_global_record_hash": self.first_global_record_hash,
            "last_global_record_hash": self.last_global_record_hash,
        }
        if include_id:
            document["audit_id"] = self.audit_id
        return document


def _selected_finding_digest(
    findings: tuple[MarketEventSequenceFinding, ...],
) -> str:
    chain = _ZERO_HASH
    for finding in findings:
        chain = _rolling_digest(
            chain,
            "stage4g1-market-event-selected-finding-node-v1",
            finding.as_dict(),
        )
    return _hash_document(
        {
            "schema": "stage4g1-market-event-selected-finding-root-v1",
            "finding_count": len(findings),
            "chain_head": chain,
        }
    )


@dataclass(frozen=True, slots=True)
class MarketEventSelectionCommitment(StructuralFixture):
    source_store_id: str
    snapshot_id: str
    snapshot_audit_id: str
    snapshot_high_water_append_order: int
    symbol: str
    market: Market
    start_source_time: datetime
    end_source_time: datetime
    interval_boundary_policy_id: str
    allowed_event_types: tuple[str, ...]
    record_limit: int
    ordered_inventory_leaf_ids: tuple[str, ...]
    relevant_finding_set_digest: str
    commitment_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.snapshot_id, "snapshot_id")
        _require_sha256(self.snapshot_audit_id, "snapshot_audit_id")
        _require_int(
            self.snapshot_high_water_append_order,
            "snapshot_high_water_append_order",
        )
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        start = _require_utc(self.start_source_time, "start_source_time")
        end = _require_utc(self.end_source_time, "end_source_time")
        if end <= start:
            raise MarketEventSourceContractError("selection interval must be non-empty")
        object.__setattr__(self, "start_source_time", start)
        object.__setattr__(self, "end_source_time", end)
        if self.interval_boundary_policy_id != MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1:
            raise MarketEventSourceContractError(
                "unsupported market-event interval boundary policy"
            )
        if (
            type(self.allowed_event_types) is not tuple
            or not self.allowed_event_types
            or any(
                type(item) is not str or not item for item in self.allowed_event_types
            )
            or self.allowed_event_types != tuple(sorted(set(self.allowed_event_types)))
        ):
            raise MarketEventSourceContractError(
                "allowed event types must be a canonical non-empty tuple"
            )
        _require_int(
            self.record_limit,
            "record_limit",
            minimum=1,
            maximum=_MAX_SELECTION_RECORDS,
        )
        if type(self.ordered_inventory_leaf_ids) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.ordered_inventory_leaf_ids
        ):
            raise MarketEventSourceContractError(
                "selection inventory leaf IDs must be an exact SHA-256 tuple"
            )
        if len(set(self.ordered_inventory_leaf_ids)) != len(
            self.ordered_inventory_leaf_ids
        ):
            raise MarketEventSourceContractError(
                "selection inventory leaf IDs must be unique"
            )
        _require_sha256(
            self.relevant_finding_set_digest,
            "relevant_finding_set_digest",
        )
        object.__setattr__(
            self,
            "commitment_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-selection-commitment-v2",
            "source_store_id": self.source_store_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_audit_id": self.snapshot_audit_id,
            "snapshot_high_water_append_order": self.snapshot_high_water_append_order,
            "symbol": self.symbol,
            "market": self.market.value,
            "start_source_time": _utc_text(self.start_source_time),
            "end_source_time": _utc_text(self.end_source_time),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "allowed_event_types": list(self.allowed_event_types),
            "record_limit": self.record_limit,
            "ordered_inventory_leaf_ids": list(self.ordered_inventory_leaf_ids),
            "relevant_finding_set_digest": self.relevant_finding_set_digest,
        }
        if include_id:
            document["commitment_id"] = self.commitment_id
        return document


@dataclass(frozen=True, slots=True)
class MarketEventSelectionMembershipWitness(StructuralFixture):
    inventory_verification_id: str
    inventory_root: str
    ordered_source_record_ids: tuple[str, ...]
    ordered_record_content_hashes: tuple[str, ...]
    relevant_finding_ids: tuple[str, ...]
    source_session_manifest_ids: tuple[str, ...]
    verification_mode: MarketEventSelectionVerificationMode
    witness_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("inventory_verification_id", "inventory_root"):
            _require_sha256(getattr(self, name), name)
        for values, name in (
            (self.ordered_source_record_ids, "ordered_source_record_ids"),
            (self.ordered_record_content_hashes, "ordered_record_content_hashes"),
            (self.relevant_finding_ids, "relevant_finding_ids"),
            (self.source_session_manifest_ids, "source_session_manifest_ids"),
        ):
            if type(values) is not tuple or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in values
            ):
                raise MarketEventSourceContractError(
                    f"{name} must be an exact SHA-256 tuple"
                )
            if len(values) != len(set(values)):
                raise MarketEventSourceContractError(f"{name} must be unique")
        if len(self.ordered_source_record_ids) != len(
            self.ordered_record_content_hashes
        ):
            raise MarketEventSourceContractError(
                "selection membership record columns have unequal length"
            )
        if (
            self.verification_mode
            is not MarketEventSelectionVerificationMode.FULL_PREFIX_RESCAN
        ):
            raise MarketEventSourceContractError(
                "selection membership requires full-prefix rescan verification"
            )
        object.__setattr__(
            self,
            "witness_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-selection-membership-witness-v2",
            "inventory_verification_id": self.inventory_verification_id,
            "inventory_root": self.inventory_root,
            "ordered_source_record_ids": list(self.ordered_source_record_ids),
            "ordered_record_content_hashes": list(self.ordered_record_content_hashes),
            "relevant_finding_ids": list(self.relevant_finding_ids),
            "source_session_manifest_ids": list(self.source_session_manifest_ids),
            "verification_mode": self.verification_mode.value,
        }
        if include_id:
            document["witness_id"] = self.witness_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class MarketEventSelectionVerification(StructuralFixture):
    selection_id: str
    selection_commitment_id: str
    membership_witness_id: str
    snapshot_id: str
    snapshot_audit_id: str
    snapshot_high_water_append_order: int
    inventory_verification_id: str
    verification_mode: MarketEventSelectionVerificationMode
    verification_id: str

    def __init__(self) -> None:
        raise TypeError(
            "MarketEventSelectionVerification requires an exact read-port rescan"
        )

    @classmethod
    def _from_exact_rescan(
        cls,
        selection: MarketEventSelection,
    ) -> MarketEventSelectionVerification:
        self = object.__new__(cls)
        values: dict[str, object] = {
            "selection_id": selection.selection_id,
            "selection_commitment_id": selection.commitment.commitment_id,
            "membership_witness_id": selection.membership_witness.witness_id,
            "snapshot_id": selection.snapshot_id,
            "snapshot_audit_id": selection.snapshot_audit_id,
            "snapshot_high_water_append_order": selection.snapshot_high_water_append_order,
            "inventory_verification_id": selection.membership_witness.inventory_verification_id,
            "verification_mode": MarketEventSelectionVerificationMode.FULL_PREFIX_RESCAN,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "verification_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-market-event-selection-verification-v2",
            "selection_id": self.selection_id,
            "selection_commitment_id": self.selection_commitment_id,
            "membership_witness_id": self.membership_witness_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_audit_id": self.snapshot_audit_id,
            "snapshot_high_water_append_order": self.snapshot_high_water_append_order,
            "inventory_verification_id": self.inventory_verification_id,
            "verification_mode": self.verification_mode.value,
        }
        if include_id:
            document["verification_id"] = self.verification_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class MarketEventStoreSnapshot(StructuralFixture):
    audit: MarketEventStoreAudit
    snapshot_id: str

    def __init__(self) -> None:
        raise TypeError("MarketEventStoreSnapshot must be created from an audit")

    @classmethod
    def from_audit(cls, audit: MarketEventStoreAudit) -> MarketEventStoreSnapshot:
        if type(audit) is not MarketEventStoreAudit:
            raise MarketEventSourceContractError("audit must be MarketEventStoreAudit")
        self = object.__new__(cls)
        object.__setattr__(self, "audit", audit)
        object.__setattr__(
            self,
            "snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": MARKET_EVENT_STORE_SNAPSHOT_SCHEMA,
            "audit": self.audit.as_dict(),
        }
        if include_id:
            document["snapshot_id"] = self.snapshot_id
        return document

    def select_from_prefix(
        self,
        *,
        records: Iterable[MarketEventSourceRecord],
        partition_heads: Iterable[MarketEventPartitionHead],
        findings: Iterable[MarketEventSequenceFinding],
        source_session_manifests: Iterable[MarketEventSourceSessionManifest],
        symbol: str,
        market: Market,
        start_source_time: datetime,
        end_source_time: datetime,
        allowed_event_types: tuple[str, ...],
        interval_boundary_policy_id: str,
        record_limit: int,
    ) -> MarketEventSelection:
        market = _require_market(market)
        symbol = _require_symbol(symbol, market)
        start = _require_utc(start_source_time, "start_source_time")
        end = _require_utc(end_source_time, "end_source_time")
        if end <= start:
            raise MarketEventSourceContractError("selection interval must be non-empty")
        if interval_boundary_policy_id != MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1:
            raise MarketEventSourceContractError(
                "unsupported market-event interval boundary policy"
            )
        if (
            type(allowed_event_types) is not tuple
            or not allowed_event_types
            or allowed_event_types != tuple(sorted(set(allowed_event_types)))
            or any(type(item) is not str or not item for item in allowed_event_types)
        ):
            raise MarketEventSourceContractError(
                "allowed event types must be a canonical non-empty tuple"
            )
        limit = _require_int(
            record_limit,
            "record_limit",
            minimum=1,
            maximum=_MAX_SELECTION_RECORDS,
        )
        records = tuple(records)
        partition_heads = tuple(partition_heads)
        findings = tuple(findings)
        source_session_manifests = tuple(source_session_manifests)
        scan = _scan_prefix(
            records=records,
            partition_heads=partition_heads,
            findings=findings,
            source_session_manifests=source_session_manifests,
            source_store_id=self.audit.source_store_id,
            audited_at=self.audit.audited_at,
            sequence_policy_id=self.audit.sequence_policy_id,
            selection_query=(symbol, market, start, end, allowed_event_types),
            selection_limit=limit,
        )
        expected_components = (
            self.audit.record_count,
            self.audit.high_water_append_order,
            self.audit.first_global_record_hash,
            self.audit.last_global_record_hash,
            self.audit.global_chain_head,
            self.audit.partition_count,
            self.audit.partition_head_root,
            self.audit.finding_count,
            self.audit.finding_set_digest,
            self.audit.chunk_count,
            self.audit.chunk_manifest_root,
            self.audit.source_session_manifest_count,
            self.audit.source_session_manifest_root,
        )
        actual_components = (
            scan.record_count,
            scan.high_water_append_order,
            scan.first_global_record_hash,
            scan.last_global_record_hash,
            scan.global_chain_head,
            scan.partition_count,
            scan.partition_head_root,
            scan.finding_count,
            scan.finding_set_digest,
            scan.chunk_count,
            scan.chunk_manifest_root,
            scan.source_session_manifest_count,
            scan.source_session_manifest_root,
        )
        if actual_components != expected_components:
            raise MarketEventSourceContractError(
                "selection prefix disagrees with the exact frozen audit"
            )
        return MarketEventSelection._from_verified_snapshot(
            snapshot=self,
            symbol=symbol,
            market=market,
            start=start,
            end=end,
            records=scan.selected_records,
            findings=scan.selected_findings,
            source_session_manifests=scan.source_session_manifests,
            coverage_verified_manifest_ids=scan.coverage_verified_manifest_ids,
            allowed_event_types=allowed_event_types,
            interval_boundary_policy_id=interval_boundary_policy_id,
            record_limit=limit,
            transport_certificate=TransportCoverageCertificate(
                self,
                records,
                partition_heads,
                findings,
                source_session_manifests,
                symbol,
                market,
                start,
                end,
                allowed_event_types,
            ),
        )

    def verify_selection(
        self,
        selection: MarketEventSelection,
        *,
        records: Iterable[MarketEventSourceRecord],
        partition_heads: Iterable[MarketEventPartitionHead],
        findings: Iterable[MarketEventSequenceFinding],
        source_session_manifests: Iterable[MarketEventSourceSessionManifest],
    ) -> MarketEventSelectionVerification:
        if type(selection) is not MarketEventSelection:
            raise MarketEventSourceContractError(
                "selection must be MarketEventSelection"
            )
        expected = self.select_from_prefix(
            records=records,
            partition_heads=partition_heads,
            findings=findings,
            source_session_manifests=source_session_manifests,
            symbol=selection.symbol,
            market=selection.market,
            start_source_time=selection.start_source_time,
            end_source_time=selection.end_source_time,
            allowed_event_types=selection.allowed_event_types,
            interval_boundary_policy_id=selection.interval_boundary_policy_id,
            record_limit=selection.record_limit,
        )
        selection._validate_contents()
        if selection.as_dict() != expected.as_dict():
            raise MarketEventSourceContractError(
                "selection does not equal the exact read-port result"
            )
        return MarketEventSelectionVerification._from_exact_rescan(selection)


@dataclass(frozen=True, slots=True)
class TransportCoverageCertificate(StructuralFixture):
    event_snapshot: MarketEventStoreSnapshot
    event_records: tuple[MarketEventSourceRecord, ...]
    partition_heads: tuple[MarketEventPartitionHead, ...]
    findings: tuple[MarketEventSequenceFinding, ...]
    manifests: tuple[MarketEventSourceSessionManifest, ...]
    symbol: str
    market: Market
    start: datetime
    end: datetime
    event_types: tuple[str, ...]
    liveness_policy_id: str = "stage4g1-fixture-transport-liveness-v2"
    certificate_id: str = field(init=False)

    def __post_init__(self) -> None:
        TransportLivenessPolicy(self.liveness_policy_id)
        _require_symbol(self.symbol, _require_market(self.market))
        start, end = _require_utc(self.start, "start"), _require_utc(self.end, "end")
        if start >= end or type(self.event_types) is not tuple or not self.event_types:
            raise MarketEventSourceContractError("invalid transport certificate query")
        if self.event_types != tuple(sorted(set(self.event_types))):
            raise MarketEventSourceContractError(
                "transport event types are not canonical"
            )
        for value in self.event_types:
            _require_text(value, "event_type", maximum=128)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if type(self.event_snapshot) is not MarketEventStoreSnapshot or any(
            type(value) is not tuple
            for value in (
                self.event_records,
                self.partition_heads,
                self.findings,
                self.manifests,
            )
        ):
            raise MarketEventSourceContractError(
                "exact transport/Event prefix inputs required"
            )
        audit = self.event_snapshot.audit
        expected = MarketEventStoreAudit.create_from_prefix(
            source_store_id=audit.source_store_id,
            source_schema_id=audit.source_schema_id,
            sequence_policy_id=audit.sequence_policy_id,
            audited_at=audit.audited_at,
            catalog_schema_fingerprint=audit.catalog_schema_fingerprint,
            inventory_verification=MarketEventInventoryVerification.create_from_prefix(
                source_store_id=audit.source_store_id,
                catalog_schema_fingerprint=audit.catalog_schema_fingerprint,
                records=self.event_records,
            ),
            records=self.event_records,
            partition_heads=self.partition_heads,
            findings=self.findings,
            source_session_manifests=self.manifests,
            transport_snapshot=audit.transport_snapshot,
        )
        if (
            expected != audit
            or MarketEventStoreSnapshot.from_audit(expected) != self.event_snapshot
        ):
            raise MarketEventSourceContractError(
                "certificate does not match exact frozen Event/Transport prefix"
            )
        object.__setattr__(
            self, "certificate_id", _hash_document(self.as_dict(include_id=False))
        )

    @staticmethod
    def _periodic(
        records: tuple[MarketEventTransportRecord, ...],
        kind: MarketEventTransportKind,
        start: datetime,
        end: datetime,
        gap: timedelta,
    ) -> bool:
        stamps = sorted({r.observed_at for r in records if r.kind is kind})
        before = [t for t in stamps if t <= start]
        after = [t for t in stamps if t >= end]
        if not before or not after:
            return False
        selected = [t for t in stamps if before[-1] <= t <= after[0]]
        return all(right - left <= gap for left, right in pairwise(selected))

    def _rows(
        self, manifest: MarketEventSourceSessionManifest
    ) -> tuple[MarketEventTransportRecord, ...]:
        transport = self.event_snapshot.audit.transport_snapshot
        key = manifest.session_id, manifest.connection_epoch, manifest.reconnect_epoch
        return (
            ()
            if transport is None
            else tuple(r for r in transport.records if r.epoch_key == key)
        )

    def _replay_state(
        self,
        manifest: MarketEventSourceSessionManifest,
        rows: tuple[MarketEventTransportRecord, ...],
        start: datetime,
        end: datetime,
    ) -> TransportCoverageState:
        incomplete = TransportCoverageState.INCOMPLETE_REPLAY_JOB
        replay, verification = manifest.replay_selection, manifest.replay_verification
        if (
            replay is None
            or verification is None
            or replay.snapshot_id == self.event_snapshot.snapshot_id
        ):
            return incomplete
        validate_selection_verification(replay, verification)
        if not replay.covers_interval(start, end):
            return incomplete
        origin = manifest.coverage_origin.value
        starts = [r for r in rows if r.kind.value == origin + "_STARTED"]
        ends = [r for r in rows if r.kind.value == origin + "_COMPLETED"]
        if len(starts) != 1 or len(ends) != 1:
            return incomplete
        first, last = starts[0], ends[0]
        if (
            first.transport_append_order >= last.transport_append_order
            or first.observed_at < manifest.collector_started_at
            or any(
                r.job_input_selection != replay
                or r.job_input_verification != verification
                for r in (first, last)
            )
            or first.job_output_high_water != 0
            or first.job_output_chain_head != _ZERO_HASH
            or last.job_output_high_water != len(self.event_records)
            or last.job_output_chain_head
            != (
                self.event_records[-1].record_content_hash
                if self.event_records
                else _ZERO_HASH
            )
            or any(r.queue_overflow_count or r.dropped_callback_count for r in rows)
            or any(r.durable_known_at > last.observed_at for r in self.event_records)
        ):
            return incomplete

        def semantic(record: MarketEventSourceRecord) -> tuple[object, ...]:
            return (
                record.symbol,
                record.market,
                record.event_type,
                record.source_time,
                record.payload_json,
                record.raw_payload_sha256,
                record.parser_id,
                record.source_schema_id,
            )

        output = tuple(
            r
            for r in self.event_records
            if r.source_session_manifest_id == manifest.manifest_id
        )
        if tuple(map(semantic, output)) != tuple(map(semantic, replay.records)):
            return incomplete
        return TransportCoverageState(origin + "_COVERAGE_PROVEN")

    @staticmethod
    def _live_lifecycle_is_ordered(
        rows: tuple[MarketEventTransportRecord, ...],
        subscription_scope_id: str,
    ) -> bool:
        # The snapshot validates contiguous append order; never sort raw evidence
        # by timestamp, since equal timestamps do not establish causal order.
        connected = acknowledged = disconnected = closed = False
        previous_order = 0
        for row in rows:
            if row.transport_append_order <= previous_order or closed:
                return False
            previous_order = row.transport_append_order
            if disconnected and row.kind is not MarketEventTransportKind.SESSION_CLOSED:
                return False
            if row.kind is MarketEventTransportKind.CONNECTED:
                if connected:
                    return False
                connected = True
            elif not connected:
                return False
            elif row.kind is MarketEventTransportKind.SUBSCRIPTION_ACKNOWLEDGED:
                if row.subscription_scope_id != subscription_scope_id:
                    return False
                acknowledged = True
            elif row.kind in {
                MarketEventTransportKind.HEARTBEAT,
                MarketEventTransportKind.CALLBACK_WATERMARK,
                MarketEventTransportKind.QUEUE_WATERMARK,
            }:
                if not acknowledged:
                    return False
            elif row.kind is MarketEventTransportKind.DISCONNECTED:
                disconnected = True
            elif row.kind is MarketEventTransportKind.SESSION_CLOSED:
                closed = True
            else:
                return False
        return True

    def _epoch_state(
        self, manifest: MarketEventSourceSessionManifest, start: datetime, end: datetime
    ) -> TransportCoverageState:
        policy = TransportLivenessPolicy(self.liveness_policy_id)
        rows = self._rows(manifest)
        if manifest.coverage_origin is not CoverageOrigin.LIVE:
            return self._replay_state(manifest, rows, start, end)
        if not self._live_lifecycle_is_ordered(
            rows, manifest.subscription.subscription_scope_id
        ):
            return TransportCoverageState.INCOMPLETE_LIFECYCLE
        connected = [r for r in rows if r.kind is MarketEventTransportKind.CONNECTED]
        if (
            len(connected) != 1
            or connected[0].observed_at > start
            or any(
                r.kind
                in {
                    MarketEventTransportKind.DISCONNECTED,
                    MarketEventTransportKind.SESSION_CLOSED,
                }
                and r.observed_at < end
                for r in rows
            )
        ):
            return TransportCoverageState.INCOMPLETE_DISCONNECTED
        if connected[0].observed_at < manifest.collector_started_at:
            return TransportCoverageState.INCOMPLETE_CLOCK_POLICY
        if not any(
            r.kind is MarketEventTransportKind.SUBSCRIPTION_ACKNOWLEDGED
            and connected[0].observed_at <= r.observed_at <= start
            and r.subscription_scope_id == manifest.subscription.subscription_scope_id
            for r in rows
        ):
            return TransportCoverageState.INCOMPLETE_SUBSCRIPTION_ACK
        if any(
            r.subscription_scope_id is not None
            and r.subscription_scope_id != manifest.subscription.subscription_scope_id
            for r in rows
        ):
            return TransportCoverageState.INCOMPLETE_SUBSCRIPTION_ACK
        if (
            manifest.queue_overflow_count
            or manifest.dropped_callback_count
            or any(r.queue_overflow_count or r.dropped_callback_count for r in rows)
        ):
            return TransportCoverageState.INCOMPLETE_QUEUE_LOSS
        clock = SourceClockPolicy(manifest.clock_policy_id)
        if any(
            r.durable_known_at - r.observed_at > clock.maximum_received_to_durable_delay
            for r in rows
        ):
            return TransportCoverageState.INCOMPLETE_CLOCK_POLICY
        if not self._periodic(
            rows,
            MarketEventTransportKind.HEARTBEAT,
            start,
            end,
            policy.maximum_heartbeat_gap,
        ):
            return TransportCoverageState.INCOMPLETE_HEARTBEAT_GAP
        for kind in (
            MarketEventTransportKind.CALLBACK_WATERMARK,
            MarketEventTransportKind.QUEUE_WATERMARK,
        ):
            if not self._periodic(rows, kind, start, end, policy.maximum_watermark_gap):
                return TransportCoverageState.INCOMPLETE_NO_FINAL_WATERMARK
        scoped = tuple(
            r for r in self.event_records if r.session_id == manifest.session_id
        )
        epoch_members = tuple(
            r for r in scoped if r.source_session_manifest_id == manifest.manifest_id
        )
        final_watermark = max(
            (
                r.observed_at
                for r in rows
                if r.kind is MarketEventTransportKind.CALLBACK_WATERMARK
            ),
            default=start,
        )
        if any(r.durable_known_at > final_watermark for r in epoch_members):
            return TransportCoverageState.INCOMPLETE_EVENT_BINDING
        for row in rows:
            if row.kind not in {
                MarketEventTransportKind.CALLBACK_WATERMARK,
                MarketEventTransportKind.SESSION_CLOSED,
            }:
                continue
            callback_records = [
                r
                for r in scoped
                if r.durable_known_at <= row.observed_at
                and (
                    manifest.callback_sequence_scope
                    is MarketEventCallbackSequenceScope.SESSION
                    or (r.connection_epoch, r.reconnect_epoch)
                    == (manifest.connection_epoch, manifest.reconnect_epoch)
                )
            ]
            if row.callback_high_water != max(
                (r.callback_seq for r in callback_records), default=0
            ):
                return TransportCoverageState.INCOMPLETE_EVENT_BINDING
            provider_records = [
                r
                for r in scoped
                if r.durable_known_at <= row.observed_at
                and (
                    manifest.provider_sequence_scope
                    in {
                        MarketEventProviderSequenceScope.SESSION,
                        MarketEventProviderSequenceScope.SYMBOL_SESSION,
                    }
                    or (r.connection_epoch, r.reconnect_epoch)
                    == (manifest.connection_epoch, manifest.reconnect_epoch)
                )
            ]
            if manifest.provider_sequence_scope in {
                MarketEventProviderSequenceScope.SYMBOL_SESSION,
                MarketEventProviderSequenceScope.SYMBOL_CONNECTION_EPOCH,
            }:
                if len(manifest.subscription.symbols) != 1:
                    return TransportCoverageState.INCOMPLETE_EVENT_BINDING
                provider_records = [
                    r for r in provider_records if r.symbol == self.symbol
                ]
            expected_provider = max(
                (
                    r.provider_seq
                    for r in provider_records
                    if r.provider_seq is not None
                ),
                default=None,
            )
            if row.provider_high_water != expected_provider:
                return TransportCoverageState.INCOMPLETE_EVENT_BINDING
        return TransportCoverageState.LIVE_COVERAGE_PROVEN

    def interval_evidence(
        self, start: datetime, end: datetime
    ) -> tuple[
        tuple[
            MarketEventSourceSessionManifest, datetime, datetime, TransportCoverageState
        ],
        ...,
    ]:
        return tuple(
            (
                m,
                max(start, m.coverage_start),
                min(end, m.coverage_through),
                self._epoch_state(
                    m, max(start, m.coverage_start), min(end, m.coverage_through)
                ),
            )
            for m in self.manifests
            if m.subscription.market is self.market
            and self.symbol in m.subscription.symbols
            and set(self.event_types).issubset(m.subscription.event_types)
            and m.coverage_start < end
            and start < m.coverage_through
        )

    def proves_interval(self, start: datetime, end: datetime) -> bool:
        start, end = _require_utc(start, "start"), _require_utc(end, "end")
        if not self.start <= start < end <= self.end:
            return False
        if any(
            f.resolution_state is FindingResolutionState.UNRESOLVED
            and f.intersects(self.symbol, self.market, start, end, self.event_types)
            for f in self.findings
        ):
            return False
        evidence = self.interval_evidence(start, end)
        if any(
            not state.value.endswith("COVERAGE_PROVEN") for _, _, _, state in evidence
        ):
            return False
        through = start
        for _, left, right, _ in sorted(evidence, key=lambda row: (row[1], row[2])):
            if left > through:
                break
            through = max(through, right)
        return through >= end

    def proves_session_close(self, close: datetime) -> bool:
        close = _require_utc(close, "session close")
        if not self.start < close <= self.end:
            return False
        for manifest, _, _, state in self.interval_evidence(
            close - timedelta(microseconds=1), close
        ):
            if not state.value.endswith("COVERAGE_PROVEN"):
                continue
            kinds = (
                {MarketEventTransportKind.SESSION_CLOSED}
                if manifest.coverage_origin is CoverageOrigin.LIVE
                else {
                    MarketEventTransportKind(
                        manifest.coverage_origin.value + "_COMPLETED"
                    )
                }
            )
            if any(
                r.kind in kinds and r.observed_at >= close for r in self._rows(manifest)
            ):
                return True
        return False

    @property
    def coverage_state(self) -> TransportCoverageState:
        states = tuple(
            state for _, _, _, state in self.interval_evidence(self.start, self.end)
        )
        if self.proves_interval(self.start, self.end):
            return (
                states[0]
                if len(set(states)) == 1
                else TransportCoverageState.MIXED_COVERAGE_PROVEN
            )
        if any(
            f.kind
            in {
                MarketEventSequenceFindingKind.SOURCE_CLOCK_SKEW,
                MarketEventSequenceFindingKind.DURABILITY_DELAY,
                MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION,
            }
            and f.resolution_state is FindingResolutionState.UNRESOLVED
            and f.intersects(
                self.symbol, self.market, self.start, self.end, self.event_types
            )
            for f in self.findings
        ):
            return TransportCoverageState.INCOMPLETE_CLOCK_POLICY
        return next(
            (state for state in states if not state.value.endswith("COVERAGE_PROVEN")),
            TransportCoverageState.INCOMPLETE_INTERVAL_GAP,
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        audit = self.event_snapshot.audit
        transport = audit.transport_snapshot
        document = {
            "schema": "stage4g1-transport-coverage-certificate-v2",
            "assurance": self.assurance.value,
            "source_store_id": audit.source_store_id,
            "market_event_snapshot_id": self.event_snapshot.snapshot_id,
            "market_event_audit_id": audit.audit_id,
            "market_event_high_water": audit.high_water_append_order,
            "transport_snapshot_id": transport.snapshot_id if transport else None,
            "transport_audit_id": transport.audit_id if transport else None,
            "transport_high_water": transport.high_water_append_order
            if transport
            else None,
            "known_at": _utc_text(audit.audited_at),
            "symbol": self.symbol,
            "market": self.market.value,
            "event_types": list(self.event_types),
            "start": _utc_text(self.start),
            "end": _utc_text(self.end),
            "liveness_policy_id": self.liveness_policy_id,
            "coverage_proven": self.proves_interval(self.start, self.end),
            "coverage_state": self.coverage_state.value,
            "epoch_evidence": [
                {
                    "manifest_id": m.manifest_id,
                    "subscription_scope_id": m.subscription.subscription_scope_id,
                    "liveness": _transport_epoch_summary(self._rows(m)),
                    "start": _utc_text(start),
                    "end": _utc_text(end),
                    "state": state.value,
                    "transport_record_ids": [r.record_id for r in self._rows(m)],
                    "callback_event_high_water": max(
                        (
                            r.callback_seq
                            for r in self.event_records
                            if r.session_id == m.session_id
                        ),
                        default=0,
                    ),
                    "clock_policy_id": m.clock_policy_id,
                }
                for m, start, end, state in self.interval_evidence(self.start, self.end)
            ],
        }
        if include_id:
            document["certificate_id"] = self.certificate_id
        return document


@dataclass(frozen=True, slots=True, init=False)
class MarketEventSelection(StructuralFixture):
    source_store_id: str
    snapshot_id: str
    snapshot_audit_id: str
    snapshot_audited_at: datetime
    snapshot_high_water_append_order: int
    snapshot_finding_set_digest: str
    sequence_policy_id: str
    symbol: str
    market: Market
    start_source_time: datetime
    end_source_time: datetime
    interval_boundary_policy_id: str
    allowed_event_types: tuple[str, ...]
    record_limit: int
    records: tuple[MarketEventSourceRecord, ...]
    relevant_findings: tuple[MarketEventSequenceFinding, ...]
    source_session_manifests: tuple[MarketEventSourceSessionManifest, ...]
    coverage_verified_manifest_ids: tuple[str, ...]
    relevant_finding_set_digest: str
    commitment: MarketEventSelectionCommitment
    membership_witness: MarketEventSelectionMembershipWitness
    selection_id: str
    transport_certificate: TransportCoverageCertificate | None

    def __init__(self) -> None:
        raise TypeError(
            "MarketEventSelection must be produced by an exact source snapshot"
        )

    @classmethod
    def _from_verified_snapshot(
        cls,
        *,
        snapshot: MarketEventStoreSnapshot,
        symbol: str,
        market: Market,
        start: datetime,
        end: datetime,
        records: tuple[MarketEventSourceRecord, ...],
        findings: tuple[MarketEventSequenceFinding, ...],
        source_session_manifests: tuple[MarketEventSourceSessionManifest, ...],
        coverage_verified_manifest_ids: tuple[str, ...],
        allowed_event_types: tuple[str, ...],
        interval_boundary_policy_id: str,
        record_limit: int,
        transport_certificate: TransportCoverageCertificate | None = None,
    ) -> MarketEventSelection:
        self = object.__new__(cls)
        values: dict[str, object] = {
            "source_store_id": snapshot.audit.source_store_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_audit_id": snapshot.audit.audit_id,
            "snapshot_audited_at": snapshot.audit.audited_at,
            "snapshot_high_water_append_order": snapshot.audit.high_water_append_order,
            "snapshot_finding_set_digest": snapshot.audit.finding_set_digest,
            "sequence_policy_id": snapshot.audit.sequence_policy_id,
            "symbol": symbol,
            "market": market,
            "start_source_time": start,
            "end_source_time": end,
            "interval_boundary_policy_id": interval_boundary_policy_id,
            "allowed_event_types": allowed_event_types,
            "record_limit": record_limit,
            "transport_certificate": transport_certificate,
            "records": records,
            "relevant_findings": findings,
            "source_session_manifests": source_session_manifests,
            "coverage_verified_manifest_ids": coverage_verified_manifest_ids,
            "relevant_finding_set_digest": _selected_finding_digest(findings),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "commitment",
            MarketEventSelectionCommitment(
                source_store_id=snapshot.audit.source_store_id,
                snapshot_id=snapshot.snapshot_id,
                snapshot_audit_id=snapshot.audit.audit_id,
                snapshot_high_water_append_order=snapshot.audit.high_water_append_order,
                symbol=symbol,
                market=market,
                start_source_time=start,
                end_source_time=end,
                interval_boundary_policy_id=interval_boundary_policy_id,
                allowed_event_types=allowed_event_types,
                record_limit=record_limit,
                ordered_inventory_leaf_ids=tuple(
                    item.inventory_leaf_id for item in records
                ),
                relevant_finding_set_digest=self.relevant_finding_set_digest,
            ),
        )
        object.__setattr__(
            self,
            "membership_witness",
            MarketEventSelectionMembershipWitness(
                inventory_verification_id=snapshot.audit.inventory_verification_id,
                inventory_root=snapshot.audit.inventory_root,
                ordered_source_record_ids=tuple(
                    item.source_record_id for item in records
                ),
                ordered_record_content_hashes=tuple(
                    item.record_content_hash for item in records
                ),
                relevant_finding_ids=tuple(item.finding_id for item in findings),
                source_session_manifest_ids=tuple(
                    item.manifest_id for item in source_session_manifests
                ),
                verification_mode=MarketEventSelectionVerificationMode.FULL_PREFIX_RESCAN,
            ),
        )
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        self._validate_contents()
        return self

    def _validate_contents(self) -> None:
        certificate = self.transport_certificate
        if certificate is not None and (
            type(certificate) is not TransportCoverageCertificate
            or replace(certificate) != certificate
            or certificate.event_snapshot.snapshot_id != self.snapshot_id
            or certificate.event_snapshot.audit.audit_id != self.snapshot_audit_id
            or certificate.event_snapshot.audit.high_water_append_order
            != self.snapshot_high_water_append_order
            or certificate.event_snapshot.audit.source_store_id != self.source_store_id
            or (
                certificate.symbol,
                certificate.market,
                certificate.start,
                certificate.end,
                certificate.event_types,
            )
            != (
                self.symbol,
                self.market,
                self.start_source_time,
                self.end_source_time,
                self.allowed_event_types,
            )
        ):
            raise MarketEventSourceContractError(
                "selection certificate identity mismatch"
            )
        records = self.records
        if type(records) is not tuple or any(
            type(item) is not MarketEventSourceRecord or replace(item) != item
            for item in records
        ):
            raise MarketEventSourceContractError(
                "selection records must be an exact source-record tuple"
            )
        if tuple(item.append_order for item in records) != tuple(
            sorted(item.append_order for item in records)
        ):
            raise MarketEventSourceContractError(
                "selection records must be ordered by append order"
            )
        inventory_columns = (
            tuple(item.event_id for item in records),
            tuple(item.source_record_id for item in records),
            tuple(item.record_storage_key for item in records),
            tuple(item.append_order for item in records),
        )
        if any(len(values) != len(set(values)) for values in inventory_columns):
            raise MarketEventSourceContractError(
                "selection record identities must be unique"
            )
        if any(
            item.source_store_id != self.source_store_id
            or item.symbol != self.symbol
            or item.market is not self.market
            or item.event_type not in self.allowed_event_types
            or not self.start_source_time <= item.source_time < self.end_source_time
            or item.append_order > self.snapshot_high_water_append_order
            for item in records
        ):
            raise MarketEventSourceContractError(
                "selection contains a record outside its exact query or snapshot"
            )
        if type(self.relevant_findings) is not tuple or any(
            type(item) is not MarketEventSequenceFinding
            for item in self.relevant_findings
        ):
            raise MarketEventSourceContractError(
                "selection findings must be an exact finding tuple"
            )
        if len({item.finding_id for item in self.relevant_findings}) != len(
            self.relevant_findings
        ) or any(
            item.source_store_id != self.source_store_id
            or item.observed_append_order > self.snapshot_high_water_append_order
            or not item.intersects(
                self.symbol,
                self.market,
                self.start_source_time,
                self.end_source_time,
                self.allowed_event_types,
            )
            for item in self.relevant_findings
        ):
            raise MarketEventSourceContractError(
                "selection findings do not intersect its impact scope"
            )
        expected_finding_digest = _selected_finding_digest(self.relevant_findings)
        if expected_finding_digest != self.relevant_finding_set_digest:
            raise MarketEventSourceContractError(
                "selection relevant finding digest mismatch"
            )
        manifest_ids = {item.manifest_id for item in self.source_session_manifests}
        if (
            type(self.coverage_verified_manifest_ids) is not tuple
            or len(set(self.coverage_verified_manifest_ids))
            != len(self.coverage_verified_manifest_ids)
            or not set(self.coverage_verified_manifest_ids).issubset(manifest_ids)
        ):
            raise MarketEventSourceContractError("coverage manifest proof is invalid")
        if (
            len(manifest_ids) != len(self.source_session_manifests)
            or any(
                item.source_store_id != self.source_store_id or replace(item) != item
                for item in self.source_session_manifests
            )
            or any(
                item.source_session_manifest_id not in manifest_ids for item in records
            )
        ):
            raise MarketEventSourceContractError(
                "selection source-session manifest inventory is invalid"
            )
        expected_commitment = MarketEventSelectionCommitment(
            source_store_id=self.source_store_id,
            snapshot_id=self.snapshot_id,
            snapshot_audit_id=self.snapshot_audit_id,
            snapshot_high_water_append_order=self.snapshot_high_water_append_order,
            symbol=self.symbol,
            market=self.market,
            start_source_time=self.start_source_time,
            end_source_time=self.end_source_time,
            interval_boundary_policy_id=self.interval_boundary_policy_id,
            allowed_event_types=self.allowed_event_types,
            record_limit=self.record_limit,
            ordered_inventory_leaf_ids=tuple(
                item.inventory_leaf_id for item in records
            ),
            relevant_finding_set_digest=self.relevant_finding_set_digest,
        )
        expected_witness = MarketEventSelectionMembershipWitness(
            inventory_verification_id=self.membership_witness.inventory_verification_id,
            inventory_root=self.membership_witness.inventory_root,
            ordered_source_record_ids=tuple(item.source_record_id for item in records),
            ordered_record_content_hashes=tuple(
                item.record_content_hash for item in records
            ),
            relevant_finding_ids=tuple(
                item.finding_id for item in self.relevant_findings
            ),
            source_session_manifest_ids=tuple(
                item.manifest_id for item in self.source_session_manifests
            ),
            verification_mode=MarketEventSelectionVerificationMode.FULL_PREFIX_RESCAN,
        )
        if (
            self.commitment != expected_commitment
            or self.membership_witness != expected_witness
        ):
            raise MarketEventSourceContractError(
                "selection commitment or membership witness mismatch"
            )
        if self.selection_id != _hash_document(self.as_dict(include_id=False)):
            raise MarketEventSourceContractError("selection_id mismatch")

    def covers_interval(self, start: datetime, end: datetime) -> bool:
        start = _require_utc(start, "coverage interval start")
        end = _require_utc(end, "coverage interval end")
        if not self.start_source_time <= start < end <= self.end_source_time:
            return False
        if (
            self.transport_certificate is None
            or not self.transport_certificate.proves_interval(start, end)
        ):
            return False
        if any(
            item.subscription.market is self.market
            and self.symbol in item.subscription.symbols
            and bool(
                set(self.allowed_event_types).intersection(
                    item.subscription.event_types
                )
            )
            and item.coverage_start < end
            and start < item.coverage_through
            and (item.queue_overflow_count or item.dropped_callback_count)
            for item in self.source_session_manifests
        ):
            return False
        if any(
            item.resolution_state is FindingResolutionState.UNRESOLVED
            and item.intersects(
                self.symbol, self.market, start, end, self.allowed_event_types
            )
            for item in self.relevant_findings
        ):
            return False
        intervals = sorted(
            (item.coverage_start, item.coverage_through)
            for item in self.source_session_manifests
            if item.manifest_id in self.coverage_verified_manifest_ids
            and item.subscription.market is self.market
            and self.symbol in item.subscription.symbols
            and set(self.allowed_event_types).issubset(item.subscription.event_types)
            and item.queue_overflow_count == 0
            and item.dropped_callback_count == 0
        )
        through = start
        for left, right in intervals:
            if left > through:
                break
            through = max(through, right)
            if through >= end:
                return True
        return False

    @property
    def completeness(self) -> SelectionCompleteness:
        if self.transport_certificate is not None and any(
            state
            in {
                TransportCoverageState.INCOMPLETE_EVENT_BINDING,
                TransportCoverageState.INCOMPLETE_CLOCK_POLICY,
            }
            for _, _, _, state in self.transport_certificate.interval_evidence(
                self.start_source_time, self.end_source_time
            )
        ):
            return SelectionCompleteness.BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY
        if any(
            item.resolution_state is FindingResolutionState.UNRESOLVED
            for item in self.relevant_findings
        ):
            return SelectionCompleteness.BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY
        if self.covers_interval(self.start_source_time, self.end_source_time):
            return (
                SelectionCompleteness.COVERED_RECORDS
                if self.records
                else SelectionCompleteness.ZERO_EVENT_PROVEN
            )
        return (
            SelectionCompleteness.INCOMPLETE_COVERAGE
            if self.records
            else SelectionCompleteness.EMPTY_NOT_PROVEN
        )

    @property
    def subscription_membership_witnesses(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                manifest.manifest_id,
                manifest.subscription.membership_witness(self.symbol, self.market),
            )
            for manifest in self.source_session_manifests
            if manifest.subscription.market is self.market
            and self.symbol in manifest.subscription.symbols
            and set(self.allowed_event_types).issubset(
                manifest.subscription.event_types
            )
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "assurance": self.assurance.value,
            "schema": MARKET_EVENT_SELECTION_SCHEMA,
            "transport_certificate_id": self.transport_certificate.certificate_id
            if self.transport_certificate
            else None,
            "subscription_membership_witnesses": [
                {"manifest_id": manifest_id, "symbol_membership_witness_id": witness_id}
                for manifest_id, witness_id in self.subscription_membership_witnesses
            ],
            "snapshot_audited_at": _utc_text(self.snapshot_audited_at),
            "completeness": self.completeness.value,
            "coverage_verified_manifest_ids": list(self.coverage_verified_manifest_ids),
            "source_store_id": self.source_store_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_audit_id": self.snapshot_audit_id,
            "snapshot_high_water_append_order": self.snapshot_high_water_append_order,
            "snapshot_finding_set_digest": self.snapshot_finding_set_digest,
            "sequence_policy_id": self.sequence_policy_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "start_source_time": _utc_text(self.start_source_time),
            "end_source_time": _utc_text(self.end_source_time),
            "interval_boundary_policy_id": self.interval_boundary_policy_id,
            "allowed_event_types": list(self.allowed_event_types),
            "record_limit": self.record_limit,
            "record_ids": [item.source_record_id for item in self.records],
            "record_content_hashes": [
                item.record_content_hash for item in self.records
            ],
            "record_storage_keys": [item.record_storage_key for item in self.records],
            "record_file_sha256s": [item.record_file_sha256 for item in self.records],
            "relevant_finding_ids": [
                item.finding_id for item in self.relevant_findings
            ],
            "relevant_finding_set_digest": self.relevant_finding_set_digest,
            "source_session_manifest_ids": [
                item.manifest_id for item in self.source_session_manifests
            ],
            "commitment": self.commitment.as_dict(),
            "membership_witness": self.membership_witness.as_dict(),
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


def validate_selection_verification(
    selection: MarketEventSelection,
    verification: MarketEventSelectionVerification,
) -> None:
    if (
        type(selection) is not MarketEventSelection
        or type(verification) is not MarketEventSelectionVerification
    ):
        raise MarketEventSourceContractError(
            "exact typed selection and verification are required"
        )
    selection._validate_contents()
    if verification != MarketEventSelectionVerification._from_exact_rescan(selection):
        raise MarketEventSourceContractError("selection verification identity mismatch")


@dataclass(frozen=True, slots=True)
class DecodedTradeTick(StructuralFixture):
    record: MarketEventSourceRecord
    selection: MarketEventSelection
    verification: MarketEventSelectionVerification
    decoder_policy_id: str
    source_schema_id: str = field(init=False)
    source_store_id: str = field(init=False)
    source_snapshot_id: str = field(init=False)
    source_audit_id: str = field(init=False)
    selection_id: str = field(init=False)
    selection_verification_id: str = field(init=False)
    source_record_id: str = field(init=False)
    event_id: str = field(init=False)
    symbol: str = field(init=False)
    market: Market = field(init=False)
    trading_day: date = field(init=False)
    source_time: datetime = field(init=False)
    received_at: datetime = field(init=False)
    durable_known_at: datetime = field(init=False)
    price: Decimal = field(init=False)
    quantity: int = field(init=False)
    decoded_payload_sha256: str = field(init=False)
    decoded_tick_id: str = field(init=False)

    def __post_init__(self) -> None:
        validate_selection_verification(self.selection, self.verification)
        record = self.record
        if type(record) is not MarketEventSourceRecord:
            raise MarketEventSourceContractError(
                "decoder requires an exact source record"
            )
        if replace(record) != record:
            raise MarketEventSourceContractError("decoder source record was mutated")
        if record not in self.selection.records:
            raise MarketEventSourceContractError(
                "decoded record is outside verified selection"
            )
        if (
            record.source_schema_id != FIXTURE_TRADE_TICK_SCHEMA_V1
            or self.decoder_policy_id != FIXTURE_TRADE_DECODER_POLICY_V1
            or record.event_type != "TRADE_TICK"
        ):
            raise MarketEventSourceContractError(
                "unsupported exact trade decoder schema/policy"
            )
        payload = record.payload()
        if set(payload) != {"last_price", "quantity"}:
            raise MarketEventSourceContractError(
                "fixture trade payload fields must be exact"
            )
        value = payload["last_price"]
        if type(value) is int:
            value = str(value)
        if (
            type(value) is not str
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None
        ):
            raise MarketEventSourceContractError(
                "trade price must be finite decimal text or integer, never float/bool"
            )
        price = Decimal(value)
        if not price.is_finite() or price <= 0:
            raise MarketEventSourceContractError(
                "trade price must be positive and finite"
            )
        quantity = _require_int(payload["quantity"], "trade quantity", minimum=1)
        for name in (
            "source_schema_id",
            "source_store_id",
            "source_record_id",
            "event_id",
            "symbol",
            "market",
            "trading_day",
            "source_time",
            "received_at",
            "durable_known_at",
        ):
            object.__setattr__(self, name, getattr(record, name))
        object.__setattr__(self, "source_snapshot_id", self.selection.snapshot_id)
        object.__setattr__(self, "source_audit_id", self.selection.snapshot_audit_id)
        object.__setattr__(self, "selection_id", self.selection.selection_id)
        object.__setattr__(
            self, "selection_verification_id", self.verification.verification_id
        )
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self,
            "decoded_payload_sha256",
            _hash_document(
                {
                    "schema": "stage4g1-decoded-trade-payload-v1",
                    "price": format(price, "f"),
                    "quantity": quantity,
                }
            ),
        )
        object.__setattr__(
            self, "decoded_tick_id", _hash_document(self.as_dict(include_id=False))
        )

    def validate(self) -> None:
        expected = type(self)(
            self.record, self.selection, self.verification, self.decoder_policy_id
        )
        if self != expected:
            raise MarketEventSourceContractError("decoded trade tick was mutated")

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        record = self.record
        document = {
            "assurance": self.assurance.value,
            "schema": "stage4g1-decoded-trade-tick-v1",
            "decoder_policy_id": self.decoder_policy_id,
            "source_schema_id": record.source_schema_id,
            "source_store_id": record.source_store_id,
            "source_snapshot_id": self.selection.snapshot_id,
            "source_audit_id": self.selection.snapshot_audit_id,
            "selection_id": self.selection.selection_id,
            "selection_verification_id": self.verification.verification_id,
            "source_record_id": record.source_record_id,
            "event_id": record.event_id,
            "source_record_content_hash": record.record_content_hash,
            "symbol": record.symbol,
            "market": record.market.value,
            "trading_day": record.trading_day.isoformat(),
            "append_order": record.append_order,
            "source_time": _utc_text(record.source_time),
            "received_at": _utc_text(record.received_at),
            "durable_known_at": _utc_text(record.durable_known_at),
            "price": format(self.price, "f"),
            "quantity": self.quantity,
            "decoded_payload_sha256": self.decoded_payload_sha256,
        }
        if include_id:
            document["decoded_tick_id"] = self.decoded_tick_id
        return document


class MarketEventReadPort(Protocol):
    """Frozen read-side boundary for an audited append-only source store."""

    schema: str

    def freeze_full_prefix(
        self,
        *,
        high_water_append_order: int | None = None,
    ) -> MarketEventStoreSnapshot: ...

    def select(
        self,
        *,
        snapshot: MarketEventStoreSnapshot,
        symbol: str,
        market: Market,
        start_source_time: datetime,
        end_source_time: datetime,
        allowed_event_types: tuple[str, ...],
        interval_boundary_policy_id: str,
        record_limit: int,
    ) -> MarketEventSelection: ...

    def verify_selection(
        self,
        selection: MarketEventSelection,
    ) -> MarketEventSelectionVerification: ...


__all__ = [
    "FIXTURE_TRADE_DECODER_POLICY_V1",
    "FIXTURE_TRADE_TICK_SCHEMA_V1",
    "MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1",
    "MARKET_EVENT_READ_PORT_SCHEMA",
    "MARKET_EVENT_SELECTION_SCHEMA",
    "MARKET_EVENT_SEQUENCE_POLICY_V3",
    "MARKET_EVENT_SOURCE_RECORD_SCHEMA",
    "MARKET_EVENT_STORE_AUDIT_SCHEMA",
    "MARKET_EVENT_STORE_SNAPSHOT_SCHEMA",
    "CoverageOrigin",
    "DecodedTradeTick",
    "EvidenceAssurance",
    "FindingImpactScope",
    "FindingResolutionState",
    "MarketEventCallbackSequenceScope",
    "MarketEventInventoryVerification",
    "MarketEventPartitionHead",
    "MarketEventProviderSequenceScope",
    "MarketEventReadPort",
    "MarketEventSelection",
    "MarketEventSelectionCommitment",
    "MarketEventSelectionMembershipWitness",
    "MarketEventSelectionVerification",
    "MarketEventSelectionVerificationMode",
    "MarketEventSequenceFinding",
    "MarketEventSequenceFindingKind",
    "MarketEventSourceContractError",
    "MarketEventSourceRecord",
    "MarketEventSourceSessionManifest",
    "MarketEventStoreAudit",
    "MarketEventStoreAuditScope",
    "MarketEventStoreSnapshot",
    "MarketEventSubscriptionManifest",
    "MarketEventTransportKind",
    "MarketEventTransportRecord",
    "MarketEventTransportSnapshot",
    "SelectionCompleteness",
    "SourceClockPolicy",
    "StructuralFixture",
    "TransportCoverageCertificate",
    "TransportCoverageState",
    "TransportLivenessPolicy",
    "validate_selection_verification",
]
