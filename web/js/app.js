/* =========================================================================
 * app.js —— 启动 / 路由 / 初始拉取 / SSE 增量更新 / 信号详情弹层
 * 对齐 architecture.md §9：fetch REST + EventSource('/api/stream')。
 * 防御：每个 API 响应做空值防御；初始拉取失败显示"连接后端失败"，不白屏；
 *       SSE 断线由 EventSource 原生重连，回调异常被隔离。
 * ========================================================================= */
(function () {
  'use strict';

  const F = window.Fmt;
  const UI = window.UI;
  const API = window.API;
  const SSE = window.SSE;

  const PAGES = ['overview', 'watch', 'radar', 'research'];

  const state = {
    market: 'A',
    meta: null,
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
  function toast(msg) {
    const t = $('#toast');
    if (!t) return;
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 1400);
  }

  /* ---------------- 路由 ---------------- */
  function setPage(name) {
    if (PAGES.indexOf(name) === -1) name = 'overview';
    PAGES.forEach(function (p) {
      const page = $('#page-' + p);
      if (page) page.classList.toggle('active', p === name);
    });
    $$('.nav-btn').forEach(function (b) { b.classList.toggle('active', b.dataset.page === name); });
  }

  function setMarket(m) {
    state.market = m;
    $$('.market-tab').forEach(function (t) { t.classList.toggle('active', t.dataset.market === m); });
    // 仅重渲染受市场影响的区块
    const idx = $('#indexGrid'); if (idx) idx.innerHTML = UI.renderIndexGrid(state.markets, m);
    renderWatch();
    renderRadar(); // 雷达可按需过滤，这里不过滤保持全量；自选/持仓按市场过滤
  }

  /* ---------------- 初始拉取 ---------------- */
  function ok(r) { return r && r.status === 'fulfilled' ? r.value : null; }

  async function loadInitial() {
    const results = await Promise.allSettled([
      API.getOverview(), API.getMarkets(), API.getWatchlist(),
      API.getPositions(), API.getRadar(), API.getSectors(), API.getProviderHealth(), API.getConfig()
    ]);
    state.overview = ok(results[0]);
    state.markets = ok(results[1]) || [];
    state.watchlist = ok(results[2]) || [];
    state.positions = ok(results[3]) || [];
    state.radar = ok(results[4]) || [];
    state.sectors = ok(results[5]) || [];
    state.providers = ok(results[6]) || [];
    state.config = ok(results[7]) || {};

    if (state.overview) state.meta = state.overview.meta || null;

    // 横幅：真实/降级/演示/失败 可见
    renderBanner();
    if (!state.meta && !state.markets.length) {
      const banner = $('#banner');
      banner.className = 'banner error';
      banner.innerHTML = '<span class="banner-dot"></span><span class="banner-mode">连接后端失败</span>' +
        '<span class="banner-text">未能从 /api/* 取到数据。请确认后端已启动（scripts/start.bat），并从前端的同源 http://localhost:8080/ 访问本页。</span>';
    }

    // 渲染全部页面（切换 tab 即时显示）
    renderOverview();
    renderWatch();
    renderRadar();
  }

  /* ---------------- 渲染：横幅 ---------------- */
  function renderBanner() {
    const banner = $('#banner');
    if (!banner) return;
    banner.className = 'banner ' + UI.bannerModeClass(state.meta);
    banner.innerHTML = UI.renderBanner(state.meta, state.providers);
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

    const top = $('#topList');
    if (top) top.innerHTML = UI.renderTopList((state.overview && state.overview.top_opportunities) || state.radar);
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
    if (id === 'page-overview') renderOverview();
    else if (id === 'page-watch') renderWatch();
    else if (id === 'page-radar') renderRadar();
  }

  /* ---------------- 信号详情弹层 ---------------- */
  async function openSignal(id) {
    if (!id) return;
    const mask = $('#sheetMask');
    const sheet = $('#sheet');
    if (!mask || !sheet) return;
    // 若本地缓存已有，先即时展示，再从 REST 取最新
    const cached = findSignalById(id);
    sheet.innerHTML = UI.renderSignalDetail(cached) ||
      '<div class="sheet-body"><div class="loading-box">加载信号详情…</div></div>' +
      '<div class="sheet-footer"><button class="sheet-close" id="sheetClose">关闭</button></div>';
    mask.hidden = false;
    bindSheetClose();
    try {
      const fresh = await API.getSignal(id);
      if (fresh) {
        sheet.innerHTML = UI.renderSignalDetail(fresh);
        bindSheetClose();
      }
    } catch (e) {
      if (!cached) sheet.innerHTML = '<div class="sheet-body"><div class="card-empty">信号详情获取失败：' + F.esc(e.message) + '</div></div>' +
        '<div class="sheet-footer"><button class="sheet-close" id="sheetClose">关闭</button></div>';
      bindSheetClose();
    }
  }

  function findSignalById(id) {
    let found = null;
    (state.radar || []).forEach(function (s) { if (s.signal_id === id) found = s; });
    if (found) return found;
    (state.watchlist || []).forEach(function (it) { if (it.signal && it.signal.signal_id === id) found = it.signal; });
    (state.positions || []).forEach(function (p) { if (p.signal && p.signal.signal_id === id) found = p.signal; });
    if ((state.overview && state.overview.top_opportunities)) {
      (state.overview.top_opportunities || []).forEach(function (s) { if (s.signal_id === id) found = s; });
    }
    return found;
  }

  function bindSheetClose() {
    const btn = $('#sheetClose');
    if (btn) btn.onclick = closeSheet;
  }
  function closeSheet() {
    const mask = $('#sheetMask');
    if (mask) mask.hidden = true;
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
    $$('.live-status[data-symbol="' + sym + '"]').forEach(function (el) { el.innerHTML = statusHtml; });
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
    (state.watchlist || []).forEach(function (it) {
      if (it.signal && it.signal.signal_id === sig.signal_id) it.signal = sig;
    });
    // positions 中的 signal
    (state.positions || []).forEach(function (p) {
      if (p.signal && p.signal.signal_id === sig.signal_id) p.signal = sig;
    });
  }

  function subscribeSSE() {
    SSE.subscribe({
      quote: function (q) { paintQuote(q); },
      signal: function (sig) { updateSignalInCaches(sig); renderActive(); },
      regime: function (r) { if (state.overview) state.overview.regime = r; renderActive(); },
      sector: function (payload) {
        const arr = Array.isArray(payload) ? payload : (payload && payload.sectors) || [];
        if (arr.length) { state.sectors = arr; if (state.overview) state.overview.sector_leaders = arr; }
        renderActive();
      },
      provider_health: function (arr) {
        if (Array.isArray(arr) && arr.length) state.providers = arr;
        else if (arr && arr.providers) state.providers = arr.providers;
        renderBanner();
      },
      open: function () { /* 连接建立，可选提示 */ },
      error: function () { /* EventSource 自动重连；此处不抛异常 */ }
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
    // 卡片点击 → 信号详情（含 data-signal 的元素）
    document.addEventListener('click', function (e) {
      const el = e.target.closest('[data-signal]');
      if (el && el.dataset.signal) openSignal(el.dataset.signal);
    });
    // 弹层遮罩点击关闭
    const mask = $('#sheetMask');
    if (mask) mask.addEventListener('click', function (e) { if (e.target === mask) closeSheet(); });
    // ESC 关闭
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { const m = $('#sheetMask'); if (m && !m.hidden) closeSheet(); }
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindEvents();
    // 先渲染占位加载态，避免白屏
    const idx = $('#indexGrid'); if (idx) idx.innerHTML = UI.loadingBox('加载指数中…');
    const grid = $('#overviewGrid'); if (grid) grid.innerHTML = UI.loadingBox('加载市场状态中…');
    const top = $('#topList'); if (top) top.innerHTML = UI.loadingBox('加载机会中…');
    const wg = $('#watchGroups'); if (wg) wg.innerHTML = UI.loadingBox('加载自选…');
    const pl = $('#positionList'); if (pl) pl.innerHTML = UI.loadingBox('加载持仓…');
    const rg = $('#radarGroups'); if (rg) rg.innerHTML = UI.loadingBox('加载机会雷达…');

    loadInitial().then(function () {
      subscribeSSE();
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
