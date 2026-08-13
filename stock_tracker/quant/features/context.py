"""Point-in-time feature-computation context."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from stock_tracker.core.types import Bar, Market

from ..core.fingerprint import fingerprint
from ..core.time import to_utc


class FeatureContextError(ValueError):
    """Raised when features could observe data later than the decision time."""


@dataclass(frozen=True, slots=True)
class FeatureContext:
    symbol: str
    market: Market
    as_of: datetime
    bars: tuple[Bar, ...]
    data_snapshot_id: str
    calendar_snapshot_id: str
    universe_snapshot_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol or not self.bars:
            raise FeatureContextError("symbol and bars are required")
        cutoff = to_utc(self.as_of, "as_of")
        for bar in self.bars:
            if bar.symbol != self.symbol or bar.market is not self.market:
                raise FeatureContextError("bar identity differs from feature context")
            if to_utc(bar.timestamp) > cutoff:
                raise FeatureContextError("future bar entered feature context")
        times = [to_utc(bar.timestamp) for bar in self.bars]
        if times != sorted(times) or len(set(times)) != len(times):
            raise FeatureContextError("bars must be strictly chronological")
        for name, value in (
            ("data_snapshot_id", self.data_snapshot_id),
            ("calendar_snapshot_id", self.calendar_snapshot_id),
            ("universe_snapshot_id", self.universe_snapshot_id),
        ):
            if len(value) != 64:
                raise FeatureContextError(f"{name} must be SHA-256")

    @property
    def context_id(self) -> str:
        return fingerprint(
            {
                "schema": "feature-context-v1",
                "symbol": self.symbol,
                "market": self.market,
                "as_of": self.as_of,
                "bar_times": [bar.timestamp for bar in self.bars],
                "data_snapshot_id": self.data_snapshot_id,
                "calendar_snapshot_id": self.calendar_snapshot_id,
                "universe_snapshot_id": self.universe_snapshot_id,
                "metadata": self.metadata,
            }
        )
