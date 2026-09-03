from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventPartitionHead,
    MarketEventSelection,
    MarketEventSequenceFinding,
    MarketEventSequenceFindingKind,
    MarketEventSourceContractError,
    MarketEventSourceRecord,
    MarketEventStoreAudit,
    MarketEventStoreAuditScope,
    MarketEventStoreSnapshot,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class MarketEventSourceSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store_id = _hash("market-event-store")
        self.base_time = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

    def record(
        self,
        *,
        append_order: int,
        symbol: str,
        previous_global: str,
        previous_partition: str,
        minute_offset: int = 0,
        event_id: str | None = None,
        durable_known_at: datetime | None = None,
    ) -> MarketEventSourceRecord:
        source_time = self.base_time + timedelta(minutes=minute_offset)
        known_at = (
            source_time + timedelta(seconds=2)
            if durable_known_at is None
            else durable_known_at
        )
        partition_key = (
            f"market=A/trading_day={source_time.date().isoformat()}/symbol={symbol}"
        )
        return MarketEventSourceRecord.create(
            source_store_id=self.store_id,
            append_order=append_order,
            event_id=event_id or _hash(f"event-{append_order}-{symbol}"),
            session_id="xtp-session-1",
            source="xtp",
            feed_mode="LEVEL2",
            symbol=symbol,
            market=Market.A,
            event_type="TRADE_TICK",
            trading_day=source_time.date(),
            source_time=source_time,
            received_at=source_time + timedelta(seconds=1),
            durable_known_at=known_at,
            callback_seq=append_order,
            provider_seq=1000 + append_order,
            partition_key=partition_key,
            previous_global_record_hash=previous_global,
            previous_partition_record_hash=previous_partition,
            raw_payload_sha256=_hash(f"raw-{append_order}"),
            payload={"last_price": 10 + append_order, "quantity": 100},
            parser_id="xtp-event-parser-v1",
            source_schema_id="stock-tracker-xtp-event-v1",
            record_file=f"records/{append_order:012d}-{symbol}.json",
            record_file_sha256=_hash(f"file-{append_order}"),
        )

    def records(self) -> tuple[MarketEventSourceRecord, ...]:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        second = self.record(
            append_order=2,
            symbol="000001.SZ",
            previous_global=first.record_hash,
            previous_partition="0" * 64,
            minute_offset=1,
        )
        third = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=second.record_hash,
            previous_partition=first.record_hash,
            minute_offset=2,
        )
        return first, second, third

    def partition_heads(
        self,
        records: tuple[MarketEventSourceRecord, ...],
    ) -> tuple[MarketEventPartitionHead, ...]:
        grouped: dict[str, list[MarketEventSourceRecord]] = {}
        for record in records:
            grouped.setdefault(record.partition_key, []).append(record)
        return tuple(
            MarketEventPartitionHead(
                partition_key=partition_key,
                event_count=len(items),
                first_record_hash=items[0].record_hash,
                last_record_hash=items[-1].record_hash,
                manifest_sha256=_hash(f"manifest-{partition_key}"),
            )
            for partition_key, items in sorted(grouped.items())
        )

    def audit(
        self,
        records: tuple[MarketEventSourceRecord, ...],
        *,
        audited_at: datetime | None = None,
    ) -> MarketEventStoreAudit:
        hashes = tuple(record.record_hash for record in records)
        return MarketEventStoreAudit(
            source_store_id=self.store_id,
            source_schema_id="stock-tracker-market-event-store-v4",
            scope=MarketEventStoreAuditScope.FULL_PREFIX,
            high_water_append_order=len(records),
            audited_at=(
                max(record.durable_known_at for record in records)
                + timedelta(seconds=1)
                if records and audited_at is None
                else audited_at or self.base_time
            ),
            catalog_schema_fingerprint=_hash("catalog-schema-v4"),
            record_hashes=hashes,
            partition_heads=self.partition_heads(records),
            first_global_record_hash="0" * 64 if not hashes else hashes[0],
            last_global_record_hash="0" * 64 if not hashes else hashes[-1],
        )

    def snapshot(
        self,
        records: tuple[MarketEventSourceRecord, ...] | None = None,
        *,
        findings: tuple[MarketEventSequenceFinding, ...] = (),
    ) -> MarketEventStoreSnapshot:
        actual_records = self.records() if records is None else records
        return MarketEventStoreSnapshot(
            audit=self.audit(actual_records),
            records=actual_records,
            findings=findings,
        )


class TestMarketEventSourceRecord(MarketEventSourceSnapshotTestCase):
    def test_record_identity_is_deterministic_and_payload_is_canonical(self) -> None:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        second = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.payload(), {"last_price": 11, "quantity": 100})
        self.assertEqual(first.payload_json, '{"last_price":11,"quantity":100}')
        self.assertRegex(first.source_record_id, r"^[0-9a-f]{64}$")

    def test_record_hash_and_partition_identity_are_recomputed(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "record_hash"):
            replace(record, record_hash="f" * 64)
        with self.assertRaisesRegex(MarketEventSourceContractError, "partition_key"):
            replace(record, partition_key="market=A/trading_day=2026-09-02/symbol=000001.SZ")

    def test_record_rejects_noncanonical_payload_and_future_durable_identity(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        with self.assertRaises(MarketEventSourceContractError):
            replace(
                record,
                payload_json='{"quantity": 100, "last_price": 11}',
                payload_sha256=hashlib.sha256(
                    b'{"quantity": 100, "last_price": 11}'
                ).hexdigest(),
            )
        with self.assertRaisesRegex(MarketEventSourceContractError, "durable_known_at"):
            replace(record, durable_known_at=record.received_at - timedelta(seconds=2))

    def test_record_file_must_be_safe_relative_path(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        for path in ("../escape.json", "/absolute.json", "x\\y.json"):
            with self.subTest(path=path), self.assertRaises(
                MarketEventSourceContractError
            ):
                replace(record, record_file=path)


class TestMarketEventStoreSnapshot(MarketEventSourceSnapshotTestCase):
    def test_snapshot_binds_full_global_and_partition_prefix(self) -> None:
        snapshot = self.snapshot()
        self.assertEqual(snapshot.audit.high_water_append_order, 3)
        self.assertEqual(
            snapshot.audit.record_hashes,
            tuple(record.record_hash for record in snapshot.records),
        )
        self.assertRegex(snapshot.snapshot_id, r"^[0-9a-f]{64}$")
        self.assertEqual(len(snapshot.audit.partition_heads), 2)

    def test_empty_snapshot_uses_zero_boundaries(self) -> None:
        snapshot = self.snapshot(records=())
        self.assertEqual(snapshot.audit.high_water_append_order, 0)
        self.assertEqual(snapshot.audit.first_global_record_hash, "0" * 64)
        self.assertEqual(snapshot.audit.last_global_record_hash, "0" * 64)
        self.assertEqual(snapshot.records, ())

    def test_snapshot_rejects_global_gap_or_chain_rewrite(self) -> None:
        first, second, third = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            MarketEventStoreSnapshot(
                audit=self.audit((first, second)),
                records=(second, first),
                findings=(),
            )
        forged_third = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=first.record_hash,
            previous_partition=first.record_hash,
            minute_offset=2,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            self.snapshot(records=(first, second, forged_third))
        self.assertNotEqual(forged_third.record_hash, third.record_hash)

    def test_snapshot_rejects_durable_known_at_rollback(self) -> None:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            minute_offset=10,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_hash,
            previous_partition=first.record_hash,
            minute_offset=0,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            self.snapshot(records=(first, second))

    def test_snapshot_rejects_partition_chain_rewrite(self) -> None:
        first, second, _third = self.records()
        forged = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=second.record_hash,
            previous_partition="0" * 64,
            minute_offset=2,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "partition"):
            self.snapshot(records=(first, second, forged))

    def test_snapshot_rejects_audit_before_durable_known_at(self) -> None:
        records = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            MarketEventStoreSnapshot(
                audit=self.audit(records, audited_at=records[-1].durable_known_at - timedelta(seconds=1)),
                records=records,
                findings=(),
            )

    def test_snapshot_rejects_duplicate_event_identity(self) -> None:
        first, second, _third = self.records()
        duplicate_event = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=second.record_hash,
            previous_partition=first.record_hash,
            minute_offset=2,
            event_id=first.event_id,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "duplicate event"):
            self.snapshot(records=(first, second, duplicate_event))

    def test_sequence_finding_must_be_inside_audited_prefix(self) -> None:
        records = self.records()
        finding = MarketEventSequenceFinding(
            kind=MarketEventSequenceFindingKind.PROVIDER_SEQUENCE,
            source_store_id=self.store_id,
            event_id=records[1].event_id,
            session_id=records[1].session_id,
            symbol=records[1].symbol,
            observed_append_order=2,
            expected_sequence=1001,
            observed_sequence=1002,
            detail_code="PROVIDER_SEQUENCE_GAP",
        )
        snapshot = self.snapshot(records=records, findings=(finding,))
        self.assertEqual(snapshot.findings[0].finding_id, finding.finding_id)
        with self.assertRaisesRegex(MarketEventSourceContractError, "outside"):
            self.snapshot(
                records=records,
                findings=(replace(finding, observed_append_order=3),),
            )
        with self.assertRaisesRegex(MarketEventSourceContractError, "outside"):
            self.snapshot(
                records=records,
                findings=(replace(finding, symbol="600519.SH"),),
            )

    def test_selection_is_bound_to_snapshot_audit_and_high_water(self) -> None:
        snapshot = self.snapshot()
        selection = snapshot.select(
            symbol="600519.SH",
            market=Market.A,
            start_source_time=self.base_time,
            end_source_time=self.base_time + timedelta(minutes=1),
        )
        self.assertEqual(selection.source_store_id, self.store_id)
        self.assertEqual(selection.snapshot_audit_id, snapshot.audit.audit_id)
        self.assertEqual(selection.snapshot_high_water_append_order, 3)
        self.assertEqual(tuple(item.append_order for item in selection.records), (1,))
        self.assertRegex(selection.selection_id, r"^[0-9a-f]{64}$")

    def test_selection_can_prove_an_empty_symbol_view(self) -> None:
        snapshot = self.snapshot()
        selection = snapshot.select(
            symbol="601318.SH",
            market=Market.A,
            start_source_time=self.base_time,
            end_source_time=self.base_time + timedelta(minutes=10),
        )
        self.assertEqual(selection.records, ())
        self.assertEqual(selection.snapshot_audit_id, snapshot.audit.audit_id)
        self.assertEqual(selection.snapshot_high_water_append_order, 3)

    def test_selection_rejects_record_after_frozen_high_water(self) -> None:
        records = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "outside"):
            MarketEventSelection(
                source_store_id=self.store_id,
                snapshot_audit_id=self.audit(records).audit_id,
                snapshot_high_water_append_order=2,
                symbol="600519.SH",
                market=Market.A,
                start_source_time=self.base_time,
                end_source_time=self.base_time + timedelta(minutes=10),
                records=(records[2],),
            )

    def test_selection_cannot_mix_symbol_or_source_store(self) -> None:
        records = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "outside"):
            MarketEventSelection(
                source_store_id=self.store_id,
                snapshot_audit_id=self.audit(records).audit_id,
                snapshot_high_water_append_order=3,
                symbol="600519.SH",
                market=Market.A,
                start_source_time=self.base_time,
                end_source_time=self.base_time + timedelta(minutes=10),
                records=(records[1],),
            )
        with self.assertRaisesRegex(MarketEventSourceContractError, "outside"):
            MarketEventSelection(
                source_store_id=_hash("other-store"),
                snapshot_audit_id=self.audit(records).audit_id,
                snapshot_high_water_append_order=3,
                symbol="600519.SH",
                market=Market.A,
                start_source_time=self.base_time,
                end_source_time=self.base_time + timedelta(minutes=10),
                records=(records[0],),
            )


if __name__ == "__main__":
    unittest.main()
