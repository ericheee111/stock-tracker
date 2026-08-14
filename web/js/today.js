/* =========================================================================
 * today.js —— 今日作战简报（Stage 1 Lane D / WorkBuddy 所有权）
 * 全局对象：Today
 * 数据来自 /api/brief/today（STAGE1-API-CONTRACT-v1.md）。
 * 核心铁律（AGENTS.md §7 / §14 / PRD §5 / PRODUCT-GAP-MATRIX）：
 *   - 动作/状态为主，数字为辅；
 *   - 概率未校准（calibrated_probability == null）时，绝不准显示百分比、
 *     0%、「—」、Opportunity/100，也绝不准把 Confidence/Model Score 当胜率；
 *   - Big Trend 状态非激活时，明确「正式算法尚未启用」，禁止渲染候选、
 *     禁止用 SectorScore 冒充、禁止制造 EMERGING/CONFIRMING；
 *   - Strategy Evidence 为 INSUFFICIENT_REAL_EVIDENCE 时，明确「暂不展示真实
 *     策略战绩」，禁止把 synthetic benchmark 当真实战绩；
 *   - 所有动态文本经 F.esc() 转义，绝不渲染原始对象或 JSON。
 * 所有读取均做空值/缺字段防御：某字段缺失只省略该片段，不整页崩溃、不白屏。
 * ========================================================================= */
(function (global) {
  'use strict';

  const F = global.Fmt;
  const esc = F.esc;
  const UI = global.UI;

  /* ---------------- 枚举：产品层 ActionState（PRD §5.1 + 矩阵 §7.1） ---------------- */
  const ACTION_META = {
    EXECUTABLE:          { label: '当前可执行', cls: 'a-exec' },
    WAIT_PULLBACK:       { label: '等回踩',     cls: 'a-wait-pb' },
    WAIT_BREAKOUT:       { label: '等突破',     cls: 'a-wait-bo' },
    HOLD:                { label: '继续持有',   cls: 'a-hold' },
    WARNING:             { label: '风险预警',   cls: 'a-warn' },
    TRIM:                { label: '建议减仓',   cls: 'a-trim' },
    PARTIAL_TAKE_PROFIT: { label: '部分止盈',   cls: 'a-ptp' },
    TREND_RUNNER:        { label: '保留趋势仓', cls: 'a-trend' },
    EXIT:                { label: '退出',       cls: 'a-exit' },
    AVOID:               { label: '当前回避',   cls: 'a-avoid' },
    WATCH:               { label: '值得观察',   cls: 'a-watch' },
    DATA_BLOCKED:        { label: '数据阻断·禁止决策', cls: 'a-block' }
  };
  function actionBadge(state) {
    const m = ACTION_META[state] || { label: state || '—', cls: 'a-watch' };
    return '<span class="act-badge ' + m.cls + '">' + esc(m.label) + '</span>';
  }

  /* ---------------- 枚举：模型倾向 / 证据等级 / 进攻度 / 主升浪 ---------------- */
  const TENDENCY_LABELS = { STRONG: '偏强', NEUTRAL: '中性', WEAK: '偏弱' };
  const EVIDENCE_LABELS = { INSUFFICIENT: '不足', LOW: '低', MEDIUM: '中', HIGH: '高' };
  const AGGRESSION_LABELS = {
    AGGRESSIVE: '建议进攻', BALANCED: '中性偏防御', DEFENSIVE: '建议防守', MODERATE: '中性'
  };
  const BIG_TREND_LABELS = {
    NONE: '无明显大趋势', EMERGING: '早期观察', CONFIRMING: '确认中', TRENDING: '趋势运行中',
    MATURE: '后期（不追高）', DISTRIBUTING: '派发/退潮', BROKEN: '结构破坏'
  };
  const ACTIVE_BIG_TREND = ['EMERGING', 'CONFIRMING', 'TRENDING', 'MATURE', 'DISTRIBUTING', 'BROKEN'];

  function gradeBadge(g) {
    if (!g) return '';
    const cls = g === 'A' ? 'g-a' : (g === 'B' ? 'g-b' : (g === 'C' ? 'g-c' : 'g-x'));
    return '<span class="grade-badge ' + cls + '">' + esc(g) + '</span>';
  }

  /* ---------------- 概率块（诚实双层：倾向 + 校准概率） ---------------- */
  function finiteUnitInterval(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return null;
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 && number <= 1 ? number : null;
  }

  function probabilityBlock(model) {
    if (!model || typeof model !== 'object') return '';
    const tendency = TENDENCY_LABELS[model.tendency] || '中性';
    const score = finiteUnitInterval(model.score);
    let html = '';
    html += '<div class="tb-prob-row"><span class="tb-k">模型倾向</span><span class="tb-v">' + esc(tendency) + '</span></div>';
    if (score !== null) {
      // 明确标注「模型分(0–1)」，与胜率/成功率严格区分。
      html += '<div class="tb-prob-row"><span class="tb-k">模型分(0–1)</span><span class="tb-v">' + esc(score.toFixed(3)) + '</span></div>';
    }
    const evidence = model.probability_evidence_level || 'INSUFFICIENT';
    const evidenceLabel = EVIDENCE_LABELS[evidence] || evidence;
    if (model.calibrated_probability == null) {
      html += '<div class="tb-prob-row tb-prob-null">' +
        '<span class="tb-k">校准成功概率</span>' +
        '<span class="tb-v tb-warn">真实样本或校准证据不足，暂不展示</span>' +
        esc('（证据等级：' + evidenceLabel + '）') + '</div>';
      if (model.message) html += '<div class="tb-prob-note">' + esc(model.message) + '</div>';
    } else {
      const probability = finiteUnitInterval(model.calibrated_probability);
      if (probability === null || evidence === 'INSUFFICIENT') {
        html += '<div class="tb-prob-row tb-prob-null">' +
          '<span class="tb-k">校准成功概率</span>' +
          '<span class="tb-v tb-warn">概率字段无效或证据不足，已隐藏</span></div>';
      } else {
        const pct = Math.round(probability * 100);
        html += '<div class="tb-prob-row"><span class="tb-k">校准成功概率</span>' +
          '<span class="tb-v">' + esc(pct + '% · 证据等级：' + evidenceLabel) + '</span></div>';
      }
    }
    return '<div class="tb-prob">' + html + '</div>';
  }

  /* ---------------- 交易计划片段（inline，不弹层） ---------------- */
  function tradePlanBlock(plan) {
    if (!plan || typeof plan !== 'object') return '';
    const rows = [];
    if (plan.entry_low != null || plan.entry_high != null)
      rows.push(['入场区间', F.fmtRange(plan.entry_low, plan.entry_high)]);
    if (plan.trigger_price != null)
      rows.push(['触发价', F.fmtPrice(plan.trigger_price)]);
    if (plan.no_chase_above != null)
      rows.push(['不追价(>)', F.fmtPrice(plan.no_chase_above)]);
    if (plan.invalidation_price != null)
      rows.push(['失效位', F.fmtPrice(plan.invalidation_price)]);
    if (plan.target_1 != null)
      rows.push(['目标①', F.fmtPrice(plan.target_1)]);
    if (plan.target_2 != null)
      rows.push(['目标②', F.fmtPrice(plan.target_2)]);
    if (plan.reward_risk != null)
      rows.push(['风险收益', F.num(plan.reward_risk).toFixed(2) + 'R']);
    // 仓位/股数：未配置账户时为 null → 明确标 DEMO_CONTRACT，绝不伪造建议股数
    if (plan.suggested_position_pct != null &&
        Number.isFinite(Number(plan.suggested_position_pct)) &&
        Number(plan.suggested_position_pct) >= 0 && Number(plan.suggested_position_pct) <= 1)
      rows.push(['建议仓位', (Number(plan.suggested_position_pct) * 100).toFixed(1) + '%']);
    else
      rows.push(['建议仓位', '未配置账户·DEMO_CONTRACT']);
    if (plan.suggested_shares != null)
      rows.push(['建议股数', F.fmtInt(plan.suggested_shares) + ' 股']);
    else
      rows.push(['建议股数', '待账户配置']);
    const rowsHtml = rows.map(function (r) {
      return '<div class="tb-plan-row"><span class="tb-pk">' + esc(r[0]) + '</span>' +
        '<span class="tb-pv">' + esc(r[1]) + '</span></div>';
    }).join('');
    const msg = plan.position_message ? '<div class="tb-plan-note">' + esc(plan.position_message) + '</div>' : '';
    return '<div class="tb-plan"><div class="tb-sub-title">交易计划</div>' + rowsHtml + msg + '</div>';
  }

  function reasonsBlock(pos, neg) {
    const posList = Array.isArray(pos) ? pos : [];
    const negList = Array.isArray(neg) ? neg : [];
    let html = '';
    if (posList.length) {
      html += '<div class="tb-reasons tb-pos"><div class="tb-sub-title">支持逻辑</div>' +
        posList.map(function (t) { return '<div class="tb-reason">' + esc(t) + '</div>'; }).join('') + '</div>';
    }
    if (negList.length) {
      html += '<div class="tb-reasons tb-neg"><div class="tb-sub-title">为什么还不能直接买 / 风险</div>' +
        negList.map(function (t) { return '<div class="tb-reason">' + esc(t) + '</div>'; }).join('') + '</div>';
    }
    return html;
  }

  function blockerText(item) {
    if (typeof item === 'string') return item;
    if (!item || typeof item !== 'object') return '';
    const code = typeof item.code === 'string' && item.code ? '[' + item.code + '] ' : '';
    const message = typeof item.message === 'string' ? item.message : '';
    return code + message;
  }

  function blockerRows(items) {
    return items.map(blockerText).filter(Boolean).map(function (text) {
      return '<div class="tb-reason">' + esc(text) + '</div>';
    }).join('');
  }

  function blockersBlock(hard, soft) {
    const h = Array.isArray(hard) ? hard : [];
    const s = Array.isArray(soft) ? soft : [];
    if (!h.length && !s.length) return '';
    let html = '<div class="tb-blockers">';
    if (h.length) {
      html += '<div class="tb-blk tb-blk-hard"><div class="tb-sub-title">硬阻断（不可绕过）</div>' +
        blockerRows(h) + '</div>';
    }
    if (s.length) {
      html += '<div class="tb-blk tb-blk-soft"><div class="tb-sub-title">软阻断（可小仓激进方案）</div>' +
        blockerRows(s) + '</div>';
    }
    return html + '</div>';
  }

  /* ---------------- 单个 Core Opportunity 卡 ---------------- */
  function coreOpportunityCard(op) {
    if (!op || typeof op !== 'object') return '';
    const sym = esc(op.symbol || '');
    const name = esc(op.name || op.symbol || '—');
    const scores = op.scores || null;
    const model = op.model || null;
    const plan = op.trade_plan || null;

    let html = '<div class="tb-card tb-core" data-symbol="' + sym + '">';
    html += '<div class="tb-core-head">' +
      '<div class="tb-core-name">' + name + ' <span class="tb-code">' + sym + '</span></div>' +
      actionBadge(op.action_state) + gradeBadge(op.opportunity_grade) + '</div>';

    if (scores) html += UI.renderScores(scores);
    html += probabilityBlock(model);

    // 为什么值得看 / 为什么是现在
    if (Array.isArray(op.positive_reasons) && op.positive_reasons.length)
      html += reasonsBlock(op.positive_reasons, null);

    html += tradePlanBlock(plan);

    // 为什么还不能买（硬/软阻断 + 负面理由）
    const neg = Array.isArray(op.negative_reasons) ? op.negative_reasons : [];
    html += blockersBlock(op.hard_blockers, op.soft_blockers);
    if (neg.length) html += reasonsBlock(null, neg);

    const nextTrigger = op.next_trigger || (plan && plan.next_trigger);
    html += UI.nextTriggerBox(nextTrigger);

    // 元数据：策略版本 / 数据状态 / 证据
    const metaBits = [];
    if (op.strategy_id) metaBits.push('策略 ' + esc(op.strategy_id) + (op.strategy_version ? (' v' + esc(op.strategy_version)) : ''));
    if (op.data_status) metaBits.push('数据 ' + esc(op.data_status));
    if (op.freshness) metaBits.push('新鲜度 ' + esc(op.freshness));
    if (op.evidence_id) metaBits.push('证据 ' + esc(op.evidence_id));
    if (metaBits.length) html += '<div class="tb-meta">' + metaBits.map(function (b) { return '<span>' + b + '</span>'; }).join('') + '</div>';

    html += '</div>';
    return html;
  }

  /* ---------------- 单个持仓动作卡 ---------------- */
  function holdingCard(h) {
    if (!h || typeof h !== 'object') return '';
    const sym = esc(h.symbol || '');
    const name = esc(h.name || h.symbol || '—');
    const hasPnl = h.pnl != null && Number.isFinite(Number(h.pnl));
    const hasPnlPct = h.pnl_pct != null && Number.isFinite(Number(h.pnl_pct));
    const pnl = hasPnl ? Number(h.pnl) : null;
    const pnlPct = hasPnlPct ? Number(h.pnl_pct) : null;
    const pnlCls = hasPnlPct ? F.chgClass(pnlPct) : 'flat'; // A股：盈利红、亏损绿
    const pnlText = (hasPnl && hasPnlPct)
      ? ((pnl >= 0 ? '+' : '') + F.fmtInt(pnl) + ' (' + F.fmtPct(pnlPct) + ')')
      : '—';
    const dist = (h.distance_to_invalidation_pct != null && Number.isFinite(Number(h.distance_to_invalidation_pct)))
      ? (Number(h.distance_to_invalidation_pct).toFixed(1) + '%') : '—';
    const lastText = (h.last != null && Number.isFinite(Number(h.last)) && Number(h.last) > 0)
      ? F.fmtPrice(h.last) : '—';
    const costText = (h.average_cost != null && Number.isFinite(Number(h.average_cost)) && Number(h.average_cost) > 0)
      ? F.fmtPrice(h.average_cost) : '—';

    let html = '<div class="tb-card tb-holding" data-symbol="' + sym + '">';
    html += '<div class="tb-core-head">' +
      '<div class="tb-core-name">' + name + ' <span class="tb-code">' + sym + '</span>' +
      ' <span class="tb-hold-shares">' + F.fmtInt(h.shares) + ' 股</span></div>' +
      actionBadge(h.action_state) + '</div>';
    html += '<div class="tb-hold-grid">' +
      '<div class="tb-hold-cell"><span class="tb-hk">现价</span><span class="tb-hv">' + esc(lastText) + '</span></div>' +
      '<div class="tb-hold-cell"><span class="tb-hk">成本</span><span class="tb-hv">' + esc(costText) + '</span></div>' +
      '<div class="tb-hold-cell"><span class="tb-hk">盈亏</span><span class="tb-hv ' + pnlCls + '">' +
      esc(pnlText) + '</span></div>' +
      '<div class="tb-hold-cell"><span class="tb-hk">距失效位</span><span class="tb-hv">' + dist + '</span></div>' +
      '</div>';
    if (h.reason) html += '<div class="tb-hold-reason">' + esc(h.reason) + '</div>';
    if (h.invalidation_price != null)
      html += '<div class="tb-plan-note">失效位：' + F.fmtPrice(h.invalidation_price) + '</div>';
    if (h.data_status) html += '<div class="tb-meta"><span>数据 ' + esc(h.data_status) + '</span></div>';
    html += '</div>';
    return html;
  }

  /* ---------------- Big Trend（未启用时绝不渲染候选） ---------------- */
  function bigTrendBlock(bt) {
    if (!bt || typeof bt !== 'object') return '';
    const status = bt.status || 'NOT_AVAILABLE';
    if (ACTIVE_BIG_TREND.indexOf(status) === -1) {
      const msg = bt.message || '主升浪识别（板块 / 个股 / 龙头 / 二次启动）将在 Stage 3 实现；当前不展示候选，避免用板块分数冒充主升浪状态。';
      return '<div class="tb-card tb-bigtrend">' +
        '<div class="tb-card-title">主升浪雷达</div>' +
        '<div class="tb-notavail"><span class="tb-na-ico">⚠</span>' +
        '<div><div class="tb-na-title">正式算法尚未启用</div>' +
        '<div class="tb-na-desc">' + esc(msg) + '</div></div></div></div>';
    }
    const label = BIG_TREND_LABELS[status] || status;
    const items = Array.isArray(bt.items) ? bt.items : [];
    const itemHtml = items.length
      ? '<div class="tb-bt-items">' + items.map(function (it) {
          const scope = it.scope === 'sector' ? '板块' : (it.scope === 'stock' ? '个股' : esc(it.scope || ''));
          return '<div class="tb-bt-item"><span class="tb-bt-scope">' + scope + '</span>' +
            '<span class="tb-bt-entity">' + esc(it.entity_id || it.name || '—') + '</span>' +
            '<span class="tb-bt-stage">' + esc(BIG_TREND_LABELS[it.stage] || it.stage || '') + '</span></div>';
        }).join('') + '</div>'
      : '';
    return '<div class="tb-card tb-bigtrend">' +
      '<div class="tb-card-title">主升浪雷达 · <span class="tb-bt-status">' + esc(label) + '</span></div>' +
      (bt.message ? '<div class="tb-na-desc">' + esc(bt.message) + '</div>' : '') +
      itemHtml + '</div>';
  }

  /* ---------------- Strategy Evidence（禁止合成当真实战绩） ---------------- */
  function strategyEvidenceBlock(se) {
    if (!se || typeof se !== 'object') return '';
    const status = se.status || '';
    if (status === 'INSUFFICIENT_REAL_EVIDENCE') {
      const msg = se.message || '当前只有工程合同和合成验证，暂不展示真实策略战绩。合成 benchmark 的收益率 / 胜率 / 回撤不得宣称为真实战绩。';
      return '<div class="tb-card tb-strat">' +
        '<div class="tb-strat-ico">⚠</div>' +
        '<div><div class="tb-strat-title">策略战绩：暂未展示真实表现</div>' +
        '<div class="tb-strat-desc">' + esc(msg) + '</div></div></div>';
    }
    return '<div class="tb-card tb-strat"><div class="tb-strat-title">策略战绩 · ' + esc(status) + '</div>' +
      (se.message ? '<div class="tb-strat-desc">' + esc(se.message) + '</div>' : '') + '</div>';
  }

  /* ---------------- 市场姿态 ---------------- */
  function postureBlock(p) {
    if (!p || typeof p !== 'object') return '';
    const regime = UI.REGIME_LABELS[p.regime] || p.regime || '—';
    const aggression = AGGRESSION_LABELS[p.aggression_level] ||
      (typeof p.aggression_level === 'number' ? ('进攻度 ' + p.aggression_level) : '—');
    const theme = p.strongest_theme || '—';
    const risk = p.main_risk || '—';
    return '<div class="tb-card tb-posture">' +
      '<div class="tb-card-title">市场姿态</div>' +
      '<div class="tb-posture-grid">' +
      '<div class="tb-pp"><span class="tb-pp-k">Regime</span><span class="tb-pp-v">' + esc(regime) + '</span></div>' +
      '<div class="tb-pp"><span class="tb-pp-k">建议进攻度</span><span class="tb-pp-v">' + esc(aggression) + '</span></div>' +
      '<div class="tb-pp"><span class="tb-pp-k">最强主线</span><span class="tb-pp-v">' + esc(theme) + '</span></div>' +
      '<div class="tb-pp"><span class="tb-pp-k">今日主要风险</span><span class="tb-pp-v tb-risk">' + esc(risk) + '</span></div>' +
      '</div></div>';
  }

  /* ---------------- 参谋摘要（D，确定性模板，非 LLM） ---------------- */
  function summaryBlock(s) {
    if (!s || typeof s !== 'object') return '';
    const modeLabel = (s.mode === 'DETERMINISTIC_TEMPLATE') ? '确定性参谋摘要（基于结构化事实，非在线 LLM）' : esc(s.mode || '参谋摘要');
    const facts = Array.isArray(s.facts) ? s.facts : [];
    const factsHtml = facts.length
      ? '<ul class="tb-facts">' + facts.map(function (f) { return '<li>' + esc(f) + '</li>'; }).join('') + '</ul>'
      : '';
    return '<div class="tb-card tb-summary">' +
      '<div class="tb-card-title">AI 参谋摘要</div>' +
      '<div class="tb-summary-mode">' + modeLabel + '</div>' +
      (s.text ? '<div class="tb-summary-text">' + esc(s.text) + '</div>' : '') +
      factsHtml + '</div>';
  }

  /* ---------------- 数据 / 模型证据状态 ---------------- */
  function dataHealthBlock(brief) {
    const bits = [];
    if (brief.as_of) bits.push('as_of ' + esc(brief.as_of));
    if (brief.schema_version) bits.push('schema ' + esc(brief.schema_version));
    if (brief.data_status) bits.push('data ' + esc(brief.data_status));
    if (brief.ranking_mode) bits.push('rank ' + esc(brief.ranking_mode));
    if (brief.evidence_id) bits.push('evidence ' + esc(brief.evidence_id));
    return '<div class="tb-card tb-datahealth">' +
      '<div class="tb-card-title">数据与模型证据状态</div>' +
      (bits.length ? '<div class="tb-meta">' + bits.map(function (b) { return '<span>' + b + '</span>'; }).join('') + '</div>' : '<div class="tb-na-desc">无证据信息</div>') +
      '<div class="tb-disclaim">本页为辅助决策参考，不构成投资建议。概率/战绩以真实样本与校准证据为准，证据不足时一律不展示。</div>' +
      '</div>';
  }

  /* ---------------- 主渲染：完整 brief ---------------- */
  function render(brief) {
    if (!brief || typeof brief !== 'object') {
      return '<div class="loading-box">今日作战简报数据缺失。</div>';
    }
    const core = Array.isArray(brief.core_opportunities)
      ? brief.core_opportunities.slice(0, 5) : [];
    const holdings = Array.isArray(brief.holding_actions) ? brief.holding_actions : [];
    const avoids = Array.isArray(brief.avoid_reasons) ? brief.avoid_reasons : [];

    // 「今天建议你做」汇总（来自 brief.actions + core 状态归类）
    const acts = brief.actions || {};
    const stateCount = {};
    core.forEach(function (o) { const s = o.action_state || 'OTHER'; stateCount[s] = (stateCount[s] || 0) + 1; });
    const summaryBits = [];
    if (acts.executable_count != null) summaryBits.push(['可执行', acts.executable_count, 'a-exec']);
    if (acts.waiting_count != null) summaryBits.push(['等待条件', acts.waiting_count, 'a-watch']);
    if (acts.holding_attention_count != null) summaryBits.push(['持仓需处理', acts.holding_attention_count, 'a-warn']);
    const summaryHtml = summaryBits.length
      ? '<div class="tb-do-summary">' + summaryBits.map(function (b) {
          return '<div class="tb-do ' + b[2] + '"><span class="tb-do-n">' + b[1] + '</span><span class="tb-do-l">' + esc(b[0]) + '</span></div>';
        }).join('') + '</div>'
      : '';

    let html = '';
    html += summaryBlock(brief.summary);
    html += postureBlock(brief.market_posture);
    if (summaryHtml) html += '<div class="tb-card tb-dosuggest"><div class="tb-card-title">今天建议你做</div>' + summaryHtml + '</div>';

    // Core Opportunities（3—5）
    html += '<div class="tb-section-label">Core Opportunities（' + core.length + '）</div>';
    html += core.length
      ? core.map(coreOpportunityCard).join('')
      : '<div class="card-empty">当前没有达到可执行/观察条件的 Core Opportunity。</div>';

    // 持仓需要处理（与机会分开）
    if (holdings.length) {
      html += '<div class="tb-section-label">持仓需要处理</div>';
      html += holdings.map(holdingCard).join('');
    }

    // 今日不要做
    if (avoids.length) {
      html += '<div class="tb-section-label">今日不要做</div>';
      html += '<div class="tb-card tb-avoid"><ul class="tb-avoid-list">' +
        avoids.map(function (a) {
          if (typeof a === 'string') return '<li>' + esc(a) + '</li>';
          const item = a && typeof a === 'object' ? a : {};
          return '<li><span class="tb-avoid-code">' + esc(item.code || '') + '</span>' +
            esc(item.message || '') + '</li>';
        }).join('') + '</ul></div>';
    }

    // 主升浪状态
    html += bigTrendBlock(brief.big_trend);

    // 数据与模型证据
    html += strategyEvidenceBlock(brief.strategy_evidence);
    html += dataHealthBlock(brief);

    return html;
  }

  /* ---------------- 兼容降级：旧 /api/overview 合同（明确标旧，不生成新字段） ---------------- */
  function renderLegacy(overview, opts) {
    opts = opts || {};
    const ov = overview || {};
    let html = '<div class="tb-card tb-legacy-banner">' +
      '<div class="tb-na-title">旧版总览合同 · 非今日作战简报</div>' +
      '<div class="tb-na-desc">/api/brief/today 暂不可用，以下由旧版 /api/overview 派生，仅展示其已有字段，' +
      '不合成动作简报、不生成概率/主升浪/策略战绩等新字段。</div></div>';

    // 市场姿态：仅当旧版含 regime 时展示，绝不以空值编造
    if (ov.regime) html += postureBlock({ regime: ov.regime.regime || ov.regime, aggression_level: '—', strongest_theme: '—', main_risk: '—' });

    // Core Opportunities：复用旧版 top_opportunities（UI.renderTopList 不引入新字段）
    const tops = Array.isArray(ov.top_opportunities) ? ov.top_opportunities : [];
    html += '<div class="tb-section-label">重点机会（旧版，' + tops.length + '）</div>';
    html += tops.length ? '<div class="tb-legacy-list">' + UI.renderTopList(tops, null) + '</div>'
      : '<div class="card-empty">旧版总览暂无重点机会。</div>';

    // 持仓：旧版 holding_signals
    const hs = Array.isArray(ov.holding_signals) ? ov.holding_signals : [];
    if (hs.length) {
      html += '<div class="tb-section-label">持仓信号（旧版）</div>';
      html += '<div class="tb-legacy-list">' + UI.renderHoldingSignals(hs) + '</div>';
    }

    // 诚实声明：概率 / 主升浪 / 策略战绩 旧版不提供
    html += '<div class="tb-card tb-bigtrend"><div class="tb-card-title">主升浪雷达</div>' +
      '<div class="tb-notavail"><span class="tb-na-ico">⚠</span><div><div class="tb-na-title">旧版合同不提供</div>' +
      '<div class="tb-na-desc">主升浪识别将在 Stage 3 提供，旧版总览不含此字段。</div></div></div></div>';
    html += strategyEvidenceBlock({ status: 'INSUFFICIENT_REAL_EVIDENCE', message: '旧版总览合同不含策略战绩字段。' });
    return html;
  }

  global.Today = {
    render: render,
    renderLegacy: renderLegacy,
    actionBadge: actionBadge
  };
})(window);
