"""信号状态机单元测试（§7.4 / PRD #15）。

验证：
- 新信号推导（proposed → 实际态）。
- 既有信号合法迁移。
- 非法迁移被回退（维持原态）。
- DQ 阻断 → DATA_INVALID。
- 追高 → OVEREXTENDED。
- 硬阻断（block_reason）→ 降级 WATCH。
- next_trigger 与 what_changed 字段生成。
- freshness 半衰期衰减与 is_expired。
- VALID_TRANSITIONS 表自洽。
"""

import unittest
from datetime import datetime, timedelta

from stock_tracker.core import types as T
from stock_tracker.signals.state_machine import (SignalStateMachine, VALID_TRANSITIONS,
                                                 freshness, is_expired)
from stock_tracker.signals.risk_gate import RiskDecision
from stock_tracker.strategies.base import SignalCandidate
from tests._common import make_quote, make_bars, make_regime, make_sector, make_ctx


def _decision(allowed: bool = True, overextended: bool = False,
              block_reason=None) -> RiskDecision:
    return RiskDecision(allowed=allowed, overextended=overextended,
                        reasons=[], block_reason=block_reason)


def _candidate(proposed=T.SignalState.ARMED_BREAKOUT, trigger=106.0,
               reward_risk=3.0, next_trigger: str = "放量站上 106 触发") -> SignalCandidate:
    return SignalCandidate(
        symbol="600519.SH", market=T.Market.A, strategy_id="S1",
        proposed_state=proposed, entry_low=99.0, entry_high=105.0,
        trigger_price=trigger, invalidation_price=98.0, reward_risk=reward_risk,
        reason="测试", next_trigger=next_trigger,
    )


def _sig(state, symbol="600519.SH", strategy="S1", changed=None) -> T.Signal:
    return T.Signal(
        signal_id=f"{symbol}:{strategy}", symbol=symbol, market=T.Market.A,
        strategy_id=strategy, state=state,
        state_changed_at=changed or datetime.now(), previous_state=None,
        reason="", trigger_price=106.0, next_trigger="",
    )


def _scores() -> T.ScoreSet:
    return T.ScoreSet(opportunity=70, timing=70, risk=40, confidence=70)


class TestNewSignal(unittest.TestCase):
    def setUp(self):
        self.sm = SignalStateMachine()

    def _ctx(self, last=110.0, dq=None):
        q = make_quote(last=last, prev_close=100.0, high=112.0, low=99.0)
        return make_ctx(quote=q, bars=make_bars(), regime=make_regime(),
                        sector=make_sector(), dq=dq)

    def test_new_watch(self):
        sig = self.sm.decide(None, _candidate(proposed=T.SignalState.WATCH),
                              _decision(), _scores(), self._ctx())
        self.assertEqual(sig.state, T.SignalState.WATCH)
        self.assertIsNone(sig.previous_state)

    def test_new_armed_triggers_when_price_crosses(self):
        sig = self.sm.decide(None, _candidate(proposed=T.SignalState.ARMED_BREAKOUT,
                                              trigger=106.0),
                              _decision(), _scores(), self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.TRIGGERED)

    def test_new_armed_no_trigger_when_below(self):
        # last < trigger → 仍 ARMED（未触发）
        sig = self.sm.decide(None, _candidate(proposed=T.SignalState.ARMED_BREAKOUT,
                                              trigger=106.0),
                              _decision(), _scores(), self._ctx(last=100.0))
        self.assertEqual(sig.state, T.SignalState.ARMED_BREAKOUT)


class TestExistingTransition(unittest.TestCase):
    def setUp(self):
        self.sm = SignalStateMachine()

    def _ctx(self, last=110.0):
        q = make_quote(last=last, prev_close=100.0, high=112.0, low=99.0)
        return make_ctx(quote=q, bars=make_bars(), regime=make_regime(),
                        sector=make_sector())

    def test_armed_to_triggered(self):
        existing = _sig(T.SignalState.ARMED_BREAKOUT)
        sig = self.sm.decide(existing, _candidate(proposed=T.SignalState.ARMED_BREAKOUT),
                              _decision(), _scores(), self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.TRIGGERED)
        self.assertEqual(sig.previous_state, T.SignalState.ARMED_BREAKOUT)

    def test_overextended_from_armed(self):
        existing = _sig(T.SignalState.ARMED_BREAKOUT)
        sig = self.sm.decide(existing, _candidate(proposed=T.SignalState.ARMED_BREAKOUT),
                              _decision(overextended=True), _scores(),
                              self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.OVEREXTENDED)

    def test_hard_block_downgrades_to_watch(self):
        existing = _sig(T.SignalState.ARMED_BREAKOUT)
        sig = self.sm.decide(existing, _candidate(proposed=T.SignalState.ARMED_BREAKOUT),
                              _decision(allowed=False, block_reason="赔率不足"),
                              _scores(), self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.WATCH)

    def test_invalid_transition_rolled_back(self):
        # TRIGGERED 不能回到 WATCH（非法迁移）→ 维持 TRIGGERED
        existing = _sig(T.SignalState.TRIGGERED)
        sig = self.sm.decide(existing, _candidate(proposed=T.SignalState.WATCH),
                              _decision(), _scores(), self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.TRIGGERED)

    def test_terminal_exit_stays(self):
        existing = _sig(T.SignalState.EXIT)
        sig = self.sm.decide(existing, _candidate(proposed=T.SignalState.WATCH),
                              _decision(), _scores(), self._ctx(last=110.0))
        self.assertEqual(sig.state, T.SignalState.EXIT)

    def test_dq_block_to_data_invalid(self):
        q = make_quote(last=110.0)
        ctx = make_ctx(quote=q, bars=make_bars(), regime=make_regime(),
                        sector=make_sector(),
                        dq=T.DataQuality(T.QualityStatus.STALE, 40, ["stale"]))
        sig = self.sm.decide(None, _candidate(proposed=T.SignalState.ARMED_BREAKOUT),
                              _decision(), _scores(), ctx)
        self.assertEqual(sig.state, T.SignalState.DATA_INVALID)


class TestFields(unittest.TestCase):
    def setUp(self):
        self.sm = SignalStateMachine()

    def test_next_trigger_copied(self):
        ctx = make_ctx(quote=make_quote(last=110.0), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        cand = _candidate(next_trigger="自定义触发条件")
        sig = self.sm.decide(None, cand, _decision(), _scores(), ctx)
        self.assertEqual(sig.next_trigger, "自定义触发条件")

    def test_what_changed_new(self):
        ctx = make_ctx(quote=make_quote(last=110.0), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        sig = self.sm.decide(None, _candidate(), _decision(), _scores(), ctx)
        self.assertTrue(any("新建信号" in c for c in sig.what_changed))

    def test_what_changed_state_change(self):
        existing = _sig(T.SignalState.ARMED_BREAKOUT)
        ctx = make_ctx(quote=make_quote(last=110.0), bars=make_bars(),
                       regime=make_regime(), sector=make_sector())
        sig = self.sm.decide(existing, _candidate(), _decision(), _scores(), ctx)
        self.assertTrue(any("ARMED_BREAKOUT → TRIGGERED" in c for c in sig.what_changed))


class TestFreshness(unittest.TestCase):
    def test_decay(self):
        now = datetime.now()
        f0 = freshness(now, 48.0, now)
        f_old = freshness(now - timedelta(hours=48), 48.0, now)
        self.assertAlmostEqual(f0, 1.0, places=4)
        self.assertLess(f_old, f0)

    def test_is_expired(self):
        now = datetime.now()
        self.assertFalse(is_expired(now, 48.0, now))
        self.assertTrue(is_expired(now - timedelta(hours=200), 48.0, now))


class TestTransitionTable(unittest.TestCase):
    def test_terminal_states_have_no_targets(self):
        self.assertEqual(VALID_TRANSITIONS[T.SignalState.EXIT], set())
        self.assertEqual(VALID_TRANSITIONS[T.SignalState.EXPIRED], set())

    def test_key_transitions_present(self):
        self.assertIn(T.SignalState.TRIGGERED,
                      VALID_TRANSITIONS[T.SignalState.ARMED_BREAKOUT])
        self.assertIn(T.SignalState.ACTIVE,
                      VALID_TRANSITIONS[T.SignalState.TRIGGERED])
        self.assertIn(T.SignalState.WATCH,
                      VALID_TRANSITIONS[T.SignalState.INVALIDATED])
        self.assertIn(T.SignalState.DATA_INVALID,
                      VALID_TRANSITIONS[T.SignalState.COLD])

    def test_each_target_is_a_valid_state(self):
        for src, targets in VALID_TRANSITIONS.items():
            self.assertIn(src, list(T.SignalState))
            for t in targets:
                self.assertIn(t, list(T.SignalState))


if __name__ == "__main__":
    unittest.main()
