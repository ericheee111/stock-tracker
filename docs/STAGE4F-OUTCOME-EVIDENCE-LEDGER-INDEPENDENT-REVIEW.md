# Stage 4F — Outcome Evidence Ledger Independent Review

状态：`ENGINEERING_READY_FOR_MERGE / TRUSTED_OUTCOME_ADMISSION_NOT_IMPLEMENTED`

审查日期：2026-08-30

## 1. 审查结论

Stage 4F 可以作为**独立、追加式、失败关闭的终态 Outcome 候选证据账本**合并。它能够保存并审计 `SignalOutcome` 的规范化不可变记录，区分 synthetic/paper 诊断样本与 live candidate，并生成绑定 exact cohort、窗口、`as_of`、Ledger Audit 和候选 Record/Outcome 身份的 Snapshot。

本结论**不**代表真实 Strategy Scoreboard 已上线，也不代表已有真实投资表现。仓库没有 Trusted Outcome Admission Authority；调用方提供的 `verified=true`、`verification_evidence_ids` 或较高 `evidence_tier` 不能自行把样本提升为可信真实战绩。当前 admitted set 固定为空，指标必须保持 `INSUFFICIENT_REAL_EVIDENCE`。

```text
OUTCOME_LEDGER_ENGINEERING = COMPLETE
APPEND_ONLY_INTEGRITY = PASSED
INDEPENDENT_REVIEW = PASSED
TRUSTED_OUTCOME_ADMISSION = NOT_IMPLEMENTED
AUTOMATIC_REAL_OUTCOME_COLLECTION = NOT_IMPLEMENTED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
INVESTMENT_PERFORMANCE_CLAIM = FALSE
AUTO_PROMOTE_MODEL = FALSE
AUTO_CHANGE_STRATEGY_WEIGHT = FALSE
AUTO_TRADE = FALSE
```

## 2. 审查范围

核心实现：

```text
stock_tracker/quant/storage/outcome_ledger.py
stock_tracker/quant/storage/__init__.py
scripts/ingest_outcome_ledger.py
scripts/report_outcome_ledger.py
```

测试与治理：

```text
tests_quant/test_outcome_ledger_codec.py
tests_quant/test_outcome_ledger_store.py
tests_quant/test_outcome_ledger_scoreboard.py
tests_quant/test_outcome_ledger_cli.py
tests_quant/test_source_distribution.py
docs/STAGE4F-OUTCOME-EVIDENCE-LEDGER-DESIGN.md
```

并行 `web/**`、`qa/**` 和截图改动不属于本审查交付范围，也不得进入 Stage 4F 提交。

## 3. 对抗审查 Findings 与修复

### F1 — CRITICAL：调用方自报验证可伪装成真实 Scoreboard 准入

风险：`SignalOutcome.real_scoreboard_eligible` 由对象内部字段派生，但 `verified=true` 和证据 SHA 仍来自调用方。若把该布尔值直接映射成 `REAL_SCOREBOARD_ELIGIBLE` Lane，导入者可自行制造“真实战绩”。

修复：Ledger 只保留 `DIAGNOSTIC_ONLY` 与 `LIVE_CANDIDATE` 两条物理 Lane。合同层 eligible 仅形成候选 ID/计数；`scoreboard_records` 固定为空，并输出 `TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED`。未来准入必须另建独立、追加式、可撤销的 Authority/Admission Ledger，禁止回写 Stage 4F Record。

### F2 — CRITICAL：`ingested_at` 在取得写锁前采样会破坏历史 `as_of` 前缀

风险：两个进程并发 Append 或系统时钟回拨时，较晚取得 Append Order 的记录可能拥有更早 `ingested_at`。按 `ingested_at <= as_of` 查询会在链中间出现不可见洞，破坏 Point-in-Time 可解释性。

修复：先执行 `BEGIN IMMEDIATE`，取得 SQLite 全局 writer lock 后再采样 `ingested_at`；每次新增前完整审计现有 Ledger，并要求摄取时间随 Append Order 非递减。时钟回拨时拒绝 Append。由此任意历史 `as_of` 的可见记录始终是全局 Hash Chain 的前缀。

### F3 — CRITICAL：`os.replace()` 可覆盖并发创建的不可变文件

风险：检查目标不存在后再调用 `os.replace()` 存在 TOCTOU 窗口；另一进程在检查与替换之间创建目标时会被静默覆盖，违反不可变证据语义。

修复：Record 与 JSON/Markdown 报告先写同目录临时文件并 `fsync`，再用不覆盖的原子 hard-link 发布。目标已存在时只接受完全相同的规范字节；不同内容、link 或非普通文件均失败关闭。并发写同一路径的不同 Snapshot 只能有一个成功，另一个被阻断。

### F4 — CRITICAL：并发首次建库可能暴露半初始化 Catalog 或误删胜出者

风险：直接在最终路径初始化 SQLite 时，另一个进程可能看到只有 Header、尚无完整 schema 的数据库；失败清理逻辑还可能删除由其他进程创建的合法 Catalog。

修复：每个进程在独立临时 SQLite 文件中完成 schema、meta、提交、校验和文件刷新，再用不覆盖 hard-link 发布。并发创建只有一个完整 Catalog 胜出，其他进程只验证胜出者；清理仅针对各自临时文件及其 sidecar。

### F5 — IMPORTANT：Audit 与 Append 跨文件/Catalog 快照不一致

风险：普通读事务不会阻止另一进程先发布 Record 文件、后提交 Catalog。Audit 若正好遍历 Record Root，会把尚未提交 Catalog 的合法文件误判为孤儿。

修复：Audit 也使用 `BEGIN IMMEDIATE`，与 Append 在 SQLite 层串行化。Catalog rows 与文件 inventory 来自同一稳定窗口。并发回归证明 Audit 结束前另一实例不能发布 Record。

### F6 — CRITICAL：提交结果不确定时补偿删除可能造成数据丢失

风险：SQLite 事务可能已经持久提交，但 `commit()` 因 I/O/连接异常仍抛错。若此时无条件删除 Record 文件，Catalog 会引用不存在的证据。

修复：捕获提交异常后，通过新的 Catalog 连接按 `outcome_id` 回读并完整验证 Record。若已提交，恢复为 `APPENDED`；若能够证明未提交，才删除本次新建文件；若无法确定，保留不可变证据并失败关闭。

### F7 — IMPORTANT：现有 Ledger 被篡改或存在孤儿时仍可继续追加

风险：只读取尾部 row 分配下一条，会把新记录链接到一个已经损坏的账本，扩大污染范围。

修复：每次 Append 在写入前执行完整 Ledger Audit，覆盖 schema、连续顺序、全链、规范字节、文件 SHA、Catalog 元数据、link/path、inventory 和摄取时间。任何既有损坏都会阻断新写入。

### F8 — IMPORTANT：深层 JSON 可泄漏 `RecursionError`

风险：恶意深层 JSON 可能绕过统一 Ledger 错误边界，令 CLI 返回非合同异常。

修复：严格 JSON 解析将 `RecursionError` 转换为 `OutcomeLedgerError`；回归覆盖深度嵌套输入。

### F9 — MINOR：重复路径守卫实现增加漂移风险

风险：同一模块中重复定义 `_is_link`、`_checked_path`、`_safe_child`，后续只修一份会造成安全逻辑不一致。

修复：收敛为单一实现，并保留 Catalog/Root/Input/Output 的 symlink/junction、祖先、重叠和生产数据库隔离检查。

### F10 — IMPORTANT：干净源码分发包缺少运行数据库时路径守卫泄漏非合同异常

风险：Git Index 导出包按设计不包含 `data/stock_tracker.db`。旧 CLI 路径校验在判断“输入/Root/Catalog 是否正指向生产数据库”之前先做 `resolve(strict=True)`，因此恶意或误配置的生产路径在数据库尚不存在时会泄漏 `FileNotFoundError`，而不是稳定的 `OutcomeLedgerError`；同一源码在开发机与干净分发包上的失败语义不一致。

修复：Ingest 与 Report CLI 先对规范化的非严格路径执行生产数据库精确路径拒绝，再解析必须存在的普通文件/目录；缺失输入、Root 或 Catalog 统一转换为 `OutcomeLedgerError`，已存在 hardlink alias 继续通过 `samefile` 失败关闭。新增“不存在的生产数据库路径”回归，并以干净 Git Index 导出重新执行 CLI/全量测试。

## 4. 不变量复核

### 4.1 数据真实性

- synthetic 与 paper 永久为 `DIAGNOSTIC_ONLY`；
- live 记录最多为 `LIVE_CANDIDATE`；
- 高 Trust Tier、`verified=true` 和 SHA 引用不能自我证明独立真实性；
- Snapshot 明确输出候选 Record Hash、候选 Outcome ID、合同层 eligible Outcome ID 和可信准入 blocker；
- 真实 Scoreboard outcomes、eligible IDs、metrics 与 bucket metrics 均保持空或 `None`。

### 4.2 Point-in-Time

- Outcome 自身要求 `recorded_at` 不晚于 Ledger `ingested_at`；
- Ledger `ingested_at` 由本进程在写锁内观测，CLI 不接受调用方指定；
- Audit 时间不接受调用方回填；
- Snapshot 仅选择 `recorded_at` 和 `ingested_at` 均不晚于 `as_of` 的 exact cohort；
- 摄取时间单调性保证该集合是全局 Append Chain 前缀。

### 4.3 不可变与隔离

- 每条 Record 绑定完整 Outcome、Lane、Append Order、Previous Hash 和 Record Hash；
- Catalog 绑定文件相对路径、文件 SHA、Outcome/Signal/策略/市场/窗口元数据；
- Record Root 不允许未登记文件、缺失文件、linked file/directory 或越界路径；
- Ledger Catalog 与 Record Root 均不得复用或别名到生产 `data/stock_tracker.db`；
- CLI 与测试只使用独立/临时数据库；
- 本阶段没有 Broker、Order、Trader、Algo 或自动交易入口。

## 5. 当前测试证据

独立审查新增并通过的对抗场景包括：

- 并发首次初始化只能发布一个完整 Catalog；
- 多个独立 Ledger 实例并发 Append 仍形成连续全局链；
- Audit 与 Append 串行，文件与 Catalog 快照一致；
- 系统时钟回拨阻断新增记录；
- 孤儿文件或既有完整性损坏阻断新增记录；
- SQLite 已提交但 `commit()` 抛错时安全恢复；
- 两个不同 Snapshot 并发写同一路径时不可覆盖；
- 深层 JSON 失败关闭；
- caller-selected ingestion/generation timestamp 不存在；
- production DB、symlink/junction、hardlink alias、非规范字节、Hash Chain、duplicate signal/outcome、orphan/missing file 均有负向覆盖。

Stage 4F 聚焦测试：`36/36 + 11 subtests` 通过。全量 Quant：`664/664 + 316 subtests`；全量 Runtime：`521 passed, 1 skipped + 350 subtests`；source distribution：`2/2` 并覆盖 `83` 个关键路径 subtests；Today Mock、真实 Today API/Web、Portfolio CRUD 分别 `17/17`、`17/17`、`13/13`。Targeted Ruff、compileall、pip check、Quant contract smoke、synthetic fixture benchmark 和 production migration dry-run 均通过；Smoke/Benchmark 保持 `synthetic_fixture_only=true`、`investment_performance_claim=false`，Challenger 未自动晋级。修复 F10 后的干净 Git Index 导出通过 Stage 4F `36 + 11 subtests`、Quant `662 passed, 2 expected skips + 233 subtests`、Runtime `521 passed, 1 skipped + 350 subtests`、targeted Ruff、compileall、Quant smoke 与 synthetic benchmark；两个 Quant skip 仅因导出包按设计没有 `.git`。生产 `data/stock_tracker.db` 在本轮门禁前后 SHA-256 均为 `6d2f1fdc5b48180c1cb32d15e8619770dc1b5edb56d13cea0072bffe964f20f2`，迁移输出为 `DRY_RUN / database_modified=false`。最终 Index tree、secret/generated scan、commit 与 push 证据在交付交接记录中补充。

## 6. 残余限制与后续阶段

1. Stage 4F 是证据保存层，不会自动从 Runtime Signal、行情路径、Portfolio 或 Broker 生成 `SignalOutcome`；真实采集服务尚未实现。
2. 没有 Trusted Outcome Admission Authority，任何候选都不能进入真实聚合指标。
3. 没有真实独立样本门槛、真实 Strategy Scoreboard API/UI 或投资表现声明。
4. 每次 Append 执行全量 Integrity Audit，优先保证证据正确性；样本规模显著增大后需要在不降低不变量的前提下设计分段 checkpoint/manifest，而不能直接跳过全量验证。
5. 不覆盖 hard-link 发布要求本地文件系统支持同卷 hard link；不支持时必须失败关闭，不能退回可覆盖写入。
6. Snapshot 是内容寻址的审计产物，不是外部数字签名。未来 Authority 必须具备独立身份、签名/撤销、权限和审计治理。
7. 正式 PIT Replay、T3 Snapshot、真实新样本 Shadow 和模型晋级仍受现有 License/Trust/T3 blocker 约束。

推荐下一阶段：先实现**独立 Outcome Collection/Finalization Service**，明确 Runtime Signal Identity、可执行价格、成交/未成交、路径完整性和终态生成；随后另立 Trusted Outcome Admission Authority。二者完成且积累足够独立真实样本前，Strategy Scoreboard 必须继续显示 `INSUFFICIENT_REAL_EVIDENCE`。
