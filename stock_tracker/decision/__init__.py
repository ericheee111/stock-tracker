"""Side-effect-free Stage 1 product decision contracts and helpers."""

from .action_mapper import map_signal_to_action
from .brief import (
    build_decision_brief,
    ranking_mode_for,
    select_core_opportunities,
    sort_holding_actions,
)
from .position_sizing import PositionSizer, size_position
from .runtime import (
    RuntimeDecisionRecord,
    build_signal_record,
    build_unbound_position_record,
)
from .trade_plan import build_trade_plan
from .types import (
    ActionDecision,
    ActionState,
    BigTrendState,
    BlockerSeverity,
    DecisionAction,
    DecisionBlocker,
    DecisionBrief,
    DecisionContractError,
    PlanVariant,
    PositionSizeResult,
    ProbabilityEvidenceLevel,
    RankingMode,
    RiskMode,
    TradePlan,
    UserPortfolioProfile,
)

__all__ = [
    "ActionDecision",
    "ActionState",
    "BigTrendState",
    "BlockerSeverity",
    "DecisionAction",
    "DecisionBlocker",
    "DecisionBrief",
    "DecisionContractError",
    "PlanVariant",
    "PositionSizeResult",
    "PositionSizer",
    "ProbabilityEvidenceLevel",
    "RankingMode",
    "RiskMode",
    "RuntimeDecisionRecord",
    "TradePlan",
    "UserPortfolioProfile",
    "build_decision_brief",
    "build_signal_record",
    "build_trade_plan",
    "build_unbound_position_record",
    "map_signal_to_action",
    "ranking_mode_for",
    "select_core_opportunities",
    "size_position",
    "sort_holding_actions",
]
