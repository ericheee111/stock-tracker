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

两个既有 Engine 均在 Stage 2G 验证之前启动并持续持有 DB/WAL。没有证据表明 Stage 2G 代码写生产 DB；Stage 2G CLI 不打开该库，CLI tests 使用 before/after hash，所有写测试使用 temp paths。

Review 判定：

```text
STAGE2G_PRODUCTION_DB_WRITE_PATH = ABSENT
GLOBAL_PRODUCTION_DB_HASH_STABLE = NOT_PROVABLE_DUE_TO_EXISTING_CONCURRENT_ENGINES
```

没有擅自停止现有 Engine，也没有恢复或覆盖生产 DB。

## 5. 当前门禁结果

最终门禁结果：

```text
Git checkout Stage 2G/provider: 80 passed + 32 subtests
Git checkout Runtime:          517 passed, 1 skipped
Git checkout Quant:            594 passed + 277 subtests
Source distribution/bytecode:  3 passed + 63 subtests

Exact Git Index export:
Stage 2G/provider              80 passed + 32 subtests
Runtime                        517 passed, 1 skipped
Quant                          592 passed, 2 expected no-.git skips + 214 subtests

Hybrid H0                     12/12
Hybrid H1/H2                  28/28 + 11/11
Hybrid H4                     18/18
Monitor Workspace             49/49
Mock Today                    17/17
Real Today                    17/17
Portfolio CRUD                13/13

compileall                    PASSED
Targeted Ruff                 PASSED
pip check                     PASSED
git diff --cached --check     PASSED
Index generated/secret scan   PASSED (578 tracked / 36 staged / 0 findings)
Quant smoke                   PASSED / synthetic only
Quant benchmark               PASSED / no promotion
Stage 2G CLI                  3 cases / 0 HARD_BLOCK / 11 TRUST_BLOCK each
Migration snapshot dry-run    database_modified=false / pending=4
```

两个 Git Index Quant skip 只因为导出目录不含 `.git`，对应 source-distribution/no-bytecode 门禁已经在真实 checkout 中通过。

## 6. Final Verdict

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
NETWORK_SECURITY_REVIEW = PASSED
PARSER_AND_IDENTITY_REVIEW = PASSED
SYNTHETIC_GOLDEN_ACCEPTANCE = PASSED
FINANCIAL_CORRECTNESS_REVIEW = PASSED
REGRESSION_GATES = PASSED
SOURCE_DISTRIBUTION = PASSED
INDEX_SECRET_SCAN = PASSED
ENGINEERING_READY_FOR_MERGE = TRUE

REAL_SOURCE_RECONCILIATION = PENDING
LICENSE_CLEARANCE = PENDING
T3_RESEARCH_GRADE = NOT_REACHED
INVESTMENT_PERFORMANCE_CLAIM = FALSE
```

工程实现可提交；数据证据仍必须保持 `SYNTHETIC_VALIDATED / LICENSE_PENDING / T3_NOT_REACHED`。
