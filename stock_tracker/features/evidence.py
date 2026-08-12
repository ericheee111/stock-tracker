"""五大独立证据族聚合（§7.2 / PRD #8 去相关）。

关键约束：MA/MACD/RSI **不各自独立计分**，先归入 Trend/Momentum 等族，族内聚合成
单一 0–100 分后再进入四分数，避免“同一份价格趋势被算四次”。

每族输出单一分数 + 简短人话理由。当历史 Bars 不足时，退化使用 Quote 当日字段
（昨收/今开/最高/最低/换手/成交额）估算，保证 Phase1 首次运行即可产出证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core import types as T
from . import indicators as I


@dataclass(slots=True)
class EvidenceSet:
    """五族证据分数（0–100）。"""

    trend: int = 50
    momentum: int = 50
    relative_strength: int = 50
    volume_liquidity: int = 50
    price_structure: int = 50
    reasons: dict = field(default_factory=dict)  # {family: reason}

    def as_dict(self) -> dict:
        return {
            "trend": self.trend, "momentum": self.momentum,
            "relative_strength": self.relative_strength,
            "volume_liquidity": self.volume_liquidity,
            "price_structure": self.price_structure,
            "reasons": self.reasons,
        }


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> int:
    return int(max(lo, min(hi, v)))


def _series(bars: list[T.Bar]) -> tuple[list[float], list[float], list[float], list[float]]:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [float(b.volume) for b in bars]
    return closes, highs, lows, vols


def compute_evidence(quote: T.Quote, bars: list[T.Bar], regime: T.MarketRegime | None,
                     sector: T.SectorSnapshot | None) -> EvidenceSet:
    """计算五族证据。"""
    closes, highs, lows, vols = _series(bars)
    reasons: dict[str, str] = {}

    day_chg = (quote.last / quote.prev_close - 1.0) * 100.0 if quote.prev_close > 0 else 0.0
    intraday = (quote.last / quote.open - 1.0) * 100.0 if quote.open > 0 else 0.0

    # ---- Trend ----
    trend = 50.0
    if len(closes) >= 20:
        ma20 = I.sma(closes, 20) or closes[-1]
        ma60 = I.sma(closes, 60) or closes[-1]
        trend += _clamp((quote.last / ma20 - 1.0) * 300.0, -25, 25)
        trend += _clamp((quote.last / ma60 - 1.0) * 200.0, -15, 15)
        if ma20 > ma60:
            trend += 10
        else:
            trend -= 10
        adx = I.atr(highs, lows, closes)  # 占位：ATR 非 ADX，但用作波动参考
        if adx is not None and quote.last > 0:
            trend += _clamp((adx / quote.last) * 200.0 - 10, -10, 10)
        reasons["trend"] = f"MA20={'%.2f' % ma20} MA60={'%.2f' % ma60}，" \
                           f"{'站上' if quote.last > ma20 else '跌破'}短期均线"
    else:
        trend = 50.0 + _clamp(day_chg * 8.0, -25, 25)
        reasons["trend"] = f"无历史均线，按当日涨跌 {day_chg:+.2f}% 估算趋势"

    # ---- Momentum ----
    momentum = 50.0
    if len(closes) >= 14:
        r = I.rsi(closes, 14) or 50.0
        roc5 = I.roc(closes, 5) or 0.0
        roc10 = I.roc(closes, 10) or 0.0
        roc20 = I.roc(closes, 20) or 0.0
        _, _, hist = I.macd(closes)
        momentum += 0.5 * (r - 50.0)
        momentum += _clamp((roc5 + roc10 + roc20) / 3.0 * 3.0, -20, 20)
        if hist is not None:
            momentum += 10.0 if hist > 0 else -10.0
        reasons["momentum"] = f"RSI={'%.0f' % r}，" \
                              f"动能{'向上' if (hist or 0) > 0 else '向下'}"
    else:
        momentum = 50.0 + _clamp(day_chg * 8.0, -25, 25) + _clamp(intraday * 6.0, -15, 15)
        reasons["momentum"] = f"当日动量 {day_chg:+.2f}%（开 {intraday:+.2f}%）"

    # ---- Relative Strength ----
    rs_base = sector.relative_strength if sector is not None else 50.0
    rs_score = 0.6 * rs_base + 0.4 * (50.0 + _clamp(day_chg * 8.0, -25, 25))
    reasons["relative_strength"] = (
        f"板块相对强度 {'%.0f' % rs_base}" if sector else "无板块归属，按个股强弱估算"
    )

    # ---- Volume / Liquidity ----
    if quote.turnover > 0:
        liq = 50.0 + (quote.turnover - 1.5) * 15.0
    else:
        liq = 50.0 + (max(0.0, __import__("math").log10(quote.amount + 1) - 8.0)) * 10.0
    liq = _clamp(liq, 5, 100)
    reasons["volume_liquidity"] = (
        f"换手 {quote.turnover:.2f}%" if quote.turnover > 0
        else f"成交额 {quote.amount / 1e8:.2f}亿（换手缺失）"
    )

    # ---- Price Structure ----
    structure = 50.0
    if quote.high > quote.low:
        pos = (quote.last - quote.low) / (quote.high - quote.low)
        structure += (pos - 0.5) * 40.0
    if len(closes) >= 6:
        recent_high = max(closes[-5:-1]) if len(closes) >= 6 else closes[-1]
        if quote.last > recent_high:
            structure += 10.0
            reasons["price_structure"] = "价格突破近期高点（结构转强）"
        else:
            reasons["price_structure"] = "未突破近期高点"
    else:
        reasons["price_structure"] = f"日内位置 {('%.0f' % (structure))}（无历史结构）"

    return EvidenceSet(
        trend=_clamp(trend), momentum=_clamp(momentum),
        relative_strength=_clamp(rs_score), volume_liquidity=_clamp(liq),
        price_structure=_clamp(structure), reasons=reasons,
    )
