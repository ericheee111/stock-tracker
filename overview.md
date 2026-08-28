# Stock Tracker v1.1 项目概览

> **定位：A 股优先的个人交易决策驾驶舱**
> 核心问题：**今天该怎么操作？**
> 市场优先级：A 股第一、港股通第二、美股第三
> 当前范围：辅助判断与手工执行；不承诺收益，不自动下单
> 默认部署：`HYBRID_PRIVATE`（本地数据与决策引擎 + 云端静态网页 + Tailscale Serve）
> 纯云后端：可选实验，不作为上线前置条件；Oracle Cloud 已排除

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
- 默认关闭的 `free-stockdb` localhost RAW Bar Sidecar 合同，用于后续 WARM/COLD PoC；
- HOT / WARM / COLD 调度；
- Quote、日线 Bar、SQLite；
- 数据质量、新鲜度和 Provider Health；
- 基础 Market Regime、Sector、S1/S2/S3；
- Opportunity / Timing / Risk / Confidence；
- Risk Gate、信号状态机、Next Trigger、What Changed、拥挤度；
- REST、SSE 与原有驾驶舱页面；
- 默认关闭的 XTP read-only Quote Sidecar 工程链：Python 3.9 隔离、IPv4 loopback IPC、Simulator、严格 Event/Session/Hash/交易日合同和官方 Quote 模块探针；
- 独立 Market Event Store：生产库路径失败关闭、immutable files、SHA-256 Hash Chain、Manifest、协调提交、分钟聚合和本地 Replay；
- Signal Monitor Engine、私有 Monitor REST/SSE、不可变 Rule Snapshot、Outbox 租约/Notification Worker 与 Inbox/Rules/Data Link/Replay 工作台；
- 64 标的 synthetic XTP Shadow；真实 XTP、Level 1/2、持续吞吐、保存权和 Live Shadow 仍待验收。

### 部署现状与目标

当前已经具备：

- 本地 Python 后端同时托管静态前端和 `/api/...`；
- 私有 API 的 loopback 免认证与强 Bearer Token 安全边界；
- fetch-stream SSE，可发送 Authorization Header；
- Hybrid H0 loopback 默认安全、非 loopback 显式确认、Tailscale Serve 运维 CLI 与临时数据库验收工具；
- 本地远程式静态页、REST、SSE、Profile/Position CRUD 验收通过；
- Docker / Render Demo 配置只以 `PURE_CLOUD_EXPERIMENTAL` 显式 opt-in。

仓库侧 Hybrid H0–H5 已实现；当前尚待 operational 验收：

- 真实 Tailscale Serve 和两台不同 Tailnet 设备；
- Windows 重启、休眠/唤醒和网络恢复；
- Cloudflare Pages/GitHub Pages 实际部署；
- 任何公开 Funnel/Tunnel；
- 真实 XTP Quote、Level 1/2、50–100 标的 Live Shadow 和持续吞吐。

因此 Render 免费后端只保留实验定位；默认目标是本地引擎承担采集、计算、SQLite、XTP Sidecar、Monitor 和私有持仓，云端只托管无密钥静态网页。

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
- Frozen Holdout、Model Registry、Experiment Ledger 与晋级门；
- 公司行为 exact-raw、身份绑定、revision graph 与 adjusted-market-data 合同；
- 行业/板块 PIT 分类与成员关系合同；
- Event Intelligence 事实、实体绑定、修订与市场确认合同；
- Big Trend v1 多证据状态机与 Trend Runner 研究合同；
- `free-stockdb` T1 Sidecar 的 loopback/read-only/RAW-only 隔离、发行包审计与差异比较治理合同；
- Signal Outcome、成本后 Strategy Scoreboard 与 `INSUFFICIENT_REAL_EVIDENCE` 门禁；
- 正式/诊断 PIT Replay 的输入快照清单与 fail-closed 计划合同；
- 失败归因和同 cohort 策略版本比较合同；
- 统一 Decision Quality / Model Promotion Gate；
- 新样本 Shadow 验证和 ACTIVE/WATCH/DOWNWEIGHTED/BLOCKED/RETIRED 生命周期建议；
- A 股、港股通和美股独立 Profile 与跨市场零权重 Shadow 隔离合同。

这些 Quant 和 Sidecar 能力目前主要是**工程合同与合成验证**。真实 free-stockdb 发行包、同步网络、数据许可和 50—100 标的数据一致性尚未审计，所有结果都不代表真实投资表现。

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

本机浏览器无需额外设置。默认远程模式使用 Tailscale Serve，Backend 只监听 loopback，家庭路由器不做端口转发；访问设备加入 Tailnet，并继续使用强 Bearer Token 作为纵深防御。需要公开给少量朋友时，才评估 Tailscale Funnel 或自有域名 + Cloudflare Tunnel。云端静态站点不得保存账户、持仓、成本或私有访问值。

---

## 5. 真实能力边界

当前明确**没有**声称完成：

- 真实校准成功概率；
- 真实 Strategy Scoreboard；
- Big Trend / 主升浪正式产品接线、真实数据校准和真实捕获率；
- 官方公告、财报、政策和新闻的真实 Event 数据接入；
- 真实 free-stockdb 发行包、同步网络和数据许可审计；
- free-stockdb 在 WARM/COLD Shadow Scanner 中的真实启用；
- 使用真实 T3 快照的 Point-in-Time Replay 产品与 UI；
- T3 研究级完整 A 股 Snapshot；
- 模型已经真实击败简单基线；
- 港股通和美股独立校准；
- 混合部署所需的 Runtime Config、跨域 CORS、Runtime Health、Tailscale Serve 与云端静态部署验收；
- 自动下单。

因此：

```text
Model Score != 胜率
Opportunity / 100 != 概率
Synthetic benchmark != 真实投资战绩
运行 SQLite Bar != 自动成为训练数据
free-stockdb 本地可达 != T2/T3 或可训练
free-stockdb 当前板块映射 != 历史 PIT 成分
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
3. `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`
4. `docs/PRODUCT-GAP-MATRIX-v1.1.md`
5. `docs/STAGE1-API-CONTRACT-v1.md`
6. `docs/HANDOFF.md`
7. `docs/VALIDATED-STRATEGY-ML-LIBRARY.md`
8. `docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md`
9. `docs/STAGE2D-STAGE4-EXECUTION-ROADMAP.md`
10. `docs/STAGE3C-FREE-STOCKDB-SIDECAR-CONTRACT.md`
11. `docs/STAGE4C-STAGE6A-EXECUTION-ROADMAP.md`
12. `docs/STAGE5A-DECISION-QUALITY-GATE.md`
13. `docs/STAGE5B-SHADOW-LIFECYCLE-CONTRACT.md`
14. `docs/STAGE6A-MARKET-ISOLATION-CONTRACT.md`

---

## 8. 当前下一步

Stage 2B—6A 的后端工程合同已经形成。Hybrid H0—H5 的仓库侧工程实现已完成，真实 Tailscale、Windows 恢复和 Pages 部署仍待 operational 验收；数据主线继续补真实研究证据：

1. **补齐 Hybrid H0 operational 验收：在安装并登录 Tailscale 的服务端启用 Serve，并从第二台独立 Tailnet 设备验证静态页、REST、SSE 和临时 Portfolio CRUD；工程实现与本地远程式验收已经完成；**
2. **Hybrid H1（已完成）：无密钥 Runtime Config、固定 Allowed API Origin/Engine ID、统一 URL Builder、Origin-scoped Token 与 Major/Engine/Build hard block；**
3. **Hybrid H2（已完成）：exact CORS/OPTIONS、Authorization、metadata-only Runtime Health、非法 Health hard block、动态 STALE 与 SSE 无热重试；**
4. **Hybrid H3/H4（工程已完成）：补真实 Serve Target、开机/休眠恢复与 Cloudflare/GitHub Pages operational 证据；**
5. **Hybrid H5（失败关闭）：可信朋友优先加入 Tailnet；公开 Funnel/Tunnel 在独立限流与安全 Review 前不启用；**
6. **Stage 3C.2：固定真实 free-stockdb Release，审计二进制、首次运行网络、同步源、manifest 和数据许可；**
7. **HiThink Financial-API：使用真实账户凭据做小窗口 exact-raw 日线捕获与许可审查；保持默认关闭和 T1，不进入 Runtime、训练或再分发；**
8. **执行 50—100 个代表性标的的 RAW 日线/分钟线多源差异矩阵，审计通过后才进入 WARM/COLD Shadow Scanner 与 EOD Reconciliation；**
9. **建设真实 Outcome 的追加式持久化与独立样本收集，达到门槛前 Strategy Scoreboard 保持 `INSUFFICIENT_REAL_EVIDENCE`；**
10. **闭环 T3 A 股 Snapshot、正式 PIT Replay、Replay UI、真实新样本 Shadow 与受控模型部署；**
11. **依次进入 Stage 6B—6F 的港股通、美股独立数据、校准、Scoreboard、Shadow 与真实证据审查。**

默认方案不依赖 Oracle Cloud、付费域名或付费云后端。本地机器关闭、休眠、断网或 Tunnel 中断时，云端页面虽然仍可加载，但必须显示 `ENGINE_OFFLINE`/`STALE`，不得继续展示旧的可执行动作。

在真实审计完成前，`free_stockdb.enabled` 与 `hithink_finance.enabled` 均保持 `false`；在真实 Outcome、许可和 T3 数据不足时，不展示真实胜率、真实 Big Trend 捕获率或正式模型晋级结论。跨市场阈值、校准和模型只能进入目标市场零权重 Shadow，不能直接复用到生产。

---

## 9. 部署规范来源（2026-08-24）

默认正式路线不再要求完整后端常驻公有云：

```text
默认模式 `HYBRID_PRIVATE`：本地 Provider、free-stockdb、SQLite、Quant、Replay、持仓与私有 API
云端：静态网页与无密钥 Runtime Config；可选脱敏快照仅为后续非默认能力
连接：本地主动建立的安全出站通道，禁止直接暴露 Sidecar
```

规范模式名统一为 `LOCAL_ONLY / HYBRID_PRIVATE / HYBRID_PUBLIC_AUTH / PURE_CLOUD_EXPERIMENTAL`，其中 `HYBRID_PRIVATE` 是当前默认正式模式；可选 `HYBRID_SNAPSHOT` 仅为后续、非默认、脱敏只读能力。`PURE_CLOUD_EXPERIMENTAL` 只有在真实数据可达性、持久化、授权、成本和安全全部通过后才可升级。Oracle Cloud 因无法注册已移出候选和依赖链。

详细架构与阶段见：

```text
docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md
```
