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
from ..storage.repository import to_jsonable


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
    """Signal → dict，强制附加 data_status + observed_age_ms。"""
    d = to_jsonable(sig)
    d["data_status"] = sig.data_status.value if sig.data_status else T.DataStatus.UNKNOWN.value
    # 信号本身无观察年龄概念，置 0 以满足契约（前端据此区分行情/信号）。
    d["observed_age_ms"] = 0
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
