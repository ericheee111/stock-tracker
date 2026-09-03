from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_tracker.core.market_time import MARKET_SESSION_LABEL_POLICY_V1
from stock_tracker.core.types import Market
from stock_tracker.runtime_evidence.path_contracts import (
    RuntimeAuthorityFactReference,
    RuntimeAuthorityKind,
    RuntimeAuthoritySnapshotBinding,
    RuntimeCalendarState,
    RuntimeCoverageState,
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
    RuntimePathWindow,
    RuntimeProjectionLineage,
    RuntimeSessionEvidence,
    RuntimeSourceReference,
    resolve_runtime_path,
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

    def source_reference(
        self,
        *,
        label: str,
        append_order: int,
        known_at: datetime,
        source_time: datetime | None = None,
    ) -> RuntimeSourceReference:
        source_time = known_at - timedelta(seconds=2) if source_time is None else source_time
        return RuntimeSourceReference(
            source_store_id=self.source_store_id,
            source_session_id="market-session-a",
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
            authority_audit_id=_hash(f"authority-audit:{kind.value}"),
            fact_id=_hash(label),
            fact_schema=f"stage4g1-{kind.value.lower()}-fact-v1",
            effective_session_date=trading_day,
            known_at=known_at,
            usable_from=known_at if usable_from is None else usable_from,
            source=f"test-{kind.value.lower()}",
            revision=revision,
            policy_id=_hash(f"policy:{kind.value}"),
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
    ) -> RuntimeFrozenPathFact:
        interval_end = (
            interval_start + timedelta(minutes=1)
            if interval_end is None
            else interval_end
        )
        durable_known_at = (
            interval_end + timedelta(seconds=2) if known_at is None else known_at
        )
        source = self.source_reference(
            label=f"point-{collection_order}",
            append_order=source_order,
            known_at=durable_known_at,
            source_time=interval_end,
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
                interval_start=interval_start,
                interval_end=interval_end,
                coverage_fact_id=self.coverage_fact_by_index.get(
                    session_index,
                    _hash(f"missing-coverage:{session_index}"),
                ),
                input_source_append_orders=(source.source_append_order,),
                input_source_record_ids=(source.source_record_id,),
                input_source_record_hashes=(source.source_record_hash,),
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
            source_reference=source,
            projection_lineage=projection_lineage,
        )
        return RuntimeFrozenPathFact(
            collection_store_id=self.collection_store_id,
            collection_append_order=collection_order,
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
    ) -> RuntimeFrozenPathPrefix:
        ordered = tuple(sorted(facts, key=lambda item: item.collection_append_order))
        last_observed = max(item.collection_observed_at for item in ordered)
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
        authority_bindings = tuple(
            RuntimeAuthoritySnapshotBinding(
                authority_kind=kind,
                authority_store_id=next(
                    item.authority_store_id
                    for item in authority_references
                    if item.authority_kind is kind
                ),
                authority_audit_id=next(
                    item.authority_audit_id
                    for item in authority_references
                    if item.authority_kind is kind
                ),
                high_water_revision=max(
                    item.revision
                    for item in authority_references
                    if item.authority_kind is kind
                ),
            )
            for kind in sorted(
                {item.authority_kind for item in authority_references},
                key=lambda item: item.value,
            )
        )
        return RuntimeFrozenPathPrefix(
            case_id=self.case_id,
            symbol="600519.SH",
            market=Market.A,
            collection_store_id=self.collection_store_id,
            market_source_snapshot=RuntimeMarketSourceSnapshotBinding(
                source_store_id=self.source_store_id,
                source_snapshot_id=self.source_snapshot_id,
                source_audit_id=self.source_audit_id,
                high_water_append_order=self.source_high_water,
                finding_set_digest=self.source_finding_set_digest,
                sequence_policy_id="stage4g1-market-event-sequence-policy-v1",
            ),
            authority_snapshot_bindings=authority_bindings,
            frozen_at=last_observed + timedelta(seconds=1) if frozen_at is None else frozen_at,
            collection_high_water_append_order=max(
                item.collection_append_order for item in ordered
            ),
            collection_audit_id=_hash("collection-audit"),
            facts=ordered,
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
            (RuntimePathBlockerCode.WINDOW_BOUNDARY_OVERLAP,),
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

    def test_traded_complete_session_without_points_is_blocked(self) -> None:
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
        with self.assertRaises(RuntimePathContractError):
            replace(
                prefix_with_point,
                market_source_snapshot=replace(
                    prefix_with_point.market_source_snapshot,
                    high_water_append_order=0,
                ),
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

    def test_bar_and_tick_source_times_must_equal_interval_end(self) -> None:
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
            interval_start=self.entry_time + timedelta(minutes=1),
        )
        assert bar.path_observation is not None
        with self.assertRaisesRegex(RuntimePathContractError, "source_time"):
            replace(
                bar.path_observation,
                source_reference=replace(
                    bar.path_observation.source_reference,
                    source_time=bar.path_observation.interval_end - timedelta(seconds=1),
                ),
            )
        tick = self.point_fact(
            collection_order=3,
            source_order=3,
            session_index=0,
            interval_start=self.entry_time + timedelta(minutes=2),
            interval_end=self.entry_time + timedelta(minutes=2),
            high="10",
            low="10",
            close="10",
            granularity=RuntimePathGranularity.TICK,
        )
        assert tick.path_observation is not None
        with self.assertRaisesRegex(RuntimePathContractError, "source_time"):
            replace(
                tick.path_observation,
                source_reference=replace(
                    tick.path_observation.source_reference,
                    source_time=tick.path_observation.interval_end - timedelta(seconds=1),
                ),
            )
        self.prefix([session, bar, tick])

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
        assert point.path_observation is not None
        with self.assertRaisesRegex(RuntimePathContractError, "projection lineage"):
            replace(point.path_observation, projection_lineage=None)
        assert point.path_observation.projection_lineage is not None
        rewritten = replace(
            point.path_observation,
            projection_lineage=replace(
                point.path_observation.projection_lineage,
                source_snapshot_id=_hash("later-source-snapshot"),
            ),
        )
        with self.assertRaisesRegex(RuntimePathContractError, "frozen market"):
            self.prefix(
                [
                    session,
                    replace(point, path_observation=rewritten),
                ]
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
            item.authority_store_id for item in prefix.authority_snapshot_bindings
        }
        self.assertEqual(len(stores), 2)
        wrong_binding = replace(
            prefix.authority_snapshot_bindings[0],
            authority_store_id=_hash("wrong-authority-store"),
        )
        with self.assertRaisesRegex(RuntimePathContractError, "exact frozen"):
            replace(
                prefix,
                authority_snapshot_bindings=(
                    wrong_binding,
                    *prefix.authority_snapshot_bindings[1:],
                ),
            )
        wrong_audit = replace(
            prefix.authority_snapshot_bindings[0],
            authority_audit_id=_hash("wrong-authority-audit"),
        )
        with self.assertRaisesRegex(RuntimePathContractError, "exact frozen"):
            replace(
                prefix,
                authority_snapshot_bindings=(
                    wrong_audit,
                    *prefix.authority_snapshot_bindings[1:],
                ),
            )
        with self.assertRaisesRegex(RuntimePathContractError, "calendar_reference"):
            replace(
                fact.session_evidence,
                calendar_reference=_hash("caller-only-fact"),
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
            execution_stream_complete=True,
            execution_stream_audit_id=_hash("execution-stream-audit"),
        )

    def test_partial_fill_aggregation_is_not_stage4g_v3_finalizable(self) -> None:
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
            execution_stream_audit_id=_hash("execution-stream-audit"),
        )
        self.assertEqual(summary.completion, RuntimeFillCompletion.PARTIAL)
        self.assertEqual(summary.filled_quantity, 50)
        self.assertEqual(summary.quantity_weighted_price, Decimal("10.4"))
        self.assertEqual(summary.total_explicit_cost, Decimal(3))
        self.assertFalse(summary.stage4g_v3_finalizable)

    def test_complete_fill_aggregation_is_finalizable(self) -> None:
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
            execution_stream_audit_id=_hash("execution-stream-audit"),
        )
        self.assertEqual(summary.completion, RuntimeFillCompletion.COMPLETE)
        self.assertTrue(summary.stage4g_v3_finalizable)

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
                execution_stream_audit_id=_hash("execution-stream-audit"),
            )
        with self.assertRaises(RuntimePathContractError) as caught:
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(),
                execution_stream_complete=True,
                execution_stream_audit_id=_hash("execution-stream-audit"),
                native_multi_leg=True,
            )
        self.assertEqual(caught.exception.code, "NATIVE_MULTI_LEG_UNSUPPORTED")

    def test_complete_execution_stream_requires_audit_identity(self) -> None:
        with self.assertRaisesRegex(
            RuntimePathContractError,
            "audit identity",
        ):
            RuntimeExecutionSummary(
                intent_id=_hash("intent"),
                side=RuntimeExecutionSide.BUY,
                requested_quantity=100,
                fragments=(),
                execution_stream_complete=True,
                execution_stream_audit_id=None,
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
                execution_stream_audit_id=_hash("execution-stream-audit"),
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
            execution_stream_complete=False,
            execution_stream_audit_id=None,
        )
        with self.assertRaises(RuntimePathContractError):
            RuntimeNoEntryEvidence(
                reason=RuntimeNoEntryReason.ENTRY_EXPIRED,
                execution_summary=incomplete_zero,
                entry_window_start=self.entry_time,
                entry_window_end=self.entry_time + timedelta(hours=1),
                decided_at=self.entry_time + timedelta(hours=1),
                execution_policy_id=_hash("policy"),
                evidence_ids=(),
                entry_window_coverage_complete=True,
            )


if __name__ == "__main__":
    unittest.main()
