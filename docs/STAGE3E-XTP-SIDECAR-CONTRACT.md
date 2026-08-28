# Stage 3E — XTP Read-only Sidecar 合同

> 状态：`ENGINEERING_IMPLEMENTED_AND_REVIEWED`
>
> 真实官方 SDK/账户验收：`PENDING`

## 1. 组件

```text
sidecars/xtp/contracts.py
sidecars/xtp/runtime.py
sidecars/xtp/server.py
sidecars/xtp/official.py
sidecars/xtp/run.py
stock_tracker/collector/xtp_sidecar.py
config/xtp_sidecar.toml
scripts/run_xtp_sidecar.py
scripts/verify_xtp_sidecar.py
```

Sidecar 为独立 Python 3.9 标准库进程。主应用通过 `stock_tracker.collector.xtp_sidecar.XtpSidecarClient` 读取事件；主运行 Quote/BAR Router、Scheduler 和决策链不会自动启用它。

## 2. IPC

只允许字面 IPv4 loopback 地址：

```text
GET /v1/health
GET /v1/session
GET /v1/metrics
GET /v1/events?after=<cursor>&limit=<bounded>
```

除可配置的公开 metadata-only Health 外，其余端点要求独立 Bearer。所有写方法返回 `405 READ_ONLY_SIDECAR`。

约束：

- 不继承系统 HTTP/HTTPS Proxy；
- 禁止 Redirect，响应 URL 必须与请求的 loopback Scheme/Host/Port/Path/Query 完全一致；
- 响应最大 8 MiB；
- Query 字段数量、Cursor 和 Limit 有界；
- JSON 拒绝重复 Key、NaN 和 Infinity；
- 服务不输出账户标识、密码、行情地址或 Sidecar Bearer；
- Sidecar 不提供 CORS，也不供浏览器直接访问。

## 3. 事件信封

```text
schema
event_id
source
feed_mode
market
symbol  # CODE.SH / CODE.SZ 后缀承载当前事件合同中的交易所身份
event_type
trading_day
exchange_timestamp
provider_timestamp
received_at
session_id
callback_seq
provider_seq
raw_payload_sha256
payload
```

身份绑定 payload hash、Session 与回调序列。以下情况失败关闭：

- 非 A 股 Symbol；
- 无时区时间；
- `trading_day` 与 UTC+08:00 A 股 civil date 不一致；
- Exchange/Provider 时间晚于 `received_at` 超出容忍窗口；
- boolean 冒充 sequence；
- 超出 signed 64-bit 的整数；
- 非有限价格；
- 负数或非整数累计成交量、负累计成交额；
- 重复 JSON Key、非法 Object Key 或嵌套集合超限；
- Event ID 或 payload hash 不一致；
- Payload 在构造后被修改；
- Health/Session/Metrics/Events 的 Session、Backend、Feed Mode 或连接状态不一致；
- 缓冲区淘汰导致 Cursor 丢失。

## 4. 指标语义

```text
callback_count
duplicate_count
callback_gap_count
provider_gap_count
out_of_order_count
reconnect_count
disconnect_count
dropped_buffer_count
latency_p50_ms
latency_p95_ms
```

`callback_gap_count` 是 Sidecar 本地回调序列缺口；只有 Provider Sequence 存在时才计算 `provider_gap_count`。

## 5. Backend

### Simulator

默认 Backend，用于确定性工程验收，不构成真实行情证据。

### Official XTP

`official.py` 当前只实现：

- Python 3.9 ABI 检查；
- 环境变量严格校验；
- Quote 模块白名单加载；
- Trader/Order/Algo 模块拒绝；
- 安全能力探针：缺少 Quote Factory 或暴露 Trader/Algo/可执行 Order 表面时硬失败；
- 回调标准化桥与有限性、价格、累计成交量/成交额单位校验。

真实 Quote Login/Subscribe Adapter 未随本阶段冒充完成；`--backend xtp` 在探针后保持失败关闭，直到取得官方 SDK、账户环境和独立 operational Review。

## 6. 验收

```text
python scripts/verify_xtp_sidecar.py
```

验证 Simulator、IPv4 loopback、Bearer、只读方法、绝对请求目标拒绝、精确响应 URL、事件游标、Session 快照、Payload 深度隔离、指标、秘密不序列化和生产数据库 SHA 不变。当前宿主没有 CPython 3.9，已用 Python 3.9 grammar parse 验证六个 Sidecar 文件；真实 3.9 运行与官方二进制加载仍属于 operational 验收。

最终状态：

```text
SIDECAR_PROTOCOL = PASSED
SIMULATOR_ACCEPTANCE = PASSED
OFFICIAL_MODULE_PROBE = IMPLEMENTED
REAL_XTP_LOGIN_AND_SUBSCRIPTION = PENDING
ALGORITHM_ACCOUNT_USED = FALSE
AUTO_TRADE = FALSE
```
