# stock-tracker 交接文档（HANDOFF）

> 主理人：齐活林（Delivery Director）｜团队：许清楚(PM) / 高见远(Architect) / 寇豆码(Engineer) / 严过关(QA)
> 对应 PRD：`docs/PRD-股票辅助判断与交易参考网站.md`（v0.3，47 节，2361 行）
> 日期：2026-08-13
> 用途：供其他 agent / 开发者接续 Phase 2–5 工作。

---

## 0. TL;DR

stock-tracker 已实现为一个**零第三方依赖的 Python 标准库后端 + 静态前端**的真实可运行系统，并通过 **143 个单元/集成测试（0 fail / 0 error）**。本会话完成了全部 P0 部署与真实数据阻断项修复（见 §2）。PRD 47 节中**核心架构已实现且经测试验证**；剩余为历史 K 线、回测/校准、市场独立配置、退出引擎等增强项（见 §5 路线图），作为明确的接续任务。

---

## 1. 系统架构速览

- **后端**：`stock_tracker/` 纯标准库（`http.server.ThreadingHTTPServer` + `sqlite3` + `urllib.request` + `tomllib`）。`python -m stock_tracker` 一键启动，无需 `pip install`。
- **前端**：`web/` 静态 HTML/JS/CSS 玻璃拟态驾驶舱，由 Python HTTP 服务托管。
- **数据层**：`stock_tracker/storage/repository.py`（SQLite），含 `save_bar` / `load_recent_bars`（历史 K 线接口已就位但**未接线**，见 §5 T1）。
- **采集**：`stock_tracker/collector/` 三家免费源：腾讯 `qt.gtimg.cn`(GBK)、东财 `push2.eastmoney.com`(JSON)、新浪 `hq.sinajs.cn`(CSV，需 Referer)。`ProviderRouter` 熔断 / 指数退避 / 跨源偏差 / token-bucket 限频。
- **调度**：`stock_tracker/collector/scheduler.py` HOT/WARM/COLD 三守护线程（A 股 HOT=3s / WARM=10s / COLD=45s）。
- **特征**：`stock_tracker/features/` 5 证据族（去相关、无重复计数）+ 市场 Regime(5 态) + 板块轮动。
- **信号**：`stock_tracker/signals/` 4 分数 + 风险闸门 + 12 态状态机 + OverextensionPenalty(反 FOMO)。
- **质量**：`stock_tracker/data_quality/` 闸门(VALID/DEGRADED/STALE/INVALID) + future-leak 硬阻断。
- **API**：`stock_tracker/api/` handlers / serializers / sse，SSE 推送 `/api/stream`。

---

## 2. 本会话已修复并验证（P0/P1 阻断项）

| 项 | 文件 | 提交/状态 | 验证方式 |
|---|---|---|---|
| `$PORT` 环境变量 + 运行时自建 `data/` | `stock_tracker/__main__.py` | `9eaa1fb` | 实测 `--help`/import 通过；`$PORT=9099` 覆盖生效；`--port` 优先于 `$PORT` 优先于 app.toml |
| Bug1 Eastmoney 快照归一化崩溃（引用不存在的 `Market.SH`） | `stock_tracker/collector/eastmoney.py` | 源码已修 | 143 测试全绿；`test_provider.test_snapshot_layout` 由 error 转 ok |
| Bug2 Sina/Eastmoney 丢失股票名 | `stock_tracker/collector/{eastmoney,sina}.py` | 源码已修 | 143 测试全绿；`test_provider.test_csv_mapping` 由 fail 转 ok |
| G10 腾讯源被指回 `127.0.0.1:9`（真实数据阻断） | `config/providers.toml` | `e59ef30` | TOML 解析通过；host 置空后用内置 `qt.gtimg.cn` |
| G12 regime 指数集混入个股 300750.SZ | `stock_tracker/features/regime.py` | `e59ef30` | `test_regime` 7/7 通过 |
| **真实数据展示（用户验收项）**：机会列表/指数卡补发实时 `quote` 与 `name`；DQ 时钟偏差容忍（future-leak 120s 漂移窗）；`observed_age_ms` 夹 0；`age<=0` 视为 LIVE | `stock_tracker/api/{handlers,serializers}.py`、`stock_tracker/data_quality/gate.py`、`stock_tracker/collector/provider.py` | `aebfb48` | 活体 curl `/api/overview` 含真实 `quote`（中芯 132.48 / 茅台 1356.31…）；Playwright 12 卡真实价格、三市场指数真实、徽章无「未知/数据不足」 |
| **机会列表按 symbol 去重**：同一股票被多策略命中时不再重复展示 | `stock_tracker/api/handlers.py` (`_top_opportunities`) | `9e850b2` | 活体 `/api/overview` 去重后 `duplicates=[]`；145 测试全绿 |
| **风险事件卡渲染原始 JSON → 结构化展示**：`renderRiskCard` 移除 `JSON.stringify` 兜底，按 symbol/市场/风险等级(HIGH红·MEDIUM琥珀·LOW灰)/风险分/状态徽章/完整 reason 渲染 | `web/js/components.js` (`renderRiskCard` + `RISK_LEVEL_COLORS/LABELS`)、`web/css/cockpit.css` (.risk-* 子类) | `6caeae7` | QA 独立回归全 PASS：源码无 JSON.stringify、`esc()` 全覆盖、7 字段契约对齐、空态保留、CSS 作用域隔离、Playwright `hasRawJSON=false` + 截图无 `{` `}`；145 测试全绿 |

---

## 3. 测试与验证结果

- **145 用例 → 全部通过（0 fail / 0 error）**（含 `aebfb48` 真实数据展示修复与 `9e850b2` 去重修复后的回归）。
- **关键验证点（均 ok）**：
  - future-leak 硬阻断：`computed_at < timestamp` → INVALID、分数 0。
  - 风险闸门：`DQ=STALE/INVALID/DEGRADED` 全阻断强信号；`VALID` 放行。
  - 状态机非法迁移回退：`TRIGGERED→WATCH` 非法 → 维持 `TRIGGERED`。
  - 端到端管线：归一化 Quote → 特征 → 策略 → 评分 → 风险闸门 → 状态机 → SQLite 落库回读（进程内，无网络依赖）。
  - 数据新鲜度不伪造：陈旧 A 股行情 → `observed_age_ms>0` 且 `data_status` 为 DELAYED/STALE，**绝不 LIVE**。
- **环境限制说明**：本开发沙箱拦截 Python 外网出口，故未做"活体"端到端真实行情 curl。在可联网宿主 / Render 上代码可正常取真实数据（G10 回环阻断已修复）。

---

## 4. 数据真实性约束（铁律，接续时务必遵守）

- 绝不 mock 当生产；UI 区分真实 / 测试。
- 港股 / 美股 15 分钟延迟数据不可伪造为实时（`data_status` 标 DELAYED/STALE，绝不标 LIVE）。
- 每条行情带 `source_timestamp / received_at / computed_at / displayed_at / observed_age_ms`；`data_status ∈ {LIVE, DELAYED, STALE, UNKNOWN}`。
- **北向资金 §17.5**：**禁止**把已停更的实时净流入字段当作实时强信号。`S3Event.evaluate` 仅接受 `inject_event` 注入事件，无注入返回 `None`（已在全仓 grep 复核，无失真因子）。

---

## 5. 接续路线图（架构师 PRD 对齐差异清单 T1–T15）

按优先级排序，每一项均可直接转交工程师（含 file:line 提示）。

### P0
- **T1 历史 K 线采集链路**：`repository.save_bar` 无调用方、`scheduler.load_recent_bars` 永远返回 `[]`。各 Provider 需实现日 K / 分钟 K 拉取并 `repo.save_bar`；scheduler `_run_cold` 接线。影响：`MA/MACD/RSI/ATR` 当前仅用当日涨跌兜底，特征失真。→ 最高杠杆修复。
- **T2 腾讯 host 配置**（本会话已完成）：production `host=""`（用内置 `qt.gtimg.cn`）。

### P1
- **T3 Exit Engine**：状态机 `decide` 缺 EXIT/TRIM 分支，ACTIVE/TRIGGERED 信号永不退出。补 `VALID_TRANSITIONS`（TRIGGERED→EXPIRED）与现价 ≤ `invalidation_price` → EXIT。
- **T4 市场独立配置**：`strategies.toml` 全局权重、S4/S5/S6 缺失、`applies_to` 通吃三市场。按市场选权重（美股中线不同）。
- **T5 OverextensionPenalty 维度补全**：PRD #14.3 要求 distance_to_MA20/ATR、涨幅分位、跳空/ATR、换手极值、板块 PEAK。
- **T6 行业/概念真实化**：`features/sector.py` 硬编码 ~35 标的 → 其余归 `BROAD` 兜底。需真实行业 / 概念数据源。
- **T7 walk-forward 回测 + 概率校准**：`ScoreSet.success_probability` 恒 `None`（正确占位），需 `research/backtest.py` + `research/calibration.py` + model_registry（依赖 T1/T4）。这是 PRD #11.5 硬性要求。
- **T8 复权 / 公司行为**：`Bar.adjustment_factor` 占位 1.0，无复权价序列。
- **T9 HOT/WARM 按市场分频**：HK/US 不应高频伪装（虽 `data_status` 标 DELAYED，但轮询浪费、Timing/Confidence 未自动打折）。

### P2
- **T10 RankScore + Top-K 多样性**：`handlers._top_opportunities` 仅按 opportunity 降序，缺校准概率 / 多样性上限。
- **T11 Regime 特征增强**：指数 MA20/MA60 斜率、新高新低、涨停跌停结构。
- **T12 持仓风险贡献 / 集中度**：`get_positions` 缺风险贡献、同板块集中度、距失效位%。
- **T13 真实 ADX**：`evidence.py` 用 ATR 冒充 ADX（占位，贡献仅 ±10）。
- **T14 regime._INDEX_SYMBOLS**（本会话已完成）。

### P3
- **T15 清理前端 DEMO 死分支**：`web/js/components.js` DEMO 文案 / 分支不可达（后端不发 DEMO，符合绝不伪造原则），加注释或删除。

---

## 6. 运行与部署

### 本地
- 一键：`scripts/start.bat`（Windows）/ `scripts/start.py`（跨平台，写 PID 文件 + 建 `data/`）。
- 手动：`python -m stock_tracker --host 0.0.0.0 --port 8080`
- 自检：`python -m stock_tracker --once`（拉 COLD+WARM 打印摘要）
- 脚本：`scripts/{start,stop,restart,status}.bat`

### 部署（Render）
- `Dockerfile`（python:3.13-slim，零依赖）+ `render.yaml`（free，region singapore，healthCheckPath `/api/provider_health`）+ `Procfile`（web: `python -m stock_tracker`）。
- **关键**：已支持 `$PORT` 环境变量（Render 注入）；启动前自建 `data/`。
- 部署后**必须验证腾讯源从 Singapore 节点可达**（中国行情源从海外节点可能受限，需实测；不可达时仅新浪兜底 A 股，HK/US 无数据）。

---

## 7. 关键配置文件

- `config/providers.toml`：腾讯 `host=""`（用内置 `qt.gtimg.cn`）；新浪需 Referer；东财 JSON。
- `config/app.toml`：server.host/port、采集间隔、cold_universe。
- `config/markets.toml`：各市场交易时段。
- `config/strategies.toml`：S1/S2/S3 阈值（待补市场维度，T4）。
- `config/risk.toml`：风险闸门阈值。

---

## 8. 给接续 agent 的建议

1. 改动前先跑 `python -m unittest discover -s tests -p "test_*.py"` 确保 143 全绿。
2. **最高杠杆 = T1（历史 K 线）**：解锁真实 MA/RSI/Regime，直接提升信号质量，且是 T3/T4/T5/T7/T11/T13 的前置依赖。
3. **T7（回测/校准）** 让 `success_probability` 有真实值，是 PRD #11.5 硬性要求，也是独立成功率指标的前提。
4. **严守数据真实性铁律（§4）**：任何"让数据好看"的伪装在 PRD 中明令禁止，会直接破坏系统价值主张。
5. 改动后务必跑全量回归 + 在可联网环境做真实行情 curl 验收（`/api/overview` 含 `breadth`/`risk_events`；`/api/markets` 收盘后 A/HK 应为 STALE 且 `observed_age_ms>0`）。

---

## 9. 前端可视化验证（Playwright 截屏 + 浏览器真实操作）

> 日期：2026-08-12｜触发：用户开放沙箱完全网络访问后，补齐此前被拦截的可视化验证目标④。
> 工具：`qa/ui/shot.cjs`（Playwright Node 版，多策略解析 playwright，截图落 `qa/ui/shots/`，支持 `BASE`/`SHOT_OUT` 环境变量；`qa/ui/click_test.cjs` 点击机会卡断言 sheet 非空）。
> **模型限制**：本模型无法读取 PNG（`Read` 截图返回图片被过滤），故验证证据以「脚本文本断言 + `/api/*` curl 实测」为准，截图仅作人工辅助。

### 9.1 本会话经可视化验证发现并修复的 Bug

| Bug | 严重度 | 根因 | 修复 | 提交 |
|---|---|---|---|---|
| 指数卡切换不过滤 | P1 | `renderIndexGrid` 用 `markets[market]` 但 dict 键为小写 `a/hk/us`、tab market 为大写 `A/HK/US`，不匹配 fallback 到 `markets.a` | 改为大小写不敏感 + 兼容 Array | `94ae7f3` |
| banner 开休市状态全显示 `?` | P1 | 后端 `market_open_status()` 返回字符串 `'TRADING'/'CLOSED'/'WEEKEND'/'DISABLED'`，前端 `renderBanner` 却按布尔判断（`open===true?开:open===false?休:?`），字符串永不等于布尔 | `renderBanner` 增加字符串→中文映射（开/周末休/休/停） | `94ae7f3` |

> 其余 6 个 UI/数据缺陷（市场 tab 不过滤 topList、信号 sheet 打不开、桌面响应式缺失、quote.last=0→全标"数据异常"、指数数据缺失、markets.toml 重复 `[index]` 节）已在 `7b9d863`/`79bdb0a`/`d972622` 修复并验证。

### 9.2 可视化回归结论（Playwright 实锤）

- **真实数据活体**：`/api/overview` `data_mode=LIVE`，腾讯主源熔断 `CLOSED`、真实指数流入（上证 `3946.68`、恒生 `25440.17`、纳指 `26585.51`），收盘后诚实标 `STALE` + `observed_age_ms>0`，绝不伪 `LIVE`。
- **banner 修复生效**：`A:休 港:休 美:开`（北京时间 23:30，A/港已休、美股正交易），与后端 `{'a':'CLOSED','hk':'CLOSED','us':'TRADING'}` 一致。
- **市场过滤生效**：切 HK/US → topList 显「该市场暂无重点机会」(len≈9)；切 A → 显 125 条。
- **指数卡渲染**：`indexGrid.len=29`（非空，切换修复生效）。
- **信号 sheet 弹层**：点击机会行 → `sheet open=true, len=284`。
- **三页导航**：watch/radar/research 均 `active=true`，内容正常渲染。
- **移动端/桌面响应式**：1440 视口 `body.width=1280`、多列布局；390 移动端单列（桌面容器 960→1280 修复见 `7b9d863`）。
- **控制台**：仅 1 个 `404`（favicon.ico，次要），**无 JS pageerror、无其他 reqfail**。

### 9.3 已知遗留 nit（非阻断，列入路线图）

- **N1 favicon 404**：未提供 `web/favicon.ico`，浏览器请求 404。建议放一个 1x1 或品牌图标。
- **N2 收盘后信号态 `DATA_INVALID` 文案偏 alarmist**：收盘后 `last=None` → 信号引擎将 A 股机会标 `DATA_INVALID` 态（显示"数据异常不给信号"）。此为信号状态机（12 态之一）在缺实时价时**主动 withholding 信号**的正确行为，**非新鲜度 bug**（新鲜度 `data_status` 为 `STALE`，`INVALID` 数量=0）。UX 上"数据异常"易误读为系统故障，建议接入 T1 历史收盘价后改为"休市无实时价"之类的温和文案。
- **N3 `_read_toml` 静默吞异常（设计缺陷，高风险）**：`stock_tracker/core/config.py::_read_toml` 用 `try/except` 吞掉 `TOMLDecodeError` 返回 `{}`，导致配置错误（如重复 `[index]` 节）被**完全掩盖**——本次 `markets.toml` 重复节就曾让整份配置静默回退默认值、指数卡全空。建议改为：解析失败时**抛错或至少 warn 日志 + 字段级校验**，避免"配置错了但服务照常起、界面静默空白"的排查地狱。**强烈建议列入 P0 修复。**

### 9.4 接续建议（可视化方向）

- 把 `qa/ui/shot.cjs` 接入 CI：每次前端改动后跑一遍，断言 `indexGrid.len>0` / `topList.len>0` / `sheet open` / 无 `pageerror`。需 CI 环境装 Playwright + Chromium（`PLAYWRIGHT_PATH` 或 `npm i -D playwright`）。
- N3 修复后，应补一个「故意写坏 TOML → 启动应报错而非静默回退」的测试。
