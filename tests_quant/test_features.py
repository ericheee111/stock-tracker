from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import date, timedelta

from _helpers import make_bar

from stock_tracker.core.types import Market
from stock_tracker.quant.features import (
    Alpha158Style,
    AuditStatus,
    FeatureComputationError,
    FeatureContext,
    FeatureContextError,
    FeatureDefinition,
    FeatureFamily,
    FeatureSetDefinition,
    NormalizationContractError,
    TrainOnlyStandardizer,
    alpha158_style_definition,
    default_qlib_audit,
    feature_names,
    point_in_time_rank,
)
from stock_tracker.quant.models import (
    DiagnosticContractError,
    ablation_indices,
    highly_correlated_pairs,
    permutation_importance,
)


class FeatureFixture(unittest.TestCase):
    def bars(self, count: int = 60) -> tuple:
        start = date(2025, 1, 1)
        result = []
        price = 10.0
        for index in range(count):
            session = start + timedelta(days=index)
            drift = 0.01 * index
            close = price + drift + (index % 3 - 1) * 0.02
            result.append(
                make_bar(
                    session,
                    open_price=close - 0.03,
                    high=close + 0.12,
                    low=close - 0.10,
                    close=close,
                    volume=10_000 + index * 100,
                )
            )
        return tuple(result)

    def context(self, count: int = 60) -> FeatureContext:
        bars = self.bars(count)
        return FeatureContext(
            symbol="600000.SH",
            market=Market.A,
            as_of=bars[-1].timestamp,
            bars=bars,
            data_snapshot_id="a" * 64,
            calendar_snapshot_id="b" * 64,
            universe_snapshot_id="c" * 64,
            metadata={"fixture": True},
        )


class TestAlpha158Style(FeatureFixture):
    def test_inventory_is_exactly_158_and_unique(self) -> None:
        names = feature_names()
        self.assertEqual(len(names), 158)
        self.assertEqual(len(set(names)), 158)
        definition = alpha158_style_definition()
        self.assertEqual(len(definition.features), 158)
        self.assertFalse(definition.numerically_equivalent_to_qlib)

    def test_computation_is_finite_and_bound_to_context(self) -> None:
        context = self.context()
        vector = Alpha158Style().compute(context)
        self.assertEqual(vector.names, feature_names())
        self.assertEqual(len(vector.values), 158)
        self.assertTrue(all(math.isfinite(value) for value in vector.values))
        self.assertEqual(vector.context_id, context.context_id)
        self.assertEqual(vector.feature_set_id, alpha158_style_definition().feature_set_id)

    def test_insufficient_history_fails_closed(self) -> None:
        with self.assertRaisesRegex(FeatureComputationError, "60 sessions"):
            Alpha158Style().compute(self.context(59))

    def test_future_bar_rejected_by_context(self) -> None:
        bars = self.bars(61)
        with self.assertRaisesRegex(FeatureContextError, "future bar"):
            FeatureContext(
                symbol="600000.SH",
                market=Market.A,
                as_of=bars[-2].timestamp,
                bars=bars,
                data_snapshot_id="a" * 64,
                calendar_snapshot_id="b" * 64,
                universe_snapshot_id="c" * 64,
            )

    def test_context_identity_binds_snapshot_ids(self) -> None:
        context = self.context()
        changed = replace(context, data_snapshot_id="d" * 64)
        self.assertNotEqual(context.context_id, changed.context_id)

    def test_formal_feature_cannot_be_noncausal(self) -> None:
        with self.assertRaisesRegex(ValueError, "causal"):
            FeatureDefinition(
                name="future_return",
                family=FeatureFamily.MOMENTUM,
                lookback_sessions=1,
                causal=False,
                description="forbidden",
                formula_version="v1",
            )

    def test_qlib_equivalence_requires_exact_revision(self) -> None:
        feature = FeatureDefinition(
            name="causal",
            family=FeatureFamily.TREND,
            lookback_sessions=5,
            causal=True,
            description="fixture",
            formula_version="v1",
        )
        with self.assertRaisesRegex(ValueError, "pinned revision"):
            FeatureSetDefinition(
                name="invalid-equivalence",
                version="1",
                features=(feature,),
                qlib_revision=None,
                numerically_equivalent_to_qlib=True,
            )


class TestNormalization(unittest.TestCase):
    def test_transform_requires_training_fit(self) -> None:
        with self.assertRaises(NormalizationContractError):
            TrainOnlyStandardizer().transform(((1.0, 2.0),))

    def test_train_only_standardizer_is_deterministic(self) -> None:
        rows = ((1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (4.0, 40.0))
        left = TrainOnlyStandardizer().fit(
            rows,
            training_dataset_id="a" * 64,
            winsor_quantile=0.0,
        )
        right = TrainOnlyStandardizer().fit(
            rows,
            training_dataset_id="a" * 64,
            winsor_quantile=0.0,
        )
        self.assertEqual(left.transform_id, right.transform_id)
        transformed = left.transform(rows)
        self.assertAlmostEqual(sum(row[0] for row in transformed), 0.0)

    def test_transform_identity_changes_with_training_dataset(self) -> None:
        rows = ((1.0,), (2.0,), (3.0,))
        left = TrainOnlyStandardizer().fit(rows, training_dataset_id="a" * 64)
        right = TrainOnlyStandardizer().fit(rows, training_dataset_id="b" * 64)
        self.assertNotEqual(left.transform_id, right.transform_id)

    def test_point_in_time_rank_handles_ties_deterministically(self) -> None:
        ranks = point_in_time_rank({"A": 1.0, "B": 2.0, "C": 2.0, "D": 4.0})
        self.assertEqual(ranks["B"], ranks["C"])
        self.assertLess(ranks["A"], ranks["B"])
        self.assertLess(ranks["B"], ranks["D"])


class TestQlibAudit(unittest.TestCase):
    def test_default_audit_keeps_three_equivalence_blockers(self) -> None:
        audit = default_qlib_audit()
        self.assertFalse(audit.may_claim_numerical_equivalence)
        self.assertEqual(
            audit.blockers,
            (
                "Corporate-action golden mapping",
                "Exact Qlib revision pinned",
                "Golden-data numerical equivalence",
            ),
        )
        self.assertTrue(
            all(
                item.status is AuditStatus.BLOCKER
                for item in audit.items
                if item.name in audit.blockers
            )
        )


class TestFeatureDiagnostics(unittest.TestCase):
    def test_high_correlation_pairs(self) -> None:
        rows = tuple((float(index), float(index * 2), float(index % 2)) for index in range(10))
        pairs = highly_correlated_pairs(rows, ("x", "two_x", "parity"), threshold=0.99)
        self.assertEqual(pairs[0][:2], ("x", "two_x"))

    def test_ablation_indices_remove_one_family(self) -> None:
        result = ablation_indices(
            ("a", "b", "c", "d"),
            {"trend": ("a", "c")},
        )
        self.assertEqual(result["trend"], (1, 3))

    def test_unknown_ablation_feature_rejected(self) -> None:
        with self.assertRaises(DiagnosticContractError):
            ablation_indices(("a",), {"bad": ("missing",)})

    def test_permutation_importance_finds_predictive_column(self) -> None:
        rows = tuple((float(index), float(index % 3)) for index in range(20))
        targets = tuple(0 if index < 10 else 1 for index in range(20))

        def predict(values: tuple | list) -> tuple[float, ...]:
            return tuple(0.05 if row[0] < 10 else 0.95 for row in values)

        result = permutation_importance(
            rows=rows,
            targets=targets,
            feature_names=("signal", "noise"),
            predict=predict,
            seed=42,
        )
        self.assertEqual(result[0].feature_name, "signal")
        self.assertGreater(result[0].importance, result[1].importance)


if __name__ == "__main__":
    unittest.main()
