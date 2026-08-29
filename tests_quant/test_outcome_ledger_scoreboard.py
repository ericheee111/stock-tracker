from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from stock_tracker.core.types import Market
from stock_tracker.quant.core.outcomes import (
    OutcomeScoreboardPolicy,
    ScoreboardState,
)
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.storage import outcome_ledger as ledger_module
from stock_tracker.quant.storage.outcome_ledger import (
    OutcomeLedger,
    OutcomeLedgerError,
    OutcomeLedgerLane,
    write_outcome_scoreboard_json,
    write_outcome_scoreboard_markdown,
)
from tests_quant.test_outcomes import (
    _BASE,
    _complete_outcome,
    _hash,
    _no_entry_outcome,
)


class TestOutcomeLedgerScoreboard(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).absolute()
        self.ledger = OutcomeLedger(
            self.root / "records",
            self.root / "catalog.db",
            production_database=self.root / "production.db",
        )
        self._last_ingested_at = None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def append(self, outcome, *, ingested_at=None):
        observed = ingested_at or outcome.recorded_at + timedelta(minutes=1)
        if (
            ingested_at is None
            and self._last_ingested_at is not None
            and observed < self._last_ingested_at
        ):
            observed = self._last_ingested_at + timedelta(microseconds=1)
        with patch.object(ledger_module, "_utc_now", return_value=observed):
            result = self.ledger.append(
                outcome,
                recorded_by="scoreboard-fixture",
            )
        self._last_ingested_at = observed
        return result

    def materialize(
        self,
        *,
        minimum: int,
        as_of=_BASE + timedelta(days=10),
        observed_at=_BASE + timedelta(days=11),
    ):
        with patch.object(ledger_module, "_utc_now", return_value=observed_at):
            return self.ledger.materialize_scoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                window_start=_BASE - timedelta(days=1),
                window_end=_BASE + timedelta(days=9),
                as_of=as_of,
                policy=OutcomeScoreboardPolicy(
                    policy_version=f"stage4f-test-minimum-{minimum}",
                    minimum_real_samples=minimum,
                    minimum_bucket_samples=min(minimum, 2),
                    recent_window=20,
                ),
            )

    def test_exact_candidate_cohort_never_self_admits_real_metrics(self) -> None:
        first = _complete_outcome(signal_suffix="score-1")
        second = _complete_outcome(
            signal_suffix="score-2",
            recorded_offset_days=1,
            exit_price="8",
        )
        no_entry = replace(
            _no_entry_outcome(),
            verified=True,
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            verification_evidence_ids=(_hash("6"),),
        )
        other_model = _complete_outcome(
            signal_suffix="other-model",
            recorded_offset_days=3,
            model_id="other-model",
        )
        for outcome in (first, second, no_entry, other_model):
            self.append(outcome)

        snapshot = self.materialize(minimum=2)
        scoreboard = snapshot.scoreboard
        self.assertEqual(len(snapshot.candidate_records), 3)
        self.assertEqual(
            snapshot.candidate_outcome_ids,
            (first.outcome_id, second.outcome_id, no_entry.outcome_id),
        )
        self.assertEqual(
            snapshot.outcome_contract_eligible_ids,
            (first.outcome_id, second.outcome_id),
        )
        self.assertEqual(snapshot.outcome_contract_eligible_count, 2)
        self.assertEqual(
            snapshot.admission_blockers,
            ("TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED",),
        )
        self.assertEqual(snapshot.scoreboard_records, ())
        self.assertEqual(snapshot.record_hashes, ())
        self.assertIs(
            scoreboard.state,
            ScoreboardState.INSUFFICIENT_REAL_EVIDENCE,
        )
        self.assertEqual(scoreboard.outcomes, ())
        self.assertEqual(scoreboard.eligible_outcome_ids, ())
        self.assertEqual(scoreboard.excluded_counts, ())
        self.assertIsNone(scoreboard.metrics)
        self.assertIn("INSUFFICIENT_REAL_EVIDENCE:0/2", scoreboard.blockers)
        payload = snapshot.as_dict()
        self.assertEqual(
            payload["candidate_outcome_ids"],
            [first.outcome_id, second.outcome_id, no_entry.outcome_id],
        )
        self.assertEqual(
            payload["outcome_contract_eligible_ids"],
            [first.outcome_id, second.outcome_id],
        )
        self.assertEqual(payload["outcome_contract_eligible_count"], 2)
        self.assertFalse(payload["trusted_outcome_authority_configured"])
        self.assertFalse(payload["investment_performance_claim"])
        self.assertFalse(payload["auto_promote_model"])
        self.assertFalse(payload["auto_change_strategy_weight"])
        self.assertFalse(payload["auto_trade"])

    def test_minimum_sample_gate_keeps_metrics_absent(self) -> None:
        self.append(_complete_outcome(signal_suffix="insufficient"))
        snapshot = self.materialize(minimum=2)
        self.assertIs(
            snapshot.scoreboard.state,
            ScoreboardState.INSUFFICIENT_REAL_EVIDENCE,
        )
        self.assertIsNone(snapshot.scoreboard.metrics)
        self.assertEqual(snapshot.outcome_contract_eligible_count, 1)
        self.assertIn("INSUFFICIENT_REAL_EVIDENCE:0/2", snapshot.scoreboard.blockers)

    def test_late_ingested_record_cannot_enter_historical_scoreboard(self) -> None:
        outcome = _complete_outcome(signal_suffix="late-visible")
        self.append(outcome, ingested_at=_BASE + timedelta(days=12))
        snapshot = self.materialize(
            minimum=1,
            as_of=_BASE + timedelta(days=10),
            observed_at=_BASE + timedelta(days=13),
        )
        self.assertEqual(snapshot.scoreboard.outcomes, ())
        self.assertIs(
            snapshot.scoreboard.state,
            ScoreboardState.INSUFFICIENT_REAL_EVIDENCE,
        )

    def test_reports_and_derived_snapshot_identity_are_immutable(self) -> None:
        self.append(_complete_outcome(signal_suffix="report"))
        snapshot = self.materialize(minimum=2)
        json_path = self.root / "reports" / f"{snapshot.snapshot_id}.json"
        markdown_path = self.root / "reports" / f"{snapshot.snapshot_id}.md"
        write_outcome_scoreboard_json(snapshot, json_path)
        write_outcome_scoreboard_markdown(snapshot, markdown_path)
        write_outcome_scoreboard_json(snapshot, json_path)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["snapshot_id"], snapshot.snapshot_id)
        self.assertIn(snapshot.snapshot_id, markdown_path.read_text(encoding="utf-8"))
        json_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(OutcomeLedgerError, "immutable"):
            write_outcome_scoreboard_json(snapshot, json_path)
        with self.assertRaises(TypeError):
            replace(snapshot, snapshot_id="f" * 64)

    def test_concurrent_report_writers_cannot_overwrite_immutable_output(self) -> None:
        self.append(_complete_outcome(signal_suffix="report-race"))
        first = self.materialize(minimum=1)
        second = self.materialize(minimum=2)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        target = self.root / "reports" / "race.json"
        barrier = Barrier(2)
        real_link = ledger_module.os.link

        def synchronized_link(source, destination, *args, **kwargs):
            if Path(destination) == target:
                barrier.wait(timeout=10)
            return real_link(source, destination, *args, **kwargs)

        with patch.object(
            ledger_module.os,
            "link",
            side_effect=synchronized_link,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(write_outcome_scoreboard_json, first, target),
                executor.submit(write_outcome_scoreboard_json, second, target),
            )
            statuses: list[str] = []
            for future in futures:
                try:
                    future.result(timeout=20)
                    statuses.append("written")
                except OutcomeLedgerError:
                    statuses.append("blocked")

        self.assertCountEqual(statuses, ["written", "blocked"])
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn(payload["snapshot_id"], {first.snapshot_id, second.snapshot_id})
        self.assertEqual(list(target.parent.glob(".race.json.tmp-*")), [])

    def test_snapshot_rejects_record_hashes_from_a_different_outcome(self) -> None:
        selected = _complete_outcome(signal_suffix="snapshot-selected")
        unrelated = _complete_outcome(
            signal_suffix="snapshot-unrelated",
            recorded_offset_days=1,
            model_id="other-model",
        )
        self.append(selected)
        unrelated_result = self.append(unrelated)
        snapshot = self.materialize(minimum=1)
        with self.assertRaises((TypeError, OutcomeLedgerError)):
            replace(
                snapshot,
                record_hashes=(unrelated_result.record.record_hash,),
            )

    def test_report_time_is_observed_and_cannot_precede_as_of(self) -> None:
        self.assertNotIn(
            "generated_at",
            inspect.signature(OutcomeLedger.materialize_scoreboard).parameters,
        )
        self.append(_complete_outcome(signal_suffix="future-generation"))
        with self.assertRaisesRegex(OutcomeLedgerError, "as_of cannot be in the future"):
            self.materialize(
                minimum=1,
                as_of=_BASE + timedelta(days=7),
                observed_at=_BASE + timedelta(days=6),
            )

    def test_verified_no_entry_remains_live_candidate(self) -> None:
        candidate = replace(
            _no_entry_outcome(),
            verified=True,
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            verification_evidence_ids=(_hash("6"),),
        )
        result = self.append(candidate)
        self.assertIs(result.record.lane, OutcomeLedgerLane.LIVE_CANDIDATE)


if __name__ == "__main__":
    unittest.main()
