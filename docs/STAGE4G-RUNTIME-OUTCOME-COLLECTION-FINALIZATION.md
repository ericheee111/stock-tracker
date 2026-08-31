# Stage 4G — Runtime Outcome Collection / Finalization Core

状态：`MANUAL_COLLECTION_CORE_REVIEW_PASSED / AUTOMATIC_RUNTIME_ADAPTER_PENDING / TRUSTED_ADMISSION_PENDING / GIT_DELIVERED`

日期：2026-08-31

## 1. 重新界定后的阶段结论

Stage 4F 已提供 append-only terminal `SignalOutcome` candidate ledger，但不负责持有未完成交易生命周期。Stage 4G 当前完成的是一个**可由受控调用方驱动的采集与终态生成核心库**：

```text
externally identified runtime episode
→ immutable runtime/decision snapshot
→ append-only collection event chain
→ terminal-ready case
→ deterministic SignalOutcome
→ Stage 4F OutcomeLedger candidate record
```

当前实现不是完整的自动 Runtime Service。仓库尚未把它接入 `SignalManager`、EventBus、Scheduler、Market Event Store、Portfolio 或 Broker；也没有后台 worker、REST 写接口、操作 CLI、重启补采编排或真实成交适配器。因此正式状态必须是：

```text
MANUAL_COLLECTION_CORE = COMPLETE
AUTOMATIC_RUNTIME_COLLECTION = NOT_IMPLEMENTED
BROKER_EXECUTION_CAPTURE = NOT_IMPLEMENTED
TRUSTED_OUTCOME_ADMISSION = NOT_IMPLEMENTED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
```

## 2. 范围

### 2.1 已实现

- 冻结 Runtime Signal、策略版本、模型、证券身份、数据与政策引用、计划和评分；
- 显式接收外部 `runtime_episode_fact_id` 与 `entry_requested_at`；
- 独立 SQLite collection database；
- 全局连续 append order 和 SHA-256 event chain；
- `CASE_OPENED / ENTRY_FILLED / PATH_POINT / EXIT_REQUESTED / EXIT_FILLED / NO_ENTRY / FINALIZATION_PREPARED / FINALIZED`；
- Paper 与 Live Manual candidate 两种模式；
- entry、path、exit、no-entry、成本、完整成交数量、冻结最小退出 session offset、reference/fill 可观察价格区间与 evidence ID 的严格类型/时间校验；
- 退出请求冻结当时已知的最大连续 PATH 前缀，并同时绑定 PATH point ID、PATH event `fact_id` 与 collection `observed_at`；
- TARGET/STOP/TIMEOUT 使用 horizon 内 first-touch 语义；同一粗粒度 PATH point 同时触及目标和止损时失败关闭；
- 两阶段 finalization、Ledger 故障恢复、Ledger 已提交但 marker 未写的重试；
- FINALIZED 后重复调用的终态幂等；
- Collection DB、生产 DB、Stage 4F Catalog/Record Root 隔离；
- 全量 event replay audit；
- 对抗并发、篡改、时钟回拨和 identity replacement 的回归测试。

### 2.2 未实现

- 自动监听 Runtime Signal 状态迁移；
- 自动生成可信 `runtime_episode_fact_id`；
- 自动绑定 Market Event Store 的连续行情路径；
- Broker/券商只读订单与成交回报采集；
- 多次部分成交和多笔退出；
- `ENTRY_EXPIRED / USER_CANCELLED` 等普通未成交终态；不得借用 `DATA_INVALID / ORDER_REJECTED` 冒充；
- `SUSPENDED / NO_TRADE / MARKET_CLOSED / MISSING_DATA` 的独立路径语义与 observation window start/end；不得伪造 OHLC 补齐；
- 根据市场规则自动决定终态原因；
- Trusted Outcome Admission Authority；
- 真实 Scoreboard API/UI；
- 自动训练、模型晋级、策略权重调整或交易执行。

## 3. Runtime Episode 与时间身份

### 3.1 为什么不能直接使用 runtime `signal_id`

运行态 `signal_id` 通常按 `symbol + strategy_id` 长期复用。若直接作为 Stage 4F Outcome 唯一键，第一个终态结果会永久阻断同一信号后续策略版本、重新触发或新交易 episode。

Stage 4G Collection schema v3 使用：

```text
runtime_episode_fact_id
→ runtime_episode_id
→ case_id = runtime_episode_id + collection_mode
→ outcome_signal_id = namespace + mode + runtime_episode_id
```

`runtime_episode_fact_id` 必须是外部、不可变、内容寻址的 episode occurrence 引用。当前核心库只校验其 SHA-256 形式，不能证明它来自真实 Runtime 事件；因此它仍属于候选证据。未来 Operational Adapter 必须从独立 append-only Runtime Transition Artifact 生成并核验该 ID。

相同 episode fact 在同一 mode 下：

- 完全相同事实重试为 `IDEMPOTENT`；
- 任一 snapshot identity drift 必须冲突；
- 新 episode 必须提供新的 `runtime_episode_fact_id`。

同一 episode 可并行跟踪 Paper 与 Live Manual，但两种 mode 的 episode evidence 必须一致。

### 3.2 `entry_requested_at` 与 `captured_at`

二者不得混用：

- `entry_requested_at`：外部 Runtime episode 声明的实际 entry intent 请求时刻；
- `captured_at`：Collection Core 首次持久观察并冻结 snapshot 的时刻。

要求：

```text
runtime_state_changed_at <= captured_at
entry_requested_at <= captured_at
```

它们均必须是 timezone-aware datetime，并规范为 UTC。无时区时间不得进入量化身份。允许在 `captured_at` 之后补录一个更早但不早于 `entry_requested_at` 的 fill fact；其“后来才被系统知道”的语义由 collection event `observed_at` 保留。

`decision_snapshot_id` 绑定完整 immutable runtime evidence、`runtime_episode_id` 和首次 `captured_at`；`TradeIntentEvidence.requested_at` 使用 `entry_requested_at`，不再错误使用 collector 的采集时刻。

## 4. Snapshot 金融合同

当前只接受：

```text
TRIGGERED
ACTIVE
DATA_INVALID
```

`WATCH` 尚未形成 entry intent；`OVEREXTENDED / INVALIDATED / EXPIRED` 缺少与现有 `OutcomeTerminalReason` 一一对应的无歧义 no-entry 合同，暂时失败关闭，防止产生永久无法终结或错误归类的 case。

长仓计划必须满足：

```text
entry_low <= entry_high
invalidation_price < entry_low
target_1 > max(entry_high, trigger_price)
target_2 >= target_1
```

评分 reason 容器和所有安全字段使用严格实际类型；NaN、Infinity、boolean-as-integer、非规范 Decimal、未知字段和无时区 datetime 均被拒绝。

## 5. Collection Mode

### 5.1 `PAPER`

- 生成 `PAPER_RECORDED`；
- Evidence Tier 固定 `BEST_EFFORT`；
- `verified=false`；
- 写入 Stage 4F `DIAGNOSTIC_ONLY`；
- 永远不能进入真实 Scoreboard。

### 5.2 `LIVE_MANUAL`

- entry snapshot 必须为 `DataStatus.LIVE`；
- entry、path、exit 和 no-entry 事实必须携带 evidence IDs；
- evidence ID 只表示候选引用，不证明文件存在、Broker authenticity、签名、许可或独立审查；
- 生成 `LIVE_OBSERVED` 但仍固定 `verified=false / BEST_EFFORT`；
- 最多进入 Stage 4F `LIVE_CANDIDATE`。

## 6. Append-only 生命周期

```text
CASE_OPENED
├─ NO_ENTRY
│  └─ FINALIZATION_PREPARED
│     └─ FINALIZED
└─ ENTRY_FILLED
   └─ PATH_POINT*
      └─ EXIT_REQUESTED
         ├─ PATH_POINT*
         └─ EXIT_FILLED
            └─ FINALIZATION_PREPARED
               └─ FINALIZED
```

投影状态：

```text
AWAITING_ENTRY
OPEN_POSITION
EXIT_REQUESTED
TERMINAL_READY
FINALIZATION_PREPARED
FINALIZED
```

`OutcomeCollectionCase.__post_init__` 会重新验证 case ID、snapshot、entry/exit identity、PATH point/fact IDs、collection known time、冻结的退出请求前缀、path 顺序、terminal facts、prepared Outcome、finalized Ledger identity 和派生状态。局部 `dataclasses.replace()` 修改状态、路径来源、前缀或终态 identity 会失败；但 Python 值对象本身不是密码学安全边界，权威结论仍必须来自 Collection DB 的完整 event replay。

## 7. Path 完整性与市场规则

Complete Outcome 必须具备：

- entry fill 必须完整满足 requested quantity；Collection schema v3 不允许静默丢弃 partial remainder；
- exit request 与完整 exit fill；
- entry session 到 exit session 的每一个 session 至少一个 `observable=true` path point；
- observable session index 连续，任何 point 不得位于 exit session 之后；
- path timestamp 严格递增；
- entry/exit 的 reference price 与 fill price 必须位于对应 observable session 区间；
- holding sessions 不得小于冻结的 `minimum_exit_session_offset`；该市场/执行限制与策略 horizon 独立，实际 exit fill 可以因停牌、未成交或执行延迟晚于 `horizon_sessions`，必须保留而不能截断，以避免幸存者偏差；
- 退出请求只绑定请求时已经 durable append 的最大连续 PATH 前缀；每一点同时绑定 point ID、完整 PATH event `fact_id` 和 collection `observed_at`，因此请求后补录的较早 market timestamp 不能回填解释旧决策；
- TARGET/STOP 必须匹配请求前、horizon 内按时间观察到的首个水平障碍；TIMEOUT 必须已有 horizon-session evidence 且此前没有 TARGET/STOP 障碍；
- 同一粗粒度 PATH point 同时触及 target/stop 时顺序不可证明，Core 失败关闭，等待更细粒度且可审计的路径或未来经 Authority 接受的明确 ambiguity policy。

`minimum_exit_session_offset` 属于被冻结的 execution/market-rule policy：例如普通 A 股股票通常配置为 1，而允许同 session 退出的 instrument 可配置为 0。Core 不把整个 `Market.A` 永久硬编码为统一 T+1；Stage 4G.1/Authority 必须证明该 offset 与具体 instrument 和交易日规则一致。

缺失 session 会低估 MFE/MAE，因此不得把不完整路径标为 `path_complete=true`。当前连续性仍依赖调用方提供的 session index；在 Trusted Admission 前，Authority 还必须将其绑定到权威交易日历、证券状态和市场规则快照。当前 `OutcomePathPoint` 未携带 observation window start，粗粒度日线可能包含 entry 前/exit 后价格，因此精确 MFE/MAE 仍需 Stage 4G.1 绑定 bar interval/window 或更细粒度事件。

## 8. Fact Identity

`fact_id` 是 fact payload 的内容 hash。相同市场事实可能合法服务于多个 case，因此唯一性范围为：

```text
UNIQUE(case_id, fact_id)
```

而不是全库 `UNIQUE(fact_id)`。同一 case 内相同 fact 重试保持幂等；同一 case 内同 ID 不同内容或不同事件类型必须冲突。PATH event `fact_id` 是完整 canonical PATH payload 的 hash，覆盖价格点、`raw_bar_snapshot_id` 与 evidence IDs；退出前缀同时绑定该 fact ID，不能只靠标准化 OHLC 的 `point_id` 替换来源事实。

## 9. 独立存储与原子初始化

默认：

```text
data/outcome-collection.db
```

它不得等于或 hardlink 到：

```text
data/stock_tracker.db
```

也不得复用 Stage 4F Catalog 或位于 Stage 4F Record Root 内。

Collection schema v3 首次建库流程：

```text
same-directory temporary SQLite
→ full schema/index/meta
→ COMMIT
→ quick_check + exact schema validation
→ fsync temporary database
→ non-overwriting hard-link publish
→ validate winning database
→ delete only this process's temporary files
```

并发首次初始化只能发布一个完整数据库；失败初始化不得在最终路径遗留半数据库。运行连接显式使用 `busy_timeout` 和 `synchronous=FULL`。

Stage 4G v1 是未接入运行系统的实验 schema，缺少外部 episode fact identity 且使用全局 fact uniqueness；v2 未绑定 PATH event `fact_id`，且 terminal reason 仍是 any-touch 语义。v3 不自动重写 v1/v2 append-only evidence；发现旧 schema 时保持原文件不变并失败关闭。若本地存在实验 Catalog，应先只读归档，再由单独、可审计的迁移 artifact 显式处理，不能静默重算历史 identity 或把旧记录解释成 first-touch。

## 10. 完整性审计

每次写入前重放并验证：

- SQLite quick check、精确表/列/index/unique identity，并拒绝额外 Trigger、View、隐藏/生成列与非 DELETE journal mode；
- append order 从 1 连续递增；
- global previous-event hash chain；
- event hash、payload SHA、canonical JSON 与 fact ID；
- `observed_at` 随 append order 单调不下降；
- case lifecycle 和全部派生身份；
- 同一 runtime episode 跨 mode evidence 一致；
- case state 与事实一致；
- database path、inode/device identity 与 production alias 隔离。

Outcome 成交成本、R 倍数、中位数、分桶和 Scoreboard 计算使用模块内固定 Decimal context；进程全局 precision/rounding 被其他库修改时，指标与身份仍必须一致。

Collection hash chain 仍是内容完整性机制，不是独立数字签名。拥有数据库写权限的攻击者理论上可重写全链；因此 Stage 4H 必须使用独立 Authority identity 和签名/撤销治理。

## 11. 两阶段 Finalization

```text
TERMINAL_READY
→ FINALIZATION_PREPARED
   - deterministic immutable SignalOutcome
   - bind exact Stage 4F ledger target path + filesystem identity
→ Stage 4F ledger.append(outcome)
→ Stage 4F ledger.audit()
→ FINALIZED marker
```

恢复语义：

- Ledger 暂时失败：保留 prepared Outcome，重试同一 Outcome；
- Ledger 已提交但 marker 写失败：Stage 4F append 返回幂等，随后补 marker；
- 两个进程并发 finalize：只能有一个 FINALIZED event，另一个核对 target/outcome/record hash/append order 后返回幂等；
- 已 FINALIZED 再调用：重新核对 Stage 4F immutable record，不追加第二个终态 event；
- 相同文件路径上的 Ledger 被删除重建：filesystem identity 改变，prepared case 必须拒绝切换目标。

## 12. 安全声明

所有输出固定：

```text
trusted_outcome_admission = false
investment_performance_claim = false
auto_promote_model = false
auto_change_strategy_weight = false
auto_trade = false
production_database_modified = false
```

Stage 4G 不调用 Broker/XTP Trader/Algo Order，不推断真实成交，不修改生产 Signal，不写生产 SQLite，不自动进入真实 Scoreboard。

## 13. 后续执行顺序

### Stage 4G.1 — Operational Runtime Evidence Adapter

先完成：

- append-only Runtime Signal Transition Artifact；
- 系统生成并验证 `runtime_episode_fact_id`；
- 将 aware decision/request timestamp、策略/数据/政策身份冻结到 episode；
- 只读 Market Event Store path collector；
- 明确 manual fill 与 broker-confirmed fill 两种 evidence type；
- 重启恢复、补采、幂等 worker、授权操作 CLI/API；
- 禁止根据 quote 猜测真实成交。

### Stage 4H — Trusted Outcome Admission Authority

随后实现独立 Authority。它必须验证 Stage 4G case、Stage 4F record、外部成交/行情/日历/市场规则证据，并使用独立身份、签名、权限、PIT 生效、撤销和 separation-of-duties。其详细设计见 `STAGE4H-TRUSTED-OUTCOME-ADMISSION-AUTHORITY-DESIGN.md`。

### Stage 4I — Strategy Scoreboard API/UI

仅在存在足够、未撤销、exact-cohort admitted outcomes 后开放真实指标。否则继续显示：

```text
INSUFFICIENT_REAL_EVIDENCE
TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED
```
