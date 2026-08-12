"""四分数聚合（§7.3 / PRD #11）。

ScoreSet = aggregate(证据族, regime, sector, dq)。证据族已去相关（§7.2），这里仅聚合：
- Opportunity：RS + TrendMomentum + SectorContext + Catalyst + Vol + Structure + RegimeFit + Persistence - RiskPenalty
- Timing：Trend + Momentum + PriceStructure 进入区（受 regime 调节）
- Risk：波动率(ATR%/当日振幅) + Regime 风险 + Overextension + Crowding（越高越危险）
- Confidence：DQ.score + 证据族一致性 + Regime 覆盖（Phase1 用规则近似，无校准模型）
- success_probability：Phase1 = None（PRD #11.5 / #13）
"""

from __future__ import annotations

import statistics

from ..core import types as T
from ..features import evidence as E


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _compute_risk(ev: E.EvidenceSet, q: T.Quote, regime: T.MarketRegime | None,
                 sector: T.SectorSnapshot | None) -> float:
    risk = 50.0
    # 追高/超买：距日内低点的涨幅
    if q.high > q.low and q.last > 0:
        gain_low = (q.last - q.low) / q.last
        risk += _clamp(gain_low * 200.0 - 20.0, -10.0, 30.0)
    # 拥挤度
    if sector is not None:
        risk += _clamp(sector.crowding * 0.3, 0.0, 20.0)
    # 市场环境风险
    if regime is not None:
        risk += {"RISK_OFF": 15.0, "OVERHEATED": 10.0, "PANIC_REBOUND": 8.0,
                 "ROTATION": 0.0, "RISK_ON_TREND": -5.0}.get(regime.regime.value, 0.0)
    # 当日振幅（波动代理）
    if q.prev_close > 0 and q.high > q.low:
        rng = (q.high - q.low) / q.prev_close * 100.0
        risk += _clamp(rng - 2.0, 0.0, 20.0)
    return _clamp(risk)


def score(ctx: T.ScanContext) -> T.ScoreSet:
    """由上下文计算四分数。"""
    ev = E.compute_evidence(ctx.quote, ctx.recent_bars, ctx.regime, ctx.sector)
    q = ctx.quote
    dq = ctx.dq or T.DataQuality(T.QualityStatus.VALID, 100, [])
    regime = ctx.regime
    sector = ctx.sector

    rs = ev.relative_strength
    trend_mom = (ev.trend + ev.momentum) / 2.0
    sector_ctx = sector.score if sector is not None else 50.0
    catalyst = 70.0 if (sector is not None and sector.catalyst) else 50.0
    vol = ev.volume_liquidity
    structure = ev.price_structure
    regime_fit = regime.market_score if regime is not None else 50.0
    persistence = sector.persistence if sector is not None else 50.0

    risk = _compute_risk(ev, q, regime, sector)

    opportunity = (
        0.20 * rs + 0.15 * trend_mom + 0.15 * sector_ctx + 0.15 * catalyst
        + 0.10 * vol + 0.10 * structure + 0.10 * regime_fit + 0.05 * persistence
    )
    opportunity -= max(0.0, (risk - 60.0)) * 0.3  # 风险惩罚
    opportunity = _clamp(opportunity)

    timing = 0.40 * ev.trend + 0.30 * ev.momentum + 0.30 * ev.price_structure
    if regime is not None:
        if regime.regime == T.RegimeState.RISK_OFF:
            timing *= 0.8
        elif regime.regime == T.RegimeState.OVERHEATED:
            timing *= 0.9

    # 置信度：DQ + 证据族一致性 + regime 覆盖
    fams = [ev.trend, ev.momentum, ev.relative_strength, ev.volume_liquidity, ev.price_structure]
    sd = statistics.pstdev(fams) if len(fams) > 1 else 0.0
    agreement = _clamp(100.0 - sd * 1.5, 0.0, 100.0)
    confidence = 0.35 * dq.score + 0.35 * agreement + 0.30 * regime_fit
    confidence = _clamp(confidence)

    # 理由
    positive: list[str] = []
    negative: list[str] = []
    for fam, r in ev.reasons.items():
        (positive if getattr(ev, fam, 50) >= 55 else negative).append(f"[{fam}] {r}")
    if dq.status != T.QualityStatus.VALID:
        negative.extend(dq.reasons)
    if regime is not None:
        positive.append(f"市场环境：{regime.regime.value}（分 {regime.market_score}）")
    if risk >= 70:
        negative.append(f"风险偏高（{risk:.0f}）：注意追高/拥挤/波动")
    if sector is not None:
        positive.append(f"板块「{sector.sector}」阶段 {sector.stage.value}（分 {sector.score}）")

    return T.ScoreSet(
        opportunity=int(round(opportunity)),
        timing=int(round(_clamp(timing))),
        risk=int(round(risk)),
        confidence=int(round(confidence)),
        success_probability=None,  # Phase1 不伪装概率
        positive_reasons=positive,
        negative_reasons=negative,
    )
