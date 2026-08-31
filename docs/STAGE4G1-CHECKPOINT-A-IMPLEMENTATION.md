# Stage 4G.1 Checkpoint A Implementation Record

状态：`CODEX IMPLEMENTED / WORKBUDDY REVIEW PENDING`

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
- `RuntimeDecisionArtifact` 使用 strict UTF-8 canonical JSON、精确字段集、严格布尔/数值/枚举/时间/identity 校验。`artifact_id` 与 `runtime_episode_fact_id` 均由系统根据 canonical identity 生成且必须相等；新 occurrence 使用系统生成的 `transition_event_id`。
- Runtime migration 使用独立、checksum 绑定的 migration history。`scripts/runtime_migrate.py` 默认只执行内存 rehearsal；只有显式 `--apply --backup <new-path>` 才修改目标 Runtime DB，且 backup 使用 no-overwrite publication。
- Signal current state、Signal History 与 immutable Outbox payload 在同一个 `BEGIN IMMEDIATE` transaction 中提交。任一 Outbox schema/state 写入失败会回滚三者，再由隔离路径只提交正常 Signal/History，使交易建议页面继续服务并显式报告 Outcome lane 不可用。
- Outbox payload 与 delivery/lease/retry/cursor 分表。Trigger 禁止 payload 与 quarantine record 的 UPDATE/DELETE；运行时审计检查 table/index/trigger SQL/view/generated-column、append order、delivery relation、cursor 与 quarantine identity。
- 独立 Artifact Store 使用稳定 `store_id`、SQLite Catalog、content-addressed files、全局 append order/hash chain、full audit before extension、路径/文件 identity 检查和 atomic no-overwrite publication。精确 crash orphan 可被幂等接续，其他孤儿或篡改会失败关闭。
- Worker 使用 lease + at-least-once replay；Artifact 写入 durable Store 且通过完整 audit 后才把 delivery 标为 `DELIVERED` 并推进连续 cursor。重复消费只产生一个可观察 Artifact；poison payload 进入 append-only quarantine；Store/audit 故障进入 retry 且 cursor 不推进。
- Runtime 进程仅在显式 migration 已应用且 schema audit 通过时启动 Artifact Worker Service。Migration 缺失或 Artifact Store 不可用不会阻断 API、Scheduler 或信号页面。

## 3. 数据和安全声明

- 实现与测试只使用临时 SQLite、临时 Artifact Store 和 synthetic fixture。
- 未修改或迁移 `data/stock_tracker.db`。
- 未启用 XTP Trader、Order、Cancel、Algo、Account 或 Position API。
- 未实现 Broker 写入或自动交易，`auto_trade=false`。
- 未生成、展示或宣称真实胜率、收益率、Sharpe、最大回撤或 Strategy Scoreboard。

## 4. 后续 Checkpoint

- Checkpoint B 冻结 Evidence Vocabulary ADR，并实现只读 Market Event Store Path Worker。
- Checkpoint C 实现永久 `DIAGNOSTIC_ONLY` 的 deterministic Paper Adapter 与永久 `LIVE_CANDIDATE` 的 authenticated Manual input。
- Checkpoint D 实现完整 shadow/restart harness 与最终门禁。

在同一 WorkBuddy 会话给出 `WORKBUDDY_CHECKPOINT_PASS:A` 前，本实现不得进入 Checkpoint B；在最终 Review 通过且用户明确授权前不得 push。
