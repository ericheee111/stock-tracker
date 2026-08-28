"""REST 端点实现（§9.1）。

所有函数接收 ``AppContext``，返回可 JSON 序列化的 dict。行情/信号类响应通过
``serializers`` 强制附加 ``data_status`` 与 ``observed_age_ms``。本模块只读
MarketStore + Repository，绝不调用上游 Provider。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..core import types as T
from ..core.config import ConfigBundle
from ..core.store import MarketStore
from ..decision.brief import build_decision_brief, sort_holding_actions
from ..decision.runtime import (
    RuntimeDecisionRecord,
    build_signal_record,
    build_unbound_position_record,
)
from ..decision.types import (
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    RiskMode,
    UserPortfolioProfile,
)
from ..features import feature_snapshot as FS
from ..signals.crowding import crowding_for
from ..storage.repository import (
    Repository,
    RepositoryConflictError,
    RepositoryValidationError,
    to_jsonable,
)
from . import serializers as S
from .sse import SSEHub


@dataclass
class AppContext:
    """API 共享上下文（由 __main__ 装配）。"""

    bundle: ConfigBundle
    store: MarketStore
    repo: Repository
    router: Any                 # ProviderRouter
    signal_manager: Any         # SignalManager
    sse_hub: SSEHub
    web_root: str = "web"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduler: Any = None
    monitor_service: Any = None
    monitor_subscription: Any = None


class APIError(ValueError):
    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field

    def response(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        return {"error": error}


_PROFILE_FIELDS = {
    "account_equity",
    "available_cash",
    "risk_mode",
    "per_trade_risk_pct",
    "max_position_pct",
    "max_portfolio_heat_pct",
    "max_sector_pct",
    "max_theme_pct",
}
_POSITION_CREATE_FIELDS = {"symbol", "market", "shares", "average_cost", "added_at"}
_POSITION_PATCH_FIELDS = {"shares", "average_cost"}


def _require_fields(payload: dict, allowed: set[str], required: set[str]) -> None:
    unknown = set(payload) - allowed
    if unknown:
        field = min(unknown)
        raise APIError(400, "UNKNOWN_FIELD", f"unknown field: {field}", field)
    missing = required - set(payload)
    if missing:
        field = min(missing)
        raise APIError(400, "MISSING_FIELD", f"missing required field: {field}", field)


def _finite_number(payload: dict, field: str, *, positive: bool = False, minimum: float = 0.0) -> float:
    value = payload[field]
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise APIError(400, "INVALID_NUMBER", f"{field} must be a finite number", field)
    number = float(value)
    if (positive and number <= 0) or (not positive and number < minimum):
        qualifier = "greater than zero" if positive else f">= {minimum:g}"
        raise APIError(400, "INVALID_NUMBER", f"{field} must be {qualifier}", field)
    return number


def _positive_integer(payload: dict, field: str) -> int:
    value = payload[field]
    if type(value) is not int or value <= 0:
        raise APIError(400, "INVALID_INTEGER", f"{field} must be a positive integer", field)
    return value


def _market_symbol(payload: dict) -> tuple[T.Market, str]:
    market_value = payload["market"]
    if type(market_value) is not str:
        raise APIError(400, "INVALID_MARKET", "market must be A, HK, or US", "market")
    try:
        market = T.Market(market_value.upper())
    except ValueError as exc:
        raise APIError(400, "INVALID_MARKET", "market must be A, HK, or US", "market") from exc
    symbol_value = payload["symbol"]
    if type(symbol_value) is not str or not symbol_value.strip():
        raise APIError(400, "INVALID_SYMBOL", "symbol must not be empty", "symbol")
    symbol = symbol_value.strip().upper()
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
    valid_suffixes = {T.Market.A: {"SH", "SZ"}, T.Market.HK: {"HK"}, T.Market.US: {"US"}}
    if suffix not in valid_suffixes[market] or not symbol.rsplit(".", 1)[0]:
        raise APIError(400, "INVALID_SYMBOL", "symbol suffix does not match market", "symbol")
    return market, symbol


def _aware_datetime(payload: dict, field: str) -> datetime:
    value = payload[field]
    if type(value) is not str:
        raise APIError(400, "INVALID_DATETIME", f"{field} must be an ISO 8601 datetime", field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise APIError(400, "INVALID_DATETIME", f"{field} must be an ISO 8601 datetime", field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise APIError(400, "INVALID_DATETIME", f"{field} must include a timezone", field)
    return parsed


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _best_signal_for(ctx: AppContext, symbol: str) -> dict | None:
    sigs = ctx.store.get_signals_by_symbol(symbol)
    if not sigs:
        return None
    best = max(sigs, key=lambda s: (s.scores.opportunity if s.scores else 0, s.state_changed_at))
    return S.serialize_signal(best)


def _load_bars_for_indicators(ctx: AppContext, symbol: str, market, n: int) -> list:
    """Load bars only for the exact security identity requested.

    A missing ``.SH`` series must never fall back to the same numeric code with
    a ``.SZ`` suffix (or vice versa): those are distinct instruments. Identity
    normalization belongs at the provider/ingestion boundary; the API fails
    closed and returns no indicators when the exact series is unavailable.
    """

    bars = ctx.repo.load_recent_bars(symbol, "1d", n=n)
    return [
        bar
        for bar in bars
        if bar.symbol == symbol and bar.market is market
    ]


def _top_opportunities(ctx: AppContext, limit: int = 12) -> list[dict]:
    # 同一标的可能被多个策略各自产出信号；按 symbol 去重，仅保留机会分最高的一条，
    # 避免 Top 机会列表出现重复标的（例如同一只股票被两条策略同时命中）。
    best_by_symbol: dict[str, Any] = {}
    for s in ctx.store.get_signals().values():
        score = s.scores.opportunity if s.scores else 0
        prev = best_by_symbol.get(s.symbol)
        prev_score = prev.scores.opportunity if (prev is not None and prev.scores) else 0
        if prev is None or score > prev_score:
            best_by_symbol[s.symbol] = s
    sigs = sorted(best_by_symbol.values(),
                  key=lambda s: (s.scores.opportunity if s.scores else 0), reverse=True)
    out: list[dict] = []
    for s in sigs[:limit]:
        # 补发实时报价与名称：与 get_watchlist/get_positions 的 quote 字段保持一致，
        # 否则前端机会列表无法渲染价格（sig.quote 为 undefined → 渲染成 "—"）。
        q = ctx.store.get_quote(s.symbol)
        quote_d = S.serialize_quote(q, _market_cfg(ctx, q.market)) if q is not None else None
        # 名称来源：优先取实时报价的 name；Signal 类型本身无 name 字段，故回退用 symbol。
        name = (q.name if (q is not None and q.name) else s.symbol)
        # 展示用技术指标（仅数值，不评分/不加权）；有 K 线即计算，无则 None（前端渲染「—」）。
        bars = _load_bars_for_indicators(ctx, s.symbol, s.market, 60)
        indicators = S.serialize_indicators(FS.build_indicators(bars, s.market)) if bars else None
        out.append({
            "symbol": s.symbol,
            "market": s.market.value,
            "state": s.state.value,
            "strategy_id": s.strategy_id,
            "name": name,
            "scores": S.serialize_signal(s).get("scores"),
            "reason": s.reason,
            "next_trigger": s.next_trigger,
            "quote": quote_d,
            "indicators": indicators,
            # §24.6 拥挤度/追高风险仪表：由已算出的展示指标派生，纯展示、不进评分。
            "crowding": crowding_for(indicators, s),
            "data_status": (q.data_status.value if (q is not None and q.data_status)
                            else (s.data_status.value if s.data_status else "UNKNOWN")),
        })
    return out


def get_quote_detail(ctx: AppContext, symbol: str) -> dict | None:
    """``/api/quote/{symbol}``：返回单标的详情（实时报价 + 展示指标 + 近期 K 线）。

    与 overview 的 indicators 同源（``build_indicators``，纯展示数值，不评分/不加权）。
    ``recent_bars`` 最多返回最近 30 根（展示用，避免响应体过大）。
    """
    if not symbol or "." not in symbol:
        return None
    code, suffix = symbol.rsplit(".", 1)
    if not code or suffix not in {"SH", "SZ", "HK", "US"}:
        return None
    market = T.market_from_symbol(symbol)
    q = ctx.store.get_quote(symbol)
    quote_d = S.serialize_quote(q, _market_cfg(ctx, market)) if q is not None else None
    name = (q.name if (q is not None and q.name) else symbol)
    # 加载足够计算全部指标的历史（roc60/ma60 需 ~61 根）
    recent = _load_bars_for_indicators(ctx, symbol, market, 80)
    indicators = S.serialize_indicators(FS.build_indicators(recent, market)) if recent else None
    recent_bars = [S.serialize_bar(b) for b in recent[-30:]] if recent else []
    return {
        "symbol": symbol,
        "market": market.value,
        "name": name,
        "quote": quote_d,
        "indicators": indicators,
        "recent_bars": recent_bars,
        "bar_count": len(recent) if recent else 0,
    }


def _market_cfg(ctx: AppContext, market: T.Market):
    """取某市场的配置（用于新鲜度阈值）。"""
    return {"A": ctx.bundle.markets.a, "HK": ctx.bundle.markets.hk,
            "US": ctx.bundle.markets.us}[market.value]


def _build_index(q: T.Quote | None, ctx: AppContext, mk: T.Market) -> dict:
    """构造单一代表性指数行情摘要；无有效行情时 last/change 为 null（前端渲染「—」）。"""
    last = q.last if (q is not None and q.last is not None and q.last > 0) else None
    prev = q.prev_close if (q is not None and q.prev_close is not None) else None
    change: float | None = None
    change_pct: float | None = None
    if last is not None and prev is not None and prev > 0:
        change = round(last - prev, 4)
        change_pct = round((last - prev) / prev * 100.0, 4)
    mc = _market_cfg(ctx, mk)
    age = S.recompute_age_ms(q) if q is not None else 0
    if q is not None and q.data_status != T.DataStatus.UNKNOWN:
        ds = S.quote_data_status(age, mc).value
    else:
        ds = T.DataStatus.UNKNOWN.value
    index_map = getattr(ctx.bundle.markets, "index", None) or {}
    idx_sym = (q.symbol if q is not None else index_map.get(mk.value.lower(), ""))
    name = (q.name if (q is not None and q.name) else idx_sym)
    return {
        "symbol": idx_sym,
        "name": name or idx_sym,
        "last": last,
        "change": change,
        "change_pct": change_pct,
        "data_status": ds,
        "observed_age_ms": age,
    }


def _build_markets(ctx: AppContext) -> list[dict]:
    """构造各市场汇总（含代表性指数 index），供 /api/markets 与 /api/overview 共用。

    返回数组，每项形如 {market, enabled, session, count, up, down, flat,
    latency_p50_ms, data_status, observed_age_ms, index}。前端 renderIndexGrid
    按 market 过滤后读取 index 渲染指数卡。
    """
    healths = {h.provider: h for h in ctx.router.health_list()}
    quotes = ctx.store.get_quotes()
    index_map = getattr(ctx.bundle.markets, "index", None) or {}
    out: list[dict] = []
    for key, mk in (("a", T.Market.A), ("hk", T.Market.HK), ("us", T.Market.US)):
        if not ctx.bundle.app.markets_enabled.get(key, False):
            out.append({"market": key, "enabled": False, "session": "DISABLED"})
            continue
        mq = [q for q in quotes.values() if q.market == mk]
        session = _session(ctx, mk)
        # 宽度统计：价格缺失（None）的标的跳过，避免 None 比较崩溃
        up = sum(1 for q in mq if q.last is not None and q.prev_close is not None
                 and q.prev_close > 0 and q.last > q.prev_close)
        down = sum(1 for q in mq if q.last is not None and q.prev_close is not None
                   and q.prev_close > 0 and q.last < q.prev_close)
        flat = len(mq) - up - down
        latency = None
        for pc in ctx.bundle.providers:
            if pc.primary and key in pc.markets:
                h = healths.get(pc.name)
                if h:
                    latency = round(h.latency_p50, 1)
        mc = _market_cfg(ctx, mk)
        age = S.market_observed_age_ms(mq)
        idx_sym = index_map.get(key)
        idx = _build_index(quotes.get(idx_sym) if idx_sym else None, ctx, mk)
        out.append({
            "market": key,
            "enabled": True,
            "session": session,
            "count": len(mq),
            "up": up, "down": down, "flat": flat,
            "latency_p50_ms": latency,
            "data_status": S.market_data_status(session, age, mc),
            "observed_age_ms": age,
            "index": idx,
        })
    return out


def _build_breadth(ctx: AppContext) -> dict:
    """市场宽度汇总（§9 契约：前端 UI.renderBreadthCard 依赖）。

    结构：{a:{advancers,decliners,flat,ratio}, hk:{...}, us:{...}, total:{...}}。
    ratio = 上涨家数 / (上涨 + 下跌)，无涨跌时记 0。
    """
    quotes = list(ctx.store.get_quotes().values())
    out: dict[str, dict] = {}
    adv_total = dec_total = flat_total = 0
    for key, mk in (("a", T.Market.A), ("hk", T.Market.HK), ("us", T.Market.US)):
        if not ctx.bundle.app.markets_enabled.get(key, False):
            out[key] = {"enabled": False, "advancers": 0, "decliners": 0,
                        "flat": 0, "ratio": 0.0}
            continue
        mq = [q for q in quotes if q.market == mk]
        adv = sum(1 for q in mq if q.last is not None and q.prev_close is not None
                  and q.prev_close > 0 and q.last > q.prev_close)
        dec = sum(1 for q in mq if q.last is not None and q.prev_close is not None
                  and q.prev_close > 0 and q.last < q.prev_close)
        flat = len(mq) - adv - dec
        denom = adv + dec
        out[key] = {
            "advancers": adv, "decliners": dec, "flat": flat,
            "ratio": round(adv / denom, 4) if denom > 0 else 0.0,
        }
        adv_total += adv
        dec_total += dec
        flat_total += flat
    denom = adv_total + dec_total
    out["total"] = {
        "advancers": adv_total, "decliners": dec_total, "flat": flat_total,
        "ratio": round(adv_total / denom, 4) if denom > 0 else 0.0,
    }
    return out


def _active_risk_events(ctx: AppContext) -> list[dict]:
    """当前活跃的高风险信号（§9 契约：前端 UI.renderRiskCard 依赖）。

    来源：SignalManager 维护的 store 信号中处于活跃态（WATCH/ARMED/TRIGGERED/
    ACTIVE/TRIM/OVEREXTENDED）且风险分偏高或已标记为追高的条目。无则空数组。
    """
    active = ctx.store.active_signal_states()
    events: list[dict] = []
    for sig in ctx.store.get_signals().values():
        if sig.state not in active:
            continue
        risk = sig.scores.risk if sig.scores else 0
        level = "HIGH" if risk >= 60 else ("MEDIUM" if risk >= 35 else "LOW")
        if risk >= 35 or sig.state == T.SignalState.OVEREXTENDED:
            events.append({
                "symbol": sig.symbol,
                "market": sig.market.value,
                "signal_id": sig.signal_id,
                "state": sig.state.value,
                "level": level,
                "risk_score": risk,
                "reason": sig.reason or "",
            })
    events.sort(key=lambda e: e["risk_score"], reverse=True)
    return events


def _active_holding_signals(ctx: AppContext) -> list[dict]:
    """收市态面板用的「中长线持仓信号」。

    来源：store 中处于活跃态（WATCH/ARMED/TRIGGERED/ACTIVE/TRIM/OVEREXTENDED）
    的全部信号（**不取前 12**，与 top_opportunities 的去重截断不同——收市面板
    要呈现完整持仓跨度，按 state_changed_at 降序）。每项经 serialize_signal
    已含 ``horizon``（几天/几周/几个月~几年）维度，供前端分组展示。
    """
    active = ctx.store.active_signal_states()
    sigs = [s for s in ctx.store.get_signals().values() if s.state in active]
    sigs.sort(key=lambda s: s.state_changed_at, reverse=True)
    out: list[dict] = []
    for s in sigs:
        d = S.serialize_signal(s)
        # Signal 类型无 name 字段：补发名称（优先实时报价 name，回退 symbol），
        # 与 top_opportunities 的 name 口径一致，避免前端渲染成代码。
        q = ctx.store.get_quote(s.symbol)
        d["name"] = (q.name if (q is not None and q.name) else s.symbol)
        # §24.6 拥挤度仪表：加载该标的展示指标并派生拥挤度（与 top_opportunities 同源）。
        bars = _load_bars_for_indicators(ctx, s.symbol, s.market, 60)
        ind = S.serialize_indicators(FS.build_indicators(bars, s.market)) if bars else None
        d["indicators"] = ind
        d["crowding"] = crowding_for(ind, s)
        out.append(d)
    return out


def _response_freshness(ctx: AppContext, quotes: list) -> tuple[str, int]:
    """聚合一组 quote 的新鲜度，供列表类端点的顶层 data_status/observed_age_ms 使用。

    不伪造实时性：observed_age_ms 取各 quote 真实年龄的最大值；data_status 取
    各 quote 按市场阈值判定后的最严重者。
    """
    ages = [S.recompute_age_ms(q) for q in quotes if q is not None]
    age = max(ages) if ages else 0
    if age <= 0:
        return "UNKNOWN", 0
    order = {"UNKNOWN": 0, "LIVE": 1, "DELAYED": 2, "STALE": 3}
    worst = "LIVE"
    for q in quotes:
        if q is None:
            continue
        st = S.quote_data_status(S.recompute_age_ms(q), _market_cfg(ctx, q.market))
        if order.get(st.value, 0) > order.get(worst, 0):
            worst = st.value
    return worst, age


_STATUS_SEVERITY = {
    T.DataStatus.LIVE: 0,
    T.DataStatus.DELAYED: 1,
    T.DataStatus.STALE: 2,
    T.DataStatus.UNKNOWN: 3,
}
_HOLDING_ACTION_PRIORITY = {
    ActionState.EXIT: 0,
    ActionState.TRIM: 1,
    ActionState.WARNING: 2,
    ActionState.HOLD: 3,
    ActionState.DATA_BLOCKED: 4,
    ActionState.PARTIAL_TAKE_PROFIT: 5,
    ActionState.TREND_RUNNER: 6,
}
_REGIME_LABELS = {
    T.RegimeState.RISK_ON_TREND: "趋势进攻",
    T.RegimeState.ROTATION: "震荡轮动",
    T.RegimeState.RISK_OFF: "风险规避",
    T.RegimeState.PANIC_REBOUND: "恐慌反弹",
    T.RegimeState.OVERHEATED: "过热",
}
_REGIME_AGGRESSION = {
    T.RegimeState.RISK_ON_TREND: 70,
    T.RegimeState.ROTATION: 50,
    T.RegimeState.RISK_OFF: 20,
    T.RegimeState.PANIC_REBOUND: 40,
    T.RegimeState.OVERHEATED: 30,
}
_REGIME_RISKS = {
    T.RegimeState.RISK_ON_TREND: "趋势仍需服从个股赔率和拥挤度约束",
    T.RegimeState.ROTATION: "板块轮动较快，追高后赔率容易恶化",
    T.RegimeState.RISK_OFF: "整体风险偏高，新增仓位需要明显收缩",
    T.RegimeState.PANIC_REBOUND: "反弹稳定性尚未确认，避免把修复当成新趋势",
    T.RegimeState.OVERHEATED: "市场过热，重点防范拥挤和高位回撤",
}


def _worst_data_status(statuses: list[T.DataStatus]) -> T.DataStatus:
    if not statuses:
        return T.DataStatus.UNKNOWN
    return max(statuses, key=lambda item: _STATUS_SEVERITY[item])


def _effective_quote_status(ctx: AppContext, quote: T.Quote | None) -> T.DataStatus:
    if quote is None:
        return T.DataStatus.UNKNOWN
    observed = (
        quote.data_status
        if isinstance(quote.data_status, T.DataStatus)
        else T.DataStatus.UNKNOWN
    )
    aged = S.quote_data_status(
        S.recompute_age_ms(quote),
        _market_cfg(ctx, quote.market),
    )
    return _worst_data_status([observed, aged])


def _sector_name(ctx: AppContext, symbol: str) -> str:
    meta = ctx.store.get_instrument(symbol) or {}
    sector = meta.get("sector")
    if type(sector) is str and sector.strip():
        return sector.strip()
    return "UNKNOWN"


def _position_reference_value(
    ctx: AppContext,
    position: T.Position,
) -> tuple[float, bool]:
    quote = ctx.store.get_quote(position.symbol)
    status = _effective_quote_status(ctx, quote)
    if (
        quote is not None
        and status in (T.DataStatus.LIVE, T.DataStatus.DELAYED)
        and type(quote.last) in (int, float)
        and math.isfinite(float(quote.last))
        and float(quote.last) > 0
    ):
        return float(quote.last), True
    return float(position.cost), False


def _portfolio_decision_context(
    ctx: AppContext,
    profile: UserPortfolioProfile | None,
    positions: list[T.Position],
) -> tuple[float, dict[str, float], tuple[DecisionBlocker, ...]]:
    if profile is None:
        return 0.0, {}, ()
    equity = float(profile.account_equity)
    total_risk = 0.0
    sector_values: dict[str, float] = {}
    incomplete: list[str] = []
    for position in positions:
        reference, price_reliable = _position_reference_value(ctx, position)
        sector = _sector_name(ctx, position.symbol)
        sector_values[sector] = sector_values.get(sector, 0.0) + (
            reference * float(position.shares)
        )
        valid_invalidations = [
            float(signal.invalidation_price)
            for signal in ctx.store.get_signals_by_symbol(position.symbol)
            if type(signal.invalidation_price) in (int, float)
            and math.isfinite(float(signal.invalidation_price))
            and 0 < float(signal.invalidation_price) < reference
        ]
        if not price_reliable or not valid_invalidations:
            incomplete.append(position.symbol)
            continue
        conservative_invalidation = min(valid_invalidations)
        total_risk += float(position.shares) * (
            reference - conservative_invalidation
        )
    heat = min(1.0, max(0.0, total_risk / equity))
    exposures = {
        sector: min(1.0, max(0.0, value / equity))
        for sector, value in sector_values.items()
    }
    blockers: tuple[DecisionBlocker, ...] = ()
    if incomplete:
        shown = "、".join(sorted(incomplete)[:3])
        suffix = " 等" if len(incomplete) > 3 else ""
        blockers = (
            DecisionBlocker(
                code="PORTFOLIO_RISK_INCOMPLETE",
                message=(
                    f"现有持仓 {shown}{suffix} 缺少可靠现价或结构失效位，"
                    "暂不生成新增仓位建议"
                ),
                severity=BlockerSeverity.HARD,
                recoverable=True,
            ),
        )
    return heat, exposures, blockers


def _instrument_lot_size(ctx: AppContext, symbol: str, market: T.Market) -> int | None:
    if market is not T.Market.HK:
        return None
    meta = ctx.store.get_instrument(symbol) or {}
    lot_size = meta.get("lot_size")
    if type(lot_size) is int and lot_size > 0:
        return lot_size
    return None


def _posture_payload(ctx: AppContext) -> tuple[dict, str, int]:
    regime = ctx.store.get_regime()
    state = regime.regime if regime is not None else T.RegimeState.ROTATION
    aggression = _REGIME_AGGRESSION[state]
    label = _REGIME_LABELS[state]
    reliable_sectors = [
        sector
        for sector in ctx.store.get_sectors().values()
        if sector.sector.strip().upper() not in {"", "UNKNOWN", "BROAD"}
    ]
    strongest = max(reliable_sectors, key=lambda item: item.score, default=None)
    strongest_theme = strongest.sector if strongest is not None else "暂无可靠板块数据"
    return (
        {
            "market": "A",
            "regime": state.value,
            "label": label,
            "aggression_level": aggression,
            "strongest_theme": strongest_theme,
            "main_risk": _REGIME_RISKS[state],
        },
        state.value,
        aggression,
    )


def _today_summary_text(
    data_status: T.DataStatus,
    aggression_level: int,
    executable_count: int,
    holding_attention_count: int,
) -> str:
    if data_status in (T.DataStatus.STALE, T.DataStatus.UNKNOWN):
        return "当前关键数据不足，暂停新增执行判断，以核对数据和持仓风险为主。"
    if aggression_level >= 65:
        prefix = "今天可以适度进攻，但只执行通过数据、赔率和风险闸门的机会。"
    elif aggression_level >= 40:
        prefix = "今天以持仓管理为主，只选择性开新仓。"
    else:
        prefix = "今天以防守和控制回撤为主，原则上不主动扩大仓位。"
    return (
        f"{prefix} 当前有 {executable_count} 个机会具备执行条件，"
        f"{holding_attention_count} 个持仓需要重点处理。"
    )


def _record_by_action(
    records: list[RuntimeDecisionRecord],
) -> dict[int, RuntimeDecisionRecord]:
    return {id(record.action): record for record in records}


def get_today_brief(ctx: AppContext) -> dict:
    """Return the deterministic Stage 1 Today Action brief.

    This endpoint consumes only MarketStore and Repository state. It does not
    call any Provider, LLM, model-training path, or quantitative migration.
    """

    as_of = datetime.now(timezone.utc)
    profile = ctx.repo.load_portfolio_profile()
    positions = ctx.repo.load_positions()
    ctx.store.set_portfolio_profile(profile)
    ctx.store.set_positions(positions)
    positions_by_symbol = {
        position.symbol: position
        for position in positions
        if position.closed_at is None
    }
    heat, sector_exposures, portfolio_blockers = _portfolio_decision_context(
        ctx,
        profile,
        list(positions_by_symbol.values()),
    )
    no_chase_pct = float(
        getattr(ctx.bundle.risk, "overextension_max_above_entry_pct", 0.05)
    )

    core_records: list[RuntimeDecisionRecord] = []
    holding_candidates: list[RuntimeDecisionRecord] = []
    invalid_signal_messages: list[str] = []
    for signal in ctx.store.get_signals().values():
        quote = ctx.store.get_quote(signal.symbol)
        status = _effective_quote_status(ctx, quote)
        sector = _sector_name(ctx, signal.symbol)
        has_position = signal.symbol in positions_by_symbol
        try:
            record = build_signal_record(
                signal,
                quote=quote,
                data_status=status,
                has_position=has_position,
                profile=None if has_position else profile,
                current_portfolio_heat_pct=heat,
                current_sector_exposure_pct=sector_exposures.get(sector, 0.0),
                current_theme_exposure_pct=sector_exposures.get(sector, 0.0),
                sector=sector,
                as_of=as_of,
                no_chase_pct=no_chase_pct,
                lot_size=_instrument_lot_size(ctx, signal.symbol, signal.market),
                external_hard_blockers=() if has_position else portfolio_blockers,
            )
        except DecisionContractError as exc:
            invalid_signal_messages.append(
                f"{signal.symbol} {signal.strategy_id}: {exc}"
            )
            continue
        if has_position:
            holding_candidates.append(record)
        else:
            core_records.append(record)

    holding_records: list[RuntimeDecisionRecord] = []
    for symbol, position in positions_by_symbol.items():
        candidates = [
            record for record in holding_candidates if record.action.symbol == symbol
        ]
        if candidates:
            candidates.sort(
                key=lambda record: (
                    _HOLDING_ACTION_PRIORITY.get(record.action.action, 99),
                    -record.action.risk,
                    record.action.strategy_id,
                )
            )
            holding_records.append(candidates[0])
        else:
            quote = ctx.store.get_quote(symbol)
            holding_records.append(
                build_unbound_position_record(
                    position,
                    quote=quote,
                    data_status=_effective_quote_status(ctx, quote),
                    sector=_sector_name(ctx, symbol),
                )
            )

    all_statuses = [record.action.data_status for record in core_records]
    all_statuses.extend(record.action.data_status for record in holding_records)
    if not all_statuses:
        all_statuses = [
            _effective_quote_status(ctx, quote)
            for quote in ctx.store.get_quotes().values()
        ]
    data_health = _worst_data_status(all_statuses)
    posture, posture_state, aggression_level = _posture_payload(ctx)

    avoid_messages: list[str] = []
    for record in core_records:
        if record.action.action in (ActionState.AVOID, ActionState.DATA_BLOCKED):
            avoid_messages.append(record.action.reason)
    avoid_messages.extend(blocker.message for blocker in portfolio_blockers)
    avoid_messages.extend(invalid_signal_messages)
    avoid_messages.append(posture["main_risk"])
    avoid_reasons = tuple(dict.fromkeys(item for item in avoid_messages if item))

    brief = build_decision_brief(
        as_of=as_of,
        market_posture=posture_state,
        aggression_level=aggression_level,
        core_candidates=tuple(record.action for record in core_records),
        holding_actions=tuple(record.action for record in holding_records),
        avoid_reasons=avoid_reasons,
        data_health=data_health,
    )
    core_lookup = _record_by_action(core_records)
    holding_lookup = _record_by_action(holding_records)
    selected_core = [core_lookup[id(action)] for action in brief.core_opportunities]
    selected_holdings = [
        holding_lookup[id(action)] for action in sort_holding_actions(brief.holding_actions)
    ]

    executable_count = sum(
        record.action.action is ActionState.EXECUTABLE for record in selected_core
    )
    waiting_count = sum(
        record.action.action
        in (ActionState.WAIT_PULLBACK, ActionState.WAIT_BREAKOUT)
        for record in selected_core
    )
    holding_attention_count = sum(
        record.action.action
        in (ActionState.EXIT, ActionState.TRIM, ActionState.WARNING, ActionState.DATA_BLOCKED)
        for record in selected_holdings
    )
    facts = list(brief.summary_facts)
    if profile is None:
        facts.append("尚未设置账户净值和现金，暂不生成建议股数")
    if invalid_signal_messages:
        facts.append(f"{len(invalid_signal_messages)} 条信号因合同不完整被跳过")

    serialized_holdings = []
    for record in selected_holdings:
        position = positions_by_symbol[record.action.symbol]
        serialized_holdings.append(S.serialize_runtime_holding(record, position))

    return {
        "schema_version": "stage1-v1",
        "as_of": as_of.isoformat(),
        "data_status": brief.data_health.value,
        "ranking_mode": brief.ranking_mode.value,
        "evidence_id": None,
        "market_posture": posture,
        "summary": {
            "mode": "DETERMINISTIC_TEMPLATE",
            "text": _today_summary_text(
                brief.data_health,
                aggression_level,
                executable_count,
                holding_attention_count,
            ),
            "facts": facts,
        },
        "actions": {
            "executable_count": executable_count,
            "waiting_count": waiting_count,
            "holding_attention_count": holding_attention_count,
        },
        "core_opportunities": [
            S.serialize_runtime_opportunity(record) for record in selected_core
        ],
        "holding_actions": serialized_holdings,
        "avoid_reasons": [
            {"code": f"AVOID_{index + 1}", "message": message}
            for index, message in enumerate(brief.avoid_reasons[:5])
        ],
        "big_trend": {
            "status": "NOT_AVAILABLE",
            "message": "正式主升浪算法尚未启用；当前不使用 SectorScore 冒充主升浪状态",
            "items": [],
        },
        "strategy_evidence": {
            "status": "INSUFFICIENT_REAL_EVIDENCE",
            "message": "当前只有工程合同和合成验证，暂不展示真实策略战绩",
        },
    }


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
def get_overview(ctx: AppContext) -> dict:
    healths = ctx.router.health_list()
    meta = S.build_meta(ctx.bundle, healths, ctx.store)
    regime = ctx.store.get_regime()
    heat = ctx.signal_manager._portfolio_heat() if ctx.signal_manager else 0.0
    quotes = ctx.store.get_quotes()
    return {
        "meta": meta,
        "regime": to_jsonable(regime) if regime else None,
        "portfolio_heat": round(heat, 3),
        "top_opportunities": _top_opportunities(ctx),
        # 收市态面板：全量活跃信号（含 horizon 维度），不取前 12（展示完整持仓跨度）。
        "holding_signals": _active_holding_signals(ctx),
        # 宽度汇总（§9 契约：UI.renderBreadthCard 依赖）
        "breadth": _build_breadth(ctx),
        # 活跃风险事件（§9 契约：UI.renderRiskCard 依赖）
        "risk_events": _active_risk_events(ctx),
        # 顶层附带，满足「行情/信号响应必含」契约（概览层面用整体模式表达）
        "data_status": meta["data_mode"],
        "observed_age_ms": S.market_observed_age_ms(list(quotes.values())),
        # 各市场汇总（含代表性指数 index），供前端指数卡 + 实时探针读取
        "markets": _build_markets(ctx),
    }


def get_watchlist(ctx: AppContext) -> dict:
    items = ctx.store.get_watchlist()
    out: list[dict] = []
    qs: list = []
    for sym, it in items.items():
        q = ctx.store.get_quote(sym)
        qs.append(q)
        entry = {
            "symbol": sym,
            "market": it.market.value,
            "added_at": it.added_at.isoformat() if it.added_at else None,
            "note": it.note,
            "quote": S.serialize_quote(q, _market_cfg(ctx, q.market)) if q else None,
            "signal": _best_signal_for(ctx, sym),
        }
        out.append(entry)
    ds, age = _response_freshness(ctx, qs)
    return {"watchlist": out, "count": len(out),
            "data_status": ds, "observed_age_ms": age}


def get_positions(ctx: AppContext) -> dict:
    positions = ctx.store.get_positions()
    out: list[dict] = []
    qs: list = []
    for pid, p in positions.items():
        q = ctx.store.get_quote(p.symbol)
        qs.append(q)
        last = q.last if (q is not None and q.last is not None) else p.cost
        pnl = (last - p.cost) * p.shares if p.shares else 0.0
        pnl_pct = ((last / p.cost - 1.0) * 100.0) if p.cost > 0 else 0.0
        if q is not None:
            mc = _market_cfg(ctx, q.market)
            pos_ds = S.quote_data_status(S.recompute_age_ms(q), mc).value
            pos_age = S.recompute_age_ms(q)
        else:
            pos_ds, pos_age = "UNKNOWN", 0
        out.append({
            "id": pid,
            "symbol": p.symbol,
            "market": p.market.value,
            "shares": p.shares,
            "cost": p.cost,
            "last": last,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "signal": _best_signal_for(ctx, p.symbol),
            "data_status": pos_ds,
            "observed_age_ms": pos_age,
        })
    ds, age = _response_freshness(ctx, qs)
    return {"positions": out, "count": len(out),
            "data_status": ds, "observed_age_ms": age}


def get_portfolio(ctx: AppContext) -> dict:
    profile = ctx.repo.load_portfolio_profile()
    positions = ctx.repo.load_positions()
    ctx.store.set_portfolio_profile(profile)
    ctx.store.set_positions(positions)
    return {
        "schema_version": "stage1-v1",
        "profile": S.serialize_portfolio_profile(profile) if profile else None,
        "positions": [S.serialize_position(position) for position in positions],
    }


def put_portfolio_profile(ctx: AppContext, payload: dict) -> dict:
    _require_fields(payload, _PROFILE_FIELDS, _PROFILE_FIELDS)
    risk_mode_value = payload["risk_mode"]
    if type(risk_mode_value) is not str:
        raise APIError(400, "INVALID_RISK_MODE", "risk_mode is invalid", "risk_mode")
    try:
        risk_mode = RiskMode(risk_mode_value.upper())
    except ValueError as exc:
        raise APIError(400, "INVALID_RISK_MODE", "risk_mode is invalid", "risk_mode") from exc
    try:
        profile = UserPortfolioProfile(
            account_equity=_finite_number(payload, "account_equity", positive=True),
            available_cash=_finite_number(payload, "available_cash"),
            risk_mode=risk_mode,
            per_trade_risk_pct=_finite_number(payload, "per_trade_risk_pct", positive=True),
            max_position_pct=_finite_number(payload, "max_position_pct", positive=True),
            max_portfolio_heat_pct=_finite_number(
                payload, "max_portfolio_heat_pct", positive=True
            ),
            max_sector_pct=_finite_number(payload, "max_sector_pct", positive=True),
            max_theme_pct=_finite_number(payload, "max_theme_pct", positive=True),
            updated_at=datetime.now(timezone.utc),
        )
    except DecisionContractError as exc:
        raise APIError(400, "INVALID_PROFILE", str(exc)) from exc
    ctx.repo.save_portfolio_profile(profile)
    ctx.store.set_portfolio_profile(profile)
    return S.serialize_portfolio_profile(profile)


def post_portfolio_position(ctx: AppContext, payload: dict) -> dict:
    _require_fields(payload, _POSITION_CREATE_FIELDS, _POSITION_CREATE_FIELDS)
    market, symbol = _market_symbol(payload)
    shares = _positive_integer(payload, "shares")
    average_cost = _finite_number(payload, "average_cost", positive=True)
    added_at = _aware_datetime(payload, "added_at")
    try:
        position = ctx.repo.create_position(
            symbol=symbol,
            market=market,
            shares=shares,
            average_cost=average_cost,
            added_at=added_at,
        )
    except RepositoryConflictError as exc:
        raise APIError(409, "POSITION_CONFLICT", str(exc), "symbol") from exc
    except RepositoryValidationError as exc:
        raise APIError(400, "INVALID_POSITION", str(exc)) from exc
    ctx.store.upsert_position(position)
    return S.serialize_position(position)


def patch_portfolio_position(ctx: AppContext, position_id: str, payload: dict) -> dict:
    _require_fields(payload, _POSITION_PATCH_FIELDS, set())
    if not payload:
        raise APIError(400, "EMPTY_PATCH", "PATCH payload must contain at least one field")
    shares = _positive_integer(payload, "shares") if "shares" in payload else None
    average_cost = (
        _finite_number(payload, "average_cost", positive=True)
        if "average_cost" in payload
        else None
    )
    current = ctx.repo.get_position(position_id)
    if current is None:
        raise APIError(404, "POSITION_NOT_FOUND", "position not found")
    try:
        position = ctx.repo.update_position(
            position_id,
            shares=shares,
            average_cost=average_cost,
        )
    except RepositoryValidationError as exc:
        raise APIError(400, "INVALID_POSITION", str(exc)) from exc
    if position is None:
        raise APIError(404, "POSITION_NOT_FOUND", "position not found")
    ctx.store.upsert_position(position)
    return S.serialize_position(position)


def delete_portfolio_position(ctx: AppContext, position_id: str) -> dict:
    if not ctx.repo.delete_position(position_id):
        raise APIError(404, "POSITION_NOT_FOUND", "position not found")
    ctx.store.remove_position(position_id)
    return {"ok": True, "position_id": position_id}


def get_radar(ctx: AppContext) -> dict:
    sigs = list(ctx.store.get_signals().values())
    sigs.sort(key=lambda s: (s.scores.opportunity if s.scores else 0), reverse=True)
    signals_out = [S.serialize_signal(s) for s in sigs]
    # 候选：有行情但尚无信号的自选标的
    candidates: list[dict] = []
    qs: list = []
    wl = ctx.store.get_watchlist()
    for sym in wl:
        if not ctx.store.get_signals_by_symbol(sym):
            q = ctx.store.get_quote(sym)
            if q:
                qs.append(q)
                candidates.append(S.serialize_quote(q, _market_cfg(ctx, q.market)))
    ds, age = _response_freshness(ctx, qs)
    return {
        "signals": signals_out,
        "candidates": candidates,
        "count": len(signals_out),
        "data_status": ds, "observed_age_ms": age,
    }


def get_signal(ctx: AppContext, signal_id: str) -> dict | None:
    sig = ctx.store.get_signal(signal_id)
    if sig is None:
        return None
    history = ctx.repo.load_signal_history(signal_id)
    serialized = S.serialize_signal(sig)
    serialized["history"] = history
    serialized["why_not_buy"] = (sig.scores.negative_reasons if sig.scores else [])
    serialized["positive_reasons"] = (sig.scores.positive_reasons if sig.scores else [])
    return serialized


def get_markets(ctx: AppContext) -> dict:
    healths = {h.provider: h for h in ctx.router.health_list()}
    quotes = ctx.store.get_quotes()
    index_map = getattr(ctx.bundle.markets, "index", None) or {}
    out: dict[str, dict] = {}
    for key, mk in (("a", T.Market.A), ("hk", T.Market.HK), ("us", T.Market.US)):
        idx_sym = index_map.get(key)
        idx = _build_index(quotes.get(idx_sym) if idx_sym else None, ctx, mk)
        if not ctx.bundle.app.markets_enabled.get(key, False):
            out[key] = {"enabled": False, "session": "DISABLED", "index": idx}
            continue
        mq = [q for q in quotes.values() if q.market == mk]
        session = _session(ctx, mk)
        if not mq:
            out[key] = {
                "enabled": True, "session": session, "count": 0,
                "up": 0, "down": 0, "flat": 0, "latency_p50_ms": None,
                "data_status": "UNKNOWN", "observed_age_ms": 0, "index": idx,
            }
            continue
        # 宽度统计：价格缺失（None）的标的跳过，避免 None 比较崩溃
        up = sum(1 for q in mq if q.prev_close is not None and q.prev_close > 0
                 and q.last is not None and q.last > q.prev_close)
        down = sum(1 for q in mq if q.prev_close is not None and q.prev_close > 0
                   and q.last is not None and q.last < q.prev_close)
        flat = len(mq) - up - down
        # 该市场主源延迟
        latency = None
        for pc in ctx.bundle.providers:
            if pc.primary and key in pc.markets:
                h = healths.get(pc.name)
                if h:
                    latency = round(h.latency_p50, 1)
        mc = _market_cfg(ctx, mk)
        age = S.market_observed_age_ms(mq)
        # 新鲜度：结合「行情时段」+「真实年龄」（收盘后 EOD 绝不 LIVE）
        out[key] = {
            "enabled": True,
            "session": session,
            "count": len(mq),
            "up": up, "down": down, "flat": flat,
            "latency_p50_ms": latency,
            "data_status": S.market_data_status(session, age, mc),
            "observed_age_ms": age,
            "index": idx,
        }
    # 顶层 observed_age_ms：全市场真实年龄最大值（不伪造 0）
    out["observed_age_ms"] = S.market_observed_age_ms(list(quotes.values()))
    return out


def _session(ctx: AppContext, market: T.Market) -> str:
    from ..core.clock import session_of
    return session_of(ctx.bundle, market)


def get_provider_health(ctx: AppContext) -> dict:
    healths = ctx.router.health_list()
    return {
        "providers": [S.serialize_health(h) for h in healths],
        "data_status": "LIVE", "observed_age_ms": 0,
    }


def get_config(ctx: AppContext) -> dict:
    s = ctx.bundle.strategies
    r = ctx.bundle.risk
    return {
        "strategies": {
            "s1": {"enabled": s.s1.enabled, "min_opportunity": s.s1.min_opportunity,
                   "min_confidence": s.s1.min_confidence},
            "s2": {"enabled": s.s2.enabled, "min_opportunity": s.s2.min_opportunity,
                   "min_confidence": s.s2.min_confidence},
            "s3": {"enabled": s.s3.enabled},
        },
        "risk": {
            "min_r_multiple": r.min_r_multiple,
            "overextension_max_gain_from_low_pct": r.overextension_max_gain_from_low_pct,
            "max_heat_pct": r.max_heat_pct,
            "regime_blocked_states": r.regime_blocked_states,
            "dq_block_if_stale": r.dq_block_if_stale,
            "dq_min_score_to_strong": r.dq_min_score_to_strong,
        },
        "data_status": "LIVE", "observed_age_ms": 0,
    }


def get_sectors(ctx: AppContext) -> dict:
    sectors = ctx.store.get_sectors()
    return {
        "sectors": [S.serialize_sector(s) for s in sectors.values()],
        "count": len(sectors),
        "data_status": "LIVE", "observed_age_ms": 0,
    }


# --------------------------------------------------------------------------- #
# 写操作（POST，轻量）：自选管理 + S3 事件注入（#17.5 仅注入，不接实时北向）
# --------------------------------------------------------------------------- #
def post_watch_add(ctx: AppContext, symbol: str, market: str | None = None) -> dict:
    from ..core import types as T
    mk = T.market_from_symbol(symbol) if market is None else T.Market(market.upper())
    item = T.WatchlistItem(symbol=symbol, market=mk)
    ctx.store.add_watch(item)
    ctx.repo.save_watchlist(list(ctx.store.get_watchlist().values()))
    return {"ok": True, "symbol": symbol}


def post_watch_remove(ctx: AppContext, symbol: str) -> dict:
    ctx.store.remove_watch(symbol)
    ctx.repo.save_watchlist(list(ctx.store.get_watchlist().values()))
    return {"ok": True, "symbol": symbol}


def post_event_inject(ctx: AppContext, payload: dict) -> dict:
    """注入 S3 占位/盘后事件（仅作弱因子，绝不进入 TRIGGERED/ACTIVE 决策）。"""
    ctx.repo.save_event(payload)
    return {"ok": True, "event": payload.get("event_type")}
