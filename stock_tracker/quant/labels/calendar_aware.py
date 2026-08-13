"""Production boundary that requires complete calendar-aligned labels."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from stock_tracker.core.types import Market

from ..backtest.execution import ExecutionBar
from ..core.calendar import CalendarAlignedBars, InstrumentSessionState
from .triple_barrier import (
    BarrierKind,
    LabelContractError,
    TripleBarrierLabeler,
    TripleBarrierResult,
)


class CalendarAwareTripleBarrierLabeler:
    """Reject raw bar arrays and preserve explicit missing-session semantics."""

    def __init__(self, labeler: TripleBarrierLabeler) -> None:
        self.labeler = labeler

    def label(
        self,
        aligned: CalendarAlignedBars,
        *,
        signal_index: int,
        atr: float,
        requested_quantity: int,
        limit_states: Mapping[date, tuple[bool, bool]] | None = None,
        same_bar_resolution: Mapping[int, BarrierKind] | None = None,
    ) -> TripleBarrierResult:
        if not isinstance(aligned, CalendarAlignedBars):
            raise TypeError("formal labels require CalendarAlignedBars")
        states = limit_states or {}
        execution: list[ExecutionBar] = []
        for bar, session_date, state in zip(
            aligned.bars,
            aligned.session_dates,
            aligned.session_states,
        ):
            if state is not InstrumentSessionState.OPEN:
                execution.append(
                    ExecutionBar(
                        bar,
                        state=state,
                        locked_limit_up=False,
                        locked_limit_down=False,
                    )
                )
                continue
            if aligned.market is Market.A:
                if session_date not in states:
                    raise LabelContractError(
                        f"A-share limit state missing for {session_date.isoformat()}"
                    )
                limit_up, limit_down = states[session_date]
            else:
                limit_up, limit_down = states.get(session_date, (False, False))
            execution.append(
                ExecutionBar(
                    bar,
                    state=state,
                    locked_limit_up=limit_up,
                    locked_limit_down=limit_down,
                )
            )
        return self.labeler.label(
            tuple(execution),
            signal_index=signal_index,
            atr=atr,
            requested_quantity=requested_quantity,
            same_bar_resolution=same_bar_resolution,
        )
