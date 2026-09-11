# W5 有限修正集成交接

状态：INDEPENDENT_REVIEW_PASSED / RELEASE_AUTHORIZED（2026-09-11）。
精确审查候选为 `139ce41baea404c9dd54798562e5f3ad85ee73f8`；
后续交接/发布记录见 [W5-GITHUB-DELIVERY.md](W5-GITHUB-DELIVERY.md)。下文保留原集成交付边界。来源基线为
`788ffdb1cc3e770f99d63bdd10d923f46d9b1ddd`，tree
`18a5b27bf2219bd5536f76736099da9ad5dac262`。本文仅记录 W5 的测试工具、UI
与专项文档集成，不替代 PRD、AGENTS、总 Handoff 或历史阶段验证报告。

## 输入与归属

使用 campaign-w5-788ffdb 的 WB1/WB2/WB3 revision-1 原始 patch、原始工作文件，
以及 WB4 原 REPO-ATLAS 与 revision-1 纠错资料。未复制审计临时重建目录。
原文件 SHA256、patch SHA256、应用时字节差异、LF 规范化结果和最终 Git blob
分别记录在仓外验证包 SOURCE-MANIFEST.json，不能将规范化相等写成原字节相等。

原 main 五文件 overlay 独立保留：`web/css/monitor.css`、`web/css/runtime.css`、
`web/css/terminal.css`、`web/index.html`、`web/js/components.js`。这部分是原有
用户工作，不计为新增 WB 修改。随后 UI 修正涉及 index、app、components、format；
qa/wb1、qa/wb2、qa/wb3 与本文是新增收编。全部工作限于独立分支。

不收编历史 PROGRESS、演示输出、audit_verify、make_deliverables、运行账本和截图。
旧 WB SUMMARY 的计数、耗时和 PASS 仅属于其旧候选，不能证明本候选。WB4 manifest
曾将生成前读取自身的 FileNotFoundError 写成 PARSE_ERROR；应以当前 JSON 解析及
逐文件散列核验结果为准，保留该旧记录而不冒充当前解析失败。

## 五项闭环合同

| 项 | 修正 | 可重复验证入口 |
| --- | --- | --- |
| T1 | 全部 subTest 跳过的 method 标 ALL_SUBTESTS_SKIPPED；没有实际验证时 NOT_VALIDATED / exit 2。unittest 原始计数与 method/subcase 计数分开 | `qa/wb2/test_w5_revision.py`，真实 CLI 与标准 TextTestRunner 差分 |
| T2 | 每个 subcase 保存 parent、subtest、唯一 occurrence 身份及完整侧车 traceback；preview 最多 2000 字符 | 同上：长尾标记、多 subcase、重复参数、XFAIL/XPASS、class/module error |
| U1 | array 必须有合法显式 market；dict 由 a/hk/us 键定身份，显式 market 必须一致；合法无 market 的 index dict 保持兼容 | `qa/wb3/w5_revision.test.cjs`、`wb3_probe_markets.cjs` |
| U2 | 仅自己启动的 server 的匹配 READY 可触发一次浏览器启动；端口占用、启动/READY/浏览器错误区分 exit 2，锁缩放为 exit 1，正例为 0 | `wb3_runner_negative.test.cjs` 实际子进程；`w5_revision.test.cjs` VM 单启动检查 |
| U3 | 冻结 expected 组合，对照身份唯一性、页面/状态、DOM 与异常采集完成；空、重复、缺项、缺 DOM 都失败 | `wb3_matrix.cjs`、`wb3_verdict.cjs`、完整 capture 及真实 broken 页面负例 |

标准 unittest 是全量门禁；WB2 timer 作为补充计量工具。testsRun、skip、expected
failure、unexpected success、subcase 分开解释，不以 method 数代替 subcase 数。
Runner 的空计划、未知 gate、空过滤、缺程序和 timeout 均有实际 CLI 非零对照。
WB1 显式三模块 gate 不属于默认 tests discover；内置名 focused-218 是历史兼容别名，
实际数量以当次结果为准。负例中的 naive datetime 是有意构造，局部 lint 例外有说明。

## 重跑方法

从精确候选 Git checkout 运行。固定 Python 3.14，记录 sys.executable/version。
PYTHONPYCACHEPREFIX、WB2_SELFTEST_DIR、WB3_REPORT_DIR 指向新的仓外目录；保留
WorkBuddy 安全环境，不改系统依赖。Playwright 可通过 PLAYWRIGHT_MODULE 或
PLAYWRIGHT_PATH 指向本机既有模块，也可使用常规 require('playwright')。
浏览器使用本机已安装的匹配 Chromium，不将个人机器路径写入源码。

```text
py -3.14 -m unittest qa.wb1.test_wb1_clock_boundary qa.wb1.test_wb1_identity_lifecycle qa.wb1.test_wb1_type_boundary -v
py -3.14 -m qa.wb1.fuzz_primitives
py -3.14 -m unittest qa.wb2.wb2_selftest qa.wb2.test_w5_revision -v
node qa/wb3/format_render.test.cjs
node --test qa/wb3/w5_revision.test.cjs
node qa/wb3/wb3_runner_negative.test.cjs
node qa/wb3/wb3_probe_markets.cjs
node qa/wb3/wb3_probe_nulls.cjs
node qa/wb3/wb3_capture.cjs
py -3.14 -m unittest tests.test_market_source_snapshot_contracts.TestR41TransportLifecycle -v
py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -v
py -3.14 -m unittest discover -s tests -p "test_*.py" -v
py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -v
py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -v
py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -v
py -3.14 -m ruff check qa/wb1 qa/wb2
py -3.14 -m basedpyright --level error stock_tracker/runtime_evidence
py -3.14 -m compileall -q stock_tracker tests tests_quant scripts qa/wb1 qa/wb2
py -3.14 -m pip check
node qa/ui/today_action_qa.cjs
py -3.14 scripts/run_stage1_today_integration.py
py -3.14 scripts/run_quant_contract_smoke.py
py -3.14 scripts/run_quant_fixture_benchmark.py
```

完整 UI 默认冻结 54 组合（页面/尺寸/主题/状态/Monitor tab）；设置 WB3_SCENARIOS
只用于明确的专项/负对照，不能替代完整矩阵。每个 capture 包保存 expected 清单、
逐组合 DOM/API/异常、截图、真实 Tab 键焦点序列和 verdict；Tab 记录只是本次按键观察，
不是完整无障碍认证。不同运行不得覆盖旧目录。服务使用自己创建的 loopback 端口和
随机 run ID；只清理自己创建的进程，端口占用不杀外部进程。

## WB4 可复用索引与纠错

| 领域 | 当前来源 | 阅读边界 |
| --- | --- | --- |
| 产品与部署 | `PRD-股票辅助判断与交易参考网站.md`、`HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md` | 当前产品/部署规范；历史门禁计数不更新 |
| Runtime 生命周期 | `STAGE4G1-CHECKPOINT-B0-B1-R4.1-VALIDATION.md` | 生命周期 scoped 修正及历史验证记录 |
| Market Path 规划 | `STAGE4G1-CHECKPOINT-B1-B3-MARKET-PATH-PIPELINE-DESIGN.md` | 设计文档，不能推导物理实现已完成 |
| Collection / Ledger | `STAGE4G-RUNTIME-OUTCOME-COLLECTION-FINALIZATION.md`、`STAGE4F-OUTCOME-EVIDENCE-LEDGER-DESIGN.md` | 手工 Collection/Finalization Core 与 append-only 候选证据 |
| Admission | `STAGE4H-TRUSTED-OUTCOME-ADMISSION-AUTHORITY-DESIGN.md` | 独立可信准入仍有前置阻断 |

调用链复核的纠错：

- `scripts/query_market_event_replay.py:main` 构造 MarketEventStore；构造创建目录和
  初始化 SQLite，MarketEventReplay.run 默认 record_run=True 并写 Replay Catalog。
  分类为 LOCAL_STORE_WRITE，不能因为脚本名含 query 就当纯只读。W5 未运行该 CLI。
- report_outcome_ledger 的报告目标参数是 `--output-dir`，并要求 strategy、market、
  horizon、evidence-tier、window、as-of；report_stage2h_market_bar_acceptance 要求
  `--manifest --artifact-root --output-dir`。ingest_outcome_ledger 是追加写账本。
  各 CLI 依其 parser 和调用链分类，不能统一套用 `--output` 或仅凭 grep 判断副作用。
- focused 和相邻合同测试包含临时 SQLite 写入；它们不是无 I/O 测试。
- `stock_tracker/__main__.py:_build_runtime_artifact_service` 已构建 Artifact/Outbox
  Worker，并在入口条件 start。尚未实现的 Market Path/B2 物理管线须另行区分；
  已有 worker 不证明自动 Outcome Collection 完成。
- reconciliation fixture 目录的旧计划引用属于历史计划与现状差异；页面已有 inline
  SVG favicon，旧路径差异不等于缺图标；WB4 TR-13a…g 应逐文件绑定 blob。

## 交付及停止条件

仓外 `w5-integration-<FINAL_SHORT_SHA>` 包包含最终 commit/tree 与各 scoped commit、
完整基线 patch、Git archive、逐文件 manifest、五项 closure、原始命令账本/退出码、
失败与重跑说明、54 场景证据、重建核验及保护对象前后 SHA。精确重建必须来自最终
Git commit，不能依赖未跟踪文件；源码树冻结散列将测试绑定到提交内容。

保持原 main overlay、原 Codex 工作树及四个 WB clone 不变。生产 DB/WAL/SHM 仅文件
散列核对，不连接、不迁移。未修改 Runtime/Transport/Authority/Outcome 核心，未进入
B1 物理 Store，未使用真实市场数据。synthetic/临时 API 工程通过不代表真实 Coverage、
Shadow Acceptance、Trusted Admission 或投资表现。自动交易仍关闭。

候选已通过独立审查。用户随后授权受控 merge/push，发布在独立工作区执行；
原 main/UI 不动，WorkBuddy 后续复跑仍须绑定精确 commit。暂不开始 B1。
