"""Frozen output comparisons are compatibility tests, not evidence of predictive power."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
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

    def test_frozen_source_identity_matches_exact_git_blob(self):
        if not (baseline.ROOT / '.git').exists():
            self.skipTest('exact source provenance requires a Git checkout')
        fixture = json.loads((baseline.ROOT/'tests/fixtures/numerical_legacy_v1.json').read_bytes())
        for key, path in (('indicator', 'stock_tracker/features/indicators.py'),
                          ('metric', 'stock_tracker/quant/evaluation/metrics.py')):
            raw = subprocess.run(['git', 'show', fixture['base_commit']+':'+path],
                                 cwd=baseline.ROOT, capture_output=True, check=True).stdout
            self.assertEqual(fixture[key+'_source_sha256'], hashlib.sha256(raw).hexdigest())

    def test_forged_base_or_source_identity_is_rejected(self):
        fixture = json.loads((baseline.ROOT/'tests/fixtures/numerical_legacy_v1.json').read_bytes())
        for key in ('base_commit', 'indicator_source_sha256', 'metric_source_sha256'):
            bad = copy.deepcopy(fixture)
            bad[key] = '0'*len(bad[key])
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'provenance'):
                baseline.validate_provenance(bad)

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
