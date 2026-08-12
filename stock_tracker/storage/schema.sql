-- Stock Tracker · SQLite Schema（§12，九张表）
-- 主键、关键索引见下表注释。所有时间统一以 ISO8601 文本存储（UTC 无关，按本机本地）。
-- idempotent：建表使用 IF NOT EXISTS；repository 在首次启动执行本文件。

-- 标的字典（COLD 更新）
CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT PRIMARY KEY,
    market      TEXT NOT NULL,
    name        TEXT,
    sector      TEXT,
    exchange    TEXT,
    currency    TEXT,
    listing_date TEXT,
    is_active   INTEGER DEFAULT 1,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_instruments_market ON instruments(market);

-- K 线（COLD/历史入库）
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    interval    TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL,
    volume      INTEGER, amount REAL, turnover REAL,
    source      TEXT,
    adjustment_factor REAL DEFAULT 1.0,
    quality_status TEXT,
    PRIMARY KEY (symbol, interval, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_ts ON bars(symbol, timestamp);

-- 最新 Quote 快照（HOT/WARM 写）
CREATE TABLE IF NOT EXISTS quotes_cache (
    symbol      TEXT PRIMARY KEY,
    market      TEXT,
    data        TEXT,            -- JSON 序列化 Quote（含 quality/data_status/timestamp）
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_cache_market ON quotes_cache(market);

-- 自选
CREATE TABLE IF NOT EXISTS watchlist (
    symbol      TEXT PRIMARY KEY,
    market      TEXT,
    added_at    TEXT,
    note        TEXT
);

-- 持仓
CREATE TABLE IF NOT EXISTS positions (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    market      TEXT,
    shares      REAL,
    cost        REAL,
    added_at    TEXT,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

-- 信号当前态
CREATE TABLE IF NOT EXISTS signals (
    signal_id   TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    market      TEXT,
    strategy_id TEXT,
    state       TEXT,
    state_changed_at TEXT,
    previous_state TEXT,
    reason      TEXT,
    entry_low   REAL, entry_high REAL, trigger_price REAL,
    invalidation_price REAL, target_1 REAL, target_2 REAL,
    reward_risk REAL, freshness REAL,
    market_regime TEXT, sector_stage TEXT,
    next_trigger TEXT, what_changed TEXT,   -- JSON
    data_status TEXT,
    scores      TEXT,                        -- JSON(ScoreSet)
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

-- 状态迁移记录
CREATE TABLE IF NOT EXISTS signal_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT,
    at          TEXT,
    reason      TEXT,
    what_changed TEXT                      -- JSON
);
CREATE INDEX IF NOT EXISTS idx_signal_history_signal ON signal_history(signal_id);

-- Provider 熔断/健康滚动值（重启后重置为 HALF_OPEN 试探）
CREATE TABLE IF NOT EXISTS provider_state (
    provider    TEXT PRIMARY KEY,
    circuit_state TEXT,
    last_success_at TEXT,
    fails_since TEXT,
    extra       TEXT                      -- JSON(健康滚动)
);

-- 事件占位（S3 用，PRD #9 / #17.5 仅注入）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,
    market      TEXT,
    event_type  TEXT,
    direction   TEXT,
    published_at TEXT,
    usable_from TEXT,
    confirmed   INTEGER DEFAULT 0,
    weight      REAL DEFAULT 0.0,
    payload     TEXT,                      -- JSON
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, published_at);
