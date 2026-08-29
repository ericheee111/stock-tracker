from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.outcomes import OutcomeEvidenceOrigin
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.storage import outcome_ledger as ledger_module
from stock_tracker.quant.storage.outcome_ledger import (
    OutcomeLedger,
    OutcomeLedgerConflict,
    OutcomeLedgerDisposition,
    OutcomeLedgerError,
    OutcomeLedgerLane,
)
from tests_quant.test_outcomes import (
    _BASE,
    _complete_outcome,
    _hash,
    _no_entry_outcome,
    _open_outcome,
)


class OutcomeLedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).absolute()
        self.production = self.root / "production.db"
        self.production.write_bytes(b"production-sentinel")
        self.record_root = self.root / "records"
        self.catalog = self.root / "outcome-ledger.db"
        self.ledger = OutcomeLedger(
            self.record_root,
            self.catalog,
            production_database=self.production,
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
                recorded_by="reviewed-fixture-import",
            )
        self._last_ingested_at = observed
        return result


class TestOutcomeLedgerStore(OutcomeLedgerFixture):
    def test_append_does_not_accept_caller_selected_ingestion_time(self) -> None:
        self.assertNotIn(
            "ingested_at",
            inspect.signature(OutcomeLedger.append).parameters,
        )

    def test_self_declared_verified_live_outcome_remains_candidate(self) -> None:
        outcome = _complete_outcome(signal_suffix="self-declared-verified")
        self.assertTrue(outcome.real_scoreboard_eligible)
        result = self.append(outcome)
        self.assertIs(result.record.lane, OutcomeLedgerLane.LIVE_CANDIDATE)

    def test_terminal_lanes_hash_chain_and_idempotency(self) -> None:
        live = _complete_outcome(signal_suffix="ledger-live")
        synthetic = _complete_outcome(
            signal_suffix="ledger-synthetic",
            recorded_offset_days=1,
            origin=OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
            verified=False,
            synthetic=True,
        )
        paper = _complete_outcome(
            signal_suffix="ledger-paper",
            recorded_offset_days=2,
            origin=OutcomeEvidenceOrigin.PAPER_RECORDED,
            verified=False,
        )
        candidate = _no_entry_outcome()
        appended = [self.append(item) for item in (live, synthetic, paper, candidate)]
        self.assertEqual(
            [item.record.lane for item in appended],
            [
                OutcomeLedgerLane.LIVE_CANDIDATE,
                OutcomeLedgerLane.DIAGNOSTIC_ONLY,
                OutcomeLedgerLane.DIAGNOSTIC_ONLY,
                OutcomeLedgerLane.LIVE_CANDIDATE,
            ],
        )
        self.assertEqual(
            [item.record.append_order for item in appended],
            [1, 2, 3, 4],
        )
        self.assertEqual(appended[0].record.previous_record_hash, "0" * 64)
        for previous, current in pairwise(appended):
            self.assertEqual(
                current.record.previous_record_hash,
                previous.record.record_hash,
            )

        retried = self.ledger.append(
            live,
            recorded_by="different-retry-actor",
        )
        self.assertIs(retried.disposition, OutcomeLedgerDisposition.IDEMPOTENT)
        self.assertEqual(retried.record.record_hash, appended[0].record.record_hash)

        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=_BASE + timedelta(days=40),
        ):
            audit = self.ledger.audit()
        self.assertEqual(audit.record_count, 4)
        self.assertEqual(
            dict(audit.lane_counts),
            {
                OutcomeLedgerLane.DIAGNOSTIC_ONLY: 2,
                OutcomeLedgerLane.LIVE_CANDIDATE: 2,
            },
        )
        self.assertFalse(audit.as_dict()["production_database_modified"])
        self.assertEqual(self.production.read_bytes(), b"production-sentinel")

    def test_open_outcome_and_conflicting_signal_are_rejected(self) -> None:
        with self.assertRaisesRegex(OutcomeLedgerError, "terminal outcomes"):
            self.append(_open_outcome())
        first = _complete_outcome(signal_suffix="immutable", exit_price="12")
        second = _complete_outcome(signal_suffix="immutable", exit_price="11")
        self.append(first)
        with self.assertRaises(OutcomeLedgerConflict):
            self.append(second)
        self.assertEqual(self.ledger.audit().record_count, 1)

    def test_empty_audit_and_observed_timestamp_are_fail_closed(self) -> None:
        self.assertNotIn(
            "audited_at",
            inspect.signature(OutcomeLedger.audit).parameters,
        )
        audited_at = _BASE + timedelta(days=1)
        with patch.object(ledger_module, "_utc_now", return_value=audited_at):
            first = self.ledger.audit()
            second = self.ledger.audit()
        self.assertEqual(first.audit_id, second.audit_id)
        self.assertEqual(first.record_count, 0)
        self.assertEqual(first.first_record_hash, "0" * 64)
        self.assertEqual(first.last_record_hash, "0" * 64)

        outcome = _complete_outcome(signal_suffix="late-audit")
        ingestion = outcome.recorded_at + timedelta(days=5)
        self.append(outcome, ingested_at=ingestion)
        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=ingestion - timedelta(seconds=1),
        ), self.assertRaisesRegex(
            OutcomeLedgerError,
            "after the audit timestamp",
        ):
            self.ledger.audit()

    def test_file_catalog_inventory_and_chain_tamper_are_detected(self) -> None:
        self.append(_complete_outcome(signal_suffix="file-tamper"))
        record_file = next(self.record_root.rglob("*.json"))
        record_file.write_bytes(
            record_file.read_bytes().replace(
                b"RISK_ON_TREND",
                b"RISK_OFF_TREND",
            )
        )
        with self.assertRaisesRegex(OutcomeLedgerError, "file SHA"):
            self.ledger.audit()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = OutcomeLedger(
                root / "records",
                root / "catalog.db",
                production_database=root / "production.db",
            )
            ledger.append(
                _complete_outcome(signal_suffix="orphan"),
                recorded_by="fixture",
            )
            (root / "records" / "orphan.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(OutcomeLedgerError, "inventory mismatch"):
                ledger.audit()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = OutcomeLedger(
                root / "records",
                root / "catalog.db",
                production_database=root / "production.db",
            )
            ledger.append(
                _complete_outcome(signal_suffix="metadata"),
                recorded_by="fixture",
            )
            with closing(sqlite3.connect(root / "catalog.db")) as connection:
                connection.execute(
                    "UPDATE outcome_records SET strategy_id='tampered' WHERE append_order=1"
                )
                connection.commit()
            with self.assertRaisesRegex(OutcomeLedgerError, "metadata mismatch"):
                ledger.audit()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = OutcomeLedger(
                root / "records",
                root / "catalog.db",
                production_database=root / "production.db",
            )
            ledger.append(
                _complete_outcome(signal_suffix="chain-1"),
                recorded_by="fixture",
            )
            ledger.append(
                _complete_outcome(
                    signal_suffix="chain-2",
                    recorded_offset_days=1,
                ),
                recorded_by="fixture",
            )
            with closing(sqlite3.connect(root / "catalog.db")) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM outcome_records WHERE append_order=2"
                ).fetchone()
                assert row is not None
                path = root / "records" / row["record_file"]
                document = json.loads(path.read_text(encoding="utf-8"))
                document["previous_record_hash"] = "f" * 64
                identity = {
                    key: value
                    for key, value in document.items()
                    if key != "record_hash"
                }
                document["record_hash"] = fingerprint(identity)
                raw = (
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                path.write_bytes(raw)
                connection.execute(
                    "UPDATE outcome_records SET previous_record_hash=?,record_hash=?,"
                    "record_file_sha256=? WHERE append_order=2",
                    (
                        document["previous_record_hash"],
                        document["record_hash"],
                        hashlib.sha256(raw).hexdigest(),
                    ),
                )
                connection.commit()
            with self.assertRaisesRegex(OutcomeLedgerError, "hash chain"):
                ledger.audit()

    def test_noncanonical_record_bytes_are_rejected_even_if_catalog_hash_matches(self) -> None:
        self.append(_complete_outcome(signal_suffix="noncanonical"))
        record_file = next(self.record_root.rglob("*.json"))
        raw = record_file.read_bytes() + b"\n"
        record_file.write_bytes(raw)
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute(
                "UPDATE outcome_records SET record_file_sha256=? WHERE append_order=1",
                (hashlib.sha256(raw).hexdigest(),),
            )
            connection.commit()
        with self.assertRaisesRegex(OutcomeLedgerError, "not canonical"):
            self.ledger.audit()

    def test_catalog_insert_failure_removes_new_record_file(self) -> None:
        original_connection = self.ledger._connection

        class FailingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, parameters=()):
                if "INSERT INTO outcome_records" in sql:
                    raise sqlite3.OperationalError("forced insert failure")
                return self.connection.execute(sql, parameters)

            def commit(self) -> None:
                self.connection.commit()

            def rollback(self) -> None:
                self.connection.rollback()

        @contextmanager
        def failing_connection():
            with original_connection() as connection:
                yield FailingConnection(connection)

        with patch.object(self.ledger, "_connection", failing_connection), self.assertRaises(
            sqlite3.OperationalError
        ):
            self.append(_complete_outcome(signal_suffix="compensation"))
        self.assertEqual(list(self.record_root.rglob("*.json")), [])
        with closing(sqlite3.connect(self.catalog)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM outcome_records").fetchone()[0]
        self.assertEqual(count, 0)

    def test_commit_error_after_durable_commit_recovers_without_data_loss(self) -> None:
        original_connection = self.ledger._connection

        class AmbiguousCommitConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, parameters=()):
                return self.connection.execute(sql, parameters)

            def commit(self) -> None:
                self.connection.commit()
                raise sqlite3.OperationalError("forced post-commit error")

            def rollback(self) -> None:
                self.connection.rollback()

        @contextmanager
        def ambiguous_connection():
            with original_connection() as connection:
                yield AmbiguousCommitConnection(connection)

        with patch.object(self.ledger, "_connection", ambiguous_connection):
            result = self.append(_complete_outcome(signal_suffix="post-commit"))
        self.assertIs(result.disposition, OutcomeLedgerDisposition.APPENDED)
        self.assertEqual(self.ledger.audit().record_count, 1)
        self.assertEqual(len(list(self.record_root.rglob("*.json"))), 1)

    def test_path_catalog_and_link_isolation_fail_closed(self) -> None:
        with self.assertRaisesRegex(OutcomeLedgerError, "production database"):
            OutcomeLedger(
                self.record_root,
                self.production,
                production_database=self.production,
            )
        with self.assertRaisesRegex(OutcomeLedgerError, "outside the record root"):
            OutcomeLedger(
                self.root / "nested",
                self.root / "nested" / "catalog.db",
                production_database=self.production,
            )
        linked_root = self.root / "linked"
        linked_root.mkdir()
        with patch.object(
            ledger_module,
            "_is_link",
            side_effect=lambda path: path == linked_root,
        ), self.assertRaisesRegex(OutcomeLedgerError, "symlink or junction"):
            OutcomeLedger(
                linked_root,
                self.root / "linked-catalog.db",
                production_database=self.production,
            )

    def test_existing_unrelated_catalog_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            catalog = root / "unrelated.db"
            with closing(sqlite3.connect(catalog)) as connection:
                connection.execute("CREATE TABLE unrelated(value TEXT NOT NULL)")
                connection.execute("INSERT INTO unrelated(value) VALUES('sentinel')")
                connection.commit()
            with self.assertRaisesRegex(OutcomeLedgerError, "table set"):
                OutcomeLedger(
                    root / "records",
                    catalog,
                    production_database=root / "production.db",
                )
            with closing(sqlite3.connect(catalog)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                sentinel = connection.execute(
                    "SELECT value FROM unrelated"
                ).fetchone()[0]
            self.assertEqual(tables, {"unrelated"})
            self.assertEqual(sentinel, "sentinel")

    def test_catalog_hardlink_alias_and_runtime_link_replacement_fail_closed(self) -> None:
        alias = self.root / "production-alias.db"
        try:
            os.link(self.production, alias)
        except OSError as exc:
            self.skipTest(f"hard links are unavailable: {exc}")
        with self.assertRaisesRegex(OutcomeLedgerError, "aliases the production database"):
            OutcomeLedger(
                self.root / "hardlink-records",
                alias,
                production_database=self.production,
            )

        with patch.object(
            ledger_module,
            "_is_link",
            side_effect=lambda path: path.resolve(strict=False)
            == self.ledger.catalog_path,
        ), self.assertRaisesRegex(OutcomeLedgerError, "symlink or junction"):
            self.ledger.audit()

        real_identity = ledger_module._path_identity

        def replaced_root_identity(path: Path, name: str) -> tuple[int, int]:
            if path == self.ledger.record_root:
                return (999_999, 999_999)
            return real_identity(path, name)

        with patch.object(
            ledger_module,
            "_path_identity",
            side_effect=replaced_root_identity,
        ), self.assertRaisesRegex(OutcomeLedgerError, "record root was replaced"):
            self.ledger.audit()

    def test_concurrent_initialization_publishes_one_complete_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            record_root = root / "records"
            catalog = root / "catalog.db"
            production = root / "production.db"
            barrier = Barrier(2)
            real_mkstemp = ledger_module.tempfile.mkstemp

            def synchronized_mkstemp(*args, **kwargs):
                created = real_mkstemp(*args, **kwargs)
                barrier.wait(timeout=10)
                return created

            def open_ledger() -> OutcomeLedger:
                return OutcomeLedger(
                    record_root,
                    catalog,
                    production_database=production,
                )

            with patch.object(
                ledger_module.tempfile,
                "mkstemp",
                side_effect=synchronized_mkstemp,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(executor.submit(open_ledger) for _ in range(2))
                ledgers = tuple(future.result(timeout=20) for future in futures)

            self.assertEqual(tuple(item.audit().record_count for item in ledgers), (0, 0))
            self.assertTrue(catalog.is_file())
            self.assertEqual(list(root.glob(".catalog.db.init-*")), [])

    def test_independent_instances_serialize_concurrent_appends(self) -> None:
        ledgers = tuple(
            OutcomeLedger(
                self.record_root,
                self.catalog,
                production_database=self.production,
            )
            for _ in range(8)
        )
        outcomes = tuple(
            _complete_outcome(
                signal_suffix=f"concurrent-{index}",
                recorded_offset_days=index,
            )
            for index in range(8)
        )
        observed = _BASE + timedelta(days=30)
        with patch.object(ledger_module, "_utc_now", return_value=observed), ThreadPoolExecutor(
            max_workers=8
        ) as executor:
            futures = tuple(
                executor.submit(
                    ledger.append,
                    outcome,
                    recorded_by="concurrent-reviewed-import",
                )
                for ledger, outcome in zip(ledgers, outcomes, strict=True)
            )
            results = tuple(future.result(timeout=30) for future in futures)

        ordered = tuple(sorted((item.record for item in results), key=lambda item: item.append_order))
        self.assertEqual(
            tuple(item.append_order for item in ordered),
            tuple(range(1, 9)),
        )
        self.assertEqual(ordered[0].previous_record_hash, "0" * 64)
        for previous, current in pairwise(ordered):
            self.assertEqual(current.previous_record_hash, previous.record_hash)
            self.assertGreaterEqual(current.ingested_at, previous.ingested_at)
        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=observed + timedelta(seconds=1),
        ):
            audit = self.ledger.audit()
        self.assertEqual(audit.record_count, 8)

    def test_audit_serializes_file_catalog_snapshot_against_append(self) -> None:
        self.append(_complete_outcome(signal_suffix="audit-before-append"))
        audit_ledger = OutcomeLedger(
            self.record_root,
            self.catalog,
            production_database=self.production,
        )
        append_ledger = OutcomeLedger(
            self.record_root,
            self.catalog,
            production_database=self.production,
        )
        audit_entered = Event()
        release_audit = Event()
        append_started = Event()
        append_published = Event()
        original_validate = audit_ledger._validate_ledger_state
        original_atomic_write = ledger_module._atomic_write_immutable
        observed = _BASE + timedelta(days=30)

        def paused_validate(connection, cutoff):
            audit_entered.set()
            if not release_audit.wait(timeout=10):
                raise AssertionError("audit release timed out")
            return original_validate(connection, cutoff)

        def observed_atomic_write(path, raw):
            append_published.set()
            return original_atomic_write(path, raw)

        def append_second():
            append_started.set()
            return append_ledger.append(
                _complete_outcome(
                    signal_suffix="append-during-audit",
                    recorded_offset_days=1,
                ),
                recorded_by="concurrent-reviewed-import",
            )

        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=observed,
        ), patch.object(
            audit_ledger,
            "_validate_ledger_state",
            side_effect=paused_validate,
        ), patch.object(
            ledger_module,
            "_atomic_write_immutable",
            side_effect=observed_atomic_write,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            audit_future = executor.submit(audit_ledger.audit)
            self.assertTrue(audit_entered.wait(timeout=10))
            append_future = executor.submit(append_second)
            self.assertTrue(append_started.wait(timeout=10))
            self.assertFalse(append_published.wait(timeout=0.5))
            release_audit.set()
            self.assertEqual(audit_future.result(timeout=20).record_count, 1)
            self.assertIs(
                append_future.result(timeout=20).disposition,
                OutcomeLedgerDisposition.APPENDED,
            )

        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=observed + timedelta(seconds=1),
        ):
            self.assertEqual(self.ledger.audit().record_count, 2)

    def test_clock_rollback_and_existing_corruption_block_new_append(self) -> None:
        first = _complete_outcome(signal_suffix="clock-first")
        second = _complete_outcome(
            signal_suffix="clock-second",
            recorded_offset_days=1,
        )
        first_ingestion = _BASE + timedelta(days=10)
        with patch.object(ledger_module, "_utc_now", return_value=first_ingestion):
            self.ledger.append(first, recorded_by="clock-reviewed-import")
        existing_files = tuple(self.record_root.rglob("*.json"))
        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=first_ingestion - timedelta(seconds=1),
        ), self.assertRaisesRegex(
            OutcomeLedgerError,
            "after the audit timestamp",
        ):
            self.ledger.append(second, recorded_by="clock-reviewed-import")
        self.assertEqual(tuple(self.record_root.rglob("*.json")), existing_files)
        with closing(sqlite3.connect(self.catalog)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM outcome_records").fetchone()[0]
        self.assertEqual(count, 1)

        (self.record_root / "orphan.json").write_text("{}\n", encoding="utf-8")
        with patch.object(
            ledger_module,
            "_utc_now",
            return_value=first_ingestion + timedelta(seconds=1),
        ), self.assertRaisesRegex(OutcomeLedgerError, "inventory mismatch"):
            self.ledger.append(second, recorded_by="clock-reviewed-import")
        with closing(sqlite3.connect(self.catalog)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM outcome_records").fetchone()[0]
        self.assertEqual(count, 1)

    def test_live_candidate_can_be_verified_no_entry_without_entering_scoreboard(self) -> None:
        candidate = replace(
            _no_entry_outcome(),
            verified=True,
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            verification_evidence_ids=(_hash("6"),),
        )
        result = self.append(candidate)
        self.assertIs(result.record.lane, OutcomeLedgerLane.LIVE_CANDIDATE)
        self.assertFalse(candidate.real_scoreboard_eligible)


if __name__ == "__main__":
    unittest.main()
