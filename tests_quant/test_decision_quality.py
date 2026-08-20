from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from stock_tracker.core.types import Market
from stock_tracker.quant.core.outcomes import StrategyScoreboard
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.evaluation.decision_quality import (
    DecisionQualityAssessment,
    DecisionQualityContractError,
    DecisionQualityEvidence,
    DecisionQualityState,
    ResearchLicenseStatus,
)
from stock_tracker.quant.evaluation.holdout import FrozenHoldoutRecord, HoldoutState
from stock_tracker.quant.evaluation.metrics import ProbabilityMetrics
from stock_tracker.quant.models.comparison import ModelEvaluation
from stock_tracker.quant.research.replay import (
    ReplayPurpose,
    build_replay_plan,
)
from tests_quant.test_attribution import _scoreboard
from tests_quant.test_outcomes import _BASE, _complete_outcome, _hash, _policy
from tests_quant.test_replay import _dependencies, _request


def _evaluation(
    model_id: str,
    *,
    brier: float,
    logloss: float,
    ece: float,
    precision: float,
    expectancy: float,
    comparison_id: str = "c" * 64,
) -> ModelEvaluation:
    return ModelEvaluation(
        model_id=model_id,
        comparison_id=comparison_id,
        metrics=ProbabilityMetrics(
            brier=brier,
            logloss=logloss,
            ece=ece,
            precision_at_k=precision,
            top_k_net_expectancy=expectancy,
        ),
        score_bucket_rates=(0.1, 0.3, 0.5, 0.7),
        regime_expectancies=(0.2, 0.25, 0.3),
        time_expectancies=(0.2, 0.25, 0.3),
        max_drawdown=0.1,
    )


def _models() -> tuple[ModelEvaluation, ModelEvaluation, ModelEvaluation]:
    baseline = _evaluation(
        "baseline",
        brier=0.25,
        logloss=0.70,
        ece=0.10,
        precision=0.65,
        expectancy=0.10,
    )
    champion = _evaluation(
        "champion",
        brier=0.20,
        logloss=0.60,
        ece=0.08,
        precision=0.70,
        expectancy=0.20,
    )
    challenger = _evaluation(
        "model-v2",
        brier=0.18,
        logloss=0.55,
        ece=0.07,
        precision=0.72,
        expectancy=0.25,
    )
    return baseline, champion, challenger


def _formal_scoreboard() -> StrategyScoreboard:
    return _scoreboard(
        "v2",
        (
            _complete_outcome(exit_price="12", signal_suffix="a"),
            _complete_outcome(
                exit_price="10.5",
                signal_suffix="b",
                recorded_offset_days=1,
            ),
        ),
    )


def _holdout(
    *,
    state: HoldoutState = HoldoutState.EXPOSED,
    exposure_count: int = 1,
) -> FrozenHoldoutRecord:
    first_exposed = _BASE + timedelta(minutes=1) if exposure_count else None
    return FrozenHoldoutRecord(
        holdout_id="holdout-v1",
        config_hash=_hash("b"),
        data_snapshot_id=_hash("d"),
        sealed_at=_BASE,
        state=state,
        first_exposed_at=first_exposed,
        compromised_at=(
            _BASE + timedelta(minutes=2)
            if state is HoldoutState.COMPROMISED
            else None
        ),
        compromise_reason=(
            "TEST_COMPROMISE" if state is HoldoutState.COMPROMISED else None
        ),
        exposure_count=exposure_count,
    )


def _evidence(
    scoreboard: StrategyScoreboard,
    replay_plan,
    holdout: FrozenHoldoutRecord,
    *,
    tier: DataTrustTier = DataTrustTier.RESEARCH_GRADE,
    license_status: ResearchLicenseStatus = ResearchLicenseStatus.CLEARED,
    verified: bool = True,
    complete: bool = True,
    synthetic: bool = False,
    calibration_verified: bool = True,
    leakage_passed: bool = True,
    negative_controls_passed: bool = True,
) -> DecisionQualityEvidence:
    return DecisionQualityEvidence(
        strategy_id="S1_BREAKOUT",
        market=Market.A,
        horizon_sessions=20,
        baseline_model_id="baseline",
        champion_model_id="champion",
        challenger_model_id="model-v2",
        code_id=_hash("a"),
        config_id=_hash("b"),
        dataset_id=_hash("d"),
        feature_set_id=_hash("e"),
        label_id=_hash("f"),
        calibration_id=_hash("1"),
        leakage_audit_id=_hash("2"),
        negative_control_id=_hash("3"),
        experiment_id=_hash("4"),
        registry_snapshot_id=_hash("5"),
        scoreboard_id=scoreboard.scoreboard_id,
        replay_plan_id=replay_plan.plan_id,
        holdout_record_id=holdout.record_hash,
        data_trust_tier=tier,
        license_status=license_status,
        license_evidence_ids=(
            ()
            if license_status is ResearchLicenseStatus.PENDING
            else (_hash("6"),)
        ),
        verification_evidence_ids=(_hash("7"),) if verified else (),
        recorded_trials=1,
        verified=verified,
        complete=complete,
        calibration_verified=calibration_verified,
        leakage_audit_passed=leakage_passed,
        negative_controls_passed=negative_controls_passed,
        synthetic_fixture_only=synthetic,
    )


def _formal_assessment(**evidence_changes) -> DecisionQualityAssessment:
    baseline, champion, challenger = _models()
    scoreboard = _formal_scoreboard()
    replay = build_replay_plan(_request())
    holdout = _holdout()
    evidence = _evidence(scoreboard, replay, holdout, **evidence_changes)
    return DecisionQualityAssessment(
        evidence=evidence,
        baseline=baseline,
        champion=champion,
        challenger=challenger,
        scoreboard=scoreboard,
        replay_plan=replay,
        holdout=holdout,
    )


class TestDecisionQualityGate(unittest.TestCase):
    def test_all_formal_gates_produce_eligibility_without_deployment(self) -> None:
        assessment = _formal_assessment()
        self.assertEqual(
            assessment.state,
            DecisionQualityState.PROMOTION_ELIGIBLE,
        )
        self.assertEqual(assessment.structural_blockers, ())
        self.assertEqual(assessment.formal_blockers, ())
        self.assertEqual(assessment.rejection_reasons, ())
        self.assertFalse(assessment.writes_model_registry)
        self.assertFalse(assessment.deploys_model)
        self.assertFalse(assessment.changes_runtime_weight)
        self.assertFalse(assessment.creates_order)

    def test_t3_and_license_pending_block_formal_promotion(self) -> None:
        assessment = _formal_assessment(
            tier=DataTrustTier.OPERATIONAL_VERIFIED,
            license_status=ResearchLicenseStatus.PENDING,
        )
        self.assertEqual(assessment.state, DecisionQualityState.BLOCKED)
        self.assertIn("T3_NOT_REACHED", assessment.formal_blockers)
        self.assertIn("LICENSE_PENDING", assessment.formal_blockers)

    def test_calibration_leakage_and_negative_controls_fail_closed(self) -> None:
        assessment = _formal_assessment(
            calibration_verified=False,
            leakage_passed=False,
            negative_controls_passed=False,
        )
        self.assertEqual(assessment.state, DecisionQualityState.BLOCKED)
        self.assertIn("CALIBRATION_NOT_VERIFIED", assessment.formal_blockers)
        self.assertIn("LEAKAGE_AUDIT_FAILED", assessment.formal_blockers)
        self.assertIn("NEGATIVE_CONTROLS_FAILED", assessment.formal_blockers)

    def test_compromised_or_overexposed_holdout_blocks(self) -> None:
        baseline, champion, challenger = _models()
        scoreboard = _formal_scoreboard()
        replay = build_replay_plan(_request())
        holdout = _holdout(state=HoldoutState.COMPROMISED, exposure_count=2)
        evidence = _evidence(scoreboard, replay, holdout)
        assessment = DecisionQualityAssessment(
            evidence=evidence,
            baseline=baseline,
            champion=champion,
            challenger=challenger,
            scoreboard=scoreboard,
            replay_plan=replay,
            holdout=holdout,
        )
        self.assertEqual(assessment.state, DecisionQualityState.BLOCKED)
        self.assertIn("FROZEN_HOLDOUT_COMPROMISED", assessment.formal_blockers)
        self.assertIn("FROZEN_HOLDOUT_OVEREXPOSED", assessment.formal_blockers)

    def test_not_strictly_better_than_champion_is_rejected(self) -> None:
        baseline, champion, _ = _models()
        challenger = replace(
            champion,
            model_id="model-v2",
        )
        scoreboard = _formal_scoreboard()
        replay = build_replay_plan(_request())
        holdout = _holdout()
        evidence = _evidence(scoreboard, replay, holdout)
        assessment = DecisionQualityAssessment(
            evidence=evidence,
            baseline=baseline,
            champion=champion,
            challenger=challenger,
            scoreboard=scoreboard,
            replay_plan=replay,
            holdout=holdout,
        )
        self.assertEqual(
            assessment.state,
            DecisionQualityState.PROMOTION_REJECTED,
        )
        self.assertTrue(
            any(item.startswith("CHAMPION:") for item in assessment.rejection_reasons)
        )

    def test_synthetic_bundle_can_only_be_diagnostic(self) -> None:
        baseline, champion, challenger = _models()
        synthetic_outcomes = (
            _complete_outcome(
                origin=__import__(
                    "stock_tracker.quant.core.outcomes",
                    fromlist=["OutcomeEvidenceOrigin"],
                ).OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
                verified=False,
                synthetic=True,
                signal_suffix="a",
            ),
            _complete_outcome(
                origin=__import__(
                    "stock_tracker.quant.core.outcomes",
                    fromlist=["OutcomeEvidenceOrigin"],
                ).OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
                verified=False,
                synthetic=True,
                signal_suffix="b",
                recorded_offset_days=1,
            ),
        )
        normalized = tuple(
            replace(item, strategy_version="v2", model_id="model-v2")
            for item in synthetic_outcomes
        )
        scoreboard = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v2",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v2",
            evidence_tier=DataTrustTier.BEST_EFFORT,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=normalized,
        )
        replay = build_replay_plan(
            _request(
                purpose=ReplayPurpose.DIAGNOSTIC,
                dependencies=_dependencies(
                    trust_tier=DataTrustTier.BEST_EFFORT,
                    verified=False,
                    complete=False,
                    synthetic=True,
                ),
            )
        )
        holdout = FrozenHoldoutRecord(
            holdout_id="synthetic-holdout",
            config_hash=_hash("b"),
            data_snapshot_id=_hash("d"),
            sealed_at=_BASE,
        )
        evidence = _evidence(
            scoreboard,
            replay,
            holdout,
            tier=DataTrustTier.BEST_EFFORT,
            license_status=ResearchLicenseStatus.PENDING,
            verified=False,
            complete=False,
            synthetic=True,
            calibration_verified=False,
            leakage_passed=False,
            negative_controls_passed=False,
        )
        assessment = DecisionQualityAssessment(
            evidence=evidence,
            baseline=baseline,
            champion=champion,
            challenger=challenger,
            scoreboard=scoreboard,
            replay_plan=replay,
            holdout=holdout,
        )
        self.assertEqual(
            assessment.state,
            DecisionQualityState.CHALLENGER_DIAGNOSTIC,
        )
        self.assertIn("SYNTHETIC_FIXTURE_ONLY", assessment.formal_blockers)
        self.assertFalse(assessment.deploys_model)

    def test_verified_flag_requires_bound_verification_evidence(self) -> None:
        assessment = _formal_assessment()
        with self.assertRaises(DecisionQualityContractError):
            replace(
                assessment.evidence,
                verification_evidence_ids=(),
            )
        with self.assertRaises(DecisionQualityContractError):
            replace(
                assessment.evidence,
                verified=False,
            )

    def test_identity_mismatch_is_structural_blocker(self) -> None:
        assessment = _formal_assessment()
        with self.assertRaises(DecisionQualityContractError):
            replace(assessment.evidence, scoreboard_id="not-a-hash")
        mismatched = replace(
            assessment.evidence,
            scoreboard_id=_hash("9"),
        )
        blocked = replace(assessment, evidence=mismatched)
        self.assertEqual(blocked.state, DecisionQualityState.BLOCKED)
        self.assertIn("SCOREBOARD_ID_MISMATCH", blocked.structural_blockers)

    def test_derived_decisions_and_state_cannot_be_injected(self) -> None:
        assessment = _formal_assessment()
        for change in (
            {"state": DecisionQualityState.BLOCKED},
            {"formal_blockers": ()},
            {"baseline_decision": assessment.baseline_decision},
            {"assessment_id": _hash("f")},
        ):
            with self.subTest(change=change), self.assertRaises(TypeError):
                replace(assessment, **change)


if __name__ == "__main__":
    unittest.main()
