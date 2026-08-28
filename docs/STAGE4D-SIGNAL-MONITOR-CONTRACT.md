# Stage 4D — Signal Monitor Engine 合同

> 状态：`ENGINEERING_IMPLEMENTED_AND_REVIEWED`

## 1. 目的

Monitor Engine 把行情链路、数据质量、既有 Signal/Action 状态和风险阻断转换为可审计的监控事件。它是观察层，不是新的决策引擎。

禁止：

```text
修改 ActionState
修改 SignalState
修改评分
升级 Trust Tier
训练模型
创建订单
调用 Trader/Algo API
```

## 2. 允许事实

规则只访问白名单路径：

```text
action_state
signal_state
data_status
data_quality.status
data_quality.score
blocker_codes
market_regime.state
market_regime.score
market_event.connection_state / feed_mode / latency / gap / ingestion / price
scores.opportunity / timing / risk / confidence
features.rsi14 / roc20 / roc60 / ann_vol / volume_ratio / pos52w / amplitude / bar_count
```

缺失事实显式记录 `present=false`，不能让 `NE` 等条件因字段缺失而误触发。

`SignalManager` 在既有评分、风险闸门和状态机完成后发布进程内 `monitor_facts`：它绑定同一次扫描的 ActionState 映射、四分数、真实技术指标、数据质量和阻断码。该主题只供 `MonitorService` 消费，不在浏览器 SSE 白名单中；浏览器只接收已经过规则、去重、持久化和生命周期处理的 `monitor.inbox` / `monitor.notification`。

Quote 主题只提供其真实拥有的价格、涨跌幅与数据质量；不存在的延迟、Gap、Signal 或 Feature 字段保持缺失/`null`，不得伪造为零。

EventBus 发布线程不得直接执行 Monitor SQLite 规则评估。允许主题在深拷贝后使用 `put_nowait` 进入上限 1024 的独立 Queue，由 Monitor Worker 消费；满队列或快照失败必须增加可见 `dropped` 计数，绝不阻塞 HOT/WARM 信号线程。停机先取消订阅，再有界排空队列。

## 3. 表达式

```text
AND / OR
EQ / NE
GT / GE
LT / LE
IN / CONTAINS
```

不支持 `eval`、`exec`、任意 Python、任意 SQL、任意 JSONPath 或用户脚本。

## 4. Scope

```text
SYMBOLS
WATCHLIST
POSITIONS
MARKET
ALL_MARKET
```

宽 Scope 需要：

- 显式 acknowledgement；
- `max_symbols`；
- 有界 Universe；
- 冷却和重复抑制。

## 5. Inbox 生命周期

```text
NEW
ACKNOWLEDGED
SNOOZED
RESOLVED
INVALIDATED
EXPIRED
```

首次触发、Cooldown/重复抑制、Inbox 插入和 Notification Outbox 插入位于同一个 `BEGIN IMMEDIATE` 事务。`suppress_window_sec=0` 的并发重复证据合并到一个活动 Inbox 并增加 `trigger_count`；正常 Engine 冷却窗口内的并发命中只允许一个写入/通知，其他评估明确返回 `suppressed=true`。

Rule Version 由 Repository 在同一 SQLite 写事务中分配：首次保存为 `v1`，每次更新严格递增。客户端提交的 `version`、`created_at`、`updated_at` 不被信任；并发更新会得到不同的连续版本，不会产生两个互相覆盖的 `v2`。

每个 Inbox 固化：

```text
rule_id
rule_version
rule_snapshot_json
rule_snapshot_sha256
historical_exact
```

Rule Snapshot 由触发时的完整规则合同生成，并由 SQLite Trigger 禁止后续改写。读取 Inbox 时会交叉校验版本、Snapshot 与 Evidence Hash。v2 Monitor Catalog 升级为 v3 时，只能使用当前 Rule 回填旧事件，因此明确标记 `historical_exact=false` 和 `migration_source=V2_CURRENT_RULE`，不会把近似回填伪装成当时的精确规则。

终态事件再次满足条件时创建新 Inbox；Rule 更新后因版本进入 Dedup Identity，会创建新的版本事件链。存在历史的 Rule 不能硬删除，只能禁用。

## 6. Notification

### Browser

通过现有 Header-authenticated SSE：

```text
monitor.inbox
monitor.notification
```

每个 SSE 客户端使用上限 256 的队列。慢客户端溢出后接收内部重连哨兵、从 Hub 移除并关闭连接；浏览器按既有 Runtime 重连与 REST reload 合同恢复。进程内 `monitor_facts` 永不转发到浏览器。

Notification Outbox 由独立 Worker 派发，而不是依赖 XTP Poll。多个 Dispatcher 通过原子 `PENDING -> SENDING` 租约领取任务；租约过期可恢复，状态条件更新阻止并发重复领取。

### Webhook

默认关闭。启用时要求：

- 精确 HTTPS Origin allowlist；
- URL 无 userinfo/query/fragment；
- 禁止 Redirect；
- 不继承 Proxy；
- 环境变量签名密钥；
- 16 KiB Body；
- 5 秒超时；
- 最大 3 次有界重试；
- Payload 不包含账户净值、持仓成本、Bearer 或 Provider 凭据。

## 7. REST

```text
GET    /api/monitor/summary
GET    /api/monitor/data-link
GET    /api/monitor/rules
GET    /api/monitor/inbox
GET    /api/monitor/outbox
GET    /api/monitor/replay
POST   /api/monitor/rules
PUT    /api/monitor/rules/{rule_id}
DELETE /api/monitor/rules/{rule_id}
POST   /api/monitor/inbox/{inbox_id}/transition
```

继续继承：

- 私有 Bearer；
- exact CORS/OPTIONS；
- Remote Write Audit；
- Query 数量上限；
- JSON duplicate-key 拒绝；
- `Cache-Control: no-store`；
- Request ID；
- 动态 Route 审计模板化。

## 8. 验收判定

```text
NON_EVAL_RULES = PASSED
MISSING_FACT_FAIL_CLOSED = PASSED
CONCURRENT_DEDUP_ATOMIC = PASSED
CONCURRENT_RULE_VERSION_ALLOCATION = PASSED
IMMUTABLE_RULE_SNAPSHOT = PASSED
V2_TO_V3_MONITOR_MIGRATION = PASSED
INBOX_LIFECYCLE = PASSED
REMOTE_AUTH_CORS_AUDIT = PASSED
RUNTIME_EVENT_ASYNC_ISOLATION = PASSED
OUTBOX_LEASE_AND_WORKER = PASSED
SSE_BACKPRESSURE = PASSED
WEBHOOK_BOUNDARY = PASSED
ACTION_OR_SCORE_MUTATION = FALSE
AUTO_TRADE = FALSE
```
