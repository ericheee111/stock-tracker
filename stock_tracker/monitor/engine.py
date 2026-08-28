"""Non-eval monitor rule evaluation with cooldown and duplicate suppression."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sidecars.xtp.contracts import canonical_json_bytes, validate_symbol

from .contracts import (
    MonitorCondition,
    MonitorExpression,
    MonitorRule,
    MonitorValidationError,
    RuleLogic,
    RuleOperator,
    ScopeKind,
)
from .repository import MonitorRepository

_MISSING = object()


@dataclass(frozen=True, slots=True)
class MonitorEvaluation:
    rule_id: str
    symbol: str
    matched: bool
    suppressed: bool
    inbox: dict[str, Any] | None
    evidence: dict[str, Any]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "symbol": self.symbol,
            "matched": self.matched,
            "suppressed": self.suppressed,
            "inbox": self.inbox,
            "evidence": self.evidence,
            "reason": self.reason,
        }


def _fact_value(facts: Mapping[str, Any], path: str) -> Any:
    current: Any = facts
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _comparable(value: Any) -> bool:
    return value is None or type(value) in (bool, int, float, str)


def _compare(actual: Any, condition: MonitorCondition) -> bool:
    if actual is _MISSING:
        return False
    expected = condition.value
    operator = condition.operator
    if operator in {RuleOperator.EQ, RuleOperator.NE}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            if type(actual) is not type(expected):
                return False
        elif type(actual) in (int, float) and type(expected) in (int, float):
            pass
        elif type(actual) is not type(expected):
            return False
        return actual == expected if operator is RuleOperator.EQ else actual != expected
    if operator is RuleOperator.IN:
        return actual in expected
    if operator is RuleOperator.CONTAINS:
        if isinstance(actual, (list, tuple, set, frozenset, str)):
            return expected in actual
        return False
    if not _comparable(actual) or actual is None or expected is None:
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if type(actual) not in (int, float) or type(expected) not in (int, float):
        return False
    if operator is RuleOperator.GT:
        return actual > expected
    if operator is RuleOperator.GE:
        return actual >= expected
    if operator is RuleOperator.LT:
        return actual < expected
    if operator is RuleOperator.LE:
        return actual <= expected
    raise MonitorValidationError("unsupported operator")


def _evaluate_expression(
    expression: MonitorExpression,
    facts: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    condition_evidence: list[dict[str, Any]] = []
    results: list[bool] = []
    for condition in expression.conditions:
        actual = _fact_value(facts, condition.fact)
        present = actual is not _MISSING
        matched = _compare(actual, condition)
        results.append(matched)
        condition_evidence.append(
            {
                "fact": condition.fact,
                "present": present,
                "actual": actual if present else None,
                "operator": condition.operator.value,
                "expected": list(condition.value)
                if isinstance(condition.value, tuple)
                else condition.value,
                "matched": matched,
            }
        )
    matched = all(results) if expression.logic is RuleLogic.AND else any(results)
    return matched, {
        "logic": expression.logic.value,
        "matched_count": sum(1 for result in results if result),
        "condition_count": len(results),
        "conditions": condition_evidence,
    }


class MonitorEngine:
    """Evaluate existing decision/quality facts without mutating them."""

    def __init__(
        self,
        repository: MonitorRepository,
        *,
        publisher: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.repository = repository
        self._publisher = publisher

    @staticmethod
    def _scope_applies(
        rule: MonitorRule,
        *,
        symbol: str,
        market: str,
        watchlist: frozenset[str],
        positions: frozenset[str],
        all_market_universe: frozenset[str],
    ) -> bool:
        scope = rule.scope
        if market != scope.market:
            return False
        if scope.kind is ScopeKind.SYMBOLS:
            return symbol in scope.symbols
        if scope.kind in {ScopeKind.MARKET, ScopeKind.ALL_MARKET}:
            return (
                0 < len(all_market_universe) <= scope.max_symbols
                and symbol in all_market_universe
            )
        if scope.kind is ScopeKind.WATCHLIST:
            return len(watchlist) <= scope.max_symbols and symbol in watchlist
        if scope.kind is ScopeKind.POSITIONS:
            return len(positions) <= scope.max_symbols and symbol in positions
        return False

    @staticmethod
    def _dedup_key(rule: MonitorRule, symbol: str) -> str:
        value = {
            "schema": "stock-tracker-monitor-dedup-v2",
            "rule_id": rule.rule_id,
            "rule_version": rule.version,
            "symbol": symbol,
            "expression": rule.expression.as_dict(),
            "scope": rule.scope.as_dict(),
        }
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    def evaluate_rule(
        self,
        rule: MonitorRule,
        *,
        symbol: str,
        market: str,
        facts: Mapping[str, Any],
        watchlist: frozenset[str] = frozenset(),
        positions: frozenset[str] = frozenset(),
        all_market_universe: frozenset[str] = frozenset(),
        now: datetime | None = None,
    ) -> MonitorEvaluation:
        symbol = validate_symbol(symbol)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise MonitorValidationError("monitor clock must be timezone-aware")
        if not rule.enabled:
            return MonitorEvaluation(rule.rule_id, symbol, False, False, None, {}, "RULE_DISABLED")
        if rule.expires_at is not None and current >= rule.expires_at:
            self.repository.expire_rule_events(rule.rule_id, now=current)
            return MonitorEvaluation(rule.rule_id, symbol, False, False, None, {}, "RULE_EXPIRED")
        if not self._scope_applies(
            rule,
            symbol=symbol,
            market=market,
            watchlist=watchlist,
            positions=positions,
            all_market_universe=all_market_universe,
        ):
            return MonitorEvaluation(rule.rule_id, symbol, False, False, None, {}, "SCOPE_MISS")
        matched, evidence = _evaluate_expression(rule.expression, facts)
        if not matched:
            return MonitorEvaluation(rule.rule_id, symbol, False, False, None, evidence, "NO_MATCH")
        dedup_key = self._dedup_key(rule, symbol)
        matched_count = int(evidence["matched_count"])
        rule_snapshot_sha256 = hashlib.sha256(
            canonical_json_bytes(rule.as_dict())
        ).hexdigest()
        title = f"{rule.name} · {symbol}"
        summary = (
            f"{matched_count}/{len(rule.expression.conditions)} 条监控条件满足；"
            "该事件只进入监控收件箱，不改变动作状态、评分或订单。"
        )
        write_result = self.repository.record_trigger(
            rule=rule,
            symbol=symbol,
            market=market,
            title=title,
            summary=summary,
            evidence={
                "schema": "stock-tracker-monitor-evidence-v1",
                "rule_version": rule.version,
                "rule_snapshot_sha256": rule_snapshot_sha256,
                "facts": evidence,
                "action_state_mutated": False,
                "score_mutated": False,
                "order_created": False,
                "trust_tier_upgraded": False,
            },
            dedup_key=dedup_key,
            triggered_at=current,
            suppress_window_sec=max(
                rule.cooldown_sec,
                rule.duplicate_window_sec,
            ),
        )
        if write_result.suppressed:
            return MonitorEvaluation(
                rule.rule_id,
                symbol,
                True,
                True,
                None,
                evidence,
                write_result.reason or "COOLDOWN_OR_DUPLICATE_SUPPRESSED",
            )
        inbox = write_result.inbox
        if inbox is None:
            raise MonitorValidationError(
                "monitor trigger write returned no inbox"
            )
        if self._publisher is not None:
            self._publisher(
                "monitor.inbox",
                {
                    "schema": "stock-tracker-monitor-sse-v1",
                    "inbox": inbox,
                },
            )
        return MonitorEvaluation(rule.rule_id, symbol, True, False, inbox, evidence, "TRIGGERED")

    def evaluate_all(
        self,
        *,
        symbol: str,
        market: str,
        facts: Mapping[str, Any],
        watchlist: frozenset[str] = frozenset(),
        positions: frozenset[str] = frozenset(),
        all_market_universe: frozenset[str] = frozenset(),
        now: datetime | None = None,
    ) -> list[MonitorEvaluation]:
        rules = self.repository.list_rules(enabled_only=True)
        return [
            self.evaluate_rule(
                rule,
                symbol=symbol,
                market=market,
                facts=facts,
                watchlist=watchlist,
                positions=positions,
                all_market_universe=all_market_universe,
                now=now,
            )
            for rule in rules
        ]
