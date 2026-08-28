# Stage 2H–2J Implementation Handoff

更新时间：2026-08-29（Asia/Tokyo）

## 1. 交付状态

```text
STAGE_2H_EXACT_RAW_ACCEPTANCE = IMPLEMENTED
STAGE_2I_ASSURANCE_DECLARATIONS = IMPLEMENTED_UNTRUSTED_INPUT_ONLY
STAGE_2J_T3_PREFLIGHT = IMPLEMENTED_FAIL_CLOSED
CHECKOUT_REGRESSION = PASSED_EXCEPT_PRE_STAGE_SOURCE_DISTRIBUTION
FINAL_GIT_INDEX_REVIEW = PENDING
GITHUB_DELIVERY = PENDING

REAL_DUAL_SOURCE_ACCEPTANCE = PENDING
TRUSTED_ASSURANCE_AUTHORITY = NOT_IMPLEMENTED
LICENSE_CLEARANCE = PENDING
T3_RESEARCH_GRADE = NOT_REACHED
```

本阶段建立真实或 synthetic exact-raw Capture 的接受、声明包盘点和 T3 前置检查工程链。它不建立数据许可批准权，不把 `synthetic_fixture=false` 解释为独立真实来源证明，也没有任何自动 `T3_REACHED`、训练、模型晋级或生产决策接线。

## 2. 阶段分工

### 2.1 Stage 2H — Exact-Raw Acceptance

输入为已经由 Capture CLI 写入本地 Artifact Store 的 descriptor，而不是运行 SQLite 中的 Bar，也不是调用方重新序列化的对象。

每个 Acceptance Case 绑定：

```text
case_name
market = A
symbol
interval = 1d
adjustment = qfq
as_of
expected_open_sessions
calendar_snapshot_id
>= 2 unique capture sources
comparable_fields
assurance_declaration_ids
optional auxiliary report references
```

处理链：

```text
Capture Descriptor
→ Raw Artifact Hash/Size 复验
→ 固定 Parser/Schema Binding
→ exact raw bytes 重放
→ normalized/capture identity 复验
→ Stage 2G Calendar/Field Reconciliation
→ content-addressed Case/Manifest/Report
```

当前工程范围固定为 A 股 `1d + qfq`。Tencent 的真实 endpoint 仅在 A 股上被观察到 `qfqday`；HK/US 不因 Parser 能解析 Fixture 就被声称为真实可用。

### 2.2 Stage 2I — Assurance Declaration Registry

Declaration 支持以下类型：

```text
CALENDAR_AUTHORITY
RECONCILIATION_POLICY_APPROVAL
SOURCE_FAMILY_INDEPENDENCE
FIELD_UNIT_POLICY
ADJUSTMENT_EQUIVALENCE
ARTIFACT_ATTESTATION
LIVE_PROVENANCE
LICENSE_APPROVAL
SECURITY_STATUS_UNIVERSE_BINDING
CORPORATE_ACTION_BINDING
T3_PROMOTION_DECISION
```

Declaration 只表示“有一份内容寻址的外部审查输入”，不表示仓库信任其结论。主要合同：

- `known_at <= usable_from <= manifest.created_at`；
- source-scoped 类型必须列出安全的 source token；
- Source Independence 必须由同一 Declaration 覆盖至少两个来源；
- License、Unit、Adjustment 等可由多个单源 Declaration 合并覆盖全部来源；
- synthetic Declaration 不满足 Coverage；
- 不覆盖 Case market/source/as-of 的 Declaration 不计入完成度；
- Manifest 拒绝缺失、未引用、重复、未来或内容 ID 不一致的 Declaration；
- 仓库没有 Trusted Assurance Authority，Declaration 不能关闭 Trust Blocker。

### 2.3 Stage 2J — T3 Preflight

Preflight 只有三态：

```text
HARD_BLOCKED
EVIDENCE_PACKAGE_INCOMPLETE
PENDING_INDEPENDENT_AUTHORITY
```

Acceptance State 只有：

```text
HARD_BLOCKED
SYNTHETIC_CONTRACT_ONLY
NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
```

其中 `NON_SYNTHETIC_DECLARED` 只说明 Capture descriptor 中的 `synthetic_fixture` 明确为 `false`。它不证明网络来源、法律来源、字段口径、复权算法或数据授权。

报告固定输出：

```text
trusted_assurance_authority_configured = false
research_grade = false
t3_reached = false
license_clearance_complete = false
production_database_modified = false
```

辅助报告 ID 只表示引用存在：

- 引用缺失会产生 `*_REFERENCE_MISSING`；
- 即使引用存在，仍保留 `*_BINDING_NOT_INDEPENDENTLY_VERIFIED`；
- arbitrary SHA-256 不能关闭 Security/Status/Universe 或 Corporate Action 门禁。

## 3. 实现文件

### 3.1 核心代码

```text
stock_tracker/quant/data/market_bar_acceptance.py
stock_tracker/quant/data/__init__.py
```

### 3.2 CLI

```text
scripts/build_stage2h_market_bar_acceptance.py
scripts/report_stage2h_market_bar_acceptance.py
scripts/capture_quant_bars.py
scripts/capture_hithink_bars.py
```

### 3.3 Provider 边界

```text
stock_tracker/collector/provider.py
stock_tracker/collector/tencent.py
```

新增内容：

- `supports_market_adjustment(market, adjust)`；
- Tencent 真实 Capture 只声明 A 股 `qfq`；
- exact-raw 通道可对明确允许的“Content-Type 错标但 Body 为严格 JSON”做窄例外；
- 例外仍拒绝 HTML 前缀、非 UTF-8、重复 Key、`NaN/Infinity`、JSON scalar、Redirect、Proxy、Host/Credential Header 与 URL 漂移。

### 3.4 测试

```text
tests_quant/test_market_bar_acceptance.py
tests_quant/test_stage2h_market_bar_acceptance_cli.py
tests_quant/test_capture_quant_bars_cli.py
tests_quant/test_capture_hithink_bars_cli.py
tests_quant/test_market_bar_reconciliation.py
tests_quant/test_source_distribution.py
tests/test_provider_research_request.py
tests/test_tencent_bars.py
```

## 4. 路径和数据库隔离

Capture、Manifest、Declaration 和 Report CLI 均拒绝：

```text
symlink / junction 路径或祖先
生产 data/stock_tracker.db
文件与目录类型不符
Artifact Store 与 Report Root 重叠
Manifest 写入 Artifact Store 内部
结束日期早于开始日期
重复 Open Session
重复 Comparable Field
```

Builder/Reporter 在 `sqlite3.connect` 与 `sqlite3.dbapi2.connect` 被强制替换为异常时仍可完成 synthetic Fixture 端到端验收，证明 Stage 2H–2J 离线链不依赖 SQLite。

## 5. 真实上游探针

### 5.1 Tencent

执行：

```text
symbol     = 600519.SH
market     = A
start/end  = 2026-08-18 .. 2026-08-20
interval   = 1d
adjustment = qfq
```

结果：

```text
capture = PASSED
rows = 3
trust_tier = BEST_EFFORT
synthetic_fixture = false
production_database_modified = false
```

该 Raw/Descriptor 保存在被 Git 忽略的 `data/stage2h-live-artifacts/`，没有进入源码提交。

### 5.2 Eastmoney

同窗口在当前主机两次均在收到 HTTP Response 前失败：

```text
http.client.RemoteDisconnected
```

加入普通 User-Agent/Accept Header 后仍失败。没有通过继承 Proxy、关闭 TLS、允许 Redirect 或切换不透明 Host 来绕过安全合同。

### 5.3 HiThink

当前进程未配置 `HITHINK_FINANCE_API_KEY`，因此未进行真实 Capture。Builder/Reporter 只创建一个离线 Parser Binding，不读取或显示 Key，也不访问网络。

## 6. Checkout 验证证据

当前工作区代码在最终定向暂存前已经通过：

```text
Stage 2H–2J / Provider focused:
77 passed + 52 subtests

Runtime full unittest:
522 passed, 1 expected live-probe skip

Quant functional suite excluding pre-stage source-distribution:
626 passed + 222 subtests

Targeted Ruff:
PASSED

compileall:
PASSED

pip check:
PASSED

Quant contract smoke:
PASSED / synthetic_fixture_only=true

Quant synthetic benchmark:
PASSED / investment_performance_claim=false
Challenger not promoted: ECE_REGRESSED, TIME_INSTABILITY
```

完整 Quant 在暂存前唯一红灯为新 Stage 2H 文件尚未被 Git 跟踪，因此 source-distribution 精确报告 5 个未跟踪关键文件。该门禁必须在定向 `git add` 和精确 Git Index 导出后重跑，不得提前写成通过。

## 7. 产品与 Hybrid 回归

当前并行 UI 工作树只读验收：

```text
Mock Today                 17/17
Real Today                 17/17
Portfolio CRUD             13/13
Monitor Workspace          49/49
Hybrid H0                  12/12
Hybrid H1/H2               28/28 + 11/11 negative
Hybrid H4                  18/18
Node syntax                PASSED
```

这些命令验证了 WorkBuddy 当前 UI 改动没有破坏现有产品合同，但 UI 文件、截图和 QA 报告不属于本阶段提交。

## 8. Migration 证据

从运行中的生产 SQLite 通过 read-only connection + SQLite backup API 创建一致 Snapshot：

```text
snapshot SHA before = 06d5d458628c99b0596bc9e0d069b5ae351a046f49d7e66b8b4a0c26e04b532a
snapshot SHA after  = 06d5d458628c99b0596bc9e0d069b5ae351a046f49d7e66b8b4a0c26e04b532a
database_modified   = false
pending migrations  = 4
```

没有对生产数据库执行 `--apply`，也没有停止或重启既有 Engine。

## 9. 并行 UI 保护

以下路径由 WorkBuddy/GLM 并行维护，本阶段只 Review，不编辑、不暂存、不提交：

```text
web/**
qa/shots/**
qa/ui-fix-report-2026-08-28.md
qa/responsive-verification-report-2026-08-28.md
```

当前 UI Review 未发现阻断项。旧截图删除、新响应式截图与三个 Web 文件改动必须由 UI 工作流单独 Review/提交。

## 10. 最终交付前剩余动作

```text
1. 完成独立对抗式 Review 文档
2. 更新 CHATGPT_HANDOFF / overview / 主 Handoff
3. 仅暂存 Stage 2H–2J 非 UI 文件
4. 重跑完整 Quant/source-distribution/no-bytecode
5. 从精确 Git Index 导出独立树
6. 在导出树重跑 focused/Runtime/Quant/Hybrid/Web 门禁
7. generated/secret scan + git diff --cached --check
8. 创建实现提交并 push
9. 写入提交/tree/远端 SHA，创建交接提交并再次 push
```

## 11. 仍然保持 PENDING

```text
Eastmoney 当前网络可达性
真实 A 股双源 Acceptance
HiThink 实际 Key Capture
来源独立性证明
字段单位与币种证明
QFQ 算法数值等价证明
数据保存/训练/再分发许可
权威 Calendar/Status/Universe
真实 Corporate Action Binding
Trusted Assurance Authority Registry
T3 Snapshot Assembler
正式 PIT Replay / 训练 / 校准 / 模型晋级
```
