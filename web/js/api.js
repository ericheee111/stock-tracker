/* =========================================================================
 * api.js —— REST 封装（超时、严格 JSON、私有会话访问、结构化错误）
 * 全局对象：API
 * ========================================================================= */
(function (global) {
  'use strict';

  const DEFAULT_TIMEOUT_MS = 8000;
  const PRIVATE_ACCESS_KEY = 'stockTrackerPrivateAccess';

  function APIRequestError(status, code, message, field, url) {
    Error.call(this, message);
    this.name = 'APIRequestError';
    this.message = message;
    this.status = status;
    this.code = code || 'HTTP_ERROR';
    this.field = field || null;
    this.url = url || '';
    if (Error.captureStackTrace) Error.captureStackTrace(this, APIRequestError);
  }
  APIRequestError.prototype = Object.create(Error.prototype);
  APIRequestError.prototype.constructor = APIRequestError;

  function privateAccessValue() {
    try {
      return global.sessionStorage.getItem(PRIVATE_ACCESS_KEY) || '';
    } catch (e) {
      return '';
    }
  }

  function setPrivateAccess(value) {
    const normalized = typeof value === 'string' ? value.trim() : '';
    try {
      if (normalized) global.sessionStorage.setItem(PRIVATE_ACCESS_KEY, normalized);
      else global.sessionStorage.removeItem(PRIVATE_ACCESS_KEY);
    } catch (e) {
      throw new Error('当前浏览器不允许保存会话访问值');
    }
  }

  function clearPrivateAccess() {
    setPrivateAccess('');
  }

  function hasPrivateAccess() {
    return privateAccessValue().length > 0;
  }

  function privateHeaders() {
    const value = privateAccessValue();
    return value ? { 'Authorization': 'Bearer ' + value } : {};
  }

  async function readResponsePayload(res) {
    if (res.status === 204) return null;
    const contentType = res.headers.get('content-type') || '';
    if (contentType.indexOf('application/json') === -1) {
      const text = await res.text().catch(function () { return ''; });
      return { __nonJson: true, text: text };
    }
    try {
      return await res.json();
    } catch (e) {
      return { __invalidJson: true };
    }
  }

  async function requestJSON(url, opts) {
    opts = opts || {};
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, opts.timeout || DEFAULT_TIMEOUT_MS);
    const method = opts.method || 'GET';
    const headers = { 'Accept': 'application/json' };
    if (opts.private) Object.assign(headers, privateHeaders());
    let body;
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      body = JSON.stringify(opts.body);
    }
    try {
      const res = await fetch(url, {
        method: method,
        signal: ctrl.signal,
        cache: 'no-store',
        headers: headers,
        body: body
      });
      const payload = await readResponsePayload(res);
      if (!res.ok) {
        const error = payload && payload.error && typeof payload.error === 'object'
          ? payload.error : {};
        throw new APIRequestError(
          res.status,
          error.code || 'HTTP_' + res.status,
          error.message || ('HTTP ' + res.status + ' @ ' + url),
          error.field || null,
          url
        );
      }
      if (payload && payload.__nonJson) {
        throw new APIRequestError(
          res.status,
          'NON_JSON_RESPONSE',
          '后端返回了非 JSON 响应',
          null,
          url
        );
      }
      if (payload && payload.__invalidJson) {
        throw new APIRequestError(
          res.status,
          'INVALID_JSON_RESPONSE',
          '后端 JSON 响应无法解析',
          null,
          url
        );
      }
      return payload;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new APIRequestError(0, 'REQUEST_TIMEOUT', '请求超时', null, url);
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function fetchJSON(url, opts) {
    opts = Object.assign({}, opts || {}, { method: 'GET' });
    return requestJSON(url, opts);
  }

  function unwrap(payload, key) {
    if (payload && typeof payload === 'object' && key && payload[key] !== undefined) {
      return payload[key];
    }
    return payload;
  }

  const API = {
    getOverview: function () {
      return fetchJSON('/api/overview', { private: true });
    },
    getBriefToday: function (opts) {
      opts = opts || {};
      return fetchJSON('/api/brief/today', {
        timeout: opts.timeout || DEFAULT_TIMEOUT_MS,
        private: true
      }).then(function (payload) { return unwrap(payload, 'brief') || payload; });
    },
    getPortfolio: function () {
      return fetchJSON('/api/portfolio', { private: true });
    },
    putPortfolioProfile: function (payload) {
      return requestJSON('/api/portfolio/profile', {
        method: 'PUT', private: true, body: payload
      });
    },
    createPortfolioPosition: function (payload) {
      return requestJSON('/api/portfolio/positions', {
        method: 'POST', private: true, body: payload
      });
    },
    patchPortfolioPosition: function (positionId, payload) {
      return requestJSON('/api/portfolio/positions/' + encodeURIComponent(positionId), {
        method: 'PATCH', private: true, body: payload
      });
    },
    deletePortfolioPosition: function (positionId) {
      return requestJSON('/api/portfolio/positions/' + encodeURIComponent(positionId), {
        method: 'DELETE', private: true
      });
    },
    getWatchlist: function () {
      return fetchJSON('/api/watchlist', { private: true })
        .then(function (d) { return unwrap(d, 'items') || unwrap(d, 'watchlist') || []; });
    },
    getPositions: function () {
      return fetchJSON('/api/positions', { private: true })
        .then(function (d) { return unwrap(d, 'items') || unwrap(d, 'positions') || []; });
    },
    getRadar: function () {
      return fetchJSON('/api/radar', { private: true })
        .then(function (d) { return unwrap(d, 'signals') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getSignal: function (id) {
      return fetchJSON('/api/signal/' + encodeURIComponent(id), { private: true });
    },
    getQuote: function (symbol) {
      return fetchJSON('/api/quote/' + encodeURIComponent(symbol));
    },
    getMarkets: function () {
      return fetchJSON('/api/markets')
        .then(function (d) { return unwrap(d, 'markets') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getProviderHealth: function () {
      return fetchJSON('/api/provider_health')
        .then(function (d) { return unwrap(d, 'providers') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getSectors: function () {
      return fetchJSON('/api/sectors')
        .then(function (d) { return unwrap(d, 'sectors') || unwrap(d, 'items') || (Array.isArray(d) ? d : []); });
    },
    getConfig: function () {
      return fetchJSON('/api/config', { private: true }).then(function (d) { return d || {}; });
    },
    requestJSON: requestJSON,
    fetchJSON: fetchJSON,
    APIRequestError: APIRequestError,
    setPrivateAccess: setPrivateAccess,
    clearPrivateAccess: clearPrivateAccess,
    hasPrivateAccess: hasPrivateAccess,
    privateHeaders: privateHeaders
  };

  global.API = API;
})(window);
