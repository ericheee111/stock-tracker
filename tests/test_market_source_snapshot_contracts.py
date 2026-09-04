from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_tracker.core.market_time import (
    MARKET_SESSION_LABEL_POLICY_V1,
    market_session_date,
)
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    FIXTURE_TRADE_DECODER_POLICY_V1,
    FIXTURE_TRADE_TICK_SCHEMA_V1,
    MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
    MARKET_EVENT_SEQUENCE_POLICY_V3,
    CoverageOrigin,
    DecodedTradeTick,
    FindingResolutionState,
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
    MarketEventSubscriptionManifest,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class MarketEventSourceSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store_id = _hash("market-event-store")
        self.base_time = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)
        self.manifests_by_id = {}
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
        manifest = MarketEventSourceSessionManifest(
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
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
            subscription=MarketEventSubscriptionManifest(
                Market.A, ("000001.SZ", "600519.SH"), ("ORDER_BOOK", "TRADE_TICK")
            ),
            subscription_activated_at=self.base_time - timedelta(hours=1),
            coverage_through=self.base_time + timedelta(hours=1),
            coverage_origin=CoverageOrigin.LIVE,
            queue_overflow_count=0,
            dropped_callback_count=0,
            first_callback_seq=None,
            last_callback_seq=None,
        )
        self.manifests_by_id[manifest.manifest_id] = manifest
        return manifest

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
            source_schema_id=FIXTURE_TRADE_TICK_SCHEMA_V1,
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
        return MarketEventSequenceFinding.from_record(
            record=record,
            manifest=self.manifests_by_id[record.source_session_manifest_id],
            kind=kind,
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
        transport_snapshot=None,
    ) -> MarketEventStoreAudit:
        actual_manifests = (self.default_manifest,) if manifests is None else manifests
        inventory = MarketEventInventoryVerification.create_from_prefix(
            source_store_id=self.store_id,
            catalog_schema_fingerprint=_hash("catalog-schema-v4"),
            records=records,
        )
        return MarketEventStoreAudit.create_from_prefix(
            source_store_id=self.store_id,
            source_schema_id="stock-tracker-market-event-store-v4",
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
            audited_at=audited_at
            or max(
                (
                    self.base_time,
                    *(item.durable_known_at for item in records),
                    *(item.coverage_through for item in actual_manifests),
                )
            )
            + timedelta(seconds=1),
            catalog_schema_fingerprint=_hash("catalog-schema-v4"),
            inventory_verification=inventory,
            partition_heads=self.partition_heads(records),
            records=records,
            findings=findings,
            source_session_manifests=actual_manifests,
            transport_snapshot=transport_snapshot,
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
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "record_content_hash"
        ):
            replace(record, record_content_hash="f" * 64)
        with self.assertRaisesRegex(MarketEventSourceContractError, "partition_key"):
            replace(
                record, partition_key="market=A/trading_day=2026-09-02/symbol=000001.SZ"
            )

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

    def test_record_rejects_noncanonical_payload_and_future_durable_identity(
        self,
    ) -> None:
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
            with (
                self.subTest(path=path),
                self.assertRaises(MarketEventSourceContractError),
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
                sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
                audited_at=records[-1].durable_known_at + timedelta(seconds=1),
                catalog_schema_fingerprint=_hash("catalog-schema-v4"),
                records=records,
                partition_heads=self.partition_heads(records),
                findings=(),
                source_session_manifests=(self.default_manifest,),
            )

    def test_inventory_rejects_duplicate_event_record_and_storage_identity(
        self,
    ) -> None:
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

    def test_reconnect_epoch_reset_is_scoped_and_provider_capability_cannot_drift(
        self,
    ) -> None:
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
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "capability changes"
        ):
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
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "fabricated|duplicate"
        ):
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
        with self.assertRaisesRegex(
            MarketEventSourceContractError, "global prefix|inventory"
        ):
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

    def test_internal_selection_factory_rejects_foreign_store_or_symbol_records(
        self,
    ) -> None:
        records = self.records()
        snapshot = self.snapshot(records)
        foreign_store = self.records()[0]
        object.__setattr__(foreign_store, "source_store_id", _hash("foreign-store"))
        for selected in ((foreign_store,), (records[1],)):
            with (
                self.subTest(record=selected[0].source_record_id),
                self.assertRaisesRegex(
                    MarketEventSourceContractError,
                    "outside its exact query|record_content_hash mismatch",
                ),
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
                    coverage_verified_manifest_ids=(),
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
                coverage_verified_manifest_ids=(),
                allowed_event_types=("TRADE_TICK",),
                interval_boundary_policy_id=MARKET_EVENT_INTERVAL_BOUNDARY_POLICY_V1,
                record_limit=100,
            )
            with (
                self.subTest(selected=len(selected)),
                self.assertRaisesRegex(
                    MarketEventSourceContractError,
                    "exact read-port result",
                ),
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


class TestR3SourceClosure(MarketEventSourceSnapshotTestCase):
    def test_r3_global_gap_on_other_symbol_contaminates_selection(self) -> None:
        first = self.records()[0]
        second = self.record(
            append_order=2,
            symbol="000001.SZ",
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
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
        snapshot = self.snapshot(records, findings=(finding,))
        selection = self.select(snapshot, records, findings=(finding,))
        self.assertEqual(selection.relevant_findings, (finding,))

    def test_r3_live_coverage_cannot_predate_collector(self) -> None:
        with self.assertRaises(MarketEventSourceContractError):
            replace(
                self.default_manifest,
                coverage_start=self.default_manifest.collector_started_at
                - timedelta(hours=1),
            )

    def test_r3_conflicting_manifest_epoch_is_rejected(self) -> None:
        conflict = replace(self.default_manifest, expected_first_callback_seq=7)
        manifests = tuple(
            sorted((self.default_manifest, conflict), key=lambda item: item.manifest_id)
        )
        with self.assertRaises(MarketEventSourceContractError):
            self.snapshot(records=(), manifests=manifests)

    def test_r3_pure_factory_cannot_claim_store_assurance(self) -> None:
        self.assertEqual(
            getattr(self.snapshot(), "assurance", None), "STRUCTURAL_FIXTURE"
        )

    def test_r3_empty_unsubscribed_selection_is_not_zero_event_proof(self) -> None:
        records = self.records()
        selection = self.select(self.snapshot(records), records, symbol="601318.SH")
        self.assertEqual(getattr(selection, "completeness", None), "EMPTY_NOT_PROVEN")

    def test_r3_queue_loss_is_part_of_coverage_contract(self) -> None:
        fields = MarketEventSourceSessionManifest.__dataclass_fields__
        self.assertIn("queue_overflow_count", fields)
        self.assertIn("dropped_callback_count", fields)

    def decode_payload(self, payload, schema=FIXTURE_TRADE_TICK_SCHEMA_V1):
        original = self.records()[0]
        excluded = {
            "source_record_id",
            "record_content_hash",
            "record_storage_key",
            "record_file_sha256",
            "payload_json",
            "payload_sha256",
        }
        values = {
            name: getattr(original, name)
            for name in original.__dataclass_fields__
            if name not in excluded
        }
        values["source_schema_id"] = schema
        record = MarketEventSourceRecord.create(**values, payload=payload)
        snapshot = self.snapshot((record,))
        selection = self.select(snapshot, (record,))
        verification = self.verify(snapshot, selection, (record,))
        return DecodedTradeTick(
            record, selection, verification, FIXTURE_TRADE_DECODER_POLICY_V1
        )

    def test_decoder_uses_exact_payload_and_exposes_exact_identity(self):
        tick = self.decode_payload({"last_price": "10.00", "quantity": 200})
        self.assertEqual(str(tick.price), "10.00")
        self.assertEqual(tick.quantity, 200)
        self.assertEqual(tick.source_record_id, tick.record.source_record_id)
        self.assertEqual(
            tick.selection_verification_id, tick.verification.verification_id
        )
        self.assertEqual(tick.as_dict()["assurance"], "STRUCTURAL_FIXTURE")
        with self.assertRaises(TypeError):
            replace(tick, price=Decimal(12))
        with self.assertRaises(MarketEventSourceContractError):
            replace(tick, decoder_policy_id="unknown-decoder")
        with self.assertRaises(MarketEventSourceContractError):
            self.decode_payload(
                {"last_price": "10", "quantity": 1}, "xtp-unimplemented"
            )

    def test_decoder_rejects_bool_float_missing_extra_and_nonfinite_payloads(self):
        payloads = (
            {"last_price": True, "quantity": 1},
            {"last_price": 10.0, "quantity": 1},
            {"last_price": "NaN", "quantity": 1},
            {"last_price": "Infinity", "quantity": 1},
            {"last_price": "0", "quantity": 1},
            {"last_price": "-10", "quantity": 1},
            {"last_price": "10", "quantity": True},
            {"last_price": "10", "quantity": 1.0},
            {"quantity": 1},
            {"last_price": "10", "quantity": 1, "unknown": 0},
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                self.assertRaises(MarketEventSourceContractError),
            ):
                self.decode_payload(payload)

    def test_record_file_hash_has_no_self_reference(self):
        record = self.records()[0]
        content = record.record_content_bytes()
        self.assertEqual(
            hashlib.sha256(content).hexdigest(), record.record_content_hash
        )
        self.assertEqual(record.record_content_hash, record.record_file_sha256)
        self.assertIn(b'"payload_json"', content)
        for key in (
            b'"record_file_sha256"',
            b'"source_record_id"',
            b'"record_content_hash"',
        ):
            self.assertNotIn(key, content)
        with self.assertRaises(MarketEventSourceContractError):
            replace(record, record_file_sha256="f" * 64)

    def test_zero_event_requires_subscribed_lossless_interval(self):
        for field in ("queue_overflow_count", "dropped_callback_count"):
            manifest = replace(self.default_manifest, **{field: 1})
            snapshot = self.snapshot((), manifests=(manifest,))
            selection = self.select(snapshot, (), manifests=(manifest,))
            self.assertFalse(
                selection.covers_interval(
                    selection.start_source_time, selection.end_source_time
                )
            )
            self.assertEqual(selection.completeness, "EMPTY_NOT_PROVEN")
        snapshot = MarketEventStoreSnapshot.from_audit(
            self.audit(
                (), transport_snapshot=transport_fixture((self.default_manifest,))
            )
        )
        selection = self.select(snapshot, ())
        self.assertEqual(selection.completeness, "ZERO_EVENT_PROVEN")
        self.assertFalse(
            selection.covers_interval(
                selection.start_source_time, self.base_time + timedelta(hours=2)
            )
        )

    def test_explicit_callback_bound_drift_is_rejected(self):
        manifest = replace(
            self.default_manifest, first_callback_seq=1, last_callback_seq=2
        )
        record = self.record(
            append_order=1,
            symbol="600519.SH",
            previous_global="0" * 64,
            previous_partition="0" * 64,
            manifest=manifest,
        )
        with self.assertRaisesRegex(MarketEventSourceContractError, "callback bounds"):
            self.snapshot((record,), manifests=(manifest,))

    def test_session_global_gap_crosses_connection_epoch_time_ranges(self):
        first_manifest = replace(
            self.default_manifest,
            coverage_through=self.base_time + timedelta(minutes=30),
        )
        second_manifest = replace(
            self.default_manifest,
            connection_epoch=2,
            coverage_start=self.base_time + timedelta(minutes=30),
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
            symbol="000001.SZ",
            previous_global=first.record_content_hash,
            previous_partition="0" * 64,
            minute_offset=40,
            callback_seq=3,
            manifest=second_manifest,
        )
        manifests = (first_manifest, second_manifest)
        finding = MarketEventSequenceFinding.from_record(
            record=second,
            manifest=second_manifest,
            kind=MarketEventSequenceFindingKind.CALLBACK_SEQUENCE,
            expected_sequence=2,
            observed_sequence=3,
            detail_code="CALLBACK_SEQUENCE_GAP",
            scope_manifests=manifests,
        )
        snapshot = self.snapshot(
            (first, second), manifests=manifests, findings=(finding,)
        )
        selection = self.select(
            snapshot, (first, second), manifests=manifests, findings=(finding,)
        )
        self.assertEqual(selection.records, (first,))
        self.assertEqual(selection.relevant_findings, (finding,))
        self.assertEqual(
            selection.completeness, "BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY"
        )
        with self.assertRaises(MarketEventSourceContractError):
            self.snapshot(
                (first, second),
                manifests=manifests,
                findings=(
                    replace(
                        finding,
                        resolution_state=FindingResolutionState.RESOLVED,
                        replay_resolution_id=_hash("caller-resolution"),
                    ),
                ),
            )

    def test_backfill_requires_verified_interval_membership_not_hash(self):
        snapshot = self.snapshot(())
        selection = self.select(snapshot, ())
        verification = self.verify(snapshot, selection, ())
        arguments = {
            "collector_started_at": self.base_time + timedelta(hours=2),
            "subscription_activated_at": self.base_time + timedelta(hours=2),
            "coverage_start": selection.start_source_time,
            "coverage_through": selection.end_source_time,
            "coverage_origin": CoverageOrigin.BACKFILL,
            "subscription": MarketEventSubscriptionManifest(
                Market.A, ("600519.SH",), ("TRADE_TICK",)
            ),
        }
        with self.assertRaisesRegex(MarketEventSourceContractError, "membership"):
            replace(self.default_manifest, **arguments)
        backfill = replace(
            self.default_manifest,
            **arguments,
            replay_selection=selection,
            replay_verification=verification,
        )
        self.assertEqual(backfill.assurance, "STRUCTURAL_FIXTURE")
        with self.assertRaises(TypeError):
            replace(backfill, assurance="STORE_RESCANNED")


def transport_fixture(
    manifests,
    records=(),
    *,
    omit=(),
    disconnect=False,
    heartbeat_gap=False,
    loss=0,
    store_id=None,
):
    from stock_tracker.runtime_evidence.source_snapshot_contracts import (
        MarketEventTransportKind,
        MarketEventTransportRecord,
        MarketEventTransportSnapshot,
    )

    events = []
    for manifest in manifests:
        start, end = manifest.coverage_start, manifest.coverage_through
        end = max(
            end,
            *(
                r.durable_known_at
                for r in records
                if r.source_session_manifest_id == manifest.manifest_id
            ),
            end,
        )
        entries = [(start, "CONNECTED"), (start, "SUBSCRIPTION_ACKNOWLEDGED")]
        stamp = start
        while stamp < end:
            entries.extend(
                (stamp, kind)
                for kind in ("HEARTBEAT", "CALLBACK_WATERMARK", "QUEUE_WATERMARK")
            )
            stamp += timedelta(minutes=5)
        entries.extend(
            (end, kind)
            for kind in (
                "HEARTBEAT",
                "CALLBACK_WATERMARK",
                "QUEUE_WATERMARK",
                "SESSION_CLOSED",
            )
        )
        if disconnect:
            entries.append((start + (end - start) / 2, "DISCONNECTED"))
        for stamp, kind in entries:
            if kind in omit or (
                heartbeat_gap and kind == "HEARTBEAT" and start < stamp < end
            ):
                continue
            members = [
                r
                for r in records
                if r.session_id == manifest.session_id
                and r.connection_epoch == manifest.connection_epoch
                and r.reconnect_epoch == manifest.reconnect_epoch
                and r.durable_known_at <= stamp
            ]
            events.append(
                (
                    stamp,
                    kind,
                    manifest,
                    max((r.callback_seq for r in members), default=0),
                    max(
                        (r.provider_seq for r in members if r.provider_seq is not None),
                        default=None,
                    ),
                )
            )
    result = []
    for stamp, kind, manifest, callback, provider in sorted(
        events, key=lambda row: row[0]
    ):
        result.append(
            MarketEventTransportRecord(
                source_store_id=store_id or manifest.source_store_id,
                transport_stream_id=_hash("fixture-transport"),
                transport_append_order=len(result) + 1,
                previous_transport_record_hash=result[-1].record_hash
                if result
                else "0" * 64,
                session_id=manifest.session_id,
                connection_epoch=manifest.connection_epoch,
                reconnect_epoch=manifest.reconnect_epoch,
                kind=MarketEventTransportKind(kind),
                observed_at=stamp,
                durable_known_at=stamp,
                subscription_scope_id=None
                if kind in {"CONNECTED", "HEARTBEAT"}
                else manifest.subscription.subscription_scope_id,
                callback_high_water=callback,
                provider_high_water=provider,
                queue_overflow_count=loss if stamp > manifest.coverage_start else 0,
                dropped_callback_count=0,
                canonical_payload="{}",
            )
        )
    return MarketEventTransportSnapshot.from_records(
        source_store_id=store_id or manifests[0].source_store_id,
        records=tuple(result),
        audited_at=max(r.durable_known_at for r in result),
    )


class TestR4TransportClosure(MarketEventSourceSnapshotTestCase):
    def with_transport(self, **kwargs):
        transport = transport_fixture((self.default_manifest,), **kwargs)
        audit = self.audit((), transport_snapshot=transport)
        return self.select(MarketEventStoreSnapshot.from_audit(audit), ())

    def test_r4_zero_callbacks_without_liveness_is_not_zero_proven(self):
        selection = self.select(self.snapshot(()), ())
        self.assertEqual(selection.completeness.value, "EMPTY_NOT_PROVEN")

    def test_r4_complete_liveness_proves_structural_zero(self):
        selection = self.with_transport()
        self.assertEqual(selection.completeness.value, "ZERO_EVENT_PROVEN")
        self.assertEqual(
            selection.transport_certificate.assurance.value, "STRUCTURAL_FIXTURE"
        )

    def test_r4_disconnect_blocks_coverage(self):
        self.assertFalse(
            self.with_transport(disconnect=True).covers_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )

    def test_r4_heartbeat_gap_blocks_coverage(self):
        self.assertFalse(
            self.with_transport(heartbeat_gap=True).covers_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )

    def test_r4_counter_increment_blocks_coverage(self):
        self.assertFalse(
            self.with_transport(loss=1).covers_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )

    def test_r4_missing_close_cannot_complete_session(self):
        selection = self.with_transport(omit=("SESSION_CLOSED",))
        self.assertFalse(
            selection.transport_certificate.proves_session_close(
                self.default_manifest.coverage_through
            )
        )

    def test_r4_transport_store_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "store"):
            self.with_transport(store_id=_hash("another-store"))

    def test_r4_later_transport_cannot_change_earlier_event_snapshot(self):
        earlier = self.snapshot(())
        transport = transport_fixture((self.default_manifest,))
        later = MarketEventStoreSnapshot.from_audit(
            self.audit((), transport_snapshot=transport)
        )
        self.assertNotEqual(earlier.snapshot_id, later.snapshot_id)
        self.assertFalse(
            self.select(earlier, ()).covers_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )

    def clock_record(self, *, ahead_seconds=0, delay_seconds=2):
        from dataclasses import fields

        first = self.records()[0]
        args = {
            f.name: getattr(first, f.name)
            for f in fields(first)
            if f.init
            and f.name
            not in {
                "payload_json",
                "payload_sha256",
                "record_storage_key",
                "record_file_sha256",
                "record_content_hash",
            }
        }
        args.update(
            received_at=first.source_time - timedelta(seconds=ahead_seconds),
            durable_known_at=first.source_time + timedelta(seconds=delay_seconds),
        )
        return MarketEventSourceRecord.create(
            **args, payload={"last_price": 11, "quantity": 100}
        )

    def test_r4_one_hour_negative_latency_rejected_or_blocked(self):
        record = self.clock_record(ahead_seconds=3600)
        with self.assertRaisesRegex(ValueError, "finding|clock"):
            self.snapshot((record,))

    def test_r4_small_explicit_clock_skew_accepted(self):
        from stock_tracker.runtime_evidence.source_snapshot_contracts import (
            SourceClockPolicy,
        )

        self.assertGreater(
            SourceClockPolicy().maximum_source_ahead_of_received.total_seconds(), 0
        )
        self.snapshot((self.clock_record(ahead_seconds=1),))

    def test_r4_transport_factory_cannot_claim_store_rescanned(self):
        transport = transport_fixture((self.default_manifest,))
        self.assertEqual(transport.assurance.value, "STRUCTURAL_FIXTURE")
        with self.assertRaises(TypeError):
            replace(transport, assurance="STORE_RESCANNED")

    def test_r4_whole_package_type_diagnostics(self):
        import subprocess

        result = subprocess.run(
            [
                "py",
                "-3.14",
                "-m",
                "basedpyright",
                "--level",
                "error",
                "stock_tracker/runtime_evidence",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, (result.stdout or "") + (result.stderr or "")
        )

    def test_r4_missing_ack_and_final_watermark_fail_closed(self):
        for kind in (
            "SUBSCRIPTION_ACKNOWLEDGED",
            "CALLBACK_WATERMARK",
            "QUEUE_WATERMARK",
        ):
            with self.subTest(kind=kind):
                selection = self.with_transport(omit=(kind,))
                self.assertEqual(selection.completeness.value, "EMPTY_NOT_PROVEN")

    def test_r4_clock_findings_are_required_and_contaminate_other_symbol(self):
        record = self.clock_record(ahead_seconds=3600)
        findings = tuple(
            self.finding(
                record,
                kind=kind,
                expected_sequence=None,
                observed_sequence=None,
                detail_code=kind.value,
            )
            for kind in (
                MarketEventSequenceFindingKind.SOURCE_CLOCK_SKEW,
                MarketEventSequenceFindingKind.DURABILITY_DELAY,
            )
        )
        snapshot = self.snapshot((record,), findings=findings)
        for symbol in ("600519.SH", "000001.SZ"):
            selection = self.select(
                snapshot, (record,), symbol=symbol, findings=findings
            )
            self.assertEqual(
                selection.completeness.value, "BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY"
            )

    def test_r4_durability_delay_boundary_is_explicit(self):
        record = self.clock_record(delay_seconds=301)
        finding = self.finding(
            record,
            kind=MarketEventSequenceFindingKind.DURABILITY_DELAY,
            expected_sequence=None,
            observed_sequence=None,
            detail_code="DURABILITY_DELAY",
        )
        selection = self.select(
            self.snapshot((record,), findings=(finding,)),
            (record,),
            findings=(finding,),
        )
        self.assertFalse(
            selection.covers_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )
        self.snapshot((self.clock_record(delay_seconds=300),))

    def test_r4_unknown_clock_and_liveness_policies_rejected(self):
        from stock_tracker.runtime_evidence.source_snapshot_contracts import (
            SourceClockPolicy,
            TransportLivenessPolicy,
        )

        for factory in (SourceClockPolicy, TransportLivenessPolicy):
            with self.assertRaisesRegex(ValueError, "policy"):
                factory(_hash("self-named-policy"))
        with self.assertRaisesRegex(ValueError, "clock"):
            replace(self.default_manifest, clock_policy_id=_hash("caller-clock"))

    def test_r4_transport_chain_and_counter_rollback_rejected(self):
        transport = transport_fixture((self.default_manifest,))
        first, second = transport.records[:2]
        for records in (
            (first, replace(second, transport_append_order=3)),
            (first, replace(second, previous_transport_record_hash=_hash("wrong"))),
        ):
            with self.assertRaisesRegex(ValueError, "prefix"):
                replace(transport, records=records)
        first = replace(first, queue_overflow_count=1)
        second = replace(second, previous_transport_record_hash=first.record_hash)
        with self.assertRaisesRegex(ValueError, "monotonic"):
            replace(transport, records=(first, second))

    def test_r4_backfill_requires_exact_completed_job(self):
        from stock_tracker.runtime_evidence.source_snapshot_contracts import (
            MarketEventTransportKind,
            MarketEventTransportRecord,
            MarketEventTransportSnapshot,
        )

        original = self.with_transport()
        snapshot = original.transport_certificate.event_snapshot
        verification = snapshot.verify_selection(
            original,
            records=(),
            partition_heads=(),
            findings=(),
            source_session_manifests=(self.default_manifest,),
        )
        job_time = self.base_time + timedelta(days=1)
        manifest = replace(
            self.default_manifest,
            session_id="backfill-job",
            coverage_origin=CoverageOrigin.BACKFILL,
            subscription=MarketEventSubscriptionManifest(
                Market.A, ("600519.SH",), ("TRADE_TICK",)
            ),
            coverage_start=original.start_source_time,
            coverage_through=original.end_source_time,
            collector_started_at=job_time,
            subscription_activated_at=job_time,
            replay_selection=original,
            replay_verification=verification,
        )
        rows = []
        for index, kind in enumerate(
            (
                MarketEventTransportKind.BACKFILL_STARTED,
                MarketEventTransportKind.BACKFILL_COMPLETED,
            ),
            1,
        ):
            rows.append(
                MarketEventTransportRecord(
                    self.store_id,
                    _hash("backfill-stream"),
                    index,
                    rows[-1].record_hash if rows else "0" * 64,
                    manifest.session_id,
                    1,
                    0,
                    kind,
                    job_time,
                    job_time,
                    manifest.subscription.subscription_scope_id,
                    0,
                    None,
                    0,
                    0,
                    "{}",
                    original,
                    verification,
                    0,
                    "0" * 64,
                )
            )

        def select_job(rows):
            transport = MarketEventTransportSnapshot.from_records(
                source_store_id=self.store_id, records=tuple(rows), audited_at=job_time
            )
            audit = self.audit(
                (),
                manifests=(manifest,),
                transport_snapshot=transport,
                audited_at=job_time,
            )
            return self.select(
                MarketEventStoreSnapshot.from_audit(audit), (), manifests=(manifest,)
            )

        self.assertEqual(select_job(rows).completeness.value, "ZERO_EVENT_PROVEN")
        self.assertEqual(select_job(rows[:1]).completeness.value, "EMPTY_NOT_PROVEN")
        wrong = replace(rows[1], job_output_chain_head=_hash("fabricated-output"))
        self.assertEqual(
            select_job((rows[0], wrong)).completeness.value, "EMPTY_NOT_PROVEN"
        )

    def test_r4_backfill_cannot_bootstrap_an_unproven_input(self):
        selection = self.select(self.snapshot(()), ())
        self.assertEqual(selection.completeness.value, "EMPTY_NOT_PROVEN")
        self.assertFalse(
            selection.transport_certificate.proves_interval(
                self.base_time, self.base_time + timedelta(minutes=10)
            )
        )

    def test_r4_event_watermark_mismatch_is_integrity_blocked(self):
        transport = transport_fixture((self.default_manifest,))
        rows = []
        for item in transport.records:
            rows.append(
                replace(
                    item,
                    callback_high_water=9,
                    previous_transport_record_hash=rows[-1].record_hash
                    if rows
                    else "0" * 64,
                )
            )
        transport = replace(transport, records=tuple(rows))
        selection = self.select(
            MarketEventStoreSnapshot.from_audit(
                self.audit((), transport_snapshot=transport)
            ),
            (),
        )
        self.assertEqual(
            selection.completeness.value, "BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY"
        )

    def test_r4_event_appended_after_final_watermark_cannot_complete_prefix(self):
        record = self.clock_record(ahead_seconds=-3600, delay_seconds=3602)
        transport = transport_fixture((self.default_manifest,))
        audit = self.audit((record,), transport_snapshot=transport)
        selection = self.select(MarketEventStoreSnapshot.from_audit(audit), (record,))
        self.assertEqual(
            selection.completeness.value, "BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY"
        )


if __name__ == "__main__":
    unittest.main()
