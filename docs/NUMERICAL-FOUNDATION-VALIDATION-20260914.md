# N1–N3 数值与HTTPS基础：验证及独立审查

2026-09-14。实现状态：INDEPENDENT_CODE_REVIEW_PASSED / SYNTHETIC_ENGINEERING_GATES_PASSED。最终发布以仓库外 DELIVERY.json 和 GitHub 回执为准，不在文档提交中自引用自身SHA。

## 精确身份

基线 `cdf8c1b4114252ead3bf5eb19d2f624be8929bd8`；N1 `9df996977fcd58e8707e3591b242aecfd03d7800`；N2 `48089c8d7626e85152ff2e7f07720a1966c2a23a`；N3初候选 `1ca3ce2905f62c011beea31d8c6cdb263a252b05`；最终修复实现 `c3c0ece027d11ad42044bd6272f52aa86c226165`，tree `bb97077b4f94119f75d86285fc2747623d5a9504`。

## 实际执行结果

| 门禁 | 结果 |
|---|---|
| 新增HTTPS/指标/评价聚焦 | 36通过；exit0 |
| 已发布公式兼容向量 | 630指标+8评价；直接核对BASE原始Git blob来源并重算 |
| Full Runtime | 887通过、1预期skip；共888项；exit0 |
| Full Quant | 740通过；exit0 |
| Outcome/4F/4G相邻 | 114通过 |
| Source distribution/no-bytecode | 2通过 |
| Today Mock/临时真实API Today | 18/18、18/18 |
| 临时Portfolio CRUD | 13/13 |
| 临时真实API计划流程/生命周期 | 27/27、18/18 |
| Targeted Ruff | pass |
| 指标/评价及完整runtime_evidence、portfolio_planning类型检查 | 0errors |
| compileall（外部缓存）/pip check | pass |
| Quant smoke/合成benchmark | pass；模型未晋级 |
| 实现树重建 | 692/692文件逐字节匹配Git blob；聚焦及分发复跑通过 |

实现归档SHA256：`76ac960b4094f132e14fa96b21e6c49b68ea6e895a7b45f15237445253f75b20`。完整命令/退出码/日志见外置 commands.jsonl，只有成功退出且完成全部测试的记录计为通过。保留旧的HTTP/SQLite ResourceWarning，不宣称已修复。没有执行完整54场景截图矩阵、真实Provider连通性或实盘效果验证；没有连接生产SQLite或apply迁移。

## 独立审查与失败闭环

Reviewer未参与实现，在只读隔离checkout中用本机Codex High运行，原输出SHA256：`225df585f486ea4d7e56f9ec97794a4fcf6c5b0a04b59e1cf01517e6a8cd1835`。独立审查先后发现RSI均值下溢、回撤次正规资金舍入及非零商下溢问题，均新增红测后修复；同时核查概率误差的同类模式。最终精确候选复审结论见下文原输出。不得把实现者的Python3.14全套结果冒充Reviewer执行。

首次N1红测、N2/N3红测及下溢红测日志保留。另保留错误测试入口造成_helpers导入失败、两项Ruff告警、首次保护门发现另一工作树推进而停止的记录；没有移除门禁换绿色。既有普通输入公式、评分/策略权重、标签、风险百分比和UI未改变。对非法/极端输入的新拒绝行为不是保证所有数据路径都拥有相同输出。

## 并行与生产保护

本任务只写新worktree `chatgpt-numerical-foundation-20260914` 和自己的仓库外证据目录。`chatgpt-numerical-baseline-20260914` 在任务开始前已有独立修改，并在期间自行提交/继续修改；未触碰、回滚或导入它。原始保护快照未改，外部变化单列于 COORDINATION.md/protected-diff.json，不能宣称它完全静止。其他已保护worktree和生产DB/WAL/SHM保持，真实远端在push前再次核对。

## 支持边界

RSI/ATR仍为原滚动算术公式，flatRSI=100保留；EMA短输入均值、hist=DIF-DEA、round-half-even最近观测值分位数保持。指标非有限/错误类型/错位或特定不可表示运算返回None；RSI均值正累计归零或比值溢出不可假装100。盈亏比/净期望及概率误差的非零商或平方/权重下溢到0会明确报错，不等同于一般浮点任意精度保证。评价成本>=0，fractional return>=-1，与R倍数区分；回撤正equity必须保持normal float范围。profit_factor无亏损返回数学inf是legacy函数级约定，不能直接当有限JSON/真实战绩展示。空回撤序列0是诊断约定，不代表有样本。

Runtime远端HTTPS校验证书/hostname且不自动redirect，证书失败交给Router处理不降级。原研究通道与本机Sidecar不合并；真实Provider当前可用性另行运营验收。

## 独立审查原输出

未发现本轮 N1/N2/N3 范围内可确认的 P1/P2/P3 新缺陷或阻断项。

已核对精确审查对象：

- candidate：`c3c0ece027d11ad42044bd6272f52aa86c226165`
- tree：`bb97077b4f94119f75d86285fc2747623d5a9504`
- base：`cdf8c1b4114252ead3bf5eb19d2f624be8929bd8`

实际检查了完整 diff、专项文档及相关 Provider、指标快照、证据聚合、评价和 benchmark 调用方。未发现普通合法输入数值漂移、ATR 新错位或收益单位改变。既有 RSI `or` 回填、评分及 DQ 问题按明确边界留待后续。

本次验证：

- 使用 `python3.12.exe -B -m unittest` 分两批运行新增策略测试、既有指标及相邻 Runtime 测试：**128 项通过**。
- 通过 `git cat-file blob` 读取 BASE 原始源码，核对 SHA-256 并在内存执行：**630 组指标＋8 组 metric** 与 fixture、候选全部一致。
- 额外 **1,600 组普通输入对照**逐值一致。
- 真实 urllib opener 配合内存模拟响应：**15 种重定向组合全部阻断**。
- 混合 int/float 溢出、RSI、成本相减、metric 下溢、破产吸收和 clipping 探针均符合约定。

限制：相邻 Quant 测试批次因环境缺少 `tzdata`，在加载阶段失败；未安装依赖，也未运行 Python 3.14 或完整工程门禁。本结论不替代发布验收。

全程未修改文件、索引或 Git refs；未连接数据库、调用真实 Provider、读取凭据、启动子代理或执行迁移、commit、merge、push。最终 HEAD/tree 与工作树检查保持一致。

INDEPENDENT_REVIEW_PASSED
