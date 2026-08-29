"""Append-only terminal outcome evidence ledger.

The ledger is deliberately separate from ``data/stock_tracker.db``.  It stores
one immutable JSON record per terminal :class:`SignalOutcome`, binds records in
a global SHA-256 chain, and keeps only query/index metadata in a dedicated
SQLite catalog. Paper, synthetic, and live observations may be retained for
diagnosis, but Stage 4F never treats caller-declared verification as trusted
admission to a real Strategy Scoreboard.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from stock_tracker.core.types import Market
from stock_tracker.quant.backtest.market_rules import TradeSide
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.outcomes import (
    OutcomeBucketMetrics,
    OutcomeContractError,
    OutcomeEvidenceOrigin,
    OutcomeFillEvidence,
    OutcomeMetrics,
    OutcomePathPoint,
    OutcomeScoreboardPolicy,
    OutcomeState,
    OutcomeTerminalReason,
    SignalOutcome,
    StrategyScoreboard,
    StrategyScoreboardMetrics,
    TradeIntentEvidence,
)
from stock_tracker.quant.core.time import ensure_aware, to_utc
from stock_tracker.quant.data.bar_artifact import DataTrustTier

LEDGER_SCHEMA = "stage4f-outcome-ledger-v1"
RECORD_SCHEMA = "stage4f-outcome-ledger-record-v1"
OUTCOME_DOCUMENT_SCHEMA = "stage4f-signal-outcome-document-v1"
AUDIT_SCHEMA = "stage4f-outcome-ledger-audit-v1"
SCOREBOARD_SNAPSHOT_SCHEMA = "stage4f-outcome-scoreboard-snapshot-v1"
TRUSTED_OUTCOME_AUTHORITY_CONFIGURED = False
DEFAULT_RECORD_ROOT = Path("data/outcome-ledger-records")
DEFAULT_CATALOG_PATH = Path("data/outcome-ledger.db")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_ZERO_HASH = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CANONICAL_DECIMAL = re.compile(
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_CATALOG_META_COLUMNS = (
    ("key", "TEXT", 0, 1),
    ("value", "TEXT", 1, 0),
)
_CATALOG_RECORD_COLUMNS = (
    ("append_order", "INTEGER", 0, 1),
    ("record_hash", "TEXT", 1, 0),
    ("previous_record_hash", "TEXT", 1, 0),
    ("outcome_id", "TEXT", 1, 0),
    ("signal_id", "TEXT", 1, 0),
    ("lane", "TEXT", 1, 0),
    ("market", "TEXT", 1, 0),
    ("strategy_id", "TEXT", 1, 0),
    ("strategy_version", "TEXT", 1, 0),
    ("horizon_sessions", "INTEGER", 1, 0),
    ("model_id", "TEXT", 0, 0),
    ("evidence_tier", "TEXT", 1, 0),
    ("origin", "TEXT", 1, 0),
    ("verified", "INTEGER", 1, 0),
    ("outcome_contract_eligible", "INTEGER", 1, 0),
    ("recorded_at", "TEXT", 1, 0),
    ("ingested_at", "TEXT", 1, 0),
    ("recorded_by", "TEXT", 1, 0),
    ("record_file", "TEXT", 1, 0),
    ("record_file_sha256", "TEXT", 1, 0),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OutcomeLedgerError(RuntimeError):
    """Raised when outcome evidence or ledger integrity is invalid."""


class OutcomeLedgerConflict(OutcomeLedgerError):
    """Raised when one signal is presented with two different outcomes."""


class OutcomeLedgerLane(StrEnum):
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    LIVE_CANDIDATE = "LIVE_CANDIDATE"


class OutcomeLedgerDisposition(StrEnum):
    APPENDED = "APPENDED"
    IDEMPOTENT = "IDEMPOTENT"


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OutcomeLedgerError(f"{name} must be an object with string keys")
    return value


def _require_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise OutcomeLedgerError(
            f"{name} field set is invalid; missing={missing}; extra={extra}"
        )


def _require_text(value: object, name: str, *, max_length: int = 4096) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    ):
        raise OutcomeLedgerError(f"{name} must be a safe non-empty trimmed string")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_safe_token(value: object, name: str) -> str:
    text = _require_text(value, name, max_length=128)
    if _SAFE_TOKEN.fullmatch(text) is None:
        raise OutcomeLedgerError(f"{name} must be a safe token")
    return text


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise OutcomeLedgerError(f"{name} must be boolean")
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
        raise OutcomeLedgerError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name, max_length=64)
    if _SHA256.fullmatch(text) is None:
        raise OutcomeLedgerError(f"{name} must be lowercase SHA-256")
    return text


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise OutcomeLedgerError("decimal value must be finite Decimal")
    _sign, digits, exponent = value.as_tuple()
    if len(digits) > 256 or abs(exponent) > 256:
        raise OutcomeLedgerError("decimal value exceeds the Stage 4F canonical bound")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    canonical = "0" if value == 0 else text
    if len(canonical) > 256:
        raise OutcomeLedgerError("decimal value exceeds the Stage 4F canonical bound")
    return canonical


def _require_decimal(value: object, name: str) -> Decimal:
    text = _require_text(value, name, max_length=256)
    if _CANONICAL_DECIMAL.fullmatch(text) is None or text == "-0":
        raise OutcomeLedgerError(f"{name} must use canonical finite decimal text")
    try:
        decimal_value = Decimal(text)
    except InvalidOperation as exc:
        raise OutcomeLedgerError(f"{name} is not a valid decimal") from exc
    if not decimal_value.is_finite() or _decimal_text(decimal_value) != text:
        raise OutcomeLedgerError(f"{name} must use canonical finite decimal text")
    return decimal_value


def _require_optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _require_decimal(value, name)


def _datetime_text(value: datetime) -> str:
    return (
        to_utc(ensure_aware(value, "datetime"))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name, max_length=64)
    if not text.endswith("Z"):
        raise OutcomeLedgerError(f"{name} must be canonical UTC with Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise OutcomeLedgerError(f"{name} must be ISO-8601") from exc
    canonical = _datetime_text(parsed)
    if canonical != text:
        raise OutcomeLedgerError(f"{name} must use canonical UTC representation")
    return parsed


def _require_list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise OutcomeLedgerError(f"{name} must be an array")
    return value


def _strict_json_loads(raw: bytes, name: str) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_JSON_BYTES:
        raise OutcomeLedgerError(f"{name} must be non-empty and at most 16 MiB")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise OutcomeLedgerError(f"{name} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OutcomeLedgerError(f"{name} must use UTF-8") from exc

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise OutcomeLedgerError(f"{name} contains duplicate JSON keys")
            result[key] = item
        return result

    def reject_constant(token: str) -> None:
        raise OutcomeLedgerError(f"{name} contains non-finite token: {token}")

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OutcomeLedgerError(f"{name} is not valid JSON") from exc


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise OutcomeLedgerError("canonical JSON exceeds the Stage 4F size bound")
    return raw


def _intent_to_dict(intent: TradeIntentEvidence) -> dict[str, Any]:
    if not isinstance(intent, TradeIntentEvidence):
        raise OutcomeLedgerError("intent must be TradeIntentEvidence")
    return {
        "schema": "trade-intent-evidence-v1",
        "symbol": intent.symbol,
        "market": intent.market.value,
        "side": intent.side.value,
        "requested_at": _datetime_text(intent.requested_at),
        "requested_quantity": intent.requested_quantity,
        "decision_snapshot_id": intent.decision_snapshot_id,
        "execution_policy_id": intent.execution_policy_id,
        "intent_id": intent.intent_id,
    }


def _intent_from_dict(value: object) -> TradeIntentEvidence:
    document = _require_mapping(value, "trade intent")
    _require_fields(
        document,
        {
            "schema",
            "symbol",
            "market",
            "side",
            "requested_at",
            "requested_quantity",
            "decision_snapshot_id",
            "execution_policy_id",
            "intent_id",
        },
        "trade intent",
    )
    if document["schema"] != "trade-intent-evidence-v1":
        raise OutcomeLedgerError("trade intent schema is invalid")
    try:
        intent = TradeIntentEvidence(
            symbol=_require_text(document["symbol"], "intent symbol"),
            market=Market(_require_text(document["market"], "intent market")),
            side=TradeSide(_require_text(document["side"], "intent side")),
            requested_at=_require_datetime(document["requested_at"], "requested_at"),
            requested_quantity=_require_int(
                document["requested_quantity"],
                "requested_quantity",
                minimum=1,
            ),
            decision_snapshot_id=_require_sha256(
                document["decision_snapshot_id"],
                "decision_snapshot_id",
            ),
            execution_policy_id=_require_sha256(
                document["execution_policy_id"],
                "execution_policy_id",
            ),
        )
    except (ValueError, OutcomeContractError) as exc:
        raise OutcomeLedgerError("trade intent contract is invalid") from exc
    if _intent_to_dict(intent) != dict(document):
        raise OutcomeLedgerError("trade intent identity or canonical form is invalid")
    return intent


def _fill_to_dict(fill: OutcomeFillEvidence) -> dict[str, Any]:
    if not isinstance(fill, OutcomeFillEvidence):
        raise OutcomeLedgerError("fill must be OutcomeFillEvidence")
    return {
        "schema": "outcome-fill-evidence-v1",
        "intent_id": fill.intent_id,
        "symbol": fill.symbol,
        "market": fill.market.value,
        "side": fill.side.value,
        "timestamp": _datetime_text(fill.timestamp),
        "session_index": fill.session_index,
        "quantity": fill.quantity,
        "reference_price": _decimal_text(fill.reference_price),
        "fill_price": _decimal_text(fill.fill_price),
        "explicit_cost": _decimal_text(fill.explicit_cost),
        "implicit_cost": _decimal_text(fill.implicit_cost),
        "execution_rule_id": fill.execution_rule_id,
        "cost_schedule_id": fill.cost_schedule_id,
        "raw_bar_snapshot_id": fill.raw_bar_snapshot_id,
        "fill_id": fill.fill_id,
    }


def _fill_from_dict(value: object) -> OutcomeFillEvidence:
    document = _require_mapping(value, "outcome fill")
    _require_fields(
        document,
        {
            "schema",
            "intent_id",
            "symbol",
            "market",
            "side",
            "timestamp",
            "session_index",
            "quantity",
            "reference_price",
            "fill_price",
            "explicit_cost",
            "implicit_cost",
            "execution_rule_id",
            "cost_schedule_id",
            "raw_bar_snapshot_id",
            "fill_id",
        },
        "outcome fill",
    )
    if document["schema"] != "outcome-fill-evidence-v1":
        raise OutcomeLedgerError("outcome fill schema is invalid")
    try:
        fill = OutcomeFillEvidence(
            intent_id=_require_sha256(document["intent_id"], "fill intent_id"),
            symbol=_require_text(document["symbol"], "fill symbol"),
            market=Market(_require_text(document["market"], "fill market")),
            side=TradeSide(_require_text(document["side"], "fill side")),
            timestamp=_require_datetime(document["timestamp"], "fill timestamp"),
            session_index=_require_int(document["session_index"], "session_index"),
            quantity=_require_int(document["quantity"], "fill quantity", minimum=1),
            reference_price=_require_decimal(
                document["reference_price"],
                "reference_price",
            ),
            fill_price=_require_decimal(document["fill_price"], "fill_price"),
            explicit_cost=_require_decimal(
                document["explicit_cost"],
                "explicit_cost",
            ),
            execution_rule_id=_require_sha256(
                document["execution_rule_id"],
                "execution_rule_id",
            ),
            cost_schedule_id=_require_sha256(
                document["cost_schedule_id"],
                "cost_schedule_id",
            ),
            raw_bar_snapshot_id=_require_sha256(
                document["raw_bar_snapshot_id"],
                "raw_bar_snapshot_id",
            ),
        )
    except (ValueError, OutcomeContractError) as exc:
        raise OutcomeLedgerError("outcome fill contract is invalid") from exc
    if _fill_to_dict(fill) != dict(document):
        raise OutcomeLedgerError("outcome fill identity or canonical form is invalid")
    return fill


def _path_point_to_dict(point: OutcomePathPoint) -> dict[str, Any]:
    if not isinstance(point, OutcomePathPoint):
        raise OutcomeLedgerError("path point must be OutcomePathPoint")
    return {
        "schema": "outcome-path-point-v1",
        "timestamp": _datetime_text(point.timestamp),
        "session_index": point.session_index,
        "high": _decimal_text(point.high),
        "low": _decimal_text(point.low),
        "close": _decimal_text(point.close),
        "observable": point.observable,
        "point_id": point.point_id,
    }


def _path_point_from_dict(value: object) -> OutcomePathPoint:
    document = _require_mapping(value, "outcome path point")
    _require_fields(
        document,
        {
            "schema",
            "timestamp",
            "session_index",
            "high",
            "low",
            "close",
            "observable",
            "point_id",
        },
        "outcome path point",
    )
    if document["schema"] != "outcome-path-point-v1":
        raise OutcomeLedgerError("outcome path point schema is invalid")
    try:
        point = OutcomePathPoint(
            timestamp=_require_datetime(document["timestamp"], "path timestamp"),
            session_index=_require_int(document["session_index"], "session_index"),
            high=_require_decimal(document["high"], "path high"),
            low=_require_decimal(document["low"], "path low"),
            close=_require_decimal(document["close"], "path close"),
            observable=_require_bool(document["observable"], "observable"),
        )
    except OutcomeContractError as exc:
        raise OutcomeLedgerError("outcome path point contract is invalid") from exc
    if _path_point_to_dict(point) != dict(document):
        raise OutcomeLedgerError("path point identity or canonical form is invalid")
    return point


def _metrics_to_dict(metrics: OutcomeMetrics | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    if not isinstance(metrics, OutcomeMetrics):
        raise OutcomeLedgerError("metrics must be OutcomeMetrics")
    return {
        "schema": "outcome-metrics-v1",
        "entry_all_in_unit_price": _decimal_text(metrics.entry_all_in_unit_price),
        "exit_net_unit_price": _decimal_text(metrics.exit_net_unit_price),
        "realized_r": _decimal_text(metrics.realized_r),
        "mfe_r": _decimal_text(metrics.mfe_r),
        "mae_r": _decimal_text(metrics.mae_r),
        "net_return": _decimal_text(metrics.net_return),
        "holding_sessions": metrics.holding_sessions,
        "total_cost": _decimal_text(metrics.total_cost),
        "metrics_id": metrics.metrics_id,
    }


def signal_outcome_to_dict(outcome: SignalOutcome) -> dict[str, Any]:
    """Return the canonical, fully derived Stage 4F outcome document."""

    if not isinstance(outcome, SignalOutcome):
        raise OutcomeLedgerError("outcome must be SignalOutcome")
    return {
        "schema": OUTCOME_DOCUMENT_SCHEMA,
        "signal_id": outcome.signal_id,
        "strategy_id": outcome.strategy_id,
        "strategy_version": outcome.strategy_version,
        "horizon_sessions": outcome.horizon_sessions,
        "model_id": outcome.model_id,
        "evidence_tier": outcome.evidence_tier.value,
        "symbol": outcome.symbol,
        "market": outcome.market.value,
        "instrument_id": outcome.instrument_id,
        "identity_fact_id": outcome.identity_fact_id,
        "decision_snapshot_id": outcome.decision_snapshot_id,
        "data_snapshot_id": outcome.data_snapshot_id,
        "policy_id": outcome.policy_id,
        "market_regime": outcome.market_regime,
        "classification_id": outcome.classification_id,
        "recorded_at": _datetime_text(outcome.recorded_at),
        "entry_intent": _intent_to_dict(outcome.entry_intent),
        "entry_fill": (
            None if outcome.entry_fill is None else _fill_to_dict(outcome.entry_fill)
        ),
        "exit_intent": (
            None if outcome.exit_intent is None else _intent_to_dict(outcome.exit_intent)
        ),
        "exit_fill": (
            None if outcome.exit_fill is None else _fill_to_dict(outcome.exit_fill)
        ),
        "path": [_path_point_to_dict(item) for item in outcome.path],
        "path_complete": outcome.path_complete,
        "invalidation_price": (
            None
            if outcome.invalidation_price is None
            else _decimal_text(outcome.invalidation_price)
        ),
        "terminal_reason": (
            None if outcome.terminal_reason is None else outcome.terminal_reason.value
        ),
        "origin": outcome.origin.value,
        "verified": outcome.verified,
        "synthetic_fixture_only": outcome.synthetic_fixture_only,
        "verification_evidence_ids": list(outcome.verification_evidence_ids),
        "state": outcome.state.value,
        "blockers": list(outcome.blockers),
        "risk_per_share": (
            None
            if outcome.risk_per_share is None
            else _decimal_text(outcome.risk_per_share)
        ),
        "metrics": _metrics_to_dict(outcome.metrics),
        "real_scoreboard_eligible": outcome.real_scoreboard_eligible,
        "outcome_id": outcome.outcome_id,
    }


def signal_outcome_from_dict(value: object) -> SignalOutcome:
    """Strictly reconstruct a :class:`SignalOutcome` and all derived identities."""

    document = _require_mapping(value, "signal outcome")
    _require_fields(
        document,
        {
            "schema",
            "signal_id",
            "strategy_id",
            "strategy_version",
            "horizon_sessions",
            "model_id",
            "evidence_tier",
            "symbol",
            "market",
            "instrument_id",
            "identity_fact_id",
            "decision_snapshot_id",
            "data_snapshot_id",
            "policy_id",
            "market_regime",
            "classification_id",
            "recorded_at",
            "entry_intent",
            "entry_fill",
            "exit_intent",
            "exit_fill",
            "path",
            "path_complete",
            "invalidation_price",
            "terminal_reason",
            "origin",
            "verified",
            "synthetic_fixture_only",
            "verification_evidence_ids",
            "state",
            "blockers",
            "risk_per_share",
            "metrics",
            "real_scoreboard_eligible",
            "outcome_id",
        },
        "signal outcome",
    )
    if document["schema"] != OUTCOME_DOCUMENT_SCHEMA:
        raise OutcomeLedgerError("signal outcome document schema is invalid")
    entry_fill_value = document["entry_fill"]
    exit_intent_value = document["exit_intent"]
    exit_fill_value = document["exit_fill"]
    terminal_value = document["terminal_reason"]
    evidence_values = _require_list(
        document["verification_evidence_ids"],
        "verification_evidence_ids",
    )
    path_values = _require_list(document["path"], "path")
    try:
        outcome = SignalOutcome(
            signal_id=_require_text(document["signal_id"], "signal_id"),
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
            evidence_tier=DataTrustTier(
                _require_text(document["evidence_tier"], "evidence_tier")
            ),
            symbol=_require_text(document["symbol"], "symbol"),
            market=Market(_require_text(document["market"], "market")),
            instrument_id=_require_text(document["instrument_id"], "instrument_id"),
            identity_fact_id=_require_sha256(
                document["identity_fact_id"],
                "identity_fact_id",
            ),
            decision_snapshot_id=_require_sha256(
                document["decision_snapshot_id"],
                "decision_snapshot_id",
            ),
            data_snapshot_id=_require_sha256(
                document["data_snapshot_id"],
                "data_snapshot_id",
            ),
            policy_id=_require_sha256(document["policy_id"], "policy_id"),
            market_regime=_require_text(document["market_regime"], "market_regime"),
            classification_id=_require_optional_text(
                document["classification_id"],
                "classification_id",
            ),
            recorded_at=_require_datetime(document["recorded_at"], "recorded_at"),
            entry_intent=_intent_from_dict(document["entry_intent"]),
            entry_fill=(
                None if entry_fill_value is None else _fill_from_dict(entry_fill_value)
            ),
            exit_intent=(
                None
                if exit_intent_value is None
                else _intent_from_dict(exit_intent_value)
            ),
            exit_fill=(
                None if exit_fill_value is None else _fill_from_dict(exit_fill_value)
            ),
            path=tuple(_path_point_from_dict(item) for item in path_values),
            path_complete=_require_bool(document["path_complete"], "path_complete"),
            invalidation_price=_require_optional_decimal(
                document["invalidation_price"],
                "invalidation_price",
            ),
            terminal_reason=(
                None
                if terminal_value is None
                else OutcomeTerminalReason(
                    _require_text(terminal_value, "terminal_reason")
                )
            ),
            origin=OutcomeEvidenceOrigin(
                _require_text(document["origin"], "origin")
            ),
            verified=_require_bool(document["verified"], "verified"),
            synthetic_fixture_only=_require_bool(
                document["synthetic_fixture_only"],
                "synthetic_fixture_only",
            ),
            verification_evidence_ids=tuple(
                _require_sha256(item, "verification_evidence_id")
                for item in evidence_values
            ),
        )
    except (ValueError, OutcomeContractError) as exc:
        raise OutcomeLedgerError("signal outcome contract is invalid") from exc
    if signal_outcome_to_dict(outcome) != dict(document):
        raise OutcomeLedgerError(
            "signal outcome identity, derived fields, or canonical form is invalid"
        )
    return outcome


def signal_outcome_to_json_bytes(outcome: SignalOutcome) -> bytes:
    return _canonical_json_bytes(signal_outcome_to_dict(outcome)) + b"\n"


def signal_outcome_from_json_bytes(raw: bytes) -> SignalOutcome:
    outcome = signal_outcome_from_dict(_strict_json_loads(raw, "signal outcome JSON"))
    if raw != signal_outcome_to_json_bytes(outcome):
        raise OutcomeLedgerError(
            "signal outcome JSON bytes are not in canonical Stage 4F form"
        )
    return outcome


def read_signal_outcome_json(path: str | Path) -> SignalOutcome:
    checked = _checked_path(Path(path), "signal outcome input").resolve(strict=True)
    return signal_outcome_from_json_bytes(
        _read_bounded(checked, "signal outcome JSON")
    )


def _validate_terminal(outcome: SignalOutcome) -> None:
    if outcome.state is OutcomeState.OPEN:
        raise OutcomeLedgerError("outcome ledger accepts terminal outcomes only")
    if outcome.state is OutcomeState.NO_ENTRY and outcome.terminal_reason not in {
        OutcomeTerminalReason.ORDER_REJECTED,
        OutcomeTerminalReason.DATA_INVALID,
    }:
        raise OutcomeLedgerError("NO_ENTRY outcome must carry a terminal reason")


def _lane_for_outcome(outcome: SignalOutcome) -> OutcomeLedgerLane:
    if outcome.origin in {
        OutcomeEvidenceOrigin.PAPER_RECORDED,
        OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
    } or outcome.synthetic_fixture_only:
        return OutcomeLedgerLane.DIAGNOSTIC_ONLY
    # Canonical JSON proves internal consistency only. It does not prove that
    # ``verified=True`` or its SHA references came from an independent
    # authority. Stage 4F therefore admits every imported live observation as
    # a candidate. A future append-only authority/admission ledger must remain
    # a separate stage and must not mutate this immutable record.
    return OutcomeLedgerLane.LIVE_CANDIDATE


@dataclass(frozen=True, slots=True)
class OutcomeLedgerRecord:
    append_order: int
    recorded_by: str
    ingested_at: datetime
    previous_record_hash: str
    outcome: SignalOutcome = field(repr=False)
    lane: OutcomeLedgerLane = field(init=False)
    record_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_int(self.append_order, "append_order", minimum=1)
        _require_safe_token(self.recorded_by, "recorded_by")
        ingested_at = to_utc(ensure_aware(self.ingested_at, "ingested_at"))
        previous = _require_sha256(
            self.previous_record_hash,
            "previous_record_hash",
        )
        if not isinstance(self.outcome, SignalOutcome):
            raise OutcomeLedgerError("record outcome must be SignalOutcome")
        _validate_terminal(self.outcome)
        if ingested_at < to_utc(self.outcome.recorded_at):
            raise OutcomeLedgerError("ingested_at cannot precede outcome recorded_at")
        lane = _lane_for_outcome(self.outcome)
        object.__setattr__(self, "ingested_at", ingested_at)
        object.__setattr__(self, "previous_record_hash", previous)
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "record_hash", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": RECORD_SCHEMA,
            "append_order": self.append_order,
            "recorded_by": self.recorded_by,
            "ingested_at": _datetime_text(self.ingested_at),
            "previous_record_hash": self.previous_record_hash,
            "lane": self.lane.value,
            "outcome": signal_outcome_to_dict(self.outcome),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), "record_hash": self.record_hash}

    @classmethod
    def from_dict(cls, value: object) -> OutcomeLedgerRecord:
        document = _require_mapping(value, "outcome ledger record")
        _require_fields(
            document,
            {
                "schema",
                "append_order",
                "recorded_by",
                "ingested_at",
                "previous_record_hash",
                "lane",
                "outcome",
                "record_hash",
            },
            "outcome ledger record",
        )
        if document["schema"] != RECORD_SCHEMA:
            raise OutcomeLedgerError("outcome ledger record schema is invalid")
        record = cls(
            append_order=_require_int(
                document["append_order"],
                "append_order",
                minimum=1,
            ),
            recorded_by=_require_safe_token(document["recorded_by"], "recorded_by"),
            ingested_at=_require_datetime(document["ingested_at"], "ingested_at"),
            previous_record_hash=_require_sha256(
                document["previous_record_hash"],
                "previous_record_hash",
            ),
            outcome=signal_outcome_from_dict(document["outcome"]),
        )
        try:
            declared_lane = OutcomeLedgerLane(
                _require_text(document["lane"], "ledger lane")
            )
        except ValueError as exc:
            raise OutcomeLedgerError("outcome ledger lane is invalid") from exc
        if record.lane is not declared_lane:
            raise OutcomeLedgerError("outcome ledger lane does not match outcome")
        if record.record_hash != _require_sha256(
            document["record_hash"],
            "record_hash",
        ):
            raise OutcomeLedgerError("outcome ledger record hash mismatch")
        if record.as_dict() != dict(document):
            raise OutcomeLedgerError("outcome ledger record canonical form is invalid")
        return record


@dataclass(frozen=True, slots=True)
class OutcomeLedgerAppendResult:
    disposition: OutcomeLedgerDisposition
    record: OutcomeLedgerRecord

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, OutcomeLedgerDisposition):
            raise OutcomeLedgerError("append disposition is invalid")
        if not isinstance(self.record, OutcomeLedgerRecord):
            raise OutcomeLedgerError("append record is invalid")


@dataclass(frozen=True, slots=True)
class OutcomeLedgerAuditReport:
    audited_at: datetime
    record_hashes: tuple[str, ...]
    lane_counts: tuple[tuple[OutcomeLedgerLane, int], ...]
    first_record_hash: str
    last_record_hash: str
    audit_id: str = field(init=False)

    def __post_init__(self) -> None:
        audited_at = to_utc(ensure_aware(self.audited_at, "audited_at"))
        hashes = tuple(
            _require_sha256(item, "audit record hash") for item in self.record_hashes
        )
        if len(hashes) != len(set(hashes)):
            raise OutcomeLedgerError("audit record hashes must be unique")
        counts = tuple(sorted(self.lane_counts, key=lambda item: item[0].value))
        if counts != self.lane_counts:
            raise OutcomeLedgerError("audit lane counts must be sorted")
        if any(
            not isinstance(lane, OutcomeLedgerLane)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            for lane, count in counts
        ):
            raise OutcomeLedgerError("audit lane counts are invalid")
        if len({lane for lane, _ in counts}) != len(counts):
            raise OutcomeLedgerError("audit lane counts must be unique")
        if sum(count for _, count in counts) != len(hashes):
            raise OutcomeLedgerError("audit lane counts disagree with records")
        first = _require_sha256(self.first_record_hash, "first_record_hash")
        last = _require_sha256(self.last_record_hash, "last_record_hash")
        expected_first = _ZERO_HASH if not hashes else hashes[0]
        expected_last = _ZERO_HASH if not hashes else hashes[-1]
        if first != expected_first or last != expected_last:
            raise OutcomeLedgerError("audit boundary hashes are inconsistent")
        object.__setattr__(self, "audited_at", audited_at)
        object.__setattr__(self, "record_hashes", hashes)
        object.__setattr__(self, "lane_counts", counts)
        object.__setattr__(self, "audit_id", fingerprint(self._identity_payload()))

    @property
    def record_count(self) -> int:
        return len(self.record_hashes)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": AUDIT_SCHEMA,
            "audited_at": _datetime_text(self.audited_at),
            "record_hashes": list(self.record_hashes),
            "lane_counts": [
                {"lane": lane.value, "count": count}
                for lane, count in self.lane_counts
            ],
            "first_record_hash": self.first_record_hash,
            "last_record_hash": self.last_record_hash,
            "record_count": self.record_count,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "audit_id": self.audit_id,
            "integrity_state": "PASSED",
            "production_database_modified": False,
        }


def _scoreboard_metrics_to_dict(
    metrics: StrategyScoreboardMetrics | None,
) -> dict[str, Any] | None:
    if metrics is None:
        return None
    return {
        "schema": "strategy-scoreboard-metrics-v1",
        "sample_count": metrics.sample_count,
        "win_rate": _decimal_text(metrics.win_rate),
        "average_r": _decimal_text(metrics.average_r),
        "median_r": _decimal_text(metrics.median_r),
        "net_expectancy_r": _decimal_text(metrics.net_expectancy_r),
        "profit_factor_r": (
            None
            if metrics.profit_factor_r is None
            else _decimal_text(metrics.profit_factor_r)
        ),
        "max_drawdown_r": _decimal_text(metrics.max_drawdown_r),
        "recent_weighted_expectancy_r": _decimal_text(
            metrics.recent_weighted_expectancy_r
        ),
        "average_holding_sessions": _decimal_text(
            metrics.average_holding_sessions
        ),
        "metrics_id": metrics.metrics_id,
    }


def _bucket_to_dict(bucket: OutcomeBucketMetrics) -> dict[str, Any]:
    return {
        "schema": "outcome-bucket-metrics-v1",
        "kind": bucket.kind.value,
        "key": bucket.key,
        "sample_count": bucket.sample_count,
        "win_rate": _decimal_text(bucket.win_rate),
        "average_r": _decimal_text(bucket.average_r),
        "bucket_id": bucket.bucket_id,
    }


def strategy_scoreboard_to_dict(scoreboard: StrategyScoreboard) -> dict[str, Any]:
    if not isinstance(scoreboard, StrategyScoreboard):
        raise OutcomeLedgerError("scoreboard must be StrategyScoreboard")
    return {
        "schema": "strategy-scoreboard-v1",
        "strategy_id": scoreboard.strategy_id,
        "strategy_version": scoreboard.strategy_version,
        "market": scoreboard.market.value,
        "horizon_sessions": scoreboard.horizon_sessions,
        "model_id": scoreboard.model_id,
        "evidence_tier": scoreboard.evidence_tier.value,
        "window_start": _datetime_text(scoreboard.window_start),
        "window_end": _datetime_text(scoreboard.window_end),
        "as_of": _datetime_text(scoreboard.as_of),
        "policy": {
            "schema": "outcome-scoreboard-policy-v1",
            "policy_version": scoreboard.policy.policy_version,
            "minimum_real_samples": scoreboard.policy.minimum_real_samples,
            "minimum_bucket_samples": scoreboard.policy.minimum_bucket_samples,
            "recent_window": scoreboard.policy.recent_window,
            "policy_id": scoreboard.policy.policy_id,
        },
        "cohort_id": scoreboard.cohort_id,
        "outcome_ids": [item.outcome_id for item in scoreboard.outcomes],
        "eligible_outcome_ids": list(scoreboard.eligible_outcome_ids),
        "excluded_counts": [
            {"reason": reason, "count": count}
            for reason, count in scoreboard.excluded_counts
        ],
        "state": scoreboard.state.value,
        "blockers": list(scoreboard.blockers),
        "metric_notes": list(scoreboard.metric_notes),
        "metrics": _scoreboard_metrics_to_dict(scoreboard.metrics),
        "bucket_metrics": [_bucket_to_dict(item) for item in scoreboard.bucket_metrics],
        "scoreboard_id": scoreboard.scoreboard_id,
    }


def _select_candidate_records(
    records: tuple[OutcomeLedgerRecord, ...],
    *,
    strategy_id: str,
    strategy_version: str,
    market: Market,
    horizon_sessions: int,
    model_id: str | None,
    evidence_tier: DataTrustTier,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
) -> tuple[OutcomeLedgerRecord, ...]:
    _require_text(strategy_id, "strategy_id")
    _require_text(strategy_version, "strategy_version")
    if not isinstance(market, Market):
        raise OutcomeLedgerError("market must be Market")
    _require_int(horizon_sessions, "horizon_sessions", minimum=1)
    if model_id is not None:
        _require_text(model_id, "model_id")
    if not isinstance(evidence_tier, DataTrustTier):
        raise OutcomeLedgerError("evidence_tier must be DataTrustTier")
    start = to_utc(ensure_aware(window_start, "window_start"))
    end = to_utc(ensure_aware(window_end, "window_end"))
    cutoff = to_utc(ensure_aware(as_of, "as_of"))
    if end < start:
        raise OutcomeLedgerError("window_end cannot precede window_start")
    if cutoff < end:
        raise OutcomeLedgerError("as_of cannot precede window_end")
    return tuple(
        record
        for record in records
        if record.outcome.strategy_id == strategy_id
        and record.outcome.strategy_version == strategy_version
        and record.outcome.market is market
        and record.outcome.horizon_sessions == horizon_sessions
        and record.outcome.model_id == model_id
        and record.outcome.evidence_tier is evidence_tier
        and start <= to_utc(record.outcome.recorded_at) <= end
        and to_utc(record.outcome.recorded_at) <= cutoff
        and record.ingested_at <= cutoff
    )


@dataclass(frozen=True, slots=True)
class OutcomeLedgerScoreboardSnapshot:
    generated_at: datetime
    audit: OutcomeLedgerAuditReport = field(repr=False)
    audited_records: tuple[OutcomeLedgerRecord, ...] = field(repr=False)
    strategy_id: str
    strategy_version: str
    market: Market
    horizon_sessions: int
    model_id: str | None
    evidence_tier: DataTrustTier
    window_start: datetime
    window_end: datetime
    as_of: datetime
    policy: OutcomeScoreboardPolicy = field(repr=False)
    candidate_records: tuple[OutcomeLedgerRecord, ...] = field(init=False, repr=False)
    scoreboard_records: tuple[OutcomeLedgerRecord, ...] = field(init=False, repr=False)
    scoreboard: StrategyScoreboard = field(init=False, repr=False)
    candidate_record_hashes: tuple[str, ...] = field(init=False)
    candidate_outcome_ids: tuple[str, ...] = field(init=False)
    outcome_contract_eligible_ids: tuple[str, ...] = field(init=False)
    record_hashes: tuple[str, ...] = field(init=False)
    candidate_lane_counts: tuple[tuple[OutcomeLedgerLane, int], ...] = field(
        init=False
    )
    outcome_contract_eligible_count: int = field(init=False)
    admission_blockers: tuple[str, ...] = field(init=False)
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        generated_at = to_utc(ensure_aware(self.generated_at, "generated_at"))
        if not isinstance(self.audit, OutcomeLedgerAuditReport):
            raise OutcomeLedgerError("snapshot audit is invalid")
        if self.audit.audited_at != generated_at:
            raise OutcomeLedgerError("snapshot audit timestamp differs from generated_at")
        audited_records = tuple(self.audited_records)
        if any(not isinstance(item, OutcomeLedgerRecord) for item in audited_records):
            raise OutcomeLedgerError(
                "snapshot audited_records must contain ledger records"
            )
        if tuple(item.append_order for item in audited_records) != tuple(
            range(1, len(audited_records) + 1)
        ):
            raise OutcomeLedgerError("snapshot audited records are not in append order")
        audited_hashes = tuple(item.record_hash for item in audited_records)
        if audited_hashes != self.audit.record_hashes:
            raise OutcomeLedgerError(
                "snapshot audited records disagree with the ledger audit"
            )
        lane_counts = tuple(
            sorted(
                Counter(item.lane for item in audited_records).items(),
                key=lambda item: item[0].value,
            )
        )
        if lane_counts != self.audit.lane_counts:
            raise OutcomeLedgerError("snapshot audited lane counts are inconsistent")

        strategy_id = _require_text(self.strategy_id, "strategy_id")
        strategy_version = _require_text(self.strategy_version, "strategy_version")
        if not isinstance(self.market, Market):
            raise OutcomeLedgerError("snapshot market must be Market")
        horizon_sessions = _require_int(
            self.horizon_sessions,
            "horizon_sessions",
            minimum=1,
        )
        model_id = (
            None if self.model_id is None else _require_text(self.model_id, "model_id")
        )
        if not isinstance(self.evidence_tier, DataTrustTier):
            raise OutcomeLedgerError("snapshot evidence_tier must be DataTrustTier")
        window_start = to_utc(ensure_aware(self.window_start, "window_start"))
        window_end = to_utc(ensure_aware(self.window_end, "window_end"))
        as_of = to_utc(ensure_aware(self.as_of, "as_of"))
        if generated_at < as_of:
            raise OutcomeLedgerError("generated_at cannot precede scoreboard as_of")
        if not isinstance(self.policy, OutcomeScoreboardPolicy):
            raise OutcomeLedgerError("snapshot policy must be OutcomeScoreboardPolicy")

        candidates = _select_candidate_records(
            audited_records,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            market=self.market,
            horizon_sessions=horizon_sessions,
            model_id=model_id,
            evidence_tier=self.evidence_tier,
            window_start=window_start,
            window_end=window_end,
            as_of=as_of,
        )
        candidate_hashes = tuple(item.record_hash for item in candidates)
        candidate_outcome_ids = tuple(item.outcome.outcome_id for item in candidates)
        contract_eligible_ids = tuple(
            item.outcome.outcome_id
            for item in candidates
            if item.outcome.real_scoreboard_eligible
        )
        candidate_lane_counts = tuple(
            sorted(
                Counter(item.lane for item in candidates).items(),
                key=lambda item: item[0].value,
            )
        )
        contract_eligible_count = len(contract_eligible_ids)
        scoreboard_records: tuple[OutcomeLedgerRecord, ...] = ()
        try:
            scoreboard = StrategyScoreboard(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                market=self.market,
                horizon_sessions=horizon_sessions,
                model_id=model_id,
                evidence_tier=self.evidence_tier,
                window_start=window_start,
                window_end=window_end,
                as_of=as_of,
                policy=self.policy,
                outcomes=(),
            )
        except OutcomeContractError as exc:
            raise OutcomeLedgerError("ledger scoreboard contract is invalid") from exc
        admission_blockers = ("TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED",)

        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "audited_records", audited_records)
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "strategy_version", strategy_version)
        object.__setattr__(self, "horizon_sessions", horizon_sessions)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "window_start", window_start)
        object.__setattr__(self, "window_end", window_end)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "candidate_records", candidates)
        object.__setattr__(self, "scoreboard_records", scoreboard_records)
        object.__setattr__(self, "scoreboard", scoreboard)
        object.__setattr__(self, "candidate_record_hashes", candidate_hashes)
        object.__setattr__(self, "candidate_outcome_ids", candidate_outcome_ids)
        object.__setattr__(
            self,
            "outcome_contract_eligible_ids",
            contract_eligible_ids,
        )
        object.__setattr__(self, "record_hashes", ())
        object.__setattr__(self, "candidate_lane_counts", candidate_lane_counts)
        object.__setattr__(
            self,
            "outcome_contract_eligible_count",
            contract_eligible_count,
        )
        object.__setattr__(self, "admission_blockers", admission_blockers)
        object.__setattr__(self, "snapshot_id", fingerprint(self._identity_payload()))

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": SCOREBOARD_SNAPSHOT_SCHEMA,
            "generated_at": _datetime_text(self.generated_at),
            "ledger_audit_id": self.audit.audit_id,
            "ledger_record_count": self.audit.record_count,
            "ledger_first_record_hash": self.audit.first_record_hash,
            "ledger_last_record_hash": self.audit.last_record_hash,
            "candidate_record_hashes": list(self.candidate_record_hashes),
            "candidate_outcome_ids": list(self.candidate_outcome_ids),
            "outcome_contract_eligible_ids": list(
                self.outcome_contract_eligible_ids
            ),
            "candidate_lane_counts": [
                {"lane": lane.value, "count": count}
                for lane, count in self.candidate_lane_counts
            ],
            "outcome_contract_eligible_count": (
                self.outcome_contract_eligible_count
            ),
            "record_hashes": list(self.record_hashes),
            "admission_blockers": list(self.admission_blockers),
            "trusted_outcome_authority_configured": (
                TRUSTED_OUTCOME_AUTHORITY_CONFIGURED
            ),
            "scoreboard": strategy_scoreboard_to_dict(self.scoreboard),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "snapshot_id": self.snapshot_id,
            "investment_performance_claim": False,
            "production_database_modified": False,
            "auto_promote_model": False,
            "auto_change_strategy_weight": False,
            "auto_trade": False,
        }


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _checked_path(path: Path, name: str) -> Path:
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_link(candidate):
            raise OutcomeLedgerError(f"{name} cannot traverse a symlink or junction")
    return absolute.resolve(strict=False)


def _safe_child(root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise OutcomeLedgerError("record storage key is invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise OutcomeLedgerError("record storage key escaped root")
    resolved_root = root.resolve(strict=False)
    target = (resolved_root / relative_path).resolve(strict=False)
    if os.path.commonpath((str(resolved_root), str(target))) != str(resolved_root):
        raise OutcomeLedgerError("record storage key escaped root")
    cursor = resolved_root
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.exists() and _is_link(cursor):
            raise OutcomeLedgerError("record storage path contains a link")
    return target


def _atomic_write_immutable(path: Path, raw: bytes) -> bool:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_JSON_BYTES:
        raise OutcomeLedgerError("immutable outcome evidence exceeds the size bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_parent = _checked_path(path.parent, "immutable outcome evidence parent")
    if checked_parent != path.parent.resolve(strict=False) or _is_link(path.parent):
        raise OutcomeLedgerError("refusing to write outcome evidence through a link")
    if path.exists() and _is_link(path):
        raise OutcomeLedgerError("refusing to write outcome evidence through a link")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish without overwrite. A hard link is atomic and fails when a
            # concurrent process has already created the final path; os.replace
            # would silently overwrite that immutable evidence.
            os.link(temporary, path)
        except FileExistsError:
            if _is_link(path) or not path.is_file():
                raise OutcomeLedgerError(
                    "immutable outcome evidence target is not a regular file"
                )
            if _read_bounded(path, "immutable outcome evidence") != raw:
                raise OutcomeLedgerError(
                    "immutable outcome evidence path contains different content"
                )
            return False
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _read_bounded(path: Path, name: str) -> bytes:
    if not path.is_file() or _is_link(path):
        raise OutcomeLedgerError(f"{name} must be a regular non-link file")
    try:
        before = path.stat()
        size = before.st_size
        if size <= 0 or size > _MAX_JSON_BYTES:
            raise OutcomeLedgerError(f"{name} size is invalid")
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise OutcomeLedgerError(f"cannot read {name}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(raw) != size or identity_after != identity_before:
        raise OutcomeLedgerError(f"{name} changed while reading")
    return raw


def _same_existing_file(left: Path, right: Path, name: str) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right)
    except OSError as exc:
        raise OutcomeLedgerError(f"cannot verify {name} file identity") from exc


def _path_identity(path: Path, name: str) -> tuple[int, int]:
    try:
        status = path.stat()
    except OSError as exc:
        raise OutcomeLedgerError(f"cannot inspect {name} identity") from exc
    return int(status.st_dev), int(status.st_ino)


def _catalog_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, int], ...]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            int(row[5]),
        )
        for row in rows
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


def _validate_catalog_schema(connection: sqlite3.Connection) -> None:
    quick_check = tuple(
        str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
    )
    if quick_check != ("ok",):
        raise OutcomeLedgerError("outcome catalog SQLite integrity check failed")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if tables != {"outcome_ledger_meta", "outcome_records"}:
        raise OutcomeLedgerError("outcome catalog table set is invalid")
    if _catalog_columns(connection, "outcome_ledger_meta") != _CATALOG_META_COLUMNS:
        raise OutcomeLedgerError("outcome catalog metadata schema is invalid")
    if _catalog_columns(connection, "outcome_records") != _CATALOG_RECORD_COLUMNS:
        raise OutcomeLedgerError("outcome catalog record schema is invalid")
    metadata = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT key,value FROM outcome_ledger_meta ORDER BY key"
        ).fetchall()
    )
    if metadata != (("schema", LEDGER_SCHEMA),):
        raise OutcomeLedgerError("outcome catalog schema identity is invalid")
    required_unique = {
        ("record_hash",),
        ("outcome_id",),
        ("signal_id",),
        ("record_file",),
    }
    if not required_unique.issubset(
        _unique_index_columns(connection, "outcome_records")
    ):
        raise OutcomeLedgerError("outcome catalog uniqueness constraints are invalid")
    query_index = tuple(
        str(item[2])
        for item in connection.execute(
            "PRAGMA index_info('idx_outcome_scoreboard_query')"
        ).fetchall()
    )
    if query_index != (
        "strategy_id",
        "strategy_version",
        "market",
        "horizon_sessions",
        "model_id",
        "evidence_tier",
        "recorded_at",
        "append_order",
    ):
        raise OutcomeLedgerError("outcome catalog query index is invalid")


class OutcomeLedger:
    """Persist immutable terminal outcomes and materialize exact-cohort scoreboards."""

    def __init__(
        self,
        record_root: str | Path = DEFAULT_RECORD_ROOT,
        catalog_path: str | Path = DEFAULT_CATALOG_PATH,
        *,
        production_database: str | Path | None = None,
    ) -> None:
        raw_root = Path(record_root)
        raw_catalog = Path(catalog_path)
        project_root = Path(__file__).resolve().parents[3]
        raw_production = Path(
            production_database
            if production_database is not None
            else project_root / "data" / "stock_tracker.db"
        )
        root = _checked_path(raw_root, "outcome record root")
        catalog = _checked_path(raw_catalog, "outcome catalog")
        production = _checked_path(raw_production, "production database")
        if root == production or catalog == production:
            raise OutcomeLedgerError("outcome ledger cannot use the production database")
        if _same_existing_file(catalog, production, "catalog/production database"):
            raise OutcomeLedgerError("outcome catalog aliases the production database")
        if catalog.is_relative_to(root):
            raise OutcomeLedgerError("outcome catalog must be outside the record root")
        if root.exists() and not root.is_dir():
            raise OutcomeLedgerError("outcome record root must be a directory")
        if catalog.exists() and not catalog.is_file():
            raise OutcomeLedgerError("outcome catalog must be a file")
        root.mkdir(parents=True, exist_ok=True)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        self.record_root = root
        self.catalog_path = catalog
        self.production_database = production
        self._record_root_identity = _path_identity(root, "outcome record root")
        self._catalog_identity: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._initialize()
        self._catalog_identity = _path_identity(catalog, "outcome catalog")

    def _assert_record_root_identity(self) -> None:
        checked = _checked_path(self.record_root, "outcome record root")
        if checked != self.record_root:
            raise OutcomeLedgerError("outcome record root path identity changed")
        if not self.record_root.is_dir() or _is_link(self.record_root):
            raise OutcomeLedgerError(
                "outcome record root must remain a regular non-link directory"
            )
        if _path_identity(self.record_root, "outcome record root") != (
            self._record_root_identity
        ):
            raise OutcomeLedgerError("outcome record root was replaced after opening")

    def _assert_catalog_identity(self) -> None:
        checked = _checked_path(self.catalog_path, "outcome catalog")
        if checked != self.catalog_path:
            raise OutcomeLedgerError("outcome catalog path identity changed")
        if not self.catalog_path.is_file() or _is_link(self.catalog_path):
            raise OutcomeLedgerError("outcome catalog must remain a regular non-link file")
        if self._catalog_identity is not None and _path_identity(
            self.catalog_path,
            "outcome catalog",
        ) != self._catalog_identity:
            raise OutcomeLedgerError("outcome catalog was replaced after opening")
        if _same_existing_file(
            self.catalog_path,
            self.production_database,
            "catalog/production database",
        ):
            raise OutcomeLedgerError("outcome catalog aliases the production database")
        try:
            with self.catalog_path.open("rb") as stream:
                header = stream.read(len(_SQLITE_HEADER))
        except OSError as exc:
            raise OutcomeLedgerError("cannot read the outcome catalog header") from exc
        if header != _SQLITE_HEADER:
            raise OutcomeLedgerError("outcome catalog is not a valid SQLite database")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._assert_catalog_identity()
        connection = sqlite3.connect(self.catalog_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            _validate_catalog_schema(connection)
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE outcome_ledger_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE outcome_records (
            append_order INTEGER PRIMARY KEY CHECK(append_order > 0),
            record_hash TEXT NOT NULL UNIQUE,
            previous_record_hash TEXT NOT NULL,
            outcome_id TEXT NOT NULL UNIQUE,
            signal_id TEXT NOT NULL UNIQUE,
            lane TEXT NOT NULL CHECK(lane IN ('DIAGNOSTIC_ONLY','LIVE_CANDIDATE')),
            market TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL CHECK(horizon_sessions > 0),
            model_id TEXT,
            evidence_tier TEXT NOT NULL,
            origin TEXT NOT NULL,
            verified INTEGER NOT NULL CHECK(verified IN (0,1)),
            outcome_contract_eligible INTEGER NOT NULL
                CHECK(outcome_contract_eligible IN (0,1)),
            recorded_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            recorded_by TEXT NOT NULL,
            record_file TEXT NOT NULL UNIQUE,
            record_file_sha256 TEXT NOT NULL
        );
        CREATE INDEX idx_outcome_scoreboard_query
            ON outcome_records(
                strategy_id,
                strategy_version,
                market,
                horizon_sessions,
                model_id,
                evidence_tier,
                recorded_at,
                append_order
            );
        """
        if self.catalog_path.exists():
            self._assert_catalog_identity()
            with closing(
                sqlite3.connect(self.catalog_path, timeout=30.0)
            ) as connection:
                connection.row_factory = sqlite3.Row
                _validate_catalog_schema(connection)
            return

        checked_parent = _checked_path(
            self.catalog_path.parent,
            "outcome catalog parent",
        )
        if checked_parent != self.catalog_path.parent.resolve(strict=False):
            raise OutcomeLedgerError("outcome catalog parent path identity changed")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.catalog_path.parent,
            prefix=f".{self.catalog_path.name}.init-",
            suffix=".db",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary, timeout=30.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(schema)
                connection.execute(
                    "INSERT INTO outcome_ledger_meta(key,value) VALUES('schema',?)",
                    (LEDGER_SCHEMA,),
                )
                connection.commit()
                _validate_catalog_schema(connection)
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            try:
                # Publish only a fully initialized catalog. Concurrent creators
                # build isolated temporary databases; exactly one link wins and
                # every loser validates the winner instead of deleting it.
                os.link(temporary, self.catalog_path)
            except FileExistsError:
                pass
            self._assert_catalog_identity()
            with closing(
                sqlite3.connect(self.catalog_path, timeout=30.0)
            ) as connection:
                connection.row_factory = sqlite3.Row
                _validate_catalog_schema(connection)
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(temporary) + suffix)
                if candidate.exists() and not _is_link(candidate):
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _record_relative_path(record: OutcomeLedgerRecord) -> str:
        strategy_key = hashlib.sha256(
            f"{record.outcome.strategy_id}\0{record.outcome.strategy_version}".encode()
        ).hexdigest()[:20]
        return (
            f"market={record.outcome.market.value.lower()}/"
            f"strategy={strategy_key}/"
            f"record-{record.append_order:012d}-{record.record_hash}.json"
        )

    def _record_from_row(self, row: sqlite3.Row) -> OutcomeLedgerRecord:
        relative = _require_text(row["record_file"], "record_file")
        path = _safe_child(self.record_root, relative)
        raw = _read_bounded(path, "outcome record")
        if hashlib.sha256(raw).hexdigest() != _require_sha256(
            row["record_file_sha256"],
            "record_file_sha256",
        ):
            raise OutcomeLedgerError("outcome record file SHA mismatch")
        document = _strict_json_loads(raw.rstrip(b"\n"), "outcome record")
        record = OutcomeLedgerRecord.from_dict(document)
        if raw != _canonical_json_bytes(record.as_dict()) + b"\n":
            raise OutcomeLedgerError("outcome record bytes are not canonical")
        expected = {
            "append_order": record.append_order,
            "record_hash": record.record_hash,
            "previous_record_hash": record.previous_record_hash,
            "outcome_id": record.outcome.outcome_id,
            "signal_id": record.outcome.signal_id,
            "lane": record.lane.value,
            "market": record.outcome.market.value,
            "strategy_id": record.outcome.strategy_id,
            "strategy_version": record.outcome.strategy_version,
            "horizon_sessions": record.outcome.horizon_sessions,
            "model_id": record.outcome.model_id,
            "evidence_tier": record.outcome.evidence_tier.value,
            "origin": record.outcome.origin.value,
            "verified": 1 if record.outcome.verified else 0,
            "outcome_contract_eligible": (
                1 if record.outcome.real_scoreboard_eligible else 0
            ),
            "recorded_at": _datetime_text(record.outcome.recorded_at),
            "ingested_at": _datetime_text(record.ingested_at),
            "recorded_by": record.recorded_by,
            "record_file": relative,
            "record_file_sha256": hashlib.sha256(raw).hexdigest(),
        }
        for key, value in expected.items():
            if row[key] != value:
                raise OutcomeLedgerError(f"outcome catalog metadata mismatch: {key}")
        return record

    def _recover_committed_record(
        self,
        expected: OutcomeLedgerRecord,
    ) -> OutcomeLedgerRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM outcome_records WHERE outcome_id=?",
                (expected.outcome.outcome_id,),
            ).fetchone()
            if row is None:
                return None
            recovered = self._record_from_row(row)
        if recovered != expected:
            raise OutcomeLedgerError(
                "persisted outcome record differs from the attempted append"
            )
        return recovered

    def _validate_ledger_state(
        self,
        connection: sqlite3.Connection,
        cutoff: datetime,
    ) -> tuple[OutcomeLedgerAuditReport, tuple[OutcomeLedgerRecord, ...]]:
        rows = connection.execute(
            "SELECT * FROM outcome_records ORDER BY append_order"
        ).fetchall()
        expected_previous = _ZERO_HASH
        previous_ingested_at: datetime | None = None
        records: list[OutcomeLedgerRecord] = []
        registered_files: set[str] = set()
        lane_counter: Counter[OutcomeLedgerLane] = Counter()
        for expected_order, row in enumerate(rows, start=1):
            if row["append_order"] != expected_order:
                raise OutcomeLedgerError("outcome append order is not contiguous")
            record = self._record_from_row(row)
            if record.ingested_at > cutoff:
                raise OutcomeLedgerError(
                    "outcome record was ingested after the audit timestamp"
                )
            if (
                previous_ingested_at is not None
                and record.ingested_at < previous_ingested_at
            ):
                raise OutcomeLedgerError(
                    "outcome record ingestion timestamps are not monotonic"
                )
            if record.previous_record_hash != expected_previous:
                raise OutcomeLedgerError("outcome record hash chain is broken")
            previous_ingested_at = record.ingested_at
            expected_previous = record.record_hash
            records.append(record)
            registered_files.add(str(row["record_file"]))
            lane_counter[record.lane] += 1

        actual_files: set[str] = set()
        for directory, child_directories, filenames in os.walk(
            self.record_root,
            followlinks=False,
        ):
            directory_path = Path(directory)
            if _is_link(directory_path):
                raise OutcomeLedgerError(
                    "outcome record root contains a linked directory"
                )
            for child in child_directories:
                if _is_link(directory_path / child):
                    raise OutcomeLedgerError(
                        "outcome record root contains a linked directory"
                    )
            for filename in filenames:
                path = directory_path / filename
                if _is_link(path):
                    raise OutcomeLedgerError(
                        "outcome record root contains a linked file"
                    )
                actual_files.add(path.relative_to(self.record_root).as_posix())
        if actual_files != registered_files:
            missing = sorted(registered_files - actual_files)
            orphan = sorted(actual_files - registered_files)
            raise OutcomeLedgerError(
                "outcome record inventory mismatch; "
                f"missing={missing}; orphan={orphan}"
            )
        lane_counts = tuple(
            sorted(lane_counter.items(), key=lambda item: item[0].value)
        )
        hashes = tuple(item.record_hash for item in records)
        return (
            OutcomeLedgerAuditReport(
                audited_at=cutoff,
                record_hashes=hashes,
                lane_counts=lane_counts,
                first_record_hash=_ZERO_HASH if not hashes else hashes[0],
                last_record_hash=_ZERO_HASH if not hashes else hashes[-1],
            ),
            tuple(records),
        )

    def append(
        self,
        outcome: SignalOutcome,
        *,
        recorded_by: str,
    ) -> OutcomeLedgerAppendResult:
        if not isinstance(outcome, SignalOutcome):
            raise OutcomeLedgerError("outcome must be SignalOutcome")
        _validate_terminal(outcome)
        actor = _require_safe_token(recorded_by, "recorded_by")
        self._assert_record_root_identity()
        created_path: Path | None = None
        record: OutcomeLedgerRecord | None = None
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Observe ingestion only after the global SQLite writer lock is
                # held. Together with the full pre-append audit, this guarantees
                # that historical as_of visibility is always an append prefix.
                observed = to_utc(ensure_aware(_utc_now(), "ingested_at"))
                _current_audit, current_records = self._validate_ledger_state(
                    connection,
                    observed,
                )
                existing = next(
                    (
                        item
                        for item in current_records
                        if item.outcome.outcome_id == outcome.outcome_id
                    ),
                    None,
                )
                if existing is not None:
                    connection.rollback()
                    return OutcomeLedgerAppendResult(
                        OutcomeLedgerDisposition.IDEMPOTENT,
                        existing,
                    )
                if any(
                    item.outcome.signal_id == outcome.signal_id
                    for item in current_records
                ):
                    raise OutcomeLedgerConflict(
                        "signal_id already has a different immutable outcome"
                    )
                append_order = len(current_records) + 1
                previous = (
                    _ZERO_HASH
                    if not current_records
                    else current_records[-1].record_hash
                )
                record = OutcomeLedgerRecord(
                    append_order=append_order,
                    recorded_by=actor,
                    ingested_at=observed,
                    previous_record_hash=previous,
                    outcome=outcome,
                )
                relative = self._record_relative_path(record)
                target = _safe_child(self.record_root, relative)
                raw = _canonical_json_bytes(record.as_dict()) + b"\n"
                if _atomic_write_immutable(target, raw):
                    created_path = target
                connection.execute(
                    """
                    INSERT INTO outcome_records(
                        append_order,record_hash,previous_record_hash,outcome_id,
                        signal_id,lane,market,strategy_id,strategy_version,
                        horizon_sessions,model_id,evidence_tier,origin,verified,
                        outcome_contract_eligible,recorded_at,ingested_at,recorded_by,
                        record_file,record_file_sha256
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.append_order,
                        record.record_hash,
                        record.previous_record_hash,
                        outcome.outcome_id,
                        outcome.signal_id,
                        record.lane.value,
                        outcome.market.value,
                        outcome.strategy_id,
                        outcome.strategy_version,
                        outcome.horizon_sessions,
                        outcome.model_id,
                        outcome.evidence_tier.value,
                        outcome.origin.value,
                        1 if outcome.verified else 0,
                        1 if outcome.real_scoreboard_eligible else 0,
                        _datetime_text(outcome.recorded_at),
                        _datetime_text(record.ingested_at),
                        record.recorded_by,
                        relative,
                        hashlib.sha256(raw).hexdigest(),
                    ),
                )
                connection.commit()
                return OutcomeLedgerAppendResult(
                    OutcomeLedgerDisposition.APPENDED,
                    record,
                )
            except (OSError, OutcomeLedgerError, sqlite3.Error, ValueError) as exc:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                if record is not None:
                    try:
                        recovered = self._recover_committed_record(record)
                    except (OSError, OutcomeLedgerError, sqlite3.Error, ValueError) as recovery_error:
                        # The transaction outcome is now uncertain. Preserving
                        # the immutable file is safer than deleting evidence that
                        # may already have a durable catalog row.
                        raise OutcomeLedgerError(
                            "outcome append failed and commit state could not be recovered: "
                            + type(exc).__name__
                        ) from recovery_error
                    if recovered is not None:
                        return OutcomeLedgerAppendResult(
                            OutcomeLedgerDisposition.APPENDED,
                            recovered,
                        )
                if created_path is not None and created_path.exists():
                    try:
                        created_path.unlink()
                    except OSError as cleanup_error:
                        raise OutcomeLedgerError(
                            "outcome catalog append failed and record compensation also failed: "
                            + type(exc).__name__
                        ) from cleanup_error
                raise

    def _audit_at(
        self,
        checked_at: datetime,
    ) -> tuple[OutcomeLedgerAuditReport, tuple[OutcomeLedgerRecord, ...]]:
        cutoff = to_utc(ensure_aware(checked_at, "audited_at"))
        self._assert_record_root_identity()
        with self._lock, self._connection() as connection:
            # The catalog and immutable files form one logical snapshot. A
            # reserved SQLite writer lock prevents another process from
            # publishing a record file before its catalog commit while this
            # audit is walking the record root.
            connection.execute("BEGIN IMMEDIATE")
            try:
                report, records = self._validate_ledger_state(connection, cutoff)
                connection.rollback()
                return report, records
            except Exception:
                connection.rollback()
                raise

    def audit(self) -> OutcomeLedgerAuditReport:
        checked_at = to_utc(ensure_aware(_utc_now(), "audited_at"))
        report, _records = self._audit_at(checked_at)
        return report

    def materialize_scoreboard(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        market: Market,
        horizon_sessions: int,
        model_id: str | None,
        evidence_tier: DataTrustTier,
        window_start: datetime,
        window_end: datetime,
        as_of: datetime,
        policy: OutcomeScoreboardPolicy,
    ) -> OutcomeLedgerScoreboardSnapshot:
        if not isinstance(policy, OutcomeScoreboardPolicy):
            raise OutcomeLedgerError("policy must be OutcomeScoreboardPolicy")
        cutoff = to_utc(ensure_aware(as_of, "as_of"))
        generated = to_utc(ensure_aware(_utc_now(), "generated_at"))
        if generated < cutoff:
            raise OutcomeLedgerError("as_of cannot be in the future")
        audit, audited_records = self._audit_at(generated)
        return OutcomeLedgerScoreboardSnapshot(
            generated_at=generated,
            audit=audit,
            audited_records=audited_records,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            market=market,
            horizon_sessions=horizon_sessions,
            model_id=model_id,
            evidence_tier=evidence_tier,
            window_start=window_start,
            window_end=window_end,
            as_of=cutoff,
            policy=policy,
        )


def render_outcome_scoreboard_markdown(
    snapshot: OutcomeLedgerScoreboardSnapshot,
) -> str:
    if not isinstance(snapshot, OutcomeLedgerScoreboardSnapshot):
        raise OutcomeLedgerError("snapshot must be OutcomeLedgerScoreboardSnapshot")
    scoreboard = snapshot.scoreboard
    lines = [
        "# Stage 4F Outcome Ledger Scoreboard Snapshot",
        "",
        f"- Snapshot ID: `{snapshot.snapshot_id}`",
        f"- Ledger audit ID: `{snapshot.audit.audit_id}`",
        f"- Strategy: `{scoreboard.strategy_id}` / `{scoreboard.strategy_version}`",
        f"- Market: `{scoreboard.market.value}`",
        f"- Evidence tier: `{scoreboard.evidence_tier.value}`",
        f"- State: `{scoreboard.state.value}`",
        f"- Candidate cohort records: `{len(snapshot.candidate_records)}`",
        (
            "- Outcome-contract eligible but not independently admitted: `"
            f"{snapshot.outcome_contract_eligible_count}`"
        ),
        f"- Trusted-admitted records: `{len(snapshot.scoreboard_records)}`",
        f"- Real eligible records: `{len(scoreboard.eligible_outcome_ids)}`",
        "- Trusted outcome authority configured: `false`",
        "- Investment performance claim: `false`",
        "- Production database modified: `false`",
        "- Auto promotion / strategy-weight change / trading: `false`",
        "",
        "## Blockers",
        "",
    ]
    blockers = tuple(
        sorted(set(snapshot.admission_blockers).union(scoreboard.blockers))
    )
    lines.extend(f"- `{item}`" for item in blockers)
    lines.extend(
        [
            "",
            "> Metrics are intentionally absent. Stage 4F stores immutable candidate evidence, but no trusted admission authority is configured to place imported records into a real Strategy Scoreboard.",
        ]
    )
    return "\n".join(lines) + "\n"


def _checked_output_path(path: Path) -> Path:
    target = _checked_path(path, "outcome report path")
    production = (
        Path(__file__).resolve().parents[3] / "data" / "stock_tracker.db"
    ).resolve(strict=False)
    if target == production or _same_existing_file(
        target,
        production,
        "report/production database",
    ):
        raise OutcomeLedgerError("outcome report cannot target the production database")
    return target


def write_outcome_scoreboard_json(
    snapshot: OutcomeLedgerScoreboardSnapshot,
    path: str | Path,
) -> None:
    if not isinstance(snapshot, OutcomeLedgerScoreboardSnapshot):
        raise OutcomeLedgerError("snapshot must be OutcomeLedgerScoreboardSnapshot")
    target = _checked_output_path(Path(path))
    _atomic_write_immutable(
        target,
        json.dumps(
            snapshot.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
    )


def write_outcome_scoreboard_markdown(
    snapshot: OutcomeLedgerScoreboardSnapshot,
    path: str | Path,
) -> None:
    target = _checked_output_path(Path(path))
    _atomic_write_immutable(
        target,
        render_outcome_scoreboard_markdown(snapshot).encode("utf-8"),
    )


__all__ = [
    "AUDIT_SCHEMA",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_RECORD_ROOT",
    "LEDGER_SCHEMA",
    "OUTCOME_DOCUMENT_SCHEMA",
    "RECORD_SCHEMA",
    "SCOREBOARD_SNAPSHOT_SCHEMA",
    "OutcomeLedger",
    "OutcomeLedgerAppendResult",
    "OutcomeLedgerAuditReport",
    "OutcomeLedgerConflict",
    "OutcomeLedgerDisposition",
    "OutcomeLedgerError",
    "OutcomeLedgerLane",
    "OutcomeLedgerRecord",
    "OutcomeLedgerScoreboardSnapshot",
    "read_signal_outcome_json",
    "render_outcome_scoreboard_markdown",
    "signal_outcome_from_dict",
    "signal_outcome_from_json_bytes",
    "signal_outcome_to_dict",
    "signal_outcome_to_json_bytes",
    "strategy_scoreboard_to_dict",
    "write_outcome_scoreboard_json",
    "write_outcome_scoreboard_markdown",
]
