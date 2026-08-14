/* =========================================================================
 * components.js —— 卡片 / 雷达 / 信号详情 / Why-Not-Buy / Next-Trigger /
 *                 What-Changed / 板块 / 源健康 渲染函数
 * 全局对象：UI
 * 所有字段读取均做空值/缺字段防御：某字段缺失不应整页崩溃。
 * 信号状态机人话映射对齐 §15.1；雷达分层对齐 §4.3。
 * ========================================================================= */
(function (global) {
  'use strict';

  const F = global.Fmt;
  const esc = F.esc;

  /* ---------------- 枚举：信号状态（§15.1 人话映射） ---------------- */
  const STATE_LABELS = {
    COLD: '暂无机会', WATCH: '值得观察', ARMED_BREAKOUT: '等突破',
    ARMED_PULLBACK: '等回踩确认', TRIGGERED: '已触发可执行', ACTIVE: '持有逻辑仍在',
    TRIM: '考虑减仓', EXIT: '退出/逻辑失效', OVEREXTENDED: '强势但禁止追高',
    INVALIDATED: '计划失效', DATA_INVALID: '数据不足暂不发信号', EXPIRED: '已过期'
  };
  const STATE_COLORS = {
    COLD: '#8e8e93', WATCH: '#64d2ff', ARMED_BREAKOUT: '#bf5af2',
    ARMED_PULLBACK: '#5e5ce6', TRIGGERED: '#30d158', ACTIVE: '#30d158',
    TRIM: '#ffd60a', EXIT: '#ff453a', OVEREXTENDED: '#ff9f0a',
    INVALIDATED: '#ff453a', DATA_INVALID: '#ff9f0a', EXPIRED: '#8e8e93'
  };

  /* ---------------- 枚举：市场 Regime 五态（§3.1 / §8） ---------------- */
  const REGIME_LABELS = {
    RISK_ON_TREND: '风险偏好 · 趋势上行', ROTATION: '轮动', RISK_OFF: '风险规避',
    PANIC_REBOUND: '恐慌反弹', OVERHEATED: '过热'
  };

  /* ---------------- 枚举：板块生命周期（§3.1） ---------------- */
  const SECTOR_STAGE_LABELS = {
    EARLY: '萌芽', ACCUMULATION: '蓄势', LEADING: '领涨',
    PEAK: '见顶', DIVERGENCE: '背离', DECLINE: '退潮'
  };
  const SECTOR_STAGE_COLORS = {
    EARLY: '#8e8e93', ACCUMULATION: '#64d2ff', LEADING: '#30d158',
    PEAK: '#ffd60a', DIVERGENCE: '#ff9f0a', DECLINE: '#ff453a'
  };

  /* ---------------- 枚举：风险事件等级（§9 契约 level 字段） ---------------- */
  // 与 SECTOR_STAGE_COLORS / risk-gate 配色风格一致（玻璃拟态深色）。
  const RISK_LEVEL_COLORS = {
    HIGH: '#ff453a',   // 红：高风险
    MEDIUM: '#ff9f0a', // 琥珀：中风险
    LOW: '#8e8e93'     // 灰：低风险
  };
  const RISK_LEVEL_LABELS = {
    HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险'
  };

  /* ---------------- 雷达分组（§4.3 六/七组分层） ---------------- */
  const RADAR_GROUP_ORDER = [
    '可执行', '等一个条件', '等突破', '等回踩', '禁止追高', '早期观察', '数据不足', '已结束'
  ];
  function radarGroupOf(state) {
    switch (state) {
      case 'TRIGGERED': case 'ACTIVE': return '可执行';
      case 'WATCH': return '等一个条件';
      case 'ARMED_BREAKOUT': return '等突破';
      case 'ARMED_PULLBACK': return '等回踩';
      case 'OVEREXTENDED': return '禁止追高';
      case 'COLD': return '早期观察';
      case 'DATA_INVALID': return '数据不足';
      default: return '已结束'; // INVALIDATED / EXIT / TRIM / EXPIRED
    }
  }
  function groupClassOf(group) {
    switch (group) {
      case '可执行': return 'g-exec';
      case '等一个条件': return 'g-watch';
      case '等突破': return 'g-breakout';
      case '等回踩': return 'g-pullback';
      case '禁止追高': return 'g-overext';
      case '早期观察': return 'g-early';
      case '数据不足': return 'g-data';
      default: return '';
    }
  }

  /* ---------------- 通用小部件 ---------------- */
  function stateBadge(state) {
    const label = STATE_LABELS[state] || state || '—';
    const color = STATE_COLORS[state] || '#8e8e93';
    return '<span class="badge" style="background:' + color + '">' + esc(label) + '</span>';
  }

  function scoreCircle(label, value, invert) {
    const v = F.num(value);
    const color = F.scoreColor(v, invert);
    return '<div class="score-cell">' +
      '<div class="score-circle" style="--sc:' + color + '">' + v + '</div>' +
      '<div class="score-label">' + esc(label) + '</div>' +
      '</div>';
  }

  /** 四分数网格（ScoreSet：opportunity/timing/risk/confidence） */
  function renderScores(score) {
    if (!score) return '<div class="card-empty">暂无评分</div>';
    return '<div class="score-grid">' +
      scoreCircle('机会', score.opportunity, false) +
      scoreCircle('时机', score.timing, false) +
      scoreCircle('风险', score.risk, true) +
      scoreCircle('置信', score.confidence, false) +
      '</div>';
  }

  function planRow(k, v) {
    return '<div class="plan-row"><span class="plan-k">' + esc(k) + '</span>' +
      '<span class="plan-v">' + esc(v) + '</span></div>';
  }

  function renderWhatChanged(list) {
    if (!list || !list.length) return '';
    const tags = list.map(function (t) { return '<span class="wc-tag">' + esc(t) + '</span>'; }).join('');
    return '<div class="wc-tags">' + tags + '</div>';
  }

  function nextTriggerBox(text) {
    if (!text) return '';
    return '<div class="next-trigger"><span class="nt-ico">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
      '</span><span>' + esc(text) + '</span></div>';
  }

  /** 拥挤度 / 追高风险仪表（§24.6）。数据来自后端 crowding（纯展示启发式）。
   *  展示：档位标签 + 0—100 分 + 进度条 + 因子明细。某字段缺失则整块不渲染。 */
  function renderCrowdingGauge(c) {
    if (!c || typeof c !== 'object') return '';
    const score = F.num(c.score);
    const color = c.color || '#8e8e93';
    const level = c.level || '—';
    const meterW = Math.max(4, Math.min(100, score));
    const factors = Array.isArray(c.factors) ? c.factors : [];
    const factorHtml = factors.length
      ? '<div class="crowd-factors">' + factors.map(function (f) {
          return '<span class="crowd-factor">' + esc(f) + '</span>';
        }).join('') + '</div>'
      : '';
    return '<div class="crowd-gauge">' +
      '<div class="crowd-head"><span class="crowd-label">拥挤度</span>' +
      '<span class="crowd-level" style="color:' + color + '">' + esc(level) + '</span>' +
      '<span class="crowd-score">' + score + '</span></div>' +
      '<div class="crowd-meter"><div class="crowd-fill" style="width:' + meterW + '%;background:' + color + '"></div></div>' +
      factorHtml +
      '</div>';
  }

  /** 从 Signal 抽取统一评分对象（兼容 score 嵌套或顶层平铺） */
  function pickScore(sig) {
    if (sig && sig.score) return sig.score;
    if (!sig) return null;
    return {
      opportunity: sig.opportunity, timing: sig.timing, risk: sig.risk, confidence: sig.confidence,
      positive_reasons: sig.positive_reasons, negative_reasons: sig.negative_reasons
    };
  }

  function card(titleHtml, bodyHtml, extraClass) {
    return '<div class="card ' + (extraClass || '') + '">' +
      (titleHtml ? '<div class="card-title">' + titleHtml + '</div>' : '') +
      bodyHtml + '</div>';
  }

  function loadingBox(text) {
    return '<div class="loading-box">' + esc(text || '数据加载中…') + '</div>';
  }

  /* ============================================================
   * 顶部数据模式横幅（meta.data_mode 真实/降级/演示 可见）
   * ============================================================ */
  function renderBanner(meta, providers) {
    if (!meta) return '<div class="banner-loading">正在连接后端，加载真实行情…</div>';
    const mode = String(F.def(meta.data_mode, 'UNKNOWN') || 'UNKNOWN').toUpperCase();
    const modeMap = {
      LIVE: '真实行情 · LIVE', DEGRADED: '数据降级 · DEGRADED',
      DEMO: '演示数据 · DEMO', UNKNOWN: '数据模式未知'
    };
    const textMap = {
      LIVE: '数据来自腾讯 / 东财 / 新浪真实行情接口，经数据质量闸门校验后展示。',
      DEGRADED: '部分数据源异常或被熔断，已降级展示；延迟 / 过期数据已逐条标注，请谨慎参考。',
      DEMO: '当前为演示数据，非真实行情。',
      UNKNOWN: '未能识别数据模式，请核对后端 meta.data_mode 字段。'
    };
    const modeText = modeMap[mode] || mode;
    const text = textMap[mode] || textMap.UNKNOWN;

    let provHtml = '';
    const provSrc = (Array.isArray(providers) && providers.length) ? providers : (Array.isArray(meta.providers) ? meta.providers : null);
    if (provSrc) {
      provHtml = provSrc.map(function (p) {
        if (typeof p === 'string') return '<span class="banner-meta">' + esc(p) + '</span>';
        const name = p.provider || p.name || '?';
        const cs = p.circuit_state || '';
        return '<span class="banner-meta">' + esc(name) + (cs ? (' · ' + esc(cs)) : '') + '</span>';
      }).join('');
    }

    const upd = F.fmtClock(meta.last_update);
    const mo = meta.market_open || {};
    const moText = ['a', 'hk', 'us'].map(function (m) {
      const st = String(mo[m] || '').toUpperCase();
      const lbl = { a: 'A', hk: '港', us: '美' }[m];
      // 后端 market_open_status 返回字符串状态，需映射为中文（收市态明确标注）
      const v = st === 'TRADING' ? '交易中'
        : st === 'WEEKEND' ? '周末休市'
        : st === 'CLOSED' ? '已收市'
        : st === 'DISABLED' ? '已停用'
        : '?';
      return lbl + ':' + v;
    }).join('  ');

    return '<span class="banner-dot"></span>' +
      '<span class="banner-mode">' + esc(modeText) + '</span>' +
      '<span class="banner-text">' + esc(text) + '</span>' +
      '<span class="banner-meta">更新 ' + upd + ' · ' + moText + '</span>' +
      provHtml;
  }

  function bannerModeClass(meta) {
    if (!meta) return 'error';
    const mode = String(F.def(meta.data_mode, 'UNKNOWN') || 'UNKNOWN').toUpperCase();
    if (mode === 'LIVE') return 'live';
    if (mode === 'DEGRADED') return 'degraded';
    if (mode === 'DEMO') return 'demo';
    return 'unknown';
  }

  /**
   * 判断是否「全部已收市」：三市场（A/港/美）均 CLOSED 或 WEEKEND，且至少
   * 有一个启用市场。DISABLED 市场不参与判定。用于收市态面板与横幅「已收市」样式。
   */
  function isAllMarketsClosed(meta) {
    if (!meta) return false;
    const mo = meta.market_open || {};
    const keys = ['a', 'hk', 'us'];
    const enabled = keys.filter(function (m) {
      return String(mo[m] || '').toUpperCase() !== 'DISABLED';
    });
    if (!enabled.length) return false;
    return enabled.every(function (m) {
      const st = String(mo[m] || '').toUpperCase();
      return st === 'CLOSED' || st === 'WEEKEND';
    });
  }

  /* ============================================================
   * 收市态面板：中长线持仓信号（按持仓周期维度分组）
   * 数据来自 overview.holding_signals（全量活跃信号，含 horizon 维度）。
   * 按 horizon.order 升序分桶：短线(几天) → 中线(几周) → 长线(几个月~几年)，
   * 每个桶头明确标注「持仓周期」跨度，便于用户了解各类中长线持仓的时间跨度。
   * ============================================================ */
  function renderHoldingSignals(signals) {
    const list = Array.isArray(signals) ? signals : [];
    if (!list.length) return '<div class="card-empty">当前无活跃的中长线持仓信号</div>';
    // 按 horizon 分桶（key 兜底 MEDIUM）
    const buckets = {};
    list.forEach(function (s) {
      const h = s.horizon || { key: 'MEDIUM', label: '中线', span: '几周', order: 2 };
      const k = h.key || 'MEDIUM';
      if (!buckets[k]) {
        buckets[k] = { label: h.label || '中线', span: h.span || '几周', order: h.order || 2, items: [] };
      }
      buckets[k].items.push(s);
    });
    const keys = Object.keys(buckets).sort(function (a, b) {
      return buckets[a].order - buckets[b].order;
    });
    return keys.map(function (k) {
      const b = buckets[k];
      const cards = b.items.map(function (s) {
        const sym = esc(s.symbol);
        const name = esc(s.name || s.symbol);
        const opp = (s.scores && s.scores.opportunity != null) ? F.num(s.scores.opportunity) : '—';
        const reason = esc(F.def(s.reason, ''));
        // 收市态强调「持仓周期」跨度标签
        const periodTag = '<span class="hl-period" style="background:var(--accent)">' + esc(b.span) + '</span>';
        return '<div class="hl-card" data-symbol="' + sym + '" ' +
          (s.signal_id ? 'data-signal="' + esc(s.signal_id) + '"' : '') + '>' +
          '<div class="hl-head"><div class="hl-name">' + name +
          ' <span class="hl-code">' + sym + '</span></div>' +
          (s.state ? stateBadge(s.state) : '') + '</div>' +
          '<div class="hl-meta">' + periodTag +
          '<span class="hl-opp">机会 ' + opp + '</span>' +
          (s.strategy_id ? '<span class="hl-strat">策略 ' + esc(s.strategy_id) + '</span>' : '') +
          '</div>' +
          (reason ? '<div class="hl-reason">' + reason + '</div>' : '') +
          (s.crowding ? renderCrowdingGauge(s.crowding) : '') +
          nextTriggerBox(s.next_trigger) +
          renderWhatChanged(s.what_changed) +
          '</div>';
      }).join('');
      return '<div class="hl-group">' +
        '<div class="group-title">' + esc(b.label) +
        ' <span class="hl-span">持仓周期 · ' + esc(b.span) + '</span>' +
        '<span class="group-count">' + b.items.length + '</span></div>' +
        cards +
        '</div>';
    }).join('');
  }

  /* ============================================================
   * 指数卡片（来自 /api/markets 或 /api/overview 的 markets[market].index）
   * 后端 markets 为 dict（{a:{...},hk:{...},us:{...},observed_age_ms}），
   * 每个市场含单一代表性指数 index:{symbol,last,change,change_pct,data_status}。
   * ============================================================ */
  function renderIndexGrid(markets, market) {
    if (!markets || typeof markets !== 'object') {
      return '<div class="card-empty">暂无指数数据</div>';
    }
    // dict 键为 lowercase（a/hk/us），tab market 为 uppercase（A/HK/US）—— 大小写不敏感匹配。
    // 兼容 Array（/api/overview 返回 list，按 .market 字段匹配）。
    let m = null;
    if (Array.isArray(markets)) {
      m = markets.find(function (x) { return x && String(x.market || '').toUpperCase() === String(market).toUpperCase(); }) || null;
    } else {
      m = markets[market] || markets[String(market).toLowerCase()] || markets.a || markets.hk || markets.us;
    }
    if (!m) return '<div class="card-empty">该市场暂无指数</div>';
    const idx = m.index;
    if (!idx || !idx.symbol) return '<div class="card-empty">该市场暂无指数</div>';
    return '<div class="index-grid">' + renderIndexCard(idx) + '</div>';
  }

  /** 单张指数卡（指数 index 为单一对象，不再是需要遍历的数组） */
  function renderIndexCard(i) {
    const chg = F.quoteChangePct(i);
    const sym = esc(i.symbol);
    return '<div class="index-card" data-symbol="' + sym + '">' +
      '<div class="index-name">' + esc(i.name || i.symbol) + '</div>' +
      '<div class="index-value live-price" data-symbol="' + sym + '">' + F.quotePrice(i) + '</div>' +
      '<div class="index-chg live-chg ' + F.chgClass(chg) + '" data-symbol="' + sym + '">' + F.fmtPct(chg) + '</div>' +
      '<div class="index-status live-status" data-symbol="' + sym + '">' + F.statusBadge(i.data_status, i.observed_age_ms) + '</div>' +
      '</div>';
  }

  /* ============================================================
   * 总览卡片：Regime / 强势板块 / 宽度 / 风险事件 / 数据源
   * ============================================================ */
  function renderRegimeCard(regime) {
    if (!regime) return loadingBox('市场状态计算中…');
    const r = regime;
    const regimeName = REGIME_LABELS[r.regime] || r.regime || '—';
    const score = F.num(r.market_score);
    const sf = r.sub_factors || {};
    const keys = ['breadth', 'trend', 'vol', 'momentum', 'risk'];
    const chips = keys.filter(function (k) { return sf[k] !== undefined && sf[k] !== null; })
      .map(function (k) {
        return '<span class="factor-chip">' + esc(k) + ' <b>' + F.num(sf[k]).toFixed(0) + '</b></span>';
      }).join('');
    return card('市场状态 · Regime',
      '<div class="regime-card">' +
      '<div class="regime-head"><span class="regime-name">' + esc(regimeName) + '</span>' +
      '<span class="regime-score">' + score + '<small>/100</small></span></div>' +
      (chips ? '<div class="regime-factors">' + chips + '</div>' : '') +
      '</div>');
  }

  function renderSectorCard(sectors) {
    const list = Array.isArray(sectors) ? sectors : [];
    if (!list.length) return loadingBox('板块数据计算中…');
    // 取评分最高的若干板块作为"强势板块"
    const top = list.slice().sort(function (a, b) { return F.num(b.score) - F.num(a.score); }).slice(0, 6);
    const rows = top.map(function (s) {
      const stage = s.stage || '—';
      const stageLabel = SECTOR_STAGE_LABELS[stage] || stage;
      const stageColor = SECTOR_STAGE_COLORS[stage] || '#8e8e93';
      return '<div class="sector-row">' +
        '<span class="sector-name">' + esc(s.sector || s.name || '—') + '</span>' +
        '<span class="sector-meta">' +
        (s.crowding !== undefined && s.crowding !== null ? '<span class="sector-crowd">拥挤 ' + F.num(s.crowding).toFixed(0) + '</span>' : '') +
        '<span class="sector-stage" style="background:' + stageColor + '">' + esc(stageLabel) + '</span>' +
        '<span class="sector-score">' + F.num(s.score).toFixed(0) + '</span>' +
        '</span></div>';
    }).join('');
    return card('强势板块（生命周期）', '<div class="sector-list">' + rows + '</div>');
  }

  function renderBreadthCard(breadth) {
    const b = breadth || {};
    const up = F.num(b.up), down = F.num(b.down), flat = F.num(b.flat);
    const total = up + down + flat || 1;
    return card('涨跌宽度',
      '<div class="breadth-bar">' +
      '<div class="seg-up" style="width:' + (up / total * 100) + '%">' + (up || '') + '</div>' +
      '<div class="seg-flat" style="width:' + (flat / total * 100) + '%">' + (flat || '') + '</div>' +
      '<div class="seg-down" style="width:' + (down / total * 100) + '%">' + (down || '') + '</div>' +
      '</div>' +
      '<div class="breadth-legend"><span class="up">涨 ' + up + '</span>' +
      '<span>平 ' + flat + '</span><span class="down">跌 ' + down + '</span></div>');
  }

  /**
   * 风险事件卡（§9 契约：后端 _active_risk_events 返回结构化 dict 列表）。
   * 按字段结构化渲染：标的 / 市场 / 风险等级 chip / 风险分 / 状态徽章 / reason。
   * 严禁 JSON.stringify 兜底——后端已是结构化数据，回退 JSON.stringify 会把整条
   * 对象渲染成原始字符串（本 bug 的根因）。所有动态文本经 esc() 防 XSS。
   */
  function renderRiskCard(events) {
    const list = Array.isArray(events) ? events : [];
    if (!list.length) return card('风险事件', '<div class="risk-list"><div class="risk-item">暂无显著风险事件</div></div>');
    const items = list.map(function (e) {
      const sym = esc(F.def(e.symbol, ''));
      const market = esc(F.def(e.market, ''));
      const level = F.def(e.level, 'LOW');
      const levelColor = RISK_LEVEL_COLORS[level] || '#8e8e93';
      const levelLabel = esc(RISK_LEVEL_LABELS[level] || level);
      const score = F.num(e.risk_score);
      const state = F.def(e.state, '');
      const reason = esc(F.def(e.reason, ''));
      return '<div class="risk-item" style="border-left-color:' + levelColor + '">' +
        '<div class="risk-head">' +
          '<span class="risk-sym">' + sym + '</span>' +
          (market ? '<span class="risk-market">' + market + '</span>' : '') +
          '<span class="risk-level-chip" style="background:' + levelColor + '">' + levelLabel + '</span>' +
          '<span class="risk-score">风险 ' + score + '</span>' +
          (state ? stateBadge(state) : '') +
        '</div>' +
        (reason ? '<div class="risk-reason">' + reason + '</div>' : '') +
      '</div>';
    }).join('');
    return card('风险事件', '<div class="risk-list">' + items + '</div>');
  }

  function renderProviderHealthCard(providers) {
    const list = Array.isArray(providers) ? providers : [];
    if (!list.length) return '';
    const rows = list.map(function (p) {
      const cs = p.circuit_state || '—';
      const lat = (p.latency_p50 !== undefined && p.latency_p50 !== null) ? ('' + F.num(p.latency_p50).toFixed(0) + 'ms') : '—';
      const err = (p.error_rate !== undefined && p.error_rate !== null) ? (F.num(p.error_rate) * 100).toFixed(0) + '%' : '—';
      return '<div class="health-row">' +
        '<span class="health-name">' + esc(p.provider || p.name || '?') + '</span>' +
        '<span class="health-meta">' +
        '<span>延迟 ' + lat + '</span><span>错误 ' + err + '</span>' +
        '<span class="circuit ' + esc(cs) + '">' + esc(cs) + '</span>' +
        '</span></div>';
    }).join('');
    return card('数据源健康', '<div class="health-list">' + rows + '</div>');
  }

  /* ============================================================
   * Top 机会列表（来自 overview.top_opportunities）
   * ============================================================ */
  function renderTopList(signals, market) {
    let list = Array.isArray(signals) ? signals : [];
    // 按当前选中市场过滤；不传 market 则全量（兼容旧调用 / 初始全量加载）
    if (market) {
      list = list.filter(function (s) { return s.market === market; });
    }
    if (!list.length) return '<div class="card-empty">该市场暂无重点机会</div>';
    return '<div class="top-list">' + list.map(function (sig) {
      const q = sig.quote || {};
      const price = F.quotePrice(q);
      const chg = F.quoteChangePct(q);
      const sym = esc(sig.symbol);
      const sid = esc(sig.signal_id || '');
      const plan = '入场 ' + F.fmtRange(sig.entry_low, sig.entry_high) +
        (sig.reward_risk != null ? ' · ' + F.num(sig.reward_risk).toFixed(2) + 'R' : '');
      // data-signal 始终存在：优先 signal_id；但 _top_opportunities 每项必有 symbol，
      // 故退而用 symbol 作可靠键。openSignal 兼容 symbol / signal_id。
      const sigKey = sid || sym;
      return '<div class="opp-card" data-symbol="' + sym + '" data-signal="' + sigKey + '">' +
        '<div class="opp-main">' +
        '<div class="opp-name">' + esc(sig.name || sig.symbol) + '<span class="opp-code">' + sym + '</span></div>' +
        '<div class="opp-plan">' + esc(plan) + '</div>' +
        '</div>' +
        '<div class="opp-right">' +
        '<div class="opp-price live-price" data-symbol="' + sym + '">' + price + '</div>' +
        '<div class="live-chg ' + F.chgClass(chg) + '" data-symbol="' + sym + '" style="font-size:12px;font-weight:700;font-family:var(--font-mono)">' + F.fmtPct(chg) + '</div>' +
        '</div>' +
        stateBadge(sig.state) +
        (sig.indicators ? '<div class="opp-ind">' + renderIndicators(sig.indicators) + '</div>' : '') +
        (sig.crowding ? renderCrowdingGauge(sig.crowding) : '') +
        nextTriggerBox(sig.next_trigger) +
        renderWhatChanged(sig.what_changed) +
        '</div>';
    }).join('') + '</div>';
  }

  /* ============================================================
   * 自选卡（来自 /api/watchlist）
   * ============================================================ */
  function renderWatchGroups(items, market) {
    const list = (Array.isArray(items) ? items : []).filter(function (it) {
      return !market || it.market === market || (it.quote && it.quote.market === market);
    });
    if (!list.length) return '<div class="card-empty">该市场暂无自选股，去机会雷达里挑选吧</div>';
    // 按机会分降序
    list.sort(function (a, b) {
      const sa = F.num((a.score && a.score.opportunity));
      const sb = F.num((b.score && b.score.opportunity));
      return sb - sa;
    });
    return list.map(function (it) {
      const q = it.quote || {};
      const sc = it.score || {};
      const sig = it.signal || {};
      const sym = esc(it.symbol || q.symbol || '');
      const name = esc(it.name || q.name || it.symbol || '—');
      const sid = esc(sig.signal_id || '');
      const price = F.quotePrice(q);
      const chg = F.quoteChangePct(q);
      const plan =
        planRow('入场', F.fmtRange(sig.entry_low, sig.entry_high)) +
        planRow('止损', F.fmtPrice(sig.invalidation_price)) +
        planRow('触发价', F.fmtPrice(sig.trigger_price)) +
        planRow('R倍数', sig.reward_risk != null ? (F.num(sig.reward_risk).toFixed(2) + 'R') : '—');
      return '<div class="wl-card" data-symbol="' + sym + '" ' + (sid ? 'data-signal="' + sid + '"' : '') + '>' +
        '<div class="wl-head"><div class="wl-name">' + name + ' <span class="wl-code">' + sym + '</span></div>' +
        (sig.state ? stateBadge(sig.state) : '') + '</div>' +
        '<div class="wl-quote">' +
        '<span class="live-price" data-symbol="' + sym + '">' + price + '</span>' +
        '<span class="live-chg ' + F.chgClass(chg) + '" data-symbol="' + sym + '">' + F.fmtPct(chg) + '</span>' +
        '<span class="live-status" data-symbol="' + sym + '">' + F.statusBadge(q.data_status, q.observed_age_ms) + '</span>' +
        '</div>' +
        renderScores(sc) +
        '<div class="wl-plan">' + plan + '</div>' +
        nextTriggerBox(sig.next_trigger) +
        renderWhatChanged(sig.what_changed) +
        '</div>';
    }).join('');
  }

  function renderWatchSummary(watchlist, positions) {
    const w = Array.isArray(watchlist) ? watchlist : [];
    const p = Array.isArray(positions) ? positions : [];
    const triggered = w.filter(function (it) {
      const st = (it.signal && it.signal.state);
      return st === 'TRIGGERED' || st === 'ACTIVE';
    }).length;
    return '今日有 <b>' + triggered + '</b> 只自选处于可执行/持有状态 · 持仓 ' + p.length + ' 笔';
  }

  /* ============================================================
   * 持仓卡（来自 /api/positions）
   * ============================================================ */
  function renderPositionList(positions, market) {
    const list = (Array.isArray(positions) ? positions : []).filter(function (p) {
      return !p.closed_at && (!market || p.market === market);
    });
    if (!list.length) return '<div class="card-empty">暂无持仓</div>';
    return list.map(function (p) {
      const q = p.quote || {};
      const sig = p.signal || {};
      const sym = esc(p.symbol || '');
      const last = F.num(F.def(q.last, q.close));
      const cost = F.num(p.cost);
      const shares = F.num(p.shares);
      const pnl = (last - cost) * shares;
      const pnlPct = cost ? ((last - cost) / cost * 100) : 0;
      const pnlCls = pnl >= 0 ? 'up' : 'down'; // A股：盈利红、亏损绿
      return '<div class="pos-card" data-symbol="' + sym + '" ' +
        (sig.signal_id ? 'data-signal="' + esc(sig.signal_id) + '"' : '') + '>' +
        '<div class="pos-head"><div class="pos-name">' + esc(p.name || p.symbol) + ' <span class="wl-code">' + sym + '</span></div>' +
        (sig.state ? stateBadge(sig.state) : '') + '</div>' +
        '<div class="pos-grid">' +
        '<div class="pos-cell"><span class="pos-k">现价</span><span class="pos-v live-price" data-symbol="' + sym + '">' + F.fmtPrice(last) + '</span></div>' +
        '<div class="pos-cell"><span class="pos-k">成本</span><span class="pos-v">' + F.fmtPrice(cost) + '</span></div>' +
        '<div class="pos-cell"><span class="pos-k">持仓</span><span class="pos-v">' + F.fmtInt(shares) + ' 股</span></div>' +
        '<div class="pos-cell"><span class="pos-k">盈亏</span><span class="pos-v pos-pnl ' + pnlCls + '">' +
        (pnl >= 0 ? '+' : '') + F.fmtInt(pnl) + ' (' + F.fmtPct(pnlPct) + ')</span></div>' +
        '</div>' +
        '<div class="wl-plan" style="margin-top:10px">' +
        planRow('止损', F.fmtPrice(sig.invalidation_price)) +
        planRow('R倍数', sig.reward_risk != null ? (F.num(sig.reward_risk).toFixed(2) + 'R') : '—') +
        '</div>' +
        nextTriggerBox(sig.next_trigger) +
        '</div>';
    }).join('');
  }

  /* ============================================================
   * 机会雷达（来自 /api/radar，按状态分组）
   * ============================================================ */
  function renderRadarCard(sig) {
    const sc = pickScore(sig) || {};
    const name = sig.name || sig.symbol || '—';
    const sym = esc(sig.symbol || '');
    const sid = esc(sig.signal_id || '');
    const grp = radarGroupOf(sig.state);
    const grpCls = groupClassOf(grp);
    const scoreMini = function (label, v, invert) {
      return '<div class="radar-score"><span class="rs-num" style="color:' + F.scoreColor(F.num(v), invert) + '">' + F.num(v) + '</span>' +
        '<span class="rs-label">' + esc(label) + '</span></div>';
    };
    const sub = '入场 ' + F.fmtRange(sig.entry_low, sig.entry_high) +
      (sig.reward_risk != null ? ' · ' + F.num(sig.reward_risk).toFixed(2) + 'R' : '') +
      (sig.regime ? ' · ' + esc(sig.regime) : '');
    return '<div class="radar-card ' + grpCls + '" data-symbol="' + sym + '" ' + (sid ? 'data-signal="' + sid + '"' : '') + '>' +
      '<div class="radar-head"><div class="radar-name">' + esc(name) + ' <span class="wl-code">' + sym + '</span></div>' +
      stateBadge(sig.state) + '</div>' +
      '<div class="radar-sub">' + esc(sub) + '</div>' +
      '<div class="radar-scores">' +
      scoreMini('机会', sc.opportunity, false) +
      scoreMini('时机', sc.timing, false) +
      scoreMini('风险', sc.risk, true) +
      scoreMini('置信', sc.confidence, false) +
      '</div>' +
      nextTriggerBox(sig.next_trigger) +
      '</div>';
  }

  function renderRadar(signals) {
    const list = Array.isArray(signals) ? signals : [];
    if (!list.length) return '<div class="card-empty">暂无机会信号，等待扫描产出</div>';
    const groups = {};
    list.forEach(function (s) {
      const g = radarGroupOf(s.state) || '其他';
      (groups[g] = groups[g] || []).push(s);
    });
    const keys = Object.keys(groups).sort(function (a, b) {
      const ia = RADAR_GROUP_ORDER.indexOf(a), ib = RADAR_GROUP_ORDER.indexOf(b);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
    return keys.map(function (g) {
      return '<div class="radar-group">' +
        '<div class="group-title">' + esc(g) + ' <span class="group-count">' + groups[g].length + '</span></div>' +
        groups[g].map(renderRadarCard).join('') +
        '</div>';
    }).join('');
  }

  /* ============================================================
   * 信号详情（sheet 弹层）—— 完整交易计划 + Why-Not-Buy + Next Trigger + What Changed
   * ============================================================ */
  function renderSignalDetail(sig) {
    if (!sig) return '<div class="card-empty">无信号数据</div>';
    const sc = pickScore(sig) || {};
    const sym = esc(sig.symbol || '');
    const name = esc(sig.name || sig.symbol || '—');

    // 交易计划网格
    const planCells = [
      ['入场区间', F.fmtRange(sig.entry_low, sig.entry_high)],
      ['触发价', F.fmtPrice(sig.trigger_price)],
      ['止损价', F.fmtPrice(sig.invalidation_price)],
      ['目标①', F.fmtPrice(sig.target_1)],
      ['目标②', F.fmtPrice(sig.target_2)],
      ['风险收益', sig.reward_risk != null ? (F.num(sig.reward_risk).toFixed(2) + 'R') : '—'],
      ['市场态', REGIME_LABELS[sig.market_regime] || sig.market_regime || '—'],
      ['板块阶段', SECTOR_STAGE_LABELS[sig.sector_stage] || sig.sector_stage || '—']
    ].map(function (c) {
      return '<div class="detail-cell"><span class="detail-k">' + esc(c[0]) + '</span><span class="detail-v">' + esc(c[1]) + '</span></div>';
    }).join('');

    // 负面理由（Why-Not-Buy）：negative_reasons + quality.reasons
    const negList = (Array.isArray(sc.negative_reasons) ? sc.negative_reasons : [])
      .concat((sig.quality && Array.isArray(sig.quality.reasons)) ? sig.quality.reasons : []);
    const negItems = negList.length
      ? negList.map(function (t) {
        return '<div class="reason-item reason-neg"><span class="reason-ico">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
          '</span><div><div class="reason-name">' + esc(t) + '</div></div></div>';
      }).join('')
      : '<div class="reason-item reason-neg"><span class="reason-ico"></span><div><div class="reason-note">暂无明确负面因素</div></div></div>';

    // 正面理由
    const posList = Array.isArray(sc.positive_reasons) ? sc.positive_reasons : [];
    const posItems = posList.length
      ? posList.map(function (t) {
        return '<div class="reason-item reason-pos"><span class="reason-ico">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
          '</span><div><div class="reason-name">' + esc(t) + '</div></div></div>';
      }).join('')
      : '';

    const reasonBlock = sig.reason
      ? '<div class="detail-section"><div class="detail-section-title">当前状态说明</div>' +
        '<div class="reason-note" style="font-size:13.5px;color:var(--text-2)">' + esc(sig.reason) + '</div></div>'
      : '';

    return '<div class="sheet-header">' +
      '<div class="sheet-title">' + name + ' <span class="row-code">' + sym + '</span> ' + stateBadge(sig.state) + '</div>' +
      '<div class="sheet-sub">策略 ' + esc(sig.strategy_id || '—') + ' · 状态变更 ' + F.fmtClock(sig.state_changed_at) +
      ' · ' + F.statusBadge(sig.data_status, sig.observed_age_ms) + '</div>' +
      '</div>' +
      '<div class="sheet-body">' +
      reasonBlock +
      '<div class="detail-section"><div class="detail-section-title">四分数</div>' + renderScores(sc) + '</div>' +
      '<div class="detail-section"><div class="detail-section-title">交易计划</div>' +
      '<div class="detail-grid">' + planCells + '</div></div>' +
      (sig.crowding ? '<div class="detail-section"><div class="detail-section-title">拥挤度 / 追高风险（§24.6）</div>' + renderCrowdingGauge(sig.crowding) + '</div>' : '') +
      (sig.next_trigger ? '<div class="detail-section">' + nextTriggerBox(sig.next_trigger) + '</div>' : '') +
      renderWhatChanged(sig.what_changed) +
      '<div class="detail-section"><div class="detail-section-title">Why-Not-Buy · 为什么还不能直接买 / 风险</div>' +
      '<div class="whynot-card">' + negItems + '</div></div>' +
      (posItems ? '<div class="detail-section"><div class="detail-section-title">支持信号的逻辑</div>' + posItems + '</div>' : '') +
      '</div>' +
      '<div class="sheet-footer"><button class="sheet-close" id="sheetClose">知道啦</button></div>';
  }

  /* ---------------- 展示用技术指标（纯数值，对齐后端 build_indicators） ----------------
   * 仅展示 MA/MACD/RSI/量比/52周位置/振幅，不做任何评分/加权（与 scoring 解耦）。
   * 所有动态文本经 esc() 防 XSS；数值经 F.num 防御空值。
   */
  function renderIndicators(ind) {
    if (!ind || typeof ind !== 'object') {
      return '<div class="ind-empty">暂无 K 线指标（历史数据收集中…）</div>';
    }
    function cell(label, val, opts) {
      opts = opts || {};
      const v = (val == null || (typeof val === 'number' && isNaN(val)))
        ? '—' : (opts.pct ? F.fmtPct(val) : (opts.percent ? F.num(val).toFixed(1) + '%' : F.num(val)));
      const cls = opts.cls ? (' ' + opts.cls) : '';
      return '<div class="ind-cell' + cls + '">' +
        '<span class="ind-k">' + esc(label) + '</span>' +
        '<span class="ind-v">' + esc(String(v)) + '</span></div>';
    }
    // 52周位置：用进度条直观展示（0=最低，1=最高）
    const pos = ind.pos52w;
    const posPct = (typeof pos === 'number' && !isNaN(pos)) ? (pos * 100).toFixed(0) : null;
    const posBar = posPct == null
      ? ''
      : '<div class="ind-posbar"><div class="ind-posfill" style="width:' + esc(posPct) + '%"></div>' +
        '<span class="ind-poslabel">' + esc(posPct) + '% · 52周位置</span></div>';

    return '<div class="ind-grid">' +
      cell('MA5', ind.ma5) + cell('MA10', ind.ma10) + cell('MA20', ind.ma20) + cell('MA60', ind.ma60) +
      cell('EMA12', ind.ema12) + cell('EMA26', ind.ema26) +
      cell('MACD', ind.macd_dif) + cell('DEA', ind.macd_dea) + cell('MACD柱', ind.macd_hist) +
      cell('RSI14', ind.rsi14) + cell('ATR14', ind.atr14) +
      cell('ROC20', ind.roc20, { pct: true }) + cell('ROC60', ind.roc60, { pct: true }) +
      cell('量比', ind.vol_ratio) + cell('振幅', ind.amplitude, { pct: true }) +
      cell('年化波动', ind.ann_vol, { percent: true }) +
      '</div>' + posBar;
  }

  global.UI = {
    STATE_LABELS: STATE_LABELS, STATE_COLORS: STATE_COLORS,
    REGIME_LABELS: REGIME_LABELS, SECTOR_STAGE_LABELS: SECTOR_STAGE_LABELS,
    RISK_LEVEL_COLORS: RISK_LEVEL_COLORS, RISK_LEVEL_LABELS: RISK_LEVEL_LABELS,
    radarGroupOf: radarGroupOf, RADAR_GROUP_ORDER: RADAR_GROUP_ORDER,
    stateBadge: stateBadge, renderScores: renderScores, nextTriggerBox: nextTriggerBox,
    renderBanner: renderBanner, bannerModeClass: bannerModeClass,
    isAllMarketsClosed: isAllMarketsClosed, renderHoldingSignals: renderHoldingSignals,
    renderCrowdingGauge: renderCrowdingGauge,
    renderIndexGrid: renderIndexGrid,
    renderRegimeCard: renderRegimeCard, renderSectorCard: renderSectorCard,
    renderBreadthCard: renderBreadthCard, renderRiskCard: renderRiskCard,
    renderProviderHealthCard: renderProviderHealthCard,
    renderTopList: renderTopList,
    renderWatchGroups: renderWatchGroups, renderWatchSummary: renderWatchSummary,
    renderPositionList: renderPositionList,
    renderRadar: renderRadar, renderRadarCard: renderRadarCard,
    renderSignalDetail: renderSignalDetail,
    renderIndicators: renderIndicators,
    loadingBox: loadingBox, card: card, esc: esc
  };
})(window);
