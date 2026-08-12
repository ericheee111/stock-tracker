/* =========================================================================
 * sse.js —— EventSource('/api/stream') 封装，按事件类型分发到回调
 * 全局对象：SSE
 * 事件类型（architecture.md §9.2）：quote / signal / regime / sector / provider_health
 * 防御：
 *   - EventSource 原生断线自动重连，此处仅做好错误处理（不抛未捕获异常）。
 *   - 每条消息 JSON.parse 失败静默忽略；每个回调异常被 try/catch 隔离。
 * ========================================================================= */
(function (global) {
  'use strict';

  const TYPES = ['quote', 'signal', 'regime', 'sector', 'provider_health'];

  let es = null;
  const listeners = {};   // type -> [fn]
  TYPES.forEach(function (t) { listeners[t] = []; });
  listeners.open = [];
  listeners.error = [];

  function emit(type, payload) {
    const fns = listeners[type] || [];
    for (let i = 0; i < fns.length; i++) {
      try { fns[i](payload); } catch (e) { /* 隔离回调异常，避免影响其他订阅者 */ console.error('[SSE] listener error @' + type, e); }
    }
  }

  function onMessage(type, ev) {
    let payload = null;
    try {
      payload = JSON.parse(ev.data);
    } catch (e) {
      // 解析失败：忽略坏帧，不抛异常
      console.warn('[SSE] 无法解析 ' + type + ' 消息', e);
      return;
    }
    emit(type, payload);
  }

  function connect() {
    if (es) return;
    try {
      es = new EventSource('/api/stream');
    } catch (e) {
      console.error('[SSE] EventSource 创建失败', e);
      return;
    }
    es.onopen = function () { emit('open'); };
    es.onerror = function (e) {
      // EventSource 会自动重连；此处仅通知上层（可用于刷新连接提示），不抛异常
      emit('error', e);
    };
    TYPES.forEach(function (type) {
      es.addEventListener(type, function (ev) { onMessage(type, ev); });
    });
  }

  function subscribe(callbacks) {
    callbacks = callbacks || {};
    Object.keys(callbacks).forEach(function (k) {
      if (typeof callbacks[k] === 'function' && listeners[k]) {
        listeners[k].push(callbacks[k]);
      }
    });
    connect();
  }

  function close() {
    if (es) { es.close(); es = null; }
  }

  global.SSE = {
    subscribe: subscribe,
    close: close,
    isConnected: function () { return !!es; }
  };
})(window);
