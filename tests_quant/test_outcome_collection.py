from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
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
        "strategy_version": "v1",
        "horizon_sessions": 20,
        "model_id": "model-v1",
        "instrument_id": "CN:SSE:600001",
        "identity_fact_id": _hash("1"),
        "data_snapshot_id": _hash("2"),
        "policy_id": _hash("3"),
        "classification_id": "C:SECTOR:TECH",
        "requested_quantity": 100,
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
    ):
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_entry_fill(
                case_id,
                timestamp=_BASE + timedelta(minutes=1),
                session_index=10,
                quantity=100,
                reference_price=Decimal(10),
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
                observable=True,
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
    ):
        observed_at = _BASE + timedelta(minutes=4)
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
    ):
        observed_at = _BASE + timedelta(minutes=5)
        with patch.object(collection_module, "_utc_now", return_value=observed_at):
            return self.collection.record_exit_fill(
                case_id,
                timestamp=observed_at,
                session_index=12,
                quantity=100,
                reference_price=Decimal(fill_price),
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
        signal = _runtime_signal(aware_state_time=False)
        result = self.open_case(signal=signal)
        case = self.collection.get_case(result.event.case_id)
        snapshot = case.snapshot
        self.assertFalse(snapshot.runtime_state_time_aware)
        self.assertTrue(snapshot.runtime_state_changed_at.endswith(".000000"))
        self.assertEqual(snapshot.entry_intent.decision_snapshot_id, snapshot.decision_snapshot_id)

        signal.reason = "mutable runtime reason changed"
        signal.scores.opportunity = 1
        signal.entry_low = 1.0
        rebuilt = RuntimeSignalSnapshot.from_dict(snapshot.as_dict())
        self.assertEqual(rebuilt, snapshot)
        self.assertEqual(rebuilt.reason, "结构与风险门已形成可审计候选")
        self.assertEqual(rebuilt.scores.opportunity, 80)
        self.assertEqual(rebuilt.entry_low, Decimal(10))
        with self.assertRaises(TypeError):
            replace(snapshot, decision_snapshot_id=_hash("f"))

    def test_snapshot_rejects_non_actionable_and_missing_identity(self) -> None:
        with self.assertRaisesRegex(OutcomeCollectionError, "not eligible"):
            self.open_case(signal=_runtime_signal(state=T.SignalState.WATCH))
        with self.assertRaisesRegex(OutcomeCollectionError, "lowercase SHA-256"):
            self.open_case(identity_fact_id="not-a-hash")
        broken = _runtime_signal()
        broken.scores = None
        with self.assertRaisesRegex(OutcomeCollectionError, "no score evidence"):
            self.open_case(signal=broken)

    def test_open_case_is_idempotent_but_identity_drift_conflicts(self) -> None:
        first = self.open_case()
        second = self.open_case(observed_at=_BASE + timedelta(minutes=1))
        self.assertIs(first.disposition, OutcomeCollectionDisposition.APPENDED)
        self.assertIs(second.disposition, OutcomeCollectionDisposition.IDEMPOTENT)
        self.assertEqual(first.event.event_hash, second.event.event_hash)
        next_episode = self.open_case(
            observed_at=_BASE + timedelta(minutes=2),
            strategy_version="v2",
        )
        self.assertIs(next_episode.disposition, OutcomeCollectionDisposition.APPENDED)
        self.assertNotEqual(first.event.case_id, next_episode.event.case_id)
        self.assertEqual(
            self.collection.get_case(first.event.case_id).snapshot.signal_id,
            self.collection.get_case(next_episode.event.case_id).snapshot.signal_id,
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
        self.assertEqual(audit.event_count, 8)
        self.assertEqual(
            dict(audit.state_counts),
            {OutcomeCollectionCaseState.FINALIZED: 1},
        )
        self.assertFalse(audit.as_dict()["investment_performance_claim"])
        self.assertEqual(self.production.read_bytes(), b"production-sentinel")

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
        with self.assertRaisesRegex(OutcomeCollectionError, "cannot be the outcome ledger"):
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
        with patch.object(collection_module, "_utc_now", return_value=_BASE), ThreadPoolExecutor(
            max_workers=2
        ) as executor:
            futures = tuple(
                executor.submit(
                    service.open_case,
                    signal,
                    mode=OutcomeCollectionMode.PAPER,
                    **_open_kwargs(),
                )
                for service, signal in zip(services, signals, strict=True)
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
