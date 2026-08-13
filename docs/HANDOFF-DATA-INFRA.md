# 数据基建交接（HANDOFF · Data-Infra Lane）

> 车道：**SoftwareCompany / 数据基建**（WorkBuddy 侧，非 Quant）
> 提交：**`c8f0ece`** `feat(data-infra): 接通历史K线采集+展示指标，覆盖A/HK/US三市场`
> 日期：2026-08-13
> 定位：**数据基建（采集 + 入库 + 展示指标），不含任何 scoring/risk/sector/quant 算法。**

---

## 0. 边界声明（重要）

本文件只描述**数据基建车道**交付的内容。**严格未触碰**以下 Quant 边界（PRD §38 研发分工）：

- `stock_tracker/quant/`（point-in-time / triple-barrier / backtest / model / calibration / market rules / 核心 scoring / risk / sector）
- `stock_tracker/storage/db.py`（Codex 的线程本地连接切换）
- `scripts/capture_quant_bars.py`、`tests_quant/*`、`.gitignore`、`docs/CODEX-QUANT*`、`docs/PRD*`(v0.4)、`docs/HANDOFF.md`(v0.4)

`stock_tracker/quant` 由 ChatGPT/Codex 负责，本车道只在其上方提供**可被消费的 K 线 + 指标数据**，不依赖其代码。

---

## 1. 交付清单（c8f0ece，30 文件 +2324/−11）

### 采集层（Collector）
- `stock_tracker/collector/provider.py`：基类，新增 `supports_bars()` / `fetch_bars()` 契约
- `stock_tracker/collector/eastmoney.py`：**`supports_bars()→True`**，东财 `push2his.eastmoney.com` 日 K 端点（独立域名，本环境实测可达，覆盖 A/HK/US + 港股指数）
- `stock_tracker/collector/tencent.py`：`supports_bars()→False`（默认 OFF，本环境腾讯 K 线持续 `bad params`）
- `stock_tracker/collector/router.py`：`fetch_bars` / `_select_bars` 只选 `supports_bars()` 的源

### 入库 / 调度 / 质量
- `stock_tracker/core/config.py`：`CollectorConfig` 接线 `bars_backfill_days / bars_max_per_symbol / bars_prune_days / bars_min_bars`
- `stock_tracker/storage/repository.py`：`save_bars_batch`（单事务 REPLACE）、`prune_bars`、`load_recent_bars`（默认 260）
- `stock_tracker/data_quality/gate.py`：`evaluate_bar`（future-leak 硬阻断 + 完整性校验）
- `stock_tracker/collector/scheduler.py`：第 4 个守护线程 `_run_bars`（首跑全量回填 `_backfill_bars` + 增量 `_incremental_bars`）

### 指标 / API
- `stock_tracker/features/feature_snapshot.py`：`build_indicators(bars, market)` —— **纯函数、try/except 包裹绝不抛**
- `stock_tracker/api/handlers.py`：`_top_opportunities` 每行附 `indicators`；新增 `get_quote_detail` → `/api/quote/{symbol}`
- `stock_tracker/api/serializers.py`：`serialize_indicators` / `serialize_bar`
- `stock_tracker/api/server.py`：新增 `/api/quote/{symbol}` 路由

### 前端
- `web/js/api.js`（`getQuote`）、`web/js/components.js`（`renderIndicators`，纯展示、与 scoring 解耦）、`web/js/app.js`（行情面板）、`web/css/cockpit.css`（`.ind-*`/`.quote-panel`/`.qb-table`）

### 测试
- 13 个新增测试文件（`test_api_indicators` / `test_bar_dq` / `test_bars_gate` / `test_bars_provider` / `test_bars_repository` / `test_config_contract` / `test_feature_snapshot` / `test_provider_bars` / `test_quote_api` / `test_scheduler_bars` / `test_tencent_bars` 等）
- 全量 `python -m unittest discover`：**220 用例通过，0 fail，1 skipped**

---

## 2. 消费契约（供 Quant / 其他车道使用）

### 2.1 REST API
- `GET /api/quote/{symbol}` 返回：
  ```json
  {
    "symbol": "00700.HK", "market": "HK", "name": "腾讯控股",
    "quote": { ... 实时报价 snapshot ... },
    "indicators": { "ma5": 466.64, "ma20": 466.0, "rsi14": 52.5,
                    "atr14": 14.2, "roc20": -8.97, "roc60": -20.18,
                    "pos52w": 0.013, "ann_vol": 37.0, "vol_ratio": 3.10,
                    "amplitude": 2.73, "last_close": 466.0, "bar_count": 30 },
    "recent_bars": [ { "date": "...", "open": ..., "close": ..., "high": ..., "low": ..., "volume": ... }, ... ],
    "bar_count": 9110
  }
  ```
  - `indicators` 任一字段可能为 `null`（输入为空/过短时不抛，返回全 null 壳）
  - `recent_bars` 最多 30 条（按日期升序）
- `GET /api/overview` 机会列表每行附 `indicators`（同结构）

### 2.2 本地 Python 复用
- `from stock_tracker.features.feature_snapshot import build_indicators`
  - 入参：`bars`（list of dict，含 date/open/close/high/low/volume）、`market`（`"A"|"HK"|"US"`）
  - 出参：dict，字段见 `serialize_indicators`；空/短输入返回全 `None` 壳
- `from stock_tracker.storage.repository import load_recent_bars`
  - `load_recent_bars(symbol, market, limit=260)` 取最近 N 根日 K

### 2.3 SQLite 存储
- 表 `bars`：列 `symbol, market, date, open, high, low, close, volume, amount, turnover, status, created_at`
- `status` 取值：`VALID`/`DELAYED`/`DEGRADED`/`STALE`/`INVALID`/`UNKNOWN`（由 `evaluate_bar` 判定；日线 EOD 为 `DELAYED` 不伪装 `LIVE`）

---

## 3. 指标口径（避免重复实现）

`build_indicators` 当前实现：
- 均线：`ma5 / ma10 / ma20 / ma60`；EMA：`ema12 / ema26`；MACD：`dif / dea / hist`
- `rsi14`（Wilder）、`atr14`（True Range 14）、`roc20 / roc60`
- `ann_vol` = `stdev(日收益) * sqrt(交易日) * 100`，`_TRADING_DAYS = {"A":242,"HK":244,"US":252}`
- `pos52w` = **分位排名** `sorted(w).index(closes[-1]) / (len(w)-1)`（非 rolling_percentile）
- `vol_ratio`（量比）、`amplitude`（当日振幅）

> 如需修改口径，请在本文件车道内改 `feature_snapshot.py`，**不要**在 Quant 侧另写一套，避免口径漂移。

---

## 4. 实测验证（活体，2026-08-13 盘后）

- `/api/quote` 三市场实测：
  - A `600519.SH`：ma20=1321.5，rsi14 已算，bar_count 填充
  - HK `00700.HK`：ma5=466.64 / ma20=466.0 / rsi14=52.5 / atr14=14.2 / roc20=-8.97 / roc60=-20.18 / **pos52w=0.013（近52周低位）** / ann_vol=37.0% / vol_ratio=3.10 / amplitude=2.73
  - US `AAPL.US`：ma20=322.18 / rsi14=35.28 / **pos52w=0.506（中位）** / ann_vol=31.0%
- SQLite `bars` 表 **9110 行**，覆盖 37 标的（A 26 + HK 5 + US 6）

---

## 5. 已知行为（非 Bug，交接提醒）

- **港/美机会列表盘后为空是预期行为**：实时报价新鲜度闸门判定 `quotes_cache` 中 HK/US 全 `STALE`（US age≈25.8h，即上一美股收盘后），其信号行 `DATA_INVALID`（"数据不足暂不发信号"），被正确排除出机会列表。**交易时段实时报价转 `LIVE` 后这些信号自然浮现。**
- 评分阈值 / 新鲜度阈值属于 Quant §38 边界，**本车道不调整**。
- 腾讯 K 线默认 OFF（本环境持续 `bad params`）；A/HK/US + 港股指数 K 线由东财 `push2his` 提供。

---

## 6. 下一步（建议 Quant 车道接续）

1. Quant 评分 / 信号可直接消费 `/api/quote/{symbol}.indicators` 与 `bars` 表，无需重建指标。
2. 把本车道的 `bars`（运行态原始日 K）与 Quant 的 `stock_tracker/quant/data/*`（研究态 PIT 数据集）按 T3 Snapshot 合同对接（见 `docs/HANDOFF.md` v0.4 §0.1）。
3. 港/美信号需要在对应市场**交易时段**才有 `LIVE` 报价支撑；验收时按交易时段复核。
