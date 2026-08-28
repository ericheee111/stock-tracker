/* =========================================================================
 * api.js — REST wrapper using the Hybrid H1 Runtime URL/handshake contract.
 * Global: API
 * ========================================================================= */
(function (global) {
  'use strict';

  const Runtime = global.Runtime;
  const DEFAULT_TIMEOUT_MS = 8000;

  function APIRequestError(status, code, message, field, url, cause) {
    Error.call(this, message);
    this.name = 'APIRequestError';
    this.message = message;
    this.status = status;
    this.code = code || 'HTTP_ERROR';
    this.field = field || null;
    this.url = url || '';
    this.cause = cause || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, APIRequestError);
  }
  APIRequestError.prototype = Object.create(Error.prototype);
  APIRequestError.prototype.constructor = APIRequestError;

  function privateAccessValue() {
    return Runtime ? Runtime.privateAccessValue() : '';
  }

  function setPrivateAccess(value) {
    if (!Runtime) throw new Error('Runtime 模块未加载');
    Runtime.setPrivateAccess(value);
  }

  function clearPrivateAccess() {
    if (Runtime) Runtime.clearPrivateAccess();
  }

  function hasPrivateAccess() {
    return Boolean(Runtime && Runtime.hasPrivateAccess());
  }

  function privateHeaders() {
    if (!Runtime) return {};
    return Runtime.privateHeaders();
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
    } catch (error) {
      return { __invalidJson: true };
    }
  }

  function resolvedUrl(path, query) {
    if (!Runtime) throw new APIRequestError(0, 'RUNTIME_MISSING', 'Runtime 模块未加载', null, path);
    try {
      return query ? Runtime.apiUrlWithQuery(path, query) : Runtime.apiUrl(path);
    } catch (error) {
      throw new APIRequestError(
        0,
        error && error.code || 'RUNTIME_CONFIG_ERROR',
        error && error.message || 'API URL 无法解析',
        null,
        String(path || ''),
        error
      );
    }
  }

  async function requestJSON(path, opts) {
    opts = opts || {};
    const url = resolvedUrl(path, opts.query || null);
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, opts.timeout || DEFAULT_TIMEOUT_MS);
    const method = opts.method || 'GET';
    const headers = { 'Accept': 'application/json' };
    let attemptedAccess;
    try {
      if (opts.decision) Runtime.assertDecisionReady();
      if (opts.private) {
        attemptedAccess = privateAccessValue();
        Object.assign(headers, privateHeaders());
      }
    } catch (error) {
      clearTimeout(timer);
      throw new APIRequestError(
        0,
        error && error.code || 'RUNTIME_HANDSHAKE_REQUIRED',
        error && error.message || 'Runtime Health 握手尚未完成',
        null,
        url,
        error
      );
    }

    let body;
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      body = JSON.stringify(opts.body);
    }

    try {
      const fetchOptions = Runtime.secureFetchOptions({
        method: method,
        signal: ctrl.signal,
        headers: headers,
        body: body
      });
      const res = await global.fetch(url, fetchOptions);
      const payload = await readResponsePayload(res);
      if (!res.ok) {
        const error = payload && payload.error && typeof payload.error === 'object'
          ? payload.error : {};
        const requestError = new APIRequestError(
          res.status,
          error.code || 'HTTP_' + res.status,
          error.message || ('HTTP ' + res.status + ' @ ' + url),
          error.field || null,
          url
        );
        if (opts.private && (res.status === 401 || res.status === 403) && Runtime) {
          Runtime.noteAuthError(requestError, attemptedAccess);
        }
        throw requestError;
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
      if (opts.private && Runtime) Runtime.noteAuthSuccess();
      return payload;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new APIRequestError(0, 'REQUEST_TIMEOUT', '请求超时', null, url, error);
      }
      if (error instanceof APIRequestError || (Runtime && error instanceof Runtime.RuntimeError)) {
        throw error;
      }
      throw new APIRequestError(
        0,
        'NETWORK_REQUEST_FAILED',
        '无法连接 API；可能是 Engine、Tunnel、CORS 或网络故障',
        null,
        url,
        error
      );
    } finally {
      clearTimeout(timer);
    }
  }

  function fetchJSON(path, opts) {
    opts = Object.assign({}, opts || {}, { method: 'GET' });
    return requestJSON(path, opts);
  }

  function unwrap(payload, key) {
    if (payload && typeof payload === 'object' && key && payload[key] !== undefined) {
      return payload[key];
    }
    return payload;
  }

  const API = {
    getRuntimeHealth: function () {
      if (!Runtime) return Promise.reject(new Error('Runtime 模块未加载'));
      return Runtime.refreshHealth();
    },
    getOverview: function () {
      return fetchJSON('/api/overview', { private: true, decision: true });
    },
    getBriefToday: function (opts) {
      opts = opts || {};
      return fetchJSON('/api/brief/today', {
        timeout: opts.timeout || DEFAULT_TIMEOUT_MS,
        private: true,
        decision: true
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
      return fetchJSON('/api/watchlist', { private: true, decision: true })
        .then(function (data) { return unwrap(data, 'items') || unwrap(data, 'watchlist') || []; });
    },
    getPositions: function () {
      return fetchJSON('/api/positions', { private: true, decision: true })
        .then(function (data) { return unwrap(data, 'items') || unwrap(data, 'positions') || []; });
    },
    getRadar: function () {
      return fetchJSON('/api/radar', { private: true, decision: true })
        .then(function (data) { return unwrap(data, 'signals') || unwrap(data, 'items') || (Array.isArray(data) ? data : []); });
    },
    getSignal: function (id) {
      return fetchJSON('/api/signal/' + encodeURIComponent(id), {
        private: true,
        decision: true
      });
    },
    getQuote: function (symbol) {
      return fetchJSON('/api/quote/' + encodeURIComponent(symbol));
    },
    getMarkets: function () {
      return fetchJSON('/api/markets')
        .then(function (data) { return unwrap(data, 'markets') || unwrap(data, 'items') || (Array.isArray(data) ? data : []); });
    },
    getProviderHealth: function () {
      return fetchJSON('/api/provider_health')
        .then(function (data) { return unwrap(data, 'providers') || unwrap(data, 'items') || (Array.isArray(data) ? data : []); });
    },
    getSectors: function () {
      return fetchJSON('/api/sectors')
        .then(function (data) { return unwrap(data, 'sectors') || unwrap(data, 'items') || (Array.isArray(data) ? data : []); });
    },
    getConfig: function () {
      return fetchJSON('/api/config', { private: true }).then(function (data) { return data || {}; });
    },
    getMonitorSummary: function () {
      return fetchJSON('/api/monitor/summary', { private: true });
    },
    getMonitorDataLink: function () {
      return fetchJSON('/api/monitor/data-link', { private: true });
    },
    getMonitorRules: function () {
      return fetchJSON('/api/monitor/rules', { private: true })
        .then(function (data) { return unwrap(data, 'rules') || []; });
    },
    getMonitorInbox: function (filters) {
      filters = filters || {};
      const query = {};
      if (filters.states && filters.states.length) query.state = filters.states;
      if (filters.limit != null) query.limit = filters.limit;
      return fetchJSON('/api/monitor/inbox', { private: true, query: query })
        .then(function (data) { return unwrap(data, 'inbox') || []; });
    },
    getMonitorOutbox: function (limit) {
      return fetchJSON('/api/monitor/outbox', {
        private: true,
        query: { limit: limit || 100 }
      }).then(function (data) { return unwrap(data, 'outbox') || []; });
    },
    getMonitorReplay: function (params) {
      params = params || {};
      return fetchJSON('/api/monitor/replay', {
        private: true,
        query: {
          symbol: params.symbol,
          start: params.start,
          end: params.end,
          backend: params.backend || 'python',
          limit: params.limit || 5000
        }
      });
    },
    createMonitorRule: function (payload) {
      return requestJSON('/api/monitor/rules', {
        method: 'POST', private: true, body: payload
      });
    },
    updateMonitorRule: function (ruleId, payload) {
      return requestJSON('/api/monitor/rules/' + encodeURIComponent(ruleId), {
        method: 'PUT', private: true, body: payload
      });
    },
    deleteMonitorRule: function (ruleId) {
      return requestJSON('/api/monitor/rules/' + encodeURIComponent(ruleId), {
        method: 'DELETE', private: true
      });
    },
    transitionMonitorInbox: function (inboxId, payload) {
      return requestJSON('/api/monitor/inbox/' + encodeURIComponent(inboxId) + '/transition', {
        method: 'POST', private: true, body: payload
      });
    },
    requestJSON: requestJSON,
    fetchJSON: fetchJSON,
    APIRequestError: APIRequestError,
    setPrivateAccess: setPrivateAccess,
    clearPrivateAccess: clearPrivateAccess,
    hasPrivateAccess: hasPrivateAccess,
    privateAccessValue: privateAccessValue,
    privateHeaders: privateHeaders,
    apiUrl: resolvedUrl,
    currentOrigin: function () {
      return Runtime ? Runtime.config().apiOrigin : '';
    }
  };

  global.API = Object.freeze(API);
})(window);
