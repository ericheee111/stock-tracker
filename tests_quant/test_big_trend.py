from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal

from _helpers import utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.big_trend import (
    DEFAULT_BIG_TREND_POLICY,
    BigTrendActionability,
    BigTrendContractError,
    BigTrendEvidenceFamily,
    BigTrendEvidencePoint,
    BigTrendInputSnapshot,
    BigTrendScope,
    BigTrendState,
    assess_big_trend,
    build_big_trend_transition,
)

AS_OF = utc_datetime(2025, 2, 1)
SESSION = date(2025, 1, 31)
SNAPSHOT_ID = "a" * 64


class BigTrendFixtures(unittest.TestCase):
    def point(
        self,
        family: BigTrendEvidenceFamily,
        score: str | Decimal,
        *,
        observed_at=AS_OF,
        source_snapshot_id: str = SNAPSHOT_ID,
    ) -> BigTrendEvidencePoint:
        value = score if isinstance(score, Decimal) else Decimal(score)
        return BigTrendEvidencePoint(
            family=family,
            score=value,
            observed_at=observed_at,
            source_snapshot_id=source_snapshot_id,
            note=f"synthetic {family.value.lower()} evidence",
        )

    def evidence(self, values: dict[BigTrendEvidenceFamily, str | Decimal]):
        return tuple(
            sorted(
                (self.point(family, score) for family, score in values.items()),
                key=lambda item: (item.family.value, item.evidence_id),
            )
        )

    def strong_values(self) -> dict[BigTrendEvidenceFamily, str]:
        return {
            BigTrendEvidenceFamily.SECTOR_RS_PERSISTENCE: "0.82",
            BigTrendEvidenceFamily.BREADTH_EXPANSION: "0.76",
            BigTrendEvidenceFamily.TURNOVER_SHARE_TREND: "0.72",
            BigTrendEvidenceFamily.LEADER_STRENGTH: "0.78",
            BigTrendEvidenceFamily.CORE_STABILITY: "0.70",
            BigTrendEvidenceFamily.TREND_QUALITY: "0.80",
            BigTrendEvidenceFamily.BREAKOUT_RETENTION: "0.74",
            BigTrendEvidenceFamily.REGIME_FIT: "0.68",
        }

    def snapshot(
        self,
        values: dict[BigTrendEvidenceFamily, str | Decimal] | None = None,
        *,
        scope: BigTrendScope = BigTrendScope.SECTOR,
        as_of=AS_OF,
        data_quality_blockers: tuple[str, ...] = (),
        tradability_blockers: tuple[str, ...] = (),
        event_snapshot_id: str | None = "b" * 64,
    ) -> BigTrendInputSnapshot:
        if scope is BigTrendScope.SECTOR:
            entity_id = "CAPCO-INDUSTRY:C39"
            identity_fact_id = None
            taxonomy_id = "CAPCO-INDUSTRY"
            classification_id = "C39"
        else:
            entity_id = "CN:SSE:SSECID-ALPHA"
            identity_fact_id = "c" * 64
            taxonomy_id = None
            classification_id = None
        return BigTrendInputSnapshot(
            scope=scope,
            entity_id=entity_id,
            market=Market.A,
            session_date=SESSION,
            as_of=as_of,
            identity_fact_id=identity_fact_id,
            taxonomy_id=taxonomy_id,
            classification_id=classification_id,
            calendar_snapshot_id="d" * 64,
            universe_snapshot_id="e" * 64,
            classification_snapshot_id="f" * 64,
            raw_bar_snapshot_id="1" * 64,
            feature_snapshot_id="2" * 64,
            regime_snapshot_id="3" * 64,
            event_snapshot_id=event_snapshot_id,
            evidence=self.evidence(values or {}),
            data_quality_blockers=data_quality_blockers,
            tradability_blockers=tradability_blockers,
        )


class TestBigTrendStateSemantics(BigTrendFixtures):
    def test_strong_independent_evidence_reaches_trending(self) -> None:
        assessment = assess_big_trend(self.snapshot(self.strong_values()))
        self.assertEqual(assessment.state, BigTrendState.TRENDING)
        self.assertEqual(
            assessment.actionability,
            BigTrendActionability.PLAN_ELIGIBLE,
        )
        self.assertGreaterEqual(assessment.supportive_family_count, 7)
        self.assertGreaterEqual(assessment.supportive_group_count, 2)

    def test_confirming_and_emerging_have_distinct_actionability(self) -> None:
        confirming = self.snapshot(
            {
                BigTrendEvidenceFamily.SECTOR_RS_PERSISTENCE: "0.58",
                BigTrendEvidenceFamily.BREADTH_EXPANSION: "0.55",
                BigTrendEvidenceFamily.LEADER_STRENGTH: "0.57",
                BigTrendEvidenceFamily.TREND_QUALITY: "0.56",
                BigTrendEvidenceFamily.BREAKOUT_RETENTION: "0.54",
            }
        )
        confirming_assessment = assess_big_trend(confirming)
        self.assertEqual(confirming_assessment.state, BigTrendState.CONFIRMING)
        self.assertEqual(
            confirming_assessment.actionability,
            BigTrendActionability.PLAN_ELIGIBLE,
        )

        emerging = self.snapshot(
            {
                BigTrendEvidenceFamily.SECTOR_RS_PERSISTENCE: "0.42",
                BigTrendEvidenceFamily.BREADTH_EXPANSION: "0.40",
                BigTrendEvidenceFamily.TURNOVER_SHARE_TREND: "0.38",
            }
        )
        emerging_assessment = assess_big_trend(emerging)
        self.assertEqual(emerging_assessment.state, BigTrendState.EMERGING)
        self.assertEqual(
            emerging_assessment.actionability,
            BigTrendActionability.WATCH_ONLY,
        )

    def test_breakout_or_event_alone_cannot_claim_big_trend(self) -> None:
        for family in (
            BigTrendEvidenceFamily.BREAKOUT_RETENTION,
            BigTrendEvidenceFamily.CATALYST_CONFIRMATION,
        ):
            with self.subTest(family=family):
                assessment = assess_big_trend(
                    self.snapshot({family: "0.95"})
                )
                self.assertEqual(assessment.state, BigTrendState.NONE)
                self.assertEqual(
                    assessment.actionability,
                    BigTrendActionability.NO_ACTION,
                )

    def test_mature_distribution_and_broken_override_positive_score(self) -> None:
        mature_values = self.strong_values()
        mature_values[BigTrendEvidenceFamily.CROWDING_ACCELERATION] = "0.80"
        mature = assess_big_trend(self.snapshot(mature_values))
        self.assertEqual(mature.state, BigTrendState.MATURE)
        self.assertEqual(
            mature.actionability,
            BigTrendActionability.HOLD_NO_CHASE,
        )

        distribution_values = self.strong_values()
        distribution_values[
            BigTrendEvidenceFamily.DISTRIBUTION_DIVERGENCE
        ] = "0.78"
        distributing = assess_big_trend(self.snapshot(distribution_values))
        self.assertEqual(distributing.state, BigTrendState.DISTRIBUTING)
        self.assertEqual(
            distributing.actionability,
            BigTrendActionability.WARNING_TRIM,
        )

        broken_values = self.strong_values()
        broken_values[BigTrendEvidenceFamily.TREND_QUALITY] = "-0.80"
        broken_values[BigTrendEvidenceFamily.BREAKOUT_RETENTION] = "-0.70"
        broken = assess_big_trend(self.snapshot(broken_values))
        self.assertEqual(broken.state, BigTrendState.BROKEN)
        self.assertEqual(
            broken.actionability,
            BigTrendActionability.CLOSE_RUNNER,
        )

    def test_data_or_tradability_blocker_is_fail_closed(self) -> None:
        assessment = assess_big_trend(
            self.snapshot(
                self.strong_values(),
                data_quality_blockers=("MISSING_CLASSIFICATION_COVERAGE",),
                tradability_blockers=("SUSPENDED",),
            )
        )
        self.assertEqual(assessment.state, BigTrendState.NONE)
        self.assertEqual(
            assessment.actionability,
            BigTrendActionability.DATA_BLOCKED,
        )
        self.assertTrue(
            all(reason.startswith("BLOCKED:") for reason in assessment.reasons)
        )


class TestBigTrendEvidenceContract(BigTrendFixtures):
    def test_scores_are_exact_decimal_and_bounded(self) -> None:
        for value in (0.5, 1, True, Decimal("NaN"), Decimal("1.1")):
            with self.subTest(value=value), self.assertRaises(
                BigTrendContractError
            ):
                BigTrendEvidencePoint(
                    family=BigTrendEvidenceFamily.TREND_QUALITY,
                    score=value,
                    observed_at=AS_OF,
                    source_snapshot_id=SNAPSHOT_ID,
                    note="invalid score",
                )

    def test_future_duplicate_and_unordered_evidence_fail_closed(self) -> None:
        future = self.point(
            BigTrendEvidenceFamily.TREND_QUALITY,
            "0.7",
            observed_at=utc_datetime(2025, 3, 1),
        )
        with self.assertRaisesRegex(BigTrendContractError, "future evidence"):
            replace(self.snapshot(), evidence=(future,))

        first = self.point(BigTrendEvidenceFamily.TREND_QUALITY, "0.7")
        duplicate = self.point(
            BigTrendEvidenceFamily.TREND_QUALITY,
            "0.8",
            source_snapshot_id="4" * 64,
        )
        with self.assertRaisesRegex(BigTrendContractError, "one evidence point"):
            replace(
                self.snapshot(),
                evidence=tuple(
                    sorted(
                        (first, duplicate),
                        key=lambda item: (item.family.value, item.evidence_id),
                    )
                ),
            )

        participation = self.point(
            BigTrendEvidenceFamily.SECTOR_RS_PERSISTENCE,
            "0.7",
        )
        with self.assertRaisesRegex(BigTrendContractError, "sorted"):
            replace(self.snapshot(), evidence=(first, participation))

    def test_snapshot_binds_all_upstream_identities(self) -> None:
        snapshot = self.snapshot(self.strong_values())
        changed = replace(snapshot, feature_snapshot_id="9" * 64)
        self.assertNotEqual(snapshot.snapshot_id, changed.snapshot_id)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(snapshot, snapshot_id="8" * 64)
        with self.assertRaises(BigTrendContractError):
            replace(snapshot, calendar_snapshot_id="not-a-sha")

    def test_sector_and_instrument_identity_boundaries_are_distinct(self) -> None:
        sector = self.snapshot()
        instrument = self.snapshot(scope=BigTrendScope.INSTRUMENT)
        self.assertNotEqual(sector.snapshot_id, instrument.snapshot_id)
        with self.assertRaisesRegex(BigTrendContractError, "sector entity_id"):
            replace(sector, entity_id="C39")
        with self.assertRaisesRegex(BigTrendContractError, "identity_fact_id"):
            replace(instrument, identity_fact_id=None)

    def test_input_order_is_explicit_not_silently_normalized(self) -> None:
        values = self.strong_values()
        ordered = self.evidence(values)
        self.assertEqual(
            self.snapshot(values).snapshot_id,
            replace(self.snapshot(), evidence=ordered).snapshot_id,
        )
        with self.assertRaisesRegex(BigTrendContractError, "sorted"):
            replace(self.snapshot(), evidence=tuple(reversed(ordered)))


class TestBigTrendPolicyAndTransitions(BigTrendFixtures):
    def test_policy_version_changes_assessment_identity(self) -> None:
        snapshot = self.snapshot(self.strong_values())
        first = assess_big_trend(snapshot)
        changed_policy = replace(
            DEFAULT_BIG_TREND_POLICY,
            policy_version="big-trend-v1.1",
        )
        second = assess_big_trend(snapshot, changed_policy)
        self.assertNotEqual(first.policy_id, second.policy_id)
        self.assertNotEqual(first.assessment_id, second.assessment_id)

    def test_derived_assessment_identity_cannot_be_relabelled(self) -> None:
        assessment = assess_big_trend(self.snapshot(self.strong_values()))
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(assessment, assessment_id="5" * 64)
        with self.assertRaisesRegex(BigTrendContractError, "inconsistent"):
            replace(
                assessment,
                actionability=BigTrendActionability.HOLD_NO_CHASE,
            )

    def test_transition_reason_is_bound_and_hard_break_is_explicit(self) -> None:
        previous = assess_big_trend(self.snapshot(self.strong_values()))
        broken_values = self.strong_values()
        broken_values[BigTrendEvidenceFamily.TREND_QUALITY] = "-0.80"
        broken_values[BigTrendEvidenceFamily.BREAKOUT_RETENTION] = "-0.70"
        current = assess_big_trend(self.snapshot(broken_values))
        transition = build_big_trend_transition(previous, current)
        self.assertEqual(transition.transition_reason, "HARD_STRUCTURE_BREAK")
        self.assertEqual(transition.current_state, BigTrendState.BROKEN)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(transition, transition_id="6" * 64)

    def test_output_has_no_probability_order_or_performance_surface(self) -> None:
        assessment = assess_big_trend(self.snapshot(self.strong_values()))
        for forbidden in (
            "probability",
            "success_probability",
            "order",
            "quantity",
            "position_size",
            "win_rate",
            "profit_factor",
            "expected_return",
        ):
            self.assertFalse(hasattr(assessment, forbidden))


if __name__ == "__main__":
    unittest.main()
