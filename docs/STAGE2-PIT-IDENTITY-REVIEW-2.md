# Stage 2A Point-in-Time / Identity 第二轮独立金融正确性 Review

FINDING_1_KNOWN_AT: CLOSED

FINDING_2_CALENDAR_DIVERGENCE: CLOSED

FINDING_3_REPORT_REPLACE: CLOSED

FINDING_4_FUTURE_SECURITY_ASOF: CLOSED

FINDING_5_DISCONNECTED_CYCLE: CLOSED

## 1. 结论

- Review 日期：2026-08-17（Asia/Shanghai）
- 工作区：`D:\Projects\stock-tracker`
- 审查基线：`main`，`8439cde17c8050eec860e0dcdba85f267023b206`
- Verdict 1：`ENGINEERING_MERGE_BLOCKED`
- Verdict 2：`EVIDENCE_TIER_STATUS = T3_NOT_REACHED`
- License：`LICENSE_PENDING`

第一轮 3 项 CRITICAL 与 2 项 IMPORTANT 的原始攻击均已失败关闭；其中 Calendar core 与 Reconciliation 已对同一显式 predecessor graph 得出相同 terminal，报告派生字段已无法通过 `dataclasses.replace()` 注入，未来 Security/Universe 也不再污染过去的 candidate findings/counts。

但本轮独立复现发现 1 项新的 IMPORTANT：Reconciliation 先按 `source_family` 检查版本，却在真正解析 revision graph 时只按裸 `source_version` 分组。两个独立 source family 只要恰好使用相同版本字符串，就会被错误合并为一张 graph。本轮用两个完整 descriptor 绑定的 SSE synthetic streams 复现出 14 个虚假的 `CALENDAR_REVISION_BRANCH_CONFLICT`；仅修改其中一个 `source_version` 字符串后，冲突全部消失。该缺陷使工程合入继续阻断。

本轮没有新增任何真实许可、完整历史 raw、完整 revision history、完整退市 closure、官方 `source_security_id` 稳定合同、Corporate Action PIT 或 T3 joint Manifest。全部对抗性输入均为临时目录中的 synthetic fixture；benchmark 也明确输出 `synthetic_fixture_only=true`。不得据此声称 T2/T3、真实策略表现、真实模型可训练、真实概率可上线或可实盘。

## 2. 第一轮 Findings 逐项复审

### 2.1 `FINDING_1_KNOWN_AT: CLOSED`

当前实现形成了三层失败关闭：

- `scripts/capture_a_share_calendar.py:123-163` 已不再公开 `--observed-at` 或 `--known-at`；`scripts/capture_a_share_calendar.py:166-204` 在 fetch 返回后取 `datetime.now(timezone.utc)`，并令 `observed_at == known_at == retrieved_at`。
- `stock_tracker/quant/data/calendar_adapter.py:667-682` 强制 Calendar `observed_at == retrieved_at`、`known_at == observed_at` 与 `usable_from >= known_at`。
- `stock_tracker/quant/data/security_universe_adapter.py:447-461,832-836` 强制所有 candidate `known_at == observed_at`；非 synthetic candidate 还必须 `observed_at == descriptor.retrieved_at`。只有 descriptor 明确为 synthetic 时才允许历史 fixture backdating，且原有 Trust blockers 不因此关闭。

独立离线 capture（mock fetch，不访问 Provider）与非 synthetic 重标攻击结果：

```text
calendar_cli_exit                                  = 0
calendar_cli_has_observed_at_override              = false
calendar_cli_has_known_at_override                 = false
calendar_known_observed_retrieved_equal            = true
non_synthetic_security_backdating_rejected         = true
security_rejection_mentions_descriptor_retrieved_at = true
```

把 synthetic fixture 改成 `synthetic=false`、同时保留历史 observation 的攻击因 `observed_at must equal descriptor retrieved_at` 失败。`test_two_synthetic_sources_agree_but_trust_blockers_stay_open` 等完整回归同时确认 synthetic agreement 不能关闭 inherited Trust blockers。

### 2.2 `FINDING_2_CALENDAR_DIVERGENCE: CLOSED`

- `stock_tracker/quant/core/calendar.py:79-107` 的 `CalendarDay` 已保留并校验 `supersedes_revision`。
- `stock_tracker/quant/data/calendar_adapter.py:1093-1111` 的 `CandidateCalendarFact.to_calendar_day()` 已复制 predecessor edge。
- `stock_tracker/quant/core/calendar.py:204-308` 提供共享 `select_superseding_revision()`；`stock_tracker/quant/data/reconciliation.py:16-20,830-895` 直接复用它，不再维护第二套 terminal selector。

独立构造 `annual-r1(OPEN) -> r2(CLOSED) -> r10(OPEN)`，令 `r2.known_at == r10.known_at`，结果如下：

```text
resolver_linear_terminal             = r10
CalendarDay(r10).supersedes_revision = r2
TradingCalendar target OPEN          = true
Reconciliation target OPEN           = true
```

同一 probe 还确认：

```text
same-payload disconnected terminal rejected = true
same-payload cycle rejected                 = true
missing predecessor rejected                = true
future revision filtered before core graph  = true
future revision filtered before report graph = true
legacy no-supersedes snapshot days          = 14
```

因此第一轮“同一链在 core 与 Reconciliation 分裂”的原始缺陷已关闭。第 4 节的新 source-family finding 是 stream identity/scoping 缺陷，单独记录，不把原 finding 的状态改写为 STILL_OPEN。

### 2.3 `FINDING_3_REPORT_REPLACE: CLOSED`

`stock_tracker/quant/data/reconciliation.py:649-718` 只把规范输入暴露为 init 参数；以下全部为 `init=False` 且在 `__post_init__()` 内从规范输入重新推导：

- `findings`
- `inherited_trust_blockers`
- `coverage_metrics`
- `unresolved_gaps`
- `license_status`
- `evidence_tier_status`

独立构造“只有 SSE Calendar + SZSE Universe”的 HARD_BLOCK 报告后，六个派生字段的直接 `replace()` 注入全部抛出 `TypeError`。将规范输入替换为匹配的 SZSE Calendar 时则完整重算：

```text
all derived fields init                 = false
all direct forgery attempts rejected    = true
hard_before_normative_replace           = true
hard_after_matching_calendar_replace    = false
report_id_changed                       = true
rederived calendar observed date count  = 14
```

另将 `reconciliation_policy_version` 替换为新值的已有回归确认：原 HARD_BLOCK 仍保留，仅 `report_id` 重算。`candidate_snapshot_state`、`has_hard_blocks`、`has_trust_blocks` 和 `finding_counts` 均为派生 property，没有可直接注入的 constructor surface。

### 2.4 `FINDING_4_FUTURE_SECURITY_ASOF: CLOSED`

- `stock_tracker/quant/data/reconciliation.py:1077-1104` 先按 descriptor retrieval 与 candidate `known_at/usable_from` 建立 as-of-visible 投影。
- `stock_tracker/quant/data/reconciliation.py:1469-1510` 对整个未来 artifact 产生 `SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF` HARD_BLOCK；对仅部分 candidate 尚不可见的 bundle 不复用 bundle-global coverage conclusion，而保留 `SECURITY_COVERAGE_NOT_AS_OF_STABLE` Trust blocker。
- `stock_tracker/quant/data/reconciliation.py:2052-2093` 的 bundle 与 candidate coverage metrics 仅来自 visible 投影。

整个 descriptor 晚于 `report.as_of` 的主动攻击：

```text
security_bundle_count      = 0
identity_candidate_count   = 0
status_candidate_count     = 0
membership_candidate_count = 0
SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF = present
```

部分 future candidate 攻击把一个合法、已 retrieved bundle 中的 identity `usable_from` 推迟到 `as_of` 之后：baseline identity count 为 4，历史可见 count 为 3，并产生 `SECURITY_COVERAGE_NOT_AS_OF_STABLE`。

对第一轮原始攻击的更精确重放额外加入 1 条未来、缺 evidence IDs 的 membership：bundle 中 candidate 数从 8 增至 9，但报告历史可见 count 仍为 8，且未提前产生 `MISSING_SOURCE_EVIDENCE_IDS`；报告保留 `SECURITY_COVERAGE_NOT_AS_OF_STABLE`。未来记录因此不再改变过去的 candidate-specific finding 或 coverage count。

### 2.5 `FINDING_5_DISCONNECTED_CYCLE: CLOSED`

`stock_tracker/quant/core/calendar.py:244-283` 在选择 terminal 前验证每个 visible node 的 predecessor，并对所有 representative nodes 执行 DFS；不是只遍历最终选中 terminal 的祖先。独立 probe 与 checked-in 回归均确认：

```text
valid terminal + disconnected A <-> B cycle
A/B/T payload all identical
result = CALENDAR_REVISION_CYCLE / HARD_BLOCK
```

同 payload 的 disconnected terminal 产生 `CALENDAR_REVISION_BRANCH_CONFLICT`，missing predecessor 产生 `CALENDAR_REVISION_PREDECESSOR_MISSING`。payload 相同不再掩盖 graph 结构错误。

## 3. SQLite predecessor persistence 复核

`stock_tracker/quant/storage/migrations/0003_pit_universe_identity.sql:7-34` 新增：

- `supersedes_revision_kind`
- `supersedes_revision_value`
- 两列同时 NULL 或同时有效的 INSERT guard
- STRING 非空 guard
- INTEGER canonical representation guard

临时 SQLite 真实 apply/insert/readback probe 结果：

```text
applied_migrations                         = 3
persisted_predecessor                      = [STRING, annual-r1]
predecessor_update_blocked_by_append_only  = true
malformed_predecessor_pair_blocked         = true
noncanonical_integer_predecessor_rejected  = true
```

当前 SQL 行为没有复现 predecessor round-trip 丢失；但 checked-in migration test 的覆盖缺口见 `MINOR-01`。

## 4. 本轮新增 Findings

### IMPORTANT-01：不同 `source_family` 会因同名 `source_version` 被误合成一张 Calendar revision graph

- file:line：
  - `stock_tracker/quant/data/calendar_adapter.py:1417-1426`
  - `stock_tracker/quant/data/calendar_adapter.py:1499-1506`
  - `stock_tracker/quant/data/reconciliation.py:980-1004`
  - `stock_tracker/quant/data/reconciliation.py:1016-1033`
- 触发条件：两个合法、完整 descriptor 绑定的 SSE inputs 分别来自 `SSE_OFFICIAL_NOTICE_DETAIL` 和 `SSE_OFFICIAL_NOTICE_ATTACHMENT`；它们是两个独立 roots、payload 完全相同、都没有 predecessor，并恰好使用相同的 `source_version="shared-version-v1"`。
- 期望：按 `(source_family, source_version)` 分别验证两张 graph，再把两个 terminal 当作独立 streams 比较；相同 payload 不应 HARD_BLOCK。
- 实际：Reconciliation 虽在 `980-1004` 按 family 检查版本混用，却在 `1016-1029` 只以裸 `source_version` 分组，两个 roots 因而成为同一 graph 的两个 terminals。

最小复现结果：

```text
families = [SSE_OFFICIAL_NOTICE_DETAIL, SSE_OFFICIAL_NOTICE_ATTACHMENT]
same source_version:
  CALENDAR_REVISION_BRANCH_CONFLICT count = 14
distinct source_version control:
  Calendar HARD_BLOCK codes = []
```

Adapter 的 `identities` tuple 同样遗漏 `source_family`，并把输出 `CalendarCoverage/CalendarDay` 降维为 owner + version；一旦跨 family documents 被组装，core 无法从 `CalendarDay` 恢复原 stream family。结果会依赖版本字符串是否碰巧同名，而不是证据 ancestry。

- 金融/治理后果：当前已复现的是保守但错误的 HARD_BLOCK，会拒绝结构上合法且相互一致的 Calendar evidence；版本字符串重命名即可改变报告结论，破坏 deterministic reconciliation。Adapter 侧的跨-family 降维还允许本应独立的 revision roots/edges 进入同一 core stream，后续若出现 override，存在错误 precedence 的风险。
- 最小修复：Adapter identity 必须包含 `source_family`，或明确拒绝跨-family assembly；Reconciliation graph key 必须至少为 `(source_family, source_version)`，独立 stream label/report identity 同时绑定 family。若确需声明跨-family predecessor，必须有单独、显式且可审计的 authority/edge contract，不能用同名版本隐式连接。
- 必需回归：相同 owner + 相同 version + 不同 family 的同 payload roots 不得 branch；不同 payload 应在各自 terminal resolve 后产生跨-source conflict；输入顺序不得改变 report identity。
- 是否阻断工程合并：是。

### MINOR-01：migration 回归没有锁定有效 predecessor readback 与 predecessor INTEGER canonical guard

- file:line：`tests_quant/test_storage_migrations.py:219-280,312-340`
- 现状：测试检查了两列存在，并用 kind/value 一边 NULL 的 INSERT 验证了 pair guard；另一个 canonical INTEGER 测试针对的是 `pit_fact.revision_value`，不是 `quant_calendar_day.supersedes_revision_value`。测试没有插入一个合法 predecessor 后读回 kind/value，也没有对 predecessor 的 `"01"` 做精确拒绝断言。
- 当前运行行为：本轮临时 SQLite probe 已确认合法 `[STRING, annual-r1]` 可持久化/readback，`"01"` 被拒绝，且 append-only trigger 阻止后改 predecessor；因此这不是当前可复现的数据丢失 defect。
- 后果：后续迁移改动若丢 predecessor、改坏 canonical guard，现有 checked-in regression 未必直接报错；只有本轮一次性 reviewer probe 会发现。
- 最小修复：在 `test_stage2_universe_tables_are_append_only_and_status_safe` 或独立测试中增加合法 predecessor insert/readback、非 canonical INTEGER predecessor 与 predecessor update/delete 三个断言。
- 是否单独阻断工程合并：否；当前合入已由 IMPORTANT-01 阻断。

## 5. 其他要求的回归面

以下均由独立 probe 或完整 test suite 覆盖，未形成额外 finding：

- 无 `supersedes_revision` 的旧 `CalendarDay` 仍走兼容路径；14 日 snapshot 成功。
- future/not-yet-usable Calendar revision 先过滤再解析 graph，未产生虚假 predecessor/branch finding。
- identical duplicate revision 在正序与逆序输入下都稳定选择最小 identity（`a` / `a`），没有随机结果。
- SSE/SZSE Calendar 严格隔离；SZSE Universe 不借用 SSE Calendar。
- `report_id` 对输入顺序稳定，并随 policy、finding message/severity 或规范 input 改变。
- self-asserted closure approval 与 synthetic closure evidence 均不能关闭 inherited blockers。
- synthetic agreement 不能作为 corroboration；`LICENSE_PENDING` 与 `T3_NOT_REACHED` 保留。
- `UNKNOWN` risk/status 不被静默改写；非重叠 code reuse、delisting period/formal delisting、relisting 和正式退市语义保持。
- 已 EXCLUDED 的旧 instrument 不要求退市后的每日 future status，且 requirement 不会跨后续 session 重复计数。

## 6. 新鲜验证证据

### 6.1 强制门禁

| 命令 | 当前结果 |
|---|---|
| `python -m unittest discover -s tests_quant -p "test_*.py" -v` | exit 0；`Ran 295 tests`；`OK` |
| `python -m unittest discover -s tests -p "test_*.py" -v` | exit 0；`Ran 341 tests`；`OK (skipped=1)` |
| `python -m compileall -q stock_tracker tests tests_quant scripts` | exit 0 |
| `python scripts/run_quant_contract_smoke.py` | exit 0；输出 `synthetic_fixture_only=true` |
| `python scripts/run_quant_fixture_benchmark.py` | exit 0；输出 `synthetic_fixture_only=true`；不作为真实表现证据 |
| `python scripts/quant_migrate.py --database data/stock_tracker.db` | exit 0；`mode=DRY_RUN`；`applied_count=0`；`pending_count=3`；`database_modified=false` |
| `python -m pip check` | exit 0；`No broken requirements found.` |
| `ruff check stock_tracker/quant/data/reconciliation.py tests_quant/test_stage2_reconciliation.py scripts/report_stage2_coverage.py` | exit 0；`All checks passed!` |
| `git diff --check` | exit 0；仅报告 13 个既有 working-copy LF/CRLF warning，无 whitespace error |

### 6.2 生产数据库完整性

```text
before SHA-256 = 1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
after  SHA-256 = 1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

未使用 `--apply`，生产数据库未变化。

### 6.3 探索 harness 中已纠正的失败

以下 exit 1 来自 reviewer 一次性 harness 的调用/期望错误，不是仓库测试失败；均在同轮修正后 exit 0：

- Windows Python 的 `PYTHONPATH` 首次误用 `:`，造成 9 个 test module import errors；改为 `PYTHONPATH='tests_quant;.'` 后 9 个 focused regressions 全部通过。
- 首次调用不存在的 `capture_a_share_calendar._parser`；改用实际 `build_parser()` 后 known-at probe 通过。
- 首次 graph probe 预期了错误的 finding code 拼写；实现实际输出 `CALENDAR_REVISION_PREDECESSOR_MISSING`，按合同纠正断言后 probe 通过。
- 首次 future-membership probe 沿用第一轮旧 count 7；当前 fixture baseline 为 8，改为相对断言后确认 bundle 8 -> 9、历史 visible count 仍为 8。

## 7. 修改、数据与 Git 边界

- 本 Reviewer 实际新增：仅 `docs/STAGE2-PIT-IDENTITY-REVIEW-2.md`。
- 未修改：任何源码、SQL、fixtures、config、测试、第一轮 `docs/STAGE2-PIT-IDENTITY-REVIEW.md` 或生产数据库。
- 工作树在 Review 开始前已包含 Stage 1/Stage 2A tracked 与 untracked 修改；本轮未覆盖、回滚、格式化或纳入那些文件。
- 数据：只使用仓库 synthetic fixtures、临时目录、临时 SQLite 和 mock fetch；未访问真实 Provider，未把运行数据库当研究数据。
- Git：未 commit、未 merge、未 push、未创建 PR，未执行 reset/clean/rebase/checkout/restore。

## 8. 最终 Verdict

Verdict 1：`ENGINEERING_MERGE_BLOCKED`

原因：第一轮五项 finding 均已关闭，但 `IMPORTANT-01` 证明 source-family scoping 仍可让独立 Calendar streams 因同名版本被错误合并，且当前完整 suite 没有覆盖该边界。修复并增加针对性 regression 后，才适合再次进入 main review。

Verdict 2：`EVIDENCE_TIER_STATUS = T3_NOT_REACHED`

`LICENSE_PENDING`、完整历史与 revision/corporate-action/source-ID 稳定性等真实证据缺口继续保留；工程修复、synthetic tests 与 synthetic benchmark 均不能自我升级 Trust Tier。
