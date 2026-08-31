from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from stock_tracker.core import types as T
from stock_tracker.quant.core.outcomes import (
    OutcomeEvidenceOrigin,
    OutcomeTerminalReason,
)
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.storage import outcome_collection as collection_module
from stock_tracker.quant.storage import outcome_ledger as ledger_module
from stock_tracker.quant.storage.outcome_collection import (
    OutcomeCollectionCaseState,
    OutcomeCollectionConflict,
    OutcomeCollectionDisposition,
    OutcomeCollectionError,
    OutcomeCollectionMode,
    OutcomeCollectionService,
    RuntimeSignalSnapshot,
)
from stock_tracker.quant.storage.outcome_ledger import (
    OutcomeLedger,
    OutcomeLedgerDisposition,
    OutcomeLedgerLane,
)

_BASE = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return character * 64


def _runtime_signal(
    *,
    suffix: str = "one",
    state: T.SignalState = T.SignalState.TRIGGERED,
    data_status: T.DataStatus = T.DataStatus.LIVE,
    aware_state_time: bool = True,
) -> T.Signal:
    changed = _BASE if aware_state_time else _BASE.replace(tzinfo=None)
    return T.Signal(
        signal_id=f"600001.SH:S1_BREAKOUT:{suffix}",
        symbol="600001.SH",
        market=T.Market.A,
        strategy_id="S1_BREAKOUT",
        state=state,
        state_changed_at=changed,
        previous_state=T.SignalState.ARMED_BREAKOUT,
        reason="结构与风险门已形成可审计候选",
        entry_low=10.0,
        entry_high=10.5,
        trigger_price=10.2,
        invalidation_price=9.1,
        target_1=11.0,
        target_2=12.0,
        reward_risk=2.0,
        freshness=0.95,
        market_regime="RISK_ON_TREND",
        sector_stage="LEADING",
        next_trigger="等待可执行成交事实",
        what_changed=["状态升级"],
        data_status=data_status,
        scores=T.ScoreSet(
            opportunity=80,
            timing=75,
            risk=30,
            confidence=70,
            success_probability=None,
            positive_reasons=["结构完整"],
            negative_reasons=["尚无成交事实"],
        ),
    )


def _open_kwargs() -> dict:
    return {
        "runtime_episode_fact_id": _hash("d"),
        "entry_requested_at": _BASE,
        "strategy_version": "v1",
        "horizon_sessions": 20,
        "model_id": "model-v1",
        "instrument_id": "CN:SSE:600001",
        "identity_fact_id": _hash("1"),
        "data_snapshot_id": _hash("2"),
        "policy_id": _hash("3"),
        "classification_id": "C:SECTOR:TECH",
        "requested_quantity": 100,
        "minimum_exit_session_offset": 1,
        "entry_execution_policy_id": _hash("4"),
        "recorded_by": "stage4g-test",
    }


class OutcomeCollectionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).absolute()
        self.production = self.root / "production.db"
        self.production.write_bytes(b"production-sentinel")
        self.collection_path = self.root / "outcome-collection.db"
        self.collection = OutcomeCollectionService(
            self.collection_path,
            production_database=self.production,
        )
        self.ledger = OutcomeLedger(
            self.root / "outcome-records",
            self.root / "outcome-ledger.db",
            production_database=self.production,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def open_case(
        self,
        *,
        signal: T.Signal | None = None,
        mode: OutcomeCollectionMode = OutcomeCollectionMode.PAPER,
        observed_at: datetime = _BASE,
        **overrides,
    ):
        kwargs = {**_open_kwargs(), **overrides}
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.open_case(
                signal or _runtime_signal(),
                mode=mode,
                **kwargs,
            )

    def record_entry(
        self,
        case_id: str,
        *,
        observed_at: datetime = _BASE + timedelta(minutes=1),
        evidence_ids=(),
        fill_price: str = "10",
        reference_price: str = "10",
        quantity: int = 100,
    ):
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_entry_fill(
                case_id,
                timestamp=_BASE + timedelta(minutes=1),
                session_index=10,
                quantity=quantity,
                reference_price=Decimal(reference_price),
                fill_price=Decimal(fill_price),
                explicit_cost=Decimal(10),
                execution_rule_id=_hash("5"),
                cost_schedule_id=_hash("6"),
                raw_bar_snapshot_id=_hash("7"),
                evidence_ids=evidence_ids,
                recorded_by="stage4g-test",
            )

    def record_path(
        self,
        case_id: str,
        *,
        minute: int,
        session_index: int,
        high: str,
        low: str,
        close: str,
        evidence_ids=(),
        observable: bool = True,
    ):
        observed_at = _BASE + timedelta(minutes=minute)
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_path_point(
                case_id,
                timestamp=observed_at,
                session_index=session_index,
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                observable=observable,
                raw_bar_snapshot_id=_hash("8"),
                evidence_ids=evidence_ids,
                recorded_by="stage4g-test",
            )

    def record_exit_request(
        self,
        case_id: str,
        *,
        evidence_ids=(),
        reason: OutcomeTerminalReason = OutcomeTerminalReason.TARGET,
        minute: int = 4,
    ):
        observed_at = _BASE + timedelta(minutes=minute)
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_exit_request(
                case_id,
                requested_at=observed_at,
                quantity=100,
                terminal_reason=reason,
                execution_policy_id=_hash("9"),
                reason="到达已冻结退出条件",
                evidence_ids=evidence_ids,
                recorded_by="stage4g-test",
            )

    def record_exit_fill(
        self,
        case_id: str,
        *,
        evidence_ids=(),
        fill_price: str = "12",
        reference_price: str | None = None,
        minute: int = 5,
        session_index: int = 12,
    ):
        observed_at = _BASE + timedelta(minutes=minute)
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_exit_fill(
                case_id,
                timestamp=observed_at,
                session_index=session_index,
                quantity=100,
                reference_price=Decimal(reference_price or fill_price),
                fill_price=Decimal(fill_price),
                explicit_cost=Decimal(10),
                execution_rule_id=_hash("a"),
                cost_schedule_id=_hash("6"),
                raw_bar_snapshot_id=_hash("b"),
                evidence_ids=evidence_ids,
                recorded_by="stage4g-test",
            )

    def make_complete_case(
        self,
        *,
        mode: OutcomeCollectionMode = OutcomeCollectionMode.PAPER,
        signal: T.Signal | None = None,
    ) -> str:
        evidence = (_hash("c"),) if mode is OutcomeCollectionMode.LIVE_MANUAL else ()
        opened = self.open_case(signal=signal, mode=mode)
        case_id = opened.event.case_id
        self.record_entry(case_id, evidence_ids=evidence)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
            evidence_ids=evidence,
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="13",
            low="10",
            close="12",
            evidence_ids=evidence,
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="12",
            low="11",
            close="12",
            evidence_ids=evidence,
        )
        self.record_exit_request(case_id, evidence_ids=evidence)
        self.record_exit_fill(case_id, evidence_ids=evidence)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.TERMINAL_READY,
        )
        return case_id

    def finalize(self, case_id: str, *, collection_at=None, ledger_at=None):
        collection_at = collection_at or _BASE + timedelta(minutes=6)
        ledger_at = ledger_at or _BASE + timedelta(minutes=7)
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=collection_at,
        ), patch.object(
            ledger_module,
            "_utc_now",
            return_value=ledger_at,
        ):
            return self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )


class TestRuntimeSignalSnapshot(OutcomeCollectionFixture):
    def test_snapshot_freezes_runtime_signal_and_recomputes_identity(self) -> None:
        signal = _runtime_signal()
        result = self.open_case(signal=signal)
        case = self.collection.get_case(result.event.case_id)
        snapshot = case.snapshot
        self.assertTrue(snapshot.runtime_state_time_aware)
        self.assertTrue(snapshot.runtime_state_changed_at.endswith("Z"))
        self.assertEqual(snapshot.runtime_episode_fact_id, _hash("d"))
        self.assertEqual(snapshot.entry_intent.decision_snapshot_id, snapshot.decision_snapshot_id)

        signal.reason = "mutable runtime reason changed"
        signal.scores.opportunity = 1
        signal.entry_low = 1.0
        rebuilt = RuntimeSignalSnapshot.from_dict(snapshot.as_dict())
        self.assertEqual(rebuilt, snapshot)
        oversized_document = snapshot.as_dict()
        oversized_document["scores"]["positive_reasons"] = [
            f"reason-{index}" for index in range(257)
        ]
        with self.assertRaisesRegex(OutcomeCollectionError, "256-item bound"):
            RuntimeSignalSnapshot.from_dict(oversized_document)
        self.assertEqual(rebuilt.reason, "结构与风险门已形成可审计候选")
        self.assertEqual(rebuilt.scores.opportunity, 80)
        self.assertEqual(rebuilt.entry_low, Decimal(10))
        with self.assertRaises(TypeError):
            replace(snapshot, decision_snapshot_id=_hash("f"))

    def test_mutating_runtime_signal_is_rejected_during_snapshot(self) -> None:
        first = _runtime_signal()
        second = _runtime_signal()
        second.reason = "concurrent mutation"
        with patch.object(
            collection_module.copy,
            "deepcopy",
            side_effect=(first, second),
        ), self.assertRaisesRegex(
            OutcomeCollectionError,
            "changed while snapshotting",
        ):
            self.open_case(signal=first)
        self.assertEqual(self.collection.audit().event_count, 0)

    def test_runtime_signal_and_scores_require_exact_project_types(self) -> None:
        class ForgedSignal(T.Signal):
            pass

        class ForgedScoreSet(T.ScoreSet):
            pass

        source = _runtime_signal()
        forged_signal = ForgedSignal(
            **{
                item.name: getattr(source, item.name)
                for item in fields(T.Signal)
                if item.init
            }
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "exact runtime Signal type",
        ):
            self.open_case(signal=forged_signal)

        source = _runtime_signal()
        assert source.scores is not None
        source.scores = ForgedScoreSet(
            **{
                item.name: getattr(source.scores, item.name)
                for item in fields(T.ScoreSet)
                if item.init
            }
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "exact ScoreSet type",
        ):
            self.open_case(signal=source)

    def test_entry_request_time_is_distinct_from_collection_capture(self) -> None:
        opened = self.open_case(
            observed_at=_BASE + timedelta(minutes=5),
            entry_requested_at=_BASE,
        )
        case = self.collection.get_case(opened.event.case_id)
        self.assertEqual(case.snapshot.entry_requested_at, _BASE)
        self.assertEqual(case.snapshot.captured_at, _BASE + timedelta(minutes=5))
        self.assertEqual(case.snapshot.entry_intent.requested_at, _BASE)
        result = self.record_entry(
            case.case_id,
            observed_at=_BASE + timedelta(minutes=6),
        )
        self.assertIs(result.case_state, OutcomeCollectionCaseState.OPEN_POSITION)
        self.assertLess(
            self.collection.get_case(case.case_id).entry_fill.timestamp,
            case.snapshot.captured_at,
        )

    def test_snapshot_rejects_non_actionable_and_missing_identity(self) -> None:
        for state in (
            T.SignalState.WATCH,
            T.SignalState.OVEREXTENDED,
            T.SignalState.INVALIDATED,
            T.SignalState.EXPIRED,
        ):
            with self.subTest(state=state), self.assertRaisesRegex(
                OutcomeCollectionError,
                "not eligible",
            ):
                self.open_case(signal=_runtime_signal(state=state))
        with self.assertRaisesRegex(OutcomeCollectionError, "timezone-aware"):
            self.open_case(signal=_runtime_signal(aware_state_time=False))
        with self.assertRaisesRegex(OutcomeCollectionError, "timezone-aware"):
            self.open_case(entry_requested_at=_BASE.replace(tzinfo=None))
        with self.assertRaisesRegex(OutcomeCollectionError, "after collection capture"):
            self.open_case(entry_requested_at=_BASE + timedelta(seconds=1))
        future_state = _runtime_signal()
        future_state.state_changed_at = _BASE + timedelta(seconds=1)
        with self.assertRaisesRegex(OutcomeCollectionError, "after collection capture"):
            self.open_case(signal=future_state)
        with self.assertRaisesRegex(OutcomeCollectionError, "lowercase SHA-256"):
            self.open_case(identity_fact_id="not-a-hash")
        with self.assertRaisesRegex(OutcomeCollectionError, "lowercase SHA-256"):
            self.open_case(runtime_episode_fact_id="not-a-hash")
        with self.assertRaisesRegex(OutcomeCollectionError, "integer"):
            self.open_case(minimum_exit_session_offset=True)
        offset_after_horizon = self.open_case(
            runtime_episode_fact_id=_hash("e"),
            horizon_sessions=2,
            minimum_exit_session_offset=3,
        )
        self.assertEqual(
            self.collection.get_case(
                offset_after_horizon.event.case_id
            ).snapshot.minimum_exit_session_offset,
            3,
        )
        broken = _runtime_signal()
        broken.scores = None
        with self.assertRaisesRegex(OutcomeCollectionError, "no score evidence"):
            self.open_case(signal=broken)
        broken_reasons = _runtime_signal()
        broken_reasons.scores.positive_reasons = ("not-a-list",)
        with self.assertRaisesRegex(OutcomeCollectionError, "reasons must be lists"):
            self.open_case(signal=broken_reasons)
        excessive_reasons = _runtime_signal()
        excessive_reasons.scores.positive_reasons = [
            f"reason-{index}" for index in range(257)
        ]
        with self.assertRaisesRegex(OutcomeCollectionError, "256-item bound"):
            self.open_case(signal=excessive_reasons)
        invalid_stop = _runtime_signal()
        invalid_stop.invalidation_price = invalid_stop.entry_low
        with self.assertRaisesRegex(OutcomeCollectionError, "planned entry range"):
            self.open_case(signal=invalid_stop)
        invalid_target = _runtime_signal()
        invalid_target.target_1 = invalid_target.entry_high
        with self.assertRaisesRegex(OutcomeCollectionError, "target plan"):
            self.open_case(signal=invalid_target)

    def test_open_case_requires_explicit_episode_identity_for_drift(self) -> None:
        first = self.open_case()
        second = self.open_case(observed_at=_BASE + timedelta(minutes=1))
        self.assertIs(first.disposition, OutcomeCollectionDisposition.APPENDED)
        self.assertIs(second.disposition, OutcomeCollectionDisposition.IDEMPOTENT)
        self.assertEqual(first.event.event_hash, second.event.event_hash)
        with self.assertRaisesRegex(
            OutcomeCollectionConflict,
            "different immutable evidence",
        ):
            self.open_case(
                observed_at=_BASE + timedelta(minutes=2),
                strategy_version="v2",
            )
        next_episode = self.open_case(
            observed_at=_BASE + timedelta(minutes=3),
            runtime_episode_fact_id=_hash("e"),
            strategy_version="v2",
        )
        self.assertIs(next_episode.disposition, OutcomeCollectionDisposition.APPENDED)
        self.assertNotEqual(first.event.case_id, next_episode.event.case_id)
        self.assertEqual(
            self.collection.get_case(first.event.case_id).snapshot.signal_id,
            self.collection.get_case(next_episode.event.case_id).snapshot.signal_id,
        )

    def test_same_episode_fact_can_track_paper_and_live_modes_consistently(self) -> None:
        paper = self.open_case(mode=OutcomeCollectionMode.PAPER)
        live = self.open_case(
            mode=OutcomeCollectionMode.LIVE_MANUAL,
            observed_at=_BASE + timedelta(minutes=1),
        )
        paper_case = self.collection.get_case(paper.event.case_id)
        live_case = self.collection.get_case(live.event.case_id)
        self.assertNotEqual(paper_case.case_id, live_case.case_id)
        self.assertEqual(
            paper_case.snapshot.runtime_episode_id,
            live_case.snapshot.runtime_episode_id,
        )
        self.assertNotEqual(
            paper_case.snapshot.decision_snapshot_id,
            live_case.snapshot.decision_snapshot_id,
        )

    def test_cross_mode_episode_fact_rejects_inconsistent_evidence(self) -> None:
        self.open_case(mode=OutcomeCollectionMode.PAPER)
        changed = _runtime_signal()
        changed.reason = "same fact id cannot describe a different runtime decision"
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "inconsistent immutable evidence",
        ):
            self.open_case(
                signal=changed,
                mode=OutcomeCollectionMode.LIVE_MANUAL,
                observed_at=_BASE + timedelta(minutes=1),
            )


class TestOutcomeCollectionLifecycle(OutcomeCollectionFixture):
    def test_paper_complete_lifecycle_finalizes_to_diagnostic_ledger(self) -> None:
        case_id = self.make_complete_case()
        result = self.finalize(case_id)
        self.assertIs(result.case.state, OutcomeCollectionCaseState.FINALIZED)
        self.assertIs(result.outcome.origin, OutcomeEvidenceOrigin.PAPER_RECORDED)
        self.assertIs(result.outcome.evidence_tier, DataTrustTier.BEST_EFFORT)
        self.assertFalse(result.outcome.verified)
        self.assertFalse(result.outcome.real_scoreboard_eligible)
        self.assertIs(result.ledger_result.record.lane, OutcomeLedgerLane.DIAGNOSTIC_ONLY)
        self.assertEqual(result.outcome.metrics.realized_r, Decimal("1.8"))
        self.assertEqual(result.outcome.metrics.mfe_r, Decimal("2.9"))
        self.assertEqual(result.outcome.metrics.mae_r, Decimal("-0.6"))
        self.assertEqual(result.outcome.metrics.total_cost, Decimal(20))

        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=8),
        ):
            audit = self.collection.audit()
        self.assertEqual(audit.case_count, 1)
        self.assertEqual(audit.event_count, 9)
        self.assertEqual(
            dict(audit.state_counts),
            {OutcomeCollectionCaseState.FINALIZED: 1},
        )
        self.assertFalse(audit.as_dict()["investment_performance_claim"])
        self.assertEqual(self.production.read_bytes(), b"production-sentinel")
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "state disagrees",
        ):
            replace(
                result.case,
                state=OutcomeCollectionCaseState.AWAITING_ENTRY,
            )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "all present or all absent",
        ):
            replace(result.case, finalized_record_hash=None)

    def test_live_manual_requires_fact_evidence_and_stays_candidate(self) -> None:
        opened = self.open_case(mode=OutcomeCollectionMode.LIVE_MANUAL)
        case_id = opened.event.case_id
        with self.assertRaisesRegex(OutcomeCollectionError, "requires evidence IDs"):
            self.record_entry(case_id)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.AWAITING_ENTRY,
        )
        evidence = (_hash("c"),)
        self.record_entry(case_id, evidence_ids=evidence)
        with self.assertRaisesRegex(OutcomeCollectionError, "requires evidence IDs"):
            self.record_path(
                case_id,
                minute=2,
                session_index=10,
                high="11",
                low="9.5",
                close="10.5",
            )
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
            evidence_ids=evidence,
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="12",
            low="10",
            close="11",
            evidence_ids=evidence,
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="12",
            low="11",
            close="12",
            evidence_ids=evidence,
        )
        self.record_exit_request(case_id, evidence_ids=evidence)
        self.record_exit_fill(case_id, evidence_ids=evidence)
        result = self.finalize(case_id)
        self.assertIs(result.outcome.origin, OutcomeEvidenceOrigin.LIVE_OBSERVED)
        self.assertIs(result.outcome.evidence_tier, DataTrustTier.BEST_EFFORT)
        self.assertFalse(result.outcome.verified)
        self.assertFalse(result.outcome.real_scoreboard_eligible)
        self.assertIs(result.ledger_result.record.lane, OutcomeLedgerLane.LIVE_CANDIDATE)

    def test_live_manual_entry_refuses_delayed_runtime_data(self) -> None:
        opened = self.open_case(
            mode=OutcomeCollectionMode.LIVE_MANUAL,
            signal=_runtime_signal(data_status=T.DataStatus.DELAYED),
        )
        with self.assertRaisesRegex(OutcomeCollectionError, "requires LIVE"):
            self.record_entry(opened.event.case_id, evidence_ids=(_hash("c"),))

    def test_partial_entry_requires_explicit_future_lifecycle_model(self) -> None:
        case_id = self.open_case().event.case_id
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "fully satisfy the requested quantity",
        ):
            self.record_entry(case_id, quantity=50)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.AWAITING_ENTRY,
        )

    def test_evidence_id_count_is_bounded_before_serialization(self) -> None:
        case_id = self.open_case().event.case_id
        evidence_ids = tuple(
            sorted(
                hashlib.sha256(str(index).encode()).hexdigest()
                for index in range(1025)
            )
        )
        with self.assertRaisesRegex(OutcomeCollectionError, "1024-item bound"):
            self.record_entry(case_id, evidence_ids=evidence_ids)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.AWAITING_ENTRY,
        )

    def test_malformed_evidence_iterables_use_collection_error_boundary(self) -> None:
        case_id = self.open_case().event.case_id
        with self.assertRaisesRegex(OutcomeCollectionError, "must be an iterable"):
            self.record_entry(case_id, evidence_ids=None)

        def broken_evidence():
            yield _hash("1")
            raise RuntimeError("forced iterator failure")

        with self.assertRaisesRegex(OutcomeCollectionError, "could not be read safely"):
            self.record_entry(case_id, evidence_ids=broken_evidence())
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.AWAITING_ENTRY,
        )

    def test_exit_fill_requires_contiguous_observable_session_coverage(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=12,
            high="12",
            low="10",
            close="11",
        )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "contiguous observable session coverage",
        ):
            self.record_exit_fill(case_id)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.EXIT_REQUESTED,
        )

    def test_sparse_extreme_session_range_fails_without_materializing_range(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="12",
            low="9.5",
            close="11",
        )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "contiguous observable session coverage",
        ):
            self.record_exit_fill(
                case_id,
                session_index=10**12,
            )

    def test_entry_fill_price_must_fit_observable_session_range(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id, reference_price="10.5")
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="12",
                low="10.1",
                close="11",
            )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "entry fill price is outside",
        ):
            self.record_exit_fill(case_id)

    def test_entry_reference_price_must_fit_observable_session_range(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id, reference_price="9")
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="12",
                low="9.5",
                close="11",
            )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "entry reference price is outside",
        ):
            self.record_exit_fill(case_id)

    def test_exit_fill_price_must_fit_observable_session_range(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="13",
            low="10",
            close="12",
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="11.5",
            low="10.5",
            close="11",
        )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "exit fill price is outside",
        ):
            self.record_exit_fill(case_id, reference_price="11")

    def test_exit_reference_price_must_fit_observable_session_range(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="13",
            low="10",
            close="12",
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="12",
            low="11",
            close="12",
        )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "exit reference price is outside",
        ):
            self.record_exit_fill(case_id, reference_price="13")

    def test_path_cannot_contain_session_after_exit(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="12",
                low="9.5",
                close="11",
            )
        self.record_path(
            case_id,
            minute=5,
            session_index=13,
            high="12",
            low="10",
            close="11",
            observable=False,
        )
        self.record_exit_request(case_id, minute=6)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "point after the exit session",
        ):
            self.record_exit_fill(case_id, minute=7, session_index=12)

    def test_delayed_timeout_exit_after_horizon_is_preserved(self) -> None:
        case_id = self.open_case(
            horizon_sessions=1,
            minimum_exit_session_offset=2,
        ).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="10.9",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="10.9",
            low="10",
            close="10.5",
        )
        self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.TIMEOUT,
            minute=3,
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="12",
            low="11",
            close="12",
        )
        result = self.record_exit_fill(case_id)
        self.assertIs(result.case_state, OutcomeCollectionCaseState.TERMINAL_READY)
        finalized = self.finalize(case_id)
        self.assertEqual(finalized.outcome.horizon_sessions, 1)
        self.assertEqual(finalized.outcome.metrics.holding_sessions, 2)
        self.assertEqual(
            finalized.outcome.terminal_reason,
            OutcomeTerminalReason.TIMEOUT,
        )

    def test_exit_fill_respects_frozen_minimum_session_offset(self) -> None:
        case_id = self.open_case(minimum_exit_session_offset=1).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="12",
            low="9.5",
            close="10.5",
        )
        self.record_exit_request(case_id)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "frozen minimum session offset",
        ):
            self.record_exit_fill(case_id, session_index=10)

    def test_zero_minimum_session_offset_allows_same_session_policy(self) -> None:
        case_id = self.open_case(minimum_exit_session_offset=0).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="12",
            low="9.5",
            close="12",
        )
        self.record_exit_request(case_id)
        result = self.record_exit_fill(case_id, session_index=10)
        self.assertIs(result.case_state, OutcomeCollectionCaseState.TERMINAL_READY)

    def test_timeout_cannot_be_requested_before_horizon_evidence(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="12",
                low="9.5",
                close="11",
            )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "TIMEOUT requires horizon evidence by exit request time",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TIMEOUT,
            )
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_terminal_reason_must_follow_first_observable_barrier(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="10.5",
            low="8.9",
            close="9.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="11.5",
            low="9.5",
            close="11",
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "first observable barrier",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TARGET,
            )
        stopped = self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.STOP,
        )
        self.assertIs(
            stopped.case_state,
            OutcomeCollectionCaseState.EXIT_REQUESTED,
        )

    def test_target_first_blocks_later_stop_reason(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11.5",
            low="9.5",
            close="11",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="10.5",
            low="8.9",
            close="9.5",
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "first observable barrier",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.STOP,
            )
        targeted = self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.TARGET,
        )
        self.assertIs(
            targeted.case_state,
            OutcomeCollectionCaseState.EXIT_REQUESTED,
        )

    def test_same_path_point_target_stop_ambiguity_fails_closed(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11.5",
            low="8.9",
            close="10",
        )
        for reason in (
            OutcomeTerminalReason.TARGET,
            OutcomeTerminalReason.STOP,
            OutcomeTerminalReason.TIMEOUT,
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(
                OutcomeCollectionError,
                "order is ambiguous",
            ):
                self.record_exit_request(case_id, reason=reason)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_timeout_conflicts_with_earlier_observable_barrier(self) -> None:
        case_id = self.open_case(horizon_sessions=1).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="10.5",
            low="9.5",
            close="10",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="11.5",
            low="9.5",
            close="11",
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "TIMEOUT conflicts with an earlier observable barrier",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TIMEOUT,
            )

    def test_target_touch_after_horizon_cannot_replace_timeout(self) -> None:
        case_id = self.open_case(horizon_sessions=1).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="10.5",
            low="9.5",
            close="10",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="10.9",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=4,
            session_index=12,
            high="11.5",
            low="9.5",
            close="11",
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "observable target touch within the configured horizon",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TARGET,
                minute=5,
            )
        timeout = self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.TIMEOUT,
            minute=5,
        )
        self.assertIs(
            timeout.case_state,
            OutcomeCollectionCaseState.EXIT_REQUESTED,
        )

    def test_target_request_requires_observable_price_evidence(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="10.9",
                low="9.5",
                close="10.5",
            )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "observable target touch within the configured horizon",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TARGET,
            )
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_stop_request_requires_observable_price_evidence(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        for minute, session_index in ((2, 10), (3, 11), (4, 12)):
            self.record_path(
                case_id,
                minute=minute,
                session_index=session_index,
                high="10.9",
                low="9.2",
                close="10.5",
            )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "observable invalidation touch within the configured horizon",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.STOP,
            )
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_exit_request_binds_path_prefix_against_backdated_facts(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        first_path = self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        second_path = self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="11",
            low="10",
            close="10.5",
        )
        request = self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.MANUAL,
        )
        requested_case = self.collection.get_case(case_id)
        self.assertEqual(
            requested_case.path_fact_ids,
            (first_path.event.fact_id, second_path.event.fact_id),
        )
        self.assertEqual(
            requested_case.as_dict()["path_fact_ids"],
            [first_path.event.fact_id, second_path.event.fact_id],
        )
        self.assertEqual(
            requested_case.path_observed_at,
            (
                _BASE + timedelta(minutes=2),
                _BASE + timedelta(minutes=3),
            ),
        )
        self.assertEqual(
            requested_case.as_dict()["path_observed_at"],
            [
                "2026-08-29T01:02:00.000000Z",
                "2026-08-29T01:03:00.000000Z",
            ],
        )
        self.assertEqual(requested_case.exit_request_path_prefix_count, 2)
        self.assertIsNotNone(requested_case.exit_request_path_prefix_id)
        prefix_id = requested_case.exit_request_path_prefix_id
        intent_id = requested_case.exit_intent.intent_id
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "omits or includes unavailable facts",
        ):
            replace(
                requested_case,
                exit_request_path_prefix_count=1,
            )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "path prefix identity is invalid",
        ):
            replace(
                requested_case,
                path_fact_ids=(
                    _hash("f"),
                    requested_case.path_fact_ids[1],
                ),
            )

        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=5),
        ):
            self.collection.record_path_point(
                case_id,
                timestamp=_BASE + timedelta(minutes=3, seconds=30),
                session_index=12,
                high=Decimal(12),
                low=Decimal(11),
                close=Decimal(12),
                observable=True,
                raw_bar_snapshot_id=_hash("8"),
                recorded_by="stage4g-test",
            )
        updated_case = self.collection.get_case(case_id)
        self.assertEqual(len(updated_case.path), 3)
        self.assertLess(
            updated_case.path[-1].timestamp,
            updated_case.exit_intent.requested_at,
        )
        self.assertEqual(updated_case.exit_request_path_prefix_count, 2)
        self.assertEqual(updated_case.exit_request_path_prefix_id, prefix_id)
        self.assertEqual(updated_case.exit_intent.intent_id, intent_id)

        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ):
            retry = self.collection.record_exit_request(
                case_id,
                requested_at=_BASE + timedelta(minutes=4),
                quantity=100,
                terminal_reason=OutcomeTerminalReason.MANUAL,
                execution_policy_id=_hash("9"),
                reason="到达已冻结退出条件",
                recorded_by="stage4g-test",
            )
        self.assertIs(retry.disposition, OutcomeCollectionDisposition.IDEMPOTENT)
        self.assertEqual(retry.event.event_hash, request.event.event_hash)

    def test_late_observed_backdated_path_cannot_justify_exit_reason(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=5),
        ):
            self.collection.record_path_point(
                case_id,
                timestamp=_BASE + timedelta(minutes=2),
                session_index=10,
                high=Decimal("11.5"),
                low=Decimal("9.5"),
                close=Decimal(11),
                observable=True,
                raw_bar_snapshot_id=_hash("8"),
                recorded_by="stage4g-test",
            )
        observed_case = self.collection.get_case(case_id)
        self.assertEqual(
            observed_case.path_observed_at,
            (_BASE + timedelta(minutes=5),),
        )
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ), self.assertRaisesRegex(
            OutcomeCollectionError,
            "observable target touch within the configured horizon",
        ):
            self.collection.record_exit_request(
                case_id,
                requested_at=_BASE + timedelta(minutes=4),
                quantity=100,
                terminal_reason=OutcomeTerminalReason.TARGET,
                execution_policy_id=_hash("9"),
                reason="不能用请求后才观察到的旧时间戳事实解释退出",
                recorded_by="stage4g-test",
            )
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_post_request_path_requires_strictly_later_observation_time(self) -> None:
        case_id = self.open_case().event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.record_exit_request(
            case_id,
            reason=OutcomeTerminalReason.MANUAL,
        )
        before = self.collection.audit().event_count
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "must be strictly later",
        ):
            self.record_path(
                case_id,
                minute=4,
                session_index=11,
                high="12",
                low="10",
                close="11",
            )
        self.assertEqual(self.collection.audit().event_count, before)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.EXIT_REQUESTED,
        )

    def test_timeout_request_requires_horizon_evidence(self) -> None:
        case_id = self.open_case(horizon_sessions=2).event.case_id
        self.record_entry(case_id)
        self.record_path(
            case_id,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.record_path(
            case_id,
            minute=3,
            session_index=11,
            high="11",
            low="10",
            close="10.5",
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "horizon evidence by exit request time",
        ):
            self.record_exit_request(
                case_id,
                reason=OutcomeTerminalReason.TIMEOUT,
            )
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.OPEN_POSITION,
        )

    def test_no_entry_finalizes_without_fabricated_path_or_metrics(self) -> None:
        opened = self.open_case(
            signal=_runtime_signal(
                state=T.SignalState.DATA_INVALID,
                data_status=T.DataStatus.UNKNOWN,
            )
        )
        case_id = opened.event.case_id
        at = _BASE + timedelta(minutes=1)
        with patch.object(collection_module, "_utc_now", return_value=at):
            self.collection.record_no_entry(
                case_id,
                fact_at=at,
                terminal_reason=OutcomeTerminalReason.DATA_INVALID,
                reason="行情事实不足，未形成成交",
                recorded_by="stage4g-test",
            )
        result = self.finalize(case_id)
        self.assertEqual(result.outcome.entry_fill, None)
        self.assertEqual(result.outcome.path, ())
        self.assertEqual(result.outcome.metrics, None)
        self.assertEqual(
            result.outcome.terminal_reason,
            OutcomeTerminalReason.DATA_INVALID,
        )
        self.assertIs(result.ledger_result.record.lane, OutcomeLedgerLane.DIAGNOSTIC_ONLY)
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "path prefix requires an exit intent",
        ):
            replace(
                result.case,
                exit_request_path_prefix_count=0,
                exit_request_path_prefix_id=_hash("f"),
            )

    def test_invalid_lifecycle_transitions_are_fail_closed(self) -> None:
        opened = self.open_case()
        case_id = opened.event.case_id
        with self.assertRaisesRegex(OutcomeCollectionError, "outside an open position"):
            self.record_path(
                case_id,
                minute=2,
                session_index=10,
                high="11",
                low="9.5",
                close="10.5",
            )
        self.record_entry(case_id)
        with self.assertRaisesRegex(OutcomeCollectionError, "entry disposition"):
            self.record_entry(case_id, fill_price="10.1")
        with self.assertRaisesRegex(OutcomeCollectionError, "not ready"):
            self.finalize(case_id)

    def test_fact_retries_are_idempotent(self) -> None:
        case_id = self.open_case().event.case_id
        first = self.record_entry(case_id)
        second = self.record_entry(
            case_id,
            observed_at=_BASE + timedelta(minutes=2),
        )
        self.assertIs(first.disposition, OutcomeCollectionDisposition.APPENDED)
        self.assertIs(second.disposition, OutcomeCollectionDisposition.IDEMPOTENT)
        self.assertEqual(first.event.event_hash, second.event.event_hash)
        self.assertEqual(self.collection.audit().event_count, 2)

    def test_identical_market_fact_can_be_reused_by_distinct_cases(self) -> None:
        first_case = self.open_case().event.case_id
        second_case = self.open_case(
            signal=_runtime_signal(suffix="two"),
            runtime_episode_fact_id=_hash("e"),
        ).event.case_id
        self.record_entry(first_case)
        self.record_entry(second_case)
        first_path = self.record_path(
            first_case,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        second_path = self.record_path(
            second_case,
            minute=2,
            session_index=10,
            high="11",
            low="9.5",
            close="10.5",
        )
        self.assertEqual(first_path.event.fact_id, second_path.event.fact_id)
        self.assertNotEqual(first_path.event.case_id, second_path.event.case_id)
        with closing(sqlite3.connect(self.collection_path)) as connection:
            duplicate_count = connection.execute(
                "SELECT COUNT(*) FROM outcome_collection_events "
                "WHERE event_type='PATH_POINT' AND fact_id=?",
                (first_path.event.fact_id,),
            ).fetchone()[0]
        self.assertEqual(duplicate_count, 2)
        self.assertEqual(self.collection.audit().case_count, 2)


class TestOutcomeCollectionRecoveryAndIntegrity(OutcomeCollectionFixture):
    def test_prepared_finalization_survives_ledger_failure_and_retries_same_outcome(self) -> None:
        case_id = self.make_complete_case()
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ), patch.object(
            self.ledger,
            "append",
            side_effect=RuntimeError("forced ledger outage"),
        ), self.assertRaisesRegex(RuntimeError, "forced ledger outage"):
            self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )
        prepared = self.collection.get_case(case_id)
        self.assertIs(
            prepared.state,
            OutcomeCollectionCaseState.FINALIZATION_PREPARED,
        )
        self.assertIsNotNone(prepared.prepared_outcome)
        prepared_id = prepared.prepared_outcome.outcome_id
        self.assertEqual(self.ledger.audit().record_count, 0)

        result = self.finalize(
            case_id,
            collection_at=_BASE + timedelta(minutes=7),
            ledger_at=_BASE + timedelta(minutes=8),
        )
        self.assertEqual(result.outcome.outcome_id, prepared_id)
        self.assertEqual(self.ledger.audit().record_count, 1)

    def test_retry_after_ledger_commit_is_idempotent_when_final_marker_failed(self) -> None:
        case_id = self.make_complete_case()
        original_record_finalized = self.collection._record_finalized
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ), patch.object(
            ledger_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=7),
        ), patch.object(
            self.collection,
            "_record_finalized",
            side_effect=RuntimeError("forced marker outage"),
        ), self.assertRaisesRegex(RuntimeError, "forced marker outage"):
            self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )
        self.assertEqual(self.ledger.audit().record_count, 1)
        self.assertIs(
            self.collection.get_case(case_id).state,
            OutcomeCollectionCaseState.FINALIZATION_PREPARED,
        )

        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=8),
        ), patch.object(
            ledger_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=9),
        ), patch.object(
            self.collection,
            "_record_finalized",
            side_effect=original_record_finalized,
        ):
            result = self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )
        self.assertIs(result.ledger_result.disposition, OutcomeLedgerDisposition.IDEMPOTENT)
        self.assertIs(result.case.state, OutcomeCollectionCaseState.FINALIZED)
        self.assertEqual(self.ledger.audit().record_count, 1)
        repeated = self.collection.finalize(
            case_id,
            self.ledger,
            recorded_by="stage4g-test",
        )
        self.assertIs(repeated.case.state, OutcomeCollectionCaseState.FINALIZED)
        self.assertIs(repeated.ledger_result.disposition, OutcomeLedgerDisposition.IDEMPOTENT)
        self.assertEqual(repeated.case.finalized_record_hash, result.case.finalized_record_hash)
        self.assertEqual(
            repeated.case.finalized_record_append_order,
            result.case.finalized_record_append_order,
        )
        self.assertEqual(self.ledger.audit().record_count, 1)

    def test_concurrent_finalization_writes_one_terminal_marker(self) -> None:
        case_id = self.make_complete_case()
        services = (
            self.collection,
            OutcomeCollectionService(
                self.collection_path,
                production_database=self.production,
            ),
        )
        ledgers = (
            self.ledger,
            OutcomeLedger(
                self.ledger.record_root,
                self.ledger.catalog_path,
                production_database=self.production,
            ),
        )
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ), patch.object(
            ledger_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=7),
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(
                    service.finalize,
                    case_id,
                    ledger,
                    recorded_by="stage4g-test",
                )
                for service, ledger in zip(services, ledgers, strict=True)
            )
            results = tuple(future.result(timeout=30) for future in futures)
        self.assertEqual(
            sorted(result.collection_disposition.value for result in results),
            ["APPENDED", "IDEMPOTENT"],
        )
        self.assertEqual(self.ledger.audit().record_count, 1)
        with closing(sqlite3.connect(self.collection_path)) as connection:
            terminal_markers = connection.execute(
                "SELECT COUNT(*) FROM outcome_collection_events "
                "WHERE event_type='FINALIZED'",
            ).fetchone()[0]
        self.assertEqual(terminal_markers, 1)
        self.assertTrue(
            all(result.case.state is OutcomeCollectionCaseState.FINALIZED for result in results)
        )

    def test_prepared_case_rejects_recreated_ledger_at_same_path(self) -> None:
        case_id = self.make_complete_case()
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=6),
        ), patch.object(
            self.ledger,
            "append",
            side_effect=RuntimeError("forced ledger outage"),
        ), self.assertRaisesRegex(RuntimeError, "forced ledger outage"):
            self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )
        self.ledger.catalog_path.unlink()
        replacement = OutcomeLedger(
            self.ledger.record_root,
            self.ledger.catalog_path,
            production_database=self.production,
        )
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=8),
        ), self.assertRaisesRegex(
            OutcomeCollectionConflict,
            "different outcome ledger",
        ):
            self.collection.finalize(
                case_id,
                replacement,
                recorded_by="stage4g-test",
            )

    def test_ledger_replacement_during_target_identity_fails_before_prepare(self) -> None:
        case_id = self.make_complete_case()
        original_assert = self.ledger._assert_catalog_identity
        assertion_count = 0

        def replace_after_first_assertion() -> None:
            nonlocal assertion_count
            assertion_count += 1
            original_assert()
            if assertion_count == 1:
                self.ledger.catalog_path.unlink()
                OutcomeLedger(
                    self.ledger.record_root,
                    self.ledger.catalog_path,
                    production_database=self.production,
                )

        with patch.object(
            self.ledger,
            "_assert_catalog_identity",
            side_effect=replace_after_first_assertion,
        ), self.assertRaisesRegex(
            OutcomeCollectionError,
            "target identity is invalid",
        ):
            self.collection.finalize(
                case_id,
                self.ledger,
                recorded_by="stage4g-test",
            )
        case = self.collection.get_case(case_id)
        self.assertIs(case.state, OutcomeCollectionCaseState.TERMINAL_READY)
        with closing(sqlite3.connect(self.collection_path)) as connection:
            prepared_count = connection.execute(
                "SELECT COUNT(*) FROM outcome_collection_events "
                "WHERE event_type='FINALIZATION_PREPARED'",
            ).fetchone()[0]
        self.assertEqual(prepared_count, 0)

    def test_clock_rollback_and_catalog_tampering_block_reads_and_writes(self) -> None:
        opened = self.open_case()
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE - timedelta(seconds=1),
        ), self.assertRaisesRegex(OutcomeCollectionError, "after the audit timestamp"):
            self.collection.audit()

        with closing(sqlite3.connect(self.collection_path)) as connection:
            connection.execute(
                "UPDATE outcome_collection_events SET payload_sha256=? WHERE append_order=1",
                (_hash("f"),),
            )
            connection.commit()
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=1),
        ), self.assertRaisesRegex(OutcomeCollectionError, "payload SHA"):
            self.collection.get_case(opened.event.case_id)
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(minutes=1),
        ), self.assertRaisesRegex(OutcomeCollectionError, "payload SHA"):
            self.record_entry(opened.event.case_id)

    def test_concurrent_first_initialization_publishes_one_complete_database(self) -> None:
        database_path = self.root / "concurrent-collection.db"
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = tuple(
                executor.submit(
                    OutcomeCollectionService,
                    database_path,
                    production_database=self.production,
                )
                for _ in range(8)
            )
            services = tuple(future.result(timeout=20) for future in futures)
        self.assertTrue(database_path.is_file())
        self.assertTrue(all(service.audit().event_count == 0 for service in services))
        self.assertEqual(
            list(self.root.glob(f".{database_path.name}.init-*")),
            [],
        )

    def test_failed_first_initialization_does_not_publish_partial_database(self) -> None:
        database_path = self.root / "failed-collection.db"
        with patch.object(
            collection_module,
            "_validate_collection_schema",
            side_effect=OutcomeCollectionError("forced schema failure"),
        ), self.assertRaisesRegex(OutcomeCollectionError, "forced schema failure"):
            OutcomeCollectionService(
                database_path,
                production_database=self.production,
            )
        self.assertFalse(database_path.exists())
        self.assertEqual(
            list(self.root.glob(f".{database_path.name}.init-*")),
            [],
        )

    def test_unsupported_atomic_publish_fails_through_collection_contract(self) -> None:
        database_path = self.root / "unsupported-publish.db"
        with patch.object(
            collection_module.os,
            "link",
            side_effect=OSError("hard links unsupported"),
        ), self.assertRaisesRegex(
            OutcomeCollectionError,
            "cannot atomically publish",
        ):
            OutcomeCollectionService(
                database_path,
                production_database=self.production,
            )
        self.assertFalse(database_path.exists())
        self.assertEqual(
            list(self.root.glob(f".{database_path.name}.init-*")),
            [],
        )

    def test_extra_global_fact_unique_index_is_rejected(self) -> None:
        database_path = self.root / "overconstrained-collection.db"
        OutcomeCollectionService(
            database_path,
            production_database=self.production,
        )
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                "CREATE UNIQUE INDEX unexpected_global_fact_id "
                "ON outcome_collection_events(fact_id)"
            )
            connection.commit()
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "index contracts",
        ):
            OutcomeCollectionService(
                database_path,
                production_database=self.production,
            )
        after = hashlib.sha256(database_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_executable_sqlite_schema_objects_are_rejected(self) -> None:
        with closing(sqlite3.connect(self.collection_path)) as connection:
            connection.executescript(
                """
                CREATE TRIGGER delete_collection_event
                AFTER INSERT ON outcome_collection_events
                BEGIN
                    DELETE FROM outcome_collection_events
                    WHERE append_order = NEW.append_order;
                END;
                """
            )
            connection.commit()
        before = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "forbidden executable schema objects",
        ):
            self.collection.audit()
        after = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_wal_journal_mode_is_rejected_without_rewriting_database(self) -> None:
        with closing(sqlite3.connect(self.collection_path)) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
        before = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "journal mode must remain DELETE",
        ):
            self.collection.audit()
        after = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_generated_or_hidden_columns_cannot_bypass_schema_identity(self) -> None:
        with closing(sqlite3.connect(self.collection_path)) as connection:
            connection.execute(
                "ALTER TABLE outcome_collection_events ADD COLUMN "
                "generated_probe TEXT GENERATED ALWAYS AS (event_type) VIRTUAL"
            )
            connection.commit()
        before = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "event schema is invalid",
        ):
            self.collection.audit()
        after = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_malformed_sqlite_file_is_rejected_through_collection_contract(self) -> None:
        database_path = self.root / "malformed-collection.db"
        database_path.write_bytes(b"SQLite format 3\x00" + bytes(4080))
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "schema validation failed",
        ):
            OutcomeCollectionService(
                database_path,
                production_database=self.production,
            )
        after = hashlib.sha256(database_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_sqlite_open_failure_is_normalized_without_database_change(self) -> None:
        before = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        with patch.object(
            collection_module.sqlite3,
            "connect",
            side_effect=sqlite3.OperationalError("forced open failure"),
        ), self.assertRaisesRegex(
            OutcomeCollectionError,
            "cannot open outcome collection database",
        ):
            self.collection.audit()
        after = hashlib.sha256(self.collection_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_legacy_schema_is_rejected_without_rewriting_database(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                database_path = self.root / f"legacy-v{version}-collection.db"
                OutcomeCollectionService(
                    database_path,
                    production_database=self.production,
                )
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute(
                        "UPDATE outcome_collection_meta SET value=? WHERE key='schema'",
                        (f"stage4g-outcome-collection-v{version}",),
                    )
                    connection.commit()
                before = hashlib.sha256(database_path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(
                    OutcomeCollectionError,
                    "schema identity",
                ):
                    OutcomeCollectionService(
                        database_path,
                        production_database=self.production,
                    )
                after = hashlib.sha256(database_path.read_bytes()).hexdigest()
                self.assertEqual(before, after)

    def test_open_service_rejects_collection_database_replacement(self) -> None:
        self.open_case()
        self.collection_path.unlink()
        OutcomeCollectionService(
            self.collection_path,
            production_database=self.production,
        )
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "replaced after opening",
        ):
            self.collection.audit()

    def test_connection_rechecks_database_identity_after_open_and_before_return(self) -> None:
        expected = self.collection._database_identity
        changed = (expected[0], expected[1] + 1)
        for identities in (
            (expected, changed),
            (expected, expected, changed),
        ):
            with self.subTest(check_count=len(identities)), patch.object(
                collection_module,
                "_path_identity",
                side_effect=identities,
            ), self.assertRaisesRegex(
                OutcomeCollectionError,
                "replaced after opening",
            ):
                self.collection.audit()
        self.assertEqual(self.collection.audit().event_count, 0)

    def test_collection_database_isolated_from_production_and_ledger(self) -> None:
        with self.assertRaisesRegex(OutcomeCollectionError, "production database"):
            OutcomeCollectionService(
                self.production,
                production_database=self.production,
            )
        alias = self.root / "production-alias.db"
        try:
            os.link(self.production, alias)
        except OSError:
            pass
        else:
            with self.assertRaisesRegex(OutcomeCollectionError, "production database"):
                OutcomeCollectionService(
                    alias,
                    production_database=self.production,
                )

        collection_on_catalog = OutcomeCollectionService(
            self.root / "separate-collection.db",
            production_database=self.production,
        )
        conflicting_ledger = OutcomeLedger(
            self.root / "different-records",
            self.root / "different-ledger.db",
            production_database=self.production,
        )
        conflicting_ledger.catalog_path = collection_on_catalog.database_path
        with self.assertRaisesRegex(
            OutcomeCollectionError,
            "outcome ledger target identity is invalid",
        ):
            collection_on_catalog.finalize(
                _hash("e"),
                conflicting_ledger,
                recorded_by="stage4g-test",
            )

    def test_independent_instances_serialize_concurrent_case_opening(self) -> None:
        services = tuple(
            OutcomeCollectionService(
                self.collection_path,
                production_database=self.production,
            )
            for _ in range(2)
        )
        signals = (_runtime_signal(suffix="a"), _runtime_signal(suffix="b"))
        episode_ids = (_hash("d"), _hash("e"))
        with patch.object(collection_module, "_utc_now", return_value=_BASE), ThreadPoolExecutor(
            max_workers=2
        ) as executor:
            futures = tuple(
                executor.submit(
                    service.open_case,
                    signal,
                    mode=OutcomeCollectionMode.PAPER,
                    **{
                        **_open_kwargs(),
                        "runtime_episode_fact_id": episode_id,
                    },
                )
                for service, signal, episode_id in zip(
                    services,
                    signals,
                    episode_ids,
                    strict=True,
                )
            )
            results = tuple(future.result(timeout=20) for future in futures)
        self.assertEqual(
            sorted(result.event.append_order for result in results),
            [1, 2],
        )
        with patch.object(
            collection_module,
            "_utc_now",
            return_value=_BASE + timedelta(seconds=1),
        ):
            audit = self.collection.audit()
        self.assertEqual(audit.event_count, 2)
        self.assertEqual(audit.case_count, 2)


if __name__ == "__main__":
    unittest.main()
