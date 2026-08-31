from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.data_quality.gate import DataQualityGate
from stock_tracker.features.engine import FeatureEngine
from stock_tracker.runtime_evidence.contracts import (
    RuntimeDecisionArtifact,
    RuntimeEvidenceContractError,
    build_runtime_decision_artifact,
)
from stock_tracker.runtime_evidence.store import (
    RuntimeArtifactAppendDisposition,
    RuntimeArtifactRecord,
    RuntimeArtifactStore,
    RuntimeArtifactStoreError,
)
from stock_tracker.runtime_evidence.worker import (
    RuntimeArtifactWorker,
    RuntimeArtifactWorkerStatus,
)
from stock_tracker.signals.manager import SignalManager
from stock_tracker.storage.db import close_all, get_connection
from stock_tracker.storage.repository import Repository
from stock_tracker.storage.runtime_migrations import (
    RuntimeMigrationError,
    audit_runtime_evidence_schema,
    migrate_runtime_database,
)

ROOT = Path(__file__).resolve().parents[1]


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_inputs(
    now: datetime, *, signal_id: str = "600519.SH:S1"
) -> tuple[T.Signal, T.Quote, list[T.Bar], T.DataQuality]:
    quote = T.Quote(
        symbol="600519.SH",
        market=T.Market.A,
        timestamp=now,
        name="fixture",
        open=100.0,
        high=108.0,
        low=99.0,
        close=105.0,
        last=105.0,
        prev_close=100.0,
        volume=1000,
        amount=100_000.0,
        source="synthetic_fixture",
        received_at=now,
        computed_at=now,
        displayed_at=now,
        data_status=T.DataStatus.LIVE,
    )
    bars = [
        T.Bar(
            symbol="600519.SH",
            market=T.Market.A,
            timestamp=now,
            open=100.0,
            high=108.0,
            low=99.0,
            close=105.0,
            volume=1000,
            amount=100_000.0,
            source="synthetic_fixture",
            quality_status=T.DataStatus.LIVE,
        )
    ]
    quality = T.DataQuality(T.QualityStatus.VALID, 100, [])
    signal = T.Signal(
        signal_id=signal_id,
        symbol="600519.SH",
        market=T.Market.A,
        strategy_id="S1",
        state=T.SignalState.WATCH,
        state_changed_at=now,
        reason="synthetic fixture",
        entry_low=100.0,
        entry_high=105.0,
        trigger_price=106.0,
        invalidation_price=95.0,
        target_1=110.0,
        target_2=120.0,
        reward_risk=2.0,
        freshness=1.0,
        market_regime="ROTATION",
        sector_stage="EARLY",
        next_trigger="fixture",
        what_changed=["fixture transition"],
        data_status=T.DataStatus.LIVE,
        scores=T.ScoreSet(
            opportunity=80,
            timing=70,
            risk=20,
            confidence=60,
            success_probability=None,
            positive_reasons=["fixture positive"],
            negative_reasons=["fixture negative"],
        ),
    )
    return signal, quote, bars, quality


class RuntimeEvidenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc)
        self.bundle = load_configs(str(ROOT / "config"))

    def tearDown(self) -> None:
        close_all()

    def temporary_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(close_all)
        return Path(temporary.name)

    def new_database(self, root: Path, *, migrate: bool = False) -> Path:
        database = root / "runtime.db"
        Repository(str(database))
        close_all()
        if migrate:
            migrate_runtime_database(
                database,
                apply=True,
                backup=root / "runtime.backup.db",
                now=self.now,
            )
        return database

    def artifact(self, *, signal_id: str = "600519.SH:S1") -> RuntimeDecisionArtifact:
        signal, quote, bars, quality = _runtime_inputs(self.now, signal_id=signal_id)
        return build_runtime_decision_artifact(
            signal=signal,
            quote=quote,
            bars=bars,
            data_quality=quality,
            bundle=self.bundle,
            decision_requested_at=self.now,
            observed_at=self.now,
            instrument_metadata={"exchange": "SSE"},
        )


class TestRuntimeDecisionArtifact(RuntimeEvidenceTestCase):
    def test_system_ids_are_canonical_and_occurrence_specific(self) -> None:
        first = self.artifact()
        second = self.artifact()
        self.assertEqual(first.artifact_id, first.runtime_episode_fact_id)
        self.assertEqual(first, RuntimeDecisionArtifact.from_json_bytes(first.to_json_bytes()))
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.outcome_case_status, "OUTCOME_EVIDENCE_PENDING")
        identity = first.identity_dict()
        self.assertFalse(identity["auto_trade"])
        self.assertTrue(
            identity["instrument_id"].startswith("UNRESOLVED_RUNTIME_IDENTITY:")
        )
        self.assertIn(
            "INSTRUMENT_IDENTITY_AUTHORITY_PENDING", identity["incomplete_reasons"]
        )
        self.assertTrue(all(value is None for value in identity["execution_rules"].values()))

    def test_strict_contract_rejects_extra_bool_numeric_and_noncanonical_json(self) -> None:
        document = self.artifact().as_dict()
        document["unexpected"] = "field"
        with self.assertRaises(RuntimeEvidenceContractError):
            RuntimeDecisionArtifact.from_dict(document)
        document = self.artifact().as_dict()
        document["scores"]["opportunity"] = True
        with self.assertRaises(RuntimeEvidenceContractError):
            RuntimeDecisionArtifact.from_dict(document)
        with self.assertRaises(RuntimeEvidenceContractError):
            RuntimeDecisionArtifact.from_json_bytes(b'{"runtime_episode_fact_id": "x"}')

    def test_legacy_naive_formal_time_fails_closed(self) -> None:
        signal, quote, bars, quality = _runtime_inputs(self.now)
        signal.state_changed_at = self.now.replace(tzinfo=None)
        with self.assertRaises(RuntimeEvidenceContractError) as caught:
            build_runtime_decision_artifact(
                signal=signal,
                quote=quote,
                bars=bars,
                data_quality=quality,
                bundle=self.bundle,
                decision_requested_at=self.now,
                observed_at=self.now,
            )
        self.assertEqual(caught.exception.code, "LEGACY_NAIVE_RUNTIME_TIME")


class TestRuntimeMigrationAndTransaction(RuntimeEvidenceTestCase):
    def test_migration_defaults_to_dry_run_and_apply_requires_backup(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        before = _sha256_file(database)
        report = migrate_runtime_database(database, now=self.now)
        self.assertEqual(report.mode, "DRY_RUN")
        self.assertFalse(report.database_modified)
        self.assertEqual(report.pending_versions, (1,))
        self.assertEqual(_sha256_file(database), before)
        with self.assertRaises(RuntimeMigrationError):
            migrate_runtime_database(database, apply=True, now=self.now)
        applied = migrate_runtime_database(
            database,
            apply=True,
            backup=root / "runtime.backup.db",
            now=self.now,
        )
        self.assertEqual(applied.mode, "APPLY")
        self.assertTrue(applied.database_modified)
        self.assertEqual(applied.current_version, 1)
        with closing(sqlite3.connect(database)) as connection:
            audit_runtime_evidence_schema(connection)

    def test_schema_audit_rejects_trigger_body_index_view_and_generated_column(self) -> None:
        mutations = (
            (
                "trigger",
                (
                    "DROP TRIGGER runtime_transition_outbox_no_update;"
                    "CREATE TRIGGER runtime_transition_outbox_no_update "
                    "BEFORE UPDATE ON runtime_transition_outbox BEGIN SELECT 1; END;"
                ),
            ),
            (
                "index",
                (
                    "DROP INDEX idx_runtime_transition_signal;"
                    "CREATE INDEX idx_runtime_transition_signal "
                    "ON runtime_transition_outbox(append_order);"
                ),
            ),
            ("view", "CREATE VIEW runtime_forbidden_view AS SELECT 1 AS value;"),
            (
                "generated",
                (
                    "ALTER TABLE runtime_outbox_delivery ADD COLUMN derived "
                    "INTEGER GENERATED ALWAYS AS (retry_count + 1) VIRTUAL;"
                ),
            ),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                root = self.temporary_root() / name
                root.mkdir()
                database = self.new_database(root, migrate=True)
                with closing(sqlite3.connect(database)) as connection:
                    connection.executescript(mutation)
                    with self.assertRaises(RuntimeMigrationError):
                        audit_runtime_evidence_schema(connection)

    def test_signal_history_and_immutable_outbox_commit_atomically(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        signal, _quote, _bars, _quality = _runtime_inputs(self.now)
        artifact = self.artifact()
        result = repository.persist_signal_decision(
            signal,
            changed=True,
            observed_at=self.now,
            artifact=artifact,
        )
        self.assertEqual(result.evidence_status, "OUTBOX_PENDING")
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0], 1
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE runtime_transition_outbox SET payload_json='{}' WHERE artifact_id=?",
                (artifact.artifact_id,),
            )
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM runtime_transition_outbox WHERE artifact_id=?",
                (artifact.artifact_id,),
            )
        connection.rollback()
        connection.execute(
            "UPDATE runtime_outbox_delivery SET retry_count=1 WHERE artifact_id=?",
            (artifact.artifact_id,),
        )
        connection.commit()
        self.assertEqual(
            connection.execute(
                "SELECT retry_count FROM runtime_outbox_delivery WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()[0],
            1,
        )

    def test_runtime_lane_failure_rolls_back_signal_and_history(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TRIGGER runtime_test_abort BEFORE INSERT ON runtime_transition_outbox "
                "BEGIN SELECT RAISE(ABORT, 'fixture abort'); END"
            )
        repository = Repository(str(database))
        signal, _quote, _bars, _quality = _runtime_inputs(self.now)
        with self.assertRaises(RuntimeMigrationError):
            repository.persist_signal_decision(
                signal,
                changed=True,
                observed_at=self.now,
                artifact=self.artifact(),
            )
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0], 0
        )


class TestRuntimeArtifactStoreAndWorker(RuntimeEvidenceTestCase):
    def new_store(
        self, root: Path, database: Path, clock: FrozenClock
    ) -> RuntimeArtifactStore:
        return RuntimeArtifactStore(
            root / "records",
            root / "artifact-catalog.db",
            production_database=database,
            clock=clock,
        )

    def enqueue(
        self, repository: Repository, artifact: RuntimeDecisionArtifact
    ) -> None:
        signal, _quote, _bars, _quality = _runtime_inputs(
            self.now, signal_id=artifact.identity_dict()["runtime_signal_id"]
        )
        repository.persist_signal_decision(
            signal,
            changed=True,
            observed_at=self.now,
            artifact=artifact,
        )

    def test_store_is_append_only_idempotent_and_identity_stable(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        store_id = store.store_id
        artifact = self.artifact()
        first = store.append(artifact)
        second = store.append(artifact)
        self.assertEqual(first.disposition, RuntimeArtifactAppendDisposition.APPENDED)
        self.assertEqual(second.disposition, RuntimeArtifactAppendDisposition.IDEMPOTENT)
        self.assertEqual(first.record.record_hash, second.record.record_hash)
        self.assertEqual(store.audit().record_count, 1)
        reopened = self.new_store(root, database, clock)
        self.assertEqual(reopened.store_id, store_id)
        self.assertEqual(reopened.get(artifact.artifact_id).record_hash, first.record.record_hash)

    def test_store_adopts_exact_crash_orphan_without_duplicate_effect(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        artifact = self.artifact()
        orphan = RuntimeArtifactRecord.create(
            append_order=1,
            store_id=store.store_id,
            artifact=artifact,
            stored_at=self.now,
            previous_record_hash="0" * 64,
        )
        target = root / "records" / orphan.record_file
        target.parent.mkdir(parents=True)
        target.write_bytes(orphan.to_json_bytes())
        result = store.append(artifact)
        self.assertEqual(result.disposition, RuntimeArtifactAppendDisposition.APPENDED)
        self.assertEqual(result.record.record_hash, orphan.record_hash)
        self.assertEqual(store.audit().record_count, 1)

    def test_store_rejects_record_root_containing_production_database(self) -> None:
        root = self.temporary_root()
        record_root = root / "records"
        production = record_root / "stock_tracker.db"
        Repository(str(production))
        close_all()
        with self.assertRaises(RuntimeArtifactStoreError):
            RuntimeArtifactStore(
                record_root,
                root / "artifact-catalog.db",
                production_database=production,
                clock=FrozenClock(self.now),
            )

    def test_store_rejects_inventory_and_exact_schema_tampering(self) -> None:
        for name in ("inventory", "trigger", "view", "generated"):
            with self.subTest(name=name):
                root = self.temporary_root() / name
                root.mkdir()
                database = self.new_database(root)
                clock = FrozenClock(self.now)
                store = self.new_store(root, database, clock)
                if name == "inventory":
                    rogue = root / "records" / "rogue.json"
                    rogue.write_bytes(b"{}")
                    with self.assertRaises(RuntimeArtifactStoreError):
                        store.audit()
                    continue
                with closing(
                    sqlite3.connect(root / "artifact-catalog.db")
                ) as connection:
                    if name == "trigger":
                        connection.executescript(
                            "DROP TRIGGER runtime_artifacts_no_update;"
                            "CREATE TRIGGER runtime_artifacts_no_update "
                            "BEFORE UPDATE ON runtime_artifacts BEGIN SELECT 1; END;"
                        )
                    elif name == "view":
                        connection.execute(
                            "CREATE VIEW forbidden_view AS SELECT 1 AS value"
                        )
                    else:
                        connection.execute(
                            "ALTER TABLE runtime_artifacts ADD COLUMN derived "
                            "INTEGER GENERATED ALWAYS AS (append_order + 1) VIRTUAL"
                        )
                with self.assertRaises(RuntimeArtifactStoreError):
                    store.audit()

    def test_worker_delivers_after_durable_append_and_audit(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        artifact = self.artifact()
        self.enqueue(repository, artifact)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        result = worker.run_once()
        self.assertEqual(result.status, RuntimeArtifactWorkerStatus.DELIVERED)
        self.assertEqual(result.cursor, 1)
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 1)
        self.assertEqual(store.audit().artifact_ids, (artifact.artifact_id,))
        self.assertEqual(worker.run_once().status, RuntimeArtifactWorkerStatus.IDLE)

    def test_audit_failure_retries_then_idempotently_delivers(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        artifact = self.artifact()
        self.enqueue(repository, artifact)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(
            repository, store, clock=clock, retry_delay_seconds=30
        )
        with mock.patch.object(
            store, "audit", side_effect=RuntimeArtifactStoreError("fixture audit failure")
        ):
            result = worker.run_once()
        self.assertEqual(result.status, RuntimeArtifactWorkerStatus.RETRY_SCHEDULED)
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)
        self.assertEqual(store.audit().record_count, 1)
        clock.advance(31)
        delivered = worker.run_once()
        self.assertEqual(delivered.status, RuntimeArtifactWorkerStatus.DELIVERED)
        self.assertEqual(delivered.cursor, 1)
        self.assertEqual(store.audit().record_count, 1)

    def test_poison_payload_is_quarantined_without_blocking_cursor(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        payload = "{}"
        artifact_id = hashlib.sha256(payload.encode()).hexdigest()
        transition_id = hashlib.sha256(b"transition").hexdigest()
        connection = get_connection(str(database))
        connection.execute(
            "INSERT INTO runtime_transition_outbox("
            "append_order,artifact_id,runtime_signal_id,transition_event_id,"
            "payload_json,payload_sha256,created_at) VALUES(1,?,?,?,?,?,?)",
            (
                artifact_id,
                "600519.SH:S1",
                transition_id,
                payload,
                artifact_id,
                "2026-09-01T01:02:03.000000Z",
            ),
        )
        connection.execute(
            "INSERT INTO runtime_outbox_delivery(artifact_id,status,retry_count) "
            "VALUES(?,'PENDING',0)",
            (artifact_id,),
        )
        connection.commit()
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        result = worker.run_once()
        self.assertEqual(result.status, RuntimeArtifactWorkerStatus.QUARANTINED)
        self.assertEqual(result.cursor, 1)
        self.assertEqual(store.audit().record_count, 0)
        status = connection.execute(
            "SELECT status FROM runtime_outbox_delivery WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()[0]
        self.assertEqual(status, "QUARANTINED")

    def test_expired_lease_replays_to_exactly_one_observable_artifact(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        artifact = self.artifact()
        self.enqueue(repository, artifact)
        clock = FrozenClock(self.now)
        claimed = repository.claim_runtime_outbox(
            worker_id="crashed-worker", now=clock.now(), lease_seconds=60
        )
        self.assertIsNotNone(claimed)
        clock.advance(61)
        store = self.new_store(root, database, clock)
        recovered = RuntimeArtifactWorker(
            repository, store, worker_id="recovered-worker", clock=clock
        )
        result = recovered.run_once()
        self.assertEqual(result.status, RuntimeArtifactWorkerStatus.DELIVERED)
        self.assertEqual(store.audit().record_count, 1)
        self.assertEqual(repository.runtime_outbox_cursor("recovered-worker"), 1)


class TestSignalManagerRuntimeEvidenceIsolation(RuntimeEvidenceTestCase):
    def manager(
        self, database: Path, clock: FrozenClock
    ) -> tuple[SignalManager, list[tuple[str, dict]]]:
        repository = Repository(str(database))
        store = MarketStore()
        manager = SignalManager(
            self.bundle,
            store,
            repository,
            None,
            FeatureEngine(self.bundle),
            DataQualityGate(self.bundle),
            clock=clock,
        )
        published: list[tuple[str, dict]] = []
        manager._bus = mock.Mock(
            publish=lambda topic, payload: published.append((topic, payload))
        )
        return manager, published

    def test_missing_migration_does_not_break_signal_persistence(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)
        manager._persist_signal_with_runtime_evidence(
            signal=signal,
            changed=True,
            quote=quote,
            bars=bars,
            dq=quality,
            decision_requested_at=self.now,
        )
        self.assertIn(signal.signal_id, manager.repo.load_signals())
        self.assertEqual(len(manager.repo.load_signal_history(signal.signal_id)), 1)
        evidence = [payload for topic, payload in published if topic == "runtime_evidence"]
        self.assertEqual(evidence[0]["status"], "UNAVAILABLE")
        self.assertFalse(evidence[0]["stage4g_case_opened"])

    def test_legacy_naive_signal_is_quarantined_and_never_opens_case(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)
        signal.state_changed_at = self.now.replace(tzinfo=None)
        manager._persist_signal_with_runtime_evidence(
            signal=signal,
            changed=True,
            quote=quote,
            bars=bars,
            dq=quality,
            decision_requested_at=self.now,
        )
        connection = get_connection(str(database))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_outbox_quarantine").fetchone()[0],
            1,
        )
        evidence = [payload for topic, payload in published if topic == "runtime_evidence"]
        self.assertEqual(evidence[0]["status"], "QUARANTINED")
        self.assertEqual(evidence[0]["error_code"], "LEGACY_NAIVE_RUNTIME_TIME")
        self.assertFalse(evidence[0]["stage4g_case_opened"])


if __name__ == "__main__":
    unittest.main()
