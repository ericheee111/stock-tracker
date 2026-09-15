# N4a/b 发布候选：实现验证与独立审查

2026-09-15。状态：INDEPENDENT_CODE_REVIEW_PASSED / ENGINEERING_GATES_PASSED。仅声明输入/合成验证；当前文档提交及远端推送结果另在仓库外DELIVERY.json记录，不自引用文档提交SHA或提前声明push成功。

## 精确身份和续作

已发布基线 `ab0fc66def78be7dd3280211760185dc12a3dd14`。本轮先恢复真实本地候选 `bc00597c1351719c87a4c558fa8d3d1f9bad22f5` 及其三项阻断审查，不重写已有N4a/b、不混入其他Store/Fill草稿。修复提交 `247f79a2315e4a0ce98638d62414c1333f286712` 独立Review通过后，发现并修正API import排序I001，并补下一阶段计划。775b9659复审又发现完整贡献/依赖键集合缺失检查，新增7组反例先红后绿，最终固定policy结构校验。最终实现 `7b8816bf6b7f3dcf9272b2039c84318554a855d1`，tree `b7620919610cd4ef4c4665a51c18c29f76cc19c8`；相对主分支32个文件，精确独立复审通过。

原Evidence、Scoring、指标公式、Signal/Strategy、Risk配置、Portfolio Planning、Physical Store及Sidecar Git blobs均保持基线。不会因新增诊断字段而改变当前排名、概率、持仓或预留。

## 本批交付

| 切片 | 已实现 | 非本轮交付 |
|---|---|---|
| N4a同输入规则对照 | 有界不可变文本副本、实际旧规则重算、显式缺失候选和逐项贡献，12固定合成场景 | 默认评分切换、参数优化、投资有效性 |
| N4a API/UI | 详情cache-only可折叠解释；单次最多260日线读，比较最多80根过去日期；声明状态及缺失传递 | 实时T信号、Global Regime/Sector真实性、历史Signal精确重放 |
| N4b声明周线 | 完整当地周、每个开市日恰一条声明日线、OHLCV聚合、全部依赖known/usable及周界 | 真实日历/复权/PIT认证、未完成周的提前使用 |
| N4b实验输入 | 用途/市场/定义分组，episode去重，严格标签可用时点purge/embargo与完整样本记账 | 训练、校准、真实holdout、候选晋级 |
| CLI/操作文档 | 2MiB严格JSON、新文件输出、结构化错误、可运行合成例 | 数据源采集、SQLite生产连接、自动安装依赖 |

## 三项原独立Review问题闭环

1. 缺失贡献不能与数值总分共存；分组缺失集合覆盖叶子缺失，score依赖的family值必须与实际family一致。JS只校验结构一致性，不复制金融公式。
2. 缺IANA时区数据只将预期ZoneInfoNotFoundError转换为HorizonResearchError/TIMEZONE_DATABASE_UNAVAILABLE，CLI返回结构化exit2；不写成功输出、不猜UTC偏移，也不隐藏一般程序错误。
3. 报价状态必须是LIVE/DELAYED/STALE/UNKNOWN之一，非LIVE警告一致并在页面显示“仅来源声明，未经独立认证”。不会升级为可信数据或交易权限。

先增加6时区测试/11 Chromium场景，在原代码复现失败，再修复为6/6与50/50。原BLOCKED报告及首次失败不删除。扩展lint新发现的I001仅通过import排序修复；serializers既有15项lint保持且无新增，不声明全仓lint清零。

## 2026-09-15 时机分解释收口与保留失败

承接75248f61的独立审查非阻断P3，补全报告中时机分的市场状态乘数、0–100范围与Python round半偶说明；没有在JS复制或更改计算。四个实际Python报告/缺上下文浏览器反例先57/61通过，再补两个非timing group伪乘数反例先61/63通过，修复后63/63通过。原57个检查完整保留，数字没有将循环内部断言重复计为用例。

本次精确实现首次完整Runtime运行999项，其中既有 `tests.test_xtp_sidecar.TestXtpSidecarHttp.test_public_health_private_endpoints_and_get_only_boundary` 在期待HTTP405的POST请求阶段遇到WinError10053，exit1/error1/skip1。原日志 `full-runtime-cab577` 保留；同一源码定点用例复跑exit0，完整Suite再跑的最终结果如下。未改XTP代码、测试断言或超时设置来获取绿色；单次复跑通过不代表已定位或修复该HTTP中止的根因。原HTTP/SQLite资源警告继续保留。

## 最终精确实现上的执行结果

| Gate | 结果 |
|---|---|
| N4/旧Evidence/Scoring/诊断聚焦 | 115通过；exit0 |
| Full Runtime | 998通过，1预期skip；共999；exit0 |
| Full Quant | 753通过；exit0 |
| Outcome/4F/4G | 114通过 |
| 分发/bytecode | 2通过 |
| 固定旧数值基线 | 414/414字段一致，Git源身份核验 |
| 评分对照CLI | 12/12合成场景，无晋级 |
| 周线/实验CLI | 两命令exit0，DECLARED_NOT_AUTHORITY_VERIFIED |
| 新解释Chromium | 63/63，含缺失传递、声明状态、完整字段集合和6个乘数/舍入场景 |
| 原诊断Chromium | 18/18 |
| Today Mock/真实临时API Today | 18/18、18/18 |
| 临时Portfolio CRUD | 13/13 |
| 临时计划API/browser及生命周期 | 27/27、18/18 |
| Targeted Ruff及继承lint差异 | pass，无新增 |
| 新核心及完整Evidence/Planning包类型检查 | 0errors |
| compileall外部缓存/pip check | pass |
| Quant smoke/synthetic benchmark | pass；未晋级 |
| 精确实现树重建 | 733/733文件与Git blob匹配；聚焦/CLI/分发/浏览器再跑通过 |

实现归档SHA256 `fa57bb79d7be46d80d53813fd7b8bae61f42b06bc9d62f91d5d9abd65c45a2c6`。完整stdout/stderr/result和失败attempt见`D:\Projects\stock-tracker-review\n4-release-closure-20260914\commands.jsonl`。每条最终门禁绑定提交及空working diff。首次探索/修改前结果不当作最终提交验证。

未重新执行全站54截图矩阵或人工逐像素/无障碍认证；63场景是实际Chromium、实际Python合成报告、生产renderer与DOM断言，不等同实盘验收。原HTTP/SQLite ResourceWarning保留。没有真实Provider访问、生产迁移、Broker写接口。辅助Review脚本的一次新文件写入被secret-looking防护拒绝，未绕过防护；改为复用既有审查入口，输出落独立精确提交路径。

## 保护和发布

本次仅写已有N4隔离工作树及本任务外置证据目录。其他工作树/原main脏文件以及生产DB/WAL/SHM的前后hash一致；不打开生产SQLite，不初始化或重启生产引擎，不自动启用新策略。

DB `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7`

WAL `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e`

SHM `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3`

独立审查通过实现之后只允许文档提交。最终文档树重新导出、逐字节校验、聚焦/CLI/UI再跑，保护和真实远端祖先核对通过后普通push。发布后回执在DELIVERY.json；不能用本文件本身作为已经推送证明。

## 独立审查原输出

执行者为未参与实现的本地Codex只读上下文，High思考强度。原输出SHA256 `58ea1f071c431b3a869f3156ac097f6e8af5220298f0a10ca0704f9405f36c27`；检查范围和实际失败限制如下。实现者的Python3.14/Chromium/full-suite数据不混算给Reviewer。

## Findings

**未发现本候选新增的 P1/P2 阻断问题，也没有需要登记的 P3 问题。** 本结论不替代完整发布门禁。

核验身份一致：

- Candidate：`7b8816bf6b7f3dcf9272b2039c84318554a855d1`
- Tree：`b7620919610cd4ef4c4665a51c18c29f76cc19c8`
- Base：`ab0fc66def78be7dd3280211760185dc12a3dd14`

## 审查结果

已读取 `AGENTS.md`、指定两份 N4 文档、全部 32 个变更文件，以及关联的旧 Evidence、Scoring、指标公式、缓存读取、时钟、前端调用和测试源码。

- **N4a**：贡献系数、截断及舍入顺序与旧实现一致；RSI 真零保留，缺失依赖向总分传播。不可变文本副本隔离后续输入修改。API 保持单次 read260、详情80、表格30，不借用全局 Regime/Sector。
- **N4b**：完整当地周、日历与日线对应关系、known/usable 时间、周界 availability、cohort 和 episode 去重、标签截止及 embargo 检查符合声明范围。未发现新增 live 策略、训练或可信准入接线。
- **前端**：固定贡献项及依赖键、身份、状态、权限标记、数量和 delta 校验完整；动态字符串转义，时机分乘数与舍入说明已接入。

## 本次实际验证

| 验证 | 结果 |
|---|---|
| `git diff --check` | 通过 |
| 三个 JS/CJS 文件的 `node --check` | 通过 |
| 实际 renderer 的 Node 纯内存探针 | 11 个正向、119 个负向通过 |
| Evidence、Scoring、indicators 与 base 字节比较 | 全部一致 |
| 结束时 HEAD/tree、工作树检查 | 身份不变，未报告修改 |

内存探针使用人工构造的结构样例，**不是 Python 生成结果，也不是 Chromium 验收**。

## 环境限制与未执行项

- `python`、`py` 均无法找到；常见解释器路径探测被沙箱拒绝。因此未运行 Python 单元测试、CLI、Python 语法检查及完整 Runtime/Quant 门禁。
- 未运行会写入截图、报告或临时 SQLite 的 Chromium/HTTP 测试。
- PowerShell 编码设置被受限模式拒绝，随后使用 Node 正确读取 UTF-8；首次 tree 查询因引号问题失败，修正后核验成功。Git 全局 ignore 文件读取出现权限警告。
- 未采用实现方测试计数作为本次验证结果。

全程未修改文件、index 或 Git refs；未连接生产 SQLite、访问 Provider、执行迁移、安装软件、使用子代理或执行 commit/merge/push。下一步应在正常验证环境完成该精确提交的工程门禁。

**INDEPENDENT_REVIEW_PASSED**
