from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts.runtime_migrate import main as runtime_migrate_main
from stock_tracker import __main__ as stock_tracker_main
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.data_quality.gate import DataQualityGate
from stock_tracker.features.engine import FeatureEngine
from stock_tracker.runtime_evidence.contracts import (
    RuntimeDecisionArtifact,
    RuntimeDecisionDraft,
    RuntimeEvidenceContractError,
    build_runtime_decision_artifact,
    build_runtime_decision_draft,
    runtime_signal_version_id,
)
from stock_tracker.runtime_evidence.store import (
    RuntimeArtifactAppendDisposition,
    RuntimeArtifactFailureClass,
    RuntimeArtifactRecord,
    RuntimeArtifactStore,
    RuntimeArtifactStoreError,
)
from stock_tracker.runtime_evidence.worker import (
    RuntimeArtifactWorker,
    RuntimeArtifactWorkerStatus,
)
from stock_tracker.signals.manager import SignalManager
from stock_tracker.storage import runtime_migrations as runtime_migrations_module
from stock_tracker.storage.db import close_all, get_connection
from stock_tracker.storage.repository import (
    Repository,
    RuntimeDatabaseIntegrityError,
    RuntimeEvidenceUnavailableError,
    RuntimeOutboxConflict,
    RuntimeOutboxError,
    SignalPersistenceError,
)
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

    def runtime_store_id(self, database: Path) -> str:
        return Repository(str(database)).runtime_evidence_store_id()

    def draft(
        self,
        *,
        signal_id: str = "600519.SH:S1",
        runtime_store_id: str = "a" * 64,
        previous_signal: T.Signal | None = None,
        signal: T.Signal | None = None,
        quote: T.Quote | None = None,
        bars: list[T.Bar] | None = None,
        quality: T.DataQuality | None = None,
        decision_requested_at: datetime | None = None,
        observed_at: datetime | None = None,
        upstream_occurrence_id: str | None = None,
    ) -> RuntimeDecisionDraft:
        if signal is None or quote is None or bars is None or quality is None:
            default_signal, default_quote, default_bars, default_quality = _runtime_inputs(
                self.now,
                signal_id=signal_id,
            )
            signal = default_signal if signal is None else signal
            quote = default_quote if quote is None else quote
            bars = default_bars if bars is None else bars
            quality = default_quality if quality is None else quality
        return build_runtime_decision_draft(
            runtime_store_id=runtime_store_id,
            signal=signal,
            previous_signal=previous_signal,
            quote=quote,
            bars=bars,
            data_quality=quality,
            bundle=self.bundle,
            decision_requested_at=decision_requested_at or self.now,
            observed_at=observed_at or self.now,
            instrument_metadata={"exchange": "SSE"},
            upstream_occurrence_id=upstream_occurrence_id,
        )

    def artifact(
        self,
        *,
        transition_event_id: str = "b" * 64,
        **kwargs,
    ) -> RuntimeDecisionArtifact:
        return build_runtime_decision_artifact(
            draft=self.draft(**kwargs),
            transition_event_id=transition_event_id,
        )


class TestRuntimeDecisionArtifact(RuntimeEvidenceTestCase):
    def test_checkpoint_a3_declares_pit_and_fork_boundaries(self) -> None:
        identity = self.artifact().identity_dict()
        self.assertEqual(identity["pit_evidence_status"], "RUNTIME_MEMORY_ONLY")
        self.assertEqual(identity["bar_known_at_authority"], "NOT_AVAILABLE")
        self.assertIn(
            "BAR_KNOWN_AT_AUTHORITY_PENDING",
            identity["incomplete_reasons"],
        )
        self.assertIs(identity["single_active_runtime_store"], True)
        self.assertEqual(identity["fork_detection"], "LOCAL_APPEND_CHAIN_ONLY")
        self.assertEqual(identity["external_checkpoint"], "NOT_IMPLEMENTED")

    def test_system_ids_are_canonical_and_retry_deterministic(self) -> None:
        first = self.artifact()
        second = self.artifact()
        later_observation = self.artifact(
            observed_at=self.now + timedelta(seconds=1),
        )
        self.assertEqual(first.artifact_id, first.runtime_episode_fact_id)
        self.assertEqual(first, RuntimeDecisionArtifact.from_json_bytes(first.to_json_bytes()))
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(
            first.identity_dict()["transition_event_id"],
            later_observation.identity_dict()["transition_event_id"],
        )
        self.assertNotEqual(first.artifact_id, later_observation.artifact_id)
        self.assertEqual(first.decision_content_id, later_observation.decision_content_id)
        self.assertEqual(first.occurrence_dedup_id, later_observation.occurrence_dedup_id)
        self.assertEqual(first.outcome_case_status, "OUTCOME_EVIDENCE_PENDING")
        identity = first.identity_dict()
        self.assertEqual(identity["runtime_store_id"], "a" * 64)
        self.assertEqual(
            identity["signal_version_id"],
            runtime_signal_version_id(_runtime_inputs(self.now)[0]),
        )
        self.assertFalse(identity["auto_trade"])
        self.assertTrue(
            identity["instrument_id"].startswith("UNRESOLVED_RUNTIME_IDENTITY:")
        )
        self.assertIn(
            "INSTRUMENT_IDENTITY_AUTHORITY_PENDING", identity["incomplete_reasons"]
        )
        self.assertTrue(all(value is None for value in identity["execution_rules"].values()))
        for field_name in (
            "signal_snapshot",
            "data_snapshot",
            "policy_snapshot",
            "identity_snapshot",
            "classification_snapshot",
        ):
            self.assertIsInstance(identity[field_name], dict)

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
        with self.assertRaisesRegex(
            RuntimeEvidenceContractError,
            "non-placeholder",
        ):
            build_runtime_decision_artifact(
                draft=self.draft(),
                transition_event_id="0" * 64,
            )

    def test_snapshot_hashes_are_rebuilt_from_embedded_canonical_evidence(self) -> None:
        document = self.artifact().as_dict()
        document["data_snapshot"]["quote"]["last"] = 999.0
        with self.assertRaisesRegex(
            RuntimeEvidenceContractError,
            "data_snapshot_id mismatch",
        ):
            RuntimeDecisionArtifact.from_dict(document)

        document = self.artifact().as_dict()
        document["signal_snapshot"]["signal"]["reason"] = "tampered"
        with self.assertRaisesRegex(
            RuntimeEvidenceContractError,
            "signal_version_id mismatch",
        ):
            RuntimeDecisionArtifact.from_dict(document)

    def test_nested_snapshot_sources_require_exact_project_types(self) -> None:
        class ForgedBar(T.Bar):
            pass

        signal, quote, bars, quality = _runtime_inputs(self.now)
        forged_bar = ForgedBar(
            **{
                item.name: getattr(bars[0], item.name)
                for item in fields(T.Bar)
                if item.init
            }
        )
        with self.assertRaisesRegex(
            RuntimeEvidenceContractError,
            "bar instrument identity mismatch",
        ):
            self.artifact(
                signal=signal,
                quote=quote,
                bars=[forged_bar],
                quality=quality,
            )

        class ForgedMetadata(dict):
            pass

        with self.assertRaisesRegex(
            RuntimeEvidenceContractError,
            "instrument_metadata must be an exact dict",
        ):
            build_runtime_decision_draft(
                runtime_store_id="a" * 64,
                signal=signal,
                previous_signal=None,
                quote=quote,
                bars=bars,
                data_quality=quality,
                bundle=self.bundle,
                decision_requested_at=self.now,
                observed_at=self.now,
                instrument_metadata=ForgedMetadata(exchange="SSE"),
            )

    def test_legacy_naive_formal_time_fails_closed(self) -> None:
        signal, quote, bars, quality = _runtime_inputs(self.now)
        signal.state_changed_at = self.now.replace(tzinfo=None)
        with self.assertRaises(RuntimeEvidenceContractError) as caught:
            build_runtime_decision_draft(
                runtime_store_id="a" * 64,
                signal=signal,
                previous_signal=None,
                quote=quote,
                bars=bars,
                data_quality=quality,
                bundle=self.bundle,
                decision_requested_at=self.now,
                observed_at=self.now,
            )
        self.assertEqual(caught.exception.code, "LEGACY_NAIVE_RUNTIME_TIME")

    def test_decision_time_rejects_future_quote_and_bar_inputs(self) -> None:
        signal, quote, bars, quality = _runtime_inputs(self.now)
        for field_name in ("timestamp", "received_at", "computed_at"):
            with self.subTest(field_name=field_name), self.assertRaises(
                RuntimeEvidenceContractError
            ):
                self.draft(
                    signal=signal,
                    quote=replace(
                        quote,
                        **{field_name: self.now + timedelta(microseconds=1)},
                    ),
                    bars=bars,
                    quality=quality,
                )
        with self.assertRaises(RuntimeEvidenceContractError):
            self.draft(
                signal=signal,
                quote=replace(
                    quote,
                    displayed_at=self.now + timedelta(microseconds=1),
                ),
                bars=bars,
                quality=quality,
            )
        with self.assertRaises(RuntimeEvidenceContractError):
            self.draft(
                signal=signal,
                quote=quote,
                bars=[
                    replace(
                        bars[0],
                        timestamp=self.now + timedelta(microseconds=1),
                    )
                ],
                quality=quality,
            )

    def test_transition_and_previous_snapshot_times_are_strict(self) -> None:
        previous, quote, bars, quality = _runtime_inputs(self.now)
        current = replace(
            previous,
            state=T.SignalState.TRIGGERED,
            previous_state=T.SignalState.WATCH,
            state_changed_at=self.now + timedelta(seconds=1),
        )
        with self.assertRaises(RuntimeEvidenceContractError):
            self.draft(
                signal=current,
                previous_signal=previous,
                quote=quote,
                bars=bars,
                quality=quality,
                decision_requested_at=self.now,
                observed_at=self.now + timedelta(seconds=1),
            )
        with self.assertRaises(RuntimeEvidenceContractError):
            self.draft(
                signal=replace(current, state_changed_at=self.now),
                previous_signal=replace(
                    previous,
                    state_changed_at=self.now + timedelta(microseconds=1),
                ),
                quote=quote,
                bars=bars,
                quality=quality,
            )
        with self.assertRaises(RuntimeEvidenceContractError):
            self.draft(
                signal=replace(current, state_changed_at=self.now),
                previous_signal=replace(previous, strategy_id="S2"),
                quote=quote,
                bars=bars,
                quality=quality,
            )

    def test_old_schema_and_missing_bar_authority_reason_fail_closed(self) -> None:
        old_schema = self.artifact().as_dict()
        old_schema["schema"] = "stage4g1-runtime-decision-artifact-v3"
        with self.assertRaises(RuntimeEvidenceContractError):
            RuntimeDecisionArtifact.from_dict(old_schema)
        missing_reason = self.artifact().as_dict()
        missing_reason["incomplete_reasons"].remove(
            "BAR_KNOWN_AT_AUTHORITY_PENDING"
        )
        with self.assertRaises(RuntimeEvidenceContractError):
            RuntimeDecisionArtifact.from_dict(missing_reason)

    def test_quote_bar_and_data_quality_subclasses_fail_closed(self) -> None:
        signal, quote, bars, quality = _runtime_inputs(self.now)

        class ForgedQuote(T.Quote):
            pass

        class ForgedBar(T.Bar):
            pass

        class ForgedQuality(T.DataQuality):
            pass

        forged_quote = ForgedQuote(
            **{item.name: getattr(quote, item.name) for item in fields(T.Quote) if item.init}
        )
        forged_bar = ForgedBar(
            **{item.name: getattr(bars[0], item.name) for item in fields(T.Bar) if item.init}
        )
        forged_quality = ForgedQuality(
            **{
                item.name: getattr(quality, item.name)
                for item in fields(T.DataQuality)
                if item.init
            }
        )
        for name, candidate_quote, candidate_bars, candidate_quality in (
            ("quote", forged_quote, bars, quality),
            ("bar", quote, [forged_bar], quality),
            ("quality", quote, bars, forged_quality),
        ):
            with self.subTest(name=name), self.assertRaises(
                RuntimeEvidenceContractError
            ):
                self.draft(
                    signal=signal,
                    quote=candidate_quote,
                    bars=candidate_bars,
                    quality=candidate_quality,
                )


class TestRuntimeMigrationAndTransaction(RuntimeEvidenceTestCase):
    def test_checkpoint_a3_latest_schema_has_delivery_and_occurrence_bindings(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        connection = get_connection(str(database))
        self.assertEqual(runtime_migrations_module.RUNTIME_OUTBOX_SCHEMA_VERSION, 3)
        self.assertEqual(
            {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('runtime_artifact_delivery_binding',"
                    "'runtime_artifact_integrity_event')"
                )
            },
            {
                "runtime_artifact_delivery_binding",
                "runtime_artifact_integrity_event",
            },
        )
        occurrence_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_xinfo(runtime_transition_occurrence)"
            )
        }
        self.assertTrue(
            {
                "signal_history_sha256",
                "occurrence_append_order",
                "previous_occurrence_hash",
                "occurrence_hash",
            }.issubset(occurrence_columns)
        )
        occurrence_uniques = {
            tuple(
                str(item[1])
                for item in connection.execute(
                    "SELECT seqno,name FROM pragma_index_info(?) ORDER BY seqno",
                    (str(index_row[1]),),
                ).fetchall()
            )
            for index_row in connection.execute(
                "PRAGMA index_list(runtime_transition_occurrence)"
            ).fetchall()
            if str(index_row[3]) == "u"
        }
        self.assertIn(("signal_history_id",), occurrence_uniques)

    def _install_runtime_version(self, database: Path, version: int) -> None:
        self.assertIn(version, (1, 2))
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in runtime_migrations_module._MIGRATIONS[:version]:
                connection.executescript(migration.path.read_text(encoding="utf-8"))
                if migration.version == 2:
                    connection.executemany(
                        "INSERT INTO runtime_evidence_meta(key,value) VALUES(?,?)",
                        (
                            (
                                "schema",
                                runtime_migrations_module.RUNTIME_EVIDENCE_STORE_SCHEMA,
                            ),
                            ("runtime_store_id", "1" * 64),
                        ),
                    )
                connection.execute(
                    "INSERT INTO runtime_schema_migration(version,name,checksum,applied_at) "
                    "VALUES(?,?,?,?)",
                    (
                        migration.version,
                        migration.name,
                        runtime_migrations_module._migration_checksum(migration),
                        runtime_migrations_module._utc_text(self.now),
                    ),
                )
            connection.commit()

    def _install_runtime_v1(self, database: Path) -> None:
        self._install_runtime_version(database, 1)

    def test_migration_defaults_to_dry_run_and_apply_requires_backup(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        before = _sha256_file(database)
        report = migrate_runtime_database(database, now=self.now)
        self.assertEqual(report.mode, "DRY_RUN")
        self.assertFalse(report.database_modified)
        self.assertEqual(report.pending_versions, (1, 2, 3))
        self.assertEqual(report.target_schema_state, "UNINITIALIZED")
        self.assertTrue(report.target_state_audit_passed)
        self.assertFalse(report.target_latest_schema_ready)
        self.assertTrue(report.rehearsal_performed)
        self.assertTrue(report.rehearsal_latest_schema_passed)
        self.assertTrue(report.migration_history_valid)
        self.assertEqual(report.current_version, 0)
        self.assertEqual(report.latest_version, 3)
        self.assertEqual(report.source_database_sha256, before)
        self.assertTrue(report.would_modify)
        self.assertIsNone(report.runtime_store_id)
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
        self.assertEqual(applied.current_version, 3)
        self.assertEqual(applied.target_schema_state, "VALID_LATEST_VERSION")
        self.assertTrue(applied.target_state_audit_passed)
        self.assertTrue(applied.rehearsal_latest_schema_passed)
        self.assertIsNotNone(applied.runtime_store_id)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                audit_runtime_evidence_schema(connection),
                applied.runtime_store_id,
            )
        verified = migrate_runtime_database(database, now=self.now)
        self.assertTrue(verified.target_state_audit_passed)
        self.assertEqual(verified.runtime_store_id, applied.runtime_store_id)
        self.assertEqual(verified.pending_versions, ())

    def test_empty_v1_and_v2_upgrade_to_latest_with_exact_history(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                root = self.temporary_root() / f"v{version}"
                root.mkdir()
                database = self.new_database(root)
                self._install_runtime_version(database, version)
                report = migrate_runtime_database(database, now=self.now)
                self.assertEqual(report.current_version, version)
                self.assertEqual(
                    report.pending_versions,
                    tuple(range(version + 1, 4)),
                )
                applied = migrate_runtime_database(
                    database,
                    apply=True,
                    backup=root / "backup.db",
                    now=self.now,
                )
                self.assertEqual(applied.current_version, 3)
                with closing(sqlite3.connect(database)) as connection:
                    rows = connection.execute(
                        "SELECT version,name,checksum FROM runtime_schema_migration "
                        "ORDER BY version"
                    ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        (
                            migration.version,
                            migration.name,
                            runtime_migrations_module._migration_checksum(migration),
                        )
                        for migration in runtime_migrations_module._MIGRATIONS
                    ],
                )

    def test_nonempty_legacy_v1_and_v2_require_export_without_mutation(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                root = self.temporary_root() / f"legacy-v{version}"
                root.mkdir()
                database = self.new_database(root)
                self._install_runtime_version(database, version)
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        "INSERT INTO runtime_transition_outbox("
                        "append_order,artifact_id,runtime_signal_id,transition_event_id,"
                        "payload_json,payload_sha256,created_at) VALUES(1,?,?,?,?,?,?)",
                        (
                            "a" * 64,
                            "600519.SH:S1",
                            "b" * 64,
                            "{}",
                            hashlib.sha256(b"{}").hexdigest(),
                            self.now.isoformat().replace("+00:00", "Z"),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO runtime_outbox_delivery(artifact_id,status,retry_count) "
                        "VALUES(?,'PENDING',0)",
                        ("a" * 64,),
                    )
                    connection.commit()
                before = database.read_bytes()
                with self.assertRaises(RuntimeMigrationError) as caught:
                    migrate_runtime_database(database, now=self.now)
                self.assertEqual(
                    caught.exception.code,
                    "LEGACY_RUNTIME_EVIDENCE_REQUIRES_EXPORT",
                )
                self.assertEqual(database.read_bytes(), before)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM runtime_transition_outbox"
                        ).fetchone()[0],
                        1,
                    )

    def test_failed_apply_rolls_back_database_bytes(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        before_database = database.read_bytes()
        wal = Path(str(database) + "-wal")
        before_wal = wal.read_bytes() if wal.exists() else None
        original = runtime_migrations_module._apply_pending_in_transaction
        calls = 0

        def fail_real_apply(connection, current_version, applied_at):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(connection, current_version, applied_at)
            connection.execute("CREATE TABLE runtime_forced_rollback(value TEXT)")
            raise RuntimeMigrationError("forced real apply failure")

        with mock.patch.object(
            runtime_migrations_module,
            "_apply_pending_in_transaction",
            side_effect=fail_real_apply,
        ), self.assertRaises(RuntimeMigrationError):
            migrate_runtime_database(
                database,
                apply=True,
                backup=root / "rollback.backup.db",
                now=self.now,
            )
        self.assertEqual(database.read_bytes(), before_database)
        self.assertEqual(wal.read_bytes() if wal.exists() else None, before_wal)

    def test_wal_logical_snapshot_and_backup_include_committed_uncheckpointed_data(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO wal_probe(value) VALUES('committed-in-wal')")
        writer.commit()
        wal_path = Path(str(database) + "-wal")
        self.assertTrue(wal_path.is_file())
        source_file_sha = _sha256_file(database)
        source_wal = wal_path.read_bytes()
        expected_logical_sha = hashlib.sha256(writer.serialize()).hexdigest()

        dry_run = migrate_runtime_database(database, now=self.now)
        self.assertEqual(dry_run.source_database_sha256, source_file_sha)
        self.assertEqual(dry_run.source_logical_sha256, expected_logical_sha)
        self.assertEqual(
            dry_run.source_wal_sha256,
            hashlib.sha256(source_wal).hexdigest(),
        )
        self.assertTrue(dry_run.rehearsal_latest_schema_passed)
        self.assertIsNone(dry_run.migration_block_code)
        self.assertEqual(_sha256_file(database), source_file_sha)
        self.assertEqual(wal_path.read_bytes(), source_wal)

        backup = root / "runtime-wal.backup.db"
        applied = migrate_runtime_database(
            database,
            apply=True,
            backup=backup,
            now=self.now,
        )
        self.assertTrue(applied.database_modified)
        self.assertTrue(applied.rehearsal_latest_schema_passed)
        self.assertEqual(applied.source_logical_sha256, expected_logical_sha)
        with closing(sqlite3.connect(backup)) as backup_connection:
            self.assertEqual(
                backup_connection.execute("SELECT value FROM wal_probe").fetchone()[0],
                "committed-in-wal",
            )
        with closing(sqlite3.connect(database)) as migrated_connection:
            self.assertEqual(
                migrated_connection.execute("SELECT value FROM wal_probe").fetchone()[0],
                "committed-in-wal",
            )
            self.assertIsInstance(audit_runtime_evidence_schema(migrated_connection), str)

    def test_runtime_migration_cli_emits_machine_readable_success_and_failure(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root)
        standard = io.StringIO()
        with redirect_stdout(standard):
            exit_code = runtime_migrate_main(["--database", str(database)])
        self.assertEqual(exit_code, 0)
        success = json.loads(standard.getvalue())
        for field_name in (
            "target_schema_state",
            "target_state_audit_passed",
            "target_latest_schema_ready",
            "rehearsal_performed",
            "rehearsal_latest_schema_passed",
            "migration_history_valid",
            "current_version",
            "latest_version",
            "pending_versions",
            "source_database_sha256",
            "source_wal_sha256",
            "would_modify",
            "database_modified",
        ):
            self.assertIn(field_name, success)

        invalid_root = root / "invalid"
        invalid_root.mkdir()
        invalid = self.new_database(invalid_root, migrate=True)
        with closing(sqlite3.connect(invalid)) as connection:
            connection.execute("CREATE VIEW runtime_invalid_view AS SELECT 1 AS value")
            connection.commit()
        error = io.StringIO()
        with redirect_stderr(error):
            exit_code = runtime_migrate_main(["--database", str(invalid)])
        self.assertEqual(exit_code, 2)
        failure = json.loads(error.getvalue())
        self.assertEqual(failure["target_schema_state"], "INVALID")
        self.assertFalse(failure["target_state_audit_passed"])
        self.assertFalse(failure["rehearsal_performed"])

    def test_dry_run_audits_the_actual_target_before_rehearsal(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                "DROP TRIGGER runtime_transition_outbox_no_update;"
                "CREATE TRIGGER runtime_transition_outbox_no_update "
                "BEFORE UPDATE ON runtime_transition_outbox BEGIN SELECT 1; END;"
            )
            connection.commit()
        before = _sha256_file(database)
        with self.assertRaises(RuntimeMigrationError) as caught:
            migrate_runtime_database(database, now=self.now)
        self.assertEqual(caught.exception.report.target_schema_state, "INVALID")
        self.assertFalse(caught.exception.report.target_state_audit_passed)
        self.assertFalse(caught.exception.report.rehearsal_performed)
        self.assertEqual(_sha256_file(database), before)

    def test_migration_reports_current_latest_partial_unknown_and_rehearsal(self) -> None:
        current_root = self.temporary_root() / "current"
        current_root.mkdir()
        current_database = self.new_database(current_root)
        self._install_runtime_v1(current_database)
        current_before = _sha256_file(current_database)
        current = migrate_runtime_database(current_database, now=self.now)
        self.assertEqual(current.target_schema_state, "VALID_CURRENT_VERSION")
        self.assertTrue(current.target_state_audit_passed)
        self.assertTrue(current.migration_history_valid)
        self.assertEqual(current.current_version, 1)
        self.assertEqual(current.pending_versions, (2, 3))
        self.assertEqual(_sha256_file(current_database), current_before)

        legacy_data_root = self.temporary_root() / "legacy-data"
        legacy_data_root.mkdir()
        legacy_data_database = self.new_database(legacy_data_root)
        self._install_runtime_v1(legacy_data_database)
        with closing(sqlite3.connect(legacy_data_database)) as connection:
            connection.execute(
                "INSERT INTO runtime_transition_outbox("
                "append_order,artifact_id,runtime_signal_id,transition_event_id,"
                "payload_json,payload_sha256,created_at) VALUES(1,?,?,?,?,?,?)",
                (
                    "a" * 64,
                    "600519.SH:S1",
                    "b" * 64,
                    "{}",
                    hashlib.sha256(b"{}").hexdigest(),
                    self.now.isoformat().replace("+00:00", "Z"),
                ),
            )
            connection.execute(
                "INSERT INTO runtime_outbox_delivery(artifact_id,status,retry_count) "
                "VALUES(?,'PENDING',0)",
                ("a" * 64,),
            )
            connection.commit()
        legacy_before = _sha256_file(legacy_data_database)
        with self.assertRaises(RuntimeMigrationError) as legacy_caught:
            migrate_runtime_database(legacy_data_database, now=self.now)
        self.assertEqual(
            legacy_caught.exception.code,
            "LEGACY_RUNTIME_EVIDENCE_REQUIRES_EXPORT",
        )
        self.assertFalse(legacy_caught.exception.report.rehearsal_latest_schema_passed)
        self.assertEqual(
            legacy_caught.exception.report.migration_block_code,
            "LEGACY_RUNTIME_EVIDENCE_REQUIRES_EXPORT",
        )
        self.assertFalse(legacy_caught.exception.report.rehearsal_performed)
        self.assertEqual(_sha256_file(legacy_data_database), legacy_before)

        tampered_root = self.temporary_root() / "history-tamper"
        tampered_root.mkdir()
        tampered_database = self.new_database(tampered_root)
        self._install_runtime_v1(tampered_database)
        with closing(sqlite3.connect(tampered_database)) as connection:
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='runtime_schema_migration_no_update'"
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER runtime_schema_migration_no_update")
            connection.execute(
                "UPDATE runtime_schema_migration SET checksum='tampered' WHERE version=1"
            )
            connection.execute(trigger_sql)
            connection.commit()
        with self.assertRaises(RuntimeMigrationError) as tampered_caught:
            migrate_runtime_database(tampered_database, now=self.now)
        self.assertEqual(tampered_caught.exception.report.target_schema_state, "INVALID")
        self.assertFalse(tampered_caught.exception.report.migration_history_valid)
        self.assertFalse(tampered_caught.exception.report.rehearsal_performed)

        latest_root = self.temporary_root() / "latest"
        latest_root.mkdir()
        latest_database = self.new_database(latest_root, migrate=True)
        latest = migrate_runtime_database(latest_database, now=self.now)
        self.assertEqual(latest.target_schema_state, "VALID_LATEST_VERSION")
        self.assertEqual(latest.pending_versions, ())
        self.assertFalse(latest.would_modify)

        partial_root = self.temporary_root() / "partial"
        partial_root.mkdir()
        partial_database = self.new_database(partial_root)
        with closing(sqlite3.connect(partial_database)) as connection:
            connection.execute("CREATE TABLE runtime_partial(value TEXT)")
            connection.commit()
        with self.assertRaises(RuntimeMigrationError) as partial_caught:
            migrate_runtime_database(partial_database, now=self.now)
        self.assertEqual(
            partial_caught.exception.report.target_schema_state,
            "PARTIALLY_MIGRATED",
        )

        unknown_root = self.temporary_root() / "unknown"
        unknown_root.mkdir()
        unknown_database = self.new_database(unknown_root)
        self._install_runtime_v1(unknown_database)
        with closing(sqlite3.connect(unknown_database)) as connection:
            connection.execute(
                "INSERT INTO runtime_schema_migration(version,name,checksum,applied_at) "
                "VALUES(99,'unknown','unknown',?)",
                (self.now.isoformat().replace("+00:00", "Z"),),
            )
            connection.commit()
        with self.assertRaises(RuntimeMigrationError) as unknown_caught:
            migrate_runtime_database(unknown_database, now=self.now)
        self.assertEqual(
            unknown_caught.exception.report.target_schema_state,
            "UNKNOWN_VERSION",
        )

        rehearsal_root = self.temporary_root() / "rehearsal"
        rehearsal_root.mkdir()
        rehearsal_database = self.new_database(rehearsal_root)
        rehearsal_before = _sha256_file(rehearsal_database)
        with mock.patch.object(
            runtime_migrations_module,
            "_apply_pending",
            side_effect=RuntimeMigrationError("fixture rehearsal failure"),
        ), self.assertRaises(RuntimeMigrationError) as rehearsal_caught:
            migrate_runtime_database(rehearsal_database, now=self.now)
        rehearsal_report = rehearsal_caught.exception.report
        self.assertTrue(rehearsal_report.target_state_audit_passed)
        self.assertTrue(rehearsal_report.rehearsal_performed)
        self.assertFalse(rehearsal_report.rehearsal_latest_schema_passed)
        self.assertEqual(_sha256_file(rehearsal_database), rehearsal_before)

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
        signal, quote, bars, quality = _runtime_inputs(self.now)
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id(),
            signal=signal,
            quote=quote,
            bars=bars,
            quality=quality,
        )
        result = repository.persist_signal_decision(
            signal,
            changed=True,
            observed_at=self.now,
            expected_previous_signal_version_id=None,
            decision_draft=draft,
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
                (result.artifact_id,),
            )
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM runtime_transition_outbox WHERE artifact_id=?",
                (result.artifact_id,),
            )
        connection.rollback()
        connection.execute(
            "UPDATE runtime_outbox_delivery SET retry_count=1 WHERE artifact_id=?",
            (result.artifact_id,),
        )
        connection.commit()
        self.assertEqual(
            connection.execute(
                "SELECT retry_count FROM runtime_outbox_delivery WHERE artifact_id=?",
                (result.artifact_id,),
            ).fetchone()[0],
            1,
        )

    def test_runtime_connections_enforce_occurrence_foreign_keys(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        signal, quote, bars, quality = _runtime_inputs(self.now)
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id(),
            signal=signal,
            quote=quote,
            bars=bars,
            quality=quality,
        )
        repository.persist_signal_decision(
            signal,
            changed=True,
            observed_at=self.now,
            expected_previous_signal_version_id=None,
            decision_draft=draft,
        )
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        history_id = connection.execute(
            "SELECT signal_history_id FROM runtime_transition_occurrence"
        ).fetchone()[0]
        artifact_id = connection.execute(
            "SELECT artifact_id FROM runtime_transition_occurrence"
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE signal_history SET reason='tampered' WHERE id=?",
                (history_id,),
            )
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM signal_history WHERE id=?", (history_id,))
        connection.rollback()
        connection.execute(
            "INSERT INTO runtime_transition_outbox("
            "append_order,artifact_id,runtime_signal_id,transition_event_id,"
            "payload_json,payload_sha256,created_at) VALUES(2,?,?,?,?,?,?)",
            (
                "2" * 64,
                "600519.SH:S2",
                "3" * 64,
                "{}",
                hashlib.sha256(b"{}").hexdigest(),
                self.now.isoformat().replace("+00:00", "Z"),
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runtime_transition_occurrence("
                "occurrence_order,occurrence_dedup_id,decision_content_id,artifact_id,"
                "transition_event_id,upstream_occurrence_id,signal_history_id,created_at,"
                "signal_history_sha256,occurrence_append_order,previous_occurrence_hash,"
                "occurrence_hash) VALUES(2,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "4" * 64,
                    "5" * 64,
                    "2" * 64,
                    "3" * 64,
                    None,
                    history_id + 10_000,
                    self.now.isoformat().replace("+00:00", "Z"),
                    "6" * 64,
                    2,
                    "7" * 64,
                    "8" * 64,
                ),
            )
        connection.rollback()
        self.assertEqual(
            connection.execute(
                "SELECT signal_history_id FROM runtime_transition_occurrence "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()[0],
            history_id,
        )

    def test_occurrence_append_chain_survives_backup_and_detects_tamper(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        for signal_id in ("600519.SH:S1", "600519.SH:S2"):
            signal, quote, bars, quality = _runtime_inputs(
                self.now,
                signal_id=signal_id,
            )
            draft = self.draft(
                runtime_store_id=runtime_store_id,
                signal=signal,
                quote=quote,
                bars=bars,
                quality=quality,
            )
            repository.persist_signal_decision(
                signal,
                changed=True,
                observed_at=self.now,
                expected_previous_signal_version_id=None,
                decision_draft=draft,
            )
        connection = get_connection(str(database))
        chain = connection.execute(
            "SELECT occurrence_append_order,previous_occurrence_hash,occurrence_hash,"
            "signal_history_sha256 FROM runtime_transition_occurrence "
            "ORDER BY occurrence_append_order"
        ).fetchall()
        self.assertEqual([int(row[0]) for row in chain], [1, 2])
        self.assertEqual(str(chain[0][1]), "0" * 64)
        self.assertEqual(str(chain[1][1]), str(chain[0][2]))
        for row in chain:
            self.assertRegex(str(row[2]), r"^[0-9a-f]{64}$")
            self.assertRegex(str(row[3]), r"^[0-9a-f]{64}$")
        close_all()
        backup = root / "restored.db"
        with closing(sqlite3.connect(database)) as source, closing(
            sqlite3.connect(backup)
        ) as target:
            source.backup(target)
        restored = Repository(str(backup))
        self.assertEqual(restored.runtime_evidence_store_id(), runtime_store_id)
        restored_chain = get_connection(str(backup)).execute(
            "SELECT occurrence_append_order,previous_occurrence_hash,occurrence_hash "
            "FROM runtime_transition_occurrence ORDER BY occurrence_append_order"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in restored_chain],
            [tuple(row[:3]) for row in chain],
        )
        close_all()
        with closing(sqlite3.connect(database)) as tampered:
            trigger_sql = str(
                tampered.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='runtime_transition_occurrence_no_update'"
                ).fetchone()[0]
            )
            tampered.execute("DROP TRIGGER runtime_transition_occurrence_no_update")
            tampered.execute(
                "UPDATE runtime_transition_occurrence SET occurrence_hash=? "
                "WHERE occurrence_append_order=2",
                ("f" * 64,),
            )
            tampered.execute(trigger_sql)
            tampered.commit()
        with self.assertRaises(RuntimeDatabaseIntegrityError):
            Repository(str(database)).runtime_evidence_store_id()

    def test_history_hash_tamper_and_foreign_key_check_fail_closed(self) -> None:
        for corruption in ("history", "foreign_key"):
            with self.subTest(corruption=corruption):
                root = self.temporary_root() / corruption
                root.mkdir()
                database = self.new_database(root, migrate=True)
                repository = Repository(str(database))
                signal, quote, bars, quality = _runtime_inputs(self.now)
                draft = self.draft(
                    runtime_store_id=repository.runtime_evidence_store_id(),
                    signal=signal,
                    quote=quote,
                    bars=bars,
                    quality=quality,
                )
                repository.persist_signal_decision(
                    signal,
                    changed=True,
                    observed_at=self.now,
                    expected_previous_signal_version_id=None,
                    decision_draft=draft,
                )
                clock = FrozenClock(self.now)
                store = RuntimeArtifactStore(
                    root / "records",
                    root / "artifact-catalog.db",
                    source_runtime_store_id=repository.runtime_evidence_store_id(),
                    production_database=database,
                    clock=clock,
                )
                close_all()
                with closing(sqlite3.connect(database)) as connection:
                    connection.row_factory = sqlite3.Row
                    trigger_name = (
                        "runtime_bound_signal_history_no_update"
                        if corruption == "history"
                        else "runtime_transition_occurrence_no_update"
                    )
                    trigger_sql = str(
                        connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                            (trigger_name,),
                        ).fetchone()[0]
                    )
                    connection.execute(f"DROP TRIGGER {trigger_name}")
                    if corruption == "history":
                        connection.execute(
                            "UPDATE signal_history SET reason='tampered history'"
                        )
                    else:
                        connection.execute(
                            "UPDATE runtime_transition_occurrence "
                            "SET signal_history_id=999999"
                        )
                    connection.execute(trigger_sql)
                    connection.commit()
                    if corruption == "foreign_key":
                        self.assertIsNotNone(
                            connection.execute("PRAGMA foreign_key_check").fetchone()
                        )
                corrupted_repository = Repository(str(database))
                with self.assertRaises(RuntimeDatabaseIntegrityError):
                    corrupted_repository.runtime_evidence_store_id()
                blocked = RuntimeArtifactWorker(
                    corrupted_repository,
                    store,
                    clock=clock,
                ).run_once()
                self.assertEqual(
                    blocked.status,
                    RuntimeArtifactWorkerStatus.HARD_BLOCKED,
                )

    def test_transition_cas_is_idempotent_and_rejects_stale_concurrency(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        previous, quote, bars, quality = _runtime_inputs(self.now)
        repository.persist_signal_decision(
            previous,
            changed=True,
            observed_at=self.now,
            expected_previous_signal_version_id=None,
        )
        previous_version = runtime_signal_version_id(previous)
        target = replace(
            previous,
            state=T.SignalState.TRIGGERED,
            previous_state=T.SignalState.WATCH,
            state_changed_at=self.now + timedelta(seconds=1),
            reason="triggered",
        )
        draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal=target,
            previous_signal=previous,
            quote=quote,
            bars=bars,
            quality=quality,
            decision_requested_at=self.now + timedelta(seconds=1),
            observed_at=self.now + timedelta(seconds=1),
        )
        first = repository.persist_signal_decision(
            target,
            changed=True,
            observed_at=self.now + timedelta(seconds=1),
            expected_previous_signal_version_id=previous_version,
            decision_draft=draft,
        )
        retry = repository.persist_signal_decision(
            target,
            changed=True,
            observed_at=self.now + timedelta(seconds=1),
            expected_previous_signal_version_id=previous_version,
            decision_draft=draft,
        )
        self.assertEqual(first.artifact_id, retry.artifact_id)
        self.assertEqual(first.outbox_append_order, retry.outbox_append_order)
        self.assertEqual(retry.evidence_status, "OUTBOX_PENDING")

        stale = replace(
            target,
            state=T.SignalState.ACTIVE,
            previous_state=T.SignalState.WATCH,
            state_changed_at=self.now + timedelta(seconds=2),
            reason="stale concurrent decision",
        )
        stale_draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal=stale,
            previous_signal=previous,
            quote=quote,
            bars=bars,
            quality=quality,
            decision_requested_at=self.now + timedelta(seconds=2),
            observed_at=self.now + timedelta(seconds=2),
        )
        with self.assertRaisesRegex(RuntimeOutboxConflict, "changed after"):
            repository.persist_signal_decision(
                stale,
                changed=True,
                observed_at=self.now + timedelta(seconds=2),
                expected_previous_signal_version_id=previous_version,
                decision_draft=stale_draft,
            )

        next_signal = replace(
            target,
            state=T.SignalState.ACTIVE,
            previous_state=T.SignalState.TRIGGERED,
            state_changed_at=self.now + timedelta(seconds=3),
            reason="next committed occurrence",
        )
        next_draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal=next_signal,
            previous_signal=target,
            quote=quote,
            bars=bars,
            quality=quality,
            decision_requested_at=self.now + timedelta(seconds=3),
            observed_at=self.now + timedelta(seconds=3),
        )
        next_result = repository.persist_signal_decision(
            next_signal,
            changed=True,
            observed_at=self.now + timedelta(seconds=3),
            expected_previous_signal_version_id=runtime_signal_version_id(target),
            decision_draft=next_draft,
        )
        self.assertNotEqual(next_result.artifact_id, first.artifact_id)
        connection = get_connection(str(database))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            2,
        )

    def test_runtime_lane_failure_rolls_back_signal_and_history(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TRIGGER runtime_test_abort BEFORE INSERT ON runtime_transition_outbox "
                "BEGIN SELECT RAISE(ABORT, 'fixture abort'); END"
            )
        signal, quote, bars, quality = _runtime_inputs(self.now)
        draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal=signal,
            quote=quote,
            bars=bars,
            quality=quality,
        )
        with self.assertRaises(RuntimeDatabaseIntegrityError):
            repository.persist_signal_decision(
                signal,
                changed=True,
                observed_at=self.now,
                expected_previous_signal_version_id=None,
                decision_draft=draft,
            )
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0], 0
        )

    def test_occurrence_identity_retry_drift_rescan_and_upstream_occurrence(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        previous, quote, bars, quality = _runtime_inputs(self.now)
        repository.persist_signal_decision(
            previous,
            changed=True,
            observed_at=self.now,
            expected_previous_signal_version_id=None,
        )
        target = replace(
            previous,
            state=T.SignalState.TRIGGERED,
            previous_state=T.SignalState.WATCH,
            state_changed_at=self.now + timedelta(seconds=1),
            reason="triggered",
        )
        draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal=target,
            previous_signal=previous,
            quote=quote,
            bars=bars,
            quality=quality,
            decision_requested_at=self.now + timedelta(seconds=1),
            observed_at=self.now + timedelta(seconds=1),
        )
        first = repository.persist_signal_decision(
            target,
            changed=True,
            observed_at=self.now + timedelta(seconds=1),
            expected_previous_signal_version_id=runtime_signal_version_id(previous),
            decision_draft=draft,
        )
        retry = repository.persist_signal_decision(
            target,
            changed=True,
            observed_at=self.now + timedelta(seconds=1),
            expected_previous_signal_version_id=runtime_signal_version_id(previous),
            decision_draft=draft,
        )
        self.assertEqual(retry.artifact_id, first.artifact_id)
        connection = get_connection(str(database))
        occurrence = connection.execute(
            "SELECT occurrence_dedup_id,decision_content_id,transition_event_id "
            "FROM runtime_transition_occurrence"
        ).fetchone()
        self.assertEqual(str(occurrence["occurrence_dedup_id"]), draft.occurrence_dedup_id)
        self.assertEqual(str(occurrence["decision_content_id"]), draft.decision_content_id)
        self.assertRegex(str(occurrence["transition_event_id"]), r"^[0-9a-f]{64}$")

        drift = self.draft(
            runtime_store_id=runtime_store_id,
            signal=target,
            previous_signal=previous,
            quote=quote,
            bars=bars,
            quality=quality,
            decision_requested_at=self.now + timedelta(seconds=1),
            observed_at=self.now + timedelta(seconds=2),
        )
        self.assertEqual(drift.occurrence_dedup_id, draft.occurrence_dedup_id)
        later_retry = repository.persist_signal_decision(
            target,
            changed=True,
            observed_at=self.now + timedelta(seconds=2),
            expected_previous_signal_version_id=runtime_signal_version_id(previous),
            decision_draft=drift,
        )
        self.assertEqual(later_retry.artifact_id, first.artifact_id)
        self.assertEqual(later_retry.outbox_append_order, first.outbox_append_order)

        history_before = len(repository.load_signal_history(target.signal_id))
        unchanged = repository.persist_signal_decision(
            target,
            changed=False,
            observed_at=self.now + timedelta(seconds=2),
            expected_previous_signal_version_id=runtime_signal_version_id(target),
        )
        self.assertEqual(unchanged.evidence_status, "NOT_REQUESTED")
        self.assertEqual(len(repository.load_signal_history(target.signal_id)), history_before)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            1,
        )

        repeated = replace(
            target,
            previous_state=T.SignalState.TRIGGERED,
            state_changed_at=self.now + timedelta(seconds=3),
            reason="untrusted upstream repeated occurrence",
        )
        with self.assertRaises(RuntimeEvidenceContractError) as upstream_error:
            self.draft(
                runtime_store_id=runtime_store_id,
                signal=repeated,
                previous_signal=target,
                quote=quote,
                bars=bars,
                quality=quality,
                decision_requested_at=self.now + timedelta(seconds=3),
                observed_at=self.now + timedelta(seconds=3),
                upstream_occurrence_id="untrusted-upstream-occurrence",
            )
        self.assertEqual(
            upstream_error.exception.code,
            "UPSTREAM_OCCURRENCE_AUTHORITY_NOT_CONFIGURED",
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_occurrence").fetchone()[0],
            1,
        )

    def test_rolled_back_generated_transition_id_leaves_no_duplicate(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        signal, quote, bars, quality = _runtime_inputs(self.now)
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id(),
            signal=signal,
            quote=quote,
            bars=bars,
            quality=quality,
        )
        original_audit = repository._audit_runtime_outbox_state_connection
        audit_calls = 0

        def fail_after_insert(connection):
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 2:
                raise RuntimeOutboxError("fixture rollback after generated id")
            return original_audit(connection)

        first_uuid = uuid.UUID(int=1)
        second_uuid = uuid.UUID(int=2)
        with mock.patch.object(
            repository,
            "_audit_runtime_outbox_state_connection",
            side_effect=fail_after_insert,
        ), mock.patch(
            "stock_tracker.storage.repository.uuid.uuid4",
            side_effect=(first_uuid, second_uuid),
        ) as generated:
            with self.assertRaises(RuntimeOutboxError):
                repository.persist_signal_decision(
                    signal,
                    changed=True,
                    observed_at=self.now,
                    expected_previous_signal_version_id=None,
                    decision_draft=draft,
                )
            result = repository.persist_signal_decision(
                signal,
                changed=True,
                observed_at=self.now,
                expected_previous_signal_version_id=None,
                decision_draft=draft,
            )
        self.assertEqual(generated.call_count, 2)
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0], 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_occurrence").fetchone()[0],
            1,
        )
        expected_transition = hashlib.sha256(second_uuid.bytes).hexdigest()
        self.assertEqual(
            connection.execute(
                "SELECT transition_event_id FROM runtime_transition_occurrence "
                "WHERE artifact_id=?",
                (result.artifact_id,),
            ).fetchone()[0],
            expected_transition,
        )


class TestRuntimeArtifactStoreAndWorker(RuntimeEvidenceTestCase):
    def new_store(
        self,
        root: Path,
        database: Path,
        clock: FrozenClock,
        *,
        source_runtime_store_id: str | None = None,
    ) -> RuntimeArtifactStore:
        source_id = source_runtime_store_id
        if source_id is None:
            try:
                source_id = Repository(str(database)).runtime_evidence_store_id()
            except (
                RuntimeMigrationError,
                RuntimeEvidenceUnavailableError,
                RuntimeDatabaseIntegrityError,
                RuntimeOutboxError,
            ):
                source_id = "a" * 64
        return RuntimeArtifactStore(
            root / "records",
            root / "artifact-catalog.db",
            source_runtime_store_id=source_id,
            production_database=database,
            clock=clock,
        )

    def enqueue(
        self, repository: Repository, draft: RuntimeDecisionDraft
    ) -> RuntimeDecisionArtifact:
        identity = draft.identity_dict()
        signal, _quote, _bars, _quality = _runtime_inputs(
            self.now,
            signal_id=identity["runtime_signal_id"],
        )
        result = repository.persist_signal_decision(
            signal,
            changed=True,
            observed_at=datetime.fromisoformat(
                identity["observed_at_utc"].replace("Z", "+00:00")
            ),
            expected_previous_signal_version_id=identity[
                "expected_previous_signal_version_id"
            ],
            decision_draft=draft,
        )
        row = get_connection(repository.db_path).execute(
            "SELECT payload_json FROM runtime_transition_outbox WHERE artifact_id=?",
            (result.artifact_id,),
        ).fetchone()
        return RuntimeDecisionArtifact.from_json_bytes(str(row[0]).encode("utf-8"))

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
        self.assertEqual(reopened.source_runtime_store_id, "a" * 64)

    def test_store_is_bound_to_one_runtime_evidence_store(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        source_id = Repository(str(database)).runtime_evidence_store_id()
        clock = FrozenClock(self.now)
        store = self.new_store(
            root,
            database,
            clock,
            source_runtime_store_id=source_id,
        )
        artifact = self.artifact(runtime_store_id=source_id)
        store.append(artifact)
        self.assertEqual(store.audit().source_runtime_store_id, source_id)
        with self.assertRaisesRegex(RuntimeArtifactStoreError, "different runtime"):
            self.new_store(
                root,
                database,
                clock,
                source_runtime_store_id="c" * 64,
            )
        with self.assertRaises(RuntimeArtifactStoreError) as mismatch:
            store.append(self.artifact(runtime_store_id="d" * 64))
        self.assertEqual(mismatch.exception.code, "ROW_RUNTIME_STORE_ID_MISMATCH")

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
                source_runtime_store_id="a" * 64,
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
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id()
        )
        artifact = self.enqueue(repository, draft)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        result = worker.run_once()
        self.assertEqual(result.status, RuntimeArtifactWorkerStatus.DELIVERED)
        self.assertEqual(result.cursor, 1)
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 1)
        audit = store.audit()
        self.assertEqual(audit.artifact_ids, (artifact.artifact_id,))
        delivery = get_connection(str(database)).execute(
            "SELECT delivered_artifact_store_id,delivered_record_hash,"
            "delivered_append_order,delivered_audit_id,delivered_at "
            "FROM runtime_outbox_delivery WHERE artifact_id=?",
            (artifact.artifact_id,),
        ).fetchone()
        with closing(sqlite3.connect(store.catalog_path)) as catalog:
            record = catalog.execute(
                "SELECT append_order,record_hash FROM runtime_artifacts "
                "WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()
        self.assertEqual(str(delivery[0]), store.store_id)
        self.assertEqual(str(delivery[1]), str(record[1]))
        self.assertEqual(int(delivery[2]), int(record[0]))
        self.assertEqual(str(delivery[3]), audit.audit_id)
        self.assertEqual(
            str(delivery[4]),
            self.now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        self.assertEqual(worker.run_once().status, RuntimeArtifactWorkerStatus.IDLE)

    def test_artifact_store_switch_creates_global_lane_block(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        self.enqueue(repository, self.draft(runtime_store_id=runtime_store_id))
        clock = FrozenClock(self.now)
        store_a = self.new_store(root / "a", database, clock)
        worker_a = RuntimeArtifactWorker(
            repository,
            store_a,
            worker_id="delivery-worker-a",
            clock=clock,
        )
        self.assertEqual(
            worker_a.run_once().status,
            RuntimeArtifactWorkerStatus.DELIVERED,
        )
        store_b = self.new_store(root / "b", database, clock)
        worker_b = RuntimeArtifactWorker(
            repository,
            store_b,
            worker_id="delivery-worker-b",
            clock=clock,
        )
        switched = worker_b.run_once()
        self.assertEqual(switched.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(switched.error_code, "ARTIFACT_STORE_BINDING_MISMATCH")
        worker_c = RuntimeArtifactWorker(
            repository,
            store_a,
            worker_id="delivery-worker-c",
            clock=clock,
        )
        persisted = worker_c.run_once()
        self.assertEqual(persisted.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(persisted.error_code, switched.error_code)
        connection = get_connection(str(database))
        binding = connection.execute(
            "SELECT runtime_store_id,artifact_store_id FROM "
            "runtime_artifact_delivery_binding"
        ).fetchone()
        self.assertEqual(tuple(binding), (runtime_store_id, store_a.store_id))
        event = connection.execute(
            "SELECT artifact_id,worker_id,block_code FROM "
            "runtime_artifact_integrity_event"
        ).fetchone()
        self.assertEqual(
            tuple(event),
            (None, "delivery-worker-b", "ARTIFACT_STORE_BINDING_MISMATCH"),
        )

    def test_artifact_hash_chain_failure_globally_blocks_next_row(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        for signal_id in ("600519.SH:S1", "600519.SH:S2"):
            self.enqueue(
                repository,
                self.draft(
                    runtime_store_id=runtime_store_id,
                    signal_id=signal_id,
                ),
            )
            self.assertEqual(
                worker.run_once().status,
                RuntimeArtifactWorkerStatus.DELIVERED,
            )
        with closing(sqlite3.connect(store.catalog_path)) as catalog:
            catalog.row_factory = sqlite3.Row
            row = catalog.execute(
                "SELECT * FROM runtime_artifacts WHERE append_order=2"
            ).fetchone()
            record_path = store.record_root / str(row["record_file"])
            original_record = RuntimeArtifactRecord.from_json_bytes(
                record_path.read_bytes()
            )
            forged = RuntimeArtifactRecord.create(
                append_order=2,
                store_id=store.store_id,
                artifact=original_record.artifact,
                stored_at=original_record.stored_at,
                previous_record_hash="f" * 64,
            )
            forged_bytes = forged.to_json_bytes()
            record_path.write_bytes(forged_bytes)
            trigger_sql = str(
                catalog.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='runtime_artifacts_no_update'"
                ).fetchone()[0]
            )
            catalog.execute("DROP TRIGGER runtime_artifacts_no_update")
            catalog.execute(
                "UPDATE runtime_artifacts SET previous_record_hash=?,record_hash=?,"
                "record_file_sha256=? WHERE append_order=2",
                (
                    forged.previous_record_hash,
                    forged.record_hash,
                    hashlib.sha256(forged_bytes).hexdigest(),
                ),
            )
            catalog.execute(trigger_sql)
            catalog.commit()
        third = self.enqueue(
            repository,
            self.draft(
                runtime_store_id=runtime_store_id,
                signal_id="600519.SH:S3",
            ),
        )
        blocked = worker.run_once()
        self.assertEqual(blocked.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(blocked.error_code, "ARTIFACT_STORE_HASH_CHAIN_FAILURE")
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 2)
        restarted = RuntimeArtifactWorker(
            repository,
            store,
            worker_id="hash-chain-worker-b",
            clock=clock,
        ).run_once()
        self.assertEqual(restarted.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        delivery = get_connection(str(database)).execute(
            "SELECT status FROM runtime_outbox_delivery WHERE artifact_id=?",
            (third.artifact_id,),
        ).fetchone()[0]
        self.assertEqual(delivery, "LEASED")

    def test_audit_failure_retries_then_idempotently_delivers(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id()
        )
        self.enqueue(repository, draft)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(
            repository, store, clock=clock, retry_delay_seconds=30
        )
        with mock.patch.object(
            store,
            "audit",
            side_effect=RuntimeArtifactStoreError(
                "fixture audit failure",
                code="FIXTURE_TRANSIENT_AUDIT_FAILURE",
                failure_class=RuntimeArtifactFailureClass.ROW_TRANSIENT,
            ),
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

    def test_post_append_audit_exhaustion_blocks_without_quarantining_artifact(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        artifact = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(
            root,
            database,
            clock,
            source_runtime_store_id=runtime_store_id,
        )
        worker = RuntimeArtifactWorker(
            repository,
            store,
            clock=clock,
            retry_delay_seconds=30,
            max_store_retries=1,
        )
        transient = RuntimeArtifactStoreError(
            "persistent audit outage after durable append",
            code="FIXTURE_POST_APPEND_AUDIT_FAILURE",
            failure_class=RuntimeArtifactFailureClass.ROW_TRANSIENT,
            safe_to_quarantine=True,
        )
        with mock.patch.object(store, "audit", side_effect=transient):
            first = worker.run_once()
            self.assertEqual(first.status, RuntimeArtifactWorkerStatus.RETRY_SCHEDULED)
            clock.advance(31)
            second = worker.run_once()
        self.assertEqual(second.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(second.error_code, "POST_APPEND_AUDIT_RETRY_EXHAUSTED")
        self.assertEqual(store.audit().artifact_ids, (artifact.artifact_id,))
        connection = get_connection(str(database))
        self.assertEqual(
            connection.execute(
                "SELECT status FROM runtime_outbox_delivery WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()[0],
            "LEASED",
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_outbox_quarantine").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_artifact_integrity_event").fetchone()[0],
            1,
        )
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)
        self.assertEqual(worker.run_once().status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)

    def test_transient_exhaustion_globally_blocks_without_quarantine(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        blocked_draft = self.draft(runtime_store_id=runtime_store_id)
        later_draft = self.draft(
            runtime_store_id=runtime_store_id,
            signal_id="600519.SH:S2",
        )
        blocked_artifact = self.enqueue(repository, blocked_draft)
        later_artifact = self.enqueue(repository, later_draft)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(
            repository,
            store,
            clock=clock,
            retry_delay_seconds=30,
            max_store_retries=1,
        )
        transient = RuntimeArtifactStoreError(
            "persistent fixture I/O failure before any durable side effect",
            code="FIXTURE_PERSISTENT_IO_FAILURE",
            failure_class=RuntimeArtifactFailureClass.ROW_TRANSIENT,
            safe_to_quarantine=True,
        )
        with mock.patch.object(store, "append", side_effect=transient):
            first = worker.run_once()
            self.assertEqual(
                first.status,
                RuntimeArtifactWorkerStatus.RETRY_SCHEDULED,
            )
            clock.advance(31)
            second = worker.run_once()
        self.assertEqual(second.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(second.error_code, "ARTIFACT_STORE_TRANSIENT_RETRY_EXHAUSTED")
        self.assertEqual(
            second.failure_class,
            RuntimeArtifactFailureClass.STORE_INTEGRITY_BLOCK,
        )
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)
        connection = get_connection(str(database))
        states = {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT artifact_id,status,retry_count FROM runtime_outbox_delivery"
            ).fetchall()
        }
        self.assertEqual(
            states[blocked_artifact.artifact_id],
            ("LEASED", 1),
        )
        self.assertEqual(states[later_artifact.artifact_id][0], "PENDING")
        self.assertEqual(store.audit().artifact_ids, ())
        self.assertEqual(worker.run_once().status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        independent = RuntimeArtifactWorker(
            repository,
            store,
            worker_id="transient-exhaustion-worker-b",
            clock=clock,
        ).run_once()
        self.assertEqual(independent.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(independent.error_code, second.error_code)
        connection = get_connection(str(database))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_outbox_quarantine").fetchone()[0],
            0,
        )

    def test_poison_row_is_quarantined_and_next_row_delivers(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        poison = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        later = self.enqueue(
            repository,
            self.draft(
                runtime_store_id=runtime_store_id,
                signal_id="600519.SH:S2",
            ),
        )
        connection = get_connection(str(database))
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='runtime_transition_outbox_no_update'"
            ).fetchone()[0]
        )
        connection.execute("DROP TRIGGER runtime_transition_outbox_no_update")
        connection.execute(
            "UPDATE runtime_transition_outbox SET payload_json='{}',"
            "payload_sha256=? WHERE artifact_id=?",
            (hashlib.sha256(b"{}").hexdigest(), poison.artifact_id),
        )
        connection.execute(trigger_sql)
        connection.commit()
        # Startup only performs structural audit so a row-level poison can be
        # handed to the worker and quarantined instead of disabling the lane.
        self.assertEqual(repository.runtime_evidence_store_id(), runtime_store_id)
        clock = FrozenClock(self.now)
        store = self.new_store(
            root,
            database,
            clock,
            source_runtime_store_id=runtime_store_id,
        )
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        quarantined = worker.run_once()
        self.assertEqual(quarantined.status, RuntimeArtifactWorkerStatus.QUARANTINED)
        self.assertEqual(quarantined.error_code, "OUTBOX_ARTIFACT_CONTRACT_INVALID")
        self.assertEqual(quarantined.cursor, 1)
        self.assertEqual(store.audit().record_count, 0)
        delivered = worker.run_once()
        self.assertEqual(delivered.status, RuntimeArtifactWorkerStatus.DELIVERED)
        self.assertEqual(delivered.artifact_id, later.artifact_id)
        self.assertEqual(delivered.cursor, 2)

    def test_store_integrity_block_stops_current_and_later_rows(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        current = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        later = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id, signal_id="600519.SH:S2"),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        (root / "records" / "rogue.json").write_bytes(b"{}")
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        blocked = worker.run_once()
        self.assertEqual(blocked.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(blocked.error_code, "ARTIFACT_STORE_INVENTORY_MISMATCH")
        self.assertIsNotNone(blocked.block_id)
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)
        again = worker.run_once()
        self.assertEqual(again.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        restarted = RuntimeArtifactWorker(
            repository,
            store,
            worker_id="independent-worker-after-global-block",
            clock=clock,
        )
        restarted_result = restarted.run_once()
        self.assertEqual(
            restarted_result.status,
            RuntimeArtifactWorkerStatus.HARD_BLOCKED,
        )
        self.assertEqual(restarted_result.artifact_id, current.artifact_id)
        self.assertEqual(restarted_result.error_code, blocked.error_code)
        connection = get_connection(str(database))
        states = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT artifact_id,status FROM runtime_outbox_delivery"
            ).fetchall()
        }
        self.assertEqual(states[current.artifact_id], "LEASED")
        self.assertEqual(states[later.artifact_id], "PENDING")

    def test_store_schema_tamper_is_a_stable_global_block(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        artifact = self.enqueue(
            repository,
            self.draft(runtime_store_id=repository.runtime_evidence_store_id()),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        with closing(sqlite3.connect(store.catalog_path)) as connection:
            connection.executescript(
                "DROP TRIGGER runtime_artifacts_no_update;"
                "CREATE TRIGGER runtime_artifacts_no_update "
                "BEFORE UPDATE ON runtime_artifacts BEGIN SELECT 1; END;"
            )
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        first = worker.run_once()
        second = worker.run_once()
        self.assertEqual(first.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(first.artifact_id, artifact.artifact_id)
        self.assertEqual(first.error_code, "ARTIFACT_STORE_SCHEMA_MISMATCH")
        self.assertEqual(second.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(second.error_code, first.error_code)
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)

    def test_runtime_database_schema_tamper_blocks_before_store_effect(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        self.enqueue(
            repository,
            self.draft(runtime_store_id=repository.runtime_evidence_store_id()),
        )
        connection = get_connection(str(database))
        connection.executescript(
            "DROP TRIGGER runtime_transition_occurrence_no_update;"
            "CREATE TRIGGER runtime_transition_occurrence_no_update "
            "BEFORE UPDATE ON runtime_transition_occurrence BEGIN SELECT 1; END;"
        )
        connection.commit()
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        first = worker.run_once()
        second = worker.run_once()
        self.assertEqual(first.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(first.error_code, "RUNTIME_DATABASE_INTEGRITY")
        self.assertEqual(second.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(second.error_code, first.error_code)
        self.assertEqual(store.audit().record_count, 0)

    def test_missing_occurrence_relation_blocks_before_store_effect(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        artifact = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(
            root,
            database,
            clock,
            source_runtime_store_id=runtime_store_id,
        )
        close_all()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='runtime_transition_occurrence_no_delete'"
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER runtime_transition_occurrence_no_delete")
            connection.execute(
                "DELETE FROM runtime_transition_occurrence WHERE artifact_id=?",
                (artifact.artifact_id,),
            )
            connection.execute(trigger_sql)
            connection.commit()
        restarted_repository = Repository(str(database))
        worker = RuntimeArtifactWorker(
            restarted_repository,
            store,
            clock=clock,
        )
        blocked = worker.run_once()
        self.assertEqual(blocked.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(blocked.error_code, "RUNTIME_OUTBOX_RELATION_INCOMPLETE")
        self.assertEqual(store.audit().record_count, 0)
        delivery = get_connection(str(database)).execute(
            "SELECT status FROM runtime_outbox_delivery WHERE artifact_id=?",
            (artifact.artifact_id,),
        ).fetchone()[0]
        self.assertEqual(delivery, "PENDING")
        independent = RuntimeArtifactWorker(
            restarted_repository,
            store,
            worker_id="independent-structural-audit-worker",
            clock=clock,
        )
        self.assertEqual(
            independent.run_once().status,
            RuntimeArtifactWorkerStatus.HARD_BLOCKED,
        )

    def test_integrity_block_persistence_failure_still_opens_local_circuit(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(
            root,
            database,
            clock,
            source_runtime_store_id=runtime_store_id,
        )
        (root / "records" / "rogue.json").write_bytes(b"{}")
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        with mock.patch.object(
            repository,
            "mark_runtime_artifact_integrity_blocked",
            side_effect=RuntimeDatabaseIntegrityError("fixture block persistence failure"),
        ) as persist_block:
            first = worker.run_once()
            second = worker.run_once()
        self.assertEqual(first.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(first.error_code, "ARTIFACT_STORE_INVENTORY_MISMATCH")
        self.assertIsNone(first.block_id)
        self.assertEqual(second.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(second.error_code, first.error_code)
        self.assertEqual(persist_block.call_count, 1)

    def test_store_clock_rollback_is_a_global_block(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        runtime_store_id = repository.runtime_evidence_store_id()
        first_artifact = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id),
        )
        store_clock = FrozenClock(self.now)
        worker_clock = FrozenClock(self.now)
        store = self.new_store(root, database, store_clock)
        worker = RuntimeArtifactWorker(repository, store, clock=worker_clock)
        self.assertEqual(worker.run_once().artifact_id, first_artifact.artifact_id)
        second_artifact = self.enqueue(
            repository,
            self.draft(runtime_store_id=runtime_store_id, signal_id="600519.SH:S2"),
        )
        worker_clock.advance(1)
        store_clock.value = self.now - timedelta(seconds=1)
        blocked = worker.run_once()
        self.assertEqual(blocked.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(blocked.artifact_id, second_artifact.artifact_id)
        self.assertEqual(blocked.error_code, "ARTIFACT_STORE_CLOCK_ROLLBACK")
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 1)
        restarted = RuntimeArtifactWorker(
            repository,
            store,
            worker_id="clock-rollback-worker-b",
            clock=worker_clock,
        ).run_once()
        self.assertEqual(restarted.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(restarted.error_code, blocked.error_code)

    def test_conflicting_artifact_id_is_a_global_block(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        victim = self.enqueue(
            repository,
            self.draft(runtime_store_id=repository.runtime_evidence_store_id()),
        )
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        other = self.artifact(signal_id="600519.SH:S9")
        forged = RuntimeArtifactRecord.create(
            append_order=1,
            store_id=store.store_id,
            artifact=other,
            stored_at=self.now,
            previous_record_hash="0" * 64,
        )
        victim_path = root / "records" / "records" / victim.artifact_id[:2] / (
            victim.artifact_id + ".json"
        )
        victim_path.parent.mkdir(parents=True)
        victim_path.write_bytes(forged.to_json_bytes())
        worker = RuntimeArtifactWorker(repository, store, clock=clock)
        blocked = worker.run_once()
        self.assertEqual(blocked.status, RuntimeArtifactWorkerStatus.HARD_BLOCKED)
        self.assertEqual(blocked.error_code, "ARTIFACT_ID_COLLISION")
        self.assertEqual(repository.runtime_outbox_cursor(worker.worker_id), 0)

    def test_expired_lease_replays_to_exactly_one_observable_artifact(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        draft = self.draft(
            runtime_store_id=repository.runtime_evidence_store_id()
        )
        self.enqueue(repository, draft)
        clock = FrozenClock(self.now)
        store = self.new_store(root, database, clock)
        claimed = repository.claim_runtime_outbox(
            worker_id="crashed-worker",
            artifact_store_id=store.store_id,
            now=clock.now(),
            lease_seconds=60,
        )
        self.assertIsNotNone(claimed)
        clock.advance(61)
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
        outcome = manager._persist_signal_with_runtime_evidence(
            signal=signal,
            existing=None,
            changed=True,
            quote=quote,
            bars=bars,
            dq=quality,
            decision_requested_at=self.now,
        )
        self.assertIn(signal.signal_id, manager.repo.load_signals())
        self.assertEqual(len(manager.repo.load_signal_history(signal.signal_id)), 1)
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertEqual(outcome.error_code, "RUNTIME_EVIDENCE_UNAVAILABLE")
        self.assertEqual(published, [])

    def test_artifact_service_startup_failure_isolated_from_runtime(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        repository = Repository(str(database))
        logger = mock.Mock()
        with mock.patch.object(
            stock_tracker_main,
            "RuntimeArtifactStore",
            side_effect=RuntimeArtifactStoreError("fixture startup failure"),
        ):
            service = stock_tracker_main._build_runtime_artifact_service(
                repository,
                str(database),
                str(root),
                logger,
            )
        self.assertIsNone(service)
        logger.exception.assert_called_once()
        self.assertIsInstance(repository.load_signals(), dict)

    def test_commit_uncertainty_retries_the_same_transition_without_duplication(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)
        original = manager.repo.persist_signal_decision
        call_count = 0

        def commit_then_raise_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = original(*args, **kwargs)
            if call_count == 1:
                raise RuntimeOutboxError("ambiguous commit acknowledgement")
            return result

        with mock.patch.object(
            manager.repo,
            "persist_signal_decision",
            side_effect=commit_then_raise_once,
        ):
            first_attempt = manager._persist_signal_with_runtime_evidence(
                signal=signal,
                existing=None,
                changed=True,
                quote=quote,
                bars=bars,
                dq=quality,
                decision_requested_at=self.now,
            )
        self.assertIsNone(first_attempt)
        self.assertEqual(call_count, 1)
        persisted = manager._persist_signal_with_runtime_evidence(
            signal=signal,
            existing=None,
            changed=True,
            quote=quote,
            bars=bars,
            dq=quality,
            decision_requested_at=self.now,
        )
        self.assertEqual(persisted.status, "OUTBOX_PENDING")
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            1,
        )
        statuses = [
            payload["status"]
            for topic, payload in published
            if topic == "runtime_evidence"
        ]
        self.assertEqual(statuses, ["SIGNAL_PERSISTENCE_FAILED"])

    def test_double_persistence_failure_returns_false_without_ghost_signal(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)
        with mock.patch.object(
            manager.repo,
            "runtime_evidence_store_id",
            side_effect=RuntimeEvidenceUnavailableError("fixture unavailable"),
        ), mock.patch.object(
            manager.repo,
            "persist_signal_decision",
            side_effect=SignalPersistenceError("forced persistence failure"),
        ) as persist:
            persisted = manager._persist_signal_with_runtime_evidence(
                signal=signal,
                existing=None,
                changed=True,
                quote=quote,
                bars=bars,
                dq=quality,
                decision_requested_at=self.now,
            )
        self.assertIsNone(persisted)
        self.assertEqual(persist.call_count, 1)
        self.assertNotIn(signal.signal_id, manager.repo.load_signals())
        self.assertNotIn(signal.signal_id, manager.store.get_signals())
        evidence = [
            payload for topic, payload in published if topic == "runtime_evidence"
        ]
        self.assertEqual(evidence[-1]["status"], "SIGNAL_PERSISTENCE_FAILED")
        self.assertFalse(evidence[-1]["stage4g_case_opened"])
        next_signal, next_quote, next_bars, next_quality = _runtime_inputs(
            self.now,
            signal_id="600519.SH:S2",
        )
        next_result = manager._persist_signal_with_runtime_evidence(
            signal=next_signal,
            existing=None,
            changed=True,
            quote=next_quote,
            bars=next_bars,
            dq=next_quality,
            decision_requested_at=self.now,
        )
        self.assertEqual(next_result.status, "OUTBOX_PENDING")
        self.assertIn(next_signal.signal_id, manager.repo.load_signals())

    def test_legacy_naive_signal_is_quarantined_and_never_opens_case(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)
        signal.state_changed_at = self.now.replace(tzinfo=None)
        outcome = manager._persist_signal_with_runtime_evidence(
            signal=signal,
            existing=None,
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
        self.assertEqual(outcome.status, "QUARANTINED")
        self.assertEqual(outcome.error_code, "LEGACY_NAIVE_RUNTIME_TIME")
        self.assertEqual(published, [])

    def test_signal_event_failure_does_not_roll_back_durable_decision(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        signal, quote, bars, quality = _runtime_inputs(self.now)

        def publish(topic, payload):
            if topic == "signal":
                raise RuntimeError("fixture transport failure")
            published.append((topic, payload))

        manager._bus.publish = publish
        manager.strategies = []
        with mock.patch.object(
            manager.gate,
            "evaluate",
            return_value=(quality, T.DataStatus.LIVE),
        ), mock.patch(
            "stock_tracker.signals.manager.score_signal",
            return_value=signal.scores,
        ), mock.patch.object(
            manager.engine,
            "build",
            return_value=mock.Mock(),
        ), mock.patch.object(
            manager.risk_gate,
            "check",
            return_value=mock.Mock(allowed=True),
        ), mock.patch.object(
            manager.sm,
            "decide",
            return_value=signal,
        ), mock.patch.object(manager, "_publish_monitor_facts"):
            produced = manager.scan_symbol(
                signal.symbol,
                quote,
                bars,
                None,
                None,
            )
        self.assertEqual(produced, [signal])
        self.assertIn(signal.signal_id, manager.repo.load_signals())
        self.assertIn(signal.signal_id, manager.store.get_signals())
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0], 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            1,
        )
        evidence = [payload for topic, payload in published if topic == "runtime_evidence"]
        self.assertEqual(evidence[0]["status"], "OUTBOX_PENDING")
        self.assertFalse(evidence[0]["stage4g_case_opened"])

    def test_candidate_loop_survives_all_observational_bus_failures(self) -> None:
        root = self.temporary_root()
        database = self.new_database(root, migrate=True)
        clock = FrozenClock(self.now)
        manager, published = self.manager(database, clock)
        first, quote, bars, quality = _runtime_inputs(self.now)
        second = replace(
            first,
            signal_id="600519.SH:S2",
            strategy_id="S2",
        )
        candidates = (
            mock.Mock(strategy_id="S1"),
            mock.Mock(strategy_id="S2"),
        )
        strategies = []
        for candidate in candidates:
            strategy = mock.Mock(enabled=True)
            strategy.applies_to.return_value = True
            strategy.evaluate.return_value = candidate
            strategies.append(strategy)
        manager.strategies = strategies

        def publish(topic, payload):
            if topic in {"signal", "runtime_evidence", "monitor_facts"}:
                raise RuntimeError(f"fixture {topic} transport failure")
            published.append((topic, payload))

        manager._bus.publish = publish
        with mock.patch.object(
            manager.gate,
            "evaluate",
            return_value=(quality, T.DataStatus.LIVE),
        ), mock.patch(
            "stock_tracker.signals.manager.score_signal",
            return_value=first.scores,
        ), mock.patch.object(
            manager.engine,
            "build",
            return_value=mock.Mock(),
        ), mock.patch.object(
            manager.risk_gate,
            "check",
            return_value=mock.Mock(allowed=True),
        ), mock.patch.object(
            manager.sm,
            "decide",
            side_effect=(first, second),
        ), mock.patch.object(
            manager,
            "_monitor_facts_payload",
            return_value={},
        ):
            produced = manager.scan_symbol(
                first.symbol,
                quote,
                bars,
                None,
                None,
            )
        self.assertEqual(produced, [first, second])
        self.assertEqual([topic for topic, _payload in published], ["quote"])
        connection = get_connection(str(database))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 2)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0],
            2,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_transition_outbox").fetchone()[0],
            2,
        )
        self.assertEqual(
            manager.observational_errors,
            (
                "SIGNAL_EVENT_PUBLISH_FAILED",
                "RUNTIME_EVIDENCE_EVENT_PUBLISH_FAILED",
                "MONITOR_EVENT_PUBLISH_FAILED",
                "SIGNAL_EVENT_PUBLISH_FAILED",
                "RUNTIME_EVIDENCE_EVENT_PUBLISH_FAILED",
                "MONITOR_EVENT_PUBLISH_FAILED",
            ),
        )


if __name__ == "__main__":
    unittest.main()
