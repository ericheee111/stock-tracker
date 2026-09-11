# B1a–B1c physical storage: implementation review and verification

Date: 2026-09-11. Baseline: `cf743a73c6fa4cb860bb1191183e5e358b7218f6`.

Implementation commit: `f5c95d82ddddfee167c34daa5469d830d241b713`; implementation tree: `5b549c81dd9bd8ad80941ab4b4d8b19607d05214`. The subsequent documentation commit is distinguished in the external release report.

Status: **IMPLEMENTED / SCOPED_SELF_REVIEW_PASSED / EXACT_RELEASE_TREE_VERIFICATION_REQUIRED**.

ChatGPT implemented this slice directly and performed the code review, adversarial tests and reconstruction checks. This is not a separately staffed or separate-model independent review. Final commit/tree, exact reconstruction and GitHub identity are recorded in the external release package; this document cannot contain its own final commit hash.

## 1. Accepted scope

`physical_store.py` and `physical_store_cli.py` implement explicit new-root initialization, canonical record bytes, immutable catalog intents, non-overwriting file publication, publication receipts, restart recovery, full physical inventory audit and bounded physical pages.

The new format is `b1-physical-store-v1`, not the completed semantic Market Event Store v4. Every public audit/CLI report remains `PHYSICAL_BYTES_ONLY`. Source bodies are opaque canonical evidence at this layer: accepted bytes are not proof of correct prices, source independence, liveness, market coverage, PIT availability or admission.

The Stage 3F implementation, production ingestion, Runtime Artifact Worker, B0/R4.1 semantics, Collection, Scoreboard and UI are not modified by the new kernel. No import or application startup automatically initializes it.

## 2. Critical design checks

| Boundary | Implemented behavior / evidence |
|---|---|
| File/catalog transaction | An immutable intent persists exact bytes before publication. Only a publication row makes a record readable. SQLite and external files are not claimed to share one atomic transaction. |
| Concurrent/repeated calls | Same stream/key and canonical input is idempotent. Changed input conflicts. One pending slot prevents a later event skipping an incomplete publication. |
| Recovery ownership | Only the exact durable intent can recreate its transient staging file. Unknown final or staging entries block audit; they are not silently adopted or deleted. |
| Non-overwrite | Temporary file flush/fsync, hard-link publication, exact final comparison. Unsupported hard links fail closed, without replace fallback. |
| Acknowledgement uncertainty | Tests simulate commit succeeding and then raising. Fresh connection/retry finds the one original publication; no evidence compensation deletion. |
| Record identity | All record fields derive from canonical file bytes (`init=False`); storage key binds order/hash. File SHA equals content hash and is not embedded in the bytes being hashed. |
| Filesystem | Expected Store ID, root/directory/catalog inode checks, symlink/reparse/hardlink rejection, journal-link check. Assumes a private local root, not an adversarial administrator racing every syscall. |
| Schema | Exact tables/indexes/triggers/column and FK inventory, application/user version, FK check, quick check, rollback journal and FULL synchronization. No automatic schema upgrade. |
| Read consistency | Audit reserves the writer; physical snapshot binds Store ID/high-water/global head. Reads recheck that prefix against the current physical Store. Later appends do not change an old prefix. |
| Resource bounds | Record limit 2 MiB; page limit 256 records and 8 MiB. Audit iterates records/directory entries; it does not return the entire historical body list. |
| Time | Aware UTC operation samples and rollback/future-input guards. These are not externally authenticated known-at timestamps or a license to backdate semantic availability. |

## 3. Real process-crash windows

A subprocess terminates with `os._exit(73)` at each of the following points. Reopening and exact retry/recovery preserves one record and no uncommitted record is served:

1. before intent transaction commit;
2. after intent commit;
3. after staging-file fsync;
4. after hard-link publication, before staging-name cleanup;
5. after final-file publication;
6. after publication-row insertion, before catalog commit;
7. after successful catalog commit, before acknowledgement.

These are seven subcases within a test method, not seven additional method counts. Two-thread exact retry, two competing initializers, partial owned staging, damaged/missing final file, catalog/record disagreement, schema injection, wrong Store/snapshot, extra hard link and journal symlink have separate negative tests.

## 4. Recorded verification

Commands were run with Windows Python 3.14.6. Complete commands, stdout/stderr hashes, durations and exits are in the external `b1-physical-20260911/commands.jsonl` package.

| Gate | Result |
|---|---|
| New physical Store / CLI tests | 45 passed, exit 0 |
| Full Runtime | 785 testsRun; 784 passed, 1 expected skip; exit 0 |
| Full Quant | 724 passed, exit 0 |
| Stage 4G/4F/Outcome adjacent tests | 114 passed, exit 0 |
| W5 boundary and test-tool regression | 72 passed, exit 0 |
| Source distribution/no tracked bytecode | 2 passed, exit 0 |
| Targeted Ruff | Passed |
| Entire runtime_evidence basedpyright error gate | 0 errors / 0 warnings / 0 notes |
| compileall with short external pycache | Passed |
| pip check | Passed |
| Quant smoke and synthetic benchmark | Passed; no promotion or investment-performance claim |
| Today mock | 17/17 |
| Temporary real API/Web Today | 17/17 |
| Temporary Portfolio CRUD | 13/13 |

Some suites overlap; their counts must not be added as distinct coverage. No fresh 54-case UI matrix was claimed for this backend-only slice. Existing HTTPError/SQLite ResourceWarnings remain visible in logs. The one Runtime live-service skip is not a pass or operational acceptance.

The source-distribution critical list now includes both new modules, both new test files and the implementation plan. The final release uses an exact Git-tree reconstruction and reruns focused/distribution/type gates against that reconstruction; final results belong to the external commit-bound delivery report.

## 5. Development defects found and fixed

- Initial Windows initialization attempted fsync through a read-only catalog handle and failed with EBADF. The owned, completed temporary catalog is now reopened read/write for the flush before non-overwriting publication.
- Record projections originally allowed independent dataclass fields. They now derive exclusively from content bytes; replacing append_order independently is rejected. Python 3.14 raises TypeError for an init=False replacement, and the negative test reflects that actual rejection.
- Missing NoReturn annotation caused optional-row type errors; failure paths were annotated without weakening checks.
- Pagination initially bounded only record count; it now also enforces a byte budget without fetching the whole bounded set upfront.
- Journal symlinks are checked before opening the catalog, and an outside-target preservation test covers the case.

Early development failures are retained in the conversation's tool results; the external final-gate log starts after those corrections. No claim is made that unavailable early raw log files were archived. No successful test was obtained by disabling a security shim or weakening a financial contract.

## 6. Accepted limits and next phase

- **No power-loss certification.** File fsync is required; directory sync is platform-dependent. Windows process-crash tests do not certify sudden power loss, controller cache behavior or every filesystem.
- The durable intent retains canonical bytes as well as the immutable file, increasing disk usage. Retention/compaction is not implemented; immutable evidence must not be deleted to hide this cost.
- Full audit before individual writes is conservative and can produce quadratic aggregate work. This slice is not high-frequency/Level 2 throughput-ready. B1d must measure batching/checkpoint amortization without weakening audit guarantees.
- Physical snapshots do not issue existing `MarketEventStoreAudit`, `TransportCoverageCertificate` or a `STORE_RESCANNED` semantic receipt. Typed ReadPort assembly must consume actual records and preserve the reviewed source/transport/authority policies.
- Legacy Stage 3F data is not migrated or relabeled. Explicit legacy inspection/export remains B1d work.
- No external fork adjudication, signature/authority, real-market shadow or automatic Outcome collection is implemented.
- Audit uses a SQLite writer reservation and can allow SQLite hot-journal recovery. It is not a guaranteed-zero-write forensic operation; production SQLite was not opened.

Next order: **B1d typed ReadPort / legacy / capacity → B2 per-case Path Store and worker → B3 Collection bridge → C execution evidence → D operational shadow → 4H/4I**.

## 7. Working-tree and database protection

Original main/UI/screenshots, all prior Codex worktrees and all four WB clones remain outside this change. No reset/clean/stash/rebase is part of delivery. The original dirty main may intentionally remain behind origin/main after the new branch is fast-forward-pushed.

Production DB/WAL/SHM are checked by file SHA only, without SQLite connection or migration apply. Final before/after protection is recorded externally. Reference values:

```
DB  ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7
WAL aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e
SHM 5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3
```

## 8. Safe local commands

Run only against a deliberately chosen new/private root, never the production data directory. Initialization, append and recovery require explicit apply; no forced cleanup option exists.

```
py -3.14 -m stock_tracker.runtime_evidence.physical_store_cli init --root <NEW_PRIVATE_ROOT>
py -3.14 -m stock_tracker.runtime_evidence.physical_store_cli init --root <NEW_PRIVATE_ROOT> --apply
py -3.14 -m stock_tracker.runtime_evidence.physical_store_cli audit --root <ROOT> --store-id <EXPECTED_ID>
py -3.14 -m stock_tracker.runtime_evidence.physical_store_cli recover --root <ROOT> --store-id <EXPECTED_ID> --apply
```

The first command is an initialization plan, not successful initialization. An empty physical page means only no published records under that physical query, never zero-market-event coverage.
