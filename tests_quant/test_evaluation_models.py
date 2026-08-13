from __future__ import annotations

import importlib.util
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from _helpers import utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.evaluation import (
    CalibrationContractError,
    CalibrationMethod,
    CalibrationRow,
    FrozenHoldout,
    HoldoutContractError,
    HoldoutState,
    IsotonicCalibrator,
    MetricContractError,
    PlattCalibrator,
    ProbabilityMetrics,
    TemporalSample,
    WalkForwardConfig,
    WalkForwardMode,
    assert_no_label_overlap,
    brier_score,
    build_walk_forward,
    expected_calibration_error,
    log_loss,
    max_drawdown,
    precision_at_k,
    select_completed_rows,
)
from stock_tracker.quant.models import (
    ChampionGate,
    ComparisonIdentity,
    DatasetContractError,
    LightGBMMetaLabelCandidate,
    LogisticBaseline,
    ModelContractError,
    ModelDataset,
    ModelEvaluation,
    horizons_for,
)


class TestProbabilityMetrics(unittest.TestCase):
    def test_nonbinary_target_is_rejected_without_truncation(self) -> None:
        with self.assertRaises(MetricContractError):
            brier_score((0.9, 1.0), (0.2, 0.8))

    def test_brier_and_logloss_are_finite(self) -> None:
        labels = (0, 0, 1, 1)
        probabilities = (0.1, 0.2, 0.8, 0.9)
        self.assertAlmostEqual(brier_score(labels, probabilities), 0.025)
        self.assertGreater(log_loss(labels, probabilities), 0.0)
        self.assertLess(expected_calibration_error(labels, probabilities, bins=2), 0.2)
        self.assertEqual(precision_at_k(labels, probabilities, 2), 1.0)

    def test_max_drawdown(self) -> None:
        self.assertAlmostEqual(max_drawdown((0.1, -0.2, 0.1)), 0.2)


class TestWalkForward(unittest.TestCase):
    def samples(self) -> tuple[TemporalSample, ...]:
        start = utc_datetime(2025, 1, 1)
        output = []
        for index in range(24):
            signal = start + timedelta(days=index)
            label_end = signal
            if index == 9:
                label_end = start + timedelta(days=11)
            output.append(
                TemporalSample(
                    sample_id=f"sample-{index:02d}",
                    signal_time=signal,
                    label_end_time=label_end,
                    features=(float(index), float(index % 3)),
                    target=index % 2,
                )
            )
        return tuple(output)

    def test_gap_purge_and_embargo_are_explicit(self) -> None:
        samples = self.samples()
        folds = build_walk_forward(
            samples,
            WalkForwardConfig(
                mode=WalkForwardMode.EXPANDING,
                minimum_train_samples=5,
                validation_samples=3,
                step_samples=3,
                gap_samples=1,
                embargo_samples=2,
            ),
        )
        self.assertGreaterEqual(len(folds), 2)
        self.assertEqual(folds[0].gap_indices, (5,))
        self.assertEqual(folds[0].validation_indices, (6, 7, 8))
        self.assertEqual(folds[0].embargo_indices, (9, 10))
        self.assertEqual(folds[1].validation_indices[0], 11)
        self.assertIn(9, folds[1].purged_indices)
        assert_no_label_overlap(samples, folds[1])

    def test_rolling_window_is_bounded(self) -> None:
        folds = build_walk_forward(
            self.samples(),
            WalkForwardConfig(
                mode=WalkForwardMode.ROLLING,
                minimum_train_samples=5,
                rolling_train_samples=7,
                validation_samples=2,
                step_samples=2,
            ),
        )
        self.assertTrue(all(len(fold.train_indices) <= 7 for fold in folds))

    def test_unordered_samples_are_rejected(self) -> None:
        samples = self.samples()
        with self.assertRaisesRegex(ValueError, "ordered"):
            build_walk_forward(
                tuple(reversed(samples)),
                WalkForwardConfig(WalkForwardMode.EXPANDING, 5, 2, 2),
            )


class TestCalibration(unittest.TestCase):
    def test_selection_uses_label_end_not_signal_time(self) -> None:
        rows = (
            CalibrationRow(
                "unfinished",
                utc_datetime(2025, 1, 1),
                utc_datetime(2025, 1, 10),
                -1.0,
                0,
            ),
            CalibrationRow(
                "complete",
                utc_datetime(2025, 1, 2),
                utc_datetime(2025, 1, 3),
                1.0,
                1,
            ),
        )
        selected = select_completed_rows(rows, cutoff=utc_datetime(2025, 1, 5))
        self.assertEqual(tuple(row.sample_id for row in selected), ("complete",))

    def test_no_completed_label_fails(self) -> None:
        row = CalibrationRow(
            "unfinished",
            utc_datetime(2025, 1, 1),
            utc_datetime(2025, 1, 10),
            0.0,
            0,
        )
        with self.assertRaises(CalibrationContractError):
            select_completed_rows((row,), cutoff=utc_datetime(2025, 1, 5))

    def test_platt_predictions_are_ordered(self) -> None:
        model = PlattCalibrator().fit(
            (-3.0, -2.0, -1.0, 1.0, 2.0, 3.0),
            (0, 0, 0, 1, 1, 1),
        )
        low, high = model.predict((-2.0, 2.0))
        self.assertLess(low, high)
        self.assertEqual(len(model.model_id), 64)

    def test_isotonic_predictions_are_monotonic(self) -> None:
        model = IsotonicCalibrator().fit(
            (0.1, 0.2, 0.3, 0.4, 0.5),
            (0, 1, 0, 1, 1),
        )
        predictions = model.predict((0.1, 0.2, 0.3, 0.4, 0.5))
        self.assertEqual(tuple(sorted(predictions)), predictions)


class TestFrozenHoldout(unittest.TestCase):
    def test_matching_first_exposure_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.json"
            holdout = FrozenHoldout.seal(
                path,
                holdout_id="holdout-A",
                config_hash="a" * 64,
                data_snapshot_id="b" * 64,
                sealed_at=utc_datetime(2025, 1, 1),
            )
            record = holdout.expose(
                config_hash="a" * 64,
                data_snapshot_id="b" * 64,
                exposed_at=utc_datetime(2025, 1, 2),
            )
            self.assertEqual(record.state, HoldoutState.EXPOSED)
            self.assertEqual(FrozenHoldout.load(path).record.exposure_count, 1)

    def test_mismatch_permanently_compromises_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.json"
            holdout = FrozenHoldout.seal(
                path,
                holdout_id="holdout-A",
                config_hash="a" * 64,
                data_snapshot_id="b" * 64,
            )
            with self.assertRaises(HoldoutContractError):
                holdout.expose(
                    config_hash="c" * 64,
                    data_snapshot_id="b" * 64,
                )
            persisted = FrozenHoldout.load(path).record
            self.assertEqual(persisted.state, HoldoutState.COMPROMISED)
            with self.assertRaises(HoldoutContractError):
                FrozenHoldout.load(path).expose(
                    config_hash="a" * 64,
                    data_snapshot_id="b" * 64,
                )

    def test_existing_path_cannot_be_resealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.json"
            FrozenHoldout.seal(
                path,
                holdout_id="holdout-A",
                config_hash="a" * 64,
                data_snapshot_id="b" * 64,
            )
            with self.assertRaises(HoldoutContractError):
                FrozenHoldout.seal(
                    path,
                    holdout_id="holdout-B",
                    config_hash="a" * 64,
                    data_snapshot_id="b" * 64,
                )


class ModelFixture(unittest.TestCase):
    def dataset(self) -> ModelDataset:
        start = utc_datetime(2025, 1, 1)
        features = tuple((index / 10.0 - 5.0, float(index % 5)) for index in range(100))
        targets = tuple(0 if row[0] < 0 else 1 for row in features)
        return ModelDataset(
            features=features,
            targets=targets,
            sample_ids=tuple(f"sample-{index:03d}" for index in range(100)),
            feature_names=("trend", "seasonality"),
            signal_times=tuple(start + timedelta(days=index) for index in range(100)),
            label_end_times=tuple(start + timedelta(days=index + 1) for index in range(100)),
            snapshot_id="d" * 64,
        )


class TestLogisticBaseline(ModelFixture):
    def test_fit_predict_and_round_trip(self) -> None:
        dataset = self.dataset()
        model = LogisticBaseline(learning_rate=0.1, max_iter=5_000).fit(dataset)
        low, high = model.predict_proba(((-4.0, 0.0), (4.0, 0.0)))
        self.assertLess(low, 0.2)
        self.assertGreater(high, 0.8)
        restored = LogisticBaseline.from_dict(model.as_dict())
        self.assertEqual(restored.model_id, model.model_id)

    def test_feature_width_mismatch_rejected(self) -> None:
        model = LogisticBaseline().fit(self.dataset())
        with self.assertRaises(ModelContractError):
            model.predict_proba(((1.0,),))

    def test_dataset_rejects_nonfinite_feature(self) -> None:
        dataset = self.dataset()
        with self.assertRaises(DatasetContractError):
            replace(
                dataset,
                features=((math.nan, 0.0), *dataset.features[1:]),
            )

    def test_optional_lightgbm_does_not_replace_baseline_dependency(self) -> None:
        candidate = LightGBMMetaLabelCandidate()
        if importlib.util.find_spec("lightgbm") is None:
            with self.assertRaisesRegex(ModelContractError, "optional"):
                candidate.fit(self.dataset())
        else:
            candidate.fit(self.dataset())
            self.assertEqual(len(candidate.model_id), 64)


class TestChampionGate(unittest.TestCase):
    def identity(self) -> ComparisonIdentity:
        return ComparisonIdentity(
            train_dataset_id="a" * 64,
            calibration_dataset_id="b" * 64,
            validation_dataset_id="c" * 64,
            feature_set_version="features-v1",
            label_version="labels-v1",
            market_rule_hash="d" * 64,
            cost_schedule_hash="e" * 64,
            calibration_definition=CalibrationMethod.PLATT.value,
            calibration_window="completed-labels-before-validation",
            top_k=10,
            top_k_definition="global-probability-descending",
            random_seed=42,
        )

    def evaluation(
        self,
        model_id: str,
        *,
        brier: float,
        logloss_value: float,
        ece: float,
        precision: float,
        expectancy: float,
        comparison_id: str | None = None,
    ) -> ModelEvaluation:
        return ModelEvaluation(
            model_id=model_id,
            comparison_id=comparison_id or self.identity().comparison_id,
            metrics=ProbabilityMetrics(
                brier=brier,
                logloss=logloss_value,
                ece=ece,
                precision_at_k=precision,
                top_k_net_expectancy=expectancy,
            ),
            score_bucket_rates=(0.1, 0.3, 0.5, 0.7),
            regime_expectancies=(0.2, 0.3, 0.25),
            time_expectancies=(0.2, 0.25, 0.3),
            max_drawdown=0.1,
        )

    def test_strictly_better_challenger_promotes(self) -> None:
        champion = self.evaluation(
            "logistic",
            brier=0.20,
            logloss_value=0.60,
            ece=0.08,
            precision=0.70,
            expectancy=0.20,
        )
        challenger = self.evaluation(
            "challenger",
            brier=0.18,
            logloss_value=0.55,
            ece=0.07,
            precision=0.72,
            expectancy=0.25,
        )
        self.assertTrue(ChampionGate().evaluate(champion, challenger).promoted)

    def test_nan_cannot_escape_gate(self) -> None:
        champion = self.evaluation(
            "logistic",
            brier=0.20,
            logloss_value=0.60,
            ece=0.08,
            precision=0.70,
            expectancy=0.20,
        )
        challenger = self.evaluation(
            "challenger",
            brier=math.nan,
            logloss_value=0.50,
            ece=0.05,
            precision=0.80,
            expectancy=0.30,
        )
        decision = ChampionGate().evaluate(champion, challenger)
        self.assertFalse(decision.promoted)
        self.assertIn("INVALID_CHALLENGER_METRICS", decision.reasons)

    def test_identity_mismatch_blocks_comparison(self) -> None:
        champion = self.evaluation(
            "logistic",
            brier=0.20,
            logloss_value=0.60,
            ece=0.08,
            precision=0.70,
            expectancy=0.20,
        )
        challenger = self.evaluation(
            "challenger",
            brier=0.18,
            logloss_value=0.55,
            ece=0.07,
            precision=0.72,
            expectancy=0.25,
            comparison_id="f" * 64,
        )
        decision = ChampionGate().evaluate(champion, challenger)
        self.assertIn("COMPARISON_IDENTITY_MISMATCH", decision.reasons)

    def test_nonmonotonic_score_buckets_block_promotion(self) -> None:
        champion = self.evaluation(
            "logistic",
            brier=0.20,
            logloss_value=0.60,
            ece=0.08,
            precision=0.70,
            expectancy=0.20,
        )
        challenger = replace(
            self.evaluation(
                "challenger",
                brier=0.18,
                logloss_value=0.55,
                ece=0.07,
                precision=0.72,
                expectancy=0.25,
            ),
            score_bucket_rates=(0.1, 0.5, 0.4, 0.8),
        )
        decision = ChampionGate().evaluate(champion, challenger)
        self.assertIn("SCORE_BUCKET_NOT_MONOTONIC", decision.reasons)


class TestMarketHorizons(unittest.TestCase):
    def test_horizons_are_market_specific(self) -> None:
        self.assertEqual(horizons_for(Market.A), (3, 5, 10, 20))
        self.assertEqual(horizons_for(Market.US), (20, 40, 60, 120))


if __name__ == "__main__":
    unittest.main()
