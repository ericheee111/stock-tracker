# 多周期 P1–P3 收口与资源摘要：验证记录

2026-09-12。状态：IMPLEMENTED / INDEPENDENT_CODE_REVIEW_PASSED / SYNTHETIC_ENGINEERING_GATES_PASSED。

## 身份与范围

发布基线 `c0c39977f4460cc9102fb3731b35cbf88786fc1d`。承接已有在线计划实现 `4b92c5b838c80fa3b4a2bdb719ce4f10db6410cd`，收口 `024c640f84bbb19c34eaa52be37108565d437872`，补充修复 `96dade78d2da4a544e44304478978919dfd4dc7b`，只读资源摘要实现 `10a81253d1f155a6e3fb4df929f37e8cab343ec8`，实现tree `089de239c893a2ca9fc1ce012760b0fa4338c725`。

同股用途分配、核心保留量、共享旧仓与币种现金、T条件预演/内部预留/未执行取消/人工对账、双主线页面和相对持有情景已贯通；本轮进一步关闭认证/重试/预演/过期/旧成本兼容/故障恢复指引问题，并增加资源摘要与复核提醒。模型、风险比例、信号算法、券商和真实成交层没有改变。

## 实际最终门禁

| 门禁 | 结果 |
|---|---|
| 领域/存储/CLI/API/恢复/资源聚焦 | 83 tests，exit 0 |
| Full Runtime | Ran 868；867通过、1预期skip；exit 0 |
| Full Quant | 724通过；exit 0 |
| 4G/4F/Outcome 相邻回归 | 114通过；exit 0 |
| Source distribution / no-bytecode | 2通过；exit 0 |
| 新增生命周期 Chromium 检查 | 18/18；exit 0 |
| 临时真实API＋计划UI | 27/27；exit 0 |
| Today Mock / 临时真实API Today | 18/18、18/18；exit 0 |
| 临时 Portfolio CRUD | 13/13；exit 0 |
| Targeted Ruff | pass |
| portfolio_planning / planning_handlers / runtime_evidence basedpyright | 0 errors |
| compileall 外部cache / pip check | pass |
| Quant smoke / synthetic benchmark | pass；未晋级 |
| 实现树独立重建 | 686/686 Git blob字节匹配；重建后83测试、分发2、生命周期18、计划UI27均通过 |

实现归档SHA-256 `dee79454a4579518e43ea482d49cccb0d0b57c988a458fe4513fdbcc9ce0d0f3`。后续文档提交的最终tree/归档与发布回执外置，避免commit自引用。所有测试使用临时SQLite和合成事实；没有打开生产SQLite或调用真实Provider。既有HTTP/临时SQLite ResourceWarning保留，不宣称已修复。没有重跑完整54截图矩阵或完整无障碍认证。

## 独立审查

原五项审查问题及后续两项UI问题经过反例、修复、复审关闭。96dade…完成基础收口独立审查后，才实施资源摘要；最终10a812…再次由独立只读Codex High审阅全部37文件feature diff并通过，12/12 Node VM合成渲染断言通过。该Reviewer环境没有Python，未执行后端/浏览器全套；实现者执行的测试与独立代码审查分开记录。完整原输出见 `MULTIHORIZON-INDEPENDENT-REVIEW-20260912.md`。

## 固定复跑入口

```text
py -3.14 -X utf8 -B -m unittest tests.test_portfolio_planning tests.test_planning_api tests.test_planning_recovery tests.test_planning_resources -v
py -3.14 -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v
py -3.14 -X utf8 -B -m unittest discover -s tests_quant -p "test_*.py" -q
py -3.14 -X utf8 -B -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q
node qa/ui/planning_lifecycle_qa.cjs
py -3.14 -X utf8 -B scripts/run_planning_integration.py
node qa/ui/today_action_qa.cjs
py -3.14 -X utf8 -B scripts/run_stage1_today_integration.py
py -3.14 -X utf8 -B -m basedpyright --level error stock_tracker/portfolio_planning stock_tracker/api/planning_handlers.py stock_tracker/runtime_evidence
```

Playwright可用 `PLAYWRIGHT_MODULE`/`PLAYWRIGHT_PATH` 指定已安装目录。生产DB/WAL/SHM只做文件哈希；TEMP/TMP/PYTHONPYCACHEPREFIX与浏览器报告写外部目录。不要使用真实账户或生产运行目录执行测试。

## 保留失败与未采纳草稿

新增生命周期红测13项仅2项通过；第二次独立审查新增两个反例后16项中14通过；修正后16/16，再扩展资源及clock rollback成18/18。旧成本/恢复红测6项含失败和异常，修正后通过。新资源模块首次类型检查有4项None收窄问题，修正后0错误。没有删失败日志或弱化断言。

另一个 `chatgpt-product-reassessment` 草稿保持原样，25项测试出现9个失败记录和4个error（含subTests，不当作13个独立失败方法）；评分/费用/输入合同未闭环，不合并。TLS和数值防御等有价值的方向放入下一独立基线任务。

## 数据、边界与发布

生产哈希：DB `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7`；WAL `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e`；SHM `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3`。原main和其他工作区保持，不使用reset/clean/force push。

证据目录 `D:\Projects\stock-tracker-review\multihorizon-closure-20260912\`。DELIVERY.json保存实际远端回执；本文件不提前声称push成功。

范围始终MANUAL_UNVERIFIED / MANUAL_SCENARIO。没有真实fill/部分成交账本、公司行为/现金流完整归因、实时T信号或自动委托；没有B1d/B2/B3语义接线或4H可信准入。计划过期不释放内部预留，提醒不自动产生EXIT；显式初始化才创建独立计划库。
