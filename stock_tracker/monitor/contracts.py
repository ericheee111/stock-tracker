"""Strict non-eval monitor rule and inbox contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sidecars.xtp.contracts import validate_symbol

_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_FACTS = frozenset(
    {
        "action_state",
        "signal_state",
        "data_status",
        "data_quality.status",
        "data_quality.score",
        "blocker_codes",
        "market_regime.state",
        "market_regime.score",
        "market_event.connection_state",
        "market_event.feed_mode",
        "market_event.latency_p50_ms",
        "market_event.latency_p95_ms",
        "market_event.duplicate_count",
        "market_event.callback_gap_count",
        "market_event.provider_gap_count",
        "market_event.out_of_order_count",
        "market_event.ingestion_lag_ms",
        "market_event.last_price",
        "market_event.change_pct",
        "scores.opportunity",
        "scores.timing",
        "scores.risk",
        "scores.confidence",
        "features.rsi14",
        "features.roc20",
        "features.roc60",
        "features.ann_vol",
        "features.volume_ratio",
        "features.pos52w",
        "features.amplitude",
        "features.bar_count",
    }
)
_MAX_CONDITIONS = 32
_MAX_SCOPE_SYMBOLS = 200
_MAX_SIGNED_INTEGER = (1 << 63) - 1


class MonitorValidationError(ValueError):
    """Raised when a monitor contract is malformed or unsafe."""


class RuleLogic(StrEnum):
    AND = "AND"
    OR = "OR"


class RuleOperator(StrEnum):
    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GE = "GE"
    LT = "LT"
    LE = "LE"
    IN = "IN"
    CONTAINS = "CONTAINS"


class ScopeKind(StrEnum):
    SYMBOLS = "SYMBOLS"
    MARKET = "MARKET"
    WATCHLIST = "WATCHLIST"
    POSITIONS = "POSITIONS"
    ALL_MARKET = "ALL_MARKET"


class MonitorSeverity(StrEnum):
    INFO = "INFO"
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class InboxState(StrEnum):
    NEW = "NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SNOOZED = "SNOOZED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    RESOLVED = "RESOLVED"


_TERMINAL_STATES = frozenset(
    {InboxState.INVALIDATED, InboxState.EXPIRED, InboxState.RESOLVED}
)


def _safe_scalar(value: Any, name: str) -> Any:
    if value is None or type(value) in (bool, int, str):
        if type(value) is int and abs(value) > _MAX_SIGNED_INTEGER:
            raise MonitorValidationError(f"{name} integer exceeds signed 64-bit range")
        if isinstance(value, str):
            if len(value) > 512 or value != value.strip():
                raise MonitorValidationError(f"{name} string is invalid")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise MonitorValidationError(f"{name} contains control characters")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise MonitorValidationError(f"{name} must be finite")
        return value
    raise MonitorValidationError(f"{name} must be a JSON scalar")


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MonitorValidationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MonitorCondition:
    fact: str
    operator: RuleOperator
    value: Any

    def __post_init__(self) -> None:
        if self.fact not in _ALLOWED_FACTS:
            raise MonitorValidationError(f"unsupported monitor fact: {self.fact}")
        if not isinstance(self.operator, RuleOperator):
            raise MonitorValidationError("condition operator must be RuleOperator")
        if self.operator is RuleOperator.IN:
            if not isinstance(self.value, (list, tuple)) or not self.value or len(self.value) > 64:
                raise MonitorValidationError("IN value must be a non-empty bounded array")
            for index, item in enumerate(self.value):
                _safe_scalar(item, f"condition.value[{index}]")
        else:
            _safe_scalar(self.value, "condition.value")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MonitorCondition:
        if not isinstance(value, dict) or set(value) != {"fact", "operator", "value"}:
            raise MonitorValidationError("condition requires exact fact/operator/value fields")
        fact = value["fact"]
        operator = value["operator"]
        if type(fact) is not str or type(operator) is not str:
            raise MonitorValidationError("condition fact/operator must be strings")
        try:
            parsed_operator = RuleOperator(operator)
        except ValueError as exc:
            raise MonitorValidationError("unsupported condition operator") from exc
        raw_value = value["value"]
        if isinstance(raw_value, list):
            raw_value = tuple(raw_value)
        return cls(fact=fact, operator=parsed_operator, value=raw_value)

    def as_dict(self) -> dict[str, Any]:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"fact": self.fact, "operator": self.operator.value, "value": value}


@dataclass(frozen=True, slots=True)
class MonitorExpression:
    logic: RuleLogic
    conditions: tuple[MonitorCondition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.logic, RuleLogic):
            raise MonitorValidationError("expression logic must be RuleLogic")
        if not self.conditions or len(self.conditions) > _MAX_CONDITIONS:
            raise MonitorValidationError("expression must contain 1-32 conditions")
        if any(not isinstance(item, MonitorCondition) for item in self.conditions):
            raise MonitorValidationError("expression contains an invalid condition")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MonitorExpression:
        if not isinstance(value, dict) or set(value) != {"logic", "conditions"}:
            raise MonitorValidationError("expression requires exact logic/conditions fields")
        if type(value["logic"]) is not str or not isinstance(value["conditions"], list):
            raise MonitorValidationError("expression fields have invalid types")
        try:
            logic = RuleLogic(value["logic"])
        except ValueError as exc:
            raise MonitorValidationError("unsupported expression logic") from exc
        return cls(
            logic=logic,
            conditions=tuple(MonitorCondition.from_dict(item) for item in value["conditions"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "logic": self.logic.value,
            "conditions": [item.as_dict() for item in self.conditions],
        }


@dataclass(frozen=True, slots=True)
class MonitorScope:
    kind: ScopeKind
    symbols: tuple[str, ...] = ()
    market: str = "A"
    max_symbols: int = 200
    all_market_acknowledged: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind):
            raise MonitorValidationError("scope kind must be ScopeKind")
        if self.market != "A":
            raise MonitorValidationError("current monitor scope supports A shares only")
        if type(self.all_market_acknowledged) is not bool:
            raise MonitorValidationError("all_market_acknowledged must be boolean")
        if type(self.max_symbols) is not int or not 1 <= self.max_symbols <= 5000:
            raise MonitorValidationError("scope max_symbols must be between 1 and 5000")
        if len(self.symbols) > _MAX_SCOPE_SYMBOLS or len(self.symbols) > self.max_symbols:
            raise MonitorValidationError("scope contains too many symbols")
        normalized = tuple(validate_symbol(symbol) for symbol in self.symbols)
        if normalized != self.symbols or len(set(normalized)) != len(normalized):
            raise MonitorValidationError("scope symbols must be normalized and unique")
        if self.kind is ScopeKind.SYMBOLS and not self.symbols:
            raise MonitorValidationError("SYMBOLS scope requires symbols")
        if self.kind is not ScopeKind.SYMBOLS and self.symbols:
            raise MonitorValidationError("only SYMBOLS scope accepts explicit symbols")
        broad_scope = self.kind in {ScopeKind.MARKET, ScopeKind.ALL_MARKET}
        if broad_scope and not self.all_market_acknowledged:
            raise MonitorValidationError(
                "MARKET/ALL_MARKET scopes require explicit acknowledgement"
            )
        if not broad_scope and self.all_market_acknowledged:
            raise MonitorValidationError(
                "all-market acknowledgement is only valid for broad scopes"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MonitorScope:
        expected = {
            "kind",
            "symbols",
            "market",
            "max_symbols",
            "all_market_acknowledged",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise MonitorValidationError("scope contains an invalid field set")
        if type(value["kind"]) is not str or not isinstance(value["symbols"], list):
            raise MonitorValidationError("scope fields have invalid types")
        if (
            type(value["market"]) is not str
            or type(value["max_symbols"]) is not int
            or type(value["all_market_acknowledged"]) is not bool
        ):
            raise MonitorValidationError("scope fields have invalid types")
        try:
            kind = ScopeKind(value["kind"])
        except ValueError as exc:
            raise MonitorValidationError("unsupported scope kind") from exc
        return cls(
            kind=kind,
            symbols=tuple(value["symbols"]),
            market=value["market"],
            max_symbols=value["max_symbols"],
            all_market_acknowledged=value["all_market_acknowledged"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "symbols": list(self.symbols),
            "market": self.market,
            "max_symbols": self.max_symbols,
            "all_market_acknowledged": self.all_market_acknowledged,
        }


@dataclass(frozen=True, slots=True)
class MonitorRule:
    rule_id: str
    name: str
    expression: MonitorExpression
    scope: MonitorScope
    version: int = 1
    severity: MonitorSeverity = MonitorSeverity.NOTICE
    enabled: bool = True
    cooldown_sec: int = 300
    duplicate_window_sec: int = 900
    expires_at: datetime | None = None
    notification_channels: tuple[str, ...] = ("BROWSER",)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if type(self.rule_id) is not str or _RULE_ID_RE.fullmatch(self.rule_id) is None:
            raise MonitorValidationError("rule_id has invalid format")
        if type(self.name) is not str or not self.name.strip() or self.name != self.name.strip():
            raise MonitorValidationError("rule name must be non-empty and trimmed")
        if len(self.name) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in self.name):
            raise MonitorValidationError("rule name is invalid")
        if not isinstance(self.expression, MonitorExpression) or not isinstance(self.scope, MonitorScope):
            raise MonitorValidationError("rule expression/scope is invalid")
        if type(self.version) is not int or not 1 <= self.version <= 1_000_000:
            raise MonitorValidationError("rule version must be an integer in 1..1000000")
        if not isinstance(self.severity, MonitorSeverity):
            raise MonitorValidationError("rule severity is invalid")
        if type(self.enabled) is not bool:
            raise MonitorValidationError("rule enabled must be boolean")
        for value, name, minimum, maximum in (
            (self.cooldown_sec, "cooldown_sec", 0, 86400),
            (self.duplicate_window_sec, "duplicate_window_sec", 0, 604800),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise MonitorValidationError(f"{name} is outside the allowed range")
        _aware(self.expires_at, "expires_at")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        allowed_channels = {"BROWSER", "WEBHOOK"}
        if not self.notification_channels or len(self.notification_channels) > 2:
            raise MonitorValidationError("notification_channels is invalid")
        if set(self.notification_channels) - allowed_channels:
            raise MonitorValidationError("unsupported notification channel")
        if len(set(self.notification_channels)) != len(self.notification_channels):
            raise MonitorValidationError("notification channels must be unique")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MonitorRule:
        required = {
            "rule_id",
            "name",
            "expression",
            "scope",
            "severity",
            "enabled",
            "cooldown_sec",
            "duplicate_window_sec",
            "expires_at",
            "notification_channels",
        }
        optional = {"version", "created_at", "updated_at"}
        if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
            raise MonitorValidationError("rule contains an invalid field set")
        if type(value["severity"]) is not str:
            raise MonitorValidationError("rule severity must be a string")
        try:
            severity = MonitorSeverity(value["severity"])
        except ValueError as exc:
            raise MonitorValidationError("unsupported rule severity") from exc

        def parse_time(raw: Any, name: str, default: datetime | None = None) -> datetime | None:
            if raw is None:
                return default
            if type(raw) is not str:
                raise MonitorValidationError(f"{name} must be ISO 8601 or null")
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MonitorValidationError(f"{name} must be ISO 8601") from exc
            return _aware(parsed, name)

        now = datetime.now(timezone.utc)
        channels = value["notification_channels"]
        if not isinstance(channels, list) or any(type(item) is not str for item in channels):
            raise MonitorValidationError("notification_channels must be a string array")
        return cls(
            rule_id=value["rule_id"],
            name=value["name"],
            expression=MonitorExpression.from_dict(value["expression"]),
            scope=MonitorScope.from_dict(value["scope"]),
            version=value.get("version", 1),
            severity=severity,
            enabled=value["enabled"],
            cooldown_sec=value["cooldown_sec"],
            duplicate_window_sec=value["duplicate_window_sec"],
            expires_at=parse_time(value["expires_at"], "expires_at"),
            notification_channels=tuple(channels),
            created_at=parse_time(value.get("created_at"), "created_at", now) or now,
            updated_at=parse_time(value.get("updated_at"), "updated_at", now) or now,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "expression": self.expression.as_dict(),
            "scope": self.scope.as_dict(),
            "version": self.version,
            "severity": self.severity.value,
            "enabled": self.enabled,
            "cooldown_sec": self.cooldown_sec,
            "duplicate_window_sec": self.duplicate_window_sec,
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
            "notification_channels": list(self.notification_channels),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def state_transition_allowed(current: InboxState, target: InboxState) -> bool:
    if current in _TERMINAL_STATES:
        return False
    allowed = {
        InboxState.NEW: {
            InboxState.ACKNOWLEDGED,
            InboxState.SNOOZED,
            InboxState.INVALIDATED,
            InboxState.EXPIRED,
            InboxState.RESOLVED,
        },
        InboxState.ACKNOWLEDGED: {
            InboxState.SNOOZED,
            InboxState.INVALIDATED,
            InboxState.EXPIRED,
            InboxState.RESOLVED,
        },
        InboxState.SNOOZED: {
            InboxState.NEW,
            InboxState.INVALIDATED,
            InboxState.EXPIRED,
            InboxState.RESOLVED,
        },
    }
    return target in allowed.get(current, set())
