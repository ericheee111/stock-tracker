# Stage 2H–2J Market-Bar Acceptance、Assurance Registry 与 T3 Preflight

状态：`DESIGN_FROZEN / ENGINEERING_IMPLEMENTED / FINAL_INDEX_REVIEW_PENDING`

日期：2026-08-29

## 1. 目的

Stage 2G 已证明 A/HK/US vendor-shaped synthetic raw payload 的 Parser、Artifact、字段对账与 Calendar Session Coverage 工程合同，但没有真实双源、许可、单位、复权等价或权威辅助数据证据。

Stage 2H–2J 的目标是把真实或声明为非 synthetic 的 exact-raw Capture 放入一个**失败关闭、内容寻址、不可自我晋级**的接受流程：

```text
Stage 2H — Exact-Raw Acceptance Manifest
→ Stage 2I — External Assurance Declaration Registry
→ Stage 2J — T3 Preflight
```

本阶段只形成审查材料与 Preflight 状态，不产生 T3 Snapshot，不进入训练、回测、校准、模型晋级或正式决策。

## 2. 冻结边界

### 2.1 包含

- 从已存在的 `CapturedBarArtifact` descriptor 离线重放 exact raw bytes；
- 绑定 source、schema、parser 与 parser-binding ID；
- 至少两个唯一来源；
- 当前只接受 A 股 `interval=1d`、`adjustment=qfq`；HK/US 必须在独立来源齐备后另升版本；
- 重新执行 Stage 2G OHLC/Volume 字段对账和 Calendar Session Coverage；
- 登记外部 Assurance Declaration，但不把 Declaration 当成批准权；
- 记录 Stage 2 Security/Status/Universe Reconciliation 与 Corporate Action Report 的引用；
- 输出内容寻址 JSON/Markdown 接受报告；
- 始终保留独立权威、许可和 T3 阻断。

### 2.2 不包含

- 自动下载或提交真实行情 Artifact；
- 自动生成 License Approval；
- 调用方布尔值关闭 Trust Blocker；
- 把两个 source label 自动认定为法律或血缘独立；
- 把同名 `qfq` 自动认定为数值等价；
- 自动绑定权威 Calendar、Status、Universe 或 Corporate Action；
- 修改 `data/stock_tracker.db`；
- 修改 Runtime Router、Signal、ActionState、模型或 UI；
- T3/T4 晋级；
- 投资表现声明。

## 3. Stage 2H — Exact-Raw Acceptance Manifest

### 3.1 Capture Reference

每个 Capture Reference 绑定：

```text
source
capture descriptor_key
parser_binding_id
```

加载时必须重新验证：

```text
Raw SHA-256
Raw byte size
RawDataArtifact identity
Capture descriptor identity
Parser version
Schema version
Normalized dataset identity
Capture ID
Deterministic parser replay
Symbol / Market / Interval / Adjustment
```

来源名称必须是安全 token；descriptor 必须使用受控 storage key。至少两个唯一来源，不能用同一来源的重复 Capture 冒充独立 corroboration。

### 3.2 Acceptance Case

每个 Case 绑定：

```text
case_name
market
symbol
interval = 1d
adjustment = qfq
as_of
expected_open_sessions
calendar_snapshot_id
capture references
comparable fields
assurance declaration IDs
auxiliary report reference IDs
```

`case_id` 由全部字段内容寻址生成。Manifest 拒绝重复 Case Name、重复 Case ID、未来 `as_of`、缺失或未引用 Declaration。

### 3.3 Acceptance State

```text
HARD_BLOCKED
SYNTHETIC_CONTRACT_ONLY
NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
```

`NON_SYNTHETIC_DECLARED` 只表示 Capture descriptor 明确声明 `synthetic_fixture=false`；它不等于独立证明的真实来源。报告继续保留：

```text
LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED
MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED
SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED
LICENSE_PENDING
T3_NOT_REACHED
```

## 4. Stage 2I — External Assurance Declaration Registry

### 4.1 Declaration 类型

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

每条 Declaration 绑定：

```text
kind
source_owner
source_version
known_at
usable_from
markets
sources
evidence_artifact_ids
synthetic
details
declaration_id
```

### 4.2 失败关闭规则

- `known_at <= usable_from <= manifest.created_at`；
- Source-scoped Declaration 必须明确列出来源；
- `SOURCE_FAMILY_INDEPENDENCE` 至少覆盖两个来源；
- synthetic Declaration 不满足 Assurance Coverage；
- Declaration 必须被至少一个 Case 引用；
- Case 引用不存在的 Declaration 失败；
- ID 与内容不一致失败；
- 未覆盖 Case market/source/as-of 的 Declaration 不计入 Coverage；
- 多个单源 License/Unit/Adjustment Declaration 可以合并覆盖全部来源；
- Source Independence 必须由同一 Declaration 覆盖全部被比较来源。

Declaration 只是 Review Input。当前仓库没有 Trusted Assurance Authority，不能因为 Declaration 完整就关闭 Trust Blocker。

## 5. Stage 2J — T3 Preflight

### 5.1 Preflight State

```text
HARD_BLOCKED
EVIDENCE_PACKAGE_INCOMPLETE
PENDING_INDEPENDENT_AUTHORITY
```

### 5.2 判定

- Reconciliation 有 HARD_BLOCK → `HARD_BLOCKED`；
- Capture 含 synthetic、Assurance Kind 缺失或辅助报告引用缺失 → `EVIDENCE_PACKAGE_INCOMPLETE`；
- 非 synthetic-declared Capture、所有 Declaration 类型与辅助引用均存在 → `PENDING_INDEPENDENT_AUTHORITY`；
- 不存在自动 `T3_REACHED` 状态；
- 输出固定：

```text
trusted_assurance_authority_configured = false
research_grade = false
t3_reached = false
license_clearance_complete = false
production_database_modified = false
```

辅助报告 ID 的存在只表示“引用已声明”。报告仍显式保留：

```text
SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED
CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED
NO_TRUSTED_MARKET_BAR_ASSURANCE_AUTHORITY
```

## 6. CLI

### 6.1 捕获 exact raw

```powershell
python scripts/capture_quant_bars.py `
  --provider tencent `
  --symbol 600519.SH `
  --market A `
  --start 2026-08-18 `
  --end 2026-08-20 `
  --adjust qfq `
  --output-root data/stage2h-live-artifacts
```

Eastmoney 使用相同参数，只替换 `--provider eastmoney`。HiThink 可通过 `scripts/capture_hithink_bars.py` 捕获 A 股 exact raw；真实调用前只在本机环境变量 `HITHINK_FINANCE_API_KEY` 中配置 Key。Builder/Reporter 的 HiThink Parser Registry 是离线的，不读取该环境变量，也不访问网络。

### 6.2 构建 Acceptance Manifest

```powershell
python scripts/build_stage2h_market_bar_acceptance.py `
  --artifact-root data/stage2h-live-artifacts `
  --output data/stage2h-acceptance/manifest.json `
  --case-name A_600519_20260818_20260820 `
  --symbol 600519.SH `
  --market A `
  --adjustment qfq `
  --as-of 2026-08-21T00:00:00Z `
  --created-at 2026-08-21T01:00:00Z `
  --calendar-snapshot-id <SHA256> `
  --open-session 2026-08-18 `
  --open-session 2026-08-19 `
  --open-session 2026-08-20 `
  --capture eastmoney=<DESCRIPTOR_KEY> `
  --capture tencent=<DESCRIPTOR_KEY>
```

### 6.3 生成 Preflight Report

```powershell
python scripts/report_stage2h_market_bar_acceptance.py `
  --manifest data/stage2h-acceptance/manifest.json `
  --artifact-root data/stage2h-live-artifacts `
  --output-dir data/stage2h-acceptance/reports
```

所有 `data/` 输出均为本地生成物，不进入 Git。

## 7. 网络与文件系统边界

- exact-raw 请求使用系统 CA 与 hostname verification；
- 不继承 Proxy；
- 不允许 Redirect；
- 禁止 Authority/Credential Header；
- Tencent 只对其已观测到的错误 `text/html` Content-Type 开启严格 JSON body 例外；HTML、重复 Key、非有限数值或非 JSON 仍失败；
- Capture/Manifest/Report 输出拒绝遍历 symlink/junction；
- Artifact Root 与 Report Root 必须分离；
- 禁止把输出目标设为生产 SQLite；
- Builder/Reporter 在 `sqlite3.connect` 与 `sqlite3.dbapi2.connect` 被禁用时仍须运行。

## 8. 2026-08-28 本机 Operational Probe

实际执行结果：

```text
Tencent / 600519.SH / A / 2026-08-18..2026-08-20 / qfq
= exact-raw capture PASSED
= row_count 3
= trust_tier BEST_EFFORT
= production_database_modified false

Eastmoney / same window
= RemoteDisconnected before response
= no Capture descriptor emitted
```

因此当前只能声明：

```text
TENCENT_LIVE_CAPTURE_PATH = OBSERVED
EASTMONEY_CURRENT_NETWORK_REACHABILITY = FAILED
REAL_DUAL_SOURCE_RECONCILIATION = PENDING
```

不得用 synthetic Golden、单一 Tencent Capture 或失败的 Eastmoney请求冒充真实双源验收。

## 9. 工程合并门禁

```text
Stage 2H–2J 专项单测
Provider exact-raw 负向测试
Runtime 全量测试
Quant 全量测试
Source distribution / no tracked bytecode
compileall
targeted Ruff
pip check
Quant contract smoke
synthetic fixture benchmark
SQLite-forbidden CLI sandbox
read-only migration snapshot dry-run
exact Git Index export
staged-tree Runtime/Quant/Hybrid/Web regression
generated/secret scan
git diff --cached --check
independent financial-correctness review
```

UI 由并行 WorkBuddy 流维护。本阶段只审查 UI diff，不修改、不暂存、不提交 `web/**` 与 `qa/shots/**`。
