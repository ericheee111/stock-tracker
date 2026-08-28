# Stage 2G Independent Engineering and Financial-Correctness Review

> 日期：2026-08-28
>
> Review 范围：Golden Raw、Exact-Raw Network、Parser、Artifact Identity、Cross-Source Reconciliation、Coverage、CLI、Docs、Tests
>
> 当前 Verdict：`ENGINEERING_READY_FOR_MERGE`
>
> 数据 Verdict：`SYNTHETIC_VALIDATED / LICENSE_PENDING / T3_NOT_REACHED`

## 1. Review 原则

本 Review 不根据“代码存在”判定完成，而尝试主动绕过以下边界：

```text
TLS / Proxy / Redirect
Parser Schema
Adjustment Identity
Mutable Dataclass
Duplicate JSON Key
Source Independence
Policy Threshold
Calendar / as_of
Coverage
License
Trust Tier
Report Identity
Production DB Isolation
```

任何工程通过都不能被解释为真实行情权威性、数据许可、T3、策略收益或投资表现。

## 2. Findings 与处置

### CRITICAL-01 — exact-raw 复用跳过 TLS 校验 Runtime 通道

**风险**：研究 Capture 若调用旧 `_request()`，会关闭 certificate/hostname verification，也会继承系统 Proxy/Redirect 语义。内容 Hash 只能证明保存了收到的字节，不能证明字节来自目标 Provider。

**修复**：新增 `_request_research()`：

```text
system CA
hostname verification
ProxyHandler({})
NoRedirectHandler
HTTPS/default 443
no credentials/fragment
no Host override
exact final URL
Content-Type/Length/body bounds
```

**验证**：故障注入覆盖 HTTP、userinfo、custom port、Host override、redirect、URL change、HTML、missing/unsupported Content-Type、invalid/oversized/mismatched length、empty/oversized body。

**状态**：`CLOSED`。

### CRITICAL-02 — Tencent QFQ 静默回退未复权 `day`

**风险**：请求 `qfq` 时若 `qfqday` 缺失而使用 `day`，会把 raw 数据冒充前复权数据；跨源差异和后续模型都会被错误口径污染。

**修复**：Strict Parser 强制 `qfqday`；Operational Parser 缺失时返回空，不回退。

**状态**：`CLOSED`。

### CRITICAL-03 — Frozen Capture 仍可通过嵌套对象修改

**风险**：`CapturedBarArtifact` frozen dataclass 内含 mutable dict 和 mutable `Bar`；调用方可在构造后修改 Request 或 Row，同时保留旧 ID。

**修复**：`validate_captured_market_bars()` 重算 Request、Rows、Bounds、Dataset ID、Descriptor Key 和 Capture ID；`MarketBarSeriesEvidence` 构造时转换为 immutable `MarketBarPoint`，报告不再读取 mutable Capture。

**验证**：修改 Request 与 Bar 后，既有 Report ID 不变化；重新验证原 Capture 失败关闭。

**状态**：`CLOSED`。

### CRITICAL-04 — `dataclasses.replace()` 可伪造 Trust 或派生报告字段

**风险**：调用方可将 Capture Trust 改为 `RESEARCH_GRADE`，或尝试删除 Report findings。

**修复**：Capture Validator 固定 Trust 上限；Report findings/comparisons/coverage/blockers/state 全部 `init=False` 派生。改变 Policy 会重算 Report ID。

**状态**：`CLOSED`。

### CRITICAL-05 — 来源名称可被改写成伪独立 Source Family

**风险**：同一 Capture 被调用方赋予不同 family 名称，可能满足 minimum source count。

**修复**：Stage 2G `source_family` 必须严格等于 Capture 的 `artifact.source`；相同 Source 重复 Capture 不计独立佐证，并保留 `SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED`。

**状态**：`CLOSED`。

### CRITICAL-06 — License 可由调用方自行标记为 Cleared

**风险**：本地 Enum 值不构成许可批准权威。

**修复**：Series 拒绝 `CLEARED_FOR_INTERNAL_RESEARCH`；Stage 2G 固定 `LICENSE_PENDING`，Policy 不能关闭 License Gate。

**状态**：`CLOSED`。

### IMPORTANT-01 — Synthetic Fixture 可被重新标为 Live

**风险**：只改变 `synthetic_fixture` 参数可能将 committed fixture 包装成真实 Capture。

**修复**：Synthetic 标记进入 immutable Request Parameters 和 Capture ID；Series 标记必须与 Capture evidence 完全一致；Pack 本身强制 `synthetic_fixture=true`。

**状态**：`CLOSED`。

### IMPORTANT-02 — Strict JSON 接受重复 Key / 非有限数值

**风险**：不同 Parser 对重复 Key 的 first/last-wins 行为可能产生不同 normalized data，且 `NaN/Infinity` 会破坏 canonical identity。

**修复**：Eastmoney、Tencent、Capture Descriptor、Golden Manifest 全部使用 duplicate-key/nonfinite rejecting loader。

**状态**：`CLOSED`。

### IMPORTANT-03 — Eastmoney Strict Parser 未验证 OHLC 与有限值

**风险**：`low > high`、非有限值或负量额可能进入 Capture，直到较晚边界才失败或污染 Runtime tolerant path。

**修复**：Strict/Operational 共用有限值、正 OHLC、一致性和非负量额检查；Operational 只跳过单行，Strict 拒绝完整响应。

**状态**：`CLOSED`。

### IMPORTANT-04 — 单一 Source 可产生假 `MATCH`

**风险**：只有一个值时最大差异为 0，可能被错误标为跨源一致。

**修复**：字段至少存在两个 Series 才 `comparable=true`；单源 Session 为 `NOT_COMPARABLE`。

**状态**：`CLOSED`。

### IMPORTANT-05 — Daily Bar 在 as_of 当天尚未最终形成

**风险**：仅比较 Session civil date `<= as_of date`，可能在盘中把当天未结束日线当作最终 Bar。

**修复**：Stage 2G daily reconciliation 要求每个 expected session **严格早于** as_of 的交易所本地日期；同日或未来日均 HARD_BLOCK `CALENDAR_SESSION_NOT_FINAL_AS_OF`。

**状态**：`CLOSED`。

### IMPORTANT-06 — 调用方可关闭 Coverage Gate

**风险**：将 `require_all_open_sessions=false` 会让缺交易日只变 Trust Block，从而误放 structurally constructible。

**修复**：Stage 2G Policy 拒绝关闭 Open Session Coverage；同时拒绝关闭 License Gate。

**状态**：`CLOSED`。

### IMPORTANT-07 — Policy 由调用方选择但可能被误当批准策略

**风险**：Policy ID 稳定不等于阈值已被独立金融审查批准。

**修复**：全 Policy 字段绑定 Report ID，同时固定 `RECONCILIATION_POLICY_NOT_INDEPENDENTLY_APPROVED` Trust Blocker。

**状态**：`CLOSED`。

### IMPORTANT-08 — 报告文件名可被新 Policy 覆盖

**风险**：`A_600519.json` 被新阈值报告覆盖后，历史证据不可追溯。

**修复**：输出改为 `<case>/<report_id>.json|md`；已存在路径内容不同时失败关闭，同内容重复写幂等。

**状态**：`CLOSED`。

### IMPORTANT-09 — Content-Length 只验证上限，不验证实际长度

**风险**：截断响应仍可能被保存和解析。

**修复**：声明 Content-Length 时实际字节长度必须完全一致。

**状态**：`CLOSED`。

### IMPORTANT-10 — `datetime` 是 `date` 子类

**风险**：Calendar Session 或 MarketBarPoint 可能接受带时间的 datetime，导致 canonical sort/identity 语义漂移。

**修复**：使用 `type(value) is date`，Report 与 Point 均拒绝 datetime。

**状态**：`CLOSED`。

### IMPORTANT-11 — HTML 错误页可伪装成允许的 `text/plain`

**风险**：仅依赖响应 Header 会保存被网关错误标注的 HTML 页面。

**修复**：在 Content-Type 校验后继续检查去除 UTF-8 BOM/空白后的 Body 前缀，拒绝 `<!doctype html`、`<html`、`<head` 与 `<body`。

**状态**：`CLOSED`。

### IMPORTANT-12 — Eastmoney Strict Parser 未拒绝重复/乱序交易日

**风险**：重复或乱序 Row 会改变内容边界、Coverage 与下游 PIT 顺序。

**修复**：Strict Parser 对 duplicate/non-chronological 日期失败关闭；Operational Parser 只跳过损坏 Row。

**状态**：`CLOSED`。

### IMPORTANT-13 — Self-consistent Golden Pack 可整体重算后替换

**风险**：仅校验内部 Pack ID，攻击者可同时修改 Raw、Source/Case ID 和 Pack ID，得到另一份自洽 Fixture。

**修复**：每个 Pack Version 绑定代码内固定 ID；未知版本或 ID 不匹配失败关闭。

**状态**：`CLOSED`。

### IMPORTANT-14 — Parser 语义改变但版本未升级

**风险**：Eastmoney 严格 JSON/OHLC/Chronology 语义改变后仍使用旧 Parser Version，会让新 Parser 冒充旧证据重放。

**修复**：保留已发布 v1 Pack 与 `eastmoney-bars-v2-raw-split` 身份；新增默认 v2 Pack，绑定 `eastmoney-bars-v3-strict-research`。当前 Parser 不能重放 v1。

**状态**：`CLOSED`。

### IMPORTANT-15 — Future Artifact 仍可能贡献 Source Count/Coverage/Comparison

**风险**：虽然有 `ARTIFACT_NOT_VISIBLE_AS_OF` HARD_BLOCK，未来 Artifact 若仍参与派生指标，会污染报告证据。

**修复**：future Artifact 在 source count、observed union、fully observed 和字段 comparison 中按空证据处理，同时保留 HARD_BLOCK。

**状态**：`CLOSED`。

### IMPORTANT-16 — In-memory Capture 未绑定 exact raw bytes 与 Parser

**风险**：只冻结 normalized Bars 与 Artifact 元数据，调用方仍可能替换 raw bytes 或 Parser 后保留旧 Capture ID。

**修复**：`CapturedBarArtifact` 保存 exact `raw_bytes` 与 Parser callable；Validator 校验 byte size/hash，重新解析 exact bytes，并要求 reparsed rows 与 frozen rows 完全一致。Validator 返回与调用方对象分离的 canonical Bar 副本；布尔数值、市场本地 Session Date 与 aware timestamp 排序也在同一边界失败关闭。

**状态**：`CLOSED`。

### IMPORTANT-17 — Capture 当地同日/未来 Daily Bar 仍可参与对账

**风险**：Report `as_of` 虽然可能在更晚日期，但某条日线在 Artifact 抓取时仍属于当地同日或未来 Session；如果继续参与 Coverage/MATCH，会把抓取时尚未最终形成的数据当成历史事实。

**修复**：每个 Series 以 Artifact `retrieved_at` 转为交易所本地日期；只有严格早于该日期的 Daily Session 才能贡献 Coverage 和字段比较。同日/未来 Session 产生 `MARKET_BAR_SESSION_NOT_FINAL_AT_CAPTURE / HARD_BLOCK` 并从派生指标排除。

**状态**：`CLOSED`。

### IMPORTANT-18 — Public exact-raw Header 可绕过 Host/凭据边界

**风险**：调用方即使不能设置配置层 Host Override，仍可能通过 `Host`、`Authorization`、`Cookie`、`Proxy-Authorization` 或 API-Key Header 改变 authority 或把秘密带入公共 Capture 通道；非规范 URL/Header 还可能触发请求分歧。

**修复**：公共 exact-raw 通道要求 canonical HTTPS URL，拒绝反斜杠、控制字符、首尾空白、非 dictionary Header、大小写重复 Header、authority/cookie/credential Header 和 Header 注入。需要认证的数据源必须使用独立专用 Adapter。

**状态**：`CLOSED`。

### IMPORTANT-19 — Report 输出可经 Symlink/Junction 越界

**风险**：即使文件名由 `report_id` 决定，预先放置的目录链接仍可能把 immutable JSON/Markdown 写到目标树之外。

**修复**：写入前逐级拒绝 symlink 与 Windows junction；内容寻址路径继续保持“同路径同内容幂等、不同内容失败关闭”。

**状态**：`CLOSED`。

## 3. 仍然 OPEN 的证据问题

这些不是当前工程 Bug，而是 Stage 2G 有意保留的外部门禁：

```text
CALENDAR_BINDING_NOT_INDEPENDENTLY_VERIFIED
RECONCILIATION_POLICY_NOT_INDEPENDENTLY_APPROVED
SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED
MARKET_BAR_FIELD_UNIT_POLICY_UNVERIFIED
ADJUSTMENT_POLICY_EQUIVALENCE_UNVERIFIED
MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED
SYNTHETIC_MARKET_BAR_EVIDENCE
LICENSE_PENDING
T3_NOT_REACHED
```

真实 Capture 即使 `synthetic=false`，仍会获得：

```text
LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED
```

直到存在独立来源/许可/政策/Calendar 权威证据。

## 4. 生产数据库归因 Review

本轮开始时 production DB SHA 为 `1cde40...`，但回归期间持续变化。只读进程审计发现：

```text
PID 55468 / port 8080 / started 07:59:37Z
PID 52008 / port 8090 / started 08:08:29Z
```

两个既有 Engine 均在 Stage 2G 验证之前启动并持续持有 DB/WAL。没有证据表明 Stage 2G 代码写生产 DB；Stage 2G CLI 不导入 Repository/SQLite，subprocess 验收通过 `sitecustomize` 强制禁止 `sqlite3.connect` 与 `sqlite3.dbapi2.connect` 后仍完整生成三市场报告，所有 Artifact/Report 写入使用 temp 或 `/data/` 生成路径。

Review 判定：

```text
STAGE2G_PRODUCTION_DB_WRITE_PATH = ABSENT
GLOBAL_PRODUCTION_DB_HASH_STABLE = NOT_PROVABLE_DUE_TO_EXISTING_CONCURRENT_ENGINES
```

没有擅自停止现有 Engine，也没有恢复或覆盖生产 DB。

## 5. 当前门禁结果

当前 Git checkout 门禁：

```text
Stage 2G/provider              93 passed + 54 subtests
Runtime                        520 passed, 1 skipped
Quant                          604 passed + 290 subtests
Source distribution/bytecode  3 passed + 70 subtests
compileall                     PASSED
Targeted Ruff                  PASSED
pip check                      PASSED
Quant smoke                    PASSED / synthetic only
Quant benchmark                PASSED / no promotion
Stage 2G CLI                   v2 pack / 3 cases / 0 HARD_BLOCK / 11 TRUST_BLOCK each
Migration snapshot dry-run     database_modified=false / pending=4
SQLite-forbidden CLI sandbox   PASSED
```

精确 Git Index 隔离导出同样通过：

```text
Stage 2G/provider              93 passed + 54 subtests
Runtime                        520 passed, 1 skipped
Quant                          602 passed, 2 expected no-.git skips + 220 subtests
Hybrid H0                      12/12
Hybrid H1/H2                   28/28 + 11/11
Hybrid H4                      18/18
Monitor Workspace              49/49
Mock Today                     17/17
Real Today                     17/17
Portfolio CRUD                 13/13
Targeted Ruff                  PASSED
compileall                     PASSED
git diff --cached --check      PASSED
Index generated/secret scan    28 staged files / 0 findings
```

两个 Quant skip 仅因为 Git archive 导出树没有 `.git`；相同 source-distribution/no-bytecode 门禁已在真实 checkout 中通过 `3 passed + 70 subtests`。本报告不预写尚未创建的 hardening commit 或远端 SHA。

## 6. Final Verdict

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
NETWORK_SECURITY_REVIEW = PASSED
PARSER_AND_IDENTITY_REVIEW = PASSED
SYNTHETIC_GOLDEN_ACCEPTANCE = PASSED
FINANCIAL_CORRECTNESS_REVIEW = PASSED
CHECKOUT_REGRESSION_GATES = PASSED
SOURCE_DISTRIBUTION = PASSED
FINAL_INDEX_REVIEW = PASSED
INDEX_GENERATED_AND_SECRET_SCAN = PASSED
ENGINEERING_READY_FOR_MERGE = TRUE

REAL_SOURCE_RECONCILIATION = PENDING
LICENSE_CLEARANCE = PENDING
T3_RESEARCH_GRADE = NOT_REACHED
INVESTMENT_PERFORMANCE_CLAIM = FALSE
```

工程实现可提交；数据证据仍必须保持 `SYNTHETIC_VALIDATED / LICENSE_PENDING / T3_NOT_REACHED`。
