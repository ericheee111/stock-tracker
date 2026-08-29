# Stage 4F — Append-Only Outcome Evidence Ledger

状态：`ENGINEERING_COMPLETE / INDEPENDENT_REVIEW_PASSED / TRUSTED_OUTCOME_ADMISSION_PENDING`

日期：2026-08-30

## 1. 目的

Stage 4A 已有不可变 `SignalOutcome` 与 Strategy Scoreboard 计算合同，Stage 4C 已有失败归因和同 cohort 版本比较，但仓库仍缺少一个能够长期、追加式保存终态 Outcome 的独立证据库。没有这个层，真实样本无法安全积累，Scoreboard 只能依赖调用方临时构造对象。

Stage 4F 建立：

```text
terminal SignalOutcome
→ strict canonical JSON codec
→ append-only immutable record
→ SQLite catalog + global hash chain
→ integrity audit
→ exact-cohort Strategy Scoreboard materialization
```

本阶段只交付工程合同和 synthetic/paper/contract-fixture 验收，不声称已经积累真实投资表现。

## 2. 冻结边界

### 2.1 包含

- 终态 `SignalOutcome` 的严格 JSON 编解码；
- `TradeIntentEvidence`、`OutcomeFillEvidence`、`OutcomePathPoint`、`OutcomeMetrics` 和所有派生 ID 的重新计算与核验；
- 独立 `OutcomeLedger`，使用不可变 JSON Record 与独立 SQLite Catalog；
- 全局 Append Order、Previous Record Hash 与 Record Hash 链；
- 同一 `outcome_id` 幂等重试；
- 同一 `signal_id` 不允许出现两个不同 Outcome；
- 两条物理证据 Lane：`DIAGNOSTIC_ONLY` 与 `LIVE_CANDIDATE`；合同层 eligible 只作为待审计标记，不是可信准入 Lane；
- Ledger 完整性审计，包括文件、Catalog、Hash、顺序、摄取时间单调性和孤儿文件；
- 从 Ledger 按精确 cohort/window 生成失败关闭的候选集合与 `StrategyScoreboard` Snapshot；
- 导入 CLI 与 Scoreboard/Audit 报告 CLI；
- 内容寻址、不可覆盖的 JSON/Markdown 报告；
- 临时目录、独立数据库和故障注入测试。

### 2.2 不包含

- 自动从 Runtime Signal、Broker、Portfolio 或 XTP 生成 Outcome；
- 保存未完成的 `OPEN` Outcome；
- 更新、覆盖或删除已保存 Outcome；
- 自动将 unverified/Paper/Synthetic 样本提升为真实样本；
- 自动修改 Strategy Scoreboard、Model Registry、策略权重或生产信号；
- 真实胜率、收益、Sharpe、最大回撤或投资表现声明；
- UI 修改；
- 订单、报单、撤单或自动交易；
- 修改 `data/stock_tracker.db`。

## 3. 终态合同

Ledger 只接受：

```text
OutcomeState.COMPLETE
OutcomeState.NO_ENTRY + terminal_reason in {ORDER_REJECTED, DATA_INVALID}
```

拒绝：

```text
OutcomeState.OPEN
非终态 NO_ENTRY
缺失或不一致的 Intent / Fill / Path / Terminal Reason
```

原因：Stage 4F 是事实证据账本，不是可变工作流状态库。未完成 Outcome 应由未来独立采集服务持有，完成后一次性形成不可变 `SignalOutcome`。

## 4. Evidence Lane

### `DIAGNOSTIC_ONLY`

适用：

- `SYNTHETIC_FIXTURE`；
- `PAPER_RECORDED`；
- 任何明确 synthetic 的 Outcome。

永远不能进入真实 Scoreboard。

### `LIVE_CANDIDATE`

适用：

- 所有 `LIVE_OBSERVED` 终态 Outcome；
- 即使 `SignalOutcome.real_scoreboard_eligible=true`，Stage 4F 也只把它视为“合同层候选”，不能把调用方自报的 `verified=true` 或 SHA 引用当作独立可信准入证明。

它可以作为待审计样本，但不能直接进入真实聚合指标。

### Trusted admission 边界

Stage 4F **没有** `REAL_SCOREBOARD_ELIGIBLE` Ledger Lane，也没有配置可信 Outcome Admission Authority。现有 `SignalOutcome.real_scoreboard_eligible` 只表示 Outcome 自身合同满足：

```text
COMPLETE
LIVE_OBSERVED
verified = true
T2/T3/T4 evidence tier
not synthetic
metrics present
```

这些条件不足以证明验证声明来自独立权威。因此当前物化流程固定输出 `TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED`，真实 Scoreboard 的 admitted record 集合保持为空。未来若增加可信准入，必须使用独立、追加式 authority/admission ledger，不能回写或变异本 Stage 4F immutable record。

## 5. Strict JSON Codec

所有 JSON：

- UTF-8；
- 最大 16 MiB；
- 拒绝 BOM；
- 拒绝重复 Key；
- 拒绝 `NaN` / `Infinity`；
- 拒绝未知或缺失字段；
- 深度嵌套导致的解析递归异常必须转换为 Ledger 合同错误并失败关闭；
- Decimal 使用规范化十进制字符串；
- Datetime 统一 UTC `Z`；
- Enum 使用稳定值；
- 布尔值必须 `type(value) is bool`；
- Integer 不接受 bool；
- 反序列化后重新构造全部 dataclass，并核验所有派生 ID、metrics、state、blockers 和 `outcome_id`；
- 输入文档必须与重建后的规范化文档完全一致。

## 6. Ledger Record

每条 Record 包含：

```text
schema
append_order
recorded_by
ingested_at
previous_record_hash
lane
outcome
record_hash
```

`record_hash` 对除自身外的完整 Record Identity 做 SHA-256。

Record 文件使用内容寻址路径，并在写入后绑定：

```text
record_file
record_file_sha256
record_hash
outcome_id
signal_id
append_order
```

## 7. Catalog 与事务

独立 Catalog 默认：

```text
data/outcome-ledger.db
```

不可变 Record Root 默认：

```text
data/outcome-ledger-records/
```

它们必须与生产数据库分离。

Catalog 首次创建先在同目录临时数据库中完成 schema、meta、事务提交和文件刷新，再用不覆盖的原子 hard-link 发布；多个进程同时首次打开时只能有一个完整 Catalog 胜出，其余进程验证胜出的 Catalog，绝不删除其他进程已发布的目标。

Append 流程：

```text
BEGIN IMMEDIATE
→ 在取得全局 SQLite writer lock 后采样 ingested_at
→ 对现有 Catalog、Record Inventory、Canonical Bytes、Hash Chain 与摄取时间执行完整预追加审计
→ 检查 outcome_id 幂等与 signal_id 唯一
→ 从已审计尾部派生 append_order 与 previous hash
→ fsync 临时 Record
→ 用不覆盖的原子 hard-link 发布不可变 Record
→ 插入 Catalog
→ COMMIT
```

`ingested_at` 必须随 Append Order 非递减；系统时钟回拨时拒绝新增记录，从而保证任意历史 `as_of` 可见集合始终是全局链的前缀。

若 Catalog 插入失败且能够证明事务未提交，删除本次新建 Record 作为补偿。若 SQLite 实际已持久提交但 `commit()` 返回异常，必须从新的 Catalog 连接回读并验证该 Record；验证成功则恢复为 `APPENDED`，无法确定时保留不可变证据并失败关闭，禁止误删可能已提交的文件。

## 8. Integrity Audit

Audit 必须验证：

- Catalog schema；
- Append Order 从 1 连续递增；
- `ingested_at` 按 Append Order 非递减，且不晚于本次审计时间；
- `previous_record_hash` 链连续；
- Record 文件存在且不是 link；
- 文件 SHA 与 Catalog 一致；
- Strict JSON 和 Record Hash；
- Outcome 全量重建和派生身份；
- Catalog 元数据与 Record 内容一致；
- Record Root 中不存在未登记 JSON；
- Catalog 不引用 Root 之外的路径。

任何不一致均失败关闭，不生成 Scoreboard。Audit 自身也使用 `BEGIN IMMEDIATE`，与 Append 在 SQLite 层串行化，防止审计遍历文件期间另一进程先发布 Record、后提交 Catalog 而产生虚假的孤儿文件判断。

## 9. Scoreboard Materialization

查询必须绑定：

```text
strategy_id
strategy_version
market
horizon_sessions
model_id
evidence_tier
window_start
window_end
as_of
scoreboard_policy_id
ledger_audit_id
selected record/outcome IDs
```

物化继续复用现有 `StrategyScoreboard`，但 Stage 4F 只计算并记录 exact-cohort candidate 集合与 `SignalOutcome.real_scoreboard_eligible` 的合同层计数。由于可信 Outcome Admission Authority 尚未配置，`scoreboard_records` 固定为空，真实指标不会从本 Ledger 直接生成，状态保持：

```text
INSUFFICIENT_REAL_EVIDENCE
TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED
```

报告固定：

```text
investment_performance_claim = false
production_database_modified = false
auto_promote_model = false
auto_change_strategy_weight = false
auto_trade = false
```

## 10. CLI

### 导入终态 Outcome

```powershell
python scripts/ingest_outcome_ledger.py `
  --input data/outcome-imports/outcome.json `
  --record-root data/outcome-ledger-records `
  --catalog data/outcome-ledger.db `
  --recorded-by manual-reviewed-import
```

### 审计并生成 Scoreboard Snapshot

```powershell
python scripts/report_outcome_ledger.py `
  --record-root data/outcome-ledger-records `
  --catalog data/outcome-ledger.db `
  --strategy-id S1_BREAKOUT `
  --strategy-version v1 `
  --market A `
  --horizon-sessions 20 `
  --evidence-tier OPERATIONAL_VERIFIED `
  --window-start 2026-01-01T00:00:00Z `
  --window-end 2026-12-31T23:59:59Z `
  --as-of 2027-01-01T00:00:00Z `
  --output-dir data/outcome-reports
```

所有 `data/` 产物均不进入 Git。

## 11. 安全与并行 UI 边界

- 拒绝 Root/Catalog/Input/Output 的 symlink/junction 祖先；
- Record 与报告发布使用同目录临时文件加不覆盖的 hard-link；并发写入同一路径时只能接受完全相同字节，内容不同必须阻断；
- Catalog 与 Record Root 不得重叠；
- 任一目标不得为生产 `data/stock_tracker.db`；
- 不接受网络 URL；
- 不读取环境中的 XTP、HiThink、私有 API 或券商凭据；
- 不记录账户净值、当前持仓或 Browser Token；
- 不修改 `web/**` 或 `qa/shots/**`；
- WorkBuddy UI 只做只读回归和 Review，单独提交。

## 12. 合并门禁

```text
Stage 4F focused tests
Outcome core regression
Runtime full tests
Quant full tests
Source distribution / no tracked bytecode
Targeted Ruff
compileall
pip check
Quant contract smoke
Synthetic fixture benchmark
Temporary-ledger CLI integration
Tamper / duplicate / orphan / chain-break tests
Read-only production backup migration dry-run
UI read-only regression
Exact Git Index export
Index-tree focused/runtime/quant/hybrid/product regression
Generated/secret scan
git diff --cached --check
Independent financial-correctness review
```

## 13. 验收状态

工程完成时允许声明：

```text
OUTCOME_LEDGER_ENGINEERING = COMPLETE
APPEND_ONLY_INTEGRITY = PASSED
SCOREBOARD_MATERIALIZATION = PASSED
REAL_SAMPLE_COLLECTION_SERVICE = NOT_IMPLEMENTED
REAL_OUTCOME_SAMPLE_COUNT = 0_OR_EXISTING_LOCAL_ONLY
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
INVESTMENT_PERFORMANCE_CLAIM = FALSE
```
