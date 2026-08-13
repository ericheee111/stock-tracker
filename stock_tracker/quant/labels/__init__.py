"""Execution-aware labeling contracts."""

from .calendar_aware import CalendarAwareTripleBarrierLabeler
from .triple_barrier import (
    AmbiguousBarError,
    BarrierKind,
    LabelContractError,
    LabelOutcome,
    SameBarPolicy,
    TripleBarrierConfig,
    TripleBarrierLabeler,
    TripleBarrierResult,
)

__all__ = [
    "AmbiguousBarError",
    "BarrierKind",
    "CalendarAwareTripleBarrierLabeler",
    "LabelContractError",
    "LabelOutcome",
    "SameBarPolicy",
    "TripleBarrierConfig",
    "TripleBarrierLabeler",
    "TripleBarrierResult",
]
