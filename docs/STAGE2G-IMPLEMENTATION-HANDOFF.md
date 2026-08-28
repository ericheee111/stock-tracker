# Stage 2G Implementation Handoff

> 日期：2026-08-28
>
> 工程状态：`IMPLEMENTED / FULL_REGRESSION_PASSED`
>
> 数据证据：`SYNTHETIC_VALIDATED / REAL_SOURCE_ACCEPTANCE_PENDING`
>
> 许可：`LICENSE_PENDING`
>
> 研究级：`T3_NOT_REACHED`

## 1. 目标与结论

Stage 2G 已建立 A/HK/US 三市场日线数据的版本化 Synthetic Golden Raw、Eastmoney/Tencent exact-raw 安全抓取、严格 Parser Binding、内容寻址 Capture、字段级跨源 Reconciliation 与 Calendar Session Coverage 合同。

本轮准确结论：

```text
ENGINEERING_CONTRACT = IMPLEMENTED
SYNTHETIC_GOLDEN_ACCEPTANCE = PASSED
REAL_SOURCE_RECONCILIATION = PENDING
LICENSE_CLEARANCE = PENDING
T3_RESEARCH_GRADE = NOT_REACHED
```

任何 Golden、对账或 `STRUCTURALLY_CONSTRUCTIBLE` 结果都不得进入正式回测、训练、校准、模型晋级或投资表现声明。

## 2. 修改范围

### 网络与 Provider

```text
stock_tracker/collector/provider.py
stock_tracker/collector/eastmoney.py
stock_tracker/collector/tencent.py
```

- 新增 exact-raw 专用研究 HTTP 通道；
- 使用系统 CA 和 hostname verification；
- 禁止 Proxy、Redirect、Host Override、非默认 HTTPS Port；
- 验证最终 URL、Status、Content-Type、Content-Length、实际长度和最大响应体；
- Eastmoney/Tencent 严格 JSON Parser 拒绝重复 Key、非有限数值和损坏 Row；
- Tencent 只声明 `qfq`，缺 `qfqday` 时不能回退到未复权 `day`。

### Artifact / Golden / Reconciliation

```text
stock_tracker/quant/data/bar_artifact.py
stock_tracker/quant/data/market_bar_golden.py
stock_tracker/quant/data/market_bar_reconciliation.py
stock_tracker/quant/data/__init__.py
```

- `CapturedBarArtifact` 内存身份重新计算；
- Synthetic Pack 的 Raw/Source/Case/Pack Identity；
- Parser Schema/Version Binding；
- Immutable `MarketBarPoint`；
- OHLC/Volume/Amount/Turnover 字段证据；
- BPS 容差与跨源 Session Comparison；
- Calendar expected/observed/missing/unexpected coverage；
- `HARD_BLOCKED / STRUCTURALLY_CONSTRUCTIBLE`；
- JSON/Markdown 内容寻址、不可变报告文件；
- 递归禁止 Trust/Promotion 字段。

### CLI

```text
scripts/capture_quant_bars.py
scripts/report_stage2g_market_bars.py
```

`capture_quant_bars.py` 现支持：

```text
--provider eastmoney
--provider tencent
```

默认仍是 Eastmoney。Tencent 只接受 `qfq`。两者均保持：

```text
BEST_EFFORT
verified=false
synthetic_fixture=false
research_grade=false
production_database_modified=false
```

`report_stage2g_market_bars.py` 完全离线处理 committed synthetic pack，输出路径按：

```text
<output-dir>/<case-name>/<report-id>.json
<output-dir>/<case-name>/<report-id>.md
```

### Fixture

```text
tests_quant/fixtures/market_bar_golden/v1/
├── manifest.json
├── a/600519_eastmoney.json
├── a/600519_tencent.json
├── hk/00700_eastmoney.json
├── hk/00700_tencent.json
├── us/AAPL_eastmoney.json
└── us/AAPL_tencent.json
```

Fixture 是 vendor-shaped synthetic envelope，不是真实历史抓取。

### 测试与发布门禁

```text
tests/test_provider_research_request.py
tests/test_bars_provider.py
tests/test_provider_bars.py
tests/test_tencent_bars.py

tests_quant/test_bar_artifact_capture.py
tests_quant/test_capture_quant_bars_cli.py
tests_quant/test_market_bar_reconciliation.py
tests_quant/test_stage2g_market_bar_cli.py
tests_quant/test_source_distribution.py
```

## 3. 关键失败关闭合同

### 3.1 网络

以下全部拒绝：

```text
HTTP
userinfo
fragment
custom HTTPS port
Host override
HTTP(S) Proxy
redirect
final URL changed
missing/HTML/unsupported Content-Type
invalid/oversized/mismatched Content-Length
oversized or empty Body
```

旧 Runtime Quote `_request()` 尚未整体迁移；只有 exact-raw 研究入口保证以上边界。

### 3.2 Parser

- 任何 strict row 损坏都会拒绝完整 Capture；
- Eastmoney/Tencent 拒绝重复 JSON Key、`NaN/Infinity`；
- OHLC 必须有限、正数且一致；
- Volume/Amount/Turnover 不能为负；
- 日期必须唯一且严格递增；
- Tencent QFQ 不得降级为 `day`。

### 3.3 Artifact Identity

重新验证：

```text
Trust Tier cap
request parameters
normalized rows
row count
content bounds
normalized dataset ID
descriptor key
capture ID
```

防止：

```text
dataclasses.replace
nested request mutation
mutable Bar mutation
partial ID recomputation
duplicate-key descriptor
parser/artifact version drift
```

### 3.4 Reconciliation

HARD_BLOCK：

```text
market/symbol/interval/adjustment mismatch
field conflict beyond tolerance
missing expected open session
bar on Calendar-closed session
same-day or future daily session not final as_of
artifact retrieved after as_of
```

固定 Trust Blocker：

```text
CALENDAR_BINDING_NOT_INDEPENDENTLY_VERIFIED
RECONCILIATION_POLICY_NOT_INDEPENDENTLY_APPROVED
SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED
MARKET_BAR_FIELD_UNIT_POLICY_UNVERIFIED
ADJUSTMENT_POLICY_EQUIVALENCE_UNVERIFIED
MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED
SYNTHETIC_MARKET_BAR_EVIDENCE / LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED
LICENSE_PENDING
T3_NOT_REACHED
```

Policy 不能关闭 Open Session Coverage 或 License Gate。

## 4. 当前验证证据

最终发布门禁：

```text
Git checkout Stage 2G / Provider focused:
80 passed + 32 subtests

Git checkout Runtime full unittest:
517 passed, 1 expected live-probe skip

Git checkout Quant full:
594 passed + 277 subtests

Source distribution / no tracked bytecode:
3 passed + 63 subtests

Exact Git Index export:
Stage 2G / Provider 80 passed + 32 subtests
Runtime 517 passed, 1 skipped
Quant 592 passed, 2 expected no-.git skips + 214 subtests

Hybrid H0:
12/12

Hybrid H1/H2:
28/28 main + 11/11 negative

Hybrid H4:
18/18

Monitor Workspace:
49/49

Mock Today:
17/17

Real Today:
17/17

Portfolio CRUD:
13/13

compileall / targeted Ruff / pip check / git diff --cached --check:
PASSED

Index generated-file and secret scan:
578 tracked files / 36 staged files
0 forbidden generated paths
0 staged credential findings

Quant contract smoke:
PASSED / synthetic_fixture_only=true

Quant synthetic benchmark:
PASSED / investment_performance_claim=false
Challenger not promoted: ECE_REGRESSED, TIME_INSTABILITY

Stage 2G CLI:
A/HK/US all STRUCTURALLY_CONSTRUCTIBLE
0 HARD_BLOCK per committed case
11 TRUST_BLOCK findings per case
T3_NOT_REACHED / LICENSE_PENDING remain open
```

## 5. 生产数据库并行写入说明

本轮开始时生产数据库 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

后续发现两个既有 Engine 持续持有并写入同一 DB/WAL：

```text
PID 55468: python -m stock_tracker --host 127.0.0.1 --port 8080
started 2026-08-28T07:59:37Z

PID 52008: python -m stock_tracker --host 127.0.0.1 --port 8090
started 2026-08-28T08:08:29Z
```

它们均在本轮 Stage 2G 验证前启动。Stage 2G CLI、专项测试和 H0 验收未写生产 DB；H0 自身的 before/after hash 相同。但由于上述并行 Engine 持续写 WAL，无法诚实声称“本轮全局时间窗内生产数据库 Hash 不变”。

本轮采用的发布证据：

- Stage 2G CLI 明确不打开生产 DB；
- CLI 单测逐次比较生产 DB before/after；
- 所有写测试使用临时目录/临时数据库；
- 从生产 DB 只读备份得到一致 Snapshot；
- Quant Migration 在该 Snapshot 上 dry-run：`database_modified=false`，4 个 migration pending；
- 最终合并证据将来自精确 Git Index 导出，不依赖运行中生产 DB。

没有终止、重启或修改这两个既有 Engine。

## 6. 不在本次范围

```text
真实 Eastmoney/Tencent 双源 Capture 验收
真实源法律/血缘独立性证明
字段单位、币种、成交量/成交额口径证明
QFQ 数值等价证明
权威 Calendar/Status/Universe/Corporate Action
License clearance authority
T3 Snapshot promotion
模型训练/回测/校准
自动交易
```

## 7. 下一步

1. 对 A/HK/US 各选小窗口执行真实 Eastmoney/Tencent exact-raw Capture；
2. 固定真实 Raw/Parser/Schema 版本和字段单位；
3. 审计许可、长期留存、训练和再分发；
4. 绑定权威 Calendar/Status/Universe/Corporate Action；
5. 运行真实 Coverage/Conflict 报告；
6. 设计独立 Policy/License/Source Verification Authority；
7. 只有所有 Blocker 有独立证据关闭后，再设计首个 T3 Snapshot。
