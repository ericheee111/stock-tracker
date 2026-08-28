# Stage 2H–2J Independent Adversarial Review

Review 日期：2026-08-29（Asia/Tokyo）

## 1. Review Scope

本报告独立审查以下工程切片：

```text
Stage 2H — A 股 exact-raw Acceptance Manifest
Stage 2I — External Assurance Declaration Registry
Stage 2J — fail-closed T3 Preflight
```

Review 不评估真实投资表现，也不把单次网络成功、同名 Provider、用户声明或本地 SHA-256 当成权威数据证明。

UI/UX 由 WorkBuddy/GLM 并行维护。本 Review 只运行产品回归并审查 Diff，不修改或暂存 `web/**`、`qa/shots/**` 与 UI 报告。

## 2. 威胁模型

重点对抗：

```text
synthetic_fixture=false 冒充独立真实来源
任意 SHA-256 冒充权威 Calendar/Universe/Corporate Action Binding
Synthetic Declaration 冒充证据包完整
空 sources 或多个单源声明冒充来源独立性
未来 Declaration 进入历史 as-of
未引用 Declaration 被偷偷塞入 Manifest
Parser/Schema/Descriptor/Raw/Normalized identity 漂移
Content-Type 错标绕开 HTML/JSON 边界
Tencent Parser 能力被误扩张到 HK/US
Capture 输出写入生产 DB 或穿越 symlink/junction
CLI 依赖运行 SQLite
Case Report / Coverage / Policy 被调用方替换
重复 Case Report 用 set equality 绕过 Manifest 完整性
并行 UI 文件被混入数据提交
```

## 3. Findings 与修复

### 3.1 `synthetic_fixture=false` 被描述为 Real Source

初版字段：

```text
real_source_observed
LIVE_STRUCTURALLY_CONSTRUCTIBLE
```

仅依赖 Capture request parameter，无法证明真实网络、法律来源或独立血缘。

修复：

```text
non_synthetic_declared
NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
```

并保留：

```text
LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED
NO_TRUSTED_MARKET_BAR_ASSURANCE_AUTHORITY
```

结论：`FIXED`。

### 3.2 任意 Auxiliary Report ID 可移除 Binding Blocker

初版只检查：

```text
stage2_reconciliation_report_id is not None
corporate_action_report_id is not None
```

任意 64 字符 SHA 就可能被解释为绑定已完成。

修复后分离：

```text
*_REFERENCE_MISSING
*_BINDING_NOT_INDEPENDENTLY_VERIFIED
```

Reference 存在只让“文件包引用缺失”消失；独立验证 Blocker 永远保留，直到未来 Trusted Authority 实现。

结论：`FIXED`。

### 3.3 Synthetic Assurance Declaration 可使 Package 看似完整

修复：

- synthetic Declaration 进入审计 ID 列表；
- 不进入 `declared_kinds`；
- `missing_kinds` 仍保持开放；
- T3 Preflight 仍为 `EVIDENCE_PACKAGE_INCOMPLETE`。

结论：`FIXED`。

### 3.4 Source-scoped Assurance 可使用空 sources

修复：

```text
SOURCE_FAMILY_INDEPENDENCE
FIELD_UNIT_POLICY
ADJUSTMENT_EQUIVALENCE
ARTIFACT_ATTESTATION
LIVE_PROVENANCE
LICENSE_APPROVAL
```

必须声明安全 source token；Source Independence 单条 Declaration 必须至少覆盖两个来源。

License/Unit/Adjustment 等允许多个单源 Declaration 合并覆盖全部来源，但不能用一个来源的许可替代另一个来源。

结论：`FIXED`。

### 3.5 Future / Orphan Declaration

修复：

- `known_at <= usable_from`；
- `known_at` 和 `usable_from` 均不得晚于 Manifest `created_at`；
- Case `as_of` 之前不可用的 Declaration 不计 Coverage；
- Manifest 拒绝未引用 Declaration；
- Declaration ID 与内容不一致失败关闭。

结论：`FIXED`。

### 3.6 Case/Report 派生字段可伪造

修复：

- `assurance_coverage` 由 Case + Declaration + Reconciliation 重新派生；
- Case Report 验证 as-of、Calendar、Session、Source Set、Case ID；
- Report 拒绝重复 Case ID、缺失/额外 Case、Policy 不一致；
- derived state 使用 `init=False`；
- `dataclasses.replace()` 不能传入 T3/Research Grade 状态；
- 输出固定 `research_grade=false / t3_reached=false`。

结论：`FIXED`。

### 3.7 Tencent Content-Type 错标

真实 Tencent A 股 Capture 的 Body 为 JSON，但上游可能返回 HTML Content-Type。直接接受会扩大 HTML 注入风险，完全拒绝则无法保存 exact raw。

实现窄例外：

```text
allow_mislabeled_json=true
```

仅 Tencent exact-raw 调用启用，并在返回前完整严格解析：

- UTF-8；
- object/array；
- 重复 Key 拒绝；
- NaN/Infinity 拒绝；
- HTML Prefix 拒绝；
- Redirect/Proxy/URL/Host/Credential 安全合同不变。

结论：`ACCEPTED_WITH_STRICT_BOUNDARY`。

### 3.8 Parser 能力与真实市场能力混淆

Tencent Parser 可对 vendor-shaped HK/US Fixture 做结构测试，但真实 endpoint 只在 A 股 `qfqday` 上获得证据。

修复：

```text
supports_market_adjustment(Market.A, "qfq") = true
supports_market_adjustment(Market.HK, "qfq") = false
supports_market_adjustment(Market.US, "qfq") = false
```

Stage 2H Case/CLI 当前固定：

```text
Market.A
1d
qfq
```

结论：`FIXED`。

### 3.9 CLI 路径、范围和 SQLite 隔离

修复：

- Capture CLI 从任意 cwd 启动时显式加入项目根；
- end < start 在 Provider 创建前失败；
- Output/Artifact/Manifest/Report/Declaration 路径逐级拒绝 symlink/junction；
- 拒绝生产 `data/stock_tracker.db`；
- 拒绝目录/文件类型错配；
- Report Root 与 Artifact Store 不得重叠；
- Assurance Declaration 必须是 regular file；
- Builder/Reporter 在 SQLite connect 被强制禁用时仍通过。

结论：`FIXED`。

### 3.10 并行 UI 边界

确认以下变更不属于 Stage 2H–2J：

```text
web/css/terminal.css
web/index.html
web/js/components.js
qa/shots/**
qa/ui-fix-report-2026-08-28.md
qa/responsive-verification-report-2026-08-28.md
```

只读 Review 结果：

- Today Mock 17/17；
- Today Real 17/17；
- Portfolio 13/13；
- Monitor 49/49；
- H0 12/12；
- H1/H2 28/28 + 11/11；
- H4 18/18；
- WorkBuddy 报告的 9 个响应式尺寸均无水平溢出；
- 未发现虚构概率、私有 Token 泄漏或外部 CDN 回归。

结论：`NO_BLOCKING_FINDING / EXCLUDED_FROM_COMMIT`。

## 4. 真实上游 Evidence Review

### Tencent

```text
600519.SH
2026-08-18 .. 2026-08-20
A / 1d / qfq
3 rows
Capture PASSED
```

该证据只证明：

```text
当前主机到 Tencent endpoint 可达
exact raw Capture/Parser/Artifact 路径可执行
```

不证明来源独立、许可、字段单位、QFQ 等价或 T3。

### Eastmoney

两次均在 HTTP Response 前发生：

```text
RemoteDisconnected
```

普通 User-Agent/Accept 重试无改善。Review 拒绝通过关闭 TLS、继承系统 Proxy、允许 Redirect 或更换未审计 Host 来制造“通过”。

### HiThink

环境没有 `HITHINK_FINANCE_API_KEY`，未执行真实请求。Offline Parser Binding 不访问网络，也不表示 HiThink 数据已获得 License 或 T2/T3 资格。

## 5. Checkout 与精确 Git Index 门禁

真实 Git Checkout 已通过：

```text
Focused Stage/Provider       77 passed + 52 subtests
Runtime                      522 passed, 1 skipped
Quant                        628 passed + 297 subtests
Source distribution/bytecode 3 passed + 75 subtests
Targeted Ruff                PASSED
compileall                   PASSED
pip check                    PASSED
Quant smoke                  PASSED / synthetic only
Quant benchmark              PASSED / no promotion
Mock Today                   17/17
Real Today                   17/17
Portfolio                    13/13
Monitor                      49/49
Hybrid H0                    12/12
Hybrid H1/H2                 28/28 + 11/11
Hybrid H4                    18/18
Migration backup dry-run     database_modified=false / pending=4
```

最终定向暂存生成的精确 Git Index：

```text
tree                         751925dbe0cc7e4b6ad9ab0c1d720f59790ef12a
staged files                 25
forbidden/generated paths    0
secret findings              0
git diff --cached --check    PASSED
```

该 tree 的隔离 Git archive 复验：

```text
Focused Stage/Provider       77 passed + 52 subtests
Runtime                      522 passed, 1 skipped
Quant                        626 passed, 2 expected no-.git skips + 222 subtests
Hybrid H0                    12/12
Hybrid H1/H2                 28/28 + 11/11
Hybrid H4                    18/18
Monitor                      49/49
Mock Today                   17/17
Real Today                   17/17
Portfolio                    13/13
Targeted Ruff                PASSED
compileall                   PASSED
```

两个 Index Quant skip 只因为 Git archive 没有 `.git`。对应 source-distribution/no-tracked-bytecode 门禁已在真实 Checkout 通过。

## 6. 数据库归因

生产 Engine 持续运行，因此不使用任务全窗口的生产 DB 文件 SHA 证明本阶段无写入。

更强证据：

```text
Stage 2H–2J CLI 无 Repository/SQLite 依赖
SQLite-forbidden subprocess = PASSED
所有写入位于显式 Artifact/Manifest/Report 路径
read-only SQLite backup snapshot SHA before/after 相同
migration database_modified=false
```

Snapshot SHA：

```text
06d5d458628c99b0596bc9e0d069b5ae351a046f49d7e66b8b4a0c26e04b532a
```

## 7. Residual Risks

```text
REAL_DUAL_SOURCE_CAPTURE = PENDING
EASTMONEY_REACHABILITY = FAILED_ON_CURRENT_HOST
HITHINK_LIVE_CAPTURE = PENDING_KEY_CONFIGURATION
SOURCE_INDEPENDENCE = UNVERIFIED
FIELD_UNIT_AND_CURRENCY = UNVERIFIED
QFQ_EQUIVALENCE = UNVERIFIED
LICENSE = PENDING
CALENDAR_AUTHORITY = PENDING
STATUS_UNIVERSE_BINDING = REFERENCE_ONLY
CORPORATE_ACTION_BINDING = REFERENCE_ONLY
TRUSTED_ASSURANCE_AUTHORITY = NOT_IMPLEMENTED
T3 = NOT_REACHED
```

即使所有 Declaration Kind 都出现，最高状态仍是：

```text
PENDING_INDEPENDENT_AUTHORITY
research_grade = false
t3_reached = false
```

## 8. Final Verdict 与 GitHub 交付

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
NETWORK_BOUNDARY_REVIEW = PASSED
PARSER_AND_IDENTITY_REVIEW = PASSED
PIT_AND_FINANCIAL_CORRECTNESS_REVIEW = PASSED
SQLITE_ISOLATION_REVIEW = PASSED
CHECKOUT_REGRESSION = PASSED
UI_READ_ONLY_REVIEW = PASSED_NO_BLOCKING_FINDING

FINAL_GIT_INDEX_REVIEW = PASSED
INDEX_GENERATED_AND_SECRET_SCAN = PASSED
ENGINEERING_READY_FOR_MERGE = TRUE
IMPLEMENTATION_GITHUB_DELIVERY = PASSED

REAL_DUAL_SOURCE_ACCEPTANCE = PENDING
LICENSE_CLEARANCE = PENDING
T3_RESEARCH_GRADE = NOT_REACHED
INVESTMENT_PERFORMANCE_CLAIM = FALSE
```

实现提交：

```text
commit:
ae9036286ee4f40a315891d44e86ab13e4347c41

message:
feat: add Stage 2H-2J market bar acceptance

verified tree:
751925dbe0cc7e4b6ad9ab0c1d720f59790ef12a
```

实现提交推送后，local `HEAD`、local `origin/main` 与 GitHub `refs/heads/main` 已验证三方一致。并行 UI 工作保持未暂存，没有进入该提交。
