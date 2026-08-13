"""Deterministic synthetic benchmark for the quant model-governance pipeline.

The output verifies model training, temporal calibration, fair comparison and
promotion gating. It is not market evidence and must never be reported as an
investment-performance result.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.evaluation import (
    PlattCalibrator,
    ProbabilityMetrics,
    max_drawdown,
    probability_metrics,
)
from stock_tracker.quant.models import (
    ChampionGate,
    ChampionGateConfig,
    ComparisonIdentity,
    LogisticBaseline,
    ModelDataset,
    ModelEvaluation,
)
from stock_tracker.quant.research import assess_negative_controls

UTC = timezone.utc
SEED = 20260813
TOP_K = 30


@dataclass(frozen=True, slots=True)
class FixtureData:
    base_features: tuple[tuple[float, ...], ...]
    interaction_features: tuple[tuple[float, ...], ...]
    targets: tuple[int, ...]
    returns_r: tuple[float, ...]
    regimes: tuple[int, ...]
    signal_times: tuple[datetime, ...]
    label_end_times: tuple[datetime, ...]
    sample_ids: tuple[str, ...]


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp_value = math.exp(-value)
        return 1.0 / (1.0 + exp_value)
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _generate_fixture(count: int = 720) -> FixtureData:
    generator = random.Random(SEED)
    start = datetime(2021, 1, 1, tzinfo=UTC)
    base_features: list[tuple[float, ...]] = []
    interaction_features: list[tuple[float, ...]] = []
    targets: list[int] = []
    returns_r: list[float] = []
    regimes: list[int] = []
    signal_times: list[datetime] = []
    label_end_times: list[datetime] = []
    sample_ids: list[str] = []

    for index in range(count):
        regime = 1 if (index // 90) % 2 == 0 else -1
        x1 = math.sin(index / 13.0) + generator.gauss(0.0, 0.35)
        x2 = math.cos(index / 7.0) + generator.gauss(0.0, 0.30)
        liquidity = generator.uniform(-1.0, 1.0)
        cycle = math.sin(index / 41.0)
        base = (x1, x2, liquidity, float(regime), cycle)
        interactions = base + (x1 * x2, x1 * regime, x2 * x2)
        latent = (
            0.85 * x1
            - 0.55 * x2
            + 1.15 * x1 * x2
            + 0.55 * x1 * regime
            - 0.25 * liquidity
            + 0.20 * cycle
        )
        true_probability = _sigmoid(latent)
        target = int(generator.random() < true_probability)
        realized_return = (
            (1.15 + 0.12 * max(latent, 0.0))
            if target
            else (-0.82 - 0.06 * max(-latent, 0.0))
        )
        realized_return += generator.gauss(0.0, 0.08)
        signal_time = start + timedelta(days=index)
        holding_sessions = 2 + index % 6

        base_features.append(base)
        interaction_features.append(interactions)
        targets.append(target)
        returns_r.append(realized_return)
        regimes.append(regime)
        signal_times.append(signal_time)
        label_end_times.append(signal_time + timedelta(days=holding_sessions))
        sample_ids.append(f"fixture-{index:04d}")

    return FixtureData(
        base_features=tuple(base_features),
        interaction_features=tuple(interaction_features),
        targets=tuple(targets),
        returns_r=tuple(returns_r),
        regimes=tuple(regimes),
        signal_times=tuple(signal_times),
        label_end_times=tuple(label_end_times),
        sample_ids=tuple(sample_ids),
    )


def _dataset(
    fixture: FixtureData,
    *,
    features: tuple[tuple[float, ...], ...],
    feature_set_name: str,
    feature_names: tuple[str, ...],
    start: int,
    end: int,
    partition: str,
) -> ModelDataset:
    return ModelDataset(
        features=features[start:end],
        targets=fixture.targets[start:end],
        sample_ids=fixture.sample_ids[start:end],
        feature_names=feature_names,
        signal_times=fixture.signal_times[start:end],
        label_end_times=fixture.label_end_times[start:end],
        snapshot_id=fingerprint(
            {
                "schema": "synthetic-fixture-partition-v1",
                "partition": partition,
                "seed": SEED,
                "sample_ids": fixture.sample_ids[start:end],
                "feature_set_name": feature_set_name,
            }
        ),
    )


def _calibrated_probabilities(
    model: LogisticBaseline,
    calibration: ModelDataset,
    validation: ModelDataset,
) -> tuple[tuple[float, ...], str]:
    calibration_scores = model.decision_function(calibration.features)
    calibrator = PlattCalibrator().fit(calibration_scores, calibration.targets)
    probabilities = calibrator.predict(model.decision_function(validation.features))
    return probabilities, calibrator.model_id


def _bucket_rates(
    targets: Sequence[int],
    probabilities: Sequence[float],
    buckets: int = 5,
) -> tuple[float, ...]:
    ranked = sorted(
        range(len(targets)),
        key=lambda index: (probabilities[index], index),
    )
    result: list[float] = []
    for bucket in range(buckets):
        start = bucket * len(ranked) // buckets
        end = (bucket + 1) * len(ranked) // buckets
        indices = ranked[start:end]
        result.append(sum(targets[index] for index in indices) / len(indices))
    return tuple(result)


def _group_expectancies(
    probabilities: Sequence[float],
    returns_r: Sequence[float],
    groups: Sequence[Sequence[int]],
    *,
    selection_fraction: float = 0.25,
    cost_r: float = 0.03,
) -> tuple[float, ...]:
    result: list[float] = []
    for group in groups:
        count = max(1, math.ceil(len(group) * selection_fraction))
        selected = sorted(
            group,
            key=lambda index: (-probabilities[index], index),
        )[:count]
        result.append(
            sum(returns_r[index] - cost_r for index in selected) / len(selected)
        )
    return tuple(result)


def _strategy_drawdown(
    probabilities: Sequence[float],
    returns_r: Sequence[float],
    *,
    selection_fraction: float = 0.25,
    cost_r: float = 0.03,
) -> float:
    count = max(1, math.ceil(len(probabilities) * selection_fraction))
    selected = set(
        sorted(
            range(len(probabilities)),
            key=lambda index: (-probabilities[index], index),
        )[:count]
    )
    chronological_returns = tuple(
        0.02 * (returns_r[index] - cost_r) if index in selected else 0.0
        for index in range(len(probabilities))
    )
    return max_drawdown(chronological_returns)


def _evaluation(
    *,
    model_id: str,
    comparison_id: str,
    targets: Sequence[int],
    probabilities: Sequence[float],
    returns_r: Sequence[float],
    regimes: Sequence[int],
) -> ModelEvaluation:
    costs = tuple(0.03 for _ in returns_r)
    metrics = probability_metrics(
        targets,
        probabilities,
        k=TOP_K,
        returns_r=returns_r,
        costs_r=costs,
    )
    regime_groups = tuple(
        tuple(index for index, regime in enumerate(regimes) if regime == value)
        for value in (-1, 1)
    )
    block_size = len(targets) // 4
    time_groups = tuple(
        tuple(
            range(
                block * block_size,
                len(targets) if block == 3 else (block + 1) * block_size,
            )
        )
        for block in range(4)
    )
    return ModelEvaluation(
        model_id=model_id,
        comparison_id=comparison_id,
        metrics=metrics,
        score_bucket_rates=_bucket_rates(targets, probabilities),
        regime_expectancies=_group_expectancies(
            probabilities,
            returns_r,
            regime_groups,
        ),
        time_expectancies=_group_expectancies(
            probabilities,
            returns_r,
            time_groups,
        ),
        max_drawdown=_strategy_drawdown(probabilities, returns_r),
    )


def _metrics_dict(metrics: ProbabilityMetrics) -> dict[str, float | None]:
    return {
        "brier": metrics.brier,
        "logloss": metrics.logloss,
        "ece": metrics.ece,
        "precision_at_k": metrics.precision_at_k,
        "top_k_net_expectancy_r": metrics.top_k_net_expectancy,
    }


def build_result() -> dict[str, object]:
    fixture = _generate_fixture()
    train_end = 432
    calibration_end = 576
    base_names = ("x1", "x2", "liquidity", "regime", "cycle")
    interaction_names = base_names + ("x1_x2", "x1_regime", "x2_squared")

    champion_train = _dataset(
        fixture,
        features=fixture.base_features,
        feature_set_name="base-features",
        feature_names=base_names,
        start=0,
        end=train_end,
        partition="train-base",
    )
    champion_calibration = _dataset(
        fixture,
        features=fixture.base_features,
        feature_set_name="base-features",
        feature_names=base_names,
        start=train_end,
        end=calibration_end,
        partition="calibration-base",
    )
    champion_validation = _dataset(
        fixture,
        features=fixture.base_features,
        feature_set_name="base-features",
        feature_names=base_names,
        start=calibration_end,
        end=len(fixture.targets),
        partition="validation-base",
    )
    challenger_train = _dataset(
        fixture,
        features=fixture.interaction_features,
        feature_set_name="interaction-features",
        feature_names=interaction_names,
        start=0,
        end=train_end,
        partition="train-interaction",
    )
    challenger_calibration = _dataset(
        fixture,
        features=fixture.interaction_features,
        feature_set_name="interaction-features",
        feature_names=interaction_names,
        start=train_end,
        end=calibration_end,
        partition="calibration-interaction",
    )
    challenger_validation = _dataset(
        fixture,
        features=fixture.interaction_features,
        feature_set_name="interaction-features",
        feature_names=interaction_names,
        start=calibration_end,
        end=len(fixture.targets),
        partition="validation-interaction",
    )

    champion_model = LogisticBaseline(
        learning_rate=0.05,
        max_iter=5_000,
        l2=1e-3,
    ).fit(champion_train)
    challenger_model = LogisticBaseline(
        learning_rate=0.05,
        max_iter=5_000,
        l2=2e-3,
    ).fit(challenger_train)
    champion_probabilities, champion_calibration_id = _calibrated_probabilities(
        champion_model,
        champion_calibration,
        champion_validation,
    )
    challenger_probabilities, challenger_calibration_id = _calibrated_probabilities(
        challenger_model,
        challenger_calibration,
        challenger_validation,
    )

    partition_ids = {
        "train": fingerprint(
            {
                "champion": champion_train.dataset_id,
                "challenger": challenger_train.dataset_id,
            }
        ),
        "calibration": fingerprint(
            {
                "champion": champion_calibration.dataset_id,
                "challenger": challenger_calibration.dataset_id,
            }
        ),
        "validation": fingerprint(
            {
                "champion": champion_validation.dataset_id,
                "challenger": challenger_validation.dataset_id,
            }
        ),
    }
    identity = ComparisonIdentity(
        train_dataset_id=partition_ids["train"],
        calibration_dataset_id=partition_ids["calibration"],
        validation_dataset_id=partition_ids["validation"],
        feature_set_version="base-v1-vs-interaction-v1",
        label_version="synthetic-binary-target-v1",
        market_rule_hash=fingerprint({"fixture_market_rule": "not-real"}),
        cost_schedule_hash=fingerprint({"fixture_cost_r": 0.03}),
        calibration_definition="PLATT_PER_MODEL_COMPLETED_LABELS_ONLY",
        calibration_window="indices-432-to-575",
        top_k=TOP_K,
        top_k_definition="validation-probability-descending-net-of-0.03R",
        random_seed=SEED,
    )
    validation_targets = fixture.targets[calibration_end:]
    validation_returns = fixture.returns_r[calibration_end:]
    validation_regimes = fixture.regimes[calibration_end:]
    champion = _evaluation(
        model_id=champion_model.model_id,
        comparison_id=identity.comparison_id,
        targets=validation_targets,
        probabilities=champion_probabilities,
        returns_r=validation_returns,
        regimes=validation_regimes,
    )
    challenger = _evaluation(
        model_id=challenger_model.model_id,
        comparison_id=identity.comparison_id,
        targets=validation_targets,
        probabilities=challenger_probabilities,
        returns_r=validation_returns,
        regimes=validation_regimes,
    )
    gate_config = ChampionGateConfig(
        minimum_brier_improvement=0.001,
        minimum_logloss_improvement=0.001,
        maximum_ece_regression=0.0,
        maximum_precision_regression=0.0,
        minimum_expectancy_improvement=0.0,
        maximum_drawdown_regression=0.0,
        monotonic_tolerance=0.0,
        maximum_regime_range=0.50,
        maximum_time_range=0.50,
    )
    decision = ChampionGate(gate_config).evaluate(champion, challenger)

    future_probabilities = tuple(
        0.001 if target == 0 else 0.999 for target in validation_targets
    )
    negative = assess_negative_controls(
        y_true=validation_targets,
        baseline_probabilities=champion_probabilities,
        future_feature_probabilities=future_probabilities,
        seed=SEED,
    )
    lightgbm_available = importlib.util.find_spec("lightgbm") is not None

    return {
        "schema": "stock-tracker-quant-synthetic-fixture-v1",
        "synthetic_fixture_only": True,
        "investment_performance_claim": False,
        "seed": SEED,
        "sample_counts": {
            "train": train_end,
            "calibration": calibration_end - train_end,
            "validation": len(validation_targets),
        },
        "comparison_identity": {
            "comparison_id": identity.comparison_id,
            "train_dataset_id": identity.train_dataset_id,
            "calibration_dataset_id": identity.calibration_dataset_id,
            "validation_dataset_id": identity.validation_dataset_id,
            "calibration_definition": identity.calibration_definition,
            "top_k_definition": identity.top_k_definition,
        },
        "champion": {
            "name": "logistic-base-features",
            "model_id": champion.model_id,
            "calibration_id": champion_calibration_id,
            "feature_count": len(base_names),
            "metrics": _metrics_dict(champion.metrics),
            "score_bucket_rates": champion.score_bucket_rates,
            "regime_expectancies_r": champion.regime_expectancies,
            "time_expectancies_r": champion.time_expectancies,
            "max_drawdown": champion.max_drawdown,
        },
        "challenger": {
            "name": "interaction-logistic-governance-fixture",
            "model_id": challenger.model_id,
            "calibration_id": challenger_calibration_id,
            "feature_count": len(interaction_names),
            "metrics": _metrics_dict(challenger.metrics),
            "score_bucket_rates": challenger.score_bucket_rates,
            "regime_expectancies_r": challenger.regime_expectancies,
            "time_expectancies_r": challenger.time_expectancies,
            "max_drawdown": challenger.max_drawdown,
        },
        "promotion_gate": {
            "promoted": decision.promoted,
            "reasons": decision.reasons,
            "decision_id": decision.decision_id,
            "config": {
                "minimum_brier_improvement": gate_config.minimum_brier_improvement,
                "minimum_logloss_improvement": gate_config.minimum_logloss_improvement,
                "maximum_ece_regression": gate_config.maximum_ece_regression,
                "maximum_precision_regression": gate_config.maximum_precision_regression,
                "minimum_expectancy_improvement": gate_config.minimum_expectancy_improvement,
                "maximum_drawdown_regression": gate_config.maximum_drawdown_regression,
                "maximum_regime_range": gate_config.maximum_regime_range,
                "maximum_time_range": gate_config.maximum_time_range,
            },
        },
        "negative_controls": {
            "baseline_brier": negative.baseline_brier,
            "random_feature_brier": negative.random_feature_brier,
            "randomized_label_brier": negative.randomized_label_brier,
            "future_feature_brier": negative.future_feature_brier,
            "future_feature_flagged": negative.future_feature_flagged,
            "suspicious_advantage_detected": negative.suspicious_advantage_detected,
        },
        "optional_lightgbm": {
            "available": lightgbm_available,
            "evaluated": False,
            "reason": (
                "This environment does not provide lightgbm."
                if not lightgbm_available
                else "Not evaluated by this dependency-light synthetic fixture."
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Optional JSON output path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_result()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
