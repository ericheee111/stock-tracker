"""N4 missingness candidate: transparent same-input comparison, never live scoring.

The existing Evidence/Score functions are called for the legacy side; they are
not patched. The candidate keeps coefficients and ordinary formula conventions
but withholds a total if any required contribution is unavailable. Context here
is a bounded runtime copy, NOT proof of market identity, PIT or data quality.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from ..core import types as T
from ..signals import scoring
from . import evidence as E
from . import indicators as I
from .evidence_snapshot import EvidenceInputError, EvidenceSnapshot, canonical

COMPARISON_SCHEMA = "evidence-comparison-v1"
CANDIDATE_POLICY = "evidence-explicit-missingness-candidate-v1"
LEGACY_RECIPE = "legacy-five-family-four-score-ab0fc66"
FAMILY_KEYS = ("trend", "momentum", "relative_strength", "volume_liquidity", "price_structure")
SCORE_KEYS = ("opportunity", "timing", "risk", "confidence")
FAMILY_LABELS = ("趋势", "动量", "相对强弱", "量能与流动性", "价格结构")
SCORE_LABELS = ("机会规则分", "时机规则分", "风险规则分", "规则置信分（非概率）")
FIXED_WARNINGS = (
    "RUNTIME_COPY_NOT_PIT", "NO_CALENDAR_OR_ADJUSTMENT_AUTHORITY", "NOT_A_T_SIGNAL",
    "NOT_CURRENT_SIGNAL_SCORE", "NO_CALIBRATED_PROBABILITY", "RULE_AGREEMENT_NOT_INDEPENDENT_EVIDENCE",
    "CANDIDATE_NOT_ENABLED", "CONTEXT_MEMBERSHIP_AND_KNOWN_AT_UNVERIFIED",
)


class _NumericUnavailable(ArithmeticError):
    pass


def _finite(value: Any) -> float:
    if not I.valid_number(value):
        raise _NumericUnavailable("nonfinite arithmetic")
    return value


def _bound(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, _finite(value)))


def _int_bound(value: float, lo: float = 0, hi: float = 100) -> int:
    return int(_bound(value, lo, hi))


def _term(key: str, label: str, dependencies: dict[str, Any], calculation: Callable[..., float]) -> dict[str, Any]:
    missing = [name for name, value in dependencies.items() if value is None]
    value = None
    status = "MISSING_INPUT" if missing else "NUMERIC_ONLY"
    if not missing:
        try:
            value = _finite(calculation(*dependencies.values()))
        except ArithmeticError:
            missing = ["NUMERIC_UNAVAILABLE"]
            status = "NUMERIC_UNAVAILABLE"
    return {"key": key, "label": label, "value": value, "status": status,
            "inputs": dict(dependencies), "missing": missing}


def _left_sum(terms: list[dict[str, Any]]) -> float:
    """Match legacy left-to-right additions, not Python's compensated sum."""
    total = terms[0]["value"]
    for term in terms[1:]:
        total += term["value"]
    return _finite(total)


def _group(key: str, label: str, terms: list[dict[str, Any]], *, rounded: bool = False,
           multiplier: float | None = 1.0, extra_missing: tuple[str, ...] = ()) -> dict[str, Any]:
    missing = sorted(set(extra_missing).union(*(t["missing"] for t in terms)))
    value = None
    if multiplier is None:
        missing = sorted(set(missing) | {"REGIME_REQUIRED"})
    if not missing and multiplier is not None:
        try:
            total = _bound(_left_sum(terms) * multiplier)
            value = round(total) if rounded else int(total)
        except ArithmeticError:
            missing = ["NUMERIC_UNAVAILABLE"]
    return {"key": key, "label": label, "value": value,
            "status": "MISSING_INPUT" if missing else "NUMERIC_ONLY",
            "missing": missing, "terms": terms, "multiplier": multiplier}


def _base() -> dict[str, Any]:
    return _term("base", "旧规则基准项（不是数据证据）", {}, lambda: 50.0)


def _candidate(ctx: T.ScanContext) -> tuple[dict[str, Any], dict[str, Any]]:
    q = ctx.quote
    assert q is not None  # Exact validated input, not an external trust decision.
    bars = ctx.recent_bars
    c = [b.close for b in bars]
    h = [b.high for b in bars]
    lo = [b.low for b in bars]
    ma20, ma60 = I.sma(c, 20), I.sma(c, 60)
    rsi = I.rsi(c, 14)
    roc5, roc10, roc20 = I.roc(c, 5), I.roc(c, 10), I.roc(c, 20)
    atr = I.atr(h, lo, c, 14)
    _, _, hist = I.macd(c)
    numbers = {"ma20": ma20, "ma60": ma60, "rsi14": rsi, "roc5": roc5, "roc10": roc10,
               "roc20": roc20, "atr14": atr, "macd_hist": hist}
    change = _term("day_change", "当日变化（%）", {"LAST_REQUIRED": q.last, "PREV_CLOSE_REQUIRED": q.prev_close},
                   lambda last, previous: (last / previous - 1.0) * 100.0)["value"]
    sector, regime = ctx.sector, ctx.regime
    families = []
    families.append(_group("trend", FAMILY_LABELS[0], [
        _base(),
        _term("ma20_distance", "MA20距离项；截断±25", {"LAST_REQUIRED": q.last, "MA20_REQUIRED": ma20},
              lambda last, mean: _int_bound((last / mean - 1.0) * 300, -25, 25)),
        _term("ma60_distance", "MA60距离项；截断±15", {"LAST_REQUIRED": q.last, "MA60_REQUIRED": ma60},
              lambda last, mean: _int_bound((last / mean - 1.0) * 200, -15, 15)),
        _term("ma_order", "MA20>MA60 +10，否则-10（旧规则）", {"MA20_REQUIRED": ma20, "MA60_REQUIRED": ma60},
              lambda short, long: 10 if short > long else -10),
        _term("atr_proxy", "ATR波动代理；不是ADX", {"ATR14_REQUIRED": atr, "LAST_REQUIRED": q.last},
              lambda average_range, last: _int_bound(average_range / last * 200 - 10, -10, 10)),
    ]))
    families.append(_group("momentum", FAMILY_LABELS[1], [
        _base(),
        _term("rsi", "0.5×(RSI14−50)，真实0保留", {"RSI14_REQUIRED": rsi}, lambda value: .5 * (value - 50)),
        _term("roc_mean", "ROC5/10/20完整平均；不重归一化", {"ROC5_REQUIRED": roc5, "ROC10_REQUIRED": roc10, "ROC20_REQUIRED": roc20},
              lambda r5, r10, r20: _int_bound((r5 + r10 + r20) / 3.0 * 3.0, -20, 20)),
        _term("macd", "HIST>0 +10，否则-10（真零保留旧规则）", {"MACD_HIST_REQUIRED": hist}, lambda value: 10 if value > 0 else -10),
    ]))
    rs = sector.relative_strength if sector is not None else None
    families.append(_group("relative_strength", FAMILY_LABELS[2], [
        _term("sector_rs", "0.6×板块相对强弱", {"SECTOR_RS_REQUIRED": rs}, lambda value: .6 * value),
        _term("day_proxy", "0.4×日变化代理（非独立因子）", {"DAY_CHANGE_REQUIRED": change},
              lambda value: .4 * (50 + _int_bound(value * 8, -25, 25))),
    ]))
    # Zero turnover retains the legacy amount proxy, explicitly distinct from missing turnover.
    liquidity = (
        _term("liquidity_proxy", "旧换手代理；不表示可成交容量", {"TURNOVER_REQUIRED": q.turnover},
              lambda turnover: _int_bound(50 + (turnover - 1.5) * 15, 5, 100))
        if q.turnover is not None and q.turnover > 0 else
        _term("liquidity_proxy", "旧成交额代理；真实0换手走此分支", {"TURNOVER_REQUIRED": q.turnover, "AMOUNT_REQUIRED": q.amount},
              lambda _turnover, amount: _int_bound(50 + max(0.0, math.log10(amount + 1) - 8) * 10, 5, 100))
    )
    families.append(_group("volume_liquidity", FAMILY_LABELS[3], [liquidity]))
    recent_high = max(c[-5:-1]) if len(c) >= 6 else None
    families.append(_group("price_structure", FAMILY_LABELS[4], [
        _base(),
        _term("intraday_position", "日内区间项（平价区间为真实0）",
              {"LAST_REQUIRED": q.last, "HIGH_REQUIRED": q.high, "LOW_REQUIRED": q.low},
              lambda last, high, low: ((last - low) / (high - low) - .5) * 40 if high > low else 0),
        _term("breakout", "前第5至第2条收盘上沿（沿用旧切片）", {"LAST_REQUIRED": q.last, "SIX_BARS_REQUIRED": recent_high},
              lambda last, previous_high: 10 if last > previous_high else 0),
    ]))
    f = {item["key"]: item["value"] for item in families}
    crowding = sector.crowding if sector is not None else None
    regime_value = regime.regime.value if regime is not None else None
    risk = _group("risk", SCORE_LABELS[2], [
        _base(),
        _term("gain_from_low", "日内低点距离风险项", {"LAST_REQUIRED": q.last, "HIGH_REQUIRED": q.high, "LOW_REQUIRED": q.low},
              lambda last, high, low: _bound((last-low)/last*200-20, -10, 30) if high > low else 0),
        _term("crowding", "板块拥挤度×0.3，上限20", {"SECTOR_CROWDING_REQUIRED": crowding}, lambda value: _bound(value*.3, 0, 20)),
        _term("regime_risk", "旧市场状态风险项", {"REGIME_REQUIRED": regime_value},
              lambda state: {"RISK_OFF": 15, "OVERHEATED": 10, "PANIC_REBOUND": 8, "ROTATION": 0, "RISK_ON_TREND": -5}[state]),
        _term("range", "日振幅风险项，上限20", {"HIGH_REQUIRED": q.high, "LOW_REQUIRED": q.low, "PREV_CLOSE_REQUIRED": q.prev_close},
              lambda high, low, previous: _bound((high-low)/previous*100-2, 0, 20) if high > low else 0),
    ], rounded=True)
    # Legacy applies risk penalty BEFORE risk rounding, so retain the raw bounded sum.
    risk_raw = _bound(_left_sum(risk["terms"])) if not risk["missing"] else None
    sc = sector.score if sector else None
    catalyst = (70 if sector.catalyst else 50) if sector else None
    persistence = sector.persistence if sector else None
    regime_fit = regime.market_score if regime else None
    opportunity = _group("opportunity", SCORE_LABELS[0], [
        _term("relative_strength", "0.20×相对强弱", {"RELATIVE_STRENGTH_REQUIRED": f["relative_strength"]}, lambda value: .20*value),
        _term("trend_momentum", "0.15×趋势/动量均值", {"TREND_REQUIRED": f["trend"], "MOMENTUM_REQUIRED": f["momentum"]}, lambda trend, momentum: .15*((trend+momentum)/2.0)),
        _term("sector", "0.15×板块规则分", {"SECTOR_SCORE_REQUIRED": sc}, lambda value: .15*value),
        _term("catalyst", "0.15×旧催化标记代理（未核实事件）", {"SECTOR_CATALYST_CONTEXT_REQUIRED": catalyst}, lambda value: .15*value),
        _term("liquidity", "0.10×量能流动性", {"LIQUIDITY_REQUIRED": f["volume_liquidity"]}, lambda value: .10*value),
        _term("structure", "0.10×价格结构", {"STRUCTURE_REQUIRED": f["price_structure"]}, lambda value: .10*value),
        _term("regime", "0.10×市场规则分", {"REGIME_REQUIRED": regime_fit}, lambda value: .10*value),
        _term("persistence", "0.05×持续性", {"SECTOR_PERSISTENCE_REQUIRED": persistence}, lambda value: .05*value),
        _term("risk_penalty", "−0.3×max(未舍入风险−60,0)", {"RISK_REQUIRED": risk_raw}, lambda value: -max(0.0, value-60)*.3),
    ], rounded=True)
    multiplier = None if regime is None else .8 if regime.regime is T.RegimeState.RISK_OFF else .9 if regime.regime is T.RegimeState.OVERHEATED else 1.0
    timing = _group("timing", SCORE_LABELS[1], [
        _term("trend", "0.40×趋势", {"TREND_REQUIRED": f["trend"]}, lambda value: .40*value),
        _term("momentum", "0.30×动量", {"MOMENTUM_REQUIRED": f["momentum"]}, lambda value: .30*value),
        _term("structure", "0.30×结构", {"STRUCTURE_REQUIRED": f["price_structure"]}, lambda value: .30*value),
    ], rounded=True, multiplier=multiplier)
    agreement = _term("agreement", "0.35×族内规则一致性（非独立证据）", {key.upper()+"_REQUIRED": value for key, value in f.items()},
                      lambda *values: .35*_bound(100.0-statistics.pstdev(values)*1.5))
    dq_score = ctx.dq.score if ctx.dq is not None and ctx.dq.status is T.QualityStatus.VALID else None
    confidence = _group("confidence", SCORE_LABELS[3], [
        _term("dq", "0.35×显式DQ规则分", {"DQ_REQUIRED": dq_score}, lambda value: .35*value), agreement,
        _term("regime", "0.30×市场规则分", {"REGIME_REQUIRED": regime_fit}, lambda value: .30*value),
    ], rounded=True, extra_missing=("DQ_NOT_VALID",) if ctx.dq is not None and ctx.dq.status is not T.QualityStatus.VALID else ())
    return {"families": families, "scores": [opportunity, timing, risk, confidence],
            "macd_direction": "UNKNOWN" if hist is None else "UP" if hist > 0 else "DOWN" if hist < 0 else "FLAT"}, numbers


def unavailable(symbol: str, market: str, computed_at: str | None, code: str, *, no_quote: bool = False) -> dict[str, Any]:
    return {"schema": COMPARISON_SCHEMA, "policy_id": CANDIDATE_POLICY, "legacy_recipe_id": LEGACY_RECIPE,
            "symbol": symbol, "market": market, "interval": "1d", "computed_at": computed_at,
            "status": "NO_QUOTE" if no_quote else "INVALID_INPUT", "input_id": None,
            "sample_info": None, "indicators": {}, "findings": [], "issues": [code],
            "warnings": list(FIXED_WARNINGS), "legacy": {"status": "UNAVAILABLE", "families": {}, "scores": {}, "reasons": {}},
            "candidate": {"families": [], "scores": [], "macd_direction": "UNKNOWN"}, "differences": [],
            "assurance": "RUNTIME_DIAGNOSTIC_ONLY", "success_probability": None,
            "auto_trade": False, "execution_authorized": False, "affects_live_scores": False,
            "investment_performance_claim": False}


def compare_snapshot(snapshot: EvidenceSnapshot) -> dict[str, Any]:
    if type(snapshot) is not EvidenceSnapshot:
        raise EvidenceInputError("EXACT_SNAPSHOT_REQUIRED")
    ctx, info = snapshot.select()
    q = ctx.quote
    assert q is not None
    report = unavailable(ctx.symbol, ctx.market.value, info["computed_at"], "")
    report.update(input_id=snapshot.input_id, sample_info=info, issues=[])
    candidate, numbers = _candidate(ctx)
    report.update(candidate=candidate, indicators=numbers)
    legacy_ctx, _ = snapshot.select()
    legacy_missing = any(getattr(q, key) is None for key in ("last", "prev_close", "open", "high", "low", "amount", "turnover"))
    if not legacy_missing:
        try:
            assert legacy_ctx.quote is not None
            legacy_ev = E.compute_evidence(legacy_ctx.quote, legacy_ctx.recent_bars, legacy_ctx.regime, legacy_ctx.sector)
            legacy_scores = scoring.score(legacy_ctx)
            report["legacy"] = {"status": "NUMERIC_ONLY", "families": {k: getattr(legacy_ev, k) for k in FAMILY_KEYS},
                                "scores": {k: getattr(legacy_scores, k) for k in SCORE_KEYS}, "reasons": legacy_ev.reasons}
        except ArithmeticError:
            report["legacy"]["error"] = "LEGACY_ARITHMETIC_UNAVAILABLE"
    else:
        report["legacy"]["error"] = "LEGACY_REQUIRES_NONMISSING_QUOTE"
    n = info["selected_count"]
    findings = []
    if n >= 14 and numbers["rsi14"] == 0:
        findings.append("RSI_ZERO_WAS_NEUTRALIZED")
    if 20 <= n < 60:
        findings.append("MA60_WAS_LAST_PRICE_FALLBACK")
    if n >= 14 and numbers["rsi14"] is None:
        findings.append("RSI_MISSING_WAS_50")
    if 14 <= n and numbers["macd_hist"] is None:
        findings.append("MACD_MISSING_WAS_DESCRIBED_DOWN")
    if 14 <= n < 21:
        findings.append("ROC_MISSING_WAS_ZERO")
    if n < 20:
        findings.append("DAY_PROXY_WAS_TREND_FALLBACK")
    if ctx.dq is None:
        findings.append("DQ_MISSING_WAS_100")
    if ctx.regime is None:
        findings.append("REGIME_MISSING_WAS_NEUTRAL")
    if ctx.sector is None:
        findings.append("SECTOR_MISSING_WAS_NEUTRAL")
    report["findings"] = findings
    warnings = report["warnings"]
    if numbers["macd_hist"] == 0:
        warnings.append("FLAT_MACD_NEGATIVE_TERM_RETAINED")
    if numbers["rsi14"] == 100 and c_is_flat(ctx.recent_bars):
        warnings.append("FLAT_RSI_100_CONVENTION_RETAINED")
    if any(getattr(q, key).tzinfo is None for key in ("timestamp", "received_at", "computed_at")) or info["time_basis"] == "LEGACY_NAIVE_DATE":
        warnings.append("NAIVE_TIME_NOT_PIT")
    if q.data_status is not T.DataStatus.LIVE:
        warnings.append("QUOTE_NOT_DECLARED_LIVE")
    for section in ("families", "scores"):
        for item in candidate[section]:
            legacy_values = cast(dict[str, Any], report["legacy"][section])
            old = legacy_values.get(item["key"])
            report["differences"].append({"section": section, "key": item["key"], "legacy_value": old,
                                          "candidate_value": item["value"],
                                          "delta": None if old is None or item["value"] is None else item["value"]-old})
    report["status"] = "COMPARABLE_NUMERIC" if report["legacy"]["status"] == "NUMERIC_ONLY" and all(x["value"] is not None for x in candidate["scores"]) else "PARTIAL"
    report["report_id"] = hashlib.sha256(canonical(report).encode("utf-8")).hexdigest()
    return report


def c_is_flat(bars: list[T.Bar]) -> bool:
    return len(bars) >= 15 and len({b.close for b in bars[-15:]}) == 1


def compare_evidence(ctx: T.ScanContext, computed_at: datetime) -> dict[str, Any]:
    """Safe product boundary for invalid input; programming exceptions stay visible."""
    try:
        return compare_snapshot(EvidenceSnapshot.capture(ctx, computed_at))
    except EvidenceInputError as exc:
        symbol = ctx.symbol if type(ctx) is T.ScanContext and type(ctx.symbol) is str and len(ctx.symbol) <= 40 else ""
        market = ctx.market.value if type(ctx) is T.ScanContext and type(ctx.market) is T.Market else "UNKNOWN"
        at = computed_at.isoformat() if type(computed_at) is datetime else None
        return unavailable(symbol, market, at, exc.code)
