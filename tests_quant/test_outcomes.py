from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, localcontext

from stock_tracker.core.types import Market
from stock_tracker.quant.backtest.market_rules import TradeSide
from stock_tracker.quant.core.outcomes import (
    OutcomeContractError,
    OutcomeEvidenceOrigin,
    OutcomeFillEvidence,
    OutcomePathPoint,
    OutcomeScoreboardPolicy,
    OutcomeState,
    OutcomeTerminalReason,
    ScoreboardState,
    SignalOutcome,
    StrategyScoreboard,
    TradeIntentEvidence,
)
from stock_tracker.quant.data.bar_artifact import DataTrustTier

_BASE = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return character * 64


def _intent(
    side: TradeSide,
    *,
    requested_at: datetime,
    quantity: int = 100,
    snapshot_character: str = "1",
) -> TradeIntentEvidence:
    return TradeIntentEvidence(
        symbol="600001.SH",
        market=Market.A,
        side=side,
        requested_at=requested_at,
        requested_quantity=quantity,
        decision_snapshot_id=_hash(snapshot_character),
        execution_policy_id=_hash("2"),
    )


def _fill(
    intent: TradeIntentEvidence,
    *,
    timestamp: datetime,
    session_index: int,
    fill_price: str,
    reference_price: str | None = None,
    explicit_cost: str = "10",
    quantity: int = 100,
) -> OutcomeFillEvidence:
    return OutcomeFillEvidence(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        market=intent.market,
        side=intent.side,
        timestamp=timestamp,
        session_index=session_index,
        quantity=quantity,
        reference_price=Decimal(reference_price or fill_price),
        fill_price=Decimal(fill_price),
        explicit_cost=Decimal(explicit_cost),
        execution_rule_id=_hash("3"),
        cost_schedule_id=_hash("4"),
        raw_bar_snapshot_id=_hash("5"),
    )


def _path_point(
    *,
    timestamp: datetime,
    session_index: int,
    high: str,
    low: str,
    close: str,
    observable: bool = True,
) -> OutcomePathPoint:
    return OutcomePathPoint(
        timestamp=timestamp,
        session_index=session_index,
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        observable=observable,
    )


def _complete_outcome(
    *,
    exit_price: str = "12",
    origin: OutcomeEvidenceOrigin = OutcomeEvidenceOrigin.LIVE_OBSERVED,
    verified: bool = True,
    synthetic: bool = False,
    signal_suffix: str = "1",
    recorded_offset_days: int = 0,
    market_regime: str = "RISK_ON_TREND",
    classification_id: str | None = "C:SECTOR:TECH",
    horizon_sessions: int = 20,
    model_id: str | None = "model-v1",
    evidence_tier: DataTrustTier | None = None,
    entry_cost: str = "10",
    exit_cost: str = "10",
    quantity: int = 100,
) -> SignalOutcome:
    offset = timedelta(days=recorded_offset_days)
    entry_intent = _intent(
        TradeSide.BUY,
        requested_at=_BASE + offset,
        quantity=quantity,
        snapshot_character="1",
    )
    entry_fill = _fill(
        entry_intent,
        timestamp=_BASE + offset + timedelta(minutes=1),
        session_index=10 + recorded_offset_days * 10,
        fill_price="10",
        explicit_cost=entry_cost,
        quantity=quantity,
    )
    exit_intent = _intent(
        TradeSide.SELL,
        requested_at=_BASE + offset + timedelta(minutes=4),
        quantity=quantity,
        snapshot_character="1",
    )
    exit_fill = _fill(
        exit_intent,
        timestamp=_BASE + offset + timedelta(minutes=5),
        session_index=12 + recorded_offset_days * 10,
        fill_price=exit_price,
        explicit_cost=exit_cost,
        quantity=quantity,
    )
    exit_value = Decimal(exit_price)
    path = (
        _path_point(
            timestamp=_BASE + offset + timedelta(minutes=2),
            session_index=10 + recorded_offset_days * 10,
            high="11",
            low="9.50",
            close="10.50",
        ),
        _path_point(
            timestamp=_BASE + offset + timedelta(minutes=3),
            session_index=11 + recorded_offset_days * 10,
            high="13",
            low="10",
            close="12",
        ),
        _path_point(
            timestamp=_BASE + offset + timedelta(minutes=5),
            session_index=12 + recorded_offset_days * 10,
            high=str(max(Decimal(12), exit_value)),
            low=str(min(Decimal(11), exit_value)),
            close=exit_price,
        ),
    )
    evidence_ids = (_hash("6"),) if verified else ()
    resolved_tier = evidence_tier or (
        DataTrustTier.OPERATIONAL_VERIFIED
        if origin is OutcomeEvidenceOrigin.LIVE_OBSERVED and verified
        else DataTrustTier.BEST_EFFORT
    )
    return SignalOutcome(
        signal_id=f"signal-{signal_suffix}",
        strategy_id="S1_BREAKOUT",
        strategy_version="v1",
        horizon_sessions=horizon_sessions,
        model_id=model_id,
        evidence_tier=resolved_tier,
        symbol="600001.SH",
        market=Market.A,
        instrument_id="CN:SSE:synthetic-600001",
        identity_fact_id=_hash("7"),
        decision_snapshot_id=entry_intent.decision_snapshot_id,
        data_snapshot_id=_hash("8"),
        policy_id=_hash("9"),
        market_regime=market_regime,
        classification_id=classification_id,
        recorded_at=_BASE + offset + timedelta(minutes=6),
        entry_intent=entry_intent,
        entry_fill=entry_fill,
        exit_intent=exit_intent,
        exit_fill=exit_fill,
        path=path,
        path_complete=True,
        invalidation_price=Decimal("9.1"),
        terminal_reason=OutcomeTerminalReason.TARGET,
        origin=origin,
        verified=verified,
        synthetic_fixture_only=synthetic,
        verification_evidence_ids=evidence_ids,
    )


def _open_outcome() -> SignalOutcome:
    entry_intent = _intent(TradeSide.BUY, requested_at=_BASE)
    entry_fill = _fill(
        entry_intent,
        timestamp=_BASE + timedelta(minutes=1),
        session_index=10,
        fill_price="10",
    )
    return SignalOutcome(
        signal_id="open-signal",
        strategy_id="S1_BREAKOUT",
        strategy_version="v1",
        horizon_sessions=20,
        model_id="model-v1",
        evidence_tier=DataTrustTier.BEST_EFFORT,
        symbol="600001.SH",
        market=Market.A,
        instrument_id="CN:SSE:synthetic-600001",
        identity_fact_id=_hash("7"),
        decision_snapshot_id=entry_intent.decision_snapshot_id,
        data_snapshot_id=_hash("8"),
        policy_id=_hash("9"),
        market_regime="ROTATION",
        classification_id=None,
        recorded_at=_BASE + timedelta(minutes=3),
        entry_intent=entry_intent,
        entry_fill=entry_fill,
        exit_intent=None,
        exit_fill=None,
        path=(
            _path_point(
                timestamp=_BASE + timedelta(minutes=2),
                session_index=10,
                high="10.50",
                low="9.90",
                close="10.20",
            ),
        ),
        path_complete=False,
        invalidation_price=Decimal("9.1"),
        terminal_reason=None,
        origin=OutcomeEvidenceOrigin.LIVE_OBSERVED,
        verified=False,
        synthetic_fixture_only=False,
        verification_evidence_ids=(),
    )


def _no_entry_outcome() -> SignalOutcome:
    entry_intent = _intent(TradeSide.BUY, requested_at=_BASE)
    return SignalOutcome(
        signal_id="no-entry-signal",
        strategy_id="S1_BREAKOUT",
        strategy_version="v1",
        horizon_sessions=20,
        model_id="model-v1",
        evidence_tier=DataTrustTier.BEST_EFFORT,
        symbol="600001.SH",
        market=Market.A,
        instrument_id="CN:SSE:synthetic-600001",
        identity_fact_id=_hash("7"),
        decision_snapshot_id=entry_intent.decision_snapshot_id,
        data_snapshot_id=_hash("8"),
        policy_id=_hash("9"),
        market_regime="ROTATION",
        classification_id=None,
        recorded_at=_BASE + timedelta(minutes=1),
        entry_intent=entry_intent,
        entry_fill=None,
        exit_intent=None,
        exit_fill=None,
        path=(),
        path_complete=False,
        invalidation_price=None,
        terminal_reason=OutcomeTerminalReason.ORDER_REJECTED,
        origin=OutcomeEvidenceOrigin.LIVE_OBSERVED,
        verified=False,
        synthetic_fixture_only=False,
        verification_evidence_ids=(),
    )


def _policy(minimum: int = 2) -> OutcomeScoreboardPolicy:
    return OutcomeScoreboardPolicy(
        policy_version="test-scoreboard-v1",
        minimum_real_samples=minimum,
        minimum_bucket_samples=min(2, minimum),
        recent_window=20,
    )


class TestSignalOutcome(unittest.TestCase):
    def test_complete_outcome_derives_costed_metrics(self) -> None:
        outcome = _complete_outcome()
        self.assertEqual(outcome.state, OutcomeState.COMPLETE)
        self.assertTrue(outcome.real_scoreboard_eligible)
        self.assertIsNotNone(outcome.metrics)
        assert outcome.metrics is not None
        self.assertEqual(outcome.metrics.entry_all_in_unit_price, Decimal("10.1"))
        self.assertEqual(outcome.metrics.exit_net_unit_price, Decimal("11.9"))
        self.assertEqual(outcome.metrics.realized_r, Decimal("1.8"))
        self.assertEqual(outcome.metrics.mfe_r, Decimal("2.9"))
        self.assertEqual(outcome.metrics.mae_r, Decimal("-0.6"))
        self.assertEqual(outcome.metrics.holding_sessions, 2)
        self.assertEqual(outcome.metrics.total_cost, Decimal(20))
        self.assertEqual(outcome.risk_per_share, Decimal(1))

    def test_implicit_cost_is_derived_without_double_counting_fill_price(self) -> None:
        intent = _intent(TradeSide.BUY, requested_at=_BASE)
        fill = _fill(
            intent,
            timestamp=_BASE + timedelta(minutes=1),
            session_index=1,
            reference_price="9.8",
            fill_price="10",
            explicit_cost="10",
        )
        self.assertEqual(fill.implicit_cost, Decimal(20))
        self.assertEqual(fill.total_cost, Decimal(30))
        self.assertEqual(fill.all_in_unit_price, Decimal("10.1"))
        with self.assertRaises(TypeError):
            replace(fill, implicit_cost=Decimal(0))

    def test_no_entry_and_open_outcomes_are_not_scoreboard_eligible(self) -> None:
        no_entry = _no_entry_outcome()
        open_outcome = _open_outcome()
        self.assertEqual(no_entry.state, OutcomeState.NO_ENTRY)
        self.assertIn("ENTRY_NOT_FILLED", no_entry.blockers)
        self.assertIsNone(no_entry.metrics)
        self.assertEqual(open_outcome.state, OutcomeState.OPEN)
        self.assertIn("EXIT_NOT_FILLED", open_outcome.blockers)
        self.assertIsNone(open_outcome.metrics)
        self.assertFalse(no_entry.real_scoreboard_eligible)
        self.assertFalse(open_outcome.real_scoreboard_eligible)

    def test_synthetic_and_paper_outcomes_cannot_enter_real_scoreboard(self) -> None:
        synthetic = _complete_outcome(
            origin=OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
            verified=False,
            synthetic=True,
        )
        paper = _complete_outcome(
            origin=OutcomeEvidenceOrigin.PAPER_RECORDED,
            verified=False,
            synthetic=False,
        )
        self.assertIn("SYNTHETIC_FIXTURE_ONLY", synthetic.blockers)
        self.assertIn("PAPER_OUTCOME_ONLY", paper.blockers)
        self.assertFalse(synthetic.real_scoreboard_eligible)
        self.assertFalse(paper.real_scoreboard_eligible)

    def test_float_bool_and_nonfinite_values_fail_closed(self) -> None:
        intent = _intent(TradeSide.BUY, requested_at=_BASE)
        with self.assertRaises(OutcomeContractError):
            replace(intent, requested_quantity=True)
        fill = _fill(
            intent,
            timestamp=_BASE + timedelta(minutes=1),
            session_index=1,
            fill_price="10",
        )
        with self.assertRaises(OutcomeContractError):
            replace(fill, fill_price=10.0)
        with self.assertRaises(OutcomeContractError):
            replace(fill, explicit_cost=Decimal("NaN"))

    def test_identity_quantity_time_and_path_mismatches_fail_closed(self) -> None:
        outcome = _complete_outcome()
        assert outcome.exit_fill is not None
        with self.assertRaises(OutcomeContractError):
            replace(
                outcome,
                exit_fill=replace(outcome.exit_fill, quantity=99),
            )
        with self.assertRaises(OutcomeContractError):
            replace(outcome, recorded_at=_BASE)
        with self.assertRaises(OutcomeContractError):
            replace(outcome, path_complete=False)
        with self.assertRaises(OutcomeContractError):
            replace(outcome, invalidation_price=Decimal("10.1"))
        with self.assertRaises(OutcomeContractError):
            replace(
                outcome,
                path=tuple(replace(point, observable=False) for point in outcome.path),
            )

    def test_origin_and_verification_cannot_be_self_promoted(self) -> None:
        synthetic = _complete_outcome(
            origin=OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
            verified=False,
            synthetic=True,
        )
        with self.assertRaises(OutcomeContractError):
            replace(
                synthetic,
                origin=OutcomeEvidenceOrigin.LIVE_OBSERVED,
            )
        with self.assertRaises(OutcomeContractError):
            replace(
                synthetic,
                verified=True,
                verification_evidence_ids=(_hash("6"),),
            )

    def test_derived_state_metrics_and_id_cannot_be_injected(self) -> None:
        outcome = _complete_outcome()
        for changes in (
            {"state": OutcomeState.OPEN},
            {"risk_per_share": Decimal("0.1")},
            {"metrics": None},
            {"outcome_id": _hash("f")},
            {"blockers": ()},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(outcome, **changes)

    def test_cost_path_and_policy_changes_change_outcome_identity(self) -> None:
        base = _complete_outcome()
        cost_changed = _complete_outcome(exit_cost="11")
        path_changed = replace(
            base,
            path=(
                base.path[0],
                replace(base.path[1], high=Decimal("13.1")),
                base.path[2],
            ),
        )
        policy_changed = replace(base, policy_id=_hash("e"))
        self.assertNotEqual(base.outcome_id, cost_changed.outcome_id)
        self.assertNotEqual(base.outcome_id, path_changed.outcome_id)
        self.assertNotEqual(base.outcome_id, policy_changed.outcome_id)


class TestStrategyScoreboard(unittest.TestCase):
    def test_insufficient_real_evidence_hides_all_performance_metrics(self) -> None:
        real = _complete_outcome(signal_suffix="real")
        scoreboard = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(real,),
        )
        self.assertEqual(
            scoreboard.state,
            ScoreboardState.INSUFFICIENT_REAL_EVIDENCE,
        )
        self.assertIsNone(scoreboard.metrics)
        self.assertEqual(scoreboard.bucket_metrics, ())
        self.assertEqual(len(scoreboard.eligible_outcome_ids), 1)
        self.assertEqual(scoreboard.excluded_counts, ())

    def test_scoreboard_rejects_mixed_evidence_tiers(self) -> None:
        real = _complete_outcome(signal_suffix="real")
        synthetic = _complete_outcome(
            origin=OutcomeEvidenceOrigin.SYNTHETIC_FIXTURE,
            verified=False,
            synthetic=True,
            signal_suffix="synthetic",
        )
        with self.assertRaises(OutcomeContractError):
            StrategyScoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                    window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE + timedelta(days=10),
                policy=_policy(minimum=2),
                outcomes=(real, synthetic),
            )

    def test_real_scoreboard_computes_costed_metrics_and_buckets(self) -> None:
        winner = _complete_outcome(
            exit_price="12",
            signal_suffix="winner",
            recorded_offset_days=0,
        )
        loser = _complete_outcome(
            exit_price="9",
            signal_suffix="loser",
            recorded_offset_days=1,
        )
        scoreboard = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(loser, winner),
        )
        self.assertEqual(
            scoreboard.state,
            ScoreboardState.REAL_EVIDENCE_AVAILABLE,
        )
        self.assertIsNotNone(scoreboard.metrics)
        assert scoreboard.metrics is not None
        self.assertEqual(scoreboard.metrics.sample_count, 2)
        self.assertEqual(scoreboard.metrics.win_rate, Decimal("0.5"))
        self.assertEqual(scoreboard.metrics.average_r, Decimal("0.3"))
        self.assertEqual(scoreboard.metrics.median_r, Decimal("0.3"))
        self.assertEqual(scoreboard.metrics.net_expectancy_r, Decimal("0.3"))
        self.assertEqual(scoreboard.metrics.profit_factor_r, Decimal("1.5"))
        self.assertEqual(scoreboard.metrics.max_drawdown_r, Decimal("1.2"))
        self.assertEqual(
            scoreboard.metrics.recent_weighted_expectancy_r,
            Decimal("-0.2"),
        )
        self.assertEqual(len(scoreboard.bucket_metrics), 2)

    def test_process_decimal_context_cannot_change_outcome_or_scoreboard(self) -> None:
        def build() -> tuple[SignalOutcome, SignalOutcome, StrategyScoreboard]:
            winner = _complete_outcome(
                exit_price="10.777777777777777777",
                signal_suffix="context-winner",
                entry_cost="1",
                exit_cost="1",
                quantity=3,
            )
            loser = _complete_outcome(
                exit_price="9.333333333333333333",
                signal_suffix="context-loser",
                recorded_offset_days=1,
                entry_cost="1",
                exit_cost="1",
                quantity=3,
            )
            scoreboard = StrategyScoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE + timedelta(days=10),
                policy=_policy(minimum=2),
                outcomes=(loser, winner),
            )
            return winner, loser, scoreboard

        baseline_winner, baseline_loser, baseline_scoreboard = build()
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            constrained_winner, constrained_loser, constrained_scoreboard = build()

        self.assertEqual(constrained_winner.metrics, baseline_winner.metrics)
        self.assertEqual(
            constrained_winner.risk_per_share,
            baseline_winner.risk_per_share,
        )
        self.assertEqual(constrained_winner.outcome_id, baseline_winner.outcome_id)
        self.assertEqual(constrained_loser.metrics, baseline_loser.metrics)
        self.assertEqual(constrained_loser.outcome_id, baseline_loser.outcome_id)
        self.assertEqual(constrained_scoreboard.metrics, baseline_scoreboard.metrics)
        self.assertEqual(
            constrained_scoreboard.bucket_metrics,
            baseline_scoreboard.bucket_metrics,
        )
        self.assertEqual(
            constrained_scoreboard.scoreboard_id,
            baseline_scoreboard.scoreboard_id,
        )

    def test_all_winners_do_not_emit_infinite_profit_factor(self) -> None:
        first = _complete_outcome(signal_suffix="first")
        second = _complete_outcome(
            signal_suffix="second",
            recorded_offset_days=1,
        )
        scoreboard = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(first, second),
        )
        assert scoreboard.metrics is not None
        self.assertIsNone(scoreboard.metrics.profit_factor_r)
        self.assertIn(
            "PROFIT_FACTOR_UNDEFINED_NO_LOSSES",
            scoreboard.metric_notes,
        )

    def test_input_order_is_normalized_and_identity_is_deterministic(self) -> None:
        first = _complete_outcome(signal_suffix="first")
        second = _complete_outcome(
            signal_suffix="second",
            recorded_offset_days=1,
        )
        left = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(first, second),
        )
        right = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=2),
            outcomes=(second, first),
        )
        self.assertEqual(left.scoreboard_id, right.scoreboard_id)
        self.assertEqual(left.outcomes, right.outcomes)

    def test_future_and_mixed_strategy_outcomes_fail_closed(self) -> None:
        outcome = _complete_outcome()
        with self.assertRaises(OutcomeContractError):
            StrategyScoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                    window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE,
                policy=_policy(minimum=1),
                outcomes=(outcome,),
            )
        with self.assertRaises(OutcomeContractError):
            StrategyScoreboard(
                strategy_id="S2_PULLBACK",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                    window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE + timedelta(days=10),
                policy=_policy(minimum=1),
                outcomes=(outcome,),
            )

    def test_duplicate_signal_and_mixed_policy_are_rejected(self) -> None:
        first = _complete_outcome(signal_suffix="duplicate")
        duplicate_signal = _complete_outcome(
            exit_price="11.5",
            signal_suffix="duplicate",
            recorded_offset_days=1,
        )
        with self.assertRaises(OutcomeContractError):
            StrategyScoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                    window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE + timedelta(days=10),
                policy=_policy(minimum=2),
                outcomes=(first, duplicate_signal),
            )
        mixed_policy = replace(
            _complete_outcome(
                signal_suffix="other",
                recorded_offset_days=1,
            ),
            policy_id=_hash("e"),
        )
        with self.assertRaises(OutcomeContractError):
            StrategyScoreboard(
                strategy_id="S1_BREAKOUT",
                strategy_version="v1",
                market=Market.A,
                horizon_sessions=20,
                model_id="model-v1",
                evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                    window_start=_BASE,
                window_end=_BASE + timedelta(days=9),
                as_of=_BASE + timedelta(days=10),
                policy=_policy(minimum=2),
                outcomes=(first, mixed_policy),
            )

    def test_scoreboard_derived_fields_cannot_be_relabelled(self) -> None:
        scoreboard = StrategyScoreboard(
            strategy_id="S1_BREAKOUT",
            strategy_version="v1",
            market=Market.A,
            horizon_sessions=20,
            model_id="model-v1",
            evidence_tier=DataTrustTier.OPERATIONAL_VERIFIED,
            window_start=_BASE,
            window_end=_BASE + timedelta(days=9),
            as_of=_BASE + timedelta(days=10),
            policy=_policy(minimum=1),
            outcomes=(_complete_outcome(),),
        )
        for changes in (
            {"state": ScoreboardState.INSUFFICIENT_REAL_EVIDENCE},
            {"metrics": None},
            {"eligible_outcome_ids": ()},
            {"scoreboard_id": _hash("f")},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(scoreboard, **changes)


if __name__ == "__main__":
    unittest.main()
