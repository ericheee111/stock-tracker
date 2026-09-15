# N4 中断续作与独立Review问题闭环

2026-09-14。当前状态：REVIEW_FIXES_IMPLEMENTED / FULL_GATES_AND_REVIEW_PENDING。基线ab0fc66def78be7dd3280211760185dc12a3dd14；继承本地候选bc00597c1351719c87a4c558fa8d3d1f9bad22f5，tree52528592786dac4831750524ebc9f4f1305a85e1。并非重新写一套指标库。

## 继承范围

保留N4a同输入Evidence/Score缺失语义候选、cache-only详情UI/API、固定合成场景CLI，以及N4b声明输入的闭合周聚合与有purge/embargo的研究分区。已有live Evidence/Score、SignalManager、策略权重、风险参数和物理Store原字节保持。不合入product-evidence-stages目录中未审查的physical_market、observed_path、collection_bridge或manual_fills草稿。

## 原审查

`continuation-audit-20260914/independent-bc00597c.md` 的结论为INDEPENDENT_REVIEW_BLOCKED，不能使用其67项受时区环境影响的尝试作为通过数。原始日志不修改。

| Finding | 实际修复 |
|---|---|
| P2 缺失贡献仍显示总分 | 子项缺失必须向分组传播；group missing包含贡献缺项；score引用的family值与实际family保持一致，不重复实现数值公式 |
| P2 缺时区CLI traceback | 只将预期ZoneInfoNotFoundError转HorizonResearchError/TIMEZONE_DATABASE_UNAVAILABLE；CLI原结构化exit2边界接收；无输出文件、不猜时区 |
| P3 报价声明状态丢弃 | 严格枚举和字符串类型、状态/非LIVE警告一致；显示中英文状态及“仅来源声明，未经独立认证”，不改变计算或权限 |

## 本轮先红后绿

在bc00597c实现上先增加6项时区测试与11个真实Chromium场景：时区组1 failure/3 errors，UI39/50通过、11失败。随后定点修复，时区6/6、UI50/50通过。时区覆盖Calendar、Observation、全闭市日历、实际子进程CLI的JSON/退出码/无成功文件及未依赖时区的实验路径；未知程序错误不隐藏。完整Suite、精确新commit以及真正独立复审仍待执行。最终记录将单独落盘，不把工作树结果当成最终commit结果。

## 保护和状态

本任务仅修改隔离的chatgpt/n4-continuation-20260914及新外部证据目录`n4-release-closure-20260914`。保护快照包含其他worktree及生产DB/WAL/SHM的文件哈希；不连接生产SQLite、不apply迁移、不调用真实Provider、自动交易false。每个阶段更新两个Handoff、AGENTS和Overview；最后更新PRD/Gap/指标治理和后续任务。

## 精确实现复审与最后格式修正

修复提交247f79a2315e4a0ce98638d62414c1333f286712取得独立只读Review通过；其500组完整输入/43项Node内存检查为独立结果，时区依赖缺失造成的失败也保留，不冒充全套通过。本机完整Runtime999运行/1skip、Quant753与Chromium50通过。扩展Ruff时发现handlers新增import排序I001；已仅交换相邻两个import的位置，没有改功能。需对最终微小差异补审并重新核对最终树；旧API serializers仍有15项继承lint告警，本轮不得新增。后续阶段设计见`N4-NEXT-STAGES-20260914.md`，不代表已实现B1d或Path/Fill。

## 最终结构复审修正

775b9659的独立复审发现此前未覆盖的响应字段删除路径：删整个ma60_distance、删必需键或删score对missing family的引用仍可能保留数值总分。不是正常Python生成器会输出该矛盾的证明，但违反响应拒绝边界。新增7组真实Chromium负向/正向检查，首先50/57通过、7组失败，再固定policy的完整贡献项/依赖键集、流动性分支及数值/市场状态类型后57/57通过。两个场景遍历所有贡献与必需键，不把循环内部assert虚算成额外测试数量。JS只登记输入图，不复制系数或重新评分；原12个Python场景全部保留通过。最终精确提交复审和门禁仍待完成，247f79a的旧PASS不得套用。

## 2026-09-15 恢复执行：时机分解释闭环

承接75248f61的独立Review，修正非阻断P3时机分缺少乘数与舍入说明。浏览器测试使用实际Python报告生成RISK_OFF/OVERHEATED/ROTATION，不手改总分；新增四例首先57/61通过、四例失败，补说明后通过。另验证乘数只能出现在固定recipe的timing，其余group必须为1，两反例先61/63后63/63通过。仅展示后端已计算的乘数、0–100范围和Python round半偶/整数截断规则；没有在JS复制评分或改变后端公式。原57场景完整保留。新精确commit还要独立Review与完整门禁，暂未发布。

## 最终验收状态

本文件此前IN_PROGRESS/PENDING段落保留为过程记录。最终实现`7b8816bf6b7f3dcf9272b2039c84318554a855d1`已通过完整本机工程门禁与独立只读Review；精确范围、真实计数和Reviewer环境限制见`N4-VALIDATION-20260914.md`，后续阶段见`N4-NEXT-STAGES-20260914.md`。最终远端更新由仓库外发布回执证明，不由本段预先认定。
