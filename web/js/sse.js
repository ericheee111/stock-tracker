/* =========================================================================
 * sse.js —— 带私有访问 Header 的 fetch-stream SSE 客户端
 * 全局对象：SSE
 * 事件类型：quote / signal / regime / sector / provider_health
 *
 * 原生 EventSource 无法发送 Authorization Header，因此个人决策流改用
 * fetch + ReadableStream。访问值仍只来自当前 sessionStorage；断线自动退避重连。
 * ========================================================================= */
(function (global) {
  'use strict';

  const TYPES = ['quote', 'signal', 'regime', 'sector', 'provider_health'];
  const listeners = {};
  TYPES.forEach(function (type) { listeners[type] = []; });
  listeners.open = [];
  listeners.error = [];

  let controller = null;
  let retryTimer = null;
  let shouldRun = false;
  let connected = false;
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
    if (!shouldRun || retryTimer) return;
    connected = false;
    if (error) emit('error', error);
    const delay = retryDelay;
    retryDelay = Math.min(30000, Math.round(retryDelay * 1.8));
    retryTimer = setTimeout(function () {
      retryTimer = null;
      connectLoop();
    }, delay);
  }

  async function connectLoop() {
    if (!shouldRun || controller) return;
    controller = new AbortController();
    const localController = controller;
    try {
      const headers = { 'Accept': 'text/event-stream' };
      if (global.API && typeof global.API.privateHeaders === 'function') {
        Object.assign(headers, global.API.privateHeaders());
      }
      const response = await fetch('/api/stream', {
        method: 'GET',
        cache: 'no-store',
        headers: headers,
        signal: localController.signal
      });
      if (!response.ok) {
        const error = new Error('SSE HTTP ' + response.status);
        error.status = response.status;
        throw error;
      }
      if (!response.body || typeof response.body.getReader !== 'function') {
        throw new Error('浏览器不支持 fetch streaming');
      }
      connected = true;
      retryDelay = 1000;
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
      if (!error || error.name !== 'AbortError') scheduleReconnect(error);
    } finally {
      if (controller === localController) controller = null;
      connected = false;
      if (shouldRun && !retryTimer) scheduleReconnect();
    }
  }

  function connect() {
    if (shouldRun) return;
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
    shouldRun = true;
    connectLoop();
  }

  global.SSE = {
    subscribe: subscribe,
    close: close,
    reconnect: reconnect,
    isConnected: function () { return connected; }
  };
})(window);
