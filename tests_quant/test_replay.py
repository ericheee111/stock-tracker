from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from stock_tracker.core.types import Market
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.research.replay import (
    DIAGNOSTIC_REPLAY_POLICY,
    FORMAL_REPLAY_POLICY,
    FROZEN_HOLDOUT_REPLAY_POLICY,
    ReplayContractError,
    ReplayDataLane,
    ReplayDependency,
    ReplayDependencyKind,
    ReplayExecutionResult,
    ReplayExecutionState,
    ReplayPlanState,
    ReplayPurpose,
    ReplayRequest,
    build_replay_plan,
)

_TARGET = datetime(2026, 6, 30, 7, 0, tzinfo=timezone.utc)
_REQUESTED = _TARGET + timedelta(days=2)
_KIND_CHARACTERS = {
    kind: format(index, "x")
    for index, kind in enumerate(ReplayDependencyKind, start=1)
}


def _hash(character: str) -> str:
    return character * 64


def _dependency(
    kind: ReplayDependencyKind,
    *,
    trust_tier: DataTrustTier = DataTrustTier.RESEARCH_GRADE,
    lane: ReplayDataLane = ReplayDataLane.RESEARCH,
    verified: bool = True,
    complete: bool = True,
    synthetic: bool = False,
    source_name: str = "authoritative_fixture",
    snapshot_as_of: datetime = _TARGET,
    known_at: datetime | None = None,
    created_at: datetime | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> ReplayDependency:
    known = known_at or (_TARGET - timedelta(minutes=1))
    created = created_at or (_TARGET + timedelta(minutes=1))
    provenance = (_hash("f"),) if verified else ()
    return ReplayDependency(
        kind=kind,
        snapshot_id=_hash(_KIND_CHARACTERS[kind]),
        snapshot_as_of=snapshot_as_of,
        known_at=known,
        created_at=created,
        valid_from=valid_from or (_TARGET - timedelta(days=1)),
        valid_to=valid_to or (_TARGET + timedelta(days=1)),
        trust_tier=trust_tier,
        lane=lane,
        verified=verified,
        complete=complete,
        synthetic_fixture_only=synthetic,
        source_name=source_name,
        provenance_ids=provenance,
    )


def _dependencies(
    *,
    trust_tier: DataTrustTier = DataTrustTier.RESEARCH_GRADE,
    lane: ReplayDataLane = ReplayDataLane.RESEARCH,
    verified: bool = True,
    complete: bool = True,
    synthetic: bool = False,
) -> tuple[ReplayDependency, ...]:
    return tuple(
        _dependency(
            kind,
            trust_tier=trust_tier,
            lane=lane,
            verified=verified,
            complete=complete,
            synthetic=synthetic,
        )
        for kind in ReplayDependencyKind
    )


def _request(
    *,
    purpose: ReplayPurpose = ReplayPurpose.FORMAL_DECISION,
    dependencies: tuple[ReplayDependency, ...] | None = None,
    expected: str | None = None,
) -> ReplayRequest:
    policy = {
        ReplayPurpose.DIAGNOSTIC: DIAGNOSTIC_REPLAY_POLICY,
        ReplayPurpose.FORMAL_DECISION: FORMAL_REPLAY_POLICY,
        ReplayPurpose.FROZEN_HOLDOUT: FROZEN_HOLDOUT_REPLAY_POLICY,
    }[purpose]
    if dependencies is None:
        dependencies = _dependencies()
    if expected is None and purpose is not ReplayPurpose.DIAGNOSTIC:
        expected = _hash("0")
    return ReplayRequest(
        target_at=_TARGET,
        requested_at=_REQUESTED,
        purpose=purpose,
        market=Market.A,
        instrument_id="CN:SSE:synthetic-600001",
        identity_fact_id=_hash("e"),
        symbol_at_target="600001.SH",
        expected_decision_snapshot_id=expected,
        policy=policy,
        dependencies=dependencies,
    )


class TestReplayDependency(unittest.TestCase):
    def test_synthetic_dependency_cannot_claim_verified_high_trust(self) -> None:
        with self.assertRaises(ReplayContractError):
            _dependency(
                ReplayDependencyKind.CALENDAR,
                synthetic=True,
                verified=True,
                trust_tier=DataTrustTier.RESEARCH_GRADE,
            )

    def test_free_stockdb_is_frozen_to_incomplete_t1_shadow(self) -> None:
        sidecar = _dependency(
            ReplayDependencyKind.RAW_BAR,
            trust_tier=DataTrustTier.BEST_EFFORT,
            lane=ReplayDataLane.SHADOW,
            verified=False,
            complete=False,
            source_name="free_stockdb",
        )
        self.assertEqual(sidecar.lane, ReplayDataLane.SHADOW)
        with self.assertRaises(ReplayContractError):
            replace(
                sidecar,
                trust_tier=DataTrustTier.RESEARCH_GRADE,
                lane=ReplayDataLane.RESEARCH,
                verified=True,
                complete=True,
                provenance_ids=(_hash("f"),),
            )

    def test_boolean_and_time_contracts_are_strict(self) -> None:
        dependency = _dependency(ReplayDependencyKind.CALENDAR)
        with self.assertRaises(ReplayContractError):
            replace(dependency, complete=1)
        with self.assertRaises(ReplayContractError):
            replace(
                dependency,
                known_at=dependency.snapshot_as_of + timedelta(minutes=1),
            )
        with self.assertRaises(ReplayContractError):
            replace(dependency, created_at=dependency.known_at - timedelta(minutes=1))


class TestReplayPlanning(unittest.TestCase):
    def test_missing_dependency_and_expected_output_block_formal_replay(self) -> None:
        request = ReplayRequest(
            target_at=_TARGET,
            requested_at=_REQUESTED,
            purpose=ReplayPurpose.FORMAL_DECISION,
            market=Market.A,
            instrument_id="CN:SSE:synthetic-600001",
            identity_fact_id=_hash("e"),
            symbol_at_target="600001.SH",
            expected_decision_snapshot_id=None,
            policy=FORMAL_REPLAY_POLICY,
            dependencies=(),
        )
        plan = build_replay_plan(request)
        self.assertEqual(plan.state, ReplayPlanState.BLOCKED)
        self.assertIn("MISSING_DEPENDENCY:EVENT", plan.blockers)
        self.assertIn("EXPECTED_DECISION_SNAPSHOT_MISSING", plan.blockers)

    def test_diagnostic_synthetic_bundle_is_ready_but_not_formal(self) -> None:
        dependencies = _dependencies(
            trust_tier=DataTrustTier.BEST_EFFORT,
            verified=False,
            complete=False,
            synthetic=True,
        )
        plan = build_replay_plan(
            _request(
                purpose=ReplayPurpose.DIAGNOSTIC,
                dependencies=dependencies,
            )
        )
        self.assertEqual(plan.state, ReplayPlanState.READY)
        self.assertFalse(plan.formal_research_eligible)

    def test_same_synthetic_bundle_is_blocked_for_formal_replay(self) -> None:
        dependencies = _dependencies(
            trust_tier=DataTrustTier.BEST_EFFORT,
            verified=False,
            complete=False,
            synthetic=True,
        )
        plan = build_replay_plan(
            _request(
                purpose=ReplayPurpose.FORMAL_DECISION,
                dependencies=dependencies,
            )
        )
        self.assertEqual(plan.state, ReplayPlanState.BLOCKED)
        self.assertIn("SYNTHETIC_DEPENDENCY:CALENDAR", plan.blockers)
        self.assertIn("UNVERIFIED_DEPENDENCY:CALENDAR", plan.blockers)
        self.assertIn("INCOMPLETE_DEPENDENCY:CALENDAR", plan.blockers)
        self.assertIn("TRUST_TIER_INSUFFICIENT:CALENDAR", plan.blockers)

    def test_complete_t3_research_bundle_is_formal_ready(self) -> None:
        plan = build_replay_plan(_request())
        self.assertEqual(plan.state, ReplayPlanState.READY)
        self.assertTrue(plan.formal_research_eligible)
        self.assertEqual(plan.blockers, ())

    def test_runtime_lane_and_sidecar_are_forbidden_for_formal_replay(self) -> None:
        dependencies = list(_dependencies())
        calendar_index = next(
            index
            for index, item in enumerate(dependencies)
            if item.kind is ReplayDependencyKind.CALENDAR
        )
        dependencies[calendar_index] = replace(
            dependencies[calendar_index],
            lane=ReplayDataLane.RUNTIME,
        )
        raw_index = next(
            index
            for index, item in enumerate(dependencies)
            if item.kind is ReplayDependencyKind.RAW_BAR
        )
        dependencies[raw_index] = _dependency(
            ReplayDependencyKind.RAW_BAR,
            trust_tier=DataTrustTier.BEST_EFFORT,
            lane=ReplayDataLane.SHADOW,
            verified=False,
            complete=False,
            source_name="free_stockdb",
        )
        plan = build_replay_plan(
            _request(dependencies=tuple(dependencies))
        )
        self.assertEqual(plan.state, ReplayPlanState.BLOCKED)
        self.assertIn("FORBIDDEN_DATA_LANE:CALENDAR:RUNTIME", plan.blockers)
        self.assertIn(
            "SIDECAR_FORBIDDEN_FOR_FORMAL_REPLAY:RAW_BAR",
            plan.blockers,
        )

    def test_sidecar_can_only_participate_in_diagnostic_replay(self) -> None:
        dependencies = list(
            _dependencies(
                trust_tier=DataTrustTier.BEST_EFFORT,
                verified=False,
                complete=False,
                synthetic=True,
            )
        )
        raw_index = next(
            index
            for index, item in enumerate(dependencies)
            if item.kind is ReplayDependencyKind.RAW_BAR
        )
        dependencies[raw_index] = _dependency(
            ReplayDependencyKind.RAW_BAR,
            trust_tier=DataTrustTier.BEST_EFFORT,
            lane=ReplayDataLane.SHADOW,
            verified=False,
            complete=False,
            synthetic=True,
            source_name="free_stockdb",
        )
        plan = build_replay_plan(
            _request(
                purpose=ReplayPurpose.DIAGNOSTIC,
                dependencies=tuple(dependencies),
            )
        )
        self.assertEqual(plan.state, ReplayPlanState.READY)
        self.assertFalse(plan.formal_research_eligible)

    def test_future_as_of_known_at_and_expired_inputs_are_blocked(self) -> None:
        dependencies = list(_dependencies())
        event_index = next(
            index
            for index, item in enumerate(dependencies)
            if item.kind is ReplayDependencyKind.EVENT
        )
        dependencies[event_index] = _dependency(
            ReplayDependencyKind.EVENT,
            snapshot_as_of=_TARGET + timedelta(days=1),
            known_at=_TARGET + timedelta(hours=1),
            created_at=_TARGET + timedelta(hours=2),
        )
        config_index = next(
            index
            for index, item in enumerate(dependencies)
            if item.kind is ReplayDependencyKind.CONFIG
        )
        dependencies[config_index] = replace(
            dependencies[config_index],
            valid_to=_TARGET - timedelta(minutes=1),
        )
        plan = build_replay_plan(
            _request(dependencies=tuple(dependencies))
        )
        self.assertIn("AS_OF_MISMATCH:EVENT", plan.blockers)
        self.assertIn("FUTURE_KNOWN_AT:EVENT", plan.blockers)
        self.assertIn("EXPIRED_DEPENDENCY:CONFIG", plan.blockers)

    def test_frozen_holdout_requires_frozen_holdout_dependencies(self) -> None:
        research_plan = build_replay_plan(
            _request(purpose=ReplayPurpose.FROZEN_HOLDOUT)
        )
        self.assertEqual(research_plan.state, ReplayPlanState.BLOCKED)
        self.assertIn("TRUST_TIER_INSUFFICIENT:MODEL", research_plan.blockers)

        frozen = _dependencies(trust_tier=DataTrustTier.FROZEN_HOLDOUT)
        frozen_plan = build_replay_plan(
            _request(
                purpose=ReplayPurpose.FROZEN_HOLDOUT,
                dependencies=frozen,
            )
        )
        self.assertEqual(frozen_plan.state, ReplayPlanState.READY)
        self.assertTrue(frozen_plan.formal_research_eligible)

    def test_dependency_input_order_is_normalized_and_deterministic(self) -> None:
        dependencies = _dependencies()
        left = build_replay_plan(_request(dependencies=dependencies))
        right = build_replay_plan(
            _request(dependencies=tuple(reversed(dependencies)))
        )
        self.assertEqual(left.plan_id, right.plan_id)
        self.assertEqual(left.request.dependencies, right.request.dependencies)

    def test_duplicate_dependency_kind_is_rejected(self) -> None:
        dependency = _dependency(ReplayDependencyKind.CALENDAR)
        with self.assertRaises(ReplayContractError):
            _request(dependencies=(dependency, dependency))

    def test_policy_change_changes_plan_identity(self) -> None:
        request = _request()
        base = build_replay_plan(request)
        changed_policy = replace(
            FORMAL_REPLAY_POLICY,
            policy_version="formal-decision-replay-v2",
        )
        changed_request = replace(request, policy=changed_policy)
        changed = build_replay_plan(changed_request)
        self.assertNotEqual(base.plan_id, changed.plan_id)

    def test_plan_derived_fields_cannot_be_relabelled(self) -> None:
        plan = build_replay_plan(_request())
        for changes in (
            {"state": ReplayPlanState.BLOCKED},
            {"blockers": ()},
            {"formal_research_eligible": False},
            {"plan_id": _hash("a")},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(plan, **changes)


class TestReplayExecution(unittest.TestCase):
    def test_blocked_plan_cannot_claim_output_or_dependency_reads(self) -> None:
        plan = build_replay_plan(
            _request(dependencies=())
        )
        blocked = ReplayExecutionResult(
            plan=plan,
            executed_at=_REQUESTED + timedelta(minutes=1),
            executor_version="replay-v1",
            observed_dependency_ids=(),
            output_decision_snapshot_id=None,
            error_code=None,
            production_database_modified=False,
        )
        self.assertEqual(blocked.state, ReplayExecutionState.BLOCKED)
        with self.assertRaises(ReplayContractError):
            replace(
                blocked,
                output_decision_snapshot_id=_hash("0"),
            )

    def test_ready_execution_matches_or_reports_mismatch(self) -> None:
        plan = build_replay_plan(_request())
        observed = tuple(sorted(plan.dependency_ids))
        matching = ReplayExecutionResult(
            plan=plan,
            executed_at=_REQUESTED + timedelta(minutes=1),
            executor_version="replay-v1",
            observed_dependency_ids=observed,
            output_decision_snapshot_id=_hash("0"),
            error_code=None,
            production_database_modified=False,
        )
        self.assertEqual(matching.state, ReplayExecutionState.COMPLETED)
        self.assertTrue(matching.decision_matches_expected)
        self.assertFalse(matching.uses_current_runtime_state)

        mismatch = replace(
            matching,
            output_decision_snapshot_id=_hash("1"),
        )
        self.assertEqual(mismatch.state, ReplayExecutionState.MISMATCH)
        self.assertFalse(mismatch.decision_matches_expected)

    def test_ready_execution_failure_is_explicit(self) -> None:
        plan = build_replay_plan(_request())
        result = ReplayExecutionResult(
            plan=plan,
            executed_at=_REQUESTED + timedelta(minutes=1),
            executor_version="replay-v1",
            observed_dependency_ids=tuple(sorted(plan.dependency_ids)),
            output_decision_snapshot_id=None,
            error_code="DETERMINISTIC_REPLAY_ERROR",
            production_database_modified=False,
        )
        self.assertEqual(result.state, ReplayExecutionState.FAILED)
        self.assertIsNone(result.decision_matches_expected)

    def test_execution_rejects_dependency_drift_and_database_write(self) -> None:
        plan = build_replay_plan(_request())
        with self.assertRaises(ReplayContractError):
            ReplayExecutionResult(
                plan=plan,
                executed_at=_REQUESTED + timedelta(minutes=1),
                executor_version="replay-v1",
                observed_dependency_ids=tuple(sorted(plan.dependency_ids[:-1])),
                output_decision_snapshot_id=_hash("0"),
                error_code=None,
                production_database_modified=False,
            )
        with self.assertRaises(ReplayContractError):
            ReplayExecutionResult(
                plan=plan,
                executed_at=_REQUESTED + timedelta(minutes=1),
                executor_version="replay-v1",
                observed_dependency_ids=tuple(sorted(plan.dependency_ids)),
                output_decision_snapshot_id=_hash("0"),
                error_code=None,
                production_database_modified=True,
            )

    def test_result_derived_fields_cannot_be_injected(self) -> None:
        plan = build_replay_plan(_request())
        result = ReplayExecutionResult(
            plan=plan,
            executed_at=_REQUESTED + timedelta(minutes=1),
            executor_version="replay-v1",
            observed_dependency_ids=tuple(sorted(plan.dependency_ids)),
            output_decision_snapshot_id=_hash("0"),
            error_code=None,
            production_database_modified=False,
        )
        for changes in (
            {"state": ReplayExecutionState.FAILED},
            {"decision_matches_expected": False},
            {"uses_current_runtime_state": True},
            {"result_id": _hash("a")},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(result, **changes)


if __name__ == "__main__":
    unittest.main()
