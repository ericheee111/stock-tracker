# Stage 4G.1 B0/B1 R4 Validation

日期：2026-09-04。状态：`R4_LOCAL_CANDIDATE / INDEPENDENT_REVIEW_PENDING`。

## 基线与范围

- 唯一 Codex 会话；未启动子 Agent、模型或其他 Codex 会话。
- 工作树：`D:\Projects\stock-tracker-wt\codex-stage4g1`。
- 分支：`agent/codex-stage4g1`。
- 起始 HEAD：`90310da632b661866e64003f1b94d965be31948f`。
- 起始 Tree：`76bfd348dee4e45864d84ee34969c26d009b7b4a`。
- 开始时 clean；没有在 main 工作树开发。
- 冻结依据：R3 证据包中的 `CHATGPT-R3-ADVERSARIAL-REVIEW.md`、
  `R4-TRANSPORT-LIVENESS-AND-EFFECTIVE-AUTHORITY-DESIGN.md` 和攻击探针。
- 本轮只修改四个 `runtime_evidence` 文件、两个已有 contract test 文件与本文。
- 不修改 AGENTS/handoff/PRD/UI/QA/生产数据；不执行 migration apply。
- 不实现 Concrete Event/Transport Store、B2 Worker、Collection Bridge、
  Paper/Manual、Admission、Scoreboard 或 Broker/XTP write API。

最终 commit/tree/archive 身份只写在外部证据包，避免自身 SHA 循环。

## 实际合同变更

### Transport 与 Event 共同冻结

- 增加 typed Transport Record、完整连续 hash prefix Snapshot、固定 fixture
  Liveness Policy 和确定性 Coverage Certificate。所有输出仍为 `STRUCTURAL_FIXTURE`。
- Record 校验 UTC、实际类型、canonical payload、全局 append/hash、epoch
  生命周期、queue/drop/callback/provider 水位单调性；hash 不能绕过 payload/type。
- Event Audit 固定 exact Transport snapshot/audit/high-water；Certificate 同时
  固定 Event snapshot/audit/high-water、完整 prefix、manifest、membership、query
  与 policy。更换 Transport 会生成不同 Event snapshot，不能回填旧快照。
- Certificate 重建 Event inventory/audit 与 Transport prefix；输出 connection/
  subscription/heartbeat/watermark intervals、counter 起终点和 clean-close 信息。
- LIVE 覆盖要求 connection、准确 subscription ACK、周期 heartbeat/queue/callback
  watermark、无丢失、准确 callback/provider prefix 绑定和最终水位。
  CONNECTED/全局 heartbeat 可无 subscription scope；ACK 必须准确绑定 scope。
- 冻结 Event prefix 中存在晚于最后 watermark 才落盘的同 epoch 事件时，失败关闭。
- `expected_first_callback_seq` 不能单独证明 start/liveness；无 callback 且无活性
  证据为 `EMPTY_NOT_PROVEN`，不能证明零事件或产生 TIMEOUT。
- 非空 records 也不自动构成完整覆盖；时钟/序列/水位完整性冲突使用
  `BLOCKED_SEQUENCE_OR_TRANSPORT_INTEGRITY`。普通断线/缺 heartbeat 保持 incomplete。
- Session Complete 额外要求 typed Calendar segments 完整和 clean close；不伪造
  BREAK/HALT 价格，不以关闭时间字段代替实际 close transport fact。
- REPLAY/BACKFILL 绑定已证明覆盖的独立 input Selection/Verification、typed job
  start/complete、exact output prefix、输入输出逐条语义一致、无失败/丢失和 completion
  known-at。没有完成 job 或 input 未证明覆盖，不能自行启动历史 coverage 证明链。

### Source Clock Policy

当前 registry 只允许 `stage4g1-fixture-source-clock-v1`：

- source ahead-of-received 最多 2 秒；
- received-to-durable 最多 5 分钟；
- source regression 容忍度 0；
- aware UTC、microsecond 精度；超限为 impact-aware finding 并阻断相交覆盖。

Liveness registry 只允许 `stage4g1-fixture-transport-liveness-v1`，heartbeat 与
watermark 最大间距均为 5 分钟；要求 subscription ACK、queue watermark 和 clean
session close，不把普通 callback 当 heartbeat。数值是冻结 synthetic fixture
policy，不是对真实 Provider/生产运行参数的推荐。未知 policy 失败关闭。

### Authority 生效修订

- 从 typed Calendar/Status/Coverage fact 派生稳定 entity key。Coverage key 不含
  Selection/Snapshot ID；快照更新属于同一 entity 的新内容版本。
- 全局 append/hash 与 entity revision/previous/supersedes 双链同时校验；首版 1，
  后续严格加 1，禁止重复、缺前驱、回退、分支或循环。
- `authority_record_policy_id` 与 business policy、effective-selection policy
  分开命名，不把一个任意 policy SHA 当作三种语义。
- `RuntimeAuthorityEffectiveSelection` 从完整 typed inventory 中筛选
  `known_at <= cutoff && usable_from <= cutoff` 且 query 匹配的最新合法修订。
  完整审计可保留尚未 usable 的未来修订；历史 cutoff 仍选择当时已生效版本。
- Prefix 的 authority audit 仍须在 freeze 时已知；引用必须等于该 entity 的
  effective active fact，而不是仅存在于完整 inventory。
- 较新 usable Calendar 在 freeze 前出现时，旧 session 引用以稳定
  `STALE_AUTHORITY_FACT` code 拒绝；本阶段不替 B2 自动追加新版 SESSION。
- `_require_number()` 只做类型收窄，bool、NaN/Infinity、正数/非负规则不变。

### 显式版本

- Event Audit / Snapshot / Selection：v5。
- Source Session Manifest：v3；Sequence Policy：v3，导出名称 `MARKET_EVENT_SEQUENCE_POLICY_V3`。
- Runtime Path Contract / Prefix：v5。
- Authority canonical fact：v3；reference / snapshot binding：v4。
- SourceCoverageFact：v2。新增 Transport / Effective Selection 合同为 v1。
- 这些是纯合同版本，不代表物理 Market Event Store v4 或 migration 已实现。

## Red → Green 与当前验证

先增加 19 个 R4 测试并保存红灯日志；直接复现无活性仍为零事件、无活性仍 TIMEOUT、
revision 回退和一小时负 observed latency 未阻断。新增 API 尚不存在的用例以
构造/import 失败保存。第一版测试 helper 的字段排除及 Windows GBK 解码问题也保留
在初始诊断日志中，随后修正，没有把测试自身错误伪装成产品缺陷。

新增/保留探针还覆盖：合法小 skew、durability delay 边界、global clock impact、
缺 ACK/最终 watermark、queue/drop、链回退、跨 Store/冻结快照、最后水位后落盘事件、
backfill job、stale Prefix、future known/usable、Coverage entity 稳定性、assurance。

R3 的 payload-derived Tick、deterministic Projection、exact member consumption、
`UNPROJECTED_SOURCE_MEMBER`、global finding、Calendar BREAK/HALT 回归继续保留。
原测试名称未删除；原先独立 entity 从 revision 700 起步的 fixture 按新首版规则改为 1，
仍验证 entity revision 不等于全局 append order。

首轮 gates-01：Focused 207、Runtime 729（skip 1）、Quant 724、4G/4F 114、distribution 2
均通过。合法 optional scope 与最小 numeric diff 收尾后，执行 gates-02 重新验证。

最终 gates-02：全部 exit 0；Focused 207、Runtime 729（skip 1）、Quant 724、
4G/4F 114、distribution/no-bytecode 2、local adversarial 46。全包 basedpyright
为 0 errors / 0 warnings / 0 notes；Ruff、compileall、pip check、diff/cached check
通过。门禁期间六个源码/测试文件 SHA 不变，DB/WAL/SHM 前后 SHA 与 task-start 一致。
公开合同探针实际观察到无 Transport → EMPTY_NOT_PROVEN / BLOCKED；完整 typed LIVE
且零事件 → ZERO_EVENT_PROVEN / TIMEOUT；断线 → EMPTY_NOT_PROVEN。
这些结果均为本地 synthetic QA，不是独立 Review。

命令、时间、退出码和日志 SHA 均在外部 `commands.jsonl`；最终门禁包括：

```text
py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -q
py -3.14 -m unittest discover -s tests -p "test_*.py" -q
py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -q
py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q
py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -q
py -3.14 -m basedpyright --level error stock_tracker/runtime_evidence
py -3.14 -m compileall -q stock_tracker tests tests_quant scripts
py -3.14 -m pip check
git diff --check
git diff --cached --check
```

Ruff 只覆盖本轮六个 Python 文件。compileall 使用外部 PYTHONPYCACHEPREFIX；不向源码树
写 bytecode。LSP 插件拒绝隔离 worktree 路径，以该工作树下完整包 CLI basedpyright
验证。长流水线曾超过 MCP 单次 300 秒等待窗口，实际子进程继续产生逐条完成日志；
验收以完成 ledger、gate-summary 和对应日志 hash 为准，不把 transport timeout 当通过。
Runtime 日志保留现有 ResourceWarning 和 skip，未掩盖警告。

## 生产数据与交付

只对 `D:\Projects\stock-tracker\data` 的 DB/WAL/SHM 计算 SHA-256；没有查询交易记录、
复制数据库、连接生产 SQLite 或执行迁移。task-start、pre/post-gates、最终打包再核对。

| 文件 | 起始 SHA-256 |
| --- | --- |
| stock_tracker.db | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` |
| stock_tracker.db-wal | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` |
| stock_tracker.db-shm | `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3` |

最终外部目录：`D:\Projects\stock-tracker-review\stage4g1-b0-b1-r4-<FINAL_SHORT_SHA>\`。
包括 MECHANICAL-INPUT、完整命令 ledger、Runtime/Quant 日志、DB hashes、adversarial
observations、exact Git Index ZIP/export、逐文件 Git blob/ SHA-256 和 scoped scans。
导出时只对该命令设置 LF，逐项核对 index == HEAD tree == archive bytes；不改 Git config。

本地提交分三组：numeric narrowing；相互依赖的 Transport/Authority 合同及测试；本文。
不 push、amend、reset、rebase、clean、stash、merge 或 `git add -A`。

## 限制与退出条件

- 标准库实现，无新运行时依赖；最小化类型修复，不借机改数值语义。
- 所有结果仅 `STRUCTURAL_FIXTURE`，不产生 STORE_RESCANNED/PIT_CANDIDATE/ADMITTED。
- 全前缀内存 rescan 是 O(N) fixture 合同验证，不是大规模物理 Store 的性能证明。
- 单一 fixture transport payload schema、注册 policy 与单符号历史 job 有意失败关闭；
  多 symbol 的 symbol-scoped provider watermark 未建立独立维度时不证明完整覆盖。
- 不声称真实多交易日 Shadow Acceptance、真实策略战绩或真实 Strategy Scoreboard。
- `auto_trade=false`；未开启任何 Broker/XTP Trader/Order/Cancel/Algo/Account/Position API。
- WorkBuddy 只做机械复跑，ChatGPT 做独立源码/攻击性 Review；本会话结果不是独立 pass。
- 完成 exact-export 验证后停在 `CODEX_CHECKPOINT_READY:B0_B1_R4`，不开始 Concrete B1 Store。
