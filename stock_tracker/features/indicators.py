"""技术指标纯函数（§7.1）。

仅作原始输入，不直接当“共振证据”。所有函数输入输出均为 ``list[float]`` 或标量，
对空/不足长度输入返回 ``None`` 或安全默认值，绝不抛异常。
"""

from __future__ import annotations

import math
from typing import Optional, Tuple


def sma(values: list[float], period: int) -> Optional[float]:
    """简单移动平均；不足长度返回 None。"""
    if not values or len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> Optional[float]:
    """指数移动平均（递推）；不足长度返回简单平均。"""
    if not values or period <= 0:
        return None
    if len(values) < period:
        return sum(values) / len(values)
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
    return prev


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """返回 (dif, dea, hist)。"""
    if not values or len(values) < slow:
        return None, None, None
    dif_series = []
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    # 逐点计算 dif 序列以得 dea
    fast_k = 2.0 / (fast + 1)
    slow_k = 2.0 / (slow + 1)
    pf = sum(values[:fast]) / fast
    ps = sum(values[:slow]) / slow
    difs: list[float] = []
    for i, v in enumerate(values):
        pf = v * fast_k + pf * (1 - fast_k) if i >= fast else pf
        ps = v * slow_k + ps * (1 - slow_k) if i >= slow else ps
        if i >= slow - 1:
            difs.append(pf - ps)
    if len(difs) < signal:
        return difs[-1] if difs else None, None, None
    dea = ema(difs, signal)
    dif = difs[-1]
    hist = dif - dea if dea is not None else None
    return dif, dea, hist


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    """相对强弱指标 0–100。"""
    if not values or len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        ch = values[i] - values[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    """平均真实波幅。"""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        h, l, c, pc = highs[i], lows[i], closes[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period


def roc(values: list[float], period: int) -> Optional[float]:
    """变化率（%）；（当前-period前）/period前*100。"""
    if not values or len(values) <= period:
        return None
    base = values[-period - 1]
    if base == 0:
        return None
    return (values[-1] - base) / base * 100.0


def rolling_percentile(values: list[float], window: int, pct: float) -> Optional[float]:
    """窗口内分位数。"""
    if not values or len(values) < 2:
        return None
    window_vals = values[-window:] if len(values) >= window else values
    s = sorted(window_vals)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def stdev(values: list[float]) -> Optional[float]:
    """样本标准差。"""
    if not values or len(values) < 2:
        return None
    m = sum(values) / len(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)


def stdev_pop(values: list[float]) -> Optional[float]:
    if not values:
        return None
    m = sum(values) / len(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))
