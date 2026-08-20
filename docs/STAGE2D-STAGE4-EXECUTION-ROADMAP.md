# Stage 2D — Stage 4 执行路线图

> 冻结日期：2026-08-18
> 适用仓库：`stock-tracker`
> 当前工程基线：Stage 2A 已提交；Stage 2B/2C 位于本地未提交工作树
> 总体原则：先关闭数据身份与时间正确性，再进入事件和主升浪，再记录真实 Outcome；没有 T3 数据时 Replay 与真实战绩保持 BLOCKED。

## 1. 执行顺序

```text
Stage 2B/2C 收口
  ↓
Stage 2D 公司行为身份绑定、Coverage 与跨 Artifact 对账
  ↓
Stage 2E 行业/板块 Point-in-Time 身份与成员关系
  ↓
Stage 3A Event Intelligence 事实合同与 Exact-Raw 候选链
  ↓
Stage 3B Big Trend v1 规则状态机与 Trend Runner 合同
  ↓
Stage 3C free-stockdb 本地行情 Sidecar：隔离合同 → 真实发行版/数据审计 → Shadow Scanner
  ↓
Stage 4A Signal Outcome / Scoreboard 合同
  ↓
Stage 4B Replay Orchestrator（T3 未满足时显式 BLOCKED）
```

不得跨越依赖：

- Stage 2D 未完成前，不允许公司行为源行自行声明永久证券身份；
- Stage 2E 未完成前，不允许用运行层静态 `_SECTOR_MAP` 声称全市场板块主升浪；
- Stage 3A 未完成前，S3 手工事件不能冒充正式 Event Intelligence；
- Stage 3B 未完成前，不展示正式 Big Trend 或 Trend Runner；
- Stage 3C.2 未完成前，free-stockdb 必须默认关闭，不得进入正式信号、回测或训练；
- Stage 4A 没有真实 Outcome 前，不展示真实胜率、Profit Factor 或历史成功率；
- T3、历史 Universe、公司行为、事件时间未闭环前，Stage 4B Replay 必须返回 BLOCKED。

## 2. Stage 2B/2C 收口

### 目标

- 保留 Stage 2B 公司行为、复权因子和 adjusted-view 身份合同；
- 保留 Stage 2C exact-raw capture 和 parse descriptor；
- 修正 Stage 2C 源文档自报 `instrument_id / identity_fact_id` 的信任越界；
- 完整 Quant 门禁除“尚未 tracked”门禁外通过。

### 退出条件

```text
CorporateActionSnapshot identity 不可注入
AdjustmentSeries 只能由真实 Snapshot 派生
source row 不携带永久 instrument_id/identity_fact_id
raw/parser/candidate identities tamper-evident
生产数据库 SHA-256 不变
```

## 3. Stage 2D：公司行为绑定与对账

### 3.1 源候选与绑定候选分层

源文档只允许：

```text
source_security_id
symbol evidence
exchange/market evidence
source-native action_id
经济条款和时间
revision/supersedes
raw/descriptor/parser evidence
```

禁止源文档自报：

```text
instrument_id
identity_fact_id
verified
complete
trust_tier
research_grade
```

绑定层从 Stage 2A `IdentityCandidate / InstrumentIdentityFact` 取得稳定身份，并验证其在 `ex_date` 有效。

### 3.2 Bundle 与 Coverage

交付：

```text
CorporateActionSourceCandidate
BoundCorporateActionCandidate
CorporateActionIdentityBinding
CorporateActionCoverageReport
CorporateActionCandidateBundle
bind_corporate_action_document()
reconcile_corporate_action_bundles()
```

必须报告：

```text
missing identity binding
symbol mismatch
identity inactive on ex-date
reused symbol ambiguity
missing action coverage
missing predecessor / cycle / multiple terminal
unparsed attachment
missing reference-price evidence
cross-source conflict
license pending
```

### 3.3 证据边界

输出始终：

```text
verified = false
complete = false
trust_state = T3_NOT_REACHED
```

## 4. Stage 2E：行业/板块 PIT 身份

### 目标

建立独立于运行层 `_SECTOR_MAP` 的历史分类合同。

### 核心对象

```text
ClassificationTaxonomy
ClassificationFact
ClassificationMembershipFact
ClassificationCoverage
ClassificationSnapshot
SectorMembershipCandidateBundle
```

身份绑定：

```text
taxonomy owner
classification system/version
classification_id
instrument_id
identity_fact_id
symbol-at-effective-date
effective_from/effective_to
known_at/usable_from
revision/supersedes
raw evidence IDs
```

### 首版来源范围

只实现 synthetic/offline exact-raw schema 和候选合同；真实 CAPCO/CSRC/CSI/CNI 数据继续保持候选和许可待确认。

### 退出条件

- symbol rename 不改变 instrument membership；
- reused symbol 不继承旧证券分类；
- future classification publication 不回填过去；
- taxonomy/version 不混流；
- 缺 coverage 时 Big Trend 输入被阻断；
- 不把商业概念板块冒充官方行业分类。

## 5. Stage 3A：Event Intelligence Foundation

### 核心对象

```text
EventSourceAuthority
EventType
EventLifecycle
EventFact
EventEntityBinding
EventRevisionGraph
EventCandidateBundle
EventEvidenceSnapshot
```

事件至少绑定：

```text
source owner/family/version
source_published_at/granularity
observed_at/retrieved_at/known_at/usable_from
affected instrument/sector/market identities
authority
materiality
novelty
surprise
confirmed
raw_artifact_id
parse_descriptor_id
parser_version
revision/supersedes
```

LLM 只允许在该结构化合同之后用于抽取/解释，不能直接生成买卖动作或事实确认。

### 首版能力

- exact-raw synthetic/offline event document；
- 官方公告/政策候选类型；
- 实体绑定；
- correction/cancellation；
- price-in/confirmation 输入合同；
- 不接公网、不写生产数据库、不产生正式 S3 信号。

## 6. Stage 3B：Big Trend v1

### 状态机

```text
NONE
EMERGING
CONFIRMING
TRENDING
MATURE
DISTRIBUTING
BROKEN
```

### 输入

必须绑定：

```text
calendar snapshot
universe/security-status snapshot
sector/classification snapshot
raw-bar/feature snapshot
optional event evidence snapshot
policy version
as_of
```

### 首版规则证据族

板块：

- 多窗口相对强弱；
- 上涨扩散和 breadth；
- 成交额占比趋势；
- 龙头/中军稳定性；
- 回撤与修复；
- 拥挤与分歧；
- 事件确认。

个股：

- 中期趋势结构；
- 相对板块/市场强度；
- 放量与缩量回踩；
- 突破延续；
- 回撤深度；
- 失效与分配风险。

### 安全规则

- `EMERGING` 只观察，不能单独触发买入；
- `MATURE` 不允许追高；
- `DISTRIBUTING` 只能产生 WARNING/TRIM 倾向；
- `BROKEN` 关闭 Trend Runner；
- 缺失分类、历史 bars 或 PIT 身份时返回 DATA_BLOCKED；
- synthetic 只验证状态机，不声明主升浪捕获率。

## 7. Stage 3C：free-stockdb 本地行情 Sidecar

### 7.1 定位

`free-stockdb` 只作为可选本地 WARM/COLD 行情读取 Sidecar，解决全市场 RAW 日线/分钟线批量查询、远程接口限流和本地断线缓存问题；它不替代 Stage 2A—2E 的 PIT、身份、Universe、分类和公司行为合同。

### 7.2 分阶段执行

```text
Stage 3C.1：默认关闭、loopback-only、read-only、RAW-only Provider 合同与 synthetic HTTP 验收
Stage 3C.2：固定真实 Release/二进制/同步源/manifest/data snapshot，审计网络行为与 50—100 标的数据差异
Stage 3C.3：通过审计后进入 WARM/COLD shadow scanner 与 EOD reconciliation
Stage 3C.4：只有许可、来源、PIT、Universe 和公司行为全部闭环后，才重新评估研究用途
```

### 7.3 硬边界

- 7899 不得暴露公网或局域网；
- 不允许 `cmd=set` 或其他写接口；
- Sidecar 目录、进程和数据独立于 `stock-tracker` 生产 SQLite；
- 启用前必须绑定 release version、binary SHA-256、sync manifest SHA-256 和 data snapshot SHA-256；
- 信任固定为 `T1_BEST_EFFORT`；
- `allow_live_decision=false`；
- `allow_model_training=false`；
- `allow_public_redistribution=false`；
- 只接受 `raw/none`，不得用 RAW Bar 冒充 qfq/hfq；
- 当前板块映射不得回填历史；
- 上游复权因子只可诊断/交叉核验，不能替代正式公司行为链。

### 7.4 本轮退出条件

```text
FreeStockDbProvider 默认关闭
literal loopback IP only
bounded GET-only HTTP
strict JSON / finite numeric / OHLC / timestamp contract
RAW/qfq 路由隔离
读取证据绑定 release/binary/data/manifest/response SHA
本地模拟 HTTP 到验收 CLI 回归通过
真实发行包和数据明确标记 NOT_AUDITED / NOT_VALIDATED
```

详细合同见：

```text
docs/STAGE3C-FREE-STOCKDB-SIDECAR-CONTRACT.md
docs/STAGE3C-FREE-STOCKDB-IMPLEMENTATION-HANDOFF.md
```

## 8. Stage 4A：Outcome 与 Scoreboard

### Outcome 合同

```text
signal_id
strategy_id/version
decision snapshot ID
entry intent/fill
exit intent/fill
cost schedule
MFE/MAE
realized R
holding sessions
terminal reason
outcome completeness
```

### Scoreboard

只有完整、可执行、成本后 Outcome 才能计入：

```text
sample count
win rate
average/median R
net expectancy
profit factor
max drawdown
recent-window weighted metrics
regime/sector buckets
```

真实证据不足时：

```text
INSUFFICIENT_REAL_EVIDENCE
```

## 9. Stage 4B：Replay

### 目标

指定历史 `as_of`，只使用当时可见数据重建：

```text
calendar
universe/status
corporate actions
sector memberships
events
bars/features
model/config/policy
portfolio state
DecisionBrief
```

### Fail-closed

任一关键 Snapshot 不满足用途合同时：

```text
ReplayState.BLOCKED
```

禁止用当前运行 SQLite 倒放冒充 PIT Replay。

## 10. 本轮实际执行范围

本轮优先实际完成：

```text
1. Stage 2B/2C 收口与全量基线
2. Stage 2D 公司行为身份绑定/Bundle/对账
3. Stage 2E 分类/板块 PIT 核心与 synthetic adapter
4. Stage 3A Event Intelligence 核心合同
5. Stage 3B Big Trend v1 规则状态机合同
6. Stage 3C.1 free-stockdb 隔离 Sidecar 合同与 synthetic localhost HTTP 验收
```

Stage 3C.2 真实发行版、网络与数据审计，以及 Stage 4A/4B 的 Outcome/Replay，进入后续独立阶段。不会为了“阶段数量”牺牲 PIT、身份、许可和证据正确性。

## 11. 统一验证门禁

每个阶段完成后至少运行：

```text
focused tests
python -m unittest discover -s tests_quant -p "test_*.py" -v
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q stock_tracker tests tests_quant scripts
python scripts/run_quant_contract_smoke.py
python scripts/run_quant_fixture_benchmark.py
python scripts/quant_migrate.py --database data/stock_tracker.db
python -m pip check
ruff check <changed Python files>
git diff --check
```

生产数据库前后 SHA-256 必须相等；禁止使用 `--apply`。
