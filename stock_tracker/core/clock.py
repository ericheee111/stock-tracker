"""交易时段判定（按市场/时区，PRD #5.1 / #2）。

设计说明（零依赖简化）：
- 本机时钟即作为“现在”。PRD 要求按市场独立判断开休市，这里用各市场
  ``trading_hours``（当地交易所时间）与“本机本地时间”比较。
- 对于跨时区市场（如美股 ET），为避免过度工程化且不引入 zoneinfo/tzdata 依赖，
  采用 markets.toml 中的 ``utc_offset_hours`` 将“本机本地时间”换算为该市场当地时间的
  近似（假设本机处于东八区，这是 Phase1 个人/小团队工具的典型部署场景）。
  该近似仅用于开休市判定；行情新鲜度（observed_age）统一按本机本地解释（见 core/types.py）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .config import ConfigBundle, MarketConfig
from . import types as T


def machine_utc_offset_hours() -> int:
    """检测本机相对 UTC 的偏移（小时，东为正）。"""
    # time.timezone: 本机标准时区相对 UTC 的秒数（西为正）。
    return round(-time.timezone / 3600)


def _market_local_now(market_cfg: MarketConfig) -> datetime:
    """将本机“现在”换算为该市场当地近似时间。"""
    now = datetime.now()
    delta = timedelta(hours=(market_cfg.utc_offset_hours - machine_utc_offset_hours()))
    return now + delta


def _in_session(market_cfg: MarketConfig, dt: datetime) -> bool:
    """dt（市场当地）是否落在任一交易时段内（含周末判断）。"""
    if dt.weekday() >= 5:  # 周六/周日
        return False
    cur_min = dt.hour * 60 + dt.minute
    for sess in market_cfg.trading_hours:
        if len(sess) == 4:
            start = sess[0] * 60 + sess[1]
            end = sess[2] * 60 + sess[3]
            if start <= cur_min <= end:
                return True
    return False


def is_trading_now(bundle: ConfigBundle, market: T.Market) -> bool:
    """market 当前是否处于交易时段。"""
    mc = _market_cfg(bundle, market)
    return _in_session(mc, _market_local_now(mc))


def session_of(bundle: ConfigBundle, market: T.Market) -> str:
    """返回 'TRADING' / 'CLOSED' / 'WEEKEND'。"""
    mc = _market_cfg(bundle, market)
    now_local = _market_local_now(mc)
    if now_local.weekday() >= 5:
        return "WEEKEND"
    return "TRADING" if _in_session(mc, now_local) else "CLOSED"


def market_open_status(bundle: ConfigBundle) -> dict:
    """返回 {'a':.., 'hk':.., 'us':..} 的开市状态字符串。"""
    out = {}
    for key, mk in (("a", T.Market.A), ("hk", T.Market.HK), ("us", T.Market.US)):
        if bundle.app.markets_enabled.get(key, False):
            out[key] = session_of(bundle, mk)
        else:
            out[key] = "DISABLED"
    return out


def _market_cfg(bundle: ConfigBundle, market: T.Market) -> MarketConfig:
    return {"A": bundle.markets.a, "HK": bundle.markets.hk, "US": bundle.markets.us}[market.value]
