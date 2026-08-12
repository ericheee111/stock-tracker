"""FeatureEngine（§7 / §3.6）：Quote+Bars → 证据族 → regime → sector → ScanContext。

- ``build_market_context``：COLD 周期调用，由全市场 Quote 推断 Regime 与 Sector 快照。
- ``build``：单标的只读上下文，供策略/评分消费。
"""

from __future__ import annotations

from ..core import types as T
from ..core.config import ConfigBundle
from . import regime as R
from . import sector as S


def _price_usable(q: T.Quote) -> bool:
    """价格字段是否可用于市场环境/板块计算（无 None 且为正）。"""
    return all(p is not None and p > 0 for p in
               (q.last, q.prev_close, q.open, q.high, q.low))


class FeatureEngine:
    """特征引擎。"""

    def __init__(self, bundle: ConfigBundle) -> None:
        self.bundle = bundle
        self.sector_engine = S.SectorEngine()

    def build_market_context(self, quotes: list[T.Quote], instruments: dict
                             ) -> tuple[T.MarketRegime, dict[str, T.SectorSnapshot]]:
        """由当前行情集合推断市场环境与板块快照。

        过滤掉价格不可用（None/非正）的标的，避免特征层对 None 价格做除法/比较时崩溃；
        这些标的已被 DQ 闸门判为 INVALID，不应参与 Regime/Sector 计算。
        """
        usable = [q for q in quotes if _price_usable(q)]
        regime = R.build_regime(usable)
        sectors = self.sector_engine.update(usable, instruments)
        return regime, sectors

    def build(self, symbol: str, quote: T.Quote, bars: list[T.Bar],
              regime: T.MarketRegime | None, sector: T.SectorSnapshot | None,
              dq: T.DataQuality | None, cfg) -> T.ScanContext:
        """组装单标的只读扫描上下文。"""
        return T.ScanContext(
            symbol=symbol, market=quote.market, quote=quote, recent_bars=bars,
            regime=regime, sector=sector, dq=dq, cfg=cfg,
        )
