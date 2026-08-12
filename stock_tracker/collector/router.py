"""ProviderRouter（§4.2）：主备选择、健康评分、熔断、退避、跨源偏差。

- ``select``：按市场过滤可用源、健康评分取主（错误率↓、延迟↓、陈旧↓、primary 优先）。
- ``fetch_quotes``：按市场分组取主源；失败交 tracker 统计（不在此重试风暴）。
- ``fetch_snapshot``：优先快照源（eastmoney）；失败回退到主报价源拉 ``universe``。
- ``cross_check``：跨源价格偏差 → health.cross_source_deviation。
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from ..core import types as T
from ..core.config import ConfigBundle, ProviderConfig
from ..data_quality.health import HealthTracker
from .provider import MarketDataProvider


class ProviderRouter:
    """多源路由与健康编排。"""

    def __init__(self, bundle: ConfigBundle, providers: list[MarketDataProvider]) -> None:
        self.bundle = bundle
        self.providers = providers
        self.trackers: dict[str, HealthTracker] = {
            p.name: HealthTracker(p.cfg) for p in providers
        }
        self._cross_sample_prob = 0.05  # 低频跨源抽样校验

    # ---- 选择 ----
    def select(self, market: Optional[T.Market], op: str = "quote") -> Optional[MarketDataProvider]:
        candidates = [
            p for p in self.providers
            if (market is None or p.applies_to(market))
            and (op != "snapshot" or p.supports_snapshot())
            and self.trackers[p.name].can_try()
        ]
        if not candidates:
            return None
        scored = []
        for p in candidates:
            h = self.trackers[p.name].to_provider_health()
            s = 0.0
            if p.cfg.primary:
                s += 10.0
            s -= h.error_rate * 8.0
            s -= h.latency_p50 / 1000.0
            s -= h.stale_ratio * 6.0
            s -= h.rate_limit_hits * 0.2
            scored.append((s, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    # ---- 批量报价 ----
    def fetch_quotes(self, symbols: list[str]) -> list[T.Quote]:
        by_market: dict[T.Market, list[str]] = {}
        for s in symbols:
            m = T.market_from_symbol(s)
            by_market.setdefault(m, []).append(s)

        results: list[T.Quote] = []
        for market, syms in by_market.items():
            provider = self.select(market, "quote")
            if provider is None:
                continue
            try:
                qs = provider.fetch_quotes(syms)
                if qs:
                    self.record_outcome(provider.name, True, qs[0].latency, False)
                    results.extend(qs)
                    self._maybe_cross_check(provider, qs)
                else:
                    self.record_outcome(provider.name, False, provider.timeout * 1000.0, False)
            except Exception:
                self.record_outcome(provider.name, False, provider.timeout * 1000.0, True)
        return results

    # ---- 快照（COLD） ----
    def fetch_snapshot(self, universe: Optional[list[str]] = None) -> list[T.Quote]:
        snap_provider = self.select(None, "snapshot")
        if snap_provider is not None:
            try:
                qs = snap_provider.fetch_snapshot()
                if qs:
                    self.record_outcome(snap_provider.name, True, qs[0].latency, False)
                    return qs
                self.record_outcome(snap_provider.name, False, snap_provider.timeout * 1000.0, False)
            except Exception:
                self.record_outcome(snap_provider.name, False, snap_provider.timeout * 1000.0, True)
        # 回退：主报价源拉 universe（保证降级态仍有 COLD 数据）
        if universe:
            return self.fetch_quotes(universe)
        return []

    # ---- 跨源偏差（低频抽样） ----
    def _maybe_cross_check(self, primary: MarketDataProvider, quotes: list[T.Quote]) -> None:
        if not quotes or random.random() > self._cross_sample_prob:
            return
        sample = random.choice(quotes)
        secondary = self.select(sample.market, "quote")
        if secondary is None or secondary.name == primary.name:
            return
        try:
            others = secondary.fetch_quotes([sample.symbol])
            if others:
                dev = abs(others[0].last - sample.last) / max(sample.last, 1e-9)
                tr = self.trackers[primary.name]
                tr.cross_source_deviation = 0.7 * tr.cross_source_deviation + 0.3 * dev
        except Exception:
            pass

    def cross_check(self, symbol: str, q_primary: T.Quote, q_secondary: T.Quote) -> float:
        """显式跨源偏差计算（供调度按需调用）。"""
        dev = abs(q_secondary.last - q_primary.last) / max(q_primary.last, 1e-9)
        tr = self.trackers.get(q_primary.source)
        if tr is not None:
            tr.cross_source_deviation = dev
        return dev

    # ---- 结果记录 ----
    def record_outcome(self, provider_name: str, ok: bool, latency_ms: float, is_timeout: bool) -> None:
        tr = self.trackers.get(provider_name)
        if tr is None:
            return
        if ok:
            tr.record_success(latency_ms)
        else:
            tr.record_failure(is_timeout)
        # 同步限频计数
        for p in self.providers:
            if p.name == provider_name:
                tr.rate_limit_hits = p._rl.hits
                break

    def health_list(self) -> list[T.ProviderHealth]:
        return [tr.to_provider_health() for tr in self.trackers.values()]
