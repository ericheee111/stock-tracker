"""Recipe metadata cannot silently outlive its actual legacy source."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from stock_tracker.features.evidence_comparison import LEGACY_RECIPE

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {'stock_tracker/features/evidence.py': '227c52b25f03b513be5504b475bf78bb769cdb080595d2a68dcdee62910c3deb', 'stock_tracker/signals/scoring.py': '955a675a32bc85a921f30e273574359a7b7f5304742e1cbfe82237f44c96e5e9'}

class TestEvidenceRecipeIdentity(unittest.TestCase):
    def test_exact_recipe_sources_are_unchanged(self):
        self.assertEqual(LEGACY_RECIPE, 'legacy-five-family-four-score-ab0fc66')
        for name, expected in SOURCES.items():
            with self.subTest(path=name):
                normalized = (ROOT/name).read_text(encoding='utf-8-sig').replace('\r\n','\n')
                self.assertEqual(hashlib.sha256(normalized.encode('utf-8')).hexdigest(), expected,
                                 'Legacy logic changed: version/review the comparison recipe, do not silently regenerate')
