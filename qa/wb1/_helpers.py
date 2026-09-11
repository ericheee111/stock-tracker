"""Shared helpers for WB1 boundary tests (R4.1, 788ffdb).

Provides a minimal valid ``MarketEventTransportRecord`` factory and re-exports
the frozen test base/fixture from the existing R4.1 suite. No production code
is imported for modification; only for boundary probing.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventTransportKind,
    MarketEventTransportRecord,
)
from tests.test_market_source_snapshot_contracts import (
    MarketEventSourceSnapshotTestCase,
    transport_fixture,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


WB1_STORE_ID = _hash("wb1-store")
WB1_STREAM_ID = _hash("wb1-stream")
WB1_BASE_TIME = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)


def valid_record(**overrides) -> MarketEventTransportRecord:
    """Build a minimal, contract-valid LIVE CONNECTED transport record.

    All boundary tests mutate exactly one field via ``overrides`` and expect
    either ``MarketEventSourceContractError`` (rejection) or a valid instance
    (positive control).
    """
    kwargs: dict = {
        "source_store_id": WB1_STORE_ID,
        "transport_stream_id": WB1_STREAM_ID,
        "transport_append_order": 1,
        "previous_transport_record_hash": "0" * 64,
        "session_id": "wb1-session",
        "connection_epoch": 1,
        "reconnect_epoch": 0,
        "kind": MarketEventTransportKind.CONNECTED,
        "observed_at": WB1_BASE_TIME,
        "durable_known_at": WB1_BASE_TIME,
        "subscription_scope_id": None,
        "callback_high_water": None,
        "provider_high_water": None,
        "queue_overflow_count": 0,
        "dropped_callback_count": 0,
        "canonical_payload": "{}",
    }
    kwargs.update(overrides)
    return MarketEventTransportRecord(**kwargs)


__all__ = [
    "WB1_BASE_TIME",
    "WB1_STORE_ID",
    "WB1_STREAM_ID",
    "MarketEventSourceSnapshotTestCase",
    "MarketEventTransportKind",
    "MarketEventTransportRecord",
    "replace",
    "transport_fixture",
    "valid_record",
]
