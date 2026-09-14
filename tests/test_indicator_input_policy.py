"""Input safety and published-formula compatibility, not strategy performance."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path

from stock_tracker.features import indicators as I


class HostileFloat(float):
    def __float__(self):
        raise AssertionError("numeric subclass must not be coerced")


class HostileList(list):
    def __iter__(self):
        raise AssertionError("collection subclass must not execute")


class TestIndicatorInputPolicy(unittest.TestCase):
    def test_published_630_compatibility_vectors(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/numerical_baselines_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["source_commit"], "cdf8c1b4114252ead3bf5eb19d2f624be8929bd8")
        self.assertEqual(len(fixture["indicator_cases"]), 630)
        for case in fixture["indicator_cases"]:
            data = fixture["series"][case["series"]][:case["length"]]
            args = [data, *case["args"]]
            if case["function"] == "atr":
                args = [[v+1 for v in data], [v-1 for v in data], data, *case["args"]]
            with self.subTest(series=case["series"], length=case["length"], function=case["function"]):
                actual = getattr(I, case["function"])(*args)
                self.assertEqual(list(actual) if isinstance(actual, tuple) else actual, case["expected"])

    def test_fixture_source_hashes_match_published_git_blobs(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / ".git").exists():
            self.skipTest("source provenance requires a Git checkout")
        fixture = json.loads((root / "tests/fixtures/numerical_baselines_v1.json").read_text(encoding="utf-8"))
        for key, path in (("indicators", "stock_tracker/features/indicators.py"), ("metrics", "stock_tracker/quant/evaluation/metrics.py")):
            with self.subTest(source=key):
                raw = subprocess.run(["git", "cat-file", "blob", fixture["source_commit"] + ":" + path], cwd=root, check=True, capture_output=True).stdout
                self.assertEqual(hashlib.sha256(raw).hexdigest(), fixture["source_sha256"][key])

    def test_manual_reference_vectors(self):
        self.assertEqual(I.sma([1, 2, 3, 4], 3), 3)
        self.assertAlmostEqual(I.ema([1, 2, 3, 4, 5], 3), 4)
        self.assertEqual(I.rsi([1, 2, 1, 3], 3), 75)
        self.assertAlmostEqual(I.atr([10, 13, 12], [9, 11, 9], [9.5, 12, 10], 2), 3.25)
        self.assertEqual(I.roc([5, 6], 1), 20)
        self.assertEqual(I.stdev([1, 2, 3]), 1)
        self.assertAlmostEqual(I.stdev_pop([1, 2, 3]), math.sqrt(2/3))

    def test_legacy_warmup_and_flat_conventions_are_preserved(self):
        self.assertIsNone(I.sma([2, 4], 5))
        self.assertEqual(I.ema([2, 4], 5), 3)
        self.assertEqual(I.rsi([10.0]*20), 100)
        self.assertEqual(I.rsi(list(range(20, 0, -1))), 0)
        self.assertEqual(I.macd([10.0]*26), (0, None, None))
        self.assertEqual(I.macd([10.0]*34), (0, 0, 0))
        self.assertEqual(I.rolling_percentile([0, 10, 20, 30], 4, 50), 20)
        self.assertEqual(I.rolling_percentile([0, 10], 5, 50), 0)

    def test_bad_periods_do_not_raise_or_compute(self):
        data = [float(v+1) for v in range(70)]
        for value in (True, False, 0, -1, 2.5, "14", None, HostileFloat(14)):
            for name in ("sma", "ema", "rsi", "roc"):
                with self.subTest(name=name, period=repr(value)):
                    self.assertIsNone(getattr(I, name)(data, value))
            with self.subTest(name="atr", period=repr(value)):
                self.assertIsNone(I.atr(data, data, data, value))

    def test_bad_members_reject_entire_supplied_series(self):
        for bad in (True, "2", None, float("nan"), float("inf"), Decimal(2), HostileFloat(2), 10**400):
            data = [1.0]*70; data[0] = bad
            for name in ("sma", "ema", "rsi", "roc"):
                with self.subTest(name=name, bad=type(bad).__name__):
                    self.assertIsNone(getattr(I, name)(data, 14))
            self.assertEqual(I.macd(data), (None, None, None))
            self.assertIsNone(I.stdev(data))
            self.assertIsNone(I.stdev_pop(data))

    def test_unknown_container_is_not_executed(self):
        for value in (None, "12345", {1: 2}, iter([1, 2]), HostileList([1, 2])):
            with self.subTest(container=type(value).__name__):
                self.assertIsNone(I.ema(value, 2))
                self.assertEqual(I.macd(value), (None, None, None))

    def test_atr_alignment_and_ohlc_consistency(self):
        self.assertIsNone(I.atr([12.0]*20, [10.0]*19, [11.0]*20))
        self.assertIsNone(I.atr([9.0]*20, [10.0]*20, [9.5]*20))
        self.assertIsNone(I.atr([12.0]*20, [10.0]*20, [13.0]*20))

    def test_macd_and_percentile_parameters(self):
        data = [1.0]*70
        for args in ((0, 26, 9), (12, 26, True), (30, 26, 9), (12, 12, 9), (12, 26, -1)):
            with self.subTest(args=args):
                self.assertEqual(I.macd(data, *args), (None, None, None))
        for window, percentile in ((0, 50), (True, 50), (5, True), (5, -1), (5, 101), (5, float("nan"))):
            with self.subTest(window=window, percentile=percentile):
                self.assertIsNone(I.rolling_percentile(data, window, percentile))

    def test_overflow_is_unavailable_not_infinity(self):
        self.assertIsNone(I.sma([1e308]*70, 14))
        self.assertIsNone(I.ema([1e308]*70, 14))
        self.assertIsNone(I.stdev([1e308, -1e308]))
        self.assertIsNone(I.stdev_pop([1e308, -1e308]))
        self.assertIsNone(I.roc([1e-308, 1e308], 1))
        self.assertEqual(I.macd([1e308]*70), (None, None, None))

    def test_read_does_not_mutate_inputs_and_tuple_works(self):
        values = [10.0, 11.0, 12.0]
        before = values[:]
        self.assertEqual(I.sma(tuple(values), 2), 11.5)
        I.ema(values, 2)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
