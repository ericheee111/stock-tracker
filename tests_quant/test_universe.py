from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from typing import cast

from _helpers import calendar_coverage, calendar_day, utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.calendar import CalendarStatus, TradingCalendar
from stock_tracker.quant.core.point_in_time import PITConflictError
from stock_tracker.quant.core.universe import (
    HistoricalUniverse,
    InstrumentIdentityFact,
    ListingState,
    RiskDesignation,
    SecurityStatusFact,
    SecurityType,
    TradingState,
    UniverseContractError,
    UniverseCoverage,
    UniverseMembershipFact,
    UniverseMembershipState,
    build_research_identity_snapshot,
)


UNIVERSE_ID = "A_ALL_COMMON_EQUITY"
SOURCE = "fixture-universe"
VERSION = "fixture-v1"
NOTE = "synthetic fixture only"


class UniverseFixtures(unittest.TestCase):
    session = date(2025, 1, 2)
    as_of = utc_datetime(2025, 1, 3)

    def coverage(self, **overrides: object) -> UniverseCoverage:
        values: dict[str, object] = {
            "universe_id": UNIVERSE_ID,
            "market": Market.A,
            "start_date": self.session,
            "end_date": self.session,
            "source": SOURCE,
            "universe_version": VERSION,
            "known_at": utc_datetime(2025, 1, 1),
            "usable_from": utc_datetime(2025, 1, 1),
            "revision": 1,
            "verified": True,
            "complete": True,
            "source_note": NOTE,
        }
        values.update(overrides)
        return UniverseCoverage(**values)

    def instrument_id(self, symbol: str) -> str:
        exchange = "SSE" if symbol.endswith(".SH") else "SZSE"
        return f"{exchange}:fixture:{symbol.split('.')[0]}"

    def identity(self, symbol: str, **overrides: object) -> InstrumentIdentityFact:
        values: dict[str, object] = {
            "instrument_id": self.instrument_id(symbol),
            "symbol": symbol,
            "market": Market.A,
            "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
            "security_type": SecurityType.COMMON_EQUITY,
            "effective_from": date(2000, 1, 1),
            "effective_to": None,
            "known_at": utc_datetime(2025, 1, 1),
            "usable_from": utc_datetime(2025, 1, 1),
            "source": "fixture-security-master",
            "revision": 1,
            "verified": True,
            "source_note": NOTE,
        }
        values.update(overrides)
        return InstrumentIdentityFact(**values)

    def status(self, symbol: str, **overrides: object) -> SecurityStatusFact:
        values: dict[str, object] = {
            "instrument_id": self.instrument_id(symbol),
            "symbol": symbol,
            "market": Market.A,
            "session_date": self.session,
            "listing_state": ListingState.LISTED,
            "trading_state": TradingState.TRADABLE,
            "risk_designation": RiskDesignation.NORMAL,
            "known_at": utc_datetime(2025, 1, 2),
            "usable_from": utc_datetime(2025, 1, 2),
            "source": "fixture-security-status",
            "revision": 1,
            "verified": True,
            "source_note": NOTE,
        }
        values.update(overrides)
        return SecurityStatusFact(**values)

    def membership(
        self,
        symbol: str,
        state: UniverseMembershipState = UniverseMembershipState.INCLUDED,
        **overrides: object,
    ) -> UniverseMembershipFact:
        values: dict[str, object] = {
            "universe_id": UNIVERSE_ID,
            "instrument_id": self.instrument_id(symbol),
            "symbol": symbol,
            "market": Market.A,
            "effective_date": date(2000, 1, 1),
            "state": state,
            "known_at": utc_datetime(2025, 1, 1),
            "usable_from": utc_datetime(2025, 1, 1),
            "source": SOURCE,
            "universe_version": VERSION,
            "revision": 1,
            "verified": True,
            "reason": "fixture membership",
            "source_note": NOTE,
        }
        values.update(overrides)
        return UniverseMembershipFact(**values)

    def complete_records(self):
        symbols = ("600000.SH", "000001.SZ", "600001.SH")
        memberships = (
            self.membership(symbols[0]),
            self.membership(symbols[1]),
            self.membership(
                symbols[2],
                UniverseMembershipState.EXCLUDED,
                effective_date=self.session,
                reason="delisted sample retained",
            ),
        )
        identities = (
            self.identity(symbols[0]),
            self.identity(symbols[1]),
            self.identity(symbols[2], effective_to=self.session),
        )
        statuses = (
            self.status(symbols[0]),
            self.status(symbols[1], trading_state=TradingState.SUSPENDED),
            self.status(
                symbols[2],
                listing_state=ListingState.DELISTED,
                trading_state=TradingState.HALTED,
                risk_designation=RiskDesignation.RISK_WARNING,
            ),
        )
        return identities, statuses, memberships

    def universe(self, *, reverse: bool = False) -> HistoricalUniverse:
        identities, statuses, memberships = self.complete_records()
        if reverse:
            identities = tuple(reversed(identities))
            statuses = tuple(reversed(statuses))
            memberships = tuple(reversed(memberships))
        return HistoricalUniverse(
            (self.coverage(),),
            identities,
            statuses,
            memberships,
        )


class TestUniverseFactContracts(UniverseFixtures):
    def test_symbol_suffix_must_match_market(self) -> None:
        with self.assertRaisesRegex(UniverseContractError, "suffix"):
            self.identity("AAPL.US", market=Market.A)

    def test_usable_from_cannot_precede_known_at(self) -> None:
        with self.assertRaisesRegex(UniverseContractError, "usable_from"):
            self.membership(
                "600000.SH",
                known_at=utc_datetime(2025, 1, 2),
                usable_from=utc_datetime(2025, 1, 1),
            )

    def test_delisted_security_cannot_be_tradable(self) -> None:
        with self.assertRaisesRegex(UniverseContractError, "cannot be TRADABLE"):
            self.status(
                "600000.SH",
                listing_state=ListingState.DELISTED,
                trading_state=TradingState.TRADABLE,
            )

    def test_safety_booleans_are_strict(self) -> None:
        for record, field, value in (
            (self.coverage(), "complete", 1),
            (self.coverage(), "verified", "true"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                UniverseContractError,
                "must be a boolean",
            ):
                replace(record, **{field: cast(bool, value)})


class TestHistoricalUniverse(UniverseFixtures):
    def test_snapshot_preserves_suspension_and_delisted_samples(self) -> None:
        snapshot = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        self.assertEqual(snapshot.member_symbols, ("000001.SZ", "600000.SH"))
        self.assertEqual(snapshot.tradable_symbols, ("600000.SH",))
        self.assertEqual(snapshot.delisted_symbols, ("600001.SH",))
        self.assertEqual(len(snapshot.snapshot_id), 64)
        self.assertEqual(len(snapshot.security_status_snapshot_id), 64)

    def test_input_order_does_not_change_snapshot_identity(self) -> None:
        first = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        second = self.universe(reverse=True).snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(
            first.security_status_snapshot_id,
            second.security_status_snapshot_id,
        )

    def test_missing_status_fails_closed(self) -> None:
        identities, statuses, memberships = self.complete_records()
        universe = HistoricalUniverse(
            (self.coverage(),),
            identities,
            statuses[:-1],
            memberships,
        )
        with self.assertRaisesRegex(UniverseContractError, "lacks visible security status"):
            universe.snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )

    def test_missing_identity_fails_closed(self) -> None:
        identities, statuses, memberships = self.complete_records()
        universe = HistoricalUniverse(
            (self.coverage(),),
            identities[:-1],
            statuses,
            memberships,
        )
        with self.assertRaisesRegex(UniverseContractError, "lacks visible instrument identity"):
            universe.snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )

    def test_future_membership_revision_is_not_visible(self) -> None:
        identities, statuses, memberships = self.complete_records()
        future = replace(
            memberships[0],
            state=UniverseMembershipState.EXCLUDED,
            known_at=utc_datetime(2025, 1, 4),
            usable_from=utc_datetime(2025, 1, 4),
            revision=2,
            reason="future correction",
        )
        snapshot = HistoricalUniverse(
            (self.coverage(),),
            identities,
            statuses,
            (*memberships, future),
        ).snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        self.assertIn("600000.SH", snapshot.member_symbols)

    def test_conflicting_latest_revision_fails(self) -> None:
        identities, statuses, memberships = self.complete_records()
        conflict = replace(
            memberships[0],
            state=UniverseMembershipState.EXCLUDED,
            reason="conflicting payload",
        )
        with self.assertRaises(PITConflictError):
            HistoricalUniverse(
                (self.coverage(),),
                identities,
                statuses,
                (*memberships, conflict),
            ).snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )

    def test_multiple_universe_versions_fail(self) -> None:
        identities, statuses, memberships = self.complete_records()
        other_coverage = self.coverage(universe_version="fixture-v2")
        other_memberships = tuple(
            replace(item, universe_version="fixture-v2") for item in memberships
        )
        with self.assertRaisesRegex(UniverseContractError, "multiple"):
            HistoricalUniverse(
                (self.coverage(), other_coverage),
                identities,
                statuses,
                (*memberships, *other_memberships),
            ).snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )

    def test_incomplete_coverage_requires_explicit_opt_out(self) -> None:
        identities, statuses, memberships = self.complete_records()
        universe = HistoricalUniverse(
            (self.coverage(complete=False),),
            identities,
            statuses,
            memberships,
        )
        with self.assertRaisesRegex(UniverseContractError, "complete universe coverage"):
            universe.snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )
        snapshot = universe.snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
            require_complete=False,
        )
        self.assertEqual(snapshot.member_symbols, ("000001.SZ", "600000.SH"))

    def test_included_delisted_member_is_rejected(self) -> None:
        identities, statuses, memberships = self.complete_records()
        invalid_memberships = (
            memberships[0],
            memberships[1],
            replace(
                memberships[2],
                state=UniverseMembershipState.INCLUDED,
                reason="invalid inclusion",
            ),
        )
        with self.assertRaisesRegex(UniverseContractError, "cannot be PRE_LISTING or DELISTED"):
            HistoricalUniverse(
                (self.coverage(),),
                identities,
                statuses,
                invalid_memberships,
            ).snapshot(
                UNIVERSE_ID,
                Market.A,
                self.session,
                self.as_of,
            )

    def test_snapshot_ids_cannot_be_relabelled(self) -> None:
        snapshot = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        with self.assertRaisesRegex(UniverseContractError, "snapshot_id"):
            replace(snapshot, snapshot_id="a" * 64)
        with self.assertRaisesRegex(
            UniverseContractError,
            "security_status_snapshot_id",
        ):
            replace(snapshot, security_status_snapshot_id="b" * 64)

    def test_symbol_change_keeps_one_stable_instrument_identity(self) -> None:
        old_symbol = "600000.SH"
        new_symbol = "600010.SH"
        instrument_id = self.instrument_id(old_symbol)
        coverage = self.coverage()
        membership = self.membership(
            new_symbol,
            instrument_id=instrument_id,
            effective_date=self.session,
        )
        identity = self.identity(
            new_symbol,
            instrument_id=instrument_id,
            effective_from=self.session,
        )
        status = self.status(new_symbol, instrument_id=instrument_id)
        snapshot = HistoricalUniverse(
            (coverage,),
            (identity,),
            (status,),
            (membership,),
        ).snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        self.assertEqual(snapshot.member_symbols, (new_symbol,))
        self.assertEqual(snapshot.memberships[0].instrument_id, instrument_id)

    def test_delisted_old_instrument_and_reused_symbol_can_coexist(self) -> None:
        old_id = "SSE:fixture:OLD-600000"
        new_id = "SSE:fixture:NEW-600000"
        target = date(2025, 1, 4)
        as_of = utc_datetime(2025, 1, 5)
        coverage = self.coverage(
            start_date=self.session,
            end_date=target,
            known_at=utc_datetime(2025, 1, 1),
            usable_from=utc_datetime(2025, 1, 1),
        )
        old_identity = self.identity(
            "600000.SH",
            instrument_id=old_id,
            effective_from=date(2020, 1, 1),
            effective_to=self.session,
        )
        new_identity = self.identity(
            "600000.SH",
            instrument_id=new_id,
            effective_from=date(2025, 1, 3),
            known_at=utc_datetime(2025, 1, 3),
            usable_from=utc_datetime(2025, 1, 3),
        )
        old_status = self.status(
            "600000.SH",
            instrument_id=old_id,
            listing_state=ListingState.DELISTED,
            trading_state=TradingState.HALTED,
            risk_designation=RiskDesignation.RISK_WARNING,
        )
        new_status = self.status(
            "600000.SH",
            instrument_id=new_id,
            session_date=target,
            known_at=utc_datetime(2025, 1, 4),
            usable_from=utc_datetime(2025, 1, 4),
        )
        old_included = self.membership(
            "600000.SH",
            instrument_id=old_id,
            effective_date=date(2020, 1, 1),
        )
        old_excluded = self.membership(
            "600000.SH",
            UniverseMembershipState.EXCLUDED,
            instrument_id=old_id,
            effective_date=self.session,
            known_at=utc_datetime(2025, 1, 2),
            usable_from=utc_datetime(2025, 1, 2),
            reason="DELISTED",
        )
        new_included = self.membership(
            "600000.SH",
            instrument_id=new_id,
            effective_date=date(2025, 1, 3),
            known_at=utc_datetime(2025, 1, 3),
            usable_from=utc_datetime(2025, 1, 3),
            reason="LISTED",
        )
        snapshot = HistoricalUniverse(
            (coverage,),
            (old_identity, new_identity),
            (old_status, new_status),
            (old_included, old_excluded, new_included),
        ).snapshot(
            UNIVERSE_ID,
            Market.A,
            target,
            as_of,
        )
        self.assertEqual(snapshot.member_symbols, ("600000.SH",))
        self.assertEqual(snapshot.tradable_symbols, ("600000.SH",))
        self.assertEqual(snapshot.delisted_instrument_ids, (old_id,))
        self.assertEqual(
            {item.instrument_id for item in snapshot.identities},
            {old_id, new_id},
        )
        old_snapshot_status = next(
            item for item in snapshot.statuses if item.instrument_id == old_id
        )
        self.assertEqual(old_snapshot_status.session_date, self.session)

    def test_latest_effective_membership_controls_session(self) -> None:
        identities, statuses, memberships = self.complete_records()
        exit_event = self.membership(
            "600000.SH",
            UniverseMembershipState.EXCLUDED,
            effective_date=self.session,
            known_at=utc_datetime(2025, 1, 2),
            usable_from=utc_datetime(2025, 1, 2),
            reason="removed on target session",
        )
        snapshot = HistoricalUniverse(
            (self.coverage(),),
            identities,
            statuses,
            (*memberships, exit_event),
        ).snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        self.assertNotIn("600000.SH", snapshot.member_symbols)


class TestResearchIdentitySnapshot(UniverseFixtures):
    def test_binds_open_calendar_and_complete_universe(self) -> None:
        calendar = TradingCalendar(
            (calendar_coverage(self.session, self.session),),
            (calendar_day(self.session, status=CalendarStatus.OPEN),),
        ).snapshot(
            Market.A,
            self.session,
            self.session,
            self.as_of,
        )
        universe = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        identity = build_research_identity_snapshot(calendar, universe)
        self.assertEqual(identity.calendar_snapshot_id, calendar.snapshot_id)
        self.assertEqual(identity.universe_snapshot_id, universe.snapshot_id)
        self.assertEqual(identity.member_symbols, universe.member_symbols)
        self.assertEqual(len(identity.snapshot_id), 64)

    def test_incomplete_universe_cannot_be_promoted_to_research_identity(self) -> None:
        identities, statuses, memberships = self.complete_records()
        universe = HistoricalUniverse(
            (self.coverage(complete=False),),
            identities,
            statuses,
            memberships,
        ).snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
            require_complete=False,
        )
        calendar = TradingCalendar(
            (calendar_coverage(self.session, self.session),),
            (calendar_day(self.session, status=CalendarStatus.OPEN),),
        ).snapshot(
            Market.A,
            self.session,
            self.session,
            self.as_of,
        )
        with self.assertRaisesRegex(UniverseContractError, "verified, complete"):
            build_research_identity_snapshot(calendar, universe)

    def test_unverified_calendar_cannot_be_promoted_to_research_identity(self) -> None:
        calendar = TradingCalendar(
            (
                calendar_coverage(
                    self.session,
                    self.session,
                    verified=False,
                ),
            ),
            (
                calendar_day(
                    self.session,
                    status=CalendarStatus.OPEN,
                    verified=False,
                ),
            ),
        ).snapshot(
            Market.A,
            self.session,
            self.session,
            self.as_of,
            require_verified=False,
        )
        universe = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        with self.assertRaisesRegex(UniverseContractError, "verified calendar"):
            build_research_identity_snapshot(calendar, universe)

    def test_closed_calendar_session_is_rejected(self) -> None:
        calendar = TradingCalendar(
            (calendar_coverage(self.session, self.session),),
            (calendar_day(self.session, status=CalendarStatus.CLOSED),),
        ).snapshot(
            Market.A,
            self.session,
            self.session,
            self.as_of,
        )
        universe = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        with self.assertRaisesRegex(UniverseContractError, "OPEN"):
            build_research_identity_snapshot(calendar, universe)

    def test_calendar_and_universe_as_of_must_match(self) -> None:
        calendar = TradingCalendar(
            (calendar_coverage(self.session, self.session),),
            (calendar_day(self.session, status=CalendarStatus.OPEN),),
        ).snapshot(
            Market.A,
            self.session,
            self.session,
            utc_datetime(2025, 1, 4),
        )
        universe = self.universe().snapshot(
            UNIVERSE_ID,
            Market.A,
            self.session,
            self.as_of,
        )
        with self.assertRaisesRegex(UniverseContractError, "as_of"):
            build_research_identity_snapshot(calendar, universe)


if __name__ == "__main__":
    unittest.main()
