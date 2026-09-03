# Stage 4G.1 Checkpoint B1–B3 — Audited Market Path Pipeline Design

状态：`DESIGN_FROZEN / B1_READ_CONTRACT_IMPLEMENTED / STORE_MIGRATION_AND_WORKER_WIRING_PENDING`

日期：2026-09-02

依赖：

- Checkpoint A3：`3e6ad3f73c78f5d4ddd8f0537250ba5b275bd0f2`
- A4 Source Runtime/Artifact Store Binding 修复：当前工作树，尚未提交
- B0 Evidence Vocabulary / Pure Resolver：当前工作树，尚未提交

合同与最终 Review：ChatGPT GPT-5.6 Pro

复杂工程实现：单一 Codex 长会话

机械复跑：单一 WorkBuddy 长会话

## 1. 设计结论

现有 `stock_tracker.market_events.store.MarketEventStore` 可以继续作为 Stage 3F 的本地行情事件容器，但**不能直接被 Stage 4G.1 当作可冻结、可审计的路径权威读源**。B1 必须先把它升级为显式迁移、稳定 Store Identity、全局 immutable append chain 和 request-time high-water snapshot；B2 才能生成 B0 路径证据；B3 才能把冻结前缀和终局候选接入 Stage 4G。

执行顺序固定为：

```text
A4   close Runtime DB ↔ Artifact Store source binding
B0   freeze vocabulary / path / no-entry / partial-fill semantics
B1   Market Event Store v4 + audited read snapshot
B2   Runtime Path Evidence Store + per-case projection worker
B3   request-time prefix + Stage 4G operational orchestration
C1   deterministic Paper execution adapter
C2   authenticated Manual live-candidate adapter
D    restart / scale / multi-session shadow acceptance
```

B1、B2、B3 不得合并为一个“读行情后直接写 PATH_POINT”的快捷实现。这样做会绕过 Store Identity、known-at、Session coverage、late event、cursor 和 crash recovery 合同。

## 2. 对现有 Market Event Store 的审查

当前 Store 的正向能力：

- XTP `EventEnvelope` 有 canonical `event_id`；
- callback/provider sequence finding 已有初步结构；
- 文件记录有 partition hash chain；
- SQLite 有全局 `append_order`；
- 有 Session、Partition、Minute Bar materialization；
- 有基本 `verify_integrity()`；
- 路径和 metadata root 有一定 symlink 检查。

当前不能满足 B1 的问题：

| 缺口 | 后果 |
|---|---|
| `event_store_meta` 只有 schema、没有稳定 `store_id` | 无法证明两次读取属于同一逻辑源 |
| 构造函数可自动把旧 schema 改到新 schema | 打开 Store 即可能写盘，无法安全 dry-run/review |
| immutable record 不包含全局 append order | SQLite row 与文件记录之间无法形成完整全局事实身份 |
| immutable record 不包含 Store ID | 同样内容可被复制到另一 Store 而不暴露来源变化 |
| immutable record 不包含 `durable_known_at` | 无法证明事实何时已被本地 Store 持久化并可用 |
| 只有 partition chain，没有 global chain | 跨 symbol/partition 的 fork、gap 和顺序不能由文件事实证明 |
| `ingested_at` 只在 mutable SQLite metadata | 文件记录无法独立重建 known-at |
| `_atomic_write` 使用 replace 语义 | 已存在的 immutable target 可能被覆盖，而非 no-overwrite |
| Schema audit 不精确覆盖 table/index/trigger/view/hidden/generated/FK | 篡改后仍可能被普通查询读取 |
| `verify_integrity()` 没有稳定 audit identity/high-water | Exit Request 无法绑定一次不可变审计结果 |
| `list_events()` 不返回 append order、record/file hash、known-at、Store ID | 不能构造 B0 `RuntimeSourceReference` |
| `minute_bars` 是 mutable projection | 不能作为原始权威来源，也不含完整输入 lineage |
| Minute completeness 不是完整 Session coverage | 无法据此证明 TIMEOUT 或 NO_TRADE |
| 无明确 legacy non-empty migration policy | 旧记录可能被新合同事后重解释 |

结论：B1 需要 **Market Event Store schema v4 / immutable record v2**，不能只给现有 `list_events()` 多加几个字段。

## 3. B1 纯 Read Contract

已实现：

```text
stock_tracker/runtime_evidence/source_snapshot_contracts.py
tests/test_market_source_snapshot_contracts.py
```

公共纯合同：

```text
MarketEventSourceRecord
MarketEventSequenceFinding
MarketEventPartitionHead
MarketEventStoreAudit
MarketEventStoreSnapshot
MarketEventSelection
MarketEventReadPort
```

### 3.1 `MarketEventSourceRecord`

每条记录必须绑定：

```text
source_store_id
append_order
event_id
session_id
source / feed_mode
symbol / market / event_type / trading_day
source_time / received_at / durable_known_at
callback_seq / provider_seq
partition_key
previous_global_record_hash
previous_partition_record_hash
raw_payload_sha256
canonical payload_json / payload_sha256
parser_id / source_schema_id
record_file / record_file_sha256
record_hash
source_record_id
```

`record_hash` 对以下事实做 canonical SHA-256：

```text
Store identity
全局 append order
Event identity
时间与序列
partition identity
两个 previous hash
raw/payload hash
parser/schema identity
```

`source_record_id` 再绑定 `record_hash + record_file_sha256`，用于 B0 Source Reference。

不变量：

```text
source_time <= durable_known_at
received_at <= durable_known_at
append_order > 0
partition_key == market/day/symbol
payload_json strict canonical JSON
payload_sha256 == SHA256(payload_json bytes)
record_file safe relative path
record_hash 可重建
```

### 3.2 `MarketEventStoreAudit`

B1 初版只允许：

```text
scope = FULL_PREFIX
```

Audit 必须绑定：

```text
source_store_id
source_schema_id
high_water_append_order
audited_at
catalog_schema_fingerprint
ordered global record hashes
canonical partition heads
first/last global record hash
audit_id
```

`high_water_append_order=N` 必须对应从 `1..N` 的完整全局前缀；不能把 filtered query 的最后一条 append order 冒充完整 high-water。

### 3.3 `MarketEventStoreSnapshot`

Snapshot 必须证明：

- Record append order 从 1 连续到 high-water；
- global previous hash 连续；
- 每个 partition previous hash 连续；
- Store ID 一致；
- `durable_known_at <= audited_at`；
- Event ID 和 immutable record path 唯一；
- Partition head count/first/last 与 Record 一致；
- Sequence Finding 指向同一 audited prefix 内的确切 Event/append order；
- Snapshot 的 Audit record hashes 与 Records 完全一致。

### 3.4 `MarketEventSelection`

Selection 是 Full Snapshot 的过滤视图，不是新的事实源。它必须绑定：

```text
source_store_id
snapshot_audit_id
snapshot_high_water_append_order
symbol / market
source-time window
ordered source record IDs/hashes
selection_id
```

Selection 不得包含 high-water 后的记录，不得跨 Store、symbol 或时间窗口。调用方必须显式提供 `market`，因此即使 audited prefix 中该 symbol 为零事件，也可以生成绑定同一 audit/high-water 的空 Selection；空 Selection 只证明“该 audited prefix/filter 没有 Event”，不自动等于 `NO_TRADE` 或 `SUSPENDED`。

## 4. Market Event Store v4

### 4.1 显式迁移

新增独立脚本：

```text
scripts/market_event_migrate.py
```

默认：

```text
DRY_RUN
```

`--apply` 必须要求：

```text
--backup <new-path>
```

Store 构造函数不得执行 schema upgrade。旧版本打开行为：

```text
UNINITIALIZED             → read/write disabled until explicit init/apply
VALID_V4                  → normal open
VALID_V2/V3 EMPTY         → migration required
VALID_V2/V3 NON_EMPTY     → LEGACY_MARKET_EVENT_STORE_REQUIRES_EXPORT
PARTIAL / UNKNOWN / TAMPERED → fail closed
```

原因：旧 immutable record 没有 Store ID、global append order、durable-known time 和 global chain。普通迁移无法在不重写历史事实的情况下补齐这些字段。

### 4.2 Legacy export

非空 v2/v3 数据只能进入独立命令：

```text
scripts/export_legacy_market_events.py
```

输出：

- 保留原 Store/文件的只读 hash inventory；
- 每条旧 Event 标记 `LEGACY_SOURCE_IDENTITY_INCOMPLETE`；
- 输出到新目录，不原地重写；
- 不把导出记录称为 v4 原生权威记录；
- 不自动送入 Trusted Admission；
- 是否允许作为 candidate-only backfill，留给独立 Review。

### 4.3 Stable Store Identity

v4 `event_store_meta` 至少包含：

```text
schema = stock-tracker-market-event-store-v4
store_id = random 256-bit material hashed to lowercase SHA-256
record_schema = stock-tracker-market-event-record-v2
single_active_writer = true
fork_detection = LOCAL_GLOBAL_APPEND_CHAIN_ONLY
external_checkpoint = NOT_IMPLEMENTED
```

Meta 必须 append-only/no-update/no-delete。Backup/restore 保留 `store_id`；新初始化产生新 `store_id`。

### 4.4 Immutable Record v2

Record file必须包含：

```text
schema
source_store_id
append_order
durable_known_at
parser_id
source_schema_id
event canonical document
previous_global_record_hash
previous_partition_record_hash
record_hash
```

`record_hash` 同时形成 global 和 partition 链。

文件发布：

```text
temporary file
→ write + flush + fsync
→ atomic no-overwrite link/publication
→ existing exact bytes = idempotent
→ existing different bytes = STORE_INTEGRITY_BLOCK
```

不得使用 silent `os.replace` 覆盖 immutable target。

### 4.5 Catalog v4

权威 append-only表至少包括：

```text
event_store_meta
events_v4
sequence_findings_v4
store_integrity_events
```

`events_v4` 必须保存：

```text
append_order PRIMARY KEY
source_store_id
event_id UNIQUE
record_hash UNIQUE
record_file UNIQUE
record_file_sha256
previous_global_record_hash
previous_partition_record_hash
durable_known_at
parser_id
source_schema_id
partition_key
```

Derived 表：

```text
sessions
partitions
minute_bars
```

可以重建或更新，但不得成为 B1 `MarketEventSourceRecord` 的事实权威。任何 B2 projection 必须最终回指 `events_v4` 的 source record IDs/hashes。

### 4.6 Exact schema audit

每次 writer extension 和每次 frozen snapshot 前必须验证：

- SQLite quick/integrity check；
- exact table set；
- `PRAGMA table_xinfo`；
- exact indexes及列顺序/unique；
- exact trigger table/body；
- views whitelist；
- hidden/generated columns；
- foreign keys及 `foreign_key_check`；
- journal/synchronous配置；
- Store/root/catalog path identity；
- symlink/junction/reparse point；
- immutable file inventory；
- Catalog ↔ Record bytes/hash；
- global/partition chains；
- monotonic `durable_known_at`；
- append order连续；
- sequence finding引用完整性。

`catalog_schema_fingerprint` 必须由受审计 Schema inventory确定，而不是常量自报。

### 4.7 Append transaction与 crash windows

Writer流程：

```text
BEGIN IMMEDIATE
→ exact schema/state audit
→ injected UTC clock / rollback guard
→ allocate append_order
→ build immutable record v2
→ atomic no-overwrite file publication
→ insert events_v4
→ append sequence findings
→ update derived projections
→ re-audit affected/global invariants
→ COMMIT
```

Crash window：

| 窗口 | 恢复 |
|---|---|
| file发布前 | 无事实，安全重试 |
| file已发布、DB未commit | 只允许 exact next-record orphan adoption |
| DB commit结果不确定 | 按 event_id/record_hash重新读取，exact相同为 idempotent |
| Catalog有row、file缺失/不同 | Store Integrity Block |
| orphan不是 exact next record | Store Integrity Block |

### 4.8 Frozen read transaction

`freeze_full_prefix()` 必须：

1. 验证 Store/root/catalog identity；
2. 获取与 writer串行化的 Store lock + `BEGIN IMMEDIATE`；
3. 确定 requested/current high-water；
4. 完整审计 `1..high-water`；
5. 验证不存在未解决 crash orphan；
6. 构造 `MarketEventSourceRecord`；
7. 计算 `MarketEventStoreAudit`；
8. 在同一 SQLite snapshot内完成结果；
9. rollback只读事务；
10. 返回 immutable `MarketEventStoreSnapshot`。

不得先查询 high-water，释放锁后再逐条读取；否则并发 append 会混合两个事实版本。

## 5. Session Coverage Authority

B0 的 TIMEOUT/NO_TRADE 不能只靠“没有看到事件”或 `MinuteCompleteness.COMPLETE`。

B1/B2 必须引入独立、可审计的 `SourceSessionCoverageFact`，至少冻结：

```text
source_store_id
session_id
symbol / market / trading_day
scheduled_open_at / scheduled_close_at
coverage_start / coverage_through
first/last source append order
first/last callback sequence
connection/reconnect epochs
gap/out-of-order/sparse findings
session_complete
coverage_policy_id
coverage_fact_id
```

规则：

- `COMPLETE_PREFIX`：连续证明到 `coverage_through < scheduled_close_at`；
- `COMPLETE_SESSION`：必须证明到 `scheduled_close_at`；
- reconnect窗口和 sequence gap不能被静默忽略；
- 仅“最后一分钟有 Bar”不证明完整 Session；
- 没有 Coverage Fact时不能返回 TIMEOUT；
- 没有 explicit zero-event coverage时不能返回 NO_TRADE。

现有 `MinuteCompleteness` 只能作为 coverage输入之一，不能单独构成 Session authority。

## 6. Calendar与Security Status Adapter

可复用现有 PIT 类型：

```text
stock_tracker.quant.core.calendar.CalendarDay
stock_tracker.quant.core.calendar.CalendarCoverage
stock_tracker.quant.core.calendar.InstrumentSessionStatus
```

Adapter 必须使用 exact project type，并重建/绑定：

```text
fact_id
known_at
usable_from
source
revision
calendar_version
verified
source_note
```

读取 cutoff：

```text
known_at <= prefix.frozen_at
usable_from <= prefix.frozen_at
```

映射：

| Source fact | B0状态 |
|---|---|
| `CalendarStatus.CLOSED` | `MARKET_CLOSED` |
| `CalendarStatus.OPEN` + complete trade coverage | `TRADED` |
| Whole-session authoritative `SUSPENDED` + complete zero-event coverage | `SUSPENDED` |
| explicit zero-trade authority + complete coverage | `NO_TRADE` |
| 缺Calendar coverage、UNKNOWN或无法证明数据完整 | `MISSING_DATA/BLOCKED` |

禁止映射：

- 无行情记录 → `SUSPENDED`；
- 无行情记录 → `NO_TRADE`；
- `HALTED/VCM_HALT/DELISTED/UNKNOWN` → whole-session `SUSPENDED`；
- 当前数据缺失 → `MARKET_CLOSED`。

当前项目没有足够的 explicit `NO_TRADE` authority，因此 B1 首版可以产生：

```text
MARKET_CLOSED
TRADED
SUSPENDED（仅有完整whole-session事实时）
MISSING_DATA
```

但不得凭推断生成 `NO_TRADE`。

## 7. Tick/Minute Projection

B2 不得直接使用 mutable `minute_bars` row 构造 B0 Point。

若使用 Tick：

- 一个 accepted `TRADE_TICK` source record → 一个 B0 TICK Observation；
- high=low=close=trade price；
- Source Reference回指确切 v4 Record。

若聚合 Minute Bar，必须生成 immutable `RuntimeMinuteProjectionArtifact`：

```text
projection_policy_id
source_store_id
snapshot_audit_id
source high-water
symbol / market
interval_start / interval_end
ordered input source_record_ids/hashes
OHLC / volume / amount
coverage_through
finding IDs
projection_id
```

同一 projection policy + 同一 ordered input records 必须 deterministic。输入事件变化或 late event到达时产生新 projection ID，不能修改旧 Artifact。

Minute Projection只在其 input interval完整且无 gap/out-of-order时可标记 observable。否则形成 B0 blocker，不生成“最佳努力”OHLC。

## 8. B2 Runtime Path Evidence Store

由于当前 Stage 4G Collection v3不能原生重建：

- Session Evidence；
- interval start/end和granularity；
- source/collection high-water；
- source audit；
- prefix/resolution identity；
- `ENTRY_EXPIRED/USER_CANCELLED`；
- partial-fill aggregation；

B2 **不能把全部新语义塞进 `raw_bar_snapshot_id/evidence_ids` 后直接声称 v3已支持**。

B2先建立独立 append-only `RuntimePathEvidenceStore`：

```text
data/runtime-path-evidence.db
data/runtime-path-evidence/
```

### 8.1 Stable identities

```text
path_store_id
source_store_id
source_adapter_policy_id
collection_store_id（可空，B3绑定）
```

首次绑定后不可静默切换。

### 8.2 Append-only事实

```text
CASE_SOURCE_REGISTERED
SOURCE_SNAPSHOT_OBSERVED
SESSION_EVIDENCE
PATH_OBSERVATION
MINUTE_PROJECTION
SOURCE_FINDING
PATH_PREFIX_FROZEN
PATH_RESOLUTION
EXECUTION_SUMMARY
NO_ENTRY_CANDIDATE
INTEGRITY_EVENT
```

Immutable payload和mutable worker state必须分离。

### 8.3 Per-case cursor

每个Case独立保存：

```text
case_id
source_store_id
last_audited_high_water
last_source_audit_id
last_projected_source_append_order
projection_policy_id
updated_at
```

原因：新Case打开时，需要回看Entry之后、当前high-water之前已存在的source records；单一全局cursor会漏掉后创建Case的历史路径。

Case首次注册：

```text
freeze source prefix at registration
→ select all matching source records up to registration high-water
→ project Entry之后记录
→ audit Path Evidence Store
→ set per-case cursor to registration high-water
```

后续：

```text
freeze newer source prefix
→ examine (last high-water, new high-water]
→ append new/late records deterministically
→ audit
→ advance per-case cursor
```

Late event即使market timestamp早，也按新source append order进入新Path fact；旧Exit Request prefix不改变。

### 8.4 Idempotency

Projection identity至少绑定：

```text
case_id
source_store_id
source_record_id or ordered input record IDs
projection_policy_id
```

同一source事实重放exact idempotent；同一identity不同payload为conflict/global block。

### 8.5 Cursor提交条件

```text
source snapshot audited
+ all applicable projections terminal
+ immutable Path Evidence durable
+ Path Store audit covers records
→ cursor advance
```

不可在“开始处理”或“写入内存”时推进。

### 8.6 Failure classes

| Class | 行为 |
|---|---|
| source record contract poison | quarantine该source record projection，保留稳定fact，继续其他Case/record |
| transient source/store unavailable | 有界retry |
| retry exhausted | block该source-store/path-store binding，不丢合法record |
| source/store identity/schema/hash/inventory/fork | global binding block |
| calendar/security coverage不足 | case-level BLOCKED evidence，不推进终局，但source cursor可在证据已记录后推进 |
| target Collection unavailable | 不影响Path Store事实；B3 delivery重试 |

## 9. B3 Stage 4G Operational Orchestration

### 9.1 兼容策略

当前 Stage 4G Collection v3保持原语义，不修改旧事实定义。B3使用两个层次：

1. `RuntimePathEvidenceStore` 保存完整B0/B1/B2事实；
2. 对当前v3可以精确表达的兼容字段，生成可重建Projection。

不得把不可表达的状态强行映射：

```text
ENTRY_EXPIRED   ≠ DATA_INVALID
USER_CANCELLED  ≠ DATA_INVALID
SUSPENDED       ≠ fake OHLC
NO_TRADE        ≠ fake OHLC
MISSING_DATA    ≠ no barrier touch
PARTIAL fill    ≠ complete Fill
```

### 9.2 Stage 4G compatible PATH projection

只有B0 Observation满足：

- exact interval/source evidence在Path Store；
- `observable=true`；
- Case已有完整Entry；
- session index和时间在窗口内；
- projection policy已冻结；

才可调用现有：

```text
OutcomeCollectionStore.record_path_point(...)
```

映射：

```text
timestamp             = observation.interval_end
session_index         = B0 open_session_index
high/low/close        = B0 Decimal
raw_bar_snapshot_id   = path_observation_id or minute_projection_id
evidence_ids          = source_record_id, source audit ID,
                        session evidence ID, path-store record ID
```

B3必须在自己的delivery state中验证Stage 4G返回的Fact/Disposition，之后才能推进Path→Collection cursor。

### 9.3 Request-time prefix

在提出Exit Request前：

```text
freeze Path Store high-water
→ build RuntimeFrozenPathPrefix
→ compute prefix_id
→ run B0 resolver
→ persist PATH_PREFIX_FROZEN
→ persist PATH_RESOLUTION
→ audit Path Store
```

只有：

```text
TARGET
STOP
TIMEOUT
```

可生成Stage 4G Exit Request candidate。

`OPEN/BLOCKED`不能调用`record_exit_request()`。

Exit Request的`evidence_ids`必须包括：

```text
prefix_id
resolution_id
path_store_audit_id
source_store_audit_id
first-touch source/path fact IDs（TARGET/STOP）
```

现有v3内部prefix freeze继续保留，但B3必须交叉验证v3 prefix与B0 frozen prefix投影完全一致。不同则失败关闭。

### 9.4 No-entry

现有v3 `OutcomeTerminalReason`不包含`ENTRY_EXPIRED/USER_CANCELLED`。在显式Collection Schema升级前：

- B2可以产生 immutable `RuntimeNoEntryEvidence` candidate；
- B3不得调用`record_no_entry(DATA_INVALID)`替代；
- Case保持AWARE/候选或进入明确unsupported状态；
- 后续新增v4 no-entry event schema后再接线。

### 9.5 Partial fill

- B2保存所有Execution Fragment和Summary；
- 当前v3只接受完整数量的单Entry/Exit Fill；
- `PARTIAL` Summary不得投影到v3；
- 完整Summary可按quantity-weighted price和总explicit cost投影，但必须保留Summary ID和所有Fragment Fact IDs；
- native multi-leg仍失败关闭。

## 10. Case打开时机

Runtime Decision Artifact成功写入不等于可以打开Stage 4G Case。

Case打开至少需要：

```text
Runtime Artifact durable + audited
execution policy/rule/cost identities frozen
requested quantity frozen
instrument identity可用
Entry Adapter产生可验证Intent
```

`record_entry_fill()`前必须有完整Entry Execution Summary。B0/B1/B2本身不创建Paper/Manual Entry，因此在C1/C2前：

```text
B1/B2可以完成Store和Worker基础设施
但不得在生产路径自动创建真实Collection Case或Entry Fill
```

## 11. 安全与能力边界

B1–B3只读行情和本地证据Store：

```text
no Trader API
no Order API
no Cancel API
no Algo API
no Account/Position write
no auto_trade
```

XTP仅允许Quote/Event ingestion。任何新代码出现下单能力即阶段阻断。

## 12. Test Matrix

### B1

- explicit dry-run/apply/backup；
- constructor不自动迁移；
- stable store ID；
- empty v2/v3 upgrade；
- non-empty legacy fail close；
- exact schema/index/trigger/view/generated/FK audit；
- global/partition chain；
- append order gap；
- record/file/catalog mismatch；
- no-overwrite collision；
- exact orphan adoption；
- DB/root replacement；
- clock rollback；
- snapshot high-water race；
- snapshot audit identity；
- filtered selection cannot exceed high-water；
- sequence finding binding；
- production Store unchanged in dry-run。

### B2

- per-case initial backfill；
- later tailing；
- late-observed early timestamp；
- duplicate source replay；
- source cursor crash before/afterPath append；
- Path Store commit uncertainty；
- source/store replacement；
- calendar closed/open；
- suspension status authority；
- missing data/gap/out-of-order；
- Tick projection；
- Minute projection input lineage；
- coverage through/scheduled close；
- no fakeOHLC；
- noEntry/partial candidate only。

### B3

- Stage 4G compatible path projection；
- exact evidence IDs；
- target-before-stop；
- stop-before-target；
- intrabar ambiguity blocked；
- TIMEOUT complete horizon only；
- frozen prefix late backfill immunity；
- OPEN/BLOCKED no exit request；
- Stage 4G append failure does not advance cursor；
- Stage 4G duplicate idempotent；
- prefix projection mismatch block；
- ENTRY_EXPIRED/USER_CANCELLED no DATA_INVALID mapping；
- partial fill no projection；
- no Broker write surface。

## 13. Review Checkpoints

```text
A4_B0_B1_CONTRACT
    A4 binding fix + B0 resolver + B1 pure snapshot contracts

B1_STORE
    Market Event Store v4 migration/write/read snapshot

B2_PATH_STORE
    Path Evidence Store + projection worker, no production Case opening

B3_COLLECTION_BRIDGE
    frozen prefix + compatible Stage 4G projection/orchestration
```

每个Checkpoint：

```text
Codex scoped commit
→ WorkBuddy mechanical rerun
→ ChatGPT exact commit/tree/diff adversarial review
→ same Codex session fixes findings
```

WorkBuddy不能签发工程PASS。

## 14. 当前分工

### ChatGPT已完成

- A3 adversarial review和runner error裁定；
- A4 source Runtime/Artifact Store binding修复；
- B0完整词汇、PIT prefix、first-touch、no-entry、partial-fill纯合同；
- B1 audited source snapshot纯合同；
- B1–B3 Store/migration/cursor/Collection bridge设计。

### Codex下一步

- 先复核当前未提交A4/B0/B1合同实现；
- 不弱化合同；
- 修正实现细节或测试；
- 分两个scoped commits提交A4和B0/B1合同；
- staging后重跑source-distribution和全部门禁；
- 停止等待ChatGPT最终Review；
- 通过后才开始B1 Store v4工程接线。

### WorkBuddy下一步

- 使用固定Python 3.14和中性checkout复跑；
- source-distribution在有`.git`的真实checkout运行；
- archive/import测试在不带safe-delete overlay的目录运行；
- 记录命令、exit code、pass/fail/skip；
- 只输出`MECHANICAL_CHECK_COMPLETE`。

## 15. 声明边界

完成B1–B3仍然只代表：

```text
operational candidate evidence pipeline
```

不代表：

```text
Trusted Outcome Admission
真实投资业绩
自动交易能力
生产多交易日Shadow Acceptance
```

永久保持：

```text
auto_trade = false
trusted_outcome_admission = false
verified = false（Paper/Manual candidate）
real_scoreboard = INSUFFICIENT_REAL_EVIDENCE
```
