"""Independent continuation regressions over the captured unfinished drafts."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from stock_tracker.core.types import Market
from stock_tracker.features.horizon_research import (
    HorizonResearchError,
    closed_week_facts,
)
from tests.test_horizon_research import NOW, week


class TestWeeklyInputSemantics(unittest.TestCase):
    def test_calendar_revision_known_time_is_in_aggregate_dependency_time(self):
        days, bars, symbol = week()
        late_calendar = NOW - timedelta(hours=1)
        days = tuple(replace(d, known_at=late_calendar, usable_from=late_calendar) for d in days)
        report = closed_week_facts(days, bars, symbol=symbol, market=Market.A, as_of=NOW)
        self.assertEqual(report['weeks'][0]['latest_known_at'], late_calendar.isoformat())

    def test_empty_week_input_identity_binds_the_requested_security(self):
        days, _, symbol = week()
        days = tuple(replace(d, is_open=False, close_at=None) for d in days)
        a = closed_week_facts(days, (), symbol=symbol, market=Market.A, as_of=NOW)
        b = closed_week_facts(days, (), symbol='000001.SZ', market=Market.A, as_of=NOW)
        self.assertNotEqual(a['input_id'], b['input_id'])

    def test_noncanonical_security_cannot_become_a_weekly_input(self):
        _, bars, _ = week()
        for symbol in (' AAPL.US', 'BAD\n.US', '.US', 'lower.US', 'C:/BAD.US'):
            with self.subTest(symbol=repr(symbol)), self.assertRaises(HorizonResearchError):
                replace(bars[0], symbol=symbol, market=Market.US)

    def test_complete_week_is_not_available_before_the_week_boundary(self):
        days, bars, symbol = week()
        result = closed_week_facts(days, bars, symbol=symbol, market=Market.A, as_of=NOW)
        row = result['weeks'][0]
        self.assertGreaterEqual(datetime.fromisoformat(row['available_at']), datetime.fromisoformat(row['week_end_exclusive']))
        self.assertGreaterEqual(datetime.fromisoformat(row['available_at']), datetime.fromisoformat(row['latest_usable_from']))


if __name__ == '__main__':
    unittest.main()
