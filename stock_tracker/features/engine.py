"""FeatureEngine（§7 / §3.6）：Quote+Bars → 证据族 → regime → sector → ScanContext。

- ``build_market_context``：COLD 周期调用，由全市场 Quote 推断 Regime 与 Sector 快照。
- ``build``：单标的只读上下文，供策略/评分消费。
"""

from __future__ import annotations

from ..core import types as T
from ..core.config import ConfigBundle
from . import regime as R
from . import sector as S


class FeatureEngine:
    """特征引擎。"""

    def __init__(self, bundle: ConfigBundle) -> None:
        self.bundle = bundle
        self.sector_engine = S.SectorEngine()

    def build_market_context(self, quotes: list[T.Quote], instruments: dict
                             ) -> tuple[T.MarketRegime, dict[str, T.SectorSnapshot]]:
        """由当前行情集合推断市场环境与板块快照。"""
        regime = R.build_regime(quotes)
        sectors = self.sector_engine.update(quotes, instruments)
        return regime, sectors

    def build(self, symbol: str, quote: T.Quote, bars: list[T.Bar],
              regime: T.MarketRegime | None, sector: T.SectorSnapshot | None,
              dq: T.DataQuality | None, cfg) -> T.ScanContext:
        """组装单标的只读扫描上下文。"""
        return T.ScanContext(
            symbol=symbol, market=quote.market, quote=quote, recent_bars=bars,
            regime=regime, sector=sector, dq=dq, cfg=cfg,
        )
