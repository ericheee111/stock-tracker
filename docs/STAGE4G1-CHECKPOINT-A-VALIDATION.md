# Stage 4G.1 Checkpoint A4 / B0 / B1 R2 Validation

## Scope and identity

- Branch: `agent/codex-stage4g1`
- A4 parent commit: `3e6ad3f73c78f5d4ddd8f0537250ba5b275bd0f2`
- Baseline: `082a6dac310388ec10c8a432427a40e275bcd7ae`
- A4 commit: `728835c96e23222abec3cd20827388d4d2cf963c`
- A4 tree: `2f32dfe900823d0fbeb2be6f04ab1bdbb763dc61`
- B0/B1 pure-contract commit: `1297f9e36611935d327965104fdab5e93c6b647f`
- Original B0/B1 pure-contract tree: `447c4569797f8d28206111c3a473803cce80cb85`
- B0 R1 causality/authority commit: `7a49222c73ca0a9800b2aec8d2c07450e195cbca`
- B1 R1 scalable snapshot commit: `ffb0441a21b9ff60b28ae2adeb393517c8301cab`
- B0/B1 R1 final commit: `599664ca4a3402731c4b5d963c3ba8e1922a4618`
- B0/B1 R1 final tree: `5cf270ac16eb22719b61a88b86163f93d7de9ab8`
- B0/B1 R1 archive SHA-256: `a2c948252e1bd0d0220db548e7b0fc0371a4e5cd7ec2e53ec59d182ed99eeefd`
- R2 evidence-membership commit: `a5eecc8e918c238547b469bf8dc6b3459bd7eb8a`
- R2 path/interval closure commit: `db94b9616b66c05d077a7ebd189b50173bcb362b`
- R2 implementation tree before this docs-only commit: `28aef4eb5927299ec52fe5af5231286893be54f4`
- R2 implementation archive SHA-256: `7f27b2146406daee9b32247d17dc1c5b8e53e068814776cf7629c9b1d1d58a56`
- Validation interpreter: `Python 3.14.6`
- The docs-only validation commit is reported after creation because a commit
  cannot contain its own future object ID without becoming self-referential.
- No push, merge, amend, reset, rebase, production migration apply, Broker/XTP
  write API, automatic trading, or Strategy Scoreboard is part of this checkpoint.

## Implemented integrity model

The Runtime DB owns one append-only `runtime_artifact_delivery_binding` from
`runtime_store_id` to `artifact_store_id`, identified by `delivery_binding_id`.
The first claim creates the binding. A later store mismatch appends one global
`runtime_artifact_integrity_event`; changing `worker_id` or returning to the old
store cannot bypass it. A4 additionally requires the Artifact Store's
`source_runtime_store_id` to equal the Runtime DB's stable `runtime_store_id`
before any binding is created or row is leased. A mismatch creates no binding,
lease, quarantine, or cursor movement. Every lease carries `runtime_store_id`,
and lease completion validates both `runtime_store_id` and `artifact_store_id`.
An Artifact Store/runtime binding mismatch is a `STORE_INTEGRITY_BLOCK`, never a
row-safe quarantine condition.

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
   failures append or imply a fail-closed global block. Checkpoint A4 has no
   automatic recovery or destructive unblock operation.

Each occurrence binds one unique `signal_history_id` through an enforced SQLite
foreign key and immutable-history triggers. `signal_history_sha256` covers
`signal_id`, `from_state`, `to_state`, `at`, `reason`, and `what_changed`.
Occurrences also form a runtime-store-bound local chain using
`occurrence_append_order`, `previous_occurrence_hash`, and `occurrence_hash`.
All Runtime Evidence connections enable `PRAGMA foreign_keys=ON` before runtime
transactions; audits require an empty `PRAGMA foreign_key_check` result.

## PIT availability matrix

| Evidence | Latest allowed time | A4 authority |
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

## B0/B1 R2 adversarial closure

R2 source schemas v3 split `record_content_hash`, deterministic
`record_storage_key`, `record_file_sha256`, and `source_record_id`. The record
member ID and inventory leaf bind all six immutable inventory coordinates. A
full-prefix `MarketEventInventoryVerification` rejects duplicate append order,
event ID, record ID, or storage key before an Audit can be created. Selection is
not an authority by itself: it has separate Commitment and Membership Witness
objects and becomes consumable only after `verify_selection()` reconstructs the
query result from the exact audited prefix. No digest is described as a Merkle,
MMR, or range proof.

`RuntimeSourceReference` now carries the exact snapshot, audit, high-water,
finding set, sequence policy, Selection, event, security, session, parser/schema,
raw hash, and durable-time member identity. A TICK must be an exact verified
`TRADE_TICK`. A projected bar instead binds an immutable Projection Artifact,
half-open interval policy, OHLC output hash, coverage authority, verified input
members, and creation/durability times; a raw tick before `interval_end` is valid
and a tick exactly on the adjacent boundary cannot belong to the earlier bar.

Path prefixes are created only from `RuntimePathStoreSnapshotBinding` and an
exact `RuntimeCaseFactSelection`. The global store prefix is contiguous, while
case fact orders may contain gaps caused by other cases. The case Selection binds
the global high-water/audit, case query policy, ordered fact IDs and record
hashes, manifest commitment, verification mode, and finding/blocker digest.
Omitting an existing TARGET, STOP, or session fact, injecting another case,
reordering facts, or changing the high-water/audit fails before the resolver.

Calendar, Security Status, and Coverage use separate canonical Authority fact
inventories and exact snapshot selections. Fact IDs and record hashes are
recomputed; membership is not inferred from `revision <= high_water`. Fact
knowledge, usability, Authority audit, path audit, source membership, and
projection durability must all be available by the frozen prefix time.

The versioned interval rule is half-open. Without an independent causal-ordering
authority, every TICK, MINUTE_BAR, or DAILY_BAR whose
`interval_start <= entry_filled_at` fails closed as
`ENTRY_BOUNDARY_ORDER_AMBIGUITY`. A complete prefix with no post-entry source
record remains `OPEN/HORIZON_NOT_REACHED`; `TRADED_SESSION_WITHOUT_POINTS` is
reserved for an exact source Selection that contains an eligible record omitted
from the case path facts. A complete zero-event horizon follows the frozen
terminal policy and reaches `TIMEOUT`.

Execution aggregation is explicitly only
`structurally_projectable_to_stage4g_v3`. Execution store/snapshot/audit,
audit-known time, high-water, Selection, and membership IDs are frozen, duplicate
callbacks cannot double count, and no-entry reason facts must be members known no
later than `decided_at`. Real finalizability remains the responsibility of a
later Execution ReadPort/Adapter. Market Event Store v4, B2 Path Worker, and the
Stage 4G Collection Bridge remain out of scope.

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
`0002_runtime_occurrence_identity.sql` remain unchanged. A3 added only
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

## Historical R1 validation results

Every final gate below completed with exit code 0.

| Surface | Exact command | Result |
| --- | --- | --- |
| interpreter | `py -3.14 --version` | `Python 3.14.6` |
| A4 focused | `py -3.14 -m unittest tests.test_runtime_evidence -q` | 57 passed |
| A4 targeted Ruff | `py -3.14 -m ruff check stock_tracker/runtime_evidence/store.py stock_tracker/runtime_evidence/worker.py stock_tracker/storage/repository.py tests/test_runtime_evidence.py` | passed |
| B0/B1 focused | `py -3.14 -m unittest tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -q` | 68 passed |
| staged combined | `py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -q` | 125 passed |
| staged distribution | `py -3.14 -m unittest tests_quant.test_source_distribution -q` | 2 passed |
| staged diff | `git diff --cached --check` | passed before B0/B1 commit |
| Stage 4G/4F adjacent | `py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -q` | 114 passed |
| full Runtime | `py -3.14 -m unittest discover -s tests -p "test_*.py" -q` | 647 passed, 1 expected skip |
| full Quant | `py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -q` | 724 passed |
| distribution/no bytecode | `py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q` | 2 passed in a real Git checkout |
| targeted Ruff | `py -3.14 -m ruff check stock_tracker/core/market_time.py stock_tracker/runtime_evidence stock_tracker/storage/repository.py tests/test_runtime_evidence.py tests/test_runtime_path_contracts.py tests/test_market_source_snapshot_contracts.py tests_quant/test_source_distribution.py` | passed |
| compile | `py -3.14 -X pycache_prefix="$(mktemp -d -t stage4g1-pycache.XXXXXX)" -m compileall -q stock_tracker tests tests_quant scripts` | passed; bytecode stayed outside the checkout |
| dependencies | `py -3.14 -m pip check` | no broken requirements |
| Quant smoke | `py -3.14 scripts/run_quant_contract_smoke.py` | `passed=true`; `synthetic_fixture_only=true`; no investment-performance claim |
| synthetic benchmark | `py -3.14 scripts/run_quant_fixture_benchmark.py` | completed; `synthetic_fixture_only=true`; no promotion and no investment-performance claim |
| Runtime migration | `py -3.14 scripts/runtime_migrate.py --database 'D:\Projects\stock-tracker\data\stock_tracker.db'` | `DRY_RUN`; `database_modified=false`; `production_database_modified=false`; `auto_trade=false` |
| Quant migration | `py -3.14 scripts/quant_migrate.py --database 'D:\Projects\stock-tracker\data\stock_tracker.db'` | `DRY_RUN`; 4 pending; `database_modified=false` |
| Today Mock | `node qa/ui/today_action_qa.cjs` | 17/17 passed |
| temporary real Today | `py -3.14 scripts/run_stage1_today_integration.py` | Today 17/17 and browser Portfolio CRUD 13/13 passed |
| Portfolio unit | `py -3.14 -m unittest tests.test_portfolio_api tests.test_portfolio_repository -q` | 15 passed |
| worktree diff | `git diff --check` | passed |
| R1 exact index export | `git rev-parse 599664ca4a3402731c4b5d963c3ba8e1922a4618^{tree} && git archive --format=tar 599664ca4a3402731c4b5d963c3ba8e1922a4618 \| sha256sum` | tree `5cf270ac16eb22719b61a88b86163f93d7de9ab8`; archive SHA-256 `a2c948252e1bd0d0220db548e7b0fc0371a4e5cd7ec2e53ec59d182ed99eeefd` |
| baseline binary audit | `git diff --numstat 082a6dac310388ec10c8a432427a40e275bcd7ae..HEAD` | all entries had numeric add/delete counts; no binary marker |
| generated artifact scan | `git diff --name-only 082a6dac310388ec10c8a432427a40e275bcd7ae..HEAD \| grep -E '(^\|/)(__pycache__\|[^/]+\.(pyc\|pyo\|db\|sqlite3?\|wal\|shm\|log\|tmp\|exe\|dll\|zip))$'` | no findings |
| tracked binary/database scan | `git ls-files '*.pyc' '*.pyo' '*.db' '*.sqlite' '*.sqlite3' '*.dll' '*.exe'` | no findings |
| secret signature scan | `git diff --unified=0 082a6dac310388ec10c8a432427a40e275bcd7ae..HEAD -- '*.py' '*.sql' '*.md'` with private-key, AWS, OpenAI, and GitHub token signatures | no findings |
| Broker/XTP write scan | `git diff --unified=0 082a6dac310388ec10c8a432427a40e275bcd7ae..HEAD -- stock_tracker scripts` with Trader/Order/Cancel/Algo/Account/Position write-call signatures | no findings |
| auto-trade audit | `git grep -n -I 'auto_trade' HEAD -- stock_tracker scripts` | Stage 4G.1 contracts reject true and emit false; no true declaration found |

## R2 implementation-tree validation

The following gates ran against implementation commit
`db94b9616b66c05d077a7ebd189b50173bcb362b` plus the docs-only working-tree
update. The final docs commit, exact final tree/archive, scans, database hashes,
and command logs are recorded in the external mechanical evidence package whose
directory is named with that final commit's short SHA.

| Surface | Exact command | Result |
| --- | --- | --- |
| focused R2 contracts | `py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -q` | 148 passed |
| Stage 4G/4F adjacent | `py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -q` | 114 passed |
| full Runtime | `py -3.14 -m unittest discover -s tests -p "test_*.py" -q` | 670 passed, 1 expected skip |
| full Quant | `py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -q` | 724 passed |
| distribution/no bytecode | `py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q` | 2 passed in a real Git checkout |
| targeted Ruff | `py -3.14 -m ruff check stock_tracker/runtime_evidence/path_contracts.py stock_tracker/runtime_evidence/source_snapshot_contracts.py stock_tracker/runtime_evidence/__init__.py tests/test_runtime_path_contracts.py tests/test_market_source_snapshot_contracts.py tests_quant/test_source_distribution.py` | passed |
| production type diagnostics | `basedpyright --level error stock_tracker/runtime_evidence/path_contracts.py stock_tracker/runtime_evidence/source_snapshot_contracts.py stock_tracker/runtime_evidence/__init__.py` | 0 errors, 0 warnings |
| compile | `py -3.14 -X pycache_prefix="<external-temp>" -m compileall -q stock_tracker tests tests_quant scripts` | passed; bytecode stayed outside the checkout |
| dependencies | `py -3.14 -m pip check` | no broken requirements |
| worktree diff | `git diff --check` | passed |

The R2 Runtime run retained the known local test-server
`ConnectionAbortedError: [WinError 10053]` and temporary HTTPError cleanup
`ResourceWarning` stderr. The suite still exited 0. Quant negative CLI tests emitted
their expected argument errors, and the known temporary migration-test
connection warnings remained visible; the suite still exited 0. Neither warning
class is treated as evidence of production database mutation.

R1 debugging intentionally began with three non-final red regressions: an omitted
callback gap Finding was accepted, a drive-qualified storage key was accepted,
and an exact-entry target tick resolved instead of blocking. The implementation
then closed those roots and the expanded 125-test focused suite passed. The
corrected horizon rule is now explicit: a horizon `COMPLETE_PREFIX` remains
`OPEN/HORIZON_NOT_REACHED`; gap/out-of-order/sparse/missing coverage blocks; only
`COMPLETE_SESSION` without an earlier barrier reaches `TIMEOUT`.

The Runtime suite emitted three full Windows local test-server
`ConnectionAbortedError: [WinError 10053]` traces and four temporary `HTTPError`
cleanup `ResourceWarning` lines. They were visible stderr noise, not suppressed;
the suite still completed 647 tests with one expected skip and exit code 0.
Quant CLI negative tests emitted expected argument/error text, and the full Quant
suite emitted four `ResourceWarning` lines for unclosed temporary migration test
connections; it completed 724 tests with exit code 0. These warnings are accepted
test-harness residuals, not evidence of production database mutation.

## Production database evidence

| Artifact | Before SHA-256 | After SHA-256 |
| --- | --- | --- |
| main DB | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` |
| WAL | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` |
| SHM | `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3` | `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3` |
| read-only SQLite backup snapshot | `6e482862c7990fbbf531c7b41caf173483a5a4b48eaef688f9ba3dedf71ed8c7` | `6e482862c7990fbbf531c7b41caf173483a5a4b48eaef688f9ba3dedf71ed8c7` |
| logical SQLite serialization | `b815ee8cf2feeda4b40a3ce634636cc65b2b9840e8af4b6c621abc96ccf49fc8` | `b815ee8cf2feeda4b40a3ce634636cc65b2b9840e8af4b6c621abc96ccf49fc8` |

The read-only backup snapshots were created in OS temporary directories outside
the repository. The DB, WAL, SHM, backup, and logical hashes are identical before
and after all gates. No production migration apply ran, and no database, WAL,
SHM, log, cache, screenshot, binary, or secret is committed.

## Accepted residuals

- Bar known-at authority remains unavailable; artifacts remain
  `RUNTIME_MEMORY_ONLY` and cannot claim complete PIT evidence.
- Fork detection is local append-chain only. External independent checkpointing,
  fork adjudication, and append-only recovery events are deferred to Stage 4H.
- No trusted upstream occurrence authority exists, so caller-supplied occurrence
  IDs remain rejected.
- B0/B1 deliver only frozen pure contracts and an audited source snapshot/read
  boundary. Checkpoint B1 Market Event Store v4 wiring, durable Path Worker,
  cursor advancement, and Stage 4G append integration have not started.
- Paper/Manual adapters, Trusted Outcome Admission Authority, admitted-sample
  shadow, and a real Strategy Scoreboard are not implemented.
- The benchmark and fixtures are synthetic engineering evidence, not real
  multi-day Shadow Acceptance or investment performance.
- Worktree LSP diagnostics could not target this isolated worktree because the
  app request cwd remained the separate main checkout. Ruff, compileall, focused,
  adjacent, and full suites cover the changed Python surfaces instead.
- Artifact Store failure is isolated from normal Today/Portfolio service, but no
  Broker/XTP Trader, Order, Cancel, Algo, Account, or Position API is enabled.
- `auto_trade=false` remains enforced. Nothing was pushed; the work is local-only
  and awaits the next independent review checkpoint.
