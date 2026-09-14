"""Actual offline CLI exits and N4b availability/cohort boundaries."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from stock_tracker.features.horizon_cli import (
    MAX_INPUT_BYTES,
    evaluate_document,
    fixture_document,
    parse_document,
)
from stock_tracker.features.horizon_research import HorizonResearchError

ROOT = Path(__file__).resolve().parents[1]


class TestHorizonAvailability(unittest.TestCase):
    def test_calendar_and_bar_availability_after_cutoff_fail(self):
        for section in ('calendar', 'observations'):
            d = fixture_document('week')
            d[section][0]['usable_from'] = '2026-09-14T08:00:01+00:00'
            with self.subTest(section=section), self.assertRaisesRegex(HorizonResearchError, 'FUTURE_USABLE'):
                evaluate_document(d)

    def test_calendar_market_is_not_borrowed_from_another_market(self):
        d = fixture_document('week')
        for row in d['calendar']:
            row['market'] = 'HK'
        with self.assertRaisesRegex(HorizonResearchError, 'CALENDAR_MARKET_MISMATCH'):
            evaluate_document(d)

    def test_availability_cannot_precede_known_time(self):
        d = fixture_document('week')
        d['observations'][0]['usable_from'] = d['calendar'][0]['known_at']
        with self.assertRaisesRegex(HorizonResearchError, 'KNOWN_AFTER_USABLE_FROM'):
            evaluate_document(d)

    def test_aggregate_reports_calendar_availability_even_without_prices(self):
        d = fixture_document('week')
        d['observations'] = []
        for row in d['calendar']:
            row.update(is_open=False, close_at=None, known_at='2026-09-13T00:00:00+00:00', usable_from='2026-09-13T01:00:00+00:00')
        report = evaluate_document(d)
        self.assertEqual(report['weeks'][0]['latest_usable_from'], '2026-09-13T01:00:00+00:00')
        self.assertEqual(report['weeks'][0]['latest_known_at'], '2026-09-13T00:00:00+00:00')
        self.assertNotIn('close', report['weeks'][0])

    def test_source_market_cohort_and_feature_availability(self):
        for change in ({'market': 'US'}, {'purpose': 'SHORT_TERM'}, {'feature_usable_from': '2026-09-14T00:00:00+00:00'}):
            d = fixture_document('experiment')
            d['samples'][0].update(change)
            with self.subTest(change=change), self.assertRaises(HorizonResearchError):
                evaluate_document(d)

    def test_label_usable_not_just_known_time_controls_purge(self):
        d = fixture_document('experiment')
        sample_id = d['samples'][0]['sample_id']
        d['samples'][0]['label_usable_from'] = d['calibration_start']
        result = evaluate_document(d)
        self.assertNotIn(sample_id, result['partitions']['train'])
        self.assertIn(sample_id, [row['sample_id'] for row in result['purged']])
        self.assertFalse(result['ready_for_fixture_comparison'])
        self.assertEqual(result['included_episode_count'], 2)
        self.assertEqual(result['input_assurance'], 'DECLARED_NOT_AUTHORITY_VERIFIED')

    def test_cutoff_and_embargo_boundary_are_not_inclusive(self):
        d = fixture_document('experiment')
        boundary = datetime.fromisoformat(d['calibration_start']) - timedelta(microseconds=d['embargo_microseconds'])
        d['samples'][0]['label_usable_from'] = boundary.isoformat()
        self.assertFalse(evaluate_document(d)['partitions']['train'])
        d['samples'][0]['label_usable_from'] = (boundary-timedelta(microseconds=1)).isoformat()
        self.assertEqual(len(evaluate_document(d)['partitions']['train']), 1)

    def test_exact_fields_types_and_size_fail_closed(self):
        for bad in (b'', b'[]', b'{"schema":"x","schema":"y"}', b'{"x":NaN}', b' '*(MAX_INPUT_BYTES+1)):
            with self.subTest(size=len(bad)), self.assertRaises(HorizonResearchError):
                parse_document(bad)
        for mode in ('week', 'experiment'):
            d = fixture_document(mode)
            d['unexpected'] = 'not accepted'
            with self.assertRaises(HorizonResearchError):
                evaluate_document(d)
        d = fixture_document('week')
        d['observations'][0]['open'] = 10
        with self.assertRaises(HorizonResearchError):
            evaluate_document(d)
        d = fixture_document('experiment')
        d['embargo_microseconds'] = True
        with self.assertRaises(HorizonResearchError):
            evaluate_document(d)

    def test_pure_evaluation_is_deterministic_and_keeps_inputs(self):
        for kind in ('week', 'experiment'):
            d = fixture_document(kind)
            before = deepcopy(d)
            a = evaluate_document(d)
            self.assertEqual(a, evaluate_document(d))
            self.assertEqual(d, before)
            self.assertFalse(a['auto_trade'])
            self.assertFalse(a['research_grade'])


class TestOfflineHorizonCLI(unittest.TestCase):
    def invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'stock_tracker.features.horizon_cli', *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=15, check=False)

    def test_two_fixture_commands_have_real_results(self):
        for kind in ('week', 'experiment'):
            cp = self.invoke('--fixture', kind)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            d = json.loads(cp.stdout)
            self.assertEqual(d['input_mode'], 'SYNTHETIC_FIXTURE')
            self.assertFalse(d['model_fitted'])
            self.assertFalse(d['source_authority_verified'])
            if kind == 'week':
                self.assertEqual(d['result']['weeks'][0]['volume'], 510)
            else:
                self.assertEqual([len(v) for v in d['result']['partitions'].values()], [1, 1, 1])

    def test_empty_unknown_and_bad_json_commands_never_succeed(self):
        for args in ((), ('--fixture', 'misspelled'), ('--fixture', 'week', '--auto-trade')):
            self.assertNotEqual(self.invoke(*args).returncode, 0)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'input.json'
            source.write_text('{"schema":"a","schema":"b"}', encoding='utf-8')
            cp = self.invoke('--input', str(source))
            self.assertEqual(cp.returncode, 2)
            self.assertEqual(json.loads(cp.stderr)['code'], 'DUPLICATE_JSON_KEY')

    def test_new_output_only_and_input_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp)/'input.json', Path(temp)/'report.json'
            source.write_text(json.dumps(fixture_document('week')), encoding='utf-8')
            before = source.read_bytes()
            cp = self.invoke('--input', str(source), '--output', str(output))
            self.assertEqual(cp.returncode, 0, cp.stderr)
            report_bytes = output.read_bytes()
            self.assertEqual(json.loads(report_bytes)['input_mode'], 'DECLARED_JSON')
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(self.invoke('--fixture', 'week', '--output', str(output)).returncode, 2)
            self.assertEqual(output.read_bytes(), report_bytes)
            self.assertEqual(self.invoke('--fixture', 'week', '--output', str(Path(temp)/'bad.db')).returncode, 2)
            self.assertFalse((Path(temp)/'bad.db').exists())


if __name__ == '__main__':
    unittest.main()
