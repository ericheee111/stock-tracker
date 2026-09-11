"""WB1 batch 1: exact type / null / bool / illegal-enum boundary tests.

Frozen source of expectations: AGENTS.md §8 (strict type checking, fail
closed) and ``source_snapshot_contracts.py`` primitives (`_require_int`
:158-169, `_require_sha256` :151-155, `_require_text` :135-148,
`_require_utc` :172-177, record ``__post_init__`` :1945-2010).

Every rejection has a positive control (``valid_record()`` succeeds). No
production code or existing assertion is modified.

Run (clone root):
    py -3.14 -m unittest qa.wb1.test_wb1_type_boundary -v
"""

from __future__ import annotations

import unittest
from datetime import timezone

from qa.wb1._helpers import valid_record
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventSourceContractError,
)


class TestTransportRecordExactType(unittest.TestCase):
    def assert_rejected(self, *, field: str, value, pattern: str):
        with self.subTest(field=field, value=repr(value)):  # noqa: SIM117 - keep fixture identity separate from rejection assertion
            with self.assertRaisesRegex(
                MarketEventSourceContractError, pattern
            ):
                valid_record(**{field: value})

    def test_positive_control_minimal_valid_record(self):
        record = valid_record()
        self.assertEqual(record.kind.value, "CONNECTED")
        # hash/record_id are derived and stable across construction
        self.assertEqual(record.transport_append_order, 1)
        self.assertEqual(record.previous_transport_record_hash, "0" * 64)

    # --- M2: exact int (bool / float / str rejected), range ---
    def test_transport_append_order_rejects_bool_float_str_and_zero(self):
        for value in (True, 1.0, "1", 0):
            self.assert_rejected(
                field="transport_append_order", value=value, pattern="integer"
            )

    def test_connection_epoch_rejects_str_and_none(self):
        for value in ("1", None):
            self.assert_rejected(
                field="connection_epoch", value=value, pattern="integer"
            )

    def test_queue_and_dropped_counters_reject_bool(self):
        for field in ("queue_overflow_count", "dropped_callback_count"):
            self.assert_rejected(field=field, value=True, pattern="integer")

    def test_reconnect_epoch_lower_bound_zero_accepted_negative_rejected(self):
        self.assertEqual(valid_record(reconnect_epoch=0).reconnect_epoch, 0)
        self.assert_rejected(
            field="reconnect_epoch", value=-1, pattern="integer"
        )

    def test_callback_high_water_rejects_bool_and_str(self):
        for value in (False, "5"):
            self.assert_rejected(
                field="callback_high_water", value=value, pattern="integer"
            )

    # --- M3: sha256 fields ---
    def test_source_store_id_rejects_nonhash_uppercase_and_none(self):
        for value in ("not-a-hash", "A" * 64, None):
            self.assert_rejected(
                field="source_store_id", value=value, pattern="SHA-256|safe"
            )

    def test_previous_transport_record_hash_rejects_non_hex(self):
        self.assert_rejected(
            field="previous_transport_record_hash",
            value="g" * 64,
            pattern="SHA-256",
        )

    def test_transport_stream_id_rejects_none(self):
        self.assert_rejected(
            field="transport_stream_id", value=None, pattern="safe|SHA-256"
        )

    # --- M3: text safety ---
    def test_session_id_rejects_whitespace_empty_and_control(self):
        for value in (" wb1 ", "", "wb1\x00session"):
            self.assert_rejected(
                field="session_id", value=value, pattern="session_id"
            )

    # --- M4: exact enum kind ---
    def test_kind_rejects_string_and_none(self):
        for value in ("CONNECTED", None):
            self.assert_rejected(field="kind", value=value, pattern="kind")

    # --- M5: aware UTC datetime ---
    def test_observed_at_rejects_naive(self):
        from datetime import datetime

        naive = datetime(2026, 9, 2, 1, 30)  # noqa: DTZ001 - deliberately invalid naive-time fixture
        self.assert_rejected(
            field="observed_at", value=naive, pattern="timezone-aware"
        )

    def test_durable_known_at_rejects_naive(self):
        from datetime import datetime

        naive = datetime(2026, 9, 2, 1, 30)  # noqa: DTZ001 - deliberately invalid naive-time fixture
        self.assert_rejected(
            field="durable_known_at", value=naive, pattern="timezone-aware"
        )

    def test_observed_equal_durable_accepted_observed_after_durable_rejected(self):
        from datetime import datetime, timedelta

        base = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(valid_record(observed_at=base).observed_at, base)
        self.assert_rejected(
            field="observed_at",
            value=base + timedelta(seconds=1),
            pattern="exceeds durable",
        )

    # --- M6: canonical payload ---
    def test_canonical_payload_rejects_nonempty_json_and_non_str(self):
        for value in ('{"x":1}', 123):
            self.assert_rejected(
                field="canonical_payload", value=value, pattern="payload"
            )

    # --- M7: LIVE vs job fields ---
    def test_live_record_rejects_job_fields(self):
        self.assert_rejected(
            field="job_output_high_water", value=5, pattern="job"
        )

    def test_replay_record_requires_job_fields(self):
        from qa.wb1._helpers import MarketEventTransportKind

        self.assert_rejected(
            field="kind",
            value=MarketEventTransportKind.REPLAY_STARTED,
            pattern="job",
        )


if __name__ == "__main__":
    unittest.main()
