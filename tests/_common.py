"""共享测试工具：确保项目根在 sys.path，提供构造器。

仅用于测试，不属于业务代码。
"""

import os
import sys

# 将项目根（tests 的父目录）加入 sys.path，保证 `import stock_tracker` 可用。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import datetime, timedelta  # noqa: E402

from stock_tracker.core import types as T  # noqa: E402


def now_ts(offset_sec: int = -10) -> datetime:
    """返回一个相对当前时间偏移的源行情时间戳（默认 10 秒前）。"""
    return datetime.now() + timedelta(seconds=offset_sec)


def make_quote(**over) -> T.Quote:
    """构造一个默认合法（VALID）的 Quote。"""
    q = T.Quote(
        symbol=over.get("symbol", "600519.SH"),
        market=over.get("market", T.Market.A),
        timestamp=over.get("timestamp", now_ts()),
        name=over.get("name", "测试"),
        open=over.get("open", 100.0),
        high=over.get("high", 110.0),
        low=over.get("low", 95.0),
        close=over.get("close", 105.0),
        last=over.get("last", 105.0),
        prev_close=over.get("prev_close", 100.0),
        volume=over.get("volume", 1_000_000),
        amount=over.get("amount", 1e9),
        turnover=over.get("turnover", 2.0),
        source=over.get("source", "tencent"),
        received_at=over.get("received_at", datetime.now()),
        computed_at=over.get("computed_at", datetime.now()),
        observed_age_ms=over.get("observed_age_ms", 10_000),
        quality=over.get("quality"),
        latency=over.get("latency", 50.0),
        data_status=over.get("data_status", T.DataStatus.UNKNOWN),
    )
    return q


def make_bars(n: int = 25, start: float = 100.0, step: float = 1.0,
              symbol: str = "600519.SH", market=T.Market.A) -> list[T.Bar]:
    """构造 n 根单调递增（上涨趋势）的 Bar，用于触发趋势/动量证据。"""
    bars: list[T.Bar] = []
    base = datetime.now()
    for i in range(n):
        c = start + i * step
        open_price = c - step * 0.5
        half_range = abs(step) * 0.5
        bars.append(T.Bar(
            symbol=symbol, market=market,
            timestamp=base - timedelta(days=n - i),
            interval="1d", open=open_price,
            high=max(open_price, c) + half_range,
            low=min(open_price, c) - half_range,
            close=c, volume=1_000_000, amount=1e9,
            turnover=2.0, source="tencent",
            adjustment_factor=1.0, quality_status=T.DataStatus.UNKNOWN,
        ))
    return bars


def make_regime(state: T.RegimeState = T.RegimeState.ROTATION,
                score: float = 55.0) -> T.MarketRegime:
    return T.MarketRegime(regime=state, market_score=score, sub_factors={})


def make_sector(stage: T.SectorStage = T.SectorStage.LEADING,
                score: float = 65.0, rs: float = 60.0,
                crowding: float = 0.0, sector: str = "白酒") -> T.SectorSnapshot:
    return T.SectorSnapshot(
        sector=sector, score=score, stage=stage, relative_strength=rs,
        breadth=60.0, volume=60.0, leader_quality=60.0, catalyst="",
        persistence=50.0, crowding=crowding,
    )


def make_ctx(quote: T.Quote = None, bars: list = None, regime=None,
             sector=None, dq=None) -> T.ScanContext:
    return T.ScanContext(
        symbol=(quote.symbol if quote else "600519.SH"),
        market=(quote.market if quote else T.Market.A),
        quote=quote if quote else make_quote(),
        recent_bars=bars if bars is not None else make_bars(),
        regime=regime, sector=sector, dq=dq,
        cfg=None,
    )
