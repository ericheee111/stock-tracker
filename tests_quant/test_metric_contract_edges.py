"""Adversarial metric domains; synthetic only, no investment-performance evidence."""
from __future__ import annotations

import math
import unittest

from stock_tracker.quant.evaluation import metrics as M


class FloatSubclass(float):
    pass


class TestMetricContractEdges(unittest.TestCase):
    def test_probabilities_reject_coercion_and_nonfinite(self):
        for value in (True, False, '0.5', None, float('nan'), float('inf'), 10**400, FloatSubclass(0.5)):
            with self.subTest(value=repr(value)), self.assertRaises(M.MetricContractError):
                M.probabilities([value])

    def test_metric_iterables_reject_text_and_unordered_containers(self):
        for values in ('01', b'01', {0, 1}, {0: 1}, 1, None):
            with self.subTest(values=repr(values)), self.assertRaises(M.MetricContractError):
                M.probabilities(values)

    def test_labels_reject_subclass_and_huge_integer(self):
        for value in (FloatSubclass(1), 10**400, True):
            with self.subTest(value=repr(value)), self.assertRaises(M.MetricContractError):
                M.binary_labels([value])

    def test_exact_positive_integer_controls(self):
        for k in (True, 1.0, '1', 0, -1, 3):
            with self.subTest(k=k), self.assertRaises(M.MetricContractError):
                M.precision_at_k([1, 0], [0.9, 0.1], k)
        for bins in (True, 1.0, '2', 0, -1, 10001):
            with self.subTest(bins=bins), self.assertRaises(M.MetricContractError):
                M.calibration_curve([1, 0], [0.9, 0.1], bins=bins)

    def test_epsilon_must_be_finite_explicit_and_representable(self):
        for epsilon in (True, '0.1', float('nan'), 0, 0.5, 1e-100):
            with self.subTest(epsilon=epsilon), self.assertRaises(M.MetricContractError):
                M.log_loss([1, 0], [0.0, 1.0], epsilon=epsilon)

    def test_negative_and_nonnumeric_cost_cannot_inflate_return(self):
        for cost in (-0.1, True, '0.1', None, float('nan'), float('inf'), 10**400):
            with self.subTest(cost=repr(cost)), self.assertRaises(M.MetricContractError):
                M.top_k_net_expectancy([1.0], [0.5], 1, costs_r=[cost])

    def test_valid_zero_cost_and_negative_return(self):
        self.assertEqual(M.top_k_net_expectancy([-2, 1], [0.9, 0.1], 1, costs_r=[0, 0]), -2)
        self.assertEqual(M.top_k_net_expectancy([1, 2], [0.9, 0.1], 1, costs_r=[0.25, 0]), 0.75)

    def test_cost_without_return_is_not_silently_ignored(self):
        with self.assertRaises(M.MetricContractError):
            M.probability_metrics([1], [0.5], k=1, costs_r=[0.1])

    def test_drawdown_initial_equity_and_return_domains(self):
        for value in (True, '1', None, -1, 0, float('inf'), 10**400):
            with self.subTest(value=repr(value)), self.assertRaises(M.MetricContractError):
                M.max_drawdown([0.1], initial_equity=value)
        for returns in ([], [True], ['0.1'], [-1.01], [float('nan')]):
            with self.subTest(returns=returns), self.assertRaises(M.MetricContractError):
                M.max_drawdown(returns)

    def test_drawdown_ruin_does_not_regrow_without_cash_inflow(self):
        self.assertEqual(M.max_drawdown([0.1, -1.0, 100.0]), 1)
        self.assertAlmostEqual(M.max_drawdown([0.1, -0.2, 0.1]), 0.2)

    def test_finite_input_overflow_does_not_produce_plausible_metric(self):
        for operation in (
            lambda: M.top_k_net_expectancy([1e308, 1e308], [0.5, 0.5], 2),
            lambda: M.profit_factor([1e308, 1e308, -1]),
            lambda: M.max_drawdown([1e308, 1e308]),
            lambda: M.profit_factor([1e308, -1e-308]),
        ):
            with self.subTest(operation=operation), self.assertRaises(M.MetricContractError):
                operation()

    def test_probability_formulas_and_tie_policy_preserved(self):
        self.assertAlmostEqual(M.brier_score([0, 1], [0.25, 0.75]), 0.0625)
        self.assertAlmostEqual(M.log_loss([0, 1], [0.25, 0.75]), -math.log(0.75))
        self.assertEqual(M.precision_at_k([1, 0], [0.5, 0.5], 1), 1)
        self.assertEqual(M.profit_factor([2, -1]), 2)
        self.assertEqual(M.profit_factor([0, 0]), 0)
        self.assertEqual(M.profit_factor([1, 2]), math.inf)  # Legacy computational sentinel only.

    def test_generator_inputs_remain_supported(self):
        self.assertEqual(M.probabilities(x / 2 for x in range(3)), (0, 0.5, 1))


if __name__ == '__main__':
    unittest.main()
