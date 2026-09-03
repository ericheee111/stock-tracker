from __future__ import annotations

import copy
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
from stock_tracker.runtime_evidence.path_contracts import (
    RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
    RuntimeAuthorityFactReference,
    RuntimeAuthorityFactSelection,
    RuntimeAuthorityKind,
    RuntimeAuthoritySnapshotBinding,
    RuntimeCalendarState,
    RuntimeCaseFactSelection,
    RuntimeCoverageState,
    RuntimeExecutionEvidenceReference,
    RuntimeExecutionFragment,
    RuntimeExecutionSide,
    RuntimeExecutionSummary,
    RuntimeFillCompletion,
    RuntimeFrozenPathFact,
    RuntimeFrozenPathPrefix,
    RuntimeMarketSourceSnapshotBinding,
    RuntimeNoEntryEvidence,
    RuntimeNoEntryReason,
    RuntimeOpenSessionState,
    RuntimePathBlockerCode,
    RuntimePathContractError,
    RuntimePathFactKind,
    RuntimePathGranularity,
    RuntimePathObservation,
    RuntimePathResolutionState,
    RuntimePathStoreSnapshotBinding,
    RuntimePathWindow,
    RuntimeProjectionArtifactReference,
    RuntimeProjectionLineage,
    RuntimeSessionEvidence,
    RuntimeSourceReference,
    resolve_runtime_path,
)
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MARKET_EVENT_SEQUENCE_POLICY_V1,
    MarketEventCallbackSequenceScope,
    MarketEventInventoryVerification,
    MarketEventPartitionHead,
    MarketEventProviderSequenceScope,
    MarketEventSourceRecord,
    MarketEventSourceSessionManifest,
    MarketEventStoreAudit,
    MarketEventStoreSnapshot,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class RuntimePathContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.entry_time = datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc)
        self.collection_store_id = _hash("collection-store")
        self.source_store_id = _hash("market-event-store")
        self.case_id = _hash("case")
        self.source_snapshot_id = _hash("source-snapshot")
        self.source_audit_id = _hash("source-audit")
        self.source_finding_set_digest = _hash("source-findings")
        self.source_high_water = 10_000
        self.coverage_fact_by_index: dict[int, str] = {}
        self.raw_source_time_by_collection_order: dict[int, datetime] = {}

    def source_reference(
        self,
        *,
        label: str,
        append_order: int,
        known_at: datetime,
        source_time: datetime | None = None,
        event_type: str = "TRADE_TICK",
    ) -> RuntimeSourceReference:
        source_time = known_at - timedelta(seconds=2) if source_time is None else source_time
        return RuntimeSourceReference(
            source_store_id=self.source_store_id,
            source_snapshot_id=self.source_snapshot_id,
            source_audit_id=self.source_audit_id,
            source_high_water_append_order=self.source_high_water,
            finding_set_digest=self.source_finding_set_digest,
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
            selection_id=_hash("placeholder-selection"),
            event_id=_hash(f"event:{label}"),
            source_session_id="market-session-a",
            symbol="600519.SH",
            market=Market.A,
            event_type=event_type,
            trading_day=market_session_date(
                source_time,
                Market.A,
                MARKET_SESSION_LABEL_POLICY_V1,
            ),
            session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
            source_record_id=_hash(f"source-record:{label}"),
            source_append_order=append_order,
            source_record_hash=_hash(f"record:{label}"),
            raw_payload_sha256=_hash(f"payload:{label}"),
            parser_id="market-event-parser-v1",
            schema_id="market-event-record-v1",
            source_time=source_time,
            received_at=known_at - timedelta(seconds=1),
            durable_known_at=known_at,
        )

    def authority_reference(
        self,
        *,
        kind: RuntimeAuthorityKind,
        trading_day: date,
        known_at: datetime,
        label: str,
        revision: int = 1,
        usable_from: datetime | None = None,
    ) -> RuntimeAuthorityFactReference:
        return RuntimeAuthorityFactReference(
            authority_kind=kind,
            authority_store_id=_hash(f"authority-store:{kind.value}"),
            fact_schema=f"stage4g1-{kind.value.lower()}-fact-v1",
            effective_session_date=trading_day,
            known_at=known_at,
            usable_from=known_at if usable_from is None else usable_from,
            source=f"test-{kind.value.lower()}",
            revision=revision,
            policy_id=_hash(f"policy:{kind.value}"),
            fact_payload_sha256=_hash(f"payload:{label}"),
        )

    def session_fact(
        self,
        *,
        trading_day: date,
        collection_order: int,
        source_order: int,
        open_session_index: int | None,
        calendar_state: RuntimeCalendarState = RuntimeCalendarState.OPEN,
        open_session_state: RuntimeOpenSessionState | None = RuntimeOpenSessionState.TRADED,
        coverage_state: RuntimeCoverageState = RuntimeCoverageState.COMPLETE_SESSION,
        session_complete: bool = True,
        coverage_through: datetime | None = None,
    ) -> RuntimeFrozenPathFact:
        day_start = datetime.combine(
            trading_day,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        scheduled_open = day_start + timedelta(hours=1)
        scheduled_close = day_start + timedelta(hours=7)
        known_at = day_start + timedelta(hours=8)
        closed = calendar_state is RuntimeCalendarState.MARKET_CLOSED
        calendar_reference = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=trading_day,
            known_at=known_at,
            label=f"calendar-{trading_day.isoformat()}",
            revision=source_order,
        )
        security_reference = (
            self.authority_reference(
                kind=RuntimeAuthorityKind.SECURITY_STATUS,
                trading_day=trading_day,
                known_at=known_at,
                label=f"security-{trading_day.isoformat()}",
                revision=source_order,
            )
            if open_session_state
            in {RuntimeOpenSessionState.SUSPENDED, RuntimeOpenSessionState.NO_TRADE}
            else None
        )
        coverage_reference = (
            None
            if closed
            else self.authority_reference(
                kind=RuntimeAuthorityKind.COVERAGE,
                trading_day=trading_day,
                known_at=known_at,
                label=f"coverage-{trading_day.isoformat()}",
                revision=source_order,
            )
        )
        if open_session_index is not None and coverage_reference is not None:
            self.coverage_fact_by_index[open_session_index] = coverage_reference.fact_id
        session = RuntimeSessionEvidence(
            symbol="600519.SH",
            market=Market.A,
            trading_day=trading_day,
            calendar_state=calendar_state,
            open_session_index=None if closed else open_session_index,
            open_session_state=None if closed else open_session_state,
            scheduled_open_at=None if closed else scheduled_open,
            scheduled_close_at=None if closed else scheduled_close,
            coverage_state=(
                RuntimeCoverageState.NOT_APPLICABLE if closed else coverage_state
            ),
            session_complete=True if closed else session_complete,
            coverage_through=(
                None
                if closed
                else (
                    coverage_through
                    if coverage_through is not None
                    else (
                        scheduled_close
                        if coverage_state is RuntimeCoverageState.COMPLETE_SESSION
                        else (
                            scheduled_open + timedelta(hours=1)
                            if coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
                            else None
                        )
                    )
                )
            ),
            session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
            calendar_reference=calendar_reference,
            security_status_reference=security_reference,
            coverage_reference=coverage_reference,
        )
        return RuntimeFrozenPathFact(
            collection_store_id=self.collection_store_id,
            collection_append_order=collection_order,
            case_id=self.case_id,
            collection_fact_id=_hash(f"collection-fact-{collection_order}"),
            collection_observed_at=known_at + timedelta(seconds=1),
            kind=RuntimePathFactKind.SESSION,
            session_evidence=session,
        )

    def point_fact(
        self,
        *,
        collection_order: int,
        source_order: int,
        session_index: int,
        interval_start: datetime,
        interval_end: datetime | None = None,
        high: str = "10.5",
        low: str = "9.5",
        close: str = "10",
        granularity: RuntimePathGranularity = RuntimePathGranularity.MINUTE_BAR,
        known_at: datetime | None = None,
        raw_source_time: datetime | None = None,
    ) -> RuntimeFrozenPathFact:
        interval_end = (
            interval_start + timedelta(minutes=1)
            if interval_end is None
            else interval_end
        )
        durable_known_at = (
            interval_end + timedelta(seconds=2) if known_at is None else known_at
        )
        actual_source_time = (
            interval_start
            if granularity is RuntimePathGranularity.TICK
            else raw_source_time
            if raw_source_time is not None
            else interval_start + (interval_end - interval_start) / 2
        )
        self.raw_source_time_by_collection_order[collection_order] = actual_source_time
        source = self.source_reference(
            label=f"point-{collection_order}",
            append_order=source_order,
            known_at=durable_known_at,
            source_time=actual_source_time,
        )
        projection_lineage = (
            None
            if granularity is RuntimePathGranularity.TICK
            else RuntimeProjectionLineage(
                projection_policy_id=_hash("projection-policy-v1"),
                source_store_id=self.source_store_id,
                source_snapshot_id=self.source_snapshot_id,
                source_audit_id=self.source_audit_id,
                source_high_water_append_order=self.source_high_water,
                finding_set_digest=self.source_finding_set_digest,
                selection_id=_hash("placeholder-selection"),
                selection_verification_id=_hash("placeholder-selection-verification"),
                interval_start=interval_start,
                interval_end=interval_end,
                interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                coverage_fact_id=self.coverage_fact_by_index.get(
                    session_index,
                    _hash(f"missing-coverage:{session_index}"),
                ),
                input_event_ids=(source.event_id,),
                input_source_append_orders=(source.source_append_order,),
                input_source_record_ids=(source.source_record_id,),
                input_source_record_hashes=(source.source_record_hash,),
            )
        )
        projection_artifact = (
            None
            if projection_lineage is None
            else RuntimeProjectionArtifactReference.create(
                projection_policy_id=projection_lineage.projection_policy_id,
                created_at=durable_known_at,
                durable_known_at=durable_known_at,
                symbol="600519.SH",
                market=Market.A,
                trading_day=market_session_date(
                    interval_end - timedelta(microseconds=1),
                    Market.A,
                    MARKET_SESSION_LABEL_POLICY_V1,
                ),
                interval_start=interval_start,
                interval_end=interval_end,
                interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                lineage=projection_lineage,
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                coverage_fact_id=projection_lineage.coverage_fact_id,
            )
        )
        point = RuntimePathObservation(
            symbol="600519.SH",
            market=Market.A,
            open_session_index=session_index,
            interval_start=interval_start,
            interval_end=interval_end,
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            granularity=granularity,
            interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
            source_reference=(
                source if granularity is RuntimePathGranularity.TICK else None
            ),
            projection_artifact_reference=projection_artifact,
        )
        return RuntimeFrozenPathFact(
            collection_store_id=self.collection_store_id,
            collection_append_order=collection_order,
            case_id=self.case_id,
            collection_fact_id=_hash(f"collection-fact-{collection_order}"),
            collection_observed_at=durable_known_at + timedelta(seconds=1),
            kind=RuntimePathFactKind.POINT,
            path_observation=point,
        )

    def prefix(
        self,
        facts: list[RuntimeFrozenPathFact],
        *,
        frozen_at: datetime | None = None,
        eligible_unprojected_source_times: tuple[datetime, ...] = (),
        source_start_proof: bool = True,
    ) -> RuntimeFrozenPathPrefix:
        ordered = tuple(sorted(facts, key=lambda item: item.collection_append_order))
        last_observed = max(item.collection_observed_at for item in ordered)
        actual_frozen_at = (
            last_observed + timedelta(seconds=1)
            if frozen_at is None
            else frozen_at
        )

        sessions = {
            item.session_evidence.open_session_index: item.session_evidence
            for item in ordered
            if item.session_evidence is not None
            and item.session_evidence.open_session_index is not None
        }
        manifests = tuple(
            MarketEventSourceSessionManifest(
                source_store_id=self.source_store_id,
                session_id=f"market-session-{session.open_session_index}",
                connection_epoch=session.open_session_index + 1,
                reconnect_epoch=0,
                collector_started_at=session.scheduled_open_at,
                coverage_start=session.scheduled_open_at,
                expected_first_callback_seq=1 if source_start_proof else None,
                callback_sequence_scope=MarketEventCallbackSequenceScope.SESSION,
                provider_sequence_available=False,
                provider_sequence_scope=MarketEventProviderSequenceScope.UNAVAILABLE,
                sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
            )
            for session in sorted(
                sessions.values(),
                key=lambda item: item.open_session_index,
            )
        )
        manifest_by_index = {
            index: manifest
            for index, manifest in zip(sorted(sessions), manifests, strict=True)
        }
        raw_specs: list[tuple[str, int, datetime, int]] = []
        for item in ordered:
            observation = item.path_observation
            if observation is None:
                continue
            raw_specs.append(
                (
                    f"point-{item.collection_append_order}",
                    item.collection_append_order,
                    self.raw_source_time_by_collection_order[
                        item.collection_append_order
                    ],
                    observation.open_session_index,
                )
            )
        for index, source_time in enumerate(eligible_unprojected_source_times, 1):
            session_index = next(
                index
                for index, session in sessions.items()
                if session.scheduled_open_at <= source_time <= session.scheduled_close_at
            )
            raw_specs.append((f"eligible-{index}", -index, source_time, session_index))
        raw_specs.sort(key=lambda item: (item[2], item[0]))
        source_records: list[MarketEventSourceRecord] = []
        previous_global = "0" * 64
        previous_by_partition: dict[str, str] = {}
        callback_by_session: dict[int, int] = {}
        record_by_collection_order: dict[int, MarketEventSourceRecord] = {}
        for append_order, (label, collection_order, source_time, session_index) in enumerate(
            raw_specs,
            1,
        ):
            manifest = manifest_by_index[session_index]
            callback_by_session[session_index] = callback_by_session.get(session_index, 0) + 1
            trading_day = market_session_date(
                source_time,
                Market.A,
                MARKET_SESSION_LABEL_POLICY_V1,
            )
            partition_key = (
                f"market=A/trading_day={trading_day.isoformat()}/symbol=600519.SH"
            )
            record = MarketEventSourceRecord.create(
                source_store_id=self.source_store_id,
                append_order=append_order,
                event_id=_hash(f"event:{label}"),
                session_id=manifest.session_id,
                source_session_manifest_id=manifest.manifest_id,
                connection_epoch=manifest.connection_epoch,
                reconnect_epoch=manifest.reconnect_epoch,
                source="test-market-event-store",
                feed_mode="LEVEL2",
                symbol="600519.SH",
                market=Market.A,
                event_type="TRADE_TICK",
                trading_day=trading_day,
                session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
                source_time=source_time,
                received_at=source_time + timedelta(microseconds=1),
                durable_known_at=source_time + timedelta(microseconds=2),
                callback_seq=callback_by_session[session_index],
                provider_seq=None,
                partition_key=partition_key,
                previous_global_record_hash=previous_global,
                previous_partition_record_hash=previous_by_partition.get(
                    partition_key,
                    "0" * 64,
                ),
                raw_payload_sha256=_hash(f"raw:{label}"),
                payload={"last_price": "10", "quantity": 100},
                parser_id="test-market-event-parser-v1",
                source_schema_id="test-market-event-record-v1",
                record_file_sha256=_hash(f"file:{label}"),
            )
            source_records.append(record)
            previous_global = record.record_content_hash
            previous_by_partition[partition_key] = record.record_content_hash
            if collection_order > 0:
                record_by_collection_order[collection_order] = record
        source_record_tuple = tuple(source_records)
        grouped: dict[str, list[MarketEventSourceRecord]] = {}
        for record in source_record_tuple:
            grouped.setdefault(record.partition_key, []).append(record)
        partition_heads = tuple(
            MarketEventPartitionHead(
                partition_key=partition_key,
                event_count=len(items),
                first_record_hash=items[0].record_content_hash,
                last_record_hash=items[-1].record_content_hash,
                manifest_sha256=_hash(f"manifest:{partition_key}"),
            )
            for partition_key, items in sorted(grouped.items())
        )
        inventory = MarketEventInventoryVerification.create_from_prefix(
            source_store_id=self.source_store_id,
            catalog_schema_fingerprint=_hash("test-market-catalog-schema-v4"),
            records=source_record_tuple,
        )
        coverage_ends = tuple(
            session.coverage_through
            for session in sessions.values()
            if session.coverage_through is not None
        )
        source_audited_at = max(
            *(item.durable_known_at for item in source_record_tuple),
            *coverage_ends,
            *(session.scheduled_open_at for session in sessions.values()),
        ) + timedelta(microseconds=1)
        source_audit = MarketEventStoreAudit.create_from_prefix(
            source_store_id=self.source_store_id,
            source_schema_id="stock-tracker-market-event-store-v4",
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V1,
            audited_at=source_audited_at,
            catalog_schema_fingerprint=_hash("test-market-catalog-schema-v4"),
            inventory_verification=inventory,
            records=source_record_tuple,
            partition_heads=partition_heads,
            findings=(),
            source_session_manifests=manifests,
        )
        source_snapshot = MarketEventStoreSnapshot.from_audit(source_audit)
        selection_start = min(
            session.scheduled_open_at for session in sessions.values()
        )
        selection_end = max(
            *coverage_ends,
            *(item.source_time for item in source_record_tuple),
            *(session.scheduled_open_at for session in sessions.values()),
        ) + timedelta(microseconds=1)
        source_selection = source_snapshot.select_from_prefix(
            records=source_record_tuple,
            partition_heads=partition_heads,
            findings=(),
            source_session_manifests=manifests,
            symbol="600519.SH",
            market=Market.A,
            start_source_time=selection_start,
            end_source_time=selection_end,
            allowed_event_types=("TRADE_TICK",),
            interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
            record_limit=max(1, len(source_record_tuple)),
        )
        source_verification = source_snapshot.verify_selection(
            source_selection,
            records=source_record_tuple,
            partition_heads=partition_heads,
            findings=(),
            source_session_manifests=manifests,
        )
        market_source_binding = RuntimeMarketSourceSnapshotBinding.from_verified_selection(
            selection=source_selection,
            verification=source_verification,
        )

        rebound_facts: list[RuntimeFrozenPathFact] = []
        for item in ordered:
            observation = item.path_observation
            if observation is None:
                rebound_facts.append(item)
                continue
            record = record_by_collection_order[item.collection_append_order]
            source_reference = RuntimeSourceReference.from_verified_selection(
                record=record,
                selection=source_selection,
                verification=source_verification,
            )
            if observation.granularity is RuntimePathGranularity.TICK:
                rebound_observation = replace(
                    observation,
                    source_reference=source_reference,
                    projection_artifact_reference=None,
                )
            else:
                session = sessions[observation.open_session_index]
                assert session.coverage_reference is not None
                lineage = RuntimeProjectionLineage(
                    projection_policy_id=_hash("projection-policy-v1"),
                    source_store_id=self.source_store_id,
                    source_snapshot_id=source_selection.snapshot_id,
                    source_audit_id=source_selection.snapshot_audit_id,
                    source_high_water_append_order=(
                        source_selection.snapshot_high_water_append_order
                    ),
                    finding_set_digest=source_selection.snapshot_finding_set_digest,
                    selection_id=source_selection.selection_id,
                    selection_verification_id=source_verification.verification_id,
                    interval_start=observation.interval_start,
                    interval_end=observation.interval_end,
                    interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                    coverage_fact_id=session.coverage_reference.fact_id,
                    input_event_ids=(record.event_id,),
                    input_source_append_orders=(record.append_order,),
                    input_source_record_ids=(record.source_record_id,),
                    input_source_record_hashes=(record.record_content_hash,),
                )
                projection = RuntimeProjectionArtifactReference.create(
                    projection_policy_id=lineage.projection_policy_id,
                    created_at=max(
                        record.durable_known_at,
                        observation.interval_end,
                    ),
                    durable_known_at=max(
                        record.durable_known_at,
                        observation.interval_end,
                    ),
                    symbol=observation.symbol,
                    market=observation.market,
                    trading_day=record.trading_day,
                    interval_start=observation.interval_start,
                    interval_end=observation.interval_end,
                    interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                    lineage=lineage,
                    high=observation.high,
                    low=observation.low,
                    close=observation.close,
                    coverage_fact_id=lineage.coverage_fact_id,
                )
                rebound_observation = replace(
                    observation,
                    source_reference=None,
                    projection_artifact_reference=projection,
                )
            rebound_facts.append(replace(item, path_observation=rebound_observation))
        ordered = tuple(rebound_facts)
        authority_references = tuple(
            reference
            for item in ordered
            if item.session_evidence is not None
            for reference in (
                item.session_evidence.calendar_reference,
                item.session_evidence.security_status_reference,
                item.session_evidence.coverage_reference,
            )
            if reference is not None
        )
        authority_selections = tuple(
            RuntimeAuthorityFactSelection.from_verified_snapshot(
                snapshot=RuntimeAuthoritySnapshotBinding.from_facts(
                    authority_kind=kind,
                    authority_store_id=next(
                        item.authority_store_id
                        for item in authority_references
                        if item.authority_kind is kind
                    ),
                    authority_audited_at=max(
                        item.usable_from
                        for item in authority_references
                        if item.authority_kind is kind
                    )
                    + timedelta(microseconds=1),
                    facts=tuple(
                        sorted(
                            (
                                item
                                for item in authority_references
                                if item.authority_kind is kind
                            ),
                            key=lambda item: (item.revision, item.fact_id),
                        )
                    ),
                ),
                facts=tuple(
                    sorted(
                        (
                            item
                            for item in authority_references
                            if item.authority_kind is kind
                        ),
                        key=lambda item: (item.revision, item.fact_id),
                    )
                ),
                selection_policy_id=_hash(f"authority-selection:{kind.value}"),
            )
            for kind in sorted(
                {item.authority_kind for item in authority_references},
                key=lambda item: item.value,
            )
        )
        path_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=self.collection_store_id,
            path_audited_at=last_observed + timedelta(microseconds=1),
            facts=ordered,
            finding_blocker_digest=_hash("path-findings-and-blockers"),
        )
        case_selection = RuntimeCaseFactSelection.from_verified_snapshot(
            path_store_snapshot=path_snapshot,
            all_facts=ordered,
            case_id=self.case_id,
            symbol="600519.SH",
            market=Market.A,
            selection_policy_id=_hash("case-selection-policy-v1"),
        )
        return RuntimeFrozenPathPrefix.from_verified_case_selection(
            case_selection=case_selection,
            market_source_snapshot=market_source_binding,
            authority_fact_selections=authority_selections,
            frozen_at=actual_frozen_at,
        )

    def window(self, *, horizon_sessions: int = 2) -> RuntimePathWindow:
        return RuntimePathWindow(
            case_id=self.case_id,
            symbol="600519.SH",
            market=Market.A,
            entry_filled_at=self.entry_time,
            entry_trading_day=date(2026, 9, 1),
            entry_session_index=0,
            entry_price=Decimal(10),
            target_price=Decimal(12),
            stop_price=Decimal(8),
            horizon_sessions=horizon_sessions,
            terminal_policy_id=_hash("terminal-policy"),
            session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
            interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
        )

    def refreeze_case(
        self,
        prefix: RuntimeFrozenPathPrefix,
        facts: tuple[RuntimeFrozenPathFact, ...],
        *,
        frozen_at: datetime | None = None,
    ) -> RuntimeFrozenPathPrefix:
        path_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=prefix.path_store_snapshot.path_store_id,
            path_audited_at=prefix.path_store_snapshot.path_audited_at,
            facts=facts,
            finding_blocker_digest=(
                prefix.path_store_snapshot.finding_blocker_digest
            ),
        )
        case_selection = RuntimeCaseFactSelection.from_verified_snapshot(
            path_store_snapshot=path_snapshot,
            all_facts=facts,
            case_id=prefix.case_id,
            symbol=prefix.symbol,
            market=prefix.market,
            selection_policy_id=prefix.case_selection.selection_policy_id,
        )
        return RuntimeFrozenPathPrefix.from_verified_case_selection(
            case_selection=case_selection,
            market_source_snapshot=prefix.market_source_snapshot,
            authority_fact_selections=prefix.authority_fact_selections,
            frozen_at=prefix.frozen_at if frozen_at is None else frozen_at,
        )

    def three_complete_sessions(self) -> list[RuntimeFrozenPathFact]:
        return [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 2),
                collection_order=2,
                source_order=2,
                open_session_index=1,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 3),
                collection_order=3,
                source_order=3,
                open_session_index=2,
            ),
        ]


class TestRuntimeFirstTouch(RuntimePathContractTestCase):
    def test_tick_at_exact_entry_boundary_is_blocked(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time,
                interval_end=self.entry_time,
                high="12.1",
                low="12.1",
                close="12.1",
                granularity=RuntimePathGranularity.TICK,
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,),
        )

    def test_stop_tick_at_exact_entry_boundary_is_blocked(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time,
                interval_end=self.entry_time,
                high="7.9",
                low="7.9",
                close="7.9",
                granularity=RuntimePathGranularity.TICK,
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,),
        )

    def test_tick_strictly_after_entry_can_resolve_target(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time + timedelta(microseconds=1),
                interval_end=self.entry_time + timedelta(microseconds=1),
                high="12.1",
                low="12.1",
                close="12.1",
                granularity=RuntimePathGranularity.TICK,
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.TARGET)

    def test_bar_starting_at_entry_is_blocked_for_each_bar_granularity(self) -> None:
        for granularity, interval_end in (
            (RuntimePathGranularity.MINUTE_BAR, self.entry_time + timedelta(minutes=1)),
            (RuntimePathGranularity.DAILY_BAR, self.entry_time + timedelta(hours=5)),
        ):
            with self.subTest(granularity=granularity.value):
                facts = self.three_complete_sessions()
                facts.append(
                    self.point_fact(
                        collection_order=4,
                        source_order=4,
                        session_index=0,
                        interval_start=self.entry_time,
                        interval_end=interval_end,
                        granularity=granularity,
                    )
                )
                result = resolve_runtime_path(self.window(), self.prefix(facts))
                self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
                self.assertEqual(
                    result.blocker_codes,
                    (RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,),
                )

    def test_adjacent_closed_boundary_source_input_is_rejected(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        interval_start = self.entry_time + timedelta(minutes=1)
        interval_end = interval_start + timedelta(minutes=1)
        bar = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=interval_start,
            interval_end=interval_end,
            raw_source_time=interval_end,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "verified raw Selection"):
            self.prefix([session, bar])

    def test_window_has_no_caller_ordering_authority_boolean(self) -> None:
        self.assertNotIn(
            "ordering_authority",
            RuntimePathWindow.__dataclass_fields__,
        )
    def test_first_target_precedes_later_stop(self) -> None:
        facts = self.three_complete_sessions()
        target = self.point_fact(
            collection_order=4,
            source_order=4,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
            high="12.1",
            low="9.8",
            close="12",
        )
        facts.extend(
            [
                target,
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=1,
                    interval_start=self.entry_time + timedelta(days=1),
                    high="10",
                    low="7.9",
                    close="8",
                ),
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.TARGET)
        self.assertEqual(
            result.first_touch_collection_fact_id,
            target.collection_fact_id,
        )

    def test_first_stop_precedes_later_target(self) -> None:
        facts = self.three_complete_sessions()
        stop = self.point_fact(
            collection_order=4,
            source_order=4,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
            high="10.2",
            low="7.9",
            close="8",
        )
        facts.extend(
            [
                stop,
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=1,
                    interval_start=self.entry_time + timedelta(days=1),
                    high="12.1",
                    low="9.8",
                    close="12",
                ),
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.STOP)
        self.assertEqual(result.first_touch_collection_fact_id, stop.collection_fact_id)

    def test_same_bar_target_and_stop_is_ambiguous(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time + timedelta(minutes=1),
                high="12.1",
                low="7.9",
                close="10",
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.INTRABAR_AMBIGUITY,),
        )

    def test_tick_sequence_with_equal_time_uses_source_order(self) -> None:
        facts = self.three_complete_sessions()
        timestamp = self.entry_time + timedelta(minutes=1)
        target = self.point_fact(
            collection_order=4,
            source_order=4,
            session_index=0,
            interval_start=timestamp,
            interval_end=timestamp,
            high="12",
            low="12",
            close="12",
            granularity=RuntimePathGranularity.TICK,
        )
        facts.extend(
            [
                target,
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=0,
                    interval_start=timestamp,
                    interval_end=timestamp,
                    high="8",
                    low="8",
                    close="8",
                    granularity=RuntimePathGranularity.TICK,
                ),
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.TARGET)
        self.assertEqual(result.first_touch_collection_fact_id, target.collection_fact_id)

    def test_timeout_requires_complete_horizon_without_barrier(self) -> None:
        facts = self.three_complete_sessions()
        facts.extend(
            [
                self.point_fact(
                    collection_order=4 + index,
                    source_order=4 + index,
                    session_index=index,
                    interval_start=self.entry_time + timedelta(days=index, minutes=1),
                )
                for index in range(3)
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.TIMEOUT)
        self.assertEqual(result.effective_session_index, 2)

    def test_complete_horizon_prefix_remains_open(self) -> None:
        facts = self.three_complete_sessions()[:2]
        facts.append(
            self.session_fact(
                trading_day=date(2026, 9, 3),
                collection_order=3,
                source_order=3,
                open_session_index=2,
                coverage_state=RuntimeCoverageState.COMPLETE_PREFIX,
                session_complete=False,
            )
        )
        facts.extend(
            [
                self.point_fact(
                    collection_order=4 + index,
                    source_order=4 + index,
                    session_index=index,
                    interval_start=self.entry_time + timedelta(days=index, minutes=1),
                )
                for index in range(3)
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.OPEN)
        self.assertEqual(result.pending_code.value, "HORIZON_NOT_REACHED")
        self.assertEqual(result.blocker_codes, ())

    def test_missing_data_blocks_timeout(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 2),
                collection_order=2,
                source_order=2,
                open_session_index=1,
                open_session_state=RuntimeOpenSessionState.MISSING_DATA,
                coverage_state=RuntimeCoverageState.INCOMPLETE_GAP,
            ),
        ]
        facts.append(
            self.point_fact(
                collection_order=3,
                source_order=3,
                session_index=0,
                interval_start=self.entry_time + timedelta(minutes=1),
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(result.blocker_codes, (RuntimePathBlockerCode.MISSING_DATA,))

    def test_market_closed_day_does_not_increment_horizon(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 2),
                collection_order=2,
                source_order=2,
                open_session_index=None,
                calendar_state=RuntimeCalendarState.MARKET_CLOSED,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 3),
                collection_order=3,
                source_order=3,
                open_session_index=1,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 4),
                collection_order=4,
                source_order=4,
                open_session_index=2,
            ),
        ]
        facts.extend(
            [
                self.point_fact(
                    collection_order=5 + index,
                    source_order=5 + index,
                    session_index=index,
                    interval_start=self.entry_time
                    + timedelta(days=day_offset, minutes=1),
                )
                for index, day_offset in enumerate((0, 2, 3))
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.TIMEOUT)

    def test_calendar_gap_fails_closed(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 3),
                collection_order=2,
                source_order=2,
                open_session_index=1,
            ),
        ]
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.CALENDAR_COVERAGE_GAP,),
        )

    def test_entry_boundary_coarse_bar_fails_closed(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time - timedelta(minutes=1),
                interval_end=self.entry_time + timedelta(minutes=1),
                high="12.1",
            )
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.ENTRY_BOUNDARY_ORDER_AMBIGUITY,),
        )

    def test_nontraded_session_cannot_carry_price_point(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.session_fact(
                trading_day=date(2026, 9, 2),
                collection_order=2,
                source_order=2,
                open_session_index=1,
                open_session_state=RuntimeOpenSessionState.SUSPENDED,
            ),
            self.point_fact(
                collection_order=3,
                source_order=3,
                session_index=0,
                interval_start=self.entry_time + timedelta(minutes=1),
            ),
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=1,
                interval_start=self.entry_time + timedelta(days=1, minutes=1),
            ),
        ]
        result = resolve_runtime_path(self.window(horizon_sessions=1), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.NONTRADED_SESSION_WITH_POINTS,),
        )

    def test_entry_time_outside_session_window_fails_closed(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        invalid_window = RuntimePathWindow(
            case_id=self.case_id,
            symbol="600519.SH",
            market=Market.A,
            entry_filled_at=datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc),
            entry_trading_day=date(2026, 9, 1),
            entry_session_index=0,
            entry_price=Decimal(10),
            target_price=Decimal(12),
            stop_price=Decimal(8),
            horizon_sessions=2,
            terminal_policy_id=_hash("terminal-policy"),
            session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
            interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
        )
        result = resolve_runtime_path(invalid_window, self.prefix([session]))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.ENTRY_TIME_OUTSIDE_SESSION_WINDOW,),
        )

    def test_point_outside_session_window_fails_closed(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            ),
            self.point_fact(
                collection_order=2,
                source_order=2,
                session_index=0,
                interval_start=datetime(2026, 9, 1, 7, 1, tzinfo=timezone.utc),
            ),
        ]
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.POINT_OUTSIDE_SESSION_WINDOW,),
        )

    def test_point_beyond_claimed_coverage_fails_closed(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
                coverage_state=RuntimeCoverageState.COMPLETE_PREFIX,
                session_complete=False,
                coverage_through=self.entry_time + timedelta(minutes=2),
            ),
            self.point_fact(
                collection_order=2,
                source_order=2,
                session_index=0,
                interval_start=self.entry_time + timedelta(minutes=5),
            ),
        ]
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.POINT_BEYOND_COVERAGE,),
        )

    def test_overlapping_bars_fail_closed(self) -> None:
        facts = self.three_complete_sessions()
        facts.extend(
            [
                self.point_fact(
                    collection_order=4,
                    source_order=4,
                    session_index=0,
                    interval_start=self.entry_time + timedelta(minutes=1),
                    interval_end=self.entry_time + timedelta(minutes=3),
                ),
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=0,
                    interval_start=self.entry_time + timedelta(minutes=2),
                    interval_end=self.entry_time + timedelta(minutes=4),
                ),
            ]
        )
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.OVERLAPPING_PATH_OBSERVATIONS,),
        )


    def test_entry_fill_cannot_exist_on_nontraded_session(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
                open_session_state=RuntimeOpenSessionState.SUSPENDED,
            )
        ]
        result = resolve_runtime_path(self.window(), self.prefix(facts))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.ENTRY_SESSION_NOT_TRADED,),
        )

    def test_traded_complete_session_without_source_records_remains_open(self) -> None:
        facts = [
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
            )
        ]
        result = resolve_runtime_path(
            self.window(horizon_sessions=1),
            self.prefix(facts),
        )
        self.assertEqual(result.state, RuntimePathResolutionState.OPEN)
        self.assertEqual(result.pending_code.value, "HORIZON_NOT_REACHED")

    def test_entry_complete_prefix_at_fill_without_post_entry_record_is_open(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
            coverage_state=RuntimeCoverageState.COMPLETE_PREFIX,
            session_complete=False,
            coverage_through=self.entry_time,
        )
        result = resolve_runtime_path(self.window(), self.prefix([session]))
        self.assertEqual(result.state, RuntimePathResolutionState.OPEN)
        self.assertEqual(result.pending_code.value, "HORIZON_NOT_REACHED")

    def test_complete_horizon_with_zero_events_times_out(self) -> None:
        result = resolve_runtime_path(
            self.window(),
            self.prefix(self.three_complete_sessions()),
        )
        self.assertEqual(result.state, RuntimePathResolutionState.TIMEOUT)
        self.assertEqual(result.effective_session_index, 2)

    def test_eligible_source_record_without_projection_is_blocked(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        prefix = self.prefix(
            [session],
            eligible_unprojected_source_times=(
                self.entry_time + timedelta(minutes=1),
            ),
        )
        result = resolve_runtime_path(self.window(horizon_sessions=1), prefix)
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.TRADED_SESSION_WITHOUT_POINTS,),
        )

    def test_post_horizon_ambiguous_point_is_ignored(self) -> None:
        facts = self.three_complete_sessions()
        facts.extend(
            [
                self.point_fact(
                    collection_order=4,
                    source_order=4,
                    session_index=0,
                    interval_start=self.entry_time + timedelta(minutes=1),
                ),
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=1,
                    interval_start=self.entry_time + timedelta(days=1, minutes=1),
                ),
                self.point_fact(
                    collection_order=6,
                    source_order=6,
                    session_index=2,
                    interval_start=self.entry_time + timedelta(days=2, minutes=1),
                    high="12.1",
                    low="7.9",
                ),
            ]
        )
        result = resolve_runtime_path(
            self.window(horizon_sessions=1),
            self.prefix(facts),
        )
        self.assertEqual(result.state, RuntimePathResolutionState.TIMEOUT)
        self.assertEqual(result.effective_session_index, 1)

    def test_late_observed_early_timestamp_requires_new_prefix(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        neutral = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=10),
        )
        original_prefix = self.prefix([session, neutral])
        original = resolve_runtime_path(self.window(), original_prefix)
        self.assertEqual(original.state, RuntimePathResolutionState.OPEN)

        late_target = self.point_fact(
            collection_order=3,
            source_order=3,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=5),
            high="12.1",
            low="9.8",
            close="12",
            known_at=original_prefix.frozen_at + timedelta(hours=1),
        )
        with self.assertRaises(RuntimePathContractError):
            self.prefix(
                [session, neutral, late_target],
                frozen_at=original_prefix.frozen_at,
            )
        revised = resolve_runtime_path(
            self.window(),
            self.prefix([session, neutral, late_target]),
        )
        self.assertEqual(revised.state, RuntimePathResolutionState.TARGET)
        self.assertEqual(
            revised.first_touch_collection_fact_id,
            late_target.collection_fact_id,
        )
        self.assertEqual(
            resolve_runtime_path(self.window(), original_prefix).resolution_id,
            original.resolution_id,
        )


class TestRuntimePrefixAndSessionContracts(RuntimePathContractTestCase):
    def test_source_member_identity_attacks_fail_before_resolver(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        tick = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
            interval_end=self.entry_time + timedelta(minutes=1),
            high="12",
            low="12",
            close="12",
            granularity=RuntimePathGranularity.TICK,
        )
        prefix = self.prefix([session, tick])
        point = prefix.facts[1]
        assert point.path_observation is not None
        source = point.path_observation.source_reference
        assert source is not None

        for label, changes in (
            ("record-id", {"source_record_id": _hash("arbitrary-record")}),
            ("snapshot", {"source_snapshot_id": _hash("wrong-snapshot")}),
            ("audit", {"source_audit_id": _hash("wrong-audit")}),
            ("findings", {"finding_set_digest": _hash("wrong-findings")}),
        ):
            with self.subTest(attack=label):
                attacked_observation = replace(
                    point.path_observation,
                    source_reference=replace(source, **changes),
                )
                attacked_fact = replace(
                    point,
                    path_observation=attacked_observation,
                )
                with self.assertRaisesRegex(
                    RuntimePathContractError,
                    "verified Selection",
                ):
                    self.refreeze_case(
                        prefix,
                        (prefix.facts[0], attacked_fact),
                    )

        with self.assertRaisesRegex(RuntimePathContractError, "trade-event member"):
            replace(
                point.path_observation,
                source_reference=replace(source, event_type="ORDER_BOOK"),
            )
        us_trading_day = market_session_date(
            source.source_time,
            Market.US,
            MARKET_SESSION_LABEL_POLICY_V1,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "trade-event member"):
            replace(
                point.path_observation,
                source_reference=replace(
                    source,
                    symbol="AAPL.US",
                    market=Market.US,
                    trading_day=us_trading_day,
                ),
            )

    def test_case_selection_rejects_omission_and_forged_inventory(self) -> None:
        facts = self.three_complete_sessions()
        facts.extend(
            (
                self.point_fact(
                    collection_order=4,
                    source_order=4,
                    session_index=0,
                    interval_start=self.entry_time + timedelta(minutes=1),
                    high="12.1",
                    low="10",
                    close="12",
                ),
                self.point_fact(
                    collection_order=5,
                    source_order=5,
                    session_index=1,
                    interval_start=self.entry_time + timedelta(days=1, minutes=1),
                    high="10",
                    low="7.9",
                    close="8",
                ),
            )
        )
        prefix = self.prefix(facts)
        for label, omitted_index in (("session", 0), ("target", 3), ("stop", 4)):
            with self.subTest(omission=label), self.assertRaisesRegex(
                RuntimePathContractError,
                "prefix",
            ):
                RuntimeCaseFactSelection.from_verified_snapshot(
                    path_store_snapshot=prefix.path_store_snapshot,
                    all_facts=tuple(
                        item
                        for index, item in enumerate(prefix.facts)
                        if index != omitted_index
                    ),
                    case_id=self.case_id,
                    symbol="600519.SH",
                    market=Market.A,
                    selection_policy_id=prefix.case_selection.selection_policy_id,
                )

        raised_high_water = copy.copy(prefix.path_store_snapshot)
        object.__setattr__(
            raised_high_water,
            "global_high_water_append_order",
            raised_high_water.global_high_water_append_order + 1,
        )
        forged_high_water_selection = copy.copy(prefix.case_selection)
        object.__setattr__(
            forged_high_water_selection,
            "path_store_snapshot",
            raised_high_water,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=forged_high_water_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=prefix.authority_fact_selections,
                frozen_at=prefix.frozen_at,
            )

        other_case_fact = replace(
            prefix.facts[-1],
            collection_append_order=len(prefix.facts) + 1,
            case_id=_hash("other-case"),
            collection_fact_id=_hash("other-case-fact"),
        )
        all_facts = (*prefix.facts, other_case_fact)
        global_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=self.collection_store_id,
            path_audited_at=prefix.path_store_snapshot.path_audited_at,
            facts=all_facts,
            finding_blocker_digest=prefix.path_store_snapshot.finding_blocker_digest,
        )
        exact_case = RuntimeCaseFactSelection.from_verified_snapshot(
            path_store_snapshot=global_snapshot,
            all_facts=all_facts,
            case_id=self.case_id,
            symbol="600519.SH",
            market=Market.A,
            selection_policy_id=prefix.case_selection.selection_policy_id,
        )
        injected = copy.copy(exact_case)
        object.__setattr__(injected, "facts", (*exact_case.facts, other_case_fact))
        with self.assertRaisesRegex(RuntimePathContractError, "verified case query"):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=injected,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=prefix.authority_fact_selections,
                frozen_at=prefix.frozen_at,
            )
        reordered = copy.copy(prefix.case_selection)
        object.__setattr__(reordered, "facts", tuple(reversed(reordered.facts)))
        with self.assertRaisesRegex(RuntimePathContractError, "verified case query"):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=reordered,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=prefix.authority_fact_selections,
                frozen_at=prefix.frozen_at,
            )
        arbitrary_audit = copy.copy(prefix.path_store_snapshot)
        object.__setattr__(arbitrary_audit, "path_audit_id", _hash("caller-audit"))
        arbitrary_audit_selection = copy.copy(prefix.case_selection)
        object.__setattr__(
            arbitrary_audit_selection,
            "path_store_snapshot",
            arbitrary_audit,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=arbitrary_audit_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=prefix.authority_fact_selections,
                frozen_at=prefix.frozen_at,
            )

    def test_complete_session_requires_source_sequence_start_proof(self) -> None:
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "start capability"):
            self.prefix([fact], source_start_proof=False)

    def test_prefix_rejects_fact_known_after_freeze(self) -> None:
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        with self.assertRaises(RuntimePathContractError):
            self.prefix(
                [fact],
                frozen_at=fact.collection_observed_at - timedelta(seconds=1),
            )

    def test_prefix_id_is_deterministic_and_binds_high_water(self) -> None:
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        first = self.prefix([fact])
        second = self.prefix([fact])
        self.assertEqual(first.prefix_id, second.prefix_id)
        point = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
        )
        prefix_with_point = self.prefix([fact, point])
        tampered_binding = copy.copy(prefix_with_point.market_source_snapshot)
        object.__setattr__(tampered_binding, "high_water_append_order", 0)
        with self.assertRaises(RuntimePathContractError):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=prefix_with_point.case_selection,
                market_source_snapshot=tampered_binding,
                authority_fact_selections=prefix_with_point.authority_fact_selections,
                frozen_at=prefix_with_point.frozen_at,
            )

    def test_market_closed_session_cannot_fabricate_open_state(self) -> None:
        calendar_reference = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 1),
            known_at=self.entry_time,
            label="calendar",
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeSessionEvidence(
                symbol="600519.SH",
                market=Market.A,
                trading_day=date(2026, 9, 1),
                calendar_state=RuntimeCalendarState.MARKET_CLOSED,
                open_session_index=0,
                open_session_state=RuntimeOpenSessionState.NO_TRADE,
                scheduled_open_at=None,
                scheduled_close_at=None,
                coverage_state=RuntimeCoverageState.NOT_APPLICABLE,
                session_complete=True,
                coverage_through=None,
                session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
                calendar_reference=calendar_reference,
                security_status_reference=None,
                coverage_reference=None,
            )

    def test_complete_session_must_cover_scheduled_close(self) -> None:
        with self.assertRaisesRegex(
            RuntimePathContractError,
            "scheduled session close",
        ):
            self.session_fact(
                trading_day=date(2026, 9, 1),
                collection_order=1,
                source_order=1,
                open_session_index=0,
                coverage_through=self.entry_time + timedelta(minutes=5),
            )

    def test_suspension_requires_authoritative_status(self) -> None:
        calendar_reference = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 1),
            known_at=self.entry_time,
            label="calendar",
        )
        coverage_reference = self.authority_reference(
            kind=RuntimeAuthorityKind.COVERAGE,
            trading_day=date(2026, 9, 1),
            known_at=self.entry_time,
            label="coverage",
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeSessionEvidence(
                symbol="600519.SH",
                market=Market.A,
                trading_day=date(2026, 9, 1),
                calendar_state=RuntimeCalendarState.OPEN,
                open_session_index=0,
                open_session_state=RuntimeOpenSessionState.SUSPENDED,
                scheduled_open_at=self.entry_time - timedelta(hours=1),
                scheduled_close_at=self.entry_time - timedelta(seconds=1),
                coverage_state=RuntimeCoverageState.COMPLETE_SESSION,
                session_complete=True,
                coverage_through=self.entry_time - timedelta(seconds=1),
                session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
                calendar_reference=calendar_reference,
                security_status_reference=None,
                coverage_reference=coverage_reference,
            )

    def test_source_reference_rejects_future_known_time(self) -> None:
        with self.assertRaises(RuntimePathContractError):
            self.source_reference(
                label="future",
                append_order=1,
                known_at=self.entry_time,
                source_time=self.entry_time + timedelta(seconds=1),
            )

    def test_projected_bar_accepts_last_tick_before_interval_end(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        bar = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=self.entry_time,
            interval_end=self.entry_time + timedelta(minutes=1),
            raw_source_time=self.entry_time + timedelta(seconds=57),
        )
        assert bar.path_observation is not None
        self.assertIsNone(bar.path_observation.source_reference)
        self.assertIsNotNone(bar.path_observation.projection_artifact_reference)
        self.prefix([session, bar])

    def test_tick_source_time_must_equal_point_time(self) -> None:
        tick = self.point_fact(
            collection_order=1,
            source_order=1,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=2),
            interval_end=self.entry_time + timedelta(minutes=2),
            high="10",
            low="10",
            close="10",
            granularity=RuntimePathGranularity.TICK,
        )
        assert tick.path_observation is not None
        assert tick.path_observation.source_reference is not None
        with self.assertRaisesRegex(RuntimePathContractError, "trade-event member"):
            replace(
                tick.path_observation,
                source_reference=replace(
                    tick.path_observation.source_reference,
                    source_time=tick.path_observation.interval_end - timedelta(seconds=1),
                ),
            )

    def test_bar_requires_exact_projection_lineage(self) -> None:
        session = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        point = self.point_fact(
            collection_order=2,
            source_order=2,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
        )
        prefix = self.prefix([session, point])
        rebound_point = prefix.facts[1]
        assert rebound_point.path_observation is not None
        projection = rebound_point.path_observation.projection_artifact_reference
        assert projection is not None
        rewritten_lineage = replace(
            projection.lineage,
            source_snapshot_id=_hash("later-source-snapshot"),
        )
        rewritten_projection = replace(projection, lineage=rewritten_lineage)
        rewritten_observation = replace(
            rebound_point.path_observation,
            projection_artifact_reference=rewritten_projection,
        )
        rewritten_fact = replace(
            rebound_point,
            path_observation=rewritten_observation,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "frozen market"):
            self.refreeze_case(
                prefix,
                (prefix.facts[0], rewritten_fact),
            )

    def test_authority_facts_are_pit_bound_to_exact_stores(self) -> None:
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        assert fact.session_evidence is not None
        prefix = self.prefix([fact])
        stores = {
            item.snapshot.authority_store_id
            for item in prefix.authority_fact_selections
        }
        self.assertEqual(len(stores), 2)
        original = prefix.authority_fact_selections[0]
        wrong_store_snapshot = copy.copy(original.snapshot)
        object.__setattr__(
            wrong_store_snapshot,
            "authority_store_id",
            _hash("wrong-authority-store"),
        )
        wrong_store_selection = copy.copy(original)
        object.__setattr__(wrong_store_selection, "snapshot", wrong_store_snapshot)
        with self.assertRaisesRegex(RuntimePathContractError, "snapshot"):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=prefix.case_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=(
                    wrong_store_selection,
                    *prefix.authority_fact_selections[1:],
                ),
                frozen_at=prefix.frozen_at,
            )
        wrong_audit_snapshot = copy.copy(original.snapshot)
        object.__setattr__(
            wrong_audit_snapshot,
            "authority_audit_id",
            _hash("wrong-authority-audit"),
        )
        wrong_audit_selection = copy.copy(original)
        object.__setattr__(wrong_audit_selection, "snapshot", wrong_audit_snapshot)
        with self.assertRaisesRegex(RuntimePathContractError, "snapshot"):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=prefix.case_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=(
                    wrong_audit_selection,
                    *prefix.authority_fact_selections[1:],
                ),
                frozen_at=prefix.frozen_at,
            )
        with self.assertRaisesRegex(RuntimePathContractError, "calendar_reference"):
            replace(
                fact.session_evidence,
                calendar_reference=_hash("caller-only-fact"),
            )

    def test_authority_member_and_future_audit_attacks_are_rejected(self) -> None:
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
            open_session_state=RuntimeOpenSessionState.SUSPENDED,
        )
        prefix = self.prefix([fact])
        rebound = prefix.facts[0]
        assert rebound.session_evidence is not None
        session = rebound.session_evidence
        references = (
            session.calendar_reference,
            session.security_status_reference,
        )
        for reference in references:
            assert reference is not None
            attacked_reference = replace(
                reference,
                fact_payload_sha256=_hash(
                    f"caller:{reference.authority_kind.value}"
                ),
            )
            attacked_session = replace(
                session,
                calendar_reference=(
                    attacked_reference
                    if reference.authority_kind is RuntimeAuthorityKind.CALENDAR
                    else session.calendar_reference
                ),
                security_status_reference=(
                    attacked_reference
                    if reference.authority_kind
                    is RuntimeAuthorityKind.SECURITY_STATUS
                    else session.security_status_reference
                ),
            )
            attacked_fact = replace(rebound, session_evidence=attacked_session)
            with self.subTest(kind=reference.authority_kind.value), self.assertRaisesRegex(
                RuntimePathContractError,
                "absent from its verified store selection",
            ):
                self.refreeze_case(prefix, (attacked_fact,))

        future_selection = copy.copy(prefix.authority_fact_selections[0])
        future_snapshot = copy.copy(future_selection.snapshot)
        object.__setattr__(
            future_snapshot,
            "authority_audited_at",
            prefix.frozen_at + timedelta(seconds=1),
        )
        object.__setattr__(future_selection, "snapshot", future_snapshot)
        with self.assertRaises(RuntimePathContractError):
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=prefix.case_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=(
                    future_selection,
                    *prefix.authority_fact_selections[1:],
                ),
                frozen_at=prefix.frozen_at,
            )
        self.assertEqual(
            len(
                {
                    item.snapshot.authority_store_id
                    for item in prefix.authority_fact_selections
                }
            ),
            3,
        )

    def test_future_calendar_and_security_authority_are_rejected(self) -> None:
        calendar_fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        assert calendar_fact.session_evidence is not None
        future = calendar_fact.collection_observed_at + timedelta(hours=1)
        future_calendar = replace(
            calendar_fact.session_evidence.calendar_reference,
            known_at=future,
            usable_from=future,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "predates authority"):
            replace(
                calendar_fact,
                session_evidence=replace(
                    calendar_fact.session_evidence,
                    calendar_reference=future_calendar,
                ),
            )
        security_fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=2,
            source_order=2,
            open_session_index=0,
            open_session_state=RuntimeOpenSessionState.SUSPENDED,
        )
        assert security_fact.session_evidence is not None
        assert security_fact.session_evidence.security_status_reference is not None
        future_security = replace(
            security_fact.session_evidence.security_status_reference,
            usable_from=security_fact.collection_observed_at + timedelta(hours=1),
        )
        with self.assertRaisesRegex(RuntimePathContractError, "predates authority"):
            replace(
                security_fact,
                session_evidence=replace(
                    security_fact.session_evidence,
                    security_status_reference=future_security,
                ),
            )

    def test_market_local_entry_and_session_dates_are_enforced(self) -> None:
        with self.assertRaisesRegex(RuntimePathContractError, "entry_trading_day"):
            replace(self.window(), entry_trading_day=date(2026, 8, 31))
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
        )
        assert fact.session_evidence is not None
        with self.assertRaisesRegex(RuntimePathContractError, "market-local"):
            replace(
                fact.session_evidence,
                scheduled_open_at=fact.session_evidence.scheduled_open_at
                + timedelta(days=1),
                scheduled_close_at=fact.session_evidence.scheduled_close_at
                + timedelta(days=1),
            )


class TestRuntimeExecutionAndNoEntry(RuntimePathContractTestCase):
    def execution_binding_kwargs(self) -> dict[str, object]:
        return {
            "execution_source_store_id": _hash("execution-store"),
            "execution_snapshot_id": _hash("execution-snapshot"),
            "execution_audit_id": _hash("execution-audit"),
            "execution_audited_at": self.entry_time + timedelta(seconds=10),
            "execution_high_water_append_order": 100,
            "execution_selection_id": _hash("execution-selection"),
            "membership_verification_id": _hash("execution-membership"),
        }

    def evidence_reference(
        self,
        fact_id: str,
        *,
        source_append_order: int,
        known_at: datetime,
        fact_kind: str,
    ) -> RuntimeExecutionEvidenceReference:
        return RuntimeExecutionEvidenceReference(
            fact_id=fact_id,
            fact_kind=fact_kind,
            source_append_order=source_append_order,
            known_at=known_at,
            membership_verification_id=_hash("execution-membership"),
        )

    def fragment(
        self,
        *,
        execution_id: str,
        quantity: int,
        price: str,
        cost: str,
        seconds: int,
        source_fact_label: str | None = None,
    ) -> RuntimeExecutionFragment:
        timestamp = self.entry_time + timedelta(seconds=seconds)
        return RuntimeExecutionFragment(
            intent_id=_hash("intent"),
            execution_id=execution_id,
            source_fact_id=_hash(
                source_fact_label or f"execution:{execution_id}"
            ),
            source_callback_id=_hash(f"callback:{execution_id}"),
            source_append_order=seconds,
            side=RuntimeExecutionSide.BUY,
            timestamp=timestamp,
            durable_known_at=timestamp + timedelta(seconds=1),
            quantity=quantity,
            price=Decimal(price),
            explicit_cost=Decimal(cost),
        )

    def zero_fill_summary(self) -> RuntimeExecutionSummary:
        return RuntimeExecutionSummary(
            intent_id=_hash("intent"),
            side=RuntimeExecutionSide.BUY,
            requested_quantity=100,
            fragments=(),
            **self.execution_binding_kwargs(),
            execution_stream_complete=True,
        )

    def test_partial_fill_aggregation_is_not_structurally_projectable_to_stage4g_v3(self) -> None:
        summary = RuntimeExecutionSummary(
            intent_id=_hash("intent"),
            side=RuntimeExecutionSide.BUY,
            requested_quantity=100,
            fragments=(
                self.fragment(
                    execution_id="exec-2",
                    quantity=20,
                    price="11",
                    cost="1",
                    seconds=2,
                ),
                self.fragment(
                    execution_id="exec-1",
                    quantity=30,
                    price="10",
                    cost="2",
                    seconds=1,
                ),
            ),
            execution_stream_complete=True,
            **self.execution_binding_kwargs(),
        )
        self.assertEqual(summary.completion, RuntimeFillCompletion.PARTIAL)
        self.assertEqual(summary.filled_quantity, 50)
        self.assertEqual(summary.quantity_weighted_price, Decimal("10.4"))
        self.assertEqual(summary.total_explicit_cost, Decimal(3))
        self.assertFalse(summary.structurally_projectable_to_stage4g_v3)

    def test_complete_fill_aggregation_is_structurally_projectable(self) -> None:
        summary = RuntimeExecutionSummary(
            intent_id=_hash("intent"),
            side=RuntimeExecutionSide.BUY,
            requested_quantity=50,
            fragments=(
                self.fragment(
                    execution_id="exec-1",
                    quantity=20,
                    price="10",
                    cost="1",
                    seconds=1,
                ),
                self.fragment(
                    execution_id="exec-2",
                    quantity=30,
                    price="11",
                    cost="2",
                    seconds=2,
                ),
            ),
            execution_stream_complete=True,
            **self.execution_binding_kwargs(),
        )
        self.assertEqual(summary.completion, RuntimeFillCompletion.COMPLETE)
        self.assertTrue(summary.structurally_projectable_to_stage4g_v3)

    def test_duplicate_execution_id_and_multi_leg_fail_closed(self) -> None:
        fragment = self.fragment(
            execution_id="duplicate",
            quantity=10,
            price="10",
            cost="1",
            seconds=1,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(fragment, fragment),
                execution_stream_complete=True,
                **self.execution_binding_kwargs(),
            )
        with self.assertRaises(RuntimePathContractError) as caught:
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(),
                execution_stream_complete=True,
                **self.execution_binding_kwargs(),
                native_multi_leg=True,
            )
        self.assertEqual(caught.exception.code, "NATIVE_MULTI_LEG_UNSUPPORTED")

    def test_complete_execution_stream_requires_audit_identity(self) -> None:
        with self.assertRaisesRegex(
            RuntimePathContractError,
            "execution_audit_id",
        ):
            bindings = self.execution_binding_kwargs()
            bindings["execution_audit_id"] = "caller-asserted"
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(),
                execution_stream_complete=True,
                **bindings,
            )

    def test_one_source_fact_cannot_produce_multiple_fragments(self) -> None:
        first = self.fragment(
            execution_id="exec-1",
            quantity=10,
            price="10",
            cost="1",
            seconds=1,
            source_fact_label="shared-source-fact",
        )
        second = self.fragment(
            execution_id="exec-2",
            quantity=10,
            price="10",
            cost="1",
            seconds=2,
            source_fact_label="shared-source-fact",
        )
        with self.assertRaisesRegex(RuntimePathContractError, "one execution source"):
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(first, second),
                execution_stream_complete=True,
                **self.execution_binding_kwargs(),
            )

    def test_duplicate_callback_cannot_hide_behind_another_source_fact(self) -> None:
        first = self.fragment(
            execution_id="exec-1",
            quantity=10,
            price="10",
            cost="1",
            seconds=1,
        )
        second = self.fragment(
            execution_id="exec-2",
            quantity=10,
            price="10",
            cost="1",
            seconds=2,
        )
        second = replace(second, source_callback_id=first.source_callback_id)
        with self.assertRaisesRegex(RuntimePathContractError, "one execution callback"):
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(first, second),
                execution_stream_complete=True,
                **self.execution_binding_kwargs(),
            )

    def test_arbitrary_audit_hash_does_not_claim_verified_execution(self) -> None:
        summary = self.zero_fill_summary()
        altered = replace(summary, execution_audit_id=_hash("caller-audit"))
        self.assertNotEqual(summary.summary_id, altered.summary_id)
        self.assertNotIn("verified", altered.as_dict())
        self.assertNotIn("finalizable", altered.as_dict())

    def test_late_reason_facts_cannot_explain_earlier_no_entry(self) -> None:
        for reason, fact_kind, keyword in (
            (RuntimeNoEntryReason.USER_CANCELLED, "USER_CANCELLATION", "cancellation_fact_id"),
            (RuntimeNoEntryReason.ORDER_REJECTED, "ORDER_REJECTION", "rejection_fact_id"),
        ):
            fact_id = _hash(f"late:{reason.value}")
            decided_at = self.entry_time + timedelta(minutes=5)
            references = (
                self.evidence_reference(
                    fact_id,
                    source_append_order=2,
                    known_at=decided_at + timedelta(seconds=1),
                    fact_kind=fact_kind,
                ),
            )
            reason_kwargs: dict[str, object] = {keyword: fact_id}
            if reason is RuntimeNoEntryReason.USER_CANCELLED:
                authentication = _hash("actor-authentication")
                references = (
                    self.evidence_reference(
                        authentication,
                        source_append_order=1,
                        known_at=self.entry_time + timedelta(minutes=1),
                        fact_kind="ACTOR_AUTHENTICATION",
                    ),
                    *references,
                )
                reason_kwargs.update(
                    actor_id="local-user",
                    actor_authentication_fact_id=authentication,
                )
            with self.subTest(reason=reason.value), self.assertRaisesRegex(
                RuntimePathContractError,
                "predates its execution evidence",
            ):
                RuntimeNoEntryEvidence(
                    reason=reason,
                    execution_summary=self.zero_fill_summary(),
                    entry_window_start=self.entry_time,
                    entry_window_end=self.entry_time + timedelta(hours=1),
                    decided_at=decided_at,
                    execution_policy_id=_hash("policy"),
                    evidence_references=references,
                    evidence_ids=tuple(item.fact_id for item in references),
                    entry_window_coverage_complete=False,
                    **reason_kwargs,
                )

    def test_entry_expired_requires_complete_zero_fill_window(self) -> None:
        coverage_fact = _hash("entry-window-coverage")
        evidence = RuntimeNoEntryEvidence(
            reason=RuntimeNoEntryReason.ENTRY_EXPIRED,
            execution_summary=self.zero_fill_summary(),
            entry_window_start=self.entry_time,
            entry_window_end=self.entry_time + timedelta(hours=1),
            decided_at=self.entry_time + timedelta(hours=1),
            execution_policy_id=_hash("policy"),
            evidence_references=(
                self.evidence_reference(
                    coverage_fact,
                    source_append_order=1,
                    known_at=self.entry_time + timedelta(minutes=1),
                    fact_kind="ENTRY_WINDOW_COVERAGE",
                ),
            ),
            evidence_ids=(coverage_fact,),
            entry_window_coverage_complete=True,
            entry_window_coverage_fact_id=coverage_fact,
        )
        self.assertEqual(evidence.reason, RuntimeNoEntryReason.ENTRY_EXPIRED)
        with self.assertRaises(RuntimePathContractError):
            RuntimeNoEntryEvidence(
                reason=RuntimeNoEntryReason.ENTRY_EXPIRED,
                execution_summary=self.zero_fill_summary(),
                entry_window_start=self.entry_time,
                entry_window_end=self.entry_time + timedelta(hours=1),
                decided_at=self.entry_time + timedelta(minutes=30),
                execution_policy_id=_hash("policy"),
                evidence_references=(),
                evidence_ids=(),
                entry_window_coverage_complete=False,
            )

    def test_user_cancelled_requires_actor_and_cancellation_fact(self) -> None:
        cancellation = _hash("cancel")
        authentication = _hash("actor-authentication")
        RuntimeNoEntryEvidence(
            reason=RuntimeNoEntryReason.USER_CANCELLED,
            execution_summary=self.zero_fill_summary(),
            entry_window_start=self.entry_time,
            entry_window_end=self.entry_time + timedelta(hours=1),
            decided_at=self.entry_time + timedelta(minutes=10),
            execution_policy_id=_hash("policy"),
            evidence_references=(
                self.evidence_reference(
                    authentication,
                    source_append_order=1,
                    known_at=self.entry_time + timedelta(minutes=1),
                    fact_kind="ACTOR_AUTHENTICATION",
                ),
                self.evidence_reference(
                    cancellation,
                    source_append_order=2,
                    known_at=self.entry_time + timedelta(minutes=5),
                    fact_kind="USER_CANCELLATION",
                ),
            ),
            evidence_ids=(authentication, cancellation),
            entry_window_coverage_complete=False,
            actor_id="local-user",
            actor_authentication_fact_id=authentication,
            cancellation_fact_id=cancellation,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeNoEntryEvidence(
                reason=RuntimeNoEntryReason.USER_CANCELLED,
                execution_summary=self.zero_fill_summary(),
                entry_window_start=self.entry_time,
                entry_window_end=self.entry_time + timedelta(hours=1),
                decided_at=self.entry_time + timedelta(minutes=10),
                execution_policy_id=_hash("policy"),
                evidence_references=(
                    self.evidence_reference(
                        cancellation,
                        source_append_order=1,
                        known_at=self.entry_time + timedelta(minutes=5),
                        fact_kind="USER_CANCELLATION",
                    ),
                ),
                evidence_ids=(cancellation,),
                entry_window_coverage_complete=False,
                cancellation_fact_id=cancellation,
            )

    def test_order_rejected_and_data_invalid_are_not_interchangeable(self) -> None:
        rejection = _hash("rejection")
        invalidity = _hash("invalidity")
        RuntimeNoEntryEvidence(
            reason=RuntimeNoEntryReason.ORDER_REJECTED,
            execution_summary=self.zero_fill_summary(),
            entry_window_start=self.entry_time,
            entry_window_end=self.entry_time + timedelta(hours=1),
            decided_at=self.entry_time + timedelta(minutes=1),
            execution_policy_id=_hash("policy"),
            evidence_references=(
                self.evidence_reference(
                    rejection,
                    source_append_order=1,
                    known_at=self.entry_time + timedelta(seconds=20),
                    fact_kind="ORDER_REJECTION",
                ),
            ),
            evidence_ids=(rejection,),
            entry_window_coverage_complete=False,
            rejection_fact_id=rejection,
        )
        RuntimeNoEntryEvidence(
            reason=RuntimeNoEntryReason.DATA_INVALID,
            execution_summary=self.zero_fill_summary(),
            entry_window_start=self.entry_time,
            entry_window_end=self.entry_time + timedelta(hours=1),
            decided_at=self.entry_time + timedelta(minutes=1),
            execution_policy_id=_hash("policy"),
            evidence_references=(
                self.evidence_reference(
                    invalidity,
                    source_append_order=1,
                    known_at=self.entry_time + timedelta(seconds=20),
                    fact_kind="DATA_INVALIDITY",
                ),
            ),
            evidence_ids=(invalidity,),
            entry_window_coverage_complete=False,
            data_invalidity_fact_id=invalidity,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeNoEntryEvidence(
                reason=RuntimeNoEntryReason.DATA_INVALID,
                execution_summary=self.zero_fill_summary(),
                entry_window_start=self.entry_time,
                entry_window_end=self.entry_time + timedelta(hours=1),
                decided_at=self.entry_time + timedelta(minutes=1),
                execution_policy_id=_hash("policy"),
                evidence_references=(
                    self.evidence_reference(
                        rejection,
                        source_append_order=1,
                        known_at=self.entry_time + timedelta(seconds=20),
                        fact_kind="ORDER_REJECTION",
                    ),
                ),
                evidence_ids=(rejection,),
                entry_window_coverage_complete=False,
                rejection_fact_id=rejection,
            )

    def test_no_entry_rejects_partial_or_unconfirmed_zero_fill(self) -> None:
        incomplete_zero = RuntimeExecutionSummary(
            intent_id=_hash("intent"),
            side=RuntimeExecutionSide.BUY,
            requested_quantity=100,
            fragments=(),
            **self.execution_binding_kwargs(),
            execution_stream_complete=False,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeNoEntryEvidence(
                reason=RuntimeNoEntryReason.ENTRY_EXPIRED,
                execution_summary=incomplete_zero,
                entry_window_start=self.entry_time,
                entry_window_end=self.entry_time + timedelta(hours=1),
                decided_at=self.entry_time + timedelta(hours=1),
                execution_policy_id=_hash("policy"),
                evidence_references=(),
                evidence_ids=(),
                entry_window_coverage_complete=True,
            )


if __name__ == "__main__":
    unittest.main()
