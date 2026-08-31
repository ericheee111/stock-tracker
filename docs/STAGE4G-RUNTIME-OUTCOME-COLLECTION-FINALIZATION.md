# Stage 4G — Runtime Outcome Collection / Finalization

状态：`ENGINEERING_COMPLETE / INDEPENDENT_REVIEW_PASSED / GIT_DELIVERY_PENDING`

日期：2026-08-31

## 1. 目标

Stage 4F 已提供 append-only terminal `SignalOutcome` candidate ledger，但不负责从运行态 Signal 生命周期采集 entry、path、exit 和 no-entry 事实。Stage 4G 增加一个独立、默认不接生产决策的 Outcome Collection/Finalization 层：

```text
Runtime Signal actionable episode
→ immutable decision snapshot
→ append-only collection event chain
→ terminal-ready case
→ deterministic SignalOutcome
→ Stage 4F OutcomeLedger
```

本阶段只建立可审计采集和终态生成合同，不建立 Trusted Outcome Admission Authority，不产生真实 Strategy Scoreboard，也不自动下单。

## 2. 身份模型

运行态 `signal_id` 不是交易 episode 的永久唯一键。相同 runtime signal 可以在不同策略版本、数据/身份/政策快照、状态变更时形成新的 actionable episode。

Stage 4G 因此冻结两层身份：

- `runtime_episode_id`：由 runtime signal、symbol/market、strategy/version、horizon/model、instrument/identity/data/policy/classification、runtime state/time/data status、entry plan、scores 等触发时事实派生，不包含采集系统自己的 `captured_at`；
- `decision_snapshot_id`：在 episode identity 上再绑定 collector `captured_at`，用于不可变 entry intent；
- `case_id`：`runtime_episode_id + collection mode`；
- Outcome Ledger 使用 namespaced episode signal identity，而不是直接复用可重复的 runtime `signal_id`，防止一个历史 runtime signal 永久阻断后续合法 episode。

相同 episode 的重试保持幂等；真正的 episode identity drift 会创建新 case，而不是篡改旧 case。

## 3. Collection Mode

### `PAPER`

- 只生成 `PAPER_RECORDED`；
- Evidence Tier 固定 `BEST_EFFORT`；
- `verified=false`；
- 进入 Stage 4F `DIAGNOSTIC_ONLY`；
- 永远不能进入真实 Scoreboard。

### `LIVE_MANUAL`

- 只允许运行态 `DataStatus.LIVE` 的 entry；
- entry/path/exit/no-entry 事实必须携带 evidence IDs；
- 生成 `LIVE_OBSERVED` candidate，但本阶段不设置独立可信验证；
- 最多进入 Stage 4F `LIVE_CANDIDATE`；
- Trusted Admission 仍由未来独立 Authority 决定。

## 4. Append-only 状态机

事件链：

```text
CASE_OPENED
→ ENTRY_FILLED | NO_ENTRY
→ PATH_POINT*
→ EXIT_REQUESTED
→ EXIT_FILLED
→ FINALIZATION_PREPARED
→ FINALIZED
```

主要状态：

```text
AWAITING_ENTRY
OPEN_POSITION
EXIT_REQUESTED
TERMINAL_READY
FINALIZATION_PREPARED
FINALIZED
```

禁止：

- FINALIZED 后继续追加事实；
- entry/no-entry 双重事实；
- 没有 entry 就记录 path/exit；
- path 时间倒退；
- exit fill 超过或不等于已填数量；
- LIVE_MANUAL 缺少 evidence IDs；
- 调用方直接构造 FINALIZED 状态。

## 5. 时间与事实边界

- 所有采集时间必须 timezone-aware，并规范为 UTC；
- runtime `state_changed_at` 的原始 aware/naive 语义被显式冻结，不能伪装成权威 PIT 时间；
- entry fill 不得早于 decision capture，也不得晚于采集观察时间；
- path 不得早于 entry，不得在 append 序列中倒退；
- exit request 不得早于 entry；
- exit fill 不得早于 exit request；
- terminal Outcome 的 `recorded_at` 等于 FINALIZATION_PREPARED 的 collector observation time；
- collection event `observed_at` 按全局 append order 单调不下降；时钟回拨失败关闭。

Stage 4G 不把 runtime 状态时间自动升级为 T3/PIT research evidence。

## 6. 独立存储与完整性

默认 collection database：

```text
data/outcome-collection.db
```

它必须与：

```text
data/stock_tracker.db
data/outcome-ledger.db
data/outcome-ledger-records/
```

隔离。

Collection DB 使用：

- 独立 SQLite schema identity；
- `BEGIN IMMEDIATE` writer serialization；
- 全局 contiguous append order；
- `previous_event_hash` / `event_hash` 链；
- canonical JSON payload；
- `fact_id` 幂等；
- schema、hash、payload SHA、时间和 lifecycle 全量重放审计；
- production DB exact path / hardlink alias 拒绝。

## 7. 两阶段 Finalization

Finalization 不直接从 mutable case 一步写 Ledger，而是：

```text
TERMINAL_READY
→ FINALIZATION_PREPARED
   - deterministic immutable SignalOutcome
   - bind ledger_target_id
→ Stage 4F ledger.append(outcome)
→ ledger.audit()
→ FINALIZED marker
```

这样即使 Ledger 暂时失败，prepared Outcome 仍可恢复；即使 Ledger 已提交但 collection marker 写失败，重试会命中 Stage 4F 的 outcome idempotency。

FINALIZED case 再次调用 `finalize()` 必须是终态幂等：重新核对目标 Ledger 中的 immutable Outcome record hash / append order，但不能追加第二个 FINALIZED event。

## 8. 终态 Outcome 语义

### Complete

需要：

- entry fill；
- 至少一个 observable path point；
- exit request；
- exit fill；
- 合法 terminal reason。

Metrics 由 Stage 4A `SignalOutcome` 合同统一计算，包括 realized R、MFE R、MAE R、holding sessions、cost。

### No Entry

只允许合同定义的 terminal no-entry reason；不得伪造 path、fill 或 metrics。

## 9. 安全与产品边界

固定为：

```text
trusted_outcome_admission = false
investment_performance_claim = false
auto_promote_model = false
auto_change_strategy_weight = false
auto_trade = false
production_database_modified = false
```

Stage 4G 不：

- 调用 Broker/XTP Trader/Algo Order；
- 自动成交；
- 根据行情自行推断真实成交；
- 修改生产 Signal；
- 写生产 SQLite；
- 自动进入真实 Scoreboard；
- 修改 UI。

## 10. 工程验收

最低门禁：

```text
Stage 4G focused lifecycle/recovery/integrity tests
Stage 4F OutcomeLedger regression
full Quant suite
full Runtime suite
targeted Ruff
compileall
pip check
Quant contract smoke
synthetic fixture benchmark
production migration dry-run with unchanged DB hash
Today/Portfolio read-only product regression
exact scoped Git Index review
generated/binary/secret scan
git diff --cached --check
independent financial-correctness review
```

## 11. 后续

Stage 4G 通过后，下一独立阶段是 Trusted Outcome Admission Authority。Authority 必须独立于 collection 与 candidate ledger，具备身份、权限、签名/撤销、PIT 生效时间和审计治理。在 Authority 和足够独立真实样本完成前，Strategy Scoreboard 继续固定为 `INSUFFICIENT_REAL_EVIDENCE`。
