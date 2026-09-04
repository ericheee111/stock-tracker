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
    TRADE_PROJECTION_POLICY_V1,
    TRADING_SEGMENT_POLICY_V1,
    CalendarSessionFact,
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
    RuntimeSecurityStatus,
    RuntimeSessionEvidence,
    RuntimeSourceReference,
    SecurityStatusFact,
    SourceCoverageFact,
    TradingSessionSegment,
    TradingSessionSegmentKind,
    resolve_runtime_path,
)
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    FIXTURE_TRADE_DECODER_POLICY_V1,
    FIXTURE_TRADE_TICK_SCHEMA_V1,
    MARKET_EVENT_SEQUENCE_POLICY_V3,
    CoverageOrigin,
    DecodedTradeTick,
    MarketEventCallbackSequenceScope,
    MarketEventInventoryVerification,
    MarketEventPartitionHead,
    MarketEventProviderSequenceScope,
    MarketEventSourceRecord,
    MarketEventSourceSessionManifest,
    MarketEventStoreAudit,
    MarketEventStoreSnapshot,
    MarketEventSubscriptionManifest,
)


def _tamper(value, **changes):
    result = copy.copy(value)
    for name, changed in changes.items():
        object.__setattr__(result, name, changed)
    return result


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class RuntimePathContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.entry_time = datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc)
        self.calendars = {}
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
        source_time = (
            known_at - timedelta(seconds=2) if source_time is None else source_time
        )
        return RuntimeSourceReference(
            source_store_id=self.source_store_id,
            source_snapshot_id=self.source_snapshot_id,
            source_audit_id=self.source_audit_id,
            source_high_water_append_order=self.source_high_water,
            finding_set_digest=self.source_finding_set_digest,
            sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
            selection_id=_hash("placeholder-selection"),
            selection_verification_id=_hash("placeholder-selection-verification"),
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

    def calendar(self, trading_day, index=0, *, closed=False):
        day = datetime.combine(trading_day, datetime.min.time(), tzinfo=timezone.utc)
        segments = (
            ()
            if closed
            else (
                TradingSessionSegment(
                    TradingSessionSegmentKind.CONTINUOUS,
                    day + timedelta(hours=1),
                    day + timedelta(hours=3, minutes=30),
                    True,
                    True,
                    TRADING_SEGMENT_POLICY_V1,
                ),
                TradingSessionSegment(
                    TradingSessionSegmentKind.BREAK,
                    day + timedelta(hours=3, minutes=30),
                    day + timedelta(hours=5),
                    False,
                    False,
                    TRADING_SEGMENT_POLICY_V1,
                ),
                TradingSessionSegment(
                    TradingSessionSegmentKind.CONTINUOUS,
                    day + timedelta(hours=5),
                    day + timedelta(hours=7),
                    True,
                    True,
                    TRADING_SEGMENT_POLICY_V1,
                ),
            )
        )
        return CalendarSessionFact(
            "600519.SH",
            Market.A,
            trading_day,
            RuntimeCalendarState.MARKET_CLOSED if closed else RuntimeCalendarState.OPEN,
            None if closed else index,
            segments,
            _hash("fixture-calendar-policy"),
        )

    def market_fixture(
        self,
        calendars,
        specs=(),
        *,
        through_by_day=None,
        loss_by_day=None,
        source_start_proof=True,
    ):
        through_by_day = through_by_day or {}
        loss_by_day = loss_by_day or {}
        specs = tuple(sorted(specs, key=lambda item: item[1]))
        manifests = []
        for calendar in sorted(calendars, key=lambda item: item.trading_day):
            day_specs = [
                item for item in specs if item[1].date() == calendar.trading_day
            ]
            through = through_by_day.get(
                calendar.trading_day, calendar.scheduled_close_at
            )
            through = max(
                (through, *(item[1] + timedelta(microseconds=1) for item in day_specs))
            )
            manifests.append(
                MarketEventSourceSessionManifest(
                    source_store_id=self.source_store_id,
                    session_id=f"fixture-{calendar.trading_day}",
                    connection_epoch=1,
                    reconnect_epoch=0,
                    collector_started_at=calendar.scheduled_open_at,
                    coverage_start=calendar.scheduled_open_at,
                    coverage_through=through,
                    expected_first_callback_seq=1 if source_start_proof else None,
                    callback_sequence_scope=MarketEventCallbackSequenceScope.SESSION,
                    provider_sequence_available=False,
                    provider_sequence_scope=MarketEventProviderSequenceScope.UNAVAILABLE,
                    sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
                    subscription=MarketEventSubscriptionManifest(
                        Market.A, ("600519.SH",), ("TRADE_TICK",)
                    ),
                    subscription_activated_at=calendar.scheduled_open_at,
                    coverage_origin=CoverageOrigin.LIVE,
                    queue_overflow_count=loss_by_day.get(calendar.trading_day, 0),
                    dropped_callback_count=0,
                    first_callback_seq=1 if day_specs else None,
                    last_callback_seq=len(day_specs) if day_specs else None,
                )
            )
        manifests = tuple(manifests)
        by_day = {item.coverage_start.date(): item for item in manifests}
        records, partition_previous, counts = [], {}, {}
        previous = "0" * 64
        for order, (label, at, price) in enumerate(specs, 1):
            manifest = by_day[at.date()]
            counts[manifest.session_id] = counts.get(manifest.session_id, 0) + 1
            partition = f"market=A/trading_day={at.date()}/symbol=600519.SH"
            record = MarketEventSourceRecord.create(
                source_store_id=self.source_store_id,
                append_order=order,
                event_id=_hash(label),
                session_id=manifest.session_id,
                source_session_manifest_id=manifest.manifest_id,
                connection_epoch=manifest.connection_epoch,
                reconnect_epoch=0,
                source="fixture-trades",
                feed_mode="SYNTHETIC",
                symbol="600519.SH",
                market=Market.A,
                event_type="TRADE_TICK",
                trading_day=at.date(),
                session_label_policy_id=MARKET_SESSION_LABEL_POLICY_V1,
                source_time=at,
                received_at=at + timedelta(microseconds=1),
                durable_known_at=at + timedelta(microseconds=2),
                callback_seq=counts[manifest.session_id],
                provider_seq=None,
                partition_key=partition,
                previous_global_record_hash=previous,
                previous_partition_record_hash=partition_previous.get(
                    partition, "0" * 64
                ),
                raw_payload_sha256=_hash(f"synthetic-raw:{label}:{price}"),
                payload={"last_price": price, "quantity": 100},
                parser_id="fixture-parser-v1",
                source_schema_id=FIXTURE_TRADE_TICK_SCHEMA_V1,
            )
            records.append(record)
            previous = partition_previous[partition] = record.record_content_hash
        records = tuple(records)
        grouped = {}
        for record in records:
            grouped.setdefault(record.partition_key, []).append(record)
        heads = tuple(
            MarketEventPartitionHead(
                key,
                len(items),
                items[0].record_content_hash,
                items[-1].record_content_hash,
                _hash(key),
            )
            for key, items in sorted(grouped.items())
        )
        audited = max(
            (
                *(item.coverage_through for item in manifests),
                *(item.durable_known_at for item in records),
            )
        ) + timedelta(microseconds=1)
        inventory = MarketEventInventoryVerification.create_from_prefix(
            source_store_id=self.source_store_id,
            catalog_schema_fingerprint=_hash("fixture-catalog"),
            records=records,
        )
        from tests.test_market_source_snapshot_contracts import transport_fixture

        transport = (
            transport_fixture(manifests, records)
            if (source_start_proof and getattr(self, "transport_enabled", True))
            else None
        )
        snapshot = MarketEventStoreSnapshot.from_audit(
            MarketEventStoreAudit.create_from_prefix(
                source_store_id=self.source_store_id,
                source_schema_id="fixture-store-v4",
                sequence_policy_id=MARKET_EVENT_SEQUENCE_POLICY_V3,
                audited_at=audited,
                catalog_schema_fingerprint=_hash("fixture-catalog"),
                inventory_verification=inventory,
                records=records,
                partition_heads=heads,
                findings=(),
                source_session_manifests=manifests,
                transport_snapshot=transport,
            )
        )

        def select(start, end):
            selection = snapshot.select_from_prefix(
                records=records,
                partition_heads=heads,
                findings=(),
                source_session_manifests=manifests,
                symbol="600519.SH",
                market=Market.A,
                start_source_time=start,
                end_source_time=end,
                allowed_event_types=("TRADE_TICK",),
                interval_boundary_policy_id=RUNTIME_INTERVAL_BOUNDARY_POLICY_V1,
                record_limit=max(1, len(records)),
            )
            verification = snapshot.verify_selection(
                selection,
                records=records,
                partition_heads=heads,
                findings=(),
                source_session_manifests=manifests,
            )
            return selection, verification

        selection, verification = select(
            min(item.coverage_start for item in manifests),
            max(item.coverage_through for item in manifests),
        )
        return selection, verification, select

    def authority_reference(
        self,
        *,
        kind,
        trading_day,
        known_at,
        label,
        revision=1,
        usable_from=None,
        fact=None,
        append_order=1,
        previous="0" * 64,
    ):
        if fact is None:
            calendar = self.calendar(trading_day)
            if kind is RuntimeAuthorityKind.CALENDAR:
                fact = calendar
            elif kind is RuntimeAuthorityKind.SECURITY_STATUS:
                fact = SecurityStatusFact(
                    "600519.SH",
                    Market.A,
                    trading_day,
                    RuntimeSecurityStatus.TRADABLE,
                    _hash("status-policy"),
                    label,
                )
            else:
                selection, verification, _ = self.market_fixture((calendar,))
                fact = SourceCoverageFact(
                    calendar,
                    selection,
                    verification,
                    calendar.scheduled_close_at,
                    _hash("coverage-policy"),
                )
        return RuntimeAuthorityFactReference(
            authority_store_id=_hash(f"authority-store:{kind.value}"),
            fact=fact,
            known_at=known_at,
            usable_from=known_at if usable_from is None else usable_from,
            source=f"fixture-{kind.value}",
            fact_revision=revision,
            authority_append_order=append_order,
            previous_authority_record_hash=previous,
            authority_record_policy_id=_hash(f"policy:{kind.value}"),
        )

    def session_fact(
        self,
        *,
        trading_day,
        collection_order,
        source_order,
        open_session_index,
        calendar_state=RuntimeCalendarState.OPEN,
        open_session_state=RuntimeOpenSessionState.TRADED,
        coverage_state=RuntimeCoverageState.COMPLETE_SESSION,
        session_complete=True,
        coverage_through=None,
    ):
        closed = calendar_state is RuntimeCalendarState.MARKET_CLOSED
        calendar = self.calendar(trading_day, open_session_index, closed=closed)
        known = datetime.combine(
            trading_day, datetime.min.time(), tzinfo=timezone.utc
        ) + timedelta(hours=8)
        calendar_ref = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=trading_day,
            known_at=known,
            label="calendar",
            revision=1,
            fact=calendar,
        )
        security_ref = coverage_ref = None
        if not closed:
            through = coverage_through or (
                calendar.scheduled_open_at + timedelta(hours=1)
                if coverage_state is RuntimeCoverageState.COMPLETE_PREFIX
                else calendar.scheduled_close_at
            )
            if coverage_state is RuntimeCoverageState.COMPLETE_SESSION and (
                not session_complete or through != calendar.scheduled_close_at
            ):
                raise RuntimePathContractError(
                    "COMPLETE_SESSION must cover the scheduled session close"
                )
            loss = int(
                coverage_state
                not in {
                    RuntimeCoverageState.COMPLETE_PREFIX,
                    RuntimeCoverageState.COMPLETE_SESSION,
                }
            )
            selection, verification, _ = self.market_fixture(
                (calendar,),
                through_by_day={trading_day: through},
                loss_by_day={trading_day: loss},
            )
            coverage = SourceCoverageFact(
                calendar, selection, verification, through, _hash("coverage-policy")
            )
            coverage_ref = self.authority_reference(
                kind=RuntimeAuthorityKind.COVERAGE,
                trading_day=trading_day,
                known_at=known,
                label="coverage",
                revision=1,
                fact=coverage,
            )
            security = SecurityStatusFact(
                "600519.SH",
                Market.A,
                trading_day,
                {
                    RuntimeOpenSessionState.TRADED: RuntimeSecurityStatus.TRADABLE,
                    RuntimeOpenSessionState.SUSPENDED: RuntimeSecurityStatus.SUSPENDED,
                    RuntimeOpenSessionState.NO_TRADE: RuntimeSecurityStatus.NO_TRADE,
                    RuntimeOpenSessionState.MISSING_DATA: RuntimeSecurityStatus.UNKNOWN,
                }[open_session_state],
                _hash("status-policy"),
                "fixture",
            )
            security_ref = self.authority_reference(
                kind=RuntimeAuthorityKind.SECURITY_STATUS,
                trading_day=trading_day,
                known_at=known,
                label="security",
                revision=1,
                fact=security,
            )
            self.calendars[open_session_index] = calendar
        session = RuntimeSessionEvidence.from_typed_authority_facts(
            calendar_reference=calendar_ref,
            security_status_reference=security_ref,
            coverage_reference=coverage_ref,
        )
        return RuntimeFrozenPathFact(
            self.collection_store_id,
            collection_order,
            self.case_id,
            _hash(f"collection-fact-{collection_order}"),
            known + timedelta(seconds=1),
            RuntimePathFactKind.SESSION,
            session_evidence=session,
        )

    def point_fact(
        self,
        *,
        collection_order,
        source_order,
        session_index,
        interval_start,
        interval_end=None,
        high="10.5",
        low="9.5",
        close="10",
        granularity=RuntimePathGranularity.MINUTE_BAR,
        known_at=None,
        raw_source_time=None,
    ):
        end = (
            interval_end
            if interval_end is not None
            else interval_start + timedelta(minutes=1)
        )
        calendar = self.calendars.get(session_index) or self.calendar(
            interval_start.date(), session_index
        )
        if granularity is RuntimePathGranularity.TICK:
            if not high == low == close or end != interval_start:
                raise RuntimePathContractError("TICK must be one exact price point")
            specs = ((f"point-{collection_order}", interval_start, close),)
            through = interval_start + timedelta(microseconds=3)
        else:
            last = (
                raw_source_time
                if raw_source_time is not None
                else end - timedelta(seconds=1)
            )
            if not interval_start <= last < end:
                raise RuntimePathContractError(
                    "projection input outside half-open interval"
                )
            specs = tuple(
                (f"point-{collection_order}-{i}", at, price)
                for i, (at, price) in enumerate(
                    (
                        (interval_start, high),
                        (interval_start + (last - interval_start) / 2, low),
                        (last, close),
                    )
                )
            )
            through = end
        selection, verification, select = self.market_fixture(
            (calendar,), specs, through_by_day={calendar.trading_day: through}
        )
        created = known_at or max(
            end + timedelta(seconds=2), selection.snapshot_audited_at
        )
        if granularity is RuntimePathGranularity.TICK:
            tick = DecodedTradeTick(
                selection.records[0],
                selection,
                verification,
                FIXTURE_TRADE_DECODER_POLICY_V1,
            )
            observation = RuntimePathObservation.from_decoded_trade_tick(
                decoded_tick=tick, calendar_fact=calendar
            )
        else:
            coverage = SourceCoverageFact(
                calendar, selection, verification, through, _hash("coverage-policy")
            )
            reference = self.authority_reference(
                kind=RuntimeAuthorityKind.COVERAGE,
                trading_day=calendar.trading_day,
                known_at=selection.snapshot_audited_at,
                label="projection-coverage",
                fact=coverage,
            )
            bar_selection, bar_verification = select(interval_start, end)
            ticks = tuple(
                DecodedTradeTick(
                    record,
                    bar_selection,
                    bar_verification,
                    FIXTURE_TRADE_DECODER_POLICY_V1,
                )
                for record in bar_selection.records
            )
            projection = RuntimeProjectionArtifactReference.create(
                selection=bar_selection,
                verification=bar_verification,
                decoded_ticks=ticks,
                projection_policy_id=TRADE_PROJECTION_POLICY_V1,
                coverage_fact=reference,
                interval_start=interval_start,
                interval_end=end,
                created_at=created,
                durable_known_at=created,
            )
            observation = RuntimePathObservation.from_projection(
                projection=projection, calendar_fact=calendar, granularity=granularity
            )
        return RuntimeFrozenPathFact(
            self.collection_store_id,
            collection_order,
            self.case_id,
            _hash(f"collection-fact-{collection_order}"),
            created + timedelta(seconds=1),
            RuntimePathFactKind.POINT,
            path_observation=observation,
        )

    def prefix(
        self,
        facts,
        *,
        frozen_at=None,
        eligible_unprojected_source_times=(),
        source_start_proof=True,
    ):
        ordered = tuple(sorted(facts, key=lambda item: item.collection_append_order))
        sessions = {
            item.session_evidence.trading_day: item.session_evidence
            for item in ordered
            if item.session_evidence is not None
        }
        open_sessions = {
            day: session
            for day, session in sessions.items()
            if session.calendar_state is RuntimeCalendarState.OPEN
        }
        specs = []
        for fact in ordered:
            point = fact.path_observation
            if point is None:
                continue
            ticks = (
                (point.decoded_trade_tick,)
                if point.decoded_trade_tick is not None
                else point.projection_artifact_reference.decoded_ticks
            )
            specs.extend(
                (tick.record.event_id, tick.record.source_time, format(tick.price, "f"))
                for tick in ticks
            )
        specs.extend(
            (f"eligible-{index}", at, "10")
            for index, at in enumerate(eligible_unprojected_source_times)
        )
        selection, verification, select = self.market_fixture(
            tuple(item.calendar_reference.fact for item in open_sessions.values()),
            specs,
            through_by_day={
                day: item.coverage_through for day, item in open_sessions.items()
            },
            loss_by_day={
                day: int(
                    item.coverage_state
                    not in {
                        RuntimeCoverageState.COMPLETE_PREFIX,
                        RuntimeCoverageState.COMPLETE_SESSION,
                    }
                )
                for day, item in open_sessions.items()
            },
            source_start_proof=source_start_proof,
        )
        known = max(
            selection.snapshot_audited_at,
            *(item.collection_observed_at for item in ordered),
        )
        refs_by_kind = {}
        new_sessions = {}
        for day, session in sorted(sessions.items()):
            new_refs = []
            for reference in (
                session.calendar_reference,
                session.security_status_reference,
                session.coverage_reference,
            ):
                if reference is None:
                    new_refs.append(None)
                    continue
                payload = reference.fact
                if reference.authority_kind is RuntimeAuthorityKind.COVERAGE:
                    payload = SourceCoverageFact(
                        session.calendar_reference.fact,
                        selection,
                        verification,
                        session.coverage_through,
                        _hash("coverage-policy"),
                    )
                prior = refs_by_kind.setdefault(reference.authority_kind, [])
                ref = replace(
                    reference,
                    fact=payload,
                    known_at=known,
                    usable_from=known,
                    authority_append_order=len(prior) + 1,
                    previous_authority_record_hash=prior[-1].fact_record_hash
                    if prior
                    else "0" * 64,
                )
                prior.append(ref)
                new_refs.append(ref)
            new_sessions[day] = RuntimeSessionEvidence.from_typed_authority_facts(
                calendar_reference=new_refs[0],
                security_status_reference=new_refs[1],
                coverage_reference=new_refs[2],
            )
        rebound = []
        for item in ordered:
            if item.session_evidence is not None:
                rebound.append(
                    replace(
                        item,
                        session_evidence=new_sessions[
                            item.session_evidence.trading_day
                        ],
                        collection_observed_at=known + timedelta(seconds=1),
                    )
                )
                continue
            point = item.path_observation
            session = new_sessions[point.calendar_fact.trading_day]
            if point.decoded_trade_tick is not None:
                original = point.decoded_trade_tick.record
                record = next(
                    record
                    for record in selection.records
                    if record.event_id == _hash(original.event_id)
                )
                tick = DecodedTradeTick(
                    record, selection, verification, FIXTURE_TRADE_DECODER_POLICY_V1
                )
                observation = RuntimePathObservation.from_decoded_trade_tick(
                    decoded_tick=tick, calendar_fact=point.calendar_fact
                )
            else:
                bar_selection, bar_verification = select(
                    point.interval_start, point.interval_end
                )
                ticks = tuple(
                    DecodedTradeTick(
                        record,
                        bar_selection,
                        bar_verification,
                        FIXTURE_TRADE_DECODER_POLICY_V1,
                    )
                    for record in bar_selection.records
                )
                projection = RuntimeProjectionArtifactReference.create(
                    selection=bar_selection,
                    verification=bar_verification,
                    decoded_ticks=ticks,
                    projection_policy_id=TRADE_PROJECTION_POLICY_V1,
                    coverage_fact=session.coverage_reference,
                    interval_start=point.interval_start,
                    interval_end=point.interval_end,
                    created_at=known,
                    durable_known_at=known,
                )
                observation = RuntimePathObservation.from_projection(
                    projection=projection,
                    calendar_fact=point.calendar_fact,
                    granularity=point.granularity,
                )
            rebound.append(
                replace(
                    item,
                    path_observation=observation,
                    collection_observed_at=known + timedelta(seconds=1),
                )
            )
        ordered = tuple(rebound)
        authority_selections = tuple(
            RuntimeAuthorityFactSelection.from_verified_snapshot(
                snapshot=RuntimeAuthoritySnapshotBinding.from_facts(
                    authority_kind=kind,
                    authority_store_id=refs[0].authority_store_id,
                    authority_audited_at=known + timedelta(microseconds=1),
                    facts=tuple(refs),
                ),
                facts=tuple(refs),
                selection_policy_id=_hash(f"authority-selection:{kind}"),
            )
            for kind, refs in sorted(
                refs_by_kind.items(), key=lambda item: item[0].value
            )
        )
        path_snapshot = RuntimePathStoreSnapshotBinding.from_prefix(
            path_store_id=self.collection_store_id,
            path_audited_at=known + timedelta(seconds=2),
            facts=ordered,
            finding_blocker_digest=_hash("path-findings"),
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
            market_source_snapshot=RuntimeMarketSourceSnapshotBinding.from_verified_selection(
                selection=selection, verification=verification
            ),
            authority_fact_selections=authority_selections,
            frozen_at=frozen_at or known + timedelta(seconds=3),
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
            finding_blocker_digest=(prefix.path_store_snapshot.finding_blocker_digest),
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
        for granularity in (
            RuntimePathGranularity.MINUTE_BAR,
            RuntimePathGranularity.DAILY_BAR,
        ):
            with self.subTest(granularity=granularity.value):
                facts = self.three_complete_sessions()
                facts.append(
                    self.point_fact(
                        collection_order=4,
                        source_order=4,
                        session_index=0,
                        interval_start=self.entry_time,
                        interval_end=self.entry_time + timedelta(minutes=1),
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
        start = self.entry_time + timedelta(minutes=1)
        with self.assertRaisesRegex(RuntimePathContractError, "half-open"):
            self.point_fact(
                collection_order=1,
                source_order=1,
                session_index=0,
                interval_start=start,
                interval_end=start + timedelta(minutes=1),
                raw_source_time=start + timedelta(minutes=1),
            )

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
        self.assertEqual(
            result.first_touch_collection_fact_id, target.collection_fact_id
        )

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
        with self.assertRaisesRegex(
            RuntimePathContractError, "contradict selected trade"
        ):
            self.prefix(facts)

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
        with self.assertRaisesRegex(RuntimePathContractError, "calendar|segment"):
            self.point_fact(
                collection_order=1,
                source_order=1,
                session_index=0,
                interval_start=datetime(2026, 9, 1, 7, 1, tzinfo=timezone.utc),
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
        with self.assertRaisesRegex(RuntimePathContractError, "segment coverage"):
            self.prefix(facts)

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
        with self.assertRaisesRegex(
            RuntimePathContractError, "consumed more than once"
        ):
            self.prefix(facts)

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

    def test_entry_complete_prefix_at_fill_without_post_entry_record_is_open(
        self,
    ) -> None:
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
            eligible_unprojected_source_times=(self.entry_time + timedelta(minutes=1),),
        )
        result = resolve_runtime_path(self.window(horizon_sessions=1), prefix)
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertEqual(
            result.blocker_codes,
            (RuntimePathBlockerCode.UNPROJECTED_SOURCE_MEMBER,),
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
                attacked_observation = _tamper(
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

        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(
                point.path_observation,
                source_reference=replace(source, event_type="ORDER_BOOK"),
            )
        us_trading_day = market_session_date(
            source.source_time,
            Market.US,
            MARKET_SESSION_LABEL_POLICY_V1,
        )
        with self.assertRaisesRegex(TypeError, "init=False"):
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
            with (
                self.subTest(omission=label),
                self.assertRaisesRegex(
                    RuntimePathContractError,
                    "prefix",
                ),
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
        prefix = self.prefix([fact], source_start_proof=False)
        self.assertFalse(prefix.facts[0].session_evidence.session_complete)
        result = resolve_runtime_path(self.window(), prefix)
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertIn(RuntimePathBlockerCode.SESSION_GAP, result.blocker_codes)

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
        calendar = self.calendar(date(2026, 9, 1), closed=True)
        with self.assertRaisesRegex(RuntimePathContractError, "MARKET_CLOSED"):
            replace(calendar, open_session_index=0)
        reference = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=calendar.trading_day,
            known_at=self.entry_time,
            label="calendar",
            fact=calendar,
        )
        session = RuntimeSessionEvidence.from_typed_authority_facts(
            calendar_reference=reference
        )
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(session, open_session_state=RuntimeOpenSessionState.NO_TRADE)

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
        fact = self.session_fact(
            trading_day=date(2026, 9, 1),
            collection_order=1,
            source_order=1,
            open_session_index=0,
            open_session_state=RuntimeOpenSessionState.SUSPENDED,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "security status"):
            replace(fact.session_evidence, security_status_reference=None)

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
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(
                tick.path_observation,
                source_reference=replace(
                    tick.path_observation.source_reference,
                    source_time=tick.path_observation.interval_end
                    - timedelta(seconds=1),
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
        rewritten_projection = _tamper(projection, lineage=rewritten_lineage)
        rewritten_observation = _tamper(
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
        self.assertEqual(len(stores), 3)
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
        with self.assertRaisesRegex(RuntimePathContractError, "typed authority"):
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
            attacked_reference = _tamper(
                reference,
                fact_payload_sha256=_hash(f"caller:{reference.authority_kind.value}"),
            )
            attacked_session = _tamper(
                session,
                calendar_reference=(
                    attacked_reference
                    if reference.authority_kind is RuntimeAuthorityKind.CALENDAR
                    else session.calendar_reference
                ),
                security_status_reference=(
                    attacked_reference
                    if reference.authority_kind is RuntimeAuthorityKind.SECURITY_STATUS
                    else session.security_status_reference
                ),
            )
            attacked_fact = replace(rebound, session_evidence=attacked_session)
            with (
                self.subTest(kind=reference.authority_kind.value),
                self.assertRaisesRegex(
                    RuntimePathContractError,
                    "typed authority|mutated",
                ),
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
        calendar = self.calendar(date(2026, 9, 1))
        shifted = tuple(
            replace(
                item,
                start=item.start + timedelta(days=1),
                end=item.end + timedelta(days=1),
                calendar_fact_id=None,
            )
            for item in calendar.segments
        )
        with self.assertRaisesRegex(RuntimePathContractError, "trading-day mismatch"):
            replace(calendar, segments=shifted)


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
            source_fact_id=_hash(source_fact_label or f"execution:{execution_id}"),
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

    def test_partial_fill_aggregation_is_not_structurally_projectable_to_stage4g_v3(
        self,
    ) -> None:
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
            (
                RuntimeNoEntryReason.USER_CANCELLED,
                "USER_CANCELLATION",
                "cancellation_fact_id",
            ),
            (
                RuntimeNoEntryReason.ORDER_REJECTED,
                "ORDER_REJECTION",
                "rejection_fact_id",
            ),
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
            with (
                self.subTest(reason=reason.value),
                self.assertRaisesRegex(
                    RuntimePathContractError,
                    "predates its execution evidence",
                ),
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


class TestR3SemanticClosure(RuntimePathContractTestCase):
    def test_r3_tick_price_cannot_disagree_with_decoded_payload(self) -> None:
        point = self.point_fact(
            collection_order=1,
            source_order=1,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
            interval_end=self.entry_time + timedelta(minutes=1),
            high="10",
            low="10",
            close="10",
            granularity=RuntimePathGranularity.TICK,
        )
        with self.assertRaises((ValueError, TypeError)):
            replace(
                point.path_observation,
                high=Decimal(12),
                low=Decimal(12),
                close=Decimal(12),
            )

    def test_r3_projection_factory_does_not_accept_caller_ohlc(self) -> None:
        import inspect

        parameters = inspect.signature(
            RuntimeProjectionArtifactReference.create
        ).parameters
        self.assertFalse({"high", "low", "close"} & set(parameters))
        self.assertIn("decoded_ticks", parameters)

    def test_r3_omitted_earlier_member_blocks_later_target(self) -> None:
        facts = self.three_complete_sessions()
        facts.append(
            self.point_fact(
                collection_order=4,
                source_order=4,
                session_index=0,
                interval_start=self.entry_time + timedelta(minutes=10),
                high="12.1",
                close="12",
            )
        )
        prefix = self.prefix(
            facts,
            eligible_unprojected_source_times=(self.entry_time + timedelta(minutes=5),),
        )
        result = resolve_runtime_path(self.window(), prefix)
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertIn("UNPROJECTED_SOURCE_MEMBER", result.blocker_codes)

    def test_r3_same_authority_cannot_support_shifted_session(self) -> None:
        session = self.three_complete_sessions()[0].session_evidence
        with self.assertRaises((ValueError, TypeError)):
            replace(
                session,
                scheduled_open_at=session.scheduled_open_at + timedelta(minutes=5),
                scheduled_close_at=session.scheduled_close_at + timedelta(minutes=5),
                coverage_through=session.coverage_through + timedelta(minutes=5),
            )

    def test_r3_fact_revision_is_not_store_high_water(self) -> None:
        prefix = self.prefix(self.three_complete_sessions())
        snapshot = prefix.authority_fact_selections[0].snapshot
        self.assertFalse(hasattr(snapshot, "high_water_revision"))
        self.assertEqual(
            snapshot.authority_high_water_append_order, snapshot.fact_count
        )

    def test_r3_lunch_tick_cannot_trigger_target(self) -> None:
        facts = self.three_complete_sessions()
        timestamp = self.entry_time.replace(hour=4, minute=0)
        try:
            facts.append(
                self.point_fact(
                    collection_order=4,
                    source_order=4,
                    session_index=0,
                    interval_start=timestamp,
                    interval_end=timestamp,
                    high="12.1",
                    low="12.1",
                    close="12.1",
                    granularity=RuntimePathGranularity.TICK,
                )
            )
            result = resolve_runtime_path(self.window(), self.prefix(facts))
        except RuntimePathContractError:
            return
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)

    def test_r3_bar_cannot_cross_lunch_break(self) -> None:
        facts = self.three_complete_sessions()
        try:
            facts.append(
                self.point_fact(
                    collection_order=4,
                    source_order=4,
                    session_index=0,
                    interval_start=self.entry_time.replace(hour=3, minute=29),
                    interval_end=self.entry_time.replace(hour=5, minute=1),
                )
            )
            result = resolve_runtime_path(self.window(), self.prefix(facts))
        except RuntimePathContractError:
            return
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)

    def test_r3_segment_coverage_and_projection_manifest_are_required(self) -> None:
        prefix = self.prefix(self.three_complete_sessions())
        self.assertTrue(hasattr(prefix, "projection_manifest"))
        self.assertTrue(hasattr(prefix.facts[0].session_evidence, "segments"))

    def test_r3_projection_recomputes_prices_and_all_interval_members(self):
        bar = self.point_fact(
            collection_order=1,
            source_order=1,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=1),
            high="11",
            low="11",
            close="11",
        )
        projection = bar.path_observation.projection_artifact_reference
        self.assertEqual(
            (projection.high, projection.low, projection.close), (Decimal(11),) * 3
        )
        self.assertEqual(len(projection.decoded_ticks), 3)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(projection, high=Decimal(99))
        with self.assertRaisesRegex(RuntimePathContractError, "every interval member"):
            replace(projection, decoded_ticks=projection.decoded_ticks[:-1])
        with self.assertRaises(RuntimePathContractError):
            replace(projection, decoded_ticks=(projection.decoded_ticks[0],) * 3)
        self.assertEqual(projection.as_dict()["assurance"], "STRUCTURAL_FIXTURE")

    def test_r3_queue_and_drop_block_session_projection_and_timeout(self):
        calendar = self.calendar(date(2026, 9, 1))
        selection, verification, select = self.market_fixture(
            (calendar,),
            (("trade", self.entry_time + timedelta(minutes=1), "12"),),
            loss_by_day={calendar.trading_day: 1},
        )
        coverage = SourceCoverageFact(
            calendar,
            selection,
            verification,
            calendar.scheduled_close_at,
            _hash("coverage"),
        )
        self.assertFalse(coverage.session_complete)
        self.assertEqual(coverage.coverage_state, RuntimeCoverageState.INCOMPLETE_GAP)
        reference = self.authority_reference(
            kind=RuntimeAuthorityKind.COVERAGE,
            trading_day=calendar.trading_day,
            known_at=selection.snapshot_audited_at,
            label="coverage",
            fact=coverage,
        )
        bar_selection, bar_verification = select(
            self.entry_time, self.entry_time + timedelta(minutes=2)
        )
        ticks = tuple(
            DecodedTradeTick(
                record, bar_selection, bar_verification, FIXTURE_TRADE_DECODER_POLICY_V1
            )
            for record in bar_selection.records
        )
        with self.assertRaisesRegex(RuntimePathContractError, "segment coverage"):
            RuntimeProjectionArtifactReference.create(
                selection=bar_selection,
                verification=bar_verification,
                decoded_ticks=ticks,
                projection_policy_id=TRADE_PROJECTION_POLICY_V1,
                coverage_fact=reference,
                interval_start=bar_selection.start_source_time,
                interval_end=bar_selection.end_source_time,
                created_at=selection.snapshot_audited_at,
                durable_known_at=selection.snapshot_audited_at,
            )
        session = self.session_fact(
            trading_day=calendar.trading_day,
            collection_order=1,
            source_order=1,
            open_session_index=0,
            coverage_state=RuntimeCoverageState.INCOMPLETE_GAP,
            session_complete=False,
        )
        result = resolve_runtime_path(self.window(), self.prefix([session]))
        self.assertEqual(result.state, RuntimePathResolutionState.BLOCKED)
        self.assertIn(RuntimePathBlockerCode.SESSION_GAP, result.blocker_codes)

    def test_r3_afternoon_segment_gap_cannot_complete_session(self):
        calendar = self.calendar(date(2026, 9, 1))
        selection, verification, _ = self.market_fixture(
            (calendar,), through_by_day={calendar.trading_day: calendar.segments[0].end}
        )
        coverage = SourceCoverageFact(
            calendar,
            selection,
            verification,
            calendar.segments[0].end,
            _hash("coverage"),
        )
        self.assertEqual(coverage.coverage_state, RuntimeCoverageState.COMPLETE_PREFIX)
        self.assertFalse(coverage.session_complete)
        with self.assertRaisesRegex(RuntimePathContractError, "audit bounds"):
            replace(coverage, coverage_through=calendar.scheduled_close_at)

    def test_r3_halt_disallows_prices_and_halfday_derives_from_calendar(self):
        day = date(2026, 9, 1)
        full = self.calendar(day)
        half = replace(
            full, segments=(replace(full.segments[0], calendar_fact_id=None),)
        )
        selection, verification, _ = self.market_fixture((half,))
        coverage = SourceCoverageFact(
            half, selection, verification, half.scheduled_close_at, _hash("coverage")
        )
        known = selection.snapshot_audited_at
        calendar_ref = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=day,
            known_at=known,
            label="halfday",
            fact=half,
        )
        status_ref = self.authority_reference(
            kind=RuntimeAuthorityKind.SECURITY_STATUS,
            trading_day=day,
            known_at=known,
            label="status",
            fact=SecurityStatusFact(
                "600519.SH",
                Market.A,
                day,
                RuntimeSecurityStatus.NO_TRADE,
                _hash("status"),
                "zero verified fixture interval",
            ),
        )
        coverage_ref = self.authority_reference(
            kind=RuntimeAuthorityKind.COVERAGE,
            trading_day=day,
            known_at=known,
            label="coverage",
            fact=coverage,
        )
        session = RuntimeSessionEvidence.from_typed_authority_facts(
            calendar_reference=calendar_ref,
            security_status_reference=status_ref,
            coverage_reference=coverage_ref,
        )
        self.assertEqual(session.scheduled_close_at, half.segments[0].end)
        self.assertTrue(session.session_complete)
        self.assertTrue(
            all(
                item.calendar_fact_id == half.calendar_fact_id
                for item in session.segments
            )
        )
        halted = replace(
            half,
            segments=(
                TradingSessionSegment(
                    TradingSessionSegmentKind.HALT,
                    half.scheduled_open_at,
                    half.scheduled_close_at,
                    False,
                    False,
                    TRADING_SEGMENT_POLICY_V1,
                ),
            ),
        )
        source, proof, _ = self.market_fixture(
            (halted,), (("illegal-trade", self.entry_time, "12"),)
        )
        tick = DecodedTradeTick(
            source.records[0], source, proof, FIXTURE_TRADE_DECODER_POLICY_V1
        )
        with self.assertRaisesRegex(RuntimePathContractError, "BREAK/HALT"):
            RuntimePathObservation.from_decoded_trade_tick(
                decoded_tick=tick, calendar_fact=halted
            )
        with self.assertRaisesRegex(RuntimePathContractError, "BREAK/HALT"):
            replace(halted.segments[0], price_events_allowed=True)

    def test_r3_revision_and_append_chain_are_independent(self):
        known = self.entry_time + timedelta(days=3)
        first = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 1),
            known_at=known,
            label="first",
            revision=1,
        )
        second = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 2),
            known_at=known,
            label="second",
            revision=1,
            append_order=2,
            previous=first.fact_record_hash,
        )
        snapshot = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=RuntimeAuthorityKind.CALENDAR,
            authority_store_id=first.authority_store_id,
            authority_audited_at=known,
            facts=(first, second),
        )
        self.assertEqual(snapshot.authority_high_water_append_order, 2)
        for invalid in (
            replace(second, authority_append_order=4),
            replace(second, previous_authority_record_hash="0" * 64),
        ):
            with self.assertRaisesRegex(RuntimePathContractError, "append chain"):
                RuntimeAuthoritySnapshotBinding.from_facts(
                    authority_kind=RuntimeAuthorityKind.CALENDAR,
                    authority_store_id=first.authority_store_id,
                    authority_audited_at=known,
                    facts=(first, invalid),
                )

    def test_r3_projection_manifest_rejects_duplicate_raw_consumption(self):
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
            interval_end=self.entry_time + timedelta(minutes=1),
            high="10",
            low="10",
            close="10",
            granularity=RuntimePathGranularity.TICK,
        )
        prefix = self.prefix([session, point])
        manifest = prefix.projection_manifest
        with self.assertRaisesRegex(
            RuntimePathContractError, "consumed more than once"
        ):
            replace(manifest, observations=manifest.observations * 2)
        attacked = _tamper(
            prefix, projection_manifest=_tamper(manifest, consumed_source_record_ids=())
        )
        with self.assertRaises(RuntimePathContractError):
            resolve_runtime_path(self.window(), attacked)

    def test_r3_execution_and_no_entry_remain_structural(self):
        fixture = TestRuntimeExecutionAndNoEntry()
        fixture.setUp()
        summary = fixture.zero_fill_summary()
        fact_id = _hash("coverage")
        reference = fixture.evidence_reference(
            fact_id,
            source_append_order=1,
            known_at=self.entry_time,
            fact_kind="ENTRY_WINDOW_COVERAGE",
        )
        evidence = RuntimeNoEntryEvidence(
            reason=RuntimeNoEntryReason.ENTRY_EXPIRED,
            execution_summary=summary,
            entry_window_start=self.entry_time,
            entry_window_end=self.entry_time + timedelta(hours=1),
            decided_at=self.entry_time + timedelta(hours=1),
            execution_policy_id=_hash("policy"),
            evidence_references=(reference,),
            evidence_ids=(fact_id,),
            entry_window_coverage_complete=True,
            entry_window_coverage_fact_id=fact_id,
        )
        self.assertEqual(summary.as_dict()["assurance"], "STRUCTURAL_FIXTURE")
        self.assertEqual(evidence.as_dict()["assurance"], "STRUCTURAL_FIXTURE")
        with self.assertRaises(TypeError):
            replace(summary, assurance="STORE_RESCANNED")


class TestR4AuthorityClosure(RuntimePathContractTestCase):
    def authority_chain(self, *, future=False):
        first = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 1),
            known_at=self.entry_time,
            label="r4-calendar",
            revision=1,
        )
        second = replace(
            first,
            fact=replace(
                first.fact,
                segments=(replace(first.fact.segments[0], calendar_fact_id=None),),
            ),
            fact_revision=2,
            authority_append_order=2,
            previous_authority_record_hash=first.fact_record_hash,
            previous_entity_fact_id=first.fact_id,
            supersedes_fact_id=first.fact_id,
            known_at=self.entry_time + timedelta(days=1 if future else 0),
            usable_from=self.entry_time + timedelta(days=1 if future else 0),
        )
        return first, second

    def authority_inventory(self, facts):
        snapshot = RuntimeAuthoritySnapshotBinding.from_facts(
            authority_kind=RuntimeAuthorityKind.CALENDAR,
            authority_store_id=facts[0].authority_store_id,
            authority_audited_at=max(f.known_at for f in facts),
            facts=facts,
        )
        return RuntimeAuthorityFactSelection.from_verified_snapshot(
            snapshot=snapshot,
            facts=facts,
            selection_policy_id=_hash("inventory"),
        )

    def effective(self, facts, cutoff):
        from stock_tracker.runtime_evidence.path_contracts import (
            RuntimeAuthorityEffectiveSelection,
        )

        return RuntimeAuthorityEffectiveSelection(
            inventory=self.authority_inventory(facts),
            cutoff=cutoff,
            symbol="600519.SH",
            market=Market.A,
            session_dates=(date(2026, 9, 1),),
        )

    def test_r4_no_liveness_cannot_timeout(self):
        self.transport_enabled = False
        result = resolve_runtime_path(
            self.window(), self.prefix(self.three_complete_sessions())
        )
        self.assertNotEqual(result.state, RuntimePathResolutionState.TIMEOUT)

    def test_r4_latest_usable_calendar_supersedes_old_fact(self):
        first, second = self.authority_chain()
        effective = self.effective((first, second), self.entry_time)
        self.assertEqual(effective.active_fact(first.authority_entity_key), second)
        self.assertEqual(effective.superseded_fact_ids, (first.fact_id,))

    def test_r4_future_known_revision_keeps_old_active(self):
        first, second = self.authority_chain(future=True)
        self.assertEqual(
            self.effective((first, second), self.entry_time).active_fact(
                first.authority_entity_key
            ),
            first,
        )

    def test_r4_duplicate_entity_revision_rejected(self):
        first, second = self.authority_chain()
        with self.assertRaisesRegex(RuntimePathContractError, "entity"):
            self.authority_inventory((first, replace(second, fact_revision=1)))

    def test_r4_entity_revision_rollback_rejected(self):
        first = self.authority_reference(
            kind=RuntimeAuthorityKind.CALENDAR,
            trading_day=date(2026, 9, 1),
            known_at=self.entry_time,
            label="rollback",
            revision=2,
        )
        second = replace(
            first,
            fact_revision=1,
            authority_append_order=2,
            previous_authority_record_hash=first.fact_record_hash,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "entity"):
            self.authority_inventory((first, second))

    def test_r4_entity_branch_rejected(self):
        first, second = self.authority_chain()
        branch = replace(
            second,
            authority_append_order=3,
            previous_authority_record_hash=second.fact_record_hash,
        )
        with self.assertRaisesRegex(RuntimePathContractError, "entity"):
            self.authority_inventory((first, second, branch))

    def test_r4_missing_entity_predecessor_rejected(self):
        first, second = self.authority_chain()
        with self.assertRaisesRegex(RuntimePathContractError, "entity"):
            self.authority_inventory(
                (first, replace(second, previous_entity_fact_id=_hash("missing")))
            )

    def test_r4_stale_calendar_rejected_by_prefix_with_stable_code(self):
        prefix = self.prefix(self.three_complete_sessions())
        inventory = next(
            i
            for i in prefix.authority_fact_selections
            if i.snapshot.authority_kind is RuntimeAuthorityKind.CALENDAR
        )
        first = inventory.facts[0]
        newer = replace(
            first,
            fact=replace(
                first.fact,
                segments=(replace(first.fact.segments[0], calendar_fact_id=None),),
            ),
            fact_revision=2,
            authority_append_order=len(inventory.facts) + 1,
            previous_authority_record_hash=inventory.facts[-1].fact_record_hash,
            previous_entity_fact_id=first.fact_id,
            supersedes_fact_id=first.fact_id,
            known_at=prefix.frozen_at,
            usable_from=prefix.frozen_at,
        )
        revised = self.authority_inventory((*inventory.facts, newer))
        with self.assertRaises(RuntimePathContractError) as raised:
            RuntimeFrozenPathPrefix.from_verified_case_selection(
                case_selection=prefix.case_selection,
                market_source_snapshot=prefix.market_source_snapshot,
                authority_fact_selections=tuple(
                    revised if i is inventory else i
                    for i in prefix.authority_fact_selections
                ),
                frozen_at=prefix.frozen_at,
            )
        self.assertEqual(raised.exception.code, "STALE_AUTHORITY_FACT")

    def test_r4_future_usable_revision_and_unknown_selection_policy(self):
        first, second = self.authority_chain()
        future = replace(second, usable_from=self.entry_time + timedelta(days=1))
        effective = self.effective((first, future), self.entry_time)
        self.assertEqual(effective.active_fact(first.authority_entity_key), first)
        with self.assertRaisesRegex(RuntimePathContractError, "policy"):
            replace(effective, effective_selection_policy_id=_hash("caller-policy"))
        with self.assertRaises(TypeError):
            replace(effective, assurance="STORE_RESCANNED")

    def test_r4_coverage_entity_excludes_selection_snapshot_identity(self):
        calendar = self.calendar(date(2026, 9, 1))
        first_selection, first_proof, _ = self.market_fixture((calendar,))
        next_selection, next_proof, _ = self.market_fixture(
            (calendar,), (("new-trade", self.entry_time, "10"),)
        )
        references = []
        for selection, proof in (
            (first_selection, first_proof),
            (next_selection, next_proof),
        ):
            coverage = SourceCoverageFact(
                calendar,
                selection,
                proof,
                calendar.scheduled_close_at,
                _hash("coverage-policy"),
            )
            references.append(
                self.authority_reference(
                    kind=RuntimeAuthorityKind.COVERAGE,
                    trading_day=calendar.trading_day,
                    known_at=selection.snapshot_audited_at,
                    label="coverage",
                    fact=coverage,
                )
            )
        self.assertNotEqual(references[0].fact_id, references[1].fact_id)
        self.assertEqual(
            references[0].authority_entity_key, references[1].authority_entity_key
        )


if __name__ == "__main__":
    unittest.main()
