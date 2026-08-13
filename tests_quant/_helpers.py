"""Shared deterministic fixtures for quant contract tests."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from stock_tracker.core.types import Bar, DataStatus, Market
from stock_tracker.quant.backtest import (
    CostSchedule,
    CostScheduleBook,
    ExecutionBar,
    ExecutionEngine,
    MarketRule,
    MarketRuleBook,
)
from stock_tracker.quant.core.calendar import (
    CalendarCoverage,
    CalendarDay,
    CalendarStatus,
    SessionKind,
)

UTC = timezone.utc
A_TZ = ZoneInfo("Asia/Shanghai")
HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")


def utc_datetime(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def exchange_datetime(
    session_date: date,
    market: Market,
    hour: int = 15,
    minute: int = 0,
) -> datetime:
    zone = {Market.A: A_TZ, Market.HK: HK_TZ, Market.US: US_TZ}[market]
    return datetime.combine(session_date, time(hour, minute), tzinfo=zone)


def make_bar(
    session_date: date,
    *,
    symbol: str = "600000.SH",
    market: Market = Market.A,
    open_price: float = 10.0,
    high: float = 10.2,
    low: float = 9.8,
    close: float = 10.0,
    volume: int = 10_000,
    source: str = "fixture",
) -> Bar:
    close_hour = 16 if market is Market.US else 15
    return Bar(
        symbol=symbol,
        market=market,
        timestamp=exchange_datetime(session_date, market, close_hour),
        interval="1d",
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=close * volume,
        turnover=1.0,
        source=source,
        adjustment_factor=1.0,
        quality_status=DataStatus.LIVE,
    )


def market_rule(
    market: Market = Market.A,
    *,
    t_plus_one: bool | None = None,
    price_limit_required: bool | None = None,
) -> MarketRule:
    return MarketRule(
        rule_id=f"{market.value}-fixture-rule",
        market=market,
        effective_from=date(2000, 1, 1),
        effective_to=None,
        currency={Market.A: "CNY", Market.HK: "HKD", Market.US: "USD"}[market],
        lot_size=100 if market is Market.A else 1,
        settlement_days=1,
        sell_t_plus_one=(market is Market.A if t_plus_one is None else t_plus_one),
        price_limit_state_required=(
            market is Market.A
            if price_limit_required is None
            else price_limit_required
        ),
        verified=True,
        source_note="synthetic fixture only",
    )


def zero_cost_schedule(market: Market = Market.A) -> CostSchedule:
    return CostSchedule(
        schedule_id=f"{market.value}-fixture-cost",
        market=market,
        effective_from=date(2000, 1, 1),
        effective_to=None,
        commission_bps=0.0,
        minimum_commission=0.0,
        sell_tax_bps=0.0,
        exchange_fee_bps=0.0,
        transfer_fee_bps=0.0,
        half_spread_bps=0.0,
        slippage_bps=0.0,
        impact_coefficient=0.0,
        max_participation_rate=1.0,
        verified=True,
        source_note="synthetic fixture only",
    )


def execution_engine(
    market: Market = Market.A,
    *,
    rule: MarketRule | None = None,
    cost: CostSchedule | None = None,
) -> ExecutionEngine:
    return ExecutionEngine(
        MarketRuleBook((rule or market_rule(market),)),
        CostScheduleBook((cost or zero_cost_schedule(market),)),
    )


def execution_bars(
    bars: tuple[Bar, ...] | list[Bar],
    *,
    unlocked: bool = True,
) -> tuple[ExecutionBar, ...]:
    return tuple(
        ExecutionBar(
            bar,
            locked_limit_up=False if unlocked else None,
            locked_limit_down=False if unlocked else None,
        )
        for bar in bars
    )


def calendar_coverage(
    start: date,
    end: date,
    *,
    market: Market = Market.A,
    known_at: datetime | None = None,
    revision: int | str = 1,
    version: str = "fixture-v1",
    verified: bool = True,
) -> CalendarCoverage:
    return CalendarCoverage(
        market=market,
        start_date=start,
        end_date=end,
        source="fixture-calendar",
        calendar_version=version,
        known_at=known_at or utc_datetime(2025, 1, 1),
        revision=revision,
        verified=verified,
        source_note="synthetic fixture only" if verified else "",
    )


def calendar_day(
    session_date: date,
    *,
    status: CalendarStatus,
    market: Market = Market.A,
    known_at: datetime | None = None,
    revision: int | str = 1,
    version: str = "fixture-v1",
    verified: bool = True,
) -> CalendarDay:
    if status is CalendarStatus.OPEN:
        open_hour = 9
        close_hour = 16 if market is Market.US else 15
        open_time = exchange_datetime(session_date, market, open_hour, 30)
        close_time = exchange_datetime(session_date, market, close_hour)
    else:
        open_time = None
        close_time = None
    return CalendarDay(
        market=market,
        session_date=session_date,
        status=status,
        open_time=open_time,
        close_time=close_time,
        session_kind=SessionKind.REGULAR,
        known_at=known_at or utc_datetime(2025, 1, 1),
        source="fixture-calendar",
        revision=revision,
        calendar_version=version,
        verified=verified,
        source_note="synthetic fixture only" if verified else "",
    )
