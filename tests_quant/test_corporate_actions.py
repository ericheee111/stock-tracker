from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from _helpers import utc_datetime

from stock_tracker.core.types import Bar, Market
from stock_tracker.quant.core.corporate_actions import (
    AdjustedMarketDataView,
    AdjustmentBasis,
    AdjustmentConvention,
    CorporateActionBook,
    CorporateActionComponent,
    CorporateActionContractError,
    CorporateActionCoverage,
    CorporateActionFact,
    CorporateActionLifecycle,
    CorporateActionSnapshot,
    bind_adjusted_market_data_view,
    build_adjustment_series,
)
from stock_tracker.quant.core.universe import (
    InstrumentIdentityFact,
    SecurityType,
)

INSTRUMENT_ID = "CN:SSE:fixture-security-1"
SYMBOL = "600000.SH"
SOURCE = "fixture-corporate-actions"
VERSION = "fixture-corporate-actions-v1"
IDENTITY_KNOWN_AT = utc_datetime(2024, 12, 1)
COVERAGE_KNOWN_AT = utc_datetime(2025, 1, 31)
ACTION_KNOWN_AT = utc_datetime(2025, 1, 14)
ACTION_USABLE_FROM = utc_datetime(2025, 1, 15)
CANCELLATION_KNOWN_AT = utc_datetime(2025, 1, 16)
DEFAULT_EX_DATE = date(2025, 1, 15)
REFERENCE_PRICE_SNAPSHOT_ID = "a" * 64
AS_OF = utc_datetime(2025, 2, 1)
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


class CorporateActionFixtures(unittest.TestCase):
    def identity(
        self,
        *,
        instrument_id: str = INSTRUMENT_ID,
        symbol: str = SYMBOL,
        known_at=IDENTITY_KNOWN_AT,
        verified: bool = True,
    ) -> InstrumentIdentityFact:
        return InstrumentIdentityFact(
            instrument_id=instrument_id,
            symbol=symbol,
            market=Market.A,
            exchange="SSE",
            security_type=SecurityType.COMMON_EQUITY,
            effective_from=date(2020, 1, 1),
            effective_to=None,
            known_at=known_at,
            usable_from=known_at,
            source="fixture-identity",
            revision="identity-r1",
            verified=verified,
            source_note="synthetic fixture identity",
        )

    def coverage(
        self,
        *,
        known_at=COVERAGE_KNOWN_AT,
        revision: int | str = "coverage-r1",
        supersedes_revision: int | str | None = None,
        verified: bool = True,
        complete: bool = True,
        source: str = SOURCE,
        action_version: str = VERSION,
    ) -> CorporateActionCoverage:
        return CorporateActionCoverage(
            instrument_id=INSTRUMENT_ID,
            market=Market.A,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            source=source,
            action_version=action_version,
            known_at=known_at,
            usable_from=known_at,
            revision=revision,
            supersedes_revision=supersedes_revision,
            verified=verified,
            complete=complete,
            source_note="synthetic complete action coverage",
        )

    def action(
        self,
        *,
        action_id: str = "action-plan-1",
        identity: InstrumentIdentityFact | None = None,
        ex_date: date = DEFAULT_EX_DATE,
        share_listing_date: date | None = None,
        lifecycle: CorporateActionLifecycle = CorporateActionLifecycle.EFFECTIVE,
        automatic_share_ratio: Decimal | None = Decimal(2),
        cash_dividend_per_share: Decimal | None = Decimal(0),
        rights_entitlement_ratio: Decimal | None = Decimal(0),
        rights_subscription_price: Decimal | None = None,
        currency: str | None = None,
        reference_price: Decimal | None = None,
        reference_price_snapshot_id: str | None = None,
        known_at=ACTION_KNOWN_AT,
        usable_from=ACTION_USABLE_FROM,
        revision: int | str = "action-r1",
        supersedes_revision: int | str | None = None,
        verified: bool = True,
        source: str = SOURCE,
        action_version: str = VERSION,
    ) -> CorporateActionFact:
        bound_identity = identity or self.identity()
        bound_share_listing_date = (
            share_listing_date
            if share_listing_date is not None
            else (
                ex_date
                if automatic_share_ratio is not None
                and automatic_share_ratio != Decimal(1)
                else None
            )
        )
        bound_reference_snapshot_id = (
            REFERENCE_PRICE_SNAPSHOT_ID
            if reference_price is not None
            and reference_price_snapshot_id is None
            else reference_price_snapshot_id
        )
        return CorporateActionFact(
            action_id=action_id,
            instrument_id=bound_identity.instrument_id,
            identity_fact_id=bound_identity.fact_id,
            symbol=bound_identity.symbol,
            market=bound_identity.market,
            ex_date=ex_date,
            record_date=date(2025, 1, 14),
            payment_date=date(2025, 1, 20),
            share_listing_date=bound_share_listing_date,
            lifecycle=lifecycle,
            automatic_share_ratio=automatic_share_ratio,
            cash_dividend_per_share=cash_dividend_per_share,
            rights_entitlement_ratio=rights_entitlement_ratio,
            rights_subscription_price=rights_subscription_price,
            currency=currency,
            reference_price=reference_price,
            reference_price_snapshot_id=bound_reference_snapshot_id,
            known_at=known_at,
            usable_from=usable_from,
            source=source,
            action_version=action_version,
            revision=revision,
            supersedes_revision=supersedes_revision,
            verified=verified,
            source_note="synthetic corporate action",
        )

    def cancelled(
        self,
        previous: CorporateActionFact,
        *,
        known_at=CANCELLATION_KNOWN_AT,
        revision: int | str = "action-r2",
    ) -> CorporateActionFact:
        return CorporateActionFact(
            action_id=previous.action_id,
            instrument_id=previous.instrument_id,
            identity_fact_id=previous.identity_fact_id,
            symbol=previous.symbol,
            market=previous.market,
            ex_date=previous.ex_date,
            record_date=previous.record_date,
            payment_date=previous.payment_date,
            share_listing_date=previous.share_listing_date,
            lifecycle=CorporateActionLifecycle.CANCELLED,
            automatic_share_ratio=None,
            cash_dividend_per_share=None,
            rights_entitlement_ratio=None,
            rights_subscription_price=None,
            currency=None,
            reference_price=None,
            reference_price_snapshot_id=None,
            known_at=known_at,
            usable_from=known_at,
            source=previous.source,
            action_version=previous.action_version,
            revision=revision,
            supersedes_revision=previous.revision,
            verified=True,
            source_note="synthetic cancellation",
        )

    def snapshot(
        self,
        actions: tuple[CorporateActionFact, ...] = (),
        *,
        identities: tuple[InstrumentIdentityFact, ...] | None = None,
        coverages: tuple[CorporateActionCoverage, ...] | None = None,
        as_of=AS_OF,
        require_verified: bool = True,
        require_complete: bool = True,
    ) -> CorporateActionSnapshot:
        if identities is None:
            unique = {item.identity_fact_id for item in actions}
            base_identity = self.identity()
            identities = (base_identity,) if base_identity.fact_id in unique else ()
        book = CorporateActionBook(
            coverages or (self.coverage(),),
            actions,
            identities,
        )
        return book.snapshot(
            INSTRUMENT_ID,
            Market.A,
            date(2025, 1, 1),
            date(2025, 1, 31),
            as_of,
            require_verified=require_verified,
            require_complete=require_complete,
        )


class TestCorporateActionTerms(CorporateActionFixtures):
    def test_split_terms_use_exact_decimal_and_components(self) -> None:
        action = self.action(automatic_share_ratio=Decimal("2.00"))
        self.assertEqual(
            action.components,
            (CorporateActionComponent.AUTOMATIC_SHARE_CHANGE,),
        )
        self.assertEqual(
            action.backward_price_multiplier(AdjustmentBasis.SHARE_CHANGE_ONLY),
            Decimal("0.5"),
        )
        self.assertEqual(action.automatic_position_multiplier, Decimal("2.00"))

    def test_float_integer_boolean_and_nonfinite_terms_are_rejected(self) -> None:
        for value in (2.0, 2, True, Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(
                CorporateActionContractError
            ):
                self.action(automatic_share_ratio=value)  # type: ignore[arg-type]

    def test_noop_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(CorporateActionContractError, "no-op"):
            self.action(
                automatic_share_ratio=Decimal(1),
                cash_dividend_per_share=Decimal(0),
                rights_entitlement_ratio=Decimal(0),
            )

    def test_rights_issue_requires_price_and_currency(self) -> None:
        with self.assertRaisesRegex(
            CorporateActionContractError,
            "rights_subscription_price",
        ):
            self.action(
                automatic_share_ratio=Decimal(1),
                rights_entitlement_ratio=Decimal("0.1"),
            )
        with self.assertRaisesRegex(CorporateActionContractError, "currency"):
            self.action(
                automatic_share_ratio=Decimal(1),
                rights_entitlement_ratio=Decimal("0.1"),
                rights_subscription_price=Decimal(5),
            )

    def test_cancelled_revision_cannot_carry_terms(self) -> None:
        with self.assertRaisesRegex(CorporateActionContractError, "CANCELLED"):
            self.action(lifecycle=CorporateActionLifecycle.CANCELLED)

    def test_total_return_cash_or_rights_requires_reference_price(self) -> None:
        cash = self.action(
            automatic_share_ratio=Decimal(1),
            cash_dividend_per_share=Decimal(1),
            currency="CNY",
            reference_price=None,
        )
        with self.assertRaisesRegex(CorporateActionContractError, "reference_price"):
            cash.backward_price_multiplier(AdjustmentBasis.TOTAL_RETURN)
        self.assertEqual(
            cash.backward_price_multiplier(AdjustmentBasis.SHARE_CHANGE_ONLY),
            Decimal(1),
        )

    def test_combined_cash_share_and_rights_formula_is_deterministic(self) -> None:
        action = self.action(
            automatic_share_ratio=Decimal("1.2"),
            cash_dividend_per_share=Decimal(1),
            rights_entitlement_ratio=Decimal("0.1"),
            rights_subscription_price=Decimal(5),
            currency="CNY",
            reference_price=Decimal(10),
        )
        with localcontext(_DECIMAL_CONTEXT):
            expected = +(Decimal("9.5") / Decimal(13))
        self.assertEqual(
            action.backward_price_multiplier(AdjustmentBasis.TOTAL_RETURN),
            expected,
        )
        self.assertEqual(
            action.components,
            (
                CorporateActionComponent.AUTOMATIC_SHARE_CHANGE,
                CorporateActionComponent.CASH_DIVIDEND,
                CorporateActionComponent.RIGHTS_ISSUE,
            ),
        )

    def test_reference_price_requires_bound_price_snapshot_identity(self) -> None:
        action = self.action(
            automatic_share_ratio=Decimal(1),
            cash_dividend_per_share=Decimal(1),
            currency="CNY",
            reference_price=Decimal(10),
        )
        self.assertEqual(
            action.reference_price_snapshot_id,
            REFERENCE_PRICE_SNAPSHOT_ID,
        )
        with self.assertRaisesRegex(
            CorporateActionContractError,
            "reference_price_snapshot_id",
        ):
            replace(action, reference_price_snapshot_id=None)
        with self.assertRaisesRegex(CorporateActionContractError, "lowercase SHA-256"):
            replace(action, reference_price_snapshot_id="R" * 64)

    def test_automatic_share_change_requires_share_listing_date(self) -> None:
        action = self.action()
        with self.assertRaisesRegex(CorporateActionContractError, "share_listing_date"):
            replace(action, share_listing_date=None)

    def test_visibility_and_safety_booleans_fail_closed(self) -> None:
        with self.assertRaisesRegex(CorporateActionContractError, "usable_from"):
            self.action(
                known_at=utc_datetime(2025, 1, 16),
                usable_from=utc_datetime(2025, 1, 15),
            )
        with self.assertRaisesRegex(CorporateActionContractError, "boolean"):
            replace(self.coverage(), complete=1)  # type: ignore[arg-type]


class TestCorporateActionSnapshots(CorporateActionFixtures):
    def test_complete_coverage_can_prove_no_actions(self) -> None:
        snapshot = self.snapshot()
        self.assertEqual(snapshot.actions, ())
        self.assertEqual(snapshot.identities, ())
        self.assertEqual(snapshot.effective_actions, ())
        self.assertEqual(len(snapshot.snapshot_id), 64)

    def test_missing_coverage_is_not_the_same_as_no_action(self) -> None:
        book = CorporateActionBook((), (), ())
        with self.assertRaisesRegex(CorporateActionContractError, "coverage"):
            book.snapshot(
                INSTRUMENT_ID,
                Market.A,
                date(2025, 1, 1),
                date(2025, 1, 31),
                AS_OF,
            )

    def test_incomplete_coverage_requires_explicit_debug_opt_out(self) -> None:
        incomplete = self.coverage(verified=False, complete=False)
        with self.assertRaisesRegex(CorporateActionContractError, "coverage"):
            self.snapshot(coverages=(incomplete,))
        debug = self.snapshot(
            coverages=(incomplete,),
            require_verified=False,
            require_complete=False,
        )
        with self.assertRaisesRegex(CorporateActionContractError, "verified and complete"):
            build_adjustment_series(
                debug,
                basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
                convention=AdjustmentConvention.BACKWARD,
            )

    def test_terminal_coverage_revision_can_remove_old_range_claim(self) -> None:
        original = self.coverage(revision="coverage-r1")
        narrowed = replace(
            original,
            start_date=date(2025, 1, 15),
            revision="coverage-r2",
            supersedes_revision="coverage-r1",
            known_at=utc_datetime(2025, 2, 1),
            usable_from=utc_datetime(2025, 2, 1),
        )
        with self.assertRaisesRegex(CorporateActionContractError, "coverage"):
            self.snapshot(coverages=(original, narrowed))

    def test_terminal_action_revision_moved_outside_range_removes_old_event(self) -> None:
        identity = self.identity()
        original = self.action(identity=identity, revision="action-r1")
        moved = replace(
            original,
            ex_date=date(2025, 2, 5),
            record_date=date(2025, 2, 4),
            payment_date=date(2025, 2, 10),
            share_listing_date=date(2025, 2, 5),
            revision="action-r2",
            supersedes_revision="action-r1",
            known_at=utc_datetime(2025, 2, 1),
            usable_from=utc_datetime(2025, 2, 1),
        )
        snapshot = self.snapshot(
            (original, moved),
            identities=(identity,),
        )
        self.assertEqual(snapshot.actions, ())
        self.assertEqual(snapshot.identities, ())

    def test_future_cancellation_does_not_rewrite_earlier_snapshot(self) -> None:
        identity = self.identity()
        effective = self.action(identity=identity)
        cancelled = self.cancelled(
            effective,
            known_at=utc_datetime(2025, 2, 2),
        )
        before = self.snapshot(
            (effective, cancelled),
            identities=(identity,),
            as_of=AS_OF,
        )
        after = self.snapshot(
            (effective, cancelled),
            identities=(identity,),
            as_of=utc_datetime(2025, 2, 3),
        )
        self.assertEqual(before.effective_actions, (effective,))
        self.assertEqual(after.effective_actions, ())
        self.assertEqual(after.cancelled_actions, (cancelled,))

    def test_revision_graph_not_lexical_order_selects_terminal(self) -> None:
        identity = self.identity()
        r2 = self.action(
            identity=identity,
            revision="r2",
            automatic_share_ratio=Decimal(2),
        )
        r10 = replace(
            r2,
            automatic_share_ratio=Decimal(3),
            known_at=r2.known_at,
            usable_from=r2.usable_from,
            revision="r10",
            supersedes_revision="r2",
        )
        snapshot = self.snapshot((r2, r10), identities=(identity,))
        self.assertEqual(snapshot.effective_actions[0].revision, "r10")
        self.assertEqual(
            snapshot.effective_actions[0].automatic_share_ratio,
            Decimal(3),
        )

    def test_cycle_and_disconnected_revision_root_fail_closed(self) -> None:
        identity = self.identity()
        base = self.action(identity=identity, revision="r1")
        cycle_a = replace(base, revision="r2", supersedes_revision="r3")
        cycle_b = replace(base, revision="r3", supersedes_revision="r2")
        with self.assertRaisesRegex(CorporateActionContractError, "cycle"):
            self.snapshot((base, cycle_a, cycle_b), identities=(identity,))
        disconnected = replace(
            base,
            revision="other-root",
            supersedes_revision=None,
        )
        with self.assertRaisesRegex(
            CorporateActionContractError,
            "multiple terminal revisions",
        ):
            self.snapshot((base, disconnected), identities=(identity,))

    def test_identity_binding_is_mandatory_and_active_on_ex_date(self) -> None:
        identity = self.identity()
        action = self.action(identity=identity)
        with self.assertRaisesRegex(CorporateActionContractError, "identity"):
            self.snapshot((action,), identities=())
        expired = replace(identity, effective_to=date(2025, 1, 10))
        rebound = replace(action, identity_fact_id=expired.fact_id)
        with self.assertRaisesRegex(CorporateActionContractError, "active on its ex_date"):
            self.snapshot((rebound,), identities=(expired,))

    def test_announced_past_action_cannot_silently_adjust_history(self) -> None:
        identity = self.identity()
        announced = self.action(
            identity=identity,
            lifecycle=CorporateActionLifecycle.ANNOUNCED,
        )
        with self.assertRaisesRegex(CorporateActionContractError, "unresolved"):
            self.snapshot((announced,), identities=(identity,))

    def test_multiple_effective_plans_on_one_ex_date_require_normalization(self) -> None:
        identity = self.identity()
        first = self.action(identity=identity, action_id="plan-one")
        second = self.action(identity=identity, action_id="plan-two")
        with self.assertRaisesRegex(CorporateActionContractError, "combined plan"):
            self.snapshot((first, second), identities=(identity,))

    def test_snapshot_id_cannot_be_relabelled(self) -> None:
        snapshot = self.snapshot()
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(snapshot, snapshot_id="f" * 64)

    def test_input_order_does_not_change_snapshot_identity(self) -> None:
        identity = self.identity()
        first = self.action(
            identity=identity,
            action_id="a",
            ex_date=date(2025, 1, 10),
        )
        second = self.action(
            identity=identity,
            action_id="b",
            ex_date=date(2025, 1, 20),
            automatic_share_ratio=Decimal("1.5"),
        )
        forward = self.snapshot((first, second), identities=(identity,))
        reverse = self.snapshot((second, first), identities=(identity,))
        self.assertEqual(forward.snapshot_id, reverse.snapshot_id)


class TestAdjustmentSeries(CorporateActionFixtures):
    def test_backward_and_forward_split_conventions_are_explicit(self) -> None:
        identity = self.identity()
        snapshot = self.snapshot(
            (self.action(identity=identity),),
            identities=(identity,),
        )
        backward = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.BACKWARD,
        )
        forward = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.FORWARD,
        )
        self.assertEqual(
            backward.price_multiplier_for(date(2025, 1, 10)),
            Decimal("0.5"),
        )
        self.assertEqual(
            backward.price_multiplier_for(date(2025, 1, 15)),
            Decimal(1),
        )
        self.assertEqual(
            backward.automatic_share_multiplier_for(date(2025, 1, 10)),
            Decimal(2),
        )
        self.assertEqual(
            forward.price_multiplier_for(date(2025, 1, 20)),
            Decimal(2),
        )
        self.assertEqual(
            forward.automatic_share_multiplier_for(date(2025, 1, 20)),
            Decimal("0.5"),
        )
        self.assertNotEqual(backward.series_id, forward.series_id)

    def test_price_ex_date_and_automatic_share_effective_date_are_distinct(self) -> None:
        identity = self.identity()
        action = self.action(
            identity=identity,
            ex_date=date(2025, 1, 15),
            share_listing_date=date(2025, 1, 20),
        )
        snapshot = self.snapshot((action,), identities=(identity,))
        backward = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.BACKWARD,
        )
        forward = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.FORWARD,
        )
        self.assertEqual(
            backward.price_multiplier_for(date(2025, 1, 16)),
            Decimal(1),
        )
        self.assertEqual(
            backward.automatic_share_multiplier_for(date(2025, 1, 16)),
            Decimal(2),
        )
        self.assertEqual(
            forward.price_multiplier_for(date(2025, 1, 16)),
            Decimal(2),
        )
        self.assertEqual(
            forward.automatic_share_multiplier_for(date(2025, 1, 16)),
            Decimal(1),
        )
        self.assertEqual(
            forward.automatic_share_multiplier_for(date(2025, 1, 20)),
            Decimal("0.5"),
        )

    def test_rights_entitlement_never_becomes_automatic_position_growth(self) -> None:
        identity = self.identity()
        action = self.action(
            identity=identity,
            automatic_share_ratio=Decimal(1),
            rights_entitlement_ratio=Decimal("0.2"),
            rights_subscription_price=Decimal(5),
            currency="CNY",
            reference_price=Decimal(10),
        )
        series = build_adjustment_series(
            self.snapshot((action,), identities=(identity,)),
            basis=AdjustmentBasis.TOTAL_RETURN,
            convention=AdjustmentConvention.BACKWARD,
        )
        factor = series.factors[0]
        self.assertEqual(factor.rights_entitlement_ratio, Decimal("0.2"))
        self.assertEqual(
            series.automatic_share_multiplier_for(date(2025, 1, 10)),
            Decimal(1),
        )

    def test_share_only_and_total_return_series_have_different_identity(self) -> None:
        identity = self.identity()
        action = self.action(
            identity=identity,
            automatic_share_ratio=Decimal(1),
            cash_dividend_per_share=Decimal(1),
            currency="CNY",
            reference_price=Decimal(10),
        )
        snapshot = self.snapshot((action,), identities=(identity,))
        share_only = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.BACKWARD,
        )
        total_return = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis.TOTAL_RETURN,
            convention=AdjustmentConvention.BACKWARD,
        )
        self.assertEqual(
            share_only.price_multiplier_for(date(2025, 1, 10)),
            Decimal(1),
        )
        self.assertEqual(
            total_return.price_multiplier_for(date(2025, 1, 10)),
            Decimal("0.9"),
        )
        self.assertNotEqual(share_only.series_id, total_return.series_id)

    def test_no_action_series_is_identity_bound_and_neutral(self) -> None:
        series = build_adjustment_series(
            self.snapshot(),
            basis=AdjustmentBasis.TOTAL_RETURN,
            convention=AdjustmentConvention.BACKWARD,
        )
        self.assertEqual(series.factors, ())
        self.assertEqual(
            series.price_multiplier_for(date(2025, 1, 10)),
            Decimal(1),
        )
        self.assertEqual(
            series.automatic_share_multiplier_for(date(2025, 1, 10)),
            Decimal(1),
        )

    def test_adjustment_view_does_not_mutate_raw_bar(self) -> None:
        identity = self.identity()
        series = build_adjustment_series(
            self.snapshot(
                (self.action(identity=identity),),
                identities=(identity,),
            ),
            basis=AdjustmentBasis.SHARE_CHANGE_ONLY,
            convention=AdjustmentConvention.BACKWARD,
        )
        bar = Bar(
            symbol=SYMBOL,
            market=Market.A,
            timestamp=utc_datetime(2025, 1, 10),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.0,
            volume=100,
            adjustment_factor=1.0,
        )
        adjusted = series.adjust_price(Decimal(str(bar.close)), date(2025, 1, 10))
        self.assertEqual(adjusted, Decimal("5.0"))
        self.assertEqual(bar.close, 10.0)
        self.assertEqual(bar.adjustment_factor, 1.0)

    def test_series_id_and_base_date_cannot_be_relabelled(self) -> None:
        series = build_adjustment_series(
            self.snapshot(),
            basis=AdjustmentBasis.TOTAL_RETURN,
            convention=AdjustmentConvention.BACKWARD,
        )
        for field_name, value in (
            ("base_date", series.start_date),
            ("factors", ()),
            ("instrument_id", "forged-instrument"),
            ("corporate_action_snapshot_id", "a" * 64),
            ("series_id", "a" * 64),
        ):
            with self.subTest(field=field_name), self.assertRaisesRegex(
                TypeError,
                "init=False",
            ):
                replace(series, **{field_name: value})


class TestAdjustedMarketDataView(CorporateActionFixtures):
    def series(self):
        return build_adjustment_series(
            self.snapshot(),
            basis=AdjustmentBasis.TOTAL_RETURN,
            convention=AdjustmentConvention.BACKWARD,
        )

    def test_view_binds_raw_calendar_and_corporate_action_snapshots(self) -> None:
        series = self.series()
        view = bind_adjusted_market_data_view(
            series,
            raw_bar_snapshot_id="b" * 64,
            calendar_snapshot_id="c" * 64,
        )
        self.assertIsInstance(view, AdjustedMarketDataView)
        self.assertEqual(view.adjustment_series_id, series.series_id)
        self.assertEqual(
            view.corporate_action_snapshot_id,
            series.corporate_action_snapshot_id,
        )
        self.assertEqual(view.instrument_id, series.instrument_id)
        self.assertEqual(view.basis, series.basis)
        self.assertEqual(view.convention, series.convention)
        self.assertFalse(hasattr(view, "adjusted_bars"))
        self.assertFalse(hasattr(view, "performance"))

    def test_any_bound_snapshot_change_changes_view_identity(self) -> None:
        series = self.series()
        original = bind_adjusted_market_data_view(
            series,
            raw_bar_snapshot_id="b" * 64,
            calendar_snapshot_id="c" * 64,
        )
        raw_changed = replace(original, raw_bar_snapshot_id="d" * 64)
        calendar_changed = replace(original, calendar_snapshot_id="e" * 64)
        self.assertNotEqual(original.view_id, raw_changed.view_id)
        self.assertNotEqual(original.view_id, calendar_changed.view_id)
        self.assertNotEqual(raw_changed.view_id, calendar_changed.view_id)

    def test_derived_view_fields_cannot_be_injected(self) -> None:
        view = bind_adjusted_market_data_view(
            self.series(),
            raw_bar_snapshot_id="b" * 64,
            calendar_snapshot_id="c" * 64,
        )
        for field_name, value in (
            ("instrument_id", "forged"),
            ("corporate_action_snapshot_id", "d" * 64),
            ("adjustment_series_id", "e" * 64),
            ("basis", AdjustmentBasis.SHARE_CHANGE_ONLY),
            ("view_id", "f" * 64),
        ):
            with self.subTest(field=field_name), self.assertRaisesRegex(
                TypeError,
                "init=False",
            ):
                replace(view, **{field_name: value})

    def test_snapshot_ids_and_series_type_fail_closed(self) -> None:
        series = self.series()
        for raw_id, calendar_id in (
            ("x" * 64, "c" * 64),
            ("b" * 64, "C" * 64),
            ("b" * 63, "c" * 64),
        ):
            with self.subTest(raw=raw_id, calendar=calendar_id), self.assertRaises(
                CorporateActionContractError
            ):
                bind_adjusted_market_data_view(
                    series,
                    raw_bar_snapshot_id=raw_id,
                    calendar_snapshot_id=calendar_id,
                )
        with self.assertRaisesRegex(CorporateActionContractError, "AdjustmentSeries"):
            AdjustedMarketDataView(
                series=object(),  # type: ignore[arg-type]
                raw_bar_snapshot_id="b" * 64,
                calendar_snapshot_id="c" * 64,
            )


if __name__ == "__main__":
    unittest.main()
