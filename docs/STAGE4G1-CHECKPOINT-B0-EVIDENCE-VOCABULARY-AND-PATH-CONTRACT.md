# Stage 4G.1 Checkpoint B0 — Evidence Vocabulary and Path Resolution Contract

状态：`CONTRACT_FROZEN / PURE_CORE_IMPLEMENTED / STORAGE_AND_WORKER_WIRING_PENDING`

日期：2026-09-02

前置实现：Stage 4G.1 Checkpoint A3 `3e6ad3f73c78f5d4ddd8f0537250ba5b275bd0f2`

合同所有者与最终 Reviewer：ChatGPT GPT-5.6 Pro

复杂实现执行者：单一 Codex 长会话

机械检查：单一 WorkBuddy 长会话

## 1. 决策摘要

Checkpoint B 不直接从 Paper 或 Manual Adapter 开始。必须先冻结路径和执行证据语义，否则不同 Adapter 会对停牌、无成交、缺失数据、first-touch、请求时可知前缀和 partial fill 做出不一致解释。

后续顺序固定为：

```text
B0  Evidence Vocabulary + Pure Resolver Core
B1  Audited Market Event Store Reader + Stable Source Identity
B2  Durable Path Worker + Stage 4G Collection Append Integration
B3  Request-Time Prefix Freeze + Terminal Request Orchestration
C1  Deterministic Paper Adapter
C2  Authenticated Manual Live-Candidate Adapter
D   Crash/Restart/Scale/Shadow Acceptance Harness
```

本文件和 `stock_tracker/runtime_evidence/path_contracts.py` 冻结 B0 语义。Codex 可以补实现、存储和适配器，但不得重新定义这些状态或把失败关闭条件改成最佳努力推断。

## 2. 本阶段不做什么

B0 不：

- 打开真实 Stage 4G Collection Case；
- 读取生产 Market Event Store；
- 自动产生真实 Entry/Exit；
- 修改 `OutcomeTerminalReason` 后向兼容语义；
- 把 `ENTRY_EXPIRED` 或 `USER_CANCELLED` 映射为 `DATA_INVALID`；
- 允许 Broker/XTP Trader、Order、Cancel、Algo、Account 或 Position 写接口；
- 建立 Trusted Outcome Admission；
- 产生或宣称真实 Strategy Scoreboard；
- 解除 `auto_trade=false`。

## 3. 词汇表

### 3.1 日历状态与开市日状态必须分层

日历层只有：

```text
OPEN
MARKET_CLOSED
```

`MARKET_CLOSED` 表示权威交易日历认定该自然日无交易 Session。它：

- 没有 `open_session_index`；
- 不增加策略 horizon；
- 不携带 OHLC；
- 不等于 `SUSPENDED`；
- 不等于 `NO_TRADE`；
- 不等于 `MISSING_DATA`。

开市日层只有：

```text
TRADED
SUSPENDED
NO_TRADE
MISSING_DATA
```

| 状态 | 含义 | 是否增加 horizon | 是否允许 OHLC Point | 完整性要求 |
|---|---|---:|---:|---|
| `TRADED` | 标的在权威开市 Session 内存在成交 | 是 | 是 | 必须绑定 coverage Fact |
| `SUSPENDED` | 权威证券状态证明停牌 | 是 | 否 | 必须有 security-status Fact 和完整 Session 证明 |
| `NO_TRADE` | 开市、非伪造停牌，但完整 Session 无成交 | 是 | 否 | 必须有 security-status 与 coverage Fact |
| `MISSING_DATA` | 应有开市 Session，但数据完整性无法证明 | 是 | 否 | 永久阻断终局解析，不能伪造 OHLC |

### 3.2 Coverage 状态

```text
COMPLETE_PREFIX
COMPLETE_SESSION
INCOMPLETE_GAP
INCOMPLETE_OUT_OF_ORDER
INCOMPLETE_SPARSE
NOT_APPLICABLE
```

- `COMPLETE_PREFIX` 必须绑定 `coverage_through`，只证明截至该时刻的无缺口前缀，不表示 Session 已结束。
- `COMPLETE_SESSION` 必须绑定完整 Session 的 `coverage_through`。
- `INCOMPLETE_*` 是可审计 blocker，不得当作“没有触及障碍”。
- `NOT_APPLICABLE` 只能用于 `MARKET_CLOSED`。
- 每个 `OPEN` Session 必须绑定 `scheduled_open_at/scheduled_close_at`。
- `COMPLETE_SESSION` 必须满足 `coverage_through == scheduled_close_at`。
- Entry 和所有 Path Point 必须位于计划 Session 窗口内。

### 3.3 路径粒度

```text
TICK
MINUTE_BAR
DAILY_BAR
```

- `TICK` 必须满足 `interval_start == interval_end` 且 `high == low == close`。
- Bar 必须有非零时间区间。
- 粗粒度 Bar 同时触及 target 和 stop 时为 `INTRABAR_AMBIGUITY`。
- 不允许事后选择更有利的障碍顺序。

### 3.4 Path Resolver 状态

```text
OPEN
TARGET
STOP
TIMEOUT
BLOCKED
```

`BLOCKED` 与 `OPEN` 不可混用：

- `OPEN`：已知前缀完整，但尚未达到 horizon，也未出现障碍。
- `BLOCKED`：存在缺失、歧义、身份或覆盖缺口，当前证据无法证明任何终局。

## 4. Source Reference 合同

每个 Session 或 Path Point 必须绑定：

```text
source_store_id
source_session_id
source_record_id
source_append_order
source_record_hash
raw_payload_sha256
parser_id
schema_id
source_time
received_at
durable_known_at
source_reference_id
```

关键不变量：

```text
source_time <= durable_known_at
received_at <= durable_known_at
source_append_order > 0
record_hash/raw_payload_sha256 使用 lowercase SHA-256
```

`source_time` 与 `received_at` 之间不在 B0 中假定绝对先后，因为外部 Provider 时钟可能存在偏差；是否允许偏差必须由 B1 的版本化 clock policy 决定。终局判断只使用可证明的 `durable_known_at`。

B1 必须为 Market Event Store 增加稳定、可审计的 `source_store_id`。当前 Market Event Store 没有可用的稳定 Store Identity，因此在 B1 完成前不能声明 Operational Path Collection 已接线。

## 5. Request-Time Frozen Prefix

退出请求或终局请求必须绑定一个不可变 `RuntimeFrozenPathPrefix`：

```text
case_id
symbol / market
collection_store_id
source_store_id
frozen_at
source_high_water_append_order
collection_high_water_append_order
source_audit_id
collection_audit_id
ordered facts
prefix_id
```

每个 frozen fact 同时绑定：

```text
collection_append_order
collection_fact_id
collection_observed_at
source_reference_id
session_evidence_id or path_observation_id
```

规则：

1. `source durable_known_at <= collection_observed_at <= frozen_at`。
2. source/collection append order 不得超过冻结时 high-water mark。
3. prefix 中的 collection append order 必须唯一且严格递增。
4. `prefix_id` 包含 ordered facts、两个 high-water mark 和两个 audit ID。
5. Resolver 只允许读取该 prefix。
6. 请求后才 durable append 的事件，即使 market timestamp 更早，也不能回填解释旧请求。
7. 新数据只能形成新的 prefix 和新的 resolution，不得修改旧 prefix。

这关闭以下未来泄漏：

```text
EXIT_REQUEST at T
late event observed at T+1, source timestamp T-1
→ late event 不属于 T 时冻结前缀
→ 不得改变 T 时的 TARGET/STOP/TIMEOUT 结论
```

## 6. Observation Window

每个 path window 必须冻结：

```text
case_id
symbol / market
entry_filled_at
entry_trading_day
entry_session_index
entry_price
target_price
stop_price
horizon_sessions
terminal_policy_id
window_id
```

价格关系必须为：

```text
stop_price < entry_price < target_price
```

窗口边界规则：

- Entry Session 中，Point 的 `interval_end < entry_filled_at`：`POINT_BEFORE_ENTRY_WINDOW`。
- Entry Session 中，粗粒度 Point 横跨 `entry_filled_at`：`WINDOW_BOUNDARY_OVERLAP`。
- 横跨边界的 Daily/Minute Bar 不能用完整 high/low 证明 Entry 后障碍。
- 需要更细粒度事件才能解除 blocker。
- horizon 只按 `OPEN` Session 的 `open_session_index` 计数。
- Prefix 中 horizon 后的数据不能影响 horizon 内 resolution。

## 7. First-Touch 解析

解析顺序：

```text
validate prefix identity
→ validate consecutive calendar days
→ validate contiguous OPEN session indexes
→ validate Session coverage/status
→ validate Point ↔ Session relationship
→ validate Point.interval_end <= Session.coverage_through
→ validate interval order/non-overlap
→ scan points in chronological source order
→ return first TARGET/STOP or blocker
→ only after complete horizon coverage may return TIMEOUT
```

### 7.1 TARGET

必须证明：

- frozen prefix 内存在 Point；
- Point 在 Entry 后、horizon 内；
- 该 Point 及其之前的 OPEN Session coverage 可证明完整；
- 之前没有 STOP；
- Point 不同时触及 STOP。

### 7.2 STOP

与 TARGET 对称。先 STOP 后 TARGET 时不能选择 TARGET。

### 7.3 TIMEOUT

必须同时满足：

- Entry 到 horizon 的 OPEN session index 连续；
- 所有相关 Session 没有 `MISSING_DATA` 或 `INCOMPLETE_*`；
- horizon Session 是 `COMPLETE_SESSION`；
- horizon 前不存在 TARGET/STOP；
- `MARKET_CLOSED` 自然日不增加 horizon。

### 7.4 BLOCKED

稳定 blocker 至少包括：

```text
CALENDAR_COVERAGE_GAP
ENTRY_SESSION_MISSING
ENTRY_SESSION_MISMATCH
ENTRY_SESSION_NOT_TRADED
ENTRY_TIME_OUTSIDE_SESSION_WINDOW
OPEN_SESSION_INDEX_GAP
POINT_WITHOUT_SESSION
POINT_BEFORE_ENTRY_WINDOW
POINT_OUTSIDE_SESSION_WINDOW
WINDOW_BOUNDARY_OVERLAP
OVERLAPPING_PATH_OBSERVATIONS
NONTRADED_SESSION_WITH_POINTS
TRADED_SESSION_WITHOUT_POINTS
POINT_BEYOND_COVERAGE
SESSION_PREFIX_NOT_CLOSED
SESSION_GAP
SESSION_OUT_OF_ORDER
SESSION_SPARSE
MISSING_DATA
INTRABAR_AMBIGUITY
HORIZON_SESSION_INCOMPLETE
```

Malformed identity/contract 使用 `RuntimePathContractError`；可表达但证据不足使用 `BLOCKED`。不能把二者静默降级为 `OPEN`。

## 8. No-Entry 词汇

B0 明确定义：

```text
ENTRY_EXPIRED
USER_CANCELLED
ORDER_REJECTED
DATA_INVALID
```

它们不是同义词。

### 8.1 共同条件

所有 no-entry 证据必须：

- 绑定一个完整、审计过的 execution stream；
- 聚合成交量为 0；
- 没有 Entry/Exit/Path 事实；
- 绑定 execution policy；
- 绑定 reason-specific Fact。

### 8.2 ENTRY_EXPIRED

必须：

```text
zero fill
execution stream complete + audit ID
entry window coverage complete + coverage Fact
now/decided_at >= entry_window_end
```

普通“没有看到成交”不构成 `ENTRY_EXPIRED`。

### 8.3 USER_CANCELLED

必须：

```text
zero fill
actor_id
actor_authentication_fact_id
cancellation_fact_id
```

未来 Manual Adapter 必须提供 replay-protected authenticated cancellation；任意文本备注不构成取消事实。

### 8.4 ORDER_REJECTED

必须有明确 rejection Fact 且 zero fill。不得从超时或取消推导。

### 8.5 DATA_INVALID

必须有明确 data-invalidity Fact。不得作为未成交、过期、用户取消、停牌、无成交或市场休市的兜底枚举。

当前 `OutcomeTerminalReason` 和 Stage 4G Collection v3 尚未原生表达 `ENTRY_EXPIRED/USER_CANCELLED`。Codex 后续必须以显式 Schema 版本升级接入；在升级前只能生成 B0 candidate evidence，不能错误映射到 `DATA_INVALID`。

## 9. Partial Fill Aggregation

B0 定义 `RuntimeExecutionFragment` 与 `RuntimeExecutionSummary`：

```text
intent_id
execution_id
source_fact_id
side
timestamp / durable_known_at
quantity
price
explicit_cost
```

Summary：

```text
requested_quantity
exact unique execution IDs
quantity-weighted price
total explicit cost
execution_stream_complete
execution_stream_audit_id
completion = ZERO_FILL | PARTIAL | COMPLETE
```

规则：

- Fragment 必须属于同一个 intent 和 side。
- execution ID 不得重复。
- 成交量不得超过 requested quantity。
- `execution_stream_complete=true` 必须绑定 audit ID。
- `PARTIAL` 不得进入当前 Stage 4G v3 finalization。
- 只有 `COMPLETE` summary 可投影成当前单 Fill 合同。
- `native_multi_leg=true` 在当前合同下失败关闭。
- Scale-in、trim、partial take-profit、Trend Runner 必须等待独立 Schema 版本，不能复用当前 v3 名称解释新语义。

## 10. A3 后的 Runtime/Artifact Store 绑定加固

A3 Review 发现一个装配层漏洞：Worker 原先只把 `artifact_store_id` 交给 Runtime DB，没有证明 Artifact Store 声明的 `source_runtime_store_id` 与 Runtime DB 的 `runtime_store_id` 相同。错误装配可能导致合法 occurrence 被当作 row poison quarantine。

已冻结修复：

```text
RuntimeOutboxLease carries runtime_store_id
Worker passes ArtifactStore.source_runtime_store_id when claiming
Repository compares source_runtime_store_id with Runtime DB runtime_store_id before binding/lease
lease binding verifies runtime_store_id + artifact_store_id
store-level runtime binding mismatch is STORE_INTEGRITY_BLOCK
mismatch cannot quarantine or advance cursor
```

稳定错误码：

```text
ARTIFACT_STORE_SOURCE_RUNTIME_MISMATCH
ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH
ARTIFACT_STORE_BINDING_MISMATCH
```

## 11. B1 实现合同

Codex 下一步只做 audited read-side，不先做 Paper/Manual。

B1 必须：

1. 为 Market Event Store 增加稳定 Store Identity 和 exact schema audit。
2. 暴露 read-only Snapshot API：
   - source store identity；
   - high-water append order；
   - audit ID；
   - ordered source records；
   - record/raw hashes；
   - parser/schema identity；
   - source/received/durable-known times。
3. 不直接把 `MinuteBarRecord` 当作完整 Session coverage。
4. 显式映射 `GapKind` 与 `MinuteCompleteness` 到 B0 coverage 状态。
5. 交易日历和 security status 必须来自可审计 authority；不能根据是否有价格记录反推停牌。
6. B1 仍不写 Stage 4G Collection。

## 12. B2 实现合同

B2 才实现 durable Path Worker：

```text
read audited source snapshot
→ construct exact B0 source/session/point evidence
→ append Stage 4G PATH facts idempotently
→ verify Stage 4G append/audit
→ advance durable source cursor
```

要求：

- source cursor 与 collection cursor 分离；
- cursor 只能在目标 durable append + audit 后推进；
- duplicate replay exactly-once observable；
- source gap/out-of-order 分区隔离；
- global source/store integrity failure 阻断 lane；
- row contract poison quarantine；
- 不删除事实重试；
- 不打开或 finalize 缺少 Entry/Execution 证据的 Case。

## 13. B3 实现合同

B3 使用 `RuntimeFrozenPathPrefix`：

- 在 Exit Request 时冻结 prefix；
- 保存 `prefix_id`、两个 audit ID 和两个 high-water mark；
- 调用纯 `resolve_runtime_path`；
- 只把 TARGET/STOP/TIMEOUT candidate 交给 Stage 4G request API；
- BLOCKED/OPEN 不得伪造 exit；
- late event 只能生成新 prefix，不修改旧请求。

## 14. 测试门禁

B0 当前纯合同测试至少覆盖：

- TARGET before STOP；
- STOP before TARGET；
- same-bar ambiguity；
- tick ordering；
- complete TIMEOUT；
- horizon not reached；
- missing data；
- market closed horizon semantics；
- calendar gap；
- Entry boundary overlap；
- non-traded session with price point；
- overlapping bars；
- prefix frozen-at/high-water identity；
- suspension authority；
- source known-at；
- partial/complete aggregation；
- duplicate execution；
- multi-leg rejection；
- ENTRY_EXPIRED；
- USER_CANCELLED authentication；
- ORDER_REJECTED vs DATA_INVALID；
- zero-fill stream audit。

B1/B2/B3 还必须补：

- stable Market Event Store ID；
- DB/store replacement；
- source snapshot high-water race；
- duplicate/gap/out-of-order；
- request-time late backfill；
- restart before/after collection append；
- source cursor commit uncertainty；
- production DB unchanged；
- no broker write surface。

## 15. Evidence Tier 与声明边界

即使 B0–B3 全部完成：

```text
runtime path artifact != trusted admission
market event hash chain != external authority
paper outcome != live performance
manual input != independently verified outcome
```

所有结果仍只能进入 candidate/diagnostic lanes：

```text
verified = false
trusted_outcome_admission = false
auto_trade = false
real_scoreboard = insufficient_real_evidence
```

## 16. 当前实现清单

本轮复杂核心已实现：

```text
stock_tracker/runtime_evidence/path_contracts.py
tests/test_runtime_path_contracts.py
```

以及 A3 delivery-binding 补强：

```text
stock_tracker/storage/repository.py
stock_tracker/runtime_evidence/worker.py
stock_tracker/runtime_evidence/store.py
tests/test_runtime_evidence.py
```

这些修改当前尚未形成新的 scoped commit。Codex 必须在同一工作树中复核、补测试、更新验证记录并创建 A4/B0 checkpoint commit；不得覆盖或弱化本合同。
