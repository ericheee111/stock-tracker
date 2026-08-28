# Stage 2G Market-Bar Golden Payload and Reconciliation Contract

> 日期：2026-08-28
>
> 工程状态：`IMPLEMENTED / SYNTHETIC_VALIDATED`
>
> 数据证据：`CONTRACT_ONLY / T1_BEST_EFFORT_INPUTS`
>
> 许可状态：`LICENSE_PENDING`
>
> 研究级状态：`T3_NOT_REACHED`

## 1. 目标

Stage 2G 建立 A/HK/US 日线原始响应的版本化 Golden Fixture、exact-raw 捕获入口、跨源字段比较和 Calendar Session 覆盖缺口报告。它解决的是：

```text
Provider exact bytes
→ deterministic strict parser
→ content-addressed artifact
→ immutable normalized identity
→ cross-source field comparison
→ Calendar-open coverage gaps
→ explicit blockers
```

本阶段不负责，也不得声称：

- 已获得交易所权威历史行情；
- 已证明 Eastmoney 与 Tencent 是法律或数据血缘意义上的独立来源；
- 已证明两源成交量、成交额、换手率和复权口径完全等价；
- 已完成来源许可、长期留存、训练或再分发授权；
- 已绑定权威、完整、可修订的 Calendar/Status/Universe/Corporate Action；
- 已达到 `T3 RESEARCH_GRADE`；
- 已得到任何真实策略收益、成功率、Sharpe 或回撤证据。

## 2. 交付文件

```text
stock_tracker/collector/provider.py
stock_tracker/collector/eastmoney.py
stock_tracker/collector/tencent.py

stock_tracker/quant/data/bar_artifact.py
stock_tracker/quant/data/market_bar_golden.py
stock_tracker/quant/data/market_bar_reconciliation.py
stock_tracker/quant/data/__init__.py

scripts/capture_quant_bars.py
scripts/report_stage2g_market_bars.py

tests_quant/fixtures/market_bar_golden/v1/**
tests_quant/test_bar_artifact_capture.py
tests_quant/test_capture_quant_bars_cli.py
tests_quant/test_market_bar_reconciliation.py
tests_quant/test_stage2g_market_bar_cli.py
tests/test_provider_research_request.py
```

## 3. Exact-raw 网络边界

Eastmoney 与 Tencent 的 raw-bar 抓取不再使用旧 Runtime Quote 通道的跳过证书校验逻辑，而使用独立研究请求：

```text
HTTPS only
system CA verification
hostname verification
no inherited HTTP(S)_PROXY
no redirect
no Host override
exact final URL
bounded Content-Length
bounded response bytes
HTML error page rejection
JSON/text content-type allowlist
```

旧 Runtime Quote 行为没有在本阶段整体迁移；本合同只保证 `fetch_bars_raw()` 的研究采集边界。

## 4. Provider 合同

### 4.1 Eastmoney

```text
endpoint: push2his.eastmoney.com/api/qt/stock/kline/get
interval: 1d
adjustment: qfq / hfq / raw
raw schema: eastmoney-kline-f51-f58-v1
parser: eastmoney-bars-v2-raw-split
```

### 4.2 Tencent

```text
endpoint: web.ifzq.gtimg.cn/appstock/app/fqkline/get
interval: 1d
adjustment: qfq only
raw schema: tencent-fqkline-qfqday-v1
parser: tencent-bars-v2-raw-split
```

Tencent 严格解析必须存在 `qfqday`。请求 `qfq` 时不得静默回退到 `day`，否则会把未复权数据冒充前复权数据。

严格解析器均拒绝：

- 空响应；
- 非 UTF-8；
- 重复 JSON Key；
- `NaN` / `Infinity`；
- 非对象顶层；
- 非数组 Rows；
- 非法日期；
- 非有限或非正 OHLC；
- OHLC 逻辑不一致；
- 负成交量/成交额；
- 重复或非严格递增交易日；
- 任一损坏 Row。

运行 Parser 可以跳过孤立坏行；正式 Artifact 捕获必须使用严格 Parser。

## 5. Golden Pack v1

Committed Fixture：

```text
A  : 600519.SH
HK : 00700.HK
US : AAPL.US
```

每个 Case 包含两个 vendor-shaped synthetic envelope：

```text
Eastmoney
Tencent
```

每个 Raw File 绑定：

```text
raw SHA-256
source
source family
source dataset
provider version
schema version
parser version
endpoint
comparable fields
license status
source ID
```

Case 绑定：

```text
market
symbol
interval
adjustment
synthetic Calendar snapshot ID
expected open sessions
source IDs
case ID
```

Pack 绑定全部 Case、固定 `retrieved_at`、`synthetic_fixture=true` 与 Pack ID。Raw、Source ID、Case ID、Pack ID 或 Schema 任一变化都会失败关闭。

Golden Pack 是 **synthetic vendor-shaped fixture**。它不是某日真实 Provider 响应，也不能通过修改布尔值或重算部分 ID 被改写为真实证据。

## 6. Immutable Capture 加固

`validate_captured_market_bars()` 会从内存对象重新计算：

```text
Trust 上限
request parameters
normalized rows
row count
content bounds
normalized dataset ID
descriptor key
capture ID
```

这用于阻止：

- `dataclasses.replace()` 自我提高 Trust；
- 修改嵌套 `request_parameters`；
- 修改可变 `Bar`；
- 重算部分 ID 后伪造 Capture；
- Descriptor 重复 JSON Key；
- Parser/Artifact Version 漂移。

`MarketBarSeriesEvidence` 构造时把可变 `Bar` 转为不可变 `MarketBarPoint`，报告之后不再读取调用方可变 Capture 内容。

## 7. Reconciliation

### 7.1 可比较字段

v1 Golden Case 只声明：

```text
OPEN
HIGH
LOW
CLOSE
VOLUME
```

`AMOUNT` 与 `TURNOVER` 仍出现在证据中，但在没有字段单位/币种/供应商口径证明前标记为 `NOT_COMPARABLE`。

### 7.2 容差

默认 Policy：

```text
minimum_independent_sources = 2
price_tolerance_bps         = 5
volume_tolerance_bps        = 50
amount_tolerance_bps        = 100
turnover_tolerance_bps      = 100
require_all_open_sessions   = true
require_license_clearance   = true
```

Policy 全字段进入 `policy_id` 和 `report_id`。调用方不能只改变阈值而保持报告身份不变。

### 7.3 Coverage

报告显式保存：

```text
expected sessions
observed union sessions
fully observed sessions
missing sessions by series
unexpected closed sessions by series
```

默认情况下：

- 缺 Calendar-open Session：`HARD_BLOCK`；
- Calendar-closed Session 出现 Bar：`HARD_BLOCK`；
- 与 `as_of` 同日或未来的 Daily Session 尚未最终形成：`HARD_BLOCK`；
- Symbol/Market/Interval/Adjustment 不同：`HARD_BLOCK`；
- 字段超过容差：`HARD_BLOCK`。

### 7.4 固定 Trust Blockers

Stage 2G 保留：

```text
CALENDAR_BINDING_NOT_INDEPENDENTLY_VERIFIED
SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED
MARKET_BAR_FIELD_UNIT_POLICY_UNVERIFIED
ADJUSTMENT_POLICY_EQUIVALENCE_UNVERIFIED
MARKET_BAR_ARTIFACT_NOT_INDEPENDENTLY_VERIFIED
SYNTHETIC_MARKET_BAR_EVIDENCE 或 LIVE_MARKET_BAR_PROVENANCE_NOT_INDEPENDENTLY_ATTESTED
LICENSE_PENDING
T3_NOT_REACHED
```

同名不同 Source Capture 也不能自动证明来源独立性。`source_family` 必须等于 Capture 自身 `source`，不能由调用方任意改名制造“多源”。

## 8. 报告状态

只允许：

```text
HARD_BLOCKED
STRUCTURALLY_CONSTRUCTIBLE
```

`STRUCTURALLY_CONSTRUCTIBLE` 仅表示当前字段、日期、身份和覆盖没有硬冲突，不表示 Trust 已通过。

报告输出递归禁止：

```text
verified
complete
trust_tier
research_grade
t3_achieved
```

## 9. CLI

### 9.1 真实 exact-raw 捕获

Eastmoney：

```powershell
python scripts/capture_quant_bars.py `
  --provider eastmoney `
  --symbol 600519.SH `
  --market A `
  --start 2024-01-01 `
  --end 2024-12-31 `
  --adjust qfq
```

Tencent：

```powershell
python scripts/capture_quant_bars.py `
  --provider tencent `
  --symbol 600519.SH `
  --market A `
  --start 2024-01-01 `
  --end 2024-12-31 `
  --adjust qfq
```

Tencent 的 `raw/hfq` 会失败关闭。两种捕获都保持：

```text
BEST_EFFORT
verified=false
synthetic_fixture=false
research_grade=false
production_database_modified=false
```

### 9.2 Synthetic Golden 报告

```powershell
python scripts/report_stage2g_market_bars.py
```

默认输出到被 Git 忽略的：

```text
data/stage2g-market-bar-artifacts/
data/stage2g-market-bar-reports/
```

命令会按 `<output-dir>/<case-name>/<report-id>.json|md` 生成内容寻址、不可变报告，并输出汇总：

```text
synthetic_fixture_only=true
source_verification_complete=false
license_clearance_complete=false
t3_reached=false
research_grade=false
production_database_modified=false
```

输出目录不得与 committed Fixture Tree、彼此或生产数据库重叠。

## 10. 证据等级与下一阶段

Stage 2G 完成后，准确状态是：

```text
ENGINEERING_CONTRACT = IMPLEMENTED
SYNTHETIC_GOLDEN = VALIDATED
CROSS_SOURCE_RECONCILIATION = IMPLEMENTED
REAL_SOURCE_RECONCILIATION = NOT_YET_ACCEPTED
LICENSE = PENDING
T3 = NOT_REACHED
```

下一阶段必须补：

1. 真实 A/HK/US 小窗口 exact-raw 双源捕获；
2. 来源许可、长期留存、训练和再分发审计；
3. 字段单位、币种、复权和交易日语义证明；
4. 权威 Calendar/Status/Universe/Corporate Action 绑定；
5. 真实 Coverage 与 Conflict 报告；
6. 独立批准的 Trust 晋级证据；
7. 只有全部门禁通过后，才评估 T3 Snapshot 组装。
