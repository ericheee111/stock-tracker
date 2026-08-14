"""Strict product-layer contracts for the Stage 1 decision engine.

This module is intentionally side-effect free: importing it never opens the
runtime database, performs network I/O, or touches the quantitative research
pipeline.  It translates verified runtime inputs into stable product-facing
contracts without pretending that an uncalibrated score is a probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from stock_tracker.core.types import DataStatus, Market, SignalState


class DecisionContractError(ValueError):
    """Raised when a decision-layer value violates a fail-closed contract."""


class ActionState(StrEnum):
    """Product-facing action vocabulary frozen by PRD v1.0."""

    WATCH = "WATCH"
    WAIT_PULLBACK = "WAIT_PULLBACK"
    WAIT_BREAKOUT = "WAIT_BREAKOUT"
    EXECUTABLE = "EXECUTABLE"
    HOLD = "HOLD"
    WARNING = "WARNING"
    TRIM = "TRIM"
    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    TREND_RUNNER = "TREND_RUNNER"
    EXIT = "EXIT"
    AVOID = "AVOID"
    DATA_BLOCKED = "DATA_BLOCKED"


class RiskMode(StrEnum):
    """User-selectable presentation/risk posture.

    Hard data, market-rule, liquidity, and tradability blockers are never
    relaxed by this value.
    """

    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


class BlockerSeverity(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class ProbabilityEvidenceLevel(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RankingMode(StrEnum):
    RULE_EVIDENCE = "RULE_EVIDENCE"
    CALIBRATED_PROBABILITY = "CALIBRATED_PROBABILITY"


class BigTrendState(StrEnum):
    """Reserved product contract; Stage 1 does not claim to compute it."""

    NONE = "NONE"
    EMERGING = "EMERGING"
    CONFIRMING = "CONFIRMING"
    TRENDING = "TRENDING"
    MATURE = "MATURE"
    DISTRIBUTING = "DISTRIBUTING"
    BROKEN = "BROKEN"


_CORE_ACTION_STATES = frozenset(
    {
        ActionState.EXECUTABLE,
        ActionState.WAIT_PULLBACK,
        ActionState.WAIT_BREAKOUT,
        ActionState.WATCH,
        ActionState.AVOID,
        ActionState.DATA_BLOCKED,
    }
)
_HOLDING_ACTION_STATES = frozenset(
    {
        ActionState.HOLD,
        ActionState.WARNING,
        ActionState.TRIM,
        ActionState.PARTIAL_TAKE_PROFIT,
        ActionState.TREND_RUNNER,
        ActionState.EXIT,
        ActionState.DATA_BLOCKED,
    }
)


def _require_string(name: str, value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise DecisionContractError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise DecisionContractError(f"{name} must not be empty")
    return value


def _require_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise DecisionContractError(f"{name} must be a real boolean")
    return value


def _require_finite_number(
    name: str,
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise DecisionContractError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise DecisionContractError(f"{name} must be finite")
    if strictly_positive and number <= 0:
        raise DecisionContractError(f"{name} must be greater than zero")
    if minimum is not None and number < minimum:
        raise DecisionContractError(f"{name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise DecisionContractError(f"{name} must be <= {maximum}")
    return number


def _require_aware_datetime(name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise DecisionContractError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DecisionContractError(f"{name} must be timezone-aware")
    return value


def _require_enum(name: str, value: object, enum_type: type[StrEnum]) -> StrEnum:
    if not isinstance(value, enum_type):
        raise DecisionContractError(f"{name} must be {enum_type.__name__}")
    return value


def _require_symbol_market(symbol: object, market: object) -> str:
    normalized = _require_string("symbol", symbol).strip().upper()
    _require_enum("market", market, Market)
    code, separator, suffix = normalized.rpartition(".")
    valid_suffixes = {
        Market.A: {"SH", "SZ"},
        Market.HK: {"HK"},
        Market.US: {"US"},
    }
    if not separator or not code or suffix not in valid_suffixes[market]:
        raise DecisionContractError("symbol suffix must match market")
    return normalized


def _require_tuple(name: str, value: object) -> tuple:
    if type(value) is not tuple:
        raise DecisionContractError(f"{name} must be a tuple")
    return value


@dataclass(frozen=True, slots=True)
class DecisionBlocker:
    """A structured reason that limits or blocks an action."""

    code: str
    message: str
    severity: BlockerSeverity
    recoverable: bool = True

    def __post_init__(self) -> None:
        _require_string("code", self.code)
        _require_string("message", self.message)
        _require_enum("severity", self.severity, BlockerSeverity)
        _require_bool("recoverable", self.recoverable)


@dataclass(frozen=True, slots=True)
class UserPortfolioProfile:
    """User-entered account constraints used for deterministic position sizing."""

    account_equity: float
    available_cash: float
    risk_mode: RiskMode = RiskMode.BALANCED
    per_trade_risk_pct: float = 0.005
    max_position_pct: float = 0.20
    max_portfolio_heat_pct: float = 0.06
    max_sector_pct: float = 0.35
    max_theme_pct: float = 0.35
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        equity = _require_finite_number(
            "account_equity", self.account_equity, strictly_positive=True
        )
        cash = _require_finite_number("available_cash", self.available_cash, minimum=0.0)
        if cash > equity:
            raise DecisionContractError(
                "available_cash cannot exceed account_equity for the current cash-account contract"
            )
        _require_enum("risk_mode", self.risk_mode, RiskMode)
        per_trade = _require_finite_number(
            "per_trade_risk_pct", self.per_trade_risk_pct, strictly_positive=True, maximum=1.0
        )
        heat = _require_finite_number(
            "max_portfolio_heat_pct",
            self.max_portfolio_heat_pct,
            strictly_positive=True,
            maximum=1.0,
        )
        if per_trade > heat:
            raise DecisionContractError(
                "per_trade_risk_pct cannot exceed max_portfolio_heat_pct"
            )
        _require_finite_number(
            "max_position_pct", self.max_position_pct, strictly_positive=True, maximum=1.0
        )
        _require_finite_number(
            "max_sector_pct", self.max_sector_pct, strictly_positive=True, maximum=1.0
        )
        _require_finite_number(
            "max_theme_pct", self.max_theme_pct, strictly_positive=True, maximum=1.0
        )
        _require_aware_datetime("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    """Auditable result of one long-only position-sizing calculation."""

    allowed: bool
    shares: int
    lot_size: int
    entry_price: float
    invalidation_price: float
    risk_per_share: float
    risk_budget_amount: float
    actual_risk_amount: float
    actual_risk_pct: float
    position_value: float
    position_pct: float
    limiting_factors: tuple[str, ...] = ()
    blockers: tuple[DecisionBlocker, ...] = ()

    def __post_init__(self) -> None:
        _require_bool("allowed", self.allowed)
        if type(self.shares) is not int or self.shares < 0:
            raise DecisionContractError("shares must be a non-negative integer")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise DecisionContractError("lot_size must be a positive integer")

        entry = _require_finite_number(
            "entry_price", self.entry_price, strictly_positive=True
        )
        invalidation = _require_finite_number(
            "invalidation_price", self.invalidation_price, strictly_positive=True
        )
        if entry <= invalidation:
            raise DecisionContractError(
                "entry_price must be greater than invalidation_price"
            )
        risk_per_share = _require_finite_number(
            "risk_per_share", self.risk_per_share, strictly_positive=True
        )
        expected_risk_per_share = entry - invalidation
        if not math.isclose(
            risk_per_share,
            expected_risk_per_share,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise DecisionContractError(
                "risk_per_share must equal entry_price - invalidation_price"
            )

        risk_budget = _require_finite_number(
            "risk_budget_amount", self.risk_budget_amount, minimum=0.0
        )
        actual_risk = _require_finite_number(
            "actual_risk_amount", self.actual_risk_amount, minimum=0.0
        )
        _require_finite_number(
            "actual_risk_pct", self.actual_risk_pct, minimum=0.0, maximum=1.0
        )
        position_value = _require_finite_number(
            "position_value", self.position_value, minimum=0.0
        )
        _require_finite_number(
            "position_pct", self.position_pct, minimum=0.0, maximum=1.0
        )
        _require_tuple("limiting_factors", self.limiting_factors)
        _require_tuple("blockers", self.blockers)
        for item in self.limiting_factors:
            _require_string("limiting_factor", item)
        for blocker in self.blockers:
            if not isinstance(blocker, DecisionBlocker):
                raise DecisionContractError("blockers must contain DecisionBlocker values")
            if blocker.severity is not BlockerSeverity.HARD:
                raise DecisionContractError(
                    "position-size blockers must be HARD blockers"
                )

        if self.shares % self.lot_size != 0:
            raise DecisionContractError("shares must be aligned to lot_size")
        if self.allowed:
            if self.shares <= 0:
                raise DecisionContractError(
                    "allowed position must contain at least one share"
                )
            if self.blockers:
                raise DecisionContractError("allowed position cannot contain blockers")
            expected_actual_risk = self.shares * risk_per_share
            expected_position_value = self.shares * entry
            if not math.isclose(
                actual_risk,
                expected_actual_risk,
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise DecisionContractError(
                    "actual_risk_amount must equal shares * risk_per_share"
                )
            if not math.isclose(
                position_value,
                expected_position_value,
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise DecisionContractError(
                    "position_value must equal shares * entry_price"
                )
            if actual_risk > risk_budget and not math.isclose(
                actual_risk,
                risk_budget,
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise DecisionContractError(
                    "actual_risk_amount cannot exceed risk_budget_amount"
                )
        else:
            if self.shares != 0:
                raise DecisionContractError("blocked position must contain zero shares")
            if not self.blockers:
                raise DecisionContractError("blocked position must explain its blocker")
            if any(
                not math.isclose(value, 0.0, abs_tol=1e-12)
                for value in (
                    actual_risk,
                    self.actual_risk_pct,
                    position_value,
                    self.position_pct,
                )
            ):
                raise DecisionContractError(
                    "blocked position must have zero risk and position values"
                )


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """Deterministic product action derived from a runtime signal."""

    action: ActionState
    source_state: SignalState
    has_position: bool
    actionable: bool
    reason: str
    data_status: DataStatus
    blockers: tuple[DecisionBlocker, ...] = ()

    def __post_init__(self) -> None:
        _require_enum("action", self.action, ActionState)
        _require_enum("source_state", self.source_state, SignalState)
        _require_bool("has_position", self.has_position)
        _require_bool("actionable", self.actionable)
        _require_string("reason", self.reason)
        _require_enum("data_status", self.data_status, DataStatus)
        _require_tuple("blockers", self.blockers)
        for blocker in self.blockers:
            if not isinstance(blocker, DecisionBlocker):
                raise DecisionContractError("blockers must contain DecisionBlocker values")
        if self.action is ActionState.EXECUTABLE:
            if self.has_position:
                raise DecisionContractError(
                    "EXECUTABLE is only valid when no position exists"
                )
            if self.data_status is not DataStatus.LIVE:
                raise DecisionContractError("EXECUTABLE requires LIVE data")
            if not self.actionable:
                raise DecisionContractError("EXECUTABLE must be actionable")
        if self.action in _HOLDING_ACTION_STATES - {ActionState.DATA_BLOCKED}:
            if not self.has_position:
                raise DecisionContractError(
                    f"{self.action.value} requires an existing position"
                )
        if self.action is ActionState.DATA_BLOCKED and self.actionable:
            raise DecisionContractError("DATA_BLOCKED cannot be actionable")
        if any(
            blocker.severity is BlockerSeverity.HARD for blocker in self.blockers
        ) and self.actionable:
            raise DecisionContractError(
                "an action with a HARD blocker cannot be actionable"
            )


@dataclass(frozen=True, slots=True)
class PlanVariant:
    """One deterministic plan variant, without natural-language hallucination."""

    name: str
    action: ActionState
    risk_budget_multiplier: float
    note: str

    def __post_init__(self) -> None:
        _require_string("name", self.name)
        _require_enum("action", self.action, ActionState)
        _require_finite_number(
            "risk_budget_multiplier",
            self.risk_budget_multiplier,
            strictly_positive=True,
            maximum=1.0,
        )
        _require_string("note", self.note)


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Product-facing plan generated from a verified runtime signal."""

    symbol: str
    market: Market
    strategy_id: str
    action: ActionState
    entry_low: float
    entry_high: float
    trigger_price: float
    no_chase_above: float
    invalidation_price: float
    target_1: float
    target_2: float
    reward_risk: float
    next_trigger: str
    position_size: Optional[PositionSizeResult]
    balanced_plan: PlanVariant
    aggressive_plan: Optional[PlanVariant]
    hard_blockers: tuple[DecisionBlocker, ...]
    soft_blockers: tuple[DecisionBlocker, ...]
    calibrated_probability: Optional[float]
    probability_evidence_level: ProbabilityEvidenceLevel
    data_status: DataStatus
    as_of: datetime

    def __post_init__(self) -> None:
        normalized_symbol = _require_symbol_market(self.symbol, self.market)
        if self.symbol != normalized_symbol:
            raise DecisionContractError("symbol must use canonical uppercase form")
        _require_string("strategy_id", self.strategy_id)
        _require_enum("action", self.action, ActionState)
        prices: dict[str, float] = {}
        for name in (
            "entry_low",
            "entry_high",
            "trigger_price",
            "no_chase_above",
            "invalidation_price",
            "target_1",
            "target_2",
        ):
            prices[name] = _require_finite_number(
                name, getattr(self, name), strictly_positive=True
            )
        if prices["entry_low"] > prices["entry_high"]:
            raise DecisionContractError("entry_low cannot exceed entry_high")
        if prices["invalidation_price"] >= prices["entry_low"]:
            raise DecisionContractError(
                "invalidation_price must be below the long entry zone"
            )
        if prices["no_chase_above"] < prices["entry_high"]:
            raise DecisionContractError(
                "no_chase_above cannot be below the entry zone"
            )
        if prices["trigger_price"] > prices["no_chase_above"]:
            raise DecisionContractError(
                "trigger_price cannot exceed no_chase_above"
            )
        if (
            prices["target_1"] <= prices["entry_high"]
            or prices["target_2"] < prices["target_1"]
        ):
            raise DecisionContractError(
                "long targets must be ordered above the entry zone"
            )
        _require_finite_number(
            "reward_risk", self.reward_risk, strictly_positive=True
        )
        _require_string("next_trigger", self.next_trigger, allow_empty=True)
        if self.position_size is not None and not isinstance(
            self.position_size, PositionSizeResult
        ):
            raise DecisionContractError("position_size must be PositionSizeResult or None")
        if not isinstance(self.balanced_plan, PlanVariant):
            raise DecisionContractError("balanced_plan must be PlanVariant")
        if self.balanced_plan.action is not self.action:
            raise DecisionContractError(
                "balanced_plan action must match the trade plan action"
            )
        if self.aggressive_plan is not None and not isinstance(
            self.aggressive_plan, PlanVariant
        ):
            raise DecisionContractError("aggressive_plan must be PlanVariant or None")
        _require_tuple("hard_blockers", self.hard_blockers)
        _require_tuple("soft_blockers", self.soft_blockers)
        for blocker in (*self.hard_blockers, *self.soft_blockers):
            if not isinstance(blocker, DecisionBlocker):
                raise DecisionContractError("blocker tuples must contain DecisionBlocker")
        if any(b.severity is not BlockerSeverity.HARD for b in self.hard_blockers):
            raise DecisionContractError("hard_blockers must contain only HARD blockers")
        if any(b.severity is not BlockerSeverity.SOFT for b in self.soft_blockers):
            raise DecisionContractError("soft_blockers must contain only SOFT blockers")
        if self.hard_blockers and self.aggressive_plan is not None:
            raise DecisionContractError("aggressive_plan cannot bypass hard blockers")
        if self.hard_blockers and self.action is ActionState.EXECUTABLE:
            raise DecisionContractError(
                "EXECUTABLE cannot coexist with hard blockers"
            )
        _require_enum("data_status", self.data_status, DataStatus)
        if self.data_status is not DataStatus.LIVE and self.action is ActionState.EXECUTABLE:
            raise DecisionContractError("EXECUTABLE requires LIVE data")
        if self.position_size is not None:
            if not math.isclose(
                self.position_size.invalidation_price,
                prices["invalidation_price"],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise DecisionContractError(
                    "position_size invalidation_price must match the trade plan"
                )
            if not (
                prices["entry_low"]
                <= self.position_size.entry_price
                <= prices["no_chase_above"]
            ):
                raise DecisionContractError(
                    "position_size entry_price must be within the plan execution range"
                )
            if self.hard_blockers and self.position_size.allowed:
                raise DecisionContractError(
                    "hard blockers require a blocked position size"
                )
            if not self.position_size.allowed:
                surfaced = {
                    (blocker.code, blocker.message)
                    for blocker in self.hard_blockers
                }
                missing = [
                    blocker
                    for blocker in self.position_size.blockers
                    if (blocker.code, blocker.message) not in surfaced
                ]
                if missing:
                    raise DecisionContractError(
                        "blocked position-size reasons must be surfaced as hard blockers"
                    )
        if self.aggressive_plan is not None:
            if not self.soft_blockers:
                raise DecisionContractError(
                    "aggressive_plan requires at least one soft blocker"
                )
            if self.aggressive_plan.action not in _CORE_ACTION_STATES:
                raise DecisionContractError(
                    "aggressive_plan must use a new-opportunity action"
                )
            if (
                self.aggressive_plan.action is ActionState.EXECUTABLE
                and self.data_status is not DataStatus.LIVE
            ):
                raise DecisionContractError(
                    "aggressive EXECUTABLE plan requires LIVE data"
                )
            if (
                self.aggressive_plan.risk_budget_multiplier
                >= self.balanced_plan.risk_budget_multiplier
            ):
                raise DecisionContractError(
                    "aggressive risk budget must be lower than the balanced risk budget"
                )
        if self.calibrated_probability is not None:
            _require_finite_number(
                "calibrated_probability",
                self.calibrated_probability,
                minimum=0.0,
                maximum=1.0,
            )
            if self.probability_evidence_level is ProbabilityEvidenceLevel.INSUFFICIENT:
                raise DecisionContractError(
                    "non-null calibrated_probability requires a non-insufficient evidence level"
                )
        elif self.probability_evidence_level is not ProbabilityEvidenceLevel.INSUFFICIENT:
            raise DecisionContractError(
                "null calibrated_probability requires INSUFFICIENT evidence level"
            )
        _require_enum(
            "probability_evidence_level",
            self.probability_evidence_level,
            ProbabilityEvidenceLevel,
        )
        _require_aware_datetime("as_of", self.as_of)


@dataclass(frozen=True, slots=True)
class DecisionAction:
    """One ranked decision item included in the daily brief."""

    symbol: str
    market: Market
    action: ActionState
    strategy_id: str
    opportunity: int
    timing: int
    risk: int
    confidence: int
    reward_risk: float
    freshness: float
    sector: str
    reason: str
    trade_plan: Optional[TradePlan]
    data_status: DataStatus
    big_trend_state: BigTrendState = BigTrendState.NONE

    def __post_init__(self) -> None:
        normalized_symbol = _require_symbol_market(self.symbol, self.market)
        if self.symbol != normalized_symbol:
            raise DecisionContractError("symbol must use canonical uppercase form")
        _require_enum("action", self.action, ActionState)
        _require_string("strategy_id", self.strategy_id)
        for name in ("opportunity", "timing", "risk", "confidence"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 100:
                raise DecisionContractError(f"{name} must be an integer in [0, 100]")
        _require_finite_number("reward_risk", self.reward_risk, minimum=0.0)
        _require_finite_number("freshness", self.freshness, minimum=0.0, maximum=1.0)
        _require_string("sector", self.sector, allow_empty=True)
        _require_string("reason", self.reason)
        if self.trade_plan is not None and not isinstance(self.trade_plan, TradePlan):
            raise DecisionContractError("trade_plan must be TradePlan or None")
        _require_enum("data_status", self.data_status, DataStatus)
        _require_enum("big_trend_state", self.big_trend_state, BigTrendState)
        if self.action is ActionState.EXECUTABLE and self.data_status is not DataStatus.LIVE:
            raise DecisionContractError("EXECUTABLE requires LIVE data")
        if self.action in {
            ActionState.EXECUTABLE,
            ActionState.WAIT_PULLBACK,
            ActionState.WAIT_BREAKOUT,
        } and self.trade_plan is None:
            raise DecisionContractError(
                f"{self.action.value} requires a trade plan"
            )
        if self.trade_plan is not None:
            if self.trade_plan.symbol != self.symbol:
                raise DecisionContractError("trade_plan symbol must match action symbol")
            if self.trade_plan.market is not self.market:
                raise DecisionContractError("trade_plan market must match action market")
            if self.trade_plan.strategy_id != self.strategy_id:
                raise DecisionContractError(
                    "trade_plan strategy_id must match action strategy_id"
                )
            if self.trade_plan.action is not self.action:
                raise DecisionContractError("trade_plan action must match action")
            if self.trade_plan.data_status is not self.data_status:
                raise DecisionContractError(
                    "trade_plan data_status must match action data_status"
                )
            if not math.isclose(
                self.trade_plan.reward_risk,
                float(self.reward_risk),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise DecisionContractError(
                    "trade_plan reward_risk must match action reward_risk"
                )


@dataclass(frozen=True, slots=True)
class DecisionBrief:
    """Deterministic input/output contract for the Today Action page."""

    as_of: datetime
    market_posture: str
    aggression_level: int
    core_opportunities: tuple[DecisionAction, ...]
    holding_actions: tuple[DecisionAction, ...]
    avoid_reasons: tuple[str, ...]
    data_health: DataStatus
    ranking_mode: RankingMode
    summary_facts: tuple[str, ...]
    ai_summary: Optional[str] = None

    def __post_init__(self) -> None:
        _require_aware_datetime("as_of", self.as_of)
        _require_string("market_posture", self.market_posture)
        if type(self.aggression_level) is not int or not 0 <= self.aggression_level <= 100:
            raise DecisionContractError("aggression_level must be an integer in [0, 100]")
        for name in (
            "core_opportunities",
            "holding_actions",
            "avoid_reasons",
            "summary_facts",
        ):
            _require_tuple(name, getattr(self, name))
        for action in (*self.core_opportunities, *self.holding_actions):
            if not isinstance(action, DecisionAction):
                raise DecisionContractError("decision action tuples must contain DecisionAction")
        for item in (*self.avoid_reasons, *self.summary_facts):
            _require_string("brief text item", item)
        _require_enum("data_health", self.data_health, DataStatus)
        _require_enum("ranking_mode", self.ranking_mode, RankingMode)
        if len(self.core_opportunities) > 5:
            raise DecisionContractError(
                "core_opportunities cannot contain more than five items"
            )
        core_symbols = [action.symbol for action in self.core_opportunities]
        holding_symbols = [action.symbol for action in self.holding_actions]
        if len(core_symbols) != len(set(core_symbols)):
            raise DecisionContractError(
                "core_opportunities must be unique by symbol"
            )
        if len(holding_symbols) != len(set(holding_symbols)):
            raise DecisionContractError(
                "holding_actions must be unique by symbol"
            )
        if set(core_symbols) & set(holding_symbols):
            raise DecisionContractError(
                "a symbol cannot appear in both core and holding actions"
            )
        if any(
            action.action not in _CORE_ACTION_STATES
            for action in self.core_opportunities
        ):
            raise DecisionContractError(
                "core_opportunities contains a holding-only action"
            )
        if any(
            action.action not in _HOLDING_ACTION_STATES
            for action in self.holding_actions
        ):
            raise DecisionContractError(
                "holding_actions contains a new-opportunity action"
            )
        all_calibrated = bool(self.core_opportunities) and all(
            action.trade_plan is not None
            and action.trade_plan.calibrated_probability is not None
            for action in self.core_opportunities
        )
        expected_ranking_mode = (
            RankingMode.CALIBRATED_PROBABILITY
            if all_calibrated
            else RankingMode.RULE_EVIDENCE
        )
        if self.ranking_mode is not expected_ranking_mode:
            raise DecisionContractError(
                "ranking_mode does not match the probability evidence in core actions"
            )
        if self.ai_summary is not None:
            _require_string("ai_summary", self.ai_summary)
