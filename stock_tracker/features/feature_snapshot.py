"""指标快照（§7）：基于历史 K 线计算展示用技术指标。

``build_indicators`` 仅计算指标「数值」用于前端展示；**不做评分、不做证据去相关、
不加权重**——这是与 scoring/evidence 解耦的硬约束（架构/PRD 边界）。

对空 / 不足长度输入返回对应 ``None``，整体**绝不抛异常**（极端脏数据下返回已计算的
或全 ``None`` 的空壳字典，交由调用方安全忽略）。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Optional

from ..core import types as T
from . import indicators as I

# 各市场年化交易日（用于年化波动率），统一口径避免港/美/ A 不一致。
_TRADING_DAYS = {T.Market.A: 242, T.Market.HK: 244, T.Market.US: 252}


def build_indicators(bars: list[T.Bar], market: T.Market = T.Market.A) -> dict:
    """由历史 K 线计算展示用指标快照（``dict[str, float|None]``）。

    计算项：
    - MA5/10/20/60、EMA12/26、MACD(12,26,9)、RSI14、ATR14、ROC20/60
    - ``pos52w``：52 周位置，**分位排名**（最高=1.0，最低=0.0）；重复价格取并列名次中点，完全平盘序列取 0.5。
    - ``ann_vol``：年化波动率 = stdev(日收益) * sqrt(市场交易日) （单位 %）。
    - ``vol_ratio``：量比 = 当日成交量 / 近 5 日均量（不含当日）。
    - ``amplitude``：当日振幅 = (high - low) / 昨收 * 100。

    任意序列不足长度 → 对应指标为 ``None``。``market`` 仅用于年化交易日口径，不影响其它计算。
    """
    result: dict[str, Optional[float]] = {
        "ma5": None, "ma10": None, "ma20": None, "ma60": None,
        "ema12": None, "ema26": None,
        "macd_dif": None, "macd_dea": None, "macd_hist": None,
        "rsi14": None, "atr14": None,
        "roc20": None, "roc60": None,
        "pos52w": None, "ann_vol": None, "vol_ratio": None, "amplitude": None,
        "last_close": None, "bar_count": 0,
    }
    try:
        if not bars:
            return result
        # 按整根 K 线过滤，避免 close/high/low/volume 分别过滤后跨日期错位。
        valid_bars: list[T.Bar] = []
        for bar in sorted(bars, key=lambda item: item.timestamp):
            prices = (bar.open, bar.high, bar.low, bar.close)
            if any(not math.isfinite(value) or value <= 0 for value in prices):
                continue
            if bar.low > min(bar.open, bar.close, bar.high):
                continue
            if bar.high < max(bar.open, bar.close, bar.low):
                continue
            if bar.volume < 0:
                continue
            valid_bars.append(bar)
        if not valid_bars:
            return result

        closes = [float(bar.close) for bar in valid_bars]
        highs = [float(bar.high) for bar in valid_bars]
        lows = [float(bar.low) for bar in valid_bars]
        vols = [int(bar.volume) for bar in valid_bars]
        n = len(valid_bars)
        result["bar_count"] = n
        last = valid_bars[-1]
        result["last_close"] = float(last.close) if last.close > 0 else None

        # MA / EMA
        result["ma5"] = I.sma(closes, 5)
        result["ma10"] = I.sma(closes, 10)
        result["ma20"] = I.sma(closes, 20)
        result["ma60"] = I.sma(closes, 60)
        result["ema12"] = I.ema(closes, 12)
        result["ema26"] = I.ema(closes, 26)

        # MACD(12,26,9)
        dif, dea, hist = I.macd(closes, 12, 26, 9)
        result["macd_dif"] = dif
        result["macd_dea"] = dea
        result["macd_hist"] = hist

        # RSI / ATR
        result["rsi14"] = I.rsi(closes, 14)
        if len(highs) >= 15 and len(lows) >= 15 and len(closes) >= 15:
            result["atr14"] = I.atr(highs, lows, closes, 14)

        # 变化率
        result["roc20"] = I.roc(closes, 20)
        result["roc60"] = I.roc(closes, 60)

        # pos52w：分位排名；重复价格取并列区间中点，平盘序列取 0.5。
        if closes:
            window = closes[-min(252, len(closes)):]
            cur = closes[-1]
            ranked = sorted(window)
            if len(ranked) == 1 or ranked[0] == ranked[-1]:
                result["pos52w"] = 0.5
            else:
                left = bisect_left(ranked, cur)
                right = bisect_right(ranked, cur) - 1
                result["pos52w"] = ((left + right) / 2) / (len(ranked) - 1)

        # ann_vol：日收益标准差 * sqrt(年化交易日)，单位 %
        if len(closes) >= 2:
            rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
                    for i in range(1, len(closes)) if closes[i - 1] != 0]
            sd = I.stdev(rets)
            if sd is not None:
                td = _TRADING_DAYS.get(market, 252)
                result["ann_vol"] = sd * math.sqrt(td) * 100.0

        # vol_ratio：当日量 / 近 5 日均量（不含当日）；零成交量保留为 0。
        if len(vols) >= 2:
            today_vol = vols[-1]
            prev = [volume for volume in vols[:-1][-5:] if volume > 0]
            if prev:
                avg_prev = sum(prev) / len(prev)
                if avg_prev > 0:
                    result["vol_ratio"] = today_vol / avg_prev

        # amplitude：当日 (high - low) / 昨收 * 100
        if last.high > 0 and last.low >= 0 and len(valid_bars) >= 2:
            prev_close = valid_bars[-2].close
            if prev_close and prev_close > 0 and last.high >= last.low:
                result["amplitude"] = (last.high - last.low) / prev_close * 100.0

        # 常规浮点四舍五入（除 bar_count 外的数值字段）
        for k in ("ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
                  "macd_dif", "macd_dea", "macd_hist", "rsi14", "atr14",
                  "roc20", "roc60", "pos52w", "ann_vol", "vol_ratio",
                  "amplitude", "last_close"):
            if result[k] is not None:
                result[k] = round(float(result[k]), 4)
    except Exception:
        # 极端数据异常：返回已计算的（或全 None）结果，绝不向外抛
        pass
    return result
