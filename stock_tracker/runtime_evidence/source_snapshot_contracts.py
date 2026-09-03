from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from stock_tracker.core.types import Market

MARKET_EVENT_SOURCE_RECORD_SCHEMA = "stage4g1-market-event-source-record-v1"
MARKET_EVENT_STORE_AUDIT_SCHEMA = "stage4g1-market-event-store-audit-v1"
MARKET_EVENT_STORE_SNAPSHOT_SCHEMA = "stage4g1-market-event-store-snapshot-v1"
MARKET_EVENT_SELECTION_SCHEMA = "stage4g1-market-event-selection-v1"
MARKET_EVENT_READ_PORT_SCHEMA = "stage4g1-market-event-read-port-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64
_MAX_TEXT = 4096
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_SNAPSHOT_RECORDS = 1_000_000


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
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int = _MAX_TEXT,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > maximum:
        raise MarketEventSourceContractError(
            f"{name} must be a safe trimmed string"
        )
    if not value and not allow_empty:
        raise MarketEventSourceContractError(f"{name} must not be empty")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise MarketEventSourceContractError(f"{name} contains control characters")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=64)
    if _SHA256.fullmatch(text) is None:
        raise MarketEventSourceContractError(
            f"{name} must be lowercase SHA-256"
        )
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
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_date(value: object, name: str) -> date:
    if type(value) is not date:
        raise MarketEventSourceContractError(f"{name} must be a date")
    return value


def _require_market(value: object) -> Market:
    if type(value) is not Market:
        raise MarketEventSourceContractError(
            "market must be the exact Market type"
        )
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
        raise MarketEventSourceContractError(
            "source snapshot exceeds its size bound"
        )
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
class MarketEventSourceRecord:
    source_store_id: str
    append_order: int
    event_id: str
    session_id: str
    source: str
    feed_mode: str
    symbol: str
    market: Market
    event_type: str
    trading_day: date
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
    record_file: str
    record_file_sha256: str
    record_hash: str
    source_record_id: str = field(init=False)

    def __post_init__(self) -> None:
        source_store_id = _require_sha256(self.source_store_id, "source_store_id")
        append_order = _require_int(self.append_order, "append_order", minimum=1)
        event_id = _require_sha256(self.event_id, "event_id")
        _require_text(self.session_id, "session_id", maximum=256)
        _require_text(self.source, "source", maximum=128)
        _require_text(self.feed_mode, "feed_mode", maximum=128)
        market = _require_market(self.market)
        _require_symbol(self.symbol, market)
        _require_text(self.event_type, "event_type", maximum=128)
        _require_date(self.trading_day, "trading_day")
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
        record_file = _require_text(self.record_file, "record_file", maximum=1024)
        if (
            "\\" in record_file
            or record_file.startswith("/")
            or ".." in record_file.split("/")
        ):
            raise MarketEventSourceContractError("record_file is not a safe relative path")
        _require_sha256(self.record_file_sha256, "record_file_sha256")
        record_identity = self._record_identity(
            source_store_id=source_store_id,
            append_order=append_order,
            event_id=event_id,
            previous_global_record_hash=previous_global,
            previous_partition_record_hash=previous_partition,
        )
        expected_record_hash = _hash_document(record_identity)
        if self.record_hash != expected_record_hash:
            raise MarketEventSourceContractError("record_hash mismatch")
        object.__setattr__(
            self,
            "source_record_id",
            _hash_document(
                {
                    "schema": "stage4g1-market-event-source-record-id-v1",
                    "source_store_id": source_store_id,
                    "append_order": append_order,
                    "record_hash": expected_record_hash,
                    "record_file_sha256": self.record_file_sha256,
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
            "source": self.source,
            "feed_mode": self.feed_mode,
            "symbol": self.symbol,
            "market": self.market.value,
            "event_type": self.event_type,
            "trading_day": self.trading_day.isoformat(),
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
        source: str,
        feed_mode: str,
        symbol: str,
        market: Market,
        event_type: str,
        trading_day: date,
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
        record_file: str,
        record_file_sha256: str,
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
            "source": source,
            "feed_mode": feed_mode,
            "symbol": symbol,
            "market": market,
            "event_type": event_type,
            "trading_day": trading_day,
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
            "record_file": record_file,
            "record_file_sha256": record_file_sha256,
        }.items():
            object.__setattr__(provisional, name, value)
        identity = provisional._record_identity()
        return cls(
            source_store_id=source_store_id,
            append_order=append_order,
            event_id=event_id,
            session_id=session_id,
            source=source,
            feed_mode=feed_mode,
            symbol=symbol,
            market=market,
            event_type=event_type,
            trading_day=trading_day,
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
            record_file=record_file,
            record_file_sha256=record_file_sha256,
            record_hash=_hash_document(identity),
        )

    def payload(self) -> dict[str, Any]:
        return dict(_strict_json_object(self.payload_json.encode("utf-8")))

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._record_identity(),
            "payload_json": self.payload_json,
            "record_file": self.record_file,
            "record_file_sha256": self.record_file_sha256,
            "record_hash": self.record_hash,
            "source_record_id": self.source_record_id,
        }


@dataclass(frozen=True, slots=True)
class MarketEventSequenceFinding:
    kind: MarketEventSequenceFindingKind
    source_store_id: str
    event_id: str
    session_id: str
    symbol: str
    observed_append_order: int
    expected_sequence: int | None
    observed_sequence: int | None
    detail_code: str
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
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
        object.__setattr__(
            self,
            "finding_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-market-event-sequence-finding-v1",
            "kind": self.kind.value,
            "source_store_id": self.source_store_id,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "observed_append_order": self.observed_append_order,
            "expected_sequence": self.expected_sequence,
            "observed_sequence": self.observed_sequence,
            "detail_code": self.detail_code,
        }
        if include_id:
            document["finding_id"] = self.finding_id
        return document


@dataclass(frozen=True, slots=True)
class MarketEventPartitionHead:
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
            "schema": "stage4g1-market-event-partition-head-v1",
            "partition_key": self.partition_key,
            "event_count": self.event_count,
            "first_record_hash": self.first_record_hash,
            "last_record_hash": self.last_record_hash,
            "manifest_sha256": self.manifest_sha256,
        }
        if include_id:
            document["partition_head_id"] = self.partition_head_id
        return document


@dataclass(frozen=True, slots=True)
class MarketEventStoreAudit:
    source_store_id: str
    source_schema_id: str
    scope: MarketEventStoreAuditScope
    high_water_append_order: int
    audited_at: datetime
    catalog_schema_fingerprint: str
    record_hashes: tuple[str, ...]
    partition_heads: tuple[MarketEventPartitionHead, ...]
    first_global_record_hash: str
    last_global_record_hash: str
    audit_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.source_store_id, "source_store_id")
        _require_text(self.source_schema_id, "source_schema_id", maximum=256)
        if type(self.scope) is not MarketEventStoreAuditScope:
            raise MarketEventSourceContractError(
                "scope must be MarketEventStoreAuditScope"
            )
        high_water = _require_int(
            self.high_water_append_order,
            "high_water_append_order",
        )
        audited_at = _require_utc(self.audited_at, "audited_at")
        object.__setattr__(self, "audited_at", audited_at)
        _require_sha256(
            self.catalog_schema_fingerprint,
            "catalog_schema_fingerprint",
        )
        if type(self.record_hashes) is not tuple or any(
            type(item) is not str or _SHA256.fullmatch(item) is None
            for item in self.record_hashes
        ):
            raise MarketEventSourceContractError(
                "record_hashes must be a tuple of lowercase SHA-256"
            )
        if len(self.record_hashes) != high_water:
            raise MarketEventSourceContractError(
                "full-prefix audit record count must equal high-water append order"
            )
        if len(set(self.record_hashes)) != len(self.record_hashes):
            raise MarketEventSourceContractError(
                "audit contains duplicate global record hashes"
            )
        if type(self.partition_heads) is not tuple or any(
            type(item) is not MarketEventPartitionHead
            for item in self.partition_heads
        ):
            raise MarketEventSourceContractError(
                "partition_heads must be an exact tuple"
            )
        ordered_heads = tuple(
            sorted(self.partition_heads, key=lambda item: item.partition_key)
        )
        if ordered_heads != self.partition_heads or len(
            {item.partition_key for item in self.partition_heads}
        ) != len(self.partition_heads):
            raise MarketEventSourceContractError(
                "partition_heads must be unique and canonically ordered"
            )
        expected_first = _ZERO_HASH if not self.record_hashes else self.record_hashes[0]
        expected_last = _ZERO_HASH if not self.record_hashes else self.record_hashes[-1]
        if (
            self.first_global_record_hash != expected_first
            or self.last_global_record_hash != expected_last
        ):
            raise MarketEventSourceContractError(
                "global audit hash boundaries are invalid"
            )
        object.__setattr__(
            self,
            "audit_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": MARKET_EVENT_STORE_AUDIT_SCHEMA,
            "source_store_id": self.source_store_id,
            "source_schema_id": self.source_schema_id,
            "scope": self.scope.value,
            "high_water_append_order": self.high_water_append_order,
            "audited_at": _utc_text(self.audited_at),
            "catalog_schema_fingerprint": self.catalog_schema_fingerprint,
            "record_hashes": list(self.record_hashes),
            "partition_heads": [item.as_dict() for item in self.partition_heads],
            "first_global_record_hash": self.first_global_record_hash,
            "last_global_record_hash": self.last_global_record_hash,
        }
        if include_id:
            document["audit_id"] = self.audit_id
        return document


@dataclass(frozen=True, slots=True)
class MarketEventStoreSnapshot:
    audit: MarketEventStoreAudit
    records: tuple[MarketEventSourceRecord, ...]
    findings: tuple[MarketEventSequenceFinding, ...]
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.audit) is not MarketEventStoreAudit:
            raise MarketEventSourceContractError(
                "audit must be MarketEventStoreAudit"
            )
        if type(self.records) is not tuple or any(
            type(item) is not MarketEventSourceRecord for item in self.records
        ):
            raise MarketEventSourceContractError(
                "records must be an exact tuple of MarketEventSourceRecord"
            )
        if len(self.records) > _MAX_SNAPSHOT_RECORDS:
            raise MarketEventSourceContractError(
                "source snapshot exceeds its record-count bound"
            )
        if len(self.records) != self.audit.high_water_append_order:
            raise MarketEventSourceContractError(
                "snapshot records do not cover the audited full prefix"
            )
        if type(self.findings) is not tuple or any(
            type(item) is not MarketEventSequenceFinding for item in self.findings
        ):
            raise MarketEventSourceContractError(
                "findings must be an exact tuple of MarketEventSequenceFinding"
            )
        expected_global_previous = _ZERO_HASH
        previous_durable_known_at: datetime | None = None
        partition_previous: dict[str, str] = {}
        for expected_order, record in enumerate(self.records, start=1):
            if (
                record.source_store_id != self.audit.source_store_id
                or record.append_order != expected_order
                or record.previous_global_record_hash != expected_global_previous
                or record.durable_known_at > self.audit.audited_at
                or (
                    previous_durable_known_at is not None
                    and record.durable_known_at < previous_durable_known_at
                )
            ):
                raise MarketEventSourceContractError(
                    "snapshot global prefix identity is invalid"
                )
            expected_partition_previous = partition_previous.get(
                record.partition_key,
                _ZERO_HASH,
            )
            if record.previous_partition_record_hash != expected_partition_previous:
                raise MarketEventSourceContractError(
                    "snapshot partition hash chain is invalid"
                )
            expected_global_previous = record.record_hash
            previous_durable_known_at = record.durable_known_at
            partition_previous[record.partition_key] = record.record_hash
        record_hashes = tuple(item.record_hash for item in self.records)
        if record_hashes != self.audit.record_hashes:
            raise MarketEventSourceContractError(
                "snapshot records disagree with the audit record hashes"
            )
        if len({item.event_id for item in self.records}) != len(self.records):
            raise MarketEventSourceContractError(
                "snapshot contains duplicate event IDs"
            )
        if len({item.record_file for item in self.records}) != len(self.records):
            raise MarketEventSourceContractError(
                "snapshot contains duplicate immutable record paths"
            )
        expected_partition_counts: dict[str, int] = {}
        expected_partition_first: dict[str, str] = {}
        for record in self.records:
            expected_partition_counts[record.partition_key] = (
                expected_partition_counts.get(record.partition_key, 0) + 1
            )
            expected_partition_first.setdefault(
                record.partition_key,
                record.record_hash,
            )
        heads = {item.partition_key: item for item in self.audit.partition_heads}
        if set(heads) != set(expected_partition_counts):
            raise MarketEventSourceContractError(
                "snapshot partition inventory disagrees with the audit"
            )
        for partition_key, count in expected_partition_counts.items():
            head = heads[partition_key]
            if (
                head.event_count != count
                or head.first_record_hash != expected_partition_first[partition_key]
                or head.last_record_hash != partition_previous[partition_key]
            ):
                raise MarketEventSourceContractError(
                    "snapshot partition head disagrees with its records"
                )
        for finding in self.findings:
            if (
                finding.source_store_id != self.audit.source_store_id
                or finding.observed_append_order > self.audit.high_water_append_order
                or not any(
                    record.event_id == finding.event_id
                    and record.append_order == finding.observed_append_order
                    and record.session_id == finding.session_id
                    and record.symbol == finding.symbol
                    for record in self.records
                )
            ):
                raise MarketEventSourceContractError(
                    "sequence finding is outside the audited source prefix"
                )
        object.__setattr__(
            self,
            "snapshot_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": MARKET_EVENT_STORE_SNAPSHOT_SCHEMA,
            "audit": self.audit.as_dict(),
            "records": [item.as_dict() for item in self.records],
            "findings": [item.as_dict() for item in self.findings],
        }
        if include_id:
            document["snapshot_id"] = self.snapshot_id
        return document

    def select(
        self,
        *,
        symbol: str,
        market: Market,
        start_source_time: datetime,
        end_source_time: datetime,
    ) -> MarketEventSelection:
        market = _require_market(market)
        symbol = _require_symbol(symbol, market)
        start = _require_utc(start_source_time, "start_source_time")
        end = _require_utc(end_source_time, "end_source_time")
        if end < start:
            raise MarketEventSourceContractError(
                "selection end cannot precede start"
            )
        selected = tuple(
            record
            for record in self.records
            if record.symbol == symbol
            and record.market is market
            and start <= record.source_time <= end
        )
        return MarketEventSelection(
            source_store_id=self.audit.source_store_id,
            snapshot_audit_id=self.audit.audit_id,
            snapshot_high_water_append_order=self.audit.high_water_append_order,
            symbol=symbol,
            market=market,
            start_source_time=start,
            end_source_time=end,
            records=selected,
        )


@dataclass(frozen=True, slots=True)
class MarketEventSelection:
    source_store_id: str
    snapshot_audit_id: str
    snapshot_high_water_append_order: int
    symbol: str
    market: Market
    start_source_time: datetime
    end_source_time: datetime
    records: tuple[MarketEventSourceRecord, ...]
    selection_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.source_store_id, "source_store_id")
        _require_sha256(self.snapshot_audit_id, "snapshot_audit_id")
        high_water = _require_int(
            self.snapshot_high_water_append_order,
            "snapshot_high_water_append_order",
        )
        market = _require_market(self.market)
        symbol = _require_symbol(self.symbol, market)
        start = _require_utc(self.start_source_time, "start_source_time")
        end = _require_utc(self.end_source_time, "end_source_time")
        if end < start:
            raise MarketEventSourceContractError(
                "selection end cannot precede start"
            )
        object.__setattr__(self, "start_source_time", start)
        object.__setattr__(self, "end_source_time", end)
        if type(self.records) is not tuple or any(
            type(item) is not MarketEventSourceRecord for item in self.records
        ):
            raise MarketEventSourceContractError(
                "selection records must be an exact tuple"
            )
        ordered = tuple(sorted(self.records, key=lambda item: item.append_order))
        if ordered != self.records or len(
            {item.append_order for item in self.records}
        ) != len(self.records):
            raise MarketEventSourceContractError(
                "selection records must use unique source append order"
            )
        if any(
            record.source_store_id != self.source_store_id
            or record.append_order > high_water
            or record.symbol != symbol
            or record.market is not market
            or not start <= record.source_time <= end
            for record in self.records
        ):
            raise MarketEventSourceContractError(
                "selection contains a record outside its frozen query"
            )
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": MARKET_EVENT_SELECTION_SCHEMA,
            "source_store_id": self.source_store_id,
            "snapshot_audit_id": self.snapshot_audit_id,
            "snapshot_high_water_append_order": self.snapshot_high_water_append_order,
            "symbol": self.symbol,
            "market": self.market.value,
            "start_source_time": _utc_text(self.start_source_time),
            "end_source_time": _utc_text(self.end_source_time),
            "record_ids": [item.source_record_id for item in self.records],
            "record_hashes": [item.record_hash for item in self.records],
        }
        if include_id:
            document["selection_id"] = self.selection_id
        return document


class MarketEventReadPort(Protocol):
    """Frozen read-side boundary for an audited append-only source store."""

    schema: str

    def freeze_full_prefix(
        self,
        *,
        high_water_append_order: int | None = None,
    ) -> MarketEventStoreSnapshot:
        ...


__all__ = [
    "MARKET_EVENT_READ_PORT_SCHEMA",
    "MARKET_EVENT_SELECTION_SCHEMA",
    "MARKET_EVENT_SOURCE_RECORD_SCHEMA",
    "MARKET_EVENT_STORE_AUDIT_SCHEMA",
    "MARKET_EVENT_STORE_SNAPSHOT_SCHEMA",
    "MarketEventPartitionHead",
    "MarketEventReadPort",
    "MarketEventSelection",
    "MarketEventSequenceFinding",
    "MarketEventSequenceFindingKind",
    "MarketEventSourceContractError",
    "MarketEventSourceRecord",
    "MarketEventStoreAudit",
    "MarketEventStoreAuditScope",
    "MarketEventStoreSnapshot",
]
