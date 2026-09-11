"""WB1 batch 3: duplicate identity / cross-store / snapshot boundary tests.

Frozen source of expectations:
- Snapshot prefix/identity checks in ``MarketEventTransportSnapshot.__post_init__``
  (source_snapshot_contracts.py:2077-2157): exact tuple, contiguous append
  order, hash prefix, store/stream/known-time identity, watermark/counter
  monotonicity, duplicate start and branch-after-close rejection.
- Inventory duplicate identity in ``create_from_prefix`` (:1243-1262).

Run (clone root):
    py -3.14 -m unittest qa.wb1.test_wb1_identity_lifecycle -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from qa.wb1._helpers import (
    WB1_BASE_TIME,
    WB1_STORE_ID,
    MarketEventSourceSnapshotTestCase,
    valid_record,
)
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventSourceContractError,
    MarketEventTransportKind,
    MarketEventTransportSnapshot,
)


def _hash(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class TestTransportSnapshotBoundary(unittest.TestCase):
    def _snapshot(self, records):
        audited_at = max(r.durable_known_at for r in records)
        return MarketEventTransportSnapshot(
            WB1_STORE_ID, tuple(records), audited_at
        )

    def test_records_list_not_tuple_rejected(self):
        record = valid_record()
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "exact tuple"
        ):
            MarketEventTransportSnapshot(
                WB1_STORE_ID, [record], record.durable_known_at
            )

    def test_mixed_transport_stream_rejected(self):
        first = valid_record()
        second = valid_record(
            transport_append_order=2,
            previous_transport_record_hash=first.record_hash,
            kind=MarketEventTransportKind.HEARTBEAT,
            transport_stream_id=_hash("other-stream"),
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "identity mismatch"
        ):
            self._snapshot((first, second))

    def test_durable_known_at_regression_rejected(self):
        first = valid_record(
            durable_known_at=WB1_BASE_TIME + timedelta(seconds=10)
        )
        second = valid_record(
            transport_append_order=2,
            previous_transport_record_hash=first.record_hash,
            kind=MarketEventTransportKind.HEARTBEAT,
            durable_known_at=WB1_BASE_TIME,
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "identity mismatch"
        ):
            self._snapshot((first, second))

    def test_callback_watermark_regression_rejected(self):
        first = valid_record(callback_high_water=5)
        second = valid_record(
            transport_append_order=2,
            previous_transport_record_hash=first.record_hash,
            kind=MarketEventTransportKind.HEARTBEAT,
            callback_high_water=3,
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "watermark regression"
        ):
            self._snapshot((first, second))

    def test_duplicate_connect_same_epoch_rejected(self):
        first = valid_record()
        second = valid_record(
            transport_append_order=2,
            previous_transport_record_hash=first.record_hash,
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "duplicate start"
        ):
            self._snapshot((first, second))

    def test_empty_transport_cannot_prove_coverage(self):
        transport = MarketEventTransportSnapshot.from_records(
            source_store_id=WB1_STORE_ID,
            records=(),
            audited_at=WB1_BASE_TIME,
        )
        self.assertEqual(transport.high_water_append_order, 0)


class TestInventoryDuplicateIdentity(MarketEventSourceSnapshotTestCase):
    def test_duplicate_event_id_rejected(self):
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            event_id=_hash("dup-event"),
        )
        second = self.record(
            append_order=2,
            symbol="000001.SZ",
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
            event_id=_hash("dup-event"),
            minute_offset=1,
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "duplicate"
        ):
            self.snapshot((first, second))

    def test_duplicate_append_order_rejected(self):
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        second = self.record(
            append_order=1,
            symbol="000001.SZ",
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
            minute_offset=1,
        )
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "contiguous"
        ):
            self.snapshot((first, second))


if __name__ == "__main__":
    unittest.main()
