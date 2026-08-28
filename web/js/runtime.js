/* =========================================================================
 * runtime.js — Hybrid H1 browser runtime, URL builder and health handshake.
 * Global: Runtime
 * ========================================================================= */
(function (global) {
  'use strict';

  const LEGACY_PRIVATE_ACCESS_KEY = 'stockTrackerPrivateAccess';
  const PRIVATE_ACCESS_PREFIX = 'stockTrackerPrivateAccess::';
  const ACTIVE_ORIGIN_KEY = 'stockTrackerPrivateAccessOrigin';
  const HEALTH_TIMEOUT_MS = 5000;
  const CORS_PROBE_TIMEOUT_MS = 2500;
  const HARD_FAILURES = Object.freeze([
    'RUNTIME_CONFIG_ERROR',
    'RUNTIME_HEALTH_INVALID',
    'NETWORK_OFFLINE',
    'ENGINE_OFFLINE',
    'TUNNEL_UNAVAILABLE',
    'CORS_BLOCKED',
    'API_VERSION_MISMATCH',
    'ENGINE_ID_MISMATCH',
    'BUILD_MISMATCH'
  ]);
  const VALID_DEPLOYMENT_MODES = Object.freeze([
    'LOCAL_ONLY',
    'HYBRID_PRIVATE',
    'HYBRID_PUBLIC_AUTH',
    'HYBRID_SNAPSHOT',
    'PURE_CLOUD_EXPERIMENTAL'
  ]);

  function RuntimeError(code, message, cause) {
    Error.call(this, message);
    this.name = 'RuntimeError';
    this.code = code || 'RUNTIME_ERROR';
    this.message = message || 'runtime error';
    this.cause = cause || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, RuntimeError);
  }
  RuntimeError.prototype = Object.create(Error.prototype);
  RuntimeError.prototype.constructor = RuntimeError;

  function visibleString(value, name, allowEmpty) {
    if (typeof value !== 'string' || value !== value.trim()) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', name + ' 必须是无首尾空白的字符串');
    }
    if (!allowEmpty && !value) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', name + ' 不能为空');
    }
    if (value.length > 256 || Array.prototype.some.call(value, function (char) {
      const code = char.charCodeAt(0);
      return code < 32 || code === 127;
    })) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', name + ' 包含无效字符或过长');
    }
    return value;
  }

  function isLoopbackHostname(value) {
    let hostname = String(value || '').toLowerCase().replace(/\.$/, '');
    if (hostname.charAt(0) === '[' && hostname.charAt(hostname.length - 1) === ']') {
      hostname = hostname.slice(1, -1);
    }
    if (hostname === 'localhost' || hostname === '::1') return true;
    const parts = hostname.split('.');
    if (parts.length !== 4 || parts[0] !== '127') return false;
    return parts.every(function (part) {
      return /^\d{1,3}$/.test(part) && Number(part) >= 0 && Number(part) <= 255;
    });
  }

  function normalizeOrigin(value) {
    const raw = visibleString(value, 'API Origin', false);
    if (raw.toLowerCase() === 'null' || raw.indexOf('\\') !== -1) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API Origin 不允许 null 或反斜杠');
    }
    let parsed;
    try {
      parsed = new URL(raw);
    } catch (error) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API Origin 无法解析', error);
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API Origin 只允许 HTTP(S)');
    }
    if (parsed.username || parsed.password || (parsed.pathname !== '/' && parsed.pathname !== '') ||
        parsed.search || parsed.hash) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API Origin 不能包含认证、路径、查询或片段');
    }
    let hostname = parsed.hostname.toLowerCase().replace(/\.$/, '');
    if (hostname.charAt(0) === '[' && hostname.charAt(hostname.length - 1) === ']') {
      hostname = hostname.slice(1, -1);
    }
    if (!hostname || (parsed.protocol === 'http:' && !isLoopbackHostname(hostname))) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', '远程 API Origin 必须使用 HTTPS；HTTP 仅限 loopback');
    }
    const host = hostname.indexOf(':') !== -1 ? '[' + hostname + ']' : hostname;
    const defaultPort = (parsed.protocol === 'https:' && parsed.port === '443') ||
      (parsed.protocol === 'http:' && parsed.port === '80');
    const port = parsed.port && !defaultPort ? ':' + parsed.port : '';
    return parsed.protocol + '//' + host + port;
  }

  function normalizeApiPath(value, name) {
    const path = visibleString(value, name || 'API path', false);
    if (path.indexOf('\\') !== -1 || path.charAt(0) !== '/' || path.indexOf('/api/') !== 0 ||
        path.indexOf('?') !== -1 || path.indexOf('#') !== -1 || /\s/.test(path)) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', (name || 'API path') + ' 必须是 /api/... 路径');
    }
    return path;
  }

  function exactBoolean(value, name, defaultValue) {
    if (value === undefined) return defaultValue;
    if (typeof value !== 'boolean') {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', name + ' 必须是 boolean');
    }
    return value;
  }

  function boundedInteger(value, name, minimum, maximum, defaultValue) {
    if (value === undefined) return defaultValue;
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      throw new RuntimeError(
        'RUNTIME_CONFIG_ERROR',
        name + ' 必须是 ' + minimum + '—' + maximum + ' 的整数'
      );
    }
    return value;
  }

  function readConfig() {
    const raw = global.STOCK_TRACKER_RUNTIME || {};
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'STOCK_TRACKER_RUNTIME 必须是对象');
    }
    const allowedKeys = [
      'deploymentMode',
      'apiBaseUrl',
      'allowedApiOrigins',
      'ssePath',
      'frontendBuild',
      'expectedApiMajor',
      'expectedEngineId',
      'allowApiOriginOverride',
      'allowPrivateBrowserCache',
      'healthPollMs'
    ];
    Object.keys(raw).forEach(function (key) {
      if (allowedKeys.indexOf(key) === -1) {
        throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'Runtime Config 包含未知字段：' + key);
      }
    });
    const mode = visibleString(raw.deploymentMode || 'HYBRID_PRIVATE', 'deploymentMode', false);
    if (VALID_DEPLOYMENT_MODES.indexOf(mode) === -1) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'deploymentMode 不在冻结枚举中');
    }

    if (!Array.isArray(raw.allowedApiOrigins || [])) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'allowedApiOrigins 必须是数组');
    }
    if ((raw.allowedApiOrigins || []).length > 32) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'allowedApiOrigins 最多允许 32 项');
    }
    const allowed = [];
    (raw.allowedApiOrigins || []).forEach(function (origin) {
      const normalized = normalizeOrigin(origin);
      if (allowed.indexOf(normalized) === -1) allowed.push(normalized);
    });

    const configuredBase = raw.apiBaseUrl === undefined ? '' :
      visibleString(raw.apiBaseUrl, 'apiBaseUrl', true);
    let apiOrigin;
    let sameOriginFallback = false;
    if (configuredBase) {
      apiOrigin = normalizeOrigin(configuredBase);
      if (allowed.indexOf(apiOrigin) === -1) {
        throw new RuntimeError(
          'RUNTIME_CONFIG_ERROR',
          '显式 apiBaseUrl 必须属于 allowedApiOrigins'
        );
      }
    } else {
      apiOrigin = normalizeOrigin(global.location.origin);
      sameOriginFallback = true;
    }

    const expectedEngineId = visibleString(
      raw.expectedEngineId || 'stock-tracker-local',
      'expectedEngineId',
      false
    );
    const frontendBuild = visibleString(
      raw.frontendBuild || 'development',
      'frontendBuild',
      false
    );
    const ssePath = normalizeApiPath(raw.ssePath || '/api/stream', 'ssePath');

    const allowApiOriginOverride = exactBoolean(
      raw.allowApiOriginOverride,
      'allowApiOriginOverride',
      false
    );
    if (allowApiOriginOverride) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', '当前正式合同禁止 API Origin Override');
    }
    const allowPrivateBrowserCache = exactBoolean(
      raw.allowPrivateBrowserCache,
      'allowPrivateBrowserCache',
      false
    );
    if (allowPrivateBrowserCache) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', '当前正式合同禁止私有浏览器持久缓存');
    }

    return Object.freeze({
      deploymentMode: mode,
      apiOrigin: apiOrigin,
      sameOriginFallback: sameOriginFallback,
      allowedApiOrigins: Object.freeze(allowed.slice()),
      ssePath: ssePath,
      frontendBuild: frontendBuild,
      expectedApiMajor: boundedInteger(raw.expectedApiMajor, 'expectedApiMajor', 1, 999, 1),
      expectedEngineId: expectedEngineId,
      allowApiOriginOverride: allowApiOriginOverride,
      allowPrivateBrowserCache: allowPrivateBrowserCache,
      healthPollMs: boundedInteger(raw.healthPollMs, 'healthPollMs', 5000, 300000, 15000)
    });
  }

  let config = null;
  let configError = null;
  try {
    config = readConfig();
  } catch (error) {
    configError = error instanceof RuntimeError ? error :
      new RuntimeError('RUNTIME_CONFIG_ERROR', '运行时配置无效', error);
  }

  const state = {
    status: configError ? 'RUNTIME_CONFIG_ERROR' : 'ENGINE_OFFLINE',
    detail: configError ? configError.message : '尚未完成 Runtime Health 握手',
    health: null,
    checkedAt: null,
    handshakeReady: false,
    buildMismatch: false,
    authState: null,
    sseConnected: false
  };
  const listeners = [];

  function snapshot() {
    return Object.freeze({
      status: state.status,
      detail: state.detail,
      health: state.health,
      checkedAt: state.checkedAt,
      handshakeReady: state.handshakeReady,
      buildMismatch: state.buildMismatch,
      authState: state.authState,
      sseConnected: state.sseConnected,
      apiOrigin: config ? config.apiOrigin : '',
      frontendBuild: config ? config.frontendBuild : 'invalid',
      deploymentMode: config ? config.deploymentMode : 'INVALID'
    });
  }

  function emit() {
    const current = snapshot();
    listeners.slice().forEach(function (listener) {
      try { listener(current); } catch (error) { console.error('[Runtime] listener', error); }
    });
  }

  function update(patch) {
    Object.keys(patch || {}).forEach(function (key) { state[key] = patch[key]; });
    emit();
  }

  function setStatus(status, detail, extra) {
    const patch = Object.assign({
      status: status,
      detail: detail || '',
      checkedAt: new Date().toISOString()
    }, extra || {});
    update(patch);
  }

  function assertConfigured() {
    if (configError || !config) throw configError;
    return config;
  }

  function apiUrl(path) {
    const active = assertConfigured();
    const normalizedPath = normalizeApiPath(path);
    const resolved = new URL(normalizedPath, active.apiOrigin + '/');
    if (resolved.origin !== active.apiOrigin || resolved.pathname !== normalizedPath ||
        resolved.search || resolved.hash) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API path 规范化后逃离 /api/... 边界');
    }
    return resolved.href;
  }

  function apiUrlWithQuery(path, query) {
    const active = assertConfigured();
    const normalizedPath = normalizeApiPath(path);
    if (!query || typeof query !== 'object' || Array.isArray(query)) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query 必须是对象');
    }
    const resolved = new URL(normalizedPath, active.apiOrigin + '/');
    const keys = Object.keys(query).sort();
    keys.forEach(function (key) {
      if (!/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(key) ||
          /(?:token|secret|password|access|authorization)/i.test(key)) {
        throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query key 非法或包含敏感语义');
      }
      const raw = query[key];
      const values = Array.isArray(raw) ? raw : [raw];
      if (!values.length || values.length > 32) {
        throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query value 数量超限');
      }
      values.forEach(function (value) {
        if (value === undefined || value === null) return;
        if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
          throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query value 类型非法');
        }
        const text = String(value);
        if (!text || text.length > 256 || /[\u0000-\u001f\u007f]/.test(text)) {
          throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query value 非法');
        }
        resolved.searchParams.append(key, text);
      });
    });
    if (resolved.origin !== active.apiOrigin || resolved.pathname !== normalizedPath || resolved.hash) {
      throw new RuntimeError('RUNTIME_CONFIG_ERROR', 'API query 规范化后逃离 /api/... 边界');
    }
    return resolved.href;
  }

  function sseUrl() {
    const active = assertConfigured();
    return apiUrl(active.ssePath);
  }

  function accessKey(origin) {
    return PRIVATE_ACCESS_PREFIX + encodeURIComponent(origin);
  }

  function initializeAccessScope() {
    if (!config) return;
    try {
      const previous = global.sessionStorage.getItem(ACTIVE_ORIGIN_KEY) || '';
      global.sessionStorage.removeItem(LEGACY_PRIVATE_ACCESS_KEY);
      if (previous !== config.apiOrigin) {
        if (previous) global.sessionStorage.removeItem(accessKey(previous));
        global.sessionStorage.removeItem(accessKey(config.apiOrigin));
      }
      global.sessionStorage.setItem(ACTIVE_ORIGIN_KEY, config.apiOrigin);
    } catch (error) {
      // Storage may be unavailable. Access APIs will return a precise error later.
    }
  }
  initializeAccessScope();

  function validPrivateAccess(value) {
    return typeof value === 'string' && value.length >= 32 && value.length <= 4096 &&
      value === value.trim() && !Array.prototype.some.call(value, function (char) {
        const code = char.charCodeAt(0);
        return code < 33 || code === 127;
      });
  }

  function privateAccessValue() {
    const active = assertConfigured();
    try {
      return global.sessionStorage.getItem(accessKey(active.apiOrigin)) || '';
    } catch (error) {
      return '';
    }
  }

  function setPrivateAccess(value) {
    const active = assertConfigured();
    if (!validPrivateAccess(value)) {
      throw new RuntimeError(
        'PRIVATE_ACCESS_INVALID',
        '私有访问值至少需要 32 个可见字符，且不能包含空白或控制字符'
      );
    }
    try {
      global.sessionStorage.setItem(ACTIVE_ORIGIN_KEY, active.apiOrigin);
      global.sessionStorage.setItem(accessKey(active.apiOrigin), value);
      emit();
    } catch (error) {
      throw new RuntimeError('SESSION_STORAGE_UNAVAILABLE', '当前浏览器不允许保存会话访问值', error);
    }
  }

  function clearPrivateAccess() {
    if (!config) return;
    try { global.sessionStorage.removeItem(accessKey(config.apiOrigin)); } catch (error) {}
    update({ authState: null });
  }

  function hasPrivateAccess() {
    return validPrivateAccess(privateAccessValue());
  }

  function assertPrivateReady() {
    assertConfigured();
    if (!state.handshakeReady || HARD_FAILURES.indexOf(state.status) !== -1) {
      throw new RuntimeError(
        HARD_FAILURES.indexOf(state.status) !== -1
          ? state.status
          : 'RUNTIME_HANDSHAKE_REQUIRED',
        state.detail || 'Runtime Health 握手尚未完成'
      );
    }
  }

  function assertDecisionReady() {
    assertPrivateReady();
    const health = state.health || {};
    if (state.status === 'STALE' || health.status === 'STALE' || health.data_status === 'STALE') {
      throw new RuntimeError(
        'DATA_STALE',
        'Runtime Health 已标记数据过期，禁止读取当前执行型决策'
      );
    }
  }

  function privateHeaders() {
    assertPrivateReady();
    const value = privateAccessValue();
    return validPrivateAccess(value) ? { 'Authorization': 'Bearer ' + value } : {};
  }

  function secureFetchOptions(extra) {
    const options = Object.assign({}, extra || {});
    options.mode = 'cors';
    options.credentials = 'omit';
    options.redirect = 'error';
    options.referrerPolicy = 'no-referrer';
    options.cache = 'no-store';
    return options;
  }

  function healthString(value, name) {
    if (typeof value !== 'string' || !value || value !== value.trim() || value.length > 256 ||
        Array.prototype.some.call(value, function (char) {
          const code = char.charCodeAt(0);
          return code < 32 || code === 127;
        })) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', name + ' 不是有效字符串');
    }
    return value;
  }

  function healthTimestamp(value, name, nullable) {
    if (nullable && value === null) return null;
    const text = healthString(value, name);
    if (Number.isNaN(Date.parse(text))) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', name + ' 不是有效时间戳');
    }
    return text;
  }

  function healthCount(value, name) {
    if (!Number.isInteger(value) || value < 0) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', name + ' 必须是非负整数');
    }
    return value;
  }

  function rejectInvalidHealth(error) {
    const invalid = error instanceof RuntimeError && error.code === 'RUNTIME_HEALTH_INVALID'
      ? error
      : new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime Health 合同无效', error);
    clearPrivateAccess();
    setStatus('RUNTIME_HEALTH_INVALID', invalid.message, {
      health: null,
      handshakeReady: false,
      buildMismatch: false,
      sseConnected: false
    });
    return invalid;
  }

  function validateHealth(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime Health 响应不是对象');
    }
    const required = [
      'schema_version', 'status', 'engine_id', 'engine_version', 'commit_id',
      'deployment_mode', 'started_at', 'last_heartbeat_at', 'last_collection_at',
      'data_as_of', 'data_status', 'scheduler_state', 'provider_summary',
      'database_state', 'sse_available', 'api_major'
    ];
    required.forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(payload, key)) {
        throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime Health 缺少字段：' + key);
      }
    });
    if (payload.schema_version !== 'hybrid-runtime-v1') {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime Health schema 不兼容');
    }
    healthString(payload.engine_id, 'engine_id');
    healthString(payload.engine_version, 'engine_version');
    healthString(payload.commit_id, 'commit_id');
    healthString(payload.deployment_mode, 'deployment_mode');
    if (VALID_DEPLOYMENT_MODES.indexOf(payload.deployment_mode) === -1) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'deployment_mode 不在冻结枚举中');
    }
    if (['ONLINE', 'DEGRADED', 'STALE'].indexOf(payload.status) === -1) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'status 不在 Runtime Health 枚举中');
    }
    if (['LIVE', 'DELAYED', 'STALE', 'UNKNOWN'].indexOf(payload.data_status) === -1) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'data_status 不在数据状态枚举中');
    }
    if (['RUNNING', 'STARTING', 'STOPPED', 'NOT_ATTACHED'].indexOf(payload.scheduler_state) === -1) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'scheduler_state 不在允许枚举中');
    }
    if (['READY', 'MISSING', 'UNAVAILABLE'].indexOf(payload.database_state) === -1) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'database_state 不在允许枚举中');
    }
    healthTimestamp(payload.started_at, 'started_at', false);
    healthTimestamp(payload.last_heartbeat_at, 'last_heartbeat_at', false);
    const lastCollectionAt = healthTimestamp(payload.last_collection_at, 'last_collection_at', true);
    const dataAsOf = healthTimestamp(payload.data_as_of, 'data_as_of', true);
    if (!payload.provider_summary || typeof payload.provider_summary !== 'object' ||
        Array.isArray(payload.provider_summary)) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'provider_summary 必须是对象');
    }
    const providerCount = healthCount(payload.provider_summary.count, 'provider_summary.count');
    const closed = healthCount(payload.provider_summary.closed, 'provider_summary.closed');
    const halfOpen = healthCount(payload.provider_summary.half_open, 'provider_summary.half_open');
    const open = healthCount(payload.provider_summary.open, 'provider_summary.open');
    if (closed + halfOpen + open !== providerCount) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'provider_summary 计数不一致');
    }
    if (typeof payload.sse_available !== 'boolean') {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'sse_available 必须是 boolean');
    }
    if (!Number.isInteger(payload.api_major) || payload.api_major < 1 || payload.api_major > 999) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'api_major 必须是 1—999 的整数');
    }
    if ((payload.status === 'ONLINE' && payload.data_status !== 'LIVE') ||
        ((payload.status === 'STALE') !== (payload.data_status === 'STALE'))) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime 与数据状态不一致');
    }
    if (payload.data_status !== 'UNKNOWN' && (!lastCollectionAt || !dataAsOf)) {
      throw new RuntimeError('RUNTIME_HEALTH_INVALID', '非 UNKNOWN 数据必须提供采集与数据时间');
    }
    return payload;
  }

  function applyHandshake(health) {
    const active = assertConfigured();
    if (!Number.isInteger(health.api_major) || health.api_major !== active.expectedApiMajor) {
      clearPrivateAccess();
      setStatus(
        'API_VERSION_MISMATCH',
        'API Major 不匹配：期望 ' + active.expectedApiMajor + '，实际 ' + health.api_major,
        { health: health, handshakeReady: false, buildMismatch: false }
      );
      throw new RuntimeError('API_VERSION_MISMATCH', state.detail);
    }
    if (health.engine_id !== active.expectedEngineId) {
      clearPrivateAccess();
      setStatus(
        'ENGINE_ID_MISMATCH',
        'Engine ID 不匹配：期望 ' + active.expectedEngineId + '，实际 ' + health.engine_id,
        { health: health, handshakeReady: false, buildMismatch: false }
      );
      throw new RuntimeError('ENGINE_ID_MISMATCH', state.detail);
    }

    const buildMismatch = active.frontendBuild !== 'development' &&
      health.commit_id !== active.frontendBuild;
    if (buildMismatch) {
      clearPrivateAccess();
      setStatus(
        'BUILD_MISMATCH',
        '前端 Build 与 Backend Commit 不一致；私有数据保持阻断直到重新部署前端',
        { health: health, handshakeReady: false, buildMismatch: true }
      );
      throw new RuntimeError('BUILD_MISMATCH', state.detail);
    }
    let status = health.status === 'ONLINE' ? 'ONLINE' :
      (health.status === 'STALE' ? 'STALE' : 'DEGRADED');
    let detail = status === 'ONLINE' ? '本地决策引擎在线' :
      (status === 'STALE' ? '本地决策引擎在线，但数据已过期' : '本地决策引擎处于降级状态');
    setStatus(status, detail, {
      health: health,
      handshakeReady: true,
      buildMismatch: false,
      authState: state.authState
    });
    return health;
  }

  async function refreshHealth() {
    const active = assertConfigured();
    if (global.navigator && global.navigator.onLine === false) {
      setStatus('NETWORK_OFFLINE', '浏览器报告当前设备没有网络连接', {
        handshakeReady: false,
        sseConnected: false
      });
      throw new RuntimeError('NETWORK_OFFLINE', state.detail);
    }
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, HEALTH_TIMEOUT_MS);
    let response;
    try {
      response = await global.fetch(
        active.apiOrigin + '/api/runtime/health',
        secureFetchOptions({
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          signal: controller.signal
        })
      );
    } catch (error) {
      const message = error && error.name === 'AbortError'
        ? 'Runtime Health 请求超时'
        : '无法读取 Runtime Health';
      throw new RuntimeError('RUNTIME_HEALTH_FETCH_FAILED', message, error);
    } finally {
      clearTimeout(timer);
    }
    let payload = null;
    try { payload = await response.json(); } catch (error) {
      throw rejectInvalidHealth(
        new RuntimeError('RUNTIME_HEALTH_INVALID', 'Runtime Health 不是有效 JSON', error)
      );
    }
    if (!response.ok) {
      const code = payload && payload.error && payload.error.code;
      if (code === 'CORS_ORIGIN_DENIED' || code === 'CORS_ORIGIN_INVALID') {
        setStatus('CORS_BLOCKED', '当前网页 Origin 未被本地引擎允许', {
          handshakeReady: false,
          sseConnected: false
        });
        throw new RuntimeError('CORS_BLOCKED', state.detail);
      }
      setStatus('ENGINE_OFFLINE', 'Runtime Health 返回 HTTP ' + response.status, {
        handshakeReady: false,
        sseConnected: false
      });
      throw new RuntimeError('ENGINE_OFFLINE', state.detail);
    }
    let health;
    try {
      health = validateHealth(payload);
    } catch (error) {
      throw rejectInvalidHealth(error);
    }
    return applyHandshake(health);
  }

  async function classifyHealthFailure(error) {
    if (error && HARD_FAILURES.indexOf(error.code) !== -1) return state.status;
    if (global.navigator && global.navigator.onLine === false) {
      setStatus('NETWORK_OFFLINE', '浏览器报告当前设备没有网络连接', {
        handshakeReady: false,
        sseConnected: false
      });
      return state.status;
    }
    const active = assertConfigured();
    if (active.sameOriginFallback || active.apiOrigin === normalizeOrigin(global.location.origin)) {
      setStatus('ENGINE_OFFLINE', '同源 Runtime Health 当前不可达', {
        handshakeReady: false,
        sseConnected: false
      });
      return state.status;
    }
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, CORS_PROBE_TIMEOUT_MS);
    try {
      await global.fetch(active.apiOrigin + '/api/runtime/health', {
        method: 'GET',
        mode: 'no-cors',
        credentials: 'omit',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        cache: 'no-store',
        signal: controller.signal
      });
      setStatus('CORS_BLOCKED', '本地引擎可达，但浏览器无法读取跨域响应', {
        handshakeReady: false,
        sseConnected: false
      });
    } catch (probeError) {
      const host = new URL(active.apiOrigin).hostname;
      const tunnel = host.toLowerCase().endsWith('.ts.net');
      setStatus(
        tunnel ? 'TUNNEL_UNAVAILABLE' : 'ENGINE_OFFLINE',
        tunnel ? 'Tailscale Serve 入口当前不可达' : '本地决策引擎当前不可达',
        { handshakeReady: false, sseConnected: false }
      );
    } finally {
      clearTimeout(timer);
    }
    return state.status;
  }

  function noteAuthError(error, attemptedAccess) {
    if (error && error._runtimeAuthHandled) return false;
    if (attemptedAccess !== undefined && privateAccessValue() !== attemptedAccess) {
      if (error) error._runtimeAuthHandled = true;
      return false;
    }
    if (error) error._runtimeAuthHandled = true;
    const code = error && error.code;
    const missing = !hasPrivateAccess();
    const status = missing || code === 'PRIVATE_API_AUTH_REQUIRED' ? 'AUTH_REQUIRED' : 'AUTH_FAILED';
    update({
      authState: status,
      checkedAt: new Date().toISOString()
    });
    return true;
  }

  function noteAuthSuccess() {
    if (!state.authState) return;
    state.authState = null;
    if (state.health) {
      applyHandshake(state.health);
    } else {
      emit();
    }
  }

  function noteSseOpen() {
    update({ sseConnected: true });
  }

  function noteSseClosed() {
    update({ sseConnected: false });
  }

  function onChange(listener) {
    if (typeof listener !== 'function') return function () {};
    if (listeners.indexOf(listener) === -1) listeners.push(listener);
    return function () {
      const index = listeners.indexOf(listener);
      if (index !== -1) listeners.splice(index, 1);
    };
  }

  function isHardFailure(status) {
    return HARD_FAILURES.indexOf(status || state.status) !== -1;
  }

  global.Runtime = Object.freeze({
    RuntimeError: RuntimeError,
    normalizeOrigin: normalizeOrigin,
    normalizeApiPath: normalizeApiPath,
    apiUrl: apiUrl,
    apiUrlWithQuery: apiUrlWithQuery,
    sseUrl: sseUrl,
    secureFetchOptions: secureFetchOptions,
    refreshHealth: refreshHealth,
    classifyHealthFailure: classifyHealthFailure,
    assertPrivateReady: assertPrivateReady,
    assertDecisionReady: assertDecisionReady,
    privateHeaders: privateHeaders,
    privateAccessValue: privateAccessValue,
    setPrivateAccess: setPrivateAccess,
    clearPrivateAccess: clearPrivateAccess,
    hasPrivateAccess: hasPrivateAccess,
    noteAuthError: noteAuthError,
    noteAuthSuccess: noteAuthSuccess,
    noteSseOpen: noteSseOpen,
    noteSseClosed: noteSseClosed,
    onChange: onChange,
    snapshot: snapshot,
    isHardFailure: isHardFailure,
    config: function () { return assertConfigured(); }
  });
})(window);
