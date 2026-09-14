"""Mixed source/clock representation without assigning a timezone to legacy sources."""
import unittest
from datetime import datetime, timedelta, timezone

from stock_tracker.api.serializers import recompute_age_ms
from stock_tracker.core import types as T


class TestQuoteAgeClockBasis(unittest.TestCase):
    def test_aware_source_with_system_local_clock(self):
        instant = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
        local_clock = instant.astimezone().replace(tzinfo=None)
        q = T.Quote("600000.SH", T.Market.A, instant-timedelta(seconds=10))
        self.assertEqual(recompute_age_ms(q, local_clock), 10000)
        self.assertEqual(q.timestamp, instant-timedelta(seconds=10))

    def test_aware_clock_and_source_offsets(self):
        clock = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
        source = (clock-timedelta(seconds=3)).astimezone(timezone(timedelta(hours=8)))
        q = T.Quote("600000.SH", T.Market.A, source)
        self.assertEqual(recompute_age_ms(q, clock), 3000)

    def test_legacy_naive_values_keep_local_label_semantics(self):
        clock = datetime(2026, 9, 14, 14)  # noqa: DTZ001 - intentional legacy naive clock
        q = T.Quote("600000.SH", T.Market.A, clock-timedelta(seconds=7))
        self.assertEqual(recompute_age_ms(q, clock), 7000)
        self.assertEqual(recompute_age_ms(q, clock.astimezone()), 7000)
        self.assertIsNone(q.timestamp.tzinfo)

    def test_received_at_fallback_uses_same_clock_alignment(self):
        clock = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
        q = T.Quote("600000.SH", T.Market.A, datetime(1970, 1, 1), received_at=clock-timedelta(seconds=5))  # noqa: DTZ001 - legacy missing-date sentinel
        self.assertEqual(recompute_age_ms(q, clock.astimezone().replace(tzinfo=None)), 5000)


if __name__ == "__main__":
    unittest.main()
