"""WB1 batch 2: market-time and source-clock boundary tests.

Frozen source of expectations:
- ``stock_tracker/core/market_time.py:17-33`` (aware datetime / exact Market /
  known policy; ValueError on violation).
- ``source_snapshot_contracts.py:1440-1464``: SOURCE_CLOCK_SKEW when
  ``source_time > received_at + 2s``, DURABILITY_DELAY when
  ``durable - received > 5min``. Both comparisons are strictly ``>``, so the
  equal boundary value is accepted; any overrun must be an impact-aware
  finding and, when the caller supplies no matching finding, the prefix scan
  fails closed with ``MarketEventSourceContractError``.

Run (clone root):
    py -3.14 -m unittest qa.wb1.test_wb1_clock_boundary -v
"""

from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone, tzinfo

from qa.wb1._helpers import MarketEventSourceSnapshotTestCase
from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventSourceContractError,
    MarketEventSourceRecord,
)


class _FakeTz(datetime):
    """A datetime subclass: market_session_date requires the exact type."""


class _NoOffsetTz(tzinfo):
    """tzinfo whose utcoffset returns None (an aware datetime without offset)."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NO-OFFSET"


class TestMarketTimeBoundary(unittest.TestCase):
    def test_naive_datetime_rejected(self):
        naive = datetime(2026, 9, 2, 1, 30)  # noqa: DTZ001 - deliberately invalid naive-time fixture
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            market_session_date(naive, Market.A, MARKET_SESSION_LABEL_POLICY_V1)

    def test_datetime_subclass_rejected(self):
        # type(timestamp) is not datetime -> rejected (market_time.py:22)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            market_session_date(
                _FakeTz(2026, 9, 2, 1, 30, tzinfo=timezone.utc),
                Market.A,
                MARKET_SESSION_LABEL_POLICY_V1,
            )

    def test_fake_tzinfo_without_offset_rejected(self):
        aware = datetime(2026, 9, 2, 1, 30, tzinfo=_NoOffsetTz())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            market_session_date(aware, Market.A, MARKET_SESSION_LABEL_POLICY_V1)

    def test_non_market_type_rejected(self):
        for value in ("A", Market.A.value, None):
            with self.subTest(value=repr(value)):  # noqa: SIM117 - keep fixture identity separate from rejection assertion
                with self.assertRaisesRegex(ValueError, "Market"):
                    market_session_date(
                        datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc),
                        value,
                        MARKET_SESSION_LABEL_POLICY_V1,
                    )

    def test_unknown_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, "policy"):
            market_session_date(
                datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc),
                Market.A,
                "stage4g1-market-session-label-v0",
            )

    def test_non_str_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, "policy"):
            market_session_date(
                datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc),
                Market.A,
                b"stage4g1-market-session-label-v1",
            )

    def test_positive_a_hk_us_conversion_control(self):
        # Re-affirm the frozen positive conversion (DST aware for US).
        self.assertEqual(
            market_session_date(
                datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc),
                Market.A,
                MARKET_SESSION_LABEL_POLICY_V1,
            ),
            date(2026, 9, 2),
        )
        self.assertEqual(
            market_session_date(
                datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc),
                Market.US,
                MARKET_SESSION_LABEL_POLICY_V1,
            ),
            date(2026, 7, 2),
        )


class TestSourceClockBoundary(MarketEventSourceSnapshotTestCase):
    def _clock_record(
        self,
        *,
        ahead: timedelta = timedelta(0),
        delay: timedelta = timedelta(seconds=2),
    ):
        first = self.records()[0]
        args = {
            f.name: getattr(first, f.name)
            for f in fields(first)
            if f.init
            and f.name
            not in {
                "payload_json",
                "payload_sha256",
                "record_storage_key",
                "record_file_sha256",
                "record_content_hash",
            }
        }
        args.update(
            received_at=first.source_time - ahead,
            durable_known_at=first.source_time + delay,
        )
        return MarketEventSourceRecord.create(
            **args, payload={"last_price": 11, "quantity": 100}
        )

    # Equal boundary value is accepted (strict ``>``): snapshot must succeed.
    def test_ahead_exactly_two_seconds_accepted(self):
        self.snapshot((self._clock_record(ahead=timedelta(seconds=2)),))

    def test_durability_exactly_five_minutes_accepted(self):
        self.snapshot(
            (self._clock_record(ahead=timedelta(0), delay=timedelta(minutes=5)),)
        )

    # Overrun fails closed (no matching supplied finding -> prefix scan error).
    def test_ahead_over_two_seconds_fails_closed(self):
        for ahead in (timedelta(seconds=2, microseconds=1), timedelta(seconds=3)):
            with self.subTest(ahead=ahead), self.assertRaisesRegex(
                MarketEventSourceContractError, "findings"
            ):
                self.snapshot((self._clock_record(ahead=ahead),))

    def test_durability_over_five_minutes_fails_closed(self):
        for delay in (timedelta(minutes=5, microseconds=1), timedelta(seconds=301)):
            with self.subTest(delay=delay), self.assertRaisesRegex(
                MarketEventSourceContractError, "findings"
            ):
                self.snapshot(
                    (self._clock_record(ahead=timedelta(0), delay=delay),)
                )


if __name__ == "__main__":
    unittest.main()
