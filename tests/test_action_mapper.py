from __future__ import annotations

import unittest

from stock_tracker.core.types import DataStatus, Market, Signal, SignalState
from stock_tracker.decision.action_mapper import map_signal_to_action
from stock_tracker.decision.types import ActionState


class TestActionMapper(unittest.TestCase):
    def _signal(self, state: SignalState, status: DataStatus = DataStatus.LIVE) -> Signal:
        return Signal(
            signal_id="600519.SH:S1",
            symbol="600519.SH",
            market=Market.A,
            strategy_id="S1",
            state=state,
            entry_low=9.5,
            entry_high=10.0,
            trigger_price=10.0,
            invalidation_price=9.0,
            target_1=12.0,
            target_2=13.0,
            reward_risk=2.0,
            data_status=status,
        )

    def test_live_trigger_maps_to_executable(self) -> None:
        result = map_signal_to_action(
            self._signal(SignalState.TRIGGERED), has_position=False
        )
        self.assertEqual(result.action, ActionState.EXECUTABLE)

    def test_stale_holding_does_not_fabricate_exit(self) -> None:
        result = map_signal_to_action(
            self._signal(SignalState.ACTIVE, DataStatus.STALE),
            has_position=True,
            current_price=8.5,
        )
        self.assertEqual(result.action, ActionState.DATA_BLOCKED)

    def test_live_invalidation_maps_to_exit(self) -> None:
        result = map_signal_to_action(
            self._signal(SignalState.ACTIVE),
            has_position=True,
            current_price=8.5,
        )
        self.assertEqual(result.action, ActionState.EXIT)


if __name__ == "__main__":
    unittest.main()
