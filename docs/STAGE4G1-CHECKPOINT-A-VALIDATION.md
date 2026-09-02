# Stage 4G.1 Checkpoint A3 Validation

## Scope and identity

- Branch: `agent/codex-stage4g1`
- Parent commit: `f8b278a7f9464e012b263066d51080a2857aa58f`
- Baseline: `082a6dac310388ec10c8a432427a40e275bcd7ae`
- Final A3 commit and tree: recorded after commit in ignored
  `data/stage4g1-review/a3/final-evidence.json`. A commit cannot contain its own
  future object ID without becoming self-referential.
- No push, merge, amend, reset, rebase, production migration apply, Broker/XTP
  write API, automatic trading, or Strategy Scoreboard is part of this checkpoint.

## Implemented integrity model

The Runtime DB owns one append-only `runtime_artifact_delivery_binding` from
`runtime_store_id` to `artifact_store_id`, identified by `delivery_binding_id`.
The first claim creates the binding. A later store mismatch appends one global
`runtime_artifact_integrity_event`; changing `worker_id` or returning to the old
store cannot bypass it.

The delivery lane has these terminal paths:

1. A canonical row is leased, atomically published without overwrite, audited,
   and only then marked `DELIVERED` with artifact-store ID, record hash, artifact
   append order, audit ID, and delivery time. Cursor advancement follows that
   durable transaction.
2. A `ROW_TRANSIENT` failure receives bounded retry. Exhaustion appends a global
   lane block, leaves the valid occurrence non-quarantined, and does not advance
   the cursor.
3. A pre-publication `ROW_PERMANENT` payload/contract failure alone may append a
   quarantine record and advance past that row. A following valid row remains
   processable.
4. Store schema, inventory, hash-chain, identity, binding, or clock integrity
   failures append or imply a fail-closed global block. Checkpoint A3 has no
   automatic recovery or destructive unblock operation.

Each occurrence binds one unique `signal_history_id` through an enforced SQLite
foreign key and immutable-history triggers. `signal_history_sha256` covers
`signal_id`, `from_state`, `to_state`, `at`, `reason`, and `what_changed`.
Occurrences also form a runtime-store-bound local chain using
`occurrence_append_order`, `previous_occurrence_hash`, and `occurrence_hash`.
All Runtime Evidence connections enable `PRAGMA foreign_keys=ON` before runtime
transactions; audits require an empty `PRAGMA foreign_key_check` result.

## PIT availability matrix

| Evidence | Latest allowed time | A3 authority |
| --- | --- | --- |
| current `state_changed_at` | exactly `decision_requested_at` | runtime snapshot |
| previous state time | `decision_requested_at` | runtime snapshot |
| quote source/received/computed | `decision_requested_at` | runtime memory only |
| quote displayed | `observed_at` | runtime memory only |
| bar timestamp | `decision_requested_at` | not a known-at authority |

Artifacts use schema `stage4g1-runtime-decision-artifact-v4`, require
`pit_evidence_status=RUNTIME_MEMORY_ONLY`, require
`bar_known_at_authority=NOT_AVAILABLE`, and retain
`BAR_KNOWN_AT_AUTHORITY_PENDING`. `SINGLE_ACTIVE_RUNTIME_STORE=true`,
`FORK_DETECTION=LOCAL_APPEND_CHAIN_ONLY`, and
`EXTERNAL_CHECKPOINT=NOT_IMPLEMENTED` are explicit. Older artifact schemas fail
closed. `upstream_occurrence_id` is fixed to null because Checkpoint A has no
trusted upstream occurrence authority.

## Legacy migration matrix

| Source state | Result |
| --- | --- |
| uninitialized Runtime schema | dry-run rehearses v1, v2, v3; source unchanged |
| empty v1 | explicit apply may reach v3 |
| empty v2 | explicit apply may reach v3 |
| non-empty v1 or v2 | `LEGACY_RUNTIME_EVIDENCE_REQUIRES_EXPORT`; no backfill/delete |
| unknown or partial schema | fail closed |
| latest v3 | audited, no pending migration |

`0001_runtime_evidence_outbox.sql` and
`0002_runtime_occurrence_identity.sql` remain unchanged. A3 adds only
`0003_runtime_delivery_binding_and_integrity.sql`. The machine-readable report
separates target-state validity from latest-schema readiness.

Production Runtime dry-run sample:

```json
{
  "database_modified": false,
  "latest_version": 3,
  "migration_history_valid": true,
  "mode": "DRY_RUN",
  "pending_versions": [1, 2, 3],
  "rehearsal_latest_schema_passed": true,
  "rehearsal_performed": true,
  "source_database_sha256": "ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7",
  "source_wal_sha256": "aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e",
  "target_latest_schema_ready": false,
  "target_schema_state": "UNINITIALIZED",
  "target_state_audit_passed": true,
  "would_modify": true
}
```

## Validation results

Every command below completed with exit code 0 unless stated otherwise.

| Surface | Exact command | Result |
| --- | --- | --- |
| Stage 4G.1 focused | `python -m unittest tests.test_runtime_evidence -q` | 55 passed |
| Stage 4G/4F adjacent | `python -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -q` | 114 passed |
| full Runtime | `python -m unittest discover -s tests -p "test_*.py" -q` | 577 passed, 1 skipped |
| full Quant | `python -m unittest discover -s tests_quant -p "test_*.py" -q` | 724 passed |
| distribution/no bytecode | `python -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q` | 2 passed |
| targeted Ruff | `python -m ruff check stock_tracker/__main__.py stock_tracker/runtime_evidence stock_tracker/signals/manager.py stock_tracker/storage/db.py stock_tracker/storage/repository.py stock_tracker/storage/runtime_migrations.py tests/test_runtime_evidence.py tests_quant/test_source_distribution.py` | passed |
| compile | `python -m compileall -q stock_tracker tests tests_quant scripts` | passed |
| dependencies | `python -m pip check` | no broken requirements |
| Quant smoke | `python scripts/run_quant_contract_smoke.py` | `passed=true`, synthetic only |
| synthetic benchmark | `python scripts/run_quant_fixture_benchmark.py` | completed; synthetic fixture only |
| Runtime migration | `python scripts/runtime_migrate.py --database D:\Projects\stock-tracker\data\stock_tracker.db` | dry-run; source unchanged |
| Quant migration | `python scripts/quant_migrate.py --database D:\Projects\stock-tracker\data\stock_tracker.db` | dry-run; 4 pending, source unchanged |
| Today Mock | `node qa/ui/today_action_qa.cjs` | 17/17 passed |
| temporary real Today | `python scripts/run_stage1_today_integration.py` | Today 17/17 and browser Portfolio 13/13 passed |
| Portfolio unit | `python -m unittest tests.test_portfolio_api tests.test_portfolio_repository -q` | 15 passed |
| exact index export | `git checkout-index --all --prefix=<temporary-directory>/` followed by compile/import checks | passed |
| generated/binary/secret scan | staged-path, numstat, and redacted content scans | no findings |
| cached diff | `git diff --cached --check` | passed |

The Runtime suite emitted expected Windows test-server connection-abort traces
while completing with exit code 0. Quant CLI negative tests emitted expected
argument/error text while the suite completed with exit code 0.

## Production database evidence

| Artifact | Before SHA-256 | After SHA-256 |
| --- | --- | --- |
| main DB | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` |
| WAL | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` |
| read-only SQLite backup snapshot | `6e482862c7990fbbf531c7b41caf173483a5a4b48eaef688f9ba3dedf71ed8c7` | `6e482862c7990fbbf531c7b41caf173483a5a4b48eaef688f9ba3dedf71ed8c7` |

The snapshot files and final machine-readable evidence stay under ignored
`data/stage4g1-review/a3/`; no database, WAL, log, cache, screenshot, binary, or
secret is committed.

## Accepted residuals

- Bar known-at authority remains unavailable; artifacts remain
  `RUNTIME_MEMORY_ONLY` and cannot claim complete PIT evidence.
- Fork detection is local append-chain only. External independent checkpointing,
  fork adjudication, and append-only recovery events are deferred to Stage 4H.
- No trusted upstream occurrence authority exists, so caller-supplied occurrence
  IDs remain rejected.
- No Trusted Outcome Admission Authority or real Strategy Scoreboard exists.
- The benchmark and fixtures are synthetic engineering evidence, not real
  multi-day Shadow Acceptance or investment performance.
- Worktree LSP diagnostics could not target this isolated worktree because the
  app request cwd remained the separate main checkout. Ruff, compileall, focused,
  adjacent, and full suites cover the changed Python surfaces instead.
- Artifact Store failure is isolated from normal Today/Portfolio service, but no
  Broker/XTP Trader, Order, Cancel, Algo, Account, or Position API is enabled.
