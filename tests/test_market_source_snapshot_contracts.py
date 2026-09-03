from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
    MARKET_EVENT_SEQUENCE_POLICY_V1,
    MarketEventCallbackSequenceScope,
    MarketEventInventoryVerification,
    MarketEventPartitionHead,
    MarketEventProviderSequenceScope,
    MarketEventSelection,
    MarketEventSelectionVerification,
    MarketEventSequenceFinding,
    MarketEventSequenceFindingKind,
    MarketEventSourceContractError,
    MarketEventSourceRecord,
    MarketEventSourceSessionManifest,
    MarketEventStoreAudit,
    MarketEventStoreSnapshot,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class MarketEventSourceSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store_id = _hash("market-event-store")
        self.base_time = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)
        self.default_manifest = self.manifest()

    def manifest(
        self,
        *,
        session_id: str = "xtp-session-1",
        connection_epoch: int = 1,
        reconnect_epoch: int = 0,
        expected_first_callback_seq: int | None = 1,
        callback_scope: MarketEventCallbackSequenceScope = MarketEventCallbackSequenceScope.SESSION,
        provider_sequence_available: bool = False,
        provider_scope: MarketEventProviderSequenceScope = MarketEventProviderSequenceScope.UNAVAILABLE,
    ) -> MarketEventSourceSessionManifest:
        return MarketEventSourceSessionManifest(
            source_store_id=self.store_id,
            session_id=session_id,
            connection_epoch=connection_epoch,
            reconnect_epoch=reconnect_epoch,
            collector_started_at=self.base_time - timedelta(hours=1),
            coverage_start=self.base_time - timedelta(hours=1),
            expected_first_callback_seq=expected_first_callback_seq,
            callback_sequence_scope=callback_scope,
            provider_sequence_available=provider_sequence_available,
            provider_sequence_scope=provider_scope,
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
        )

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
        callback_seq: int | None = None,
        provider_seq: int | None = None,
        trading_day: date | None = None,
        market: Market = Market.A,
        event_type: str = "TRADE_TICK",
        manifest: MarketEventSourceSessionManifest | None = None,
    ) -> MarketEventSourceRecord:
        manifest = self.default_manifest if manifest is None else manifest
        source_time = self.base_time + timedelta(minutes=minute_offset)
        known_at = (
            source_time + timedelta(seconds=2)
            if durable_known_at is None
            else durable_known_at
        )
        partition_key = (
            f"market={market.value}/trading_day="
            f"{(trading_day or source_time.date()).isoformat()}/symbol={symbol}"
        )
        return MarketEventSourceRecord.create(
            source_store_id=self.store_id,
            append_order=append_order,
            event_id=event_id or _hash(f"event-{append_order}-{symbol}"),
            session_id=manifest.session_id,
            source_session_manifest_id=manifest.manifest_id,
            connection_epoch=manifest.connection_epoch,
            reconnect_epoch=manifest.reconnect_epoch,
            source="xtp",
            feed_mode="LEVEL2",
            symbol=symbol,
            market=market,
            event_type=event_type,
            trading_day=trading_day or source_time.date(),
            session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
            source_time=source_time,
            received_at=source_time + timedelta(seconds=1),
            durable_known_at=known_at,
            callback_seq=append_order if callback_seq is None else callback_seq,
            provider_seq=provider_seq,
            partition_key=partition_key,
            previous_global_record_hash=previous_global,
            previous_partition_record_hash=previous_partition,
            raw_payload_sha256=_hash(f"raw-{append_order}"),
            payload={"last_price": 10 + append_order, "quantity": 100},
            parser_id="xtp-event-parser-v1",
            source_schema_id="stock-tracker-xtp-event-v1",
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
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
            minute_offset=1,
        )
        third = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=second.record_content_hash,
            previous_partition=first.record_content_hash,
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
                first_record_hash=items[0].record_content_hash,
                last_record_hash=items[-1].record_content_hash,
                manifest_sha256=_hash(f"manifest-{partition_key}"),
            )
            for partition_key, items in sorted(grouped.items())
        )

    def finding(
        self,
        record: MarketEventSourceRecord,
        *,
        kind: MarketEventSequenceFindingKind,
        expected_sequence: int | None,
        observed_sequence: int | None,
        detail_code: str,
    ) -> MarketEventSequenceFinding:
        return MarketEventSequenceFinding(
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
            kind=kind,
            source_store_id=self.store_id,
            event_id=record.event_id,
            session_id=record.session_id,
            symbol=record.symbol,
            observed_append_order=record.append_order,
            expected_sequence=expected_sequence,
            observed_sequence=observed_sequence,
            detail_code=detail_code,
        )

    def audit(
        self,
        records: tuple[MarketEventSourceRecord, ...],
        *,
        audited_at: datetime | None = None,
        findings: tuple[MarketEventSequenceFinding, ...] = (),
        manifests: tuple[MarketEventSourceSessionManifest, ...] | None = None,
    ) -> MarketEventStoreAudit:
        actual_manifests = (
            (self.default_manifest,) if manifests is None else manifests
        )
        inventory = MarketEventInventoryVerification.create_from_prefix(
            source_store_id=self.store_id,
            catalog_schema_fingerprint=_hash("catalog-schema-v4"),
            records=records,
        )
        return MarketEventStoreAudit.create_from_prefix(
            source_store_id=self.store_id,
            source_schema_id="stock-tracker-market-event-store-v4",
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
            audited_at=(
                max(record.durable_known_at for record in records)
                + timedelta(seconds=1)
                if records and audited_at is None
                else audited_at or self.base_time
            ),
            catalog_schema_fingerprint=_hash("catalog-schema-v4"),
            inventory_verification=inventory,
            partition_heads=self.partition_heads(records),
            records=records,
            findings=findings,
            source_session_manifests=actual_manifests,
        )

    def snapshot(
        self,
        records: tuple[MarketEventSourceRecord, ...] | None = None,
        *,
        findings: tuple[MarketEventSequenceFinding, ...] = (),
        audited_at: datetime | None = None,
        manifests: tuple[MarketEventSourceSessionManifest, ...] | None = None,
    ) -> MarketEventStoreSnapshot:
        actual_records = self.records() if records is None else records
        return MarketEventStoreSnapshot.from_audit(
            self.audit(
                actual_records,
                findings=findings,
                audited_at=audited_at,
                manifests=manifests,
            )
        )

    def select(
        self,
        snapshot: MarketEventStoreSnapshot,
        records: tuple[MarketEventSourceRecord, ...],
        *,
        symbol: str = "600519.SH",
        findings: tuple[MarketEventSequenceFinding, ...] = (),
        record_limit: int = 100,
        manifests: tuple[MarketEventSourceSessionManifest, ...] | None = None,
        allowed_event_types: tuple[str, ...] = ("TRADE_TICK",),
    ) -> MarketEventSelection:
        return snapshot.select_from_prefix(
            records=records,
            partition_heads=self.partition_heads(records),
            findings=findings,
            source_session_manifests=(
                (self.default_manifest,) if manifests is None else manifests
            ),
            symbol=symbol,
            market=Market.A,
            start_source_time=self.base_time,
            end_source_time=self.base_time + timedelta(minutes=10),
            allowed_event_types=allowed_event_types,
            interval_boundary_policy_id=MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
            record_limit=record_limit,
        )

    def verify(
        self,
        snapshot: MarketEventStoreSnapshot,
        selection: MarketEventSelection,
        records: tuple[MarketEventSourceRecord, ...],
        *,
        findings: tuple[MarketEventSequenceFinding, ...] = (),
        manifests: tuple[MarketEventSourceSessionManifest, ...] | None = None,
    ) -> MarketEventSelectionVerification:
        return snapshot.verify_selection(
            selection,
            records=records,
            partition_heads=self.partition_heads(records),
            findings=findings,
            source_session_manifests=(
                (self.default_manifest,) if manifests is None else manifests
            ),
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
        with self.assertRaisesRegex(MarketEventSourceContractError, "record_content_hash"):
            replace(record, record_content_hash="f" * 64)
        with self.assertRaisesRegex(MarketEventSourceContractError, "partition_key"):
            replace(record, partition_key="market=A/trading_day=2026-09-02/symbol=000001.SZ")

    def test_storage_key_is_deterministic_and_part_of_member_identity(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        self.assertEqual(
            record.record_storage_key,
            f"records/{record.append_order:020d}-{record.event_id}.json",
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "deterministic"):
            replace(record, record_storage_key="records/other-safe-name.json")
        self.assertIn("record_storage_key", record.inventory_leaf())
        self.assertIn("record_file_sha256", record.inventory_leaf())

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
        for path in (
            "../escape.json",
            "/absolute.json",
            "x\\y.json",
            "C:/records/event.json",
            "C:",
            "//server/share/event.json",
            "records//event.json",
            "./event.json",
            "records/event.json:secret",
            "records/CON.json",
            "records/event.json.",
            "records/event file.json",
        ):
            with self.subTest(path=path), self.assertRaises(
                MarketEventSourceContractError
            ):
                replace(record, record_storage_key=path)

    def test_record_trading_day_uses_market_local_session_label(self) -> None:
        source_time = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
        self.base_time = source_time
        with self.assertRaisesRegex(MarketEventSourceContractError, "trading_day"):
            self.record(
                append_order=1,
                symbol="AAPL.US",
                market=Market.US,
                trading_day=date(2026, 7, 2),
                previous_global="0" * 64,
                previous_partition="0" * 64,
            )
        record = self.record(
            append_order=1,
            symbol="AAPL.US",
            market=Market.US,
            trading_day=date(2026, 7, 1),
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        self.assertEqual(record.trading_day, date(2026, 7, 1))

    def test_market_session_date_covers_a_hk_and_us_dst(self) -> None:
        policy = MARKET_SESSION_LABEL_POLICY_V1
        self.assertEqual(
            market_session_date(
                datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc),
                Market.A,
                policy,
            ),
            date(2026, 9, 2),
        )
        self.assertEqual(
            market_session_date(
                datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc),
                Market.HK,
                policy,
            ),
            date(2026, 9, 2),
        )
        self.assertEqual(
            market_session_date(
                datetime(2026, 1, 2, 4, 30, tzinfo=timezone.utc),
                Market.US,
                policy,
            ),
            date(2026, 1, 1),
        )
        self.assertEqual(
            market_session_date(
                datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc),
                Market.US,
                policy,
            ),
            date(2026, 7, 2),
        )

    def test_hk_record_rejects_utc_date_as_trading_day(self) -> None:
        self.base_time = datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc)
        with self.assertRaisesRegex(MarketEventSourceContractError, "trading_day"):
            self.record(
                append_order=1,
                symbol="00700.HK",
                market=Market.HK,
                trading_day=date(2026, 9, 1),
                previous_global="0" * 64,
                previous_partition="0" * 64,
            )


class TestMarketEventStoreSnapshot(MarketEventSourceSnapshotTestCase):
    def test_audit_requires_exact_catalog_inventory_verification(self) -> None:
        records = self.records()
        with self.assertRaises(TypeError):
            MarketEventStoreAudit.create_from_prefix(
                source_store_id=self.store_id,
                source_schema_id="stock-tracker-market-event-store-v4",
                sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
                audited_at=records[-1].durable_known_at + timedelta(seconds=1),
                catalog_schema_fingerprint=_hash("catalog-schema-v4"),
                records=records,
                partition_heads=self.partition_heads(records),
                findings=(),
                source_session_manifests=(self.default_manifest,),
            )

    def test_inventory_rejects_duplicate_event_record_and_storage_identity(self) -> None:
        first, second, _third = self.records()
        duplicate_event = self.record(
            append_order=2,
            symbol="000001.SZ",
            event_id=first.event_id,
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
            minute_offset=1,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "duplicate"):
            MarketEventInventoryVerification.create_from_prefix(
                source_store_id=self.store_id,
                catalog_schema_fingerprint=_hash("catalog-schema-v4"),
                records=(first, duplicate_event),
            )
        object.__setattr__(second, "source_record_id", first.source_record_id)
        with self.assertRaisesRegex(MarketEventSourceContractError, "duplicate"):
            MarketEventInventoryVerification.create_from_prefix(
                source_store_id=self.store_id,
                catalog_schema_fingerprint=_hash("catalog-schema-v4"),
                records=(first, second),
            )
        second = self.records()[1]
        object.__setattr__(second, "record_storage_key", first.record_storage_key)
        with self.assertRaisesRegex(MarketEventSourceContractError, "duplicate"):
            MarketEventInventoryVerification.create_from_prefix(
                source_store_id=self.store_id,
                catalog_schema_fingerprint=_hash("catalog-schema-v4"),
                records=(first, second),
            )

    def test_first_callback_gap_requires_manifest_bound_finding(self) -> None:
        manifest = self.manifest(expected_first_callback_seq=1)
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            callback_seq=3,
            manifest=manifest,
        )
        finding = self.finding(
            record,
            kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
            expected_sequence=1,
            observed_sequence=3,
            detail_code="CALLBACK_SEQUENCE_GAP",
        )
        self.snapshot(
            records=(record,),
            findings=(finding,),
            manifests=(manifest,),
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "findings"):
            self.snapshot(records=(record,), manifests=(manifest,))

    def test_reconnect_epoch_reset_is_scoped_and_provider_capability_cannot_drift(self) -> None:
        first_manifest = self.manifest(
            reconnect_epoch=0,
            callback_scope=MarketEventCallbackSequenceScope.CONNECTION_EPOCH,
        )
        second_manifest = self.manifest(
            reconnect_epoch=1,
            callback_scope=MarketEventCallbackSequenceScope.CONNECTION_EPOCH,
        )
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            manifest=first_manifest,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            callback_seq=1,
            manifest=second_manifest,
        )
        self.snapshot(
            records=(first, second),
            manifests=(first_manifest, second_manifest),
        )
        changed_capability = self.manifest(
            reconnect_epoch=1,
            callback_scope=MarketEventCallbackSequenceScope.CONNECTION_EPOCH,
            provider_sequence_available=True,
            provider_scope=MarketEventProviderSequenceScope.CONNECTION_EPOCH,
        )
        changed_second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            callback_seq=1,
            provider_seq=1,
            manifest=changed_capability,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "capability changes"):
            self.snapshot(
                records=(first, changed_second),
                manifests=(first_manifest, changed_capability),
            )

    def test_provider_unavailable_and_missing_start_proof_are_explicit(self) -> None:
        manifest = self.manifest(expected_first_callback_seq=None)
        self.assertFalse(manifest.provider_sequence_available)
        self.assertFalse(manifest.has_sequence_start_proof)
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            provider_seq=None,
            callback_seq=27,
            manifest=manifest,
        )
        snapshot = self.snapshot(records=(record,), manifests=(manifest,))
        self.assertEqual(snapshot.audit.finding_count, 0)
    def test_snapshot_rejects_omitted_callback_gap_finding(self) -> None:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            callback_seq=1,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            callback_seq=3,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "finding"):
            self.snapshot(records=(first, second), findings=())

    def test_fabricated_extra_and_duplicate_findings_are_rejected(self) -> None:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            callback_seq=1,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            callback_seq=3,
        )
        finding = self.finding(
            second,
            kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
            expected_sequence=2,
            observed_sequence=3,
            detail_code="CALLBACK_SEQUENCE_GAP",
        )
        records = (first, second)
        self.snapshot(records=records, findings=(finding,))
        with self.assertRaisesRegex(MarketEventSourceContractError, "fabricated|duplicate"):
            self.snapshot(records=records, findings=(finding, finding))
        with self.assertRaisesRegex(MarketEventSourceContractError, "findings"):
            self.snapshot(
                records=records,
                findings=(
                    replace(
                        finding,
                        kind=MarketEventSequenceFindingKind.PROVIDER_SEQUENCE,
                    ),
                ),
            )

    def test_out_of_order_and_source_time_regression_are_recomputed(self) -> None:
        manifest = self.manifest(expected_first_callback_seq=None)
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            minute_offset=1,
            callback_seq=2,
            durable_known_at=self.base_time + timedelta(minutes=2),
            manifest=manifest,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=0,
            callback_seq=1,
            durable_known_at=self.base_time + timedelta(minutes=3),
            manifest=manifest,
        )
        callback = self.finding(
            second,
            kind=MarketEventSequenceFindingKind.OUT_OF_ORDER,
            expected_sequence=3,
            observed_sequence=1,
            detail_code="CALLBACK_SEQUENCE_NOT_ADVANCED",
        )
        regression = self.finding(
            second,
            kind=MarketEventSequenceFindingKind.SOURCE_TIME_REGRESSION,
            expected_sequence=None,
            observed_sequence=None,
            detail_code="SOURCE_TIME_REGRESSION",
        )
        snapshot = self.snapshot(
            records=(first, second),
            findings=(callback, regression),
            manifests=(manifest,),
        )
        self.assertEqual(snapshot.audit.finding_count, 2)

    def test_drive_qualified_record_file_is_rejected(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
        )
        with self.assertRaises(MarketEventSourceContractError):
            replace(record, record_storage_key="C:/records/event.json")

    def test_snapshot_binds_full_global_and_partition_prefix(self) -> None:
        snapshot = self.snapshot()
        self.assertEqual(snapshot.audit.high_water_append_order, 3)
        self.assertEqual(snapshot.audit.record_count, 3)
        self.assertRegex(snapshot.snapshot_id, r"^[0-9a-f]{64}$")
        self.assertEqual(snapshot.audit.partition_count, 2)
        self.assertNotIn("record_hashes", snapshot.audit.as_dict())
        self.assertNotIn("partition_heads", snapshot.audit.as_dict())

    def test_empty_snapshot_uses_zero_boundaries(self) -> None:
        snapshot = self.snapshot(records=())
        self.assertEqual(snapshot.audit.high_water_append_order, 0)
        self.assertEqual(snapshot.audit.first_global_record_hash, "0" * 64)
        self.assertEqual(snapshot.audit.last_global_record_hash, "0" * 64)
        self.assertEqual(snapshot.audit.record_count, 0)

    def test_snapshot_rejects_global_gap_or_chain_rewrite(self) -> None:
        first, second, third = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix|inventory"):
            self.snapshot(records=(second, first))
        forged_third = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=2,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            self.snapshot(records=(first, second, forged_third))
        self.assertNotEqual(forged_third.record_content_hash, third.record_content_hash)

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
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=0,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            self.snapshot(records=(first, second))

    def test_snapshot_rejects_partition_chain_rewrite(self) -> None:
        first, second, _third = self.records()
        forged = self.record(
            append_order=3,
            symbol="600519.SH",
            previous_global=second.record_content_hash,
            previous_partition="0" * 64,
            minute_offset=2,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "partition"):
            self.snapshot(records=(first, second, forged))

    def test_snapshot_rejects_audit_before_durable_known_at(self) -> None:
        records = self.records()
        with self.assertRaisesRegex(MarketEventSourceContractError, "global prefix"):
            self.snapshot(
                records=records,
                audited_at=records[-1].durable_known_at - timedelta(seconds=1),
            )

    def test_audit_is_compact_and_binds_catalog_schema(self) -> None:
        audit = self.audit(self.records())
        self.assertLess(len(str(audit.as_dict())), 4096)
        self.assertFalse(hasattr(audit, "record_hashes"))
        self.assertFalse(hasattr(self.snapshot(), "records"))
        self.assertEqual(audit.catalog_schema_fingerprint, _hash("catalog-schema-v4"))
        self.assertRegex(audit.chunk_manifest_root, r"^[0-9a-f]{64}$")

    def test_provider_gap_finding_must_be_complete_and_exact(self) -> None:
        manifest = self.manifest(
            provider_sequence_available=True,
            provider_scope=MarketEventProviderSequenceScope.SESSION,
        )
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            provider_seq=100,
            manifest=manifest,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            provider_seq=102,
            manifest=manifest,
        )
        records = (first, second)
        finding = self.finding(
            second,
            kind=MarketEventSequenceFindingKind.PROVIDER_SEQUENCE,
            expected_sequence=101,
            observed_sequence=102,
            detail_code="PROVIDER_SEQUENCE_GAP",
        )
        manifests = (manifest,)
        snapshot = self.snapshot(
            records=records,
            findings=(finding,),
            manifests=manifests,
        )
        self.assertEqual(snapshot.audit.finding_count, 1)
        self.assertRegex(snapshot.audit.finding_set_digest, r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(MarketEventSourceContractError, "findings"):
            self.snapshot(
                records=records,
                findings=(),
                manifests=manifests,
            )
        with self.assertRaisesRegex(MarketEventSourceContractError, "findings"):
            self.snapshot(
                records=records,
                findings=(replace(finding, observed_sequence=103),),
                manifests=manifests,
            )

    def test_audit_and_selection_identities_bind_finding_digests(self) -> None:
        first = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            callback_seq=1,
        )
        second = self.record(
            append_order=2,
            symbol="600519.SH",
            previous_global=first.record_content_hash,
            previous_partition=first.record_content_hash,
            minute_offset=1,
            callback_seq=3,
        )
        finding = self.finding(
            second,
            kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
            expected_sequence=2,
            observed_sequence=3,
            detail_code="CALLBACK_SEQUENCE_GAP",
        )
        records = (first, second)
        snapshot = self.snapshot(records=records, findings=(finding,))
        selection = self.select(snapshot, records, findings=(finding,))
        self.assertEqual(
            snapshot.audit.as_dict()["finding_set_digest"],
            snapshot.audit.finding_set_digest,
        )
        self.assertEqual(
            selection.as_dict()["relevant_finding_ids"],
            [finding.finding_id],
        )
        self.assertEqual(
            selection.snapshot_finding_set_digest,
            snapshot.audit.finding_set_digest,
        )

    def test_selection_is_bound_to_snapshot_audit_and_high_water(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        selection = self.select(snapshot, records)
        self.assertEqual(selection.source_store_id, self.store_id)
        self.assertEqual(selection.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(selection.snapshot_audit_id, snapshot.audit.audit_id)
        self.assertEqual(selection.snapshot_high_water_append_order, 3)
        self.assertEqual(tuple(item.append_order for item in selection.records), (1, 3))
        self.assertRegex(selection.selection_id, r"^[0-9a-f]{64}$")
        verification = self.verify(snapshot, selection, records)
        self.assertEqual(verification.selection_id, selection.selection_id)

    def test_selection_can_prove_an_empty_symbol_view(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        selection = self.select(snapshot, records, symbol="601318.SH")
        self.assertEqual(selection.records, ())
        self.assertEqual(selection.snapshot_audit_id, snapshot.audit.audit_id)
        self.assertEqual(selection.snapshot_high_water_append_order, 3)
        self.assertRegex(selection.commitment.commitment_id, r"^[0-9a-f]{64}$")
        self.assertRegex(selection.membership_witness.witness_id, r"^[0-9a-f]{64}$")

    def test_selection_rejects_record_after_frozen_high_water(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        fourth = self.record(
            append_order=4,
            symbol="600519.SH",
            previous_global=records[-1].record_content_hash,
            previous_partition=records[-1].record_content_hash,
            minute_offset=3,
        )
        raced_prefix = records + (fourth,)
        with self.assertRaisesRegex(
            MarketEventSourceContractError,
            "global prefix|frozen audit",
        ):
            self.select(snapshot, raced_prefix)

    def test_arbitrary_direct_empty_selection_cannot_self_certify(self) -> None:
        with self.assertRaises(TypeError):
            MarketEventSelection()

    def test_selection_limit_is_strict(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        with self.assertRaisesRegex(MarketEventSourceContractError, "strict"):
            self.select(snapshot, records, record_limit=1)

    def test_selection_from_other_snapshot_is_rejected(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        selection = self.select(snapshot, records)
        other = self.snapshot(records=records[:2])
        with self.assertRaisesRegex(MarketEventSourceContractError, "exact"):
            self.verify(other, selection, records[:2])

    def test_internal_selection_factory_rejects_foreign_store_or_symbol_records(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        foreign_store = self.records()[0]
        object.__setattr__(foreign_store, "source_store_id", _hash("foreign-store"))
        for selected in ((foreign_store,), (records[1],)):
            with self.subTest(record=selected[0].source_record_id), self.assertRaisesRegex(
                MarketEventSourceContractError,
                "outside its exact query",
            ):
                MarketEventSelection._from_verified_snapshot(
                    snapshot=snapshot,
                    symbol="600519.SH",
                    market=Market.A,
                    start=self.base_time,
                    end=self.base_time + timedelta(minutes=10),
                    records=selected,
                    findings=(),
                    source_session_manifests=(self.default_manifest,),
                    allowed_event_types=("TRADE_TICK",),
                    interval_boundary_policy_id=MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
                    record_limit=100,
                )

    def test_exact_rescan_rejects_forged_or_incomplete_selection(self) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        selection = self.select(snapshot, records)
        object.__setattr__(selection, "selection_id", _hash("forged-selection"))
        with self.assertRaisesRegex(MarketEventSourceContractError, "selection_id"):
            self.verify(snapshot, selection, records)

        selection = self.select(snapshot, records)
        object.__setattr__(
            selection,
            "relevant_finding_set_digest",
            _hash("forged-findings"),
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "finding digest"):
            self.verify(snapshot, selection, records)

        for selected in ((records[0],), ()):
            forged = MarketEventSelection._from_verified_snapshot(
                snapshot=snapshot,
                symbol="600519.SH",
                market=Market.A,
                start=self.base_time,
                end=self.base_time + timedelta(minutes=10),
                records=selected,
                findings=(),
                source_session_manifests=(self.default_manifest,),
                allowed_event_types=("TRADE_TICK",),
                interval_boundary_policy_id=MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
                record_limit=100,
            )
            with self.subTest(selected=len(selected)), self.assertRaisesRegex(
                MarketEventSourceContractError,
                "exact read-port result",
            ):
                self.verify(snapshot, forged, records)

    def test_selection_rejects_non_trade_event_when_trade_only_requested(self) -> None:
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            event_type="ORDER_BOOK",
        )
        snapshot = self.snapshot(records=(record,))
        selection = self.select(snapshot, (record,))
        self.assertEqual(selection.records, ())
        order_book = self.select(
            snapshot,
            (record,),
            allowed_event_types=("ORDER_BOOK",),
        )
        self.assertEqual(order_book.records, (record,))


if __name__ == "__main__":
    unittest.main()
