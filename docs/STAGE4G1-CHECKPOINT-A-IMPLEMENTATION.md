# Stage 4G.1 Checkpoint A Implementation Record

状态：`CHECKPOINT_A_REVIEW_PENDING / OPERATIONAL_SCALE_READINESS_NOT_PROVEN / TRUSTED_OUTCOME_ADMISSION_NOT_IMPLEMENTED / AUTO_TRADE_FALSE`

日期：2026-09-01

基线：`082a6dac310388ec10c8a432427a40e275bcd7ae`

分支：`agent/codex-stage4g1`

## 1. 本 Checkpoint 的边界

本 Checkpoint 只实现 Runtime Decision Artifact、事务 Outbox、独立 Artifact Store 与物化 Worker。它不打开 Stage 4G Collection Case，不实现 Path/Execution Evidence，不建立 Trusted Outcome Admission，不生成真实 Strategy Scoreboard，也不启用任何 Broker/XTP 写能力。

所有 Checkpoint A Artifact 固定为：

```text
execution_mode = OBSERVATIONAL_ONLY
outcome_case_status = OUTCOME_EVIDENCE_PENDING
auto_trade = false
trusted_outcome_admission = false
investment_performance_claim = false
```

执行规则、市场规则、费用、滑点、reference price 与 minimum exit session offset 在 B/C 前保持显式 `null`，并进入 canonical `incomplete_reasons`。Checkpoint A 也不把 `symbol` 冒充永久证券身份；`instrument_id` 保持 `UNRESOLVED_RUNTIME_IDENTITY:<identity_fact_id>`，并显式保留 `INSTRUMENT_IDENTITY_AUTHORITY_PENDING`。

## 2. 已实现合同

- `SignalManager` 使用可注入 UTC Clock；新正式 occurrence 时间必须 timezone-aware。Legacy naive `state_changed_at` 不会被静默附加时区，而是进入 quarantine 或 migration-unavailable 失败关闭路径。
- `RuntimeDecisionArtifact` 使用 strict UTF-8 canonical JSON、精确字段集、严格布尔/数值/枚举/时间/identity 校验。`decision_content_id` 绑定决策内容，`occurrence_dedup_id` 绑定冻结的 occurrence 因子；Repository 仅在首次成功插入时生成并持久化 opaque `transition_event_id`，Artifact builder 只消费该已持久化 ID。`artifact_id` 与 `runtime_episode_fact_id` 由最终 canonical identity 生成且必须相等。
- Runtime migration 使用独立、checksum 绑定的 migration history，并分别报告 actual target audit 与临时 copy rehearsal。状态严格区分 `UNINITIALIZED / VALID_CURRENT_VERSION / VALID_LATEST_VERSION / INVALID / UNKNOWN_VERSION / PARTIALLY_MIGRATED`。`scripts/runtime_migrate.py` 默认 dry-run；只有显式 `--apply --backup <new-path>` 才修改目标 Runtime DB，且 backup 使用 no-overwrite publication。
- Signal current state、Signal History、occurrence identity 与 immutable Outbox payload 在同一个 `BEGIN IMMEDIATE` transaction 中提交。只有 `RuntimeEvidenceUnavailableError`（schema/migration/outbox 尚未可用）允许降级为无 Evidence 的正常 Signal/History commit；Runtime DB integrity 或 Signal persistence 失败会失败关闭该 candidate，不写 MarketStore、不发布 signal event，并隔离后续 candidate。
- Outbox payload 与 delivery/lease/retry/cursor 分表。Trigger 禁止 payload 与 quarantine record 的 UPDATE/DELETE；运行时审计检查 table/index/trigger SQL/view/generated-column、append order、delivery relation、cursor 与 quarantine identity。
- 独立 Artifact Store 使用稳定 `store_id`、SQLite Catalog、content-addressed files、全局 append order/hash chain、full audit before extension、路径/文件 identity 检查和 atomic no-overwrite publication。精确 crash orphan 可被幂等接续，其他孤儿或篡改会失败关闭。
- Worker 使用 lease + at-least-once replay；Artifact 写入 durable Store 且通过完整 audit 后才把 delivery 标为 `DELIVERED` 并推进连续 cursor。重复消费只产生一个可观察 Artifact。`ROW_PERMANENT` 立即 quarantine 后继续；`ROW_TRANSIENT` 按版本化 capped exponential policy retry，耗尽后以 `TRANSIENT_RETRY_EXHAUSTED` quarantine 后继续；`STORE_INTEGRITY_BLOCK` 写入 append-only worker block，保留当前 delivery/cursor 并禁止消费后续行。Immutable metadata 只保存稳定 code/policy identity，不保存异常原文。
- Runtime 进程仅在显式 migration 已应用且 schema audit 通过时启动 Artifact Worker Service。Migration 缺失或 Artifact Store 不可用不会阻断 API、Scheduler 或信号页面。

## 3. 数据和安全声明

- 实现与测试只使用临时 SQLite、临时 Artifact Store 和 synthetic fixture。
- 未修改或迁移 `data/stock_tracker.db`。
- 未启用 XTP Trader、Order、Cancel、Algo、Account 或 Position API。
- 未实现 Broker 写入或自动交易，`auto_trade=false`。
- 未生成、展示或宣称真实胜率、收益率、Sharpe、最大回撤或 Strategy Scoreboard。
- 当前只证明 scoped contract correctness；`OPERATIONAL_SCALE_READINESS_NOT_PROVEN`。

## 4. 后续 Checkpoint

- Checkpoint B 冻结 Evidence Vocabulary ADR，并实现只读 Market Event Store Path Worker。
- Checkpoint C 实现永久 `DIAGNOSTIC_ONLY` 的 deterministic Paper Adapter 与永久 `LIVE_CANDIDATE` 的 authenticated Manual input。
- Checkpoint D 实现完整 shadow/restart harness 与最终门禁。

WorkBuddy 只提供机械检查证据，不签发工程 PASS。只有 ChatGPT 对精确 commit/tree/diff 完成最终 Review 并输出 `CHATGPT_CHECKPOINT_A_REVIEW_PASSED` 后，才允许进入 B1 存储接线；在最终 Review 通过且用户明确授权前不得 push。
