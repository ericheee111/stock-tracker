# 两条数值工作线的受控集成

2026-09-14。状态 INTEGRATION_PENDING_TESTS_AND_INDEPENDENT_REVIEW。

起点main `1088ac668aed788738b175ea98ef322aeaef5017` 已包含指标诊断UI/API、严格有序非空评价输入、period上限和安全类型序列化；数值基础分支 `7c9545dbb42657ce370663300a027f9dbd7b99a1` 已包含更强Runtime HTTPS、冻结源向量及独立审查发现的算术下溢防御。两条线分别通过的事实不冒充合并提交已通过。

## 冲突裁定

- 界面/诊断/序列化/特征快照接线完整保留main字节；本轮不重复写UI。
- 指标使用有限快照计算、保留main公开valid_number与MAX_PERIOD=1000000，不删除已有输入上限；普通合法向量与两套冻结fixture都必须相同。
- 评价保持main的有序、非空输入以及拒绝Mapping/Set等规则；合并有限除法/乘积、RSI及回撤极端值防御。policy升级为quant-metric-input-v3-ordered-finite-underflow。
- 明确采用main的empty max_drawdown抛MetricContractError，不能回到已发布前的诊断0。仅修订尚未合入main的feature测试期待，同时保留main现有非空测试。
- HTTPS采用no-redirect新opener；已发布TLS测试只替换mock注入点，仍验证证书拒绝不重试与CA/hostname开启，不调用外网。
- 分发清单取并集；两套fixture、历史报告和提交祖先均保留。所有历史通过数仅对应各自提交。

## 验收

全量Runtime/Quant、两套数值fixture脚本、边界测试、诊断UI真实浏览器、Today和人工计划、类型/Ruff、精确重建、独立只读Codex High审查通过后，才能普通快进更新main。原main/两个作者worktree及生产DB/WAL/SHM保持不动，禁止force/reset/clean。未启用订单、自动训练或晋级；不是实盘表现证明。

## 后续

本次先结束重复基础实现。下一步从已核验诊断输出做多周期研究对照与评分缺失语义版本实验，而非再次创建第三份指标库；随后B1d typed ReadPort与容量、B2/B3、手工实际成交归因。资金风险参数、真实概率与Trusted Admission不自动放宽。

## 初步执行记录

组合聚焦125项初次两项冻结样本hash失败：新Windows worktree checkout自动将LF转CRLF，Git blob与固定hash一致。未改fixture期望或固定hash，而是对两份样本增加精确eol=lf属性，并实际在core.autocrlf=true的临时checkout-index中验证字节一致。修正后125项通过，Ruff/类型/compile/pip/smoke/benchmark通过，两套基线均保留。后续完整测试、精确树重建与独立审查仍待执行。
