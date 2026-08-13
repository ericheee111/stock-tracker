from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from typing import cast

from _helpers import utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.config import QuantConfigError, load_quant_config
from stock_tracker.quant.core.reproducibility import (
    ReproducibilityRecord,
    set_reproducible,
)
from stock_tracker.quant.models import (
    ModelRegistry,
    RegistryContractError,
    RegistryEventType,
    registry_event,
)
from stock_tracker.quant.research import (
    CandidateContractError,
    CandidateSpec,
    ExperimentContractError,
    ExperimentEventType,
    ExperimentLedger,
    FeatureAvailability,
    LeakageContractError,
    ProbabilityAdvisory,
    assert_calibration_boundary,
    assess_negative_controls,
    experiment_event,
    risk_gated_action,
)


class TestQuantConfig(unittest.TestCase):
    def test_default_config_is_fail_closed(self) -> None:
        config = load_quant_config()
        self.assertFalse(config.safety.auto_apply_sql)
        self.assertFalse(config.safety.auto_promote_models)
        self.assertFalse(config.safety.allow_best_case_same_bar)
        self.assertFalse(config.safety.allow_random_kfold)
        self.assertTrue(config.safety.probability_advisory_only)
        self.assertEqual(len(config.config_hash), 64)

    def test_absolute_source_path_does_not_enter_config_hash(self) -> None:
        default = load_quant_config()
        paths = tuple(Path(path) for path in default.source_paths)
        explicit = load_quant_config(paths=paths)
        self.assertEqual(default.config_hash, explicit.config_hash)

    def test_relaxed_safety_control_is_rejected(self) -> None:
        source = Path("config/quant_wave1.toml").read_text(encoding="utf-8")
        source = source.replace("auto_apply_sql = false", "auto_apply_sql = true")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relaxed.toml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(QuantConfigError, "auto_apply_sql"):
                load_quant_config(paths=(path,))

    def test_invalid_toml_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_text("[safety\n", encoding="utf-8")
            with self.assertRaisesRegex(QuantConfigError, "invalid"):
                load_quant_config(paths=(path,))

    def test_missing_config_is_not_silently_ignored(self) -> None:
        with self.assertRaisesRegex(QuantConfigError, "missing"):
            load_quant_config(paths=(Path("does-not-exist.toml"),))


class TestProbabilitySafety(unittest.TestCase):
    def test_candidate_cannot_allow_direct_probability_action(self) -> None:
        with self.assertRaisesRegex(CandidateContractError, "cannot directly"):
            CandidateSpec(
                name="unsafe",
                model_family="fixture",
                strategy_id="S1",
                market=Market.A,
                horizon_sessions=5,
                feature_set_id="a" * 64,
                label_version="labels-v1",
                probability_is_advisory_only=False,
            )

    def test_candidate_safety_flags_require_real_booleans(self) -> None:
        with self.assertRaisesRegex(
            CandidateContractError,
            "probability_is_advisory_only must be a boolean",
        ):
            CandidateSpec(
                name="unsafe-type",
                model_family="fixture",
                strategy_id="S1",
                market=Market.A,
                horizon_sessions=5,
                feature_set_id="a" * 64,
                label_version="labels-v1",
                probability_is_advisory_only=cast(bool, "true"),
            )
        with self.assertRaisesRegex(
            CandidateContractError,
            "requires_calibration must be a boolean",
        ):
            CandidateSpec(
                name="unsafe-calibration-type",
                model_family="fixture",
                strategy_id="S1",
                market=Market.A,
                horizon_sessions=5,
                feature_set_id="a" * 64,
                label_version="labels-v1",
                requires_calibration=cast(bool, 1),
            )

    def test_probability_advisory_fields_require_strict_types(self) -> None:
        with self.assertRaisesRegex(
            CandidateContractError,
            "calibrated must be a boolean",
        ):
            ProbabilityAdvisory(
                probability=0.9,
                calibrated=cast(bool, "true"),
                calibration_id="a" * 64,
                model_id="fixture-model",
            )
        with self.assertRaisesRegex(
            CandidateContractError,
            "probability must be a finite number",
        ):
            ProbabilityAdvisory(
                probability=cast(float, True),
                calibrated=True,
                calibration_id="a" * 64,
                model_id="fixture-model",
            )

    def test_calibrated_advisory_still_requires_all_non_ml_gates(self) -> None:
        advisory = ProbabilityAdvisory(
            probability=0.9,
            calibrated=True,
            calibration_id="a" * 64,
            model_id="fixture-model",
        )
        blocked = risk_gated_action(
            advisory,
            rule_signal_allowed=True,
            risk_gate_allowed=False,
            data_quality_allowed=True,
            minimum_probability=0.6,
        )
        self.assertFalse(blocked.actionable)
        self.assertIn("RISK_GATE_BLOCKED", blocked.reasons)
        allowed = risk_gated_action(
            advisory,
            rule_signal_allowed=True,
            risk_gate_allowed=True,
            data_quality_allowed=True,
            minimum_probability=0.6,
        )
        self.assertTrue(allowed.actionable)

    def test_action_gates_require_real_booleans(self) -> None:
        advisory = ProbabilityAdvisory(
            probability=0.9,
            calibrated=True,
            calibration_id="a" * 64,
            model_id="fixture-model",
        )
        invalid = (
            ("rule_signal_allowed", "true", True, True),
            ("risk_gate_allowed", True, 1, True),
            ("data_quality_allowed", True, True, "false"),
        )
        for expected_name, rule_gate, risk_gate, quality_gate in invalid:
            with self.subTest(name=expected_name), self.assertRaisesRegex(
                CandidateContractError,
                f"{expected_name} must be a boolean",
            ):
                risk_gated_action(
                    advisory,
                    rule_signal_allowed=cast(bool, rule_gate),
                    risk_gate_allowed=cast(bool, risk_gate),
                    data_quality_allowed=cast(bool, quality_gate),
                    minimum_probability=0.6,
                )

    def test_action_threshold_rejects_boolean(self) -> None:
        advisory = ProbabilityAdvisory(
            probability=0.9,
            calibrated=True,
            calibration_id="a" * 64,
            model_id="fixture-model",
        )
        with self.assertRaisesRegex(
            CandidateContractError,
            "minimum_probability must be a finite number",
        ):
            risk_gated_action(
                advisory,
                rule_signal_allowed=True,
                risk_gate_allowed=True,
                data_quality_allowed=True,
                minimum_probability=cast(float, True),
            )

    def test_uncalibrated_probability_is_not_actionable(self) -> None:
        advisory = ProbabilityAdvisory(
            probability=0.99,
            calibrated=False,
            calibration_id=None,
            model_id="fixture-model",
        )
        decision = risk_gated_action(
            advisory,
            rule_signal_allowed=True,
            risk_gate_allowed=True,
            data_quality_allowed=True,
            minimum_probability=0.6,
        )
        self.assertIn("UNCALIBRATED_PROBABILITY", decision.reasons)


class TestLeakageControls(unittest.TestCase):
    def test_future_feature_availability_is_rejected(self) -> None:
        with self.assertRaisesRegex(LeakageContractError, "future feature"):
            FeatureAvailability(
                sample_id="sample-1",
                signal_time=utc_datetime(2025, 1, 2),
                feature_known_at=utc_datetime(2025, 1, 3),
                feature_usable_from=utc_datetime(2025, 1, 3),
            )

    def test_unfinished_calibration_label_is_detected(self) -> None:
        with self.assertRaisesRegex(LeakageContractError, "unfinished"):
            assert_calibration_boundary(
                (utc_datetime(2025, 1, 1),),
                (utc_datetime(2025, 1, 10),),
                utc_datetime(2025, 1, 5),
            )

    def test_future_feature_negative_control_is_flagged(self) -> None:
        labels = tuple(index % 2 for index in range(100))
        baseline = tuple(0.45 if label == 0 else 0.55 for label in labels)
        future = tuple(0.001 if label == 0 else 0.999 for label in labels)
        result = assess_negative_controls(
            y_true=labels,
            baseline_probabilities=baseline,
            future_feature_probabilities=future,
        )
        self.assertTrue(result.future_feature_flagged)
        self.assertTrue(result.suspicious_advantage_detected)
        self.assertLess(result.future_feature_brier, result.baseline_brier)


class TestReproducibility(unittest.TestCase):
    def test_seed_reproduces_python_random_sequence(self) -> None:
        set_reproducible(42)
        left = tuple(random.random() for _ in range(5))
        set_reproducible(42)
        right = tuple(random.random() for _ in range(5))
        self.assertEqual(left, right)

    def test_record_binds_config_data_code_and_seed(self) -> None:
        record = ReproducibilityRecord.build(
            config={"alpha": {3, 2, 1}},
            data_snapshot_ids=("b" * 64, "a" * 64),
            code_version="fixture-code",
            random_seed=42,
            trained_at=utc_datetime(2025, 1, 2),
        )
        self.assertEqual(record.data_snapshot_ids, ("a" * 64, "b" * 64))
        self.assertEqual(len(record.record_id), 64)
        self.assertIn("python", record.runtime)


class TestModelRegistry(unittest.TestCase):
    def event(
        self,
        event_type: RegistryEventType,
        model_id: str,
        *,
        predecessor: str | None = None,
        day: int = 1,
    ):
        return registry_event(
            event_type=event_type,
            model_id=model_id,
            strategy_id="S1",
            market=Market.A,
            horizon_sessions=5,
            data_snapshot_ids=("a" * 64,),
            feature_set_id="b" * 64,
            label_version="labels-v1",
            evidence_id="c" * 64,
            comparison_id="d" * 64 if event_type is RegistryEventType.PROMOTE else None,
            predecessor_model_id=predecessor,
            occurred_at=utc_datetime(2025, 1, day),
        )

    def test_append_and_current_champion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ModelRegistry(Path(directory) / "registry.jsonl")
            registry.append(self.event(RegistryEventType.REGISTER, "model-1"))
            registry.append(self.event(RegistryEventType.PROMOTE, "model-1", day=2))
            champion = registry.current_champion(("S1", Market.A, 5))
            self.assertEqual(champion.model_id, "model-1")
            self.assertEqual(len(registry.events()), 2)

    def test_promotion_predecessor_must_match_current_champion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ModelRegistry(Path(directory) / "registry.jsonl")
            registry.append(self.event(RegistryEventType.PROMOTE, "model-1"))
            with self.assertRaisesRegex(RegistryContractError, "predecessor"):
                registry.append(
                    self.event(
                        RegistryEventType.PROMOTE,
                        "model-2",
                        predecessor="wrong-model",
                        day=2,
                    )
                )

    def test_tampered_registry_line_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.jsonl"
            registry = ModelRegistry(path)
            registry.append(self.event(RegistryEventType.REGISTER, "model-1"))
            value = json.loads(path.read_text(encoding="utf-8"))
            value["model_id"] = "tampered"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryContractError, "invalid registry"):
                registry.events()


class TestExperimentLedger(unittest.TestCase):
    def reproducibility(self) -> ReproducibilityRecord:
        return ReproducibilityRecord.build(
            config={"fixture": True},
            data_snapshot_ids=("a" * 64,),
            code_version="fixture-code",
            random_seed=1,
            trained_at=utc_datetime(2025, 1, 1),
        )

    def test_terminal_experiment_cannot_receive_more_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ExperimentLedger(Path(directory) / "experiments.jsonl")
            reproducibility = self.reproducibility()
            ledger.append(
                experiment_event(
                    experiment_id="experiment-1",
                    event_type=ExperimentEventType.CREATED,
                    reproducibility=reproducibility,
                    occurred_at=utc_datetime(2025, 1, 1),
                )
            )
            ledger.append(
                experiment_event(
                    experiment_id="experiment-1",
                    event_type=ExperimentEventType.COMPLETED,
                    reproducibility=reproducibility,
                    metrics={"brier": 0.2},
                    occurred_at=utc_datetime(2025, 1, 2),
                )
            )
            with self.assertRaisesRegex(ExperimentContractError, "terminal"):
                ledger.append(
                    experiment_event(
                        experiment_id="experiment-1",
                        event_type=ExperimentEventType.STARTED,
                        reproducibility=reproducibility,
                        occurred_at=utc_datetime(2025, 1, 3),
                    )
                )

    def test_tampered_experiment_line_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiments.jsonl"
            ledger = ExperimentLedger(path)
            ledger.append(
                experiment_event(
                    experiment_id="experiment-1",
                    event_type=ExperimentEventType.CREATED,
                    reproducibility=self.reproducibility(),
                )
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            value["experiment_id"] = "tampered"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ExperimentContractError, "invalid experiment"):
                ledger.events()


if __name__ == "__main__":
    unittest.main()
