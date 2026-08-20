from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from stock_tracker.core.types import Market
from stock_tracker.quant.core.outcomes import (
    OutcomeEvidenceOrigin,
    OutcomeTerminalReason,
)
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.evaluation.attribution import (
    AttributionCategory,
    AttributionContractError,
    AttributionState,
    OutcomeAttribution,
    StrategyVersionComparison,
    VersionComparisonState,
)
from tests_quant.test_outcomes import _BASE, _complete_outcome, _hash, _policy


def _scoreboard(
    version: str,
    outcomes: tuple,
    *,
    window_start=None,
    window_end=None,
    as_of=None,
):
    from stock_tracker.quant.core.outcomes import StrategyScoreboard

    normalized = tuple(
        replace(item, strategy_version=version, model_id=f"model-{version}")
        for item in outcomes
    )
    return StrategyScoreboard(
        strategy_id="S1_BREAKOUT",
        strategy_version=version,
        market=Market.A,
        horizon_sessions=20,
        model_id=f"model-{version}",
        evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
        window_start=_BASE if window_start is None else window_start,
        window_end=(
            _BASE + timedelta(days=9) if window_end is None else window_end
        ),
        as_of=_BASE + timedelta(days=10) if as_of is None else as_of,
        policy=_policy(minimum=2),
        outcomes=normalized,
    )


class TestOutcomeAttribution(unittest.TestCase):
    def test_formal_outcome_is_descriptively_attributed(self) -> None:
        attribution = OutcomeAttribution(_complete_outcome())
        self.assertEqual(attribution.state, AttributionState.FORMAL_READY)
        categories = {item.category for item in attribution.findings}
        self.assertIn(AttributionCategory.TARGET_CAPTURED, categories)
        self.assertIn(AttributionCategory.COST_DRAG, categories)
        self.assertEqual(attribution.blockers, ())

    def test_synthetic_outcome_remains_diagnostic_only(self) -> None:
        outcome = _complete_outcome(
            origin=OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
            verified=False,
            synthetic=True,
        )
        attribution = OutcomeAttribution(outcome)
        self.assertEqual(attribution.state, AttributionState.DIAGNOSTIC_ONLY)
        self.assertIn("OUTCOME_NOT_FORMAL_SCOREBOARD_ELIGIBLE", attribution.blockers)
        self.assertIn("ORIGIN:SYNTHETIC_FIXTURE", attribution.blockers)

    def test_manual_exit_with_large_capture_gap_is_flagged(self) -> None:
        outcome = replace(
            _complete_outcome(exit_price="10"),
            terminal_reason=OutcomeTerminalReason.MANUAL,
        )
        attribution = OutcomeAttribution(outcome)
        categories = {item.category for item in attribution.findings}
        self.assertIn(AttributionCategory.MANUAL_EXIT, categories)
        self.assertIn(AttributionCategory.EARLY_EXIT_OPPORTUNITY_COST, categories)

    def test_open_outcome_cannot_receive_terminal_attribution(self) -> None:
        from tests_quant.test_outcomes import _open_outcome

        with self.assertRaises(AttributionContractError):
            OutcomeAttribution(_open_outcome())

    def test_derived_fields_cannot_be_injected(self) -> None:
        attribution = OutcomeAttribution(_complete_outcome())
        for change in (
            {"findings": ()},
            {"blockers": ()},
            {"state": AttributionState.DIAGNOSTIC_ONLY},
            {"attribution_id": _hash("f")},
        ):
            with self.subTest(change=change), self.assertRaises(TypeError):
                replace(attribution, **change)


class TestStrategyVersionComparison(unittest.TestCase):
    def test_same_cohort_better_candidate_is_reviewable_not_deployed(self) -> None:
        baseline = _scoreboard(
            "v1",
            (
                _complete_outcome(exit_price="11", signal_suffix="a"),
                _complete_outcome(
                    exit_price="9",
                    signal_suffix="b",
                    recorded_offset_days=1,
                ),
            ),
        )
        candidate = _scoreboard(
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
        comparison = StrategyVersionComparison(baseline, candidate)
        self.assertEqual(comparison.state, VersionComparisonState.CANDIDATE_BETTER)
        self.assertGreaterEqual(comparison.average_r_delta, Decimal("0.1"))
        self.assertFalse(comparison.changes_runtime_weight)
        self.assertFalse(comparison.deploys_model)

    def test_cohort_window_and_evidence_mismatch_block_comparison(self) -> None:
        outcomes = (
            _complete_outcome(signal_suffix="a"),
            _complete_outcome(signal_suffix="b", recorded_offset_days=1),
        )
        baseline = _scoreboard("v1", outcomes)
        candidate = _scoreboard(
            "v2",
            (
                _complete_outcome(signal_suffix="a"),
                _complete_outcome(signal_suffix="c", recorded_offset_days=1),
            ),
        )
        comparison = StrategyVersionComparison(baseline, candidate)
        self.assertEqual(comparison.state, VersionComparisonState.BLOCKED)
        self.assertIn("COHORT_ID_MISMATCH", comparison.blockers)
        self.assertIsNone(comparison.average_r_delta)

    def test_as_of_mismatch_blocks_version_comparison(self) -> None:
        outcomes = (
            _complete_outcome(signal_suffix="a"),
            _complete_outcome(signal_suffix="b", recorded_offset_days=1),
        )
        baseline = _scoreboard("v1", outcomes)
        candidate = _scoreboard(
            "v2",
            outcomes,
            as_of=_BASE + timedelta(days=11),
        )
        comparison = StrategyVersionComparison(baseline, candidate)
        self.assertEqual(comparison.state, VersionComparisonState.BLOCKED)
        self.assertIn("AS_OF_MISMATCH", comparison.blockers)
        self.assertIsNone(comparison.average_r_delta)

    def test_insufficient_real_evidence_blocks_version_claim(self) -> None:
        from stock_tracker.quant.core.outcomes import StrategyScoreboard

        outcome = _complete_outcome(signal_suffix="only")
        baseline = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(outcome,),
        )
        candidate = replace(
            baseline,
            strategy_version="v2",
            model_id="model-v2",
            outcomes=(
                replace(outcome, strategy_version="v2", model_id="model-v2"),
            ),
        )
        comparison = StrategyVersionComparison(baseline, candidate)
        self.assertEqual(comparison.state, VersionComparisonState.BLOCKED)
        self.assertIn("BASELINE_REAL_EVIDENCE_UNAVAILABLE", comparison.blockers)
        self.assertIn("CANDIDATE_REAL_EVIDENCE_UNAVAILABLE", comparison.blockers)

    def test_comparison_identity_and_state_cannot_be_relabelled(self) -> None:
        baseline = _scoreboard(
            "v1",
            (
                _complete_outcome(signal_suffix="a"),
                _complete_outcome(signal_suffix="b", recorded_offset_days=1),
            ),
        )
        candidate = _scoreboard(
            "v2",
            (
                _complete_outcome(exit_price="12.5", signal_suffix="a"),
                _complete_outcome(
                    exit_price="12",
                    signal_suffix="b",
                    recorded_offset_days=1,
                ),
            ),
        )
        comparison = StrategyVersionComparison(baseline, candidate)
        for change in (
            {"state": VersionComparisonState.BLOCKED},
            {"blockers": ()},
            {"average_r_delta": Decimal(100)},
            {"comparison_id": _hash("f")},
        ):
            with self.subTest(change=change), self.assertRaises(TypeError):
                replace(comparison, **change)


if __name__ == "__main__":
    unittest.main()
