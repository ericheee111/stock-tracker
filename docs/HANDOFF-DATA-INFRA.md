# 数据基建交接（HANDOFF · Data-Infra Lane）

> 车道：**SoftwareCompany / 数据基建**（WorkBuddy 侧，非 Quant）
> 提交：**`c8f0ece`** `feat(data-infra): 接通历史K线采集+展示指标，覆盖A/HK/US三市场`
> 日期：2026-08-13
> 定位：**数据基建（采集 + 入库 + 展示指标），不含任何 scoring/risk/sector/quant 算法。**

---

## 0. 边界声明（重要）

`c8f0ece` 只描述**数据基建车道**的原始交付，当时未触碰以下 Quant 边界：

- `stock_tracker/quant/`（point-in-time / triple-barrier / backtest / model / calibration / market rules / 核心 scoring / risk / sector）
- `scripts/capture_quant_bars.py`、`tests_quant/*`、`.gitignore`、`docs/CODEX-QUANT*`、PRD v0.4

后续 Codex hardening 在独立提交中增加了 `stock_tracker/quant/data/`、raw Artifact 捕获、source-distribution 门禁，并修复 `stock_tracker/storage/db.py` 的线程本地连接切换生命周期。运行态 K 线可用于展示、候选发现和规则信号，但**不得直接冒充 T3 研究训练集**。

---

## 1. 交付清单（c8f0ece，30 文件 +2324/−11）

### 采集层（Collector）
- `stock_tracker/collector/provider.py`：基类新增 `supports_bars()` / `fetch_bars()`；研究捕获边界另有 `supports_raw_bars()` / `fetch_bars_raw()` / `parse_bars()`
- `stock_tracker/collector/eastmoney.py`：**`supports_bars()→True`**，东财 `push2his.eastmoney.com` 日 K；运行解析可跳过孤立坏行，研究解析 `parse_bars_strict()` 对任一坏行失败关闭
- `stock_tracker/collector/tencent.py`：`supports_bars()→False`，仅在配置 `bars_fallback=true` 时作为显式兜底；当前只接受 `1d + qfq`
- `stock_tracker/collector/router.py`：主源异常或单标的空结果时继续尝试 fallback；symbol-specific 空结果不会污染 Provider 全局健康度

### 入库 / 调度 / 质量
- `stock_tracker/core/config.py`：`CollectorConfig` 接线 `bars_enabled / bars_interval_sec / bar_batch_size / bar_batch_pause_sec / bar_backfill_days / bar_keep_days`，安全布尔字段要求真实 TOML boolean
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
- 覆盖 API 指标、Bar DQ、Provider/Router fallback、Repository、严格配置、Scheduler、前端消费契约和数据库连接生命周期
- 测试数量会随 hardening 增长；交接与发布必须引用当前 commit 的自动化输出，不再把 `c8f0ece` 当时的固定计数作为最新结论

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
    "recent_bars": [ { "timestamp": "...", "open": ..., "close": ..., "high": ..., "low": ..., "volume": ..., "source": "eastmoney", "adjustment_factor": 1.0, "quality_status": "DELAYED" }, ... ],
    "bar_count": 9110
  }
  ```
  - `indicators` 任一字段可能为 `null`（输入为空/过短时不抛，返回全 null 壳）
  - `recent_bars` 最多 30 条（按日期升序）
- `GET /api/overview` 机会列表每行附 `indicators`（同结构）

### 2.2 本地 Python 复用
- `from stock_tracker.features.feature_snapshot import build_indicators`
  - 入参：`list[Bar]` 与 `Market`；按整根 Bar 对齐校验，避免 OHLC/volume 分别过滤后跨日期错位
  - 出参：dict，字段见 `serialize_indicators`；空/短输入返回全 `None` 壳
- `Repository(db_path).load_recent_bars(symbol, interval="1d", n=260)` 取最近 N 根日 K

### 2.3 SQLite 存储
- 表 `bars`：主键为 `symbol + timestamp + interval`，保存 `market, OHLC, volume, amount, turnover, source, adjustment_factor, quality_status`
- `quality_status` 使用 `DataStatus`：运行态有效日线为 `DELAYED`，字段不完整为 `STALE`，future-leak 为 `UNKNOWN` 且被 Scheduler 丢弃；不得把 EOD 伪装成 `LIVE`

---

## 3. 指标口径（避免重复实现）

`build_indicators` 当前实现：
- 均线：`ma5 / ma10 / ma20 / ma60`；EMA：`ema12 / ema26`；MACD：`dif / dea / hist`
- `rsi14`（Wilder）、`atr14`（True Range 14）、`roc20 / roc60`
- `ann_vol` = `stdev(日收益) * sqrt(交易日) * 100`，`_TRADING_DAYS = {"A":242,"HK":244,"US":252}`
- `pos52w` = **分位排名**；重复价格取并列名次中点，完全平盘序列取 `0.5`
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

1. 运行态规则评分 / 页面展示可消费精确 symbol 的指标与 `bars` 缓存；缺数据时返回空，不做 SH/SZ 等跨证券身份猜测。
2. 正式标签、回测、校准和模型晋级必须通过 `scripts/capture_quant_bars.py` 的 exact raw Artifact 入口，并继续绑定 Calendar/Status/Universe/Corporate Action 后才能形成 T3 Snapshot；不得把 SQLite 运行缓存直接回流为训练集。
3. Stage 2G 已建立 A/HK/US 三市场 synthetic Golden Raw、Eastmoney/Tencent exact-raw 安全抓取、字段级 reconciliation 与 Calendar Session 覆盖缺口报告；下一步必须用真实双源 capture、来源独立性、单位/币种、复权和许可证据复验，不能把 fixture 直接晋级为 T3。
4. 港/美信号需要在对应市场**交易时段**才有 `LIVE` 报价支撑；验收时按交易时段复核。
