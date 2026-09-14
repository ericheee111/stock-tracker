# N4之后的产品与证据阶段

2026-09-14。状态：DESIGN_PLANNED_NOT_IMPLEMENTED。N4a只读对照及N4b声明输入工具的本批验证状态见`N4-REVIEW-CLOSURE-20260914.md`，最终提交/门禁见最终验证文档；本文件不预先赋予任何阶段通过资格。

## 保持的产品方向

用户主要进行数周至数月的波段，持有期间做T，同时有长持与短线。持仓管理与新机会并重；T是父持仓上的操作层，不制造独立库存、现金或重复样本。资金比例、风控参数和交易权限不代设。现有候选解释不参与live排名，不把规则置信分改名为上涨概率。

## 本轮完成与未完成的分界

N4a提供同一有界副本上的旧规则重算与显式缺失候选。它能够解释RSI真零、MA60预热不足、MACD未知、缺DQ/市场/板块上下文，但未接替线上Evidence/Score。N4b提供严格日历/日线声明输入的完整周聚合，以及按用途/市场分组、按标签可用时间剔除重叠样本的实验清单；未训练、未校准、未消费真实holdout、未证明日历来源或已实现波段alpha。不得再以相同N4名称创建一套重复指标基础。

## 1. B1d-1：实际字节到类型化只读接口

### 设计

复用已实现的`runtime_evidence/physical_store.py`，禁止再建立平行的通用Store。冻结一个物理快照及其store_id/high-water，所有Event、Transport、源Session及订阅元数据都从该快照内实际bytes解码。明确物理append order与各语义流序号，不用一个自报整数替代另一个。Record payload必须按精确schema/parser版本重建，不从UI或独立dict另填价格、来源或已知时间。

物理完整性和语义Coverage分层报告：PHYSICAL_BYTES_ONLY不自动成为Coverage，更不能晋级PIT_CANDIDATE或TRUSTED_ADMITTED。原B0/B1纯合同可作为对照算法，不能把tuple/hash工厂标成真实Store receipt。在未验证语义前，先交付codec、共用高水位读取与可复算失败结果，不切换现有生产采集。

### 出口条件

从临时物理Store真实append并读回；错误source_store_id/schema、内容篡改、未提交意图、跨快照混合、晚到Transport证明、部分页遗漏、读后再追加等负例均不能生成更早窗口的完整Coverage。一次冻结读不能混用两个修订。记录读取量、字节与峰值内存，分页必须有上限。独立审查精确提交后方可进入有限适配，不把进程中断测试称为整机断电认证。

## 2. B1d-2：Legacy检查、导出与容量

只允许显式inspect/export，默认dry-run。不把非空旧事实静默变更schema，不修改旧来源known_at，不自动迁移生产数据。导出包绑定原schema、源文件/manifest和输出数量；必须区分可解析、不支持、损坏，不能跳过坏记录后声称完整。输出新路径、拒绝覆盖，失败残留不能当成功数据。

批量append和audit先测后优化，给出固定synthetic规模、硬件/运行器、耗时、吞吐、内存及操作计数。优先减少重复读取与重复解析，不能靠删完整性校验、缩小可见失败或信任调用方verified标志提速。大规模读取通过SQLite游标/有界块，不物化全历史tuple。规模门槛来自测量，不宣称已支持真实Level2流量。

## 3. B2：按Case的Path Store与Worker

每个Case固定父计划、entry occurrence、source identity和查询截止。先回填Entry后至当前高水位，再tail新增记录，不能因为全局cursor已经推进而漏掉新Case历史。游标、投影结果与交付状态形成明确持久化边界，至少一次执行不重复事实；未成功审计投影不能推进Case进度。

输入到投影必须反向完整：每个eligible source member恰好被消费一次，早STOP不能被后TARGET覆盖；Entry同时间戳、Bar跨Entry、午休/HALT、双障碍同Bar、断线、缺数、source-clock异常保留失败关闭语义。对停牌、无成交、市场休市和数据缺失分别表达。Worker错误分row永久错误与全局Store故障，不用全局故障quarantine合法行。

出口：真实临时Store/Worker回放、不同Case并发、重复delivery、重启、lease超时、未完成publication与游标窗口全部验证；不把synthetic Path当成真实交易收益。

## 4. B3：Collection桥接

只接受与Runtime decision、Path选择、执行事实匹配的版本化输入。Terminal原因由冻结路径决定，不可仅传TARGET/STOP字符串。调用完成但响应丢失时重放同一命令/事实身份；重复终态不得增加样本。输出保留来源和未完成原因，Paper/Manual/Provider来源不混入同一可信总体。先接只读观察和显式人工入口，自动服务启用另经运行验收。

## 5. C：手工实际成交与归因

复用已发布人工计划/资源概念，不另立无限资金池。订单意图、资源预留、实际fill、部分成交、撤销未成交量、外部占用分别记录。人工录入是MANUALLY_REPORTED，不冒充Broker确认；同成交重复提交幂等，修改通过追加纠错/撤销事实而非删除历史。

共享同币种现金和同证券可卖量；核心仓保护、零碎股、费用、T未配对腿与卖出后未买回必须可表达。相对持有归因绑定统一估值时点、价格口径、公司行为和外部现金流；基线不足时结果为未知，不能用配对差价代替增量收益。当前“已开始操作”标记不直接转换为fill。

## 6. N4c与D：候选评价、实际使用和可信准入

N4c复用已存在Quant训练/校准/验证平台与N4b清单，固定可比cohort和版本，一次明确假设对应一份对照；先展示缺失率/输入覆盖/排名差异，不通过把未知样本删除来得到更高收益。未来引入真实数据时先证明价格口径、日历、Universe和可用时间，不把示例周期当用户默认。

D要求真实多交易日运行日志、断线/恢复、库存确认过期、界面操作和反馈。合成单元或浏览器测试不替代Shadow验收。4H仍独立负责可信准入、撤销和外部时间/权限证据，4I只消费合法admitted样本；样本不足保持不可用。

## 执行与交接

默认ChatGPT直接设计、实现、自审，并使用未参与实现的只读上下文做独立Review。复杂持久化/PIT/并发由本地Codex辅助时使用最高可用思考档（通常xhigh）；普通接线和独立Review用High，机械打包用Medium。WorkBuddy按需处理固定fixture、界面矩阵、引用索引，不维持多会话并行写同一区域，不为了跑量重复测试。

每个切片维护AGENTS、CHATGPT_HANDOFF、docs/HANDOFF和Overview；阶段变化同步PRD/Gap Matrix。保留首次失败和真实退出码；最终精确commit/tree重建、独立Review、生产文件保护与远端祖先关系通过后普通push。保护已有worktree，不force/reset/clean，不混入其它未审查草稿。
