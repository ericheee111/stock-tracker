# Stage 2A Agent D Reconciliation / Coverage Gap Handoff

> 日期：2026-08-17
> 工程状态：`ENGINEERING_READY_FOR_MAIN_REVIEW`（两轮独立 Review findings 经主车道修复并完成完整门禁）
> 数据证据状态：`CONTRACT_ONLY / SYNTHETIC_VALIDATED / T2_CANDIDATE_EVIDENCE`
> 许可状态：`LICENSE_PENDING`
> 研究级状态：`T3_NOT_REACHED`

## 1. 修改文件

```text
stock_tracker/quant/data/reconciliation.py
scripts/report_stage2_coverage.py
tests_quant/test_stage2_reconciliation.py
docs/STAGE2-RECONCILIATION-HANDOFF.md
```

没有新增静态 `tests_quant/fixtures/reconciliation/**`。定向测试在临时目录中通过 Agent B 的 exact-raw/parse-descriptor 公共入口生成 synthetic Calendar 树，并复用 Agent C 已有 synthetic artifact；这样不会把 raw 或新的伪官方数据提交到仓库。

未修改：

```text
stock_tracker/quant/core/**
stock_tracker/quant/storage/**
stock_tracker/quant/data/calendar_adapter.py
stock_tracker/quant/data/security_universe_adapter.py
stock_tracker/quant/data/__init__.py
Agent B/C 测试和 fixtures
Stage 1 文件
data/stock_tracker.db
```

未执行 `git add`、`commit`、`merge`、`push`、`reset`、`clean`、`checkout`、`switch`、`restore` 或 `stash`。

### 1.1 主车道 Review 后修复

Agent D 初版完成后，主车道在放独立 Reviewer 前发现并直接修复了以下合入阻断或报告正确性问题：

- **Trust closure 自我批准路径**：`synthetic=false + independently_approved=true` 原本仍由调用方自报；Stage 2A 没有可信外部批准注册表/签名，因此现在所有 closure request 都保持 `OPEN` 并产生 `BLOCKER_CLOSURE_REJECTED`。`CLOSED_WITH_EVIDENCE` 仅保留为未来治理 schema，不在 Stage 2A 可达；
- **直接构造 `ReconciliationReport` 绕过**：dataclass 自身现在重新推导 base blocker、Calendar gaps、C `trust_blocker_codes` 和 synthetic blocker；不得省略 `LICENSE_PENDING/T3_NOT_REACHED`、不得传入 CLOSED blocker，也不得从 `unresolved_gaps` 删除 HARD/TRUST 结论；
- **交易所 Calendar 串用**：SSE/SZSE 的 observed/open session 按 exchange 独立，SZSE Universe 不能借 SSE OPEN date 通过检查；
- **Calendar revision 选择**：不再用 `revision_id` 字符串排序。每个 source-version stream 只按显式 `supersedes_revision_id` 图选择终点，并检测 cycle、missing predecessor、branch conflict；
- **跨来源版本语义**：不同独立 source 可以有各自 `source_version`；只有同一 source identity/source family 内混版本才 HARD_BLOCK；
- **Universe membership 跨源冲突**：同 instrument/session 的 `INCLUDED` vs `EXCLUDED` 直接 HARD_BLOCK；state 相同但 reason 冲突为 TRUST_BLOCK；
- **Synthetic corroboration**：只要任一 Security/Universe input 是 synthetic，就保留 `SYNTHETIC_EVIDENCE_NOT_CORROBORATION`，不能和真实输入混合后消失；
- **as-of coverage**：未来尚不可见/不可用 Calendar facts 不计入 observed coverage；
- **Coverage status 计数**：EXCLUDED 证券的同一退出状态 requirement 跨后续多个 session 去重，不虚增 required/observed status；
- **CLI 输入不可变**：JSON/Markdown 输出不得彼此同路径，也不得覆盖 security artifact/descriptor、Calendar parse/raw descriptor 或 raw artifact；
- **报告身份**：被拒绝 closure 的 requested reason/policy 也进入 finding，因此改变 rejected request 会改变 `report_id`；
- **输入身份格式**：关键 artifact/descriptor/bundle/report IDs 在 D 合同边界要求 lowercase SHA-256。

这些修复均有 adversarial 回归测试，不改变 `LICENSE_PENDING / T3_NOT_REACHED`。

### 1.2 独立 Reviewer 第一轮 remediation（待二次独立 Review）

`docs/STAGE2-PIT-IDENTITY-REVIEW.md` 第一轮 verdict 为 `ENGINEERING_MERGE_BLOCKED / T3_NOT_REACHED`，提出 3 个 CRITICAL 和 2 个 IMPORTANT。主车道已逐项修复并增加独立复现：

1. **first-known 回填**：Calendar raw capture 现在强制 `observed_at = known_at = retrieved_at`；CLI 删除 `--observed-at/--known-at`。非 synthetic Security/Universe candidate 同样强制 first observation/knowledge 等于 descriptor retrieval；网页发布日期只保留 `source_published_at`，不能回填 `known_at`。
2. **Calendar core/Reconciliation 分叉**：`CalendarDay` 新增 `supersedes_revision`，Adapter→core 不再丢失 revision edge；core 和 Reconciliation 共用 `select_superseding_revision()`，只按显式 supersedes graph 选择 terminal，并验证所有 visible node 的 cycle、missing predecessor、terminal conflict。
3. **ReconciliationReport replace 绕过**：`findings / inherited_trust_blockers / coverage_metrics / unresolved_gaps` 改为 `init=False` 派生字段。报告只能由规范输入构造，任何可变 input 的 `replace()` 都会重跑完整 reconciliation；不能单独注入/删除 HARD_BLOCK。
4. **future Security/Universe 污染**：artifact `retrieved_at > as_of` 时不使用任何 candidate，并 HARD_BLOCK；artifact 已存在但含 future candidate 时只投影 as-of 可见内容，同时保留 `SECURITY_COVERAGE_NOT_AS_OF_STABLE`，不使用未来 bundle-global coverage 证明过去完整。
5. **断开同 payload cycle**：共享 revision graph resolver 会遍历所有 visible nodes，因此即使 cycle 与另一个 terminal payload 相同，也会失败关闭。

此外 migration 0003 为 `quant_calendar_day` 增加 `supersedes_revision_kind/value` 和插入 guard，防止内存 revision graph 在未来 SQLite round-trip 中丢失。

当前 remediation 门禁已通过；第二轮独立 Reviewer 已确认上述五项全部 `CLOSED`。

### 1.3 第二轮 Reviewer blocker remediation

`docs/STAGE2-PIT-IDENTITY-REVIEW-2.md` 新发现 `IMPORTANT-01`：Reconciliation 在真正解析 revision graph 时只用裸 `source_version` 分组，导致不同 `source_family` 恰好同名 version 时被误合成一张图。主车道已完成：

- Calendar stream identity 冻结为 `(exchange, source_family, source_version)`；
- 同 family 内 version/parser mixing 继续失败关闭；
- 不同 family 即使 version 字符串相同，也先独立解析各自 supersedes graph；
- terminal 语义一致时不产生虚假 branch conflict；terminal 语义不一致时产生真正的 `CALENDAR_OPEN_CLOSED_CONFLICT` 或 `CALENDAR_SESSION_CONFLICT`；
- `CalendarReconciliationInput.input_id`、finding scope/details 与 `report_id` 均绑定 source family；
- Adapter 拒绝把不同 source family 的 documents 组装成同一个 core stream；
- `CalendarCoverage.source` / `CalendarDay.source` 使用 `owner/family` 身份，避免进入 core 后降维；
- 增加同名 version 跨 family 一致、冲突、输入顺序稳定三类回归。

第二轮 `MINOR-01` 也已补齐：migration 测试现在实际插入并读回合法 predecessor，验证 predecessor UPDATE/DELETE 被 append-only 阻止，并精确拒绝 `INTEGER "01"`。

主线最终门禁：Calendar Adapter 25、Reconciliation 37、完整 Quant 298、Runtime 341（另 1 个既有 localhost live probe 跳过）、真实 Stage 1 Web/API QA 17+13 全部通过。工程实现可以进入主线提交；证据等级继续保持 `LICENSE_PENDING / T3_NOT_REACHED`。

## 2. Reconciliation schema

报告 schema：

```text
stage2-reconciliation-report-v1
```

默认 policy version：

```text
stage2-reconciliation-policy-v1
```

`ReconciliationReport` 绑定：

- `schema`；
- `reconciliation_policy_version`；
- timezone-aware UTC `as_of`；
- 所有 Calendar input identity；
- 所有 Security/Universe input identity；
- canonical findings；
- inherited trust blocker 状态（Stage 2A 当前全部必须 `OPEN`）；
- rejected closure request 的 evidence IDs、requested reason / policy（通过 finding 进入 identity）；
- coverage metrics；
- unresolved gaps；
- 固定 `LICENSE_PENDING`；
- 固定 `T3_NOT_REACHED`；
- candidate snapshot 结构状态与 finding counts。

报告输出禁止出现：

```text
verified
complete
trust_tier
t2_achieved
t3_achieved
research_grade
```

即使值为 `false`，这些字段也不能进入 Agent D 报告，避免调用方把模糊布尔值当晋级合同。

## 3. Report identity

`report_id` 是上述完整 payload 的 canonical SHA-256。

所有输入先按 `input_id` 排序，finding、subject/evidence/detail、blocker、gap 也 canonicalize。因此语义相同但输入顺序不同，`report_id` 不变。

以下任一变化都会改变 `report_id`：

- raw descriptor / parse descriptor / raw artifact identity；
- Calendar document 或 parser/source version；
- Security bundle / normalized artifact / coverage report identity；
- Security parser/schema/source version；
- `as_of`；
- policy version；
- finding code、message、severity、scope、subject、evidence 或 detail；
- inherited blocker `OPEN` 集合；
- rejected closure request 的 evidence IDs、requested reason 或 policy；
- coverage metric 或 unresolved gap。

## 4. Finding severity

只允许：

| Severity | 语义 |
|---|---|
| `HARD_BLOCK` | candidate Snapshot 本身不能安全构造或输入身份已不可信 |
| `TRUST_BLOCK` | 可以调试或构造 candidate，但不能 verified、complete 或 Trust 晋级 |
| `WARNING` | 不破坏当前身份选择，但必须保留人工/revision audit |
| `INFO` | 普通 coverage、symbol change、合法 code reuse 或 provenance 信息 |

输入 schema/hash/descriptor/binding/redirect/parser 篡改在报告生成前抛出带 `HARD_BLOCK` severity 的 `ReconciliationInputError`。CLI 返回 non-zero，且不写成功报告；downstream normalized candidate 不能覆盖这类错误。

## 5. Inherited blocker closure

Schema 为未来治理预留两种 blocker status：

```text
OPEN
CLOSED_WITH_EVIDENCE
```

但 **Stage 2A 当前没有可信 external closure authority**（没有独立批准注册表、签名或受信任审批 artifact）。因此当前工程合同进一步失败关闭：

- `ReconciliationReport` 中所有 inherited blocker 必须是 `OPEN`；
- 即使调用方构造 `synthetic=false`、`independently_approved=true` 的 `ExternalClosureEvidence`，也不能自行关闭 blocker；
- 任何 closure request 都保持 `OPEN`，并产生 `BLOCKER_CLOSURE_REJECTED / TRUST_BLOCK`；
- rejected request 的 evidence IDs、requested reason 和 policy version 都进入 finding/report identity；
- `CLOSED_WITH_EVIDENCE` 只作为未来有可信 closure authority 后的 schema 预留，本阶段不可达。

未来若单独设计可信 closure authority，至少仍需内容寻址 evidence、非 synthetic、独立批准、目标 blocker code、匹配 evidence kind，并在 upstream raw closure 时绑定 exact raw artifact IDs；该能力不属于当前 Stage 2A。

当前以下全部保持 `OPEN`：

```text
ADAPTER_UNVERIFIED_INCOMPLETE
SOURCE_SECURITY_ID_STABILITY_UNPROVEN
UPSTREAM_RAW_PROVENANCE_INCOMPLETE
LICENSE_PENDING
T3_NOT_REACHED
SYNTHETIC_EVIDENCE_NOT_CORROBORATION
```

## 6. Agent B input contract

正式入口是：

```python
CalendarReconciliationInput.from_parse_descriptor(...)
```

其内部必须调用：

```python
parse_calendar_from_descriptor()
load_calendar_parse_descriptor()
```

报告绑定：

- `parse_descriptor_id` / `parse_descriptor_key`；
- `raw_descriptor_id` / `raw_descriptor_key`；
- `raw_artifact_id`；
- Calendar parser version；
- source owner/family/version；
- candidate document ID 与所有 fact IDs；
- B 原始 gaps。

raw hash、raw descriptor、parse descriptor、raw/parse binding、parser version、redirect owner-domain 任一失败都会停止报告生成。Annual weekday inference 只作为 candidate；later explicit notice 在合法 revision chain 中优先，同时保留 warning 与 gap audit。

以下 B gap 不因 civil-date 完整而消失：

```text
LICENSE_PENDING
SINGLE_SOURCE_NOT_RECONCILED
TEMPORARY_AND_TECHNICAL_NOTICE_COVERAGE_UNPROVEN
WEEKDAY_OPEN_BASELINE_INFERRED
```

## 7. Agent C input contract

CLI 输入是 C 的原始 normalized candidate artifact 和 checksum-bound descriptor，重新调用：

```python
read_security_universe_descriptor()
parse_security_universe_artifact()
```

报告绑定：

- normalized artifact ID；
- descriptor ID；
- bundle ID；
- coverage report ID；
- source / dataset / source version；
- parser / schema version；
- exchange / universe ID；
- identity/status/membership candidate IDs；
- `required_session_dates`；
- C 全部 `trust_blocker_codes`。

`has_snapshot_blockers=false` 只影响 candidate 结构判断，不清空 C trust blockers。SSE 与 SZSE 保持 `A_SHARE_SSE_ALL` / `A_SHARE_SZSE_ALL` 独立；D 不生成 `A_SHARE_ALL`。

INCLUDED 要求 target-session active identity 和 target-session DAILY status。EXCLUDED 只要求 exclusion date active identity，以及不晚于 exclusion date 的最后可见 DAILY status。不会为已退市旧证券要求或伪造未来 target-session status。

`RiskDesignation.UNKNOWN` 保持 UNKNOWN。与另一来源的 NORMAL/OTHER/缺省值相遇时输出 Trust conflict，不做自动多数选择。

## 8. Reconciliation coverage

已实现检测包括：

- Calendar civil-date gap、按交易所独立的 observed/open session、同 source family 内 source/parser version mixing、OPEN/CLOSED/session conflict、显式 supersedes revision graph（含 cycle/missing predecessor/branch conflict）、known/usable/as-of、inferred vs explicit revision、parser/provenance input identity；
- instrument/source-security identity instability、symbol change、同 instrument 不兼容 symbol overlap、identity interval conflict、同 symbol 跨期合法 reuse、同 session 多 INCLUDED instrument conflict、relisting continuity ambiguity、market/exchange conflict；
- membership 缺 identity、INCLUDED 缺 target status、EXCLUDED 缺 exit/last-visible status、INCLUDED + DELISTED、跨源 INCLUDED/EXCLUDED state conflict 与 reason conflict；
- ST/*ST、suspension、intraday overlap、delisting-period/delisted、UNKNOWN 和 future correction conflict；
- current anchor 当 history、absence 当 EXCLUDED、缺 historical exit/delisting、unclosed delisting、quantity continuity、SSE/SZSE mixing、premature union；
- normalized candidate 未闭合 upstream exact raw、source ID provenance、missing evidence IDs；
- synthetic fixture agreement 不构成 independent corroboration。

Coverage metrics 显式记录 as-of 可见的 Calendar expected/observed/missing/open dates，以及 identity/status/membership、INCLUDED/EXCLUDED、required/observed/missing status 和 unclosed delisted instrument counts。安全校验按 SSE/SZSE 独立 Calendar 集合执行；EXCLUDED 的同一退出状态 requirement 跨后续 session 只计一次。任何单个退市缺口不会被高 coverage 比例忽略。

## 9. CLI

```text
scripts/report_stage2_coverage.py
```

输入：

```text
--calendar-root
--calendar-parse-descriptor (repeatable)
--security-artifact (repeatable)
--security-descriptor (repeatable, one-to-one pairing)
--as-of
--policy-version
--json-output
--markdown-output
```

CLI 不含 Provider、network、database、migration、trust-tier 或 promotion 参数。它只读显式文件并原子写 JSON / Markdown；两个输出路径必须彼此不同，且不得覆盖任一 security artifact/descriptor、Calendar parse/raw descriptor 或 exact raw artifact。路径比较使用解析后的本地文件身份，避免明显的别名覆盖。

退出码：

```text
0 = report successfully generated
2 = input schema/hash/descriptor/tamper or report contract error
```

exit 0 不表示 Trust 通过。stdout 显式输出 `report_generated=true` 与 `trust_passed=false`。

## 10. Synthetic fixture boundary

本交付只使用：

- Agent B synthetic Calendar HTML；
- Agent C `golden_sse.json` synthetic normalized candidate；
- 测试内临时 boundary mutation。

`tests_quant/fixtures/stage2_aux/**` 没有被当作第二来源、corroboration、Trust evidence 或 closure evidence。WorkBuddy synthetic 与 C synthetic 即使结果完全一致，也只产生 `SYNTHETIC_EVIDENCE_NOT_CORROBORATION`。

没有访问真实 SSE/SZSE/CNINFO/ChinaClear 数据，没有真实历史 coverage 结论，没有真实策略、回测、训练、校准、胜率、收益、Sharpe 或回撤结论。

## 11. 当前验证结果

```text
python -m unittest discover -s tests_quant -p "test_stage2_reconciliation.py" -v
  PASS: 37 tests

python -m compileall -q stock_tracker tests tests_quant scripts
  PASS: exit 0

python -m unittest discover -s tests_quant -p "test_*.py" -v
  PASS: 298 tests

python -m unittest discover -s tests -p "test_*.py" -v
  PASS: 341 tests
  SKIP: 1 existing :8080 live-service probe

python scripts/run_quant_contract_smoke.py
  PASS: passed=true
  synthetic_fixture_only=true
  production_database_modified=false
  migration_count=3

python scripts/run_quant_fixture_benchmark.py
  PASS: exit 0
  synthetic_fixture_only=true
  investment_performance_claim=false
  promoted=false

python -m pip check
  PASS: No broken requirements found.

ruff check stock_tracker/quant/data/reconciliation.py tests_quant/test_stage2_reconciliation.py scripts/report_stage2_coverage.py
  PASS: All checks passed!

python scripts/quant_migrate.py --database data/stock_tracker.db
  PASS: DRY_RUN
  applied_count=0
  pending_count=3
  database_modified=false

production DB SHA-256 before/after validation
  1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
  unchanged=true

git diff --check
  PASS: exit 0
  only existing LF/CRLF warnings

standalone subprocess CLI smoke
  PASS: exit 0
  JSON exists=true
  Markdown exists=true
  report_id matches=true
  HARD_BLOCK=0
  TRUST_BLOCK=12
  trust_passed=false
  LICENSE_PENDING
  T3_NOT_REACHED
```

最终 `git diff --check` 结果以 Agent D 最终汇报为准。

## 12. 当前限制与结论

未发现需要越权修改 B/C/core 的 shared-contract blocker。

仍未闭合：

- 许可；
- 真实官方 raw bundle；
- `source_security_id` 跨代码变更、重新上市和身份变化的稳定合同；
- C normalized candidate 到 SSE/SZSE/CNINFO/ChinaClear exact upstream raw 的链；
- 临时/技术 Calendar notice 全历史覆盖；
- 退市逐证券闭环与数量连续性；
- SSE/SZSE 跨源真实 reconciliation；
- T3 joint Manifest 和独立晋级决定。

当前结论必须继续保持：

```text
LICENSE_PENDING
T3_NOT_REACHED
```

本交付只达到工程合同与 synthetic adversarial coverage。两轮独立 Review 的全部工程 findings 已经修复并进入回归；主线最终状态为：

```text
ENGINEERING_READY_FOR_MAIN_REVIEW
EVIDENCE_TIER_STATUS = T3_NOT_REACHED
```

该工程 verdict 只允许提交合同、Adapter、Reconciliation、测试与文档，不允许把任何 candidate 数据或 synthetic benchmark 宣称为真实研究级证据。
