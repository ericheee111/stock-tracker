# Stage 3D–5C 独立工程与安全 Review

> 日期：2026-08-28
>
> 结论：`ENGINEERING_READY_FOR_MERGE`
>
> 真实 XTP operational 验收：`PENDING`

## 1. 审查范围

```text
Stage 3D  XTP 资格、账户与数据合同
Stage 3E  Read-only Quote Sidecar
Stage 3F  Append-only Market Event Store / Replay
Stage 4D  Signal Monitor Engine / API / Notification
Stage 4E  Monitor Workspace UI
Stage 5C  Synthetic Shadow Acceptance
```

## 2. 最终判定

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
SECURITY_REVIEW = PASSED
LOCAL_SIMULATOR_ACCEPTANCE = PASSED
LOCAL_EVENT_STORE_ACCEPTANCE = PASSED
MONITOR_API_ACCEPTANCE = PASSED
MONITOR_BROWSER_ACCEPTANCE = PASSED
SYNTHETIC_SHADOW_ACCEPTANCE = PASSED
REGRESSION_GATES = PASSED
PRODUCTION_DATABASE_MODIFIED = FALSE
ORDER_AND_ALGO_APIS = NOT_INTEGRATED
AUTO_TRADE = FALSE
ENGINEERING_READY_FOR_MERGE = TRUE

REAL_XTP_LOGIN_AND_SUBSCRIPTION = PENDING
LIVE_XTP_SHADOW = PENDING
LEVEL1_LEVEL2_PERMISSION_PROOF = PENDING
SUSTAINED_L2_THROUGHPUT_BENCHMARK = PENDING
DATA_STORAGE_TRAINING_REDISTRIBUTION_RIGHTS = PENDING
```

真实账户、真实 Level 1/2、正式 SDK Login/Subscribe、50–100 标的 Live Shadow、长时吞吐和许可证据没有被 Simulator 或 synthetic fixture 替代。

## 3. 对抗式 Findings 与修复

### IMPORTANT — 原始事件与分钟聚合可能半提交

原实现先提交事件/Manifest，再单独更新分钟聚合。聚合失败可能留下 durable raw event 与缺失派生索引。

修复：Session、Event、Finding、分钟聚合和 Manifest 元数据进入同一 `BEGIN IMMEDIATE`；失败时回滚 SQLite、删除本轮新建 immutable file，并按已提交 Catalog 恢复 Manifest。故障注入覆盖分钟聚合、Manifest 和 Commit 失败。

### IMPORTANT — 事件对象冻结但嵌套 Payload 可变

`frozen=True` 只冻结 dataclass 字段绑定；调用方仍可能修改嵌套 `dict/list`，从而让内存事件内容与 `event_id/raw_payload_sha256` 分离。

修复：构造时通过 canonical JSON 深度规范化；`as_dict()` 深拷贝；Sidecar Runtime 与 Event Store 写边界重新执行完整信封、Payload Hash 与 Event ID 校验。直接 mutation、`dataclasses.replace()` 和越界整数均不能绕过身份合同。

### IMPORTANT — `trading_day` 与 A 股自然日可能不一致

若使用 UTC `datetime.date()`，北京时间凌晨可能被错误归入前一自然日；调用者也可能提交任意日期。

修复：按 UTC+08:00 的 A 股 civil date 生成和验证 `trading_day`；Exchange/Provider/Received 时间必须有时区，源时间不能超出 `received_at` 容忍窗口。

### IMPORTANT — Sidecar 元数据与 Session 快照竞态

Health、Session、Metrics 与 Events 分次读取时，重连可能使它们属于不同 Session；只看单个响应会把跨 Session Cursor 或指标拼成一个伪快照。

修复：严格验证三份 metadata 的 `session_id/backend/feed_mode/connection_state/subscription_count` 一致，并在 Events 请求绑定 `expected_session_id`。Session 变化时从独立 Event Store 的已提交游标恢复，不信任旧内存 Cursor。

### IMPORTANT — Sidecar Transport 可被 URL 变化或绝对请求目标弱化

只禁止标准 Redirect 不足以覆盖自定义 Opener、错误响应 URL 或 absolute-form request target。

修复：只允许字面 IPv4 loopback；Server 拒绝带 Scheme/Netloc 的请求目标；Client 要求响应 URL 与请求的 Scheme、Host、Port、Path、Params、Query 完全一致；禁用继承 Proxy；所有 Metadata 时间、状态、Backend 和错误字段严格校验。

### IMPORTANT — XTP/Monitor 配置可误指向生产 SQLite

原相对路径检查不能阻止把 Event Catalog 或 Monitor DB 配成 `data/stock_tracker.db`，也不能阻止二者共用同一 SQLite 或 Event/Quarantine 根重叠。

修复：配置解析失败关闭以下情况：

```text
event_store.metadata_db == data/stock_tracker.db
monitor.database == data/stock_tracker.db
event_store.metadata_db == monitor.database
event_store.root 与 quarantine_root 相同或互为父子目录
```

生产数据库 SHA 在全部验收前后保持一致。

### IMPORTANT — 首次 Monitor Trigger 并发重复

原实现首次 Trigger 的查询与创建跨连接，两线程可能创建两个活动 Inbox。

修复：查询、Inbox 插入、Outbox 插入和 Trigger Count 更新使用同一 `BEGIN IMMEDIATE`。并发测试证明只生成一个活动 Inbox，并准确累计触发次数。

### IMPORTANT — Rule Version 与历史 Evidence 可漂移

客户端版本或后续规则更新不能证明事件触发时使用的规则。

修复：版本由 SQLite 写事务分配；Inbox 固化完整 Rule Snapshot、Version、SHA-256 与 `historical_exact`，Trigger 保护其不可变；旧 v2 近似迁移明确标为非精确历史。

### IMPORTANT — 缺失事实可能满足 `NE`

缺失路径原先可退化为 `None`，使 `None != "STALE"` 误触发规则。

修复：引入 `_MISSING` sentinel；任何缺失事实一律不匹配，并在 Evidence 记录 `present=false`。规则仍禁止 `eval`、`exec`、任意 Python、SQL 或 JSONPath。

### IMPORTANT — 同步 EventBus 可能让观察层阻塞信号线程

EventBus 同步调用所有订阅者；若 Monitor 在回调内直接执行 SQLite 规则评估，数据库锁等待会拖慢 HOT/WARM 信号线程。虽然不会改变决策值，但会破坏“观察层不得影响主链延迟”的隔离合同。

修复：运行态 `regime/provider_health/quote/monitor_facts` 只做深拷贝后 `put_nowait` 到上限 1024 的 Monitor Queue；独立 Worker 执行规则与 SQLite。队列满或快照失败会增加可见 `dropped` 计数并失败关闭，不阻塞发布者。停机先取消订阅，再有界排空队列；UI/Data Link 显示 Queue、processed/dropped 与 Worker 状态。

### IMPORTANT — Notification Outbox 并发重复发送与无人派发

多个 Dispatcher 同时读取 `PENDING` 可能重复发送；只在 XTP Poll 后派发会使运行链 Signal Monitor 的通知长期滞留。

修复：Outbox 使用原子 `PENDING -> SENDING` 租约、状态条件更新和超时租约恢复；并发 Dispatcher 只领取一次。主 Engine 启动独立有界 Notification Worker，退出时停止；Webhook 默认关闭，保持精确 HTTPS Allowlist、无 Proxy、无 Redirect、签名和有界重试。

### IMPORTANT — SSE 慢客户端可能无限占用内存

每客户端无界 Queue 会让断网或慢读客户端持续积压行情与 Monitor 通知。

修复：Queue 固定上限 256；溢出时替换为内部重连哨兵、从 Hub 移除客户端并关闭该 SSE，让浏览器按既有 Runtime 重连与 REST reload 合同恢复。内部 `monitor_facts` 永不进入浏览器 SSE。

### IMPORTANT — 每 Poll 全库 Integrity 扫描不可扩展

历史 Event 增长后，每次 Poll 全量扫描会线性拖慢采集。

修复：每 Poll 只校验本批受影响分区；发布和人工验收仍执行全量扫描。UI 只展示实际最近一次 Integrity 结果。

### IMPORTANT — 分钟 Finding 查询可能超过 SQLite 参数上限

原实现按当前分钟所有 Event ID 生成 `IN (...)`，高频标的一分钟内可能超过 bind parameter 上限。

修复：改为按 Symbol、时间窗口和 Event Type JOIN Finding；累计成交量/成交额只在同 Session 按增量聚合，持久化分钟 Bar 固定为 `DELAYED`。

### IMPORTANT — Replay GET 可能产生写副作用或返回窗口外分钟 Bar

读取型 API 不应因 Replay Catalog 记录而写入本地元数据，也不能把请求窗口外的分钟 Bar 混入结果。

修复：Monitor GET 使用 `record_run=false`；事件行和分钟 Bar 同时绑定相同 start/end；CLI 也按相同窗口查询。Replay 仍是本地 Event Replay，不是正式 PIT Replay。

### IMPORTANT — Monitor 事实桥与 UI 可出现过度声明

Quote 不拥有 Signal/Feature/Gap 事实，UI 文本也可能包含 `<...>`；若直接拼装会制造伪零值或 HTML 注入。

修复：`SignalManager` 只在既有评分、风险闸门与状态机完成后发布进程内 `monitor_facts`；Quote 仅提供真实拥有的价格与质量，其他字段保持缺失。UI 对动态文本统一转义，Playwright 证明标签以文本显示；Rule Builder 只暴露冻结白名单。

### IMPORTANT — Query 与静态响应边界不足

Monitor/Sidecar Query 若无字段数量上限可制造解析放大；Windows 浏览器若缺 UTF-8 charset 会出现乱码。

修复：Monitor 最多 16 个 Query 字段，Sidecar 最多 4 个；敏感 Query Key 被 Runtime Builder 拒绝；文本、JavaScript、JSON、SVG 响应附 `charset=utf-8`。

## 4. 新鲜测试证据

```text
XTP/Event/Monitor/Shadow targeted: 83 passed, 1 skipped + 19 subtests
Runtime full suite:              512 passed, 1 skipped + 316 subtests
Quant full suite:                563 passed + 248 subtests
Monitor Workspace browser:       49/49
Hybrid H0 local acceptance:      12/12
Hybrid H1/H2 browser:            28/28 + 11/11 negative scenarios
Hybrid H4 browser:               18/18
Mock Today:                      17/17
Real Today:                      17/17
Portfolio CRUD:                  13/13
Targeted Ruff:                   PASSED
JavaScript syntax:               6 files PASSED
CPython 3.9 syntax parse:         6 sidecar files PASSED
compileall:                      PASSED
pip check:                       PASSED
source distribution/bytecode:    3 passed + 49 subtests
Quant contract smoke:            PASSED / synthetic only
Quant fixture benchmark:         PASSED / challenger not promoted
production migration dry-run:    database_modified=false
secret/config scan:              PASSED
```

生产数据库验证前后 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

真实 CPython 3.9 未安装在当前验收宿主，因此执行的是 Python 3.9 grammar parse，不是官方 XTP 二进制加载证明。最终 staged-tree 复验和精确 Git SHA 由 `CHATGPT_HANDOFF.md` 记录。

## 5. Ruff 说明

本阶段新增和修改的 Python 文件 targeted Ruff 全部通过。没有把未重新收敛的历史全仓库 lint 债务包装成当前阶段通过；`ruff format --check` 也不在通过声明中。

## 6. 真实性边界

以下声明禁止：

```text
真实 XTP 已连接
Level 1/2 已验收
交易所原始 wire bytes 已保存
XTP 已优于当前全部数据源
XTP 已达到 T3
模型准确率得到提升
真实胜率或收益已证明
自动交易已实现
```

当前能够证明的是：隔离架构、严格数据与安全合同、Simulator、Event Store、Monitor、浏览器产品形态和 Synthetic Shadow 的工程正确性。真实市场能力只有在用户本机完成官方 SDK、股票 Quote 账户、实际交易时段和独立 operational Review 后才能升级。
