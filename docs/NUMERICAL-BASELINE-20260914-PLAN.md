# N1–N3 数值基线、输入防御与波段窗口展示

日期：2026-09-14。基线：`cdf8c1b4114252ead3bf5eb19d2f624be8929bd8`。状态：IMPLEMENTATION_IN_PROGRESS。用户主画像：数周到数月波段、期间做T，兼有长持和短线；持仓和新机会同为主线。

## 切片及出口

1. **N1 输入/传输防御**：Runtime HTTPS恢复系统CA与hostname验证；证书失败不降级。Quant概率/标签/成本/收益/epsilon/k/bins显式类型、有限性及定义域校验；有限输入溢出不能变成貌似有效指标。默认利润因子全盈利的历史infinity sentinel保留为计算合同，不允许把它当JSON真实绩效。最大回撤输入明确为简单收益率，不是R倍数，禁止<-100%的非融资权益路径。费用不允许负数冒充返利。冻结有效样本回归。
2. **N2 指标基线**：保留既有SMA/EMA/rolling RSI/rolling ATR/MACD公式与精确normal-domain输出。非法类型、bool、非有限值、错位OHLC、非法周期和数值溢出返回未知，不伪造0。不把rolling RSI/ATR改叫Wilder，不调整策略权重。提供离线、合成、固定输入的兼容性报告与向量；旧草稿不整包移植。
3. **N3 产品展示**：详细页增加有界日线窗口核对（20/60/120/252根）及各指标warm-up/方法说明。只读已有本地bars，无Provider请求；不是已验证周线策略、连续交易日历、PIT或T信号。输入身份/顺序/数据不可信时明确缺口；原短周期/Opportunity/风险参数保持。历史收盘排名不再把不足252根标为“52周”。

## 不在本轮

不修改 `signals/scoring.py`、策略权重、风险比例、Evidence默认算法；缺失DQ与旧Evidence猜测MA60/RSI0等问题保留为下一阶段已定位候选，先版本化离线比较再采纳，不以合成收益证明效果。不会运行真实行情、券商写入、模型训练晋级、生产DB迁移或启用T自动下单。B1d/B2/B3和手工fill另阶段执行。

## 验收

先写反例再修复；固定向量与有效样本对照；TLS只用mock无真实请求；完整Runtime/Quant与相邻门禁、数值JSON严格序列化、浏览器真假状态/输入逃逸/缺样本对照、W5/人工计划回归、独立只读Review、精确候选重建。所有错误尝试保留；未通过不得push。

指标默认有效域结果不得借防御性修复偷偷换公式；确需改变的边界须逐项在验证表说明。确定性计算不等于真实策略有效。证据外置：`D:\Projects\stock-tracker-review\numerical-baseline-20260914\`。

## 技术依据

Python 3.14 ssl 官方文档：`https://docs.python.org/3.14/library/ssl.html`（2026-09-14查阅）；`create_default_context`使用默认CA与客户端校验，hostname与链必须一起检查。只改变本轮声明的Runtime HTTPS上下文，研究通道仍维持独立严格接口。Indicator和metrics公式定义以当前基线源码/固定向量为准，不宣称与第三方库等价。
