/* =========================================================================
 * sse.js — Header-authenticated fetch-stream SSE through the Runtime URL layer.
 * Global: SSE
 * ========================================================================= */
(function (global) {
  'use strict';

  const Runtime = global.Runtime;
  const TYPES = [
    'quote', 'signal', 'regime', 'sector', 'provider_health',
    'monitor.inbox', 'monitor.notification'
  ];
  const listeners = {};
  TYPES.forEach(function (type) { listeners[type] = []; });
  listeners.open = [];
  listeners.error = [];

  let controller = null;
  let retryTimer = null;
  let shouldRun = false;
  let connected = false;
  let authBlocked = false;
  let retryDelay = 1000;

  function emit(type, payload) {
    const callbacks = listeners[type] || [];
    callbacks.forEach(function (callback) {
      try {
        callback(payload);
      } catch (error) {
        console.error('[SSE] listener error @' + type, error);
      }
    });
  }

  function parsePayload(type, data) {
    if (TYPES.indexOf(type) === -1) return;
    try {
      emit(type, JSON.parse(data));
    } catch (error) {
      console.warn('[SSE] 无法解析 ' + type + ' 消息', error);
    }
  }

  function createParser() {
    let buffer = '';
    let eventType = 'message';
    let dataLines = [];

    function dispatch() {
      if (dataLines.length) parsePayload(eventType, dataLines.join('\n'));
      eventType = 'message';
      dataLines = [];
    }

    function processLine(rawLine) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (line === '') {
        dispatch();
        return;
      }
      if (line.charAt(0) === ':') return;
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.charAt(0) === ' ') value = value.slice(1);
      if (field === 'event') eventType = value || 'message';
      else if (field === 'data') dataLines.push(value);
    }

    return {
      push: function (text) {
        buffer += text;
        let newline;
        while ((newline = buffer.indexOf('\n')) !== -1) {
          processLine(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
        }
      },
      finish: function () {
        if (buffer) processLine(buffer);
        dispatch();
        buffer = '';
      }
    };
  }

  function scheduleReconnect(error) {
    if (!shouldRun || authBlocked || retryTimer) return;
    connected = false;
    if (Runtime) Runtime.noteSseClosed();
    if (error) emit('error', error);
    const delay = retryDelay;
    retryDelay = Math.min(30000, Math.round(retryDelay * 1.8));
    retryTimer = setTimeout(function () {
      retryTimer = null;
      connectLoop();
    }, delay);
  }

  function blockForAuth(error, attemptedAccess) {
    if (Runtime && Runtime.noteAuthError(error, attemptedAccess) === false) return;
    authBlocked = true;
    shouldRun = false;
    connected = false;
    if (Runtime) {
      Runtime.noteSseClosed();

    }
    emit('error', error);
  }

  async function connectLoop() {
    if (!shouldRun || authBlocked || controller) return;
    controller = new AbortController();
    const localController = controller;
    let attemptedAccess;
    try {
      if (!Runtime) throw new Error('Runtime 模块未加载');
      Runtime.assertPrivateReady();
      const headers = { 'Accept': 'text/event-stream' };
      attemptedAccess = Runtime.privateAccessValue();
      Object.assign(headers, Runtime.privateHeaders());
      const response = await global.fetch(
        Runtime.sseUrl(),
        Runtime.secureFetchOptions({
          method: 'GET',
          headers: headers,
          signal: localController.signal
        })
      );
      if (!response.ok) {
        const error = new Error('SSE HTTP ' + response.status);
        error.status = response.status;
        error.code = response.status === 401 ? 'PRIVATE_API_AUTH_REQUIRED' :
          (response.status === 403 ? 'PRIVATE_API_AUTH_FAILED' : 'SSE_HTTP_' + response.status);
        if (response.status === 401 || response.status === 403) {
          blockForAuth(error, attemptedAccess);
          return;
        }
        throw error;
      }
      if (!response.body || typeof response.body.getReader !== 'function') {
        const unsupported = new Error('浏览器不支持 fetch streaming');
        unsupported.code = 'SSE_STREAM_UNSUPPORTED';
        throw unsupported;
      }
      connected = true;
      retryDelay = 1000;
      if (Runtime) {
        Runtime.noteAuthSuccess();
        Runtime.noteSseOpen();
      }
      emit('open');
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      const parser = createParser();
      while (shouldRun && controller === localController) {
        const chunk = await reader.read();
        if (chunk.done) break;
        parser.push(decoder.decode(chunk.value, { stream: true }));
      }
      parser.push(decoder.decode());
      parser.finish();
    } catch (error) {
      if (!error || error.name !== 'AbortError') {
        if (error && (error.code === 'PRIVATE_API_AUTH_REQUIRED' ||
            error.code === 'PRIVATE_API_AUTH_FAILED')) {
          blockForAuth(error, attemptedAccess);
        } else if (error && ((Runtime && Runtime.isHardFailure(error.code)) ||
            error.code === 'RUNTIME_HANDSHAKE_REQUIRED')) {
          shouldRun = false;
          emit('error', error);
        } else {
          scheduleReconnect(error);
        }
      }
    } finally {
      if (controller === localController) controller = null;
      connected = false;
      if (Runtime) Runtime.noteSseClosed();
      if (shouldRun && !authBlocked && !retryTimer) scheduleReconnect();
    }
  }

  function connect() {
    if (shouldRun && !authBlocked) return;
    authBlocked = false;
    shouldRun = true;
    connectLoop();
  }

  function subscribe(callbacks) {
    callbacks = callbacks || {};
    Object.keys(callbacks).forEach(function (type) {
      const callback = callbacks[type];
      if (typeof callback === 'function' && listeners[type] &&
          listeners[type].indexOf(callback) === -1) {
        listeners[type].push(callback);
      }
    });
    connect();
  }

  function close() {
    shouldRun = false;
    connected = false;
    authBlocked = false;
    if (Runtime) Runtime.noteSseClosed();
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (controller) {
      controller.abort();
      controller = null;
    }
  }

  function reconnect() {
    close();
    retryDelay = 1000;
    authBlocked = false;
    shouldRun = true;
    connectLoop();
  }

  global.SSE = Object.freeze({
    subscribe: subscribe,
    close: close,
    reconnect: reconnect,
    isConnected: function () { return connected; },
    isAuthBlocked: function () { return authBlocked; }
  });
})(window);
