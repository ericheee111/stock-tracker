# Stage 4G.1 B1 — physical evidence storage implementation slices

Date: 2026-09-11. Baseline: `cf743a73c6fa4cb860bb1191183e5e358b7218f6`.

## Scope and delivery sequence

The reviewed B0/R4.1 semantic contracts remain unchanged. This document refines physical implementation, not market, execution, PIT or admission rules. W5 is already delivered. No further general contract-hardening campaign is a prerequisite.

1. **B1a: physical codec and explicit initialization.** Strict canonical bytes, independent root/catalog, stable Store ID, explicit create, expected-ID open, exact schema and safe path checks.
2. **B1b: publication and restart recovery.** A durable immutable intent, no-overwrite record file publication, append-only publication receipt, retry by original identity, and real subprocess crash tests.
3. **B1c: physical inventory and bounded reads.** Stream through catalog and files under the writer reservation, check global/partition chains and inventory, return compact physical snapshots and bounded pages. This slice does not issue the existing semantic `MarketEventStoreAudit` or coverage certificates.
4. **B1d: typed Event/Transport ReadPort and legacy tooling.** Adapt existing reviewed Event, Transport, subscription, clock and authority contracts to physical records; derive sequence/liveness findings from one committed snapshot. Explicit legacy dry-run/export, no automatic reinterpretation. Requires separate semantic integration review.
5. **B2: per-case Path Evidence Store / worker.** Historical backfill then tail, exact projection inputs, cursor/lease recovery, Calendar/Status/coverage membership. Do not write directly to Collection from quotes.
6. **B3/C/D: Collection bridge, execution adapters and shadow.** Only supported complete lifecycles; Paper diagnostic only; manual live remains candidate. Multi-day shadow is an operational gate, not a unit-test count.
7. **4H/4I:** separately approved signer/authority/time-anchor, admitted sample accumulation, then read-only Strategy Scoreboard.

This delivery targets B1a–B1c **physical byte integrity only**. B1d, B2 and later remain unimplemented. The old Stage 3F Store stays untouched and current ingestion is not switched over.

## Architecture decision: durable publication intent

SQLite cannot make an arbitrary external JSON file and a catalog row one atomic transaction. Instead of guessing ownership of an orphan file, preserve the exact intended record bytes in an append-only intent row before publishing the file. An intent is NOT a committed event and must never appear in a data page or semantic snapshot.

```
BEGIN IMMEDIATE
  validate identity / schema / inventory
  deduplicate source key and exact input bytes
  reserve next order + immutable canonical intent
COMMIT
  [crash: RECOVERY_REQUIRED, no visible event]
BEGIN IMMEDIATE
  re-read exact intent (a different worker may already have recovered it)
  temp file -> flush/fsync -> no-overwrite hard link -> unlink own staging name
  compare final file with intended bytes
  append publication row
COMMIT
  [lost acknowledgement: retry returns same record, never a second event]
```

Only one pending slot exists. A different event cannot skip an unresolved intent. Any worker may recover that exact intent; neither another worker ID nor a different input can change it. Unknown final files are not adopted or deleted. Partial **owned staging files** can be recreated from durable intent bytes. Missing/different committed files block the Store.

This explicitly supersedes the earlier B1 draft's unauthenticated 'next orphan' ownership inference for the physical kernel. It does not rewrite any already stored Stage 3F record. The kernel has its own `b1-physical-store-v1` identity; it is not falsely advertised as completion of the eventual Market Event v4 semantic adapter.

## Bytes, identities and ordering

- Event and Transport publication use one physical `append_order` and global hash chain; partition chains are additionally scoped by `(stream, partition_key)`.
- `record_content_bytes` are canonical UTF-8 JSON without their own file hash or source-record ID. `record_content_hash = file_sha256 = SHA256(bytes)`; no self-reference.
- The deterministic storage key binds order and content hash. Source-record identity additionally binds Store ID, stream/key, order, content hash and storage key.
- The input key is an external deduplication key, not proof of an independently sampled strategy episode. Exact key + exact canonical input is idempotent; key reuse with changed input conflicts.
- `recorded_at` and publication clock samples are local operation timestamps, not independently authenticated `known_at`. The physical kernel does not backdate an as-of semantic admission or manufacture `durable_known_at` for the reviewed semantic models.
- Reads require the expected Store ID. Restored stores retain logical ID; concurrently running clones are not independently adjudicated without an external checkpoint.

## Durability and threat boundary

Default claim: `PROCESS_CRASH_TESTED`, not power-loss certification. File `fsync` is required. Directory sync is used where supported; Windows namespace/power-loss guarantees require a separately verified backend and hardware/OS evidence. No code fallback silently replaces existing files when hard links are unavailable.

The caller owns a private local root. Symlinks, junctions/reparse points, extra hard links and root/catalog replacement are rejected at operation boundaries. This is not protection against an administrator actively replacing files between every system call. Store IDs, hashes and SQLite triggers are integrity mechanisms, not signatures or authorization.

The catalog uses rollback journal, synchronous FULL and foreign keys. Audit reserves the writer with `BEGIN IMMEDIATE`; it reads SQL/file data but may let SQLite perform its own hot-journal recovery. It is not described as a guaranteed no-filesystem-side-effect forensic reader. No operation targets production SQLite.

## Compact audit and scale boundary

Physical audit iterates records and directory entries; it does not materialize all record bodies in Python. Partition predecessor lookup uses an indexed SQL query. Pages have an explicit maximum and come from a fully validated committed prefix.

Full physical audit before mutation prioritizes integrity. Repeated single-record append is not a high-throughput claim; batching and persistent audit checkpoint amortization belong to B1d capacity work and must not weaken mutation detection. Benchmarks report input size, memory and elapsed time separately; no million-record operational claim follows from small fixtures.

## Required negative and crash tests

- canonical JSON duplicate keys, nonfinite/floats, oversize/deep input, unsafe keys and bool-as-order;
- create/open distinction, wrong expected ID, unknown schema/extra trigger/view/index;
- same key retry, changed key content, two workers and competing events;
- kill after intent commit, temp fsync, file publication, and catalog commit;
- recreate partial known staging; reject unknown final files; never delete immutable evidence;
- uncertain commit acknowledgement followed by fresh-connection verification;
- file/catalog/hash/partition tamper, missing file, hardlink/symlink/root replacement;
- clock rollback and future input observation;
- snapshot high-water stable during later appends; bounded page integrity and wrong-store token;
- CLI init requires explicit apply; audit/recover paths explicit; no auto production use.

## Execution ownership and cost

ChatGPT implements and reviews directly while the connector is usable. A single local Codex session is a fallback for inaccessible local execution or large environment-specific work: xhigh for persistence/recovery, high for ordinary adapter integration, medium for packaging. WorkBuddy is optional, not a standing four-session campaign: HY3 for inventories/checklists, GLM 5.3 Flash for bounded QA; a stronger available model is only used for a concrete failing test. Pricing/free periods are user-provided operating constraints, not independently verified here.

## Sources for the implementation decisions

SQLite: https://www.sqlite.org/atomiccommit.html ; https://www.sqlite.org/isolation.html
Python: https://docs.python.org/3/library/os.html ; https://docs.python.org/3/library/sqlite3.html

These references describe database/file primitives. They do not certify this implementation; real local tests and review remain mandatory.
