"""Legacy indicator formulas with explicit input and arithmetic failure behavior.

Missing/invalid values return None (three Nones for MACD), never manufactured zero.
RSI and ATR use rolling arithmetic averages, NOT Wilder smoothing. EMA's short-
window mean, flat RSI=100 and histogram=DIF-DEA remain compatibility conventions.
No statistical independence, forecasting skill or third-party equivalence is implied.
"""
from __future__ import annotations

import math
from typing import cast

INDICATOR_INPUT_POLICY_ID = "runtime-indicator-input-v2-legacy-formulas"
INDICATOR_FORMULA_VERSION = "runtime-indicator-legacy-formulas-v1"
MAX_PERIOD = 1_000_000


def valid_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(cast(float, value))
    except OverflowError:
        return False


def _series(values: object) -> tuple[float, ...] | None:
    if type(values) not in (list, tuple):
        return None
    frozen = tuple(cast("list[float] | tuple[float, ...]", values))
    return frozen if frozen and all(valid_number(v) for v in frozen) else None


def _period(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_PERIOD


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def sma(values: list[float], period: int) -> float | None:
    """Simple average of the complete requested window."""
    data = _series(values)
    if not _period(period) or data is None or len(data) < period:
        return None
    try:
        return _finite(sum(data[-period:]) / period)
    except OverflowError:
        return None


def ema(values: list[float], period: int) -> float | None:
    """SMA-seeded EMA; legacy short input returns its mean, not a warmed-up EMA."""
    data = _series(values)
    if not _period(period) or data is None:
        return None
    try:
        if len(data) < period:
            return _finite(sum(data) / len(data))
        k = 2.0 / (period + 1)
        previous = sum(data[:period]) / period
        for value in data[period:]:
            previous = value * k + previous * (1 - k)
        return _finite(previous)
    except OverflowError:
        return None


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float | None, float | None, float | None]:
    """DIF, signal EMA, DIF-DEA; slow+signal-1 samples for the full default tuple."""
    data = _series(values)
    missing = (None, None, None)
    if (not all(_period(p) for p in (fast, slow, signal)) or fast >= slow
            or data is None or len(data) < slow):
        return missing
    try:
        fk, sk = 2.0 / (fast + 1), 2.0 / (slow + 1)
        pf, ps = sum(data[:fast]) / fast, sum(data[:slow]) / slow
        difs: list[float] = []
        for i, value in enumerate(data):
            pf = value * fk + pf * (1 - fk) if i >= fast else pf
            ps = value * sk + ps * (1 - sk) if i >= slow else ps
            if i >= slow - 1:
                difs.append(pf - ps)
        if not all(math.isfinite(d) for d in difs):
            return missing
        if len(difs) < signal:
            return difs[-1], None, None
        dea = ema(difs, signal)
        return difs[-1], dea, _finite(difs[-1] - dea) if dea is not None else None
    except OverflowError:
        return missing


def rsi(values: list[float], period: int = 14) -> float | None:
    """Rolling-average RSI. Zero-loss convention is 100, including flat input."""
    data = _series(values)
    if not _period(period) or data is None or len(data) < period + 1:
        return None
    try:
        gains = losses = 0.0
        for i in range(-period, 0):
            change = data[i] - data[i - 1]
            if change >= 0:
                gains += change
            else:
                losses -= change
        avg_gain, avg_loss = gains / period, losses / period
        if not math.isfinite(avg_gain) or not math.isfinite(avg_loss):
            return None
        if (gains > 0 and avg_gain == 0) or (losses > 0 and avg_loss == 0):
            return None  # Arithmetic underflow is not an all-gain/flat observation.
        if avg_loss == 0:
            return 100.0
        ratio = avg_gain / avg_loss
        if not math.isfinite(ratio):
            return None
        return _finite(100.0 - 100.0 / (1.0 + ratio))
    except OverflowError:
        return None


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Arithmetic mean true range; do not truncate misaligned or invalid OHLC."""
    h, lo, c = _series(highs), _series(lows), _series(closes)
    if (not _period(period) or h is None or lo is None or c is None
            or len(h) != len(lo) or len(h) != len(c) or len(h) < period + 1):
        return None
    if any(not low <= close <= high for high, low, close in zip(h, lo, c, strict=True)):
        return None
    try:
        ranges = [max(h[i] - lo[i], abs(h[i] - c[i-1]), abs(lo[i] - c[i-1])) for i in range(1, len(h))]
        return _finite(sum(ranges[-period:]) / period)
    except OverflowError:
        return None


def roc(values: list[float], period: int) -> float | None:
    """Percent change, not a fraction; requires a nonzero reference observation."""
    data = _series(values)
    if not _period(period) or data is None or len(data) <= period or data[-period-1] == 0:
        return None
    try:
        return _finite((data[-1] - data[-period-1]) / data[-period-1] * 100.0)
    except OverflowError:
        return None


def rolling_percentile(values: list[float], window: int, pct: float) -> float | None:
    """Legacy round-half-even nearest observation, using available short input."""
    data = _series(values)
    if not _period(window) or not valid_number(pct) or not 0 <= pct <= 100 or data is None or len(data) < 2:
        return None
    ordered = sorted(data[-window:])
    index = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _std(values: list[float], *, sample: bool) -> float | None:
    data = _series(values)
    if data is None or len(data) < (2 if sample else 1):
        return None
    try:
        mean = sum(data) / len(data)
        variance = sum((v - mean) ** 2 for v in data) / (len(data) - int(sample))
        return _finite(math.sqrt(variance))
    except OverflowError:
        return None


def stdev(values: list[float]) -> float | None:
    """Sample standard deviation, ddof=1."""
    return _std(values, sample=True)


def stdev_pop(values: list[float]) -> float | None:
    """Population standard deviation, ddof=0."""
    return _std(values, sample=False)
