"""Append-only local market-event storage and deterministic replay."""

from .contracts import (
    EventDisposition,
    GapKind,
    IngestionFinding,
    IngestionResult,
    MinuteBarRecord,
    MinuteCompleteness,
)
from .store import MarketEventStore, MarketEventStoreError

__all__ = [
    "EventDisposition",
    "GapKind",
    "IngestionFinding",
    "IngestionResult",
    "MarketEventStore",
    "MarketEventStoreError",
    "MinuteBarRecord",
    "MinuteCompleteness",
]
