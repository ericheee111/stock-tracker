# N4 续作：评分解释与多周期研究入口

2026-09-14；基线 `ab0fc66def78be7dd3280211760185dc12a3dd14`；状态 IMPLEMENTING_NOT_REVIEWED。使用 `chatgpt/n4-continuation-20260914` 独立树，原两份草稿仅作捕获输入，其逐文件身份在外置 `continuation-audit-20260914/draft-input-manifest.json`。

## 三个发布切片

1. **N4a 核心**：同一份有界、不可变文本副本分别运行现行 Evidence/Scoring 与显式缺失候选，真实 RSI=0 保留、MA60/ROC/MACD 缺样本不代填，未知 DQ/Regime/Sector 不凭空变为有效分。贡献、缺依赖、旧值/候选值/差异明确；未改变旧计算器或默认排序。
2. **N4a 解释界面与命令**：证券详情复用一次缓存日线读取，旧指标及表格样本窗口保持；解释区只读、默认收起，证券/市场/周期/schema/assurance 均需匹配。离线 CLI 仅固定 synthetic 场景，不连生产库或Provider。未知值不补零，不从对照结果生成任何交易许可。
3. **N4b 多周期研究入口**：闭合周聚合只接受完整当地日期日历和每个OPEN日的唯一已收盘、截止时已知日线；区分闭市周与价格零值。研究规格区分波段/长持/短线，参数显式输入，标签结束/已知时间用于切分purge，重复episode不能放大样本。使用声明输入，仅输出清单，不训练/校准/晋级，不将页面缓存提升为PIT数据。

## 已确认画像与保持边界

数周至数月波段为主，期间做T，兼有长持和短线；持仓与机会并重。资金比例、规则权重、RiskGate、SignalManager、真实下单和模型晋级不改。Runtime诊断不是当前Signal历史得分，更不是上涨概率。周线/研究输入仅为DECLARED_NOT_AUTHORITY_VERIFIED，任何哈希都不自证数据来源。

## 未纳入发布的草稿

`chatgpt-product-evidence-stages-20260914` 还包含 native physical_market、observed_path、collection_bridge、manual_fills 等大范围草稿。它们尚无完成的独立审查与完整门禁；本批不整包导入。特别需要检查 observed_path 的 exact-entry 次序、全局未验证coverage、Store读取成本、幂等及未知响应语义。原文件不回滚、不删除。B1d/B2/B3/C/D 的后续状态仍是待完成，不因新文件存在而宣称整个阶段完成。

## 验收与文件更新

先专项与反例，再临时API/Chromium和完整Runtime/Quant；保留失败日志及确切源码身份。最终候选经未参与实现的只读Codex High独立审查，测试执行环境与实现方分开记载。精确commit树重建、分发/字节码/敏感字段检查、保护指纹与远端祖先均通过后方可普通push。无生产DB连接、migration apply、真实Provider抓取或原main清理。每个切片更新AGENTS及两个Handoff，历史状态由当前标记覆盖而不抹除。

## 候选前收口

本次已实际补齐周线calendar+bar获知/可用时间、完整周available_at、证券/市场身份及跨市场cohort边界，并增加可运行离线CLI。前端新增4个矛盾数据反例先红后绿，最终39/39 Chromium通过。新增实际临时SQLite HTTP详情/资源路由2测试通过；总聚焦109通过。旧Evidence与Scoring源码身份单独固定，未改默认信号；完整门禁与精确提交独立Review待执行。首次14项lint问题及各反例失败记录保留，未删除门禁。
