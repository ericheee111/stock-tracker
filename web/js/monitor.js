/* =========================================================================
 * monitor.js — Stage 4E Monitor Workspace
 * Dense, evidence-gated intraday monitoring without fabricated signals.
 * Uses only local static assets, the existing Runtime/Auth boundary, and
 * CSP-safe native SVG rendering. Global: MonitorWorkspace.
 * ========================================================================= */
(function (global) {
  'use strict';

  const API = global.API;
  const Runtime = global.Runtime;
  const F = global.Fmt;
  const ROOT_ID = 'monitorWorkspace';
  const ACTIVE_STATES = ['NEW', 'ACKNOWLEDGED', 'SNOOZED'];
  const NUMERIC_FACTS = [
    'market_event.latency_p95_ms',
    'market_event.callback_gap_count',
    'market_event.provider_gap_count',
    'market_event.out_of_order_count',
    'market_event.ingestion_lag_ms',
    'market_event.last_price',
    'market_event.change_pct',
    'data_quality.score',
    'market_regime.score',
    'scores.opportunity',
    'scores.timing',
    'scores.risk',
    'scores.confidence',
    'features.rsi14',
    'features.roc20',
    'features.roc60',
    'features.ann_vol',
    'features.volume_ratio',
    'features.pos52w',
    'features.amplitude',
    'features.bar_count'
  ];

  const state = {
    loaded: false,
    loading: false,
    error: null,
    activeTab: 'inbox',
    summary: null,
    dataLink: null,
    rules: [],
    inbox: [],
    outbox: [],
    replay: null,
    replayError: null,
    lastLoadedAt: null
  };

  let bound = false;
  let refreshTimer = null;

  function $(selector) { return document.querySelector(selector); }

  function esc(value) {
    if (F && typeof F.esc === 'function') return F.esc(value == null ? '' : String(value));
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function finite(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : (fallback == null ? 0 : fallback);
  }

  function integer(value, fallback) {
    const number = Number(value);
    return Number.isInteger(number) ? number : (fallback == null ? 0 : fallback);
  }

  function formatMetric(value, suffix) {
    if (value === null || value === undefined || value === '') return '—';
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    const rendered = Math.abs(number) >= 1000 ? Math.round(number).toLocaleString('zh-CN') :
      (Math.abs(number) >= 100 ? number.toFixed(0) : number.toFixed(number % 1 ? 1 : 0));
    return rendered + (suffix || '');
  }

  function relativeTime(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';
    const seconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000));
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return parsed.toLocaleString('zh-CN', { hour12: false });
  }

  function statusTone(value) {
    const text = String(value || '').toUpperCase();
    if (/ONLINE|CONNECTED|READY|OK|DELIVERED|COMPLETE/.test(text)) return 'ok';
    if (/DISABLED|UNKNOWN|PENDING|SNOOZED|DELAYED/.test(text)) return 'muted';
    if (/DEGRADED|STALE|GAP|WARNING|ACKNOWLEDGED/.test(text)) return 'warn';
    if (/OFFLINE|FAILED|ERROR|INVALID|CRITICAL|CONFLICT/.test(text)) return 'bad';
    return 'info';
  }

  function root() { return document.getElementById(ROOT_ID); }

  function runtimeSnapshot() {
    try { return Runtime ? Runtime.snapshot() : {}; } catch (error) { return {}; }
  }

  function isBlocked() {
    const snapshot = runtimeSnapshot();
    return !Runtime || Runtime.isHardFailure(snapshot.status);
  }

  function metricCard(label, value, detail, tone) {
    return '<div class="mon-metric mon-tone-' + esc(tone || 'info') + '">' +
      '<span class="mon-metric-label">' + esc(label) + '</span>' +
      '<strong class="mon-metric-value">' + esc(value) + '</strong>' +
      '<span class="mon-metric-detail">' + esc(detail || '') + '</span>' +
      '</div>';
  }

  function emptyState(title, description, action) {
    return '<div class="mon-empty">' +
      '<span class="mon-empty-mark" aria-hidden="true">∅</span>' +
      '<strong>' + esc(title) + '</strong>' +
      '<p>' + esc(description) + '</p>' +
      (action || '') +
      '</div>';
  }

  function renderStatusRail() {
    const dataLink = state.dataLink || (state.summary && state.summary.data_link) || {};
    const health = dataLink.sidecar_health || dataLink.sidecar || {};
    const metrics = dataLink.sidecar_metrics || {};
    const session = dataLink.sidecar_session || {};
    const store = dataLink.event_store || {};
    const monitor = (state.summary && state.summary.monitor) || {};
    const outbox = monitor.outbox_by_state || {};
    const runtimeWorker = dataLink.runtime_event_worker || {};
    const runtime = runtimeSnapshot();
    return '<div class="mon-rail" aria-label="监控链路状态">' +
      metricCard('ENGINE', runtime.status || 'OFFLINE', runtime.detail || '', statusTone(runtime.status)) +
      metricCard('XTP LINK', dataLink.status || 'DISABLED', health.feed_mode || health.backend || 'read-only', statusTone(dataLink.status)) +
      metricCard('SUBSCRIPTIONS', formatMetric(health.subscription_count || (session.symbols || []).length), 'PoC cap 20', 'info') +
      metricCard('LAST EVENT', relativeTime(health.last_event_at || store.last_event_at), 'callback snapshot', statusTone(health.last_event_at ? 'OK' : 'PENDING')) +
      metricCard('P50 / P95', formatMetric(metrics.latency_p50_ms, 'ms') + ' / ' + formatMetric(metrics.latency_p95_ms, 'ms'), 'receive latency', finite(metrics.latency_p95_ms) > 1000 ? 'bad' : (finite(metrics.latency_p95_ms) > 200 ? 'warn' : 'ok')) +
      metricCard('GAPS', formatMetric(integer(metrics.callback_gap_count) + integer(metrics.provider_gap_count)), 'callback + provider', integer(metrics.callback_gap_count) + integer(metrics.provider_gap_count) ? 'warn' : 'ok') +
      metricCard('OUT OF ORDER', formatMetric(metrics.out_of_order_count), 'never auto-repaired', integer(metrics.out_of_order_count) ? 'warn' : 'ok') +
      metricCard('STORE LAG', formatMetric(store.ingestion_lag_ms, 'ms'), formatMetric(store.event_count) + ' events', finite(store.ingestion_lag_ms) > 5000 ? 'warn' : 'ok') +
      metricCard('OUTBOX', formatMetric(outbox.PENDING || 0), 'monitor drop ' + formatMetric(runtimeWorker.dropped || 0), (outbox.FAILED || integer(runtimeWorker.dropped)) ? 'bad' : (outbox.PENDING ? 'warn' : 'ok')) +
      '</div>';
  }

  function latencyChart() {
    const dataLink = state.dataLink || {};
    const metrics = dataLink.sidecar_metrics || {};
    const p50 = Math.max(0, Math.min(5000, finite(metrics.latency_p50_ms)));
    const p95 = Math.max(p50, Math.min(5000, finite(metrics.latency_p95_ms)));
    if (!p50 && !p95) {
      return emptyState('暂无延迟序列', 'Sidecar 未运行或尚未收到可计算时间戳的行情回调。');
    }
    const width = 620;
    const height = 154;
    const scale = function (value) { return 28 + (Math.min(value, 1000) / 1000) * 540; };
    return '<div class="mon-chart-wrap">' +
      '<svg class="mon-latency-chart" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="XTP 接收延迟 P50 与 P95">' +
      '<line x1="28" y1="116" x2="588" y2="116" class="mon-chart-axis" />' +
      '<line x1="84" y1="26" x2="84" y2="126" class="mon-chart-grid" />' +
      '<line x1="308" y1="26" x2="308" y2="126" class="mon-chart-grid" />' +
      '<line x1="588" y1="26" x2="588" y2="126" class="mon-chart-grid" />' +
      '<rect x="28" y="42" width="' + Math.max(2, scale(p50) - 28) + '" height="22" rx="5" class="mon-bar-p50" />' +
      '<rect x="28" y="82" width="' + Math.max(2, scale(p95) - 28) + '" height="22" rx="5" class="mon-bar-p95" />' +
      '<text x="34" y="38" class="mon-chart-label">P50 ' + esc(formatMetric(p50, 'ms')) + '</text>' +
      '<text x="34" y="78" class="mon-chart-label">P95 ' + esc(formatMetric(p95, 'ms')) + '</text>' +
      '<text x="28" y="142" class="mon-chart-tick">0</text>' +
      '<text x="295" y="142" class="mon-chart-tick">500ms</text>' +
      '<text x="556" y="142" class="mon-chart-tick">1000ms+</text>' +
      '</svg>' +
      '<div class="mon-chart-note">Native SVG · CSP-safe · 不把 callback_seq 冒充交易所序列</div>' +
      '</div>';
  }

  function renderInbox() {
    const rows = Array.isArray(state.inbox) ? state.inbox : [];
    if (!rows.length) {
      return emptyState(
        '信号收件箱为空',
        '只有规则与证据同时满足时才创建监控事件；空白不代表市场安全。',
        '<button class="mon-button mon-button-secondary" type="button" data-monitor-tab="rules">配置规则</button>'
      );
    }
    return '<div class="mon-inbox-list">' + rows.map(function (item) {
      const evidence = item.evidence || {};
      const factEvidence = evidence.facts || {};
      const conditionCount = Array.isArray(factEvidence.conditions) ? factEvidence.conditions.length : 0;
      const actions = item.state === 'NEW' || item.state === 'ACKNOWLEDGED' || item.state === 'SNOOZED' ?
        '<div class="mon-row-actions">' +
          (item.state === 'NEW' ? '<button type="button" data-monitor-transition="ACKNOWLEDGED" data-inbox-id="' + esc(item.inbox_id) + '">ACK</button>' : '') +
          (item.state !== 'SNOOZED' ? '<button type="button" data-monitor-transition="SNOOZED" data-inbox-id="' + esc(item.inbox_id) + '">SNOOZE</button>' : '') +
          '<button type="button" data-monitor-transition="RESOLVED" data-inbox-id="' + esc(item.inbox_id) + '">RESOLVE</button>' +
          '<button type="button" data-monitor-transition="INVALIDATED" data-inbox-id="' + esc(item.inbox_id) + '">INVALIDATE</button>' +
        '</div>' : '';
      return '<article class="mon-inbox-row mon-tone-' + statusTone(item.severity) + '">' +
        '<div class="mon-inbox-time"><strong>' + esc(relativeTime(item.last_triggered_at)) + '</strong><span>×' + esc(item.trigger_count || 1) + '</span></div>' +
        '<div class="mon-inbox-body">' +
          '<div class="mon-inbox-head"><span class="mon-chip mon-chip-' + statusTone(item.state) + '">' + esc(item.state) + '</span>' +
          '<span class="mon-chip mon-chip-' + statusTone(item.severity) + '">' + esc(item.severity) + '</span>' +
          '<code>' + esc(item.symbol) + '</code></div>' +
          '<h3>' + esc(item.title) + '</h3>' +
          '<p>' + esc(item.summary) + '</p>' +
          '<div class="mon-inbox-meta">Rule ' + esc(item.rule_id) + ' · v' + esc(item.rule_version || 1) + ' · ' + esc(conditionCount) + ' conditions · first ' + esc(relativeTime(item.first_triggered_at)) + '</div>' +
          actions +
        '</div>' +
      '</article>';
    }).join('') + '</div>';
  }

  function ruleConditionText(condition) {
    if (!condition) return '—';
    const value = Array.isArray(condition.value) ? condition.value.join(', ') : condition.value;
    return condition.fact + ' ' + condition.operator + ' ' + value;
  }

  function cleanRulePayload(rule, enabled) {
    return {
      rule_id: rule.rule_id,
      name: rule.name,
      expression: rule.expression,
      scope: rule.scope,
      severity: rule.severity,
      enabled: enabled === undefined ? rule.enabled : enabled,
      cooldown_sec: rule.cooldown_sec,
      duplicate_window_sec: rule.duplicate_window_sec,
      expires_at: rule.expires_at || null,
      notification_channels: rule.notification_channels || ['BROWSER']
    };
  }

  function renderRules() {
    const rules = Array.isArray(state.rules) ? state.rules : [];
    const list = rules.length ? '<div class="mon-rule-list">' + rules.map(function (rule) {
      const conditions = (rule.expression && rule.expression.conditions) || [];
      return '<article class="mon-rule-row">' +
        '<div class="mon-rule-state"><span class="mon-switch ' + (rule.enabled ? 'is-on' : '') + '" aria-hidden="true"></span></div>' +
        '<div class="mon-rule-main"><div class="mon-inbox-head"><code>' + esc(rule.rule_id) + '</code>' +
          '<span class="mon-chip mon-chip-info">v' + esc(rule.version || 1) + '</span>' +
          '<span class="mon-chip mon-chip-' + statusTone(rule.severity) + '">' + esc(rule.severity) + '</span></div>' +
          '<h3>' + esc(rule.name) + '</h3>' +
          '<p>' + esc(rule.expression.logic) + ' · ' + esc(conditions.map(ruleConditionText).join(' · ')) + '</p>' +
          '<div class="mon-inbox-meta">' + esc(rule.scope.kind) + ' · max ' + esc(rule.scope.max_symbols) + ' · cooldown ' + esc(rule.cooldown_sec) + 's</div></div>' +
        '<div class="mon-row-actions mon-rule-actions">' +
          '<button type="button" data-monitor-rule-toggle="' + esc(rule.rule_id) + '" data-rule-enabled="' + (rule.enabled ? 'true' : 'false') + '">' + (rule.enabled ? 'DISABLE' : 'ENABLE') + '</button>' +
          '<button type="button" data-monitor-rule-delete="' + esc(rule.rule_id) + '">DELETE</button>' +
        '</div>' +
      '</article>';
    }).join('') + '</div>' : emptyState('暂无监控规则', '先从一个标的、一个条件开始；全市场规则需要显式边界与确认。');

    return '<div class="mon-rules-layout">' +
      '<section class="mon-card"><div class="mon-card-head"><div><span class="mon-eyebrow">RULE REGISTRY</span><h2>规则中心</h2></div><span class="mon-chip mon-chip-info">NON-EVAL</span></div>' + list + '</section>' +
      '<section class="mon-card mon-rule-builder"><div class="mon-card-head"><div><span class="mon-eyebrow">BOUNDED BUILDER</span><h2>新建规则</h2></div></div>' +
        '<form id="monitorRuleForm" autocomplete="off">' +
          '<label>规则名称<input name="name" maxlength="120" required placeholder="例如：P95 延迟超过 200ms" /></label>' +
          '<div class="mon-form-grid">' +
            '<label>标的<input name="symbol" maxlength="9" required value="600519.SH" pattern="[0-9]{6}\.(SH|SZ)" /></label>' +
            '<label>严重度<select name="severity"><option>NOTICE</option><option>WARNING</option><option>CRITICAL</option><option>INFO</option></select></label>' +
            '<label>事实<select name="fact">' +
              '<option value="market_event.latency_p95_ms">XTP P95 延迟</option>' +
              '<option value="market_event.callback_gap_count">Callback Gap</option>' +
              '<option value="market_event.provider_gap_count">Provider Gap</option>' +
              '<option value="market_event.out_of_order_count">Out of order</option>' +
              '<option value="market_event.ingestion_lag_ms">Event store lag</option>' +
              '<option value="market_event.last_price">最新价</option>' +
              '<option value="market_event.change_pct">涨跌幅</option>' +
              '<option value="data_quality.score">数据质量分</option>' +
              '<option value="market_regime.score">市场环境分</option>' +
              '<option value="scores.opportunity">机会分</option>' +
              '<option value="scores.timing">时机分</option>' +
              '<option value="scores.risk">风险分</option>' +
              '<option value="scores.confidence">置信分</option>' +
              '<option value="features.rsi14">RSI14</option>' +
              '<option value="features.roc20">ROC20</option>' +
              '<option value="features.roc60">ROC60</option>' +
              '<option value="features.ann_vol">年化波动率</option>' +
              '<option value="features.volume_ratio">量比</option>' +
              '<option value="features.pos52w">52周位置</option>' +
              '<option value="features.amplitude">振幅</option>' +
              '<option value="features.bar_count">有效K线数</option>' +
              '<option value="data_status">数据状态</option>' +
              '<option value="signal_state">信号状态</option>' +
              '<option value="action_state">动作状态</option>' +
            '</select></label>' +
            '<label>比较<select name="operator"><option>GE</option><option>GT</option><option>EQ</option><option>NE</option><option>LE</option><option>LT</option></select></label>' +
            '<label>阈值<input name="value" maxlength="64" required value="200" /></label>' +
            '<label>冷却秒数<input name="cooldown" type="number" min="0" max="86400" value="300" /></label>' +
          '</div>' +
          '<div class="mon-form-note">规则只能观察已允许事实；不会改分、改 ActionState、训练模型或创建订单。</div>' +
          '<button class="mon-button" type="submit">CREATE RULE</button>' +
        '</form>' +
      '</section>' +
    '</div>';
  }

  function renderDataLink() {
    const link = state.dataLink || {};
    const health = link.sidecar_health || link.sidecar || {};
    const session = link.sidecar_session || {};
    const metrics = link.sidecar_metrics || {};
    const store = link.event_store || {};
    const runtimeWorker = link.runtime_event_worker || {};
    const notificationWorker = link.notification_worker || {};
    const lastPoll = link.last_poll || {};
    const integrity = lastPoll.integrity || {};
    const integrityPassed = typeof integrity.passed === 'boolean' ? integrity.passed :
      (typeof store.integrity_passed === 'boolean' ? store.integrity_passed : null);
    const rows = [
      ['Deployment', link.status || 'DISABLED', 'read-only quote lane'],
      ['Backend', health.backend || session.backend || 'SIMULATOR', 'official binding remains operational gate'],
      ['Feed mode', health.feed_mode || session.feed_mode || '—', 'LEVEL1 / LEVEL2 / SIMULATOR'],
      ['Session', session.session_id ? 'PRESENT' : 'PENDING', 'identity hidden from compact rail'],
      ['Callbacks', formatMetric(metrics.callback_count), 'local callback count'],
      ['Duplicates', formatMetric(metrics.duplicate_count), 'not silently discarded from evidence'],
      ['Callback gaps', formatMetric(metrics.callback_gap_count), 'local sequence only'],
      ['Provider gaps', formatMetric(metrics.provider_gap_count), 'only when provider sequence exists'],
      ['Out of order', formatMetric(metrics.out_of_order_count), 'never auto-repaired'],
      ['Reconnects', formatMetric(metrics.reconnect_count), 'session boundary must remain visible'],
      ['Monitor worker', runtimeWorker.running ? 'RUNNING' : 'STOPPED', formatMetric(runtimeWorker.queue_size) + '/' + formatMetric(runtimeWorker.queue_capacity) + ' queued'],
      ['Monitor events', formatMetric(runtimeWorker.processed) + ' / ' + formatMetric(runtimeWorker.dropped), 'processed / dropped; never blocks signal pipeline'],
      ['Notification worker', notificationWorker.running ? 'RUNNING' : 'STOPPED', notificationWorker.last_error_code || 'bounded outbox lease'],
      ['Partitions', formatMetric(store.partition_count), formatMetric(store.event_count) + ' immutable records'],
      ['Integrity', integrityPassed === true ? 'PASSED' : (integrityPassed === false ? 'FAILED' : 'NOT RUN'), 'SHA-256 chain + manifest'],
      ['Last ingestion', lastPoll.completed_at ? relativeTime(lastPoll.completed_at) : '—', lastPoll.accepted != null ? String(lastPoll.accepted) + ' accepted' : 'no poll']
    ];
    return '<div class="mon-data-layout">' +
      '<section class="mon-card"><div class="mon-card-head"><div><span class="mon-eyebrow">DATA LINK</span><h2>链路与存储</h2></div><span class="mon-chip mon-chip-' + statusTone(link.status) + '">' + esc(link.status || 'DISABLED') + '</span></div>' +
      '<div class="mon-definition-list">' + rows.map(function (row) {
        return '<div><span>' + esc(row[0]) + '</span><strong>' + esc(row[1]) + '</strong><small>' + esc(row[2]) + '</small></div>';
      }).join('') + '</div></section>' +
      '<section class="mon-card"><div class="mon-card-head"><div><span class="mon-eyebrow">LATENCY</span><h2>接收延迟</h2></div><span class="mon-chip mon-chip-info">NO WIRE CLAIM</span></div>' + latencyChart() + '</section>' +
      '<section class="mon-card mon-boundary-card"><div class="mon-card-head"><div><span class="mon-eyebrow">BOUNDARIES</span><h2>证据与账户边界</h2></div></div>' +
        '<ul class="mon-boundary-list"><li>股票测试账户：已登记，真实行情验收待本机环境配置</li><li>算法账户：不使用</li><li>Trader / Order / Algo API：未接入</li><li>allow_live_decision / allow_model_training / auto_trade：false</li><li>callback_seq：仅本地回调顺序，不宣称为交易所序列</li></ul>' +
      '</section>' +
    '</div>';
  }

  function replayChart() {
    const replay = state.replay || {};
    const bars = Array.isArray(replay.minute_bars) ? replay.minute_bars.filter(function (bar) {
      return Number.isFinite(Number(bar.open)) && Number.isFinite(Number(bar.high)) &&
        Number.isFinite(Number(bar.low)) && Number.isFinite(Number(bar.close));
    }) : [];
    if (!bars.length) {
      return emptyState('暂无 Replay 数据', '选择已捕获标的和时间窗口；没有事件时保持空白，不生成示例收益或信号。');
    }
    const limited = bars.slice(-80);
    const lows = limited.map(function (bar) { return Number(bar.low); });
    const highs = limited.map(function (bar) { return Number(bar.high); });
    const minimum = Math.min.apply(null, lows);
    const maximum = Math.max.apply(null, highs);
    const range = Math.max(maximum - minimum, maximum * 0.002, 0.01);
    const width = 780;
    const height = 280;
    const left = 42;
    const right = 16;
    const top = 20;
    const bottom = 34;
    const chartWidth = width - left - right;
    const chartHeight = height - top - bottom;
    const candleWidth = Math.max(2, Math.min(8, chartWidth / limited.length * 0.58));
    function y(value) { return top + (maximum - value) / range * chartHeight; }
    const candles = limited.map(function (bar, index) {
      const x = left + (index + 0.5) / limited.length * chartWidth;
      const open = Number(bar.open);
      const close = Number(bar.close);
      const high = Number(bar.high);
      const low = Number(bar.low);
      const rising = close >= open;
      const bodyTop = Math.min(y(open), y(close));
      const bodyHeight = Math.max(1.5, Math.abs(y(open) - y(close)));
      return '<g class="' + (rising ? 'mon-candle-up' : 'mon-candle-down') + '">' +
        '<line x1="' + x.toFixed(2) + '" y1="' + y(high).toFixed(2) + '" x2="' + x.toFixed(2) + '" y2="' + y(low).toFixed(2) + '" />' +
        '<rect x="' + (x - candleWidth / 2).toFixed(2) + '" y="' + bodyTop.toFixed(2) + '" width="' + candleWidth.toFixed(2) + '" height="' + bodyHeight.toFixed(2) + '" />' +
      '</g>';
    }).join('');
    return '<div class="mon-chart-wrap"><svg class="mon-replay-chart" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="本地事件 Replay K线">' +
      '<line x1="' + left + '" y1="' + (top + chartHeight) + '" x2="' + (width - right) + '" y2="' + (top + chartHeight) + '" class="mon-chart-axis" />' +
      '<line x1="' + left + '" y1="' + top + '" x2="' + (width - right) + '" y2="' + top + '" class="mon-chart-grid" />' +
      '<text x="4" y="' + (top + 5) + '" class="mon-chart-tick">' + esc(maximum.toFixed(2)) + '</text>' +
      '<text x="4" y="' + (top + chartHeight) + '" class="mon-chart-tick">' + esc(minimum.toFixed(2)) + '</text>' +
      candles + '</svg><div class="mon-chart-note">' + esc(limited.length) + ' minute bars · ' + esc(replay.backend_used || 'python') + ' replay · persisted bars remain DELAYED</div></div>';
  }

  function replayDefaultWindow() {
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
    function local(value) {
      const offset = value.getTimezoneOffset() * 60000;
      return new Date(value.getTime() - offset).toISOString().slice(0, 16);
    }
    return { start: local(start), end: local(end) };
  }

  function renderReplay() {
    const defaults = replayDefaultWindow();
    const replay = state.replay || {};
    return '<div class="mon-replay-layout">' +
      '<section class="mon-card mon-replay-controls"><div class="mon-card-head"><div><span class="mon-eyebrow">LOCAL REPLAY</span><h2>事件回放</h2></div><span class="mon-chip mon-chip-muted">NO PERFORMANCE CLAIM</span></div>' +
        '<form id="monitorReplayForm">' +
          '<label>标的<input name="symbol" required value="600519.SH" pattern="[0-9]{6}\.(SH|SZ)" /></label>' +
          '<label>开始<input name="start" type="datetime-local" required value="' + esc(defaults.start) + '" /></label>' +
          '<label>结束<input name="end" type="datetime-local" required value="' + esc(defaults.end) + '" /></label>' +
          '<label>Backend<select name="backend"><option value="python">Python</option><option value="auto">Auto</option><option value="duckdb">DuckDB</option></select></label>' +
          '<button class="mon-button" type="submit">RUN REPLAY</button>' +
        '</form>' +
        '<div class="mon-form-note">Replay 只读取独立 Market Event Store；不读取或修改生产持仓数据库。</div>' +
        (state.replayError ? '<div class="mon-inline-error">' + esc(state.replayError) + '</div>' : '') +
      '</section>' +
      '<section class="mon-card mon-replay-chart-card"><div class="mon-card-head"><div><span class="mon-eyebrow">EVENT-BOUND OHLC</span><h2>' + esc(replay.symbol || '等待查询') + '</h2></div>' +
        (replay.row_count != null ? '<span class="mon-chip mon-chip-info">' + esc(replay.row_count) + ' EVENTS</span>' : '') + '</div>' + replayChart() + '</section>' +
    '</div>';
  }

  function panelContent() {
    if (state.activeTab === 'rules') return renderRules();
    if (state.activeTab === 'data') return renderDataLink();
    if (state.activeTab === 'replay') return renderReplay();
    return '<section class="mon-card"><div class="mon-card-head"><div><span class="mon-eyebrow">SIGNAL INBOX</span><h2>待处理监控事件</h2></div><span class="mon-chip mon-chip-info">' + esc(state.inbox.length) + ' EVENTS</span></div>' + renderInbox() + '</section>';
  }

  function render() {
    const element = root();
    if (!element) return;
    if (isBlocked()) {
      const snapshot = runtimeSnapshot();
      element.innerHTML = emptyState(
        'Monitor Workspace 暂不可用',
        snapshot.detail || '先恢复 Runtime Health、版本握手和私有访问边界。',
        '<button class="mon-button mon-button-secondary" type="button" data-monitor-reload>RETRY</button>'
      );
      return;
    }
    if (state.loading && !state.loaded) {
      element.innerHTML = '<div class="mon-loading"><span></span><strong>加载 Monitor Workspace…</strong><small>读取规则、收件箱、链路健康和本地 Replay 元数据</small></div>';
      return;
    }
    if (state.error && !state.loaded) {
      element.innerHTML = emptyState(
        'Monitor 数据加载失败',
        state.error,
        '<button class="mon-button mon-button-secondary" type="button" data-monitor-reload>RETRY</button>'
      );
      return;
    }
    element.innerHTML = renderStatusRail() +
      '<div class="mon-tabs" role="tablist" aria-label="Monitor workspace sections">' +
        [['inbox', '信号收件箱'], ['rules', '规则中心'], ['data', '数据链路'], ['replay', 'Replay']].map(function (item) {
          return '<button type="button" role="tab" aria-selected="' + (state.activeTab === item[0] ? 'true' : 'false') + '" class="mon-tab ' + (state.activeTab === item[0] ? 'active' : '') + '" data-monitor-tab="' + item[0] + '">' + esc(item[1]) + '</button>';
        }).join('') +
        '<button type="button" class="mon-refresh" data-monitor-reload>' + (state.loading ? 'REFRESHING…' : 'REFRESH') + '</button>' +
      '</div>' +
      (state.error ? '<div class="mon-inline-error">' + esc(state.error) + '</div>' : '') +
      '<div class="mon-panel">' + panelContent() + '</div>' +
      '<div class="mon-footnote">Last loaded ' + esc(state.lastLoadedAt ? relativeTime(state.lastLoadedAt) : '—') + ' · Decision Mode remains authoritative for user actions · Monitor events are observational only.</div>';
  }

  function failureMessage(results) {
    for (let index = 0; index < results.length; index += 1) {
      const result = results[index];
      if (result.status === 'rejected') {
        const error = result.reason || {};
        return error.message || error.code || 'Monitor API request failed';
      }
    }
    return null;
  }

  async function load(options) {
    options = options || {};
    if (!API || state.loading) return false;
    state.loading = true;
    if (!options.silent) render();
    const results = await Promise.allSettled([
      API.getMonitorSummary(),
      API.getMonitorDataLink(),
      API.getMonitorRules(),
      API.getMonitorInbox({ limit: 200 }),
      API.getMonitorOutbox(100)
    ]);
    state.loading = false;
    state.error = failureMessage(results);
    if (results[0].status === 'fulfilled') state.summary = results[0].value;
    if (results[1].status === 'fulfilled') state.dataLink = results[1].value;
    if (results[2].status === 'fulfilled') state.rules = results[2].value || [];
    if (results[3].status === 'fulfilled') state.inbox = results[3].value || [];
    if (results[4].status === 'fulfilled') state.outbox = results[4].value || [];
    state.loaded = results.some(function (result) { return result.status === 'fulfilled'; });
    state.lastLoadedAt = new Date().toISOString();
    render();
    return !state.error;
  }

  function scheduleReload() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { load({ silent: true }); }, 160);
  }

  function handleSSE(type, payload) {
    if (type !== 'monitor.inbox' && type !== 'monitor.notification') return;
    if (type === 'monitor.inbox' && payload && payload.inbox) {
      const incoming = payload.inbox;
      const index = state.inbox.findIndex(function (item) { return item.inbox_id === incoming.inbox_id; });
      if (index === -1) state.inbox.unshift(incoming);
      else state.inbox[index] = incoming;
      render();
    }
    scheduleReload();
  }

  async function transition(inboxId, target) {
    if (!API || !inboxId || ACTIVE_STATES.indexOf(target) === -1 && ['RESOLVED', 'INVALIDATED'].indexOf(target) === -1) return;
    const payload = { state: target, reason: 'Monitor Workspace user action' };
    if (target === 'SNOOZED') payload.snooze_sec = 900;
    try {
      await API.transitionMonitorInbox(inboxId, payload);
      await load({ silent: true });
    } catch (error) {
      state.error = error.message || 'Monitor transition failed';
      render();
    }
  }

  function parseRuleForm(form) {
    const data = new FormData(form);
    const fact = String(data.get('fact') || '');
    const rawValue = String(data.get('value') || '').trim();
    const value = NUMERIC_FACTS.indexOf(fact) !== -1 ? Number(rawValue) : rawValue;
    if (NUMERIC_FACTS.indexOf(fact) !== -1 && !Number.isFinite(value)) {
      throw new Error('数值阈值无效');
    }
    const symbol = String(data.get('symbol') || '').trim().toUpperCase();
    if (!/^[0-9]{6}\.(SH|SZ)$/.test(symbol)) throw new Error('标的必须使用 CODE.SH / CODE.SZ');
    const name = String(data.get('name') || '').trim();
    if (!name) throw new Error('规则名称不能为空');
    const cooldown = Number(data.get('cooldown'));
    if (!Number.isInteger(cooldown) || cooldown < 0 || cooldown > 86400) {
      throw new Error('冷却秒数必须是 0—86400 的整数');
    }
    const ruleId = 'mon-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
    return {
      rule_id: ruleId,
      name: name,
      expression: {
        logic: 'AND',
        conditions: [{ fact: fact, operator: String(data.get('operator') || 'GE'), value: value }]
      },
      scope: {
        kind: 'SYMBOLS',
        symbols: [symbol],
        market: 'A',
        max_symbols: 1,
        all_market_acknowledged: false
      },
      severity: String(data.get('severity') || 'NOTICE'),
      enabled: true,
      cooldown_sec: cooldown,
      duplicate_window_sec: Math.max(cooldown, 300),
      expires_at: null,
      notification_channels: ['BROWSER']
    };
  }

  async function createRule(form) {
    try {
      const payload = parseRuleForm(form);
      await API.createMonitorRule(payload);
      form.reset();
      await load({ silent: true });
    } catch (error) {
      state.error = error.message || '规则创建失败';
      render();
    }
  }

  async function toggleRule(ruleId, enabled) {
    const rule = state.rules.find(function (item) { return item.rule_id === ruleId; });
    if (!rule) return;
    try {
      await API.updateMonitorRule(ruleId, cleanRulePayload(rule, !enabled));
      await load({ silent: true });
    } catch (error) {
      state.error = error.message || '规则更新失败';
      render();
    }
  }

  async function deleteRule(ruleId) {
    try {
      await API.deleteMonitorRule(ruleId);
      await load({ silent: true });
    } catch (error) {
      state.error = error.message || '规则删除被拒绝；存在审计历史时请禁用规则';
      render();
    }
  }

  async function runReplay(form) {
    const data = new FormData(form);
    const symbol = String(data.get('symbol') || '').trim().toUpperCase();
    const startText = String(data.get('start') || '');
    const endText = String(data.get('end') || '');
    const start = new Date(startText);
    const end = new Date(endText);
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(endText) && !Number.isNaN(end.getTime())) {
      end.setSeconds(59, 999);
    }
    if (!/^[0-9]{6}\.(SH|SZ)$/.test(symbol) || Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end <= start) {
      state.replayError = 'Replay 标的或时间窗口无效';
      render();
      return;
    }
    state.replayError = null;
    try {
      state.replay = await API.getMonitorReplay({
        symbol: symbol,
        start: start.toISOString(),
        end: end.toISOString(),
        backend: String(data.get('backend') || 'python'),
        limit: 5000
      });
    } catch (error) {
      state.replay = null;
      state.replayError = error.message || 'Replay 查询失败';
    }
    render();
  }

  function bind() {
    if (bound) return;
    bound = true;
    document.addEventListener('click', function (event) {
      const tab = event.target.closest('[data-monitor-tab]');
      if (tab) {
        state.activeTab = tab.dataset.monitorTab;
        render();
        return;
      }
      if (event.target.closest('[data-monitor-reload]')) {
        load();
        return;
      }
      const transitionButton = event.target.closest('[data-monitor-transition]');
      if (transitionButton) {
        transition(transitionButton.dataset.inboxId, transitionButton.dataset.monitorTransition);
        return;
      }
      const toggle = event.target.closest('[data-monitor-rule-toggle]');
      if (toggle) {
        toggleRule(toggle.dataset.monitorRuleToggle, toggle.dataset.ruleEnabled === 'true');
        return;
      }
      const remove = event.target.closest('[data-monitor-rule-delete]');
      if (remove) deleteRule(remove.dataset.monitorRuleDelete);
    });
    document.addEventListener('submit', function (event) {
      if (event.target && event.target.id === 'monitorRuleForm') {
        event.preventDefault();
        createRule(event.target);
      }
      if (event.target && event.target.id === 'monitorReplayForm') {
        event.preventDefault();
        runReplay(event.target);
      }
    });
  }

  function clear(error) {
    state.loaded = false;
    state.loading = false;
    state.error = error ? (error.message || error.code || String(error)) : null;
    state.summary = null;
    state.dataLink = null;
    state.rules = [];
    state.inbox = [];
    state.outbox = [];
    state.replay = null;
    state.replayError = null;
    render();
  }

  function onRuntimeSnapshot(snapshot) {
    if (!Runtime) return;
    if (Runtime.isHardFailure(snapshot && snapshot.status)) clear(snapshot);
    else if (state.loaded) render();
  }

  function init() {
    bind();
    render();
  }

  global.MonitorWorkspace = Object.freeze({
    init: init,
    load: load,
    activate: function () { return state.loaded ? (render(), Promise.resolve(true)) : load(); },
    render: render,
    clear: clear,
    onRuntimeSnapshot: onRuntimeSnapshot,
    handleSSE: handleSSE,
    snapshot: function () { return JSON.parse(JSON.stringify(state)); }
  });
})(window);
