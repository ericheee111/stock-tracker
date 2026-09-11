/* =========================================================================
 * wb3_server.cjs —— WB3 自包含 mock/临时 API harness（合成数据，零真实数据）
 * 监听 127.0.0.1:18183（仅回环）。场景由 env WB3_SCENARIO 选择：
 *   full | empty | error | stale | auth | nulls | longnames
 * 静态服务 <repo>/web/**；/api/* 全部合成载荷；/api/stream SSE 保持连接。
 * 不读取生产 SQLite、不启用外网 Provider、不做任何持久化。
 * 用法：node wb3_server.cjs   （前台运行；外部进程管理生命周期）
 * ========================================================================= */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = Number(process.env.WB3_PORT || 18183);
const HOST = '127.0.0.1';
const SCENARIO = process.env.WB3_SCENARIO || 'full';
const ROOT = path.resolve(__dirname, '..', '..');
const WEB = path.join(ROOT, 'web');
const FIXTURE = path.join(ROOT, 'qa', 'fixtures', 'today-brief-v1.json');

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.png': 'image/png'
};

const NOW = Date.now();
const iso = (offsetMs) => new Date(NOW + (offsetMs || 0)).toISOString();

function healthPayload(status, dataStatus) {
  return {
    schema_version: 'hybrid-runtime-v1',
    status: status,
    engine_id: 'stock-tracker-local',
    engine_version: '1.1.0',
    commit_id: 'wb3-synthetic-commit',
    deployment_mode: 'HYBRID_PRIVATE',
    started_at: iso(-3600000),
    last_heartbeat_at: iso(0),
    last_collection_at: dataStatus === 'STALE' ? iso(-7200000) : iso(-30000),
    data_as_of: dataStatus === 'STALE' ? iso(-7200000) : iso(-30000),
    data_status: dataStatus,
    scheduler_state: 'RUNNING',
    provider_summary: { count: 2, closed: 2, half_open: 0, open: 0 },
    database_state: 'READY',
    sse_available: true,
    api_major: 1
  };
}

function quote(last, changePct, status, ageMs) {
  return {
    symbol: '000000.SYN', last: last, prev_close: last != null ? +(last / (1 + changePct / 100)).toFixed(2) : null,
    change_pct: changePct, data_status: status || 'LIVE',
    observed_age_ms: ageMs == null ? 12000 : ageMs, source: 'wb3-synthetic', timestamp: iso(-12000)
  };
}

function scores(o, t, r, c) { return { opportunity: o, timing: t, risk: r, confidence: c }; }

const LONG_NAME = '超长名称回归验证·中国人民保险集团股份有限公司全称不被截断校验用例标的';

function radarSignal(state, symbol, name, sc) {
  return {
    symbol: symbol, name: name, market: 'A', state: state, strategy_id: 'S1',
    signal_id: 'sig-' + symbol.toLowerCase() + '-' + state.toLowerCase(),
    score: sc || scores(80, 70, 40, 65),
    entry_low: 10.1, entry_high: 10.4, trigger_price: 10.5,
    invalidation_price: 9.7, target_1: 11.5, target_2: 12.2, reward_risk: 2.1,
    regime: 'ROTATION', sector_stage: 'LEADING',
    next_trigger: '合成数据·下一触发说明文本',
    what_changed: ['合成·价格上穿 MA20', '合成·量比抬升'],
    quote: quote(10.45, 1.8, 'LIVE', 15000),
    data_status: 'LIVE', observed_age_ms: 15000,
    state_changed_at: iso(-600000)
  };
}

function watchItem(symbol, name, sc, sigState) {
  return {
    symbol: symbol, name: name, market: 'A',
    quote: quote(10.45, 1.8, 'LIVE', 20000),
    score: sc || scores(72, 60, 45, 58),
    signal: sigState === null ? null : {
      signal_id: 'sig-' + symbol.toLowerCase(), state: sigState || 'WATCH',
      entry_low: 10.1, entry_high: 10.4, trigger_price: 10.5,
      invalidation_price: 9.7, reward_risk: 2.1,
      next_trigger: '合成·等待回踩确认', what_changed: ['合成·缩量']
    }
  };
}

function positionItem(symbol, name, cost, shares) {
  return {
    symbol: symbol, name: name, market: 'A', cost: cost, shares: shares, closed_at: null,
    quote: quote(10.45, -0.6, 'LIVE', 18000),
    signal: {
      signal_id: 'sig-pos-' + symbol.toLowerCase(), state: 'ACTIVE',
      invalidation_price: 9.7, reward_risk: 1.8, next_trigger: '合成·持有观察'
    }
  };
}

const SECTORS = [
  { sector: '机器人', score: 88, stage: 'LEADING', crowding: 61 },
  { sector: '化工', score: 76, stage: 'ACCUMULATION', crowding: 40 },
  { sector: '食品饮料', score: 65, stage: 'EARLY', crowding: 22 },
  { sector: '有色金属', score: 58, stage: 'PEAK', crowding: 74 },
  { sector: '新能源', score: 44, stage: 'DIVERGENCE', crowding: 55 },
  { sector: '医药', score: 31, stage: 'DECLINE', crowding: 30 }
];

const PROVIDERS = [
  { provider: 'tencent', circuit_state: 'CLOSED', latency_p50: 126, error_rate: 0 },
  { provider: 'eastmoney', circuit_state: 'OPEN', latency_p50: null, error_rate: 1.0 }
];

function overview(metaMode) {
  return {
    meta: {
      data_mode: metaMode || 'LIVE', last_update: iso(-30000),
      market_open: { a: 'TRADING', hk: 'TRADING', us: 'CLOSED' },
      providers: PROVIDERS
    },
    regime: { regime: 'ROTATION', market_score: 56, sub_factors: { breadth: 58, trend: 51, vol: 62, momentum: 49, risk: 40 } },
    sector_leaders: SECTORS,
    breadth: { up: 2100, down: 1800, flat: 500 },
    risk_events: [
      { symbol: '600000.SH', market: 'A', level: 'MEDIUM', risk_score: 55, state: 'OVEREXTENDED', reason: '合成·短期涨幅偏离均线较远' }
    ],
    top_opportunities: [
      { symbol: '600000.SH', name: '浦发银行', market: 'A', state: 'TRIGGERED', signal_id: 'sig-600000', scores: scores(84, 78, 41, 72), quote: quote(10.45, 1.8), entry_low: 10.1, entry_high: 10.4, reward_risk: 2.1, next_trigger: '合成·执行条件满足', what_changed: ['合成·突破'] },
      { symbol: '00700.HK', name: '腾讯控股', market: 'HK', state: 'ARMED_PULLBACK', signal_id: 'sig-00700', scores: scores(66, 55, 48, 60), quote: quote(382.4, -0.9, 'DELAYED', 900000), entry_low: 375, entry_high: 385, reward_risk: 1.6, next_trigger: '合成·回踩观察' }
    ],
    holding_signals: [
      { symbol: '601319.SH', name: '中国人民保险集团', state: 'ACTIVE', scores: scores(62, 58, 35, 66), horizon: { key: 'MEDIUM', label: '中线', span: '几周', order: 2 }, reason: '合成·持有逻辑仍在', strategy_id: 'S2', signal_id: 'sig-601319', next_trigger: '合成·跟踪止损', what_changed: ['合成·无变化'] }
    ]
  };
}

function marketsDict() {
  return {
    a: { index: { symbol: '000001.SH', name: '上证指数', last: 3128.43, change_pct: 0.62, data_status: 'LIVE', observed_age_ms: 15000 } },
    hk: { index: { symbol: 'HSI', name: '恒生指数', last: 25410.7, change_pct: -0.44, data_status: 'DELAYED', observed_age_ms: 900000 } },
    us: { index: { symbol: 'SPX', name: '标普500', last: 5624.3, change_pct: 0.21, data_status: 'DELAYED', observed_age_ms: 5400000 } }
  };
}

function briefToday() {
  // 复用仓库既有 fixture（qa/fixtures/today-brief-v1.json，synthetic 合同样本）
  return JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
}

function portfolioPayload() {
  return {
    profile: {
      account_equity: 100000, available_cash: 50000, risk_mode: 'BALANCED',
      per_trade_risk_pct: 0.005, max_position_pct: 0.2,
      max_portfolio_heat_pct: 0.06, max_sector_pct: 0.35, max_theme_pct: 0.35
    },
    positions: [
      { id: 'pos-1', symbol: '601319.SH', market: 'A', shares: 1000, average_cost: 10.5, added_at: iso(-86400000 * 30) }
    ]
  };
}

function monitorSummary() {
  return {
    monitor: { outbox_by_state: { PENDING: 0, DELIVERED: 3, FAILED: 0 } },
    data_link: {}
  };
}

function monitorDataLink() {
  return {
    status: 'DISABLED',
    sidecar_health: { backend: 'SIMULATOR', feed_mode: 'SIMULATOR', subscription_count: 0, last_event_at: null },
    sidecar_session: { session_id: null, backend: 'SIMULATOR', feed_mode: 'SIMULATOR', symbols: [] },
    sidecar_metrics: { latency_p50_ms: null, latency_p95_ms: null, callback_count: 0, duplicate_count: 0, callback_gap_count: 0, provider_gap_count: 0, out_of_order_count: 0, reconnect_count: 0 },
    event_store: { ingestion_lag_ms: null, event_count: 0, partition_count: 1, integrity_passed: null },
    runtime_event_worker: { running: true, queue_size: 0, queue_capacity: 256, processed: 0, dropped: 0 },
    notification_worker: { running: false, last_error_code: null },
    last_poll: { integrity: { passed: null }, completed_at: null, accepted: null }
  };
}

const RULES = [
  {
    rule_id: 'mon-wb3-001', name: 'P95 延迟超过 200ms（合成）', version: 1, severity: 'WARNING', enabled: true,
    expression: { logic: 'AND', conditions: [{ fact: 'market_event.latency_p95_ms', operator: 'GE', value: 200 }] },
    scope: { kind: 'SYMBOLS', symbols: ['600519.SH'], market: 'A', max_symbols: 1, all_market_acknowledged: false },
    cooldown_sec: 300, duplicate_window_sec: 300, expires_at: null, notification_channels: ['BROWSER']
  }
];

const INBOX = [
  {
    inbox_id: 'wb3-inbox-001', state: 'NEW', severity: 'WARNING', symbol: '600519.SH',
    title: '合成监控事件·P95 延迟超阈值', summary: '合成数据·XTP P95 延迟 254ms 超过 200ms 阈值（仅演示证据流，非真实行情）',
    rule_id: 'mon-wb3-001', rule_version: 1,
    evidence: { facts: { conditions: [{ fact: 'market_event.latency_p95_ms', value: 254 }] } },
    last_triggered_at: iso(-120000), first_triggered_at: iso(-600000), trigger_count: 3
  }
];

function replayPayload() {
  const bars = [];
  let price = 10.0;
  for (let i = 0; i < 60; i++) {
    const open = price;
    const close = +(price + (i % 7 - 3) * 0.012).toFixed(3);
    bars.push({ open: open, high: +(Math.max(open, close) + 0.02).toFixed(3), low: +(Math.min(open, close) - 0.02).toFixed(3), close: close });
    price = close;
  }
  return { symbol: '600519.SH', backend_used: 'python', row_count: bars.length, minute_bars: bars };
}

/* ---------------- 场景差异 ---------------- */
function okJson(body) { return { status: 200, body: body }; }

function privateErrorResponse() {
  return { error: { code: 'INTERNAL', message: 'wb3 mock：合成内部错误（错误态场景）' } };
}

function buildScenario() {
  const decisionPaths = ['/api/brief/today', '/api/overview', '/api/watchlist', '/api/positions', '/api/radar'];
  const map = new Map();

  if (SCENARIO === 'stale') {
    map.set('HEALTH', healthPayload('STALE', 'STALE'));
    // 决策端点请求会被前端 assertDecisionReady 本地阻断；若仍到达服务端则诚实返回 409
    decisionPaths.forEach((p) => map.set(p, { status: 409, body: { error: { code: 'DATA_STALE', message: 'wb3 mock：数据已过期，禁止决策' } } }));
    map.set('/api/markets', okJson({ markets: marketsDict() }));
    map.set('/api/portfolio', okJson(portfolioPayload()));
    map.set('/api/provider_health', okJson({ providers: PROVIDERS }));
    map.set('/api/sectors', okJson({ sectors: SECTORS }));
    map.set('/api/config', okJson({}));
    monitorEndpoints(map, 'stale');
    return map;
  }

  if (SCENARIO === 'auth') {
    map.set('HEALTH', healthPayload('ONLINE', 'LIVE'));
    const authError = { status: 401, body: { error: { code: 'PRIVATE_API_AUTH_REQUIRED', message: '缺少私有访问值' } } };
    ['/api/brief/today', '/api/overview', '/api/watchlist', '/api/positions', '/api/radar', '/api/portfolio', '/api/config',
      '/api/monitor/summary', '/api/monitor/data-link', '/api/monitor/rules', '/api/monitor/inbox', '/api/monitor/outbox'].forEach((p) => map.set(p, authError));
    map.set('/api/markets', okJson({ markets: marketsDict() }));
    map.set('/api/provider_health', okJson({ providers: PROVIDERS }));
    map.set('/api/sectors', okJson({ sectors: SECTORS }));
    return map;
  }

  if (SCENARIO === 'error') {
    map.set('HEALTH', healthPayload('ONLINE', 'LIVE'));
    const err = { status: 500, body: privateErrorResponse() };
    ['/api/brief/today', '/api/overview', '/api/watchlist', '/api/positions', '/api/radar', '/api/portfolio',
      '/api/monitor/summary', '/api/monitor/data-link', '/api/monitor/rules', '/api/monitor/inbox', '/api/monitor/outbox'].forEach((p) => map.set(p, err));
    map.set('/api/markets', okJson({ markets: marketsDict() }));
    map.set('/api/provider_health', okJson({ providers: PROVIDERS }));
    map.set('/api/sectors', okJson({ sectors: SECTORS }));
    map.set('/api/config', okJson({}));
    return map;
  }

  map.set('HEALTH', healthPayload('ONLINE', 'LIVE'));
  map.set('/api/markets', okJson({ markets: marketsDict() }));
  map.set('/api/provider_health', okJson({ providers: PROVIDERS }));
  map.set('/api/sectors', okJson({ sectors: SECTORS }));
  map.set('/api/config', okJson({}));
  map.set('/api/portfolio', okJson(portfolioPayload()));
  map.set('/api/brief/today', okJson(briefToday()));
  monitorEndpoints(map, SCENARIO);

  if (SCENARIO === 'empty') {
    map.set('/api/overview', okJson(Object.assign(overview('LIVE'), {
      sector_leaders: [], risk_events: [], top_opportunities: [], holding_signals: []
    })));
    map.set('/api/watchlist', okJson({ items: [] }));
    map.set('/api/positions', okJson({ items: [] }));
    map.set('/api/radar', okJson({ signals: [] }));
    map.set('/api/portfolio', okJson({ profile: null, positions: [] }));
    return map;
  }

  if (SCENARIO === 'nulls') {
    const ov = overview('LIVE');
    ov.top_opportunities[0].quote = quote(null, null, 'UNKNOWN', null);           // null 最新价 → 必须 '—'
    ov.risk_events[0].risk_score = null;                                           // null 风险分
    ov.sector_leaders = SECTORS.map((s) => Object.assign({}, s, { crowding: null })); // null 拥挤度
    map.set('/api/overview', okJson(ov));
    const wl = [
      watchItem('600000.SH', '浦发银行', scores(72, 60, 45, 58), 'WATCH'),
      watchItem('600036.SH', '招商银行', null, 'WATCH')
    ];
    wl[1].quote = quote(null, null, 'UNKNOWN', null);
    wl[1].score = null;                                                            // null 四分数 → 「暂无评分」
    map.set('/api/watchlist', okJson({ items: wl }));
    map.set('/api/positions', okJson({ items: [positionItem('601319.SH', '中国人民保险集团', 10.5, 1000)] }));
    const rs = radarSignal('WATCH', '600000.SH', '浦发银行', null);
    rs.score = null;                                                               // null 分数
    map.set('/api/radar', okJson({ signals: [rs] }));
    map.set('/api/brief/today', okJson(briefToday()));
    return map;
  }

  if (SCENARIO === 'longnames') {
    const ov = overview('LIVE');
    ov.top_opportunities[0] = Object.assign({}, ov.top_opportunities[0], { name: LONG_NAME, symbol: '600000.SH' });
    ov.holding_signals[0] = Object.assign({}, ov.holding_signals[0], { name: LONG_NAME });
    ov.risk_events[0] = Object.assign({}, ov.risk_events[0], { symbol: '600000.SH', reason: LONG_NAME + '·合成风险理由长文本验证换行与遮挡行为' });
    map.set('/api/overview', okJson(ov));
    map.set('/api/watchlist', okJson({ items: [watchItem('600000.SH', LONG_NAME, null, 'WATCH')] }));
    map.set('/api/positions', okJson({ items: [positionItem('601319.SH', LONG_NAME, 10.5, 1000)] }));
    map.set('/api/radar', okJson({ signals: [radarSignal('ARMED_BREAKOUT', '600000.SH', LONG_NAME)] }));
    map.set('/api/brief/today', okJson(briefToday()));
    return map;
  }

  if (SCENARIO === 'servererror') {
    // 公开 /api/markets 返回 500（非 error/auth 场景白名单，属意外错误端点）→ runner 应非零
    map.set('HEALTH', healthPayload('ONLINE', 'LIVE'));
    map.set('/api/markets', { status: 500, body: { error: { code: 'INTERNAL', message: 'wb3 mock: markets down' } } });
    map.set('/api/provider_health', okJson({ providers: PROVIDERS }));
    map.set('/api/sectors', okJson({ sectors: SECTORS }));
    map.set('/api/config', okJson({}));
    map.set('/api/portfolio', okJson(portfolioPayload()));
    map.set('/api/brief/today', okJson(briefToday()));
    map.set('/api/overview', okJson(overview('LIVE')));
    map.set('/api/watchlist', okJson({ items: [watchItem('600000.SH', '浦发银行', scores(72, 60, 45, 58), 'WATCH')] }));
    map.set('/api/positions', okJson({ items: [positionItem('601319.SH', '中国人民保险集团', 10.5, 1000)] }));
    map.set('/api/radar', okJson({ signals: [radarSignal('TRIGGERED', '600000.SH', '浦发银行')] }));
    monitorEndpoints(map, 'full');
    return map;
  }

  // full（broken/locked 也走此 API 路径，静态层再注入坏页面/锁缩放）
  map.set('/api/overview', okJson(overview('LIVE')));
  map.set('/api/watchlist', okJson({
    items: [
      watchItem('600000.SH', '浦发银行', scores(72, 60, 45, 58), 'WATCH'),
      watchItem('600519.SH', '贵州茅台', scores(55, 48, 52, 50), 'ARMED_PULLBACK'),
      watchItem('601319.SH', '中国人民保险集团', scores(66, 58, 38, 62), 'TRIGGERED')
    ]
  }));
  map.set('/api/positions', okJson({
    items: [
      positionItem('601319.SH', '中国人民保险集团', 10.5, 1000),
      positionItem('600036.SH', '招商银行', 38.2, 400)
    ]
  }));
  map.set('/api/radar', okJson({
    signals: [
      radarSignal('TRIGGERED', '600000.SH', '浦发银行'),
      radarSignal('ARMED_PULLBACK', '600519.SH', '贵州茅台', scores(58, 52, 44, 55)),
      radarSignal('ARMED_BREAKOUT', '300750.SZ', '宁德时代', scores(70, 64, 50, 60)),
      radarSignal('OVEREXTENDED', '601899.SH', '紫金矿业', scores(75, 70, 62, 58)),
      radarSignal('WATCH', '600887.SH', '伊利股份', scores(45, 40, 35, 42)),
      radarSignal('COLD', '601888.SH', '中国中免', scores(30, 28, 30, 35)),
      radarSignal('DATA_INVALID', '688981.SH', '中芯国际', scores(50, 45, 55, 30)),
      radarSignal('INVALIDATED', '601012.SH', '隆基绿能', scores(20, 18, 60, 25))
    ]
  }));
  return map;
}

function monitorEndpoints(map, scenario) {
  if (scenario === 'empty') {
    map.set('/api/monitor/summary', okJson({ monitor: { outbox_by_state: {} }, data_link: {} }));
    map.set('/api/monitor/data-link', okJson(monitorDataLink()));
    map.set('/api/monitor/rules', okJson({ rules: [] }));
    map.set('/api/monitor/inbox', okJson({ inbox: [] }));
    map.set('/api/monitor/outbox', okJson({ outbox: [] }));
    return;
  }
  map.set('/api/monitor/summary', okJson(monitorSummary()));
  map.set('/api/monitor/data-link', okJson(monitorDataLink()));
  map.set('/api/monitor/rules', okJson({ rules: RULES }));
  map.set('/api/monitor/inbox', okJson({ inbox: INBOX }));
  map.set('/api/monitor/outbox', okJson({ outbox: [] }));
}

/* ---------------- HTTP 服务 ---------------- */
const ROUTES = buildScenario();

function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

const server = http.createServer((req, res) => {
  res.on('error', () => {});
  req.on('error', () => {});
  const u = new URL(req.url, 'http://' + HOST + ':' + PORT);
  const p = u.pathname;

  if (p === '/api/runtime/health') return sendJson(res, 200, ROUTES.get('HEALTH'));
  if (p === '/api/stream') {   // SSE hold：保持连接但不发事件
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
    res.write(': wb3 synthetic stream\n\n');
    return;
  }
  if (ROUTES.has(p)) {
    const r = ROUTES.get(p);
    return sendJson(res, r.status, r.body);
  }
  if (p.indexOf('/api/') === 0) return sendJson(res, 404, { error: { code: 'NOT_FOUND', message: 'wb3 mock：未知端点 ' + p } });

  // 静态文件
  let fp = decodeURIComponent(p);
  if (fp === '/') fp = '/index.html';
  const filePath = path.normalize(path.join(WEB, fp));
  if (filePath.indexOf(WEB) !== 0) { res.writeHead(403); return res.end('forbidden'); }
  fs.readFile(filePath, (err, buf) => {
    if (err) { res.writeHead(404, { 'Content-Type': 'text/plain' }); return res.end('not found'); }
    let body = buf;
    if (fp === '/index.html' && SCENARIO === 'broken') {
      body = Buffer.from(String(buf).replace('</body>',
        '<script>setTimeout(function(){ throw new Error("wb3-deliberate-broken-page"); }, 0);</script></body>'));
    } else if (fp === '/index.html' && SCENARIO === 'locked') {
      body = Buffer.from(String(buf).replace(
        'content="width=device-width, initial-scale=1.0"',
        'content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"'));
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream' });
    res.end(body);
  });
});

server.listen(PORT, HOST, () => {
  console.log('WB3_READY ' + JSON.stringify({scenario: SCENARIO, port: server.address().port,
    runId: process.env.WB3_RUN_ID || null, pid: process.pid}));
});
