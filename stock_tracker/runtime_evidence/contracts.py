from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Protocol

from stock_tracker.core import config as C
from stock_tracker.core import types as T
from stock_tracker.core.config import ConfigBundle

RUNTIME_DECISION_ARTIFACT_SCHEMA = "stage4g1-runtime-decision-artifact-v4"
_DECISION_CONTENT_SCHEMA = "stage4g1-runtime-decision-content-v2"
_TRANSITION_OCCURRENCE_SCHEMA = "stage4g1-runtime-transition-occurrence-v4"
_SIGNAL_SNAPSHOT_SCHEMA = "stage4g1-runtime-signal-snapshot-v1"
_DATA_SNAPSHOT_SCHEMA = "stage4g1-runtime-decision-inputs-v1"
_POLICY_SNAPSHOT_SCHEMA = "stage4g1-runtime-policy-snapshot-v1"
_IDENTITY_SNAPSHOT_SCHEMA = "stage4g1-runtime-instrument-identity-v1"
_CLASSIFICATION_SNAPSHOT_SCHEMA = "stage4g1-runtime-classification-v1"
_SHA256_LENGTH = 64
_ZERO_SHA256 = "0" * _SHA256_LENGTH
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_REASON_COUNT = 256
_MAX_BAR_COUNT = 5000
_SIGNAL_STATES = frozenset(item.value for item in T.SignalState)
_DATA_STATUSES = frozenset(item.value for item in T.DataStatus)
_QUALITY_STATUSES = frozenset(item.value for item in T.QualityStatus)
_SNAPSHOT_DATACLASS_TYPES = frozenset(
    {
        T.Signal,
        T.ScoreSet,
        T.Quote,
        T.Bar,
        T.DataQuality,
        C.ConfigBundle,
        C.AppConfig,
        C.ServerConfig,
        C.RuntimeConfig,
        C.LoggingConfig,
        C.CollectorConfig,
        C.StoreConfig,
        C.MarketsConfig,
        C.MarketConfig,
        C.StrategiesConfig,
        C.StrategyConfig,
        C.ProviderConfig,
        C.RiskConfig,
    }
)
_SNAPSHOT_ENUM_TYPES = frozenset(
    {
        T.Market,
        T.DataStatus,
        T.QualityStatus,
        T.SignalState,
    }
)
_SIGNAL_FIELD_NAMES = {item.name for item in fields(T.Signal)}
_SCORE_FIELD_NAMES = {item.name for item in fields(T.ScoreSet)}
_QUOTE_FIELD_NAMES = {item.name for item in fields(T.Quote)}
_BAR_FIELD_NAMES = {item.name for item in fields(T.Bar)}
_DATA_QUALITY_FIELD_NAMES = {item.name for item in fields(T.DataQuality)}
_EXPECTED_IDENTITY_FIELDS = {
    "schema",
    "runtime_store_id",
    "runtime_signal_id",
    "transition_event_id",
    "decision_content_id",
    "occurrence_dedup_id",
    "upstream_occurrence_id",
    "signal_version_id",
    "expected_previous_signal_version_id",
    "symbol",
    "market",
    "strategy_id",
    "strategy_version",
    "model_id",
    "state",
    "previous_state",
    "state_changed_at_utc",
    "decision_requested_at_utc",
    "observed_at_utc",
    "pit_evidence_status",
    "bar_known_at_authority",
    "single_active_runtime_store",
    "fork_detection",
    "external_checkpoint",
    "entry_plan",
    "scores",
    "data_status",
    "data_quality",
    "instrument_id",
    "identity_fact_id",
    "data_snapshot_id",
    "policy_id",
    "classification_id",
    "market_regime",
    "sector_stage",
    "execution_mode",
    "execution_rules",
    "outcome_case_status",
    "incomplete_reasons",
    "auto_trade",
    "trusted_outcome_admission",
    "investment_performance_claim",
    "signal_snapshot",
    "previous_signal_snapshot",
    "data_snapshot",
    "policy_snapshot",
    "identity_snapshot",
    "classification_snapshot",
}
_DRAFT_IDENTITY_FIELDS = _EXPECTED_IDENTITY_FIELDS - {"transition_event_id"}
_DECISION_CONTENT_FIELDS = _EXPECTED_IDENTITY_FIELDS - {
    "schema",
    "runtime_store_id",
    "transition_event_id",
    "decision_content_id",
    "occurrence_dedup_id",
    "upstream_occurrence_id",
    "observed_at_utc",
}


class RuntimeEvidenceContractError(ValueError):
    def __init__(self, message: str, *, code: str = "ARTIFACT_CONTRACT_INVALID") -> None:
        super().__init__(message)
        self.code = code


class UtcClock(Protocol):
    def now(self) -> datetime:
        ...


class SystemUtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def require_utc_clock_value(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeEvidenceContractError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeEvidenceContractError(
            f"{name} must be timezone-aware",
            code="LEGACY_NAIVE_RUNTIME_TIME",
        )
    return value.astimezone(timezone.utc)


def _datetime_text(value: object, name: str) -> str:
    return (
        require_utc_clock_value(value, name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _datetime_from_text(value: object, name: str) -> datetime:
    text = _require_text(value, name, maximum=64)
    if not text.endswith("Z"):
        raise RuntimeEvidenceContractError(f"{name} must use canonical UTC Z form")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeEvidenceContractError(f"{name} must be ISO-8601") from exc
    if _datetime_text(parsed, name) != text:
        raise RuntimeEvidenceContractError(f"{name} is not canonical UTC")
    return parsed


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or value != value.strip() or len(value) > maximum:
        raise RuntimeEvidenceContractError(f"{name} must be a safe trimmed string")
    if not value and not allow_empty:
        raise RuntimeEvidenceContractError(f"{name} must not be empty")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise RuntimeEvidenceContractError(f"{name} contains control characters")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimeEvidenceContractError(f"{name} must be boolean")
    return value


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeEvidenceContractError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_number(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise RuntimeEvidenceContractError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise RuntimeEvidenceContractError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise RuntimeEvidenceContractError(f"{name} must be non-negative")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=_SHA256_LENGTH)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise RuntimeEvidenceContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise RuntimeEvidenceContractError(f"{name} must be an exact object with string keys")
    return value


def _require_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RuntimeEvidenceContractError(
            f"{name} field set is invalid; missing={sorted(expected - actual)}; "
            f"extra={sorted(actual - expected)}"
        )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeEvidenceContractError("artifact is not canonical JSON") from exc
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise RuntimeEvidenceContractError("artifact exceeds the JSON size bound")
    return raw


def _strict_json_loads(raw: bytes) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_JSON_BYTES:
        raise RuntimeEvidenceContractError("artifact JSON bytes are invalid")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeEvidenceContractError("artifact contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise RuntimeEvidenceContractError(
            f"artifact contains non-finite token {token}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeEvidenceContractError("artifact is not strict UTF-8 JSON") from exc
    document = _require_mapping(value, "artifact")
    if _canonical_json_bytes(document) != raw:
        raise RuntimeEvidenceContractError("artifact JSON is not canonical")
    return document


def _hash_document(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _explicit_datetime(value: object) -> dict[str, Any]:
    if not isinstance(value, datetime):
        raise RuntimeEvidenceContractError("decision input timestamp must be datetime")
    aware = value.tzinfo is not None and value.utcoffset() is not None
    return {
        "value": (
            _datetime_text(value, "decision input timestamp")
            if aware
            else value.isoformat(timespec="microseconds")
        ),
        "timezone_aware": aware,
    }


def _strict_snapshot_value(
    value: Any,
    *,
    allow_legacy_naive_datetime: bool = False,
) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RuntimeEvidenceContractError("snapshot contains non-finite number")
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            if not allow_legacy_naive_datetime:
                raise RuntimeEvidenceContractError(
                    "snapshot contains a legacy naive datetime",
                    code="LEGACY_NAIVE_RUNTIME_TIME",
                )
            return {
                "value": value.isoformat(timespec="microseconds"),
                "timezone_aware": False,
            }
        return _explicit_datetime(value)
    value_type = type(value)
    if value_type in _SNAPSHOT_ENUM_TYPES:
        return value.value
    if value_type in _SNAPSHOT_DATACLASS_TYPES:
        return {
            item.name: _strict_snapshot_value(
                getattr(value, item.name),
                allow_legacy_naive_datetime=allow_legacy_naive_datetime,
            )
            for item in fields(value_type)
        }
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise RuntimeEvidenceContractError("snapshot mapping keys must be strings")
        return {
            key: _strict_snapshot_value(
                item,
                allow_legacy_naive_datetime=allow_legacy_naive_datetime,
            )
            for key, item in value.items()
        }
    if type(value) in (list, tuple):
        return [
            _strict_snapshot_value(
                item,
                allow_legacy_naive_datetime=allow_legacy_naive_datetime,
            )
            for item in value
        ]
    raise RuntimeEvidenceContractError(
        f"snapshot contains unsupported type {type(value).__name__}"
    )


def _snapshot_document(value: object, name: str) -> dict[str, Any]:
    document = _require_mapping(value, name)
    normalized = _strict_snapshot_value(dict(document))
    if type(normalized) is not dict:
        raise RuntimeEvidenceContractError(f"{name} must be a snapshot object")
    return normalized


def _signal_snapshot(
    signal: T.Signal,
    *,
    allow_legacy_naive_datetime: bool,
) -> dict[str, Any]:
    if type(signal) is not T.Signal:
        raise RuntimeEvidenceContractError("signal must be the exact runtime Signal type")
    snapshot = _strict_snapshot_value(
        signal,
        allow_legacy_naive_datetime=allow_legacy_naive_datetime,
    )
    if type(snapshot) is not dict:
        raise RuntimeEvidenceContractError("runtime signal snapshot is invalid")
    return {"schema": _SIGNAL_SNAPSHOT_SCHEMA, "signal": snapshot}


def runtime_signal_version_id(
    signal: T.Signal | None,
    *,
    allow_legacy_naive_datetime: bool = True,
) -> str | None:
    if signal is None:
        return None
    return _hash_document(
        _signal_snapshot(
            signal,
            allow_legacy_naive_datetime=allow_legacy_naive_datetime,
        )
    )


def _snapshot_datetime_document(value: object, name: str) -> dict[str, Any]:
    document = _require_mapping(value, name)
    _require_fields(document, {"value", "timezone_aware"}, name)
    if _require_bool(document["timezone_aware"], f"{name}.timezone_aware") is not True:
        raise RuntimeEvidenceContractError(
            f"{name} must be timezone-aware",
            code="LEGACY_NAIVE_RUNTIME_TIME",
        )
    parsed = _datetime_from_text(document["value"], f"{name}.value")
    return {"value": _datetime_text(parsed, name), "timezone_aware": True}


def _snapshot_datetime_value(value: object, name: str) -> datetime:
    document = _snapshot_datetime_document(value, name)
    return _datetime_from_text(document["value"], f"{name}.value")


def _validate_signal_snapshot_document(
    value: object,
    name: str,
) -> dict[str, Any]:
    snapshot = _snapshot_document(value, name)
    _require_fields(snapshot, {"schema", "signal"}, name)
    if snapshot["schema"] != _SIGNAL_SNAPSHOT_SCHEMA:
        raise RuntimeEvidenceContractError(f"{name} schema is invalid")
    signal = _snapshot_document(snapshot["signal"], f"{name}.signal")
    _require_fields(signal, _SIGNAL_FIELD_NAMES, f"{name}.signal")
    signal["state_changed_at"] = _snapshot_datetime_document(
        signal["state_changed_at"],
        f"{name}.signal.state_changed_at",
    )
    scores = signal["scores"]
    if scores is not None:
        normalized_scores = _snapshot_document(scores, f"{name}.signal.scores")
        _require_fields(
            normalized_scores,
            _SCORE_FIELD_NAMES,
            f"{name}.signal.scores",
        )
        signal["scores"] = normalized_scores
    return {"schema": _SIGNAL_SNAPSHOT_SCHEMA, "signal": signal}


def _validate_data_snapshot_document(value: object) -> dict[str, Any]:
    snapshot = _snapshot_document(value, "data_snapshot")
    _require_fields(
        snapshot,
        {"schema", "quote", "bars", "data_quality"},
        "data_snapshot",
    )
    if snapshot["schema"] != _DATA_SNAPSHOT_SCHEMA:
        raise RuntimeEvidenceContractError("data_snapshot schema is invalid")
    quote = _snapshot_document(snapshot["quote"], "data_snapshot.quote")
    _require_fields(quote, _QUOTE_FIELD_NAMES, "data_snapshot.quote")
    for field_name in ("timestamp", "received_at", "computed_at", "displayed_at"):
        quote[field_name] = _snapshot_datetime_document(
            quote[field_name],
            f"data_snapshot.quote.{field_name}",
        )
    bars = snapshot["bars"]
    if type(bars) is not list or len(bars) > _MAX_BAR_COUNT:
        raise RuntimeEvidenceContractError("data_snapshot.bars must be a bounded array")
    normalized_bars: list[dict[str, Any]] = []
    for index, value_item in enumerate(bars):
        bar = _snapshot_document(value_item, f"data_snapshot.bars[{index}]")
        _require_fields(bar, _BAR_FIELD_NAMES, f"data_snapshot.bars[{index}]")
        bar["timestamp"] = _snapshot_datetime_document(
            bar["timestamp"],
            f"data_snapshot.bars[{index}].timestamp",
        )
        normalized_bars.append(bar)
    quality = _snapshot_document(snapshot["data_quality"], "data_snapshot.data_quality")
    _require_fields(
        quality,
        _DATA_QUALITY_FIELD_NAMES,
        "data_snapshot.data_quality",
    )
    return {
        "schema": _DATA_SNAPSHOT_SCHEMA,
        "quote": quote,
        "bars": normalized_bars,
        "data_quality": quality,
    }


def _validate_policy_snapshot_document(value: object) -> dict[str, Any]:
    snapshot = _snapshot_document(value, "policy_snapshot")
    _require_fields(
        snapshot,
        {"schema", "strategy", "risk", "market", "price_limit_version"},
        "policy_snapshot",
    )
    if snapshot["schema"] != _POLICY_SNAPSHOT_SCHEMA:
        raise RuntimeEvidenceContractError("policy_snapshot schema is invalid")
    strategy = _snapshot_document(snapshot["strategy"], "policy_snapshot.strategy")
    _require_fields(strategy, {"strategy_id", "config"}, "policy_snapshot.strategy")
    if strategy["config"] is not None:
        strategy["config"] = _snapshot_document(
            strategy["config"],
            "policy_snapshot.strategy.config",
        )
    return {
        "schema": _POLICY_SNAPSHOT_SCHEMA,
        "strategy": strategy,
        "risk": _snapshot_document(snapshot["risk"], "policy_snapshot.risk"),
        "market": _snapshot_document(snapshot["market"], "policy_snapshot.market"),
        "price_limit_version": _require_text(
            snapshot["price_limit_version"],
            "policy_snapshot.price_limit_version",
            allow_empty=True,
        ),
    }


def _validate_identity_snapshot_document(value: object) -> dict[str, Any]:
    snapshot = _snapshot_document(value, "identity_snapshot")
    _require_fields(
        snapshot,
        {"schema", "symbol", "market", "metadata"},
        "identity_snapshot",
    )
    if snapshot["schema"] != _IDENTITY_SNAPSHOT_SCHEMA:
        raise RuntimeEvidenceContractError("identity_snapshot schema is invalid")
    return {
        "schema": _IDENTITY_SNAPSHOT_SCHEMA,
        "symbol": _require_text(snapshot["symbol"], "identity_snapshot.symbol"),
        "market": _require_text(snapshot["market"], "identity_snapshot.market"),
        "metadata": _snapshot_document(
            snapshot["metadata"],
            "identity_snapshot.metadata",
        ),
    }


def _validate_classification_snapshot_document(value: object) -> dict[str, Any]:
    snapshot = _snapshot_document(value, "classification_snapshot")
    _require_fields(
        snapshot,
        {"schema", "market_regime", "sector_stage"},
        "classification_snapshot",
    )
    if snapshot["schema"] != _CLASSIFICATION_SNAPSHOT_SCHEMA:
        raise RuntimeEvidenceContractError("classification_snapshot schema is invalid")
    return {
        "schema": _CLASSIFICATION_SNAPSHOT_SCHEMA,
        "market_regime": _require_text(
            snapshot["market_regime"],
            "classification_snapshot.market_regime",
            allow_empty=True,
        ),
        "sector_stage": _require_text(
            snapshot["sector_stage"],
            "classification_snapshot.sector_stage",
            allow_empty=True,
        ),
    }


def _validate_reasons(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_REASON_COUNT:
        raise RuntimeEvidenceContractError(f"{name} must be a bounded array")
    return [_require_text(item, name, maximum=1024) for item in value]


def _validate_identity(
    identity: Mapping[str, Any],
    *,
    allow_placeholder_transition: bool = False,
) -> dict[str, Any]:
    _require_fields(identity, _EXPECTED_IDENTITY_FIELDS, "artifact identity")
    if identity["schema"] != RUNTIME_DECISION_ARTIFACT_SCHEMA:
        raise RuntimeEvidenceContractError("runtime artifact schema is invalid")
    runtime_store_id = _require_sha256(
        identity["runtime_store_id"],
        "runtime_store_id",
    )
    runtime_signal_id = _require_text(identity["runtime_signal_id"], "runtime_signal_id")
    transition_event_id = _require_sha256(
        identity["transition_event_id"], "transition_event_id"
    )
    if transition_event_id == _ZERO_SHA256 and not allow_placeholder_transition:
        raise RuntimeEvidenceContractError(
            "transition_event_id must be a persisted non-placeholder identity"
        )
    decision_content_id = _require_sha256(
        identity["decision_content_id"],
        "decision_content_id",
    )
    occurrence_dedup_id = _require_sha256(
        identity["occurrence_dedup_id"],
        "occurrence_dedup_id",
    )
    upstream_occurrence_id = _require_optional_text(
        identity["upstream_occurrence_id"],
        "upstream_occurrence_id",
    )
    if upstream_occurrence_id is not None:
        raise RuntimeEvidenceContractError(
            "upstream occurrence authority is not configured in Checkpoint A",
            code="UPSTREAM_OCCURRENCE_AUTHORITY_NOT_CONFIGURED",
        )
    signal_version_id = _require_sha256(
        identity["signal_version_id"],
        "signal_version_id",
    )
    expected_previous_signal_version_id = identity[
        "expected_previous_signal_version_id"
    ]
    if expected_previous_signal_version_id is not None:
        expected_previous_signal_version_id = _require_sha256(
            expected_previous_signal_version_id,
            "expected_previous_signal_version_id",
        )
    market = _require_text(identity["market"], "market", maximum=8)
    try:
        market_enum = T.Market(market)
    except ValueError as exc:
        raise RuntimeEvidenceContractError("market is invalid") from exc
    symbol = _require_text(identity["symbol"], "symbol", maximum=64)
    suffixes = {T.Market.A: (".SH", ".SZ"), T.Market.HK: (".HK",), T.Market.US: (".US",)}
    if symbol != symbol.upper() or not symbol.endswith(suffixes[market_enum]):
        raise RuntimeEvidenceContractError("symbol suffix must match market")
    strategy_id = _require_text(identity["strategy_id"], "strategy_id", maximum=128)
    strategy_version = _require_text(
        identity["strategy_version"], "strategy_version", maximum=256
    )
    model_id = _require_optional_text(identity["model_id"], "model_id")
    state = _require_text(identity["state"], "state", maximum=64)
    if state not in _SIGNAL_STATES:
        raise RuntimeEvidenceContractError("state is invalid")
    previous_state = identity["previous_state"]
    if previous_state is not None:
        previous_state = _require_text(previous_state, "previous_state", maximum=64)
        if previous_state not in _SIGNAL_STATES:
            raise RuntimeEvidenceContractError("previous_state is invalid")
    state_changed_at = _datetime_from_text(
        identity["state_changed_at_utc"], "state_changed_at_utc"
    )
    requested_at = _datetime_from_text(
        identity["decision_requested_at_utc"], "decision_requested_at_utc"
    )
    observed_at = _datetime_from_text(identity["observed_at_utc"], "observed_at_utc")
    if state_changed_at > observed_at or requested_at > observed_at:
        raise RuntimeEvidenceContractError("artifact decision times exceed observed_at")
    if state_changed_at != requested_at:
        raise RuntimeEvidenceContractError(
            "transition state_changed_at must equal decision_requested_at"
        )
    pit_evidence_status = _require_text(
        identity["pit_evidence_status"], "pit_evidence_status"
    )
    if pit_evidence_status != "RUNTIME_MEMORY_ONLY":
        raise RuntimeEvidenceContractError(
            "Checkpoint A PIT evidence must remain runtime-memory-only"
        )
    bar_known_at_authority = _require_text(
        identity["bar_known_at_authority"], "bar_known_at_authority"
    )
    if bar_known_at_authority != "NOT_AVAILABLE":
        raise RuntimeEvidenceContractError(
            "Checkpoint A has no authoritative Bar known_at"
        )
    if _require_bool(
        identity["single_active_runtime_store"], "single_active_runtime_store"
    ) is not True:
        raise RuntimeEvidenceContractError("single active Runtime Store is required")
    fork_detection = _require_text(identity["fork_detection"], "fork_detection")
    if fork_detection != "LOCAL_APPEND_CHAIN_ONLY":
        raise RuntimeEvidenceContractError("fork detection boundary is invalid")
    external_checkpoint = _require_text(
        identity["external_checkpoint"], "external_checkpoint"
    )
    if external_checkpoint != "NOT_IMPLEMENTED":
        raise RuntimeEvidenceContractError("external checkpoint is not implemented")

    signal_snapshot = _validate_signal_snapshot_document(
        identity["signal_snapshot"],
        "signal_snapshot",
    )
    previous_signal_snapshot_value = identity["previous_signal_snapshot"]
    previous_signal_snapshot = (
        None
        if previous_signal_snapshot_value is None
        else _validate_signal_snapshot_document(
            previous_signal_snapshot_value,
            "previous_signal_snapshot",
        )
    )
    data_snapshot = _validate_data_snapshot_document(identity["data_snapshot"])
    policy_snapshot = _validate_policy_snapshot_document(identity["policy_snapshot"])
    identity_snapshot = _validate_identity_snapshot_document(
        identity["identity_snapshot"]
    )
    classification_snapshot = _validate_classification_snapshot_document(
        identity["classification_snapshot"]
    )
    quote_document = data_snapshot["quote"]
    for field_name in ("timestamp", "received_at", "computed_at"):
        if _snapshot_datetime_value(
            quote_document[field_name],
            f"data_snapshot.quote.{field_name}",
        ) > requested_at:
            raise RuntimeEvidenceContractError(
                f"quote {field_name} exceeds decision_requested_at"
            )
    if _snapshot_datetime_value(
        quote_document["displayed_at"],
        "data_snapshot.quote.displayed_at",
    ) > observed_at:
        raise RuntimeEvidenceContractError("quote displayed_at exceeds observed_at")
    for index, bar_document in enumerate(data_snapshot["bars"]):
        if _snapshot_datetime_value(
            bar_document["timestamp"],
            f"data_snapshot.bars[{index}].timestamp",
        ) > requested_at:
            raise RuntimeEvidenceContractError(
                "bar timestamp exceeds decision_requested_at"
            )
    if _hash_document(signal_snapshot) != signal_version_id:
        raise RuntimeEvidenceContractError("signal_version_id mismatch")
    if previous_signal_snapshot is None:
        if expected_previous_signal_version_id is not None:
            raise RuntimeEvidenceContractError(
                "previous signal version requires a previous signal snapshot"
            )
    elif _hash_document(previous_signal_snapshot) != expected_previous_signal_version_id:
        raise RuntimeEvidenceContractError(
            "expected_previous_signal_version_id mismatch"
        )

    signal_document = signal_snapshot["signal"]
    if (
        signal_document["signal_id"] != runtime_signal_id
        or signal_document["symbol"] != symbol
        or signal_document["market"] != market
        or signal_document["strategy_id"] != strategy_id
        or signal_document["state"] != state
        or signal_document["previous_state"] != previous_state
        or signal_document["state_changed_at"]
        != {"value": _datetime_text(state_changed_at, "state_changed_at_utc"), "timezone_aware": True}
    ):
        raise RuntimeEvidenceContractError(
            "signal snapshot disagrees with the runtime artifact envelope"
        )
    if previous_signal_snapshot is None:
        if previous_state is not None:
            raise RuntimeEvidenceContractError(
                "previous_state requires a previous signal snapshot"
            )
    else:
        previous_document = previous_signal_snapshot["signal"]
        previous_state_changed_at = _snapshot_datetime_value(
            previous_document["state_changed_at"],
            "previous_signal_snapshot.signal.state_changed_at",
        )
        if (
            previous_document["signal_id"] != runtime_signal_id
            or previous_document["symbol"] != symbol
            or previous_document["market"] != market
            or previous_document["strategy_id"] != strategy_id
            or previous_document["state"] != previous_state
            or previous_state == state
            or previous_state_changed_at > requested_at
        ):
            raise RuntimeEvidenceContractError(
                "previous signal snapshot disagrees with the transition envelope"
            )

    if _hash_document(data_snapshot) != _require_sha256(
        identity["data_snapshot_id"],
        "data_snapshot_id",
    ):
        raise RuntimeEvidenceContractError("data_snapshot_id mismatch")
    if _hash_document(policy_snapshot) != _require_sha256(
        identity["policy_id"],
        "policy_id",
    ):
        raise RuntimeEvidenceContractError("policy_id mismatch")
    if _hash_document(identity_snapshot) != _require_sha256(
        identity["identity_fact_id"],
        "identity_fact_id",
    ):
        raise RuntimeEvidenceContractError("identity_fact_id mismatch")
    classification_snapshot_id = _hash_document(classification_snapshot)
    if classification_snapshot_id != _require_sha256(
        identity["classification_id"],
        "classification_id",
    ):
        raise RuntimeEvidenceContractError("classification_id mismatch")

    entry_plan = _require_mapping(identity["entry_plan"], "entry_plan")
    _require_fields(
        entry_plan,
        {
            "entry_low",
            "entry_high",
            "trigger_price",
            "invalidation_price",
            "target_1",
            "target_2",
            "reward_risk",
        },
        "entry_plan",
    )
    normalized_plan = {
        name: _require_number(
            entry_plan[name],
            name,
            positive=name != "reward_risk",
            nonnegative=name == "reward_risk",
        )
        for name in entry_plan
    }
    if normalized_plan["entry_low"] > normalized_plan["entry_high"]:
        raise RuntimeEvidenceContractError("entry range is inconsistent")
    if normalized_plan["invalidation_price"] >= normalized_plan["entry_low"]:
        raise RuntimeEvidenceContractError("invalidation must be below entry range")
    if (
        normalized_plan["target_1"]
        <= max(normalized_plan["entry_high"], normalized_plan["trigger_price"])
        or normalized_plan["target_2"] < normalized_plan["target_1"]
    ):
        raise RuntimeEvidenceContractError("targets are inconsistent")

    scores = _require_mapping(identity["scores"], "scores")
    _require_fields(
        scores,
        {
            "opportunity",
            "timing",
            "risk",
            "confidence",
            "success_probability",
            "positive_reasons",
            "negative_reasons",
        },
        "scores",
    )
    normalized_scores: dict[str, Any] = {
        name: _require_int(scores[name], name, maximum=100)
        for name in ("opportunity", "timing", "risk", "confidence")
    }
    probability = scores["success_probability"]
    if probability is not None:
        probability = _require_number(
            probability, "success_probability", nonnegative=True
        )
        if probability > 1:
            raise RuntimeEvidenceContractError("success_probability must be in [0, 1]")
    normalized_scores.update(
        success_probability=probability,
        positive_reasons=_validate_reasons(scores["positive_reasons"], "positive_reason"),
        negative_reasons=_validate_reasons(scores["negative_reasons"], "negative_reason"),
    )

    data_status = _require_text(identity["data_status"], "data_status", maximum=32)
    if data_status not in _DATA_STATUSES:
        raise RuntimeEvidenceContractError("data_status is invalid")
    quality = _require_mapping(identity["data_quality"], "data_quality")
    _require_fields(quality, {"status", "score", "reasons"}, "data_quality")
    quality_status = _require_text(quality["status"], "quality status", maximum=32)
    if quality_status not in _QUALITY_STATUSES:
        raise RuntimeEvidenceContractError("data quality status is invalid")
    normalized_quality = {
        "status": quality_status,
        "score": _require_int(quality["score"], "quality score", maximum=100),
        "reasons": _validate_reasons(quality["reasons"], "quality reason"),
    }

    instrument_id = _require_text(identity["instrument_id"], "instrument_id")
    identity_fact_id = _require_sha256(identity["identity_fact_id"], "identity_fact_id")
    data_snapshot_id = _require_sha256(identity["data_snapshot_id"], "data_snapshot_id")
    policy_id = _require_sha256(identity["policy_id"], "policy_id")
    classification_id = _require_sha256(
        identity["classification_id"],
        "classification_id",
    )
    market_regime = _require_text(
        identity["market_regime"], "market_regime", maximum=128, allow_empty=True
    )
    sector_stage = _require_text(
        identity["sector_stage"], "sector_stage", maximum=128, allow_empty=True
    )
    if instrument_id != f"UNRESOLVED_RUNTIME_IDENTITY:{identity_fact_id}":
        raise RuntimeEvidenceContractError(
            "instrument_id must remain bound to the unresolved identity snapshot"
        )
    if (
        identity_snapshot["symbol"] != symbol
        or identity_snapshot["market"] != market
        or classification_snapshot["market_regime"] != market_regime
        or classification_snapshot["sector_stage"] != sector_stage
    ):
        raise RuntimeEvidenceContractError(
            "identity or classification snapshot disagrees with the envelope"
        )
    if policy_snapshot["strategy"]["strategy_id"] != strategy_id:
        raise RuntimeEvidenceContractError(
            "policy strategy snapshot disagrees with strategy_id"
        )
    expected_strategy_version = (
        f"runtime-strategy-config-v1:{_hash_document(policy_snapshot['strategy'])}"
    )
    if strategy_version != expected_strategy_version:
        raise RuntimeEvidenceContractError("strategy_version mismatch")
    if (
        signal_document["entry_low"] != normalized_plan["entry_low"]
        or signal_document["entry_high"] != normalized_plan["entry_high"]
        or signal_document["trigger_price"] != normalized_plan["trigger_price"]
        or signal_document["invalidation_price"]
        != normalized_plan["invalidation_price"]
        or signal_document["target_1"] != normalized_plan["target_1"]
        or signal_document["target_2"] != normalized_plan["target_2"]
        or signal_document["reward_risk"] != normalized_plan["reward_risk"]
        or signal_document["scores"] != normalized_scores
        or signal_document["data_status"] != data_status
        or signal_document["market_regime"] != market_regime
        or signal_document["sector_stage"] != sector_stage
    ):
        raise RuntimeEvidenceContractError(
            "signal snapshot content disagrees with the artifact decision fields"
        )
    quote_snapshot = data_snapshot["quote"]
    quote_source_time = _snapshot_datetime_value(
        quote_snapshot["timestamp"],
        "data_snapshot.quote.timestamp",
    )
    quote_received_at = _snapshot_datetime_value(
        quote_snapshot["received_at"],
        "data_snapshot.quote.received_at",
    )
    quote_computed_at = _snapshot_datetime_value(
        quote_snapshot["computed_at"],
        "data_snapshot.quote.computed_at",
    )
    quote_displayed_at = _snapshot_datetime_value(
        quote_snapshot["displayed_at"],
        "data_snapshot.quote.displayed_at",
    )
    bar_times = tuple(
        _snapshot_datetime_value(
            bar["timestamp"],
            f"data_snapshot.bars[{index}].timestamp",
        )
        for index, bar in enumerate(data_snapshot["bars"])
    )
    if (
        quote_snapshot["symbol"] != symbol
        or quote_snapshot["market"] != market
        or quote_snapshot["data_status"] != data_status
        or data_snapshot["data_quality"] != normalized_quality
        or any(
            bar["symbol"] != symbol or bar["market"] != market
            for bar in data_snapshot["bars"]
        )
    ):
        raise RuntimeEvidenceContractError(
            "data snapshot disagrees with the artifact instrument or quality fields"
        )
    if (
        quote_source_time > requested_at
        or quote_received_at > requested_at
        or quote_computed_at > requested_at
        or quote_displayed_at > observed_at
        or any(value > requested_at for value in bar_times)
    ):
        raise RuntimeEvidenceContractError(
            "runtime decision snapshot contains facts unavailable at decision time"
        )
    execution_mode = _require_text(
        identity["execution_mode"], "execution_mode", maximum=64
    )
    if execution_mode != "OBSERVATIONAL_ONLY":
        raise RuntimeEvidenceContractError("Checkpoint A execution_mode must be observational")

    rules = _require_mapping(identity["execution_rules"], "execution_rules")
    _require_fields(
        rules,
        {
            "minimum_exit_session_offset",
            "execution_policy_id",
            "market_rule_id",
            "cost_schedule_id",
            "reference_price_definition",
        },
        "execution_rules",
    )
    if any(value is not None for value in rules.values()):
        raise RuntimeEvidenceContractError(
            "Checkpoint A cannot self-declare execution rules"
        )
    incomplete_reasons = identity["incomplete_reasons"]
    expected_incomplete = [
        "BAR_KNOWN_AT_AUTHORITY_PENDING",
        "COST_SCHEDULE_ID_PENDING",
        "EXECUTION_POLICY_ID_PENDING",
        "INSTRUMENT_IDENTITY_AUTHORITY_PENDING",
        "MARKET_RULE_ID_PENDING",
        "MINIMUM_EXIT_SESSION_OFFSET_PENDING",
        "REFERENCE_PRICE_DEFINITION_PENDING",
    ]
    if incomplete_reasons != expected_incomplete:
        raise RuntimeEvidenceContractError("incomplete_reasons are not canonical")
    if identity["outcome_case_status"] != "OUTCOME_EVIDENCE_PENDING":
        raise RuntimeEvidenceContractError("Checkpoint A artifact cannot open Stage 4G")
    if _require_bool(identity["auto_trade"], "auto_trade"):
        raise RuntimeEvidenceContractError("auto_trade must remain false")
    if _require_bool(
        identity["trusted_outcome_admission"], "trusted_outcome_admission"
    ):
        raise RuntimeEvidenceContractError("trusted admission must remain false")
    if _require_bool(
        identity["investment_performance_claim"], "investment_performance_claim"
    ):
        raise RuntimeEvidenceContractError("performance claim must remain false")

    normalized = {
        "schema": RUNTIME_DECISION_ARTIFACT_SCHEMA,
        "runtime_store_id": runtime_store_id,
        "runtime_signal_id": runtime_signal_id,
        "transition_event_id": transition_event_id,
        "decision_content_id": decision_content_id,
        "occurrence_dedup_id": occurrence_dedup_id,
        "upstream_occurrence_id": upstream_occurrence_id,
        "signal_version_id": signal_version_id,
        "expected_previous_signal_version_id": expected_previous_signal_version_id,
        "symbol": symbol,
        "market": market,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "model_id": model_id,
        "state": state,
        "previous_state": previous_state,
        "state_changed_at_utc": _datetime_text(state_changed_at, "state_changed_at_utc"),
        "decision_requested_at_utc": _datetime_text(requested_at, "decision_requested_at_utc"),
        "observed_at_utc": _datetime_text(observed_at, "observed_at_utc"),
        "pit_evidence_status": pit_evidence_status,
        "bar_known_at_authority": bar_known_at_authority,
        "single_active_runtime_store": True,
        "fork_detection": fork_detection,
        "external_checkpoint": external_checkpoint,
        "entry_plan": normalized_plan,
        "scores": normalized_scores,
        "data_status": data_status,
        "data_quality": normalized_quality,
        "instrument_id": instrument_id,
        "identity_fact_id": identity_fact_id,
        "data_snapshot_id": data_snapshot_id,
        "policy_id": policy_id,
        "classification_id": classification_id,
        "market_regime": market_regime,
        "sector_stage": sector_stage,
        "execution_mode": execution_mode,
        "execution_rules": dict(rules),
        "outcome_case_status": "OUTCOME_EVIDENCE_PENDING",
        "incomplete_reasons": expected_incomplete,
        "auto_trade": False,
        "trusted_outcome_admission": False,
        "investment_performance_claim": False,
        "signal_snapshot": signal_snapshot,
        "previous_signal_snapshot": previous_signal_snapshot,
        "data_snapshot": data_snapshot,
        "policy_snapshot": policy_snapshot,
        "identity_snapshot": identity_snapshot,
        "classification_snapshot": classification_snapshot,
    }
    expected_decision_content_id = _hash_document(
        {
            "schema": _DECISION_CONTENT_SCHEMA,
            **{
                name: normalized[name]
                for name in sorted(_DECISION_CONTENT_FIELDS)
            },
        }
    )
    if decision_content_id != expected_decision_content_id:
        raise RuntimeEvidenceContractError("decision_content_id mismatch")
    expected_occurrence_dedup_id = _hash_document(
        {
            "schema": _TRANSITION_OCCURRENCE_SCHEMA,
            "runtime_store_id": runtime_store_id,
            "runtime_signal_id": runtime_signal_id,
            "from_state": previous_state,
            "to_state": state,
            "state_changed_at_utc": normalized["state_changed_at_utc"],
            "decision_requested_at_utc": normalized["decision_requested_at_utc"],
            "decision_content_id": decision_content_id,
            "upstream_occurrence_id": upstream_occurrence_id,
        }
    )
    if occurrence_dedup_id != expected_occurrence_dedup_id:
        raise RuntimeEvidenceContractError("occurrence_dedup_id mismatch")
    return normalized


def _normalize_draft_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    document = _require_mapping(identity, "runtime decision draft")
    _require_fields(document, _DRAFT_IDENTITY_FIELDS, "runtime decision draft")
    normalized = _validate_identity(
        {**document, "transition_event_id": _ZERO_SHA256},
        allow_placeholder_transition=True,
    )
    normalized.pop("transition_event_id")
    return normalized


@dataclass(frozen=True, slots=True)
class RuntimeDecisionDraft:
    _identity_json: str
    decision_content_id: str
    occurrence_dedup_id: str

    def __post_init__(self) -> None:
        identity = _strict_json_loads(self._identity_json.encode("utf-8"))
        normalized = _normalize_draft_identity(identity)
        if _canonical_json_bytes(normalized).decode("utf-8") != self._identity_json:
            raise RuntimeEvidenceContractError("runtime decision draft is not canonical")
        if self.decision_content_id != normalized["decision_content_id"]:
            raise RuntimeEvidenceContractError("draft decision_content_id mismatch")
        if self.occurrence_dedup_id != normalized["occurrence_dedup_id"]:
            raise RuntimeEvidenceContractError("draft occurrence_dedup_id mismatch")

    @classmethod
    def create(cls, identity: Mapping[str, Any]) -> RuntimeDecisionDraft:
        normalized = _normalize_draft_identity(identity)
        identity_json = _canonical_json_bytes(normalized).decode("utf-8")
        return cls(
            identity_json,
            str(normalized["decision_content_id"]),
            str(normalized["occurrence_dedup_id"]),
        )

    def identity_dict(self) -> dict[str, Any]:
        return dict(_strict_json_loads(self._identity_json.encode("utf-8")))

    def to_artifact(self, *, transition_event_id: str) -> RuntimeDecisionArtifact:
        return build_runtime_decision_artifact(
            draft=self,
            transition_event_id=transition_event_id,
        )


@dataclass(frozen=True, slots=True)
class RuntimeDecisionArtifact:
    _identity_json: str
    artifact_id: str
    runtime_episode_fact_id: str

    def __post_init__(self) -> None:
        identity = _strict_json_loads(self._identity_json.encode("utf-8"))
        normalized = _validate_identity(identity)
        if _canonical_json_bytes(normalized).decode("utf-8") != self._identity_json:
            raise RuntimeEvidenceContractError("artifact identity is not canonical")
        expected = _hash_document(normalized)
        if self.artifact_id != expected:
            raise RuntimeEvidenceContractError("artifact_id mismatch")
        if self.runtime_episode_fact_id != expected:
            raise RuntimeEvidenceContractError(
                "runtime_episode_fact_id must equal artifact_id"
            )

    @classmethod
    def create(cls, identity: Mapping[str, Any]) -> RuntimeDecisionArtifact:
        normalized = _validate_identity(_require_mapping(identity, "artifact identity"))
        identity_json = _canonical_json_bytes(normalized).decode("utf-8")
        artifact_id = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        return cls(identity_json, artifact_id, artifact_id)

    @classmethod
    def from_dict(cls, value: object) -> RuntimeDecisionArtifact:
        document = _require_mapping(value, "runtime artifact")
        _require_fields(
            document,
            {*_EXPECTED_IDENTITY_FIELDS, "artifact_id", "runtime_episode_fact_id"},
            "runtime artifact",
        )
        identity = {
            key: item
            for key, item in document.items()
            if key not in {"artifact_id", "runtime_episode_fact_id"}
        }
        artifact = cls.create(identity)
        if artifact.artifact_id != _require_sha256(document["artifact_id"], "artifact_id"):
            raise RuntimeEvidenceContractError("artifact_id mismatch")
        if artifact.runtime_episode_fact_id != _require_sha256(
            document["runtime_episode_fact_id"], "runtime_episode_fact_id"
        ):
            raise RuntimeEvidenceContractError("runtime_episode_fact_id mismatch")
        if artifact.as_dict() != dict(document):
            raise RuntimeEvidenceContractError("runtime artifact canonical form is invalid")
        return artifact

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> RuntimeDecisionArtifact:
        return cls.from_dict(_strict_json_loads(raw))

    def identity_dict(self) -> dict[str, Any]:
        return dict(_strict_json_loads(self._identity_json.encode("utf-8")))

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "artifact_id": self.artifact_id,
            "runtime_episode_fact_id": self.runtime_episode_fact_id,
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def outcome_case_status(self) -> str:
        return str(self.identity_dict()["outcome_case_status"])

    @property
    def decision_content_id(self) -> str:
        return str(self.identity_dict()["decision_content_id"])

    @property
    def occurrence_dedup_id(self) -> str:
        return str(self.identity_dict()["occurrence_dedup_id"])


def _strategy_snapshot(bundle: ConfigBundle, strategy_id: str) -> dict[str, Any]:
    mapping = {
        "S1": bundle.strategies.s1,
        "S1_BREAKOUT": bundle.strategies.s1,
        "S2": bundle.strategies.s2,
        "S2_PULLBACK": bundle.strategies.s2,
        "S3": bundle.strategies.s3,
        "S3_EVENT": bundle.strategies.s3,
    }
    config = mapping.get(strategy_id)
    return {
        "strategy_id": strategy_id,
        "config": None if config is None else _strict_snapshot_value(config),
    }


def _runtime_snapshot_bundle(
    *,
    signal: T.Signal,
    previous_signal: T.Signal | None,
    quote: T.Quote,
    bars: Sequence[T.Bar],
    data_quality: T.DataQuality,
    bundle: ConfigBundle,
    instrument_metadata: dict[str, Any],
) -> dict[str, Any]:
    if type(signal) is not T.Signal or type(signal.scores) is not T.ScoreSet:
        raise RuntimeEvidenceContractError(
            "signal must carry the exact runtime Signal and ScoreSet types"
        )
    if previous_signal is not None and type(previous_signal) is not T.Signal:
        raise RuntimeEvidenceContractError(
            "previous_signal must be the exact runtime Signal type or None"
        )
    if type(quote) is not T.Quote or type(data_quality) is not T.DataQuality:
        raise RuntimeEvidenceContractError(
            "quote and data_quality must be exact runtime types"
        )
    if type(bundle) is not ConfigBundle:
        raise RuntimeEvidenceContractError("bundle must be ConfigBundle")
    if type(bars) not in (list, tuple) or len(bars) > _MAX_BAR_COUNT:
        raise RuntimeEvidenceContractError("bars must be a bounded exact list or tuple")
    if type(instrument_metadata) is not dict:
        raise RuntimeEvidenceContractError("instrument_metadata must be an exact dict")
    if (
        type(signal.market) is not T.Market
        or type(signal.state) is not T.SignalState
        or (
            signal.previous_state is not None
            and type(signal.previous_state) is not T.SignalState
        )
        or type(signal.data_status) is not T.DataStatus
        or type(quote.market) is not T.Market
        or type(quote.data_status) is not T.DataStatus
        or type(data_quality.status) is not T.QualityStatus
    ):
        raise RuntimeEvidenceContractError(
            "runtime decision enums must use exact project types"
        )
    if any(
        type(bar) is not T.Bar
        or type(bar.market) is not T.Market
        or bar.symbol != signal.symbol
        or bar.market is not signal.market
        for bar in bars
    ):
        raise RuntimeEvidenceContractError("bar instrument identity mismatch")
    if signal.symbol != quote.symbol or signal.market is not quote.market:
        raise RuntimeEvidenceContractError("signal and quote instrument identity mismatch")
    if signal.data_status is not quote.data_status:
        raise RuntimeEvidenceContractError("signal and quote data status mismatch")
    if previous_signal is None:
        if signal.previous_state is not None:
            raise RuntimeEvidenceContractError(
                "new runtime decision cannot claim a previous signal state"
            )
    elif (
        previous_signal.signal_id != signal.signal_id
        or previous_signal.symbol != signal.symbol
        or previous_signal.market is not signal.market
        or signal.previous_state is not previous_signal.state
    ):
        raise RuntimeEvidenceContractError(
            "previous runtime signal disagrees with the decision transition"
        )
    strategy_snapshot = _strategy_snapshot(bundle, signal.strategy_id)
    identity_snapshot = {
        "schema": _IDENTITY_SNAPSHOT_SCHEMA,
        "symbol": signal.symbol,
        "market": signal.market.value,
        "metadata": _strict_snapshot_value(instrument_metadata),
    }
    data_snapshot = {
        "schema": _DATA_SNAPSHOT_SCHEMA,
        "quote": _strict_snapshot_value(quote),
        "bars": [_strict_snapshot_value(item) for item in bars],
        "data_quality": _strict_snapshot_value(data_quality),
    }
    policy_snapshot = {
        "schema": _POLICY_SNAPSHOT_SCHEMA,
        "strategy": strategy_snapshot,
        "risk": _strict_snapshot_value(bundle.risk),
        "market": _strict_snapshot_value(
            {
                T.Market.A: bundle.markets.a,
                T.Market.HK: bundle.markets.hk,
                T.Market.US: bundle.markets.us,
            }[signal.market]
        ),
        "price_limit_version": bundle.markets.price_limit_version,
    }
    classification_snapshot = {
        "schema": _CLASSIFICATION_SNAPSHOT_SCHEMA,
        "market_regime": signal.market_regime,
        "sector_stage": signal.sector_stage,
    }
    return {
        "signal_snapshot": _signal_snapshot(
            signal,
            allow_legacy_naive_datetime=False,
        ),
        "previous_signal_snapshot": (
            None
            if previous_signal is None
            else _signal_snapshot(
                previous_signal,
                allow_legacy_naive_datetime=False,
            )
        ),
        "data_snapshot": data_snapshot,
        "policy_snapshot": policy_snapshot,
        "identity_snapshot": identity_snapshot,
        "classification_snapshot": classification_snapshot,
    }


def build_runtime_decision_draft(
    *,
    runtime_store_id: str,
    signal: T.Signal,
    previous_signal: T.Signal | None,
    quote: T.Quote,
    bars: Sequence[T.Bar],
    data_quality: T.DataQuality,
    bundle: ConfigBundle,
    decision_requested_at: datetime,
    observed_at: datetime,
    instrument_metadata: dict[str, Any] | None = None,
    upstream_occurrence_id: str | None = None,
) -> RuntimeDecisionDraft:
    runtime_store = _require_sha256(runtime_store_id, "runtime_store_id")
    requested = require_utc_clock_value(decision_requested_at, "decision_requested_at")
    observed = require_utc_clock_value(observed_at, "observed_at")
    if requested > observed:
        raise RuntimeEvidenceContractError("runtime decision time exceeds observation")
    metadata = {} if instrument_metadata is None else instrument_metadata
    if type(metadata) is not dict:
        raise RuntimeEvidenceContractError("instrument_metadata must be an exact dict")
    if upstream_occurrence_id is not None:
        raise RuntimeEvidenceContractError(
            "upstream occurrence authority is not configured in Checkpoint A",
            code="UPSTREAM_OCCURRENCE_AUTHORITY_NOT_CONFIGURED",
        )
    upstream = None
    first_snapshots = _runtime_snapshot_bundle(
        signal=signal,
        previous_signal=previous_signal,
        quote=quote,
        bars=bars,
        data_quality=data_quality,
        bundle=bundle,
        instrument_metadata=metadata,
    )
    second_snapshots = _runtime_snapshot_bundle(
        signal=signal,
        previous_signal=previous_signal,
        quote=quote,
        bars=bars,
        data_quality=data_quality,
        bundle=bundle,
        instrument_metadata=metadata,
    )
    if first_snapshots != second_snapshots:
        raise RuntimeEvidenceContractError(
            "runtime decision inputs changed while snapshotting",
            code="MUTABLE_RUNTIME_DECISION_INPUT",
        )
    signal_snapshot = first_snapshots["signal_snapshot"]
    previous_signal_snapshot = first_snapshots["previous_signal_snapshot"]
    data_snapshot = first_snapshots["data_snapshot"]
    policy_snapshot = first_snapshots["policy_snapshot"]
    identity_snapshot = first_snapshots["identity_snapshot"]
    classification_snapshot = first_snapshots["classification_snapshot"]
    signal_document = signal_snapshot["signal"]
    state_changed = _datetime_from_text(
        signal_document["state_changed_at"]["value"],
        "state_changed_at",
    )
    if state_changed > observed:
        raise RuntimeEvidenceContractError("runtime decision time exceeds observation")
    signal_version_id = _hash_document(signal_snapshot)
    expected_previous_signal_version_id = (
        None
        if previous_signal_snapshot is None
        else _hash_document(previous_signal_snapshot)
    )
    identity_fact_id = _hash_document(identity_snapshot)
    data_snapshot_id = _hash_document(data_snapshot)
    policy_id = _hash_document(policy_snapshot)
    classification_id = _hash_document(classification_snapshot)
    strategy_snapshot = policy_snapshot["strategy"]
    strategy_version = f"runtime-strategy-config-v1:{_hash_document(strategy_snapshot)}"
    scores = signal_document["scores"]
    if type(scores) is not dict:
        raise RuntimeEvidenceContractError("runtime signal snapshot has no score evidence")
    identity = {
        "schema": RUNTIME_DECISION_ARTIFACT_SCHEMA,
        "runtime_store_id": runtime_store,
        "runtime_signal_id": signal_document["signal_id"],
        "decision_content_id": "0" * _SHA256_LENGTH,
        "occurrence_dedup_id": "0" * _SHA256_LENGTH,
        "upstream_occurrence_id": upstream,
        "signal_version_id": signal_version_id,
        "expected_previous_signal_version_id": expected_previous_signal_version_id,
        "symbol": signal_document["symbol"],
        "market": signal_document["market"],
        "strategy_id": signal_document["strategy_id"],
        "strategy_version": strategy_version,
        "model_id": None,
        "state": signal_document["state"],
        "previous_state": signal_document["previous_state"],
        "state_changed_at_utc": _datetime_text(state_changed, "state_changed_at"),
        "decision_requested_at_utc": _datetime_text(requested, "decision_requested_at"),
        "observed_at_utc": _datetime_text(observed, "observed_at"),
        "pit_evidence_status": "RUNTIME_MEMORY_ONLY",
        "bar_known_at_authority": "NOT_AVAILABLE",
        "single_active_runtime_store": True,
        "fork_detection": "LOCAL_APPEND_CHAIN_ONLY",
        "external_checkpoint": "NOT_IMPLEMENTED",
        "entry_plan": {
            "entry_low": signal_document["entry_low"],
            "entry_high": signal_document["entry_high"],
            "trigger_price": signal_document["trigger_price"],
            "invalidation_price": signal_document["invalidation_price"],
            "target_1": signal_document["target_1"],
            "target_2": signal_document["target_2"],
            "reward_risk": signal_document["reward_risk"],
        },
        "scores": scores,
        "data_status": signal_document["data_status"],
        "data_quality": data_snapshot["data_quality"],
        "instrument_id": f"UNRESOLVED_RUNTIME_IDENTITY:{identity_fact_id}",
        "identity_fact_id": identity_fact_id,
        "data_snapshot_id": data_snapshot_id,
        "policy_id": policy_id,
        "classification_id": classification_id,
        "market_regime": signal_document["market_regime"],
        "sector_stage": signal_document["sector_stage"],
        "execution_mode": "OBSERVATIONAL_ONLY",
        "execution_rules": {
            "minimum_exit_session_offset": None,
            "execution_policy_id": None,
            "market_rule_id": None,
            "cost_schedule_id": None,
            "reference_price_definition": None,
        },
        "outcome_case_status": "OUTCOME_EVIDENCE_PENDING",
        "incomplete_reasons": [
            "BAR_KNOWN_AT_AUTHORITY_PENDING",
            "COST_SCHEDULE_ID_PENDING",
            "EXECUTION_POLICY_ID_PENDING",
            "INSTRUMENT_IDENTITY_AUTHORITY_PENDING",
            "MARKET_RULE_ID_PENDING",
            "MINIMUM_EXIT_SESSION_OFFSET_PENDING",
            "REFERENCE_PRICE_DEFINITION_PENDING",
        ],
        "auto_trade": False,
        "trusted_outcome_admission": False,
        "investment_performance_claim": False,
        "signal_snapshot": signal_snapshot,
        "previous_signal_snapshot": previous_signal_snapshot,
        "data_snapshot": data_snapshot,
        "policy_snapshot": policy_snapshot,
        "identity_snapshot": identity_snapshot,
        "classification_snapshot": classification_snapshot,
    }
    identity["decision_content_id"] = _hash_document(
        {
            "schema": _DECISION_CONTENT_SCHEMA,
            **{
                name: identity[name]
                for name in sorted(_DECISION_CONTENT_FIELDS)
            },
        }
    )
    identity["occurrence_dedup_id"] = _hash_document(
        {
            "schema": _TRANSITION_OCCURRENCE_SCHEMA,
            "runtime_store_id": runtime_store,
            "runtime_signal_id": signal_document["signal_id"],
            "from_state": signal_document["previous_state"],
            "to_state": signal_document["state"],
            "state_changed_at_utc": identity["state_changed_at_utc"],
            "decision_requested_at_utc": identity["decision_requested_at_utc"],
            "decision_content_id": identity["decision_content_id"],
            "upstream_occurrence_id": upstream,
        }
    )
    return RuntimeDecisionDraft.create(identity)


def build_runtime_decision_artifact(
    *,
    draft: RuntimeDecisionDraft,
    transition_event_id: str,
) -> RuntimeDecisionArtifact:
    if type(draft) is not RuntimeDecisionDraft:
        raise RuntimeEvidenceContractError(
            "draft must be the exact RuntimeDecisionDraft type"
        )
    transition = _require_sha256(transition_event_id, "transition_event_id")
    return RuntimeDecisionArtifact.create(
        {**draft.identity_dict(), "transition_event_id": transition}
    )


__all__ = [
    "RUNTIME_DECISION_ARTIFACT_SCHEMA",
    "RuntimeDecisionArtifact",
    "RuntimeDecisionDraft",
    "RuntimeEvidenceContractError",
    "SystemUtcClock",
    "UtcClock",
    "build_runtime_decision_artifact",
    "build_runtime_decision_draft",
    "require_utc_clock_value",
    "runtime_signal_version_id",
]
