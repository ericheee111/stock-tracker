from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from stock_tracker.core.types import Market
from stock_tracker.quant.core.events import (
    EventAuthority,
    EventBook,
    EventContractError,
    EventCoverage,
    EventDirection,
    EventEntityBinding,
    EventEntityKind,
    EventEvidenceSnapshot,
    EventFact,
    EventLifecycle,
    EventMarketConfirmation,
    EventSourceStream,
    EventType,
    PublicationGranularity,
)

KNOWN = datetime.fromisoformat("2024-01-10T08:00:00+08:00")
AS_OF = datetime.fromisoformat("2024-01-31T16:00:00+08:00")
LATE = datetime.fromisoformat("2024-02-10T16:00:00+08:00")
START = date(2024, 1, 1)
END = date(2024, 1, 31)
EVENT_DATE = date(2024, 1, 10)


class EventFixtures(unittest.TestCase):
    def stream(self, **changes):
        values = {
            "owner": "SSE",
            "family": "LISTED_COMPANY_NOTICE",
            "version": "fixture-v1",
            "authority": EventAuthority.EXCHANGE,
        }
        values.update(changes)
        return EventSourceStream(**values)

    def other_stream(self):
        return self.stream(
            owner="CSRC",
            family="POLICY_NOTICE",
            version="policy-v1",
            authority=EventAuthority.REGULATOR,
        )

    def market_binding(self):
        return EventEntityBinding(
            kind=EventEntityKind.MARKET,
            market=Market.A,
            entity_id="A",
        )

    def instrument_binding(self):
        return EventEntityBinding(
            kind=EventEntityKind.INSTRUMENT,
            market=Market.A,
            entity_id="CN:SSE:SSECID-ALPHA",
            identity_fact_id="a" * 64,
            symbol="600101.SH",
        )

    def classification_binding(self):
        return EventEntityBinding(
            kind=EventEntityKind.CLASSIFICATION,
            market=Market.A,
            entity_id="CAPCO-INDUSTRY:C39",
            taxonomy_id="CAPCO-INDUSTRY",
            classification_id="C39",
            classification_snapshot_id="b" * 64,
        )

    def coverage(self, stream=None, **changes):
        values = {
            "stream": stream or self.stream(),
            "market": Market.A,
            "start_date": START,
            "end_date": END,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "revision": "coverage-r1",
            "supersedes_revision": None,
            "verified": False,
            "complete": False,
            "source_note": "",
        }
        values.update(changes)
        return EventCoverage(**values)

    def event(self, stream=None, **changes):
        binding = self.market_binding()
        values = {
            "event_id": "event-1",
            "stream": stream or self.stream(),
            "market": Market.A,
            "event_type": EventType.POLICY,
            "lifecycle": EventLifecycle.ANNOUNCED,
            "event_date": EVENT_DATE,
            "effective_from": date(2024, 1, 15),
            "effective_to": None,
            "title": "合成事件",
            "summary": "仅用于验证事件时间、实体与修订合同。",
            "source_published_at": date(2024, 1, 10),
            "publication_granularity": PublicationGranularity.DATE,
            "observed_at": KNOWN,
            "retrieved_at": KNOWN,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "entity_bindings": (binding,),
            "materiality": Decimal("0.8"),
            "novelty": Decimal("0.7"),
            "surprise": Decimal("0.4"),
            "direction": EventDirection.POSITIVE,
            "source_uri": "https://www.sse.com.cn/fixture/event-1",
            "raw_artifact_id": "c" * 64,
            "parse_descriptor_id": "d" * 64,
            "parser_version": "event-parser-v1",
            "evidence_ids": ("e" * 64,),
            "revision": "r1",
            "supersedes_revision": None,
            "verified": False,
            "source_note": "",
        }
        values.update(changes)
        if "entity_bindings" in changes:
            values["entity_bindings"] = tuple(
                sorted(changes["entity_bindings"], key=lambda item: item.binding_id)
            )
        return EventFact(**values)

    def confirmation(self, event=None, **changes):
        event = event or self.event()
        values = {
            "event_fact_id": event.fact_id,
            "evaluated_as_of": datetime.fromisoformat(
                "2024-01-12T16:00:00+08:00"
            ),
            "raw_bar_snapshot_id": "f" * 64,
            "feature_snapshot_id": "1" * 64,
            "price_response": Decimal("0.6"),
            "volume_response": Decimal("0.5"),
            "breadth_response": Decimal("0.4"),
            "confirmed": True,
            "direction": EventDirection.POSITIVE,
            "policy_version": "event-confirmation-v1",
        }
        values.update(changes)
        return EventMarketConfirmation(**values)

    def snapshot(
        self,
        *,
        streams=None,
        coverages=None,
        events=None,
        confirmations=(),
        as_of=AS_OF,
        start_date=START,
        end_date=END,
        require_verified=False,
        require_complete=False,
    ):
        stream_tuple = tuple(streams or (self.stream(),))
        coverage_tuple = tuple(coverages or (self.coverage(stream_tuple[0]),))
        event_tuple = tuple(events or (self.event(stream_tuple[0]),))
        return EventBook(
            coverage_tuple,
            event_tuple,
            confirmations,
        ).snapshot(
            Market.A,
            start_date,
            end_date,
            as_of,
            required_streams=stream_tuple,
            require_verified=require_verified,
            require_complete=require_complete,
        )


class TestEventEntityAndTimeContracts(EventFixtures):
    def test_market_instrument_and_classification_bindings_are_distinct(self) -> None:
        market = self.market_binding()
        instrument = self.instrument_binding()
        classification = self.classification_binding()
        self.assertEqual(len({market.binding_id, instrument.binding_id, classification.binding_id}), 3)
        with self.assertRaisesRegex(EventContractError, "identity_fact_id"):
            EventEntityBinding(
                kind=EventEntityKind.INSTRUMENT,
                market=Market.A,
                entity_id="CN:SSE:FORGED",
                symbol="600101.SH",
            )
        with self.assertRaisesRegex(EventContractError, "taxonomy:classification"):
            EventEntityBinding(
                kind=EventEntityKind.CLASSIFICATION,
                market=Market.A,
                entity_id="forged",
                taxonomy_id="CAPCO-INDUSTRY",
                classification_id="C39",
                classification_snapshot_id="b" * 64,
            )

    def test_date_only_publication_never_fabricates_intraday_precision(self) -> None:
        event = self.event()
        self.assertIs(type(event.source_published_at), date)
        with self.assertRaisesRegex(EventContractError, "date without fabricated time"):
            self.event(
                source_published_at=KNOWN,
                publication_granularity=PublicationGranularity.DATE,
            )
        with self.assertRaisesRegex(EventContractError, "cannot follow known_at"):
            self.event(source_published_at=date(2024, 1, 11))

    def test_scores_require_exact_finite_decimal_ranges(self) -> None:
        for field_name, value in (
            ("materiality", 0.8),
            ("novelty", True),
            ("surprise", Decimal("1.1")),
            ("materiality", Decimal("NaN")),
        ):
            with self.subTest(field=field_name), self.assertRaises(
                EventContractError
            ):
                self.event(**{field_name: value})

    def test_known_and_usable_time_are_fail_closed(self) -> None:
        earlier = datetime.fromisoformat("2024-01-09T08:00:00+08:00")
        later = datetime.fromisoformat("2024-01-11T08:00:00+08:00")
        with self.assertRaisesRegex(EventContractError, "known_at cannot precede"):
            self.event(known_at=earlier)
        with self.assertRaisesRegex(EventContractError, "usable_from cannot precede"):
            self.event(known_at=later, usable_from=KNOWN)

    def test_event_carries_no_trade_action_or_performance_surface(self) -> None:
        event = self.event()
        for forbidden in (
            "action",
            "buy",
            "sell",
            "position_size",
            "expected_return",
            "win_rate",
            "profit_factor",
        ):
            self.assertFalse(hasattr(event, forbidden))


class TestEventCoverageAndRevision(EventFixtures):
    def test_incomplete_candidate_snapshot_requires_explicit_opt_out(self) -> None:
        with self.assertRaisesRegex(EventContractError, "complete"):
            self.snapshot(require_complete=True)
        snapshot = self.snapshot()
        self.assertFalse(snapshot.coverages[0].complete)
        self.assertEqual(len(snapshot.active_events), 1)

    def test_required_streams_require_exact_coverage(self) -> None:
        with self.assertRaisesRegex(EventContractError, "missing visible coverage"):
            self.snapshot(streams=(self.stream(), self.other_stream()))
        second = self.other_stream()
        snapshot = self.snapshot(
            streams=(self.stream(), second),
            coverages=(self.coverage(), self.coverage(second, revision="other-coverage")),
            events=(self.event(), self.event(second, event_id="event-2", revision="other-r1")),
        )
        self.assertEqual(len(snapshot.coverages), 2)
        self.assertEqual(len(snapshot.events), 2)

    def test_future_cancellation_does_not_rewrite_earlier_snapshot(self) -> None:
        original = self.event()
        cancellation_time = datetime.fromisoformat("2024-02-01T08:00:00+08:00")
        cancellation = self.event(
            lifecycle=EventLifecycle.CANCELLED,
            observed_at=cancellation_time,
            retrieved_at=cancellation_time,
            known_at=cancellation_time,
            usable_from=cancellation_time,
            source_published_at=date(2024, 2, 1),
            revision="r2",
            supersedes_revision="r1",
        )
        book = EventBook((self.coverage(end_date=date(2024, 2, 29)),), (original, cancellation))
        before = book.snapshot(
            Market.A,
            START,
            END,
            AS_OF,
            required_streams=(self.stream(),),
            require_verified=False,
            require_complete=False,
        )
        after = book.snapshot(
            Market.A,
            START,
            END,
            LATE,
            required_streams=(self.stream(),),
            require_verified=False,
            require_complete=False,
        )
        self.assertEqual(before.events[0].revision, "r1")
        self.assertEqual(after.events[0].revision, "r2")
        self.assertEqual(len(after.active_events), 0)

    def test_terminal_event_moved_outside_range_removes_old_event(self) -> None:
        original = self.event()
        correction_time = datetime.fromisoformat("2024-01-20T08:00:00+08:00")
        moved = self.event(
            event_date=date(2024, 2, 5),
            observed_at=correction_time,
            retrieved_at=correction_time,
            known_at=correction_time,
            usable_from=correction_time,
            source_published_at=date(2024, 1, 20),
            lifecycle=EventLifecycle.CORRECTED,
            revision="r2",
            supersedes_revision="r1",
        )
        snapshot = EventBook(
            (self.coverage(end_date=date(2024, 2, 29)),),
            (original, moved),
        ).snapshot(
            Market.A,
            START,
            END,
            AS_OF,
            required_streams=(self.stream(),),
            require_verified=False,
            require_complete=False,
        )
        self.assertEqual(snapshot.events, ())

    def test_terminal_coverage_narrowing_removes_old_claim(self) -> None:
        broad = self.coverage(end_date=date(2024, 2, 29))
        changed_time = datetime.fromisoformat("2024-02-01T08:00:00+08:00")
        narrow = self.coverage(
            start_date=date(2024, 2, 1),
            end_date=date(2024, 2, 29),
            known_at=changed_time,
            usable_from=changed_time,
            revision="coverage-r2",
            supersedes_revision="coverage-r1",
        )
        book = EventBook((broad, narrow), (self.event(),))
        book.snapshot(
            Market.A,
            START,
            END,
            AS_OF,
            required_streams=(self.stream(),),
            require_verified=False,
            require_complete=False,
        )
        with self.assertRaisesRegex(EventContractError, "no longer contains"):
            book.snapshot(
                Market.A,
                START,
                END,
                LATE,
                required_streams=(self.stream(),),
                require_verified=False,
                require_complete=False,
            )

    def test_revision_cycle_missing_predecessor_and_disconnected_terminals_fail(self) -> None:
        base = self.event()
        missing = replace(base, revision="r2", supersedes_revision="missing")
        cycle_a = replace(base, revision="r2", supersedes_revision="r3")
        cycle_b = replace(base, revision="r3", supersedes_revision="r2")
        disconnected = replace(
            base,
            revision="other-root",
            supersedes_revision=None,
        )
        for name, events in (
            ("missing", (missing,)),
            ("cycle", (cycle_a, cycle_b)),
            ("disconnected", (base, disconnected)),
        ):
            with self.subTest(name=name), self.assertRaises(EventContractError):
                self.snapshot(events=events)

    def test_future_event_is_not_visible(self) -> None:
        future_time = datetime.fromisoformat("2024-02-01T08:00:00+08:00")
        future = self.event(
            event_id="future-event",
            observed_at=future_time,
            retrieved_at=future_time,
            known_at=future_time,
            usable_from=future_time,
            source_published_at=date(2024, 2, 1),
            event_date=date(2024, 1, 20),
            revision="future-r1",
        )
        snapshot = self.snapshot(events=(self.event(), future))
        self.assertEqual([item.event_id for item in snapshot.events], ["event-1"])


class TestEventMarketConfirmation(EventFixtures):
    def test_latest_visible_confirmation_is_selected_and_bound_to_snapshots(self) -> None:
        event = self.event()
        first = self.confirmation(event)
        second = self.confirmation(
            event,
            evaluated_as_of=datetime.fromisoformat("2024-01-15T16:00:00+08:00"),
            raw_bar_snapshot_id="2" * 64,
            feature_snapshot_id="3" * 64,
            price_response=Decimal("0.8"),
        )
        future = self.confirmation(
            event,
            evaluated_as_of=datetime.fromisoformat("2024-02-01T16:00:00+08:00"),
            raw_bar_snapshot_id="4" * 64,
            feature_snapshot_id="5" * 64,
            price_response=Decimal("0.9"),
        )
        snapshot = self.snapshot(
            events=(event,),
            confirmations=(first, second, future),
        )
        self.assertEqual(len(snapshot.confirmations), 1)
        self.assertEqual(
            snapshot.confirmations[0].raw_bar_snapshot_id,
            "2" * 64,
        )
        self.assertEqual(snapshot.confirmed_events, (event,))

    def test_same_time_conflicting_confirmation_fails_closed(self) -> None:
        event = self.event()
        first = self.confirmation(event)
        second = replace(first, price_response=Decimal("0.7"))
        with self.assertRaisesRegex(EventContractError, "disagree"):
            self.snapshot(events=(event,), confirmations=(first, second))

    def test_market_confirmation_cannot_precede_event_visibility(self) -> None:
        event = self.event()
        confirmation = self.confirmation(
            event,
            evaluated_as_of=datetime.fromisoformat(
                "2024-01-09T16:00:00+08:00"
            ),
        )
        with self.assertRaisesRegex(EventContractError, "usable_from"):
            self.snapshot(events=(event,), confirmations=(confirmation,))

    def test_cancelled_event_cannot_retain_confirmed_market_response(self) -> None:
        cancelled = self.event(lifecycle=EventLifecycle.CANCELLED)
        confirmation = self.confirmation(cancelled)
        with self.assertRaisesRegex(EventContractError, "cancelled"):
            EventEvidenceSnapshot(
                market=Market.A,
                start_date=START,
                end_date=END,
                as_of=AS_OF,
                required_streams=(self.stream(),),
                coverages=(self.coverage(),),
                events=(cancelled,),
                confirmations=(confirmation,),
                require_verified=False,
                require_complete=False,
            )

    def test_confirmation_scores_and_ids_are_strict(self) -> None:
        event = self.event()
        for field_name, value in (
            ("price_response", 0.5),
            ("volume_response", Decimal("1.1")),
            ("breadth_response", Decimal("NaN")),
            ("raw_bar_snapshot_id", "X" * 64),
        ):
            with self.subTest(field=field_name), self.assertRaises(
                EventContractError
            ):
                self.confirmation(event, **{field_name: value})


class TestEventSnapshotIdentity(EventFixtures):
    def test_snapshot_identity_is_derived_and_input_order_independent(self) -> None:
        stream = self.stream()
        other = self.other_stream()
        first_event = self.event(stream)
        second_event = self.event(
            other,
            event_id="event-2",
            event_date=date(2024, 1, 20),
            revision="event-2-r1",
        )
        first_coverage = self.coverage(stream)
        second_coverage = self.coverage(other, revision="coverage-2-r1")
        forward = self.snapshot(
            streams=(stream, other),
            coverages=(first_coverage, second_coverage),
            events=(first_event, second_event),
        )
        reverse = self.snapshot(
            streams=(other, stream),
            coverages=(second_coverage, first_coverage),
            events=(second_event, first_event),
        )
        self.assertEqual(forward.snapshot_id, reverse.snapshot_id)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(forward, snapshot_id="f" * 64)

    def test_entity_query_uses_binding_identity(self) -> None:
        bindings = tuple(
            sorted(
                (self.instrument_binding(), self.classification_binding()),
                key=lambda item: item.binding_id,
            )
        )
        event = self.event(entity_bindings=bindings)
        snapshot = self.snapshot(events=(event,))
        self.assertEqual(
            snapshot.events_for_entity(self.instrument_binding().binding_id),
            (event,),
        )

    def test_wrong_direct_constructor_types_fail_as_contract_errors(self) -> None:
        with self.assertRaisesRegex(EventContractError, "EventSourceStream"):
            self.snapshot(streams=(object(),))
        with self.assertRaisesRegex(EventContractError, "EventEntityBinding"):
            replace(self.event(), entity_bindings=(object(),))


if __name__ == "__main__":
    unittest.main()
