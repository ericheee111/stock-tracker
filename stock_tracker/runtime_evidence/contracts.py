from __future__ import annotations

import hashlib
import json
import math
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from stock_tracker.core import types as T
from stock_tracker.core.config import ConfigBundle

RUNTIME_DECISION_ARTIFACT_SCHEMA = "stage4g1-runtime-decision-artifact-v1"
_TRANSITION_OCCURRENCE_SCHEMA = "stage4g1-runtime-transition-occurrence-v1"
_DATA_SNAPSHOT_SCHEMA = "stage4g1-runtime-decision-inputs-v1"
_POLICY_SNAPSHOT_SCHEMA = "stage4g1-runtime-policy-snapshot-v1"
_IDENTITY_SNAPSHOT_SCHEMA = "stage4g1-runtime-instrument-identity-v1"
_CLASSIFICATION_SNAPSHOT_SCHEMA = "stage4g1-runtime-classification-v1"
_SHA256_LENGTH = 64
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_REASON_COUNT = 256
_SIGNAL_STATES = frozenset(item.value for item in T.SignalState)
_DATA_STATUSES = frozenset(item.value for item in T.DataStatus)
_QUALITY_STATUSES = frozenset(item.value for item in T.QualityStatus)
_EXPECTED_IDENTITY_FIELDS = {
    "schema",
    "runtime_signal_id",
    "transition_event_id",
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


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise RuntimeEvidenceContractError(f"{name} must be an object with string keys")
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


def _strict_snapshot_value(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RuntimeEvidenceContractError("snapshot contains non-finite number")
        return value
    if isinstance(value, datetime):
        return _explicit_datetime(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _strict_snapshot_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise RuntimeEvidenceContractError("snapshot mapping keys must be strings")
        return {key: _strict_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_strict_snapshot_value(item) for item in value]
    raise RuntimeEvidenceContractError(
        f"snapshot contains unsupported type {type(value).__name__}"
    )


def _validate_reasons(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_REASON_COUNT:
        raise RuntimeEvidenceContractError(f"{name} must be a bounded array")
    return [_require_text(item, name, maximum=1024) for item in value]


def _validate_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    _require_fields(identity, _EXPECTED_IDENTITY_FIELDS, "artifact identity")
    if identity["schema"] != RUNTIME_DECISION_ARTIFACT_SCHEMA:
        raise RuntimeEvidenceContractError("runtime artifact schema is invalid")
    runtime_signal_id = _require_text(identity["runtime_signal_id"], "runtime_signal_id")
    transition_event_id = _require_sha256(
        identity["transition_event_id"], "transition_event_id"
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
    classification_id = identity["classification_id"]
    if classification_id is not None:
        classification_id = _require_sha256(classification_id, "classification_id")
    market_regime = _require_text(
        identity["market_regime"], "market_regime", maximum=128, allow_empty=True
    )
    sector_stage = _require_text(
        identity["sector_stage"], "sector_stage", maximum=128, allow_empty=True
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

    return {
        "schema": RUNTIME_DECISION_ARTIFACT_SCHEMA,
        "runtime_signal_id": runtime_signal_id,
        "transition_event_id": transition_event_id,
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
    }


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


def build_runtime_decision_artifact(
    *,
    signal: T.Signal,
    quote: T.Quote,
    bars: Sequence[T.Bar],
    data_quality: T.DataQuality,
    bundle: ConfigBundle,
    decision_requested_at: datetime,
    observed_at: datetime,
    instrument_metadata: Mapping[str, Any] | None = None,
) -> RuntimeDecisionArtifact:
    if type(signal) is not T.Signal or type(signal.scores) is not T.ScoreSet:
        raise RuntimeEvidenceContractError("signal must carry the exact runtime ScoreSet")
    if type(quote) is not T.Quote or type(data_quality) is not T.DataQuality:
        raise RuntimeEvidenceContractError("quote and data_quality must be exact runtime types")
    if type(bundle) is not ConfigBundle:
        raise RuntimeEvidenceContractError("bundle must be ConfigBundle")
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
        raise RuntimeEvidenceContractError("runtime decision enums must use exact types")
    if signal.symbol != quote.symbol or signal.market is not quote.market:
        raise RuntimeEvidenceContractError("signal and quote instrument identity mismatch")
    if signal.data_status is not quote.data_status:
        raise RuntimeEvidenceContractError("signal and quote data status mismatch")
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes, bytearray)):
        raise RuntimeEvidenceContractError("bars must be a sequence")
    if any(
        type(bar) is not T.Bar
        or bar.symbol != signal.symbol
        or type(bar.market) is not T.Market
        or bar.market is not signal.market
        for bar in bars
    ):
        raise RuntimeEvidenceContractError("bar instrument identity mismatch")
    requested = require_utc_clock_value(decision_requested_at, "decision_requested_at")
    observed = require_utc_clock_value(observed_at, "observed_at")
    state_changed = require_utc_clock_value(signal.state_changed_at, "state_changed_at")
    if requested > observed or state_changed > observed:
        raise RuntimeEvidenceContractError("runtime decision time exceeds observation")
    metadata = {} if instrument_metadata is None else dict(
        _require_mapping(instrument_metadata, "instrument_metadata")
    )
    strategy_snapshot = _strategy_snapshot(bundle, signal.strategy_id)
    strategy_version = f"runtime-strategy-config-v1:{_hash_document(strategy_snapshot)}"
    identity_snapshot = {
        "schema": _IDENTITY_SNAPSHOT_SCHEMA,
        "symbol": signal.symbol,
        "market": signal.market.value,
        "metadata": _strict_snapshot_value(metadata),
    }
    identity_fact_id = _hash_document(identity_snapshot)
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
    transition_event_id = _hash_document(
        {
            "schema": _TRANSITION_OCCURRENCE_SCHEMA,
            "system_nonce": secrets.token_hex(32),
            "runtime_signal_id": signal.signal_id,
            "state": signal.state.value,
            "decision_requested_at_utc": _datetime_text(requested, "decision_requested_at"),
        }
    )
    scores = signal.scores
    identity = {
        "schema": RUNTIME_DECISION_ARTIFACT_SCHEMA,
        "runtime_signal_id": signal.signal_id,
        "transition_event_id": transition_event_id,
        "symbol": signal.symbol,
        "market": signal.market.value,
        "strategy_id": signal.strategy_id,
        "strategy_version": strategy_version,
        "model_id": None,
        "state": signal.state.value,
        "previous_state": None if signal.previous_state is None else signal.previous_state.value,
        "state_changed_at_utc": _datetime_text(state_changed, "state_changed_at"),
        "decision_requested_at_utc": _datetime_text(requested, "decision_requested_at"),
        "observed_at_utc": _datetime_text(observed, "observed_at"),
        "entry_plan": {
            "entry_low": signal.entry_low,
            "entry_high": signal.entry_high,
            "trigger_price": signal.trigger_price,
            "invalidation_price": signal.invalidation_price,
            "target_1": signal.target_1,
            "target_2": signal.target_2,
            "reward_risk": signal.reward_risk,
        },
        "scores": {
            "opportunity": scores.opportunity,
            "timing": scores.timing,
            "risk": scores.risk,
            "confidence": scores.confidence,
            "success_probability": scores.success_probability,
            "positive_reasons": list(scores.positive_reasons),
            "negative_reasons": list(scores.negative_reasons),
        },
        "data_status": signal.data_status.value,
        "data_quality": {
            "status": data_quality.status.value,
            "score": data_quality.score,
            "reasons": list(data_quality.reasons),
        },
        "instrument_id": f"UNRESOLVED_RUNTIME_IDENTITY:{identity_fact_id}",
        "identity_fact_id": identity_fact_id,
        "data_snapshot_id": _hash_document(data_snapshot),
        "policy_id": _hash_document(policy_snapshot),
        "classification_id": _hash_document(classification_snapshot),
        "market_regime": signal.market_regime,
        "sector_stage": signal.sector_stage,
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
    }
    return RuntimeDecisionArtifact.create(identity)


__all__ = [
    "RUNTIME_DECISION_ARTIFACT_SCHEMA",
    "RuntimeDecisionArtifact",
    "RuntimeEvidenceContractError",
    "SystemUtcClock",
    "UtcClock",
    "build_runtime_decision_artifact",
    "require_utc_clock_value",
]
