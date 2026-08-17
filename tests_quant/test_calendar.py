from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import cast

from _helpers import (
    calendar_coverage,
    calendar_day,
    make_bar,
    utc_datetime,
)

from stock_tracker.core.types import Market
from stock_tracker.quant.core.calendar import (
    CalendarAlignedBars,
    CalendarContractError,
    CalendarDay,
    CalendarStatus,
    InstrumentSessionState,
    InstrumentSessionStatus,
    SessionKind,
    TradingCalendar,
)
from stock_tracker.quant.core.point_in_time import PITConflictError


class TestCalendarDayContract(unittest.TestCase):
    def test_open_day_requires_exchange_timezone(self) -> None:
        session = date(2025, 1, 2)
        with self.assertRaises(CalendarContractError):
            CalendarDay(
                market=Market.A,
                session_date=session,
                status=CalendarStatus.OPEN,
                open_time=datetime(2025, 1, 2, 9, 30, tzinfo=timezone.utc),
                close_time=datetime(2025, 1, 2, 15, 0, tzinfo=timezone.utc),
                session_kind=SessionKind.REGULAR,
                known_at=utc_datetime(2025, 1, 1),
                source="fixture-calendar",
                revision=1,
                calendar_version="fixture-v1",
                verified=True,
                source_note="fixture",
            )

    def test_closed_day_cannot_have_times(self) -> None:
        original = calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN)
        with self.assertRaises(CalendarContractError):
            replace(original, status=CalendarStatus.CLOSED)

    def test_verified_day_requires_source_note(self) -> None:
        original = calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN)
        with self.assertRaises(CalendarContractError):
            replace(original, source_note="")

    def test_verified_fields_require_real_booleans(self) -> None:
        day = calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN)
        coverage = calendar_coverage(date(2025, 1, 2), date(2025, 1, 2))
        status = InstrumentSessionStatus(
            symbol="600000.SH",
            market=Market.A,
            session_date=date(2025, 1, 2),
            status=InstrumentSessionState.SUSPENDED,
            known_at=utc_datetime(2025, 1, 2),
            source="fixture-status",
            revision=1,
            reference_price=10.0,
            share_factor=1.0,
            verified=True,
            source_note="synthetic fixture only",
        )
        for record, invalid in (
            (day, "true"),
            (coverage, 1),
            (status, "false"),
        ):
            with self.subTest(record=type(record).__name__), self.assertRaisesRegex(
                CalendarContractError,
                "verified must be a boolean",
            ):
                replace(record, verified=cast(bool, invalid))

    def test_usable_from_defaults_to_known_at(self) -> None:
        day = calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN)
        coverage = calendar_coverage(date(2025, 1, 2), date(2025, 1, 2))
        status = InstrumentSessionStatus(
            symbol="600000.SH",
            market=Market.A,
            session_date=date(2025, 1, 2),
            status=InstrumentSessionState.SUSPENDED,
            known_at=utc_datetime(2025, 1, 2),
            source="fixture-status",
            revision=1,
            reference_price=10.0,
            share_factor=1.0,
            verified=True,
            source_note="synthetic fixture only",
        )
        self.assertEqual(day.usable_from, day.known_at)
        self.assertEqual(coverage.usable_from, coverage.known_at)
        self.assertEqual(status.usable_from, status.known_at)

    def test_usable_from_cannot_precede_known_at(self) -> None:
        original = calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN)
        with self.assertRaisesRegex(CalendarContractError, "usable_from"):
            replace(
                original,
                known_at=utc_datetime(2025, 1, 2),
                usable_from=utc_datetime(2025, 1, 1),
            )


class TestTradingCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.start = date(2025, 1, 2)
        self.end = date(2025, 1, 6)
        self.coverage = calendar_coverage(self.start, self.end)
        self.days = (
            calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN),
            calendar_day(date(2025, 1, 3), status=CalendarStatus.CLOSED),
            calendar_day(date(2025, 1, 4), status=CalendarStatus.CLOSED),
            calendar_day(date(2025, 1, 5), status=CalendarStatus.OPEN),
            calendar_day(date(2025, 1, 6), status=CalendarStatus.OPEN),
        )

    def test_snapshot_gate_requires_real_boolean(self) -> None:
        with self.assertRaisesRegex(
            CalendarContractError,
            "require_verified must be a boolean",
        ):
            TradingCalendar((self.coverage,), self.days).snapshot(
                Market.A,
                self.start,
                self.end,
                utc_datetime(2025, 1, 7),
                require_verified=cast(bool, 0),
            )

    def test_snapshot_requires_every_civil_date(self) -> None:
        calendar = TradingCalendar((self.coverage,), self.days[:-1])
        with self.assertRaisesRegex(CalendarContractError, "incomplete"):
            calendar.snapshot(
                Market.A,
                self.start,
                self.end,
                utc_datetime(2025, 1, 7),
            )

    def test_future_revision_not_visible(self) -> None:
        revised = replace(
            self.days[0],
            status=CalendarStatus.CLOSED,
            open_time=None,
            close_time=None,
            known_at=utc_datetime(2025, 1, 10),
            usable_from=utc_datetime(2025, 1, 10),
            revision=2,
        )
        calendar = TradingCalendar((self.coverage,), (*self.days, revised))
        snapshot = calendar.snapshot(
            Market.A,
            self.start,
            self.end,
            utc_datetime(2025, 1, 7),
        )
        self.assertEqual(snapshot.days[0].status, CalendarStatus.OPEN)

    def test_known_but_not_yet_usable_revision_is_not_visible(self) -> None:
        delayed = replace(
            self.days[0],
            status=CalendarStatus.CLOSED,
            open_time=None,
            close_time=None,
            known_at=utc_datetime(2025, 1, 2),
            usable_from=utc_datetime(2025, 1, 10),
            revision=2,
        )
        snapshot = TradingCalendar(
            (self.coverage,),
            (*self.days, delayed),
        ).snapshot(
            Market.A,
            self.start,
            self.end,
            utc_datetime(2025, 1, 7),
        )
        self.assertEqual(snapshot.days[0].status, CalendarStatus.OPEN)

    def test_conflicting_latest_revision_fails(self) -> None:
        conflict = replace(
            self.days[0],
            status=CalendarStatus.CLOSED,
            open_time=None,
            close_time=None,
        )
        calendar = TradingCalendar((self.coverage,), (*self.days, conflict))
        with self.assertRaises(PITConflictError):
            calendar.snapshot(
                Market.A,
                self.start,
                self.end,
                utc_datetime(2025, 1, 7),
            )

    def test_multiple_calendar_versions_fail(self) -> None:
        other_coverage = calendar_coverage(
            self.start,
            self.end,
            version="fixture-v2",
        )
        other_days = tuple(replace(day, calendar_version="fixture-v2") for day in self.days)
        calendar = TradingCalendar(
            (self.coverage, other_coverage),
            (*self.days, *other_days),
        )
        with self.assertRaisesRegex(CalendarContractError, "multiple"):
            calendar.snapshot(
                Market.A,
                self.start,
                self.end,
                utc_datetime(2025, 1, 7),
            )

    def test_calendar_snapshot_id_cannot_be_relabelled(self) -> None:
        snapshot = TradingCalendar((self.coverage,), self.days).snapshot(
            Market.A,
            self.start,
            self.end,
            utc_datetime(2025, 1, 7),
        )
        with self.assertRaisesRegex(CalendarContractError, "snapshot_id"):
            replace(snapshot, snapshot_id="a" * 64)

    def test_advance_and_distance_count_open_sessions(self) -> None:
        snapshot = TradingCalendar((self.coverage,), self.days).snapshot(
            Market.A,
            self.start,
            self.end,
            utc_datetime(2025, 1, 7),
        )
        self.assertEqual(snapshot.advance(date(2025, 1, 2), 1), date(2025, 1, 5))
        self.assertEqual(snapshot.session_distance(date(2025, 1, 2), date(2025, 1, 6)), 2)

    def test_missing_bar_requires_explicit_status(self) -> None:
        bars = (
            make_bar(date(2025, 1, 2)),
            make_bar(date(2025, 1, 6), open_price=11.2, high=11.5, low=11.0, close=11.3),
        )
        calendar = TradingCalendar((self.coverage,), self.days)
        with self.assertRaisesRegex(CalendarContractError, "no verified explanation"):
            calendar.align_bars(
                symbol="600000.SH",
                market=Market.A,
                bars=bars,
                start=self.start,
                end=self.end,
                as_of=utc_datetime(2025, 1, 7),
            )

    def test_suspension_creates_nonobservable_placeholder(self) -> None:
        bars = (
            make_bar(date(2025, 1, 2)),
            make_bar(date(2025, 1, 6), open_price=11.2, high=11.5, low=11.0, close=11.3),
        )
        status = InstrumentSessionStatus(
            symbol="600000.SH",
            market=Market.A,
            session_date=date(2025, 1, 5),
            status=InstrumentSessionState.SUSPENDED,
            known_at=utc_datetime(2025, 1, 5),
            source="fixture-status",
            revision=1,
            reference_price=10.0,
            share_factor=1.0,
            verified=True,
            source_note="synthetic fixture only",
        )
        aligned = TradingCalendar(
            (self.coverage,),
            self.days,
            (status,),
        ).align_bars(
            symbol="600000.SH",
            market=Market.A,
            bars=bars,
            start=self.start,
            end=self.end,
            as_of=utc_datetime(2025, 1, 7),
        )
        self.assertEqual(aligned.session_dates, (date(2025, 1, 2), date(2025, 1, 5), date(2025, 1, 6)))
        self.assertEqual(aligned.placeholder_dates, (date(2025, 1, 5),))
        self.assertEqual(aligned.bars[1].volume, 0)
        self.assertFalse(aligned.is_observable(1))

    def test_unverified_status_is_not_accepted(self) -> None:
        status = InstrumentSessionStatus(
            symbol="600000.SH",
            market=Market.A,
            session_date=date(2025, 1, 5),
            status=InstrumentSessionState.SUSPENDED,
            known_at=utc_datetime(2025, 1, 5),
            source="fixture-status",
            revision=1,
            reference_price=10.0,
            share_factor=1.0,
            verified=False,
            source_note="",
        )
        calendar = TradingCalendar((self.coverage,), self.days, (status,))
        with self.assertRaises(CalendarContractError):
            calendar.align_bars(
                symbol="600000.SH",
                market=Market.A,
                bars=(make_bar(date(2025, 1, 2)), make_bar(date(2025, 1, 6))),
                start=self.start,
                end=self.end,
                as_of=utc_datetime(2025, 1, 7),
            )

    def test_manual_alignment_rejects_duplicate_sessions(self) -> None:
        bar = make_bar(date(2025, 1, 2))
        with self.assertRaisesRegex(CalendarContractError, "unique"):
            CalendarAlignedBars(
                symbol=bar.symbol,
                market=bar.market,
                bars=(bar, bar),
                session_dates=(date(2025, 1, 2), date(2025, 1, 2)),
                session_states=(InstrumentSessionState.OPEN, InstrumentSessionState.OPEN),
                placeholder_dates=(),
                calendar_as_of=utc_datetime(2025, 1, 7),
                calendar_snapshot_id="a" * 64,
                calendar_version="fixture-v1",
                instrument_status_snapshot_id="b" * 64,
                coverage_start=date(2025, 1, 2),
                coverage_end=date(2025, 1, 2),
            )


if __name__ == "__main__":
    unittest.main()
