"""Immutable signal-outcome and real-evidence scoreboard contracts.

An outcome records what was decided, what was actually executable, the costed
fills, and the complete observable path.  Synthetic or paper outcomes may test
the contract but can never populate the real strategy scoreboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from stock_tracker.core.types import Market

from ..backtest.market_rules import TradeSide
from ..data.bar_artifact import DataTrustTier
from .fingerprint import fingerprint
from .time import ensure_aware, to_utc

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal(0)


class OutcomeContractError(ValueError):
    """Raised when outcome evidence is incomplete or internally inconsistent."""


class OutcomeEvidenceOrigin(StrEnum):
    LIVE_OBSERVED = "LIVE_OBSERVED"
    PAPER_RECORDED = "PAPER_RECORDED"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


class OutcomeState(StrEnum):
    NO_ENTRY = "NO_ENTRY"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"


class OutcomeTerminalReason(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"
    MANUAL = "MANUAL"
    TRAILING_STOP = "TRAILING_STOP"
    BROKEN_TREND = "BROKEN_TREND"
    DATA_INVALID = "DATA_INVALID"
    ORDER_REJECTED = "ORDER_REJECTED"


class ScoreboardState(StrEnum):
    INSUFFICIENT_REAL_EVIDENCE = "INSUFFICIENT_REAL_EVIDENCE"
    REAL_EVIDENCE_AVAILABLE = "REAL_EVIDENCE_AVAILABLE"


class OutcomeBucketKind(StrEnum):
    MARKET_REGIME = "MARKET_REGIME"
    CLASSIFICATION = "CLASSIFICATION"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OutcomeContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise OutcomeContractError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise OutcomeContractError(f"{name} must be lowercase SHA-256")
    return text


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OutcomeContractError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OutcomeContractError(f"{name} must be a non-negative integer")
    return value


def _require_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if type(value) is not Decimal:
        raise OutcomeContractError(
            f"{name} must be Decimal; float, integer and boolean are forbidden"
        )
    if not value.is_finite():
        raise OutcomeContractError(f"{name} must be finite")
    if positive and value <= _ZERO:
        raise OutcomeContractError(f"{name} must be positive")
    if nonnegative and value < _ZERO:
        raise OutcomeContractError(f"{name} must be non-negative")
    return value


def _require_symbol(symbol: object, market: Market) -> str:
    value = _require_text(symbol, "symbol")
    suffixes = {
        Market.A: (".SH", ".SZ"),
        Market.HK: (".HK",),
        Market.US: (".US",),
    }
    if value != value.upper() or not value.endswith(suffixes[market]):
        raise OutcomeContractError("symbol suffix must match market")
    return value


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


@dataclass(frozen=True, slots=True)
class TradeIntentEvidence:
    symbol: str
    market: Market
    side: TradeSide
    requested_at: datetime
    requested_quantity: int
    decision_snapshot_id: str
    execution_policy_id: str
    intent_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise OutcomeContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if not isinstance(self.side, TradeSide):
            raise OutcomeContractError("side must be TradeSide")
        ensure_aware(self.requested_at, "requested_at")
        _require_positive_int(self.requested_quantity, "requested_quantity")
        _require_sha256(self.decision_snapshot_id, "decision_snapshot_id")
        _require_sha256(self.execution_policy_id, "execution_policy_id")
        object.__setattr__(
            self,
            "intent_id",
            fingerprint(
                {
                    "schema": "trade-intent-evidence-v1",
                    "symbol": self.symbol,
                    "market": self.market,
                    "side": self.side,
                    "requested_at": to_utc(self.requested_at),
                    "requested_quantity": self.requested_quantity,
                    "decision_snapshot_id": self.decision_snapshot_id,
                    "execution_policy_id": self.execution_policy_id,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class OutcomeFillEvidence:
    intent_id: str
    symbol: str
    market: Market
    side: TradeSide
    timestamp: datetime
    session_index: int
    quantity: int
    reference_price: Decimal
    fill_price: Decimal
    explicit_cost: Decimal
    execution_rule_id: str
    cost_schedule_id: str
    raw_bar_snapshot_id: str
    implicit_cost: Decimal = field(init=False)
    fill_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "intent_id")
        if not isinstance(self.market, Market):
            raise OutcomeContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        if not isinstance(self.side, TradeSide):
            raise OutcomeContractError("side must be TradeSide")
        ensure_aware(self.timestamp, "timestamp")
        _require_nonnegative_int(self.session_index, "session_index")
        _require_positive_int(self.quantity, "quantity")
        _require_decimal(self.reference_price, "reference_price", positive=True)
        _require_decimal(self.fill_price, "fill_price", positive=True)
        _require_decimal(self.explicit_cost, "explicit_cost", nonnegative=True)
        adverse_delta = (
            self.fill_price - self.reference_price
            if self.side is TradeSide.BUY
            else self.reference_price - self.fill_price
        )
        implicit_cost = max(_ZERO, adverse_delta) * Decimal(self.quantity)
        object.__setattr__(self, "implicit_cost", implicit_cost)
        for name in (
            "execution_rule_id",
            "cost_schedule_id",
            "raw_bar_snapshot_id",
        ):
            _require_sha256(getattr(self, name), name)
        object.__setattr__(
            self,
            "fill_id",
            fingerprint(
                {
                    "schema": "outcome-fill-evidence-v1",
                    "intent_id": self.intent_id,
                    "symbol": self.symbol,
                    "market": self.market,
                    "side": self.side,
                    "timestamp": to_utc(self.timestamp),
                    "session_index": self.session_index,
                    "quantity": self.quantity,
                    "reference_price": self.reference_price,
                    "fill_price": self.fill_price,
                    "explicit_cost": self.explicit_cost,
                    "implicit_cost": self.implicit_cost,
                    "execution_rule_id": self.execution_rule_id,
                    "cost_schedule_id": self.cost_schedule_id,
                    "raw_bar_snapshot_id": self.raw_bar_snapshot_id,
                }
            ),
        )

    @property
    def total_cost(self) -> Decimal:
        return self.explicit_cost + self.implicit_cost

    @property
    def all_in_unit_price(self) -> Decimal:
        # The observed fill already contains slippage/impact versus reference.
        # Only explicit fees are added/subtracted here to avoid double counting.
        unit_explicit_cost = self.explicit_cost / Decimal(self.quantity)
        if self.side is TradeSide.BUY:
            return self.fill_price + unit_explicit_cost
        return self.fill_price - unit_explicit_cost


@dataclass(frozen=True, slots=True)
class OutcomePathPoint:
    timestamp: datetime
    session_index: int
    high: Decimal
    low: Decimal
    close: Decimal
    observable: bool
    point_id: str = field(init=False)

    def __post_init__(self) -> None:
        ensure_aware(self.timestamp, "timestamp")
        _require_nonnegative_int(self.session_index, "session_index")
        for name in ("high", "low", "close"):
            _require_decimal(getattr(self, name), name, positive=True)
        if self.low > min(self.close, self.high) or self.high < max(
            self.close,
            self.low,
        ):
            raise OutcomeContractError("path point high/low/close are inconsistent")
        _require_bool(self.observable, "observable")
        object.__setattr__(
            self,
            "point_id",
            fingerprint(
                {
                    "schema": "outcome-path-point-v1",
                    "timestamp": to_utc(self.timestamp),
                    "session_index": self.session_index,
                    "high": self.high,
                    "low": self.low,
                    "close": self.close,
                    "observable": self.observable,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class OutcomeMetrics:
    entry_all_in_unit_price: Decimal
    exit_net_unit_price: Decimal
    realized_r: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    net_return: Decimal
    holding_sessions: int
    total_cost: Decimal
    metrics_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "entry_all_in_unit_price",
            "exit_net_unit_price",
        ):
            _require_decimal(getattr(self, name), name, positive=True)
        for name in ("realized_r", "mfe_r", "mae_r", "net_return"):
            _require_decimal(getattr(self, name), name)
        if self.mfe_r < self.mae_r:
            raise OutcomeContractError("mfe_r cannot be below mae_r")
        _require_nonnegative_int(self.holding_sessions, "holding_sessions")
        _require_decimal(self.total_cost, "total_cost", nonnegative=True)
        object.__setattr__(
            self,
            "metrics_id",
            fingerprint(
                {
                    "schema": "outcome-metrics-v1",
                    "entry_all_in_unit_price": self.entry_all_in_unit_price,
                    "exit_net_unit_price": self.exit_net_unit_price,
                    "realized_r": self.realized_r,
                    "mfe_r": self.mfe_r,
                    "mae_r": self.mae_r,
                    "net_return": self.net_return,
                    "holding_sessions": self.holding_sessions,
                    "total_cost": self.total_cost,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    signal_id: str
    strategy_id: str
    strategy_version: str
    horizon_sessions: int
    model_id: str | None
    evidence_tier: DataTrustTier
    symbol: str
    market: Market
    instrument_id: str
    identity_fact_id: str
    decision_snapshot_id: str
    data_snapshot_id: str
    policy_id: str
    market_regime: str
    classification_id: str | None
    recorded_at: datetime
    entry_intent: TradeIntentEvidence
    entry_fill: OutcomeFillEvidence | None
    exit_intent: TradeIntentEvidence | None
    exit_fill: OutcomeFillEvidence | None
    path: tuple[OutcomePathPoint, ...]
    path_complete: bool
    invalidation_price: Decimal | None
    terminal_reason: OutcomeTerminalReason | None
    origin: OutcomeEvidenceOrigin
    verified: bool
    synthetic_fixture_only: bool
    verification_evidence_ids: tuple[str, ...]
    state: OutcomeState = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    risk_per_share: Decimal | None = field(init=False)
    metrics: OutcomeMetrics | None = field(init=False)
    outcome_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("signal_id", "strategy_id", "strategy_version"):
            _require_text(getattr(self, name), name)
        _require_positive_int(self.horizon_sessions, "horizon_sessions")
        if self.model_id is not None:
            _require_text(self.model_id, "model_id")
        if not isinstance(self.evidence_tier, DataTrustTier):
            raise OutcomeContractError("evidence_tier must be DataTrustTier")
        if not isinstance(self.market, Market):
            raise OutcomeContractError("market must be Market")
        _require_symbol(self.symbol, self.market)
        _require_text(self.instrument_id, "instrument_id")
        for name in (
            "identity_fact_id",
            "decision_snapshot_id",
            "data_snapshot_id",
            "policy_id",
        ):
            _require_sha256(getattr(self, name), name)
        _require_text(self.market_regime, "market_regime")
        if self.classification_id is not None:
            _require_text(self.classification_id, "classification_id")
        ensure_aware(self.recorded_at, "recorded_at")
        if not isinstance(self.entry_intent, TradeIntentEvidence):
            raise OutcomeContractError(
                "entry_intent must be TradeIntentEvidence"
            )
        if self.entry_intent.side is not TradeSide.BUY:
            raise OutcomeContractError("entry_intent must be BUY")
        if (
            self.entry_intent.symbol != self.symbol
            or self.entry_intent.market is not self.market
            or self.entry_intent.decision_snapshot_id != self.decision_snapshot_id
        ):
            raise OutcomeContractError(
                "entry intent identity must match outcome identity"
            )
        for optional, expected_type, name in (
            (self.entry_fill, OutcomeFillEvidence, "entry_fill"),
            (self.exit_intent, TradeIntentEvidence, "exit_intent"),
            (self.exit_fill, OutcomeFillEvidence, "exit_fill"),
        ):
            if optional is not None and not isinstance(optional, expected_type):
                raise OutcomeContractError(f"{name} has the wrong type")
        evidence_times = [self.entry_intent.requested_at]
        evidence_times.extend(point.timestamp for point in self.path)
        for optional in (self.entry_fill, self.exit_intent, self.exit_fill):
            if optional is not None:
                evidence_times.append(
                    optional.timestamp
                    if isinstance(optional, OutcomeFillEvidence)
                    else optional.requested_at
                )
        if to_utc(self.recorded_at) < max(to_utc(item) for item in evidence_times):
            raise OutcomeContractError(
                "recorded_at cannot precede bound intent, fill, or path evidence"
            )
        if any(not isinstance(item, OutcomePathPoint) for item in self.path):
            raise OutcomeContractError(
                "path must contain OutcomePathPoint values"
            )
        path_order = tuple(
            (to_utc(item.timestamp), item.session_index, item.point_id)
            for item in self.path
        )
        if path_order != tuple(sorted(path_order)):
            raise OutcomeContractError("path must be sorted by timestamp")
        if len({to_utc(item.timestamp) for item in self.path}) != len(self.path):
            raise OutcomeContractError("path timestamps must be unique")
        if any(
            current.session_index < previous.session_index
            for previous, current in zip(self.path, self.path[1:])
        ):
            raise OutcomeContractError("path session indices must be monotonic")
        _require_bool(self.path_complete, "path_complete")
        if self.invalidation_price is not None:
            _require_decimal(
                self.invalidation_price,
                "invalidation_price",
                positive=True,
            )
        if self.terminal_reason is not None and not isinstance(
            self.terminal_reason,
            OutcomeTerminalReason,
        ):
            raise OutcomeContractError(
                "terminal_reason must be OutcomeTerminalReason"
            )
        if not isinstance(self.origin, OutcomeEvidenceOrigin):
            raise OutcomeContractError(
                "origin must be OutcomeEvidenceOrigin"
            )
        _require_bool(self.verified, "verified")
        _require_bool(self.synthetic_fixture_only, "synthetic_fixture_only")
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.verification_evidence_ids
        ):
            raise OutcomeContractError(
                "verification_evidence_ids must contain lowercase SHA-256"
            )
        if self.verification_evidence_ids != tuple(
            sorted(set(self.verification_evidence_ids))
        ):
            raise OutcomeContractError(
                "verification_evidence_ids must be sorted and unique"
            )
        if self.verified and not self.verification_evidence_ids:
            raise OutcomeContractError(
                "verified outcome requires verification evidence"
            )
        if not self.verified and self.verification_evidence_ids:
            raise OutcomeContractError(
                "unverified outcome cannot carry verification evidence"
            )
        if self.origin is OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE:
            if not self.synthetic_fixture_only or self.verified:
                raise OutcomeContractError(
                    "synthetic origin must stay synthetic and unverified"
                )
        elif self.synthetic_fixture_only:
            raise OutcomeContractError(
                "non-synthetic origin cannot be labelled synthetic"
            )
        if self.origin is OutcomeEvidenceOrigin.PAPER_RECORDED and self.verified:
            raise OutcomeContractError("paper outcome cannot be verified as live")
        high_trust = {
            DataTrustTier.OPERATIONAL_VERIFIED,
            DataTrustTier.RESEARCH_GRADE,
            DataTrustTier.FROZEN_HOLDOUT,
        }
        if self.evidence_tier in high_trust and not self.verified:
            raise OutcomeContractError(
                "high-trust outcome evidence requires verification"
            )
        if self.origin in {
            OutcomeEvidenceOrigin.PAPER_RECORDED,
            OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
        } and self.evidence_tier is not DataTrustTier.BEST_EFFORT:
            raise OutcomeContractError(
                "paper and synthetic outcomes must remain BEST_EFFORT"
            )
        if (
            self.origin is OutcomeEvidenceOrigin.LIVE_OBSERVED
            and self.verified
            and self.evidence_tier not in high_trust
        ):
            raise OutcomeContractError(
                "verified live outcome requires operational-or-higher evidence"
            )

        blockers: set[str] = set()
        risk_per_share: Decimal | None = None
        metrics: OutcomeMetrics | None = None
        if self.entry_fill is None:
            if (
                self.exit_intent is not None
                or self.exit_fill is not None
                or self.path
                or self.path_complete
            ):
                raise OutcomeContractError(
                    "unfilled entry cannot carry exit or path evidence"
                )
            if self.terminal_reason not in {
                None,
                OutcomeTerminalReason.ORDER_REJECTED,
                OutcomeTerminalReason.DATA_INVALID,
            }:
                raise OutcomeContractError(
                    "unfilled entry has an invalid terminal reason"
                )
            state = OutcomeState.NO_ENTRY
            blockers.add("ENTRY_NOT_FILLED")
        else:
            entry = self.entry_fill
            if (
                entry.intent_id != self.entry_intent.intent_id
                or entry.side is not TradeSide.BUY
                or entry.symbol != self.symbol
                or entry.market is not self.market
                or to_utc(entry.timestamp) < to_utc(self.entry_intent.requested_at)
                or entry.quantity > self.entry_intent.requested_quantity
            ):
                raise OutcomeContractError("entry fill does not match entry intent")
            if self.invalidation_price is None:
                raise OutcomeContractError(
                    "filled entry requires invalidation_price"
                )
            entry_price = entry.all_in_unit_price
            if self.invalidation_price >= entry_price:
                raise OutcomeContractError(
                    "invalidation_price must be below the costed entry price"
                )
            risk_per_share = entry_price - self.invalidation_price
            if any(
                to_utc(point.timestamp) < to_utc(entry.timestamp)
                or point.session_index < entry.session_index
                for point in self.path
            ):
                raise OutcomeContractError("path precedes entry fill")
            if self.exit_fill is None:
                if self.exit_intent is not None and (
                    self.exit_intent.side is not TradeSide.SELL
                    or self.exit_intent.symbol != self.symbol
                    or self.exit_intent.market is not self.market
                    or to_utc(self.exit_intent.requested_at)
                    < to_utc(entry.timestamp)
                ):
                    raise OutcomeContractError(
                        "pending exit intent is inconsistent"
                    )
                if self.terminal_reason is not None or self.path_complete:
                    raise OutcomeContractError(
                        "open outcome cannot be terminal or path-complete"
                    )
                state = OutcomeState.OPEN
                blockers.add("EXIT_NOT_FILLED")
            else:
                if self.exit_intent is None:
                    raise OutcomeContractError(
                        "exit fill requires an exit intent"
                    )
                exit_fill = self.exit_fill
                if (
                    self.exit_intent.side is not TradeSide.SELL
                    or self.exit_intent.symbol != self.symbol
                    or self.exit_intent.market is not self.market
                    or exit_fill.intent_id != self.exit_intent.intent_id
                    or exit_fill.side is not TradeSide.SELL
                    or exit_fill.symbol != self.symbol
                    or exit_fill.market is not self.market
                    or exit_fill.quantity != entry.quantity
                    or self.exit_intent.requested_quantity != entry.quantity
                    or to_utc(self.exit_intent.requested_at) < to_utc(entry.timestamp)
                    or to_utc(exit_fill.timestamp)
                    < to_utc(self.exit_intent.requested_at)
                    or exit_fill.session_index < entry.session_index
                ):
                    raise OutcomeContractError(
                        "exit intent/fill is inconsistent with the open position"
                    )
                if self.terminal_reason is None:
                    raise OutcomeContractError(
                        "complete outcome requires terminal_reason"
                    )
                if self.terminal_reason is OutcomeTerminalReason.ORDER_REJECTED:
                    raise OutcomeContractError(
                        "filled exit cannot use ORDER_REJECTED as terminal reason"
                    )
                if not self.path_complete or not self.path:
                    raise OutcomeContractError(
                        "complete outcome requires a complete observable path"
                    )
                if any(
                    to_utc(point.timestamp) > to_utc(exit_fill.timestamp)
                    or point.session_index > exit_fill.session_index
                    for point in self.path
                ):
                    raise OutcomeContractError("path follows exit fill")
                observable = tuple(point for point in self.path if point.observable)
                if not observable:
                    raise OutcomeContractError(
                        "complete path requires at least one observable point"
                    )
                exit_price = exit_fill.all_in_unit_price
                risk = risk_per_share
                assert risk is not None
                metrics = OutcomeMetrics(
                    entry_all_in_unit_price=entry_price,
                    exit_net_unit_price=exit_price,
                    realized_r=(exit_price - entry_price) / risk,
                    mfe_r=(max(point.high for point in observable) - entry_price)
                    / risk,
                    mae_r=(min(point.low for point in observable) - entry_price)
                    / risk,
                    net_return=(exit_price - entry_price) / entry_price,
                    holding_sessions=(
                        exit_fill.session_index - entry.session_index
                    ),
                    total_cost=entry.total_cost + exit_fill.total_cost,
                )
                state = OutcomeState.COMPLETE

        if self.origin is OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE:
            blockers.add("SYNTHETIC_FIXTURE_ONLY")
        elif self.origin is OutcomeEvidenceOrigin.PAPER_RECORDED:
            blockers.add("PAPER_OUTCOME_ONLY")
        if not self.verified:
            blockers.add("OUTCOME_NOT_VERIFIED")
        blocker_tuple = tuple(sorted(blockers))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "blockers", blocker_tuple)
        object.__setattr__(self, "risk_per_share", risk_per_share)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(
            self,
            "outcome_id",
            fingerprint(
                {
                    "schema": "signal-outcome-v1",
                    "signal_id": self.signal_id,
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "horizon_sessions": self.horizon_sessions,
                    "model_id": self.model_id,
                    "evidence_tier": self.evidence_tier,
                    "symbol": self.symbol,
                    "market": self.market,
                    "instrument_id": self.instrument_id,
                    "identity_fact_id": self.identity_fact_id,
                    "decision_snapshot_id": self.decision_snapshot_id,
                    "data_snapshot_id": self.data_snapshot_id,
                    "policy_id": self.policy_id,
                    "market_regime": self.market_regime,
                    "classification_id": self.classification_id,
                    "recorded_at": to_utc(self.recorded_at),
                    "entry_intent_id": self.entry_intent.intent_id,
                    "entry_fill_id": (
                        None if self.entry_fill is None else self.entry_fill.fill_id
                    ),
                    "exit_intent_id": (
                        None if self.exit_intent is None else self.exit_intent.intent_id
                    ),
                    "exit_fill_id": (
                        None if self.exit_fill is None else self.exit_fill.fill_id
                    ),
                    "path_point_ids": [item.point_id for item in self.path],
                    "path_complete": self.path_complete,
                    "invalidation_price": self.invalidation_price,
                    "risk_per_share": risk_per_share,
                    "terminal_reason": self.terminal_reason,
                    "origin": self.origin,
                    "verified": self.verified,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "verification_evidence_ids": list(
                        self.verification_evidence_ids
                    ),
                    "state": state,
                    "blockers": blocker_tuple,
                    "metrics_id": None if metrics is None else metrics.metrics_id,
                }
            ),
        )

    @property
    def real_scoreboard_eligible(self) -> bool:
        return (
            self.state is OutcomeState.COMPLETE
            and self.origin is OutcomeEvidenceOrigin.LIVE_OBSERVED
            and self.verified
            and self.evidence_tier
            in {
                DataTrustTier.OPERATIONAL_VERIFIED,
                DataTrustTier.RESEARCH_GRADE,
                DataTrustTier.FROZEN_HOLDOUT,
            }
            and not self.synthetic_fixture_only
            and self.metrics is not None
        )


@dataclass(frozen=True, slots=True)
class OutcomeScoreboardPolicy:
    policy_version: str
    minimum_real_samples: int = 30
    minimum_bucket_samples: int = 5
    recent_window: int = 20
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        minimum = _require_positive_int(
            self.minimum_real_samples,
            "minimum_real_samples",
        )
        bucket = _require_positive_int(
            self.minimum_bucket_samples,
            "minimum_bucket_samples",
        )
        recent = _require_positive_int(self.recent_window, "recent_window")
        if bucket > minimum:
            raise OutcomeContractError(
                "minimum_bucket_samples cannot exceed minimum_real_samples"
            )
        if minimum > 100000 or recent > 100000:
            raise OutcomeContractError("scoreboard sample limits are unreasonable")
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "outcome-scoreboard-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_real_samples": self.minimum_real_samples,
                    "minimum_bucket_samples": self.minimum_bucket_samples,
                    "recent_window": self.recent_window,
                }
            ),
        )


DEFAULT_OUTCOME_SCOREBOARD_POLICY = OutcomeScoreboardPolicy(
    policy_version="outcome-scoreboard-v1"
)


@dataclass(frozen=True, slots=True)
class OutcomeBucketMetrics:
    kind: OutcomeBucketKind
    key: str
    sample_count: int
    win_rate: Decimal
    average_r: Decimal
    bucket_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OutcomeBucketKind):
            raise OutcomeContractError("kind must be OutcomeBucketKind")
        _require_text(self.key, "key")
        _require_positive_int(self.sample_count, "sample_count")
        _require_decimal(self.win_rate, "win_rate", nonnegative=True)
        if self.win_rate > Decimal(1):
            raise OutcomeContractError("win_rate cannot exceed one")
        _require_decimal(self.average_r, "average_r")
        object.__setattr__(
            self,
            "bucket_id",
            fingerprint(
                {
                    "schema": "outcome-bucket-metrics-v1",
                    "kind": self.kind,
                    "key": self.key,
                    "sample_count": self.sample_count,
                    "win_rate": self.win_rate,
                    "average_r": self.average_r,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class StrategyScoreboardMetrics:
    sample_count: int
    win_rate: Decimal
    average_r: Decimal
    median_r: Decimal
    net_expectancy_r: Decimal
    profit_factor_r: Decimal | None
    max_drawdown_r: Decimal
    recent_weighted_expectancy_r: Decimal
    average_holding_sessions: Decimal
    metrics_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_positive_int(self.sample_count, "sample_count")
        _require_decimal(self.win_rate, "win_rate", nonnegative=True)
        if self.win_rate > Decimal(1):
            raise OutcomeContractError("win_rate cannot exceed one")
        for name in (
            "average_r",
            "median_r",
            "net_expectancy_r",
            "recent_weighted_expectancy_r",
            "average_holding_sessions",
        ):
            _require_decimal(getattr(self, name), name)
        if self.profit_factor_r is not None:
            _require_decimal(
                self.profit_factor_r,
                "profit_factor_r",
                nonnegative=True,
            )
        _require_decimal(
            self.max_drawdown_r,
            "max_drawdown_r",
            nonnegative=True,
        )
        object.__setattr__(
            self,
            "metrics_id",
            fingerprint(
                {
                    "schema": "strategy-scoreboard-metrics-v1",
                    "sample_count": self.sample_count,
                    "win_rate": self.win_rate,
                    "average_r": self.average_r,
                    "median_r": self.median_r,
                    "net_expectancy_r": self.net_expectancy_r,
                    "profit_factor_r": self.profit_factor_r,
                    "max_drawdown_r": self.max_drawdown_r,
                    "recent_weighted_expectancy_r": (
                        self.recent_weighted_expectancy_r
                    ),
                    "average_holding_sessions": self.average_holding_sessions,
                }
            ),
        )


def _scoreboard_metrics(
    outcomes: tuple[SignalOutcome, ...],
    policy: OutcomeScoreboardPolicy,
) -> tuple[StrategyScoreboardMetrics, tuple[str, ...]]:
    metrics = tuple(outcome.metrics for outcome in outcomes)
    if any(item is None for item in metrics):
        raise OutcomeContractError("eligible outcome is missing metrics")
    resolved = tuple(item for item in metrics if item is not None)
    returns = tuple(item.realized_r for item in resolved)
    sample_count = len(returns)
    wins = sum(1 for value in returns if value > _ZERO)
    win_rate = Decimal(wins) / Decimal(sample_count)
    average = sum(returns, start=_ZERO) / Decimal(sample_count)
    gross_profit = sum(
        (value for value in returns if value > _ZERO),
        start=_ZERO,
    )
    gross_loss = -sum(
        (value for value in returns if value < _ZERO),
        start=_ZERO,
    )
    notes: set[str] = set()
    if gross_loss == _ZERO:
        profit_factor: Decimal | None = None
        notes.add("PROFIT_FACTOR_UNDEFINED_NO_LOSSES")
    else:
        profit_factor = gross_profit / gross_loss
    cumulative = _ZERO
    peak = _ZERO
    max_drawdown = _ZERO
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    recent = returns[-min(policy.recent_window, sample_count) :]
    weights = tuple(Decimal(index) for index in range(1, len(recent) + 1))
    weighted = sum(
        (value * weight for value, weight in zip(recent, weights)),
        start=_ZERO,
    ) / sum(weights, start=_ZERO)
    average_holding = sum(
        (Decimal(item.holding_sessions) for item in resolved),
        start=_ZERO,
    ) / Decimal(sample_count)
    return (
        StrategyScoreboardMetrics(
            sample_count=sample_count,
            win_rate=win_rate,
            average_r=average,
            median_r=_median(returns),
            net_expectancy_r=average,
            profit_factor_r=profit_factor,
            max_drawdown_r=max_drawdown,
            recent_weighted_expectancy_r=weighted,
            average_holding_sessions=average_holding,
        ),
        tuple(sorted(notes)),
    )


def _bucket_metrics(
    outcomes: tuple[SignalOutcome, ...],
    policy: OutcomeScoreboardPolicy,
) -> tuple[OutcomeBucketMetrics, ...]:
    groups: dict[tuple[OutcomeBucketKind, str], list[Decimal]] = {}
    for outcome in outcomes:
        assert outcome.metrics is not None
        groups.setdefault(
            (OutcomeBucketKind.MARKET_REGIME, outcome.market_regime),
            [],
        ).append(outcome.metrics.realized_r)
        if outcome.classification_id is not None:
            groups.setdefault(
                (OutcomeBucketKind.CLASSIFICATION, outcome.classification_id),
                [],
            ).append(outcome.metrics.realized_r)
    result: list[OutcomeBucketMetrics] = []
    for (kind, key), values in groups.items():
        if len(values) < policy.minimum_bucket_samples:
            continue
        returns = tuple(values)
        result.append(
            OutcomeBucketMetrics(
                kind=kind,
                key=key,
                sample_count=len(returns),
                win_rate=(
                    Decimal(sum(1 for value in returns if value > _ZERO))
                    / Decimal(len(returns))
                ),
                average_r=sum(returns, start=_ZERO) / Decimal(len(returns)),
            )
        )
    return tuple(sorted(result, key=lambda item: item.bucket_id))


@dataclass(frozen=True, slots=True)
class StrategyScoreboard:
    strategy_id: str
    strategy_version: str
    market: Market
    horizon_sessions: int
    model_id: str | None
    evidence_tier: DataTrustTier
    window_start: datetime
    window_end: datetime
    as_of: datetime
    policy: OutcomeScoreboardPolicy
    outcomes: tuple[SignalOutcome, ...]
    cohort_id: str = field(init=False)
    eligible_outcome_ids: tuple[str, ...] = field(init=False)
    excluded_counts: tuple[tuple[str, int], ...] = field(init=False)
    state: ScoreboardState = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    metric_notes: tuple[str, ...] = field(init=False)
    metrics: StrategyScoreboardMetrics | None = field(init=False)
    bucket_metrics: tuple[OutcomeBucketMetrics, ...] = field(init=False)
    scoreboard_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.strategy_id, "strategy_id")
        _require_text(self.strategy_version, "strategy_version")
        if not isinstance(self.market, Market):
            raise OutcomeContractError("market must be Market")
        _require_positive_int(self.horizon_sessions, "horizon_sessions")
        if self.model_id is not None:
            _require_text(self.model_id, "model_id")
        if not isinstance(self.evidence_tier, DataTrustTier):
            raise OutcomeContractError("evidence_tier must be DataTrustTier")
        ensure_aware(self.window_start, "window_start")
        ensure_aware(self.window_end, "window_end")
        ensure_aware(self.as_of, "as_of")
        if to_utc(self.window_end) < to_utc(self.window_start):
            raise OutcomeContractError("window_end cannot precede window_start")
        if to_utc(self.as_of) < to_utc(self.window_end):
            raise OutcomeContractError("as_of cannot precede window_end")
        if not isinstance(self.policy, OutcomeScoreboardPolicy):
            raise OutcomeContractError(
                "policy must be OutcomeScoreboardPolicy"
            )
        if any(not isinstance(item, SignalOutcome) for item in self.outcomes):
            raise OutcomeContractError("outcomes must contain SignalOutcome values")
        normalized = tuple(sorted(self.outcomes, key=lambda item: item.outcome_id))
        if len({item.outcome_id for item in normalized}) != len(normalized):
            raise OutcomeContractError("outcome_id must be unique")
        signal_ids = tuple(sorted(item.signal_id for item in normalized))
        if len(set(signal_ids)) != len(normalized):
            raise OutcomeContractError(
                "one scoreboard cannot count the same signal more than once"
            )
        cohort_id = fingerprint(
            {
                "schema": "strategy-scoreboard-cohort-v1",
                "signal_ids": list(signal_ids),
            }
        )
        object.__setattr__(self, "cohort_id", cohort_id)
        if len({item.policy_id for item in normalized}) > 1:
            raise OutcomeContractError(
                "scoreboard cannot mix decision policy identities"
            )
        for outcome in normalized:
            if (
                outcome.strategy_id != self.strategy_id
                or outcome.strategy_version != self.strategy_version
            ):
                raise OutcomeContractError(
                    "scoreboard cannot mix strategy identities"
                )
            if outcome.market is not self.market:
                raise OutcomeContractError("scoreboard cannot mix markets")
            if outcome.horizon_sessions != self.horizon_sessions:
                raise OutcomeContractError("scoreboard cannot mix horizons")
            if outcome.model_id != self.model_id:
                raise OutcomeContractError("scoreboard cannot mix model identities")
            if outcome.evidence_tier is not self.evidence_tier:
                raise OutcomeContractError("scoreboard cannot mix evidence tiers")
            if not (
                to_utc(self.window_start)
                <= to_utc(outcome.recorded_at)
                <= to_utc(self.window_end)
            ):
                raise OutcomeContractError(
                    "outcome recorded_at is outside scoreboard window"
                )
            if to_utc(outcome.recorded_at) > to_utc(self.as_of):
                raise OutcomeContractError("future outcome cannot enter scoreboard")
            if (
                outcome.exit_fill is not None
                and to_utc(outcome.exit_fill.timestamp) > to_utc(self.as_of)
            ):
                raise OutcomeContractError("future exit cannot enter scoreboard")
        object.__setattr__(self, "outcomes", normalized)

        eligible = tuple(
            sorted(
                (
                    outcome
                    for outcome in normalized
                    if outcome.real_scoreboard_eligible
                ),
                key=lambda item: (
                    to_utc(item.exit_fill.timestamp)
                    if item.exit_fill is not None
                    else to_utc(item.recorded_at),
                    item.outcome_id,
                ),
            )
        )
        excluded: dict[str, int] = {}
        for outcome in normalized:
            if outcome in eligible:
                continue
            if outcome.state is not OutcomeState.COMPLETE:
                reason = "NON_COMPLETE"
            elif outcome.synthetic_fixture_only:
                reason = "SYNTHETIC"
            elif outcome.origin is OutcomeEvidenceOrigin.PAPER_RECORDED:
                reason = "PAPER"
            elif not outcome.verified:
                reason = "UNVERIFIED"
            else:
                reason = "OTHER_INELIGIBLE"
            excluded[reason] = excluded.get(reason, 0) + 1
        excluded_counts = tuple(sorted(excluded.items()))
        eligible_ids = tuple(item.outcome_id for item in eligible)
        blockers: set[str] = set()
        metric_notes: tuple[str, ...] = ()
        metrics: StrategyScoreboardMetrics | None = None
        buckets: tuple[OutcomeBucketMetrics, ...] = ()
        if len(eligible) < self.policy.minimum_real_samples:
            state = ScoreboardState.INSUFFICIENT_REAL_EVIDENCE
            blockers.add(
                "INSUFFICIENT_REAL_EVIDENCE:"
                f"{len(eligible)}/{self.policy.minimum_real_samples}"
            )
        else:
            state = ScoreboardState.REAL_EVIDENCE_AVAILABLE
            metrics, metric_notes = _scoreboard_metrics(eligible, self.policy)
            buckets = _bucket_metrics(eligible, self.policy)
        object.__setattr__(self, "eligible_outcome_ids", eligible_ids)
        object.__setattr__(self, "excluded_counts", excluded_counts)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "blockers", tuple(sorted(blockers)))
        object.__setattr__(self, "metric_notes", metric_notes)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "bucket_metrics", buckets)
        object.__setattr__(
            self,
            "scoreboard_id",
            fingerprint(
                {
                    "schema": "strategy-scoreboard-v1",
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "market": self.market,
                    "horizon_sessions": self.horizon_sessions,
                    "model_id": self.model_id,
                    "evidence_tier": self.evidence_tier,
                    "cohort_id": self.cohort_id,
                    "window_start": to_utc(self.window_start),
                    "window_end": to_utc(self.window_end),
                    "as_of": to_utc(self.as_of),
                    "policy_id": self.policy.policy_id,
                    "outcome_ids": [item.outcome_id for item in normalized],
                    "eligible_outcome_ids": list(eligible_ids),
                    "excluded_counts": excluded_counts,
                    "state": state,
                    "blockers": tuple(sorted(blockers)),
                    "metric_notes": metric_notes,
                    "metrics_id": None if metrics is None else metrics.metrics_id,
                    "bucket_ids": [item.bucket_id for item in buckets],
                }
            ),
        )


__all__ = [
    "DEFAULT_OUTCOME_SCOREBOARD_POLICY",
    "OutcomeBucketKind",
    "OutcomeBucketMetrics",
    "OutcomeContractError",
    "OutcomeEvidenceOrigin",
    "OutcomeFillEvidence",
    "OutcomeMetrics",
    "OutcomePathPoint",
    "OutcomeScoreboardPolicy",
    "OutcomeState",
    "OutcomeTerminalReason",
    "ScoreboardState",
    "SignalOutcome",
    "StrategyScoreboard",
    "StrategyScoreboardMetrics",
    "TradeIntentEvidence",
]
