# Stage 5A Decision Quality and Model Promotion Gate

Date: 2026-08-20

## Purpose

Stage 5A turns separate model, data, replay, outcome and holdout checks into one
fail-closed promotion assessment. A good model metric alone is never sufficient.

## Bound evidence

Every assessment binds:

```text
strategy / market / horizon
baseline / champion / challenger model IDs
shared model-comparison identity
code and configuration IDs
dataset / feature / label IDs
calibration evidence
leakage audit
negative controls
experiment and registry snapshots
Strategy Scoreboard
PIT Replay plan
Frozen Holdout record
data trust tier
license evidence
recorded trial count
```

All content identities are included in the assessment fingerprint. Derived
decisions, blockers, state and ID are not constructor inputs.

## Assessment states

```text
BLOCKED
CHALLENGER_DIAGNOSTIC
PROMOTION_REJECTED
PROMOTION_ELIGIBLE
```

- `CHALLENGER_DIAGNOSTIC` is the maximum state for synthetic evidence.
- `PROMOTION_REJECTED` means formal evidence exists but the challenger did not
  strictly beat baseline and champion under the configured comparison gate.
- `PROMOTION_ELIGIBLE` means the evidence gate passed; it does not deploy or
  register the model.

## Formal promotion gates

Formal eligibility requires all of the following:

1. Data trust meets the versioned minimum, currently T3/research-grade.
2. Data/source licensing is explicitly cleared.
3. Evidence is verified and complete.
4. Calibration evidence is verified.
5. Leakage audit and negative controls pass.
6. Multiple-testing trial identity is recorded.
7. Real Strategy Scoreboard evidence is available.
8. Formal PIT Replay is ready and formally eligible.
9. Frozen Holdout is exposed exactly within policy, not sealed/unseen,
   compromised or overexposed.
10. Holdout configuration and dataset identities match the evaluation evidence.
11. Challenger strictly improves on both baseline and current champion.
12. Score buckets are monotonic and temporal/regime stability gates pass through
    the existing ChampionGate.

Missing metrics and evidence remain blockers; they are never replaced with zero,
neutral or favorable values.

## Non-actions

The assessment always exposes:

```text
writes_model_registry = false
deploys_model = false
changes_runtime_weight = false
creates_order = false
```

Any controlled production promotion must be a separate reviewed workflow.

## Current project status

The engineering gate is implemented and tested. Formal promotion remains
blocked in the real project because licensing is `LICENSE_PENDING`, the complete
research data chain is `T3_NOT_REACHED`, formal replay is not yet available and
real outcome samples are still insufficient.
