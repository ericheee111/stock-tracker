# HiThink Financial-API 接入合同

> 状态：`IMPLEMENTED_OPTIONAL / LIVE_KEY_ACCEPTANCE_PENDING`
>
> 审计日期：2026-08-26
>
> 上游：`HiThink-Tech/Financial-API`

## 1. 结论与定位

该仓库适合本项目，但首期只作为**可选的同花顺官方 A 股日线原始数据捕获源**接入，不替换腾讯实时主源，不进入 HOT/WARM/COLD 运行决策，也不因“官方 API”自动升级为 T2/T3。

采用直接 REST Adapter，而不是把上游 Python SDK、CLI 或 DuckDB marketdb 整体复制进仓库，原因是：

- 当前运行时坚持标准库优先；
- Provider 必须可替换、可熔断、可做原始字节留存；
- 只需要一个窄接口即可完成首期证据收集；
- 大规模全市场建库以后应单独评估 Market Dumps / marketdb，不应逐股票循环请求多年 REST。

当前接入文件：

```text
stock_tracker/collector/hithink_finance.py
scripts/capture_hithink_bars.py
tests/test_hithink_finance_provider.py
tests_quant/test_capture_hithink_bars_cli.py
```

## 2. 当前支持范围

```text
市场：A 股
接口：GET /api/a-share/prices/historical
周期：1d
复权：raw/none → none；qfq → forward；hfq → backward
窗口：单标的、最长 10 年
保存：exact JSON bytes + content-addressed T1 artifact
```

当前明确不接入：

```text
实时 Quote / Snapshot 路由
分钟 K / Tick
港股 / 美股
财务报表进入生产决策
交易日历直接替换 PIT Calendar
公司行为直接升级为权威复权链
模型训练、校准或晋级
公开数据再分发
```

## 3. 安全合同

Adapter 固定：

- 官方 Base Origin：`https://fuyao.aicubes.cn`；
- 系统 CA/TLS 校验；
- 禁用环境代理继承；
- 禁止 HTTP Redirect；
- 只允许历史日线固定路径；
- 响应大小上限 16 MiB；
- HTTP 200 后仍严格检查业务 `code == 0`；
- 不把请求 Header、原始响应、Key 或上游错误正文写入日志；
- 仅保留受限 `request_id` 便于官方排障。

凭据只能放在进程环境变量：

```text
HITHINK_FINANCE_API_KEY
```

禁止把值写入：

```text
config/providers.toml
.env 或任何仓库文件
Runtime Config
浏览器代码
命令行参数
日志、错误响应、测试 Fixture
Git commit
```

## 4. 在哪里填写 API Key

### PowerShell：仅当前终端会话

```powershell
$hithinkKey = Read-Host "Paste HiThink API key"
$env:HITHINK_FINANCE_API_KEY = $hithinkKey
Remove-Variable hithinkKey
```

这样不会把 Key 明文写进 PowerShell 历史；关闭该窗口后环境值失效。

### Windows：持久化到当前用户环境

```powershell
$hithinkKey = Read-Host "Paste HiThink API key"
[Environment]::SetEnvironmentVariable(
  "HITHINK_FINANCE_API_KEY",
  $hithinkKey,
  "User"
)
Remove-Variable hithinkKey
```

设置后重新打开终端。若 Engine 由 Task Scheduler 启动，需要重新登录或重启受监督任务，使新环境生效。

**不要把 Key 发到聊天中，也不要填入 `config/providers.toml`。**

## 5. 首次验证

`config/providers.toml` 中必须继续保持：

```toml
name = "hithink_finance"
enabled = false
```

这会阻止普通 Engine 启动时实例化该 Provider。专用捕获命令本身就是显式启用边界：它只在当前进程中复制配置并临时设置 `enabled=true`，不会改变 TOML，也不会把该源加入 Runtime Quote/BAR Router。

设置好环境变量后，执行一次小窗口原始数据捕获：

```powershell
python scripts/capture_hithink_bars.py `
  --symbol 600519.SH `
  --start 2024-01-01 `
  --end 2024-03-31 `
  --adjust raw `
  --output-root data/quant-artifacts
```

成功输出必须满足：

```text
trust_tier = BEST_EFFORT
research_grade = false
production_database_modified = false
credential_in_output = false
```

原始响应和 descriptor 会写入 `data/quant-artifacts/`；该目录属于本地 Artifact，不应加入 Git。

## 6. 为什么默认仍是 T1

技术可访问不等于具备正式研究资格。升级前还必须补齐：

1. 当前账户的存储、研究和再分发授权边界；
2. 真实标的覆盖率、历史窗口、停牌和退市样本检查；
3. 数据修订、回补和重复请求一致性；
4. 与交易所/法定披露或第二独立源的 reconciliation；
5. Calendar、Historical Universe、Security Status、Corporate Action 与 Revision Policy；
6. `known_at <= usable_from <= as_of` 的 PIT 时间证据；
7. 代表性 50—100 标的 golden payload 与数值差异矩阵。

在以上证据完成前：

```text
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
trust_tier = T1_BEST_EFFORT
```

这些字段由 Provider 构造器再次强制校验，不能仅修改 TOML 绕过。

## 7. 后续阶段

```text
HITHINK-H1  当前：单标的 exact raw 日线捕获
HITHINK-H2  真实 Key 小窗口验收与错误码/限流观察
HITHINK-H3  50—100 标的跨源 reconciliation 与覆盖报告
HITHINK-H4  公司行为、Calendar、Universe、Status 的独立用途审计
HITHINK-H5  评估 Market Dumps/marketdb 的全市场批量回填
HITHINK-H6  只有证据满足后，才讨论 T2/T3 或 Shadow 用途
```

任何阶段都不得把同花顺特色数据、财务指标或热榜直接变成买卖分数，也不得绕过现有 ActionState、Risk Gate、概率和模型晋级门禁。
