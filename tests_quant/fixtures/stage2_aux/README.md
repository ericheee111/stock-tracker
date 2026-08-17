# Stage 2A — Auxiliary Synthetic Fixture Corpus (stage2_aux)

> ## ⚠️ SYNTHETIC_FIXTURE_ONLY
>
> Every file in this directory is a **synthetic, mechanically generated test fixture**.
> It does **not** contain real exchange data, real security masters, real calendars,
> or any verified/complete/T2/T3 evidence. No real strategy result, win rate, return,
> Sharpe, or drawdown may be derived from these files.
>
> These fixtures exist **only** to give Agent B (Calendar Adapter) and Agent C
> (Security/Universe Adapter) concrete boundary-case material for their parser/CLI
> smoke and unit tests. They are not production inputs and never enter the research
> identity snapshot on their own.

## Why this corpus exists

- Agent A's authoritative-source audit (`docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md`)
  froze a large set of failure-closed and revision boundary cases.
- The main contract tests (`test_calendar.py`, `test_universe.py`) exercise generic
  mechanics but do **not** instantiate the specific notice types and event values.
- Agent B/C adapter tests (`test_calendar_adapter.py`, `test_security_universe_adapter.py`)
  are not present yet, so the raw-artifact → parser boundary is uncovered at the adapter layer.
- This corpus supplies the missing boundary material.

## Conventions

1. **Synthetic symbols only.** Codes use non-real prefixes so they can never be
   mistaken for a live security:
   - Shanghai: `607001.SH`, `607002.SH`, `607003.SH`, `607004.SH`
   - Shenzhen: `009001.SZ`, `009002.SZ`, `009003.SZ`, `009004.SZ`
2. **No trust claims.** Each fixture sets `verified: false`, `complete: false`,
   `trust_tier: null`. Adapters must not upgrade these.
3. **Raw artifacts are illustrative.** `raw_artifacts[].bytes` / `raw_text` are short,
   clearly synthetic snippets — never copied official prose. `sha256` values are
   placeholder digests, not real captures.
4. **`expected_facts` are parser hints, not truth.** They describe what a correct
   parser *should* emit for Agent B/C to assert against. They are not verified facts.
5. **`expected_contract_outcome`** describes the failure-closed or selection behavior
   the contract must enforce. It is a test-design expectation, not a claim that the
   contract already does it.
6. Every fixture carries `synthetic_fixture_only: true` and `no_real_strategy_claim: true`.

## Directory layout

```text
stage2_aux/
  README.md
  manifest.json
  selfcheck.py
  calendar/   annual_notice, holiday_notice, temporary_revision,
              date_only_publication, missing_date, duplicate_date, changed_raw_bytes
  identity/   symbol_rename, same_symbol_reused, listing, delisting, relisting
  status/     st, star_st, suspension, resume, delisting_period
  universe/   included, excluded, missing_exit_history, delisted_retained
```

## Validation

Run `python tests_quant/fixtures/stage2_aux/selfcheck.py` from the repo root to confirm
every fixture is well-formed, synthetic-only, and free of any verified/complete/T2/T3 claim.
