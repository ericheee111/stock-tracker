"""Bounded read-only daily-window diagnostics, never an execution or PIT authority.

The current runtime Bar has no proven known-at, adjustment basis or calendar
coverage. This module exposes numerical sample availability, not a validated
multi-horizon strategy. Same-day rows are conservatively excluded; naive dates
remain explicitly labelled legacy dates and are never silently made UTC instants.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ..core.market_time import MARKET_SESSION_LABEL_POLICY_V1, market_session_date
from ..core.types import Bar, Market
from . import indicators as I

DIAGNOSTIC_SCHEMA = 'daily-window-diagnostics-v1'
MAX_DIAGNOSTIC_BARS = 260
WINDOWS = (20, 60, 120, 252)
METHODS = (
    ('sma20', 'SMA20', 20, 'ARITHMETIC_MEAN'),
    ('sma60', 'SMA60', 60, 'ARITHMETIC_MEAN'),
    ('ema12', 'EMA12', 12, 'SMA_SEEDED_LEGACY_SHORT_MEAN'),
    ('ema26', 'EMA26', 26, 'SMA_SEEDED_LEGACY_SHORT_MEAN'),
    ('rsi14', 'RSI14', 15, 'ROLLING_MEAN_NOT_WILDER_FLAT_100'),
    ('atr14', 'ATR14', 15, 'ROLLING_MEAN_NOT_WILDER'),
    ('macd_dif', 'MACD DIF', 26, 'SMA_SEEDED_12_26'),
    ('macd_hist', 'MACD HIST', 34, 'DIF_MINUS_DEA_NOT_TIMES_TWO'),
    ('roc60', 'ROC60', 61, 'PERCENT_CHANGE_60'),
)


def daily_window_diagnostics(bars: list[Bar], symbol: str, market: Market,
                             computed_at: datetime) -> dict[str, Any]:
    """Use one supplied bounded daily cache snapshot; no sorting/dedup/infill."""
    if type(computed_at) is not datetime or computed_at.tzinfo is None or computed_at.utcoffset() is None:
        raise ValueError('computed_at requires an aware clock')
    if type(market) is not Market or type(symbol) is not str or not symbol:
        raise ValueError('exact market and symbol required')
    cutoff = market_session_date(computed_at, market, MARKET_SESSION_LABEL_POLICY_V1)
    result: dict[str, Any] = {
        'schema': DIAGNOSTIC_SCHEMA, 'symbol': symbol, 'market': market.value, 'interval': '1d',
        'computed_at': computed_at.astimezone(timezone.utc).isoformat(),
        'policy_id': I.INDICATOR_INPUT_POLICY_ID, 'status': 'NO_DATA',
        'input_count': len(bars) if type(bars) is list else 0, 'sample_count': 0,
        'same_day_excluded': 0, 'first_date': None, 'last_date': None,
        'age_calendar_days': None, 'time_basis': 'UNAVAILABLE', 'sources': [],
        'issues': [], 'warnings': ['RUNTIME_CACHE_NOT_PIT', 'ADJUSTMENT_BASIS_UNVERIFIED',
                                  'CALENDAR_CONTINUITY_UNVERIFIED', 'NOT_A_T_SIGNAL'],
        'windows': [], 'methods': [], 'assurance': 'RUNTIME_DIAGNOSTIC_ONLY',
        'calendar_coverage_verified': False, 'execution_authorized': False,
        'investment_performance_claim': False, 'auto_trade': False,
    }
    if type(bars) is not list or len(bars) > MAX_DIAGNOSTIC_BARS:
        result.update(status='INVALID_INPUT', issues=['INVALID_CONTAINER_OR_LIMIT'])
        return result
    if not bars:
        return result
    accepted: list[Bar] = []
    dates: list[date] = []
    previous: date | None = None
    naive_flags: set[bool] = set()
    sources: set[str] = set()
    issues: set[str] = set()
    for bar in bars:
        if type(bar) is not Bar or bar.symbol != symbol or bar.market is not market or bar.interval != '1d':
            issues.add('BAR_IDENTITY_OR_INTERVAL_MISMATCH')
            continue
        if type(bar.timestamp) is not datetime:
            issues.add('INVALID_TIMESTAMP')
            continue
        naive = bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None
        naive_flags.add(naive)
        day = bar.timestamp.date() if naive else market_session_date(bar.timestamp, market, MARKET_SESSION_LABEL_POLICY_V1)
        if previous is not None and day <= previous:
            issues.add('DUPLICATE_OR_UNORDERED_DAILY_DATE')
        previous = day
        if day > cutoff:
            issues.add('FUTURE_BAR_DATE')
        prices = (bar.open, bar.high, bar.low, bar.close)
        if (not all(I.valid_number(p) and p > 0 for p in prices)
                or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
                or type(bar.volume) is not int or bar.volume < 0
                or not I.valid_number(bar.adjustment_factor) or bar.adjustment_factor <= 0):
            issues.add('INVALID_OHLCV')
        if type(bar.source) is not str or not bar.source.strip():
            issues.add('SOURCE_UNAVAILABLE')
        else:
            sources.add(bar.source)
        if day == cutoff:
            result['same_day_excluded'] += 1
        elif day < cutoff:
            accepted.append(bar)
            dates.append(day)
    if len(naive_flags) > 1:
        issues.add('MIXED_TIMESTAMP_BASIS')
    if len(sources) > 1:
        issues.add('MIXED_SOURCE_SERIES')
    result.update(sources=sorted(sources), time_basis='LEGACY_NAIVE_DATE' if naive_flags == {True} else 'MARKET_LOCAL_DATE')
    if True in naive_flags:
        result['warnings'].append('NAIVE_DATE_NOT_AN_OBSERVED_INSTANT')
    if issues:
        result.update(status='INVALID_INPUT', issues=sorted(issues))
        return result
    n = len(accepted)
    result['sample_count'] = n
    if not n:
        result['issues'] = ['NO_PRIOR_DAY_SAMPLES']
        return result
    closes = [bar.close for bar in accepted]
    result.update(status='NUMERIC_ONLY', first_date=dates[0].isoformat(), last_date=dates[-1].isoformat(),
                  age_calendar_days=(cutoff-dates[-1]).days)
    for window in WINDOWS:
        mean = I.sma(closes, window)
        change = I.roc(closes, window)
        state = 'INSUFFICIENT_SAMPLES' if n < window else 'NUMERIC_ONLY' if mean is not None else 'NUMERIC_UNAVAILABLE'
        result['windows'].append({'sample_window': window, 'available_samples': min(n, window),
                                  'state': state, 'mean_close': mean, 'change_percent': change,
                                  'change_required_samples': window+1})
    for key, label, warmup, method in METHODS:
        result['methods'].append({'key': key, 'label': label, 'method_id': method,
                                  'required_samples': warmup, 'available_samples': min(n, warmup),
                                  'status': 'SAMPLE_COUNT_MET' if n >= warmup else 'WARMUP_REQUIRED'})
    return result
