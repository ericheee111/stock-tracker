from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.evaluation.shadow_lifecycle import (
    ShadowEvidenceState,
    ShadowLifecycleContractError,
    ShadowLifecyclePolicy,
    ShadowValidationEvidence,
    StrategyLifecycleAssessment,
    StrategyLifecycleState,
)
from tests_quant.test_attribution import _scoreboard
from tests_quant.test_decision_quality import _formal_assessment
from tests_quant.test_outcomes import _BASE, _complete_outcome, _hash


def _scoreboards(
    *,
    recent_exit_prices: tuple[str, str] = ("12", "10.5"),
):
    long_term = _scoreboard(
        "v2",
        (
            _complete_outcome(exit_price="12", signal_suffix="a"),
            _complete_outcome(
                exit_price="10.5",
                signal_suffix="b",
                recorded_offset_days=1,
            ),
        ),
        window_start=_BASE - timedelta(days=30),
    )
    recent = _scoreboard(
        "v2",
        (
            _complete_outcome(exit_price=recent_exit_prices[0], signal_suffix="c"),
            _complete_outcome(
                exit_price=recent_exit_prices[1],
                signal_suffix="d",
                recorded_offset_days=1,
            ),
        ),
    )
    return long_term, recent


def _policy() -> ShadowLifecyclePolicy:
    return ShadowLifecyclePolicy(
        policy_version="test-shadow-lifecycle-v1",
        minimum_shadow_samples=2,
    )


def _evidence(
    decision_quality,
    long_term,
    recent,
    *,
    calibration_ece_delta: Decimal = Decimal("0.00"),
    max_drawdown_delta_r: Decimal = Decimal("0.00"),
    out_of_sample: bool = True,
    production_weight_zero: bool = True,
    used_frozen_holdout: bool = False,
    orders_created: bool = False,
    verified: bool = True,
    complete: bool = True,
    synthetic: bool = False,
    tier: DataTrustTier | None = None,
) -> ShadowValidationEvidence:
    resolved_tier = tier or (
        DataTrustTier.BEST_EFFORT
        if synthetic
        else DataTrustTier.OPERATIONAL_VERIFIED
    )
    sample_count = 0 if recent.metrics is None else recent.metrics.sample_count
    return ShadowValidationEvidence(
        strategy_id=long_term.strategy_id,
        strategy_version=long_term.strategy_version,
        model_id=long_term.model_id or "model-v2",
        market=long_term.market,
        horizon_sessions=long_term.horizon_sessions,
        decision_quality_assessment_id=decision_quality.assessment_id,
        long_scoreboard_id=long_term.scoreboard_id,
        recent_scoreboard_id=recent.scoreboard_id,
        shadow_snapshot_id=_hash("7"),
        shadow_run_id=_hash("8"),
        data_trust_tier=resolved_tier,
        sample_count=sample_count,
        calibration_ece_delta=calibration_ece_delta,
        regime_expectancy_range=Decimal("0.10"),
        max_drawdown_delta_r=max_drawdown_delta_r,
        out_of_sample=out_of_sample,
        production_weight_zero=production_weight_zero,
        used_frozen_holdout=used_frozen_holdout,
        orders_created=orders_created,
        verified=verified,
        complete=complete,
        synthetic_fixture_only=synthetic,
        verification_evidence_ids=(_hash("9"),) if verified else (),
    )


def _assessment(
    current: StrategyLifecycleState,
    *,
    recent_exit_prices: tuple[str, str] = ("12", "10.5"),
    consecutive_blocked_windows: int = 0,
    **evidence_changes,
) -> StrategyLifecycleAssessment:
    decision_quality = _formal_assessment()
    long_term, recent = _scoreboards(recent_exit_prices=recent_exit_prices)
    evidence = _evidence(
        decision_quality,
        long_term,
        recent,
        **evidence_changes,
    )
    return StrategyLifecycleAssessment(
        current_state=current,
        decision_quality=decision_quality,
        long_term_scoreboard=long_term,
        recent_scoreboard=recent,
        shadow_evidence=evidence,
        consecutive_blocked_windows=consecutive_blocked_windows,
        policy=_policy(),
    )


class TestShadowValidationEvidence(unittest.TestCase):
    def test_new_out_of_sample_zero_weight_evidence_is_formal_ready(self) -> None:
        decision_quality = _formal_assessment()
        long_term, recent = _scoreboards()
        evidence = _evidence(decision_quality, long_term, recent)
        self.assertEqual(evidence.state, ShadowEvidenceState.FORMAL_READY)
        self.assertEqual(evidence.blockers, ())

    def test_holdout_reuse_orders_and_production_weight_fail_closed(self) -> None:
        decision_quality = _formal_assessment()
        long_term, recent = _scoreboards()
        evidence = _evidence(
            decision_quality,
            long_term,
            recent,
            production_weight_zero=False,
            used_frozen_holdout=True,
            orders_created=True,
        )
        self.assertEqual(evidence.state, ShadowEvidenceState.BLOCKED)
        self.assertIn("FROZEN_HOLDOUT_REUSED_AS_SHADOW", evidence.blockers)
        self.assertIn("SHADOW_CREATED_ORDERS", evidence.blockers)
        self.assertIn("SHADOW_PRODUCTION_WEIGHT_NOT_ZERO", evidence.blockers)

    def test_synthetic_shadow_stays_diagnostic(self) -> None:
        decision_quality = _formal_assessment()
        long_term, recent = _scoreboards()
        evidence = _evidence(
            decision_quality,
            long_term,
            recent,
            verified=False,
            complete=False,
            synthetic=True,
        )
        self.assertEqual(evidence.state, ShadowEvidenceState.DIAGNOSTIC_ONLY)
        self.assertIn("SHADOW_EVIDENCE_NOT_VERIFIED", evidence.blockers)

    def test_verified_shadow_requires_bound_verification_evidence(self) -> None:
        decision_quality = _formal_assessment()
        long_term, recent = _scoreboards()
        evidence = _evidence(decision_quality, long_term, recent)
        with self.assertRaises(ShadowLifecycleContractError):
            replace(evidence, verification_evidence_ids=())
        with self.assertRaises(ShadowLifecycleContractError):
            replace(evidence, verified=False)

    def test_synthetic_cannot_self_promote_to_high_trust(self) -> None:
        decision_quality = _formal_assessment()
        long_term, recent = _scoreboards()
        with self.assertRaises(ShadowLifecycleContractError):
            _evidence(
                decision_quality,
                long_term,
                recent,
                verified=True,
                synthetic=True,
                tier=DataTrustTier.RESEARCH_GRADE,
            )


class TestStrategyLifecycleAssessment(unittest.TestCase):
    def test_healthy_formal_shadow_recommends_active_without_mutation(self) -> None:
        assessment = _assessment(StrategyLifecycleState.SHADOW)
        self.assertEqual(
            assessment.recommended_state,
            StrategyLifecycleState.ACTIVE,
        )
        self.assertFalse(assessment.changes_runtime_state)
        self.assertFalse(assessment.changes_runtime_weight)
        self.assertFalse(assessment.deploys_model)
        self.assertFalse(assessment.creates_order)

    def test_weak_negative_and_severe_expectancy_map_to_lifecycle_states(self) -> None:
        watch = _assessment(
            StrategyLifecycleState.ACTIVE,
            recent_exit_prices=("10.1", "10.1"),
        )
        self.assertEqual(watch.recommended_state, StrategyLifecycleState.WATCH)

        downweighted = _assessment(
            StrategyLifecycleState.ACTIVE,
            recent_exit_prices=("10.0", "10.0"),
        )
        self.assertEqual(
            downweighted.recommended_state,
            StrategyLifecycleState.DOWNWEIGHTED,
        )

        blocked = _assessment(
            StrategyLifecycleState.ACTIVE,
            recent_exit_prices=("9.8", "9.8"),
        )
        self.assertEqual(
            blocked.recommended_state,
            StrategyLifecycleState.BLOCKED,
        )

    def test_calibration_or_drawdown_regression_blocks(self) -> None:
        calibration = _assessment(
            StrategyLifecycleState.ACTIVE,
            calibration_ece_delta=Decimal("0.04"),
        )
        self.assertEqual(
            calibration.recommended_state,
            StrategyLifecycleState.BLOCKED,
        )
        self.assertIn("CALIBRATION_REGRESSED", calibration.reasons)

        drawdown = _assessment(
            StrategyLifecycleState.ACTIVE,
            max_drawdown_delta_r=Decimal("0.60"),
        )
        self.assertEqual(
            drawdown.recommended_state,
            StrategyLifecycleState.BLOCKED,
        )
        self.assertIn("MAX_DRAWDOWN_REGRESSED", drawdown.reasons)

    def test_repeated_severe_blocked_windows_recommend_retirement(self) -> None:
        assessment = _assessment(
            StrategyLifecycleState.BLOCKED,
            recent_exit_prices=("9.8", "9.8"),
            consecutive_blocked_windows=3,
        )
        self.assertEqual(
            assessment.recommended_state,
            StrategyLifecycleState.RETIRED,
        )
        self.assertIn("REPEATED_BLOCKED_WINDOWS", assessment.reasons)

    def test_recovered_blocked_strategy_is_not_retired_automatically(self) -> None:
        assessment = _assessment(
            StrategyLifecycleState.BLOCKED,
            consecutive_blocked_windows=3,
        )
        self.assertEqual(
            assessment.recommended_state,
            StrategyLifecycleState.ACTIVE,
        )

    def test_retired_state_is_terminal(self) -> None:
        assessment = _assessment(StrategyLifecycleState.RETIRED)
        self.assertEqual(
            assessment.recommended_state,
            StrategyLifecycleState.RETIRED,
        )
        self.assertIn("RETIRED_STATE_IS_TERMINAL", assessment.reasons)

    def test_synthetic_shadow_never_recommends_active(self) -> None:
        assessment = _assessment(
            StrategyLifecycleState.SHADOW,
            verified=False,
            complete=False,
            synthetic=True,
        )
        self.assertEqual(
            assessment.recommended_state,
            StrategyLifecycleState.SHADOW,
        )
        self.assertIn("SYNTHETIC_SHADOW_DIAGNOSTIC_ONLY", assessment.reasons)

    def test_window_as_of_and_policy_mismatch_block_lifecycle_comparison(self) -> None:
        assessment = _assessment(StrategyLifecycleState.SHADOW)
        recent = replace(
            assessment.recent_scoreboard,
            window_start=assessment.long_term_scoreboard.window_start,
            as_of=assessment.recent_scoreboard.as_of + timedelta(days=1),
            policy=replace(
                assessment.recent_scoreboard.policy,
                policy_version="different-scoreboard-policy",
            ),
        )
        evidence = replace(
            assessment.shadow_evidence,
            recent_scoreboard_id=recent.scoreboard_id,
        )
        blocked = replace(
            assessment,
            recent_scoreboard=recent,
            shadow_evidence=evidence,
        )
        self.assertEqual(
            blocked.recommended_state,
            StrategyLifecycleState.BLOCKED,
        )
        self.assertIn("SCOREBOARD_POLICY_MISMATCH", blocked.structural_blockers)
        self.assertIn("SCOREBOARD_AS_OF_MISMATCH", blocked.structural_blockers)
        self.assertIn(
            "RECENT_WINDOW_NOT_STRICT_SUBWINDOW",
            blocked.structural_blockers,
        )

    def test_identity_mismatch_blocks_and_derived_state_is_immutable(self) -> None:
        assessment = _assessment(StrategyLifecycleState.SHADOW)
        mismatched_evidence = replace(
            assessment.shadow_evidence,
            long_scoreboard_id=_hash("9"),
        )
        blocked = replace(assessment, shadow_evidence=mismatched_evidence)
        self.assertEqual(
            blocked.recommended_state,
            StrategyLifecycleState.BLOCKED,
        )
        self.assertIn("LONG_SCOREBOARD_ID_MISMATCH", blocked.structural_blockers)
        for change in (
            {"recommended_state": StrategyLifecycleState.ACTIVE},
            {"reasons": ()},
            {"structural_blockers": ()},
            {"assessment_id": _hash("f")},
        ):
            with self.subTest(change=change), self.assertRaises(TypeError):
                replace(assessment, **change)


if __name__ == "__main__":
    unittest.main()
