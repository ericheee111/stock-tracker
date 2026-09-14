"""Frozen output comparisons are compatibility tests, not evidence of predictive power."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import run_numerical_baseline as baseline


class TestFrozenNumericalBaseline(unittest.TestCase):
    def test_all_published_normal_domain_vectors_unchanged(self):
        report = baseline.verify()
        self.assertTrue(report['passed'], report['mismatches'])
        self.assertEqual(report['indicator_vectors'], 44)
        self.assertEqual(report['metric_vectors'], 3)
        self.assertEqual(report['comparisons'], 414)
        self.assertTrue(report['synthetic_fixture_only'])
        self.assertFalse(report['investment_performance_claim'])

    def test_deliberate_formula_change_is_detected(self):
        with patch.object(baseline.I, 'sma', return_value=-1):
            report = baseline.verify()
        self.assertFalse(report['passed'])
        self.assertTrue(any(m['field'] == 'sma20' for m in report['mismatches']))

    def test_fixture_change_is_not_recalibrated_away(self):
        with patch.object(baseline, 'FIXTURE_SHA256', '0'*64), self.assertRaisesRegex(ValueError, 'Frozen baseline'):
            baseline.verify()


if __name__ == '__main__':
    unittest.main()
