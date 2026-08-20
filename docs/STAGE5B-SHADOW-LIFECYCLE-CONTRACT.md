# Stage 5B Shadow Validation and Strategy Lifecycle Contract

Date: 2026-08-20

## Purpose

Stage 5B evaluates a candidate or active strategy on new samples and recommends
a lifecycle state. It never applies the recommendation automatically.

## Shadow evidence requirements

Formal Shadow evidence must be:

- out of sample;
- bound to an immutable snapshot and run ID;
- verified and complete;
- operational-verified or higher under the versioned policy;
- collected with production weight exactly zero;
- collected without creating orders;
- independent of the Frozen Holdout used for formal promotion;
- bound to the exact Stage 5A assessment, long-term Scoreboard and recent
  Scoreboard.

Synthetic Shadow evidence remains `DIAGNOSTIC_ONLY` and can never recommend
`ACTIVE`.

## Lifecycle states

```text
SHADOW
ACTIVE
WATCH
DOWNWEIGHTED
BLOCKED
RETIRED
```

The assessment considers:

- Stage 5A promotion eligibility;
- long-term and recent real-evidence Scoreboards;
- recent net expectancy;
- recent versus long-term average R;
- calibration ECE regression;
- maximum-drawdown regression;
- new Shadow sample count;
- consecutive severe blocked windows.

Thresholds are versioned and monotonic. A recovered strategy is not retired
merely because old windows were blocked; retirement requires repeated blocked
history **and a currently severe condition**. `RETIRED` is terminal within this
contract.

## Identity requirements

Long-term and recent Scoreboards must share:

```text
strategy_id
strategy_version
model_id
market
horizon_sessions
evidence_tier
```

Their windows may differ, but the Shadow evidence must bind both exact
Scoreboard IDs. A sample-count mismatch or identity mismatch blocks the
assessment.

## Non-actions

The contract always reports:

```text
changes_runtime_state = false
changes_runtime_weight = false
deploys_model = false
creates_order = false
```

A separate reviewed deployment/control-plane workflow is required to act on a
recommendation.

## Current project status

The policy and tests are implemented. A real Shadow collection service and
sufficient new verified samples are not yet present. The wider project remains
`LICENSE_PENDING` and `T3_NOT_REACHED`.
