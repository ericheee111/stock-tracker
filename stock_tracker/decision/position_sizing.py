from __future__ import annotations

import math

from stock_tracker.core.types import Market

from .types import (
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    PositionSizeResult,
    UserPortfolioProfile,
)


def _finite_number(
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


def _validated_lot_size(market: Market, lot_size: object | None) -> int:
    if not isinstance(market, Market):
        raise DecisionContractError("market must be Market")
    if lot_size is None:
        if market is Market.A:
            return 100
        if market is Market.US:
            return 1
        raise DecisionContractError("lot_size is required for HK securities")
    if type(lot_size) is not int or lot_size <= 0:
        raise DecisionContractError("lot_size must be a positive integer")
    return lot_size


def _validated_hard_blockers(value: object) -> tuple[DecisionBlocker, ...]:
    if type(value) is not tuple:
        raise DecisionContractError("hard_blockers must be a tuple")
    for blocker in value:
        if not isinstance(blocker, DecisionBlocker):
            raise DecisionContractError("hard_blockers must contain DecisionBlocker values")
        if blocker.severity is not BlockerSeverity.HARD:
            raise DecisionContractError("hard_blockers must contain only HARD blockers")
    return value


def _blocked_result(
    *,
    entry_price: float,
    invalidation_price: float,
    lot_size: int,
    risk_budget_amount: float,
    risk_per_share: float,
    limiting_factors: tuple[str, ...],
    blockers: tuple[DecisionBlocker, ...],
) -> PositionSizeResult:
    return PositionSizeResult(
        allowed=False,
        shares=0,
        lot_size=lot_size,
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        risk_per_share=risk_per_share,
        risk_budget_amount=risk_budget_amount,
        actual_risk_amount=0.0,
        actual_risk_pct=0.0,
        position_value=0.0,
        position_pct=0.0,
        limiting_factors=limiting_factors,
        blockers=blockers,
    )


def size_position(
    profile: UserPortfolioProfile,
    *,
    market: Market,
    entry_price: float,
    invalidation_price: float,
    current_portfolio_heat_pct: float = 0.0,
    current_sector_exposure_pct: float = 0.0,
    current_theme_exposure_pct: float = 0.0,
    lot_size: int | None = None,
    liquidity_max_shares: int | None = None,
    risk_budget_multiplier: float = 1.0,
    hard_blockers: tuple[DecisionBlocker, ...] = (),
) -> PositionSizeResult:
    if not isinstance(profile, UserPortfolioProfile):
        raise DecisionContractError("profile must be UserPortfolioProfile")
    entry = _finite_number("entry_price", entry_price, strictly_positive=True)
    invalidation = _finite_number(
        "invalidation_price", invalidation_price, strictly_positive=True
    )
    if entry <= invalidation:
        raise DecisionContractError("entry_price must be greater than invalidation_price")
    heat = _finite_number(
        "current_portfolio_heat_pct",
        current_portfolio_heat_pct,
        minimum=0.0,
        maximum=1.0,
    )
    sector = _finite_number(
        "current_sector_exposure_pct",
        current_sector_exposure_pct,
        minimum=0.0,
        maximum=1.0,
    )
    theme = _finite_number(
        "current_theme_exposure_pct",
        current_theme_exposure_pct,
        minimum=0.0,
        maximum=1.0,
    )
    multiplier = _finite_number(
        "risk_budget_multiplier",
        risk_budget_multiplier,
        strictly_positive=True,
        maximum=1.0,
    )
    resolved_lot_size = _validated_lot_size(market, lot_size)
    blockers = _validated_hard_blockers(hard_blockers)
    if liquidity_max_shares is not None and (
        type(liquidity_max_shares) is not int or liquidity_max_shares < 0
    ):
        raise DecisionContractError("liquidity_max_shares must be a non-negative integer or None")

    equity = float(profile.account_equity)
    risk_per_share = entry - invalidation
    risk_budget_amount = equity * float(profile.per_trade_risk_pct) * multiplier

    if blockers:
        return _blocked_result(
            entry_price=entry,
            invalidation_price=invalidation,
            lot_size=resolved_lot_size,
            risk_budget_amount=risk_budget_amount,
            risk_per_share=risk_per_share,
            limiting_factors=("HARD_BLOCKER",),
            blockers=blockers,
        )

    share_limits: dict[str, float] = {
        "PER_TRADE_RISK": risk_budget_amount / risk_per_share,
        "AVAILABLE_CASH": float(profile.available_cash) / entry,
        "MAX_POSITION": equity * float(profile.max_position_pct) / entry,
        "PORTFOLIO_HEAT": (
            equity * max(0.0, float(profile.max_portfolio_heat_pct) - heat)
            / risk_per_share
        ),
        "SECTOR_EXPOSURE": (
            equity * max(0.0, float(profile.max_sector_pct) - sector) / entry
        ),
        "THEME_EXPOSURE": (
            equity * max(0.0, float(profile.max_theme_pct) - theme) / entry
        ),
    }
    if liquidity_max_shares is not None:
        share_limits["LIQUIDITY"] = float(liquidity_max_shares)

    raw_shares = min(share_limits.values())
    shares = math.floor(raw_shares / resolved_lot_size) * resolved_lot_size
    limiting_factors = tuple(
        name
        for name, limit in share_limits.items()
        if math.isclose(limit, raw_shares, rel_tol=1e-12, abs_tol=1e-12)
    )
    if shares < raw_shares:
        limiting_factors = (*limiting_factors, "LOT_SIZE")
    if shares < resolved_lot_size:
        blocker = DecisionBlocker(
            code="BELOW_MINIMUM_LOT",
            message="Available risk or capital is insufficient for one trading lot",
            severity=BlockerSeverity.HARD,
            recoverable=True,
        )
        return _blocked_result(
            entry_price=entry,
            invalidation_price=invalidation,
            lot_size=resolved_lot_size,
            risk_budget_amount=risk_budget_amount,
            risk_per_share=risk_per_share,
            limiting_factors=tuple(dict.fromkeys((*limiting_factors, "LOT_SIZE"))),
            blockers=(blocker,),
        )

    position_value = shares * entry
    actual_risk_amount = shares * risk_per_share
    return PositionSizeResult(
        allowed=True,
        shares=shares,
        lot_size=resolved_lot_size,
        entry_price=entry,
        invalidation_price=invalidation,
        risk_per_share=risk_per_share,
        risk_budget_amount=risk_budget_amount,
        actual_risk_amount=actual_risk_amount,
        actual_risk_pct=actual_risk_amount / equity,
        position_value=position_value,
        position_pct=position_value / equity,
        limiting_factors=limiting_factors,
        blockers=(),
    )


class PositionSizer:
    def __init__(self, profile: UserPortfolioProfile) -> None:
        if not isinstance(profile, UserPortfolioProfile):
            raise DecisionContractError("profile must be UserPortfolioProfile")
        self.profile = profile

    def size(self, **kwargs: object) -> PositionSizeResult:
        return size_position(self.profile, **kwargs)

    calculate = size
