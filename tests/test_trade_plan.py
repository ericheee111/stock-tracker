from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_tracker.core.types import DataStatus, Market
from stock_tracker.decision.position_sizing import size_position
from stock_tracker.decision.trade_plan import build_trade_plan
from stock_tracker.decision.types import (
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    ProbabilityEvidenceLevel,
    UserPortfolioProfile,
)


class TestTradePlan(unittest.TestCase):
    def setUp(self) -> None:
        profile = UserPortfolioProfile(account_equity=100_000, available_cash=50_000)
        self.position = size_position(
            profile,
            market=Market.A,
            entry_price=10,
            invalidation_price=9.5,
        )
        self.base = {
            "symbol": "600000.SH",
            "market": Market.A,
            "strategy_id": "S2",
            "action": ActionState.WAIT_PULLBACK,
            "entry_low": 10.0,
            "entry_high": 10.4,
            "trigger_price": 10.5,
            "no_chase_above": 10.7,
            "invalidation_price": 9.5,
            "target_1": 11.5,
            "target_2": 12.2,
            "reward_risk": 2.1,
            "next_trigger": "Wait for pullback confirmation",
            "position_size": self.position,
            "calibrated_probability": None,
            "probability_evidence_level": ProbabilityEvidenceLevel.INSUFFICIENT,
            "data_status": DataStatus.LIVE,
            "as_of": datetime.now(timezone.utc),
        }

    def build(self, **overrides: object):
        values = dict(self.base)
        values.update(overrides)
        return build_trade_plan(**values)

    def test_builds_balanced_plan_with_null_probability(self) -> None:
        plan = self.build()
        self.assertEqual(plan.balanced_plan.name, "BALANCED")
        self.assertIsNone(plan.calibrated_probability)
        self.assertIsNone(plan.aggressive_plan)

    def test_rejects_invalid_long_price_relationships(self) -> None:
        invalid_cases = (
            {"entry_low": 10.5, "entry_high": 10.4},
            {"invalidation_price": 10.0},
            {"no_chase_above": 10.3},
            {"target_1": 10.4},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(DecisionContractError):
                    self.build(**kwargs)

    def test_position_size_must_be_bound_to_plan_prices(self) -> None:
        profile = UserPortfolioProfile(account_equity=100_000, available_cash=50_000)
        mismatched_invalidation = size_position(
            profile, market=Market.A, entry_price=10, invalidation_price=9
        )
        outside_entry_reference = size_position(
            profile, market=Market.A, entry_price=11, invalidation_price=9.5
        )
        for position in (mismatched_invalidation, outside_entry_reference):
            with self.subTest(position=position):
                with self.assertRaises(DecisionContractError):
                    self.build(position_size=position)

    def test_soft_blocker_can_create_reduced_aggressive_plan(self) -> None:
        soft = DecisionBlocker(
            "WAIT_CONFIRMATION", "Confirmation is incomplete", BlockerSeverity.SOFT
        )
        plan = self.build(
            soft_blockers=(soft,),
            aggressive_risk_budget_multiplier=0.5,
        )
        self.assertIsNotNone(plan.aggressive_plan)
        self.assertLess(
            plan.aggressive_plan.risk_budget_multiplier,
            plan.balanced_plan.risk_budget_multiplier,
        )

    def test_aggressive_multiplier_must_be_strictly_lower(self) -> None:
        soft = DecisionBlocker("WAIT", "Wait", BlockerSeverity.SOFT)
        with self.assertRaisesRegex(DecisionContractError, "lower"):
            self.build(
                soft_blockers=(soft,),
                aggressive_risk_budget_multiplier=1.0,
            )

    def test_hard_blocker_removes_aggressive_and_zeroes_position(self) -> None:
        hard = DecisionBlocker("HALT", "Security is halted", BlockerSeverity.HARD)
        soft = DecisionBlocker("WAIT", "Wait", BlockerSeverity.SOFT)
        plan = self.build(
            action=ActionState.EXECUTABLE,
            hard_blockers=(hard,),
            soft_blockers=(soft,),
            aggressive_risk_budget_multiplier=0.5,
        )
        self.assertIsNone(plan.aggressive_plan)
        self.assertFalse(plan.position_size.allowed)
        self.assertEqual(plan.position_size.shares, 0)
        self.assertEqual(plan.action, ActionState.AVOID)

    def test_stale_executable_plan_fails_closed(self) -> None:
        plan = self.build(action=ActionState.EXECUTABLE, data_status=DataStatus.STALE)
        self.assertEqual(plan.action, ActionState.DATA_BLOCKED)
        self.assertTrue(plan.hard_blockers)
        self.assertFalse(plan.position_size.allowed)

    def test_probability_contract_is_not_filled_from_scores(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "null calibrated_probability"):
            self.build(probability_evidence_level=ProbabilityEvidenceLevel.HIGH)


if __name__ == "__main__":
    unittest.main()
