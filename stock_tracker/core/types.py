"""Stock Tracker · 数据契约（dataclass + 枚举）。

落地架构文档 §3：所有跨模块传递的数据结构定义于此。
实现约束：
- 使用 ``@dataclass(slots=True)`` 减少内存占用并防止拼写错误。
- 枚举使用 ``enum.StrEnum``（Python 3.11+），序列化即为字符串。
- ``symbol`` 规范码为 ``CODE.MK``，``MK ∈ {SH, SZ, HK, US}``。
- 时间戳原则（PRD #5.4）：``computed_at >= quote.timestamp``；禁用未来 Bar。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Optional

from datetime import datetime


# --------------------------------------------------------------------------- #
# 指数标的注册表（供 provider 符号映射区分指数 / 个股）
# 腾讯对港股/美股指数使用 `r_` 前缀（如 r_hkHSI / r_usIXIC），对 A 股指数无前缀。
# 该集合在应用启动时由配置（markets.toml [index]）注册，供 to_provider_symbol 使用。
# --------------------------------------------------------------------------- #
_INDEX_SYMBOLS: set[str] = set()


def register_index_symbols(symbols: Iterable[str]) -> None:
    """注册指数标的（如 000001.SH / HSI.HK / IXIC.US），用于 provider 符号映射。"""
    _INDEX_SYMBOLS.clear()
    _INDEX_SYMBOLS.update(symbols)


def is_index_symbol(symbol: str) -> bool:
    """symbol 是否为已注册的指数标的（影响 tencent 查询符号的 r_ 前缀）。"""
    return symbol in _INDEX_SYMBOLS


# --------------------------------------------------------------------------- #
# 枚举（§3.1）
# --------------------------------------------------------------------------- #
class Market(StrEnum):
    """市场。"""

    A = "A"
    HK = "HK"
    US = "US"


class DataStatus(StrEnum):
    """行情新鲜度状态（PRD #26.10 / §3.1）。"""

    LIVE = "LIVE"
    DELAYED = "DELAYED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class QualityStatus(StrEnum):
    """数据质量状态（§3.4 / §6）。"""

    VALID = "VALID"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INVALID = "INVALID"


class SignalState(StrEnum):
    """信号状态机 12 态（§7.4 / PRD #15）。"""

    COLD = "COLD"
    WATCH = "WATCH"
    ARMED_BREAKOUT = "ARMED_BREAKOUT"
    ARMED_PULLBACK = "ARMED_PULLBACK"
    TRIGGERED = "TRIGGERED"
    ACTIVE = "ACTIVE"
    TRIM = "TRIM"
    EXIT = "EXIT"
    OVEREXTENDED = "OVEREXTENDED"
    INVALIDATED = "INVALIDATED"
    DATA_INVALID = "DATA_INVALID"
    EXPIRED = "EXPIRED"


class CircuitState(StrEnum):
    """Provider 熔断状态（§4.2）。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class RegimeState(StrEnum):
    """市场环境五态（§8 / PRD #6）。"""

    RISK_ON_TREND = "RISK_ON_TREND"
    ROTATION = "ROTATION"
    RISK_OFF = "RISK_OFF"
    PANIC_REBOUND = "PANIC_REBOUND"
    OVERHEATED = "OVERHEATED"


class SectorStage(StrEnum):
    """板块生命周期六态（§8 / PRD #7.3，观察/二启 映射见 features/sector.py）。"""

    EARLY = "EARLY"
    ACCUMULATION = "ACCUMULATION"
    LEADING = "LEADING"
    PEAK = "PEAK"
    DIVERGENCE = "DIVERGENCE"
    DECLINE = "DECLINE"


# --------------------------------------------------------------------------- #
# Quote / Bar（§3.2 / §3.3）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Quote:
    """实时行情归一化后唯一形态。"""

    symbol: str
    market: Market
    timestamp: datetime            # 源行情时间（交易所时间，Point-in-Time）
    name: str = ""                 # 名称（扩展字段，便于展示/落 instruments）
    # 价格字段允许 None：源返回 "--"/空/字段缺失时解析为 None（缺失），而非 0.0。
    # 0.0 会被数据质量闸门误判为「非法价格」，且前端会把缺失渲染成 "0.00"。
    open: Optional[float] = 0.0
    high: Optional[float] = 0.0
    low: Optional[float] = 0.0
    close: Optional[float] = 0.0
    last: Optional[float] = 0.0
    prev_close: Optional[float] = 0.0   # 昨收（振幅/涨跌幅参考）
    volume: int = 0                # 成交量（股）
    amount: float = 0.0            # 成交额（元/本币）
    turnover: float = 0.0          # 换手率（%）
    source: str = ""               # provider 名
    received_at: datetime = field(default_factory=datetime.now)
    computed_at: datetime = field(default_factory=datetime.now)
    displayed_at: datetime = field(default_factory=datetime.now)
    observed_age_ms: int = 0       # received_at - timestamp（毫秒）
    quality: "DataQuality" = None  # 类型前向引用在模块末尾补齐
    latency: float = 0.0           # 本次请求往返毫秒
    data_status: DataStatus = DataStatus.UNKNOWN


@dataclass(slots=True)
class Bar:
    """K 线（COLD/历史入库）。"""

    symbol: str
    market: Market
    timestamp: datetime
    interval: str = "1d"
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    turnover: float = 0.0
    source: str = ""
    adjustment_factor: float = 1.0
    quality_status: DataStatus = DataStatus.UNKNOWN


@dataclass(slots=True)
class DataQuality:
    """数据质量结论（§3.4 / §6）。"""

    status: QualityStatus = QualityStatus.VALID
    score: int = 100
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderHealth:
    """Provider 健康滚动统计（§3.4 / §4.2 / PRD #26.7）。"""

    provider: str = ""
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    error_rate: float = 0.0
    timeout_rate: float = 0.0
    stale_ratio: float = 0.0
    rate_limit_hits: int = 0
    last_success_at: Optional[datetime] = None
    cross_source_deviation: float = 0.0
    circuit_state: CircuitState = CircuitState.CLOSED


# --------------------------------------------------------------------------- #
# ScoreSet / Signal / ScanContext（§3.5 / §3.6）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ScoreSet:
    """四分数（§7.3 / PRD #11）。"""

    opportunity: int = 0           # 机会质量 0–100
    timing: int = 0                # 入场时机 0–100
    risk: int = 0                  # 风险 0–100（越高越危险）
    confidence: int = 0            # 置信度 0–100
    success_probability: Optional[float] = None  # Phase1 = None（PRD #11.5）
    positive_reasons: list[str] = field(default_factory=list)
    negative_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Signal:
    """信号当前态（§7.4 / PRD #15 / #16）。"""

    signal_id: str = ""
    symbol: str = ""
    market: Market = Market.A
    strategy_id: str = ""
    state: SignalState = SignalState.COLD
    state_changed_at: datetime = field(default_factory=datetime.now)
    previous_state: Optional[SignalState] = None
    reason: str = ""
    entry_low: float = 0.0
    entry_high: float = 0.0
    trigger_price: float = 0.0
    invalidation_price: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    reward_risk: float = 0.0
    freshness: float = 1.0         # 新鲜度 0–1（半衰期，PRD #15.2）
    market_regime: str = ""
    sector_stage: str = ""
    next_trigger: str = ""         # 下一触发条件（人话，PRD #24.2）
    what_changed: list[str] = field(default_factory=list)
    data_status: DataStatus = DataStatus.UNKNOWN
    scores: Optional[ScoreSet] = None


@dataclass(slots=True)
class ScanContext:
    """传递给策略/评分的只读上下文（§3.6）。"""

    symbol: str = ""
    market: Market = Market.A
    quote: Optional[Quote] = None
    recent_bars: list[Bar] = field(default_factory=list)
    regime: Optional["MarketRegime"] = None
    sector: Optional["SectorSnapshot"] = None
    watch: Any = None
    position: Any = None
    dq: Optional[DataQuality] = None
    cfg: Any = None                 # 配置快照（策略/风险阈值）


@dataclass(slots=True)
class WatchlistItem:
    """自选（§3.7）。"""

    symbol: str = ""
    market: Market = Market.A
    added_at: datetime = field(default_factory=datetime.now)
    note: Optional[str] = None


@dataclass(slots=True)
class Position:
    """持仓（§3.7 / PRD #24.8）。"""

    id: str = ""
    symbol: str = ""
    market: Market = Market.A
    shares: float = 0.0
    cost: float = 0.0
    added_at: datetime = field(default_factory=datetime.now)
    closed_at: Optional[datetime] = None


@dataclass(slots=True)
class MarketRegime:
    """市场环境快照（§8 / PRD #6）。"""

    regime: RegimeState = RegimeState.ROTATION
    market_score: float = 50.0     # 0–100
    sub_factors: dict = field(default_factory=dict)


@dataclass(slots=True)
class SectorSnapshot:
    """板块快照（§8 / PRD #7）。"""

    sector: str = ""
    score: float = 50.0
    stage: SectorStage = SectorStage.EARLY
    relative_strength: float = 0.0
    breadth: float = 0.0
    volume: float = 0.0
    leader_quality: float = 0.0
    catalyst: str = ""
    persistence: float = 0.0
    crowding: float = 0.0


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def market_from_symbol(symbol: str) -> Market:
    """由规范码推导市场。``CODE.MK`` → ``MK``。"""
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    mapping = {"SH": Market.A, "SZ": Market.A, "HK": Market.HK, "US": Market.US}
    return mapping.get(suffix, Market.A)


def to_provider_symbol(symbol: str, provider: str) -> str:
    """规范码 → provider 查询码（§3 符号规范）。

    - 腾讯：A 用 ``sh``/``sz`` 前缀；港股 ``hk``；美股 ``us``（大写代码）。
    - 东财：A 用 ``1.``/``0.`` secid；港股 ``116.``；美股 ``105.``/``100.``。
    - 新浪：同腾讯 A 前缀。
    """
    code, _, mk = symbol.partition(".")
    mk = mk.upper()
    if provider == "tencent":
        if mk in ("SH", "SZ"):
            return ("sh" if mk == "SH" else "sz") + code
        if mk == "HK":
            # 港股指数（如恒生 HSI）腾讯使用 r_ 前缀：r_hkHSI
            return ("r_hk" if is_index_symbol(symbol) else "hk") + code
        if mk == "US":
            # 美股指数（如纳指 IXIC）腾讯使用 r_ 前缀：r_usIXIC
            return ("r_us" if is_index_symbol(symbol) else "us") + code.upper()
    if provider == "sina":
        if mk in ("SH", "SZ"):
            return ("sh" if mk == "SH" else "sz") + code
        return symbol  # 新浪本环境仅作 A 备份
    if provider == "eastmoney":
        if mk == "SH":
            return "1." + code
        if mk == "SZ":
            return "0." + code
        if mk == "HK":
            return "116." + code
        if mk == "US":
            return "105." + code
    return symbol


def clamp(value: float, low: float, high: float) -> float:
    """裁剪到 [low, high]。"""
    return max(low, min(high, value))


def safe_div(num: float, den: float, default: float = 0.0) -> float:
    """安全除法，避免除零。"""
    if den == 0 or math.isnan(den) or math.isinf(den):
        return default
    return num / den
