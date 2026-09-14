# N4：同输入评分缺失语义对照与解释界面

> **续作说明：** 此为已冻结草稿的原方案，当前范围/修正/审查状态以 `N4-CONTINUATION-20260914.md` 为准。原worktree不回写；存在的草稿测试记录不是当前候选的独立Review。

2026-09-14，状态 IN_PROGRESS。基线 `ab0fc66def78be7dd3280211760185dc12a3dd14`。延续数周至数月波段/持有期间做T、兼有长短持、持仓与机会并重。不改默认评分、排序、风险比例或Signal状态；不是新策略或实盘效果证明。

## 本批三个可验收切片

1. **N4a-1 冻结输入和候选计算**：同一份有界Runtime缓存样本，复制并规范序列化所用Quote/Bar/DQ/Regime/Sector字段，两边分别从相同字节解码；调用现有旧Evidence/Scoring计算器重算基准，候选只改显式缺失语义。输入ID是重现承诺，不是外部来源或PIT证明。不引用可变原对象、不读取账户或策略秘密。
2. **N4a-2 解释与固定反例**：五族与四分数给出旧值/候选值/差异/缺失依赖、逐项贡献与未变的系数。保留旧RSI/ATR/EMA公式，RSI真零不回填，MA60/MACD/ROC不足不补值；缺DQ/Regime/Sector不当作有效中性或高置信，也不对已知项重归一化。候选只有全部必需贡献可计算时才有总分。纯诊断不得产生概率、可执行动作或升级证据。CLI仅运行固定synthetic场景，不读取生产库、不修改默认配置。
3. **N4a-3 详情页只读接线**：复用单次最多260条日线缓存读取，旧指标80条/表格30条不变。新对照使用过去日期的最多80条；同日数据排除并明示。不在API猜测没有市场/成员绑定的Regime/Sector。无上下文导致总分不可计算是正常状态，仍解释已知技术贡献。不增加上游请求、不让诊断失败破坏旧详情。移动端、未知数据、身份/版本、防注入及自动测试真实退出码必须验证。

## 冻结计算语义

- Trend沿用50+MA20距离+MA60距离+均线关系+ATR波动代理；需要60条及有效报价，不足则该族总分null。ATR明确不是ADX，也不宣称趋势独立性。
- Momentum沿用50+0.5*(RSI-50)+三窗口ROC平均项+MACD项；RSI>=15样本，ROC20>=21，完整MACD>=34。任何所需项缺失则族总分null；不把缺hist写“向下”。hist==0沿用旧负项，列为保留的规则偏差，不在缺值修复中偷偷调方向。
- Relative strength的板块项不可用时该族不造50；Volume保留旧换手/成交额代理的显式分支，真正0与None分开；Structure保留同输入原公式及窗口，缺实际依赖不猜。
- Opportunity、Timing、Risk和规则Confidence只在各自所有依赖可得时重算，系数与舍入顺序保持旧实现。Confidence仍是未校准规则分，不是上涨概率或数据可信度。DQ非VALID时candidate confidence不可用；仍可查看数值诊断但无动作许可。
- 同输入旧重算不是当前Signal历史分数：样本过滤、context可能与原决策不同，UI必须明确。源代码指纹测试绑定旧计算器，防止后续源码变更而对照元数据不更新。
- 对aware时间检查不晚于捕获时间；legacy naive保留标签并显式警告，不附加UTC。没有Bar known-at、复权口径和Calendar连续性时固定RUNTIME_DIAGNOSTIC_ONLY，不能用于正式历史as-of推断。

## 验收和保护

先红测再修复；类型/身份/非有限数、顺序/重复日、未来日、恶意子类、深拷贝、突变后结果稳定、缺数据不凑数、真零、固定正向以及候选绝不流入Signal/Portfolio。运行完整Runtime/Quant、相邻Outcome、诊断/Today/计划浏览器、数值基线、types/Ruff/分发/精确Git树；独立只读Codex High审阅精确候选。失败尝试保留，不把两条旧分支PASS当本批PASS。正常快进push前检查远端及所有保护指纹。

## 后续阶段设计边界

N4b先冻结按持仓目的/周期/标签/费用/数据身份划分的对照协议；复用Quant既有walk-forward/holdout，不造第二套模型平台。周线聚合和真正策略比较需Calendar、时区、复权及known-at条件，不把现有日线缓存重命名成研究样本。B1d继续实际Event/Transport Typed ReadPort，然后B2/B3 Path/Collection、手工实际fill/partial fill、跨交易日使用验收。本批不声称这些全部已实现。

## 技术依据

Python 3.14官方文档：`json` 的allow_nan=False及输入长度限制（https://docs.python.org/3.14/library/json.html）；`dataclasses` 的frozen只模拟只读（https://docs.python.org/3.14/library/dataclasses.html）；aware/naive区别（https://docs.python.org/3.14/library/datetime.html）。标准库特性不构成金融数据Authority。
