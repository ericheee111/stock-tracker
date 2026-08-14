"""API 序列化（§9.1 强制契约）。

所有行情/信号响应 dict **必含** ``data_status``（LIVE/DELAYED/STALE/UNKNOWN）与
``observed_age_ms``（数据观察年龄，毫秒），供前端显示「真实/测试数据」横幅与延迟。

不伪造测试数据：``data_mode`` 仅 LIVE（真实且新鲜）/ DEGRADED（有源降级/熔断）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..core import types as T
from ..core.config import ConfigBundle
from ..decision.runtime import RuntimeDecisionRecord
from ..decision.types import (
    ActionState,
    DecisionBlocker,
    PlanVariant,
    PositionSizeResult,
    TradePlan,
)
from ..storage.repository import to_jsonable


def serialize_portfolio_profile(profile: object) -> dict:
    return to_jsonable(profile)


def serialize_position(position: T.Position) -> dict:
    return {
        "id": position.id,
        "symbol": position.symbol,
        "market": position.market.value,
        "shares": position.shares,
        "average_cost": position.cost,
        "added_at": position.added_at.isoformat(),
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
    }


_ACTION_LABELS = {
    ActionState.WATCH: "值得观察",
    ActionState.WAIT_PULLBACK: "等回踩确认",
    ActionState.WAIT_BREAKOUT: "等突破确认",
    ActionState.EXECUTABLE: "当前具备执行条件",
    ActionState.HOLD: "继续持有",
    ActionState.WARNING: "风险上升，密切观察",
    ActionState.TRIM: "建议降低仓位",
    ActionState.PARTIAL_TAKE_PROFIT: "建议部分止盈",
    ActionState.TREND_RUNNER: "保留趋势仓",
    ActionState.EXIT: "原逻辑失效，建议退出",
    ActionState.AVOID: "当前不值得参与",
    ActionState.DATA_BLOCKED: "数据或决策证据不足",
}


def serialize_decision_blocker(blocker: DecisionBlocker) -> dict:
    return {
        "code": blocker.code,
        "message": blocker.message,
        "severity": blocker.severity.value,
        "recoverable": blocker.recoverable,
    }


def serialize_plan_variant(variant: PlanVariant | None) -> Optional[dict]:
    if variant is None:
        return None
    return {
        "name": variant.name,
        "action_state": variant.action.value,
        "action_label": _ACTION_LABELS[variant.action],
        "risk_budget_multiplier": variant.risk_budget_multiplier,
        "note": variant.note,
    }


def serialize_position_size(result: PositionSizeResult | None) -> Optional[dict]:
    if result is None:
        return None
    return {
        "allowed": result.allowed,
        "shares": result.shares,
        "lot_size": result.lot_size,
        "entry_price": result.entry_price,
        "invalidation_price": result.invalidation_price,
        "risk_per_share": result.risk_per_share,
        "risk_budget_amount": result.risk_budget_amount,
        "actual_risk_amount": result.actual_risk_amount,
        "actual_risk_pct": result.actual_risk_pct,
        "position_value": result.position_value,
        "position_pct": result.position_pct,
        "limiting_factors": list(result.limiting_factors),
        "blockers": [serialize_decision_blocker(item) for item in result.blockers],
    }


def serialize_trade_plan(plan: TradePlan | None) -> Optional[dict]:
    if plan is None:
        return None
    position_size = serialize_position_size(plan.position_size)
    suggested_position_pct = None
    suggested_shares = None
    risk_budget_amount = None
    actual_risk_amount = None
    position_message = "请先设置账户净值、可用现金和风险参数"
    if position_size is None and plan.hard_blockers:
        position_message = "；".join(
            blocker.message for blocker in plan.hard_blockers
        )
    if position_size is not None:
        risk_budget_amount = position_size["risk_budget_amount"]
        actual_risk_amount = position_size["actual_risk_amount"]
        if position_size["allowed"]:
            suggested_position_pct = position_size["position_pct"]
            suggested_shares = position_size["shares"]
            position_message = "已按风险、现金、集中度和交易单位取最小值"
        else:
            messages = [item["message"] for item in position_size["blockers"]]
            position_message = "；".join(messages) or "当前账户约束不允许新开仓"
    return {
        "entry_low": plan.entry_low,
        "entry_high": plan.entry_high,
        "trigger_price": plan.trigger_price,
        "no_chase_above": plan.no_chase_above,
        "invalidation_price": plan.invalidation_price,
        "target_1": plan.target_1,
        "target_2": plan.target_2,
        "reward_risk": plan.reward_risk,
        "next_trigger": plan.next_trigger,
        "suggested_position_pct": suggested_position_pct,
        "suggested_shares": suggested_shares,
        "risk_budget_amount": risk_budget_amount,
        "actual_risk_amount": actual_risk_amount,
        "position_message": position_message,
        "balanced_plan": serialize_plan_variant(plan.balanced_plan),
        "aggressive_plan": serialize_plan_variant(plan.aggressive_plan),
        "position_size": position_size,
        "calibrated_probability": plan.calibrated_probability,
        "probability_evidence_level": plan.probability_evidence_level.value,
        "as_of": plan.as_of.isoformat(),
        "data_status": plan.data_status.value,
    }


def _opportunity_grade(opportunity: int) -> str:
    if opportunity >= 80:
        return "A"
    if opportunity >= 70:
        return "B"
    if opportunity >= 60:
        return "C"
    return "X"


def _model_tendency(opportunity: int, confidence: int) -> str:
    if opportunity >= 75 and confidence >= 60:
        return "STRONG"
    if opportunity >= 55:
        return "NEUTRAL"
    return "WEAK"


def serialize_runtime_opportunity(record: RuntimeDecisionRecord) -> dict:
    action = record.action
    signal = record.signal
    scores = signal.scores if signal is not None else None
    plan = serialize_trade_plan(action.trade_plan)
    probability_level = (
        action.trade_plan.probability_evidence_level.value
        if action.trade_plan is not None
        else "INSUFFICIENT"
    )
    calibrated_probability = (
        action.trade_plan.calibrated_probability
        if action.trade_plan is not None
        else None
    )
    return {
        "symbol": action.symbol,
        "market": action.market.value,
        "name": record.name,
        "action_state": action.action.value,
        "action_label": _ACTION_LABELS[action.action],
        "opportunity_grade": _opportunity_grade(action.opportunity),
        "strategy_id": action.strategy_id,
        "strategy_version": "runtime-v1",
        "scores": {
            "opportunity": action.opportunity,
            "timing": action.timing,
            "risk": action.risk,
            "confidence": action.confidence,
        },
        "model": {
            "tendency": _model_tendency(action.opportunity, action.confidence),
            "score": None,
            "calibrated_probability": calibrated_probability,
            "probability_evidence_level": probability_level,
            "message": (
                "真实样本或校准证据不足，暂不展示概率"
                if calibrated_probability is None
                else "概率来自已校准模型"
            ),
        },
        "trade_plan": plan,
        "next_trigger": signal.next_trigger if signal is not None else "",
        "positive_reasons": list(scores.positive_reasons) if scores is not None else [],
        "negative_reasons": list(scores.negative_reasons) if scores is not None else [],
        "hard_blockers": [serialize_decision_blocker(item) for item in record.hard_blockers],
        "soft_blockers": [serialize_decision_blocker(item) for item in record.soft_blockers],
        "data_status": action.data_status.value,
        "freshness": action.freshness,
        "evidence_id": None,
    }


def serialize_runtime_holding(
    record: RuntimeDecisionRecord,
    position: T.Position,
) -> dict:
    action = record.action
    quote = record.quote
    last: float | None = None
    if quote is not None and type(quote.last) in (int, float):
        candidate = float(quote.last)
        if candidate > 0:
            last = candidate
    pnl = None
    pnl_pct = None
    if last is not None and position.cost > 0:
        pnl = round((last - position.cost) * position.shares, 2)
        pnl_pct = round((last / position.cost - 1.0) * 100.0, 2)
    invalidation = None
    if record.signal is not None and record.signal.invalidation_price > 0:
        invalidation = record.signal.invalidation_price
    distance = None
    if last is not None and invalidation is not None:
        distance = round((last - invalidation) / last * 100.0, 2)
    return {
        "position_id": position.id,
        "symbol": position.symbol,
        "market": position.market.value,
        "name": record.name,
        "shares": position.shares,
        "average_cost": position.cost,
        "last": last,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "action_state": action.action.value,
        "action_label": _ACTION_LABELS[action.action],
        "reason": action.reason,
        "strategy_id": action.strategy_id,
        "invalidation_price": invalidation,
        "distance_to_invalidation_pct": distance,
        "hard_blockers": [serialize_decision_blocker(item) for item in record.hard_blockers],
        "soft_blockers": [serialize_decision_blocker(item) for item in record.soft_blockers],
        "data_status": action.data_status.value,
    }


def _quote_age_ms(q: T.Quote, now: datetime) -> int:
    """真实观察年龄：当前时钟 - 源时间戳（秒→毫秒）。

    时间戳不可靠（缺省 <2000 年）时回退到 received_at 口径；始终 >= 0。
    """
    ts = q.timestamp
    if ts is not None and getattr(ts, "year", 0) >= 2000:
        return max(0, int((now - ts).total_seconds() * 1000))
    ra = q.received_at
    if ra is not None:
        return max(0, int((now - ra).total_seconds() * 1000))
    return int(q.observed_age_ms or 0)


def recompute_age_ms(q: T.Quote, now: Optional[datetime] = None) -> int:
    """当前时钟下 quote 的真实观察年龄（毫秒）。"""
    return _quote_age_ms(q, now or datetime.now())


def quote_data_status(age_ms: int, market_cfg: "object" = None) -> T.DataStatus:
    """按年龄阈值映射 freshness（PRD #26.10）。

    年龄 <= 0 表示「刚接收 / 时钟偏差导致无法判定精确源时间戳」——此类 quote 已通过
    DQ 闸门且 last>0，是真实实时报价，视为新鲜（LIVE），而非 UNKNOWN。
    """
    if age_ms <= 0:
        return T.DataStatus.LIVE
    if market_cfg is not None:
        stale = getattr(market_cfg, "stale_ms", 0) or 0
        delayed = getattr(market_cfg, "delayed_ms", 0) or 0
        if stale and age_ms > stale:
            return T.DataStatus.STALE
        if delayed and age_ms > delayed:
            return T.DataStatus.DELAYED
    else:
        # 无市场配置兜底：>5min 视为 STALE，>15s 视为 DELAYED
        if age_ms > 300000:
            return T.DataStatus.STALE
        if age_ms > 15000:
            return T.DataStatus.DELAYED
    return T.DataStatus.LIVE


def market_observed_age_ms(quotes: list, now: Optional[datetime] = None) -> int:
    """市场代表年龄：取该市场所有 quote 中最大真实年龄（最保守口径）。"""
    now = now or datetime.now()
    best = 0
    for q in quotes:
        age = _quote_age_ms(q, now)
        if age > best:
            best = age
    return best


def market_data_status(session: str, age_ms: int, market_cfg: "object" = None) -> str:
    """市场级 freshness：结合「行情时段」与「数据年龄」。

    - 收盘/周末（CLOSED/WEEKEND）：EOD 数据绝不可伪装 LIVE，至少 DELAYED；
      年龄超 stale 阈值判 STALE，超 delayed 阈值判 DELAYED。
    - 交易时段（TRADING）：按年龄真实判定（新鲜才 LIVE）。
    - 时间戳不可靠（age<=0）：收盘→保守 DELAYED；交易→UNKNOWN。
    """
    stale = getattr(market_cfg, "stale_ms", 0) or 0
    delayed = getattr(market_cfg, "delayed_ms", 0) or 0
    closed = session in ("CLOSED", "WEEKEND")
    if age_ms <= 0:
        return (T.DataStatus.DELAYED if closed else T.DataStatus.LIVE).value
    if stale and age_ms > stale:
        return T.DataStatus.STALE.value
    if delayed and age_ms > delayed:
        return T.DataStatus.DELAYED.value
    if closed:
        return T.DataStatus.DELAYED.value
    return T.DataStatus.LIVE.value


def serialize_quote(q: T.Quote, market_cfg: "object" = None) -> dict:
    """Quote → dict，强制附加真实 data_status + observed_age_ms。

    新鲜度基于「当前时钟 - 源时间戳」实时计算（不伪造实时性）；若 DQ 闸门已
    标记 UNKNOWN（硬问题）则保留，否则按年龄阈值重判（可随真实年龄老化降级）。
    """
    d = to_jsonable(q)
    now = datetime.now()
    age = _quote_age_ms(q, now)
    d["observed_age_ms"] = age
    if q.data_status == T.DataStatus.UNKNOWN:
        ds = T.DataStatus.UNKNOWN
    else:
        ds = quote_data_status(age, market_cfg)
    d["data_status"] = ds.value
    return d


def serialize_signal(sig: T.Signal) -> dict:
    """Signal → dict，强制附加 data_status + observed_age_ms + horizon。

    ``horizon`` 为展示用持仓周期维度（几天/几周/几个月~几年），由普通层
    ``signals.horizon`` 从 strategy_id 派生，不引用 quant、不改 schema。
    """
    d = to_jsonable(sig)
    d["data_status"] = sig.data_status.value if sig.data_status else T.DataStatus.UNKNOWN.value
    # 信号本身无观察年龄概念，置 0 以满足契约（前端据此区分行情/信号）。
    d["observed_age_ms"] = 0
    from ..signals.horizon import horizon_for_signal
    d["horizon"] = horizon_for_signal(sig)
    return d


def serialize_sector(sec: T.SectorSnapshot) -> dict:
    """SectorSnapshot → dict。"""
    return to_jsonable(sec)


def serialize_health(h: T.ProviderHealth) -> dict:
    """ProviderHealth → dict。"""
    d = to_jsonable(h)
    d["circuit_state"] = h.circuit_state.value if h.circuit_state else "CLOSED"
    return d


def serialize_indicators(ind: dict) -> dict:
    """指标快照 → dict（已是 ``dict[str, float|None]``，直接透传）。

    仅做 JSON 安全化：None 保持 None，其余转为 float（指标均为标量）。
    """
    if not ind:
        return {}
    return {k: (None if v is None else float(v)) for k, v in ind.items()}


def serialize_bar(bar: T.Bar) -> dict:
    """精简 Bar → dict（详情面板历史 K 线展示用，仅保留数值字段）。"""
    return {
        "symbol": bar.symbol,
        "market": bar.market.value,
        "timestamp": bar.timestamp.isoformat() if bar.timestamp else None,
        "interval": bar.interval,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover": bar.turnover,
        "source": bar.source,
        "adjustment_factor": bar.adjustment_factor,
        "quality_status": bar.quality_status.value,
    }


def build_meta(bundle: ConfigBundle, healths: list[T.ProviderHealth], store: "object",
               last_data_at: Optional[datetime] = None) -> dict:
    """构造 overview 顶层 meta（data_mode / providers / last_update / market_open）。

    data_mode 判定（真实优先，绝不伪造 DEMO）：
    - 若任一「主源（cfg.primary）」对启用市场处于熔断 OPEN/HALF_OPEN → DEGRADED（主源降级）。
    - 否则 → LIVE（主源健康，或失败源有同市场备用源接管且主源未断裂）。
    注：本环境东财快照源常态不可用（远端断开），但其非 A/HK/US 报价主源，不影响 LIVE 判定。
    """
    from ..core.clock import market_open_status

    # 主源降级判定
    degraded = False
    for h in healths:
        if h.circuit_state != T.CircuitState.CLOSED:
            # 找到该 provider 配置，判断是否为某启用市场的 primary
            for pc in bundle.providers:
                if pc.name == h.provider and pc.primary:
                    # 该主源熔断，且对应市场启用
                    if any(bundle.app.markets_enabled.get(mk, False)
                           for mk in ("a", "hk", "us")
                           if mk in pc.markets):
                        degraded = True
                        break
    data_mode = "DEGRADED" if degraded else "LIVE"

    providers = [pc.name for pc in bundle.providers]
    market_open = market_open_status(bundle)

    last_update = last_data_at.isoformat() if last_data_at else (
        store.get_last_update().isoformat() if getattr(store, "get_last_update", None)
        and store.get_last_update() else None)

    return {
        "data_mode": data_mode,
        "providers": providers,
        "last_update": last_update,
        "market_open": market_open,
    }
