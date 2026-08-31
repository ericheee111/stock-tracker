# Stage 4G — Runtime Outcome Collection / Finalization Independent Review

状态：`ENGINEERING_READY_FOR_MERGE / TRUSTED_OUTCOME_ADMISSION_PENDING`

审查日期：2026-08-31

## 1. 结论

Stage 4G 的工程边界成立：它是 Stage 4F candidate ledger 之前的独立 append-only collection/finalization 层，不是 Broker execution engine、Trusted Admission Authority 或真实战绩系统。

允许的最终声明仅为：

```text
RUNTIME_OUTCOME_COLLECTION_ENGINEERING = COMPLETE
APPEND_ONLY_COLLECTION_INTEGRITY = IMPLEMENTED
PAPER_OUTCOME_FINALIZATION = IMPLEMENTED
LIVE_MANUAL_CANDIDATE_FINALIZATION = IMPLEMENTED
TRUSTED_OUTCOME_ADMISSION = NOT_IMPLEMENTED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
INVESTMENT_PERFORMANCE_CLAIM = FALSE
AUTO_TRADE = FALSE
```

## 2. 对抗审查 Findings

### G1 — CRITICAL：直接用 runtime `signal_id` 作为 Outcome 唯一键会永久阻断后续合法 episode

原风险：Runtime Signal 的 `signal_id` 可以跨刷新/策略版本持续存在，而 Stage 4F 对 Outcome `signal_id` 有全局唯一约束。若 Stage 4G 直接复用 runtime `signal_id`，第一个终态 Outcome 会令同一 runtime signal 后续策略版本或触发 episode 无法记录。

修复：新增 `runtime_episode_id`，从触发时 immutable runtime/strategy/data/policy/plan/scores 事实派生；`case_id` 使用 episode + mode；写入 Stage 4F 时使用 namespaced episode Outcome signal identity。相同 episode 重试幂等，identity drift 形成新 case。

状态：`FIXED`。

### G2 — CRITICAL：FINALIZED 后再次 finalize 会尝试追加第二个 FINALIZED event

原风险：两阶段恢复路径覆盖了“Ledger 已提交、marker 未写”的重试，但 case 已成功 FINALIZED 后再次调用 `finalize()` 会构造同一 payload 并尝试追加事件，违反终态不可追加状态机。

修复：`finalize()` 先读取 case。若已经 FINALIZED，则验证 ledger target、prepared Outcome、record hash 和 append order，调用 Stage 4F 幂等 append 核对 immutable record 后直接返回 `IDEMPOTENT`，不追加第二个 collection event。

状态：`FIXED`。

### G3 — IMPORTANT：FINALIZED projection 丢弃 record append order / ledger disposition

风险：若 case projection 不保留 FINALIZED marker 中的 append order/disposition，终态重试无法完整核对 collection marker 与 Stage 4F immutable record。

修复：`OutcomeCollectionCase` projection 保留 `finalized_record_append_order` 与 `finalized_ledger_disposition`，并进入 case audit representation。

状态：`FIXED`。

### G4 — IMPORTANT：collector capture timestamp 不应决定 episode 是否相同

风险：若 episode identity 包含 `_utc_now()` 采样的 `captured_at`，同一事实重试会因为采集时间不同生成新 case，破坏幂等。

修复：`runtime_episode_id` 明确排除 collector `captured_at`；`decision_snapshot_id` 才绑定首次 capture time。重试发现相同 episode 后复用原 capture time。

状态：`FIXED`。

### G5 — IMPORTANT：Live Manual 不能因为存在 evidence SHA 就升级为可信真实战绩

风险：evidence ID 只证明引用格式存在，不证明独立权威、签名、PIT、许可或 Broker authenticity。

处置：Live Manual 生成 `LIVE_OBSERVED` candidate，但 `verified=false`，最多进入 Stage 4F `LIVE_CANDIDATE`；Trusted Admission 仍未实现。

状态：`FAIL_CLOSED BY DESIGN`。

## 3. 关键不变量

- collection DB 与 production DB、Stage 4F ledger catalog/record root 隔离；
- collection event 全局 append order/hash chain 可重放；
- 所有 lifecycle transition 由 event replay 验证；
- Paper 不进入真实战绩；
- Live Manual 需要 evidence IDs 且只形成 candidate；
- no-entry 不伪造 fills/path/metrics；
- complete Outcome 必须有 entry、observable path、exit request、exit fill；
- FINALIZATION_PREPARED 在 Ledger 失败后可恢复；
- Ledger 已提交而 marker 失败时可幂等恢复；
- FINALIZED 再调用 finalize 只核对，不再追加事件；
- 同一 runtime signal 可以产生多个合法 episode；
- 不修改生产 SQLite、不下单、不自动晋级模型或策略。

## 4. 最终门禁

在以下项目全部通过前不得合并：

```text
Stage 4G focused tests
Stage 4F ledger regressions
full Quant
full Runtime
Ruff / compileall / pip check
Quant smoke / synthetic benchmark
production migration dry-run + DB hash unchanged
Today / Portfolio regression
exact scoped Git Index review
secret/generated/binary scan
cached diff check
```

最终测试数字、exact index tree、commit/push SHA 在完成后写入 `CHATGPT_HANDOFF.md`。

## 5. 后续

下一阶段是独立 Trusted Outcome Admission Authority。不得把 Stage 4G 的 `LIVE_MANUAL`、evidence IDs、collector hash chain 或 Stage 4F `LIVE_CANDIDATE` 直接解释为可信真实战绩。
