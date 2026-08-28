# Stage 4E — Monitor Workspace 实施交接

> 状态：`ENGINEERING_COMPLETE`
>
> UI 验收：`49/49 PASSED`

## 1. 产品定位

新增“盘中监控”工作模式，不替换既有 Today Decision Mode：

```text
Decision Mode：今天应该怎么做
Monitor Mode：现在发生了什么，哪些条件刚刚变化
```

Monitor UI 不生成新的买卖结论，也不把 XTP Simulator 或 DELAYED 数据包装为可执行信号。

## 2. 页面

```text
盘中监控
├── 信号收件箱
├── 规则中心
├── 数据链路
└── Replay
```

状态轨显示：

```text
Engine
XTP Link
Subscription Count
Last Event
P50 / P95 Latency
Gap
Out-of-order
Event Store Lag
Notification Outbox
```

## 3. 技术

```text
web/js/monitor.js
web/css/monitor.css
web/js/api.js
web/js/runtime.js
web/js/sse.js
web/js/app.js
web/index.html
```

设计采用现有 Token 与紧凑金融工作台布局，使用 CSP-safe native SVG 绘制延迟和本地 Replay 图，不依赖公共 CDN、远程字体或外部分析脚本。

成熟金融 UI 模板的价值已吸收为：紧凑表格、状态条、Tabs、Drawer/Sheet、Badge、时间线和 Dense Metrics；本阶段没有直接引入 Tabler/Bootstrap、React、KLineChart 或 ECharts，以避免全局 CSS 污染、供应链和 CSP 范围扩张。以后图表复杂度达到阈值时再做固定版本、许可证和静态资产审计。

## 4. Runtime 安全

- Monitor API 延迟加载，Today 首屏不新增 Monitor 请求；
- REST 继续使用 Runtime URL Builder；
- 新增 `apiUrlWithQuery()`，Query Key/Value 严格校验；
- `token/secret/password/access/authorization` 等敏感 Query Key 被拒绝；
- Bearer 只在 Header；
- 所有 API 文本经过 HTML escape；
- Monitor SSE 复用现有 Header-authenticated fetch stream；
- Runtime hard failure 时清空 Monitor 内存状态；
- Engine Offline/Auth Required 显式展示；
- 静态 JS/CSS/HTML 以 UTF-8 Content-Type 返回。

## 5. Replay 边界

Replay 只读取独立 Market Event Store，显示 Event-bound OHLC 和数据完整性。页面不显示或推导：

```text
胜率
收益率
Sharpe
最大回撤
成功概率
策略晋级
```

本地事件 Replay 不等于正式 PIT Replay。

## 6. 浏览器验收

```text
python scripts/run_monitor_workspace_integration.py
```

覆盖：

- 390 / 768 / 1280 无横向溢出；
- 9 个状态指标和 4 个 Workspace Tab；
- 无 pageerror、5xx、意外 console error；
- 无外部网络依赖；
- API 文本防 HTML 注入；
- Inbox 显示精确 Rule Version、Trigger Snapshot 与真实条件数量；
- Inbox ACK；
- 规则 Builder；
- 最新价与延迟规则字段；
- 分区完整性 `PASSED` 可见；
- Runtime Monitor Queue、processed/dropped 和 Worker 状态可见；
- 数据与账户边界可见；
- Replay SVG；
- 敏感 Query Key 拒绝；
- Auth Required；
- Engine Offline；
- Token 不进入 DOM/URL；
- 生产数据库 SHA 不变。

最终：

```text
MONITOR_WORKSPACE_ACCEPTANCE = PASSED
TOTAL = 49
FAIL = 0
SYNTHETIC_FIXTURE_ONLY = TRUE
REAL_XTP_ACCOUNT_ACCEPTANCE = PENDING
AUTO_TRADE = FALSE
```
