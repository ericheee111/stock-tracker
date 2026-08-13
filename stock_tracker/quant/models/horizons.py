"""Market-specific prediction horizons from the PRD contract."""

from __future__ import annotations

from dataclasses import dataclass

from stock_tracker.core.types import Market


@dataclass(frozen=True, slots=True)
class HorizonPolicy:
    market: Market
    sessions: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.sessions or any(value <= 0 for value in self.sessions):
            raise ValueError("horizon sessions must be positive")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("horizon sessions must be unique and sorted")


DEFAULT_HORIZONS: dict[Market, HorizonPolicy] = {
    Market.A: HorizonPolicy(Market.A, (3, 5, 10, 20)),
    Market.HK: HorizonPolicy(Market.HK, (3, 5, 10, 20)),
    Market.US: HorizonPolicy(Market.US, (20, 40, 60, 120)),
}


def horizons_for(market: Market) -> tuple[int, ...]:
    return DEFAULT_HORIZONS[market].sessions
