# 数值与请求基础：2026-09-14

状态：IN_PROGRESS。基线 `cdf8c1b4114252ead3bf5eb19d2f624be8929bd8`。延续数周至数月波段、持有期间做T、兼有长持/短线，以及持仓/机会并重的已确认画像。

## 本批三个可独立验收切片

1. N1 Runtime HTTPS：系统CA和hostname验证；校验失败不能降级；标准Runtime远端请求只接受HTTPS，不自动跟随重定向。研究exact-raw通道继续隔离，Sidecar本机HTTP不受此接口改动影响。仅离线/本机测试，不以此声称真实Provider可用或数据达到T2/T3。
2. N2 指标基础：SMA/EMA/MACD/RSI/ATR/ROC/分位数/标准差严格拒绝bool、字符串、数值子类、非有限值、非法窗口及错位OHLC；返回None，不填零。合法普通输入的旧公式、EMA短样本均值、flat RSI=100、MACD histogram=DIF-DEA、ATR算术均值、分位数round-half-even最近观测值约定保持。固定已发布代码的兼容向量，并补手算与边界测试。不是Wilder/TA-Lib等价证明，不是alpha验证。
3. N3 评价基础：概率/收益/成本/初始资金严格数值；费用非负；bins/k为有界或合法整数；fractional return不低于-1；拒绝不可表示的运算结果。无亏损时profit factor的数学inf保留为函数级约定，不允许直接作为JSON/UI真实战绩；max_drawdown空序列保留诊断0，不代表有样本。模型/标签/策略权重/风险百分比不变。

## 草稿处置

`chatgpt-product-reassessment` 原试改只作审查输入，保持其磁盘原状。不导入evidence.py或scoring.py的排名变化。RSI零值被or替代、MA60缺窗口回填、缺DQ当高置信等问题留给下一独立策略版本/诊断任务，不在数值防御中静默修改权重。

## 验证与发布

先红测再修复；保存实际exit code/失败日志。合法输入兼容向量必须一致；完整Runtime/Quant、相邻Outcome、人工计划与Today浏览器回归、类型/Ruff/分发/精确Git树重建均需通过。独立只读本地Codex High审阅精确实现提交，区别其静态审查与本对话实际执行门禁。无真实Provider请求，无生产DB连接/迁移，无自动交易，无force push。原main及所有既有工作树和DB/WAL/SHM只做保护指纹。

共享Handoff在每个切片完成后更新。最终提交/树/远端回执在仓库外 `D:\Projects\stock-tracker-review\numerical-foundation-20260914\`；当前未声称任何测试/审查/推送已通过。

## 后续顺序

N4 版本化多周期指标诊断与评分缺失语义对照（不直接晋级）→ B1d typed ReadPort和容量 → B2/B3 Path/Collection → 手工实际成交与部分成交归因。物理存储/人工计划不等于真实绩效准入，4H仍独立。

## 技术依据

Python 3.14 SSL官方文档 https://docs.python.org/3.14/library/ssl.html ：create_default_context的客户端CA/hostname验证及证书失败处理。数学与数值接口约定以本项目冻结合同和测试为准，不据此引入新的市场交易制度或投资建议。

## 当前执行证据（候选提交前）

N1新增8测试及研究/Provider相邻合计31通过；N2新增10测试及指标/证据/API相邻合计50通过，包含630组已发布合法输入兼容向量和手算例；N3新增13测试及评价模型相邻22通过，8组已发布指标向量一致。首次N1红测、N2/N3红测、一次使用错误测试入口导致_helpers导入失败、两项Ruff规则失败全部保留。独立审查、完整Runtime/Quant和精确树验证未完成。

## 独立Review第一轮与修复

精确候选 `1ca3ce2905f62c011beea31d8c6cdb263a252b05` 的只读Codex High独立审查确认全部630+8来源向量重算一致，并运行100离线测试与额外普通输入对照；发现RSI均值下溢伪100、回撤次正规资金舍入伪0两项算术防御缺口。两项均加反例先失败后修复：RSI正累计均值归零及比值溢出返回None；回撤将初始和正的中间equity明确限制在normal float范围，低于该范围拒绝计算（而非声称任意资金尺度均支持）。合法普通域向量保持，聚焦34测试通过。独立复审待返回。

另一个数值诊断工作树在本任务期间独立推进；原始保护快照保留，单独记录其变更，不声称所有其他工作树完全静止。未写入那个工作树，不自动导入其尚未通过审查代码；发布前仍核对真实远端，不覆盖后来提交。
