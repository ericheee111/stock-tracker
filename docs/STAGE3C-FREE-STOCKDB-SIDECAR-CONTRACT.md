# Stage 3C：free-stockdb 本地行情 Sidecar 合同

> 冻结日期：2026-08-19
> 上游项目：`hello245m/free-stockdb`
> 本项目角色：可选、隔离、默认关闭的 A 股 WARM/COLD 行情 Sidecar
> 当前证据状态：`CONTRACT_ONLY / SYNTHETIC_LOCAL_HTTP_VALIDATED`
> 信任边界：`T1_BEST_EFFORT / LICENSE_PENDING / T3_NOT_REACHED`

## 1. 为什么接入

`free-stockdb` 的核心价值不是替代 `stock-tracker` 的数据治理，而是降低全市场批量读取的工程成本。它把日线、分钟线和部分板块/指标能力放在本地服务中，适合以下场景：

```text
WARM：观察池和 Big Trend 候选的批量原始行情读取
COLD：全市场扫描、离线回填、Provider 断线后的本地缓存
EOD：与现有远程 Provider 的日终差异检查
PoC：分钟线覆盖、吞吐和本机资源消耗评估
```

首版明确不用于：

```text
HOT 实时主行情
自动产生强执行信号
正式 PIT 回测
模型训练、概率校准或 Champion 晋级
历史 Universe / 证券身份重建
正式公司行为与复权因子
历史板块成分回填
公网数据再分发
```

## 2. 上游能力与未闭环事项

截至本合同冻结时，上游 README 和 Releases 描述了本地日 K、分钟 K、ETF/tick、复权、批量查询、指标与板块映射能力；默认 HTTP 服务监听 `127.0.0.1:7899`，同步源由 `sync_url.txt` 或等价参数控制。公开 Release 最新页面显示 `v0.2.1` 测试发行版。

这些描述只能证明“上游宣称提供什么”，不能证明某个发行包、镜像或数据快照满足本项目的金融正确性要求。仍需分别确认：

- 实际下载资产及全部可执行文件 SHA-256；
- 发行包是否与公开源码一致；
- 首次启动和数据更新的真实网络行为；
- 实际同步镜像、数据来源、许可和再分发条款；
- 日线/分钟线的字段、单位、时区、停牌、退市和代码变更语义；
- 复权因子是否有原始公告、`known_at`、`usable_from` 和 revision chain；
- 板块成员是否有历史有效区间和 PIT 时间。

上游软件许可证与数据版权不是同一件事。即使软件代码可以使用，也不能据此推断同步数据可以公开展示、再分发或进入 T3 研究链。

## 3. 架构定位

```text
                     Internet / upstream mirrors
                                │
                     free-stockdb updater
                                │
                 独立目录、独立低权限进程、独立 data/
                                │
                  127.0.0.1:7899，禁止公网暴露
                                │
              FreeStockDbProvider（只读、有限查询、RAW）
                                │
                   ProviderRouter 的 RAW Bar 路由
                                │
          WARM/COLD Scanner / shadow reconciliation / PoC report
                                │
             仍须经过 stock-tracker 自有 PIT/身份/质量合同
```

`free-stockdb` 的数据目录、进程和更新器不得并入：

```text
stock-tracker/data/stock_tracker.db
stock_tracker.quant.storage
用户 Portfolio / Position 数据
私有 API token 或环境密钥目录
```

## 4. 配置合同

配置位于 `config/providers.toml`，默认关闭：

```toml
[[providers]]
name = "free_stockdb"
cls = "FreeStockDbProvider"
markets = ["a"]
enabled = false
primary = false
supports_snapshot = false
host = "127.0.0.1:7899"
bars_fallback = false
bars_priority = 30
read_only = true
trust_tier = "T1_BEST_EFFORT"
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
release_version = ""
binary_inventory_sha256 = ""
data_snapshot_manifest_sha256 = ""
sync_manifest_sha256 = ""
```

启用时必须满足：

1. `host` 是字面量 loopback IP；首版不接受 DNS 名、局域网 IP、HTTPS URL、凭据、path、query 或 fragment；
2. 只允许 A 股；
3. `primary=false`，不能参加 HOT Quote 主源选择；
4. `supports_snapshot=false`，不能参加当前全市场实时 Snapshot 路由；
5. `read_only=true`；
6. 信任等级固定为 `T1_BEST_EFFORT`；
7. live decision、model training、public redistribution 三个开关必须全部为 `false`；
8. 必须填写非空 release version；
9. 必须填写实际 binary、data snapshot、sync manifest 的 lowercase SHA-256。

调用方把任一限制改宽时，Provider 构造必须失败关闭。

## 5. HTTP 查询合同

Stage 3C.1 只使用公开的只读查询形式：

```text
GET /?cmd=get&t=日k:<6位代码>:<日期或日期范围>
GET /?cmd=get&t=分钟k:<6位代码>:<时间或时间范围>
```

禁止：

```text
cmd=set
任何写接口
HTTP redirect
非 loopback 最终 URL
不受限的时间区间
未公开且未经版本化验证的响应结构
```

当前支持：

| 项目 | 合同 |
|---|---|
| 市场 | A 股 |
| 规范代码 | `CODE.SH` / `CODE.SZ`，代码严格六位数字 |
| 周期 | `1d`、`1m` |
| 复权 | 仅 `raw` / `none` |
| 日线最大单次范围 | 3660 日 |
| 分钟线最大单次范围 | 31 日 |
| 响应上限 | 32 MiB |
| 字符集 | strict UTF-8 |
| JSON | 禁止重复 key、NaN、Infinity |

`qfq` 和 `hfq` 请求必须过滤该 Provider，不能用 RAW Bar 冒充复权行情。

## 6. Bar 解析合同

首版接受三种已验证的 JSON 形态：

```text
单条 bar object
bar object array
按日期/时间为 key、bar object 为 value 的 mapping
```

日线最少要求：

```text
code
date
open
high
low
close
volume
amount
turnover
```

分钟线最少要求：

```text
code
date
open
high
low
close
volume
amount
```

规则：

- `code` 必须等于请求代码；
- 日线 `date` 是八位日期；分钟线是十四位时间；
- 日线时间规范化为 `Asia/Shanghai` 15:00；
- 分钟线保留 `Asia/Shanghai` 精确分钟时间；
- OHLC 必须为有限正数且相互一致；
- `volume` 必须是非负 JSON integer；
- `amount`、`turnover` 必须有限且非负；
- 重复时间戳失败关闭；
- 响应中的 Bar 不得越过请求的 start/end；
- 输出 `adjustment_factor=1.0`；
- 输出 `quality_status=UNKNOWN`，不得因本地读取成功就标为 LIVE 或 VALID；
- 分钟响应未提供 turnover 时暂填 `0.0`，同时保持 `UNKNOWN`，不得把它解释为真实零换手。

## 7. 读取证据

每次成功读取可产生 `FreeStockDbReadEvidence`：

```text
provider
release_version
binary_inventory_sha256
data_snapshot_manifest_sha256
sync_manifest_sha256
response_sha256
queried_at
symbol
interval
adjustment_mode
request_url
evidence_id
```

`evidence_id` 由全部字段确定性派生。它只能说明：

> 某个声明并固定的本地发行版和数据快照，在某次 loopback 查询中返回了某些 exact response bytes。

它不能说明：

```text
数据来源权威
许可已清除
历史时点可知
Universe 完整
公司行为正确
适合训练或正式回测
达到 T2/T3
```

## 8. 路由规则

`ProviderRouter` 新增：

```text
supports_adjustment(adjust)
bars_priority
```

首版路由语义：

- 显式请求 `adjust=raw` 时，启用且健康的 free-stockdb 可以凭 `bars_priority` 优先于远程源；
- 请求 `qfq/hfq` 时，free-stockdb 必须被过滤；
- Sidecar 返回空结果时可以继续尝试其他 Bar 源；
- Sidecar 异常进入现有 Health/Circuit 统计；
- 不改变腾讯、东财、新浪的 Quote/Snapshot 主备结构；
- 不因 Sidecar 本地可达而改变产品 DataStatus。

## 9. 安全边界

### 9.1 进程隔离

真实安装必须：

- 放在仓库外的独立目录；
- 使用独立低权限 OS 用户或等价限制；
- 禁止读取 `.env`、Portfolio 数据库和项目日志；
- 默认只监听字面量 `127.0.0.1`；
- 防火墙阻止 7899 的公网和局域网入口；
- 更新器与查询服务分时运行；
- 保存发行资产和更新器 SHA-256 清单。

### 9.2 网络审计

首次启动和首次同步应记录：

```text
process image hash
parent/child process
DNS queries
remote IP/port
HTTP Host/SNI
下载文件名、大小和 SHA-256
sync manifest bytes/hash
最终 data snapshot identity
```

发现设备校验、未声明远端、自动上传、遥测、凭据读取或绕过 `sync_url` 的行为时，PoC 立即失败关闭。

### 9.3 公网边界

不得把 7899 暴露给互联网。公网访问只能是：

```text
authenticated stock-tracker API
        ↓
server-side bounded query policy
        ↓
loopback free-stockdb
```

Stage 3C.1 甚至不提供 stock-tracker 公网 API，仅提供本地 Provider 和验收 CLI。

## 10. 真实 PoC 验收矩阵

Stage 3C.2 选取 50—100 个代表性标的，覆盖：

```text
沪主板
深主板
创业板
科创板
北交所（若上游代码/市场语义可确认）
ETF
ST / *ST
停牌
退市样本
代码或简称变化
现金分红
送转
配股
大幅跳空
高成交额/低流动性
分钟缺口与午间休市边界
```

至少比较：

```text
stock-tracker 当前 Provider
交易所/法定披露或权威样本
free-stockdb RAW 日线/分钟线
```

指标：

- 交易日覆盖；
- 缺失、重复、越界 Bar；
- OHLC 差异；
- 成交量与成交额单位；
- 分钟时间戳、午休、集合竞价和收盘时间；
- 停牌日期；
- ST 状态；
- 代码变化与退市保留；
- 更新延迟；
- 原始未复权行情差异；
- qfq/hfq 与本项目正式公司行为链的差异，仅作诊断；
- 板块成员差异，仅用于当前候选诊断。

PoC 报告必须逐项保留 mismatch 和 gap，不允许只报告平均一致率。

## 11. 阶段划分

### Stage 3C.1：隔离合同与本地模拟验收（本轮）

交付：

```text
FreeStockDbProvider
FreeStockDbReadEvidence
ProviderConfig 安全字段
RAW/qfq 路由隔离
verify_free_stockdb_sidecar.py
synthetic localhost HTTP tests
```

状态：

```text
CONTRACT_ONLY
SYNTHETIC_LOCAL_HTTP_VALIDATED
REAL_RELEASE_NOT_AUDITED
REAL_DATA_NOT_VALIDATED
```

### Stage 3C.2：固定发行版与真实数据审计

- 选择明确 release；
- 下载并保存资产哈希；
- 审计安装包文件；
- 监控首次运行网络；
- 固定同步源和 manifest；
- 执行 50—100 标的矩阵；
- 输出机器可读差异报告；
- 决定是否允许用于 WARM/COLD shadow。

### Stage 3C.3：Shadow Scanner 与 EOD Reconciliation

只有 Stage 3C.2 通过后：

- 让 RAW 批量行情参与 Core/Big Trend 候选预筛；
- 当前板块映射只可作非历史 shadow 特征；
- 与远程 Provider 做 EOD 差异检查；
- Sidecar 失败自动回退且不改变正式信号；
- 记录吞吐、延迟、磁盘、更新耗时和错误率。

### Stage 3C.4：研究用途再评估

只有数据许可、来源、历史 Universe、PIT 时间和公司行为证据全部闭环，才讨论将某个数据集从 T1 候选提升至更高等级。该阶段不是当前默认计划，也不能由 Provider 配置自行完成。

## 12. 明确不做

本阶段不：

- vendoring 或 fork 上游二进制；
- 把上游 data 文件提交 Git；
- 自动下载 Release；
- 自动运行更新器；
- 允许 `cmd=set`；
- 使用上游 qfq/hfq 训练模型；
- 使用当前板块映射倒灌历史；
- 对外转发原始数据；
- 修改生产数据库；
- 声称真实 Big Trend、策略或模型准确率提升。

## 13. 退出条件

Stage 3C.1 可合入的工程条件：

```text
默认关闭
只允许 loopback 字面 IP
只读 cmd=get
RAW only
qfq/hfq 不会误路由
所有安全配置严格类型校验
release/data/manifest/binary identity 完整
严格 JSON/字段/数值/时间解析
本地 HTTP 模拟回归通过
无生产数据库修改
独立 Review 无 CRITICAL/IMPORTANT blocker
```

Stage 3C.1 合入不等于允许用户立即开启。真正开启仍需 Stage 3C.2 的真实发行包与数据审计。
