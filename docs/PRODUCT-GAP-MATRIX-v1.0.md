# Stock Tracker v1.0 产品 Gap Matrix 与 Stage 1 实施入口

> 审计日期：2026-08-14
> 工作区：`D:\Projects\stock-tracker`
> 分支：`main`
> 产品基准：`docs/PRD-股票辅助判断与交易参考网站.md` v1.0
> Agent 规则：根目录 `AGENTS.md`
> 状态：Stage 0 完成；Stage 1 核心决策链已实现并通过真实 API/Web 集成，Portfolio 编辑 UI 尚待完成

---

## 0.1 2026-08-14 Stage 1 集成更新

本节覆盖下文基线审计中关于 Stage 1 “未实现/尚未开始”的旧状态。下文仍保留，用于说明实现前的差距和设计依据。

| 能力 | 当前状态 | 当前证据 | 剩余缺口 |
|---|---|---|---|
| 产品层严格合同 | `IMPLEMENTED` | `stock_tracker/decision/types.py`，直接构造绕过测试 | 继续随新动作扩展 |
| Signal → Action 映射 | `IMPLEMENTED` | LIVE 才可执行；STALE/UNKNOWN → DATA_BLOCKED；持仓独立语义 | 更高级 Exit 留在后续 Stage |
| PositionSizer | `IMPLEMENTED` | 风险、现金、单股、Heat、板块/主题、lot size 共同约束 | 流动性冲击模型仍为后续能力 |
| TradePlan | `IMPLEMENTED` | 主方案、软阻断激进方案、硬阻断失败关闭 | 事件与 Big Trend 尚未接入 |
| Core 3—5 与 DecisionBrief | `IMPLEMENTED` | 动作优先、去重、板块配额、确定性摘要 | 真实校准概率继续为 `null` |
| Portfolio Profile / Position CRUD | `IMPLEMENTED` | 临时 SQLite、严格 REST、奇数股持仓事实、私有 API 保护 | 前端编辑界面尚未实现 |
| `GET /api/brief/today` | `IMPLEMENTED` | 真实 Python API + 真实 Web Playwright 集成通过 | 需要持续用真实运行数据观察 |
| Today Action 首页 | `IMPLEMENTED` | 动作优先、概率/战绩诚实降级、桌面/移动 QA | Portfolio 编辑入口尚未实现 |
| 最低安全 Exit baseline | `IMPLEMENTED` | 只有 LIVE 价格跌破失效位才可 EXIT；数据异常不伪造卖出 | 部分止盈、Trend Runner、事件 Exit 未实现 |
| Big Trend | `NOT_IMPLEMENTED` | 明确返回 `NOT_AVAILABLE` | Stage 3 |
| Event Intelligence | `NOT_IMPLEMENTED` | 仍仅有旧事件占位 | Stage 3 |
| Strategy Scoreboard | `CONTRACT_ONLY` | 明确返回 `INSUFFICIENT_REAL_EVIDENCE` | Stage 4 |
| Replay | `CONTRACT_ONLY` | PIT 基础存在 | Stage 4 |
| 真实校准概率 | `BLOCKED` | 继续严格为 `null` | T3/T4 数据、校准与 Shadow 证据 |

私有数据安全：`/api/brief/today` 与 `/api/portfolio*` 本机直连可用；公网部署未配置私有访问时失败关闭。反向代理不能通过本机 TCP 来源绕过认证。

当前下一产品切片是：**完成 Portfolio 设置/持仓编辑 UI，并用真实 REST API 做端到端 CRUD 验收**。在此之前不进入 Big Trend 或真实概率展示。

---

## 1. 审计目的

本文件回答四个问题：

1. PRD v1.0 要求的产品能力，当前仓库究竟实现到什么程度；
2. 哪些只是工程合同或 synthetic fixture，不能当成真实策略能力；
3. Stage 1 Today Action MVP 应该按什么顺序实现；
4. 哪些模块必须继续保持失败关闭，不能为了快速展示而伪造。

本审计以实际源码、配置、测试和数据库 schema 为准，不以历史 Handoff 中的旧测试数字或“已完成”描述代替当前证据。

---

## 2. 状态词典

| 状态 | 含义 |
|---|---|
| `IMPLEMENTED` | 当前运行代码已接线，并有至少基础测试或真实运行证据 |
| `PARTIAL` | 有部分数据结构、API、UI 或算法，但未达到 v1.0 完整合同 |
| `CONTRACT_ONLY` | 正确性合同、接口或基础类存在，但尚未接入真实产品链路 |
| `SYNTHETIC_VALIDATED` | 合成 fixture 已证明工程行为，不代表真实投资表现 |
| `REAL_DATA_RESEARCH` | 使用真实、用途合格的数据做离线研究，但尚未影响生产信号 |
| `SHADOW` | 新能力使用真实新样本影子运行，不影响主信号 |
| `PRODUCTION_APPROVED` | 已达到数据、模型、校准、风险和发布门槛，可影响正式产品 |
| `NOT_IMPLEMENTED` | 当前仓库没有对应能力或只有无关占位 |
| `BLOCKED` | 缺少必要数据、身份、测试或安全前置，不能安全继续 |

重要规则：

- `CONTRACT_ONLY` 不能写成“功能已上线”；
- `SYNTHETIC_VALIDATED` 不能写成“真实胜率有效”；
- `PARTIAL` 不得通过 UI 文案伪装为完整能力；
- 概率必须达到相应证据门槛，否则继续为 `null`。

---

## 3. 当前总体成熟度

### 3.1 已具备的运行底座

当前运行产品已经具备：

- 标准库 Python 后端；
- 静态 Web 前端；
- Provider / Router / Provider Health；
- HOT / WARM / COLD 调度；
- Quote 和日线 Bar；
- MarketStore 与 SQLite Repository；
- 数据质量和新鲜度状态；
- 基础 Market Regime；
- 基础 Sector Engine；
- S1/S2/S3 候选策略；
- Opportunity / Timing / Risk / Confidence 四分数；
- Risk Gate；
- 信号状态机；
- Next Trigger、What Changed、拥挤度展示；
- REST、SSE 和基础交易驾驶舱。

### 3.2 已具备的 Quant Foundation

独立 `stock_tracker.quant` 已具备：

- Point-in-Time Fact / Snapshot；
- 稳定 fingerprint；
- RawDataArtifact 与 Manifest；
- 交易日历和证券状态合同；
- Market Rule / Cost Schedule；
- next executable price；
- A 股 T+1、停牌和涨跌停执行合同；
- Triple Barrier / Target-before-stop；
- Purged walk-forward；
- Logistic baseline；
- 可选 LightGBM 候选接口；
- Platt / Isotonic 校准；
- Frozen Holdout；
- Model Registry、Experiment Ledger 和晋级门。

这些能力当前主要属于：

```text
CONTRACT_ONLY + SYNTHETIC_VALIDATED
```

它们不代表已经获得真实投资表现。

### 3.3 当前产品核心缺口

v1.0 最重要但尚未完整实现的能力是：

1. 今日作战简报；
2. 账户净值和现金配置；
3. 完整仓位与建议股数；
4. 统一动作合同；
5. 完整 Exit Engine；
6. Big Trend Radar；
7. 官方 Event Intelligence；
8. Strategy Scoreboard；
9. Point-in-Time Replay；
10. T3 A 股真实研究数据；
11. 真实校准成功概率；
12. 港股通 Universe 和独立策略验证。

---

# 4. 产品能力 Gap Matrix

## 4.1 首页与决策输出

| v1.0 能力 | 当前状态 | 当前证据 | 主要缺口 | 建议阶段 |
|---|---|---|---|---|
| 今日作战简报 | `PARTIAL` | `/api/overview` 已返回 regime、portfolio heat、top opportunities、holding signals、breadth、risk events、markets | 缺统一 `DecisionBrief`、市场姿态、结构化 action、今日不要做、Big Trend、证据状态和 AI 摘要输入合同 | Stage 1 |
| AI 交易参谋摘要 | `NOT_IMPLEMENTED` | 当前后端未见基于结构化决策的摘要对象 | 需要先有确定性 Brief；LLM 只能解释，不可自行决策 | Stage 1 后半 |
| 首页动作优先 | `PARTIAL` | 当前已有 signal state、next trigger、why-not-buy 部分字段 | 首页仍主要围绕机会分、指标和旧状态；没有 v1.0 动作合同 | Stage 1 |
| 首页 Core 3—5 个 | `PARTIAL` | `_top_opportunities()` 支持 limit，按 symbol 去重 | 默认仍为 12；只按 Opportunity 排序；无多样性、Expected R、安全概率模式和状态分组 | Stage 1 |
| 今日不要做 | `PARTIAL` | negative reasons、Risk Gate、crowding 已存在 | 未汇总为市场级/账户级 avoid list；没有 hard/soft blocker 分类 | Stage 1 |
| 数据与模型证据状态 | `PARTIAL` | data_status、observed_age、success_probability=None 已存在 | 缺 evidence level、data trust tier、model status 和可见的“真实证据不足”合同 | Stage 1 |

### 关键源码证据

```text
stock_tracker/api/handlers.py
- get_overview()
- _top_opportunities()
- get_positions()
- get_signal()

stock_tracker/signals/scoring.py
- success_probability=None

stock_tracker/signals/crowding.py
- 当前为展示型拥挤度，不进入核心评分
```

---

## 4.2 Core Opportunity Radar

| 能力 | 当前状态 | 当前实现 | 缺口 |
|---|---|---|---|
| 多策略候选 | `IMPLEMENTED` | S1/S2/S3 和 SignalManager 已接线 | S4 龙头/中军、二次启动尚未正式实现 |
| 同股票去重 | `IMPLEMENTED` | `_top_opportunities()` 按 symbol 选择最高 Opportunity | 尚未合并多策略证据和辅策略身份 |
| 安全概率空值 | `IMPLEMENTED` | `ScoreSet.success_probability` 默认 `None`，测试覆盖 | 尚无 `model_tendency`、evidence level 和概率显示策略对象 |
| Core 排序 | `PARTIAL` | 当前按 Opportunity 降序 | 缺 Expected R、状态、Regime、流动性、Confidence、拥挤和组合约束 |
| Top-K 多样性 | `NOT_IMPLEMENTED` | 无板块/主题配额 | 需要确定性去重和多样性算法 |
| 3—5 个首页结果 | `PARTIAL` | 函数有 limit 参数 | 默认 API 仍返回 12；无用户配置 |
| 完整正反证据 | `PARTIAL` | positive/negative reasons、reason、next_trigger | 缺 hard_blockers、soft_blockers、证据 ID 和版本 |
| Core Outcome | `NOT_IMPLEMENTED` | 有 signal history，但不是交易结果 | 缺 entry/exit、MFE/MAE、realized R 和成本结果 |

---

## 4.3 交易计划与“为什么不能买”

| 能力 | 当前状态 | 当前实现 | 缺口 |
|---|---|---|---|
| 入场区间 | `IMPLEMENTED` | Signal 有 `entry_low/entry_high` | 需要 v1.0 TradePlan identity 和有效期 |
| 触发价 | `IMPLEMENTED` | `trigger_price`、`next_trigger` | 缺结构化 trigger 条件集合 |
| 失效位 | `IMPLEMENTED` | `invalidation_price` | 缺持仓后的移动保护和失效类型 |
| 目标 1/2 | `IMPLEMENTED` | Signal 已持久化 | 缺部分止盈和 Trend Runner 合同 |
| Reward/Risk | `IMPLEMENTED` | `reward_risk` 已有 | 缺费用后 Expected R 和与仓位的绑定 |
| 不追价 | `PARTIAL` | Overextension / crowding 可阻断或展示 | 缺明确 `no_chase_above` 字段和取消条件 |
| 为什么不能买 | `PARTIAL` | signal detail 将 negative reasons 作为 `why_not_buy` | 缺 blocker 类型、严重度、可恢复性和下一解锁条件 |
| BALANCED 主方案 | `NOT_IMPLEMENTED` | Risk Gate 只有 allowed / block reason | 缺结构化主方案 |
| 激进方案 | `NOT_IMPLEMENTED` | 无独立风险折扣方案 | 必须只允许软阻断，且风险预算更低 |
| 建议股数 | `NOT_IMPLEMENTED` | 当前无账户净值/现金 | 依赖 UserPortfolioProfile 与 PositionSizer |

---

## 4.4 持仓与组合风险

| 能力 | 当前状态 | 当前实现 | 缺口 |
|---|---|---|---|
| 持仓股票 | `IMPLEMENTED` | `Position.symbol/market`，SQLite `positions` | 缺正式 CRUD API |
| 持仓股数 | `IMPLEMENTED` | `Position.shares` | 缺严格范围和市场交易单位验证 |
| 成本价 | `IMPLEMENTED` | `Position.cost`，API 计算 PnL | 缺字段命名迁移和用户编辑界面 |
| 当前盈亏 | `IMPLEMENTED` | `get_positions()` 计算 pnl / pnl_pct | 无费用、汇率和已实现 R |
| 账户净值 | `NOT_IMPLEMENTED` | 无 schema、dataclass 或 API | Stage 1 前置 |
| 可用现金 | `NOT_IMPLEMENTED` | 无 schema、dataclass 或 API | Stage 1 前置 |
| 单笔风险预算 | `PARTIAL` | `risk.toml` 有全局阈值概念 | 无用户 profile 和具体金额计算 |
| Portfolio Heat | `PARTIAL` | overview 调用 `SignalManager._portfolio_heat()` | 需审计公式，绑定账户净值、持仓和真实失效位；当前不足以形成完整组合决策 |
| 单股风险贡献 | `NOT_IMPLEMENTED` | 无标准输出 | 依赖账户和失效位 |
| 板块/主题集中 | `NOT_IMPLEMENTED` | 无组合聚合输出 | 当前 Sector identity 也不完整 |
| 建议仓位百分比 | `NOT_IMPLEMENTED` | 无 PositionSizer | Stage 1 |
| 建议股数 | `NOT_IMPLEMENTED` | 无 PositionSizer | Stage 1 |
| 多币种/汇率 | `NOT_IMPLEMENTED` | 当前产品优先 A 股，可延后 | Stage 6 |

### 当前 Position 合同

```text
id
symbol
market
shares
cost
added_at
closed_at
```

v1.0 需要新增独立账户 Profile，而不是把账户字段塞进每条 Position。

---

## 4.5 Exit Engine

| 能力 | 当前状态 | 证据 | 缺口 |
|---|---|---|---|
| `TRIM/EXIT` 枚举与迁移 | `PARTIAL` | `SignalState` 和 `VALID_TRANSITIONS` 已包含 | 只表示状态能力，不等于有退出算法 |
| 入场后维持 ACTIVE | `PARTIAL` | 状态机支持 ACTIVE | 当前 `decide()` 主要处理 DQ、overextension、entry trigger 和 armed expiration |
| 失效位触发 EXIT | `NOT_IMPLEMENTED` | 未见 `last <= invalidation` 的 ACTIVE 退出分支 | Stage 1 必须补最低安全退出 |
| WARNING | `NOT_IMPLEMENTED` | 旧状态机无独立 WARNING | 需 v1.0 ActionState 或兼容映射 |
| 部分止盈 | `NOT_IMPLEMENTED` | 无状态、比例或持久化 | Stage 2/3 |
| Trend Runner | `NOT_IMPLEMENTED` | 无状态、仓位切片或退出规则 | 依赖 Big Trend，Stage 3 |
| 重大事件直接退出 | `NOT_IMPLEMENTED` | 事件仅人工注入弱占位 | 依赖 Event Intelligence |
| Exit 真实战绩 | `NOT_IMPLEMENTED` | 无 signal outcome | Stage 4 |

结论：

> 当前仓库有退出“词汇”和迁移表，但没有达到 v1.0 的完整 Exit Engine。不能在 UI 中声称系统已经能可靠管理退出。

---

## 4.6 Big Trend Radar

| 能力 | 当前状态 | 当前相关基础 | 缺口 |
|---|---|---|---|
| Big Trend 状态机 | `NOT_IMPLEMENTED` | 基础 SectorStage 可作为输入 | 需要独立状态和证据，不能复用旧 SectorStage 充数 |
| 板块大行情 | `NOT_IMPLEMENTED` | SectorEngine 有当日 RS、breadth、leader、crowding | 缺多日持续性、成交额占比、扩散、龙头/中军、分歧和历史状态 |
| 个股大行情 | `NOT_IMPLEMENTED` | Trend/Momentum/RS 特征基础存在 | 缺专用目标、状态和持久化 |
| 龙头/中军 | `NOT_IMPLEMENTED` | 当前只用最大日涨幅近似 leader quality | 缺角色识别和稳定性 |
| 二次启动 | `PARTIAL` | 旧 SectorStage DECLINE 可回 ACCUMULATION | 不等于正式 Second-Wave 识别 |
| Early Radar | `NOT_IMPLEMENTED` | 无观察型专用输出 | Stage 3 后研究 |
| 主升浪 KPI | `NOT_IMPLEMENTED` | 无 outcome/benchmark | 依赖 Stage 4 结果跟踪 |
| Trend Runner 联动 | `NOT_IMPLEMENTED` | 无 | Stage 3 |

当前 `stock_tracker/features/sector.py` 使用小规模静态 `_SECTOR_MAP`，其余归 `BROAD`，因此现阶段无法安全声称具备全市场板块主升浪识别。

---

## 4.7 Event Intelligence

| 能力 | 当前状态 | 当前实现 | 缺口 |
|---|---|---|---|
| 事件表 | `PARTIAL` | SQLite `events` 占位表 | schema 过轻，无来源身份、known_at、raw artifact、novelty 等完整合同 |
| 手工注入事件 | `IMPLEMENTED` | `POST /api/events` → `post_event_inject()` | 仅弱占位，不是自动事件引擎 |
| 官方公告采集 | `NOT_IMPLEMENTED` | 无正式 Collector | A 股优先缺口 |
| 财报/政策/新闻采集 | `NOT_IMPLEMENTED` | 无 | Stage 3 |
| 原始文本/字节身份 | `CONTRACT_ONLY` | Quant Manifest 支持 ANNOUNCEMENT DataKind | 尚未接入事件采集 |
| 实体映射 | `NOT_IMPLEMENTED` | 无 | Stage 3 |
| Authority/Materiality/Novelty/Surprise | `NOT_IMPLEMENTED` | 旧 payload 可任意存 JSON | 缺严格 schema 和验证 |
| Price-in / confirmation | `NOT_IMPLEMENTED` | 无 | 依赖行情和事件时间 |
| LLM 抽取 | `NOT_IMPLEMENTED` | 无 | 必须在结构化合同后接入 |
| 事件驱动生产信号 | `BLOCKED` | S3 仅占位/注入 | 必须先有权威来源、PIT 时间和验证 |

---

## 4.8 Strategy Scoreboard

| 能力 | 当前状态 | 当前基础 | 缺口 |
|---|---|---|---|
| 策略 ID | `IMPLEMENTED` | Signal 有 strategy_id | 缺完整 strategy version |
| Signal History | `IMPLEMENTED` | SQLite `signal_history` | 记录迁移，不是交易结果 |
| Outcome | `NOT_IMPLEMENTED` | 无 runtime signal_outcome | 无 entry/exit/MFE/MAE/realized R/cost |
| 回测指标函数 | `CONTRACT_ONLY` | Quant evaluation metrics | 尚未绑定真实 T3 数据和产品策略 |
| 样本数/胜率/平均 R | `NOT_IMPLEMENTED` | synthetic benchmark 不能当真实战绩 | Stage 4 |
| Net Expectancy/Profit Factor/MaxDD | `CONTRACT_ONLY` | Quant 层具备部分能力 | 缺真实产品结果和 API |
| Brier/LogLoss/ECE | `CONTRACT_ONLY + SYNTHETIC_VALIDATED` | Calibration/metrics 代码存在 | 无真实校准模型 |
| ACTIVE/WATCH/DOWNWEIGHTED/BLOCKED | `CONTRACT_ONLY` | Registry/Promotion 基础存在 | 未接运行策略权重和 UI |
| Scoreboard API/UI | `NOT_IMPLEMENTED` | 无标准端点 | Stage 4 |

---

## 4.9 Replay

| 能力 | 当前状态 | 当前基础 | 缺口 |
|---|---|---|---|
| 指定历史 as-of | `CONTRACT_ONLY` | PIT Snapshot 和时间工具存在 | 运行产品没有 Replay orchestration |
| 冻结数据快照 | `CONTRACT_ONLY` | Manifest/PIT 基础 | 无真实 T3 Snapshot |
| 历史模型/配置身份 | `PARTIAL` | Quant registry/config fingerprint 有基础 | 未与产品信号持久化完整绑定 |
| 逐日步进 | `NOT_IMPLEMENTED` | 无 | Stage 4 |
| Replay API/UI | `NOT_IMPLEMENTED` | 无 | Stage 4 |
| Future visibility blocker | `CONTRACT_ONLY` | leakage audit 和 PIT 合同 | 需要端到端 replay test |

Replay 在 T3 数据、历史 Universe、事件时间和公司行为不足时必须保持 `BLOCKED`，不能用当前数据库简单倒放冒充 Point-in-Time Replay。

---

# 5. 数据与市场 Gap Matrix

## 5.1 A 股

| 数据能力 | 当前状态 | 说明 |
|---|---|---|
| 实时/准实时报价 | `IMPLEMENTED` | 多 Provider、Router、Health 基础存在，稳定性仍取决于免费源 |
| 日线 Bar | `IMPLEMENTED`（运行） | Eastmoney 路径和 SQLite 已接线；属于运行缓存/低可信研究起点 |
| 原始 Bar Artifact | `PARTIAL` | raw/parse、Manifest 和 capture 脚本基础存在；默认 BEST_EFFORT |
| 交易日历 | `CONTRACT_ONLY` | Quant 日历合同存在，尚缺持续权威真实数据供应 |
| 停复牌/风险警示 | `CONTRACT_ONLY/PARTIAL` | 执行合同存在，真实历史覆盖不足 |
| 公司行为/复权 | `PARTIAL` | Bar 有 adjustment_factor，但运行路径多为占位或隐式复权；缺可重建因子序列 |
| 历史 Universe | `NOT_IMPLEMENTED` | 无完整 PIT 上市/退市/成分数据 |
| 行业/概念 | `PARTIAL` | instruments.sector + 静态 map；非全市场、非 PIT |
| 官方公告 | `NOT_IMPLEMENTED` | Event Stage 前置 |
| 历史成交成本 | `CONTRACT_ONLY` | CostSchedule 合同存在，需真实版本数据 |
| T3 Snapshot | `BLOCKED` | 缺 Calendar/Status/Universe/Corporate Action 完整绑定 |

## 5.2 港股通

| 数据能力 | 当前状态 | 说明 |
|---|---|---|
| 港股报价/Bar | `PARTIAL` | Provider 支持部分港股，需诚实标延迟 |
| 港股通可交易名单 | `NOT_IMPLEMENTED` | 当前是广义 HK，不是 Stock Connect Universe |
| HKEX 日历/状态/VCM | `CONTRACT_ONLY` | 真实历史覆盖缺失 |
| 港股公司行为 | `NOT_IMPLEMENTED/PARTIAL` | 未形成研究级路径 |
| HKEXnews | `NOT_IMPLEMENTED` | Event Intelligence 前置 |
| 独立策略阈值 | `NOT_IMPLEMENTED` | `strategies.toml` 仍主要是全局 S1/S2/S3 |
| 独立校准/战绩 | `BLOCKED` | 缺数据和样本 |

## 5.3 美股

| 数据能力 | 当前状态 | 说明 |
|---|---|---|
| 报价/Bar | `PARTIAL` | 可用于低频展示，时效按源降级 |
| SEC/财报事件 | `NOT_IMPLEMENTED` | Stage 6 或 Event 通用层后接入 |
| 公司行为 | `NOT_IMPLEMENTED/PARTIAL` | 无研究级完整路径 |
| 行业和 Universe | `PARTIAL` | 无 PIT 完整数据 |
| 中线专属策略 | `NOT_IMPLEMENTED` | 旧策略未按市场完整拆分 |
| 独立校准/战绩 | `BLOCKED` | 低优先级，不能复用 A 股结果 |

---

# 6. 研究与模型 Gap Matrix

| 能力 | 当前状态 | 结论 |
|---|---|---|
| Point-in-Time 合同 | `SYNTHETIC_VALIDATED` | 工程基础可用 |
| Fingerprint/Manifest | `SYNTHETIC_VALIDATED` | 具备严格身份和篡改检测基础 |
| Calendar-aware Label | `SYNTHETIC_VALIDATED` | 不能替代真实 Calendar 数据 |
| Execution Backtester | `SYNTHETIC_VALIDATED` | 当前安全边界主要为单标的；真实多资产研究仍需扩展 |
| Triple Barrier | `SYNTHETIC_VALIDATED` | 标签构造正确性基础存在 |
| Purged Walk-forward | `SYNTHETIC_VALIDATED` | 需要真实候选和 T3 数据 |
| Logistic Baseline | `CONTRACT_ONLY/SYNTHETIC_VALIDATED` | 尚无真实 A 股 baseline 结论 |
| LightGBM Candidate | `CONTRACT_ONLY` | 可选依赖，不能默认 Champion |
| Calibration | `SYNTHETIC_VALIDATED` | 无真实产品概率 |
| Frozen Holdout | `CONTRACT_ONLY` | 尚未冻结首个真实 T4 区间 |
| Model Registry | `CONTRACT_ONLY` | 尚未驱动产品运行模型 |
| Champion/Challenger | `SYNTHETIC_VALIDATED` | synthetic challenger 曾因校准/时间稳定性失败而未晋级，治理行为正确 |
| 真实 success probability | `BLOCKED` | 继续保持 null |
| Big Trend 模型 | `NOT_IMPLEMENTED` | 先规则状态机和 outcome，再训练 |
| Event Continuation 模型 | `NOT_IMPLEMENTED` | 先事件数据合同 |
| Exit 风险模型 | `NOT_IMPLEMENTED` | 先确定性 Exit baseline |

---

# 7. 架构级发现

## 7.1 新旧状态词汇尚未统一

当前运行 `SignalState` 主要是：

```text
COLD
WATCH
ARMED_BREAKOUT
ARMED_PULLBACK
TRIGGERED
ACTIVE
TRIM
EXIT
OVEREXTENDED
INVALIDATED
DATA_INVALID
EXPIRED
```

v1.0 产品动作需要：

```text
WATCH
WAIT_PULLBACK
WAIT_BREAKOUT
EXECUTABLE
HOLD
WARNING
TRIM
PARTIAL_TAKE_PROFIT
TREND_RUNNER
EXIT
AVOID
DATA_BLOCKED
```

建议：

- 不直接破坏现有 SignalState；
- 新增产品层 `ActionState`；
- 用确定性映射从 SignalState、持仓状态、Risk Gate 和 Big Trend 得出 ActionState；
- 后续再评估是否合并底层状态。

这样能避免一次性迁移破坏现有 API、SQLite 和测试。

## 7.2 API 写操作能力不足

当前 HTTP Server 主要实现 GET 和少量 POST：

- watch add/remove；
- event inject。

Stage 1 需要：

- 账户 profile 读取和更新；
- Position 增删改；
- 严格 payload 类型和范围校验；
- 明确 400/404/409 错误；
- 不能把任意 JSON 直接写入数据库。

需要扩展 `PUT/PATCH/DELETE` 或采用明确 POST action 合同。建议优先实现标准 REST 动词，并补服务器级测试。

## 7.3 运行 schema 需要显式迁移

当前 `positions` 已有 shares/cost，但没有账户 profile。

建议新增：

```text
portfolio_profile
- id
- account_equity
- available_cash
- risk_mode
- per_trade_risk_pct
- max_position_pct
- max_portfolio_heat_pct
- max_sector_pct
- max_theme_pct
- updated_at
```

运行 schema 迁移与 Quant migration 必须区分，且：

- 测试先在临时数据库运行；
- 对生产 `data/stock_tracker.db` 不自动 apply；
- 应有幂等升级路径；
- 应备份/回滚或明确兼容策略。

## 7.4 Sector 数据不足以支撑 Big Trend

当前 `_SECTOR_MAP` 只覆盖少量代表性股票，其余归 `BROAD`。

因此：

- 可以继续用于 Demo/降级上下文；
- 不能作为全市场 Big Trend 正式数据；
- Stage 3 前必须先在 Stage 2 获得可靠行业/板块 identity；
- 历史研究还要求 PIT 成分，而不仅是今天的分类。

## 7.5 真实概率继续保持空值是正确行为

当前运行评分明确：

```text
success_probability = None
```

这与 v1.0 设计一致。Stage 1 不应为了首页完整而填入伪概率。

首页应展示：

```text
模型倾向：规则证据偏强/中性/偏弱
校准成功概率：真实证据不足，暂不展示
```

---

# 8. Stage 1：Today Action MVP 实施方案

## 8.1 Stage 1 目标

在不依赖真实校准概率、不假装 Big Trend 已完成的前提下，让系统第一次完整回答：

> **今天该怎么操作？**

Stage 1 重点是把已有运行能力组织成可信动作，并补齐账户/持仓与最低安全 Exit，而不是先训练新模型。

## 8.2 切片顺序

### Slice 1：产品层合同

建议新增独立模块：

```text
stock_tracker/decision/
├── __init__.py
├── types.py
├── action_mapper.py
├── trade_plan.py
├── position_sizing.py
└── brief.py
```

核心对象：

```text
ActionState
RiskMode
BlockerType
DecisionEvidenceLevel
UserPortfolioProfile
PositionRisk
TradePlan
DecisionAction
DecisionBrief
```

要求：

- 严格类型；
- 数值范围验证；
- 纯函数优先；
- 不在 import 时访问数据库或网络；
- 概率保持 Optional；
- hard/soft blocker 分离。

### Slice 2：账户与持仓持久化

实现：

- `portfolio_profile` schema；
- Repository CRUD；
- Position CRUD；
- 数据库升级测试；
- 幂等初始化；
- Windows 连接关闭测试；
- 不自动改生产数据库。

验证：

- 非数字、负数、NaN/Inf 拒绝；
- shares/cost/account equity/cash 范围校验；
- A 股股数建议按 lot size 取整；
- `available_cash <= account_equity` 是否作为可配置约束，需要产品层明确。

### Slice 3：Action Mapper

确定性地将现有运行状态映射为 v1.0 动作：

```text
ARMED_PULLBACK  -> WAIT_PULLBACK
ARMED_BREAKOUT  -> WAIT_BREAKOUT
TRIGGERED       -> EXECUTABLE（仅 Risk Gate 通过）
ACTIVE          -> HOLD
TRIM            -> TRIM
EXIT/INVALIDATED-> EXIT 或 AVOID
OVEREXTENDED    -> AVOID
DATA_INVALID    -> DATA_BLOCKED
```

持仓和非持仓必须区分：

- 无持仓的失效候选是 AVOID；
- 有持仓且核心逻辑失效才是 EXIT；
- 数据异常不能自动等价于卖出，应输出 DATA_BLOCKED 并保留最后可信动作。

### Slice 4：Trade Plan 与 Position Sizer

从现有 Signal 生成基础 TradePlan：

- entry zone；
- trigger；
- invalidation；
- targets；
- reward/risk；
- no-chase；
- next trigger；
- hard/soft blockers；
- balanced plan；
- optional aggressive plan；
- risk budget；
- position pct；
- shares。

Position Sizer 必须同时受：

- per-trade risk；
- max position；
- available cash；
- lot size；
- portfolio heat；
- data/risk hard block。

Stage 1 可以先不做高级流动性冲击，但必须标记 `liquidity_limit_not_yet_modeled`，不能假装已处理。

### Slice 5：最低安全 Exit Baseline

先实现确定性规则，不训练 Exit 模型：

- 有持仓且可信现价跌破结构失效位 → EXIT；
- 接近失效、风险恶化 → WARNING；
- overextended / 风险收益恶化但结构未破坏 → TRIM 候选；
- 数据 STALE/UNKNOWN → DATA_BLOCKED，不自动卖出；
- 严重负面事件留到 Stage 3 接入。

Stage 1 不实现：

- 部分止盈；
- Trend Runner；
- Big Trend 驱动 Exit；
- ML Exit risk。

但数据对象应预留兼容字段。

### Slice 6：Decision Brief Aggregator

生成确定性 `DecisionBrief`：

```text
market_posture
aggression_level
actions
core_opportunities
holding_actions
avoid_reasons
event_risks
data_health
model_evidence
```

Stage 1 的 `ai_summary` 可以：

- 先由确定性模板生成；或
- 设为 null，同时返回结构化 summary facts。

不应在没有 LLM 事实约束器之前直接调用外部模型。

### Slice 7：Core 3—5 Selector

Stage 1 使用安全降级排序：

- ActionState 优先；
- Opportunity；
- Timing；
- Reward/Risk；
- Confidence；
- Freshness；
- Crowding penalty；
- DataStatus；
- 板块配额。

当 probability 为 null 时，绝不填替代概率。

输出应包含：

- `ranking_mode = RULE_EVIDENCE`；
- `calibrated_probability = null`；
- `probability_evidence_level = INSUFFICIENT`。

### Slice 8：API

目标端点：

```text
GET  /api/brief/today
GET  /api/portfolio
PUT  /api/portfolio/profile
POST /api/portfolio/positions
PATCH /api/portfolio/positions/{id}
DELETE /api/portfolio/positions/{id}
GET  /api/decision/{symbol}
```

要求：

- API 不访问上游；
- 严格解析 JSON；
- 错误不静默变成空 payload；
- 400/404/409 语义明确；
- 所有决策响应带 as_of、data_status、evidence/mode 字段；
- 不破坏现有端点。

### Slice 9：前端

第一屏先实现：

- 市场姿态；
- 今天建议你做；
- Core 3—5；
- 持仓需要处理；
- 今日不要做；
- 概率证据不足提示；
- 账户/持仓本地编辑入口。

Stage 1 Big Trend 卡只显示：

```text
主升浪雷达：尚未启用正式算法
```

或完全隐藏。不得复用 SectorScore 冒充 Big Trend。

### Slice 10：测试与证据

至少新增：

```text
tests/test_decision_types.py
tests/test_action_mapper.py
tests/test_position_sizing.py
tests/test_trade_plan.py
tests/test_decision_brief.py
tests/test_portfolio_repository.py
tests/test_portfolio_api.py
tests/test_today_brief_api.py
```

关键金融用例：

- 硬阻断下激进方案不存在；
- probability null 不影响序列化；
- DATA_BLOCKED 不自动等于 EXIT；
- 有持仓跌破失效位才可 EXIT；
- 建议股数不会超过现金和硬上限；
- 建议股数按交易单位向下取整；
- entry == invalidation 时失败关闭；
- 负值、NaN、Inf 拒绝；
- 同一板块不会占满 Core 3—5；
- 无数据时 Brief 明确阻断，不生成伪动作；
- API 不访问 Provider。

---

# 9. Stage 1 明确不做

为了避免范围失控，Stage 1 不做：

- Big Trend 正式算法；
- 官方公告自动采集；
- LLM 在线摘要；
- 真实校准概率；
- LightGBM 晋级；
- Strategy Scoreboard 真实战绩；
- Replay；
- 港股通完整 Universe；
- 美股专属模型；
- 自动下单；
- 生产数据库自动 migration；
- 多币种组合完整汇率风险。

这些能力保留在后续 Stage，不应为了“看起来完整”做假占位。

---

# 10. Stage 1 验收标准

## 10.1 产品

- 首页能在 60 秒内回答今日动作；
- Core Opportunities 默认不超过 5；
- 持仓动作和新机会分开；
- 每个机会都有完整基础交易计划；
- 每个不可执行机会都有明确 Why Not Buy；
- 概率证据不足时显示 null 和说明；
- 不展示伪 Big Trend；
- 不展示伪策略战绩。

## 10.2 数据和风险

- 账户和持仓 payload 严格校验；
- 仓位计算不超过现金、风险和硬上限；
- hard blocker 不能被 AGGRESSIVE 绕过；
- 数据异常时不产生新执行动作；
- 数据异常不会凭空触发卖出；
- 所有决策有 as-of 和 data status。

## 10.3 工程

- 不破坏现有 API；
- 不访问上游的 API 不新增网络依赖；
- migration 在临时数据库验证；
- 生产数据库未改变；
- legacy 和 quant tests 全绿；
- compileall 通过；
- 关键源码进入 Git；
- 当前 diff 不混入 `overview.md` 等无关文件。

---

# 11. 任务分工建议

## Codex 主做

- ActionState 与映射；
- TradePlan 合同；
- hard/soft blocker；
- PositionSizer；
- 最低安全 Exit baseline；
- Core 排序和多样性；
- DecisionBrief 组合；
- 金融正确性测试和 Review。

## 普通工程 Agent 可并行

在合同冻结后：

- portfolio CRUD；
- API 路由；
- Repository；
- 前端表单和卡片；
- SSE/通知接线；
- 普通 API/前端测试；
- 文档和配置。

所有仓位、Exit、Risk Gate、Core 排序语义必须经过 Codex Review。

---

# 12. 推荐的下一动作

Stage 0 文档交付完成后，下一安全动作是：

1. 先冻结 Stage 1 的 Python 数据合同；
2. 用纯函数实现 PositionSizer、Action Mapper 和 TradePlan；
3. 补齐针对性测试；
4. 再设计运行 schema migration 和 API；
5. 最后改首页。

不建议第一步直接重写前端，因为没有稳定决策合同时，UI 很容易再次固化旧字段和旧状态。

---

# 13. 审计结论

当前项目不是从零开始：运行采集、信号骨架和 Quant Foundation 已经提供了良好基础。

但从 v1.0 产品视角看，当前成熟度更准确地描述为：

```text
运行交易驾驶舱骨架：已实现
Today Action 决策产品：部分实现
持仓与仓位：部分实现
Exit Engine：部分词汇，无完整算法
Big Trend：未实现
Event Intelligence：人工占位
Strategy Scoreboard：合同基础，无真实产品
Replay：合同基础，无产品实现
真实校准模型：被正确阻断
```

因此正确的开发顺序不是继续堆模型或直接做主升浪 UI，而是先完成 Stage 1 Today Action MVP 的产品合同和账户/持仓决策闭环，同时继续保持真实概率、真实战绩和 Big Trend 的诚实状态。
