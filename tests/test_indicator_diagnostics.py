"""Daily cache diagnostics: exact identities, sample windows, no decision authority."""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from stock_tracker.api.handlers import get_quote_detail
from stock_tracker.core.types import Bar, Market
from stock_tracker.features.indicator_diagnostics import daily_window_diagnostics
from tests import test_api_indicators as api_fixture
from tests import test_planning_api as http_fixture

NOW = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)


def rows(n=80, market=Market.A, symbol='600519.SH'):
    end = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
    return [Bar(symbol, market, end-timedelta(days=n-1-i), open=20+i*.1, high=21+i*.1,
                low=19+i*.1, close=20.5+i*.1, volume=100, source='synthetic-fixture') for i in range(n)]


def report(bars):
    return daily_window_diagnostics(bars, '600519.SH', Market.A, NOW)


class TestDailyIndicatorDiagnostics(unittest.TestCase):
    def test_mixed_large_integer_diagnostic_does_not_raise(self):
        data = rows(20)
        for index, value in enumerate([10**308, 10**308]+[1.0]*18):
            data[index] = replace(data[index], open=value, high=value, low=value, close=value)
        result = report(data)
        self.assertEqual(result['windows'][0]['state'], 'NUMERIC_UNAVAILABLE')
        self.assertIsNone(result['windows'][0]['mean_close'])
        json.dumps(result, allow_nan=False)

    def test_empty_is_unknown_not_zero(self):
        result = report([])
        self.assertEqual(result['status'], 'NO_DATA')
        self.assertEqual(result['windows'], [])
        self.assertFalse(result['execution_authorized'])

    def test_windows_use_samples_not_calendar_months(self):
        result = report(rows(80))
        self.assertEqual(result['sample_count'], 80)
        self.assertEqual([w['sample_window'] for w in result['windows']], [20, 60, 120, 252])
        self.assertEqual([w['state'] for w in result['windows']], ['NUMERIC_ONLY']*2+['INSUFFICIENT_SAMPLES']*2)
        self.assertIsNone(result['windows'][2]['mean_close'])
        self.assertIsNone(result['windows'][3]['change_percent'])
        self.assertFalse(result['calendar_coverage_verified'])
        self.assertFalse(result['investment_performance_claim'])

    def test_roc_requires_one_more_than_average(self):
        result = report(rows(60))
        self.assertIsNotNone(result['windows'][1]['mean_close'])
        self.assertIsNone(result['windows'][1]['change_percent'])
        self.assertIsNotNone(report(rows(61))['windows'][1]['change_percent'])

    def test_current_day_is_excluded_without_assuming_close(self):
        data = rows(60)
        data.append(replace(data[-1], timestamp=NOW, close=99, high=100))
        result = report(data)
        self.assertEqual(result['sample_count'], 60)
        self.assertEqual(result['same_day_excluded'], 1)
        self.assertEqual(result['last_date'], '2026-09-13')

    def test_future_date_blocks_not_relabels(self):
        data = rows(60)
        data[-1] = replace(data[-1], timestamp=NOW+timedelta(days=1))
        result = report(data)
        self.assertIn('FUTURE_BAR_DATE', result['issues'])
        self.assertEqual(result['windows'], [])

    def test_duplicates_and_unsorted_rows_fail_closed(self):
        for data in (rows(60)[::-1], rows(60)+[rows(60)[-1]]):
            with self.subTest():
                self.assertIn('DUPLICATE_OR_UNORDERED_DAILY_DATE', report(data)['issues'])

    def test_other_market_symbol_interval_never_mix(self):
        for change in ({'symbol': '000001.SZ'}, {'market': Market.US}, {'interval': '1m'}):
            data = rows(60); data[0] = replace(data[0], **change)
            with self.subTest(change=change):
                self.assertEqual(report(data)['status'], 'INVALID_INPUT')
                self.assertEqual(report(data)['windows'], [])

    def test_bad_numeric_fields_are_not_dropped_to_fill_window(self):
        for change in ({'close': True}, {'volume': True}, {'close': float('inf')},
                       {'low': 100}, {'adjustment_factor': float('nan')}):
            data = rows(60); data[0] = replace(data[0], **change)
            with self.subTest(change=change):
                self.assertEqual(report(data)['status'], 'INVALID_INPUT')

    def test_mixed_sources_and_time_basis_block(self):
        for change in ({'source': 'another-source'}, {'timestamp': rows(60)[0].timestamp.replace(tzinfo=None)}):
            data = rows(60); data[0] = replace(data[0], **change)
            self.assertEqual(report(data)['status'], 'INVALID_INPUT')

    def test_legacy_naive_date_is_explicit_not_pit(self):
        data = [replace(b, timestamp=b.timestamp.replace(tzinfo=None)) for b in rows(60)]
        result = report(data)
        self.assertEqual(result['time_basis'], 'LEGACY_NAIVE_DATE')
        self.assertIn('NAIVE_DATE_NOT_AN_OBSERVED_INSTANT', result['warnings'])
        self.assertEqual(result['assurance'], 'RUNTIME_DIAGNOSTIC_ONLY')

    def test_limit_is_fail_closed(self):
        self.assertEqual(report(rows(261))['status'], 'INVALID_INPUT')
        self.assertEqual(report(tuple(rows(5)))['status'], 'INVALID_INPUT')

    def test_typed_clock_required(self):
        with self.assertRaises(ValueError):
            daily_window_diagnostics(rows(), '600519.SH', Market.A, NOW.replace(tzinfo=None))

    def test_input_unchanged_json_finite(self):
        data = rows(260); before = copy.deepcopy(data)
        result = report(data)
        json.dumps(result, allow_nan=False)
        self.assertEqual(data, before)
        self.assertEqual(result['age_calendar_days'], 1)

    def test_warmup_method_names_do_not_claim_wilder(self):
        methods = {m['key']: m for m in report(rows(14))['methods']}
        self.assertEqual(methods['rsi14']['status'], 'WARMUP_REQUIRED')
        self.assertEqual(methods['macd_hist']['required_samples'], 34)
        self.assertIn('NOT_WILDER', methods['atr14']['method_id'])

    def test_quote_detail_reads_one_bounded_local_snapshot(self):
        ctx = api_fixture.TestApiIndicators()._ctx()
        ctx.repo.load_recent_bars = Mock(return_value=rows(260))
        ctx.router = Mock()
        result = get_quote_detail(ctx, '600519.SH')
        ctx.repo.load_recent_bars.assert_called_once_with('600519.SH', '1d', n=260)
        self.assertEqual(result['bar_count'], 80)  # Legacy detail window remains unchanged.
        self.assertEqual(len(result['recent_bars']), 30)
        self.assertIn('indicator_diagnostics', result)
        self.assertEqual(ctx.router.mock_calls, [])


class TestDailyDiagnosticHTTP(unittest.TestCase):
    def setUp(self):
        self.api = http_fixture.TestPlanningAPI(methodName='test_get_no_write_no_provider_and_unclassified')
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        self.api.ctx.bundle = api_fixture._bundle()

    def test_actual_http_reads_local_daily_cache_without_provider(self):
        data = rows(260)
        self.api.repo.save_bars_batch(data)
        before = self.api.repo.load_recent_bars('600519.SH', '1d', n=260)
        status, response = self.api.request('GET', '/api/quote/600519.SH')
        self.assertEqual(status, 200)
        diagnostic = response['indicator_diagnostics']
        self.assertEqual(diagnostic['sample_count'], 260)
        self.assertEqual(diagnostic['status'], 'NUMERIC_ONLY')
        self.assertEqual(diagnostic['last_date'], '2026-09-13')
        self.assertEqual(response['bar_count'], 80)
        self.assertEqual(len(response['recent_bars']), 30)
        self.assertFalse(diagnostic['execution_authorized'])
        self.assertEqual(self.api.repo.load_recent_bars('600519.SH', '1d', n=260), before)

    def test_actual_http_empty_cache_has_no_fabricated_windows(self):
        status, response = self.api.request('GET', '/api/quote/600519.SH')
        self.assertEqual(status, 200)
        self.assertEqual(response['indicator_diagnostics']['status'], 'NO_DATA')
        self.assertEqual(response['indicator_diagnostics']['windows'], [])


if __name__ == '__main__':
    unittest.main()
