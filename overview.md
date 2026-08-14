# Stock Tracker v1.0 项目概览

> **定位：A 股优先的个人交易决策驾驶舱**
> 核心问题：**今天该怎么操作？**
> 市场优先级：A 股第一、港股通第二、美股第三
> 当前范围：辅助判断与手工执行；不承诺收益，不自动下单

---

## 1. 产品目标

Stock Tracker 不把“指标很多”或“模型分数很高”当成最终答案，而是把可信行情、市场环境、板块、个股信号、风险预算和持仓事实组合成可执行的今日简报：

```text
今天总体进攻、防守还是等待
哪些持仓继续持有、预警、减仓或退出
今天真正值得看的 3—5 个机会
现在能不能执行
买在哪里、什么价格不追、错了在哪里退出
按账户风险最多买多少股
为什么还不能买
数据、概率和策略证据是否足够
```

模型准确率仍是长期核心竞争力，但模型输出必须经过数据、可交易性、风险、组合和校准门禁。

---

## 2. 当前已实现

### 运行底座

- Python 标准库后端与静态 Web；
- 腾讯、东财、新浪 Provider 与 Router；
- HOT / WARM / COLD 调度；
- Quote、日线 Bar、SQLite；
- 数据质量、新鲜度和 Provider Health；
- 基础 Market Regime、Sector、S1/S2/S3；
- Opportunity / Timing / Risk / Confidence；
- Risk Gate、信号状态机、Next Trigger、What Changed、拥挤度；
- REST、SSE 与原有驾驶舱页面。

### Stage 1 Today Action

- 严格 `ActionState`、Blocker、Portfolio、TradePlan 和 DecisionBrief 合同；
- 旧 `SignalState` 到产品动作的确定性映射；
- 只有 LIVE 可信价格跌破结构失效位才产生最低安全 `EXIT`；
- STALE/UNKNOWN 数据只产生 `DATA_BLOCKED`，不伪造买入或卖出；
- long-only PositionSizer：同时约束单笔风险、现金、单股上限、Portfolio Heat、板块/主题暴露、流动性上限和交易单位；
- A 股新开仓建议按 100 股取整；港股 lot size 必须显式提供；美股默认 1 股；
- Portfolio Profile 与 Position REST CRUD；
- 持仓事实允许零碎股，不把账户事实误当成新订单；
- Core Opportunity 默认最多 5 个，按动作优先、symbol 去重和板块配额筛选；
- 真实 `GET /api/brief/today`；
- “今日作战简报 + 确定性参谋摘要”首页；
- 概率不足时保持 `null`；
- Big Trend 未实现时明确 `NOT_AVAILABLE`；
- 真实策略战绩不足时明确 `INSUFFICIENT_REAL_EVIDENCE`；
- Mock 前端 QA 与真实 Python API + Web Playwright 集成 runner。

### Quant Foundation

独立 `stock_tracker.quant` 已包含：

- Point-in-Time Fact / Snapshot；
- 稳定 fingerprint 与 Manifest；
- 交易日历、证券状态和可信数据合同；
- Market Rule、Cost Schedule、next executable price；
- A 股 T+1、停牌、涨跌停与交易单位执行合同；
- Triple Barrier / Target-before-stop；
- Purged walk-forward；
- Logistic baseline、可选 LightGBM Candidate；
- Platt / Isotonic calibration；
- Frozen Holdout、Model Registry、Experiment Ledger 与晋级门。

这些 Quant 能力目前主要是**工程合同与合成验证**，不代表真实投资表现。

---

## 3. 今日首页输出

```text
今日作战简报
├── 确定性参谋摘要
├── 市场姿态与进攻度
├── 今天建议你做
├── Core Opportunities（最多 5 个）
├── 持仓需要处理
├── 今日不要做
├── 主升浪状态
└── 数据与模型证据状态
```

动作词汇：

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

Stage 1 只实际启用已经有确定性证据支持的动作；部分止盈和 Trend Runner 仍等待后续 Big Trend / Exit 阶段。

---

## 4. 私有数据安全

以下端点可能包含账户净值、持仓、盈亏或建议股数，属于私有 API：

```text
/api/brief/today
/api/portfolio
/api/portfolio/profile
/api/portfolio/positions
/api/portfolio/positions/*
```

默认规则：

- 本机直连时，只有 TCP 客户端地址和 HTTP `Host` 都是 localhost/loopback 才免认证；
- 公网或反向代理访问必须配置运行环境 `STOCK_TRACKER_PRIVATE_ACCESS`；
- 未配置时公网私有 API 返回 503；
- 配置后必须提供 `Authorization: Bearer ...`；
- Web 端只从当前浏览器会话的 `sessionStorage.stockTrackerPrivateAccess` 读取访问值；
- 私有访问值不得提交到 Git、写入公开前端或日志。

本机浏览器无需额外设置。公网私有部署需由用户主动配置服务端环境变量，并在当前浏览器会话中设置相同访问值。

---

## 5. 真实能力边界

当前明确**没有**声称完成：

- 真实校准成功概率；
- 真实 Strategy Scoreboard；
- Big Trend / 主升浪正式算法；
- 官方公告、财报、政策和新闻 Event Intelligence；
- Point-in-Time Replay 产品；
- T3 研究级完整 A 股 Snapshot；
- 模型已经真实击败简单基线；
- 港股通和美股独立校准；
- 自动下单。

因此：

```text
Model Score != 胜率
Opportunity / 100 != 概率
Synthetic benchmark != 真实投资战绩
运行 SQLite Bar != 自动成为训练数据
SectorScore != Big Trend
```

---

## 6. 启动与验证

本机启动：

```bash
python -m stock_tracker
```

运行产品测试：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Quant 测试：

```bash
python -m unittest discover -s tests_quant -p "test_*.py" -v
```

Today Action Mock QA：

```bash
node qa/ui/today_action_qa.cjs
```

真实 Python API + Web 集成：

```bash
python scripts/run_stage1_today_integration.py
```

完整门禁见根目录 `AGENTS.md`。

---

## 7. 必读文档

按顺序：

1. `AGENTS.md`
2. `docs/PRD-股票辅助判断与交易参考网站.md`
3. `docs/PRODUCT-GAP-MATRIX-v1.0.md`
4. `docs/STAGE1-API-CONTRACT-v1.md`
5. `docs/HANDOFF.md`
6. `docs/VALIDATED-STRATEGY-ML-LIBRARY.md`
7. `docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md`

---

## 8. 当前下一步

Stage 1 核心决策链已经完成。当前下一产品切片是：

> **Portfolio 设置与持仓编辑 UI，并用真实私有 REST API完成端到端 CRUD 验收。**

完成后再进入 Stage 2 的 A 股数据与决策质量建设，而不是提前展示 Big Trend、真实概率或真实策略战绩。
