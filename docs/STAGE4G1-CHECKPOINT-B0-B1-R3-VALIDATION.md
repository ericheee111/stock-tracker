# Stage 4G.1 B0/B1 R3 Validation

日期：2026-09-04。状态：`R3_LOCAL_CANDIDATE / INDEPENDENT_REVIEW_PENDING`。

本记录只覆盖 Semantic Derivation、Impact-aware Coverage、Typed Authority
Facts、Projection Correctness 和 Selection→Path Reverse Completeness。
所有新纯 Python factory 的 Assurance 固定为 `STRUCTURAL_FIXTURE`。

## 1. Baseline 与边界

- Workspace：`D:\Projects\stock-tracker-wt\codex-stage4g1`。
- Branch：`agent/codex-stage4g1`。
- 起始 HEAD：`e6f2dd439765db608f0e2a78b9c7c2a6d57683f9`。
- 起始 Tree：`32a734bb64ee9c6d8b4d17d0c0b8326a275fbc58`。
- 本轮开始时工作树 clean；全部实现、修复与验证在同一个 Codex 会话完成。
- 不启动子 Agent，不在 main 工作树开发，不改生产 DB，不执行 migration apply。
- 不实现 Concrete Market Event Store、B2 Path Worker、Collection Bridge、
  Paper/Manual、Trusted Admission、Scoreboard 或 Broker/XTP write API。
- 没有 push、amend、reset、rebase、clean、stash 或 `git add -A`。

最终 commit/index tree/archive 身份在外部证据包 `MECHANICAL-INPUT.md`
和 `git-evidence.txt` 中记录，避免把最终 commit SHA 写入自身形成循环。

## 2. 实际修改

生产文件仅三个：`runtime_evidence/source_snapshot_contracts.py`、
`runtime_evidence/path_contracts.py`、`runtime_evidence/__init__.py`。
测试只扩展/适配已有的两个 contract test 文件；另更新 B0/B1 设计与本记录。

- `DecodedTradeTick` 从 exact schema/policy/verified member 的 canonical
  payload 派生 Decimal price、quantity 与全部来源身份，不接受独立价格。
- Projection 从全部 ordered decoded ticks 计算 high/low/close；空 selection、
  遗漏输入、未完成 interval coverage、越过 BREAK/HALT 均失败关闭。
- `PathProjectionManifest` 绑定 selection→decoded tick→observation 消费映射；
  Resolver 在 first-touch 前检查 eligible source member，遗漏返回
  `BLOCKED / UNPROJECTED_SOURCE_MEMBER`，重复消费直接拒绝。
- Sequence Finding 按 impact scope/time/epoch/event-type intersection 污染
  selection，不能因 gap carrier 属于其他 symbol 就隐去 global gap。
- Subscription Manifest 保存实际 membership；Source Session Manifest 保留
  LIVE/REPLAY/BACKFILL、activation、coverage bounds、callback bounds、
  queue/drop 计数。空 selection 只有在完整、订阅且无损的覆盖下才证明零事件。
- Calendar、Security Status、Source Coverage 是真实 typed payload；session
  fields 从 authority references 派生，不能另传状态、边界或 complete。
- Calendar segments 明确区分 continuous/auction/break/halt；coverage 按要求
  segment 验证。半日市来自 typed Calendar，不在生产代码硬编码市场时刻。
- Authority 全局 append order/hash chain 与每个 fact revision 分离。
- Record file bytes 使用无自引用的 canonical content；file SHA 等于 content
  SHA，catalog identity 不写入被自身哈希的文档。
- 受影响 schema/policy 显式升级，旧形状失败关闭。Execution/no-entry 仍是
  `structurally_projectable_to_stage4g_v3`，不是 verified/finalizable。

## 3. Red→Green 与可观察行为

先在 R2 行为上加入本轮 14 个必须失败的回归，保存
`red-regressions.log`，随后修复实现和严格 fixture 构造路径。
原有测试名称没有删除；旧的独立 OHLC/session fixture 改为真实 synthetic
payload→decoder→projection/typed facts。非法构造在更早边界拒绝的用例，
断言随边界移动，不改成接受非法数据。

本地公开合同探针 `run_r3_adversarial_probes.py` 观察到：

| 输入/操作 | 实际结果 |
| --- | --- |
| payload price 10 → Tick Path | price 为 10；另填 12 被拒绝 |
| 三个 input price 11 → Bar | high/low/close 均为 11；另填 99 被拒绝 |
| Projection 遗漏 interval member | `RuntimePathContractError` |
| Selection 有两条，Path 只消费后一条 TARGET | `BLOCKED / UNPROJECTED_SOURCE_MEMBER` |

另外运行 28 项 R3 对抗测试，覆盖跨 symbol/epoch gap、订阅与零事件证明、
queue/drop、LIVE/backfill、午休/halt/半日市、segment gap、revision/append
chain、record hash 和 assurance 边界。结果全部通过，详情在
`adversarial-probes.json`；这是本会话 local synthetic QA，不是独立 Review。

## 4. 本轮完整门禁

使用 CPython 3.14；全部命令 exit code 0。时间、完整 argv、cwd、日志 SHA
保存在外部 `commands.jsonl`。`PYTHONPYCACHEPREFIX` 指向外部 evidence 工作目录，
compileall 不把 bytecode 写入源码树。

| Gate | 当前结果 |
| --- | --- |
| Focused：runtime_evidence + runtime_path_contracts + market_source_snapshot_contracts | 176 tests，OK |
| Full Runtime | 698 tests，OK，skip 1 |
| Full Quant | 724 tests，OK |
| Stage 4G/4F adjacent regressions | 114 tests，OK |
| Source distribution / no tracked bytecode | 2 tests，OK |
| Targeted Ruff，三个生产文件和两个测试文件 | All checks passed |
| basedpyright，三个生产文件，`--level error` | 0 errors，error-level gate 通过 |
| compileall，stock_tracker/tests/tests_quant/scripts | exit 0 |
| pip check | No broken requirements found |
| diff/check、scoped binary/generated/secret/Broker-write/auto-trade scan | 通过 |

主要可复跑命令：

```text
py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -q
py -3.14 -m unittest discover -s tests -p "test_*.py" -q
py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -q
py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q
py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -q
py -3.14 -m ruff check stock_tracker/runtime_evidence/path_contracts.py stock_tracker/runtime_evidence/source_snapshot_contracts.py stock_tracker/runtime_evidence/__init__.py tests/test_runtime_path_contracts.py tests/test_market_source_snapshot_contracts.py
py -3.14 -m basedpyright --level error stock_tracker/runtime_evidence/path_contracts.py stock_tracker/runtime_evidence/source_snapshot_contracts.py stock_tracker/runtime_evidence/__init__.py
py -3.14 -m compileall -q stock_tracker tests tests_quant scripts
py -3.14 -m pip check
git diff --check
git diff --cached --check
```

Full Runtime 日志含 fixture cleanup `ResourceWarning`，并保留一个现有 skip，
不将其写成全部无警告/无跳过。LSP 插件拒绝隔离 worktree 路径，故以该路径下
CLI basedpyright 做 error-level 诊断；这不等于默认严格配置的所有 warning 已清零。

最终 index/tree/archive 导出、fresh export import/test 与最终 scoped scan
结果由外部交付清单记录；只有这些机械步骤成功才输出 checkpoint-ready。

## 5. 生产数据不变

只计算 main checkout `D:\Projects\stock-tracker\data` 下三个文件 SHA-256，
未读取交易记录、复制 DB 或执行迁移。Task-start、before-gates、after-gates
一致，最终包另附 final 核对。

| 文件 | SHA-256 |
| --- | --- |
| stock_tracker.db | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` |
| stock_tracker.db-wal | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` |
| stock_tracker.db-shm | `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3` |

## 6. 交付与限制

证据目录：`D:\Projects\stock-tracker-review\stage4g1-b0-b1-r3-<FINAL_SHORT_SHA>\`。
包含 `MECHANICAL-INPUT.md`、`commands.jsonl`、Runtime/Quant 等完整日志、
`git-evidence.txt`、`production-db-hashes.txt`、`adversarial-probes.json`，
以及 exact source archive/export 和对应身份校验清单。

- 仅 `STRUCTURAL_FIXTURE`；tuple/hash rescan 不是外部 Store Authority。
- 未产生 `STORE_RESCANNED`、`PIT_CANDIDATE`、`TRUSTED_ADMITTED` receipt。
- 当前全前缀内存扫描只证明工程合同，不证明大数据量 Store 性能或生产吞吐。
- Decoder 只允许冻结 fixture schema，未接真实 XTP decoder。
- Projection 不支持跨 BREAK/HALT 的 Bar，即使调用方称为 DAILY_BAR。
- 未使用真实交易数据、真实多交易日 Shadow Acceptance 或真实策略战绩。
- 没有 Broker 写调用；没有启用自动交易，`auto_trade=false` 边界不变。
- 本地 scoped commits 后暂停，WorkBuddy 机械检查/最终独立 Review 仍待完成。
  未收到 pass 不进入后续阶段；未收到用户明确授权不 push。

本轮最小化原则使用已有模块与标准库，不增加运行时第三方依赖；验证遵循
先观察失败、修复根因、再记录新日志的证据先行流程。
