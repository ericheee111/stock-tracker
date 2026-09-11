# 多周期 P1–P3 候选与验证记录

2026-09-11。当前状态：IMPLEMENTATION_CANDIDATE / FULL_GATES_AND_INDEPENDENT_REVIEW_PENDING。

## 精确范围

基线：`c0c39977f4460cc9102fb3731b35cbf88786fc1d`。工作区：`chatgpt-project-reassessment-4d8d0753`。本次承接此前已确认但未提交的交易画像/PRD文档，并新增可用的手工计划切片；没有导入另一个 `chatgpt-product-reassessment` 工作树中的未验证算法修改。

P1 持仓分层、P2 独立计划日志/库存现金/预留和对账、P3 双主线与手工情景测算已实现。原评分、市场规则配置、风险百分比、策略、生产Schema和Outcome证据层未改。本计划库的规则是人工规划约束，不证明券商实时库存或可成交性。

## 已执行的阶段验证

- 初始领域/存储42测试：Windows只读fsync句柄错误导致15项初始化error；改为写权限文件句柄后42通过。不是绕过文件发布步骤。
- API首次合并57测试：一项预期401的断言与既有未配置私有API返回503不符；按实际安全合同拆成未配置503、已配置但未认证401后通过。未修改原认证规则。
- 新增跨计划库ID、同股重建旧预留边界后：59测试通过。
- 临时真实API/browser：24/24 场景通过，包括54场景旧矩阵以外的新增计划网流程、核心保留量、同ID丢响应重试、未自动释放、对账、情景、360/768/1440与禁用/认证错误。
- 首次Ruff/type检查有import/泛型/None收窄错误；已按源代码边界修正，最终计数待整体验证附录。

“24/24”仅为新增流程的自动DOM/API/异常断言，不代表人工逐像素或完整可访问性认证。所有行情及账户都是合成fixture，使用临时SQLite；无真实投资表现。

## 固定门禁

```text
py -3.14 -X utf8 -B -m unittest tests.test_portfolio_planning tests.test_planning_api -v
py -3.14 -X utf8 -B scripts/run_planning_integration.py
py -3.14 -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v
py -3.14 -X utf8 -B -m unittest discover -s tests_quant -p "test_*.py" -q
py -3.14 -X utf8 -B -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -q
py -3.14 -X utf8 -B -m basedpyright --level error stock_tracker/portfolio_planning stock_tracker/api/planning_handlers.py stock_tracker/runtime_evidence
node qa/ui/today_action_qa.cjs
py -3.14 -X utf8 -B scripts/run_stage1_today_integration.py
```

另执行相邻Outcome/4F/4G、Targeted Ruff、compileall（外部pycache）、pip check、Quant smoke与fixture benchmark；最终提交从Git树精确重建复跑。新源文件进入critical分发清单，不删门禁换绿色。

## 独立审查门

将由未参与实现的本地Codex实例，High思考强度、read-only sandbox，对精确候选进行独立代码审查。未获得真实review输出前不写通过；CLI、模型、候选SHA、原始结果和修复闭环在仓库外证据包保存。此文档目前不声称独立审查已执行。

## 保护和发布

证据目录：`D:\Projects\stock-tracker-review\multihorizon-20260911\`，包含每次执行的实际命令、退出码及保留失败。原main/其他Agent工作区、生产DB/WAL/SHM前后文件hash需一致。此任务不对生产数据库打开SQLite连接或应用迁移。

实现与文档候选可本地提交；只有独立审查与最终门禁通过才允许普通快进发布。禁止force push、reset、clean或隐藏原并行工作。最终commit/tree外置于交付记录，避免commit自引用SHA。

## 非完成项

多周期算法参数与模型校准未变；没有实时T信号或自动订单；没有真实fill/部分成交账本、公司行为/外部现金流归因；没有自动连接B1d/B2/B3或4H可信准入；计划日志不是数字签名。当前只按本轮手工计划范围验收。
