"""Legacy indicator formulae with explicit, versioned numeric-domain guards.

These functions are descriptors, not votes or calibrated probabilities. RSI and
ATR retain rolling arithmetic means (not Wilder smoothing); MACD hist is DIF-DEA.
EMA retains its historical short-series mean; flat RSI retains 100. Formula changes
require a separate versioned comparison. Unknown/invalid inputs return None.
"""
from __future__ import annotations

import math

INDICATOR_INPUT_POLICY_ID = "runtime-indicator-input-v2-legacy-formulas"
MAX_PERIOD = 1_000_000


def valid_number(value: object) -> bool:
    """Accept only explicit finite built-in numeric values, never bool/coercion."""
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _series(values: object) -> bool:
    return (isinstance(values, (list, tuple)) and type(values) in (list, tuple)
            and bool(values) and all(valid_number(v) for v in values))


def _period(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_PERIOD


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def sma(values: list[float], period: int) -> float | None:
    """Complete-window arithmetic mean, or unknown."""
    if not _period(period) or not _series(values) or len(values) < period:
        return None
    return _finite(sum(values[-period:]) / period)


def ema(values: list[float], period: int) -> float | None:
    """SMA-seeded EMA; legacy short-series mean is not a fully warmed EMA."""
    if not _period(period) or not _series(values):
        return None
    if len(values) < period:
        return _finite(sum(values) / len(values))
    k = 2.0 / (period + 1)
    previous = sum(values[:period]) / period
    for value in values[period:]:
        previous = value * k + previous * (1 - k)
    return _finite(previous)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float | None, float | None, float | None]:
    """SMA-seeded DIF, signal EMA, DIF-DEA (not twice the histogram)."""
    if (not all(_period(p) for p in (fast, slow, signal)) or fast >= slow
            or not _series(values) or len(values) < slow):
        return None, None, None
    fast_k, slow_k = 2.0 / (fast + 1), 2.0 / (slow + 1)
    pf, ps = sum(values[:fast]) / fast, sum(values[:slow]) / slow
    difs: list[float] = []
    for i, value in enumerate(values):
        pf = value * fast_k + pf * (1 - fast_k) if i >= fast else pf
        ps = value * slow_k + ps * (1 - slow_k) if i >= slow else ps
        if i >= slow - 1:
            difs.append(pf - ps)
    if not _series(difs):
        return None, None, None
    if len(difs) < signal:
        return difs[-1], None, None
    dea, dif = ema(difs, signal), difs[-1]
    hist = _finite(dif - dea) if dea is not None else None
    return dif, dea, hist


def rsi(values: list[float], period: int = 14) -> float | None:
    """Rolling arithmetic RSI. Legacy zero-loss rule is 100, including flat."""
    if not _period(period) or not _series(values) or len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / period, losses / period
    if not math.isfinite(avg_gain) or not math.isfinite(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    ratio = avg_gain / avg_loss
    return _finite(100.0 - (100.0 / (1.0 + ratio)))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Arithmetic true-range average. Misaligned arrays cannot be truncated."""
    if not _period(period) or not all(_series(s) for s in (highs, lows, closes)):
        return None
    n = len(highs)
    if n != len(lows) or n != len(closes) or n < period + 1:
        return None
    if any(not lo <= c <= hi for hi, lo, c in zip(highs, lows, closes, strict=True)):
        return None
    ranges = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
              for i in range(1, n)]
    return _finite(sum(ranges[-period:]) / period)


def roc(values: list[float], period: int) -> float | None:
    """Change in percent, requiring period+1 samples and a nonzero base."""
    if not _period(period) or not _series(values) or len(values) <= period:
        return None
    base = values[-period-1]
    if base == 0:
        return None
    return _finite((values[-1] - base) / base * 100.0)


def rolling_percentile(values: list[float], window: int, pct: float) -> float | None:
    """Legacy rounded-index order statistic; short window uses available samples."""
    if (not _period(window) or not valid_number(pct) or not 0 <= pct <= 100
            or not _series(values) or len(values) < 2):
        return None
    selected = values[-window:] if len(values) >= window else values
    ordered = sorted(selected)
    index = max(0, min(len(ordered)-1, round((pct/100.0)*(len(ordered)-1))))
    return ordered[index]


def _std(values: list[float], *, sample: bool) -> float | None:
    if not _series(values) or len(values) < (2 if sample else 1):
        return None
    try:
        mean = sum(values) / len(values)
        variance = sum((x-mean)**2 for x in values) / (len(values)-int(sample))
        return _finite(math.sqrt(variance))
    except (OverflowError, ValueError):
        return None


def stdev(values: list[float]) -> float | None:
    """Sample standard deviation, ddof=1."""
    return _std(values, sample=True)


def stdev_pop(values: list[float]) -> float | None:
    """Population standard deviation, ddof=0."""
    return _std(values, sample=False)
