# Stage 2A — Fixture Coverage Checklist & Gap Report

> ## ⚠️ SYNTHETIC_FIXTURE_ONLY
>
> This document and every fixture under
> `tests_quant/fixtures/stage2_aux/**` are **synthetic, mechanically generated test
> material**. They contain **no real exchange data, no verified/complete evidence, and no
> T2/T3 claim**. No real strategy result, win rate, return, Sharpe, or drawdown may be
> derived from them. They exist only to give **Agent B (Calendar Adapter)** and
> **Agent C (Security/Universe Adapter)** boundary-case inputs for their parser/CLI
> smoke and unit tests.

- Date: 2026-08-14
- Author role: WorkBuddy HY3 — Agent B/C mechanical test-material & verification assist only
- Based on: `docs/STAGE2-PIT-IDENTITY-CONTRACT.md`, `docs/STAGE2-PARALLEL-EXECUTION-PLAN.md`,
  `docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md`
- Does NOT modify: `stock_tracker/quant/core/**`, `stock_tracker/quant/storage/**`,
  `stock_tracker/quant/data/*.py`, `scripts/*.py`, any Agent Handoff, or the production DB.

## 1. Summary

| Item | Count |
|---|---:|
| Required boundary cases (from task) | 21 |
| Synthetic fixtures produced | 21 |
| Fixture directory | `tests_quant/fixtures/stage2_aux/` |
| Self-check | `tests_quant/fixtures/stage2_aux/selfcheck.py` (passes, 22/22 JSON compliant) |
| Boundaries instantiated by existing **contract** tests | 6 |
| Boundaries with **no** existing contract-test instance | 15 |
| Agent B Calendar adapter tests | 23 PASS after main-lane Review |
| Agent C Security/Universe adapter tests | 35 PASS after main-lane Review |

This checklist was authored before B/C dedicated tests landed. Its `stage2_aux` fixtures remain supplemental mechanical inputs, not authoritative coverage evidence. The current source of truth for exercised adapter behavior is `tests_quant/test_calendar_adapter.py` and `tests_quant/test_security_universe_adapter.py`; the matrix below remains useful as a boundary inventory.

## 2. Coverage matrix

Legend — **Contract test?**: whether the boundary *value* is already instantiated in
`tests_quant/test_calendar.py` / `tests_quant/test_universe.py`. **Fixture?**: provided
in `stage2_aux/`. **Agent B/C test?**: whether an adapter/parser test exists
(`test_calendar_adapter.py` / `test_security_universe_adapter.py`) — currently none.

### Calendar (7)

| Boundary | Fixture | Contract test? | Fixture? | Notes |
|---|---|---|---|---|
| annual notice | `calendar/annual_notice.json` | No | Yes | notice_type=ANNUAL; no contract test for notice type. |
| holiday notice | `calendar/holiday_notice.json` | No | Yes | Separate HOLIDAY notice augmenting annual plan. |
| temporary revision | `calendar/temporary_revision.json` | Partial | Yes | Contract tests generic revision/conflict, not TEMPORARY/TECHNICAL type. |
| DATE-only publication | `calendar/date_only_publication.json` | No | Yes | DATE granularity; intraday replay must be disabled. |
| missing date | `calendar/missing_date.json` | **Yes** | Yes | `test_calendar.py::TestTradingCalendar::test_snapshot_requires_every_civil_date`. |
| duplicate date | `calendar/duplicate_date.json` | Partial | Yes | Same mechanism as `test_conflicting_latest_revision_fails`, but no named duplicate-date input. |
| changed raw bytes | `calendar/changed_raw_bytes.json` | No | Yes | Append-only capture vs silent server replacement. |

### Identity (5)

| Boundary | Fixture | Contract test? | Fixture? | Notes |
|---|---|---|---|---|
| symbol rename | `identity/symbol_rename.json` | **Yes** | Yes | `test_universe.py::test_symbol_change_keeps_one_stable_instrument_identity`. |
| same symbol reused | `identity/same_symbol_reused.json` | No | Yes | Two instrument_ids sharing a code at non-overlapping periods. |
| listing | `identity/listing.json` | No | Yes | Listing event: identity effective_from + membership reason LISTED. |
| delisting | `identity/delisting.json` | Partial | Yes | DELISTED status used in contract tests, but no decision-vs-actual delist event. |
| relisting | `identity/relisting.json` | No | Yes | INCLUDED→EXCLUDED(DELISTED)→INCLUDED(RELISTED) event chain. |

### Status (5)

| Boundary | Fixture | Contract test? | Fixture? | Notes |
|---|---|---|---|---|
| ST | `status/st.json` | No | Yes | risk_designation=ST, still TRADABLE. Not in contract tests. |
| *ST | `status/star_st.json` | No | Yes | risk_designation=STAR_ST. Not in contract tests. |
| suspension | `status/suspension.json` | **Yes** | Yes | SUSPENDED used in `test_snapshot_preserves_suspension_and_delisted_samples`. |
| resume | `status/resume.json` | No | Yes | Contract has **no RESUMED state**; resume = TRADABLE + reason_code. |
| delisting period | `status/delisting_period.json` | No | Yes | Contract has **no DELISTING_PERIOD risk designation**; mapped to OTHER. |

### Universe (4)

| Boundary | Fixture | Contract test? | Fixture? | Notes |
|---|---|---|---|---|
| INCLUDED | `universe/included.json` | **Yes** | Yes | Basic inclusion, exercised throughout `test_universe.py`. |
| EXCLUDED | `universe/excluded.json` | **Yes** | Yes | `test_latest_effective_membership_controls_session`, `complete_records`. |
| missing exit history | `universe/missing_exit_history.json` | No | Yes | Current-list-only must force complete=false (survivorship guard). |
| delisted retained | `universe/delisted_retained.json` | **Yes** | Yes | `snapshot.delisted_symbols` preserves delisted sample. |

## 3. What the fixtures provide (and do not)

Each fixture contains:
- `synthetic_fixture_only: true`, `no_real_strategy_claim: true`, `trust_tier: null`,
  `verified: false`, `complete: false`;
- `raw_artifacts[]`: short, clearly synthetic notice snippets (never copied official text);
- `expected_facts[]`: **parser hints** describing what a correct Agent B/C parser *should*
  emit — these are not verified facts and may be revised by Agent B/C;
- `expected_contract_outcome`: the failure-closed / selection behavior the contract must
  enforce — a test-design expectation, not a claim that the contract already does it.

They are **input material** for Agent B/C adapter tests. They do **not**:
- assert that the contract currently enforces each outcome;
- constitute verified/complete/T2/T3 evidence;
- replace the authoritative-source audit or real captures.

## 4. Coverage follow-up after Agent B/C landed

This section supersedes the pre-adapter snapshot that originally reported 0/21 parser coverage.

### 4.1 Adapter/parser layer — dedicated suites now present

Current dedicated suites are:

```text
tests_quant/test_calendar_adapter.py            23 PASS
tests_quant/test_security_universe_adapter.py   35 PASS
```

They cover the high-risk raw/descriptor/PIT/identity cases directly, including main-lane Review additions for official-domain redirect confinement and delisted-old-instrument + reused-symbol coexistence. The `stage2_aux` corpus remains useful for future mutation/fixture expansion, but D must not infer Trust from fixture count or from this checklist.
### 4.2 Contract-test value gaps (15 boundaries with no instance)

Even at the contract level, these boundary *values* are not instantiated and need either
new contract tests or (preferably) Agent B/C adapter tests built on the fixtures:

| Boundary | Why it matters | Suggested test |
|---|---|---|
| annual notice | notice_type taxonomy not exercised | Parse annual notice → CalendarCoverage + days |
| holiday notice | independent notice augments plan | Holiday notice overrides/augments annual day |
| temporary revision | TECHNICAL/TEMPORARY revision semantics | Append-only temporary revision selection |
| DATE-only publication | no intraday known_at; replay disabled | known_at not back-dated; intraday replay fails |
| duplicate date | same-date conflict, not silent drop | Conflicting same-date → PITConflictError |
| changed raw bytes | silent server replacement risk | New revision created; old bytes retained |
| same symbol reused | symbol is not a stable key | Two instrument_ids, same code, non-overlap |
| listing | listing event, not just status | identity effective_from = listing date |
| delisting | decision date ≠ delisted_on | effective_to = actual delist; sample retained |
| relisting | multi-event chain | Latest effective membership controls session |
| ST | 其他风险警示, still tradable | risk_designation=ST + TRADABLE valid |
| *ST | 退市风险警示, still tradable | risk_designation=STAR_ST + TRADABLE valid |
| resume | no RESUMED enum state | Resume = TRADABLE + reason_code RESUME |
| delisting period | no DELISTING_PERIOD designation | DELISTING + TRADABLE; risk_designation=OTHER |
| missing exit history | survivorship-bias trap | complete=false forced; research promotion refused |

### 4.3 Contract-level limitations discovered (flag for Agent B/C/D)

These are observations from building the fixtures, not fixture defects:

1. **No `RESUMED` trading state.** The `TradingState` enum is
   `{TRADABLE, SUSPENDED, HALTED, UNKNOWN}`. Resume is represented as a `TRADABLE` status
   fact with `reason_code="RESUME"`. Agent B/C/D should confirm this mapping is intended;
   if an explicit state is wanted, the enum must change (main-contract decision).
2. **No `DELISTING_PERIOD` risk designation.** `RiskDesignation` is
   `{NORMAL, ST, STAR_ST, RISK_WARNING, OTHER}`. The delisting period is mapped to
   `listing_state=DELISTING` + `risk_designation=OTHER` (or `RISK_WARNING`). Confirm
   convention; consider adding an explicit value if downstream logic needs it.
3. **`reason` on membership is free text.** The audit suggested
   `LISTED/RELISTED/DELISTED/TYPE_CHANGE/OUT_OF_SCOPE/UNKNOWN`; the contract only requires
   a non-empty string. Agent C should freeze the controlled vocabulary it emits.

## 5. Non-claims (explicit)

- These fixtures are **not** real SSE/SZSE data and imply no real security (synthetic
  codes `607xxx.SH` / `009xxx.SZ` only).
- They do **not** assert `verified=true`, `complete=true`, `T2`, or `T3`.
- They produce **no** real strategy result, win rate, return, Sharpe, or drawdown.
- They do **not** upgrade any Trust Tier; Trust Tier can only rise via the source audit,
  coverage reconciliation, and license confirmation described in Agent A's audit.
- The current project status for this work remains
  `CONTRACT_ONLY / SYNTHETIC_VALIDATED`, never `RESEARCH_GRADE`.

## 6. Recommended next steps for Agent B/C/D

1. **Agent B** — build `calendar_adapter.py` + `test_calendar_adapter.py`; consume the 7
   calendar fixtures; assert `expected_contract_outcome` for each (DATE-only, missing date,
   duplicate date, changed raw bytes especially).
2. **Agent C** — build `security_universe_adapter.py` + `test_security_universe_adapter.py`;
   consume the 14 identity/status/universe fixtures; pay attention to `same_symbol_reused`,
   `relisting`, `missing_exit_history`, and the ST/*ST/resume/delisting-period designations.
3. **Agent D** — fold these fixtures into reconciliation gap reporting; verify the
   survivorship guards (`delisted_retained`, `missing_exit_history`) are detected as
   HARD_BLOCK/TRUST_BLOCK where appropriate.
4. Main contract — decide on the `RESUMED` / `DELISTING_PERIOD` enum gaps (§4.3) before
   Agent C freezes its schema.
5. Re-run `python tests_quant/fixtures/stage2_aux/selfcheck.py` after any fixture edit to
   keep the synthetic-only guarantee.
