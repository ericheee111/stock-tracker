# ruff: noqa: FLY002, UP006, UP031, UP035, UP037, UP045
"""Strict, Python-3.9-compatible XTP sidecar wire contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

EVENT_SCHEMA = "stock-tracker-xtp-event-v1"
HEALTH_SCHEMA = "stock-tracker-xtp-health-v1"
SESSION_SCHEMA = "stock-tracker-xtp-session-v1"
METRICS_SCHEMA = "stock-tracker-xtp-metrics-v1"
EVENTS_RESPONSE_SCHEMA = "stock-tracker-xtp-events-response-v1"
SOURCE_NAME = "xtp"

ENV_QUOTE_USER = "_".join(("STOCK", "TRACKER", "XTP", "QUOTE", "USER"))
ENV_QUOTE_PASSWORD = "_".join(("STOCK", "TRACKER", "XTP", "QUOTE", "PASSWORD"))
ENV_QUOTE_SERVER = "_".join(("STOCK", "TRACKER", "XTP", "QUOTE", "SERVER"))
ENV_QUOTE_PORT = "_".join(("STOCK", "TRACKER", "XTP", "QUOTE", "PORT"))
ENV_QUOTE_PROTOCOL = "_".join(("STOCK", "TRACKER", "XTP", "QUOTE", "PROTOCOL"))
ENV_CLIENT_ID = "_".join(("STOCK", "TRACKER", "XTP", "CLIENT", "ID"))
ENV_SIDECAR_ACCESS = "_".join(("STOCK", "TRACKER", "XTP", "SIDECAR", "ACCESS"))

_ALLOWED_EVENT_TYPES = frozenset(
    {
        "MARKET_DATA",
        "ORDER_BOOK",
        "TRADE_TICK",
        "ORDER_TICK",
        "TRADING_STATUS",
        "HEARTBEAT",
        "CONNECTION",
    }
)
_ALLOWED_FEED_MODES = frozenset({"SIMULATOR", "LEVEL1", "LEVEL2"})
_ALLOWED_MARKETS = frozenset({"A"})
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ)$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PAYLOAD_DEPTH = 8
_MAX_COLLECTION_ITEMS = 4096
_MAX_TEXT_LENGTH = 4096
_MAX_FUTURE_SECONDS = 120
_MAX_INTEGER = (1 << 63) - 1
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


class XtpSidecarContractError(ValueError):
    """Raised when XTP sidecar data violates the frozen wire contract."""


def strict_json_loads(raw: bytes) -> Any:
    """Decode UTF-8 JSON while rejecting duplicate keys and non-finite values."""

    if not isinstance(raw, bytes) or not raw:
        raise XtpSidecarContractError("JSON payload must be non-empty bytes")

    def pairs_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        output: Dict[str, Any] = {}
        for key, value in pairs:
            if not isinstance(key, str):
                raise XtpSidecarContractError("JSON object keys must be strings")
            if key in output:
                raise XtpSidecarContractError("JSON object contains duplicate keys")
            output[key] = value
        return output

    def reject_constant(value: str) -> Any:
        raise XtpSidecarContractError("JSON contains non-finite token: %s" % value)

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise XtpSidecarContractError("JSON payload must use UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise XtpSidecarContractError("invalid JSON payload") from exc


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value, path="$", depth=0)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise XtpSidecarContractError("hash input must be bytes")
    return hashlib.sha256(value).hexdigest()


def _validate_json_value(value: Any, *, path: str, depth: int) -> None:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise XtpSidecarContractError("payload nesting exceeds limit at %s" % path)
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER:
            raise XtpSidecarContractError("integer exceeds signed 64-bit range at %s" % path)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise XtpSidecarContractError("non-finite number at %s" % path)
        return
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH:
            raise XtpSidecarContractError("text exceeds limit at %s" % path)
        if any(ord(char) < 9 or (13 < ord(char) < 32) or ord(char) == 127 for char in value):
            raise XtpSidecarContractError("control character at %s" % path)
        return
    if isinstance(value, list):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise XtpSidecarContractError("array exceeds item limit at %s" % path)
        for index, item in enumerate(value):
            _validate_json_value(item, path="%s[%d]" % (path, index), depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise XtpSidecarContractError("object exceeds item limit at %s" % path)
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > _MAX_TEXT_LENGTH
                or any(ord(char) < 32 or ord(char) == 127 for char in key)
            ):
                raise XtpSidecarContractError("object key is invalid at %s" % path)
            _validate_json_value(item, path="%s.%s" % (path, key), depth=depth + 1)
        return
    raise XtpSidecarContractError("unsupported JSON type at %s" % path)


def _require_exact_keys(value: Mapping[str, Any], expected: Iterable[str], name: str) -> None:
    if not isinstance(value, dict):
        raise XtpSidecarContractError("%s must be an object" % name)
    expected_set = set(expected)
    actual = set(value)
    unknown = sorted(actual - expected_set)
    missing = sorted(expected_set - actual)
    if unknown:
        raise XtpSidecarContractError("%s contains unknown field: %s" % (name, unknown[0]))
    if missing:
        raise XtpSidecarContractError("%s is missing field: %s" % (name, missing[0]))


def _require_string(value: Any, name: str, *, pattern: Optional[re.Pattern] = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise XtpSidecarContractError("%s must be a non-empty trimmed string" % name)
    if len(value) > _MAX_TEXT_LENGTH:
        raise XtpSidecarContractError("%s exceeds length limit" % name)
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise XtpSidecarContractError("%s contains invalid characters" % name)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise XtpSidecarContractError("%s has invalid format" % name)
    return value


def _require_int(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_INTEGER,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise XtpSidecarContractError(
            "%s must be an integer in [%d, %d]" % (name, minimum, maximum)
        )
    return value


def _optional_int(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_INTEGER,
) -> Optional[int]:
    if value is None:
        return None
    return _require_int(value, name, minimum=minimum, maximum=maximum)


def _parse_aware_datetime(value: Any, name: str) -> datetime:
    text = _require_string(value, name)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise XtpSidecarContractError("%s must be ISO 8601" % name) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise XtpSidecarContractError("%s must include a timezone" % name)
    utc = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if (utc - now).total_seconds() > _MAX_FUTURE_SECONDS:
        raise XtpSidecarContractError("%s is too far in the future" % name)
    return utc


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise XtpSidecarContractError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def trading_day_for(value: datetime) -> str:
    """Return the A-share civil date for a timezone-aware instant."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise XtpSidecarContractError("trading-day source must be timezone-aware")
    return value.astimezone(_SHANGHAI).date().isoformat()


def validate_symbol(value: Any) -> str:
    symbol = _require_string(value, "symbol")
    if _SYMBOL_RE.fullmatch(symbol) is None:
        raise XtpSidecarContractError("symbol must use six-digit CODE.SH or CODE.SZ")
    return symbol


def validate_access(value: Optional[str]) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < 32 or len(value) > 4096:
        raise XtpSidecarContractError("sidecar access value must contain 32-4096 visible characters")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise XtpSidecarContractError("sidecar access value contains invalid characters")
    return value


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    source: str
    feed_mode: str
    market: str
    symbol: str
    event_type: str
    trading_day: str
    exchange_timestamp: Optional[datetime]
    provider_timestamp: Optional[datetime]
    received_at: datetime
    session_id: str
    callback_seq: int
    provider_seq: Optional[int]
    raw_payload_sha256: str
    payload: Dict[str, Any]
    schema: str = EVENT_SCHEMA

    def __post_init__(self) -> None:
        _require_string(self.schema, "schema")
        if self.schema != EVENT_SCHEMA:
            raise XtpSidecarContractError("unsupported event schema")
        _require_string(self.event_id, "event_id", pattern=_SHA256_RE)
        source = _require_string(self.source, "source")
        if source != SOURCE_NAME:
            raise XtpSidecarContractError("source must be xtp")
        feed_mode = _require_string(self.feed_mode, "feed_mode")
        if feed_mode not in _ALLOWED_FEED_MODES:
            raise XtpSidecarContractError("unsupported feed_mode")
        market = _require_string(self.market, "market")
        if market not in _ALLOWED_MARKETS:
            raise XtpSidecarContractError("unsupported market")
        validate_symbol(self.symbol)
        event_type = _require_string(self.event_type, "event_type")
        if event_type not in _ALLOWED_EVENT_TYPES:
            raise XtpSidecarContractError("unsupported event_type")
        trading_day_text = _require_string(self.trading_day, "trading_day")
        try:
            parsed_trading_day = date.fromisoformat(trading_day_text)
        except ValueError as exc:
            raise XtpSidecarContractError("trading_day must use YYYY-MM-DD") from exc

        now = datetime.now(timezone.utc)
        normalized_times: Dict[str, Optional[datetime]] = {}
        for name, value in (
            ("exchange_timestamp", self.exchange_timestamp),
            ("provider_timestamp", self.provider_timestamp),
            ("received_at", self.received_at),
        ):
            if value is None:
                if name == "received_at":
                    raise XtpSidecarContractError("received_at must be timezone-aware")
                normalized_times[name] = None
                continue
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise XtpSidecarContractError("%s must be timezone-aware" % name)
            utc = value.astimezone(timezone.utc)
            if (utc - now).total_seconds() > _MAX_FUTURE_SECONDS:
                raise XtpSidecarContractError("%s is too far in the future" % name)
            normalized_times[name] = utc

        received_at = normalized_times["received_at"]
        assert received_at is not None
        for name in ("exchange_timestamp", "provider_timestamp"):
            value = normalized_times[name]
            if value is not None and (value - received_at).total_seconds() > _MAX_FUTURE_SECONDS:
                raise XtpSidecarContractError("%s is after received_at beyond tolerance" % name)

        source_time = (
            normalized_times["exchange_timestamp"]
            or normalized_times["provider_timestamp"]
            or received_at
        )
        if parsed_trading_day.isoformat() != trading_day_for(source_time):
            raise XtpSidecarContractError("trading_day does not match the A-share source date")

        object.__setattr__(self, "exchange_timestamp", normalized_times["exchange_timestamp"])
        object.__setattr__(self, "provider_timestamp", normalized_times["provider_timestamp"])
        object.__setattr__(self, "received_at", received_at)

        _require_string(self.session_id, "session_id", pattern=_IDENTIFIER_RE)
        _require_int(self.callback_seq, "callback_seq", minimum=1)
        _optional_int(self.provider_seq, "provider_seq", minimum=0)
        _require_string(
            self.raw_payload_sha256,
            "raw_payload_sha256",
            pattern=_SHA256_RE,
        )
        if not isinstance(self.payload, dict):
            raise XtpSidecarContractError("payload must be an object")
        raw_payload = canonical_json_bytes(self.payload)
        normalized_payload = strict_json_loads(raw_payload)
        if not isinstance(normalized_payload, dict):
            raise XtpSidecarContractError("payload must be an object")
        object.__setattr__(self, "payload", normalized_payload)
        if sha256_hex(raw_payload) != self.raw_payload_sha256:
            raise XtpSidecarContractError("raw payload hash mismatch")
        if sha256_hex(canonical_json_bytes(self._identity_dict())) != self.event_id:
            raise XtpSidecarContractError("event_id mismatch")

    @classmethod
    def create(
        cls,
        *,
        feed_mode: str,
        symbol: str,
        event_type: str,
        trading_day: str,
        received_at: datetime,
        session_id: str,
        callback_seq: int,
        payload: Dict[str, Any],
        exchange_timestamp: Optional[datetime] = None,
        provider_timestamp: Optional[datetime] = None,
        provider_seq: Optional[int] = None,
    ) -> "EventEnvelope":
        raw_payload = canonical_json_bytes(payload)
        raw_hash = sha256_hex(raw_payload)
        identity = {
            "schema": EVENT_SCHEMA,
            "source": SOURCE_NAME,
            "feed_mode": feed_mode,
            "market": "A",
            "symbol": symbol,
            "event_type": event_type,
            "trading_day": trading_day,
            "exchange_timestamp": None if exchange_timestamp is None else _iso_utc(exchange_timestamp),
            "provider_timestamp": None if provider_timestamp is None else _iso_utc(provider_timestamp),
            "received_at": _iso_utc(received_at),
            "session_id": session_id,
            "callback_seq": callback_seq,
            "provider_seq": provider_seq,
            "raw_payload_sha256": raw_hash,
        }
        event_id = sha256_hex(canonical_json_bytes(identity))
        return cls(
            event_id=event_id,
            source=SOURCE_NAME,
            feed_mode=feed_mode,
            market="A",
            symbol=symbol,
            event_type=event_type,
            trading_day=trading_day,
            exchange_timestamp=exchange_timestamp,
            provider_timestamp=provider_timestamp,
            received_at=received_at,
            session_id=session_id,
            callback_seq=callback_seq,
            provider_seq=provider_seq,
            raw_payload_sha256=raw_hash,
            payload=payload,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        fields = (
            "schema",
            "event_id",
            "source",
            "feed_mode",
            "market",
            "symbol",
            "event_type",
            "trading_day",
            "exchange_timestamp",
            "provider_timestamp",
            "received_at",
            "session_id",
            "callback_seq",
            "provider_seq",
            "raw_payload_sha256",
            "payload",
        )
        _require_exact_keys(value, fields, "event")
        payload = value["payload"]
        if not isinstance(payload, dict):
            raise XtpSidecarContractError("payload must be an object")
        event = cls(
            schema=_require_string(value["schema"], "schema"),
            event_id=_require_string(value["event_id"], "event_id"),
            source=_require_string(value["source"], "source"),
            feed_mode=_require_string(value["feed_mode"], "feed_mode"),
            market=_require_string(value["market"], "market"),
            symbol=validate_symbol(value["symbol"]),
            event_type=_require_string(value["event_type"], "event_type"),
            trading_day=_require_string(value["trading_day"], "trading_day"),
            exchange_timestamp=(
                None
                if value["exchange_timestamp"] is None
                else _parse_aware_datetime(value["exchange_timestamp"], "exchange_timestamp")
            ),
            provider_timestamp=(
                None
                if value["provider_timestamp"] is None
                else _parse_aware_datetime(value["provider_timestamp"], "provider_timestamp")
            ),
            received_at=_parse_aware_datetime(value["received_at"], "received_at"),
            session_id=_require_string(value["session_id"], "session_id"),
            callback_seq=_require_int(value["callback_seq"], "callback_seq", minimum=1),
            provider_seq=_optional_int(value["provider_seq"], "provider_seq", minimum=0),
            raw_payload_sha256=_require_string(value["raw_payload_sha256"], "raw_payload_sha256"),
            payload=payload,
        )
        if sha256_hex(canonical_json_bytes(event.payload)) != event.raw_payload_sha256:
            raise XtpSidecarContractError("raw payload hash mismatch")
        if sha256_hex(canonical_json_bytes(event._identity_dict())) != event.event_id:
            raise XtpSidecarContractError("event_id mismatch")
        return event

    def _identity_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "source": self.source,
            "feed_mode": self.feed_mode,
            "market": self.market,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "trading_day": self.trading_day,
            "exchange_timestamp": None if self.exchange_timestamp is None else _iso_utc(self.exchange_timestamp),
            "provider_timestamp": None if self.provider_timestamp is None else _iso_utc(self.provider_timestamp),
            "received_at": _iso_utc(self.received_at),
            "session_id": self.session_id,
            "callback_seq": self.callback_seq,
            "provider_seq": self.provider_seq,
            "raw_payload_sha256": self.raw_payload_sha256,
        }

    def as_dict(self) -> Dict[str, Any]:
        value = self._identity_dict()
        value["event_id"] = self.event_id
        value["payload"] = copy.deepcopy(self.payload)
        return value


def validate_symbol_list(values: Iterable[Any], *, maximum: int = 20) -> List[str]:
    if isinstance(values, (str, bytes)):
        raise XtpSidecarContractError("symbols must be an array")
    symbols = [validate_symbol(value) for value in values]
    if not symbols:
        raise XtpSidecarContractError("at least one symbol is required")
    if len(symbols) > maximum:
        raise XtpSidecarContractError("symbol count exceeds maximum of %d" % maximum)
    if len(set(symbols)) != len(symbols):
        raise XtpSidecarContractError("symbols must be unique")
    return symbols
