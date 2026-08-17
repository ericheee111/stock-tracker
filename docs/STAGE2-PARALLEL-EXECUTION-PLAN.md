# Stage 2A 并行执行计划与 Agent 提示词

> 日期：2026-08-14
> 工作区：`D:\Projects\stock-tracker`
> 主目标：A 股 Calendar + Security Status + Historical Universe + PIT Research Identity
> 当前基线：主合同已在工作树实现，尚未 commit/push；权威真实数据尚未接入

---

## 1. 调度原则

Stage 2 不是普通 CRUD。PIT、survivorship、退市样本、修订选择、可信等级和 Research Snapshot 必须由主模型冻结并最终 Review。

执行分两层：

```text
Phase 0  主合同冻结 + Agent A 来源审计（已完成）
    ↓
Phase 1  Agent B Calendar Adapter || Agent C Security/Universe Adapter
         WorkBuddy HY3 仅做不重叠的 fixture/CLI smoke 辅助
    ↓
Phase 2  Agent D Reconciliation/Gap Report（消费 B/C 冻结 schema）
    ↓
Phase 3  GPT-5.6 Sol 独立金融正确性 Review
    ↓
Phase 4  主车道集成、完整门禁、再决定是否提交
```

当前工作树另有 Stage 1.1 未提交文件。所有 Stage 2 Agent 必须遵守文件所有权，禁止格式化、覆盖或提交无关文件。

---

## 2. 模型与推理强度

| 车道 | 推荐模型 | 推理强度 | 说明 |
|---|---|---:|---|
| 主合同与最终集成 | GPT-5.6 Sol Pro + CodexPro | Pro / Ultra | 负责 PIT 语义、迁移、交叉合同和最终门禁 |
| Agent A：权威来源审计 | GPT-5.6 Sol | xhigh | **已完成**，输出 `RESEARCH_COMPLETE / LICENSE_PENDING / T3_NOT_REACHED` |
| Agent B：Calendar Adapter | GPT-5.6 Terra | xhigh | 异构 HTML/PDF/XLSX exact-raw、时间语义与确定性解析；由 Sol 最终 Review |
| Agent C：Security/Universe Adapter | GPT-5.6 Sol | xhigh | 剩余实现中金融正确性最高：稳定证券身份、退市样本、survivorship、状态事件 |
| Agent D：Reconciliation/Gap Report | GPT-5.6 Sol | xhigh | B/C Review 后任务已升级为 Trust-blocker closure governance；质量优先，不负责 Trust 晋级 |
| WorkBuddy 辅助 | HY3 | 最高可用 | 只做不重叠 fixture、CLI smoke、Markdown/JSON 展示与机械测试扩充，不决定 PIT/Trust |
| 独立最终 Review | GPT-5.6 Sol | max；若 Codex 未暴露 max 则 xhigh | 只 Review，质量优先，检查 look-ahead、survivorship、Trust 自我升级和迁移 |

只使用 Codex 内 GPT 系列与 WorkBuddy HY3。Luna 更适合高吞吐机械任务，本阶段核心 Adapter/Review 不使用 Luna；若要额外批量生成 fixture，可单独用 Luna high/xhigh，但产物必须由 B/C/D 所属 Codex Agent 审核。

---

## 3. 文件所有权

### 主车道已占用

```text
stock_tracker/quant/core/calendar.py
stock_tracker/quant/core/universe.py
stock_tracker/quant/core/__init__.py
stock_tracker/quant/storage/migrations/0003_pit_universe_identity.sql

tests_quant/test_calendar.py
tests_quant/test_universe.py
tests_quant/test_storage_migrations.py

docs/STAGE2-PIT-IDENTITY-CONTRACT.md
docs/STAGE2-PARALLEL-EXECUTION-PLAN.md
```

其他 Agent 不得修改这些文件。如发现合同问题，只在自己的 Handoff 文档中报告。

### Agent A

```text
docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md
```

### Agent B

```text
stock_tracker/quant/data/calendar_adapter.py
scripts/capture_a_share_calendar.py
tests_quant/test_calendar_adapter.py
tests_quant/fixtures/calendar/**
docs/STAGE2-CALENDAR-ADAPTER-HANDOFF.md
```

### Agent C

```text
stock_tracker/quant/data/security_universe_adapter.py
scripts/import_a_share_identity.py
tests_quant/test_security_universe_adapter.py
tests_quant/fixtures/security_universe/**
docs/STAGE2-SECURITY-UNIVERSE-HANDOFF.md
```

### Agent D

```text
stock_tracker/quant/data/reconciliation.py
scripts/report_stage2_coverage.py
tests_quant/test_stage2_reconciliation.py
tests_quant/fixtures/reconciliation/**
docs/STAGE2-RECONCILIATION-HANDOFF.md
```

### Reviewer

```text
docs/STAGE2-PIT-IDENTITY-REVIEW.md
```

所有 Agent 禁止修改共享 `stock_tracker/quant/data/__init__.py`；最终统一 export 由主车道完成。

---

# 4. 主车道任务（GPT-5.6 Sol Pro + CodexPro，Ultra）

```text
工作区：D:\Projects\stock-tracker

目标：实现 Stage 2A 的不可绕过 PIT 身份底座，而不是抓取真实数据。

必须完成：
1. 为 Calendar 补齐 usable_from，并保证 known_at <= usable_from <= as_of；
2. 定义证券 Identity、每日 Listing/Trading/Risk Status；
3. 定义事件化 Historical Universe，明确 INCLUDED/EXCLUDED；
4. Snapshot 默认要求 verified + complete；
5. 每个历史 membership 必须有 identity 和目标日 status；
6. 保留暂停、ST、退市和 EXCLUDED 样本；
7. 多 source/version、缺失、未来修订、冲突修订全部失败关闭；
8. Calendar + Universe + Status 绑定为 Research Identity Snapshot；
9. Snapshot ID 必须按内容重算验证，不能通过替换 ID 自我升级；
10. 新增 append-only SQLite migration；
11. 只使用 synthetic fixtures 验证，不接生产信号或真实数据库；
12. 输出冻结合同文档和外部 Agent 文件所有权。

验证：
- targeted Calendar/Universe/Migration tests；
- 全量 tests_quant；
- compileall；
- quant contract smoke；
- migration dry-run 并核对生产数据库 SHA-256 不变；
- git diff --check。

禁止：
- 修改 Stage 1.1 正在进行的前端/API 文件；
- 对 data/stock_tracker.db 执行 --apply；
- 宣称 T3、真实收益、真实胜率或真实策略有效；
- commit/push，除非用户另行明确授权。
```

---

# 5. Agent A 提示词：A 股权威来源、修订和许可审计

**模型：GPT-5.6 Terra，Ultra；回退 GPT-5.5 High。**

```text
你是 stock-tracker Stage 2A 的权威数据来源审计 Agent。只做研究和证据矩阵，不修改 Python/JS/SQL。

工作区：D:\Projects\stock-tracker

先完整读取：
- AGENTS.md
- docs/PRD-股票辅助判断与交易参考网站.md
- docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md
- docs/STAGE2-PARALLEL-EXECUTION-PLAN.md

唯一允许修改：
- docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md

研究目标：为接近零成本的 A 股研究链，寻找可合法、可复现、可保留修订历史的数据来源。优先官方一手来源，并为每项结论给出发布日期/有效日期和直接引用链接。

必须覆盖：
1. SSE/SZSE 官方交易日历、休市、临时调整和历史修订；
2. 证券主数据：上市日、退市日、证券类型、交易所、代码变更；
3. 停牌/复牌/临时停牌历史；
4. ST、*ST、风险警示、退市整理等状态历史；
5. 历史 A 股全量 Universe，必须包括退市样本；
6. 指数/行业/板块历史成分的 PIT 可得性；
7. 分红、送转、拆并股、配股、增发等公司行为；
8. 每类数据能否获得 exact raw bytes、发布时间、known_at、usable_from 和 revision；
9. API/下载频率、robots/服务条款、再分发和仓库存储限制；
10. 免费/低成本方案、官方源与二级源的 reconciliation 组合。

为每个候选来源输出矩阵：
- source / owner；
- official or secondary；
- fields；
- historical depth；
- revision history；
- timestamp semantics；
- exact raw capture feasibility；
- license/redistribution risk；
- cost/rate limit；
- expected Trust Tier 上限；
- gaps；
- recommended role：primary / corroboration / fallback / reject。

必须特别回答：
- “今天的证券列表”为什么不能构造历史 Universe；
- 仅靠单一公开源为何不能设 complete=true 或 verified=true；
- 哪些来源能证明退市样本没有被遗漏；
- 哪些字段只有公告发布时间，没有可靠 usable_from；
- 是否存在后补修订覆盖原值的问题。

结尾给出：
A. 推荐 Source Stack；
B. 不推荐来源与理由；
C. Agent B/C 实现时必须冻结的 endpoint/file/schema；
D. 仍然无法达到 T3 的缺口；
E. 需要人工确认的许可问题。

禁止：
- 修改任何代码；
- 把第三方博客/聚合站当权威来源；
- 无引用地声称“官方”或“完整”；
- 因有 SHA-256 就升级 Trust Tier；
- git add/commit/push。
```

---

# 6. Agent B 提示词：A 股 Calendar exact-raw Adapter

**模型：GPT-5.6 Terra，reasoning=xhigh。**

```text
你是 stock-tracker Stage 2A 的 A 股交易日历 Adapter Agent。

工作区：D:\Projects\stock-tracker

前置条件：以下主合同必须已存在；不存在时停止并报告，禁止自行重写：
- stock_tracker/quant/core/calendar.py
- stock_tracker/quant/core/universe.py
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md

先读取：
- AGENTS.md
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md
- docs/STAGE2-PARALLEL-EXECUTION-PLAN.md
- docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md（如已存在）
- stock_tracker/quant/data/bar_artifact.py
- stock_tracker/quant/data/manifest.py
- stock_tracker/quant/core/calendar.py

只允许修改：
- stock_tracker/quant/data/calendar_adapter.py
- scripts/capture_a_share_calendar.py
- tests_quant/test_calendar_adapter.py
- tests_quant/fixtures/calendar/**
- docs/STAGE2-CALENDAR-ADAPTER-HANDOFF.md

目标：实现“精确原始响应捕获”和“确定性解析”分离的 Calendar Adapter。不得把网络响应直接变成 verified Calendar。

要求：
1. exact raw bytes 内容寻址保存；
2. descriptor 绑定 endpoint、请求参数、retrieved_at、provider/parser/schema version；
3. parser 只接受 fixture/已捕获 bytes，不在解析函数内联网；
4. 输出 CalendarCoverage + 每个自然日 CalendarDay；
5. 明确 OPEN/CLOSED，覆盖区间每个 civil date 都有记录；
6. open/close 使用 Asia/Shanghai；
7. 显式 known_at 与 usable_from；不得把 retrieved_at 自动当事件发布时间；
8. 修订必须 append-only；相同身份不同 bytes 失败；
9. 默认 verified=false；只有调用方提供已审计证据对象时才允许生成“候选核验记录”，Adapter 本身不得升级；
10. 网络失败、HTML 错误页、字段缺失、日期重复、范围不完整、异常时区全部失败关闭；
11. CLI 默认只写指定 artifact root，不写生产 SQLite；
12. 测试全部离线，不依赖互联网。

Golden fixtures 至少包括：
- 正常完整区间；
- 周末/法定休市；
- 临时日历修订；
- 缺一天；
- 重复日期；
- 字段顺序变化但语义相同；
- 内容篡改；
- known_at 已知但 usable_from 尚未来到；
- 跨 Calendar version 拼接。

验收：
- exact bytes 可重放；
- 同 bytes + 同 request identity 得到稳定 descriptor ID；
- parser 输出稳定；
- 不完整 Coverage 不能 complete=true；
- Adapter 不能宣称 T2/T3；
- targeted tests、compileall、git diff --check 通过。

不要修改：
- stock_tracker/quant/core/**
- stock_tracker/quant/storage/**
- stock_tracker/quant/data/__init__.py
- 其他 Agent 文件
- data/stock_tracker.db

禁止 git add/commit/push。
最终在 Handoff 中列出真实运行命令、测试结果、来源假设、Trust Tier 上限和未解决缺口。
```

---

# 7. Agent C 提示词：Security Status + Historical Universe Adapter

**模型：GPT-5.6 Sol，reasoning=xhigh。**

```text
你是 stock-tracker Stage 2A 的证券身份、每日状态与历史 Universe Adapter Agent。

工作区：D:\Projects\stock-tracker

前置条件：以下主合同必须已存在；不存在时停止并报告：
- stock_tracker/quant/core/universe.py
- stock_tracker/quant/storage/migrations/0003_pit_universe_identity.sql
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md

先读取：
- AGENTS.md
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md
- docs/STAGE2-PARALLEL-EXECUTION-PLAN.md
- docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md（如已存在）
- stock_tracker/quant/data/manifest.py
- stock_tracker/quant/core/universe.py

只允许修改：
- stock_tracker/quant/data/security_universe_adapter.py
- scripts/import_a_share_identity.py
- tests_quant/test_security_universe_adapter.py
- tests_quant/fixtures/security_universe/**
- docs/STAGE2-SECURITY-UNIVERSE-HANDOFF.md

目标：从不可变 raw artifacts 确定性产生：
- InstrumentIdentityFact；
- SecurityStatusFact；
- UniverseMembershipFact；
- UniverseCoverage candidate。

硬规则：
1. 不得用当前证券列表回填历史；
2. 必须保留退市、暂停、ST/*ST、风险警示和 EXCLUDED 事件；
3. absence 不能解释为 EXCLUDED；
4. membership 必须是显式 INCLUDED/EXCLUDED 事件；
5. listing/trading/risk status 必须分开；
6. known_at、usable_from、effective_date/session_date 不得互相代替；
7. 代码变更、重新上市或证券身份变化必须用有效期/新事实表达；
8. parser 不联网；capture/import CLI 与 parser 分离；
9. 默认 verified=false、complete=false；Adapter 无权自行升级；
10. 同 source/version/revision 身份出现不同 payload 时失败关闭；
11. 输出必须可直接交给 HistoricalUniverse.snapshot()；
12. 不得丢弃已 EXCLUDED 的 symbol。

Golden fixtures 至少包括：
- 正常上市证券；
- ST → NORMAL 修订；
- 停牌 → 复牌；
- 退市及 Universe EXCLUDED；
- 同代码跨身份有效期；
- 缺 identity；
- 缺目标日 status；
- 只给当前列表、没有退出历史；
- 未来才公开的修订；
- 同 known_at/revision 冲突；
- 重复行和乱序输入。

CLI：
- 输入必须是 artifact/descriptor 路径；
- 默认输出 JSON/JSONL candidate facts 和 coverage report；
- 不写生产数据库；
- 不提供 `--trust-tier T3` 之类自我升级参数；
- 输出中明确 synthetic/fixture 或真实来源身份。

验收：
- 乱序输入输出稳定；
- 退市样本仍可在 Snapshot 的 delisted_symbols 中看到；
- 缺 identity/status 失败；
- 当前列表不能声称 complete；
- targeted tests、compileall、git diff --check 通过。

不要修改：
- stock_tracker/quant/core/**
- stock_tracker/quant/storage/**
- stock_tracker/quant/data/__init__.py
- Agent B/D 文件
- data/stock_tracker.db

禁止 git add/commit/push。
```

---

# 8. Agent D 提示词：多源 Reconciliation 与覆盖率缺口报告

**模型：GPT-5.6 Sol，reasoning=xhigh。B/C Handoff 与主车道 Review 都完成后再启动。**

> B/C 主车道 Review 已完成：Calendar Adapter 23 项、Security/Universe Adapter 35 项定向测试通过。D 必须以更新后的 Handoff 和当前磁盘源码为准，不得依赖 B/C 初始汇报中的旧测试数或已修复 limitation。C 的 `trust_blocker_codes` 至少含 `ADAPTER_UNVERIFIED_INCOMPLETE`、`SOURCE_SECURITY_ID_STABILITY_UNPROVEN`、`UPSTREAM_RAW_PROVENANCE_INCOMPLETE`；D 只能用独立证据显式关闭 blocker，不能因 schema 可用或测试通过自动消失。

```text
你是 stock-tracker Stage 2A 的 reconciliation 和 coverage-gap Agent。

工作区：D:\Projects\stock-tracker

先读取：
- AGENTS.md
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md
- docs/STAGE2-PARALLEL-EXECUTION-PLAN.md
- docs/research/STAGE2-A-SHARE-AUTHORITATIVE-SOURCE-AUDIT.md
- docs/STAGE2-CALENDAR-ADAPTER-HANDOFF.md
- docs/STAGE2-SECURITY-UNIVERSE-HANDOFF.md
- docs/STAGE2-FIXTURE-COVERAGE-CHECKLIST.md
- stock_tracker/quant/data/manifest.py
- stock_tracker/quant/core/calendar.py
- stock_tracker/quant/core/universe.py
- stock_tracker/quant/data/calendar_adapter.py
- stock_tracker/quant/data/security_universe_adapter.py

只允许修改：
- stock_tracker/quant/data/reconciliation.py
- scripts/report_stage2_coverage.py
- tests_quant/test_stage2_reconciliation.py
- tests_quant/fixtures/reconciliation/**
- docs/STAGE2-RECONCILIATION-HANDOFF.md

目标：建立确定性、不可自我升级的 Calendar/Identity/Status/Universe 差异与缺口报告。

主车道 Review 后的冻结语义：
- B 的 exact-raw descriptor 必须保持 official-domain redirect confinement；HTML candidate 必须通过独立 `a-share-calendar-parse-descriptor-v1` 绑定 raw descriptor + notice/PIT/effective/revision provenance 后重放；任何 raw/parse descriptor/hash/binding 异常都是 HARD_BLOCK/TRUST_BLOCK，不能靠 normalized 输出覆盖；
- B 的 annual weekday inference、`LICENSE_PENDING`、`SINGLE_SOURCE_NOT_RECONCILED` 等 gaps 必须原样进入 reconciliation；不能因 civil-date 数量完整就视为 verified calendar；
- C 的 `has_snapshot_blockers=false` 只表示 candidate snapshot 结构可构造，绝不表示 Trust 已闭环；
- C 的全部 `trust_blocker_codes` 必须显式消费和保留/关闭；没有独立证据时至少保留 `ADAPTER_UNVERIFIED_INCOMPLETE`、`SOURCE_SECURITY_ID_STABILITY_UNPROVEN`、`UPSTREAM_RAW_PROVENANCE_INCOMPLETE`；
- `source_security_id` 的字段名本身不证明跨代码变更/重新上市稳定；稳定性只能由来源合同/独立证据关闭 blocker；
- EXCLUDED 退市旧证券只需绑定退出日有效 identity 和不晚于退出日的最后可见 status；不得要求或伪造退市后的 target-session status；
- 同一 symbol 在不同时间可被不同 instrument_id 复用，只要有效期不重叠；目标 session 只能有一个 INCLUDED instrument 使用该 symbol；
- `RiskDesignation.UNKNOWN` 必须保持 UNKNOWN，不能降成 OTHER；
- WorkBuddy `stage2_aux` fixture 仅是 synthetic 边界材料，不能作为 source corroboration 或 Trust evidence；
- SSE 与 SZSE Universe 分开 reconciliation，不能直接在本层生成一个“完整 A_SHARE_ALL”；
- Agent A 的状态 `LICENSE_PENDING / T3_NOT_REACHED` 不能被 D 解除。

输出至少包括：
1. civil-date calendar coverage 缺口；
2. source/version 混用；
3. OPEN/CLOSED 冲突；
4. membership 无 identity；
5. membership 无目标日 status；
6. INCLUDED + DELISTED 冲突；
7. 当前列表缺少历史 EXCLUDED/退市证据；
8. ST/停牌/退市状态跨源冲突；
9. known_at/usable_from 不一致；
10. 修订冲突；
11. symbol/market/exchange identity 冲突；
12. Artifact hash/descriptor 不匹配；
13. 数据覆盖率、可解释缺口与不可解释缺口分开统计；
14. 每个 inherited trust blocker 的状态：OPEN | CLOSED_WITH_EVIDENCE，并列出 closing_evidence_ids；
15. `source_security_id` 稳定性证据缺失；
16. normalized candidate 未绑定 upstream exact raw 的 provenance 缺失；
17. 同代码跨期复用、代码变更、relisting 的 instrument_id 连续性冲突；
18. inferred calendar day 与 explicit official notice 的证据等级区别。

报告身份必须绑定固定 `reconciliation_policy_version`、所有输入 artifact/descriptor/bundle/report IDs、parser versions、as_of 和排序后的 findings。输入顺序不得改变 report_id。

严重度：
- HARD_BLOCK：不能组装 Snapshot；
- TRUST_BLOCK：可调试，但不能 verified/complete；
- WARNING：不破坏身份，但需人工核验；
- INFO：覆盖率说明。

硬规则：
- 报告只能降低或阻断 Trust，不能单独把数据升级到 T2/T3；
- 两个来源一致不自动等于权威；
- absence 不等于 EXCLUDED；
- 不允许用比例阈值忽略退市样本；
- 输出顺序、ID 和 JSON 必须稳定；
- 所有 fixture 测试离线。

CLI 默认读取 descriptor/normalized fact JSON，不联网、不写数据库。输出 JSON + Markdown 摘要，明确 source IDs、as-of 和 config hash。

最少负例测试：
- 两个来源一致仍保留 `ADAPTER_UNVERIFIED_INCOMPLETE` / LICENSE blocker；
- 99.9% coverage 但漏 1 个退市样本仍是 TRUST_BLOCK/HARD_BLOCK；
- `has_snapshot_blockers=false` 但 inherited trust blockers 非空；
- source_security_id 未证明稳定时不能关闭 stability blocker；
- 只有 normalized JSON、没有 upstream raw chain 时不能关闭 raw provenance blocker；
- 旧退市 instrument + 新 instrument 复用同 symbol 是合法非重叠情况；重叠使用相同 symbol 才冲突；
- EXCLUDED 旧 instrument 不要求退市后 target-session status；
- Calendar inferred weekday 与 explicit revision 冲突时 explicit evidence 优先，但必须保留 revision/gap 审计；
- WorkBuddy synthetic fixture 与 Agent C synthetic artifact 不能充当独立第二来源；
- findings 输入顺序变化不能改变 report_id；
- 调整 finding 文案但不改 policy/version 时必须改变 report_id；
- 尝试输出 T3/verified/complete=true 必须被合同拒绝或报告为非法晋级。

不要修改：
- stock_tracker/quant/core/**
- stock_tracker/quant/storage/**
- stock_tracker/quant/data/__init__.py
- Agent B/C 文件
- data/stock_tracker.db

禁止 git add/commit/push。
```

---

# 9. 独立 Review 提示词

**模型：GPT-5.6 Sol，reasoning=max；若当前 Codex 未暴露 max，则用 xhigh。**

```text
你是 Stage 2A 独立金融正确性 Reviewer。第一轮只 Review，不修改代码。

工作区：D:\Projects\stock-tracker

读取：
- AGENTS.md
- docs/PRD-股票辅助判断与交易参考网站.md
- docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md
- docs/STAGE2-PIT-IDENTITY-CONTRACT.md
- docs/STAGE2-PARALLEL-EXECUTION-PLAN.md
- 所有 Stage 2A 新增源码、测试、fixtures 和 Handoff

唯一允许写：
- docs/STAGE2-PIT-IDENTITY-REVIEW.md

Review 重点：
1. 是否存在 look-ahead：source_published_at/observed_at/known_at/usable_from/effective/as_of 的顺序和粒度；
2. 是否用今天的成分构造历史 Universe；absence 是否被错误解释为 EXCLUDED；
3. `instrument_id` 是否真正独立于 symbol；代码变更、代码复用、relisting 是否会错误合并/拆分；
4. 是否保留退市、ST/*ST、停牌、盘中临停和 EXCLUDED 样本；EXCLUDED 是否被错误要求退市后每日 status；
5. UNKNOWN 是否被静默映射为 NORMAL/OTHER/空值，从而隐藏证据缺失；
6. source/version/revision 冲突是否失败关闭；future correction 是否会改写过去 Snapshot；
7. complete/verified/Trust Tier 是否可被调用方、Adapter、Reconciliation、hash 或“两个来源一致”自我升级；
8. Snapshot/report ID 是否真正绑定内容、policy version、gates、source/parser versions 和 as-of；
9. B 的 exact raw、descriptor、parser 是否分离；redirect 是否可能离开官方 owner domain；annual inferred weekdays 是否被误当官方显式日历；
10. C 的 normalized candidate JSON 是否被错误称为 upstream exact raw；`source_security_id` 稳定性是否有独立来源证据；
11. D 是否逐项消费 C `trust_blocker_codes`；Stage 2A 当前没有可信 external closure authority，所有 inherited blocker 必须保持 `OPEN`，即使调用者自报 `synthetic=false + independently_approved=true` 也不能关闭；
12. 是否能绕过 `reconcile_stage2()` 直接构造 `ReconciliationReport` 来省略 `LICENSE_PENDING/T3_NOT_REACHED`、C blocker 或 blocking `unresolved_gaps`；
13. D 是否错误地“多数投票即权威”、以 coverage 比例忽略退市缺口、把任意 synthetic input（包括混入真实 input）当独立 evidence；
14. SSE/SZSE Calendar observed/open 是否严格按交易所隔离；SZSE Universe 不能借 SSE OPEN session 通过；SSE/SZSE Universe 也不能过早拼成“完整 A_SHARE_ALL”；
15. Calendar revision 是否按显式 `supersedes_revision_id` 图选择终点，而不是 `revision_id` 字典序/数字外观；cycle、missing predecessor、branch conflict 是否失败关闭；
16. 不同独立 source 是否允许各自 source_version，而同一 source identity/source family 内混版本是否阻断；
17. 跨源同 instrument/session 的 INCLUDED vs EXCLUDED 是否 HARD_BLOCK；membership reason 不一致是否至少 TRUST_BLOCK；
18. future/not-yet-usable Calendar facts 是否会虚增 as-of observed coverage；EXCLUDED 退出状态 requirement 是否被跨后续 session 重复计数；
19. CLI JSON/Markdown 输出是否可能覆盖 security artifact/descriptor、Calendar parse/raw descriptor、raw artifact 或彼此；
20. rejected closure request 的 evidence/reason/policy 是否真正绑定 `report_id`；关键 input IDs 是否严格 SHA-256；
21. migration 是否 append-only、dry-run、安全兼容旧行和 `instrument_id`/UNKNOWN 枚举；
22. Agent A 的 `LICENSE_PENDING / T3_NOT_REACHED` 是否始终保留；
23. 测试是否真正覆盖 adversarial 负例，而非只测 happy path；
24. 是否有任何 T2/T3、真实胜率、真实战绩或可实盘的过度声明。

Reviewer 必须给两个相互独立的最终 verdict：
- `ENGINEERING_MERGE_BLOCKED` 或 `ENGINEERING_READY_FOR_MAIN_REVIEW`；
- `EVIDENCE_TIER_STATUS`，本阶段若没有新增真实许可与全历史闭环证据，必须仍为 `T3_NOT_REACHED`。工程可合并不等于证据可晋级。

按严重度输出：
- CRITICAL / IMPORTANT / MINOR；
- 文件与行号；
- 可复现路径；
- 金融后果；
- 最小修复建议；
- 是否阻断合入；
- 仍缺哪些真实证据。

必须独立运行适当测试，但不得修改生产数据库，不得 git add/commit/push。
```

---

## 10. 集成门禁

外部 Agent 结果回到主车道后，至少执行：

```text
python -m compileall -q stock_tracker tests tests_quant scripts
python -m unittest discover -s tests_quant -p "test_*.py" -v
python scripts/run_quant_contract_smoke.py
python scripts/run_quant_fixture_benchmark.py
python scripts/quant_migrate.py --database data/stock_tracker.db
python -m pip check
git diff --check
```

同时记录 `data/stock_tracker.db` 前后 SHA-256。Migration 命令必须保持 `DRY_RUN`，且数据库哈希不变。

在权威 raw artifacts、覆盖率、退市样本和 reconciliation 证据完成前，最终状态只能是：

```text
CONTRACT_ONLY / SYNTHETIC_VALIDATED
```

不得写成：

```text
RESEARCH_GRADE
真实回测有效
真实胜率已验证
模型可实盘
```
