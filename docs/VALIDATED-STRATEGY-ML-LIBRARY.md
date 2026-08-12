# Validated Strategy & ML Library｜股票预测、策略与机器学习验证库

> 文档定位：为 `stock-tracker` 提供“哪些现成算法/框架可以直接复用、哪些只能作为 Challenger、哪些暂时只能研究”的技术决策依据。  
> 核心原则：**模型名字不加分，只看 point-in-time、样本外、扣成本后的真实增益。**  
> 适用市场：A股 / 港股 / 美股  
> 当前产品周期：A股 1—20 个交易日；港股 2—20 个交易日；美股 4—12 周，可延长至 3—6 个月。

---

## 1. 研究结论摘要

### 1.1 可以直接采用的生产级基线

1. **Logistic Regression**：作为最透明、最难过拟合的概率基线。
2. **LightGBM + Alpha158 风格特征**：作为第一版 ML Champion 的首选。
3. **Out-of-time Probability Calibration**：Sigmoid/Platt 为默认；样本足够时才允许 Isotonic。
4. **Market Regime + Sector Context + Relative Strength + Risk Gate**：必须在模型之外保留，不能把全部职责丢给单一预测模型。
5. **Walk-forward + purge/gap/embargo + next executable price**：是验证底座，不是可选优化项。
6. **PBO / Deflated Sharpe / Multiple-testing governance**：用于治理“反复调参直到历史最好”的研究污染。

### 1.2 值得作为 Challenger

1. **Qlib DoubleEnsemble**：样本重加权 + 特征选择，适合金融低信噪比和非平稳数据。
2. **Qlib TRA**：适合多市场状态 / 多交易模式路由，与现有 Regime Engine 很契合。
3. **DoubleAdapt**：用于后期 continual / incremental learning，针对 concept drift。

### 1.3 只能进入 Shadow Research 的模型

1. **HIST**：只有在 point-in-time 行业/概念关系图谱可靠后再研究。
2. **MASTER**：研究价值高，但开源复现版本存在数据处理/验证一致性注意事项，不适合作为首版 Champion。
3. **LSTM / GRU / Transformer 直接预测价格**：复杂度高、增益不稳定，必须证明超越 LightGBM/Logistic 后才允许升级。
4. **FinRL / PPO / SAC / DDPG 等 RL**：只用于研究与影子回测；在撮合、滑点、涨跌停、流动性模拟不够真实时极容易学到“模拟器漏洞”。

---

## 2. 证据等级

| 等级 | 定义 | 产品处理 |
|---|---|---|
| A | 同行评审研究/成熟官方框架 + 可复现实现/benchmark + 与目标问题高度相关 | 可作为生产基线，但仍须本市场重新验证 |
| B | 强研究证据，但市场/周期与本项目不完全一致 | 可加入候选特征/Challenger，必须独立验证 |
| C | 新论文/预印本/复现存在限制/样本较窄 | Shadow Research |
| D | 只靠博客、GitHub 星数、漂亮回测、无 point-in-time/成本验证 | 不进入正式模型 |

任何“现成策略”即使属于 A 级，也只是**方法可复用**，不是未来收益可复制。

---

## 3. 机器学习生产架构建议

```text
Market Data / Events / Fundamentals
                ↓
        Point-in-Time Features
                ↓
    ┌───────────────────────────┐
    │ Rule Candidate Generator  │
    │ Breakout / Pullback / ... │
    └───────────────────────────┘
                ↓
      Feature Snapshot vN
                ↓
    ┌───────────────────────────┐
    │ Logistic Baseline         │
    │ LightGBM Champion         │
    │ DoubleEnsemble Challenger │
    │ TRA Challenger            │
    └───────────────────────────┘
                ↓
      OOT Probability Calibration
                ↓
       P(Target Before Stop)
                ↓
   Risk / RR / Liquidity / Event Gate
                ↓
 WATCH / ARMED / TRIGGERED / BLOCK
```

### 3.1 为什么 LightGBM 先做 Champion

- 能学习 `板块强 + 个股相对强 + 回踩缩量 + 事件确认` 等非线性交互；
- 对中等规模表格型因子数据非常合适；
- 训练和推理成本低，符合本项目近零成本和低延迟要求；
- 解释性、特征重要性、SHAP/Permutation 分析都比复杂深度模型容易治理；
- Qlib 官方 Alpha158 benchmark 中表现稳定，但并非所有数据集永远最好，因此必须保留 Logistic baseline 和 Challenger。

### 3.2 Champion 必须先击败 Logistic

一个复杂模型只有在最新的独立 walk-forward 窗口中同时满足以下条件才有资格升级：

```text
Brier Score        better
LogLoss            better
Precision@K        better or equal
Net Expectancy     better after costs
Max Drawdown       not materially worse
Score Monotonicity preserved
Regime Stability   acceptable
```

如果 LightGBM 不能稳定击败 Logistic，则正式系统使用 Logistic；**复杂度本身不构成优势。**

---

## 4. Alpha158：直接复用“特征思想与实现”，不直接复用标签

Qlib Alpha158 是非常好的现成表格型股票特征基线。建议直接参考/复用其 feature definitions 与实现方式，避免重新手写大量相似因子。

但正式系统不要照搬其默认 next-period return label。

### 4.1 Alpha158 可作为基础层

建议保留/映射以下类型：

- K线形态与位置；
- rolling return；
- rolling volatility；
- volume/amount；
- price-volume correlation；
- moving-average / rolling statistics；
- trend / dispersion / normalized price features。

### 4.2 必须增加项目自己的“第二层独立信息”

Alpha158 主要仍来自价格/成交数据，因此必须新增：

```text
MarketRegime
MarketBreadth
SectorScore
SectorStage
Stock-vs-Market RelativeStrength
Stock-vs-Sector RelativeStrength
IndustryMomentum
LeaderLag
Crowding / Comomentum proxy
Overextension
Intraday-vs-Overnight return decomposition
Event / Earnings / Guidance
Liquidity / Spread
A股 Price-Limit Structure
DataQuality / Freshness
Portfolio Concentration
```

这些信息比再增加第 300 个 RSI 变体更有可能提供独立增益。

---

## 5. 标签设计：Triple Barrier / Target-before-stop

### 5.1 不预测精确收盘价

正式目标不是：

```text
明天涨跌幅 = 1.37%
```

而是：

```text
P(Target before Stop | 当前可知信息)
```

### 5.2 建议标签周期

A股：

- H = 3 / 5 / 10 / 20 个交易日

港股：

- H = 3 / 5 / 10 / 20 个交易日

美股：

- H = 20 / 40 / 60 个交易日
- 后期可增加 120 日

### 5.3 Barrier 必须波动率自适应

```text
entry = next executable price
TP    = entry + k_tp * ATR
SL    = entry - k_sl * ATR
```

记录：

```text
label = TP_FIRST | SL_FIRST | TIMEOUT
MFE
MAE
realized_R
holding_days
```

注意：Triple Barrier 是**标签构造方法，不是经过验证的 alpha 本身**。必须比较不同 `k_tp / k_sl / H` 在参数邻域内是否稳定，而不是寻找历史最优尖峰。

---

## 6. Probability Calibration：必须独立于模型训练

### 6.1 推荐顺序

1. 默认：Sigmoid / Platt calibration；
2. calibration 样本足够多、关系明显非线性时：Isotonic；
3. 不允许使用随机 KFold 打散时间。

### 6.2 产品展示门槛

只有当概率桶具备稳定样本时才能显示：

> “历史同类预测 60%—70% 的信号，实际成功率约 64%。”

否则只展示：

```text
ModelScore
Confidence
```

不展示伪概率。

---

## 7. DoubleEnsemble：Challenger A

### 7.1 值得研究的原因

DoubleEnsemble 针对金融预测的两个核心问题：

- low signal-to-noise；
- non-stationarity。

核心是：

- Sample Reweighting；
- Feature Selection；
- Ensemble。

### 7.2 为什么不直接替代 LightGBM

Qlib 官方公开 benchmark 中，DoubleEnsemble 在部分 CSI300 场景显著强于 LightGBM，但在 CSI500 场景并未稳定保持优势，说明模型有效性对 universe/period/config 很敏感。

因此设计为：

```text
Champion       LightGBM
Challenger A   DoubleEnsemble
```

两者必须使用完全相同的 point-in-time 特征、标签、费用和 walk-forward 切分比较。

---

## 8. TRA：Challenger B

TRA 的核心思想是：

> 市场不存在永远统一的一种 trading pattern。

这与本项目 Market Regime 高度契合。

后期可设计：

```text
Regime / Pattern Router
   ├── Trend Predictor
   ├── Breakout Predictor
   ├── Pullback Predictor
   ├── Reversal Predictor
   └── Event Predictor
```

但第一版不让 Router 完全自由学习，而应保留显式 Regime 特征，方便判断模型究竟学到了什么。

---

## 9. DoubleAdapt：Concept Drift 的后期方案

金融市场的分布会变化，模型不能默认 2018 年的关系在 2026 年仍然同样有效。

DoubleAdapt / continual-learning 思路可以用于：

- 新样本增量更新；
- 数据分布变化；
- 模型快速适配。

但必须满足：

- 有 model registry；
- 老 Champion 不被实时更新直接覆盖；
- 新适配版本先作为 Challenger；
- 必须通过最近独立样本才晋级。

禁止“每天自动重训然后自动覆盖生产模型”。

---

## 10. A股经过研究后最值得加入的策略/特征

### 10.1 日级 Momentum 与中短期 Reversal 分离

中国 A股研究表明，不同周期可以出现不同方向：日级存在 momentum，而更长的周/月窗口并不能简单照搬美股经典 momentum，部分研究中 A股表现出明显 reversal。

因此不要使用单一：

```text
MomentumScore
```

改为：

```text
Momentum_1D
Momentum_3D
Momentum_5D
Reversal_5D
Reversal_10D
Reversal_20D
```

再由 Regime、Turnover、SectorStage 决定有效性。

### 10.2 Turnover-conditioned Momentum/Reversal

研究证据显示短期 momentum/reversal 与 turnover/交易活跃度有关。

建议加入：

```text
TurnoverPercentile
Momentum_x_Turnover
Reversal_x_Turnover
LiquidityShock
```

而不是所有股票使用同一个动量规则。

### 10.3 Sector / Industry Momentum

行业信息经常比单股价格更稳定地扩散。

建议高优先级实现：

```text
SectorReturn_1D/3D/5D/10D
SectorBreadth
SectorVolumeShare
SectorRS
LeaderStrength
FollowerDispersion
```

### 10.4 Leader-Lag / 补涨识别

增加：

```text
LeaderLagScore
```

示例逻辑：

```text
SectorStage == STARTUP/FOMENT
Leader RS very high
Sector breadth expanding
Target stock quality/liquidity pass
Target stock RS not broken
Target stock lags leader within historical normal range
```

这时可以标记“补涨候选”，但必须防止把真正弱股误判为 laggard opportunity。

### 10.5 涨停不是买入信号

Price Limit 结构用于：

- 情绪；
- crowding；
- continuation/reversal risk；
- 可交易性。

建议增加：

```text
LimitUpCount
LimitDownCount
BoardHeight
FailedLimitRate
LimitCloseStrength
DaysSinceLimit
LimitUpTurnover
PostLimitGap
```

任何“涨停 → +20 买入分”规则禁止进入生产。

### 10.6 Crowding / Momentum Crash Detector

强势并不意味着越涨越买。

建议：

```text
ComomentumProxy
CrossSectionalMomentumCorrelation
MomentumCrowding
DistanceMA20_ATR
Return3DPercentile
Return5DPercentile
TurnoverExtreme
```

在：

```text
PANIC_REBOUND
OVERHEATED
SectorStage == CLIMAX
```

提高 Momentum Crash / Chase Risk。

### 10.7 Northbound 降级

北向资金不进入盘中核心 alpha。

用途降级为：

- 盘后确认；
- 季度持仓变化；
- 研究特征。

不得作为实时强触发器。

---

## 11. 港股最值得加入的策略/特征

港股建议优先复用“结构”，不要照搬 A股涨停逻辑：

```text
A/H Relative Strength
Sector Momentum
Turnover-conditioned Momentum/Reversal
Spread / Liquidity Risk
Gap Risk
Earnings / Profit Warning Events
Southbound-related public data
VCM / trading status
```

如果行情本身 DELAYED，模型允许更新 Opportunity，但盘中 Timing 自动降级。

港股需要独立 walk-forward；不能因为某策略在 A股/美股成立就默认港股成立。

---

## 12. 美股最值得加入的策略/特征

### 12.1 Medium-term Momentum

对 4—12 周目标周期，高优先级：

```text
20D RS
60D RS
Stock-vs-Sector RS
Industry Momentum
Trend Quality
Volatility-adjusted Momentum
```

### 12.2 Earnings Momentum + Conditional PEAD

不要：

```text
EPS Beat -> BUY
```

而要：

```text
Earnings Surprise
Guidance Surprise
Gap Direction
Gap / ATR
Volume Surprise
Day-1 Retention
Day-2/3 Retention
Industry Confirmation
Relative Strength
Information Diffusion Proxy
```

最后预测：

```text
P(Event Continuation)
```

### 12.3 Intraday vs Overnight Decomposition

研究显示日内和隔夜收益包含不同的信息结构。

后期可增加：

```text
OvernightReturn
IntradayReturn
OvernightMomentum
IntradayMomentum
GapPersistence
```

但只能作为 B 级候选特征，必须在目标 universe 重新验证。

### 12.4 Volatility Scaling

高波动环境自动降低仓位/提高执行门槛：

```text
PositionRiskMultiplier = f(RealizedVol, ATR, Regime)
```

这属于风险控制，不应与 alpha 分数混成一个指标。

---

## 13. Event / News 模型：结构化而不是“情绪分直接买”

LLM/NLP 只负责：

```text
Event Extraction
Entity Mapping
Event Type
Direction
Materiality
Novelty
Authority
Confirmed/Unconfirmed
PublishedAt
UsableFrom
Numeric Surprise
```

然后将结构化结果交给模型/规则。

禁止：

```text
LLM says bullish -> Opportunity +25
```

### 13.1 Event Continuation 的推荐元特征

```text
EventAuthority
EventMateriality
EventNovelty
EventSurprise
PriceReaction
VolumeReaction
Retention1D
Retention3D
SectorConfirmation
Crowding
GapATR
```

---

## 14. HIST / MASTER：何时才值得做

### HIST

只有当系统已经具备：

- point-in-time 行业成分；
- point-in-time 概念成分；
- 历史公司关系映射；
- 关系图不偷看未来；

才值得研究图模型。

### MASTER

研究价值高，但由于公开复现版本的数据/处理链注意事项，必须：

1. 先独立复现；
2. 核对数据处理；
3. 对照 Logistic/LightGBM；
4. 通过全新 holdout；
5. 只以 Shadow 运行。

否则不允许进入 Champion。

---

## 15. FinRL / RL：暂不进入正式信号

FinRL 非常适合作为研究平台，但 RL 的生产风险比普通监督学习高得多。

主要风险：

- reward hacking；
- fill assumption 不真实；
- 滑点错误；
- 涨跌停/停牌未真实模拟；
- 流动性和冲击成本过于理想；
- Agent 学到模拟器缺陷。

因此：

```text
FinRL / PPO / SAC / TD3
status = SHADOW_RESEARCH
```

只有未来自建高可信 execution simulator 后再重新评估。

---

## 16. Factor Zoo：明确拒绝“网上搜 500 个指标全塞进去”

大规模异常因子复制研究说明，很多文献/网络因子在更严格复制、权重、交易成本或 multiple-testing 条件下显著衰减甚至失效。

因此任何新增 factor 必须经过：

```text
Economic Rationale
Point-in-Time Availability
Incremental IC / Predictive Value
Conditional Value by Regime
Cost-adjusted Expectancy
Stability across windows
Correlation with existing feature families
Multiple-testing record
```

如果一个新指标只是已有 Trend/Momentum 的另一种变形，不允许因为“回测多 2%”就进入生产。

---

## 17. 研究治理：防止把系统调成历史作弊器

必须维护：

```text
strategy_trial
- trial_id
- hypothesis
- feature_set
- parameter_hash
- label_version
- train_range
- validation_range
- holdout_range
- costs_version
- result
- promoted
```

### 17.1 必须计算/记录

- Deflated Sharpe Ratio；
- PBO / CSCV（适合策略搜索阶段）；
- White Reality Check / data-snooping 思路；
- number of trials；
- out-of-sample degradation；
- parameter neighborhood stability。

### 17.2 Frozen Holdout

每次研究周期必须保留真正未参与调参的 frozen holdout。

禁止：

> 看到 holdout 结果不好 → 调参数 → 再看同一个 holdout → 继续称它为 holdout。

一旦使用过，该区间就是 validation，不再是真正 holdout。

---

## 18. 回测工具的建议分工

### Qlib

用途：

- Alpha158 / ML pipeline 参考；
- model benchmark；
- research workflow；
- model comparison。

不要强迫生产实时架构完全依赖 Qlib。

### vectorbt

用途：

- 快速因子/策略扫描；
- 大批量向量化初筛；
- 研究 notebook。

### 自定义 production backtester

正式晋级模型最终必须经过自己的事件准确型执行回测：

- next executable price；
- 涨跌停；
- 停牌；
- gap；
- commission/tax/spread；
- slippage；
- liquidity；
- delisting；
- corporate actions；
- point-in-time universe。

### LEAN

成熟且开源，但本项目当前不自动下单、规模 ≤10 人，整体引入 LEAN 可能过重。未来如果进入多券商执行/复杂订单系统，再评估。

---

## 19. 正式模型晋级制度

### Baseline

```text
Rule-only
Logistic Regression
```

### Champion Candidate

```text
LightGBM
```

### Challengers

```text
DoubleEnsemble
TRA
DoubleAdapt (later)
```

### Shadow Research

```text
HIST
MASTER
LSTM/GRU
Transformer
FinRL/RL
```

### 晋级条件

新模型至少满足：

1. 独立 out-of-time 数据；
2. 完全相同交易成本；
3. 完全相同 label；
4. 完全相同 universe；
5. Brier/LogLoss 改善；
6. Precision@K / Net Expectancy 改善；
7. MaxDD 不显著恶化；
8. 高分桶结果单调；
9. 至少多个 Regime 有效；
10. Shadow 运行新样本通过后才晋级。

---

## 20. 明确禁止进入生产的做法

- 随机 KFold 做金融时序模型验收；
- 今天训练、今天信号使用包含未来修订的数据；
- OpportunityScore/100 当成功概率；
- 用全历史最优参数直接上线；
- 用当前行业成分回填 5 年前历史；
- 忽略退市股票；
- 收盘特征生成后假设能以同一个收盘价成交；
- 只报告最好的一次实验；
- 用未经确认的网络“主力资金”单点触发；
- 把 Transformer / RL / AI 名字当成质量证明；
- 只看胜率，不看赔率、成本、回撤；
- 把港股延迟行情伪装成实时 Timing；
- 把复杂模型无法解释的提升直接视为真正 alpha。

---

# 21. Codex 与 WorkBuddy + hy3 的任务边界

## 21.1 必须优先交给 Codex 的高复杂度任务

以下任务涉及**时间泄漏、统计有效性、模型治理、复杂算法、系统核心架构或性能正确性**，建议由 Codex 主做，WorkBuddy/hy3 不独立决定核心实现。

### C1. Point-in-Time Feature Pipeline【Codex】

包括：

- `known_at / usable_from`；
- 公司行为；
- 历史成分；
- delisting；
- 公告时间；
- feature version；
- leakage audit。

这是整个系统最容易“回测看起来神、实盘失败”的地方。

### C2. Triple Barrier / Target-before-stop Label Engine【Codex】

包括：

- next executable price；
- TP/SL hit ordering；
- gap-through；
- 涨跌停不可成交；
- 停牌；
- timeout；
- MFE/MAE；
- label versioning。

### C3. Walk-forward / Purge / Embargo Evaluation Engine【Codex】

不能简单调用普通 `train_test_split`。

### C4. Alpha158/Qlib 适配与 Leakage Audit【Codex】

不是“pip install qlib”就完成，而要确认：

- 哪些 feature 真正 point-in-time；
- 默认 label 哪些要替换；
- 数据 normalization 是否偷看未来；
- universe 是否正确。

### C5. Logistic + LightGBM + Calibration Champion Pipeline【Codex】

包括：

- time-respecting calibration；
- model registry；
- feature importance；
- probability diagnostics；
- promotion criteria。

### C6. DoubleEnsemble / TRA / DoubleAdapt Challenger【Codex】

涉及复杂非平稳建模和公平 benchmark。

### C7. Production-grade Backtest Execution Simulator【Codex】

尤其 A股：

- 涨跌停；
- 停牌；
- T+1；
- 手续费/印花税；
- gap；
- next executable price；
- slippage/liquidity；
- point-in-time universe。

### C8. Sector Rotation / Leader-Lag 核心算法【Codex】

WorkBuddy 可以照 specification 写组件，但“如何区分弱股与真正补涨候选”的算法由 Codex 设计/复核。

### C9. Model Promotion / Champion-Challenger Governance【Codex】

包括：

- frozen holdout；
- shadow；
- Brier/LogLoss；
- PBO；
- DSR；
- multiple testing；
- version promotion。

### C10. HIST / MASTER / Graph Model 研究【Codex】

必须先审计 relation graph 是否 point-in-time。

### C11. Performance-Critical Incremental Inference【Codex】

当 HOT/WARM 实时引擎需要低延迟增量计算、并发安全和性能优化时由 Codex 处理。

### C12. 所有“模型/策略为什么赢”的归因与反证【Codex】

包括：

- ablation；
- permutation；
- SHAP；
- counterfactual；
- hidden momentum proxy detection；
- factor correlation / redundancy。

模型只有在知道“为什么比 baseline 好”后才允许升级。

---

## 21.2 WorkBuddy + hy3 适合承担的任务

这些任务在接口/公式/验收条件明确后，大多属于确定性工程，可并行交给 WorkBuddy + hy3。

### W1. 已冻结公式的 Feature 实现

例如 Codex 已定义：

```text
RS_5D = stock_return_5d - sector_return_5d
```

WorkBuddy 可以批量实现、写 unit tests。

### W2. 普通 Provider Adapter

对于字段和 API 已明确、无需逆向复杂协议的数据源：

- 请求；
- normalize；
- retry；
- schema mapping；
- fixture tests。

### W3. CRUD / API / Schema

- watchlist；
- positions；
- configs；
- model registry UI API；
- signal history；
- user settings。

### W4. 前端 UI

- Opportunity cards；
- Signal detail；
- Research dashboard；
- calibration chart；
- provider status；
- responsive layout；
- SSE state updates。

### W5. Data Import / Export / Scheduled Jobs

规格明确后可机械执行。

### W6. 日志、Health 页面、运维脚本

- start/stop/status；
- Windows service wrapper；
- structured logs；
- provider health dashboard。

### W7. 自动化测试

由 Codex 定义关键金融 correctness cases 后，WorkBuddy 可以大量补充：

- unit test；
- API test；
- frontend test；
- regression test；
- fixtures。

### W8. 文档与配置

- YAML；
- README；
- operations；
- deployment；
- data-source catalog。

---

## 21.3 强制 Codex Review 的 WorkBuddy 代码

即使实现由 WorkBuddy/hy3 完成，只要触碰以下区域，合并前必须 Codex Review：

```text
labels/
backtest/
features/point_in_time/
models/
calibration/
research/
market_rules/
risk/
sector_rotation/core
signal/scoring
```

理由：这些区域一个很小的时间戳或公式错误就足以让整个历史结果失真。

---

## 22. 建议的实际开发顺序

### Wave 1 — Codex 先定义核心正确性

Codex：

1. point-in-time 数据合同；
2. label engine；
3. walk-forward evaluator；
4. realistic execution backtester；
5. Logistic baseline；
6. LightGBM + calibration；
7. Alpha158 mapping/audit。

WorkBuddy + hy3 并行：

1. 数据表/schema；
2. provider adapters；
3. frontend；
4. SSE；
5. CRUD；
6. logs；
7. test scaffolding。

### Wave 2 — 策略与行业模型

Codex：

- SectorStage；
- LeaderLag；
- Momentum/Reversal conditional model；
- Event Continuation meta-features；
- Crowding/Crash detector。

WorkBuddy：

- 按 specification 批量实现 feature；
- 页面与筛选器；
- tests；
- scheduled scans。

### Wave 3 — Challenger

Codex：

- DoubleEnsemble；
- TRA；
- DoubleAdapt；
- model promotion。

WorkBuddy：

- challenger dashboard；
- experiment UI；
- automated reports。

### Wave 4 — Advanced Research

Codex：

- HIST；
- MASTER；
- graph relations；
- Counterfactual；
- sophisticated attribution。

RL 继续保持 Shadow，不进入 production signal。

---

## 23. 关键研究来源与复现注意事项

### 23.1 框架与机器学习

- **Microsoft Qlib**：官方框架、Alpha158/Alpha360、LightGBM、DoubleEnsemble、TRA 等模型与 benchmark。正式项目可复用其 feature/model 思路，但必须替换成自己的 point-in-time 数据与标签。
- **DoubleEnsemble**：针对低信噪比和非平稳金融数据，通过 sample reweighting 与 feature selection 提升预测稳定性；属于高价值 Challenger。
- **TRA (Temporal Routing Adaptor)**：通过多个 predictor/router 处理不同 trading patterns；与 Regime Engine 具有结构上的互补价值。
- **DoubleAdapt**：面向 concept drift 的 incremental/meta-learning 方案；只在 model registry 和 Challenger 机制完善后考虑。
- **HIST**：利用 predefined + hidden stock concepts 的图关系建模；核心前提是 point-in-time relation graph。
- **MASTER**：market-guided stock transformer；研究价值较高，但其公开复现仓库曾披露 validation/preprocessing 一致性注意事项，因此不得直接作为生产 Champion。
- **FinRL**：官方项目当前明确更适合作为 education/benchmarking/research prototyping；RL 暂时只做 Shadow Research。

### 23.2 资产定价与模型复杂度

- Gu, Kelly & Xiu, *Empirical Asset Pricing via Machine Learning*：树模型/神经网络可以从非线性交互中取得样本外价值，动量、流动性、波动等特征反复重要。
- *The Virtue of Complexity in Return Prediction*：在适当正则化下，高复杂度可能增加样本外预测价值。
- Nagel, *Seemingly Virtuous Complexity in Return Prediction*：某些看似复杂的高维模型可能实质退化为 recency-weighted / volatility-timed momentum；因此必须做模型归因和反证。

### 23.3 A股 Momentum / Reversal / Price Limit

- *Daily Momentum and New Investors in an Emerging Stock Market*：中国 A股在日频存在 momentum，但传统周/月 momentum 不明显；说明周期必须分开建模。
- *Momentum, Reversals, and Investor Clientele*：不同投资者结构对应不同 momentum/reversal 行为，A股可出现月度 reversal。
- *Daily Price Limits and Destructive Market Behavior*：涨停行为可能与短期交易和后续反转相关；支持把涨停/连板主要作为情绪、拥挤和可交易性特征，而不是直接买入信号。
- 2026 年关于中国 limit-up/reversal 的若干新预印本可以放入研究池，但因较新、独立复制不足，不进入正式算法先验。

### 23.4 Industry / Momentum / Crowding

- 行业 lead-lag 研究支持“行业领先公司先反映信息、其他公司后扩散”的特征设计。
- 行业 momentum 研究说明单股 momentum 的相当一部分可以来自行业共同成分。
- *Comomentum* 与 *Momentum Crashes* 支持在 crowding、panic rebound、高波动/反弹环境下对 momentum 降权甚至反向提高风险惩罚。
- *Volatility Managed Portfolios* 支持将高波动环境的风险缩放放在 risk/position 层，而不是混入 alpha 分数。

### 23.5 Earnings / PEAD

- 经典 earnings/price momentum 研究支持 past return 与 earnings surprise 各自包含后续 drift 信息。
- 更新研究显示 PEAD 强弱与 disclosure、信息扩散、套利限制有关，不能简单写成 `EPS beat -> BUY`。
- 因此本项目采用 Conditional Event Continuation：事件 surprise + gap + volume + retention + sector confirmation + RS。

### 23.6 Factor Zoo / Data Snooping

- *Replicating Anomalies*：大量已发表 anomaly 在更严格复制、权重和交易摩擦条件下显著衰减。
- *Taming the Factor Zoo*：大量新 factor 具有冗余和 multiple-testing 问题。
- *Probability of Backtest Overfitting*、White Reality Check、Deflated Sharpe Ratio 等用于研究治理，不作为交易 alpha。

这些研究来源的使用方式是：**约束方法、提供候选和反例；最终生产结论仍由本项目自己的 A/H/US point-in-time walk-forward 数据决定。**

---

## 24. 最终技术原则

> **简单模型先证明 alpha 存在，复杂模型再证明自己带来的是新增信息，而不是更隐蔽地重现 momentum。**

> **任何模型只有经过 point-in-time、真实交易约束、独立 walk-forward、概率校准和新样本 shadow 验证后，才有资格影响“可执行”信号。**

> **模型失败时宁可退回 Logistic/Rule baseline，也不保留一个无法解释、无法复现的高级模型。**

> **研究系统的目标不是找到历史上最漂亮的一条曲线，而是找到在未来不同 Regime 中仍然能维持正净期望的决策规则。**
