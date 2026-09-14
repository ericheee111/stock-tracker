# 数值基础与指标诊断：集成验证和独立审查

2026-09-14。状态：INDEPENDENT_REVIEW_PASSED / SYNTHETIC_ENGINEERING_GATES_PASSED。最终文档提交SHA、GitHub main回执记录于外置 DELIVERY.json；这里不预先声称push成功。

## 集成身份

主分支基线 `1088ac668aed788738b175ea98ef322aeaef5017`。合入已审查数值分支 `7c9545dbb42657ce370663300a027f9dbd7b99a1`。精确两父提交 `97e3694aadf11929f308e732755123ae51c1a0e1`，tree `de4faf283b534110343d4c9258ff3a4a3a3877bd`。两条历史保留，不重写main、不覆盖任何原worktree。

相对main共20文件：三个生产模块（HTTPS/指标/metrics），一个既有TLS测试mock接缝，新增测试/fixture、分发、LF属性和文档。主分支的web、诊断API、序列化、feature_snapshot、indicator_diagnostics、Evidence/scoring/strategies、人工计划、config/sidecars的Git blobs保持。重建清单核验完整文件，不用已有的分支PASS代替集成PASS。

## 明确语义

1. HTTPS远端CA/hostname检查、无自动重定向、失败不降级；研究exact-raw通道独立，loopback Sidecar能力未变。
2. 合法输入仍使用既有指标公式；保留diagnostics调用的valid_number与MAX_PERIOD。冻结list/tuple避免校验后继续消费可变容器。输入不合法/特定溢出下溢返回None；不是任意精度证明。
3. 评价v3=`quant-metric-input-v3-ordered-finite-underflow`，保留main有序且非空iterable约束、拒绝Mapping/Set等、不强转bool/text。成本非负，R倍数与百分比收益分开；空max_drawdown抛错，不回退为分支历史中的0。
4. 子正常数值资金可能隐藏回撤，因此正equity限定normal float域；-1的全损保持吸收状态。RSI均值/比值、指标误差平方和商的不可表示结果不伪装为100或0。真正零值正常保留，无亏损PF数学inf仍是函数级旧约定，不能直接用于JSON/真实战绩。
5. 日线缓存诊断UI保持只读/诊断性质，严格匹配symbol/market/interval/schema；不代表实时T信号、研究级数据或模型有效性。

## 实际工程门禁

| 门禁 | 当前组合提交结果 |
|---|---|
| 组合聚焦 | 125通过；包含两条分支的指标/API/边界测试 |
| Full Runtime | 共922项，921通过/1预期skip；exit0 |
| Full Quant | 753通过；exit0 |
| 4G/4F/Outcome相邻 | 114通过 |
| Source distribution/bytecode | 2通过 |
| 已发布两套冻结基线 | 630指标+8评价；另44指标+3评价、414字段；源Git blob核验，结果无差异 |
| 指标诊断浏览器 | 18/18；含非法身份/未知schema、空/不足输入、零值、转义、360/1440、键盘details |
| Today Mock/临时真实API Today | 18/18、18/18 |
| 临时Portfolio CRUD | 13/13 |
| 人工计划真实临时API/生命周期 | 27/27、18/18 |
| Targeted Ruff/类型检查 | pass；types0errors |
| compileall外部cache/pip | pass |
| Quant smoke/合成benchmark | pass；未晋级 |
| 精确实现树重建 | 708文件逐字节匹配，组合125测试/分发2/旧基线/诊断浏览器18再跑通过 |

实现归档SHA256 `6b42e2972bb627860455963f0e37e907bc814088c930ae7938bf72e3642b7070`。所有结果来自实际command/exit code，完整日志在 `D:\Projects\stock-tracker-review\numerical-integration-20260914\commands.jsonl`。不是实盘/收益/完整无障碍验收；没有重跑旧54全场景截图矩阵。保留既有ResourceWarning，不能因exit0声称警告已修好。

## 保留的失败及修正依据

本次组合聚焦首次125项中两个冻结fixture校验error。确认是Windows checkout引入CRLF，Git中bytes与固定hash完全一致；不是公式变化。只对两份冻结样本加text eol=lf，并用core.autocrlf=true真实checkout-index正向验证。固定hash/expected向量均未改变；不是忽略错误或重生成样本换绿。

基础分支的两轮独立Review发现RSI/回撤/quotient下溢，均先红测后修复，其原日志保留在 numerical-foundation-20260914；本文件只将集成实际执行计为当前结果。

## 工作区和数据保护

基础阶段曾观察到另一个作者在自己的数值诊断worktree独立提交并推送main；基础分支因此只推feature，没有覆盖其main。随后以真实发布1088ac6为新基线单独创建本集成工作树，完整保留两条历史和UI成果。集成前后的原worktree HEAD/未提交文件与production DB/WAL/SHM均一致。没有连接生产SQLite或apply迁移，没有访问真实Provider或启动订单接口。

生产SHA256：
- DB `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7`
- WAL `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e`
- SHM `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3`

## 独立代码审查原输出

Reviewer为未参与实现的本地Codex只读上下文，High思考强度。原报告SHA256 `30b9d529fac7b5c06676d01abae341d78c7ce42b988fa30bb086d597b3ac9a16`。其执行范围及环境限制以下文为准，不将实现方完整测试混算给Reviewer。

未发现本次冻结范围内可确认的 P1/P2/P3 缺陷或阻断项。

已核实：
- commit：`97e3694aadf11929f308e732755123ae51c1a0e1`
- tree：`de4faf283b534110343d4c9258ff3a4a3a3877bd`
- 两个直接父提交分别为指定的 `1088ac6…`、`7c9545d…`，双线历史保留。

已审阅全部 20 个变更文件、相关双亲源码及调用方。main 的诊断 UI/API、序列化、特征快照、公开接口和窗口上限保留；metric v3 保持有序、非空输入，`max_drawdown([])` 拒绝。研究请求与 Sidecar 隔离，解析器、评分、风险配置及生产 schema 无回退。

本次实际验证：
- `python3.12.exe -B -m unittest … -v`：分批 **126 项通过**，覆盖数值策略、既有指标、快照、API、研究请求及源码分发。
- `python3.12.exe -B scripts/run_numerical_baseline.py`：**414 项比较通过**。
- 两套 fixture 与所属父提交原始字节一致，LF 属性及源码 SHA-256 正确；原始 Git 源码在内存重算 **630＋8 组向量及 414 项比较**均通过。
- 对两个父提交额外执行 **3,200 项普通输入比较**，结果一致。
- 真实 urllib opener 配合内存响应：**15 种重定向组合全部阻断**，证书错误正常传播。
- 混合数值溢出、下溢伪零/100、破产吸收、空输入、窗口上限及直接诊断调用探针通过；变更 Python 源码内存编译、`git diff --check` 通过。

限制：诊断测试模块因缺少 `tzdata` 在导入阶段失败；直接诊断函数探针通过。自编基线探针首轮因旧源码没有 policy 常量中断，改为直接逐值比较后通过。未运行 Python 3.14、完整工程门禁或浏览器验收。

全程未修改文件、索引或 refs；未连接数据库、访问真实 Provider 或凭据，未安装软件、启动代理、迁移、commit、merge 或 push。最终 HEAD/tree 与干净工作树保持一致。本结论仅适用于该精确集成提交的独立审查，不替代完整发布验收。

INDEPENDENT_REVIEW_PASSED
