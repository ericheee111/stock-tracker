"""Append-only runtime outcome collection and finalization service.

Stage 4G bridges a mutable runtime :class:`stock_tracker.core.types.Signal` to
Stage 4F's immutable terminal :class:`SignalOutcome` ledger without pretending
that caller-declared hashes or manual facts are independently trusted.  The
collection database is separate from both the production runtime database and
the terminal outcome ledger.  Every lifecycle fact is appended to a global
SHA-256 chain; finalization is prepared durably before the terminal outcome is
submitted idempotently to Stage 4F.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from stock_tracker.core import types as RuntimeTypes
from stock_tracker.core.types import DataStatus, Market, SignalState
from stock_tracker.quant.backtest.market_rules import TradeSide
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.outcomes import (
    OutcomeContractError,
    OutcomeEvidenceOrigin,
    OutcomeFillEvidence,
    OutcomePathPoint,
    OutcomeTerminalReason,
    SignalOutcome,
    TradeIntentEvidence,
)
from stock_tracker.quant.core.time import ensure_aware, to_utc
from stock_tracker.quant.data.bar_artifact import DataTrustTier

from .outcome_ledger import (
    OutcomeLedger,
    OutcomeLedgerAppendResult,
    OutcomeLedgerDisposition,
    OutcomeLedgerError,
    signal_outcome_from_dict,
    signal_outcome_to_dict,
)

COLLECTION_SCHEMA = "stage4g-outcome-collection-v1"
COLLECTION_EVENT_SCHEMA = "stage4g-outcome-collection-event-v1"
RUNTIME_SCORE_SNAPSHOT_SCHEMA = "stage4g-runtime-score-snapshot-v1"
RUNTIME_SIGNAL_SNAPSHOT_SCHEMA = "stage4g-runtime-signal-snapshot-v1"
CASE_OPENED_SCHEMA = "stage4g-outcome-case-opened-v1"
ENTRY_FILL_SCHEMA = "stage4g-outcome-entry-fill-v1"
PATH_POINT_SCHEMA = "stage4g-outcome-path-point-v1"
EXIT_REQUEST_SCHEMA = "stage4g-outcome-exit-request-v1"
EXIT_FILL_SCHEMA = "stage4g-outcome-exit-fill-v1"
NO_ENTRY_SCHEMA = "stage4g-outcome-no-entry-v1"
FINALIZATION_PREPARED_SCHEMA = "stage4g-outcome-finalization-prepared-v1"
FINALIZED_SCHEMA = "stage4g-outcome-finalized-v1"
COLLECTION_AUDIT_SCHEMA = "stage4g-outcome-collection-audit-v1"
DEFAULT_COLLECTION_DATABASE = Path("data/outcome-collection.db")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_ZERO_HASH = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CANONICAL_DECIMAL = re.compile(
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_COLLECTION_META_COLUMNS = (
    ("key", "TEXT", 0, 1),
    ("value", "TEXT", 1, 0),
)
_COLLECTION_EVENT_COLUMNS = (
    ("append_order", "INTEGER", 0, 1),
    ("event_hash", "TEXT", 1, 0),
    ("previous_event_hash", "TEXT", 1, 0),
    ("case_id", "TEXT", 1, 0),
    ("event_type", "TEXT", 1, 0),
    ("fact_id", "TEXT", 1, 0),
    ("observed_at", "TEXT", 1, 0),
    ("recorded_by", "TEXT", 1, 0),
    ("payload_json", "TEXT", 1, 0),
    ("payload_sha256", "TEXT", 1, 0),
)
_ACTIONABLE_CAPTURE_STATES = {
    SignalState.TRIGGERED,
    SignalState.ACTIVE,
    SignalState.OVEREXTENDED,
    SignalState.DATA_INVALID,
    SignalState.INVALIDATED,
    SignalState.EXPIRED,
}
_ENTRY_FILL_STATES = {SignalState.TRIGGERED, SignalState.ACTIVE}
_COMPLETE_TERMINAL_REASONS = {
    OutcomeTerminalReason.TARGET,
    OutcomeTerminalReason.STOP,
    OutcomeTerminalReason.TIMEOUT,
    OutcomeTerminalReason.MANUAL,
    OutcomeTerminalReason.TRAILING_STOP,
    OutcomeTerminalReason.BROKEN_TREND,
    OutcomeTerminalReason.DATA_INVALID,
}
_NO_ENTRY_TERMINAL_REASONS = {
    OutcomeTerminalReason.ORDER_REJECTED,
    OutcomeTerminalReason.DATA_INVALID,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OutcomeCollectionError(RuntimeError):
    """Raised when Stage 4G collection evidence or state is invalid."""


class OutcomeCollectionConflict(OutcomeCollectionError):
    """Raised when immutable collection facts conflict."""


class OutcomeCollectionMode(StrEnum):
    PAPER = "PAPER"
    LIVE_MANUAL = "LIVE_MANUAL"


class OutcomeCollectionEventType(StrEnum):
    CASE_OPENED = "CASE_OPENED"
    ENTRY_FILLED = "ENTRY_FILLED"
    PATH_POINT = "PATH_POINT"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_FILLED = "EXIT_FILLED"
    NO_ENTRY = "NO_ENTRY"
    FINALIZATION_PREPARED = "FINALIZATION_PREPARED"
    FINALIZED = "FINALIZED"


class OutcomeCollectionDisposition(StrEnum):
    APPENDED = "APPENDED"
    IDEMPOTENT = "IDEMPOTENT"


class OutcomeCollectionCaseState(StrEnum):
    AWAITING_ENTRY = "AWAITING_ENTRY"
    OPEN_POSITION = "OPEN_POSITION"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    TERMINAL_READY = "TERMINAL_READY"
    FINALIZATION_PREPARED = "FINALIZATION_PREPARED"
    FINALIZED = "FINALIZED"


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OutcomeCollectionError(f"{name} must be an object with string keys")
    return value


def _require_fields(
    value: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise OutcomeCollectionError(
            f"{name} field set is invalid; missing={sorted(expected - actual)}; "
            f"extra={sorted(actual - expected)}"
        )


def _require_text(
    value: object,
    name: str,
    *,
    max_length: int = 4096,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > max_length:
        raise OutcomeCollectionError(f"{name} must be a safe trimmed string")
    if not allow_empty and not value:
        raise OutcomeCollectionError(f"{name} must be a non-empty string")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise OutcomeCollectionError(f"{name} contains unsafe control characters")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_safe_token(value: object, name: str) -> str:
    text = _require_text(value, name, max_length=128)
    if _SAFE_TOKEN.fullmatch(text) is None:
        raise OutcomeCollectionError(f"{name} must be a safe token")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise OutcomeCollectionError(f"{name} must be boolean")
    return value


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise OutcomeCollectionError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, max_length=64)
    if _SHA256.fullmatch(text) is None:
        raise OutcomeCollectionError(f"{name} must be lowercase SHA-256")
    return text


def _require_market(value: object, name: str = "market") -> Market:
    try:
        return Market(_require_text(value, name, max_length=8))
    except ValueError as exc:
        raise OutcomeCollectionError(f"{name} is invalid") from exc


def _require_symbol(value: object, market: Market) -> str:
    symbol = _require_text(value, "symbol", max_length=64)
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if symbol != symbol.upper() or not symbol.endswith(suffixes[market]):
        raise OutcomeCollectionError("symbol suffix must match market")
    return symbol


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise OutcomeCollectionError("decimal value must be finite Decimal")
    sign, digits, exponent = value.as_tuple()
    del sign
    if len(digits) > 256 or abs(exponent) > 256:
        raise OutcomeCollectionError("decimal value exceeds the Stage 4G bound")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    canonical = "0" if value == 0 else text
    if len(canonical) > 256:
        raise OutcomeCollectionError("decimal value exceeds the Stage 4G bound")
    return canonical


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise OutcomeCollectionError(f"{name} must be finite Decimal")
    if positive and value <= 0:
        raise OutcomeCollectionError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise OutcomeCollectionError(f"{name} must be non-negative")
    _decimal_text(value)
    return value


def _decimal_from_text(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    text = _require_text(value, name, max_length=256)
    if _CANONICAL_DECIMAL.fullmatch(text) is None or text == "-0":
        raise OutcomeCollectionError(f"{name} must use canonical decimal text")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise OutcomeCollectionError(f"{name} is invalid") from exc
    if _decimal_text(result) != text:
        raise OutcomeCollectionError(f"{name} must use canonical decimal text")
    return _require_decimal(
        result,
        name,
        positive=positive,
        nonnegative=nonnegative,
    )


def _decimal_from_runtime_number(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise OutcomeCollectionError(f"runtime {name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise OutcomeCollectionError(f"runtime {name} must be finite")
    return _require_decimal(
        Decimal(str(value)),
        name,
        positive=positive,
        nonnegative=nonnegative,
    )


def _datetime_text(value: datetime) -> str:
    return (
        to_utc(ensure_aware(value, "datetime"))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _datetime_from_text(value: object, name: str) -> datetime:
    text = _require_text(value, name, max_length=64)
    if not text.endswith("Z"):
        raise OutcomeCollectionError(f"{name} must be canonical UTC with Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise OutcomeCollectionError(f"{name} must be ISO-8601") from exc
    if _datetime_text(parsed) != text:
        raise OutcomeCollectionError(f"{name} must use canonical UTC representation")
    return parsed


def _runtime_state_time_text(value: datetime) -> tuple[str, bool]:
    if not isinstance(value, datetime):
        raise OutcomeCollectionError("runtime signal state_changed_at must be datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return _datetime_text(value), True
    return value.isoformat(timespec="microseconds"), False


def _validate_runtime_state_time_text(value: object, aware: bool) -> str:
    text = _require_text(value, "runtime_state_changed_at", max_length=64)
    if aware:
        _datetime_from_text(text, "runtime_state_changed_at")
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OutcomeCollectionError(
            "runtime_state_changed_at must be ISO-8601"
        ) from exc
    if parsed.tzinfo is not None or parsed.utcoffset() is not None:
        raise OutcomeCollectionError(
            "runtime_state_changed_at awareness declaration is inconsistent"
        )
    if parsed.isoformat(timespec="microseconds") != text:
        raise OutcomeCollectionError(
            "runtime_state_changed_at must use canonical microseconds"
        )
    return text


def _normalize_evidence_ids(
    values: Iterable[str],
    name: str = "evidence_ids",
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise OutcomeCollectionError(f"{name} must be an iterable of SHA-256 IDs")
    normalized = tuple(_require_sha256(item, name) for item in values)
    if normalized != tuple(sorted(set(normalized))):
        raise OutcomeCollectionError(f"{name} must be sorted and unique")
    return normalized


def _canonical_json_text(value: Mapping[str, Any]) -> str:
    try:
        text = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise OutcomeCollectionError("collection payload is not canonical JSON") from exc
    if not text or len(text.encode("utf-8")) > _MAX_JSON_BYTES:
        raise OutcomeCollectionError("collection payload exceeds the size bound")
    return text


def _canonical_json_loads(text: object, name: str) -> Mapping[str, Any]:
    raw = _require_text(text, name, max_length=_MAX_JSON_BYTES)

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise OutcomeCollectionError(f"{name} contains duplicate JSON keys")
            document[key] = value
        return document

    def reject_constant(token: str) -> None:
        raise OutcomeCollectionError(f"{name} contains non-finite token: {token}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OutcomeCollectionError(f"{name} is not valid JSON") from exc
    document = _require_mapping(value, name)
    if _canonical_json_text(document) != raw:
        raise OutcomeCollectionError(f"{name} is not in canonical JSON form")
    return document


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_path(path: Path, name: str) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise OutcomeCollectionError(f"{name} cannot traverse a symlink or junction")
    return absolute.resolve(strict=False)


def _same_existing_file(left: Path, right: Path, name: str) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right)
    except OSError as exc:
        raise OutcomeCollectionError(f"cannot verify {name} file identity") from exc


def _path_identity(path: Path, name: str) -> tuple[int, int]:
    try:
        status = path.stat()
    except OSError as exc:
        raise OutcomeCollectionError(f"cannot inspect {name} identity") from exc
    return int(status.st_dev), int(status.st_ino)


@dataclass(frozen=True, slots=True)
class RuntimeScoreSnapshot:
    opportunity: int
    timing: int
    risk: int
    confidence: int
    success_probability: Decimal | None
    positive_reasons: tuple[str, ...]
    negative_reasons: tuple[str, ...]
    score_snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("opportunity", "timing", "risk", "confidence"):
            _require_int(getattr(self, name), name, maximum=100)
        probability = self.success_probability
        if probability is not None:
            _require_decimal(probability, "success_probability", nonnegative=True)
            if probability > 1:
                raise OutcomeCollectionError("success_probability cannot exceed one")
        positive = tuple(
            _require_text(item, "positive_reason", max_length=1024)
            for item in self.positive_reasons
        )
        negative = tuple(
            _require_text(item, "negative_reason", max_length=1024)
            for item in self.negative_reasons
        )
        object.__setattr__(self, "positive_reasons", positive)
        object.__setattr__(self, "negative_reasons", negative)
        object.__setattr__(
            self,
            "score_snapshot_id",
            fingerprint(self._identity_payload()),
        )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": RUNTIME_SCORE_SNAPSHOT_SCHEMA,
            "opportunity": self.opportunity,
            "timing": self.timing,
            "risk": self.risk,
            "confidence": self.confidence,
            "success_probability": (
                None
                if self.success_probability is None
                else _decimal_text(self.success_probability)
            ),
            "positive_reasons": list(self.positive_reasons),
            "negative_reasons": list(self.negative_reasons),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "score_snapshot_id": self.score_snapshot_id,
        }

    @classmethod
    def from_runtime(cls, value: RuntimeTypes.ScoreSet) -> RuntimeScoreSnapshot:
        if not isinstance(value, RuntimeTypes.ScoreSet):
            raise OutcomeCollectionError("runtime signal must carry ScoreSet evidence")
        probability: Decimal | None = None
        if value.success_probability is not None:
            probability = _decimal_from_runtime_number(
                value.success_probability,
                "success_probability",
                nonnegative=True,
            )
            if probability > 1:
                raise OutcomeCollectionError("success_probability cannot exceed one")
        return cls(
            opportunity=_require_int(value.opportunity, "opportunity", maximum=100),
            timing=_require_int(value.timing, "timing", maximum=100),
            risk=_require_int(value.risk, "risk", maximum=100),
            confidence=_require_int(value.confidence, "confidence", maximum=100),
            success_probability=probability,
            positive_reasons=tuple(value.positive_reasons),
            negative_reasons=tuple(value.negative_reasons),
        )

    @classmethod
    def from_dict(cls, value: object) -> RuntimeScoreSnapshot:
        document = _require_mapping(value, "runtime score snapshot")
        _require_fields(
            document,
            {
                "schema",
                "opportunity",
                "timing",
                "risk",
                "confidence",
                "success_probability",
                "positive_reasons",
                "negative_reasons",
                "score_snapshot_id",
            },
            "runtime score snapshot",
        )
        if document["schema"] != RUNTIME_SCORE_SNAPSHOT_SCHEMA:
            raise OutcomeCollectionError("runtime score snapshot schema is invalid")
        positive = document["positive_reasons"]
        negative = document["negative_reasons"]
        if not isinstance(positive, list) or not isinstance(negative, list):
            raise OutcomeCollectionError("runtime score reasons must be arrays")
        probability_value = document["success_probability"]
        snapshot = cls(
            opportunity=_require_int(document["opportunity"], "opportunity", maximum=100),
            timing=_require_int(document["timing"], "timing", maximum=100),
            risk=_require_int(document["risk"], "risk", maximum=100),
            confidence=_require_int(document["confidence"], "confidence", maximum=100),
            success_probability=(
                None
                if probability_value is None
                else _decimal_from_text(
                    probability_value,
                    "success_probability",
                    nonnegative=True,
                )
            ),
            positive_reasons=tuple(positive),
            negative_reasons=tuple(negative),
        )
        if snapshot.score_snapshot_id != _require_sha256(
            document["score_snapshot_id"],
            "score_snapshot_id",
        ):
            raise OutcomeCollectionError("runtime score snapshot ID mismatch")
        if snapshot.as_dict() != dict(document):
            raise OutcomeCollectionError("runtime score snapshot canonical form is invalid")
        return snapshot


@dataclass(frozen=True, slots=True)
class RuntimeSignalSnapshot:
    signal_id: str
    symbol: str
    market: Market
    strategy_id: str
    strategy_version: str
    horizon_sessions: int
    model_id: str | None
    instrument_id: str
    identity_fact_id: str
    data_snapshot_id: str
    policy_id: str
    classification_id: str | None
    market_regime: str
    sector_stage: str
    runtime_signal_state: SignalState
    runtime_state_changed_at: str
    runtime_state_time_aware: bool
    runtime_data_status: DataStatus
    reason: str
    next_trigger: str
    requested_quantity: int
    entry_execution_policy_id: str
    entry_low: Decimal
    entry_high: Decimal
    trigger_price: Decimal
    invalidation_price: Decimal
    target_1: Decimal
    target_2: Decimal
    reward_risk: Decimal
    scores: RuntimeScoreSnapshot
    captured_at: datetime
    runtime_episode_id: str = field(init=False)
    decision_snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        signal_id = _require_text(self.signal_id, "signal_id", max_length=256)
        if not isinstance(self.market, Market):
            raise OutcomeCollectionError("market must be Market")
        symbol = _require_symbol(self.symbol, self.market)
        strategy_id = _require_text(self.strategy_id, "strategy_id", max_length=128)
        strategy_version = _require_text(
            self.strategy_version,
            "strategy_version",
            max_length=128,
        )
        horizon = _require_int(
            self.horizon_sessions,
            "horizon_sessions",
            minimum=1,
            maximum=100000,
        )
        model_id = (
            None if self.model_id is None else _require_text(self.model_id, "model_id")
        )
        instrument_id = _require_text(self.instrument_id, "instrument_id")
        identity_fact_id = _require_sha256(self.identity_fact_id, "identity_fact_id")
        data_snapshot_id = _require_sha256(self.data_snapshot_id, "data_snapshot_id")
        policy_id = _require_sha256(self.policy_id, "policy_id")
        classification_id = _require_optional_text(
            self.classification_id,
            "classification_id",
        )
        market_regime = _require_text(self.market_regime, "market_regime")
        sector_stage = _require_text(
            self.sector_stage,
            "sector_stage",
            max_length=256,
            allow_empty=True,
        )
        if not isinstance(self.runtime_signal_state, SignalState):
            raise OutcomeCollectionError("runtime_signal_state must be SignalState")
        if self.runtime_signal_state not in _ACTIONABLE_CAPTURE_STATES:
            raise OutcomeCollectionError(
                "runtime signal state is not eligible for outcome collection"
            )
        aware = _require_bool(
            self.runtime_state_time_aware,
            "runtime_state_time_aware",
        )
        state_time = _validate_runtime_state_time_text(
            self.runtime_state_changed_at,
            aware,
        )
        if not isinstance(self.runtime_data_status, DataStatus):
            raise OutcomeCollectionError("runtime_data_status must be DataStatus")
        reason = _require_text(self.reason, "reason")
        next_trigger = _require_text(
            self.next_trigger,
            "next_trigger",
            allow_empty=True,
        )
        quantity = _require_int(
            self.requested_quantity,
            "requested_quantity",
            minimum=1,
        )
        execution_policy = _require_sha256(
            self.entry_execution_policy_id,
            "entry_execution_policy_id",
        )
        entry_low = _require_decimal(self.entry_low, "entry_low", positive=True)
        entry_high = _require_decimal(self.entry_high, "entry_high", positive=True)
        trigger = _require_decimal(self.trigger_price, "trigger_price", positive=True)
        invalidation = _require_decimal(
            self.invalidation_price,
            "invalidation_price",
            positive=True,
        )
        target_1 = _require_decimal(self.target_1, "target_1", positive=True)
        target_2 = _require_decimal(self.target_2, "target_2", positive=True)
        reward_risk = _require_decimal(
            self.reward_risk,
            "reward_risk",
            nonnegative=True,
        )
        if entry_low > entry_high:
            raise OutcomeCollectionError("entry_low cannot exceed entry_high")
        if invalidation >= min(entry_high, trigger):
            raise OutcomeCollectionError(
                "invalidation_price must be below the entry/trigger plan"
            )
        if target_1 <= invalidation or target_2 < target_1:
            raise OutcomeCollectionError("target plan is internally inconsistent")
        if not isinstance(self.scores, RuntimeScoreSnapshot):
            raise OutcomeCollectionError("scores must be RuntimeScoreSnapshot")
        captured = to_utc(ensure_aware(self.captured_at, "captured_at"))

        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "strategy_version", strategy_version)
        object.__setattr__(self, "horizon_sessions", horizon)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "identity_fact_id", identity_fact_id)
        object.__setattr__(self, "data_snapshot_id", data_snapshot_id)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "classification_id", classification_id)
        object.__setattr__(self, "market_regime", market_regime)
        object.__setattr__(self, "sector_stage", sector_stage)
        object.__setattr__(self, "runtime_state_changed_at", state_time)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "next_trigger", next_trigger)
        object.__setattr__(self, "requested_quantity", quantity)
        object.__setattr__(self, "entry_execution_policy_id", execution_policy)
        object.__setattr__(self, "entry_low", entry_low)
        object.__setattr__(self, "entry_high", entry_high)
        object.__setattr__(self, "trigger_price", trigger)
        object.__setattr__(self, "invalidation_price", invalidation)
        object.__setattr__(self, "target_1", target_1)
        object.__setattr__(self, "target_2", target_2)
        object.__setattr__(self, "reward_risk", reward_risk)
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(
            self,
            "runtime_episode_id",
            fingerprint(self._episode_payload()),
        )
        object.__setattr__(
            self,
            "decision_snapshot_id",
            fingerprint(self._identity_payload()),
        )

    def _episode_payload(self) -> dict[str, Any]:
        return {
            "schema": "stage4g-runtime-signal-episode-v1",
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "horizon_sessions": self.horizon_sessions,
            "model_id": self.model_id,
            "instrument_id": self.instrument_id,
            "identity_fact_id": self.identity_fact_id,
            "data_snapshot_id": self.data_snapshot_id,
            "policy_id": self.policy_id,
            "classification_id": self.classification_id,
            "market_regime": self.market_regime,
            "sector_stage": self.sector_stage,
            "runtime_signal_state": self.runtime_signal_state.value,
            "runtime_state_changed_at": self.runtime_state_changed_at,
            "runtime_state_time_aware": self.runtime_state_time_aware,
            "runtime_data_status": self.runtime_data_status.value,
            "reason": self.reason,
            "next_trigger": self.next_trigger,
            "requested_quantity": self.requested_quantity,
            "entry_execution_policy_id": self.entry_execution_policy_id,
            "entry_low": _decimal_text(self.entry_low),
            "entry_high": _decimal_text(self.entry_high),
            "trigger_price": _decimal_text(self.trigger_price),
            "invalidation_price": _decimal_text(self.invalidation_price),
            "target_1": _decimal_text(self.target_1),
            "target_2": _decimal_text(self.target_2),
            "reward_risk": _decimal_text(self.reward_risk),
            "scores": self.scores.as_dict(),
        }

    def _identity_payload(self) -> dict[str, Any]:
        return {
            **self._episode_payload(),
            "schema": RUNTIME_SIGNAL_SNAPSHOT_SCHEMA,
            "runtime_episode_id": self.runtime_episode_id,
            "captured_at": _datetime_text(self.captured_at),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "decision_snapshot_id": self.decision_snapshot_id,
        }

    @property
    def entry_intent(self) -> TradeIntentEvidence:
        try:
            return TradeIntentEvidence(
                symbol=self.symbol,
                market=self.market,
                side=TradeSide.BUY,
                requested_at=self.captured_at,
                requested_quantity=self.requested_quantity,
                decision_snapshot_id=self.decision_snapshot_id,
                execution_policy_id=self.entry_execution_policy_id,
            )
        except OutcomeContractError as exc:
            raise OutcomeCollectionError(
                "runtime snapshot cannot construct entry intent"
            ) from exc

    @classmethod
    def from_runtime_signal(
        cls,
        signal: RuntimeTypes.Signal,
        *,
        strategy_version: str,
        horizon_sessions: int,
        model_id: str | None,
        instrument_id: str,
        identity_fact_id: str,
        data_snapshot_id: str,
        policy_id: str,
        classification_id: str | None,
        requested_quantity: int,
        entry_execution_policy_id: str,
        captured_at: datetime,
    ) -> RuntimeSignalSnapshot:
        if not isinstance(signal, RuntimeTypes.Signal):
            raise OutcomeCollectionError("signal must be a runtime Signal")
        if signal.scores is None:
            raise OutcomeCollectionError("runtime signal has no score evidence")
        if not isinstance(signal.market, Market):
            raise OutcomeCollectionError("runtime signal market is invalid")
        if not isinstance(signal.state, SignalState):
            raise OutcomeCollectionError("runtime signal state is invalid")
        if not isinstance(signal.data_status, DataStatus):
            raise OutcomeCollectionError("runtime signal data status is invalid")
        state_time, state_time_aware = _runtime_state_time_text(
            signal.state_changed_at
        )
        return cls(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            market=signal.market,
            strategy_id=signal.strategy_id,
            strategy_version=strategy_version,
            horizon_sessions=horizon_sessions,
            model_id=model_id,
            instrument_id=instrument_id,
            identity_fact_id=identity_fact_id,
            data_snapshot_id=data_snapshot_id,
            policy_id=policy_id,
            classification_id=classification_id,
            market_regime=signal.market_regime,
            sector_stage=signal.sector_stage,
            runtime_signal_state=signal.state,
            runtime_state_changed_at=state_time,
            runtime_state_time_aware=state_time_aware,
            runtime_data_status=signal.data_status,
            reason=signal.reason,
            next_trigger=signal.next_trigger,
            requested_quantity=requested_quantity,
            entry_execution_policy_id=entry_execution_policy_id,
            entry_low=_decimal_from_runtime_number(
                signal.entry_low,
                "entry_low",
                positive=True,
            ),
            entry_high=_decimal_from_runtime_number(
                signal.entry_high,
                "entry_high",
                positive=True,
            ),
            trigger_price=_decimal_from_runtime_number(
                signal.trigger_price,
                "trigger_price",
                positive=True,
            ),
            invalidation_price=_decimal_from_runtime_number(
                signal.invalidation_price,
                "invalidation_price",
                positive=True,
            ),
            target_1=_decimal_from_runtime_number(
                signal.target_1,
                "target_1",
                positive=True,
            ),
            target_2=_decimal_from_runtime_number(
                signal.target_2,
                "target_2",
                positive=True,
            ),
            reward_risk=_decimal_from_runtime_number(
                signal.reward_risk,
                "reward_risk",
                nonnegative=True,
            ),
            scores=RuntimeScoreSnapshot.from_runtime(signal.scores),
            captured_at=captured_at,
        )

    @classmethod
    def from_dict(cls, value: object) -> RuntimeSignalSnapshot:
        document = _require_mapping(value, "runtime signal snapshot")
        expected = {
            "schema",
            "signal_id",
            "symbol",
            "market",
            "strategy_id",
            "strategy_version",
            "horizon_sessions",
            "model_id",
            "instrument_id",
            "identity_fact_id",
            "data_snapshot_id",
            "policy_id",
            "classification_id",
            "market_regime",
            "sector_stage",
            "runtime_signal_state",
            "runtime_state_changed_at",
            "runtime_state_time_aware",
            "runtime_data_status",
            "reason",
            "next_trigger",
            "requested_quantity",
            "entry_execution_policy_id",
            "entry_low",
            "entry_high",
            "trigger_price",
            "invalidation_price",
            "target_1",
            "target_2",
            "reward_risk",
            "scores",
            "runtime_episode_id",
            "captured_at",
            "decision_snapshot_id",
        }
        _require_fields(document, expected, "runtime signal snapshot")
        if document["schema"] != RUNTIME_SIGNAL_SNAPSHOT_SCHEMA:
            raise OutcomeCollectionError("runtime signal snapshot schema is invalid")
        try:
            state = SignalState(
                _require_text(document["runtime_signal_state"], "runtime_signal_state")
            )
            data_status = DataStatus(
                _require_text(document["runtime_data_status"], "runtime_data_status")
            )
        except ValueError as exc:
            raise OutcomeCollectionError("runtime signal enum is invalid") from exc
        snapshot = cls(
            signal_id=_require_text(document["signal_id"], "signal_id"),
            symbol=_require_text(document["symbol"], "symbol"),
            market=_require_market(document["market"]),
            strategy_id=_require_text(document["strategy_id"], "strategy_id"),
            strategy_version=_require_text(
                document["strategy_version"],
                "strategy_version",
            ),
            horizon_sessions=_require_int(
                document["horizon_sessions"],
                "horizon_sessions",
                minimum=1,
            ),
            model_id=_require_optional_text(document["model_id"], "model_id"),
            instrument_id=_require_text(document["instrument_id"], "instrument_id"),
            identity_fact_id=_require_sha256(
                document["identity_fact_id"],
                "identity_fact_id",
            ),
            data_snapshot_id=_require_sha256(
                document["data_snapshot_id"],
                "data_snapshot_id",
            ),
            policy_id=_require_sha256(document["policy_id"], "policy_id"),
            classification_id=_require_optional_text(
                document["classification_id"],
                "classification_id",
            ),
            market_regime=_require_text(document["market_regime"], "market_regime"),
            sector_stage=_require_text(
                document["sector_stage"],
                "sector_stage",
                allow_empty=True,
            ),
            runtime_signal_state=state,
            runtime_state_changed_at=_require_text(
                document["runtime_state_changed_at"],
                "runtime_state_changed_at",
            ),
            runtime_state_time_aware=_require_bool(
                document["runtime_state_time_aware"],
                "runtime_state_time_aware",
            ),
            runtime_data_status=data_status,
            reason=_require_text(document["reason"], "reason"),
            next_trigger=_require_text(
                document["next_trigger"],
                "next_trigger",
                allow_empty=True,
            ),
            requested_quantity=_require_int(
                document["requested_quantity"],
                "requested_quantity",
                minimum=1,
            ),
            entry_execution_policy_id=_require_sha256(
                document["entry_execution_policy_id"],
                "entry_execution_policy_id",
            ),
            entry_low=_decimal_from_text(
                document["entry_low"],
                "entry_low",
                positive=True,
            ),
            entry_high=_decimal_from_text(
                document["entry_high"],
                "entry_high",
                positive=True,
            ),
            trigger_price=_decimal_from_text(
                document["trigger_price"],
                "trigger_price",
                positive=True,
            ),
            invalidation_price=_decimal_from_text(
                document["invalidation_price"],
                "invalidation_price",
                positive=True,
            ),
            target_1=_decimal_from_text(
                document["target_1"],
                "target_1",
                positive=True,
            ),
            target_2=_decimal_from_text(
                document["target_2"],
                "target_2",
                positive=True,
            ),
            reward_risk=_decimal_from_text(
                document["reward_risk"],
                "reward_risk",
                nonnegative=True,
            ),
            scores=RuntimeScoreSnapshot.from_dict(document["scores"]),
            captured_at=_datetime_from_text(document["captured_at"], "captured_at"),
        )
        if snapshot.runtime_episode_id != _require_sha256(
            document["runtime_episode_id"],
            "runtime_episode_id",
        ):
            raise OutcomeCollectionError("runtime episode ID mismatch")
        if snapshot.decision_snapshot_id != _require_sha256(
            document["decision_snapshot_id"],
            "decision_snapshot_id",
        ):
            raise OutcomeCollectionError("runtime decision snapshot ID mismatch")
        if snapshot.as_dict() != dict(document):
            raise OutcomeCollectionError("runtime signal snapshot canonical form is invalid")
        return snapshot


def _case_id(snapshot: RuntimeSignalSnapshot, mode: OutcomeCollectionMode) -> str:
    return fingerprint(
        {
            "schema": "stage4g-outcome-case-identity-v1",
            "runtime_episode_id": snapshot.runtime_episode_id,
            "mode": mode.value,
        }
    )


def _outcome_signal_id(
    snapshot: RuntimeSignalSnapshot,
    mode: OutcomeCollectionMode,
) -> str:
    return f"runtime-episode:{mode.value.lower()}:{snapshot.runtime_episode_id}"


@dataclass(frozen=True, slots=True)
class OutcomeCollectionEvent:
    append_order: int
    case_id: str
    event_type: OutcomeCollectionEventType
    fact_id: str
    observed_at: datetime
    recorded_by: str
    previous_event_hash: str
    payload_json: str = field(repr=False)
    event_hash: str = field(init=False)

    def __post_init__(self) -> None:
        order = _require_int(self.append_order, "append_order", minimum=1)
        case_id = _require_sha256(self.case_id, "case_id")
        if not isinstance(self.event_type, OutcomeCollectionEventType):
            raise OutcomeCollectionError("event_type is invalid")
        fact_id = _require_sha256(self.fact_id, "fact_id")
        observed = to_utc(ensure_aware(self.observed_at, "observed_at"))
        actor = _require_safe_token(self.recorded_by, "recorded_by")
        previous = _require_sha256(
            self.previous_event_hash,
            "previous_event_hash",
        )
        payload = _canonical_json_loads(self.payload_json, "event payload_json")
        if _require_sha256(payload.get("fact_id"), "payload fact_id") != fact_id:
            raise OutcomeCollectionError("event fact_id disagrees with payload")
        object.__setattr__(self, "append_order", order)
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "recorded_by", actor)
        object.__setattr__(self, "previous_event_hash", previous)
        object.__setattr__(
            self,
            "event_hash",
            fingerprint(self._identity_payload()),
        )

    @property
    def payload(self) -> Mapping[str, Any]:
        return _canonical_json_loads(self.payload_json, "event payload_json")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": COLLECTION_EVENT_SCHEMA,
            "append_order": self.append_order,
            "case_id": self.case_id,
            "event_type": self.event_type.value,
            "fact_id": self.fact_id,
            "observed_at": _datetime_text(self.observed_at),
            "recorded_by": self.recorded_by,
            "previous_event_hash": self.previous_event_hash,
            "payload_json": self.payload_json,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), "event_hash": self.event_hash}

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OutcomeCollectionEvent:
        try:
            event_type = OutcomeCollectionEventType(row["event_type"])
        except ValueError as exc:
            raise OutcomeCollectionError("collection event type is invalid") from exc
        payload_json = _require_text(
            row["payload_json"],
            "payload_json",
            max_length=_MAX_JSON_BYTES,
        )
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if payload_sha != _require_sha256(row["payload_sha256"], "payload_sha256"):
            raise OutcomeCollectionError("collection payload SHA mismatch")
        event = cls(
            append_order=_require_int(row["append_order"], "append_order", minimum=1),
            case_id=_require_sha256(row["case_id"], "case_id"),
            event_type=event_type,
            fact_id=_require_sha256(row["fact_id"], "fact_id"),
            observed_at=_datetime_from_text(row["observed_at"], "observed_at"),
            recorded_by=_require_safe_token(row["recorded_by"], "recorded_by"),
            previous_event_hash=_require_sha256(
                row["previous_event_hash"],
                "previous_event_hash",
            ),
            payload_json=payload_json,
        )
        if event.event_hash != _require_sha256(row["event_hash"], "event_hash"):
            raise OutcomeCollectionError("collection event hash mismatch")
        return event


@dataclass(frozen=True, slots=True)
class OutcomeCollectionAppendResult:
    disposition: OutcomeCollectionDisposition
    event: OutcomeCollectionEvent
    case_state: OutcomeCollectionCaseState

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, OutcomeCollectionDisposition):
            raise OutcomeCollectionError("collection append disposition is invalid")
        if not isinstance(self.event, OutcomeCollectionEvent):
            raise OutcomeCollectionError("collection append event is invalid")
        if not isinstance(self.case_state, OutcomeCollectionCaseState):
            raise OutcomeCollectionError("collection case state is invalid")


@dataclass(frozen=True, slots=True)
class OutcomeCollectionCase:
    case_id: str
    mode: OutcomeCollectionMode
    snapshot: RuntimeSignalSnapshot
    event_hashes: tuple[str, ...]
    entry_fill: OutcomeFillEvidence | None
    path: tuple[OutcomePathPoint, ...]
    exit_intent: TradeIntentEvidence | None
    exit_fill: OutcomeFillEvidence | None
    terminal_reason: OutcomeTerminalReason | None
    prepared_outcome: SignalOutcome | None
    ledger_target_id: str | None
    finalized_record_hash: str | None
    finalized_record_append_order: int | None
    finalized_ledger_audit_id: str | None
    finalized_ledger_disposition: OutcomeLedgerDisposition | None
    state: OutcomeCollectionCaseState

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage4g-outcome-collection-case-v1",
            "case_id": self.case_id,
            "mode": self.mode.value,
            "runtime_signal_id": self.snapshot.signal_id,
            "runtime_episode_id": self.snapshot.runtime_episode_id,
            "outcome_signal_id": _outcome_signal_id(self.snapshot, self.mode),
            "decision_snapshot_id": self.snapshot.decision_snapshot_id,
            "state": self.state.value,
            "event_hashes": list(self.event_hashes),
            "entry_fill_id": (
                None if self.entry_fill is None else self.entry_fill.fill_id
            ),
            "path_point_ids": [item.point_id for item in self.path],
            "exit_intent_id": (
                None if self.exit_intent is None else self.exit_intent.intent_id
            ),
            "exit_fill_id": None if self.exit_fill is None else self.exit_fill.fill_id,
            "terminal_reason": (
                None if self.terminal_reason is None else self.terminal_reason.value
            ),
            "prepared_outcome_id": (
                None
                if self.prepared_outcome is None
                else self.prepared_outcome.outcome_id
            ),
            "ledger_target_id": self.ledger_target_id,
            "finalized_record_hash": self.finalized_record_hash,
            "finalized_record_append_order": self.finalized_record_append_order,
            "finalized_ledger_audit_id": self.finalized_ledger_audit_id,
            "finalized_ledger_disposition": (
                None
                if self.finalized_ledger_disposition is None
                else self.finalized_ledger_disposition.value
            ),
            "trusted_outcome_admission": False,
            "investment_performance_claim": False,
            "auto_trade": False,
        }


@dataclass(frozen=True, slots=True)
class OutcomeCollectionAuditReport:
    audited_at: datetime
    event_hashes: tuple[str, ...]
    case_ids: tuple[str, ...]
    state_counts: tuple[tuple[OutcomeCollectionCaseState, int], ...]
    first_event_hash: str
    last_event_hash: str
    audit_id: str = field(init=False)

    def __post_init__(self) -> None:
        audited = to_utc(ensure_aware(self.audited_at, "audited_at"))
        hashes = tuple(
            _require_sha256(item, "audit event hash") for item in self.event_hashes
        )
        if len(hashes) != len(set(hashes)):
            raise OutcomeCollectionError("audit event hashes must be unique")
        cases = tuple(_require_sha256(item, "audit case ID") for item in self.case_ids)
        if cases != tuple(sorted(set(cases))):
            raise OutcomeCollectionError("audit case IDs must be sorted and unique")
        counts = tuple(sorted(self.state_counts, key=lambda item: item[0].value))
        if counts != self.state_counts:
            raise OutcomeCollectionError("audit state counts must be sorted")
        if any(
            not isinstance(state, OutcomeCollectionCaseState)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            for state, count in counts
        ):
            raise OutcomeCollectionError("audit state counts are invalid")
        if sum(count for _state, count in counts) != len(cases):
            raise OutcomeCollectionError("audit state counts disagree with cases")
        first = _require_sha256(self.first_event_hash, "first_event_hash")
        last = _require_sha256(self.last_event_hash, "last_event_hash")
        expected_first = _ZERO_HASH if not hashes else hashes[0]
        expected_last = _ZERO_HASH if not hashes else hashes[-1]
        if first != expected_first or last != expected_last:
            raise OutcomeCollectionError("audit event boundary hashes are inconsistent")
        object.__setattr__(self, "audited_at", audited)
        object.__setattr__(self, "event_hashes", hashes)
        object.__setattr__(self, "case_ids", cases)
        object.__setattr__(self, "state_counts", counts)
        object.__setattr__(self, "audit_id", fingerprint(self._identity_payload()))

    @property
    def event_count(self) -> int:
        return len(self.event_hashes)

    @property
    def case_count(self) -> int:
        return len(self.case_ids)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": COLLECTION_AUDIT_SCHEMA,
            "audited_at": _datetime_text(self.audited_at),
            "event_hashes": list(self.event_hashes),
            "case_ids": list(self.case_ids),
            "state_counts": [
                {"state": state.value, "count": count}
                for state, count in self.state_counts
            ],
            "first_event_hash": self.first_event_hash,
            "last_event_hash": self.last_event_hash,
            "event_count": self.event_count,
            "case_count": self.case_count,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "audit_id": self.audit_id,
            "integrity_state": "PASSED",
            "trusted_outcome_admission": False,
            "investment_performance_claim": False,
            "production_database_modified": False,
            "auto_promote_model": False,
            "auto_change_strategy_weight": False,
            "auto_trade": False,
        }


@dataclass(frozen=True, slots=True)
class OutcomeCollectionFinalizationResult:
    case: OutcomeCollectionCase
    outcome: SignalOutcome
    ledger_result: OutcomeLedgerAppendResult
    ledger_audit_id: str
    collection_disposition: OutcomeCollectionDisposition

    def __post_init__(self) -> None:
        if self.case.state is not OutcomeCollectionCaseState.FINALIZED:
            raise OutcomeCollectionError("finalization result case is not finalized")
        if not isinstance(self.outcome, SignalOutcome):
            raise OutcomeCollectionError("finalization outcome is invalid")
        if not isinstance(self.ledger_result, OutcomeLedgerAppendResult):
            raise OutcomeCollectionError("finalization ledger result is invalid")
        _require_sha256(self.ledger_audit_id, "ledger_audit_id")
        if not isinstance(self.collection_disposition, OutcomeCollectionDisposition):
            raise OutcomeCollectionError("collection disposition is invalid")


def _payload_document(
    schema: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    identity_document = {"schema": schema, **dict(identity)}
    return {
        **identity_document,
        "fact_id": fingerprint(identity_document),
    }


def _parse_payload(
    event: OutcomeCollectionEvent,
    schema: str,
    fields: set[str],
) -> Mapping[str, Any]:
    payload = event.payload
    _require_fields(payload, {"schema", "fact_id", *fields}, event.event_type.value)
    if payload["schema"] != schema:
        raise OutcomeCollectionError(f"{event.event_type.value} payload schema is invalid")
    identity = {key: value for key, value in payload.items() if key != "fact_id"}
    expected = fingerprint(identity)
    if expected != _require_sha256(payload["fact_id"], "fact_id"):
        raise OutcomeCollectionError(f"{event.event_type.value} fact ID mismatch")
    if event.fact_id != expected:
        raise OutcomeCollectionError(f"{event.event_type.value} event fact ID mismatch")
    return payload


def _open_payload(
    snapshot: RuntimeSignalSnapshot,
    mode: OutcomeCollectionMode,
) -> dict[str, Any]:
    case_id = _case_id(snapshot, mode)
    return _payload_document(
        CASE_OPENED_SCHEMA,
        {
            "case_id": case_id,
            "mode": mode.value,
            "snapshot": snapshot.as_dict(),
        },
    )


def _parse_open_event(
    event: OutcomeCollectionEvent,
) -> tuple[RuntimeSignalSnapshot, OutcomeCollectionMode]:
    payload = _parse_payload(
        event,
        CASE_OPENED_SCHEMA,
        {"case_id", "mode", "snapshot"},
    )
    try:
        mode = OutcomeCollectionMode(_require_text(payload["mode"], "mode"))
    except ValueError as exc:
        raise OutcomeCollectionError("collection mode is invalid") from exc
    snapshot = RuntimeSignalSnapshot.from_dict(payload["snapshot"])
    case_id = _require_sha256(payload["case_id"], "case_id")
    if case_id != _case_id(snapshot, mode) or case_id != event.case_id:
        raise OutcomeCollectionError("opened case identity is invalid")
    if event.observed_at != snapshot.captured_at:
        raise OutcomeCollectionError("case open time differs from snapshot capture time")
    return snapshot, mode


def _build_entry_fill_payload(
    snapshot: RuntimeSignalSnapshot,
    *,
    timestamp: datetime,
    session_index: int,
    quantity: int,
    reference_price: Decimal,
    fill_price: Decimal,
    explicit_cost: Decimal,
    execution_rule_id: str,
    cost_schedule_id: str,
    raw_bar_snapshot_id: str,
    evidence_ids: tuple[str, ...],
) -> tuple[dict[str, Any], OutcomeFillEvidence]:
    try:
        fill = OutcomeFillEvidence(
            intent_id=snapshot.entry_intent.intent_id,
            symbol=snapshot.symbol,
            market=snapshot.market,
            side=TradeSide.BUY,
            timestamp=to_utc(ensure_aware(timestamp, "entry fill timestamp")),
            session_index=_require_int(session_index, "session_index"),
            quantity=_require_int(quantity, "quantity", minimum=1),
            reference_price=_require_decimal(
                reference_price,
                "reference_price",
                positive=True,
            ),
            fill_price=_require_decimal(fill_price, "fill_price", positive=True),
            explicit_cost=_require_decimal(
                explicit_cost,
                "explicit_cost",
                nonnegative=True,
            ),
            execution_rule_id=_require_sha256(
                execution_rule_id,
                "execution_rule_id",
            ),
            cost_schedule_id=_require_sha256(
                cost_schedule_id,
                "cost_schedule_id",
            ),
            raw_bar_snapshot_id=_require_sha256(
                raw_bar_snapshot_id,
                "raw_bar_snapshot_id",
            ),
        )
    except OutcomeContractError as exc:
        raise OutcomeCollectionError("entry fill contract is invalid") from exc
    payload = _payload_document(
        ENTRY_FILL_SCHEMA,
        {
            "timestamp": _datetime_text(fill.timestamp),
            "session_index": fill.session_index,
            "quantity": fill.quantity,
            "reference_price": _decimal_text(fill.reference_price),
            "fill_price": _decimal_text(fill.fill_price),
            "explicit_cost": _decimal_text(fill.explicit_cost),
            "execution_rule_id": fill.execution_rule_id,
            "cost_schedule_id": fill.cost_schedule_id,
            "raw_bar_snapshot_id": fill.raw_bar_snapshot_id,
            "evidence_ids": list(evidence_ids),
            "fill_id": fill.fill_id,
        },
    )
    return payload, fill


def _parse_entry_fill(
    event: OutcomeCollectionEvent,
    snapshot: RuntimeSignalSnapshot,
) -> tuple[OutcomeFillEvidence, tuple[str, ...]]:
    payload = _parse_payload(
        event,
        ENTRY_FILL_SCHEMA,
        {
            "timestamp",
            "session_index",
            "quantity",
            "reference_price",
            "fill_price",
            "explicit_cost",
            "execution_rule_id",
            "cost_schedule_id",
            "raw_bar_snapshot_id",
            "evidence_ids",
            "fill_id",
        },
    )
    evidence = payload["evidence_ids"]
    if not isinstance(evidence, list):
        raise OutcomeCollectionError("entry fill evidence_ids must be an array")
    evidence_ids = _normalize_evidence_ids(evidence)
    rebuilt_payload, fill = _build_entry_fill_payload(
        snapshot,
        timestamp=_datetime_from_text(payload["timestamp"], "entry fill timestamp"),
        session_index=_require_int(payload["session_index"], "session_index"),
        quantity=_require_int(payload["quantity"], "quantity", minimum=1),
        reference_price=_decimal_from_text(
            payload["reference_price"],
            "reference_price",
            positive=True,
        ),
        fill_price=_decimal_from_text(
            payload["fill_price"],
            "fill_price",
            positive=True,
        ),
        explicit_cost=_decimal_from_text(
            payload["explicit_cost"],
            "explicit_cost",
            nonnegative=True,
        ),
        execution_rule_id=_require_sha256(
            payload["execution_rule_id"],
            "execution_rule_id",
        ),
        cost_schedule_id=_require_sha256(
            payload["cost_schedule_id"],
            "cost_schedule_id",
        ),
        raw_bar_snapshot_id=_require_sha256(
            payload["raw_bar_snapshot_id"],
            "raw_bar_snapshot_id",
        ),
        evidence_ids=evidence_ids,
    )
    if fill.fill_id != _require_sha256(payload["fill_id"], "fill_id"):
        raise OutcomeCollectionError("entry fill ID mismatch")
    if rebuilt_payload != dict(payload):
        raise OutcomeCollectionError("entry fill payload is not canonical")
    return fill, evidence_ids


def _build_path_payload(
    *,
    timestamp: datetime,
    session_index: int,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    observable: bool,
    raw_bar_snapshot_id: str,
    evidence_ids: tuple[str, ...],
) -> tuple[dict[str, Any], OutcomePathPoint]:
    try:
        point = OutcomePathPoint(
            timestamp=to_utc(ensure_aware(timestamp, "path timestamp")),
            session_index=_require_int(session_index, "session_index"),
            high=_require_decimal(high, "high", positive=True),
            low=_require_decimal(low, "low", positive=True),
            close=_require_decimal(close, "close", positive=True),
            observable=_require_bool(observable, "observable"),
        )
    except OutcomeContractError as exc:
        raise OutcomeCollectionError("path point contract is invalid") from exc
    payload = _payload_document(
        PATH_POINT_SCHEMA,
        {
            "timestamp": _datetime_text(point.timestamp),
            "session_index": point.session_index,
            "high": _decimal_text(point.high),
            "low": _decimal_text(point.low),
            "close": _decimal_text(point.close),
            "observable": point.observable,
            "raw_bar_snapshot_id": _require_sha256(
                raw_bar_snapshot_id,
                "raw_bar_snapshot_id",
            ),
            "evidence_ids": list(evidence_ids),
            "point_id": point.point_id,
        },
    )
    return payload, point


def _parse_path_point(
    event: OutcomeCollectionEvent,
) -> tuple[OutcomePathPoint, tuple[str, ...]]:
    payload = _parse_payload(
        event,
        PATH_POINT_SCHEMA,
        {
            "timestamp",
            "session_index",
            "high",
            "low",
            "close",
            "observable",
            "raw_bar_snapshot_id",
            "evidence_ids",
            "point_id",
        },
    )
    evidence = payload["evidence_ids"]
    if not isinstance(evidence, list):
        raise OutcomeCollectionError("path evidence_ids must be an array")
    evidence_ids = _normalize_evidence_ids(evidence)
    rebuilt_payload, point = _build_path_payload(
        timestamp=_datetime_from_text(payload["timestamp"], "path timestamp"),
        session_index=_require_int(payload["session_index"], "session_index"),
        high=_decimal_from_text(payload["high"], "high", positive=True),
        low=_decimal_from_text(payload["low"], "low", positive=True),
        close=_decimal_from_text(payload["close"], "close", positive=True),
        observable=_require_bool(payload["observable"], "observable"),
        raw_bar_snapshot_id=_require_sha256(
            payload["raw_bar_snapshot_id"],
            "raw_bar_snapshot_id",
        ),
        evidence_ids=evidence_ids,
    )
    if point.point_id != _require_sha256(payload["point_id"], "point_id"):
        raise OutcomeCollectionError("path point ID mismatch")
    if rebuilt_payload != dict(payload):
        raise OutcomeCollectionError("path point payload is not canonical")
    return point, evidence_ids


def _build_exit_request_payload(
    case_id: str,
    snapshot: RuntimeSignalSnapshot,
    *,
    requested_at: datetime,
    quantity: int,
    terminal_reason: OutcomeTerminalReason,
    execution_policy_id: str,
    reason: str,
    evidence_ids: tuple[str, ...],
) -> tuple[dict[str, Any], TradeIntentEvidence]:
    if terminal_reason not in _COMPLETE_TERMINAL_REASONS:
        raise OutcomeCollectionError("exit terminal_reason is invalid")
    requested = to_utc(ensure_aware(requested_at, "exit requested_at"))
    quantity = _require_int(quantity, "quantity", minimum=1)
    execution_policy = _require_sha256(
        execution_policy_id,
        "execution_policy_id",
    )
    reason = _require_text(reason, "exit reason")
    decision_identity = {
        "schema": "stage4g-exit-decision-snapshot-v1",
        "case_id": _require_sha256(case_id, "case_id"),
        "requested_at": _datetime_text(requested),
        "quantity": quantity,
        "terminal_reason": terminal_reason.value,
        "execution_policy_id": execution_policy,
        "reason": reason,
        "evidence_ids": list(evidence_ids),
    }
    decision_snapshot_id = fingerprint(decision_identity)
    try:
        intent = TradeIntentEvidence(
            symbol=snapshot.symbol,
            market=snapshot.market,
            side=TradeSide.SELL,
            requested_at=requested,
            requested_quantity=quantity,
            decision_snapshot_id=decision_snapshot_id,
            execution_policy_id=execution_policy,
        )
    except OutcomeContractError as exc:
        raise OutcomeCollectionError("exit intent contract is invalid") from exc
    payload = _payload_document(
        EXIT_REQUEST_SCHEMA,
        {
            "requested_at": _datetime_text(requested),
            "quantity": quantity,
            "terminal_reason": terminal_reason.value,
            "execution_policy_id": execution_policy,
            "reason": reason,
            "evidence_ids": list(evidence_ids),
            "decision_snapshot_id": decision_snapshot_id,
            "intent_id": intent.intent_id,
        },
    )
    return payload, intent


def _parse_exit_request(
    event: OutcomeCollectionEvent,
    snapshot: RuntimeSignalSnapshot,
) -> tuple[TradeIntentEvidence, OutcomeTerminalReason, tuple[str, ...]]:
    payload = _parse_payload(
        event,
        EXIT_REQUEST_SCHEMA,
        {
            "requested_at",
            "quantity",
            "terminal_reason",
            "execution_policy_id",
            "reason",
            "evidence_ids",
            "decision_snapshot_id",
            "intent_id",
        },
    )
    try:
        terminal = OutcomeTerminalReason(
            _require_text(payload["terminal_reason"], "terminal_reason")
        )
    except ValueError as exc:
        raise OutcomeCollectionError("exit terminal_reason is invalid") from exc
    evidence = payload["evidence_ids"]
    if not isinstance(evidence, list):
        raise OutcomeCollectionError("exit request evidence_ids must be an array")
    evidence_ids = _normalize_evidence_ids(evidence)
    rebuilt_payload, intent = _build_exit_request_payload(
        event.case_id,
        snapshot,
        requested_at=_datetime_from_text(payload["requested_at"], "requested_at"),
        quantity=_require_int(payload["quantity"], "quantity", minimum=1),
        terminal_reason=terminal,
        execution_policy_id=_require_sha256(
            payload["execution_policy_id"],
            "execution_policy_id",
        ),
        reason=_require_text(payload["reason"], "reason"),
        evidence_ids=evidence_ids,
    )
    if intent.decision_snapshot_id != _require_sha256(
        payload["decision_snapshot_id"],
        "decision_snapshot_id",
    ) or intent.intent_id != _require_sha256(payload["intent_id"], "intent_id"):
        raise OutcomeCollectionError("exit request identity mismatch")
    if rebuilt_payload != dict(payload):
        raise OutcomeCollectionError("exit request payload is not canonical")
    return intent, terminal, evidence_ids


def _build_exit_fill_payload(
    snapshot: RuntimeSignalSnapshot,
    exit_intent: TradeIntentEvidence,
    *,
    timestamp: datetime,
    session_index: int,
    quantity: int,
    reference_price: Decimal,
    fill_price: Decimal,
    explicit_cost: Decimal,
    execution_rule_id: str,
    cost_schedule_id: str,
    raw_bar_snapshot_id: str,
    evidence_ids: tuple[str, ...],
) -> tuple[dict[str, Any], OutcomeFillEvidence]:
    try:
        fill = OutcomeFillEvidence(
            intent_id=exit_intent.intent_id,
            symbol=snapshot.symbol,
            market=snapshot.market,
            side=TradeSide.SELL,
            timestamp=to_utc(ensure_aware(timestamp, "exit fill timestamp")),
            session_index=_require_int(session_index, "session_index"),
            quantity=_require_int(quantity, "quantity", minimum=1),
            reference_price=_require_decimal(
                reference_price,
                "reference_price",
                positive=True,
            ),
            fill_price=_require_decimal(fill_price, "fill_price", positive=True),
            explicit_cost=_require_decimal(
                explicit_cost,
                "explicit_cost",
                nonnegative=True,
            ),
            execution_rule_id=_require_sha256(
                execution_rule_id,
                "execution_rule_id",
            ),
            cost_schedule_id=_require_sha256(
                cost_schedule_id,
                "cost_schedule_id",
            ),
            raw_bar_snapshot_id=_require_sha256(
                raw_bar_snapshot_id,
                "raw_bar_snapshot_id",
            ),
        )
    except OutcomeContractError as exc:
        raise OutcomeCollectionError("exit fill contract is invalid") from exc
    payload = _payload_document(
        EXIT_FILL_SCHEMA,
        {
            "timestamp": _datetime_text(fill.timestamp),
            "session_index": fill.session_index,
            "quantity": fill.quantity,
            "reference_price": _decimal_text(fill.reference_price),
            "fill_price": _decimal_text(fill.fill_price),
            "explicit_cost": _decimal_text(fill.explicit_cost),
            "execution_rule_id": fill.execution_rule_id,
            "cost_schedule_id": fill.cost_schedule_id,
            "raw_bar_snapshot_id": fill.raw_bar_snapshot_id,
            "evidence_ids": list(evidence_ids),
            "fill_id": fill.fill_id,
        },
    )
    return payload, fill


def _parse_exit_fill(
    event: OutcomeCollectionEvent,
    snapshot: RuntimeSignalSnapshot,
    exit_intent: TradeIntentEvidence,
) -> tuple[OutcomeFillEvidence, tuple[str, ...]]:
    payload = _parse_payload(
        event,
        EXIT_FILL_SCHEMA,
        {
            "timestamp",
            "session_index",
            "quantity",
            "reference_price",
            "fill_price",
            "explicit_cost",
            "execution_rule_id",
            "cost_schedule_id",
            "raw_bar_snapshot_id",
            "evidence_ids",
            "fill_id",
        },
    )
    evidence = payload["evidence_ids"]
    if not isinstance(evidence, list):
        raise OutcomeCollectionError("exit fill evidence_ids must be an array")
    evidence_ids = _normalize_evidence_ids(evidence)
    rebuilt_payload, fill = _build_exit_fill_payload(
        snapshot,
        exit_intent,
        timestamp=_datetime_from_text(payload["timestamp"], "exit fill timestamp"),
        session_index=_require_int(payload["session_index"], "session_index"),
        quantity=_require_int(payload["quantity"], "quantity", minimum=1),
        reference_price=_decimal_from_text(
            payload["reference_price"],
            "reference_price",
            positive=True,
        ),
        fill_price=_decimal_from_text(
            payload["fill_price"],
            "fill_price",
            positive=True,
        ),
        explicit_cost=_decimal_from_text(
            payload["explicit_cost"],
            "explicit_cost",
            nonnegative=True,
        ),
        execution_rule_id=_require_sha256(
            payload["execution_rule_id"],
            "execution_rule_id",
        ),
        cost_schedule_id=_require_sha256(
            payload["cost_schedule_id"],
            "cost_schedule_id",
        ),
        raw_bar_snapshot_id=_require_sha256(
            payload["raw_bar_snapshot_id"],
            "raw_bar_snapshot_id",
        ),
        evidence_ids=evidence_ids,
    )
    if fill.fill_id != _require_sha256(payload["fill_id"], "fill_id"):
        raise OutcomeCollectionError("exit fill ID mismatch")
    if rebuilt_payload != dict(payload):
        raise OutcomeCollectionError("exit fill payload is not canonical")
    return fill, evidence_ids


def _build_no_entry_payload(
    *,
    fact_at: datetime,
    terminal_reason: OutcomeTerminalReason,
    reason: str,
    evidence_ids: tuple[str, ...],
) -> dict[str, Any]:
    if terminal_reason not in _NO_ENTRY_TERMINAL_REASONS:
        raise OutcomeCollectionError("no-entry terminal_reason is invalid")
    return _payload_document(
        NO_ENTRY_SCHEMA,
        {
            "fact_at": _datetime_text(
                to_utc(ensure_aware(fact_at, "no-entry fact_at"))
            ),
            "terminal_reason": terminal_reason.value,
            "reason": _require_text(reason, "no-entry reason"),
            "evidence_ids": list(evidence_ids),
        },
    )


def _parse_no_entry(
    event: OutcomeCollectionEvent,
) -> tuple[datetime, OutcomeTerminalReason, tuple[str, ...]]:
    payload = _parse_payload(
        event,
        NO_ENTRY_SCHEMA,
        {"fact_at", "terminal_reason", "reason", "evidence_ids"},
    )
    try:
        terminal = OutcomeTerminalReason(
            _require_text(payload["terminal_reason"], "terminal_reason")
        )
    except ValueError as exc:
        raise OutcomeCollectionError("no-entry terminal_reason is invalid") from exc
    evidence = payload["evidence_ids"]
    if not isinstance(evidence, list):
        raise OutcomeCollectionError("no-entry evidence_ids must be an array")
    evidence_ids = _normalize_evidence_ids(evidence)
    fact_at = _datetime_from_text(payload["fact_at"], "fact_at")
    rebuilt = _build_no_entry_payload(
        fact_at=fact_at,
        terminal_reason=terminal,
        reason=_require_text(payload["reason"], "reason"),
        evidence_ids=evidence_ids,
    )
    if rebuilt != dict(payload):
        raise OutcomeCollectionError("no-entry payload is not canonical")
    return fact_at, terminal, evidence_ids


def _build_signal_outcome(
    *,
    snapshot: RuntimeSignalSnapshot,
    mode: OutcomeCollectionMode,
    entry_fill: OutcomeFillEvidence | None,
    path: tuple[OutcomePathPoint, ...],
    exit_intent: TradeIntentEvidence | None,
    exit_fill: OutcomeFillEvidence | None,
    terminal_reason: OutcomeTerminalReason,
    recorded_at: datetime,
) -> SignalOutcome:
    origin = (
        OutcomeEvidenceOrigin.PAPER_RECORDED
        if mode is OutcomeCollectionMode.PAPER
        else OutcomeEvidenceOrigin.LIVE_OBSERVED
    )
    try:
        return SignalOutcome(
            signal_id=_outcome_signal_id(snapshot, mode),
            strategy_id=snapshot.strategy_id,
            strategy_version=snapshot.strategy_version,
            horizon_sessions=snapshot.horizon_sessions,
            model_id=snapshot.model_id,
            evidence_tier=DataTrustTier.BEST_EFFORT,
            symbol=snapshot.symbol,
            market=snapshot.market,
            instrument_id=snapshot.instrument_id,
            identity_fact_id=snapshot.identity_fact_id,
            decision_snapshot_id=snapshot.decision_snapshot_id,
            data_snapshot_id=snapshot.data_snapshot_id,
            policy_id=snapshot.policy_id,
            market_regime=snapshot.market_regime,
            classification_id=snapshot.classification_id,
            recorded_at=to_utc(ensure_aware(recorded_at, "recorded_at")),
            entry_intent=snapshot.entry_intent,
            entry_fill=entry_fill,
            exit_intent=exit_intent,
            exit_fill=exit_fill,
            path=path,
            path_complete=entry_fill is not None,
            invalidation_price=(
                None if entry_fill is None else snapshot.invalidation_price
            ),
            terminal_reason=terminal_reason,
            origin=origin,
            verified=False,
            synthetic_fixture_only=False,
            verification_evidence_ids=(),
        )
    except OutcomeContractError as exc:
        raise OutcomeCollectionError(
            "collected facts cannot form a terminal SignalOutcome"
        ) from exc


def _build_prepared_payload(
    outcome: SignalOutcome,
    ledger_target_id: str,
) -> dict[str, Any]:
    return _payload_document(
        FINALIZATION_PREPARED_SCHEMA,
        {
            "ledger_target_id": _require_sha256(
                ledger_target_id,
                "ledger_target_id",
            ),
            "outcome_id": outcome.outcome_id,
            "outcome": signal_outcome_to_dict(outcome),
        },
    )


def _parse_prepared(
    event: OutcomeCollectionEvent,
) -> tuple[SignalOutcome, str]:
    payload = _parse_payload(
        event,
        FINALIZATION_PREPARED_SCHEMA,
        {"ledger_target_id", "outcome_id", "outcome"},
    )
    try:
        outcome = signal_outcome_from_dict(payload["outcome"])
    except OutcomeLedgerError as exc:
        raise OutcomeCollectionError("prepared outcome is invalid") from exc
    target = _require_sha256(payload["ledger_target_id"], "ledger_target_id")
    if outcome.outcome_id != _require_sha256(payload["outcome_id"], "outcome_id"):
        raise OutcomeCollectionError("prepared outcome ID mismatch")
    if _build_prepared_payload(outcome, target) != dict(payload):
        raise OutcomeCollectionError("prepared outcome payload is not canonical")
    return outcome, target


def _build_finalized_payload(
    *,
    ledger_target_id: str,
    outcome_id: str,
    record_hash: str,
    record_append_order: int,
    ledger_audit_id: str,
    ledger_disposition: OutcomeLedgerDisposition,
) -> dict[str, Any]:
    if not isinstance(ledger_disposition, OutcomeLedgerDisposition):
        raise OutcomeCollectionError("ledger disposition is invalid")
    return _payload_document(
        FINALIZED_SCHEMA,
        {
            "ledger_target_id": _require_sha256(
                ledger_target_id,
                "ledger_target_id",
            ),
            "outcome_id": _require_sha256(outcome_id, "outcome_id"),
            "record_hash": _require_sha256(record_hash, "record_hash"),
            "record_append_order": _require_int(
                record_append_order,
                "record_append_order",
                minimum=1,
            ),
            "ledger_audit_id": _require_sha256(
                ledger_audit_id,
                "ledger_audit_id",
            ),
            "ledger_disposition": ledger_disposition.value,
        },
    )


def _parse_finalized(
    event: OutcomeCollectionEvent,
) -> tuple[str, str, int, str, OutcomeLedgerDisposition]:
    payload = _parse_payload(
        event,
        FINALIZED_SCHEMA,
        {
            "ledger_target_id",
            "outcome_id",
            "record_hash",
            "record_append_order",
            "ledger_audit_id",
            "ledger_disposition",
        },
    )
    try:
        disposition = OutcomeLedgerDisposition(
            _require_text(payload["ledger_disposition"], "ledger_disposition")
        )
    except ValueError as exc:
        raise OutcomeCollectionError("ledger disposition is invalid") from exc
    rebuilt = _build_finalized_payload(
        ledger_target_id=_require_sha256(
            payload["ledger_target_id"],
            "ledger_target_id",
        ),
        outcome_id=_require_sha256(payload["outcome_id"], "outcome_id"),
        record_hash=_require_sha256(payload["record_hash"], "record_hash"),
        record_append_order=_require_int(
            payload["record_append_order"],
            "record_append_order",
            minimum=1,
        ),
        ledger_audit_id=_require_sha256(
            payload["ledger_audit_id"],
            "ledger_audit_id",
        ),
        ledger_disposition=disposition,
    )
    if rebuilt != dict(payload):
        raise OutcomeCollectionError("finalized payload is not canonical")
    return (
        str(payload["ledger_target_id"]),
        str(payload["record_hash"]),
        int(payload["record_append_order"]),
        str(payload["ledger_audit_id"]),
        disposition,
    )


def _require_live_evidence(
    mode: OutcomeCollectionMode,
    evidence_ids: tuple[str, ...],
    name: str,
) -> None:
    if mode is OutcomeCollectionMode.LIVE_MANUAL and not evidence_ids:
        raise OutcomeCollectionError(f"live manual {name} requires evidence IDs")


def _project_case(events: tuple[OutcomeCollectionEvent, ...]) -> OutcomeCollectionCase:
    if not events or events[0].event_type is not OutcomeCollectionEventType.CASE_OPENED:
        raise OutcomeCollectionError("outcome case must start with CASE_OPENED")
    snapshot, mode = _parse_open_event(events[0])
    case_id = events[0].case_id
    if any(event.case_id != case_id for event in events):
        raise OutcomeCollectionError("outcome case contains a foreign event")

    entry_fill: OutcomeFillEvidence | None = None
    path: list[OutcomePathPoint] = []
    exit_intent: TradeIntentEvidence | None = None
    exit_fill: OutcomeFillEvidence | None = None
    terminal_reason: OutcomeTerminalReason | None = None
    no_entry = False
    prepared_outcome: SignalOutcome | None = None
    ledger_target_id: str | None = None
    finalized_record_hash: str | None = None
    finalized_record_append_order: int | None = None
    finalized_ledger_audit_id: str | None = None
    finalized_ledger_disposition: OutcomeLedgerDisposition | None = None
    prepared_seen = False
    finalized_seen = False

    for event in events[1:]:
        if finalized_seen:
            raise OutcomeCollectionError("events cannot follow FINALIZED")
        if prepared_seen and event.event_type is not OutcomeCollectionEventType.FINALIZED:
            raise OutcomeCollectionError(
                "only FINALIZED may follow FINALIZATION_PREPARED"
            )

        if event.event_type is OutcomeCollectionEventType.CASE_OPENED:
            raise OutcomeCollectionError("outcome case cannot be opened twice")
        if event.event_type is OutcomeCollectionEventType.ENTRY_FILLED:
            if entry_fill is not None or no_entry:
                raise OutcomeCollectionError("entry disposition is already recorded")
            fill, evidence_ids = _parse_entry_fill(event, snapshot)
            if snapshot.runtime_signal_state not in _ENTRY_FILL_STATES:
                raise OutcomeCollectionError(
                    "runtime signal state does not permit an entry fill"
                )
            if mode is OutcomeCollectionMode.LIVE_MANUAL:
                if snapshot.runtime_data_status is not DataStatus.LIVE:
                    raise OutcomeCollectionError(
                        "live manual entry requires LIVE runtime data"
                    )
            elif snapshot.runtime_data_status not in {
                DataStatus.LIVE,
                DataStatus.DELAYED,
            }:
                raise OutcomeCollectionError(
                    "paper entry requires LIVE or DELAYED runtime data"
                )
            _require_live_evidence(mode, evidence_ids, "entry fill")
            if fill.timestamp < snapshot.captured_at or fill.timestamp > event.observed_at:
                raise OutcomeCollectionError("entry fill timestamp is outside evidence time")
            if fill.quantity > snapshot.requested_quantity:
                raise OutcomeCollectionError("entry fill exceeds requested quantity")
            entry_fill = fill
            continue
        if event.event_type is OutcomeCollectionEventType.NO_ENTRY:
            if entry_fill is not None or no_entry or path or exit_intent is not None:
                raise OutcomeCollectionError("no-entry conflicts with collected trade facts")
            fact_at, reason, evidence_ids = _parse_no_entry(event)
            _require_live_evidence(mode, evidence_ids, "no-entry fact")
            if fact_at < snapshot.captured_at or fact_at > event.observed_at:
                raise OutcomeCollectionError("no-entry timestamp is outside evidence time")
            no_entry = True
            terminal_reason = reason
            continue
        if event.event_type is OutcomeCollectionEventType.PATH_POINT:
            if entry_fill is None or no_entry or exit_fill is not None:
                raise OutcomeCollectionError("path point is outside an open position")
            point, evidence_ids = _parse_path_point(event)
            _require_live_evidence(mode, evidence_ids, "path point")
            if point.timestamp < entry_fill.timestamp or point.timestamp > event.observed_at:
                raise OutcomeCollectionError("path timestamp is outside evidence time")
            if path and point.timestamp <= path[-1].timestamp:
                raise OutcomeCollectionError("path timestamps must be appended in order")
            if path and point.session_index < path[-1].session_index:
                raise OutcomeCollectionError("path session indices must be monotonic")
            if point.session_index < entry_fill.session_index:
                raise OutcomeCollectionError("path point precedes entry session")
            path.append(point)
            continue
        if event.event_type is OutcomeCollectionEventType.EXIT_REQUESTED:
            if entry_fill is None or no_entry or exit_intent is not None:
                raise OutcomeCollectionError("exit request is not valid in this case state")
            intent, reason, evidence_ids = _parse_exit_request(event, snapshot)
            _require_live_evidence(mode, evidence_ids, "exit request")
            if intent.requested_at < entry_fill.timestamp or intent.requested_at > event.observed_at:
                raise OutcomeCollectionError("exit request timestamp is outside evidence time")
            if intent.requested_quantity != entry_fill.quantity:
                raise OutcomeCollectionError("exit request must close the filled quantity")
            exit_intent = intent
            terminal_reason = reason
            continue
        if event.event_type is OutcomeCollectionEventType.EXIT_FILLED:
            if exit_intent is None or entry_fill is None or exit_fill is not None:
                raise OutcomeCollectionError("exit fill requires one open exit request")
            fill, evidence_ids = _parse_exit_fill(event, snapshot, exit_intent)
            _require_live_evidence(mode, evidence_ids, "exit fill")
            if fill.timestamp < exit_intent.requested_at or fill.timestamp > event.observed_at:
                raise OutcomeCollectionError("exit fill timestamp is outside evidence time")
            if fill.quantity != entry_fill.quantity:
                raise OutcomeCollectionError("exit fill must close the filled quantity")
            if fill.session_index < entry_fill.session_index:
                raise OutcomeCollectionError("exit fill precedes entry session")
            if path and path[-1].timestamp > fill.timestamp:
                raise OutcomeCollectionError("collected path extends after exit fill")
            exit_fill = fill
            continue
        if event.event_type is OutcomeCollectionEventType.FINALIZATION_PREPARED:
            if prepared_seen:
                raise OutcomeCollectionError("finalization was prepared twice")
            if no_entry:
                if terminal_reason not in _NO_ENTRY_TERMINAL_REASONS:
                    raise OutcomeCollectionError("no-entry terminal reason is missing")
            elif (
                entry_fill is None
                or exit_intent is None
                or exit_fill is None
                or terminal_reason not in _COMPLETE_TERMINAL_REASONS
                or not path
                or not any(point.observable for point in path)
            ):
                raise OutcomeCollectionError(
                    "complete finalization requires entry, path, exit request and exit fill"
                )
            outcome, target = _parse_prepared(event)
            expected = _build_signal_outcome(
                snapshot=snapshot,
                mode=mode,
                entry_fill=entry_fill,
                path=tuple(path),
                exit_intent=exit_intent,
                exit_fill=exit_fill,
                terminal_reason=terminal_reason,
                recorded_at=outcome.recorded_at,
            )
            if expected != outcome:
                raise OutcomeCollectionError(
                    "prepared outcome disagrees with collected facts"
                )
            if outcome.recorded_at != event.observed_at:
                raise OutcomeCollectionError(
                    "prepared outcome recorded_at must equal preparation observation time"
                )
            prepared_outcome = outcome
            ledger_target_id = target
            prepared_seen = True
            continue
        if event.event_type is OutcomeCollectionEventType.FINALIZED:
            if not prepared_seen or prepared_outcome is None or ledger_target_id is None:
                raise OutcomeCollectionError("FINALIZED requires prepared outcome")
            target, record_hash, append_order, audit_id, disposition = _parse_finalized(
                event
            )
            if target != ledger_target_id:
                raise OutcomeCollectionError("finalized ledger target changed")
            payload = event.payload
            if payload["outcome_id"] != prepared_outcome.outcome_id:
                raise OutcomeCollectionError("finalized outcome ID changed")
            finalized_record_hash = record_hash
            finalized_record_append_order = append_order
            finalized_ledger_audit_id = audit_id
            finalized_ledger_disposition = disposition
            finalized_seen = True
            continue
        raise OutcomeCollectionError("unsupported outcome collection event")

    if finalized_seen:
        state = OutcomeCollectionCaseState.FINALIZED
    elif prepared_seen:
        state = OutcomeCollectionCaseState.FINALIZATION_PREPARED
    elif no_entry or exit_fill is not None:
        state = OutcomeCollectionCaseState.TERMINAL_READY
    elif exit_intent is not None:
        state = OutcomeCollectionCaseState.EXIT_REQUESTED
    elif entry_fill is not None:
        state = OutcomeCollectionCaseState.OPEN_POSITION
    else:
        state = OutcomeCollectionCaseState.AWAITING_ENTRY

    return OutcomeCollectionCase(
        case_id=case_id,
        mode=mode,
        snapshot=snapshot,
        event_hashes=tuple(event.event_hash for event in events),
        entry_fill=entry_fill,
        path=tuple(path),
        exit_intent=exit_intent,
        exit_fill=exit_fill,
        terminal_reason=terminal_reason,
        prepared_outcome=prepared_outcome,
        ledger_target_id=ledger_target_id,
        finalized_record_hash=finalized_record_hash,
        finalized_record_append_order=finalized_record_append_order,
        finalized_ledger_audit_id=finalized_ledger_audit_id,
        finalized_ledger_disposition=finalized_ledger_disposition,
        state=state,
    )


def _validate_events(
    events: tuple[OutcomeCollectionEvent, ...],
    cutoff: datetime,
) -> tuple[OutcomeCollectionCase, ...]:
    expected_previous = _ZERO_HASH
    previous_observed: datetime | None = None
    seen_hashes: set[str] = set()
    seen_facts: set[str] = set()
    by_case: dict[str, list[OutcomeCollectionEvent]] = defaultdict(list)
    for expected_order, event in enumerate(events, start=1):
        if event.append_order != expected_order:
            raise OutcomeCollectionError("collection append order is not contiguous")
        if event.previous_event_hash != expected_previous:
            raise OutcomeCollectionError("collection event hash chain is broken")
        if event.event_hash in seen_hashes or event.fact_id in seen_facts:
            raise OutcomeCollectionError("collection event/fact identity is duplicated")
        if event.observed_at > cutoff:
            raise OutcomeCollectionError(
                "collection event was observed after the audit timestamp"
            )
        if previous_observed is not None and event.observed_at < previous_observed:
            raise OutcomeCollectionError(
                "collection observation time is not monotonic"
            )
        expected_previous = event.event_hash
        previous_observed = event.observed_at
        seen_hashes.add(event.event_hash)
        seen_facts.add(event.fact_id)
        by_case[event.case_id].append(event)

    cases = tuple(
        _project_case(tuple(case_events))
        for _case_id_value, case_events in sorted(by_case.items())
    )
    outcome_signal_ids = tuple(
        _outcome_signal_id(case.snapshot, case.mode) for case in cases
    )
    if len(outcome_signal_ids) != len(set(outcome_signal_ids)):
        raise OutcomeCollectionError("one runtime episode has multiple collection cases")
    return cases


def _catalog_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _unique_index_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(row[2]) != 1:
            continue
        index_name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in connection.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
        )
        result.add(columns)
    return result


def _validate_collection_schema(connection: sqlite3.Connection) -> None:
    quick_check = tuple(
        str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
    )
    if quick_check != ("ok",):
        raise OutcomeCollectionError("collection SQLite integrity check failed")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if tables != {"outcome_collection_meta", "outcome_collection_events"}:
        raise OutcomeCollectionError("collection database table set is invalid")
    if _catalog_columns(connection, "outcome_collection_meta") != (
        _COLLECTION_META_COLUMNS
    ):
        raise OutcomeCollectionError("collection metadata schema is invalid")
    if _catalog_columns(connection, "outcome_collection_events") != (
        _COLLECTION_EVENT_COLUMNS
    ):
        raise OutcomeCollectionError("collection event schema is invalid")
    metadata = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT key,value FROM outcome_collection_meta ORDER BY key"
        ).fetchall()
    )
    if metadata != (("schema", COLLECTION_SCHEMA),):
        raise OutcomeCollectionError("collection schema identity is invalid")
    required_unique = {("event_hash",), ("fact_id",)}
    if not required_unique.issubset(
        _unique_index_columns(connection, "outcome_collection_events")
    ):
        raise OutcomeCollectionError("collection uniqueness constraints are invalid")
    query_index = tuple(
        str(item[2])
        for item in connection.execute(
            "PRAGMA index_info('idx_outcome_collection_case')"
        ).fetchall()
    )
    if query_index != ("case_id", "append_order"):
        raise OutcomeCollectionError("collection case index is invalid")


class OutcomeCollectionService:
    """Collect runtime outcome facts and finalize them into Stage 4F."""

    def __init__(
        self,
        database_path: str | Path = DEFAULT_COLLECTION_DATABASE,
        *,
        production_database: str | Path | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[3]
        database = _checked_path(Path(database_path), "outcome collection database")
        production = _checked_path(
            Path(
                production_database
                if production_database is not None
                else project_root / "data" / "stock_tracker.db"
            ),
            "production database",
        )
        if database == production or _same_existing_file(
            database,
            production,
            "collection/production database",
        ):
            raise OutcomeCollectionError(
                "outcome collection cannot use the production database"
            )
        if database.exists() and not database.is_file():
            raise OutcomeCollectionError("outcome collection database must be a file")
        database.parent.mkdir(parents=True, exist_ok=True)
        if _is_link(database.parent):
            raise OutcomeCollectionError(
                "outcome collection database parent cannot be a link"
            )
        self.database_path = database
        self.production_database = production
        self._lock = threading.RLock()
        self._initialize()
        self._database_identity = _path_identity(
            self.database_path,
            "outcome collection database",
        )

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE outcome_collection_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE outcome_collection_events (
            append_order INTEGER PRIMARY KEY CHECK(append_order > 0),
            event_hash TEXT NOT NULL UNIQUE,
            previous_event_hash TEXT NOT NULL,
            case_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'CASE_OPENED','ENTRY_FILLED','PATH_POINT','EXIT_REQUESTED',
                'EXIT_FILLED','NO_ENTRY','FINALIZATION_PREPARED','FINALIZED'
            )),
            fact_id TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            recorded_by TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL
        );
        CREATE INDEX idx_outcome_collection_case
            ON outcome_collection_events(case_id, append_order);
        """
        with closing(sqlite3.connect(self.database_path, timeout=30.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if not tables:
                    connection.executescript(schema)
                    connection.execute(
                        "INSERT INTO outcome_collection_meta(key,value) "
                        "VALUES('schema',?)",
                        (COLLECTION_SCHEMA,),
                    )
                    connection.commit()
                else:
                    _validate_collection_schema(connection)
                    connection.rollback()
            except Exception:
                connection.rollback()
                raise
        with closing(sqlite3.connect(self.database_path, timeout=30.0)) as connection:
            connection.row_factory = sqlite3.Row
            _validate_collection_schema(connection)

    def _assert_database_identity(self) -> None:
        checked = _checked_path(
            self.database_path,
            "outcome collection database",
        )
        if checked != self.database_path:
            raise OutcomeCollectionError("collection database path identity changed")
        if not self.database_path.is_file() or _is_link(self.database_path):
            raise OutcomeCollectionError(
                "collection database must remain a regular non-link file"
            )
        if _path_identity(
            self.database_path,
            "outcome collection database",
        ) != self._database_identity:
            raise OutcomeCollectionError(
                "collection database was replaced after opening"
            )
        if _same_existing_file(
            self.database_path,
            self.production_database,
            "collection/production database",
        ):
            raise OutcomeCollectionError(
                "collection database aliases the production database"
            )
        try:
            with self.database_path.open("rb") as stream:
                header = stream.read(len(_SQLITE_HEADER))
        except OSError as exc:
            raise OutcomeCollectionError(
                "cannot read collection database header"
            ) from exc
        if header != _SQLITE_HEADER:
            raise OutcomeCollectionError(
                "collection database is not a valid SQLite database"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._assert_database_identity()
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            _validate_collection_schema(connection)
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _rows_to_events(rows: Iterable[sqlite3.Row]) -> tuple[OutcomeCollectionEvent, ...]:
        return tuple(OutcomeCollectionEvent.from_row(row) for row in rows)

    def _load_validated(
        self,
        connection: sqlite3.Connection,
        cutoff: datetime,
    ) -> tuple[tuple[OutcomeCollectionEvent, ...], tuple[OutcomeCollectionCase, ...]]:
        events = self._rows_to_events(
            connection.execute(
                "SELECT * FROM outcome_collection_events ORDER BY append_order"
            ).fetchall()
        )
        cases = _validate_events(events, cutoff)
        return events, cases

    @staticmethod
    def _case_map(
        cases: tuple[OutcomeCollectionCase, ...],
    ) -> dict[str, OutcomeCollectionCase]:
        return {case.case_id: case for case in cases}

    @staticmethod
    def _event_for_fact(
        events: tuple[OutcomeCollectionEvent, ...],
        fact_id: str,
    ) -> OutcomeCollectionEvent | None:
        return next((event for event in events if event.fact_id == fact_id), None)

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        events: tuple[OutcomeCollectionEvent, ...],
        *,
        case_id: str,
        event_type: OutcomeCollectionEventType,
        payload: Mapping[str, Any],
        observed_at: datetime,
        recorded_by: str,
    ) -> tuple[OutcomeCollectionEvent, OutcomeCollectionDisposition]:
        payload_json = _canonical_json_text(payload)
        fact_id = _require_sha256(payload.get("fact_id"), "fact_id")
        existing = self._event_for_fact(events, fact_id)
        if existing is not None:
            if (
                existing.case_id != case_id
                or existing.event_type is not event_type
                or existing.payload_json != payload_json
            ):
                raise OutcomeCollectionConflict(
                    "collection fact ID already contains different immutable evidence"
                )
            return existing, OutcomeCollectionDisposition.IDEMPOTENT
        previous = _ZERO_HASH if not events else events[-1].event_hash
        event = OutcomeCollectionEvent(
            append_order=len(events) + 1,
            case_id=case_id,
            event_type=event_type,
            fact_id=fact_id,
            observed_at=observed_at,
            recorded_by=recorded_by,
            previous_event_hash=previous,
            payload_json=payload_json,
        )
        _validate_events((*events, event), observed_at)
        connection.execute(
            """
            INSERT INTO outcome_collection_events(
                append_order,event_hash,previous_event_hash,case_id,event_type,
                fact_id,observed_at,recorded_by,payload_json,payload_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.append_order,
                event.event_hash,
                event.previous_event_hash,
                event.case_id,
                event.event_type.value,
                event.fact_id,
                _datetime_text(event.observed_at),
                event.recorded_by,
                event.payload_json,
                hashlib.sha256(event.payload_json.encode("utf-8")).hexdigest(),
            ),
        )
        return event, OutcomeCollectionDisposition.APPENDED

    @staticmethod
    def _result_case_state(
        events: tuple[OutcomeCollectionEvent, ...],
        case_id: str,
        cutoff: datetime,
    ) -> OutcomeCollectionCaseState:
        cases = _validate_events(events, cutoff)
        case = next((item for item in cases if item.case_id == case_id), None)
        if case is None:
            raise OutcomeCollectionError("collection case disappeared")
        return case.state

    def open_case(
        self,
        signal: RuntimeTypes.Signal,
        *,
        mode: OutcomeCollectionMode,
        strategy_version: str,
        horizon_sessions: int,
        model_id: str | None,
        instrument_id: str,
        identity_fact_id: str,
        data_snapshot_id: str,
        policy_id: str,
        classification_id: str | None,
        requested_quantity: int,
        entry_execution_policy_id: str,
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        if not isinstance(mode, OutcomeCollectionMode):
            raise OutcomeCollectionError("mode must be OutcomeCollectionMode")
        actor = _require_safe_token(recorded_by, "recorded_by")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = to_utc(ensure_aware(_utc_now(), "captured_at"))
                events, cases = self._load_validated(connection, observed)
                provisional = RuntimeSignalSnapshot.from_runtime_signal(
                    signal,
                    strategy_version=strategy_version,
                    horizon_sessions=horizon_sessions,
                    model_id=model_id,
                    instrument_id=instrument_id,
                    identity_fact_id=identity_fact_id,
                    data_snapshot_id=data_snapshot_id,
                    policy_id=policy_id,
                    classification_id=classification_id,
                    requested_quantity=requested_quantity,
                    entry_execution_policy_id=entry_execution_policy_id,
                    captured_at=observed,
                )
                provisional_case_id = _case_id(provisional, mode)
                existing = self._case_map(cases).get(provisional_case_id)
                snapshot = (
                    provisional
                    if existing is None
                    else RuntimeSignalSnapshot.from_runtime_signal(
                        signal,
                        strategy_version=strategy_version,
                        horizon_sessions=horizon_sessions,
                        model_id=model_id,
                        instrument_id=instrument_id,
                        identity_fact_id=identity_fact_id,
                        data_snapshot_id=data_snapshot_id,
                        policy_id=policy_id,
                        classification_id=classification_id,
                        requested_quantity=requested_quantity,
                        entry_execution_policy_id=entry_execution_policy_id,
                        captured_at=existing.snapshot.captured_at,
                    )
                )
                case_id = _case_id(snapshot, mode)
                payload = _open_payload(snapshot, mode)
                if existing is not None and existing.snapshot != snapshot:
                    raise OutcomeCollectionConflict(
                        "runtime episode already has different immutable evidence"
                    )
                event, disposition = self._append_event_locked(
                    connection,
                    events,
                    case_id=case_id,
                    event_type=OutcomeCollectionEventType.CASE_OPENED,
                    payload=payload,
                    observed_at=snapshot.captured_at,
                    recorded_by=actor,
                )
                new_events = events if disposition is OutcomeCollectionDisposition.IDEMPOTENT else (*events, event)
                state = self._result_case_state(new_events, case_id, observed)
                connection.commit()
                return OutcomeCollectionAppendResult(disposition, event, state)
            except Exception:
                connection.rollback()
                raise

    def _append_fact(
        self,
        case_id: str,
        event_type: OutcomeCollectionEventType,
        payload_builder,
        *,
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        case_id = _require_sha256(case_id, "case_id")
        actor = _require_safe_token(recorded_by, "recorded_by")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = to_utc(ensure_aware(_utc_now(), "observed_at"))
                events, cases = self._load_validated(connection, observed)
                case = self._case_map(cases).get(case_id)
                if case is None:
                    raise OutcomeCollectionError("outcome collection case does not exist")
                payload = payload_builder(case, observed)
                event, disposition = self._append_event_locked(
                    connection,
                    events,
                    case_id=case_id,
                    event_type=event_type,
                    payload=payload,
                    observed_at=observed,
                    recorded_by=actor,
                )
                new_events = events if disposition is OutcomeCollectionDisposition.IDEMPOTENT else (*events, event)
                state = self._result_case_state(new_events, case_id, observed)
                connection.commit()
                return OutcomeCollectionAppendResult(disposition, event, state)
            except Exception:
                connection.rollback()
                raise

    def record_entry_fill(
        self,
        case_id: str,
        *,
        timestamp: datetime,
        session_index: int,
        quantity: int,
        reference_price: Decimal,
        fill_price: Decimal,
        explicit_cost: Decimal,
        execution_rule_id: str,
        cost_schedule_id: str,
        raw_bar_snapshot_id: str,
        evidence_ids: Iterable[str] = (),
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        normalized_evidence = _normalize_evidence_ids(evidence_ids)

        def build(case: OutcomeCollectionCase, _observed: datetime) -> Mapping[str, Any]:
            payload, _fill = _build_entry_fill_payload(
                case.snapshot,
                timestamp=timestamp,
                session_index=session_index,
                quantity=quantity,
                reference_price=reference_price,
                fill_price=fill_price,
                explicit_cost=explicit_cost,
                execution_rule_id=execution_rule_id,
                cost_schedule_id=cost_schedule_id,
                raw_bar_snapshot_id=raw_bar_snapshot_id,
                evidence_ids=normalized_evidence,
            )
            return payload

        return self._append_fact(
            case_id,
            OutcomeCollectionEventType.ENTRY_FILLED,
            build,
            recorded_by=recorded_by,
        )

    def record_path_point(
        self,
        case_id: str,
        *,
        timestamp: datetime,
        session_index: int,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        observable: bool,
        raw_bar_snapshot_id: str,
        evidence_ids: Iterable[str] = (),
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        normalized_evidence = _normalize_evidence_ids(evidence_ids)

        def build(_case: OutcomeCollectionCase, _observed: datetime) -> Mapping[str, Any]:
            payload, _point = _build_path_payload(
                timestamp=timestamp,
                session_index=session_index,
                high=high,
                low=low,
                close=close,
                observable=observable,
                raw_bar_snapshot_id=raw_bar_snapshot_id,
                evidence_ids=normalized_evidence,
            )
            return payload

        return self._append_fact(
            case_id,
            OutcomeCollectionEventType.PATH_POINT,
            build,
            recorded_by=recorded_by,
        )

    def record_exit_request(
        self,
        case_id: str,
        *,
        requested_at: datetime,
        quantity: int,
        terminal_reason: OutcomeTerminalReason,
        execution_policy_id: str,
        reason: str,
        evidence_ids: Iterable[str] = (),
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        normalized_evidence = _normalize_evidence_ids(evidence_ids)

        def build(case: OutcomeCollectionCase, _observed: datetime) -> Mapping[str, Any]:
            payload, _intent = _build_exit_request_payload(
                case.case_id,
                case.snapshot,
                requested_at=requested_at,
                quantity=quantity,
                terminal_reason=terminal_reason,
                execution_policy_id=execution_policy_id,
                reason=reason,
                evidence_ids=normalized_evidence,
            )
            return payload

        return self._append_fact(
            case_id,
            OutcomeCollectionEventType.EXIT_REQUESTED,
            build,
            recorded_by=recorded_by,
        )

    def record_exit_fill(
        self,
        case_id: str,
        *,
        timestamp: datetime,
        session_index: int,
        quantity: int,
        reference_price: Decimal,
        fill_price: Decimal,
        explicit_cost: Decimal,
        execution_rule_id: str,
        cost_schedule_id: str,
        raw_bar_snapshot_id: str,
        evidence_ids: Iterable[str] = (),
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        normalized_evidence = _normalize_evidence_ids(evidence_ids)

        def build(case: OutcomeCollectionCase, _observed: datetime) -> Mapping[str, Any]:
            if case.exit_intent is None:
                raise OutcomeCollectionError("exit fill requires a collected exit request")
            payload, _fill = _build_exit_fill_payload(
                case.snapshot,
                case.exit_intent,
                timestamp=timestamp,
                session_index=session_index,
                quantity=quantity,
                reference_price=reference_price,
                fill_price=fill_price,
                explicit_cost=explicit_cost,
                execution_rule_id=execution_rule_id,
                cost_schedule_id=cost_schedule_id,
                raw_bar_snapshot_id=raw_bar_snapshot_id,
                evidence_ids=normalized_evidence,
            )
            return payload

        return self._append_fact(
            case_id,
            OutcomeCollectionEventType.EXIT_FILLED,
            build,
            recorded_by=recorded_by,
        )

    def record_no_entry(
        self,
        case_id: str,
        *,
        fact_at: datetime,
        terminal_reason: OutcomeTerminalReason,
        reason: str,
        evidence_ids: Iterable[str] = (),
        recorded_by: str,
    ) -> OutcomeCollectionAppendResult:
        normalized_evidence = _normalize_evidence_ids(evidence_ids)

        def build(_case: OutcomeCollectionCase, _observed: datetime) -> Mapping[str, Any]:
            return _build_no_entry_payload(
                fact_at=fact_at,
                terminal_reason=terminal_reason,
                reason=reason,
                evidence_ids=normalized_evidence,
            )

        return self._append_fact(
            case_id,
            OutcomeCollectionEventType.NO_ENTRY,
            build,
            recorded_by=recorded_by,
        )

    def _ledger_target_id(self, ledger: OutcomeLedger) -> str:
        if not isinstance(ledger, OutcomeLedger):
            raise OutcomeCollectionError("ledger must be OutcomeLedger")
        catalog = ledger.catalog_path.resolve(strict=False)
        root = ledger.record_root.resolve(strict=False)
        if self.database_path == catalog or _same_existing_file(
            self.database_path,
            catalog,
            "collection/outcome ledger database",
        ):
            raise OutcomeCollectionError(
                "collection database cannot be the outcome ledger catalog"
            )
        if self.database_path == root or self.database_path.is_relative_to(root):
            raise OutcomeCollectionError(
                "collection database cannot be inside the outcome ledger record root"
            )
        return fingerprint(
            {
                "schema": "stage4g-outcome-ledger-target-v1",
                "record_root": str(root),
                "catalog_path": str(catalog),
            }
        )

    def _prepare_finalization(
        self,
        case_id: str,
        ledger_target_id: str,
        *,
        recorded_by: str,
    ) -> tuple[OutcomeCollectionCase, SignalOutcome, OutcomeCollectionDisposition]:
        case_id = _require_sha256(case_id, "case_id")
        actor = _require_safe_token(recorded_by, "recorded_by")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = to_utc(ensure_aware(_utc_now(), "recorded_at"))
                events, cases = self._load_validated(connection, observed)
                case = self._case_map(cases).get(case_id)
                if case is None:
                    raise OutcomeCollectionError("outcome collection case does not exist")
                if case.prepared_outcome is not None:
                    if case.ledger_target_id != ledger_target_id:
                        raise OutcomeCollectionConflict(
                            "case was prepared for a different outcome ledger"
                        )
                    return (
                        case,
                        case.prepared_outcome,
                        OutcomeCollectionDisposition.IDEMPOTENT,
                    )
                if case.state is not OutcomeCollectionCaseState.TERMINAL_READY:
                    raise OutcomeCollectionError(
                        "outcome case is not ready for finalization"
                    )
                if case.terminal_reason is None:
                    raise OutcomeCollectionError("outcome case has no terminal reason")
                outcome = _build_signal_outcome(
                    snapshot=case.snapshot,
                    mode=case.mode,
                    entry_fill=case.entry_fill,
                    path=case.path,
                    exit_intent=case.exit_intent,
                    exit_fill=case.exit_fill,
                    terminal_reason=case.terminal_reason,
                    recorded_at=observed,
                )
                payload = _build_prepared_payload(outcome, ledger_target_id)
                event, disposition = self._append_event_locked(
                    connection,
                    events,
                    case_id=case_id,
                    event_type=OutcomeCollectionEventType.FINALIZATION_PREPARED,
                    payload=payload,
                    observed_at=observed,
                    recorded_by=actor,
                )
                new_events = events if disposition is OutcomeCollectionDisposition.IDEMPOTENT else (*events, event)
                new_cases = _validate_events(new_events, observed)
                prepared_case = self._case_map(new_cases)[case_id]
                connection.commit()
                return prepared_case, outcome, disposition
            except Exception:
                connection.rollback()
                raise

    def _record_finalized(
        self,
        case_id: str,
        payload: Mapping[str, Any],
        *,
        recorded_by: str,
    ) -> tuple[OutcomeCollectionCase, OutcomeCollectionDisposition]:
        result = self._append_fact(
            case_id,
            OutcomeCollectionEventType.FINALIZED,
            lambda _case, _observed: payload,
            recorded_by=recorded_by,
        )
        return self.get_case(case_id), result.disposition

    def finalize(
        self,
        case_id: str,
        ledger: OutcomeLedger,
        *,
        recorded_by: str,
    ) -> OutcomeCollectionFinalizationResult:
        target_id = self._ledger_target_id(ledger)
        existing_case = self.get_case(case_id)
        if existing_case.state is OutcomeCollectionCaseState.FINALIZED:
            if existing_case.ledger_target_id != target_id:
                raise OutcomeCollectionConflict(
                    "case was finalized against a different outcome ledger"
                )
            if (
                existing_case.prepared_outcome is None
                or existing_case.finalized_record_hash is None
                or existing_case.finalized_record_append_order is None
                or existing_case.finalized_ledger_audit_id is None
                or existing_case.finalized_ledger_disposition is None
            ):
                raise OutcomeCollectionError("finalized case evidence is incomplete")
            ledger_result = ledger.append(
                existing_case.prepared_outcome,
                recorded_by=recorded_by,
            )
            if (
                ledger_result.record.record_hash != existing_case.finalized_record_hash
                or ledger_result.record.append_order
                != existing_case.finalized_record_append_order
            ):
                raise OutcomeCollectionConflict(
                    "finalized case disagrees with immutable outcome ledger"
                )
            return OutcomeCollectionFinalizationResult(
                case=existing_case,
                outcome=existing_case.prepared_outcome,
                ledger_result=ledger_result,
                ledger_audit_id=existing_case.finalized_ledger_audit_id,
                collection_disposition=OutcomeCollectionDisposition.IDEMPOTENT,
            )
        _prepared_case, outcome, _prepare_disposition = self._prepare_finalization(
            case_id,
            target_id,
            recorded_by=recorded_by,
        )
        ledger_result = ledger.append(outcome, recorded_by=recorded_by)
        ledger_audit = ledger.audit()
        payload = _build_finalized_payload(
            ledger_target_id=target_id,
            outcome_id=outcome.outcome_id,
            record_hash=ledger_result.record.record_hash,
            record_append_order=ledger_result.record.append_order,
            ledger_audit_id=ledger_audit.audit_id,
            ledger_disposition=ledger_result.disposition,
        )
        case, disposition = self._record_finalized(
            case_id,
            payload,
            recorded_by=recorded_by,
        )
        return OutcomeCollectionFinalizationResult(
            case=case,
            outcome=outcome,
            ledger_result=ledger_result,
            ledger_audit_id=ledger_audit.audit_id,
            collection_disposition=disposition,
        )

    def audit(self) -> OutcomeCollectionAuditReport:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = to_utc(ensure_aware(_utc_now(), "audited_at"))
                events, cases = self._load_validated(connection, observed)
                connection.rollback()
            except Exception:
                connection.rollback()
                raise
        hashes = tuple(event.event_hash for event in events)
        state_counts = tuple(
            sorted(
                Counter(case.state for case in cases).items(),
                key=lambda item: item[0].value,
            )
        )
        return OutcomeCollectionAuditReport(
            audited_at=observed,
            event_hashes=hashes,
            case_ids=tuple(sorted(case.case_id for case in cases)),
            state_counts=state_counts,
            first_event_hash=_ZERO_HASH if not hashes else hashes[0],
            last_event_hash=_ZERO_HASH if not hashes else hashes[-1],
        )

    def list_cases(self) -> tuple[OutcomeCollectionCase, ...]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                observed = to_utc(ensure_aware(_utc_now(), "audited_at"))
                _events, cases = self._load_validated(connection, observed)
                connection.rollback()
                return cases
            except Exception:
                connection.rollback()
                raise

    def get_case(self, case_id: str) -> OutcomeCollectionCase:
        case_id = _require_sha256(case_id, "case_id")
        case = next((item for item in self.list_cases() if item.case_id == case_id), None)
        if case is None:
            raise OutcomeCollectionError("outcome collection case does not exist")
        return case


__all__ = [
    "CASE_OPENED_SCHEMA",
    "COLLECTION_AUDIT_SCHEMA",
    "COLLECTION_EVENT_SCHEMA",
    "COLLECTION_SCHEMA",
    "DEFAULT_COLLECTION_DATABASE",
    "ENTRY_FILL_SCHEMA",
    "EXIT_FILL_SCHEMA",
    "EXIT_REQUEST_SCHEMA",
    "FINALIZATION_PREPARED_SCHEMA",
    "FINALIZED_SCHEMA",
    "NO_ENTRY_SCHEMA",
    "PATH_POINT_SCHEMA",
    "RUNTIME_SCORE_SNAPSHOT_SCHEMA",
    "RUNTIME_SIGNAL_SNAPSHOT_SCHEMA",
    "OutcomeCollectionAppendResult",
    "OutcomeCollectionAuditReport",
    "OutcomeCollectionCase",
    "OutcomeCollectionCaseState",
    "OutcomeCollectionConflict",
    "OutcomeCollectionDisposition",
    "OutcomeCollectionError",
    "OutcomeCollectionEvent",
    "OutcomeCollectionEventType",
    "OutcomeCollectionFinalizationResult",
    "OutcomeCollectionMode",
    "OutcomeCollectionService",
    "RuntimeScoreSnapshot",
    "RuntimeSignalSnapshot",
]
