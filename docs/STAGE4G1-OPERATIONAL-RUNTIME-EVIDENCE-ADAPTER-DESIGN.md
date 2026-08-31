# Stage 4G.1 — Operational Runtime Evidence Adapter

状态：`DESIGNED / IMPLEMENTATION_PENDING`

日期：2026-08-31

## 1. 目的

Stage 4G Collection schema v3 已形成严格的 manual collection/finalization core，但它仍要求受控调用方提供：

```text
runtime_episode_fact_id
entry_requested_at
identity / data / policy IDs
entry / path / exit / no-entry facts
session_index
evidence IDs
```

这些字段的结构可以校验，来源却尚未由系统自动证明。Stage 4G.1 的目标是建立一个**只读、可恢复、不会下单的 Operational Runtime Evidence Adapter**，把运行系统已经产生的决策、行情和人工/外部成交事实转换为可验证的 Stage 4G 输入。

它不是 Trusted Admission Authority，也不能把任何 Outcome 提升为真实战绩。

## 2. 冻结边界

### 2.1 包含

- append-only Runtime Decision/Transition Artifact；
- 系统生成的 `runtime_episode_fact_id`；
- timezone-aware decision/request/observation timestamps；
- Signal、strategy/version、score、plan、data/policy/identity 引用冻结；
- EventBus/Scheduler 的 observational subscriber；
- Market Event Store 的连续 path collector；
- Paper execution adapter；
- Manual execution-evidence ingest；
- 可选的未来 Broker-confirmed read-only adapter contract；
- crash-safe cursor、restart recovery、idempotent retry 和 quarantine；
- 授权操作 CLI/API；
- Stage 4G Core 调用编排；
- 独立审计与运行指标。

### 2.2 不包含

- 下单、改单、撤单或算法交易；
- 根据 quote 猜测真实成交；
- 把 Paper fill 冒充真实 fill；
- 调用方自报 trusted/verified；
- Trusted Outcome Admission；
- Strategy Scoreboard 真实指标；
- 自动训练、模型晋级或策略权重调整；
- 将运行 SQLite bars 自动升级为 T3 研究数据。

## 3. 前置问题：当前 Runtime Signal 不能直接作为权威 episode

当前运行态 `Signal.state_changed_at` 仍可能由无时区 `datetime.now()` 产生，`signal_id` 主要是 `symbol + strategy_id`，且 Signal 表未冻结：

- strategy version；
- model ID；
- data snapshot ID；
- policy ID；
- instrument/identity fact ID；
- entry request timestamp；
- immutable decision fingerprint。

因此 Stage 4G.1 不应简单轮询 `signals` 表并自行拼接 ID。它必须在**决策产生边界**生成新的 immutable artifact。

## 4. Runtime Decision Artifact

建议新增独立合同：

```text
RuntimeDecisionArtifact
- schema
- runtime_signal_id
- transition_event_id
- symbol / market
- strategy_id / strategy_version
- model_id
- state / previous_state
- state_changed_at_utc
- decision_requested_at_utc
- observed_at_utc
- entry plan / invalidation / targets / reward-risk
- scores and reasons
- data_status / quality
- instrument_id / identity_fact_id
- data_snapshot_id
- policy_id
- classification_id
- market_regime / sector_stage
- execution_mode
- artifact_id
```

`artifact_id` 对除自身外的 canonical payload 做 SHA-256。Stage 4G 的 `runtime_episode_fact_id` 必须等于该 artifact ID。

### 4.1 时间规则

- 新 artifact 的正式字段全部 timezone-aware；
- `state_changed_at_utc <= observed_at_utc`；
- `decision_requested_at_utc <= observed_at_utc`；
- 禁止从 legacy naive time 静默附加本地时区；
- legacy signal 只能进入 quarantine 或 diagnostic migration lane；
- Runtime Clock 必须可注入并在测试中冻结。

### 4.2 Episode occurrence

同一 `runtime_signal_id` 可形成多个 artifact。新的 episode 必须来自新的不可变 transition/decision occurrence，而不是由调用方修改 score 或 strategy version 后重算。

推荐 occurrence identity：

```text
transition_event_id
+ runtime_signal_id
+ target state
+ decision_requested_at_utc
+ exact decision inputs
```

## 5. Artifact Store

使用独立于 `data/stock_tracker.db`、Stage 4G collection DB 和 Stage 4F ledger 的 append-only store，例如：

```text
data/runtime-decision-artifacts.db
data/runtime-decision-artifacts/
```

要求：

- canonical immutable record；
- SQLite Catalog + content-addressed files；
- global append order/hash chain；
- atomic no-overwrite publication；
- full audit before extension；
- production DB alias/path isolation；
- event cursor and processing status separate from immutable artifact；
- artifact 不因后续 Signal 刷新而改变。

## 6. Adapter Pipeline

```text
SignalManager decision boundary
→ publish/store RuntimeDecisionArtifact
→ Operational Adapter reads artifact cursor
→ validate eligible state and mode
→ open Stage 4G case with artifact_id
→ collect execution disposition
→ collect complete market path
→ determine terminal-ready facts under versioned rules
→ Stage 4G finalize
```

Adapter 必须 at-least-once 处理，但依赖 artifact/case/fact identity 实现 exactly-once observable effect。

### 6.1 Runtime DB 与 Artifact Store 的跨库一致性

不能采用“Signal 已提交后，仅通过内存 EventBus 尽力写 Artifact”的实现。进程在两步之间崩溃会永久丢失 episode，而现有 `signals/signal_history` 又不足以重建完整 decision inputs。

推荐先实现 Runtime transactional outbox：

```text
BEGIN IMMEDIATE on runtime database
→ upsert Signal current state
→ append signal_history
→ insert immutable runtime_transition_outbox payload/artifact_id
→ COMMIT
→ Artifact Worker materializes append-only RuntimeDecisionArtifact
→ verify artifact
→ mark outbox delivery cursor/status
```

要求：

- Signal、history 与 outbox 必须由一个新的 Repository transaction 方法原子提交，不能继续三次独立 commit；
- outbox payload 使用 canonical bytes/ID，写入后不可被调用方修改；
- worker at-least-once 重放，artifact store 幂等；
- cursor/status 可以更新，但 immutable outbox payload 不能覆盖；
- Artifact 成功前，该 episode 必须显示 `OUTCOME_EVIDENCE_PENDING`，不得进入 Stage 4G；
- Artifact 永久失败时，运行页面/信号可以继续服务，但 Outcome lane 必须失败关闭并显示 `OUTCOME_EVIDENCE_UNAVAILABLE`；
- 若不愿修改生产 Runtime schema，只能实现 `OBSERVATIONAL_BEST_EFFORT` shadow 模式，不能声明完整 operational collection；
- schema migration 必须先 backup、dry-run、回滚演练并验证生产 DB hash/版本边界。

这一区分保证 Outcome 证据故障不会篡改交易建议，同时不会把丢失的证据静默当成完整样本。

### 6.2 规则与价格字段绑定

Runtime Artifact 必须冻结由当时有效规则派生的：

```text
minimum_exit_session_offset
execution_policy_id
market_rule_id
cost_schedule_id
reference_price_definition
```

Stage 4G Core 只执行冻结 offset、horizon、完整数量以及 reference/fill 位于对应可观察 session 区间等内部一致性校验。`minimum_exit_session_offset` 与策略 horizon 是独立维度，市场限制晚于 horizon 时必须保留延迟成交与额外 holding sessions。Stage 4G.1 必须证明 offset 与具体 instrument/交易日规则一致；不能把 `Market.A` 永久硬编码为统一 T+1，也不能把任意 benchmark 值当作 reference price。

## 7. Execution Evidence Types

### 7.1 `PAPER_SIMULATED`

- 只能进入 Stage 4G `PAPER`；
- 必须使用版本化 execution rule、next executable price、spread/slippage/cost；
- A 股处理 T+1、涨跌停、停牌和交易单位；
- 生成 deterministic simulation artifact；
- 永久 `DIAGNOSTIC_ONLY`。

### 7.2 `MANUAL_ATTESTED`

- 用户显式输入 fill/no-entry；
- 保存结构化事实、输入时间、附件/外部证据 opaque ID；
- 不保存券商密码、访问 token 或完整敏感账户号；
- 只能进入 Stage 4G `LIVE_MANUAL`；
- 永久只是 `LIVE_CANDIDATE`，除非未来 Authority 独立验证。

### 7.3 `BROKER_CONFIRMED_READ_ONLY`

本阶段只冻结接口，不要求立即接入。未来适配器必须：

- 只读订阅订单/成交回报；
- 不暴露任何 order endpoint；
- 绑定 broker session、order ID、execution ID、instrument、side、quantity、price、fees 和 exchange/broker timestamps；
- 处理 partial fills、duplicate callback、out-of-order callback 和 reconnect gaps；
- 原始回报保存到独立 evidence artifact；
- 凭据仅位于本地 secret store/environment；
- 不能因为来自 Broker 就自动 trusted admission。

当前 XTP sidecar 仍是 quote-only/read-only 边界，不得为完成本阶段而启用 Trader 或 Algo API。

## 8. Market Path Collector

优先消费 Stage 3F append-only Market Event Store 或正式 Bar Artifact，而不是页面内存 quote。

要求：

- exact symbol/market/session；
- source/session/parser/schema identity；
- exchange/provider/received/known timestamps，以及明确的 observation window start/end；
- duplicate/gap/out-of-order detection；
- authoritative calendar/session/security-status mapping；
- entry 到 exit/horizon 的连续 session coverage，并区分可交易 bar、`SUSPENDED / NO_TRADE / MARKET_CLOSED / MISSING_DATA`；
- 每个 Stage 4G PATH event 引用 raw/bar artifact ID，退出请求前缀同时冻结 point ID、完整 PATH `fact_id` 与 collection `observed_at`；
- 只有在 `EXIT_REQUEST` 前 durable append 的 PATH fact 才能证明当时可知的 TARGET/STOP/TIMEOUT；请求后补录的早时间戳数据不得回填到旧请求；
- path worker 必须先确认 Stage 4G append 成功并完成审计，再推进自身 cursor；
- 数据 gap 或 no-trade 语义无法证明时保持 OPEN/QUARANTINED，不得伪造 OHLC 或自动 `path_complete=true`。

## 9. Terminal Resolver

Terminal Resolver 必须版本化，输出 reason evidence，而不是只输出枚举。

最低规则：

- TARGET：请求前、horizon 内按时间观察到的首个水平障碍为冻结目标；
- STOP：请求前、horizon 内按时间观察到的首个水平障碍为冻结 invalidation；
- TIMEOUT：权威 session count 到达 horizon，且此前没有 target/stop 水平障碍；
- ORDER_REJECTED：execution evidence 明确拒绝且无 fill；
- DATA_INVALID：数据质量/连续性合同明确失败，不得作为普通未成交过期或用户撤单的替代枚举；
- ENTRY_EXPIRED / USER_CANCELLED：当前 Stage 4A Outcome 合同尚未定义；实施自动 Adapter 前必须先冻结向后兼容的 Outcome/no-entry session evidence；
- MANUAL：需要人工事实和理由；
- TRAILING_STOP / BROKEN_TREND：需要版本化 trailing/trend rule artifact。

同一 Stage 4G PATH point 同时触及 stop/target 时 Core 失败关闭。Adapter 只有在提供更细粒度、按时间排序且来源可审计的路径，或未来明确冻结并由 Authority 接受的 intrabar ambiguity policy 后，才能解除该 blocker；不得事后选择更有利结果。

## 10. Partial Fill 与多腿退出

Stage 4G Collection schema v3 目前只支持单 entry fill 与单完整 exit fill。Operational Adapter 的实现顺序应为：

1. 首版只接受已聚合且可证明完整的 fill summary；
2. summary 必须绑定全部原始 execution IDs；
3. quantity-weighted price、费用和时间规则可重算；
4. 存在未完成 partial fill 时不得 finalization；
5. 后续以独立、显式版本升级的多腿合同原生建模 scale-in、trim、partial take-profit 和 Trend Runner；不得复用当前 v3 schema 名称解释新语义。

不能丢弃 partial fills 后把剩余成交伪装成完整单笔交易。

## 11. Worker、Cursor 与恢复

建议独立 worker 状态库：

```text
artifact_cursor
path_cursor
execution_cursor
last_success_at
retry_count
quarantine_reason
lease_owner / lease_expiry
```

不可把 cursor 混入 immutable evidence chain。

恢复规则：

- crash 后从最后 durable cursor 重放；
- Stage 4G case/fact ID 保证重复消费幂等；
- cursor 只能在目标写入确认后前移；
- poison artifact 进入 quarantine，不阻塞其他 symbol；
- clock rollback、schema mismatch、source gap、identity drift 必须报警并失败关闭；
- 不允许通过删除 case/record 来“重试”。

## 12. 操作接口

首版建议提供本地 CLI：

```text
capture-runtime-decision
run-outcome-adapter --once
run-outcome-adapter --daemon
record-manual-fill
record-manual-no-entry
show-outcome-case
retry-quarantined-case
```

若提供 REST：

- 仅监听 loopback 或私有认证通道；
- manual write 必须有 CSRF/replay protection、actor identity 和审计日志；
- 不提供 order/trade endpoint；
- 不返回 secret；
- 公网 UI 不能直接访问本地 evidence store。

## 13. 可观测性

至少输出：

- artifact lag；
- open/terminal/prepared/finalized/quarantined case counts；
- path gap count；
- duplicate/idempotent count；
- execution callback gap/out-of-order count；
- oldest unprocessed artifact age；
- clock rollback count；
- schema mismatch count；
- production DB modified=false；
- auto_trade=false。

## 14. 安全与信任边界

即使 Operational Adapter 自动运行：

```text
runtime artifact != trusted admission
broker callback != independent authority
manual attachment != verified evidence
hash chain != external signature
```

所有 Outcome 仍只进入 Stage 4F candidate lanes。Adapter 无权写 Admission Ledger、Scoreboard 或 Model Registry。

## 15. 实施顺序

### 4G.1-A — Aware Runtime Decision Artifact / Transactional Outbox

- 引入可注入 UTC Clock，legacy naive time 进入 quarantine；
- 建立显式 Runtime schema migration history；默认 dry-run，apply/backup/rollback rehearsal 必须单独授权；
- 将 Signal current state、signal history 与 immutable transition outbox 通过单一 Repository transaction 原子提交；
- outbox payload 不可更新/删除，delivery/lease/cursor 与 payload 分离；
- worker at-least-once 物化 append-only Runtime Decision Artifact，并在 durable write + audit 后推进 cursor；
- `artifact_id == runtime_episode_fact_id`，完成同一 occurrence 幂等与新 occurrence 区分的端到端测试。

### 4G.1-B — Paper Adapter

- 绑定 execution policy/market rules；
- 自动生成 diagnostic entry/path/exit；
- 故障和 restart recovery。

### 4G.1-C — Manual Live Candidate Adapter

- 授权本地输入；
- opaque evidence attachment；
- 不回显 secret；
- candidate-only finalization。

### 4G.1-D — Market Path Worker

- 消费 append-only market artifacts；
- calendar/session/gap 验证；
- deterministic terminal resolver。

### 4G.1-E — Shadow Acceptance

- 不启用 Broker trader；
- 至少跨多交易日运行；
- 验证 duplicate/reconnect/gap/restart；
- 所有样本仍不进入真实 Scoreboard。

## 16. 合并门禁

```text
artifact codec/store/audit tests
aware-time and legacy-naive quarantine tests
episode identity drift/idempotency tests
paper execution market-rule tests
path gap and intrabar ambiguity tests
worker crash/restart/cursor tests
manual auth/input/secret tests
Stage 4G/4F regressions
Runtime/Quant full suites
production DB unchanged
no order/trader/algo endpoint
exact Git Index and secret/generated scan
independent financial-correctness review
```

完成本阶段后才具备进入 Stage 4H Trusted Outcome Admission Authority 的可靠候选输入基础。
