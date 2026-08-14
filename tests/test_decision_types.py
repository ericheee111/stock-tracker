from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone

from stock_tracker.core.types import DataStatus, Market, SignalState
from stock_tracker.decision.types import (
    ActionDecision,
    ActionState,
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    PlanVariant,
    PositionSizeResult,
    ProbabilityEvidenceLevel,
    RiskMode,
    TradePlan,
    UserPortfolioProfile,
)


class TestPortfolioProfile(unittest.TestCase):
    def test_valid_profile(self) -> None:
        profile = UserPortfolioProfile(
            account_equity=500_000,
            available_cash=120_000,
        )
        self.assertEqual(profile.risk_mode, RiskMode.BALANCED)

    def test_rejects_boolean_and_nonfinite_numbers(self) -> None:
        for value in (True, math.nan, math.inf):
            with self.subTest(value=value):
                with self.assertRaises(DecisionContractError):
                    UserPortfolioProfile(
                        account_equity=value,
                        available_cash=0,
                    )

    def test_cash_cannot_exceed_equity(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "available_cash"):
            UserPortfolioProfile(account_equity=100, available_cash=101)

    def test_requires_aware_updated_at(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "timezone-aware"):
            UserPortfolioProfile(
                account_equity=100,
                available_cash=10,
                updated_at=datetime(2026, 1, 1),
            )


class TestStrictDecisionContracts(unittest.TestCase):
    def _position(self, **overrides: object) -> PositionSizeResult:
        values: dict[str, object] = {
            "allowed": True,
            "shares": 100,
            "lot_size": 100,
            "entry_price": 10.0,
            "invalidation_price": 9.0,
            "risk_per_share": 1.0,
            "risk_budget_amount": 100.0,
            "actual_risk_amount": 100.0,
            "actual_risk_pct": 0.001,
            "position_value": 1_000.0,
            "position_pct": 0.01,
            "limiting_factors": ("PER_TRADE_RISK",),
            "blockers": (),
        }
        values.update(overrides)
        return PositionSizeResult(**values)

    def _plan(self, **overrides: object) -> TradePlan:
        action = overrides.pop("action", ActionState.WATCH)
        values: dict[str, object] = {
            "symbol": "600519.SH",
            "market": Market.A,
            "strategy_id": "S1",
            "action": action,
            "entry_low": 10.0,
            "entry_high": 11.0,
            "trigger_price": 11.0,
            "no_chase_above": 11.5,
            "invalidation_price": 9.0,
            "target_1": 12.0,
            "target_2": 13.0,
            "reward_risk": 2.0,
            "next_trigger": "wait",
            "position_size": None,
            "balanced_plan": PlanVariant("BALANCED", action, 1.0, "wait"),
            "aggressive_plan": None,
            "hard_blockers": (),
            "soft_blockers": (),
            "calibrated_probability": None,
            "probability_evidence_level": ProbabilityEvidenceLevel.INSUFFICIENT,
            "data_status": DataStatus.LIVE,
            "as_of": datetime.now(timezone.utc),
        }
        values.update(overrides)
        return TradePlan(**values)

    def test_blocker_boolean_is_strict(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "real boolean"):
            DecisionBlocker(
                code="X",
                message="x",
                severity=BlockerSeverity.HARD,
                recoverable=1,
            )

    def test_action_decision_requires_tuple_blockers(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "tuple"):
            ActionDecision(
                action=ActionState.WATCH,
                source_state=SignalState.WATCH,
                has_position=False,
                actionable=False,
                reason="watch",
                data_status=DataStatus.LIVE,
                blockers=[],
            )

    def test_executable_action_decision_requires_live_no_position(self) -> None:
        invalid = (
            {"has_position": True, "data_status": DataStatus.LIVE},
            {"has_position": False, "data_status": DataStatus.DELAYED},
        )
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaises(DecisionContractError):
                    ActionDecision(
                        action=ActionState.EXECUTABLE,
                        source_state=SignalState.TRIGGERED,
                        actionable=True,
                        reason="execute",
                        blockers=(),
                        **case,
                    )

    def test_position_result_rejects_inconsistent_math(self) -> None:
        invalid = (
            {"risk_per_share": 2.0},
            {"actual_risk_amount": 99.0},
            {"position_value": 999.0},
            {"risk_budget_amount": 99.0},
        )
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaises(DecisionContractError):
                    self._position(**case)

    def test_blocked_position_requires_hard_reason_and_zero_values(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "explain its blocker"):
            self._position(
                allowed=False,
                shares=0,
                actual_risk_amount=0.0,
                actual_risk_pct=0.0,
                position_value=0.0,
                position_pct=0.0,
                blockers=(),
            )
        soft = DecisionBlocker("WAIT", "wait", BlockerSeverity.SOFT)
        with self.assertRaisesRegex(DecisionContractError, "HARD"):
            self._position(
                allowed=False,
                shares=0,
                actual_risk_amount=0.0,
                actual_risk_pct=0.0,
                position_value=0.0,
                position_pct=0.0,
                blockers=(soft,),
            )

    def test_probability_null_requires_insufficient_evidence(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "null calibrated_probability"):
            self._plan(
                probability_evidence_level=ProbabilityEvidenceLevel.HIGH,
            )

    def test_direct_trade_plan_rejects_invalid_long_relationships(self) -> None:
        invalid = (
            {"invalidation_price": 10.0},
            {"no_chase_above": 10.5},
            {"trigger_price": 12.0},
            {"target_1": 11.0},
            {"target_1": 12.0, "target_2": 11.5},
        )
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaises(DecisionContractError):
                    self._plan(**case)

    def test_direct_trade_plan_rejects_symbol_market_mismatch(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "suffix"):
            self._plan(symbol="AAPL.US", market=Market.A)

    def test_hard_blocker_forbids_executable_or_allowed_position(self) -> None:
        hard = DecisionBlocker("DQ", "bad data", BlockerSeverity.HARD)
        with self.assertRaisesRegex(DecisionContractError, "EXECUTABLE"):
            self._plan(
                action=ActionState.EXECUTABLE,
                balanced_plan=PlanVariant(
                    "BALANCED", ActionState.EXECUTABLE, 1.0, "blocked"
                ),
                hard_blockers=(hard,),
            )
        with self.assertRaisesRegex(DecisionContractError, "blocked position"):
            self._plan(
                action=ActionState.AVOID,
                balanced_plan=PlanVariant(
                    "BALANCED", ActionState.AVOID, 1.0, "blocked"
                ),
                hard_blockers=(hard,),
                position_size=self._position(),
            )

    def test_hard_blocker_forbids_aggressive_plan(self) -> None:
        hard = DecisionBlocker("DQ", "bad data", BlockerSeverity.HARD)
        with self.assertRaisesRegex(DecisionContractError, "cannot bypass"):
            self._plan(
                action=ActionState.AVOID,
                balanced_plan=PlanVariant(
                    "BALANCED", ActionState.AVOID, 1.0, "blocked"
                ),
                aggressive_plan=PlanVariant(
                    "AGGRESSIVE", ActionState.AVOID, 0.5, "small"
                ),
                hard_blockers=(hard,),
            )

    def test_aggressive_plan_must_have_soft_blocker_and_lower_budget(self) -> None:
        aggressive = PlanVariant("AGGRESSIVE", ActionState.WATCH, 1.0, "small")
        with self.assertRaisesRegex(DecisionContractError, "soft blocker"):
            self._plan(aggressive_plan=aggressive)
        soft = DecisionBlocker("WAIT", "wait", BlockerSeverity.SOFT)
        with self.assertRaisesRegex(DecisionContractError, "lower"):
            self._plan(aggressive_plan=aggressive, soft_blockers=(soft,))

    def test_direct_aggressive_executable_requires_live_data(self) -> None:
        soft = DecisionBlocker("WAIT", "wait", BlockerSeverity.SOFT)
        aggressive = PlanVariant(
            "AGGRESSIVE",
            ActionState.EXECUTABLE,
            0.5,
            "reduced risk",
        )
        with self.assertRaisesRegex(DecisionContractError, "requires LIVE"):
            self._plan(
                data_status=DataStatus.DELAYED,
                aggressive_plan=aggressive,
                soft_blockers=(soft,),
            )


if __name__ == "__main__":
    unittest.main()
