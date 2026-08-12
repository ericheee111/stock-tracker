"""策略基类（§7.5 / PRD #10）。

每个策略实现 ``evaluate(ctx) -> Optional[SignalCandidate]``。Phase1 实现 S1/S2/S3。
证据族在策略内按需在 Quote/Bars 上计算（特征引擎已去相关聚合，策略直接使用族分数）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..core import types as T
from ..core.config import StrategyConfig
from ..features import evidence as E


@dataclass(slots=True)
class SignalCandidate:
    """策略产出的候选（尚未经评分/闸门/状态机）。"""

    symbol: str = ""
    market: T.Market = T.Market.A
    strategy_id: str = ""
    proposed_state: T.SignalState = T.SignalState.WATCH
    entry_low: float = 0.0
    entry_high: float = 0.0
    trigger_price: float = 0.0
    invalidation_price: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    reward_risk: float = 0.0
    reason: str = ""
    next_trigger: str = ""
    half_life_hours: float = 48.0


class Strategy(ABC):
    """策略抽象基类。"""

    id: str = "base"

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def applies_to(self, market: T.Market) -> bool:
        """默认全市场适用；子类可覆盖。"""
        return True

    def _evidence(self, ctx: T.ScanContext) -> E.EvidenceSet:
        return E.compute_evidence(ctx.quote, ctx.recent_bars, ctx.regime, ctx.sector)

    def _rr(self, entry_high: float, invalidation: float, target: float) -> float:
        risk = entry_high - invalidation
        if risk <= 0:
            return 0.0
        return (target - entry_high) / risk

    @abstractmethod
    def evaluate(self, ctx: T.ScanContext) -> Optional[SignalCandidate]:
        raise NotImplementedError
