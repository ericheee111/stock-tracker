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
