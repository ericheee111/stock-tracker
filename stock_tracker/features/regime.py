"""市场环境分类器（§8 / PRD #6）。

五态：RISK_ON_TREND / ROTATION / RISK_OFF / PANIC_REBOUND / OVERHEATED。
由市场级特征（宽度/趋势/波动/流动性/情绪）聚合，market_score 0–100，sub_factors 字典。
Phase1 用规则分类（无历史 K 线时退化使用当日涨跌广度），预留模型接口。
"""

from __future__ import annotations

from ..core import types as T


# 已知指数标的（用于提取“市场”趋势代理）
_INDEX_SYMBOLS = {"000001.SH", "399001.SZ", "399006.SZ"}


def _day_change(q: T.Quote) -> float:
    return (q.last / q.prev_close - 1.0) * 100.0 if q.prev_close > 0 else 0.0


def _intraday_recovery(q: T.Quote) -> float:
    """从最低价的日内反弹幅度（%），正为收复。"""
    if q.high <= q.low:
        return 0.0
    return (q.last - q.low) / (q.high - q.low) * 100.0


def build_regime(quotes: list[T.Quote]) -> T.MarketRegime:
    """由当前行情集合推断市场环境。"""
    if not quotes:
        return T.MarketRegime(regime=T.RegimeState.ROTATION, market_score=50.0,
                              sub_factors={"note": "无行情数据"})

    ups = sum(1 for q in quotes if _day_change(q) > 0)
    breadth = ups / len(quotes)

    # 市场趋势代理：优先用指数，否则用平均涨跌
    idx_chg = None
    for q in quotes:
        if q.symbol in _INDEX_SYMBOLS:
            idx_chg = _day_change(q)
            break
    market_chg = idx_chg if idx_chg is not None else sum(_day_change(q) for q in quotes) / len(quotes)

    # 波动质量：平均振幅（当日）
    avg_range = 0.0
    for q in quotes:
        if q.high > q.low and q.prev_close > 0:
            avg_range += (q.high - q.low) / q.prev_close * 100.0
    avg_range /= len(quotes)

    # 流动性：成交额分布（对数）
    import math
    amps = [math.log10(q.amount + 1) for q in quotes if q.amount > 0]
    liq = (sum(amps) / len(amps) - 7.0) / 3.0 if amps else 0.5  # 约 1000万→0
    liq = max(0.0, min(1.0, liq))

    # 情绪/拥挤：涨幅极端股比例
    extreme_up = sum(1 for q in quotes if _day_change(q) > 5.0) / len(quotes)
    sentiment = breadth * 0.7 + (1.0 - min(1.0, extreme_up / 0.15)) * 0.3

    # 因子打分（0–100）
    trend_score = max(0.0, min(100.0, 50.0 + market_chg * 12.0))
    breadth_score = breadth * 100.0
    vol_quality = max(0.0, min(100.0, 100.0 - abs(avg_range - 2.0) * 15.0))
    liq_score = liq * 100.0
    sent_score = sentiment * 100.0

    market_score = (
        trend_score * 0.25 + breadth_score * 0.25 + vol_quality * 0.20
        + liq_score * 0.15 + sent_score * 0.15
    )

    # 五态分类
    regime = _classify(breadth, market_chg, avg_range, extreme_up)

    sub_factors = {
        "trend": round(trend_score, 1),
        "breadth": round(breadth_score, 1),
        "volatility_quality": round(vol_quality, 1),
        "liquidity": round(liq_score, 1),
        "sentiment": round(sent_score, 1),
        "market_chg_pct": round(market_chg, 2),
        "avg_range_pct": round(avg_range, 2),
    }
    return T.MarketRegime(regime=regime, market_score=round(market_score, 1), sub_factors=sub_factors)


def _classify(breadth: float, market_chg: float, avg_range: float, extreme_up: float) -> T.RegimeState:
    if market_chg < -3.0 and avg_range > 3.0:
        return T.RegimeState.PANIC_REBOUND if breadth > 0.3 else T.RegimeState.RISK_OFF
    if breadth >= 0.7 and market_chg > 1.5 and extreme_up > 0.1:
        return T.RegimeState.OVERHEATED
    if breadth >= 0.58 and market_chg > 0.2:
        return T.RegimeState.RISK_ON_TREND
    if breadth < 0.42 or market_chg < -0.8:
        return T.RegimeState.RISK_OFF
    return T.RegimeState.ROTATION
