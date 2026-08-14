from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_tracker.core.types import DataStatus, Market
from stock_tracker.decision.brief import (
    build_decision_brief,
    select_core_opportunities,
    sort_holding_actions,
)
from stock_tracker.decision.types import (
    ActionState,
    DecisionAction,
    DecisionBrief,
    DecisionContractError,
    PlanVariant,
    ProbabilityEvidenceLevel,
    RankingMode,
    TradePlan,
)


class TestDecisionBrief(unittest.TestCase):
    def plan(
        self,
        symbol: str,
        action: ActionState,
        *,
        data_status: DataStatus = DataStatus.LIVE,
        probability: float | None = None,
    ) -> TradePlan:
        evidence = (
            ProbabilityEvidenceLevel.HIGH
            if probability is not None
            else ProbabilityEvidenceLevel.INSUFFICIENT
        )
        return TradePlan(
            symbol=symbol,
            market=Market.US,
            strategy_id="S1",
            action=action,
            entry_low=10.0,
            entry_high=10.5,
            trigger_price=10.6,
            no_chase_above=11.0,
            invalidation_price=9.0,
            target_1=12.0,
            target_2=13.0,
            reward_risk=2.0,
            next_trigger="deterministic trigger",
            position_size=None,
            balanced_plan=PlanVariant("BALANCED", action, 1.0, "standard"),
            aggressive_plan=None,
            hard_blockers=(),
            soft_blockers=(),
            calibrated_probability=probability,
            probability_evidence_level=evidence,
            data_status=data_status,
            as_of=datetime.now(timezone.utc),
        )

    def action(
        self,
        symbol: str,
        action: ActionState,
        *,
        sector: str = "TECH",
        opportunity: int = 50,
        risk: int = 30,
        data_status: DataStatus = DataStatus.LIVE,
        probability: float | None = None,
    ) -> DecisionAction:
        plan = None
        if action in {
            ActionState.EXECUTABLE,
            ActionState.WAIT_PULLBACK,
            ActionState.WAIT_BREAKOUT,
        }:
            plan = self.plan(
                symbol,
                action,
                data_status=data_status,
                probability=probability,
            )
        return DecisionAction(
            symbol=symbol,
            market=Market.US,
            action=action,
            strategy_id="S1",
            opportunity=opportunity,
            timing=60,
            risk=risk,
            confidence=70,
            reward_risk=2.0,
            freshness=0.9,
            sector=sector,
            reason="deterministic reason",
            trade_plan=plan,
            data_status=data_status,
        )

    def test_core_defaults_to_five_and_prioritizes_action(self) -> None:
        candidates = tuple(
            [
                self.action("WATCH.US", ActionState.WATCH, sector="S1", opportunity=99),
                self.action("EXEC.US", ActionState.EXECUTABLE, sector="S2", opportunity=20),
            ]
            + [
                self.action(f"X{index}.US", ActionState.WAIT_BREAKOUT, sector=f"S{index}")
                for index in range(6)
            ]
        )
        selected = select_core_opportunities(candidates)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0].symbol, "EXEC.US")

    def test_same_symbol_is_deduplicated(self) -> None:
        selected = select_core_opportunities(
            (
                self.action("X.US", ActionState.WATCH, opportunity=90),
                self.action("X.US", ActionState.EXECUTABLE, opportunity=10),
            )
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].action, ActionState.EXECUTABLE)

    def test_sector_quota_is_two(self) -> None:
        selected = select_core_opportunities(
            tuple(
                self.action(f"T{index}.US", ActionState.WATCH, sector="TECH", opportunity=90 - index)
                for index in range(4)
            )
            + (self.action("BANK.US", ActionState.WATCH, sector="BANK"),)
        )
        self.assertEqual(sum(item.sector == "TECH" for item in selected), 2)
        self.assertIn("BANK.US", {item.symbol for item in selected})

    def test_unknown_sectors_do_not_share_one_quota_bucket(self) -> None:
        selected = select_core_opportunities(
            tuple(
                self.action(f"U{index}.US", ActionState.WATCH, sector="UNKNOWN")
                for index in range(5)
            )
        )
        self.assertEqual(len(selected), 5)

    def test_non_live_data_cannot_construct_executable(self) -> None:
        for status in (DataStatus.DELAYED, DataStatus.STALE, DataStatus.UNKNOWN):
            with self.subTest(status=status):
                with self.assertRaisesRegex(DecisionContractError, "LIVE"):
                    self.action(
                        "X.US",
                        ActionState.EXECUTABLE,
                        data_status=status,
                    )

    def test_holding_actions_have_independent_priority(self) -> None:
        ordered = sort_holding_actions(
            (
                self.action("H.US", ActionState.HOLD),
                self.action("W.US", ActionState.WARNING),
                self.action("E.US", ActionState.EXIT),
                self.action("D.US", ActionState.DATA_BLOCKED),
                self.action("T.US", ActionState.TRIM),
            )
        )
        self.assertEqual(
            [item.action for item in ordered],
            [
                ActionState.EXIT,
                ActionState.TRIM,
                ActionState.WARNING,
                ActionState.HOLD,
                ActionState.DATA_BLOCKED,
            ],
        )

    def test_brief_uses_rule_evidence_and_deterministic_facts(self) -> None:
        brief = build_decision_brief(
            as_of=datetime.now(timezone.utc),
            market_posture="ROTATION",
            aggression_level=50,
            core_candidates=(self.action("X.US", ActionState.EXECUTABLE),),
            holding_actions=(self.action("E.US", ActionState.EXIT),),
            avoid_reasons=("Do not chase",),
            data_health=DataStatus.LIVE,
        )
        self.assertEqual(brief.ranking_mode, RankingMode.RULE_EVIDENCE)
        self.assertIsNone(brief.ai_summary)
        self.assertIn("校准成功概率尚不可用", brief.summary_facts)
        self.assertFalse(hasattr(brief, "big_trend_state"))

    def test_calibrated_core_sets_calibrated_ranking_mode(self) -> None:
        brief = build_decision_brief(
            as_of=datetime.now(timezone.utc),
            market_posture="RISK_ON_TREND",
            aggression_level=70,
            core_candidates=(
                self.action(
                    "X.US",
                    ActionState.EXECUTABLE,
                    probability=0.62,
                ),
            ),
            holding_actions=(),
            avoid_reasons=(),
            data_health=DataStatus.LIVE,
        )
        self.assertEqual(
            brief.ranking_mode,
            RankingMode.CALIBRATED_PROBABILITY,
        )

    def test_direct_brief_rejects_mixed_or_duplicate_lanes(self) -> None:
        executable = self.action("X.US", ActionState.EXECUTABLE)
        holding = self.action("H.US", ActionState.HOLD)
        base = {
            "as_of": datetime.now(timezone.utc),
            "market_posture": "ROTATION",
            "aggression_level": 50,
            "core_opportunities": (executable,),
            "holding_actions": (holding,),
            "avoid_reasons": (),
            "data_health": DataStatus.LIVE,
            "ranking_mode": RankingMode.RULE_EVIDENCE,
            "summary_facts": ("fact",),
        }
        with self.assertRaisesRegex(DecisionContractError, "holding-only"):
            DecisionBrief(
                **{
                    **base,
                    "core_opportunities": (holding,),
                    "holding_actions": (),
                }
            )
        with self.assertRaisesRegex(DecisionContractError, "both core and holding"):
            DecisionBrief(
                **{
                    **base,
                    "holding_actions": (
                        self.action("X.US", ActionState.HOLD),
                    ),
                }
            )
        with self.assertRaisesRegex(DecisionContractError, "unique"):
            DecisionBrief(
                **{
                    **base,
                    "core_opportunities": (executable, executable),
                }
            )


if __name__ == "__main__":
    unittest.main()
