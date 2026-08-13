"""REST 端点实现（§9.1）。

所有函数接收 ``AppContext``，返回可 JSON 序列化的 dict。行情/信号类响应通过
``serializers`` 强制附加 ``data_status`` 与 ``observed_age_ms``。本模块只读
MarketStore + Repository，绝不调用上游 Provider。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..core import types as T
from ..core.config import ConfigBundle
from ..core.store import MarketStore
from ..features import feature_snapshot as FS
from ..storage.repository import Repository, to_jsonable
from . import serializers as S
from .sse import SSEHub
from ..signals.crowding import crowding_for


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


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _best_signal_for(ctx: AppContext, symbol: str) -> Optional[dict]:
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


def get_quote_detail(ctx: AppContext, symbol: str) -> Optional[dict]:
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


def _build_index(q: Optional[T.Quote], ctx: AppContext, mk: T.Market) -> dict:
    """构造单一代表性指数行情摘要；无有效行情时 last/change 为 null（前端渲染「—」）。"""
    last = q.last if (q is not None and q.last is not None and q.last > 0) else None
    prev = q.prev_close if (q is not None and q.prev_close is not None) else None
    change: Optional[float] = None
    change_pct: Optional[float] = None
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


def get_signal(ctx: AppContext, signal_id: str) -> Optional[dict]:
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
def post_watch_add(ctx: AppContext, symbol: str, market: Optional[str] = None) -> dict:
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
