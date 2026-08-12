/* =========================================================================
 * api.js —— REST 封装（fetch + 超时 + 失败兜底）
 * 全局对象：API
 * 端点严格对齐 architecture.md §9.1：
 *   /api/overview  /api/watchlist  /api/positions  /api/radar
 *   /api/signal/<id>  /api/markets  /api/provider_health
 *   /api/sectors  /api/config
 * 强制契约（§9.1）：行情/信号响应必含 data_status 与 observed_age_ms；
 *   /api/overview 顶层含 meta:{data_mode, providers, last_update, market_open}。
 * 所有函数：成功返回解析后的对象；失败抛出 Error（由调用方兜底，不白屏）。
 * ========================================================================= */
(function (global) {
  'use strict';

  const DEFAULT_TIMEOUT_MS = 8000;

  /**
   * 带超时的 JSON GET。fetch 失败时抛 Error，便于上层 Promise.allSettled 兜底。
   */
  async function fetchJSON(url, opts) {
    opts = opts || {};
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, opts.timeout || DEFAULT_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        method: 'GET',
        signal: ctrl.signal,
        cache: 'no-store',
        headers: { 'Accept': 'application/json' }
      });
      if (!res.ok) {
        throw new Error('HTTP ' + res.status + ' @ ' + url);
      }
      const ct = res.headers.get('content-type') || '';
      if (ct.indexOf('application/json') === -1) {
        // 后端可能在错误路由返回 HTML，统一按失败处理
        throw new Error('非 JSON 响应 @ ' + url);
      }
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  /** 归一化：兼容后端返回 {data:...} 或裸数组/对象，统一抽取 */
  function unwrap(payload, key) {
    if (payload && typeof payload === 'object' && key && payload[key] !== undefined) {
      return payload[key];
    }
    return payload;
  }

  const API = {
    getOverview: function () {
      return fetchJSON('/api/overview').then(function (d) { return d; });
    },
    getWatchlist: function () {
      return fetchJSON('/api/watchlist').then(function (d) { return unwrap(d, 'items') || unwrap(d, 'watchlist') || []; });
    },
    getPositions: function () {
      return fetchJSON('/api/positions').then(function (d) { return unwrap(d, 'items') || unwrap(d, 'positions') || []; });
    },
    getRadar: function () {
      return fetchJSON('/api/radar').then(function (d) { return unwrap(d, 'signals') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getSignal: function (id) {
      return fetchJSON('/api/signal/' + encodeURIComponent(id));
    },
    getMarkets: function () {
      return fetchJSON('/api/markets').then(function (d) { return unwrap(d, 'markets') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getProviderHealth: function () {
      return fetchJSON('/api/provider_health').then(function (d) { return unwrap(d, 'providers') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getSectors: function () {
      return fetchJSON('/api/sectors').then(function (d) { return unwrap(d, 'sectors') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getConfig: function () {
      return fetchJSON('/api/config').then(function (d) { return d || {}; });
    },
    // 暴露底层以便复用
    fetchJSON: fetchJSON
  };

  global.API = API;
})(window);
