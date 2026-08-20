"""ProviderRouter（§4.2）：主备选择、健康评分、熔断、退避、跨源偏差。

- ``select``：按市场过滤可用源、健康评分取主（错误率↓、延迟↓、陈旧↓、primary 优先）。
- ``fetch_quotes``：按市场分组取主源；失败交 tracker 统计（不在此重试风暴）。
- ``fetch_snapshot``：优先快照源（eastmoney）；失败回退到主报价源拉 ``universe``。
- ``cross_check``：跨源价格偏差 → health.cross_source_deviation。
"""

from __future__ import annotations

import random
from datetime import datetime

from ..core import types as T
from ..core.config import ConfigBundle
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
    def select(self, market: T.Market | None, op: str = "quote") -> MarketDataProvider | None:
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
            except Exception:  # noqa: BLE001 -- provider process boundary
                self.record_outcome(provider.name, False, provider.timeout * 1000.0, True)
        return results

    # ---- 快照（COLD） ----
    def fetch_snapshot(self, universe: list[str] | None = None) -> list[T.Quote]:
        qs: list[T.Quote] = []
        snap_provider = self.select(None, "snapshot")
        covered: set[T.Market] = set()
        if snap_provider is not None:
            try:
                snap = snap_provider.fetch_snapshot() or []
                if snap:
                    self.record_outcome(snap_provider.name, True, snap[0].latency, False)
                    qs.extend(snap)
                    # 记录快照源已覆盖的市场（东财快照仅含 A 股）
                    for q in snap:
                        covered.add(q.market)
                else:
                    self.record_outcome(snap_provider.name, False, snap_provider.timeout * 1000.0, False)
            except Exception:  # noqa: BLE001 -- provider process boundary
                self.record_outcome(snap_provider.name, False, snap_provider.timeout * 1000.0, True)
        # 快照源未覆盖的市场（典型：港/美指数）→ 主报价源按 universe 补齐，保证 COLD 全宇宙可达
        if universe:
            missing = [s for s in universe if T.market_from_symbol(s) not in covered]
            if missing:
                qs.extend(self.fetch_quotes(missing))
        return qs

    # ---- 历史 K 线（低频 BAR 调度用） ----
    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> list[T.Bar]:
        """按市场选 supports_bars() / bars_fallback 的 provider（主 eastmoney、兜底 tencent）。

        依次尝试按评分排序的候选源（主源优先，失败/熔断后自动兜底到 fallback 源），
        返回首个成功结果；全部失败则上抛最后一个异常，由调度层（``_run_bars``）吸收并跳过本轮。
        """
        candidates = self._select_bars(market, adjust)
        if not candidates:
            raise RuntimeError(f"无可用 K 线数据源（market={market.value}）")
        last_err: Exception | None = None
        saw_empty = False
        for provider in candidates:
            try:
                bars = provider.fetch_bars(symbol, market, interval, start, end, adjust)
                if bars:
                    self.record_outcome(
                        provider.name,
                        True,
                        provider.timeout * 1000.0,
                        False,
                    )
                    return bars
                # “接口可达但该标的无数据”不是全局 Provider 故障；继续尝试兜底源，
                # 也不要用一次 symbol-specific 空结果污染整个源的健康分。
                saw_empty = True
            except Exception as exc:  # noqa: BLE001  单个源失败不影响兜底源
                self.record_outcome(provider.name, False, provider.timeout * 1000.0, True)
                last_err = exc
        if saw_empty:
            return []
        # 全部候选源均异常：上抛最后一个异常。
        assert last_err is not None
        raise last_err

    def _select_bars(
        self,
        market: T.Market | None,
        adjust: str,
    ) -> list[MarketDataProvider]:
        """选择能诚实满足指定复权模式且市场匹配、健康可试的 K 线源。

        按评分降序返回候选列表（供 fetch_bars 依次尝试兜底）：
        - 真正的 K 线源（supports_bars=True）优先（+50）；
        - 仅作兜底的源（bars_fallback=True）次之（+0）；
        - 主源（primary）加权（+10）；
        - ``bars_priority`` 用于同类源的显式稳定排序。

        Provider 若不能满足请求的复权模式，必须被过滤，不能用未复权数据
        冒充 qfq/hfq。旧测试替身未实现 ``supports_adjustment`` 时按兼容的
        “全部支持”处理。
        """
        candidates = [
            p for p in self.providers
            if (p.supports_bars() or p.cfg.bars_fallback)
            and getattr(p, "supports_adjustment", lambda _adjust: True)(adjust)
            and (market is None or p.applies_to(market))
            and self.trackers[p.name].can_try()
        ]
        if not candidates:
            return []
        scored = []
        for p in candidates:
            h = self.trackers[p.name].to_provider_health()
            s = 0.0
            if p.supports_bars():
                s += 50.0
            if p.cfg.bars_fallback:
                s += 0.0
            if p.cfg.primary:
                s += 10.0
            s += float(p.cfg.bars_priority)
            s -= h.error_rate * 8.0
            s -= h.latency_p50 / 1000.0
            s -= h.stale_ratio * 6.0
            s -= h.rate_limit_hits * 0.2
            scored.append((s, p))
        scored.sort(key=lambda x: (x[0], x[1].name), reverse=True)
        return [p for _, p in scored]

    # ---- 跨源偏差（低频抽样） ----
    def _maybe_cross_check(self, primary: MarketDataProvider, quotes: list[T.Quote]) -> None:
        if not quotes or random.random() > self._cross_sample_prob:
            return
        sample = random.choice(quotes)
        if sample.last is None:
            return
        secondary = self.select(sample.market, "quote")
        if secondary is None or secondary.name == primary.name:
            return
        try:
            others = secondary.fetch_quotes([sample.symbol])
            if others and others[0].last is not None:
                dev = abs(others[0].last - sample.last) / max(sample.last, 1e-9)
                tr = self.trackers[primary.name]
                tr.cross_source_deviation = 0.7 * tr.cross_source_deviation + 0.3 * dev
        except Exception:  # noqa: BLE001, S110 -- optional cross-source sampling
            pass

    def cross_check(self, symbol: str, q_primary: T.Quote, q_secondary: T.Quote) -> float:
        """显式跨源偏差计算（供调度按需调用）。"""
        if q_primary.last is None or q_secondary.last is None:
            return 0.0
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
