# Stage 2B–Stage 6A Independent Engineering Review

Date: 2026-08-20

## Verdict

```text
ENGINEERING_READY_FOR_MERGE
LICENSE_STATUS = LICENSE_PENDING
EVIDENCE_TIER_STATUS = T3_NOT_REACHED
```

The reviewed engineering contracts are suitable for merge. This verdict does
not assert that real investment performance, formal T3 replay, formal model
promotion, or live HK/US market support already exists.

## Reviewed scope

The review covers the previously uncommitted Stage 2B–4B stack and the newly
implemented Stage 4C–6A stack:

- corporate-action facts, capture, extraction, reconciliation and adjusted
  market data;
- classification, event intelligence and Big Trend contracts;
- isolated `free-stockdb` T1 sidecar and governance evidence;
- Signal Outcome, costed Strategy Scoreboard and PIT Replay;
- outcome attribution and same-cohort strategy-version comparison;
- unified Decision Quality / model-promotion gate;
- new-sample Shadow validation and lifecycle recommendation;
- A-share, HK Connect and US market-isolation foundation.

## Review method

The review was adversarial and focused on:

- point-in-time visibility and future-information leakage;
- stable instrument identity, symbol rename and symbol reuse;
- corporate-action revision, ex-date and share-listing semantics;
- exact Decimal identity and deterministic fingerprints;
- caller-controlled derived state and `dataclasses.replace()` bypasses;
- transaction-cost double counting;
- incomplete/censored/synthetic outcome contamination;
- Strategy Scoreboard sample identity and aggregation boundaries;
- Frozen Holdout exposure and compromise;
- baseline/champion/challenger comparison identity;
- Shadow sample independence and runtime side effects;
- cross-market reuse of rules, costs, thresholds, models and calibration;
- source-distribution completeness and production-database immutability.

## Findings resolved during review

### 1. Implicit execution cost could be caller supplied and double counted

**Severity:** CRITICAL — resolved.

`OutcomeFillEvidence.implicit_cost` is now derived from adverse
reference-to-fill movement multiplied by quantity. It is not a constructor
input. Observed fill price already contains slippage/impact, so all-in unit price
adds or subtracts only explicit fees. Total-cost reporting still includes the
derived implicit component.

### 2. Initial risk could be caller supplied

**Severity:** CRITICAL — resolved.

`risk_per_share` is now derived from costed entry price and the bound
`invalidation_price`. A filled long entry fails closed when invalidation is
missing or not below the costed entry.

### 3. Scoreboards could mix incomparable samples

**Severity:** CRITICAL — resolved.

A `StrategyScoreboard` now binds and enforces:

```text
strategy_id / strategy_version
market
horizon_sessions
model_id
evidence_tier
window_start / window_end / as_of
decision policy
```

Duplicate signals and mixed identities fail closed. The cohort identity is not
caller supplied; it is deterministically derived from the actual sorted
`signal_id` set.

### 4. Verified evidence was represented by a Boolean without immutable proof

**Severity:** IMPORTANT — resolved.

Decision-quality evidence, Shadow validation, market profiles, Sidecar release
audit and comparison series now bind immutable verification/provenance IDs.
`verified=true` requires evidence IDs, while unverified evidence cannot carry
them. Synthetic evidence remains unverified and BEST_EFFORT.

### 5. Version comparison and lifecycle windows could be cherry-picked

**Severity:** IMPORTANT — resolved.

Strategy-version comparison now requires identical `as_of` in addition to the
same market, horizon, evidence tier, derived cohort, evaluation window and
Scoreboard policy.

Lifecycle comparison now requires:

- the same Scoreboard policy;
- the same `as_of`;
- the same window end;
- a recent window that is a strict subwindow of the long-term window.

### 6. Repeated historical blocked windows could retire a recovered strategy

**Severity:** IMPORTANT — resolved.

Retirement now requires both the configured number of consecutive blocked
windows and a currently severe condition. A recovered strategy is not retired
from stale historical blockers alone.

### 7. Low-trust cross-market profiles could enter Shadow transfer

**Severity:** IMPORTANT — resolved.

Cross-market transfer now requires operational-verified-or-higher source and
target profiles. Even a valid request remains zero-weight, no-order
`SHADOW_ONLY`; production reuse, model deployment and runtime weight changes
remain false.

### 8. Sidecar comparison and audit chronology were not strict enough

**Severity:** IMPORTANT — resolved.

The Sidecar governance contract now requires:

- audit capture time not earlier than observed audit events;
- observed process hashes to belong to the audited binary inventory;
- license and approval evidence IDs;
- exactly one evidenced primary venue category per sample;
- unique instruments in a comparison report;
- verified and complete reference series;
- complete Sidecar comparison series;
- release identity binding for every Sidecar sample.

### 9. Content-addressed fixtures were not archive-portable on Windows

**Severity:** CRITICAL — resolved.

A fresh `git archive HEAD` verification on Windows exposed that
`core.autocrlf=true` could convert exact-raw JSON fixtures to CRLF during
archive export, while their immutable descriptors bind the canonical LF bytes.
The ordinary worktree tests passed, but the committed archive failed closed on
byte size and SHA-256 as designed.

A repository-level `.gitattributes` now fixes all textual research fixtures to
LF and marks PDF fixtures binary. The attributes file is itself protected by
the source-distribution test. This keeps repository blobs, checkouts, archives
and CI inputs byte-identical across Windows and Unix hosts.

## No unresolved critical or important findings

No unresolved critical or important issue remains in the reviewed engineering
scope. Remaining blockers are external evidence/product blockers and are
preserved explicitly rather than bypassed.

## Validation evidence

### Test suites

```text
Runtime unit tests: 364 passed, 1 live-service probe skipped
Quant unit tests: 560 passed
Frozen Stage 4A–6A targeted tests: 107 passed
```

The Quant suite includes the Git-index-aware source-distribution test, which
confirmed all critical new modules, migrations, scripts, tests and the exact-raw
`.gitattributes` policy are tracked.

A fresh plain `git archive HEAD` was then extracted into an isolated directory.
The archived security-universe fixture matched its descriptor exactly
(`byte_size=22854`, SHA-256
`0d4e1d93902f51fd978b47a5719f16a0b46c1c9a89195a26b188f2c2796dfcbe`),
and the archived tree passed all 364 runtime tests and all 560 Quant tests. The
only archive-side skip was the expected Git-index source-distribution check,
because an archive intentionally has no `.git` directory.

### Additional gates

- `python -m compileall -q stock_tracker tests tests_quant scripts`: passed.
- affected/new-file Ruff checks: passed.
- `git diff --cached --check`: passed.
- Quant contract smoke: passed; `synthetic_fixture_only=true` and
  `investment_performance_claim=false`.
- synthetic fixture benchmark: passed; challenger correctly not promoted due
  ECE regression and temporal instability.
- migration CLI default dry-run: passed; four migrations pending, zero applied,
  `database_modified=false`.
- `python -m pip check`: passed.
- production database SHA-256 before and after validation:
  `1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1`.

The repository-wide Ruff run still reports pre-existing Stage 1 style debt in
files outside this changed-file gate. All changed and newly added Python files
in this delivery pass the targeted Ruff gate. `ruff format --check` could not be
run because the workspace command guard classified the word `format` as a disk
format operation; no guard bypass was attempted. Whitespace integrity is
covered by the successful staged diff check.

## Preserved real-world blockers

The following statements remain true after merge:

```text
LICENSE_PENDING
T3_NOT_REACHED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
FORMAL_PIT_REPLAY = BLOCKED until the complete T3 snapshot chain exists
FORMAL_MODEL_PROMOTION = BLOCKED until all evidence gates pass
FREE_STOCKDB = default-disabled T1 WARM/COLD Sidecar only
HK_CONNECT_LIVE_SUPPORT = NOT_YET_IMPLEMENTED
US_LIVE_SUPPORT = NOT_YET_IMPLEMENTED
```

## Non-actions confirmed

The delivered contracts do not:

- manufacture win rate, return, probability or replay evidence;
- write or silently promote the Model Registry;
- deploy a model;
- change runtime strategy weights;
- mutate the production database;
- create or submit orders;
- reuse A-share thresholds, calibration or model artifacts directly in another
  market.
