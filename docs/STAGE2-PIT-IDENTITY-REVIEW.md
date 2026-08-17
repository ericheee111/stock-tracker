# Stage 2A Point-in-Time / Identity 独立金融正确性 Review

## 1. 结论

- Review 日期：2026-08-17（Asia/Shanghai）
- 工作区：`D:\Projects\stock-tracker`
- 审查基线：`main`，`8439cde17c8050eec860e0dcdba85f267023b206`
- Verdict 1：`ENGINEERING_MERGE_BLOCKED`
- Verdict 2：`EVIDENCE_TIER_STATUS = T3_NOT_REACHED`

当前实现保留了 `LICENSE_PENDING`、`T3_NOT_REACHED` 及 Agent C 的 inherited Trust blockers，完整测试也全部通过；但是独立对抗性复现确认了 3 项 CRITICAL 和 2 项 IMPORTANT 缺陷。最严重的问题是：调用方可以回填没有证据支撑的 `known_at`；同一条 `annual-r1 -> r2 -> r10` Calendar 链在 Reconciliation 与核心 `TradingCalendar` 中会得出相反的 OPEN/CLOSED 结论；并且 `dataclasses.replace()` 可以删除已计算出的 HARD_BLOCK 后把报告改成 `STRUCTURALLY_CONSTRUCTIBLE`。因此不能工程合入。

本次没有发现任何此前不存在的新真实来源证据。所有功能验证和对抗性复现均为本地代码、临时目录和 synthetic fixtures；不得据此声称 T2/T3、真实回测有效、真实模型可训练、真实概率可上线或可实盘。

## 2. Findings

### CRITICAL-01：`known_at` 可以在无可证明来源时间证据时任意回填

- file:line：
  - `stock_tracker/quant/data/calendar_adapter.py:667-676`
  - `stock_tracker/quant/data/security_universe_adapter.py:446-454`
  - `scripts/capture_a_share_calendar.py:153-155,202-231`
  - 合同依据：`docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md:62-67,243-256`
- 触发条件：exact raw 直到 2024 年才被 `observed_at/retrieved_at` 捕获，来源只有较早的 DATE 级发布日期；调用方把 `known_at` 和 `usable_from` 手工填写为 2020 年。现有校验只要求 `known_at <= observed_at <= retrieved_at` 和 `known_at <= usable_from`，没有要求 `known_at` 由“首次可证明观察”或可靠、已绑定的服务端时间证据产生。Calendar CLI 还直接公开了不受证据约束的 `--known-at`。
- 最小复现：

  ```text
  Calendar observed_at = 2024-01-01T00:00:00Z
  Calendar known_at    = 2020-01-01T00:00:00Z
  Security observed_at = 2024-01-15T07:00:00Z
  Security known_at    = 2020-01-01T00:00:00Z
  result                = both accepted
  ```

  复现分别通过 `dataclasses.replace(CalendarProvenance, ...)` 与 `CandidateProvenance.from_mapping(...)`，没有修改仓库文件。
- 金融后果：2024 年首次获得的公告、身份或 Universe 修订可以进入 2020 年的 Replay、标签、训练集和校准窗口，直接形成 look-ahead；hash、官方域名和 parser 成功都不能修复这个时间穿越。
- 为什么现有测试没抓住：`tests_quant/test_security_universe_adapter.py:556-572` 只覆盖 `known_at > observed_at` 和发布日期晚于 `known_at`；Calendar 测试覆盖 `usable_from < known_at`。多数 fixture 直接令 `observed_at == known_at`，没有攻击“远早于首次观察但仍满足不等式”的回填。
- 最小修复方案：把 `known_at` 的来源类型纳入 descriptor/身份合同。没有可靠且已绑定的服务端内容发布时间证据时，强制 `known_at == observed_at`；存在可靠秒级证据时，必须绑定该原始响应字段及其验证规则，不能接受裸 CLI 参数自报。`usable_from` 继续按交易会话保守推迟。为 Calendar 与 Security/Universe 增加“首次观察 2024、调用方声称 2020”回归测试。
- 是否阻断工程合并：是。

### CRITICAL-02：同一 Calendar 修订链在 Reconciliation 与核心 Snapshot 中出现 OPEN/CLOSED 分裂

- file:line：
  - `stock_tracker/quant/data/calendar_adapter.py:1481-1492`
  - `stock_tracker/quant/core/calendar.py:198-213,400-417`
  - `stock_tracker/quant/data/reconciliation.py:812-983`
- 触发条件：`annual-r1 -> r2(CLOSED) -> r10(OPEN)`，其中 `r2` 与 `r10` 具有相同 `known_at`。Reconciliation 按 `supersedes` 图选择 terminal `r10`；Adapter 却把所有修订都转成 `CalendarDay`，而核心 `_select_revision()` 在相同 `known_at` 下按字符串 revision lexical order 选择 `r2`。
- 最小复现：

  ```text
  revisions: annual-r1 -> r2(CLOSED) -> r10(OPEN)
  r2.known_at == r10.known_at
  Reconciliation calendar_open_dates contains target = true
  Reconciliation selected revision               = r10
  TradingCalendar.snapshot target status          = CLOSED
  ```

- 金融后果：同一证据包可在对账报告中被判定为交易日、在核心 Snapshot 中被判定为休市日。结果会改变 bar 对齐、next executable price、标签 horizon、停牌占位、回测成交和 Replay，可能虚增或压低收益并污染训练标签。
- 为什么现有测试没抓住：`tests_quant/test_stage2_reconciliation.py:793-843` 只验证 Reconciliation 选择 `r10`，没有把同一 `assemble_calendar_candidates()` 结果送入 `TradingCalendar.snapshot()`。核心 Calendar 测试仍把通用 revision ordering 当作选择依据，没有覆盖显式 `supersedes` 图。
- 最小修复方案：建立单一、可复用的 Calendar revision-graph resolver，并让 Adapter、Reconciliation 和核心 Snapshot 使用同一结果。核心选择不能再依赖字符串或数字外观排序；若核心继续接收所有候选，则 `CalendarDay`/相邻合同必须携带可验证的 predecessor 关系。增加端到端测试，要求同一 `r2 -> r10` 输入在 Reconciliation 与 `TradingCalendar` 中都选 `r10`。
- 是否阻断工程合并：是。

### CRITICAL-03：可删除计算出的 HARD_BLOCK，并伪造 `STRUCTURALLY_CONSTRUCTIBLE` 报告

- file:line：
  - `stock_tracker/quant/data/reconciliation.py:627-705,724-740`
  - `tests_quant/test_stage2_reconciliation.py:451-483`
- 触发条件：先用“只有 SSE Calendar + SZSE Universe”生成含 `REQUIRED_SESSION_CALENDAR_MISSING` 的合法报告；随后用 `dataclasses.replace()` 删除所有 HARD_BLOCK findings，并同步删除对应 `unresolved_gaps`。`ReconciliationReport.__post_init__()` 只根据调用方仍提供的 findings 检查 unresolved 项，不会从 inputs、`as_of` 和 coverage 重新计算 findings/metrics。
- 最小复现：

  ```text
  original hard codes = [REQUIRED_SESSION_CALENDAR_MISSING]
  original state      = HARD_BLOCKED
  replace(findings=without_hard, unresolved_gaps=recomputed_from_remaining)
  bypass state        = STRUCTURALLY_CONSTRUCTIBLE
  bypass has_hard     = false
  ```

  同一复现还把 `calendar_observed_civil_dates`、`calendar_open_dates`、identity/status/membership counts 全部改成 0；构造器仍接受了相互矛盾的 coverage metrics。
- 金融后果：调用方可以把缺失目标交易所 Calendar 的报告改成“结构可构造”，或重写覆盖率后生成一个新的、hash 自洽但语义虚假的 `report_id`。下游若以 `candidate_snapshot_state`、coverage 或 report identity 作为 Replay/治理门禁，会绕过金融正确性 HARD_BLOCK。
- 为什么现有测试没抓住：现有 direct-constructor 测试只删除 required inherited blocker、伪造 `CLOSED_WITH_EVIDENCE`，或单独删减 unresolved gaps；没有删除 finding 与对应 unresolved code，也没有篡改 `as_of`/coverage metrics 后检查 dataclass 是否重新推导。
- 最小修复方案：让 `ReconciliationReport` 的 dataclass 边界对所有派生字段执行规范化重算并逐项比对，至少包括 findings、inherited blockers、coverage metrics、unresolved gaps 和 candidate state；更稳妥的形态是构造器只接收规范输入与 closure/additional-finding 原始请求，派生字段全部 `init=False`。不能仅依赖 factory 命名、frozen dataclass 或 hash。
- 是否阻断工程合并：是。

### IMPORTANT-01：未来 Security/Universe 候选污染历史 `as_of` findings 与 coverage metrics

- file:line：
  - `stock_tracker/quant/data/reconciliation.py:1485-1516`
  - `stock_tracker/quant/data/reconciliation.py:1831-1871`
  - `stock_tracker/quant/data/reconciliation.py:2068-2085`
- 触发条件：报告 `as_of=2024-01-14T08:00:00Z`；加入一条 `known_at=2024-01-14T18:05:00+08:00`、`usable_from=2024-01-15T09:30:00+08:00` 的未来 membership，并让该未来记录缺少 evidence IDs。`_analyze_security()` 先吸收 bundle-global coverage report 及所有候选，证据检查和 candidate counts 也不按 cutoff 过滤。
- 最小复现：

  ```text
  as_of                         = 2024-01-14T08:00:00Z
  future known_at               = 2024-01-14T18:05:00+08:00
  future usable_from            = 2024-01-15T09:30:00+08:00
  new pre-knowledge finding     = MISSING_SOURCE_EVIDENCE_IDS
  clean membership count        = 7
  contaminated membership count = 8
  ```

- 金融后果：同一个历史截止时间会因后来到达的记录而改变 findings 和覆盖率，导致历史 Replay/审计报告不可稳定复现。当前复现表现为提前出现 Trust block 和虚增 coverage count；它虽偏保守，仍会污染治理、覆盖率解释和历史报告语义。
- 为什么现有测试没抓住：`tests_quant/test_security_universe_adapter.py:409-415` 只验证核心 Universe Snapshot 在 future correction 前后选择正确；Reconciliation 的 future coverage 测试 `tests_quant/test_stage2_reconciliation.py:710-726` 只覆盖 Calendar，没有覆盖 Security/Universe findings 和 metrics。
- 最小修复方案：在 D 层先构造严格的 as-of-visible Security/Universe 投影，再基于该投影计算证据 findings、required-status 结论和 coverage metrics；bundle-global `coverage_report` 不能未经 cutoff 处理直接映射进历史报告。完整 input/artifact ID 仍可保留在 provenance/report identity 中，但未来记录不得改变过去的语义 findings 和 observed coverage。
- 是否阻断工程合并：是。

### IMPORTANT-02：断开的同语义 Calendar cycle 不会被 revision graph 检测

- file:line：`stock_tracker/quant/data/reconciliation.py:862-960`
- 触发条件：一个有效 annual terminal 与一个断开的 `r2 -> r3 -> r2` cycle 同时存在；cycle 节点与 annual terminal 对目标日期给出相同 OPEN payload。实现只遍历被选 terminal 的祖先，并且只在断开节点 payload 不同于 selected payload 时报告 branch conflict，因此整个 cycle 被忽略。
- 最小复现：

  ```text
  valid terminal payload       = OPEN
  disconnected r2 <-> r3      = OPEN / OPEN
  CALENDAR_REVISION_CYCLE      = absent
  target counted as OPEN       = true
  ```

- 金融后果：不可审计、缺 predecessor 完整性的修订历史可以被视为结构有效；后续补入节点或语义变化时，同一历史输入可能被重新解释。当前同 payload 不立即改变 OPEN/CLOSED，但破坏了完整 revision history 和 deterministic Replay 的先决条件。
- 为什么现有测试没抓住：线性链测试只覆盖 `annual-r1 -> r2 -> r10`。当前测试集中没有覆盖“有效 terminal + 断开的同 payload cycle”，也没有覆盖未被选中同 payload 分支上的 missing predecessor。
- 最小修复方案：在选择 terminal 和比较 payload 之前，对每个 visible revision node 做全图验证：所有非 root predecessor 必须存在；对所有 connected components 做 DFS/颜色检测；任何 cycle 都 HARD_BLOCK；断开分支必须按明确的来源根合同处理，不能因 payload 恰好相同而跳过结构错误。
- 是否阻断工程合并：是。

## 3. 未形成 Finding 的重点审查项

以下范围结合当前源码、fixtures 和本轮完整测试检查后，未发现额外可复现缺陷；这不构成真实数据等级晋级：

- exact raw -> raw descriptor -> parse descriptor -> deterministic replay 的 hash/identity 绑定，以及 raw/raw descriptor/parse descriptor 篡改失败关闭；
- SSE/SZSE 官方 HTTPS owner domain 与 redirect-chain 约束；
- DATE publication 不伪造成 SECOND，年度 weekday inference 仍显式保留 gap；
- SSE/SZSE Calendar 与 Universe 隔离，SZSE 不能借用 SSE OPEN coverage；
- stable `instrument_id`、symbol rename、非重叠 code reuse、退市样本保留、absence != EXCLUDED；
- membership state 跨源冲突 HARD_BLOCK、reason 冲突 TRUST_BLOCK，以及 ST/*ST、SUSPENDED/HALTED、DELISTING/DELISTED/UNKNOWN 的保守处理；
- inherited blockers 不能被调用方自报的 `synthetic=false` / `independently_approved=true` 关闭，synthetic input 不能被其他来源洗白；
- 独立 source 可拥有独立 version，同一 source identity 的互斥 version 被阻断；
- CLI 输出路径不会覆盖已声明的输入文件，JSON 与 Markdown 同路径会失败；
- migration 0003 的 revision 原类型编码、UNKNOWN、append-only UPDATE/DELETE trigger、checksum 与旧 DB dry-run 路径。

## 4. 验证证据

### 4.1 请求指定命令

| 命令 | 当前运行结果 |
|---|---|
| `python -m unittest discover -s tests_quant -p "test_*.py" -v` | exit 0；290 tests；全部通过 |
| `python -m unittest discover -s tests -p "test_*.py" -v` | exit 0；341 tests；通过；1 个 localhost `:8080` live probe 因服务不可达而 skip |
| `python -m compileall -q stock_tracker tests tests_quant scripts` | exit 0 |
| `python scripts/run_quant_contract_smoke.py` | exit 0；`passed=true`；`synthetic_fixture_only=true`；`production_database_modified=false` |
| `python scripts/run_quant_fixture_benchmark.py` | exit 0；`synthetic_fixture_only=true`；`investment_performance_claim=false`；candidate 未晋级 |
| `python scripts/quant_migrate.py --database data/stock_tracker.db` | exit 0；`mode=DRY_RUN`；`applied_count=0`；`database_modified=false`；3 migrations pending |
| `python -m pip check` | exit 0；`No broken requirements found.` |
| `ruff check stock_tracker/quant/data/reconciliation.py tests_quant/test_stage2_reconciliation.py scripts/report_stage2_coverage.py` | exit 0；`All checks passed!` |
| `git diff --check` | exit 0；仅报告既有工作树文件的 LF/CRLF 提示，无 whitespace error |

### 4.2 生产数据库不变性

```text
before SHA-256 = 1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
after  SHA-256 = 1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

没有运行 `--apply`，没有读取或展示用户持仓内容，没有修改生产数据库。

### 4.3 独立对抗性复现

本轮使用 `python - <<'PY'` 在临时目录运行只读/临时 fixture 复现，没有保存脚本或改动源码。已确认：

1. Calendar 与 Security `known_at` 远早于首次观察仍被接受；
2. `r2 -> r10` 在 Reconciliation 为 OPEN、核心 Snapshot 为 CLOSED；
3. 断开的同 payload cycle 未产生 `CALENDAR_REVISION_CYCLE`；
4. 删除 HARD_BLOCK findings 后报告从 `HARD_BLOCKED` 变为 `STRUCTURALLY_CONSTRUCTIBLE`；
5. not-yet-known/not-yet-usable Security membership 提前改变 findings 与 coverage count。

另外两次更激进的身份冲突构造在 Security Adapter 入口被正确拒绝（wrong-exchange symbol、重叠 stable identities），没有据此制造 finding。

## 5. 变更、数据与 Git 声明

- 本 Reviewer 实际只新增本文件：`docs/STAGE2-PIT-IDENTITY-REVIEW.md`。
- 未修改 Python、SQL、JavaScript、fixtures、config 或生产数据库。
- 开始审查时工作树已经包含其他人工/Agent 的 tracked 与 untracked 变更；本轮没有覆盖、格式化、回滚或纳入这些变更。
- 未访问外部 Provider，未新增真实来源证据；验证只使用 synthetic fixture/临时数据库。本地 legacy suite 的 live probe 只检查 localhost `:8080`，服务不可达后按测试合同 skip。
- 未执行 `git add`、`git commit`、`git merge`、`git push`、`git reset`、`git clean`、`git checkout`、`git switch`、`git restore` 或 `git stash`。

## 6. 合入前最低要求

1. 修复全部 3 项 CRITICAL，并加入跨 Adapter/Reconciliation/Core 的同链一致性测试。
2. 修复 Security/Universe as-of 投影与 Calendar 全图结构验证两项 IMPORTANT。
3. 重新运行本报告中的全部命令与五个对抗性回归，并再次记录生产数据库前后 SHA-256。
4. 由新的独立金融正确性 Review 复核修复后的当前 commit；不能用本次测试全绿替代复核。
5. 即使工程修复完成，仍保持 `LICENSE_PENDING` 与 `EVIDENCE_TIER_STATUS = T3_NOT_REACHED`，直至出现真实、完整、许可闭环且可审计的新证据。
