"""Physical B1a-c tests: real local SQLite/files; all stores are disposable."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from stock_tracker.runtime_evidence.physical_store import (
    MAX_BYTES,
    EvidenceInput,
    PhysicalEvidenceStore,
    PhysicalRecord,
    PhysicalSnapshot,
    PhysicalStoreError,
    StreamKind,
    canonical_bytes,
)

NOW = datetime(2026, 9, 11, 9, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def fixture(n: int = 1, *, stream: StreamKind = StreamKind.EVENT,
            partition: str = "A/2026-09-11/600519.SH") -> EvidenceInput:
    return EvidenceInput(stream, hashlib.sha256(str(n).encode()).hexdigest(), partition,
                         "b1-test-fixture-v1", NOW - timedelta(seconds=1),
                         canonical_bytes({"last_price": "10.25", "sequence": n}).decode())


class TestPhysicalCodec(unittest.TestCase):
    def test_input_round_trip_and_deterministic_request(self):
        value = fixture()
        self.assertEqual(EvidenceInput.from_document(value.document()), value)
        self.assertEqual(fixture().request_id, value.request_id)

    def test_payload_rejects_duplicate_noncanonical_nonfinite_and_float(self):
        for text in ('{"x":1,"x":2}', '{"x": 1}', '{"x":NaN}', '{"x":1.0}',
                     '[]', '\ufeff{}', '{"x":Infinity}'):
            with self.subTest(text=text), self.assertRaises(PhysicalStoreError):
                replace(fixture(), payload_json=text)

    def test_unicode_and_decimal_strings_remain_exact(self):
        payload = canonical_bytes({"name": "测试", "price": "0.0100"}).decode()
        value = replace(fixture(), payload_json=payload)
        self.assertEqual(value.document()["payload"]["price"], "0.0100")

    def test_input_metadata_exact_types(self):
        for key, value in (("stream", "EVENT"), ("record_key", "A" * 64),
                           ("partition_key", True), ("source_schema", "bad\nname"),
                           ("observed_at", NOW.replace(tzinfo=None)),  # Deliberate invalid-time fixture.
                           ("payload_json", {"x": 1})):
            with self.subTest(key=key), self.assertRaises(PhysicalStoreError):
                replace(fixture(), **{key: value})

    def test_deep_and_oversize_json_fail_closed(self):
        for text in ('{"x":' * 40 + '1' + '}' * 40, '{"x":"' + 'a' * MAX_BYTES + '"}'):
            with self.subTest(length=len(text)), self.assertRaises(PhysicalStoreError):
                replace(fixture(), payload_json=text)

    def test_bool_snapshot_high_water_rejected(self):
        with self.assertRaises(PhysicalStoreError):
            PhysicalSnapshot("a" * 64, True, "b" * 64)


class TestPhysicalStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="b1-physical-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "store"
        self.store = PhysicalEvidenceStore.initialize(self.root, clock=lambda: NOW)

    def sql(self, statements, params=()):
        conn = sqlite3.connect(self.store.catalog)
        try:
            result = conn.execute(statements, params).fetchall()
            conn.commit()
            return result
        finally:
            conn.close()

    def pending_record(self):
        content = self.sql("SELECT content FROM intents ORDER BY append_order DESC LIMIT 1")[0][0]
        return PhysicalRecord.decode(content)

    def interrupt(self, stage: str):
        def fault(point):
            if point == stage:
                raise RuntimeError("deliberate injected interruption")
        store = PhysicalEvidenceStore(self.root, self.store.store_id, clock=lambda: NOW, fault=fault)
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            store.append(fixture())

    def test_explicit_initialization_empty_audit_and_wrong_store_id(self):
        audit = self.store.audit()
        self.assertTrue(audit.ready)
        self.assertEqual(audit.snapshot.high_water, 0)
        for name in ("market_coverage_verified", "trusted_admission", "auto_trade", "power_loss_certified"):
            self.assertIs(audit.to_dict()[name], False)
        other = PhysicalEvidenceStore(self.root, "f" * 64, clock=lambda: NOW)
        with self.assertRaises(PhysicalStoreError):
            other.audit()

    def test_open_does_not_initialize_or_migrate(self):
        absent = self.parent / "missing"
        with self.assertRaises(PhysicalStoreError):
            PhysicalEvidenceStore(absent, "a" * 64)
        self.assertFalse(absent.exists())
        with self.assertRaises(PhysicalStoreError):
            PhysicalEvidenceStore.initialize(self.root)
        self.assertEqual(self.store.audit().snapshot.high_water, 0)

    def test_event_transport_global_and_partition_chains(self):
        first = self.store.append(fixture(1))
        second = self.store.append(fixture(2, stream=StreamKind.TRANSPORT, partition="connection/epoch-1"))
        third = self.store.append(fixture(3))
        self.assertEqual([first.append_order, second.append_order, third.append_order], [1, 2, 3])
        self.assertEqual(second.previous_global_hash, first.content_hash)
        self.assertEqual(third.previous_global_hash, second.content_hash)
        self.assertEqual(third.previous_partition_hash, first.content_hash)
        self.assertEqual(second.previous_partition_hash, "0" * 64)
        audit = self.store.audit()
        self.assertEqual(audit.file_count, 3)
        self.assertTrue(audit.ready)
        self.assertEqual(audit.byte_count, sum(len(r.content) for r in (first, second, third)))

    def test_exact_retry_does_not_mutate_identity(self):
        first = self.store.append(fixture())
        self.assertEqual(self.store.append(fixture()), first)
        self.assertEqual(self.store.audit().snapshot.high_water, 1)
        self.assertEqual(len(list(self.store.records.iterdir())), 1)

    def test_changed_content_same_key_conflicts(self):
        self.store.append(fixture())
        with self.assertRaisesRegex(PhysicalStoreError, "IDEMPOTENCY_CONFLICT"):
            self.store.append(replace(fixture(), payload_json='{"last_price":"999"}'))
        self.assertEqual(self.store.snapshot().high_water, 1)

    def test_record_fields_cannot_be_replaced_independently_of_bytes(self):
        record = self.store.append(fixture())
        with self.assertRaises((PhysicalStoreError, ValueError, TypeError)):
            replace(record, append_order=900)

    def test_content_hash_is_the_real_file_hash_without_self_reference(self):
        record = self.store.append(fixture())
        data = (self.root / record.storage_key).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), record.content_hash)
        self.assertNotIn('content_hash', json.loads(data))
        self.assertNotIn('record_id', json.loads(data))
        self.assertEqual(PhysicalRecord.decode(data), record)

    def test_pending_intent_is_not_visible_and_other_event_cannot_skip_it(self):
        self.interrupt("intent_committed")
        self.assertEqual(self.store.audit().pending_order, 1)
        with self.assertRaisesRegex(PhysicalStoreError, "RECOVERY_REQUIRED"):
            self.store.snapshot()
        with self.assertRaisesRegex(PhysicalStoreError, "RECOVERY_REQUIRED"):
            self.store.append(fixture(2))
        original = self.pending_record()
        restored = self.store.recover()
        self.assertEqual(restored, original)
        self.assertEqual(self.store.append(fixture()), original)
        self.assertIsNone(self.store.recover())

    def test_recover_partial_owned_staging_file(self):
        self.interrupt("intent_committed")
        record = self.pending_record()
        stage = self.store.staging / (Path(record.storage_key).name + ".tmp")
        stage.write_bytes(b"interrupted partial temp")
        self.assertEqual(self.store.recover(), record)
        self.assertFalse(stage.exists())
        self.assertEqual(self.store.snapshot().high_water, 1)

    def test_recover_link_before_temp_cleanup(self):
        self.interrupt("linked_before_staging_cleanup")
        self.assertEqual(self.store.audit().pending_order, 1)
        record = self.pending_record()
        self.assertEqual((self.root / record.storage_key).stat().st_nlink, 2)
        self.assertEqual(self.store.recover(), record)
        self.assertEqual((self.root / record.storage_key).stat().st_nlink, 1)

    def test_publication_rollback_preserves_exact_record_file(self):
        self.interrupt("publication_before_commit")
        record = self.pending_record()
        self.assertTrue((self.root / record.storage_key).is_file())
        self.assertEqual(self.store.audit().snapshot.high_water, 0)
        self.assertEqual(self.store.recover(), record)

    def test_lost_reply_after_commit_is_idempotent(self):
        self.interrupt("catalog_committed")
        record = self.pending_record()
        self.assertEqual(self.store.audit().snapshot.high_water, 1)
        self.assertEqual(self.store.append(fixture()), record)

    def test_no_replace_capability_failure_never_falls_back_to_replace(self):
        with (
            patch('stock_tracker.runtime_evidence.physical_store.os.link', side_effect=OSError("unsupported")),
            self.assertRaisesRegex(PhysicalStoreError, "NO_OVERWRITE_PUBLICATION_UNAVAILABLE"),
        ):
            self.store.append(fixture())
        self.assertEqual(self.store.audit().snapshot.high_water, 0)
        self.assertIsNotNone(self.store.recover())
        self.assertEqual(self.store.snapshot().high_water, 1)

    def test_missing_or_modified_committed_file_blocks_read_and_append(self):
        record = self.store.append(fixture())
        target = self.root / record.storage_key
        target.write_bytes(b"changed")
        for operation in (self.store.audit, lambda: self.store.append(fixture(2)),
                          lambda: self.store.read_page(PhysicalSnapshot(self.store.store_id, 1, record.content_hash))):
            with self.subTest(operation=operation), self.assertRaises(PhysicalStoreError):
                operation()
        target.unlink()
        with self.assertRaises(PhysicalStoreError):
            self.store.audit()

    def test_unknown_record_and_staging_files_not_deleted_or_adopted(self):
        for directory in (self.store.records, self.store.staging):
            path = directory / 'unknown.json'
            path.write_bytes(b'not owned by an intent')
            with self.assertRaises(PhysicalStoreError):
                self.store.recover()
            self.assertEqual(path.read_bytes(), b'not owned by an intent')
            path.unlink()

    def test_pending_final_mismatch_blocks_and_preserves_evidence(self):
        self.interrupt('intent_committed')
        record = self.pending_record()
        target = self.root / record.storage_key
        target.write_bytes(b'conflict')
        with self.assertRaises(PhysicalStoreError):
            self.store.recover()
        self.assertEqual(target.read_bytes(), b'conflict')

    def test_catalog_and_record_bytes_disagreement_detected(self):
        self.store.append(fixture())
        self.sql('DROP TRIGGER intents_no_update')
        self.sql("UPDATE intents SET content=?", (b'{}',))
        from stock_tracker.runtime_evidence.physical_store import SQL
        self.sql(next(s for s in SQL if s.startswith('CREATE TRIGGER intents_no_update ')))
        with self.assertRaises(PhysicalStoreError):
            self.store.audit()

    def test_extra_schema_objects_rejected(self):
        for name, sql in (('test_index', 'CREATE INDEX test_index ON intents(record_key)'),
                          ('test_view', 'CREATE VIEW test_view AS SELECT * FROM intents'),
                          ('test_trigger', 'CREATE TRIGGER test_trigger AFTER INSERT ON intents BEGIN SELECT 1; END')):
            with self.subTest(name=name):
                self.sql(sql)
                with self.assertRaisesRegex(PhysicalStoreError, 'SCHEMA_INVENTORY_MISMATCH'):
                    self.store.audit()
                self.sql('DROP ' + ('INDEX' if 'index' in name else 'VIEW' if 'view' in name else 'TRIGGER') + ' ' + name)

    def test_immutable_sql_rows_reject_update_delete(self):
        self.store.append(fixture())
        for table in ('meta', 'intents', 'publications'):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.sql('DELETE FROM ' + table)

    def test_unknown_schema_does_not_get_automatically_upgraded(self):
        self.sql('PRAGMA user_version=999')
        before = self.store.catalog.read_bytes()
        with self.assertRaisesRegex(PhysicalStoreError, 'SCHEMA_VERSION_MISMATCH'):
            self.store.audit()
        self.assertEqual(self.store.catalog.read_bytes(), before)

    def test_clock_rollback_and_future_observation_do_not_reserve_record(self):
        older = PhysicalEvidenceStore(self.root, self.store.store_id, clock=lambda: NOW - timedelta(seconds=1))
        with self.assertRaises(PhysicalStoreError):
            older.append(fixture())
        with self.assertRaises(PhysicalStoreError):
            self.store.append(replace(fixture(), observed_at=NOW + timedelta(seconds=1)))
        self.assertEqual(self.store.snapshot().high_water, 0)

    def test_store_or_catalog_replacement_is_detected(self):
        renamed = self.parent / 'original'
        self.root.rename(renamed)
        shutil.copytree(renamed, self.root)
        with self.assertRaisesRegex(PhysicalStoreError, 'STORE_PATH_REPLACED'):
            self.store.audit()
        restored = PhysicalEvidenceStore(self.root, self.store.store_id, clock=lambda: NOW)
        self.assertEqual(restored.snapshot().store_id, self.store.store_id)

    def test_extra_hardlink_to_committed_file_is_rejected(self):
        record = self.store.append(fixture())
        outside = self.parent / 'alias'
        os.link(self.root / record.storage_key, outside)
        with self.assertRaises(PhysicalStoreError):
            self.store.audit()
        outside.unlink()

    def test_symlink_root_rejected_when_platform_supports_it(self):
        link = self.parent / 'link'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        with self.assertRaises(PhysicalStoreError):
            PhysicalEvidenceStore(link, self.store.store_id)

    def test_stable_snapshot_and_bounded_pages(self):
        first = self.store.append(fixture(1))
        token = self.store.snapshot()
        self.store.append(fixture(2, stream=StreamKind.TRANSPORT))
        page = self.store.read_page(token, limit=1)
        self.assertEqual(page.records, (first,))
        self.assertFalse(page.has_more)
        current = self.store.snapshot()
        first_page = self.store.read_page(current, limit=1)
        self.assertTrue(first_page.has_more)
        self.assertEqual(self.store.read_page(current, after_order=1, limit=1).records[0].append_order, 2)

    def test_forged_snapshot_and_bool_page_limit_rejected(self):
        self.store.append(fixture())
        token = self.store.snapshot()
        for bad in (replace(token, store_id='f' * 64), replace(token, high_water=2),
                    replace(token, global_head='e' * 64)):
            with self.subTest(token=bad), self.assertRaises(PhysicalStoreError):
                self.store.read_page(bad)
        for limit in (True, 0, 257, '2'):
            with self.subTest(limit=limit), self.assertRaises(PhysicalStoreError):
                self.store.read_page(token, limit=limit)

    def test_two_threads_same_key_exact_once(self):
        def submit(_):
            return PhysicalEvidenceStore(self.root, self.store.store_id, clock=lambda: NOW).append(fixture())
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.snapshot().high_water, 1)

    def test_commit_raises_after_success_retry_verifies_real_catalog(self):
        real_connect = sqlite3.connect
        calls = []
        class UncertainConnection(sqlite3.Connection):
            def commit(self):
                super().commit()
                calls.append(1)
                if len(calls) == 2:
                    raise sqlite3.OperationalError('simulated lost commit acknowledgement')
        def connect(*args, **kwargs):
            return real_connect(*args, factory=UncertainConnection, **kwargs)
        with (
            patch('stock_tracker.runtime_evidence.physical_store.sqlite3.connect', side_effect=connect),
            self.assertRaisesRegex(PhysicalStoreError, 'COMMIT_OUTCOME_UNKNOWN'),
        ):
            self.store.append(fixture())
        self.assertEqual(self.store.snapshot().high_water, 1)
        self.assertEqual(self.store.append(fixture()).append_order, 1)
        self.assertEqual(len(list(self.store.records.iterdir())), 1)

    def test_page_has_a_byte_budget_not_only_record_count(self):
        records = [self.store.append(fixture(i)) for i in range(4)]
        budget = len(records[0].content) + len(records[1].content)
        with patch('stock_tracker.runtime_evidence.physical_store.MAX_PAGE_BYTES', budget):
            page = self.store.read_page(self.store.snapshot(), limit=256)
        self.assertEqual(len(page.records), 2)
        self.assertTrue(page.has_more)
        self.assertLessEqual(sum(len(r.content) for r in page.records), budget)

    def test_journal_symlink_rejected_without_touching_target(self):
        target = self.parent / 'outside-journal'
        target.write_bytes(b'preserve this unrelated file')
        link = self.root / 'catalog.sqlite3-journal'
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest('symlink unavailable: ' + str(exc))
        with self.assertRaises(PhysicalStoreError):
            self.store.append(fixture())
        self.assertEqual(target.read_bytes(), b'preserve this unrelated file')
        link.unlink()

    def test_two_initializers_do_not_overwrite_each_other(self):
        destination = self.parent / 'race-init'
        def initialize(_):
            try:
                return PhysicalEvidenceStore.initialize(destination, clock=lambda: NOW).store_id
            except PhysicalStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(initialize, range(2)))
        winners = [v for v in ids if v is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(PhysicalEvidenceStore(destination, winners[0], clock=lambda: NOW).snapshot().high_water, 0)

    def test_same_schema_catalog_alias_to_other_file_is_rejected(self):
        alias = self.parent / 'catalog-alias'
        os.link(self.store.catalog, alias)
        with self.assertRaises(PhysicalStoreError):
            self.store.audit()
        alias.unlink()

    def test_real_process_exit_at_every_publication_window(self):
        code = '''
import os, sys
from datetime import UTC, datetime
from stock_tracker.runtime_evidence.physical_store import EvidenceInput, PhysicalEvidenceStore, StreamKind
now = datetime(2026,9,11,9,tzinfo=UTC)
value = EvidenceInput(StreamKind.EVENT,'a'*64,'A/session/symbol','fixture-v1',now,'{"last_price":"10"}')
def fault(stage):
    if stage == sys.argv[3]: os._exit(73)
s = PhysicalEvidenceStore(sys.argv[1],sys.argv[2],clock=lambda:now,fault=fault)
s.append(value)
'''
        phases = ('intent_before_commit', 'intent_committed', 'temp_synced',
                  'linked_before_staging_cleanup', 'file_published',
                  'publication_before_commit', 'catalog_committed')
        for phase in phases:
            with self.subTest(phase=phase):
                store = PhysicalEvidenceStore.initialize(self.parent / phase, clock=lambda: NOW)
                child = subprocess.run([sys.executable, '-B', '-c', code, str(store.root), store.store_id, phase],
                                       cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(child.returncode, 73, child.stderr)
                value = EvidenceInput(StreamKind.EVENT, 'a'*64, 'A/session/symbol', 'fixture-v1', NOW, '{"last_price":"10"}')
                result = store.append(value)
                self.assertEqual(result.append_order, 1)
                self.assertEqual(store.snapshot().high_water, 1)
                self.assertEqual(store.append(value), result)
                self.assertIsNone(store.recover())
                self.assertEqual(len(list(store.records.iterdir())), 1)
                self.assertEqual(list(store.staging.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
