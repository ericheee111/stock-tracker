# Stage 4C Outcome Attribution and Version Comparison Contract

Date: 2026-08-20

## Purpose

Stage 4C answers two questions without inventing causality:

1. What observable terminal and path facts explain a recorded outcome?
2. Did a candidate strategy version perform better than a baseline version on
   the same independently identified cohort and evaluation window?

## Authoritative inputs

Attribution accepts only an immutable `SignalOutcome` produced by the Stage 4A
contract. The outcome binds strategy/model identity, market, horizon, evidence
tier, instrument identity, decision/data snapshots, entry and exit intents,
observed fills, costs, invalidation, path, terminal reason and verification.

The caller cannot supply attribution findings, state or attribution ID.

## Descriptive findings

The evaluator may emit:

- entry not filled;
- data invalid;
- target captured;
- stop loss;
- timeout;
- manual exit;
- trailing stop;
- broken trend;
- material cost drag;
- large adverse excursion;
- early-exit opportunity cost;
- favorable excursion not retained.

These findings describe evidence already in the outcome. They do not claim that
one factor caused the market move or that changing one rule would necessarily
improve future performance.

## Formal versus diagnostic attribution

`FORMAL_READY` requires a complete, verified, live-observed, real-scoreboard-
eligible outcome. Paper and synthetic outcomes remain `DIAGNOSTIC_ONLY` and
carry explicit blockers.

Open outcomes cannot receive terminal attribution.

## Version comparison identity

A baseline and candidate are comparable only when all of the following match:

```text
strategy_id
market
horizon_sessions
evidence_tier
cohort_id
window_start
window_end
scoreboard policy
```

The strategy versions must differ. Both Scoreboards must have sufficient real
evidence and non-null metrics.

The comparison reports deltas in:

- average R;
- recent weighted expectancy;
- maximum drawdown;
- win rate.

A version can be `CANDIDATE_BETTER`, `CANDIDATE_NOT_BETTER` or `BLOCKED` under a
versioned policy. This is not a deployment or weight-change action.

## Safety boundaries

- No synthetic-to-real promotion.
- No cross-market or cross-horizon aggregation.
- No different-cohort comparison.
- No missing-metric defaulting.
- No runtime weight change.
- No model deployment.
- No order creation.

## Current evidence status

The engineering contract and synthetic/contract tests are implemented. Real
aggregate claims remain gated by independent verified outcomes. The wider
project remains `LICENSE_PENDING` and `T3_NOT_REACHED`.
