from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market

MARKET_EVENT_SOURCE_RECORD_SCHEMA = "stage4g1-market-event-source-record-v2"
MARKET_EVENT_STORE_AUDIT_SCHEMA = "stage4g1-market-event-store-audit-v2"
MARKET_EVENT_STORE_SNAPSHOT_SCHEMA = "stage4g1-market-event-store-snapshot-v2"
MARKET_EVENT_SELECTION_SCHEMA = "stage4g1-market-event-selection-v2"
MARKET_EVENT_READ_PORT_SCHEMA = "stage4g1-market-event-read-port-v2"
MARKET_EVENT_SEQUENCE_POLICY_V1 = "stage4g1-market-event-sequence-policy-v1"

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
        if market_session_date(
            source_time,
            market,
            self.session_label_policy_id,
        ) != self.trading_day:
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
        _require_storage_key(self.record_file, "record_file")
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
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V1:
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
        object.__setattr__(
            self,
            "finding_id",
            _hash_document(self.as_dict(include_id=False)),
        )

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": "stage4g1-market-event-sequence-finding-v2",
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
    selected_records: tuple[MarketEventSourceRecord, ...]
    selected_findings: tuple[MarketEventSequenceFinding, ...]


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
    callback_max: dict[str, int],
    symbol_heads: dict[tuple[str, str], MarketEventSourceRecord],
    sequence_policy_id: str,
) -> tuple[MarketEventSequenceFinding, ...]:
    findings: list[MarketEventSequenceFinding] = []
    previous_callback = callback_max.get(record.session_id)
    if previous_callback is not None:
        if record.callback_seq <= previous_callback:
            findings.append(
                MarketEventSequenceFinding(
                    sequence_policy_id=sequence_policy_id,
                    kind=MarketEventSequenceFindingKind.OUT_OF_ORDER,
                    source_store_id=record.source_store_id,
                    event_id=record.event_id,
                    session_id=record.session_id,
                    symbol=record.symbol,
                    observed_append_order=record.append_order,
                    expected_sequence=previous_callback + 1,
                    observed_sequence=record.callback_seq,
                    detail_code="CALLBACK_SEQUENCE_NOT_ADVANCED",
                )
            )
        elif record.callback_seq > previous_callback + 1:
            findings.append(
                MarketEventSequenceFinding(
                    sequence_policy_id=sequence_policy_id,
                    kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
                    source_store_id=record.source_store_id,
                    event_id=record.event_id,
                    session_id=record.session_id,
                    symbol=record.symbol,
                    observed_append_order=record.append_order,
                    expected_sequence=previous_callback + 1,
                    observed_sequence=record.callback_seq,
                    detail_code="CALLBACK_SEQUENCE_GAP",
                )
            )
    previous_symbol = symbol_heads.get((record.session_id, record.symbol))
    if previous_symbol is not None:
        if record.provider_seq is not None and previous_symbol.provider_seq is not None:
            if record.provider_seq <= previous_symbol.provider_seq:
                findings.append(
                    MarketEventSequenceFinding(
                        sequence_policy_id=sequence_policy_id,
                        kind=MarketEventSequenceFindingKind.OUT_OF_ORDER,
                        source_store_id=record.source_store_id,
                        event_id=record.event_id,
                        session_id=record.session_id,
                        symbol=record.symbol,
                        observed_append_order=record.append_order,
                        expected_sequence=previous_symbol.provider_seq + 1,
                        observed_sequence=record.provider_seq,
                        detail_code="PROVIDER_SEQUENCE_NOT_ADVANCED",
                    )
                )
            elif record.provider_seq > previous_symbol.provider_seq + 1:
                findings.append(
                    MarketEventSequenceFinding(
                        sequence_policy_id=sequence_policy_id,
                        kind=MarketEventSequenceFindingKind.PROVIDER_SEQUENCE,
                        source_store_id=record.source_store_id,
                        event_id=record.event_id,
                        session_id=record.session_id,
                        symbol=record.symbol,
                        observed_append_order=record.append_order,
                        expected_sequence=previous_symbol.provider_seq + 1,
                        observed_sequence=record.provider_seq,
                        detail_code="PROVIDER_SEQUENCE_GAP",
                    )
                )
        if record.source_time < previous_symbol.source_time:
            findings.append(
                MarketEventSequenceFinding(
                    sequence_policy_id=sequence_policy_id,
                    kind=MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION,
                    source_store_id=record.source_store_id,
                    event_id=record.event_id,
                    session_id=record.session_id,
                    symbol=record.symbol,
                    observed_append_order=record.append_order,
                    expected_sequence=None,
                    observed_sequence=None,
                    detail_code="SOURCE_TIME_REGRESSION",
                )
            )
    callback_max[record.session_id] = max(
        record.callback_seq,
        callback_max.get(record.session_id, record.callback_seq),
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
    source_store_id: str,
    audited_at: datetime,
    sequence_policy_id: str,
    selection_query: tuple[str, Market, datetime, datetime] | None = None,
    selection_limit: int = _MAX_SELECTION_RECORDS,
) -> _PrefixScan:
    if sequence_policy_id != MARKET_EVENT_SEQUENCE_POLICY_V1:
        raise MarketEventSourceContractError(
            "unsupported market-event sequence policy"
        )
    supplied_findings = iter(findings)
    expected_previous = _ZERO_HASH
    previous_known_at: datetime | None = None
    partition_state: dict[str, tuple[int, str, str]] = {}
    callback_max: dict[str, int] = {}
    symbol_heads: dict[tuple[str, str], MarketEventSourceRecord] = {}
    finding_chain = _ZERO_HASH
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
        if type(record) is not MarketEventSourceRecord:
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
            record.record_hash if previous_partition is None else previous_partition[1],
            record.record_hash,
        )
        record_selected = False
        if selection_query is not None:
            symbol, market, start, end = selection_query
            record_selected = (
                record.symbol == symbol
                and record.market is market
                and start <= record.source_time <= end
            )
            if record_selected:
                selected_records.append(record)
                if len(selected_records) > selection_limit:
                    raise MarketEventSourceContractError(
                        "selection exceeds its strict record limit"
                    )
        for expected_finding in _expected_sequence_findings(
            record,
            callback_max,
            symbol_heads,
            sequence_policy_id,
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
            if record_selected:
                selected_findings.append(expected_finding)
        if record_count == 1:
            first_hash = record.record_hash
        expected_previous = record.record_hash
        previous_known_at = record.durable_known_at
        chunk_chain = _rolling_digest(
            chunk_chain,
            "stage4g1-market-event-record-chunk-node-v1",
            {
                "append_order": record.append_order,
                "source_record_id": record.source_record_id,
                "record_hash": record.record_hash,
            },
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
        if previous_partition_key is not None and head.partition_key <= previous_partition_key:
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
        selected_records=tuple(selected_records),
        selected_findings=tuple(selected_findings),
    )


@dataclass(frozen=True, slots=True, init=False)
class MarketEventStoreAudit:
    source_store_id: str
    source_schema_id: str
    scope: MarketEventStoreAuditScope
    sequence_policy_id: str
    high_water_append_order: int
    record_count: int
    audited_at: datetime
    catalog_schema_fingerprint: str
    global_chain_head: str
    partition_count: int
    partition_head_root: str
    finding_count: int
    finding_set_digest: str
    chunk_count: int
    chunk_manifest_root: str
    first_global_record_hash: str
    last_global_record_hash: str
    audit_id: str

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
        records: Iterable[MarketEventSourceRecord],
        partition_heads: Iterable[MarketEventPartitionHead],
        findings: Iterable[MarketEventSequenceFinding],
    ) -> MarketEventStoreAudit:
        source_store_id = _require_sha256(source_store_id, "source_store_id")
        source_schema_id = _require_text(
            source_schema_id,
            "source_schema_id",
            maximum=256,
        )
        audited_at = _require_utc(audited_at, "audited_at")
        catalog_schema_fingerprint = _require_sha256(
            catalog_schema_fingerprint,
            "catalog_schema_fingerprint",
        )
        scan = _scan_prefix(
            records=records,
            partition_heads=partition_heads,
            findings=findings,
            source_store_id=source_store_id,
            audited_at=audited_at,
            sequence_policy_id=sequence_policy_id,
        )
        self = object.__new__(cls)
        values: dict[str, object] = {
            "source_store_id": source_store_id,
            "source_schema_id": source_schema_id,
            "scope": MarketEventStoreAuditScope.FULL_PREFIX,
            "sequence_policy_id": sequence_policy_id,
            "high_water_append_order": scan.high_water_append_order,
            "record_count": scan.record_count,
            "audited_at": audited_at,
            "catalog_schema_fingerprint": catalog_schema_fingerprint,
            "global_chain_head": scan.global_chain_head,
            "partition_count": scan.partition_count,
            "partition_head_root": scan.partition_head_root,
            "finding_count": scan.finding_count,
            "finding_set_digest": scan.finding_set_digest,
            "chunk_count": scan.chunk_count,
            "chunk_manifest_root": scan.chunk_manifest_root,
            "first_global_record_hash": scan.first_global_record_hash,
            "last_global_record_hash": scan.last_global_record_hash,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "audit_id", _hash_document(self.as_dict(include_id=False)))
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": MARKET_EVENT_STORE_AUDIT_SCHEMA,
            "source_store_id": self.source_store_id,
            "source_schema_id": self.source_schema_id,
            "scope": self.scope.value,
            "sequence_policy_id": self.sequence_policy_id,
            "high_water_append_order": self.high_water_append_order,
            "record_count": self.record_count,
            "audited_at": _utc_text(self.audited_at),
            "catalog_schema_fingerprint": self.catalog_schema_fingerprint,
            "global_chain_head": self.global_chain_head,
            "partition_count": self.partition_count,
            "partition_head_root": self.partition_head_root,
            "finding_count": self.finding_count,
            "finding_set_digest": self.finding_set_digest,
            "chunk_count": self.chunk_count,
            "chunk_manifest_root": self.chunk_manifest_root,
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


@dataclass(frozen=True, slots=True, init=False)
class MarketEventStoreSnapshot:
    audit: MarketEventStoreAudit
    snapshot_id: str

    def __init__(self) -> None:
        raise TypeError("MarketEventStoreSnapshot must be created from an audit")

    @classmethod
    def from_audit(cls, audit: MarketEventStoreAudit) -> MarketEventStoreSnapshot:
        if type(audit) is not MarketEventStoreAudit:
            raise MarketEventSourceContractError(
                "audit must be MarketEventStoreAudit"
            )
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
        symbol: str,
        market: Market,
        start_source_time: datetime,
        end_source_time: datetime,
        record_limit: int,
    ) -> MarketEventSelection:
        market = _require_market(market)
        symbol = _require_symbol(symbol, market)
        start = _require_utc(start_source_time, "start_source_time")
        end = _require_utc(end_source_time, "end_source_time")
        if end < start:
            raise MarketEventSourceContractError(
                "selection end cannot precede start"
            )
        limit = _require_int(
            record_limit,
            "record_limit",
            minimum=1,
            maximum=_MAX_SELECTION_RECORDS,
        )
        scan = _scan_prefix(
            records=records,
            partition_heads=partition_heads,
            findings=findings,
            source_store_id=self.audit.source_store_id,
            audited_at=self.audit.audited_at,
            sequence_policy_id=self.audit.sequence_policy_id,
            selection_query=(symbol, market, start, end),
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
            record_limit=limit,
        )

    def verify_selection(self, selection: MarketEventSelection) -> None:
        if type(selection) is not MarketEventSelection or (
            selection.snapshot_id != self.snapshot_id
            or selection.snapshot_audit_id != self.audit.audit_id
            or selection.source_store_id != self.audit.source_store_id
            or selection.snapshot_high_water_append_order
            != self.audit.high_water_append_order
            or selection.snapshot_finding_set_digest
            != self.audit.finding_set_digest
        ):
            raise MarketEventSourceContractError(
                "selection was not produced from this exact source snapshot"
            )


@dataclass(frozen=True, slots=True, init=False)
class MarketEventSelection:
    source_store_id: str
    snapshot_id: str
    snapshot_audit_id: str
    snapshot_high_water_append_order: int
    snapshot_finding_set_digest: str
    symbol: str
    market: Market
    start_source_time: datetime
    end_source_time: datetime
    record_limit: int
    records: tuple[MarketEventSourceRecord, ...]
    relevant_findings: tuple[MarketEventSequenceFinding, ...]
    relevant_finding_set_digest: str
    range_proof_digest: str
    selection_id: str

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
        record_limit: int,
    ) -> MarketEventSelection:
        self = object.__new__(cls)
        values: dict[str, object] = {
            "source_store_id": snapshot.audit.source_store_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_audit_id": snapshot.audit.audit_id,
            "snapshot_high_water_append_order": snapshot.audit.high_water_append_order,
            "snapshot_finding_set_digest": snapshot.audit.finding_set_digest,
            "symbol": symbol,
            "market": market,
            "start_source_time": start,
            "end_source_time": end,
            "record_limit": record_limit,
            "records": records,
            "relevant_findings": findings,
            "relevant_finding_set_digest": _selected_finding_digest(findings),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "range_proof_digest",
            _hash_document(
                {
                    "schema": "stage4g1-market-event-selection-range-proof-v1",
                    "snapshot_id": snapshot.snapshot_id,
                    "audit_id": snapshot.audit.audit_id,
                    "high_water_append_order": snapshot.audit.high_water_append_order,
                    "global_chain_head": snapshot.audit.global_chain_head,
                    "chunk_manifest_root": snapshot.audit.chunk_manifest_root,
                    "symbol": symbol,
                    "market": market.value,
                    "start_source_time": _utc_text(start),
                    "end_source_time": _utc_text(end),
                    "record_ids": [item.source_record_id for item in records],
                    "record_hashes": [item.record_hash for item in records],
                    "relevant_finding_set_digest": self.relevant_finding_set_digest,
                }
            ),
        )
        object.__setattr__(
            self,
            "selection_id",
            _hash_document(self.as_dict(include_id=False)),
        )
        return self

    def as_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        document = {
            "schema": MARKET_EVENT_SELECTION_SCHEMA,
            "source_store_id": self.source_store_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_audit_id": self.snapshot_audit_id,
            "snapshot_high_water_append_order": self.snapshot_high_water_append_order,
            "snapshot_finding_set_digest": self.snapshot_finding_set_digest,
            "symbol": self.symbol,
            "market": self.market.value,
            "start_source_time": _utc_text(self.start_source_time),
            "end_source_time": _utc_text(self.end_source_time),
            "record_limit": self.record_limit,
            "record_ids": [item.source_record_id for item in self.records],
            "record_hashes": [item.record_hash for item in self.records],
            "relevant_finding_ids": [
                item.finding_id for item in self.relevant_findings
            ],
            "relevant_finding_set_digest": self.relevant_finding_set_digest,
            "range_proof_digest": self.range_proof_digest,
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

    def select(
        self,
        *,
        snapshot: MarketEventStoreSnapshot,
        symbol: str,
        market: Market,
        start_source_time: datetime,
        end_source_time: datetime,
        record_limit: int,
    ) -> MarketEventSelection:
        ...


__all__ = [
    "MARKET_EVENT_READ_PORT_SCHEMA",
    "MARKET_EVENT_SELECTION_SCHEMA",
    "MARKET_EVENT_SEQUENCE_POLICY_V1",
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
