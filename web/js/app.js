/* =========================================================================
 * app.js —— 启动 / 路由 / 初始拉取 / SSE 增量更新 / 信号详情弹层
 * 对齐 architecture.md §9：fetch REST + 带私有 Header 的 fetch-stream SSE。
 * 防御：每个 API 响应做空值防御；初始拉取失败显示"连接后端失败"，不白屏；
 *       SSE 断线由 fetch-stream 客户端退避重连，回调异常被隔离。
 * ========================================================================= */
(function () {
  'use strict';

  const F = window.Fmt;
  const UI = window.UI;
  const Runtime = window.Runtime;
  const API = window.API;
  const SSE = window.SSE;

  const PAGES = ['today', 'overview', 'watch', 'radar', 'research'];

  const state = {
    market: 'A',
    runtime: null,
    meta: null,
    brief: null,
    portfolio: null,
    privateError: null,
    portfolioBusy: false,
    overview: null,
    markets: [],
    watchlist: [],
    positions: [],
    radar: [],
    sectors: [],
    providers: [],
    config: {},
    loaded: { overview: false, markets: false, watchlist: false, positions: false, radar: false, sectors: false, providers: false }
  };

  /* ---------------- DOM 工具 ---------------- */
  const $ = function (sel) { return document.querySelector(sel); };
  const $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  let toastTimer = null;
  let toastHideTimer = null;
  let todayRefreshTimer = null;
  let healthPollTimer = null;
  let runtimeReloading = false;
  let runtimeClosingSse = false;
  let sseSubscribed = false;
  let dataLoadPromise = null;
  function hideToast() {
    const t = $('#toast');
    if (!t) return;
    t.classList.remove('show');          // 对称退场：沿同一边缘下滑淡出
    toastHideTimer = setTimeout(function () {
      if (!t.classList.contains('show')) t.hidden = true;
    }, 260);
    toastTimer = null;
  }
  function toast(msg) {
    const t = $('#toast');
    if (!t) return;
    clearTimeout(toastHideTimer);
    t.textContent = msg;
    t.hidden = false;
    void t.offsetWidth;                 // 强制 reflow，确保入场过渡触发
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 1400);
  }

  /* ---------------- Hybrid H1/H2 Runtime 状态 ---------------- */
  const RUNTIME_LABELS = {
    ONLINE: 'Engine online',
    DEGRADED: 'Engine degraded',
    STALE: 'Data stale',
    ENGINE_OFFLINE: 'Engine offline',
    NETWORK_OFFLINE: 'Network offline',
    AUTH_REQUIRED: 'Access required',
    AUTH_FAILED: 'Access rejected',
    CORS_BLOCKED: 'CORS blocked',
    API_VERSION_MISMATCH: 'API mismatch',
    ENGINE_ID_MISMATCH: 'Engine mismatch',
    BUILD_MISMATCH: 'Build mismatch',
    TUNNEL_UNAVAILABLE: 'Tunnel unavailable',
    RUNTIME_CONFIG_ERROR: 'Runtime config error',
    RUNTIME_HEALTH_INVALID: 'Runtime health invalid'
  };

  function effectiveRuntimeStatus(snapshot) {
    const current = snapshot || state.runtime || {};
    if (Runtime && (Runtime.isHardFailure(current.status) || current.status === 'STALE')) {
      return current.status;
    }
    return current.authState || current.status || 'ENGINE_OFFLINE';
  }

  function renderRuntimeStatus() {
    const el = $('#runtimeStatus');
    if (!el) return;
    const snapshot = state.runtime || (Runtime ? Runtime.snapshot() : null) || {};
    const status = effectiveRuntimeStatus(snapshot);
    const health = snapshot.health || {};
    let detail = snapshot.detail || '';
    if (status === 'AUTH_REQUIRED') detail = '当前 API Origin 需要会话访问值';
    if (status === 'AUTH_FAILED') detail = '当前 API Origin 的会话访问值未通过验证';
    const bits = [];
    if (snapshot.apiOrigin) bits.push('API ' + snapshot.apiOrigin);
    if (health.engine_id) bits.push('Engine ' + health.engine_id);
    if (health.api_major != null) bits.push('API v' + health.api_major);
    if (health.commit_id) bits.push('Backend ' + health.commit_id);
    if (snapshot.frontendBuild) bits.push('Frontend ' + snapshot.frontendBuild);
    if (health.data_as_of) bits.push('Data ' + health.data_as_of);
    if (snapshot.sseConnected) bits.push('SSE online');
    el.className = 'runtime-status runtime-' + status.toLowerCase().replace(/_/g, '-');
    el.dataset.runtimeState = status;
    el.innerHTML = '<span class="runtime-status-dot"></span>' +
      '<span class="runtime-status-label">' + F.esc(RUNTIME_LABELS[status] || status) + '</span>' +
      '<span class="runtime-status-detail">' + F.esc(detail) + '</span>' +
      (bits.length ? '<span class="runtime-status-meta">' + F.esc(bits.join(' · ')) + '</span>' : '');
  }

  function clearDecisionData() {
    state.meta = null;
    state.brief = null;
    state.overview = null;
    state.watchlist = [];
    state.positions = [];
    state.radar = [];
    state.loaded.overview = false;
    state.loaded.watchlist = false;
    state.loaded.positions = false;
    state.loaded.radar = false;
  }

  function clearRuntimeData(error) {
    state.meta = null;
    state.brief = null;
    state.portfolio = null;
    state.privateError = error || null;
    state.overview = null;
    state.markets = [];
    state.watchlist = [];
    state.positions = [];
    state.radar = [];
    state.sectors = [];
    state.providers = [];
    state.config = {};
    Object.keys(state.loaded).forEach(function (key) { state.loaded[key] = false; });
  }

  function renderAllRuntimeViews() {
    renderRuntimeStatus();
    renderBanner();
    renderToday();
    renderOverview();
    renderWatch();
    renderRadar();
    renderHolding();
  }

  function handleRuntimeSnapshot(snapshot) {
    state.runtime = snapshot;
    renderRuntimeStatus();
    if (!Runtime) return;
    if (snapshot.status === 'STALE') {
      clearDecisionData();
      renderToday();
      renderOverview();
      renderWatch();
      renderRadar();
      return;
    }
    if (!Runtime.isHardFailure(snapshot.status)) return;
    const error = {
      code: snapshot.status,
      message: snapshot.detail || '本地决策引擎当前不可用'
    };
    clearRuntimeData(error);
    if (sseSubscribed && !runtimeClosingSse && SSE && typeof SSE.close === 'function') {
      runtimeClosingSse = true;
      try { SSE.close(); } finally { runtimeClosingSse = false; }
    }
    renderAllRuntimeViews();
  }

  async function ensureRuntimeHealth() {
    if (!Runtime) return false;
    try {
      await Runtime.refreshHealth();
      return true;
    } catch (error) {
      try {
        await Runtime.classifyHealthFailure(error);
      } catch (classificationError) {
        console.error('[runtime] health classification failed', classificationError);
      }
      return false;
    }
  }

  function ensureSSEConnection() {
    if (!SSE) return;
    if (!sseSubscribed) {
      subscribeSSE();
      sseSubscribed = true;
    } else if (typeof SSE.reconnect === 'function') {
      SSE.reconnect();
    }
  }

  function scheduleHealthPoll() {
    clearTimeout(healthPollTimer);
    if (!Runtime) return;
    let delay = 15000;
    try { delay = Runtime.config().healthPollMs; } catch (error) { return; }
    healthPollTimer = setTimeout(async function () {
      healthPollTimer = null;
      const before = state.runtime || Runtime.snapshot();
      const wasBlocked = Runtime.isHardFailure(before.status) || before.status === 'STALE';
      const healthy = await ensureRuntimeHealth();
      if (healthy && wasBlocked && !runtimeReloading) {
        runtimeReloading = true;
        try {
          const dataReady = await loadInitial();
          if (dataReady) ensureSSEConnection();
        } finally {
          runtimeReloading = false;
        }
      }
      scheduleHealthPoll();
    }, delay);
  }

  /* ---------------- 路由 ---------------- */
  function setPage(name) {
    if (PAGES.indexOf(name) === -1) name = 'overview';
    PAGES.forEach(function (p) {
      const page = $('#page-' + p);
      if (page) page.classList.toggle('active', p === name);
    });
    $$('.nav-btn').forEach(function (b) { b.classList.toggle('active', b.dataset.page === name); });
    positionNavIndicator();
  }

  function setMarket(m) {
    state.market = m;
    $$('.market-tab').forEach(function (t) { t.classList.toggle('active', t.dataset.market === m); });
    positionMarketIndicator();
    // 重渲染所有受市场影响的区块；重点机会 topList 按当前市场过滤
    const idx = $('#indexGrid'); if (idx) idx.innerHTML = UI.renderIndexGrid(state.markets, m);
    renderWatch();
    renderRadar(); // 雷达可按需过滤，这里不过滤保持全量；自选/持仓按市场过滤
    renderTopList(); // 重点机会列表随市场切换刷新（按 market 过滤）
  }

  /* ---------------- 弹性滑动指示（分段控件 / 底部导航） ---------------- */
  function positionMarketIndicator() {
    const wrap = $('#marketTabs');
    if (!wrap) return;
    let ind = wrap.querySelector('.market-tabs-indicator');
    if (!ind) {
      ind = document.createElement('span');
      ind.className = 'market-tabs-indicator';
      wrap.appendChild(ind);
    }
    const active = wrap.querySelector('.market-tab.active');
    if (!active) return;
    ind.style.width = active.offsetWidth + 'px';
    ind.style.transform = 'translateX(' + active.offsetLeft + 'px)';
  }
  function positionNavIndicator() {
    const nav = $('.bottom-nav');
    if (!nav) return;
    let ind = nav.querySelector('.nav-indicator');
    if (!ind) {
      ind = document.createElement('span');
      ind.className = 'nav-indicator';
      nav.appendChild(ind);
    }
    const active = nav.querySelector('.nav-btn.active');
    if (!active) return;
    ind.style.width = active.offsetWidth + 'px';
    ind.style.transform = 'translateX(' + active.offsetLeft + 'px)';
  }
  function initIndicators() {
    positionMarketIndicator();
    positionNavIndicator();
    // 双 rAF：等首帧布局稳定后再校正一次，消除初值偏差
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { positionMarketIndicator(); positionNavIndicator(); });
    });
    // 字体就绪后再次校正（字体影响 Tab 文字宽度）
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () { positionMarketIndicator(); positionNavIndicator(); });
    }
    let rAF = null;
    window.addEventListener('resize', function () {
      if (rAF) cancelAnimationFrame(rAF);
      rAF = requestAnimationFrame(function () { positionMarketIndicator(); positionNavIndicator(); });
    });
  }

  /* ---------------- 初始拉取 ---------------- */
  function ok(r) { return r && r.status === 'fulfilled' ? r.value : null; }
  function failure(r) { return r && r.status === 'rejected' ? r.reason : null; }

  function clearPrivateDecisionState(error) {
    state.brief = null;
    state.portfolio = null;
    state.privateError = error || null;
    state.overview = null;
    state.watchlist = [];
    state.positions = [];
    state.radar = [];
    state.config = {};
    state.meta = null;
  }

  async function refreshRuntimeHealth() {
    return ensureRuntimeHealth();
  }

  function applyPrivateFailure(error) {
    if (!error || !Runtime) return false;
    state.privateError = error;
    const code = error.code || '';
    if (error.status === 401 || error.status === 403 ||
        code === 'PRIVATE_API_AUTH_REQUIRED' || code === 'PRIVATE_API_DISABLED' ||
        code === 'PRIVATE_API_MISCONFIGURED') {
      Runtime.noteAuthError(error);
      return true;
    }
    return false;
  }

  async function loadInitial() {
    if (!Runtime) {
      clearRuntimeData({ code: 'RUNTIME_MISSING', message: 'Runtime 模块未加载' });
      renderAllRuntimeViews();
      return false;
    }
    const ready = await refreshRuntimeHealth();
    if (!ready) {
      renderAllRuntimeViews();
      return false;
    }
    return loadDataOnce();
  }

  async function loadApplicationData() {
    const results = await Promise.allSettled([
      API.getBriefToday(), API.getPortfolio(), API.getOverview(), API.getMarkets(),
      API.getWatchlist(), API.getPositions(), API.getRadar(), API.getSectors(),
      API.getProviderHealth(), API.getConfig()
    ]);
    state.brief = ok(results[0]);
    state.portfolio = ok(results[1]);
    state.privateError = failure(results[1]) || failure(results[0]) ||
      failure(results[2]) || failure(results[4]) || failure(results[5]) ||
      failure(results[6]) || failure(results[9]);
    state.overview = ok(results[2]);
    state.markets = ok(results[3]) || [];
    state.watchlist = ok(results[4]) || [];
    state.positions = ok(results[5]) || [];
    state.radar = ok(results[6]) || [];
    state.sectors = ok(results[7]) || [];
    state.providers = ok(results[8]) || [];
    state.config = ok(results[9]) || {};

    if (state.overview) state.meta = state.overview.meta || null;

    if (state.privateError) {
      applyPrivateFailure(state.privateError);
    }

    // 横幅：真实/降级/演示/失败 可见
    renderBanner();
    renderHolding();
    if (!state.meta && !state.markets.length) {
      const banner = $('#banner');
      banner.className = 'banner error';
      banner.innerHTML = '<span class="banner-dot"></span><span class="banner-mode">连接后端失败</span>' +
        '<span class="banner-text">未能从配置的 API Origin 取得市场数据；请检查 Engine、私有通道、CORS 与认证状态。</span>';
    }

    // 渲染全部页面（切换 tab 即时显示）
    renderRuntimeStatus();
    renderToday();
    renderOverview();
    renderWatch();
    renderRadar();
    return !state.privateError;
  }

  function loadDataOnce() {
    if (dataLoadPromise) return dataLoadPromise;
    dataLoadPromise = loadApplicationData().finally(function () { dataLoadPromise = null; });
    return dataLoadPromise;
  }

  function scheduleRuntimeHealthPoll() {
    scheduleHealthPoll();
  }

  async function recoverRuntime() {
    const before = state.runtime || (Runtime ? Runtime.snapshot() : null) || {};
    const ready = await refreshRuntimeHealth();
    if (ready) {
      let dataReady = !state.privateError;
      if (Runtime.isHardFailure(before.status) || before.status === 'STALE' || !dataReady) {
        dataReady = await loadDataOnce();
      }
      if (dataReady) ensureSSEConnection();
    } else {
      renderAllRuntimeViews();
    }
  }

  function initRuntimeMonitoring() {
    if (!Runtime) return;
    state.runtime = Runtime.snapshot();
    Runtime.onChange(handleRuntimeSnapshot);
    renderRuntimeStatus();
    window.addEventListener('offline', function () {
      Runtime.classifyHealthFailure({ code: 'NETWORK_OFFLINE' }).catch(function () {});
    });
    window.addEventListener('online', function () { recoverRuntime(); });
    scheduleRuntimeHealthPoll();
  }

  /* ---------------- 渲染：⓪ 今日作战简报（Stage 1 Lane D） ---------------- */
  function renderPortfolioPanel() {
    const el = $('#portfolioPanel');
    if (!el) return;
    const P = window.PortfolioUI;
    if (!P) {
      el.innerHTML = UI.loadingBox('账户管理模块未加载');
      return;
    }
    el.innerHTML = P.renderPanel(
      state.portfolio,
      state.privateError,
      state.portfolioBusy
    );
  }

  function renderToday() {
    renderPortfolioPanel();
    const el = $('#todayBrief');
    if (!el) return;
    const T = window.Today;
    if (!T) { el.innerHTML = UI.loadingBox('今日简报模块未加载'); return; }
    if (state.brief) {
      el.innerHTML = T.render(state.brief);
    } else if (state.overview) {
      // 兼容降级：旧 /api/overview 合同，明确标旧，不生成新字段
      el.innerHTML = T.renderLegacy(state.overview);
    } else {
      el.innerHTML = UI.loadingBox('今日作战简报加载失败：/api/brief/today 与 /api/overview 均不可用。');
    }
  }

  /* ---------------- 渲染：横幅 ---------------- */
  function renderBanner() {
    const banner = $('#banner');
    if (!banner) return;
    const closed = UI.isAllMarketsClosed(state.meta);
    banner.className = 'banner ' + UI.bannerModeClass(state.meta) + (closed ? ' closed' : '');
    banner.innerHTML = UI.renderBanner(state.meta, state.providers);
  }

  /* ---------------- 渲染：收市态中长线持仓信号面板 ---------------- */
  function renderHolding() {
    const panel = $('#holdingPanel');
    if (!panel) return;
    const closed = UI.isAllMarketsClosed(state.meta);
    if (!closed) {
      // 交易时段：隐藏收市面板，避免与实时机会列表重复。
      panel.hidden = true;
      panel.innerHTML = '';
      return;
    }
    panel.hidden = false;
    const sigs = (state.overview && state.overview.holding_signals) || [];
    panel.innerHTML =
      '<div class="closed-banner">🌙 已收市 · 中长线持仓信号</div>' +
      '<div class="hl-wrap">' + UI.renderHoldingSignals(sigs) + '</div>';
  }

  /* ---------------- 渲染：① 市场总览 ---------------- */
  function renderOverview() {
    const idx = $('#indexGrid');
    if (idx) idx.innerHTML = UI.renderIndexGrid(state.markets, state.market);

    const grid = $('#overviewGrid');
    if (grid) {
      const ov = state.overview || {};
      const sectors = ov.sector_leaders || state.sectors;
      const parts = [
        UI.renderRegimeCard(ov.regime),
        UI.renderSectorCard(sectors),
        UI.renderBreadthCard(ov.breadth),
        UI.renderRiskCard(ov.risk_events),
        UI.renderProviderHealthCard(state.providers)
      ];
      grid.innerHTML = parts.join('');
    }

    renderTopList();
    renderHolding();
  }

  /* ---------------- 渲染：①b 重点机会（按市场过滤） ---------------- */
  function renderTopList() {
    const el = $('#topList');
    if (!el) return;
    const items = (state.overview && state.overview.top_opportunities) || state.radar || [];
    el.innerHTML = UI.renderTopList(items, state.market);
  }

  /* ---------------- 渲染：② 自选 / 持仓 ---------------- */
  function renderWatch() {
    const sum = $('#watchSummary');
    if (sum) sum.innerHTML = UI.renderWatchSummary(state.watchlist, state.positions);
    const groups = $('#watchGroups');
    if (groups) groups.innerHTML = UI.renderWatchGroups(state.watchlist, state.market);
    const pos = $('#positionList');
    if (pos) pos.innerHTML = UI.renderPositionList(state.positions, state.market);
  }

  /* ---------------- 渲染：③ 机会雷达 ---------------- */
  function renderRadar() {
    const el = $('#radarGroups');
    if (el) el.innerHTML = UI.renderRadar(state.radar);
  }

  /* ---------------- 渲染当前页（SSE 增量后调用） ---------------- */
  function renderActive() {
    const active = $$('.page.active')[0];
    if (!active) return;
    const id = active.id;
    if (id === 'page-today') renderToday();
    else if (id === 'page-overview') renderOverview();
    else if (id === 'page-watch') renderWatch();
    else if (id === 'page-radar') renderRadar();
  }

  /* ---------------- 信号详情弹层 ---------------- */
  async function openSignal(id) {
    if (!id) return;
    const mask = $('#sheetMask');
    const sheet = $('#sheet');
    if (!mask || !sheet) return;
    // 兼容：id 可能是 signal_id（雷达/自选/持仓卡片）或 symbol（重点机会卡片）。
    const cached = lookupSignal(id);
    sheet.innerHTML = UI.renderSignalDetail(cached) ||
      '<div class="sheet-body"><div class="loading-box">加载信号详情…</div></div>' +
      '<div class="sheet-footer"><button class="sheet-close" id="sheetClose">关闭</button></div>';
    openSheetMask(mask);
    bindSheetClose();
    // 缓存信号带 signal_id 时，从 REST 取最新完整详情（含入场/止损/目标/history）
    if (cached && cached.signal_id) {
      try {
        const fresh = await API.getSignal(cached.signal_id);
        if (fresh) {
          sheet.innerHTML = UI.renderSignalDetail(fresh);
          bindSheetClose();
        }
      } catch (e) {
        // 保留缓存展示，静默失败（缓存已含基础信息）
      }
    }
    // K 线指标详情：在信号详情定稿后追加，避免被上方 innerHTML 覆盖（展示增强）
    let qSym = (typeof id === 'string' && id.indexOf('.') >= 0) ? id : null;
    if (!qSym && cached && cached.symbol) qSym = cached.symbol;
    if (qSym) { try { await openQuote(qSym); } catch (e) { /* 展示增强，静默 */ } }
  }

  /** 拉取并渲染单标的 K 线指标 + 近期历史（详情面板增强，失败不影响信号详情）。 */
  async function openQuote(symbol) {
    if (!symbol) return;
    const sheet = $('#sheet');
    if (!sheet) return;
    const box = document.createElement('div');
    box.className = 'quote-panel';
    box.innerHTML = '<div class="ind-empty">加载 K 线指标…</div>';
    sheet.appendChild(box);
    try {
      const d = await API.getQuote(symbol);
      if (!d) { box.remove(); return; }
      box.innerHTML = renderQuotePanel(d);
    } catch (e) {
      box.innerHTML = '<div class="ind-empty">指标加载失败（展示增强，不影响信号详情）</div>';
    }
  }

  /** 由 /api/quote/{symbol} 响应渲染指标 + 近期 K 线表（所有动态文本经 F.esc 防 XSS）。 */
  function renderQuotePanel(d) {
    const name = d.name || d.symbol || '—';
    const ind = d.indicators ? UI.renderIndicators(d.indicators) : '<div class="ind-empty">暂无指标</div>';
    const bars = Array.isArray(d.recent_bars) ? d.recent_bars : [];
    const rows = bars.slice().reverse().map(function (b) {
      return '<tr>' +
        '<td>' + F.esc(b.timestamp ? String(b.timestamp).slice(0, 10) : '') + '</td>' +
        '<td>' + F.num(b.open) + '</td>' +
        '<td>' + F.num(b.high) + '</td>' +
        '<td>' + F.num(b.low) + '</td>' +
        '<td>' + F.num(b.close) + '</td>' +
        '<td>' + F.num(b.volume) + '</td>' +
        '</tr>';
    }).join('');
    return '<div class="quote-panel-head">K线指标 · ' + F.esc(name) +
      ' <span class="quote-count">' + (d.bar_count || 0) + ' 根</span></div>' +
      ind +
      (rows ? '<div class="qb-scroll"><table class="qb-table"><thead><tr>' +
        '<th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>量</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>' : '');
  }

  function findSignalById(id) {
    let found = null;
    (state.radar || []).forEach(function (s) { if (s.signal_id === id) found = s; });
    if (found) return found;
    (Array.isArray(state.watchlist) ? state.watchlist : []).forEach(function (it) { if (it.signal && it.signal.signal_id === id) found = it.signal; });
    (Array.isArray(state.positions) ? state.positions : []).forEach(function (p) { if (p.signal && p.signal.signal_id === id) found = p.signal; });
    if ((state.overview && state.overview.top_opportunities)) {
      (state.overview.top_opportunities || []).forEach(function (s) { if (s.signal_id === id) found = s; });
    }
    return found;
  }

  /** 按 signal_id 或 symbol 查找信号对象（重点机会卡片仅带 symbol）。
   *  优先返回带 signal_id 的完整对象（来自 radar/watchlist/positions），便于后续 REST 取详情。 */
  function lookupSignal(idOrSymbol) {
    if (!idOrSymbol) return null;
    const byId = findSignalById(idOrSymbol);
    if (byId) return byId;
    const wanted = String(idOrSymbol);
    const cands = [];
    (state.radar || []).forEach(function (s) { if (s.symbol === wanted) cands.push(s); });
    (Array.isArray(state.watchlist) ? state.watchlist : []).forEach(function (it) { if (it.symbol === wanted && it.signal) cands.push(it.signal); });
    (Array.isArray(state.positions) ? state.positions : []).forEach(function (p) { if (p.symbol === wanted && p.signal) cands.push(p.signal); });
    if (state.overview && state.overview.top_opportunities) {
      (state.overview.top_opportunities || []).forEach(function (s) { if (s.symbol === wanted) cands.push(s); });
    }
    for (let i = 0; i < cands.length; i++) { if (cands[i].signal_id) return cands[i]; }
    return cands.length ? cands[0] : null;
  }

  function bindSheetClose() {
    const btn = $('#sheetClose');
    if (btn) btn.onclick = closeSheet;
  }
  let sheetCloseTimer = null;
  function closeSheet() {
    const mask = $('#sheetMask');
    if (!mask || mask.hidden || mask.classList.contains('closing')) return;
    mask.classList.add('closing');              // 触发退场过渡（遮罩淡出 + 弹层下滑）
    clearTimeout(sheetCloseTimer);
    sheetCloseTimer = setTimeout(function () {
      mask.hidden = true;
      mask.classList.remove('closing');
    }, 340);
  }
  function openSheetMask(mask) {
    if (!mask) return;
    clearTimeout(sheetCloseTimer);
    mask.classList.remove('closing');           // 复位退场态，确保新弹层正常入场
    mask.hidden = false;
  }

  /* ---------------- Stage 1.1：Portfolio 私有 CRUD ---------------- */
  function openPortfolioSheet() {
    const P = window.PortfolioUI;
    const mask = $('#sheetMask');
    const sheet = $('#sheet');
    if (!P || !mask || !sheet) return;
    sheet.innerHTML = P.renderSheet(state.portfolio, state.privateError);
    openSheetMask(mask);
    bindSheetClose();
  }

  function setFormBusy(form, busy) {
    if (!form) return;
    form.querySelectorAll('button, input, select').forEach(function (field) {
      field.disabled = busy;
    });
  }

  function portfolioErrorMessage(error) {
    const P = window.PortfolioUI;
    if (P && P.privateErrorText) {
      const mapped = P.privateErrorText(error);
      if (mapped) return mapped;
    }
    return (error && error.message) || '账户操作失败';
  }

  async function refreshPrivateData(reopenSheet) {
    state.portfolioBusy = true;
    renderPortfolioPanel();
    const results = await Promise.allSettled([
      API.getPortfolio(),
      API.getBriefToday(),
      API.getPositions(),
      API.getOverview(),
      API.getRadar(),
      API.getConfig(),
      API.getWatchlist()
    ]);
    state.portfolio = ok(results[0]);
    state.brief = ok(results[1]);
    state.positions = ok(results[2]) || [];
    state.overview = ok(results[3]);
    state.radar = ok(results[4]) || [];
    state.config = ok(results[5]) || {};
    state.watchlist = ok(results[6]) || [];
    state.meta = state.overview ? (state.overview.meta || null) : null;
    state.privateError = failure(results[0]) || failure(results[1]) ||
      failure(results[2]) || failure(results[3]) || failure(results[4]) ||
      failure(results[5]) || failure(results[6]);
    if (state.privateError) applyPrivateFailure(state.privateError);
    state.portfolioBusy = false;
    renderBanner();
    renderOverview();
    renderRadar();
    renderToday();
    renderWatch();
    renderHolding();
    renderRuntimeStatus();
    if (reopenSheet) openPortfolioSheet();
    return !state.privateError;
  }

  async function runPortfolioTask(form, task, successMessage) {
    if (state.portfolioBusy) return;
    state.portfolioBusy = true;
    setFormBusy(form, true);
    renderPortfolioPanel();
    try {
      await task();
      const refreshed = await refreshPrivateData(true);
      toast(refreshed ? successMessage : successMessage + '，但页面刷新失败');

    } catch (error) {
      toast(portfolioErrorMessage(error));
      state.portfolioBusy = false;
    renderBanner();
    renderOverview();
    renderRadar();
      setFormBusy(form, false);
      renderPortfolioPanel();
    }
  }

  async function submitPrivateAccess(form) {
    const field = form.elements.namedItem('private_access');
    const value = field ? String(field.value || '') : '';
    if (!value) {
      toast('请输入当前会话私有访问值');
      return;
    }
    try {
      API.setPrivateAccess(value);
      const connected = await refreshPrivateData(true);
      if (connected && SSE && typeof SSE.reconnect === 'function') SSE.reconnect();
      toast(connected ? '私有接口已连接' : '私有访问值未通过验证');
    } catch (error) {
      toast(portfolioErrorMessage(error));
    }
  }

  function confirmDelete(button) {
    if (button.dataset.confirming === 'true') return true;
    button.dataset.confirming = 'true';
    button.classList.add('pf-delete-confirming');
    button.textContent = '再次点击确认删除';
    setTimeout(function () {
      if (!button.isConnected) return;
      button.dataset.confirming = 'false';
      button.classList.remove('pf-delete-confirming');
      button.textContent = '删除记录';
    }, 5000);
    return false;
  }

  function scheduleTodayRefresh(delayMs) {
    clearTimeout(todayRefreshTimer);
    todayRefreshTimer = setTimeout(async function () {
      todayRefreshTimer = null;
      try {
        state.brief = await API.getBriefToday();
        state.privateError = null;
        renderRuntimeStatus();
        renderToday();
      } catch (error) {
        state.privateError = error;
        if (applyPrivateFailure(error)) {
          state.brief = null;
          state.portfolio = null;
          state.positions = [];
          state.watchlist = [];
          state.overview = null;
          state.radar = [];
          state.config = {};
          state.meta = null;
        }
        renderRuntimeStatus();
        renderToday();
      }
    }, delayMs || 400);
  }

  /* ---------------- SSE：行情增量（定向更新，避免整页重绘闪烁） ---------------- */
  function paintQuote(q) {
    if (!q || !q.symbol) return;
    const sym = q.symbol;
    const price = F.quotePrice(q);
    const chg = F.quoteChangePct(q);
    const chgTxt = F.fmtPct(chg);
    const chgCls = F.chgClass(chg);
    const statusHtml = F.statusBadge(q.data_status, q.observed_age_ms);

    $$('.live-price[data-symbol="' + sym + '"]').forEach(function (el) { el.textContent = price; });
    $$('.live-chg[data-symbol="' + sym + '"]').forEach(function (el) {
      el.textContent = chgTxt;
      el.classList.remove('up', 'down', 'flat');
      el.classList.add(chgCls);
    });
    // 保留 data-symbol：只替换内部 HTML，不替换元素本身
    Array.prototype.slice.call(document.querySelectorAll('.live-status[data-symbol="' + sym + '"]')).forEach(function (el) { el.innerHTML = statusHtml; });
    scheduleTodayRefresh(500);
  }

  function updateSignalInCaches(sig) {
    if (!sig || !sig.signal_id) return;
    // radar
    let replaced = false;
    state.radar = (state.radar || []).map(function (s) {
      if (s.signal_id === sig.signal_id) { replaced = true; return sig; }
      return s;
    });
    if (!replaced) state.radar = (state.radar || []).concat([sig]);
    // watchlist 中的 signal 摘要
    (Array.isArray(state.watchlist) ? state.watchlist : []).forEach(function (it) {
      if (it.signal && it.signal.signal_id === sig.signal_id) it.signal = sig;
    });
    // positions 中的 signal
    (Array.isArray(state.positions) ? state.positions : []).forEach(function (p) {
      if (p.signal && p.signal.signal_id === sig.signal_id) p.signal = sig;
    });
  }

  function subscribeSSE() {
    SSE.subscribe({
      quote: function (q) { paintQuote(q); },
      signal: function (sig) { updateSignalInCaches(sig); renderActive(); scheduleTodayRefresh(200); },
      regime: function (r) { if (state.overview) state.overview.regime = r; renderActive(); scheduleTodayRefresh(200); },
      sector: function (payload) {
        const arr = Array.isArray(payload) ? payload : (payload && payload.sectors) || [];
        if (arr.length) { state.sectors = arr; if (state.overview) state.overview.sector_leaders = arr; }
        renderActive();
        scheduleTodayRefresh(200);
      },
      provider_health: function (arr) {
        if (Array.isArray(arr) && arr.length) state.providers = arr;
        else if (arr && arr.providers) state.providers = arr.providers;
        renderBanner();
      },
      open: function () { /* 连接建立，可选提示 */ },
      error: function (error) {
        if (error && (error.status === 401 || error.status === 403 ||
            error.code === 'PRIVATE_API_AUTH_REQUIRED' ||
            error.code === 'PRIVATE_API_AUTH_FAILED')) {
          Runtime.noteAuthError(error);
          clearPrivateDecisionState(error);
          renderAllRuntimeViews();
          return;
        }
        refreshRuntimeHealth();
      }
    });
  }

  /* ---------------- 事件绑定（委托，挂载一次） ---------------- */
  function bindEvents() {
    // 底部导航
    $$('.nav-btn').forEach(function (b) {
      b.addEventListener('click', function () { setPage(b.dataset.page); });
    });
    // 顶部市场切换
    $$('.market-tab').forEach(function (t) {
      t.addEventListener('click', function () { setMarket(t.dataset.market); });
    });
    // 页面级委托：Portfolio 操作优先，其次信号详情。
    document.addEventListener('click', function (e) {
      const portfolioOpen = e.target.closest('[data-portfolio-open]');
      if (portfolioOpen) {
        openPortfolioSheet();
        return;
      }
      const clearAccess = e.target.closest('[data-private-access-clear]');
      if (clearAccess) {
        API.clearPrivateAccess();
        if (SSE && typeof SSE.close === 'function') {
          SSE.close();
          sseSubscribed = false;
        }
        state.portfolio = null;
        state.brief = null;
        state.overview = null;
        state.radar = [];
        state.config = {};
        state.meta = null;
        state.positions = [];
        state.watchlist = [];
        refreshPrivateData(true).then(function (connected) {
          if (connected) ensureSSEConnection();
          toast(connected ? '已清除会话访问值，本机私有接口仍可用' : '已清除当前会话访问值');
        });
        return;
      }
      const deleteButton = e.target.closest('[data-position-delete]');
      if (deleteButton) {
        if (!confirmDelete(deleteButton)) return;
        const positionId = deleteButton.dataset.positionId;
        const form = deleteButton.closest('form');
        runPortfolioTask(
          form,
          function () { return API.deletePortfolioPosition(positionId); },
          '持仓记录已删除'
        );
        return;
      }
      const el = e.target.closest('[data-signal]');
      if (el && el.dataset.signal) openSignal(el.dataset.signal);
    });
    document.addEventListener('submit', function (e) {
      const form = e.target;
      const P = window.PortfolioUI;
      if (!P || !form) return;
      if (form.id === 'privateAccessForm') {
        e.preventDefault();
        submitPrivateAccess(form);
        return;
      }
      if (form.id === 'portfolioProfileForm') {
        e.preventDefault();
        try {
          const payload = P.readProfile(form);
          runPortfolioTask(
            form,
            function () { return API.putPortfolioProfile(payload); },
            '账户参数已保存'
          );
        } catch (error) {
          toast(portfolioErrorMessage(error));
        }
        return;
      }
      if (form.id === 'portfolioPositionCreateForm') {
        e.preventDefault();
        try {
          const payload = P.readNewPosition(form);
          runPortfolioTask(
            form,
            function () { return API.createPortfolioPosition(payload); },
            '持仓记录已新增'
          );
        } catch (error) {
          toast(portfolioErrorMessage(error));
        }
        return;
      }
      if (form.matches('[data-position-form]')) {
        e.preventDefault();
        try {
          const payload = P.readPositionPatch(form);
          const positionId = form.dataset.positionId;
          runPortfolioTask(
            form,
            function () { return API.patchPortfolioPosition(positionId, payload); },
            '持仓记录已更新'
          );
        } catch (error) {
          toast(portfolioErrorMessage(error));
        }
      }
    });
    // 弹层遮罩点击关闭
    const mask = $('#sheetMask');
    if (mask) mask.addEventListener('click', function (e) { if (e.target === mask) closeSheet(); });
    // ESC 关闭
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { const m = $('#sheetMask'); if (m && !m.hidden) closeSheet(); }
    });
  }

  /* ---------------- 主题切换（深/浅） ---------------- */
  function initTheme() {
    const KEY = 'stk-theme';
    const root = document.documentElement;
    let stored = null;
    try { stored = localStorage.getItem(KEY); } catch (e) { stored = null; }
    if (stored === 'light' || stored === 'dark') root.setAttribute('data-theme', stored);

    const btn = $('#themeToggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      const cur = root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      const next = cur === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
    });
  }

  /* ---------------- 轻触涟漪（移动端反馈） ---------------- */
  function initRipple() {
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const hosts = '.ripple-host, .market-tab, .nav-btn, .icon-btn, .sheet-close, .pf-primary, .pf-secondary, .pf-danger, .act-badge, .card, .opp-card, .wl-card, .radar-card, .hl-card, .pos-card, .tb-card, .tb-do';
    document.addEventListener('pointerdown', function (e) {
      if (e.button !== undefined && e.button !== 0) return;
      const el = e.target.closest(hosts);
      if (!el || el.disabled) return;
      const rect = el.getBoundingClientRect();
      if (!rect.width) return;
      const size = Math.max(rect.width, rect.height);
      const dot = document.createElement('span');
      dot.className = 'ripple-dot';
      dot.style.width = dot.style.height = size + 'px';
      dot.style.left = (e.clientX - rect.left - size / 2) + 'px';
      dot.style.top = (e.clientY - rect.top - size / 2) + 'px';
      el.appendChild(dot);
      setTimeout(function () { if (dot.parentNode) dot.parentNode.removeChild(dot); }, 560);
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    initTheme();
    initRipple();
    initIndicators();
    bindEvents();
    initRuntimeMonitoring();
    // 先渲染占位加载态，避免白屏
    const pp = $('#portfolioPanel'); if (pp) pp.innerHTML = UI.loadingBox('加载账户与持仓…');
    const tb = $('#todayBrief'); if (tb) tb.innerHTML = UI.loadingBox('加载今日作战简报…');
    const idx = $('#indexGrid'); if (idx) idx.innerHTML = UI.loadingBox('加载指数中…');
    const grid = $('#overviewGrid'); if (grid) grid.innerHTML = UI.loadingBox('加载市场状态中…');
    const top = $('#topList'); if (top) top.innerHTML = UI.loadingBox('加载机会中…');
    const wg = $('#watchGroups'); if (wg) wg.innerHTML = UI.loadingBox('加载自选…');
    const pl = $('#positionList'); if (pl) pl.innerHTML = UI.loadingBox('加载持仓…');
    const rg = $('#radarGroups'); if (rg) rg.innerHTML = UI.loadingBox('加载机会雷达…');

    loadInitial().then(function (ready) {
      if (ready) ensureSSEConnection();
    }).catch(function (e) {
      console.error('[app] 初始化失败', e);
      toast('初始化失败：' + e.message);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
