"""Regression tests for the independent N4 review's environment-error boundary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from stock_tracker.core.types import Market
from stock_tracker.features import horizon_research as H
from stock_tracker.features.horizon_cli import evaluate_document, fixture_document

ROOT = Path(__file__).resolve().parents[1]


class TestN4TimezoneFailureBoundary(unittest.TestCase):
    def test_calendar_missing_zone_is_domain_error_not_key_error(self):
        at = datetime(2026, 9, 7, 7, tzinfo=UTC)
        with (
            patch.object(H, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('synthetic missing tzdata')),
            self.assertRaisesRegex(H.HorizonResearchError, '^TIMEZONE_DATABASE_UNAVAILABLE$'),
        ):
            H.DeclaredCalendarDay(date(2026, 9, 7), True, at, at, 'a'*64, market=Market.A, usable_from=at)

    def test_daily_observation_missing_zone_is_domain_error(self):
        from decimal import Decimal
        at = datetime(2026, 9, 7, 7, tzinfo=UTC)
        with (
            patch.object(H, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('synthetic missing tzdata')),
            self.assertRaisesRegex(H.HorizonResearchError, '^TIMEZONE_DATABASE_UNAVAILABLE$'),
        ):
            H.DeclaredDailyObservation('600519.SH', Market.A, date(2026, 9, 7), at, at,
                                      Decimal(10), Decimal(12), Decimal(9), Decimal(11), 1, 'b'*64, 'c'*64,
                                      usable_from=at)

    def test_all_closed_calendar_still_requires_actual_timezone(self):
        document = fixture_document('week')
        for row in document['calendar']:
            row.update(is_open=False, close_at=None)
        document['observations'] = []
        with (
            patch.object(H, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('synthetic missing tzdata')),
            self.assertRaisesRegex(H.HorizonResearchError, '^TIMEZONE_DATABASE_UNAVAILABLE$'),
        ):
            evaluate_document(document)

    def test_real_cli_exit_json_and_absent_output_when_timezone_missing(self):
        script = (
            'import sys; from unittest.mock import patch; from zoneinfo import ZoneInfoNotFoundError; '
            'from stock_tracker.features import horizon_research as h; '
            'from stock_tracker.features.horizon_cli import main; '
            'p=patch.object(h,"ZoneInfo",side_effect=ZoneInfoNotFoundError("synthetic missing tzdata")); '
            'p.start(); raise SystemExit(main(["--fixture","week","--output",sys.argv[1]]))'
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'report.json'
            cp = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', script, str(output)],
                                cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                capture_output=True, text=True, encoding='utf-8', timeout=15, check=False)
            self.assertEqual(cp.returncode, 2, cp.stderr)
            self.assertEqual(cp.stdout, '')
            result = json.loads(cp.stderr)
            self.assertEqual(result['schema'], 'n4-offline-horizon-error-v1')
            self.assertEqual(result['error_type'], 'HorizonResearchError')
            self.assertEqual(result['code'], 'TIMEZONE_DATABASE_UNAVAILABLE')
            self.assertIs(result['model_fitted'], False)
            self.assertIs(result['auto_trade'], False)
            self.assertFalse(output.exists())
            self.assertNotIn('Traceback', cp.stderr)

    def test_experiment_without_market_timezone_still_evaluates(self):
        with patch.object(H, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('synthetic missing tzdata')):
            result = evaluate_document(fixture_document('experiment'))
        self.assertEqual([len(v) for v in result['partitions'].values()], [1, 1, 1])
        self.assertFalse(result['model_fitted'])
        self.assertFalse(result['research_grade'])

    def test_unexpected_programming_failure_is_not_hidden(self):
        with (
            patch.object(H, 'ZoneInfo', side_effect=RuntimeError('programming failure')),
            self.assertRaisesRegex(RuntimeError, 'programming failure'),
        ):
            evaluate_document(fixture_document('week'))


if __name__ == '__main__':
    unittest.main()
