# Stage 4C–Stage 6A Execution Roadmap

Date: 2026-08-20
Repository: `stock-tracker`
Scope: quant research and governance contracts only

## 1. Executive status

This roadmap follows the Stage 2B–4B contracts already present in the working
tree and completes four additional engineering slices:

| Stage | Scope | Engineering status | Real-world status |
|---|---|---|---|
| 4C | Outcome attribution and same-cohort strategy-version comparison | Implemented and tested | Real aggregation remains sample-gated |
| 5A | Unified decision-quality/model-promotion gate | Implemented and tested | Formal promotion remains blocked by `T3_NOT_REACHED` and `LICENSE_PENDING` |
| 5B | New-sample Shadow validation and lifecycle recommendation | Implemented and tested | No production weight changes or deployment are performed |
| 6A | A/HK Connect/US market-isolation foundation | Implemented and tested | Real HK/US providers, PIT data and calibration remain future work |

The delivered code intentionally does **not** claim that real investment
performance, formal PIT replay, or formal model promotion already exists.

## 2. Cross-stage safety invariants

All four stages preserve these invariants:

1. Synthetic fixtures cannot be relabelled as verified real evidence.
2. `dataclasses.replace()` cannot inject derived state, IDs, blockers, metrics,
   promotion decisions or lifecycle recommendations.
3. Outcome performance is cost-adjusted and based on observed fills; implicit
   price impact is derived rather than caller supplied.
4. Strategy Scoreboards cannot mix market, horizon, model, evidence tier,
   cohort, time window, strategy version or decision-policy identities.
5. Formal model promotion requires strict improvement over both baseline and
   champion under one comparison identity.
6. Frozen Holdout overexposure or compromise fails closed.
7. Shadow validation uses new out-of-sample evidence, zero production weight,
   no orders, and never reuses Frozen Holdout as a recurring sample source.
8. Cross-market reuse is forbidden by default and can only enter a target-market
   validated, zero-weight, no-order Shadow lane.
9. None of these contracts writes the model registry, deploys a model, changes
   runtime weights, modifies the production database or creates orders.

## 3. Stage 4C — Outcome Attribution and Version Comparison

### Delivered

- Deterministic terminal attribution from immutable `SignalOutcome` facts.
- Cost-drag, adverse-excursion and uncaptured-favorable-excursion findings.
- Explicit distinction between descriptive attribution and causal claims.
- Same-cohort version comparison with strict market/horizon/evidence/window
  identity.
- Candidate-better / candidate-not-better / blocked states.
- No automatic weight, deployment or order surface.

### Remaining product work

- Persist verified real outcomes in an append-only production-side evidence
  store without weakening the research contract.
- Build the UI for failure attribution and strategy-version comparison.
- Accumulate enough independent real samples for formal aggregate claims.

## 4. Stage 5A — Decision Quality and Promotion Gate

### Delivered

The unified gate binds:

- baseline, champion and challenger model evaluations;
- code, configuration, dataset, feature, label and calibration identities;
- leakage audit and negative controls;
- multiple-testing/experiment and registry snapshots;
- formal PIT Replay plan;
- real Strategy Scoreboard;
- Frozen Holdout state and exposure count;
- data trust and license evidence.

Possible states are:

```text
BLOCKED
CHALLENGER_DIAGNOSTIC
PROMOTION_REJECTED
PROMOTION_ELIGIBLE
```

`PROMOTION_ELIGIBLE` is an auditable recommendation, not a deployment action.

### Remaining evidence blockers

```text
T3_NOT_REACHED
LICENSE_PENDING
REAL_OUTCOME_SCOREBOARD_UNAVAILABLE (until enough real outcomes exist)
FORMAL_PIT_REPLAY_UNAVAILABLE (until the complete T3 snapshot chain exists)
```

## 5. Stage 5B — Shadow Validation and Lifecycle

### Delivered

A versioned policy recommends one of:

```text
SHADOW
ACTIVE
WATCH
DOWNWEIGHTED
BLOCKED
RETIRED
```

The recommendation uses long-term and recent costed Scoreboards, calibration
regression, drawdown regression, new Shadow sample count and Stage 5A decision
quality. Retirement requires repeated **current severe** blocked windows; a
recovered strategy is not mechanically retired from stale history.

### Remaining product work

- Create a real new-sample Shadow collection service.
- Store lifecycle assessments and require explicit human/controlled deployment
  workflow before any production change.
- Add UI explanations for state changes and affected regimes/classifications.

## 6. Stage 6A — Market Isolation Foundation

### Delivered

Independent market profiles bind:

```text
access scope
settlement currency
time zone
horizons
configuration
calendar
universe
market rules
cost schedule
data snapshot
feature policy
label policy
model
calibration
scoreboard
trust tier
```

The default required scopes are:

```text
A_DOMESTIC
HK_CONNECT
US_CASH
```

Shared config/calendar/universe/rules/costs/data/features/labels/model/
calibration/scoreboard identities across markets fail closed.

Cross-market transfer requests can only become `SHADOW_ONLY`; production reuse
remains false even when explicit target-market validation evidence exists.

### Remaining market work

1. Stage 6B: HK Connect authoritative universe, calendar, trading status,
   corporate actions, costs and operational data adapters.
2. Stage 6C: HK Connect PIT research dataset and market-specific calibration.
3. Stage 6D: US market authoritative universe, delisting history, splits,
   dividends, costs and session rules.
4. Stage 6E: US PIT research dataset, market-specific Scoreboard and Shadow.
5. Stage 6F: independent real-evidence reviews before any ACTIVE state.

## 7. Acceptance gates

Before merge/push:

- targeted Stage 4C–6A tests pass;
- complete runtime and quant unit-test suites pass;
- source-distribution test confirms all critical files are tracked;
- Ruff format/check pass;
- `compileall`, contract smoke, fixture benchmark and migration dry-run pass;
- production database hash remains unchanged;
- independent adversarial review finds no unresolved critical/important issue;
- `LICENSE_PENDING` and `T3_NOT_REACHED` remain explicit in documentation and
  release report.

## 8. Non-goals

This delivery does not:

- manufacture real win rate, return, probability or replay evidence;
- promote or deploy a model;
- alter live strategy weights;
- activate HK/US data providers;
- reuse A-share thresholds in another market;
- create or submit orders.
