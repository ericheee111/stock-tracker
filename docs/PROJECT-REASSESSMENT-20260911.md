# Stock Tracker 全项目再评估与产品优化提案

> **实现范围补充（2026-09-11）：** 本轮已完成P1–P3的有限手工计划候选，详见 `MULTIHORIZON-P1-P3-IMPLEMENTATION.md`；下文更广的策略、真实执行与数据语义设计并未全部实现。主周期保持用户确认，原风险阈值与信号算法未改。

日期：2026-09-11。状态：**USER_PROFILE_CONFIRMED / DESIGN_PROPOSAL / MANUAL_SEMANTIC_REVIEW_PENDING**。

已确认的画像与派生设计见 `PRODUCT-TRADING-PROFILE-20260911.md`；静态盘点完成不表示全部代码语义已审计。

> 这份文件区分自动盘点、已交付历史证据、设计判断与待用户确认的产品取舍。它不是全仓代码已经通过新一轮独立 Review 的证明，也不声明任何策略最优或真实收益已经改善。原有金融正确性、PIT、手工执行和生产数据保护规则不变。

## 1. 本轮工作边界

本轮目标不是再扩张功能清单，而是使现有能力更容易解释、验证、使用和维护。先回答四个问题：用户今天能否完成决策；数据不足时是否诚实降级；指标是否表达它声称表达的东西；维护成本是否与实际产品价值匹配。

本轮保留现有算法参数、风险上限、执行规则和不可变证据版本。暂不自动切换生产 Provider、初始化生产 Store、应用迁移、启用云公网入口或 Broker 写操作。任何改变候选排序、交易频率、止损/退出方式的优化必须单独版本化并验证。

## 2. 产品目标：先完成一个可重复的日常流程

产品目标仍是个人、A 股优先、本地优先、辅助判断与手工执行。建议将验收单位从“实现一个模块”改为“完成一个用户任务”。

| 任务 | 用户应该看到 | 不应出现 |
|---|---|---|
| 开始看盘 | 市场/数据健康、持仓待处理、少量候选及明确失效条件 | 一屏大量分数但不知道下一步 |
| 判断一个候选 | 策略适用条件、触发价区间、失效价、数据时间、风险与反方证据 | 用一个综合分数代替交易计划 |
| 管理持仓 | 当前持仓事实、退出条件、组合风险、数据不足状态 | 用新机会榜代替持仓退出任务 |
| 记录操作 | 决策时快照、计划、人工执行事实、偏离原因 | 根据页面价格猜测真实成交 |
| 复盘 | 计划与执行分离、可归因结果、缺失证据清单 | synthetic/paper 被标成真实胜率 |

用户已确认：**持仓管理与新机会发现并重**。首页两条主线都必须可见；只把紧急风险和数据故障置顶，不把新机会长期降为次要入口。主画像是几周到几个月波段，期间做 T，兼容长持与短线。

## 3. 信息架构建议

保留已有页面和链接，不立即更换框架或组件库。建议主导航逐步收敛为：今日、观察与机会、持仓、记录与复盘、数据与设置；Monitor/Replay/Research 作为高级入口保留。先用原有静态前端验证导航和信息层级，再决定是否需要结构拆分。

今日页使用公共状态条，加持仓/做 T 与新机会两个并列任务区；移动端两区摘要和入口同时可达。详细指标、来源审计和模型诊断采用展开/侧栏。持仓目的与 T 操作状态分别展示，不用一个买卖徽章覆盖所有周期。

页面必须明确区分“现在可以依据什么制定手工计划”“正在观察什么”“因什么证据不足不能给建议”。不使用同一种警告色表示未加载、缺数据、风险过高和系统故障。

## 4. 指标与算法：重建语义，不先调权重

详见 `METRIC-AND-ALGORITHM-GOVERNANCE.md`。每个对用户可见的指标都必须回答：定义、输入、时间窗口、单位、缺失处理、适用市场、版本、可解释性和验证方式。

评分、概率、风险与置信度是不同对象。一个规则得分可以用于同一策略和同一时间截面的排序，但不能自动变成上涨概率；风险分不能直接解释为亏损概率；数据完整度不能自动解释为预测置信度。

保留现有规则体系作为稳定基线。先建立模块级回放和增量实验，识别高度相关指标重复加分、同源信号重复计算、不同策略不可比排序、历史窗口不足和单位混用，再提出简化候选。

任何新的综合分、动态权重或模型都必须与简单基线比较。复杂度只有在样本外增益、稳定性、解释成本和运行成本上被验证后才应成为默认，不以更长公式或更多特征代表更好。

## 5. 交易与组合风险

建议把硬风险约束与机会排序彻底分开：数据不满足、标的不可交易、仓位输入缺失、止损方向错误、交易单位不合法时，不允许高分覆盖阻断。反过来，证据研究层未达 T3 不应禁止用户查看公开行情或记录手工操作，应只限制依赖该证据的声明和研究行为。

组合层至少需要透明展示单笔计划风险、同标的总暴露、行业/因子集中、同时触发的总风险、现金/费用约束。实际阈值沿用当前配置；未获得用户确认前不代设新的风险百分比。

仓位计算需要版本化的最小交易单位、价格精度、费用、币种和证券类型规则。停牌/涨跌停/流动性问题影响可执行性，不能仅依据理论止损价声称风险一定可控。

## 6. 数据可信度与决策可用性分层

维持六类不混淆的状态：来源可达、数据新鲜、字段/单位有效、可执行性满足、历史可回放、独立准入。不要把它们压缩成一个绿色圆点。

运行数据可以是本地观察事实，但研究级 PIT 需要额外来源、修订、可用时间与范围证据。物理 hash chain 只证明内部一致性，不能代替来源真实性或独立时间锚。此前已完成的 B1物理内核仍保持 `PHYSICAL_BYTES_ONLY`。

默认诊断应帮助用户采取行动：哪个来源失败、哪个市场过期、哪个字段缺失、哪条建议因此被阻断、下一次重试或人工处理入口是什么。调试信息与私密凭据不得直接展示到页面。

## 7. 架构再评估：保持边界，减少重复机制

建议使用清晰的单向依赖：领域值对象/规则 → 应用服务与用例 → I/O适配器与存储；UI/API是应用层入口。Quant research与运行决策可以共享精确定义的纯数学函数，但不能相互隐式导入运行副作用或把研究结果直接晋级到生产。

优先拆分有实际修改冲突、重复解析或测试成本的模块，而不是按文件长度机械拆分。对于大型合同模块，可分为数据类型/codec、确定性推导、验证器、查询视图和工作流；第一步保持公共导入兼容与序列化字节不变。

建立三个明确的信任边界：外部输入验证、存储读取验证、已验证内存对象内部调用。避免为同一可信对象在一条调用链上反复执行完整序列化和全量前缀审计；但缓存必须绑定不可变身份、版本和验证范围，不允许用全局“verified=true”绕过实际读取。

存储层不要进一步增加同一事实的并行权威副本。当前意图+文件提供恢复机制但增加空间/审计成本；是否优化应通过批处理和容量数据决定，不能先删除回放和篡改检查。不要把该通用物理内核宣称为已接通完整行情语义Store。

## 8. 性能优化顺序

先测四类端到端路径：冷启动到可用、一次市场刷新、今日摘要生成、单Case路径回放。分别记录输入规模、p50/p95、内存峰值、文件读取量、SQLite锁持有时间和失败率；不要使用同机器并行测试竞争中的耗时作为基准。

优先优化可证明重复的I/O、JSON/hash、数据库查询和全量重扫。大批数据使用有界流和批次提交；重启游标/发布身份不得因批处理而改变。性能优化必须用同一fixture逐字段对照原结果和hash。

当前每次追加前全量审计的设计适合作为保守正确性基线，不等于高频运行能力。明确规模上限并优雅降级，比无上限接受输入更实用。

## 9. UI/交互优化合同

- 数值：未知显示“—”，真实0保留；概率不存在时不展示伪百分比；价格、百分比、手数、金额和时间使用共享formatter。
- 决策卡：把动作、触发条件、失效条件、持仓风险和数据时间放在同一层；分数默认次要，解释可展开。
- 状态：Loading、Empty、Stale、Blocked、Auth Required、Error 独立；无数据与无机会不能使用相同文案。
- 移动端：360px起避免横向溢出；允许缩放；主要动作可触达；复杂表格保留列优先级/详细视图而不是无限缩小字体。
- 可访问性：颜色之外保留文字和图标语义；键盘焦点、对话框返回、错误关联、动态状态播报需要实际验证。
- 数据呈现：不只展示时间“多久前”，同时显示是行情时间、接收时间还是计算时间；跨市场币种与Session标签可辨。

上述是目标合同。此前W5已修复的null/market/QA问题保持关闭；本轮没有因此声称重新人工检查了全部截图。

## 10. 验证体系与研究方法

工程正确性、产品可用性和投资有效性使用不同证据。单元测试证明定义和边界；故障注入证明恢复；浏览器测试证明状态与交互；真实运行记录证明环境可用；样本外研究只在有效数据和明确假设下支持策略评价。

新增/修改指标需要固定向量、边界向量和参考结果；新增策略需要无前视回放、费用/不可成交处理、基线比较、时序切分及多次试验记录。重叠持有期样本必须在验证方案中处理，不能把随机划分当成默认。

模型概率只有经过对应市场/周期/标签定义下的样本外校准与可靠性检查，才允许以概率呈现。校准、区分能力、经济效用分别报告；阈值由使用场景和错误成本确定，不根据同一测试集反复优化。

测试结果器必须保留方法/子测试/skip/XFAIL/XPASS、初次失败及重跑身份。零执行或全跳过不能PASS。现有W5已通过的工具继续作为可复用资产，不重新发明一套平行结果器。

## 11. 文档收敛

PRD负责产品目标、用户流程、能力边界和验收；领域ADR负责公式/版本/语义；一个当前Handoff负责最新提交、阻断项和下一步；历史Review保留为追溯，不再让十几个旧状态段落同时充当当前规范。

本轮新增提案不删除历史证据。建议后续将现有大文档的历史任务移到明确的归档段落，主入口只保留当前状态表及指向。每个阶段完成时更新同一当前状态，不再层层追加互相竞争的“最新状态”。

## 12. 实施路线：产品闭环与基础设施并重

| 阶段 | 实际交付 | 验收与停止条件 |
|---|---|---|
| P0 再评估闭环 | 当前能力/指标目录、PRD修订提案、代码与UI检查清单 | 静态候选与确认缺陷区分；不自动改交易参数 |
| P1 日常可用性 | 今日任务排序、持仓风险解释、候选失效/数据状态、记录入口 | 使用固定任务完成浏览器验收；未知数据不造数 |
| P2 算法基线 | 共用指标定义、版本化策略解释、固定向量、离线回放对照 | 行为保持或显式版本变化；不把synthetic当真实效果 |
| P3 数据与性能 | B1d语义ReadPort、旧数据工具、有界批处理与容量实测 | 数据身份/恢复不削弱；无生产自动切换 |
| P4 自动证据闭环 | B2 per-case worker、B3桥接、Paper/Manual事实 | 人工候选与真实准入分离；不下单 |
| P5 真实使用验收 | 小范围用户任务/多交易日运行、缺口日志 | 用户确认实用性；真实样本与失败都保留 |
| P6 研究升级 | 4H可信准入、有效样本积累、4I只读Scoreboard | 没有准入/样本时保持不足状态 |

P1/P2可以在不依赖可信收益宣称的范围内先行；无需让所有用户体验改善等待4H。P3/P4仍遵守已冻结证据合同，不因产品提速省略关键身份与时间检查。

## 13. 已确认画像与非阻塞配置

2026-09-11 用户确认：几周到几个月波段为主，期间做 T，也有部分长持和短线；持仓管理与发现新机会都要有。不要再次询问这两项。

具体资金比例、机动数量、复核频率、提醒偏好和模型窗口仍是可配置设计，不是用户已指定值。保留现有三市场、手工执行、风险阈值与策略参数；不移除市场或功能。具体实现详见 `PRODUCT-TRADING-PROFILE-20260911.md`。

## 14. 当前证据强度

本轮新增自动盘点只覆盖Git源码、AST结构、函数/模块规模、候选危险模式和文档标题。它不等于真实调用覆盖率，也不证明候选模式就是bug。完整逐模块语义Review、实际UI视觉/键盘验收、真实行情有效性仍必须明确记录，不应由本提案替代。


## 附录A：当前提交自动盘点

- 基线提交：`c0c39977f4460cc9102fb3731b35cbf88786fc1d`
- Tree：`93871b8f4b22e4f99969239b120f7a33751334de`
- 跟踪文件数：664
- Python源码行数（stock_tracker目录）：78740
- 测试/QA文本行数：51933
- AST语法解析错误数：0

| 模块 | 行数 | 核验性质 |
|---|---:|---|
| `stock_tracker/quant/storage/outcome_collection.py` | 4366 | 结构盘点，不等于缺陷 |
| `stock_tracker/runtime_evidence/path_contracts.py` | 4265 | 结构盘点，不等于缺陷 |
| `stock_tracker/runtime_evidence/source_snapshot_contracts.py` | 4006 | 结构盘点，不等于缺陷 |
| `stock_tracker/storage/repository.py` | 2478 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/reconciliation.py` | 2233 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/storage/outcome_ledger.py` | 2098 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/corporate_action_adapter.py` | 1956 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/security_universe_adapter.py` | 1642 | 结构盘点，不等于缺陷 |
| `stock_tracker/runtime_evidence/contracts.py` | 1579 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/corporate_action_extraction.py` | 1556 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/calendar_adapter.py` | 1527 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/market_bar_acceptance.py` | 1452 | 结构盘点，不等于缺陷 |
| `stock_tracker/market_events/store.py` | 1316 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/market_bar_reconciliation.py` | 1293 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/free_stockdb_governance.py` | 1251 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/data/adjusted_market_data.py` | 1224 | 结构盘点，不等于缺陷 |
| `stock_tracker/quant/core/outcomes.py` | 1176 | 结构盘点，不等于缺陷 |
| `stock_tracker/api/handlers.py` | 1164 | 结构盘点，不等于缺陷 |

### 优先人工审查的复杂函数

| 位置 | 行数 | 分支节点数 |
|---|---:|---:|
| `stock_tracker/quant/core/outcomes.py:378 __post_init__` | 346 | 57 |
| `stock_tracker/runtime_evidence/contracts.py:584 _validate_identity` | 529 | 57 |
| `stock_tracker/quant/storage/outcome_collection.py:2956 _project_case` | 245 | 51 |
| `stock_tracker/runtime_evidence/path_contracts.py:3273 resolve_runtime_path` | 424 | 49 |
| `stock_tracker/quant/data/reconciliation.py:1455 _analyze_security` | 461 | 48 |
| `stock_tracker/quant/storage/outcome_collection.py:1399 __post_init__` | 308 | 47 |
| `stock_tracker/quant/evaluation/decision_quality.py:330 __post_init__` | 163 | 43 |
| `stock_tracker/runtime_evidence/path_contracts.py:2620 from_verified_case_selection` | 358 | 42 |
| `stock_tracker/storage/repository.py:882 _audit_runtime_outbox_structure_connection` | 353 | 42 |
| `stock_tracker/quant/data/corporate_action_reconciliation.py:489 reconcile_corporate_actions` | 258 | 39 |
| `stock_tracker/quant/evaluation/shadow_lifecycle.py:367 __post_init__` | 206 | 39 |
| `stock_tracker/storage/repository.py:1509 persist_signal_decision` | 335 | 38 |
| `stock_tracker/decision/action_mapper.py:59 map_signal_to_action` | 129 | 35 |
| `stock_tracker/quant/core/universe.py:532 snapshot` | 180 | 35 |
| `stock_tracker/quant/data/corporate_action_adapter.py:961 __post_init__` | 174 | 34 |
| `stock_tracker/runtime_evidence/source_snapshot_contracts.py:1494 _scan_prefix` | 362 | 34 |
| `stock_tracker/quant/core/events.py:529 __post_init__` | 163 | 33 |
| `stock_tracker/quant/core/universe.py:331 __post_init__` | 134 | 33 |

### 模式候选（仅供人工定位）

- `scripts/run_stage1_today_integration.py:63`：TIME_CALL_REVIEW；未作bug裁定。
- `scripts/run_stage1_today_integration.py:105`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/api/serializers.py:346`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/api/serializers.py:308`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/api/serializers.py:279`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/eastmoney.py:310`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/eastmoney.py:333`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/provider.py:359`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/provider.py:383`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/provider.py:366`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/provider.py:390`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/router.py:209`：BROAD_EXCEPTION_WITHOUT_ACTION；未作bug裁定。
- `stock_tracker/collector/scheduler.py:263`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/scheduler.py:286`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/sina.py:82`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/tencent.py:79`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/tencent.py:68`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/collector/tencent.py:73`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/core/eventbus.py:42`：BROAD_EXCEPTION_WITHOUT_ACTION；未作bug裁定。
- `stock_tracker/core/store.py:37`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/data_quality/gate.py:50`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/data_quality/gate.py:135`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/data_quality/health.py:44`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/features/feature_snapshot.py:138`：BROAD_EXCEPTION_WITHOUT_ACTION；未作bug裁定。
- `stock_tracker/runtime_evidence/store.py:993`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/store.py:1098`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/store.py:1121`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/worker.py:211`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/worker.py:296`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/worker.py:350`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/runtime_evidence/worker.py:328`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/signals/manager.py:128`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/signals/manager.py:452`：BROAD_EXCEPTION_WITHOUT_ACTION；未作bug裁定。
- `stock_tracker/signals/manager.py:467`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/signals/state_machine.py:55`：TIME_CALL_REVIEW；未作bug裁定。
- `stock_tracker/signals/state_machine.py:75`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/_common.py:55`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/_common.py:21`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/_common.py:41`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/_common.py:42`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_bar_dq.py:41`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_bar_dq.py:64`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_bar_dq.py:20`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_bars_gate.py:21`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_bars_gate.py:38`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_data_quality.py:98`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_data_quality.py:109`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_data_quality.py:44`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_data_quality.py:118`：TIME_CALL_REVIEW；未作bug裁定。
- `tests/test_data_quality.py:119`：TIME_CALL_REVIEW；未作bug裁定。

完整JSON和算法/文档索引保存在仓库外审计目录，包含基线身份和逐文件哈希。无生产数据内容。
