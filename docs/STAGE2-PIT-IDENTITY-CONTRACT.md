# Stage 2A：A 股 PIT 身份基础合同

> 日期：2026-08-17
> 状态：核心合同、Adapter/Reconciliation 与独立 Review remediation 已实现；权威真实数据接入尚未完成
> 范围：Exchange Calendar + Security Identity/Status + Historical Universe
> 目标：为后续公司行为、真实行业、回测、校准、Replay 和 Big Trend 提供不可绕过的时间身份

---

## 1. 为什么先做身份，而不是继续加模型

真实模型、策略战绩和 Replay 的上限首先由数据身份决定。以下缺陷会直接制造虚假的优秀结果：

- 用今天仍上市的股票回填过去，形成 survivorship bias；
- 未保留退市、风险警示和长期停牌样本；
- 把“接口今天返回的状态”当作历史上当时已知的状态；
- 只记录 `known_at`，忽略数据真正允许进入决策的 `usable_from`；
- 混用多个 Calendar/Universe 版本；
- 缺少某只证券时静默解释为“不在 Universe”；
- 通过改 descriptor、布尔开关或重新计算 ID 自我升级到研究级。

因此 Stage 2A 先冻结：

```text
known_at <= usable_from <= as_of
Calendar Source/Version 唯一
Universe Source/Version 唯一
证券永久身份使用 instrument_id / source_security_id，不使用 symbol 作为永久主键
每个 membership 必须有 identity + 当日 status
退市样本必须保留
缺失必须失败关闭
Snapshot ID 必须由内容重算验证
```

Agent A 的权威来源审计已确认：当前免费/近零成本公开来源最多先形成带缺口的 T2 evidence bundle，状态保持 `LICENSE_PENDING / T3_NOT_REACHED`。Adapter 无权设置 `verified=true` 或 `complete=true`。

---

## 2. 已实现文件

```text
stock_tracker/quant/core/calendar.py
stock_tracker/quant/core/universe.py
stock_tracker/quant/core/__init__.py
stock_tracker/quant/storage/migrations/0003_pit_universe_identity.sql

tests_quant/test_calendar.py
tests_quant/test_universe.py
tests_quant/test_storage_migrations.py
```

这些文件属于研究链，不接入生产信号，不修改运行数据库，不自动训练或晋级模型。

---

## 3. Calendar 可见性

`CalendarDay`、`CalendarCoverage` 和 `InstrumentSessionStatus` 现在都具有：

```text
known_at
usable_from
revision
verified
source
```

兼容规则：

- 老调用方未提供 `usable_from` 时，规范化为 `known_at`；
- 新权威 Adapter 必须显式写入 UTC 归一化 `usable_from`；
- `usable_from < known_at` 直接拒绝；
- **没有独立、内容绑定的历史时间权威时，`observed_at = known_at = raw retrieved_at`**；网页上的发布日期/日期只能记录为 `source_published_at`，不能把 first-known 回填到更早时间；
- 当前 Calendar capture CLI 不再接受任意 `--observed-at/--known-at` 参数，首次可知时间由 exact-raw capture 自动生成；
- 非 synthetic Security/Universe candidate 同样要求 `observed_at = known_at = descriptor.retrieved_at`；只有明确标记 synthetic 的历史测试 fixture 可以使用人为历史时间；
- Snapshot 同时要求 `known_at <= as_of` 和 `usable_from <= as_of`；
- 已知但尚不可用于决策的修订不得进入 Snapshot；
- Calendar 修订只能通过显式 `supersedes_revision` 图选取，禁止用 `revision_id` 字典序/数字外观推断新旧；cycle、missing predecessor 和冲突 terminal 必须失败关闭；
- Calendar revision graph 的 stream identity 是 `(exchange, source_family, source_version)`；不同 family 即使 version 字符串相同也先独立解析，随后才比较 terminal 语义；
- Adapter 不允许把不同 source family 的 documents 组装成一个 core stream；`CalendarCoverage.source` 与 `CalendarDay.source` 必须保留 owner/family 身份。

迁移 0003 为现有三张 Calendar/Status 表增加 nullable `usable_from`，并为 `quant_calendar_day` 增加 `supersedes_revision_kind/value` 及插入校验。旧行的 `NULL` 由读取层按 `known_at` / 无 supersedes 解释；不得通过更新旧 append-only 行伪造历史可用时间或 revision ancestry。

---

## 4. Security Identity

`InstrumentIdentityFact` 表示一段有效期内的证券身份：

```text
instrument_id          # 稳定内部身份，真实 Adapter 由 exchange + source_security_id 映射
symbol                 # 目标有效期内的可变交易代码，不是永久主键
market
exchange
security_type
effective_from
effective_to
known_at
usable_from
source
revision
verified
```

硬规则：

- `instrument_id` 必须非空，真实数据不得简单从当前 symbol 反推永久身份；
- symbol 必须是规范大写形式；
- `.SH/.SZ` 才可属于 A 股；
- 代码变更时，同一 `instrument_id` 可以在不同有效期映射到不同 symbol；
- 有效期不得倒置；
- identity 必须在目标 session 有效；
- 未核验 identity 默认不能进入正式 Snapshot。

---

## 5. 每日证券状态

`SecurityStatusFact` 绑定一个证券和一个交易日：

```text
listing_state:
  PRE_LISTING | LISTED | DELISTING | DELISTED

trading_state:
  TRADABLE | SUSPENDED | HALTED | UNKNOWN

risk_designation:
  NORMAL | ST | STAR_ST | RISK_WARNING | OTHER | UNKNOWN
```

硬规则：

- `PRE_LISTING` 和 `DELISTED` 不得同时为 `TRADABLE`；
- 每个被 Universe 历史提及的 symbol 在目标 session 必须有一个可见 status；
- `SUSPENDED` 不等于从 Universe 删除；
- `DELISTED` 样本必须保留在历史身份中，不能被当前证券列表过滤掉。

---

## 6. Historical Universe

Universe 使用事件语义：

```text
UniverseMembershipState.INCLUDED
UniverseMembershipState.EXCLUDED
```

每条 membership 包含：

```text
universe_id
instrument_id
symbol
market
effective_date
state
known_at
usable_from
source
universe_version
revision
verified
reason
```

Snapshot 规则：

1. 选择一个覆盖目标日期的 Universe Coverage；
2. 默认要求 Coverage `verified=true` 且 `complete=true`；
3. 同一 Universe 请求只允许一个 source/version；
4. 只选择在 `as_of` 可见且可用的修订；
5. 每个 `instrument_id` 取不晚于目标 session 的最新 membership 事件；symbol 只由目标日有效 identity 提供；
6. membership 的缺失不能自动解释为 EXCLUDED；
7. INCLUDED membership 必须用同一 `instrument_id` 绑定目标日 active identity 和目标日 status；
8. EXCLUDED membership 只要求 exclusion date 当时有效的 identity，以及不晚于 exclusion date 的最后可见 status；不得为退市后的目标日伪造每日 status；
9. INCLUDED 成员不能处于 PRE_LISTING 或 DELISTED；
10. EXCLUDED/DELISTED 样本仍保留在 Snapshot 身份中；
11. 相同 `known_at + revision` 但内容冲突时失败关闭。

`member_symbols` 只返回 INCLUDED；`tradable_symbols` 进一步要求当日 TRADABLE；`delisted_symbols` 用于证明退市样本没有被删除。

---

## 7. Research Identity Snapshot

`build_research_identity_snapshot()` 将以下对象绑定：

```text
CalendarSnapshot
UniverseSnapshot
Security Status Snapshot ID
```

进入 Research Identity 的条件：

- market 相同；
- as-of 完全相同；
- 目标日期在 Calendar 中且为 OPEN；
- Calendar Coverage 和 Calendar Days 全部 verified；
- Universe 使用 `require_verified=true`；
- Universe 使用 `require_complete=true`；
- Snapshot ID 与实际内容一致。

因此以下操作不能升级为研究身份：

- `require_complete=false` 的临时 Universe；
- `require_verified=false` 的 Calendar；
- 手工替换 `snapshot_id`；
- 只提供当前成分、不提供 EXCLUDED/退市历史；
- 缺少某个 member 的 identity 或 status。

---

## 8. SQLite Migration 0003

新增 append-only 表：

```text
quant_universe_coverage
quant_instrument_identity
quant_security_status
quant_universe_membership
```

并为旧表增加 `usable_from`：

```text
quant_calendar_coverage
quant_calendar_day
quant_instrument_session_status
```

所有新增事实表具有：

- 64 位内容身份主键；
- revision kind/value 原类型保存；
- enum/check 约束；
- lookup index；
- `BEFORE UPDATE` 失败触发器；
- `BEFORE DELETE` 失败触发器。

迁移仍由 `scripts/quant_migrate.py` 管理，默认必须 dry-run。未经明确授权不得对 `data/stock_tracker.db` 使用 `--apply`。

---

## 9. Reconciliation 派生不变量

`ReconciliationReport` 是“规范输入 → 确定性派生结果”，不是允许调用方传入分析结果的普通 DTO：

```text
可构造输入：
calendar_inputs
security_universe_inputs
as_of
policy_version
closure_requests
external_closure_evidence
additional_findings

只读派生：
findings
inherited_trust_blockers
coverage_metrics
unresolved_gaps
candidate_snapshot_state
```

硬规则：

- 派生字段均 `init=False`，`dataclasses.replace()` 不能单独删除 HARD/TRUST finding、blocker 或 coverage；
- 改变任何规范输入会重新执行完整 reconciliation；
- Security/Universe artifact 若 `retrieved_at > as_of`，其 candidate 不进入历史 findings/coverage，并产生 `SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF`；
- bundle 内存在 as-of 尚不可见 candidate 时，只使用可见 candidate 做结构分析，且 `SECURITY_COVERAGE_NOT_AS_OF_STABLE` 保持 Trust blocker，不能拿未来生成的 bundle-global coverage 证明过去完整；
- SSE/SZSE Calendar observed/open 集合严格隔离；
- Stage 2A 没有可信 external closure authority，所有 inherited blocker 继续 `OPEN`。

---

## 10. 当前证据等级

本轮验证是：

```text
SYNTHETIC_VALIDATED / CONTRACT_ONLY
```

它证明：

- PIT 修订选择逻辑可运行；
- survivorship/退市样本合同存在；
- 缺失与冲突会失败关闭；
- Snapshot ID 稳定且不可任意重标；
- Migration 可在临时 SQLite 应用并保持 append-only。

它不证明：

- 已接入权威交易所历史日历；
- 已拥有完整 A 股上市/退市/停牌/ST 历史；
- Universe Coverage 的 `complete=true` 已被真实证据支持；
- 数据已经达到 T3 `RESEARCH_GRADE`；
- 任何策略、胜率、收益、Sharpe 或回撤表现。

---

## 11. 下一接入顺序

```text
1. 权威来源与许可审计
2. Calendar exact raw capture + deterministic parse
3. Security master/status exact raw capture + deterministic parse
4. Historical Universe membership importer
5. 覆盖率、缺口和跨源 reconciliation
6. Corporate Action / adjustment identity
7. 组装首个真实 A 股 Research Identity Snapshot
8. 只有证据充分后，才组装 T3 DataSnapshotManifest
```

任何 Adapter 都只能输出事实，不得自行设置 `complete=true` 或 `verified=true`。这些等级必须由来源审计、覆盖率和 reconciliation 的新增证据决定。
