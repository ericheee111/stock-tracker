/* =========================================================================
 * format.js —— 数字 / 百分比 / 涨跌色 / 数据年龄 / 分数色阶 格式化工具
 * 全局对象：Fmt
 * 约定：A股习惯 —— 涨=红、跌=绿（chgClass 返回 up=红 / down=绿）。
 *       数据新鲜度（LIVE/DELAYED/STALE/UNKNOWN）用独立于涨跌的状态色。
 * ========================================================================= */
(function (global) {
  'use strict';

  /** 安全取默认值 */
  function def(v, d) {
    return (v === undefined || v === null) ? (d === undefined ? null : d) : v;
  }

  /** 转数字，失败返回 0 */
  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  /** 百分比计算（分母保护） */
  function pct(part, whole) {
    return whole ? (part / whole) * 100 : 0;
  }

  /** 价格：固定 2 位小数 + 千分位 */
  function fmtPrice(v) {
    const n = num(v);
    if (!Number.isFinite(n)) return '—';
    return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /** 整数千分位 */
  function fmtInt(v) {
    const n = num(v);
    if (!Number.isFinite(n)) return '—';
    return Math.round(n).toLocaleString('zh-CN');
  }

  /** 带正负号的百分比文本 */
  function fmtPct(v) {
    const n = num(v);
    if (!Number.isFinite(n)) return '—';
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  /** 区间文本（入场区间等） */
  function fmtRange(a, b) {
    const na = num(a), nb = num(b);
    if (na && nb) return fmtPrice(a) + ' ~ ' + fmtPrice(b);
    if (na) return fmtPrice(a);
    if (nb) return fmtPrice(b);
    return '—';
  }

  /** 涨跌色 class（A股：正=up 红，负=down 绿，零=flat） */
  function chgClass(v) {
    const n = num(v);
    if (n > 0) return 'up';
    if (n < 0) return 'down';
    return 'flat';
  }

  /** 数据观察年龄 → 人话（"12s 前" / "15m 前" / "2h 前"） */
  function fmtAge(ms) {
    if (ms === undefined || ms === null || !Number.isFinite(ms)) return '—';
    const s = Math.round(ms / 1000);
    if (s < 60) return s + 's 前';
    const m = Math.round(s / 60);
    if (m < 60) return m + 'm 前';
    const h = Math.round(m / 60);
    return h + 'h 前';
  }

  /** 数据状态徽章 HTML（与涨跌色解耦） */
  function statusBadge(status, ageMs) {
    const s = String(def(status, 'UNKNOWN') || 'UNKNOWN').toUpperCase();
    const map = {
      LIVE:    { t: '实时', c: 'status-live' },
      DELAYED: { t: '延迟', c: 'status-delayed' },
      STALE:   { t: '过期', c: 'status-stale' },
      UNKNOWN: { t: '未知', c: 'status-unknown' }
    };
    const info = map[s] || map.UNKNOWN;
    const age = (ageMs !== undefined && ageMs !== null && Number.isFinite(ageMs))
      ? (' ' + fmtAge(ageMs)) : '';
    return '<span class="ds-badge ' + info.c + '">' + info.t + age + '</span>';
  }

  /** 分数色阶（0–100）。invert=true 表示高分=差（如风险分） */
  function scoreColor(v, invert) {
    const x = num(v);
    if (invert) {
      if (x <= 25) return 'var(--score-good)';
      if (x <= 50) return 'var(--score-mid)';
      if (x <= 75) return 'var(--score-low)';
      return 'var(--score-poor)';
    }
    if (x >= 75) return 'var(--score-good)';
    if (x >= 50) return 'var(--score-mid)';
    if (x >= 25) return 'var(--score-low)';
    return 'var(--score-poor)';
  }

  /**
   * 从 Quote dict 抽取最新价：优先 last。
   * 缺失/非法的最新价（null / undefined / 0 / 非数）→ 破折号「—」，
   * 绝不把后端的 None（缺失）渲染成 "0.00"（修复 quote.last=0 时指数/个股显示 0.00）。
   */
  function quotePrice(q) {
    if (!q) return '—';
    const last = q.last;
    if (last === undefined || last === null ||
        !Number.isFinite(Number(last)) || Number(last) === 0) {
      return '—';
    }
    return fmtPrice(last);
  }

  /**
   * 从 Quote dict 抽取涨跌幅(%)：
   *   1) 若含 change_pct 直接使用；
   *   2) 否则用 last vs prev_close / open 推导；
   *   3) 都缺失返回 0。
   */
  function quoteChangePct(q) {
    if (!q) return 0;
    if (q.change_pct !== undefined && q.change_pct !== null) return num(q.change_pct);
    const last = num(def(q.last, q.close));
    if (q.prev_close) return pct(last - q.prev_close, q.prev_close);
    if (q.open) return pct(last - q.open, q.open);
    return 0;
  }

  /** ISO 时间 → 本地 HH:MM:SS（失败返回 '—'） */
  function fmtClock(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /** HTML 转义（防御 XSS，所有外部文本必须经此） */
  function esc(s) {
    if (s === undefined || s === null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  global.Fmt = {
    def: def, num: num, pct: pct,
    fmtPrice: fmtPrice, fmtInt: fmtInt, fmtPct: fmtPct, fmtRange: fmtRange,
    chgClass: chgClass, fmtAge: fmtAge, statusBadge: statusBadge,
    scoreColor: scoreColor, quotePrice: quotePrice, quoteChangePct: quoteChangePct,
    fmtClock: fmtClock, esc: esc
  };
})(window);
