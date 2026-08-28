# Stage 3D — XTP 资格、账户与数据合同审计

> 状态：`ENGINEERING_AUDIT_COMPLETE`
>
> 日期：2026-08-27
>
> 真实账户验收：`PENDING`

## 1. 结论

XTP 适合作为本项目的 **A 股本地低延迟行情 Sidecar**，但不替代历史 PIT 数据、公司行为、历史 Universe、基本面或权威研究快照。首期只接 Quote/Market Data，算法账户、Trader API、订单、撤单、成交与自动交易全部不使用。

用户已注册：

- 股票类型测试账户；
- 算法类型测试账户。

仓库不保存账户标识、密码、行情地址或访问值。算法类型账户在本阶段明确保持未使用。

## 2. ABI 与进程边界

当前冻结合同：

```text
XTP API contract: 2.2.50.8
official Python binary runtime: CPython 3.9.x
stock-tracker main runtime: CPython 3.14
```

因此：

- XTP Python 二进制不得加载进主进程；
- XTP 运行在独立 Python 3.9 或未来独立 C++ Sidecar；
- 主进程只通过 loopback HTTP/JSON 读取标准化事件；
- 仓库不 vendor 上游 SDK、DLL、SO 或账户文件；
- 版本、二进制来源、许可证和可再分发范围在真实部署前仍需复验。

## 3. 环境变量

真实 Quote 验收时，只在本机 Sidecar 进程环境中配置：

```text
STOCK_TRACKER_XTP_QUOTE_USER
STOCK_TRACKER_XTP_QUOTE_PASSWORD
STOCK_TRACKER_XTP_QUOTE_SERVER
STOCK_TRACKER_XTP_QUOTE_PORT
STOCK_TRACKER_XTP_QUOTE_PROTOCOL=TCP
STOCK_TRACKER_XTP_CLIENT_ID
STOCK_TRACKER_XTP_SIDECAR_ACCESS
```

禁止写入：

```text
config/xtp_sidecar.toml
Git
命令行参数
URL
Runtime Config
前端静态文件
日志
QA 输出
聊天记录
```

`STOCK_TRACKER_XTP_QUOTE_SERVER` 必须是测试账户门户提供的字面 IP；端口和 Client ID 必须为有界整数。所有值禁止首尾空白和控制字符。

## 4. 允许能力

```text
Quote 登录前置探针
行情订阅配置
Level 1 / Level 2 feed mode 标识
回调快照标准化
断线/重连与 Session 监控
延迟、重复、乱序与序列可用性统计
本地 Shadow/Reconciliation
```

## 5. 禁止能力

```text
Trader API
Order API
Algo API
报单
撤单
账户资产
持仓同步
成交回报
自动交易
XTP 数据自动进入模型训练
XTP 数据自动升级 Trust Tier
XTP 数据公开再分发
```

## 6. 时间与序列语义

每条事件保留：

```text
exchange_timestamp
provider_timestamp
received_at
session_id
callback_seq
provider_seq
```

其中：

- `callback_seq` 只代表 Sidecar 本地回调顺序；
- 不得称其为交易所序列；
- Provider 没有提供序列时，`provider_seq=null`；
- 只有真实 Provider Sequence 存在时，才能计算 Provider Gap；
- XTP SDK 回调对象是 deterministic callback snapshot，不声称是交易所 wire bytes。

## 7. Trust 与用途

当前固定：

```text
read_only = true
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
auto_trade = false
```

真实账户连接成功也不会自动改变这些值。晋级至少需要：

1. 真实股票 Quote 账户验收；
2. 权限、Level 1/2、时间戳和字段单位复验；
3. 存储/训练/再分发授权边界；
4. 50–100 标的 Shadow；
5. 开盘、午休、收盘、停牌、涨跌停与断线恢复样本；
6. 与当前源的差异报告；
7. 独立安全和金融正确性 Review。

## 8. 当前交付判定

```text
ACCOUNT_CATEGORY_RECORDED = TRUE
CREDENTIALS_PERSISTED = FALSE
ALGORITHM_ACCOUNT_USED = FALSE
TRADER_OR_ORDER_API_INTEGRATED = FALSE
ENGINEERING_QUALIFICATION = COMPLETE
LIVE_XTP_ACCEPTANCE = PENDING
EVIDENCE_TIER_STATUS = T3_NOT_REACHED
```
