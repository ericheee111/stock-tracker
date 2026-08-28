"""Contracts for append-only market-event ingestion and replay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class EventDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    QUARANTINED = "QUARANTINED"


class GapKind(StrEnum):
    CALLBACK_SEQUENCE = "CALLBACK_SEQUENCE"
    PROVIDER_SEQUENCE = "PROVIDER_SEQUENCE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    SOURCE_TIME_REGRESSION = "SOURCE_TIME_REGRESSION"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"


class MinuteCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE_BASELINE = "INCOMPLETE_BASELINE"
    INCOMPLETE_GAP = "INCOMPLETE_GAP"
    INCOMPLETE_OUT_OF_ORDER = "INCOMPLETE_OUT_OF_ORDER"
    INCOMPLETE_SPARSE = "INCOMPLETE_SPARSE"


@dataclass(frozen=True, slots=True)
class IngestionFinding:
    kind: GapKind
    session_id: str
    symbol: str
    expected: int | None
    observed: int | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IngestionResult:
    event_id: str | None
    disposition: EventDisposition
    partition_key: str | None
    findings: tuple[IngestionFinding, ...]
    production_database_modified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "disposition": self.disposition.value,
            "partition_key": self.partition_key,
            "findings": [item.as_dict() for item in self.findings],
            "production_database_modified": self.production_database_modified,
        }


@dataclass(frozen=True, slots=True)
class MinuteBarRecord:
    symbol: str
    market: str
    minute_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    event_count: int
    first_callback_seq: int
    last_callback_seq: int
    completeness: MinuteCompleteness
    source: str = "xtp"
    data_status: str = "DELAYED"

    def __post_init__(self) -> None:
        if self.minute_start.tzinfo is None or self.minute_start.utcoffset() is None:
            raise ValueError("minute_start must be timezone-aware")
        values = (self.open, self.high, self.low, self.close, self.amount)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("minute bar prices and amount must be numeric")
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("minute bar numbers must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("minute bar prices must be positive")
        if self.low > min(self.open, self.close, self.high) or self.high < max(
            self.open, self.close, self.low
        ):
            raise ValueError("minute bar OHLC values are inconsistent")
        if type(self.volume) is not int or self.volume < 0:
            raise ValueError("minute bar volume must be non-negative")
        if type(self.event_count) is not int or self.event_count <= 0:
            raise ValueError("minute bar event_count must be positive")
        if self.data_status == "LIVE":
            raise ValueError("persisted minute bars must not be labelled LIVE")

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "minute_start": self.minute_start.astimezone(timezone.utc).isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "event_count": self.event_count,
            "first_callback_seq": self.first_callback_seq,
            "last_callback_seq": self.last_callback_seq,
            "completeness": self.completeness.value,
            "source": self.source,
            "data_status": self.data_status,
        }
