from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from typing import cast

from _helpers import (
    calendar_coverage,
    calendar_day,
    execution_bars,
    execution_engine,
    make_bar,
    market_rule,
    utc_datetime,
    zero_cost_schedule,
)

from stock_tracker.core.types import Market
from stock_tracker.quant.backtest import (
    BacktestContractError,
    CostSchedule,
    ExecutionBacktester,
    ExecutionBar,
    ExecutionContractError,
    InstrumentRule,
    LedgerEventType,
    OrderIntent,
    RuleContractError,
    TradeSide,
)
from stock_tracker.quant.core.calendar import (
    CalendarStatus,
    InstrumentSessionState,
    InstrumentSessionStatus,
    TradingCalendar,
)
from stock_tracker.quant.labels import (
    AmbiguousBarError,
    BarrierKind,
    CalendarAwareTripleBarrierLabeler,
    LabelContractError,
    LabelOutcome,
    SameBarPolicy,
    TripleBarrierConfig,
    TripleBarrierLabeler,
)


class TestExecutionEngine(unittest.TestCase):
    def test_a_share_t_plus_one_blocks_same_session_sell(self) -> None:
        engine = execution_engine(Market.A)
        bars = execution_bars((make_bar(date(2025, 1, 2)),))
        entry = engine.fill_at(
            bars,
            0,
            side=TradeSide.BUY,
            requested_quantity=100,
        )
        with self.assertRaisesRegex(ExecutionContractError, "T_PLUS_ONE"):
            engine.fill_at(
                bars,
                0,
                side=TradeSide.SELL,
                requested_quantity=100,
                acquired_session_index=entry.session_index,
            )

    def test_unknown_a_share_limit_state_fails_closed(self) -> None:
        engine = execution_engine(Market.A)
        bar = ExecutionBar(make_bar(date(2025, 1, 2)))
        with self.assertRaisesRegex(ExecutionContractError, "UNKNOWN_PRICE_LIMIT_STATE"):
            engine.fill_at(
                (bar,),
                0,
                side=TradeSide.BUY,
                requested_quantity=100,
            )

    def test_locked_limit_up_blocks_buy(self) -> None:
        engine = execution_engine(Market.A)
        bar = ExecutionBar(
            make_bar(date(2025, 1, 2)),
            locked_limit_up=True,
            locked_limit_down=False,
        )
        with self.assertRaisesRegex(ExecutionContractError, "LOCKED_LIMIT_UP"):
            engine.fill_at(
                (bar,),
                0,
                side=TradeSide.BUY,
                requested_quantity=100,
            )

    def test_limit_state_flags_require_real_booleans(self) -> None:
        with self.assertRaisesRegex(ExecutionContractError, "locked_limit_up"):
            ExecutionBar(
                make_bar(date(2025, 1, 2)),
                locked_limit_up=cast(bool | None, "true"),
                locked_limit_down=False,
            )
        with self.assertRaisesRegex(ExecutionContractError, "locked_limit_down"):
            ExecutionBar(
                make_bar(date(2025, 1, 2)),
                locked_limit_up=False,
                locked_limit_down=cast(bool | None, 0),
            )

    def test_rule_and_cost_safety_flags_require_real_booleans(self) -> None:
        with self.assertRaisesRegex(RuleContractError, "sell_t_plus_one"):
            replace(
                market_rule(),
                sell_t_plus_one=cast(bool, "true"),
            )
        with self.assertRaisesRegex(
            RuleContractError,
            "price_limit_state_required",
        ):
            replace(
                market_rule(),
                price_limit_state_required=cast(bool, 1),
            )
        with self.assertRaisesRegex(RuleContractError, "verified"):
            replace(
                zero_cost_schedule(),
                verified=cast(bool, "false"),
            )

    def test_instrument_rule_safety_flags_require_real_booleans(self) -> None:
        rule = InstrumentRule(
            rule_id="fixture-instrument-rule",
            symbol="600000.SH",
            market=Market.A,
            effective_from=date(2000, 1, 1),
            effective_to=None,
            lot_size=100,
            risk_warning=False,
            newly_listed=False,
            price_limit_up=11.0,
            price_limit_down=9.0,
            verified=True,
            source_note="synthetic fixture only",
        )
        for name, value in (
            ("risk_warning", "true"),
            ("newly_listed", 1),
            ("verified", "false"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                RuleContractError,
                f"{name} must be a boolean",
            ):
                replace(rule, **{name: value})

    def test_slippage_price_is_clipped_to_observed_bar(self) -> None:
        expensive = CostSchedule(
            schedule_id="US-extreme-cost",
            market=Market.US,
            effective_from=date(2000, 1, 1),
            effective_to=None,
            commission_bps=0.0,
            minimum_commission=0.0,
            sell_tax_bps=0.0,
            exchange_fee_bps=0.0,
            transfer_fee_bps=0.0,
            half_spread_bps=500.0,
            slippage_bps=500.0,
            impact_coefficient=1.0,
            max_participation_rate=1.0,
            verified=True,
            source_note="synthetic extreme fixture",
        )
        engine = execution_engine(Market.US, cost=expensive)
        bar = make_bar(
            date(2025, 1, 2),
            symbol="AAPL.US",
            market=Market.US,
            open_price=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=100,
        )
        fill = engine.fill_at(
            execution_bars((bar,)),
            0,
            side=TradeSide.BUY,
            requested_quantity=100,
        )
        self.assertEqual(fill.price, 101.0)
        realized_implicit = (
            fill.costs.spread_cost
            + fill.costs.slippage_cost
            + fill.costs.impact_cost
        )
        self.assertAlmostEqual(realized_implicit, (fill.price - fill.reference_price) * 100)

    def test_participation_limit_smaller_than_one_lot_fails(self) -> None:
        limited = replace(
            zero_cost_schedule(Market.A),
            schedule_id="A-limited",
            max_participation_rate=0.05,
        )
        engine = execution_engine(Market.A, cost=limited)
        bar = make_bar(date(2025, 1, 2), volume=1_000)
        with self.assertRaisesRegex(
            ExecutionContractError,
            "allows no tradable lot",
        ):
            engine.fill_at(
                execution_bars((bar,)),
                0,
                side=TradeSide.BUY,
                requested_quantity=1_000,
            )


class TestTripleBarrier(unittest.TestCase):
    def labeler(
        self,
        *,
        policy: SameBarPolicy = SameBarPolicy.MARK_AMBIGUOUS,
        horizon: int = 4,
    ) -> TripleBarrierLabeler:
        return TripleBarrierLabeler(
            execution_engine(Market.A),
            TripleBarrierConfig(
                take_profit_atr=1.0,
                stop_loss_atr=1.0,
                horizon_sessions=horizon,
                entry_delay_sessions=0,
                same_bar_policy=policy,
            ),
        )

    def test_best_case_is_forbidden(self) -> None:
        with self.assertRaises(LabelContractError):
            TripleBarrierConfig(
                take_profit_atr=1.0,
                stop_loss_atr=1.0,
                horizon_sessions=2,
                same_bar_policy=SameBarPolicy.BEST_CASE,
            )

    def test_gap_through_target_fills_at_open(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(
                    date(2025, 1, 3),
                    open_price=12.0,
                    high=12.2,
                    low=11.8,
                    close=12.0,
                ),
            )
        )
        result = self.labeler().label(
            bars,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
        )
        self.assertEqual(result.outcome, LabelOutcome.TP_FIRST)
        self.assertEqual(result.exit.price, 12.0)

    def test_same_bar_both_barriers_marked_ambiguous(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(
                    date(2025, 1, 3),
                    open_price=10.0,
                    high=12.0,
                    low=8.0,
                    close=10.0,
                ),
            )
        )
        result = self.labeler().label(
            bars,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
        )
        self.assertEqual(result.outcome, LabelOutcome.AMBIGUOUS)
        self.assertTrue(result.ambiguous)

    def test_same_bar_worst_case_resolves_to_stop(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(
                    date(2025, 1, 3),
                    open_price=10.0,
                    high=12.0,
                    low=8.0,
                    close=10.0,
                ),
            )
        )
        result = self.labeler(policy=SameBarPolicy.WORST_CASE).label(
            bars,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
        )
        self.assertEqual(result.outcome, LabelOutcome.SL_FIRST)

    def test_lower_timeframe_resolution_is_required(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(date(2025, 1, 3), high=12.0, low=8.0),
            )
        )
        labeler = self.labeler(policy=SameBarPolicy.LOWER_TIMEFRAME_REQUIRED)
        with self.assertRaises(AmbiguousBarError):
            labeler.label(
                bars,
                signal_index=0,
                atr=1.0,
                requested_quantity=100,
            )
        result = labeler.label(
            bars,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
            same_bar_resolution={1: BarrierKind.TAKE_PROFIT},
        )
        self.assertEqual(result.outcome, LabelOutcome.TP_FIRST)

    def test_blocked_first_stop_cannot_be_overwritten_by_later_target(self) -> None:
        bars = (
            ExecutionBar(
                make_bar(date(2025, 1, 2)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
            ExecutionBar(
                make_bar(
                    date(2025, 1, 3),
                    open_price=9.0,
                    high=10.0,
                    low=8.5,
                    close=9.0,
                ),
                locked_limit_up=False,
                locked_limit_down=True,
            ),
            ExecutionBar(
                make_bar(
                    date(2025, 1, 6),
                    open_price=10.5,
                    high=11.5,
                    low=10.3,
                    close=11.0,
                ),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
        )
        result = self.labeler().label(
            bars,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
        )
        self.assertEqual(result.first_barrier, BarrierKind.STOP_LOSS)
        self.assertEqual(result.outcome, LabelOutcome.SL_FIRST)
        self.assertTrue(any("LOCKED_LIMIT_DOWN" in reason for reason in result.blocked_reasons))

    def test_nonobservable_placeholder_does_not_pollute_mfe(self) -> None:
        bars = (
            ExecutionBar(
                make_bar(date(2025, 1, 2)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
            ExecutionBar(
                make_bar(
                    date(2025, 1, 3),
                    open_price=100.0,
                    high=100.0,
                    low=100.0,
                    close=100.0,
                    volume=0,
                ),
                state=InstrumentSessionState.SUSPENDED,
                locked_limit_up=False,
                locked_limit_down=False,
            ),
            ExecutionBar(
                make_bar(date(2025, 1, 6)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
        )
        result = self.labeler(horizon=3).label(
            bars,
            signal_index=0,
            atr=5.0,
            requested_quantity=100,
        )
        self.assertLess(result.mfe, 1.0)


class TestCalendarAwareLabel(unittest.TestCase):
    def test_calendar_alignment_prevents_horizon_drift(self) -> None:
        start = date(2025, 1, 2)
        end = date(2025, 1, 6)
        coverage = calendar_coverage(start, end)
        days = (
            calendar_day(date(2025, 1, 2), status=CalendarStatus.OPEN),
            calendar_day(date(2025, 1, 3), status=CalendarStatus.CLOSED),
            calendar_day(date(2025, 1, 4), status=CalendarStatus.CLOSED),
            calendar_day(date(2025, 1, 5), status=CalendarStatus.OPEN),
            calendar_day(date(2025, 1, 6), status=CalendarStatus.OPEN),
        )
        raw_bars = (
            make_bar(date(2025, 1, 2)),
            make_bar(
                date(2025, 1, 6),
                open_price=11.2,
                high=11.5,
                low=11.0,
                close=11.3,
            ),
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
        config = TripleBarrierConfig(
            take_profit_atr=1.0,
            stop_loss_atr=1.0,
            horizon_sessions=2,
            entry_delay_sessions=0,
            same_bar_policy=SameBarPolicy.MARK_AMBIGUOUS,
        )
        base = TripleBarrierLabeler(execution_engine(Market.A), config)
        unsafe = base.label(
            execution_bars(raw_bars),
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
        )
        aligned = TradingCalendar((coverage,), days, (status,)).align_bars(
            symbol="600000.SH",
            market=Market.A,
            bars=raw_bars,
            start=start,
            end=end,
            as_of=utc_datetime(2025, 1, 7),
        )
        safe = CalendarAwareTripleBarrierLabeler(base).label(
            aligned,
            signal_index=0,
            atr=1.0,
            requested_quantity=100,
            limit_states={
                date(2025, 1, 2): (False, False),
                date(2025, 1, 5): (False, False),
                date(2025, 1, 6): (False, False),
            },
        )
        self.assertEqual(unsafe.outcome, LabelOutcome.TP_FIRST)
        self.assertEqual(safe.outcome, LabelOutcome.TIMEOUT)

    def test_raw_sequence_rejected_at_production_boundary(self) -> None:
        base = TripleBarrierLabeler(
            execution_engine(Market.A),
            TripleBarrierConfig(1.0, 1.0, 2, entry_delay_sessions=0),
        )
        with self.assertRaises(TypeError):
            CalendarAwareTripleBarrierLabeler(base).label(
                execution_bars((make_bar(date(2025, 1, 2)),)),
                signal_index=0,
                atr=1.0,
                requested_quantity=100,
            )


class TestExecutionBacktester(unittest.TestCase):
    def test_sequential_buy_and_sell_use_shared_execution(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(
                    date(2025, 1, 3),
                    open_price=11.0,
                    high=11.2,
                    low=10.8,
                    close=11.0,
                ),
            )
        )
        result = ExecutionBacktester(
            execution_engine(Market.A),
            initial_cash=10_000.0,
        ).run(
            bars,
            (
                OrderIntent("buy-1", "position-1", TradeSide.BUY, 0, 100),
                OrderIntent("sell-1", "position-1", TradeSide.SELL, 1, 100),
            ),
        )
        self.assertEqual(result.rejected_orders, 0)
        self.assertEqual(len(result.closed_trades), 1)
        self.assertGreater(result.final_equity, result.initial_cash)
        self.assertEqual(
            tuple(event.event_type for event in result.events),
            (LedgerEventType.BUY, LedgerEventType.SELL),
        )

    def test_suspended_session_delays_fill_without_reordering_ledger(self) -> None:
        bars = (
            ExecutionBar(
                make_bar(date(2025, 1, 2), volume=0),
                state=InstrumentSessionState.SUSPENDED,
                locked_limit_up=False,
                locked_limit_down=False,
            ),
            ExecutionBar(
                make_bar(date(2025, 1, 3)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
        )
        result = ExecutionBacktester(
            execution_engine(Market.A),
            initial_cash=10_000.0,
        ).run(
            bars,
            (OrderIntent("buy-delayed", "position-delayed", TradeSide.BUY, 0, 100),),
        )
        self.assertEqual(result.rejected_orders, 0)
        self.assertEqual(result.events[0].session_index, 1)
        self.assertEqual(result.open_positions[0].entry_fill.session_index, 1)

    def test_crossed_fill_time_fails_closed_without_time_travel(self) -> None:
        bars = (
            ExecutionBar(
                make_bar(date(2025, 1, 2)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
            ExecutionBar(
                make_bar(date(2025, 1, 3)),
                locked_limit_up=False,
                locked_limit_down=True,
            ),
            ExecutionBar(
                make_bar(date(2025, 1, 6)),
                locked_limit_up=False,
                locked_limit_down=True,
            ),
            ExecutionBar(
                make_bar(date(2025, 1, 7)),
                locked_limit_up=False,
                locked_limit_down=False,
            ),
        )
        result = ExecutionBacktester(
            execution_engine(Market.A),
            initial_cash=10_000.0,
        ).run(
            bars,
            (
                OrderIntent("buy-a", "position-a", TradeSide.BUY, 0, 100),
                OrderIntent("sell-a", "position-a", TradeSide.SELL, 1, 100),
                OrderIntent("buy-b", "position-b", TradeSide.BUY, 2, 100),
            ),
        )
        self.assertEqual(
            tuple(event.event_type for event in result.events),
            (LedgerEventType.BUY, LedgerEventType.SELL, LedgerEventType.REJECT),
        )
        self.assertEqual(result.events[1].session_index, 3)
        self.assertEqual(result.events[2].reason, "NON_MONOTONIC_FILL_TIME")
        self.assertEqual(result.rejected_orders, 1)
        self.assertEqual(result.open_positions, ())
        self.assertEqual(len(result.closed_trades), 1)

    def test_sell_quantity_must_exactly_match_position(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(date(2025, 1, 3)),
            )
        )
        for sell_quantity in (50, 200):
            with self.subTest(sell_quantity=sell_quantity):
                result = ExecutionBacktester(
                    execution_engine(Market.A),
                    initial_cash=10_000.0,
                ).run(
                    bars,
                    (
                        OrderIntent("buy", "position", TradeSide.BUY, 0, 100),
                        OrderIntent(
                            "sell",
                            "position",
                            TradeSide.SELL,
                            1,
                            sell_quantity,
                        ),
                    ),
                )
                self.assertEqual(result.rejected_orders, 1)
                self.assertEqual(result.events[-1].event_type, LedgerEventType.REJECT)
                self.assertEqual(
                    result.events[-1].reason,
                    "SELL_QUANTITY_MUST_EQUAL_POSITION",
                )
                self.assertEqual(result.open_positions[0].quantity, 100)
                self.assertEqual(result.closed_trades, ())

    def test_multi_symbol_input_is_rejected_instead_of_misfilled(self) -> None:
        bars = execution_bars(
            (
                make_bar(date(2025, 1, 2)),
                make_bar(date(2025, 1, 3), symbol="000001.SZ"),
            )
        )
        with self.assertRaisesRegex(
            BacktestContractError,
            "one symbol and one market",
        ):
            ExecutionBacktester(
                execution_engine(Market.A),
                initial_cash=10_000.0,
            ).run(bars, ())


if __name__ == "__main__":
    unittest.main()
