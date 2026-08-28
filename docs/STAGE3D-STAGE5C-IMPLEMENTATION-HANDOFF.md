# Stage 3D–5C XTP / Market Event / Monitor 实施交接

> 日期：2026-08-28
>
> 工程状态：`COMPLETE`
>
> GitHub 交付：由 `CHATGPT_HANDOFF.md` 记录最终提交 SHA

## 1. 交付范围

### XTP Sidecar

```text
config/xtp_sidecar.toml
requirements-xtp.txt
sidecars/xtp/*
stock_tracker/collector/xtp_sidecar.py
scripts/run_xtp_sidecar.py
scripts/run_xtp_sidecar.bat
scripts/verify_xtp_sidecar.py
scripts/ingest_xtp_sidecar.py
```

Sidecar 是独立 CPython 3.9 标准库进程；主应用继续运行 CPython 3.14，不加载 XTP DLL/SO。默认 Backend 为 Simulator，正式 Quote Backend 只完成环境/ABI/模块能力探针和回调标准化桥，真实 Login/Subscribe 仍为 operational gate。

### Event Store / Replay / Shadow

```text
stock_tracker/market_events/*
scripts/query_market_event_replay.py
scripts/run_xtp_shadow_acceptance.py
```

Market Event Store 与生产 SQLite 分离，提供 immutable callback snapshot、Hash Chain、Manifest、Finding、分钟聚合、Integrity 和 Python/可选 DuckDB Replay。它不自动进入正式 Runtime Router、PIT 研究、训练或模型晋级。

### Monitor Backend

```text
stock_tracker/monitor/*
stock_tracker/api/monitor_handlers.py
stock_tracker/api/server.py
stock_tracker/api/audit.py
stock_tracker/api/handlers.py
stock_tracker/api/sse.py
stock_tracker/signals/manager.py
stock_tracker/__main__.py
```

Monitor Engine 是观察层：规则、Inbox、Outbox、Notification Worker、私有 REST/SSE 和远程写审计均已接线，但不能修改 ActionState、SignalState、评分、Trust、模型或订单。

### Monitor Frontend

```text
web/css/monitor.css
web/js/monitor.js
web/js/api.js
web/js/runtime.js
web/js/sse.js
web/js/app.js
web/index.html
qa/ui/monitor_workspace_qa.cjs
scripts/run_monitor_workspace_integration.py
```

新增独立“盘中监控”工作模式，包含信号收件箱、规则中心、数据链路和 Replay，不替换 Today Decision Mode。

### Tests

```text
tests/test_xtp_sidecar.py
tests/test_market_events.py
tests/test_monitor.py
tests/test_monitor_api.py
tests/test_xtp_shadow_acceptance.py
tests/test_integration.py
```

## 2. 默认启用边界

提交配置保持：

```toml
enabled = false
backend = "simulator"
bind_host = "127.0.0.1"
max_symbols = 20
read_only = true
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
auto_trade = false
```

配置解析明确拒绝：

```text
非 IPv4 loopback
超过 20 个标的
启用交易/训练/再分发
Event Catalog 或 Monitor DB 指向 data/stock_tracker.db
Event Catalog 与 Monitor DB 复用同一 SQLite
Event Root 与 Quarantine Root 重叠
任何秘密字段进入 TOML
```

工程验证命令：

```text
python scripts/verify_xtp_sidecar.py
python scripts/run_xtp_shadow_acceptance.py
python scripts/run_monitor_workspace_integration.py
```

## 3. 真实 XTP 前置条件

1. 安装独立 CPython 3.9；
2. 按测试账户门户对应版本安装官方 Quote SDK；
3. 设置七个本机环境变量；
4. 完成并独立 Review 官方 Quote Login/Subscribe Adapter；
5. Sidecar 继续只监听 loopback；
6. 首轮只订阅不超过 20 个标的；
7. 验证 Level 1/2 权限、字段单位、时间戳、重连和历史回补能力；
8. 做 50–100 标的 Live Shadow、开午收盘样本、人工断网与持续吞吐基准；
9. 审核行情保存、训练和再分发条款；
10. 以上通过后才单独评估 Runtime Router，仍不自动进入交易执行。

## 4. 账号填写位置

XTP 不是 API Key 模式。凭据只填在 **Sidecar 进程的本机环境变量**：

```text
STOCK_TRACKER_XTP_QUOTE_USER
STOCK_TRACKER_XTP_QUOTE_PASSWORD
STOCK_TRACKER_XTP_QUOTE_SERVER
STOCK_TRACKER_XTP_QUOTE_PORT
STOCK_TRACKER_XTP_QUOTE_PROTOCOL=TCP
STOCK_TRACKER_XTP_CLIENT_ID
STOCK_TRACKER_XTP_SIDECAR_ACCESS
```

不要修改 `config/xtp_sidecar.toml` 添加秘密，不要把值发到聊天、Git、URL、前端、日志或验收 JSON。算法账户不填写、不使用。

## 5. 关键工程合同

### Sidecar

- 字面 IPv4 loopback；
- GET-only IPC；
- 独立 Bearer；
- 禁止继承 Proxy 和 Redirect；
- 响应 URL 与请求 Scheme/Host/Port/Path/Query 完全一致；
- Metadata、Session、Cursor 与 Event 身份严格交叉校验；
- `callback_seq` 只代表本地回调顺序；
- Provider Sequence 不存在时不得声称无丢包；
- Event Payload、Hash、Event ID、A 股交易日和时间语义在 Runtime/Store 边界重新验证；
- 官方断线原因只保留安全错误码，不序列化原始账户/连接文本。

### Event Store

- 独立 SQLite 与 immutable event files；
- 按真实追加顺序连接 Hash Chain；
- SQLite 与文件系统采用协调提交和补偿恢复，不夸大成跨资源原子事务；
- Duplicate、Gap、Out-of-order、Source Time Regression 显式保留；
- 分钟成交量/成交额只使用同 Session 累计值增量；
- 持久化分钟 Bar 固定为 `DELAYED`；
- Monitor GET Replay 不记录 Replay Run，且 Event/Minute Bar 绑定相同时间窗口；
- Replay 不等于正式 PIT Replay。

### Monitor

- non-eval 白名单规则；
- 缺失事实失败关闭；
- Rule Version 与 Snapshot 不可变；
- 首次 Trigger、Inbox、Outbox 同事务；
- EventBus 只向上限 1024 的非阻塞 Queue 提交深拷贝快照，独立 Runtime Event Worker 执行规则/SQLite；满队列增加可见 dropped 计数；
- Outbox 采用 `PENDING -> SENDING` 租约，防止并发重复领取并支持超时恢复；
- 独立 Notification Worker 负责 Browser/Webhook 派发；
- Webhook 默认关闭、精确 HTTPS Allowlist、HMAC、无 Proxy/Redirect、有界重试；
- SSE 每客户端队列上限 256，慢客户端溢出后断开重连；
- 内部 `monitor_facts` 不进入浏览器 SSE；
- Monitor 不能改变动作、评分或订单。

## 6. 新鲜验证结果

```text
Targeted Stage tests: 83 passed, 1 skipped + 19 subtests
Runtime:              512 passed, 1 skipped + 316 subtests
Quant:                563 passed + 248 subtests
Monitor UI:           49/49
Hybrid H0:            12/12
Hybrid H1/H2:         28/28 + 11/11 negative scenarios
Hybrid H4:            18/18
Mock Today:           17/17
Real Today:           17/17
Portfolio CRUD:       13/13
Source/bytecode:      3 passed + 49 subtests
Targeted Ruff:        passed
JavaScript syntax:    passed
CPython 3.9 grammar:  6 sidecar files passed
compileall:           passed
pip check:            passed
Quant smoke:          passed / synthetic only
Fixture benchmark:    passed / challenger not promoted
Migration:            dry-run / database_modified=false
Production DB:        unchanged
```

生产数据库 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

详细 Findings 与真实性边界见 `docs/STAGE3D-STAGE5C-INDEPENDENT-REVIEW.md`。
