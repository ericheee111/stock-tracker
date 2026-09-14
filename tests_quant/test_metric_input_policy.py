"""Metric input, arithmetic and fixed published-baseline regression tests."""
from __future__ import annotations

import dataclasses
import json
import math
import unittest
from decimal import Decimal
from pathlib import Path

from stock_tracker.quant.evaluation import metrics as M


class HostileFloat(float):
    def __float__(self):
        raise AssertionError("coercion must not be reached")


class TestMetricInputPolicy(unittest.TestCase):
    def test_eight_published_metric_vectors(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/numerical_baselines_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(fixture["metric_cases"]), 8)
        for case in fixture["metric_cases"]:
            with self.subTest(function=case["function"]):
                result = getattr(M, case["function"])(*case["args"], **case["kwargs"])
                if dataclasses.is_dataclass(result):
                    result = dataclasses.asdict(result)
                self.assertEqual(result, case["expected"])

    def test_no_implicit_probability_or_label_coercion(self):
        for invalid in (True, False, "0.5", Decimal("0.5"), HostileFloat(.5), math.nan, math.inf, 10**400):
            for function in (M.probabilities, M.binary_labels):
                with self.subTest(function=function.__name__, invalid=type(invalid).__name__), self.assertRaises(M.MetricContractError):
                    function([invalid])

    def test_returns_costs_and_equity_are_exact_finite_numbers(self):
        for invalid in (True, "0.01", Decimal("0.01"), HostileFloat(.01), None, math.nan, math.inf, 10**400):
            for function in (M.profit_factor, M.max_drawdown):
                with self.subTest(function=function.__name__, invalid=type(invalid).__name__), self.assertRaises(M.MetricContractError):
                    function([invalid])
            with self.subTest(equity=type(invalid).__name__), self.assertRaises(M.MetricContractError):
                M.max_drawdown([.01], initial_equity=invalid)
            with self.subTest(cost=type(invalid).__name__), self.assertRaises(M.MetricContractError):
                M.top_k_net_expectancy([.1], [.9], 1, costs_r=[invalid])

    def test_negative_cost_cannot_inflate_net_return(self):
        with self.assertRaises(M.MetricContractError):
            M.top_k_net_expectancy([1.0], [.9], 1, costs_r=[-.1])
        self.assertEqual(M.top_k_net_expectancy([1.0], [.9], 1, costs_r=[0.0]), 1.0)

    def test_k_rejects_bool_float_text_and_out_of_range(self):
        for k in (True, False, 1.0, "1", 0, -1, 3):
            with self.subTest(k=k), self.assertRaises(M.MetricContractError):
                M.precision_at_k([0, 1], [.2, .8], k)
            with self.subTest(k=k), self.assertRaises(M.MetricContractError):
                M.top_k_net_expectancy([-.1, .1], [.2, .8], k)

    def test_bins_are_bounded_exact_integer(self):
        for bins in (True, False, 1.0, "10", 0, -1, 10001):
            with self.subTest(bins=bins), self.assertRaises(M.MetricContractError):
                M.calibration_curve([0, 1], [.2, .8], bins=bins)
        self.assertEqual(sum(b.count for b in M.calibration_curve([0, 1], [0, 1], bins=10000)), 2)

    def test_logloss_epsilon_must_be_representable(self):
        for epsilon in (True, "0.01", Decimal("0.01"), HostileFloat(.01), 0, .5, math.nan, 1e-300):
            with self.subTest(epsilon=type(epsilon).__name__), self.assertRaises(M.MetricContractError):
                M.log_loss([0, 1], [1, 0], epsilon=epsilon)
        self.assertTrue(math.isfinite(M.log_loss([0, 1], [1, 0])))

    def test_invalid_fractional_return_rejected_not_r_multiple(self):
        with self.assertRaises(M.MetricContractError):
            M.max_drawdown([.1, -1.01, .3])
        # R multiples can be below -1 in expectancy; they are not fractional equity returns.
        self.assertEqual(M.top_k_net_expectancy([-2.0], [.9], 1), -2.0)

    def test_full_loss_is_absorbing_and_empty_input_is_rejected(self):
        self.assertEqual(M.max_drawdown([-1.0, 100.0, -.5]), 1.0)
        with self.assertRaises(M.MetricContractError):
            M.max_drawdown([])
        self.assertAlmostEqual(M.max_drawdown([.1, -.2, .1], initial_equity=100), .2)

    def test_arithmetic_overflow_is_not_a_metric(self):
        for call in (
            lambda: M.profit_factor([1e308, 1e308, -1.0]),
            lambda: M.top_k_net_expectancy([-1e308], [.9], 1, costs_r=[1e308]),
            lambda: M.max_drawdown([1e308, 1e308]),
            lambda: M.max_drawdown([-.5], initial_equity=5e-324),
        ):
            with self.subTest(call=call), self.assertRaises(M.MetricContractError):
                call()

    def test_subnormal_equity_cannot_hide_loss(self):
        for equity in (5e-324, 1e-310):
            with self.subTest(initial_equity=equity), self.assertRaises(M.MetricContractError):
                M.max_drawdown([-.25], initial_equity=equity)
        with self.assertRaises(M.MetricContractError):
            M.max_drawdown([-.99], initial_equity=1e-307)
        for equity in (1.0, 100.0, 1e-200):
            with self.subTest(initial_equity=equity):
                self.assertAlmostEqual(M.max_drawdown([-.25], initial_equity=equity), .25)

    def test_nonzero_division_underflow_is_not_reported_as_zero(self):
        with self.assertRaises(M.MetricContractError):
            M.profit_factor([1e-200, -1e200])
        with self.assertRaises(M.MetricContractError):
            M.top_k_net_expectancy([5e-324, 0.0], [1, 0], 2)
        with self.assertRaises(M.MetricContractError):
            M.top_k_net_expectancy([-5e-324, 0.0], [1, 0], 2)
        self.assertEqual(M.top_k_net_expectancy([0.0, 0.0], [1, 0], 2), 0.0)
        self.assertEqual(M.profit_factor([0.0, -1.0]), 0.0)
        self.assertTrue(math.isinf(M.profit_factor([1.0])))

    def test_probability_error_underflow_is_not_perfect_calibration(self):
        with self.assertRaises(M.MetricContractError):
            M.brier_score([0], [1e-200])
        with self.assertRaises(M.MetricContractError):
            M.calibration_curve([0, 0], [5e-324, 0.0], bins=1)
        self.assertEqual(M.brier_score([0, 1], [0, 1]), 0.0)
        self.assertEqual(M.expected_calibration_error([0, 1], [0, 1]), 0.0)

    def test_profit_factor_no_loss_convention_is_explicit(self):
        self.assertEqual(M.profit_factor([1, 2, -1]), 3.0)
        self.assertTrue(math.isinf(M.profit_factor([1, 2])))
        self.assertEqual(M.profit_factor([0, 0]), 0.0)
        with self.assertRaises(M.MetricContractError):
            M.profit_factor([])

    def test_mismatched_arrays_and_costs_without_returns_rejected(self):
        for call in (
            lambda: M.brier_score([0, 1], [.1]),
            lambda: M.top_k_net_expectancy([1, 2], [.1, .2], 1, costs_r=[0]),
            lambda: M.probability_metrics([0, 1], [.1, .2], k=1, costs_r=[0, 0]),
        ):
            with self.subTest(call=call), self.assertRaises(M.MetricContractError):
                call()

    def test_tie_ranking_stable_and_generators_still_work(self):
        self.assertEqual(M.precision_at_k([0, 1], [.5, .5], 1), 0)
        self.assertEqual(M.probabilities(x for x in (0, .5, 1)), (0., .5, 1.))
        self.assertEqual(M.brier_score([0, 1], [0, 1]), 0)


if __name__ == "__main__":
    unittest.main()
