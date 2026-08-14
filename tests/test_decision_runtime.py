from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_tracker.core import types as T
from stock_tracker.decision.runtime import (
    build_signal_record,
    build_unbound_position_record,
)
from stock_tracker.decision.types import (
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    RiskMode,
    UserPortfolioProfile,
)


class TestDecisionRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime.now(timezone.utc)
        self.profile = UserPortfolioProfile(
            account_equity=100_000,
            available_cash=50_000,
            risk_mode=RiskMode.BALANCED,
            per_trade_risk_pct=0.01,
            max_position_pct=0.30,
            max_portfolio_heat_pct=0.10,
            max_sector_pct=0.50,
            max_theme_pct=0.50,
        )

    def quote(self, *, status: T.DataStatus = T.DataStatus.LIVE, last: float = 10.5) -> T.Quote:
        return T.Quote(
            symbol="600000.SH",
            market=T.Market.A,
            timestamp=datetime.now(),
            name="浦发银行",
            open=10.0,
            high=10.8,
            low=9.9,
            close=10.5,
            last=last,
            prev_close=10.0,
            volume=1_000_000,
            data_status=status,
        )

    def signal(
        self,
        state: T.SignalState,
        *,
        status: T.DataStatus = T.DataStatus.LIVE,
    ) -> T.Signal:
        return T.Signal(
            signal_id="600000.SH:S1",
            symbol="600000.SH",
            market=T.Market.A,
            strategy_id="S1",
            state=state,
            entry_low=10.0,
            entry_high=10.4,
            trigger_price=10.5,
            invalidation_price=9.5,
            target_1=11.5,
            target_2=12.2,
            reward_risk=2.1,
            freshness=0.9,
            next_trigger="等待结构确认",
            data_status=status,
            scores=T.ScoreSet(
                opportunity=82,
                timing=75,
                risk=35,
                confidence=70,
                positive_reasons=["趋势结构完整"],
                negative_reasons=["临近前高"],
            ),
        )

    def build(self, signal: T.Signal, **overrides: object):
        values = {
            "quote": self.quote(status=signal.data_status),
            "data_status": signal.data_status,
            "has_position": False,
            "profile": self.profile,
            "current_portfolio_heat_pct": 0.0,
            "current_sector_exposure_pct": 0.0,
            "current_theme_exposure_pct": 0.0,
            "sector": "金融",
            "as_of": self.as_of,
            "no_chase_pct": 0.05,
        }
        values.update(overrides)
        return build_signal_record(signal, **values)

    def test_live_trigger_builds_executable_plan_and_size(self) -> None:
        record = self.build(self.signal(T.SignalState.TRIGGERED))
        self.assertEqual(record.action.action, ActionState.EXECUTABLE)
        self.assertIsNotNone(record.action.trade_plan)
        self.assertTrue(record.action.trade_plan.position_size.allowed)
        self.assertGreater(record.action.trade_plan.position_size.shares, 0)
        self.assertIsNone(record.action.trade_plan.calibrated_probability)

    def test_delayed_trigger_fails_closed_without_plan(self) -> None:
        signal = self.signal(T.SignalState.TRIGGERED, status=T.DataStatus.DELAYED)
        record = self.build(
            signal,
            quote=self.quote(status=T.DataStatus.DELAYED),
            data_status=T.DataStatus.DELAYED,
        )
        self.assertEqual(record.action.action, ActionState.DATA_BLOCKED)
        self.assertIsNone(record.action.trade_plan)

    def test_waiting_plan_can_offer_reduced_live_aggressive_variant(self) -> None:
        record = self.build(self.signal(T.SignalState.ARMED_PULLBACK))
        plan = record.action.trade_plan
        self.assertEqual(record.action.action, ActionState.WAIT_PULLBACK)
        self.assertIsNotNone(plan.aggressive_plan)
        self.assertEqual(plan.aggressive_plan.action, ActionState.EXECUTABLE)
        self.assertLess(
            plan.aggressive_plan.risk_budget_multiplier,
            plan.balanced_plan.risk_budget_multiplier,
        )

    def test_no_profile_keeps_plan_but_does_not_invent_position_size(self) -> None:
        record = self.build(
            self.signal(T.SignalState.ARMED_BREAKOUT),
            profile=None,
        )
        self.assertEqual(record.action.action, ActionState.WAIT_BREAKOUT)
        self.assertIsNone(record.action.trade_plan.position_size)
        self.assertIsNone(record.action.trade_plan.aggressive_plan)

    def test_external_hard_blocker_prevents_executable(self) -> None:
        blocker = DecisionBlocker(
            "PORTFOLIO_RISK_INCOMPLETE",
            "现有持仓风险无法完整计算",
            BlockerSeverity.HARD,
        )
        record = self.build(
            self.signal(T.SignalState.TRIGGERED),
            external_hard_blockers=(blocker,),
        )
        self.assertEqual(record.action.action, ActionState.AVOID)
        self.assertIn(blocker, record.hard_blockers)
        self.assertFalse(record.action.trade_plan.position_size.allowed)

    def test_holding_near_invalidation_is_warning(self) -> None:
        signal = self.signal(T.SignalState.ACTIVE)
        record = self.build(
            signal,
            quote=self.quote(last=9.65),
            has_position=True,
        )
        self.assertEqual(record.action.action, ActionState.WARNING)
        self.assertIsNone(record.action.trade_plan)

    def test_unbound_holding_is_explicitly_data_blocked(self) -> None:
        position = T.Position(
            id="pos-1",
            symbol="600000.SH",
            market=T.Market.A,
            shares=37,
            cost=9.8,
            added_at=datetime.now(),
        )
        record = build_unbound_position_record(
            position,
            quote=self.quote(),
            data_status=T.DataStatus.LIVE,
            sector="金融",
        )
        self.assertEqual(record.action.action, ActionState.DATA_BLOCKED)
        self.assertEqual(record.hard_blockers[0].code, "POSITION_THESIS_MISSING")

    def test_hk_position_size_fails_closed_without_explicit_lot_size(self) -> None:
        signal = self.signal(T.SignalState.ARMED_PULLBACK)
        signal.symbol = "00700.HK"
        signal.market = T.Market.HK
        quote = self.quote()
        quote.symbol = "00700.HK"
        quote.market = T.Market.HK
        record = self.build(signal, quote=quote, sector="港股科技")
        self.assertEqual(record.action.action, ActionState.WAIT_PULLBACK)
        self.assertTrue(record.hard_blockers)
        self.assertIsNone(record.action.trade_plan.position_size)
        self.assertIsNone(record.action.trade_plan.aggressive_plan)


if __name__ == "__main__":
    unittest.main()
