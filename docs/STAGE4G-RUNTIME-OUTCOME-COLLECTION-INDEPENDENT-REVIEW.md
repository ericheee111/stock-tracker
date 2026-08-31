# Stage 4G — Runtime Outcome Collection / Finalization Core Independent Review

状态：`INDEPENDENT_REVIEW_PASSED / MANUAL_CORE_ONLY / AUTOMATIC_RUNTIME_ADAPTER_PENDING / GIT_DELIVERY_PENDING`

审查日期：2026-08-31

## 1. 修正后的审查结论

原 Stage 4G 提交可以作为 manual collection/finalization core 的起点，但“Runtime Outcome Collection/Finalization Service 已完成”的表述过度。实际代码没有接入 Runtime SignalManager、EventBus、Scheduler、Market Event Store 或 Broker，也没有 worker、CLI/API 和重启补采编排。

本轮将阶段结论修正为：

```text
MANUAL_OUTCOME_COLLECTION_CORE = ENGINEERING_COMPLETE
AUTOMATIC_RUNTIME_COLLECTION_ADAPTER = NOT_IMPLEMENTED
BROKER_EXECUTION_CAPTURE = NOT_IMPLEMENTED
TRUSTED_OUTCOME_ADMISSION = NOT_IMPLEMENTED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
INVESTMENT_PERFORMANCE_CLAIM = FALSE
AUTO_TRADE = FALSE
```

## 2. 审查范围

```text
stock_tracker/quant/storage/outcome_collection.py
stock_tracker/quant/storage/__init__.py
tests_quant/test_outcome_collection.py
docs/STAGE4G-RUNTIME-OUTCOME-COLLECTION-FINALIZATION.md
docs/PRD-股票辅助判断与交易参考网站.md
docs/PRODUCT-GAP-MATRIX-v1.1.md
```

同时核对 Stage 4F `OutcomeLedger` 接口、`SignalOutcome` 合同、运行态 `Signal` 时间语义、市场规则和下一阶段 Authority 规划。

并行 `web/**`、`qa/**` 和截图改动不属于本审查，也不得进入 hardening commit。

## 3. Findings 与修复

### G1 — CRITICAL：阶段完成状态过度声明

风险：核心库只能被 Python 调用方手工驱动，却被表述为完整 Runtime Service，容易让后续工作错误跳过自动 episode capture、path collection、restart recovery 和 operational controls。

修复：设计、PRD、Gap Matrix、Overview 与 Handoff 改为 `MANUAL_COLLECTION_CORE_COMPLETE / AUTOMATIC_RUNTIME_ADAPTER_PENDING`；下一执行点先增加 Stage 4G.1 Operational Adapter，再进入 Trusted Admission。

状态：`FIXED`。

### G2 — CRITICAL：runtime `signal_id` 不能唯一标识交易 episode

风险：运行信号 ID 长期复用，而 Stage 4F 对 Outcome signal identity 全局唯一。直接复用会让首个 Outcome 永久阻断后续策略版本或重新触发。

原修复引入了 `runtime_episode_id`，但其 identity 仍由调用时的全部 mutable snapshot 字段隐式决定，调用方改一个 score/reason 就能静默创建新 episode。

本轮修复：要求显式 `runtime_episode_fact_id`；`runtime_episode_id` 只由该外部 occurrence fact 派生。相同 fact + 相同 mode 的任一 snapshot drift 都冲突；新 episode 必须引用新的 fact ID。Stage 4F 使用 namespaced episode Outcome identity。

状态：`FIXED / EXTERNAL FACT AUTHORITY STILL PENDING`。

### G3 — CRITICAL：采集时刻被错误当作 entry intent 请求时刻

风险：原实现令 `TradeIntentEvidence.requested_at == captured_at`。迟启动、重启或补录会把实际决策时间改写为采集时间，破坏执行时序和 PIT 解释。

修复：新增显式 `entry_requested_at`。它与 collector `captured_at` 分离，均绑定到 snapshot；entry fill 只需不早于真实 request time，允许系统稍后才观察到已有事实。

状态：`FIXED / FUTURE ADAPTER MUST BIND SOURCE FACT`。

### G4 — CRITICAL：naive runtime datetime 进入正式量化身份

风险：原实现显式接受无时区 `Signal.state_changed_at` 并写入 episode/decision identity，违反仓库“无时区 datetime 不得进入正式量化身份”的硬规则。

修复：runtime state time、entry request、事件、fill、path、exit 与 audit 时间全部要求 timezone-aware，并统一通过 `OutcomeCollectionError` 边界转换为 UTC。`runtime_state_changed_at` 与 `entry_requested_at` 不得晚于首次 collection capture。

状态：`FIXED`。

### G5 — CRITICAL：不完整 path 可被标记为 complete

风险：原实现只要求至少一个 observable path point，并在有 entry fill 时设置 `path_complete=true`。entry 到 exit 之间缺失 session 会系统性低估 MFE/MAE，却仍形成 COMPLETE Outcome。

修复：要求 entry session 到 exit session 的 observable session set 精确连续；path timestamp 严格递增；holding 不得小于冻结的 `minimum_exit_session_offset`。`TIMEOUT` 不能早于配置 horizon，且 horizon evidence 必须在 exit request 时已可见；实际 fill 晚于 horizon 时仍保留额外 holding sessions，不能截断最差执行样本。Core 不再把整个 `Market.A` 永久硬编码为统一 T+1；具体 offset 必须来自版本化执行/市场规则，并由 Stage 4G.1/Authority 复核。

状态：`FIXED`。

### G6 — IMPORTANT：长仓计划边界过弱

风险：原 invalidation 检查可能允许 stop 位于 entry range 内，target 也可能不高于计划 entry/trigger。

修复：要求 `invalidation < entry_low`、`target_1 > max(entry_high, trigger)`、`target_2 >= target_1`。

状态：`FIXED`。

### G7 — CRITICAL：Collection DB 首次建库不是原子发布

风险：原实现直接连接最终路径并逐步建表。并发首次打开可能看到半初始化数据库；初始化异常会在最终路径留下损坏文件。

修复：在同目录临时 SQLite 完成 schema、metadata、commit、quick check、exact schema validation 与 fsync，再用不覆盖 hard-link 发布。并发失败者验证胜出者，清理仅作用于自己的临时文件。

状态：`FIXED`。

### G8 — IMPORTANT：`fact_id` 全库唯一造成合法跨 case 冲突

风险：同一个 market/path fact 可以被多个 episode 合法引用；`UNIQUE(fact_id)` 会错误阻断第二个 case。

修复：Collection schema v3 使用 `UNIQUE(case_id, fact_id)`，event replay 也按该复合身份检查。同一 case 内重试保持幂等，跨 case 可复用同一事实。

状态：`FIXED`。

### G9 — CRITICAL：并发 finalization 可能写第二个 FINALIZED event

风险：两个进程都完成 Stage 4F 幂等 append 后，第二个进程可能在第一个 marker 已提交后再尝试追加不同 audit/disposition 的 FINALIZED 事件，从而报错或破坏终态语义。

修复：FINALIZED marker 在单一 `BEGIN IMMEDIATE` 事务内重放 case。已完成时仅比较 ledger target、outcome、record hash 和 append order，返回 collection `IDEMPOTENT`；不会写第二个 marker。

状态：`FIXED`。

### G10 — IMPORTANT：prepared case 只绑定 Ledger 路径

风险：删除并在同一路径重建一个新 Stage 4F Ledger 时，path-only target ID 无法区分物理证据库，prepared Outcome 可能被切换到另一个 Ledger。

修复：target identity 同时绑定 canonical path 与 record root/catalog 的 filesystem device/inode，并先运行 Stage 4F 自身 identity guards。相同路径上的替换会失败关闭。

状态：`FIXED`。

### G11 — CRITICAL：公开 Case 可通过 `dataclasses.replace()` 伪造状态

风险：`OutcomeCollectionCase` 原来没有 `__post_init__`，外部代码可以只把 state 改为 FINALIZED 或删除 blocker identity，并调用 `as_dict()` 生成看似成功的报告。

修复：Case 现在重验 case ID、event hash tuple、snapshot、entry/exit/path、终态、prepared Outcome、Ledger identity 组合和派生 state。局部 replace 不能生成结构合法的伪终态。

状态：`FIXED`。

### G12 — IMPORTANT：Finalization result 没有交叉核对三方身份

风险：Result 只检查对象类型，未确认 Case、prepared Outcome 与 Stage 4F Record 是同一份结果。

修复：`OutcomeCollectionFinalizationResult` 交叉核对 prepared Outcome、Ledger Outcome、record hash、append order 与 marker audit ID。

状态：`FIXED`。

### G13 — IMPORTANT：Stage 4G v1/v2 不具备安全自动迁移条件

风险：v1 缺少外部 episode fact identity 且使用全局 fact uniqueness；v2 的退出前缀未绑定 PATH event `fact_id`，terminal reason 仍是 any-touch。静默升级到 v3 需要重算 append-only payload/event/case identities，会制造“历史未变”的假象。

处置：Collection、Exit Request、Exit Decision、Case 与 Audit 语义明确升级为 v3；v1/v2 文件只读保留并失败关闭，不自动改写。未来若确有本地实验 evidence，必须通过单独 migration artifact 显式迁移，不能把旧记录解释成 first-touch。

状态：`FAIL_CLOSED BY DESIGN`。

### G14 — CRITICAL：未建模部分成交却允许 partial entry

风险：当前单腿 Core 只有一个 entry fill、一个完整 exit fill，没有剩余委托取消/拒绝、partial fill aggregation 或多腿生命周期。原实现允许 entry fill quantity 小于 requested quantity，会把未说明去向的剩余委托静默丢弃。

修复：Collection schema v3 要求 entry fill quantity 精确等于 requested quantity；未来 partial fill 必须由 Stage 4G.1 聚合 artifact 或后续独立的原生多腿合同显式表达。

状态：`FIXED / NATIVE_PARTIAL_FILL_MODEL_PENDING`。

### G15 — CRITICAL：成交价未与可观察 session 区间核对

风险：内部类型正确的 fill price 仍可能高于该 session high 或低于 low，形成市场上不可能的 Outcome。

修复：Complete path 必须包含 entry/exit session 的 observable point；entry/exit 的 reference price 与 fill price 分别位于对应 session 所有 observable point 的合并 low/high 区间，且任何 path point 都不得位于 exit session 之后。

状态：`FIXED`。

### G16 — IMPORTANT：原子发布不支持时可能泄漏裸 `OSError`

风险：Collection DB 所在文件系统不支持 hard-link 或权限异常时，初始化会泄漏非合同异常。

修复：非 `FileExistsError` 的 publish 异常统一转换为 `OutcomeCollectionError`，并确认最终路径和临时文件均不残留。

状态：`FIXED`。

### G17 — CRITICAL：退出原因可被事后价格合理化

风险：原 TARGET/STOP 校验查看 exit fill 前的全部 path。调用方可以先以 TARGET 名义发出退出请求，再等待之后价格触及目标，从而用未来于请求的事实解释过去决策；TIMEOUT 也可能在 horizon evidence 出现前发出。

修复：`EXIT_REQUEST` v3 绑定请求时已知的最大 path 前缀。TARGET/STOP/TIMEOUT 不仅要求 market timestamp 不晚于 `exit_intent.requested_at`，还要求对应 PATH_POINT collection event 的 `observed_at` 在请求时已经发生，并执行 horizon 内 first-touch；请求后的价格或迟到回填事实不能改变已冻结 terminal reason。

状态：`FIXED`。

### G18 — IMPORTANT：市场退出限制被粗暴绑定到 `Market.A`

风险：把 `Market.A` 全部硬编码为 T+1 会混淆具体 instrument/交易日规则，也无法表达可同日回转的品种；反过来完全不约束又会接受违反冻结市场规则的 Outcome。

修复：snapshot 新增严格整数 `minimum_exit_session_offset`，绑定 episode/decision identity，并与策略 horizon 分开建模。Core 只执行该冻结值；普通 A 股股票的 operational policy 可提供 1，其他品种按其规则提供 0 或其他值，Stage 4G.1/Authority 必须核验规则来源和当时有效性。若市场/执行限制晚于策略 horizon，真实延迟成交及额外 holding sessions 必须保留。

状态：`FIXED / RULE AUTHORITY PENDING`。

### G19 — CRITICAL：市场时间戳不能证明退出请求时已经知道该事实

风险：迟到采集的 PATH_POINT 可以携带早于退出请求的 market timestamp。若只比较 `point.timestamp <= requested_at`，调用方仍可在请求后补录旧时间戳行情，回溯合理化 TARGET、STOP 或 TIMEOUT；也可通过缩短 path prefix 故意遗漏当时已知的不利事实。

修复：Case 现在保存每个 PATH_POINT 的 collection `observed_at`；path-prefix identity 同时绑定 point ID 与 known time。写入与重放都独立计算请求时已经观察到的**最大连续前缀**，拒绝遗漏已知事实、包含尚不可知事实，以及请求后使用相同 `observed_at` 追加 path 的边界绕过。

状态：`FIXED`。

### G20 — CRITICAL：Collection/Ledger 文件身份存在校验后使用竞态

风险：路径或 inode 可在预检查后、SQLite 打开前或 Ledger target 构造期间被替换，造成“校验旧文件、写入新文件”或 prepared case 错绑替换后的 Ledger。

修复：Collection connection 在打开前、打开后及成功返回前重复核对冻结的 device/inode；构造函数在冻结最终文件身份后再完整打开验证。Ledger target 使用 Ledger 打开时冻结的 root/catalog 身份，并在读取路径和身份后再次运行 Stage 4F identity guards。替换发生在任一窗口都失败关闭。

状态：`FIXED`。

### G21 — CRITICAL：宽松 SQLite schema 检查允许可执行或隐藏结构混入

风险：仅检查普通列和部分索引列名会漏掉 trigger/view、WAL sidecar、generated/hidden column、partial unique index、错误 index origin 或额外约束。恶意 trigger 可在 INSERT 后删除/改写证据，WAL 会把当前状态分散到未绑定身份的 sidecar。

修复：schema validation 要求 `journal_mode=DELETE`，拒绝所有非系统 trigger/view；使用 `table_xinfo` 精确校验 hidden 标志；精确核对 index name、columns、unique、origin 与 partial 标志；每个事务取得锁后重新验证 schema。任何额外或替代结构均失败关闭。

状态：`FIXED`。

### G22 — IMPORTANT：损坏或不可打开的 SQLite 会泄漏底层异常

风险：合法文件头但内部损坏、权限问题或连接失败可能泄漏裸 `sqlite3.Error`，破坏统一服务合同并令上层恢复逻辑难以判定。

修复：schema validation、连接建立和事务数据库错误统一转换为 `OutcomeCollectionError`；负向测试确认损坏文件和失败连接不会被改写。

状态：`FIXED`。

### G23 — IMPORTANT：可变 Runtime Signal 与无界集合可能污染 snapshot

风险：Runtime `Signal`/`ScoreSet` 是可变对象，采集期间并发刷新可能形成从未真实存在过的混合 snapshot；超大 reason/evidence 集合也可在 JSON 上限前消耗不受控资源，概率字段原边界不足。

修复：Core 只接受项目定义的 exact `Signal` / `ScoreSet` 类型，拒绝可覆写 deepcopy/equality/属性行为的子类；随后进行两次独立 deep-copy 并要求稳定相等，不稳定或不可复制对象失败关闭。evidence ID 上限 1024，正/负 reason 各上限 256，`success_probability` 严格限定为 `[0,1]`。

状态：`FIXED / IMMUTABLE RUNTIME ARTIFACT STILL REQUIRED`。

### G24 — CRITICAL：any-touch 允许事后选择更有利的 TARGET/STOP/TIMEOUT

风险：只验证“请求前曾触及所声明阈值”会允许先止损后触目标却选择 TARGET、先触目标后跌破止损却选择 STOP；TIMEOUT 也可能掩盖 horizon 内已经出现的水平障碍。粗粒度 OHLC 同一点同时跨越目标和止损时，路径无法证明先后。

修复：对请求前、horizon 内的 observable PATH 按严格时间顺序执行 first-touch。TARGET/STOP 必须等于首个水平障碍；TIMEOUT 必须存在 horizon-session evidence 且此前没有 TARGET/STOP；同一点双触发直接失败关闭，等待更细粒度且可审计的路径或未来被 Authority 明确接受的 ambiguity policy。新增 stop→target、target→stop、TIMEOUT 前障碍、同点双触发和 horizon 后触发回归。

状态：`FIXED / FINER-GRAINED AMBIGUITY EVIDENCE MAY STILL BE REQUIRED`。

### G25 — CRITICAL：退出前缀未绑定 PATH 来源事实

风险：v2 前缀只绑定标准化 `point_id + observed_at`。同一 OHLC/time 可以来自不同 raw bar、parser 或 evidence 引用；替换来源后 point ID 不变，退出决策的 known-at/source identity 仍可能被重写。

修复：Case 逐点保留 PATH event `fact_id`；该 ID 是完整 canonical PATH payload 的 hash，覆盖 `raw_bar_snapshot_id` 与 evidence IDs。退出前缀 v3 同时绑定 point ID、fact ID 与 collection `observed_at`。Collection、Exit Request、Exit Decision、Case 与 Audit schema 明确升级为 v3；v1/v2 保持原文件不变并失败关闭，不静默重算或解释为新语义。

状态：`FIXED / EXTERNAL SOURCE AUTHORITY STILL PENDING`。

### G26 — IMPORTANT：进程全局 Decimal context 可改变金融指标

风险：第三方库可修改全局 Decimal precision/rounding，导致相同成交事实得到不同 implicit cost、all-in price、R 倍数、中位数、分桶或 Scoreboard 指标，破坏跨进程确定性。

修复：Outcome 与 Scoreboard 运算使用模块内固定高精度、`ROUND_HALF_EVEN` 的 local context；新增低精度 `ROUND_DOWN` 进程上下文回归，验证 Outcome metrics/ID、Scoreboard metrics/buckets/ID 均保持一致。

状态：`FIXED`。

## 4. 残余限制

1. `runtime_episode_fact_id`、identity/data/policy IDs、evidence IDs 和 session index 当前仍由受控调用方提供；Core 只验证结构和内部一致性，不能证明外部事实真实性。
2. 没有自动 Runtime Transition Artifact、Market Event path worker 或 Broker execution adapter。
3. Live Manual 仍固定 `BEST_EFFORT / verified=false / LIVE_CANDIDATE`。
4. 只支持单次完整 entry fill 和单次完整 exit fill；scale-up、partial fills、trim 和 Trend Runner 多腿退出尚未建模。
5. 当前 no-entry 枚举只安全覆盖 `ORDER_REJECTED / DATA_INVALID`；普通未成交过期、用户撤单或 entry-validity timeout 不得借用这两个枚举伪装，Stage 4G.1 前必须冻结向后兼容的 Outcome/no-entry evidence 合同。
6. `OutcomePathPoint` 只有 point timestamp 与 session OHLC，没有 observation window start/end，也没有 `SUSPENDED / NO_TRADE / MARKET_CLOSED / MISSING_DATA` 独立类型。粗粒度日线可能包含 entry 前/exit 后价格；无 bar session 不能用伪造 OHLC 补齐。
7. Core 已验证请求时最大 known prefix、horizon 内 first-touch 和同点双触发失败关闭，但仍不能证明 PATH event 的 raw artifact、session/calendar/status、instrument rule、trailing/broken-trend/manual rule 来自独立权威；Stage 4G.1 与 Authority 必须重新读取并验证这些来源。
8. `OutcomeCollectionCase.__post_init__` 能阻断局部对象漂移，但 Python 值对象不是安全边界；权威证据来自 Collection DB 的完整 event replay。
9. Collection hash chain 没有外部签名；本地管理员可理论上重写全链。因此它不能替代 Trusted Authority。
10. 每次写入执行全量重放，优先正确性；规模增大后需设计不削弱不变量的 checkpoint/manifest。

## 5. 最终门禁

hardening commit 前必须通过：

```text
Stage 4G focused tests
Stage 4F ledger regressions
source distribution
full Quant
full Runtime
Ruff
compileall
pip check
Quant contract smoke
synthetic fixture benchmark
production migration dry-run + unchanged DB hash
Today Mock / real API-Web / Portfolio CRUD
exact scoped Git Index review
secret/generated/binary scan
git diff --cached --check
```

最终数字、tree、commit 和 push SHA 写入 `CHATGPT_HANDOFF.md`。

## 6. 下一阶段审查意见

不能直接把“实现 Authority”当成下一步唯一任务，因为 Authority 需要可验证的 Runtime episode 与外部执行证据。正确顺序是：

```text
Stage 4G.1 Operational Runtime Evidence Adapter
→ Stage 4H Trusted Outcome Admission Authority
→ admitted-sample shadow accumulation
→ Stage 4I Strategy Scoreboard API/UI
```

Stage 4H 的详细安全与治理设计见 `STAGE4H-TRUSTED-OUTCOME-ADMISSION-AUTHORITY-DESIGN.md`。
