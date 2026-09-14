/* N4 readonly comparison renderer. No portfolio, signals, network or storage writes. */
(function (root) {
  'use strict';
  const schema = 'evidence-comparison-v1';
  const policy = 'evidence-explicit-missingness-candidate-v1';
  const recipe = 'legacy-five-family-four-score-ab0fc66';
  const families = ['trend', 'momentum', 'relative_strength', 'volume_liquidity', 'price_structure'];
  const scores = ['opportunity', 'timing', 'risk', 'confidence'];
  const quoteStatuses = Object.freeze({LIVE:'声明实时',DELAYED:'声明延迟',STALE:'声明过期',UNKNOWN:'声明未知'});
  const familyDependencies = Object.freeze({TREND_REQUIRED:'trend',MOMENTUM_REQUIRED:'momentum',
    RELATIVE_STRENGTH_REQUIRED:'relative_strength',LIQUIDITY_REQUIRED:'volume_liquidity',VOLUME_LIQUIDITY_REQUIRED:'volume_liquidity',
    STRUCTURE_REQUIRED:'price_structure',PRICE_STRUCTURE_REQUIRED:'price_structure'});
  // Input graph of the fixed Python recipe. No coefficients or score calculations live here.
  const termSchema = Object.freeze({
    trend:{base:[],ma20_distance:['LAST_REQUIRED','MA20_REQUIRED'],ma60_distance:['LAST_REQUIRED','MA60_REQUIRED'],
      ma_order:['MA20_REQUIRED','MA60_REQUIRED'],atr_proxy:['ATR14_REQUIRED','LAST_REQUIRED']},
    momentum:{base:[],rsi:['RSI14_REQUIRED'],roc_mean:['ROC5_REQUIRED','ROC10_REQUIRED','ROC20_REQUIRED'],macd:['MACD_HIST_REQUIRED']},
    relative_strength:{sector_rs:['SECTOR_RS_REQUIRED'],day_proxy:['DAY_CHANGE_REQUIRED']},
    volume_liquidity:{liquidity_proxy:['TURNOVER_REQUIRED','AMOUNT_REQUIRED']},
    price_structure:{base:[],intraday_position:['LAST_REQUIRED','HIGH_REQUIRED','LOW_REQUIRED'],breakout:['LAST_REQUIRED','SIX_BARS_REQUIRED']},
    opportunity:{relative_strength:['RELATIVE_STRENGTH_REQUIRED'],trend_momentum:['TREND_REQUIRED','MOMENTUM_REQUIRED'],
      sector:['SECTOR_SCORE_REQUIRED'],catalyst:['SECTOR_CATALYST_CONTEXT_REQUIRED'],liquidity:['LIQUIDITY_REQUIRED'],
      structure:['STRUCTURE_REQUIRED'],regime:['REGIME_REQUIRED'],persistence:['SECTOR_PERSISTENCE_REQUIRED'],risk_penalty:['RISK_REQUIRED']},
    timing:{trend:['TREND_REQUIRED'],momentum:['MOMENTUM_REQUIRED'],structure:['STRUCTURE_REQUIRED']},
    risk:{base:[],gain_from_low:['LAST_REQUIRED','HIGH_REQUIRED','LOW_REQUIRED'],crowding:['SECTOR_CROWDING_REQUIRED'],
      regime_risk:['REGIME_REQUIRED'],range:['HIGH_REQUIRED','LOW_REQUIRED','PREV_CLOSE_REQUIRED']},
    confidence:{dq:['DQ_REQUIRED'],agreement:['TREND_REQUIRED','MOMENTUM_REQUIRED','RELATIVE_STRENGTH_REQUIRED','VOLUME_LIQUIDITY_REQUIRED','PRICE_STRUCTURE_REQUIRED'],regime:['REGIME_REQUIRED']}
  });
  const labels = {trend:'趋势', momentum:'动量', relative_strength:'相对强弱', volume_liquidity:'量能与流动性',
    price_structure:'价格结构', opportunity:'机会规则分', timing:'时机规则分', risk:'风险规则分', confidence:'规则置信分（非概率）'};
  const reasons = {
    RSI_ZERO_WAS_NEUTRALIZED:'旧规则把 RSI 的真实 0 回填为 50；候选保留真实零。',
    MA60_WAS_LAST_PRICE_FALLBACK:'MA60 样本不足，旧规则使用末价代替；候选不拼出趋势总分。',
    RSI_MISSING_WAS_50:'RSI 尚不可计算，旧规则使用中性值；候选保持未知。',
    MACD_MISSING_WAS_DESCRIBED_DOWN:'缺少完整 MACD，旧文字仍描述向下；候选标记未知。',
    ROC_MISSING_WAS_ZERO:'部分 ROC 窗口不足，旧规则补零；候选不重新平均剩余窗口。',
    DAY_PROXY_WAS_TREND_FALLBACK:'旧规则用当日涨跌估算趋势；这不等同于数周至数月的趋势证据。',
    DQ_MISSING_WAS_100:'缺少数据质量结论，旧规则按 100 参与计算；候选不生成置信分。',
    REGIME_MISSING_WAS_NEUTRAL:'市场上下文没有绑定，候选不以中性分代替。',
    SECTOR_MISSING_WAS_NEUTRAL:'板块归属/上下文没有绑定，候选不以中性分代替。'
  };
  const missing = {MA20_REQUIRED:'MA20样本',MA60_REQUIRED:'MA60样本',ATR14_REQUIRED:'ATR14样本',
    RSI14_REQUIRED:'RSI14样本',MACD_HIST_REQUIRED:'完整MACD',ROC5_REQUIRED:'ROC5',ROC10_REQUIRED:'ROC10',ROC20_REQUIRED:'ROC20',
    LAST_REQUIRED:'有效报价',PREV_CLOSE_REQUIRED:'昨收',HIGH_REQUIRED:'日高',LOW_REQUIRED:'日低',SIX_BARS_REQUIRED:'结构窗口',
    DQ_REQUIRED:'有效DQ结论',DQ_NOT_VALID:'DQ非有效状态',REGIME_REQUIRED:'市场上下文',SECTOR_RS_REQUIRED:'板块相对强弱',
    SECTOR_CROWDING_REQUIRED:'板块拥挤度',SECTOR_SCORE_REQUIRED:'板块规则分',SECTOR_PERSISTENCE_REQUIRED:'板块持续性',
    SECTOR_CATALYST_CONTEXT_REQUIRED:'催化上下文',TURNOVER_REQUIRED:'换手字段',AMOUNT_REQUIRED:'成交额',
    DAY_CHANGE_REQUIRED:'日变化',TREND_REQUIRED:'完整趋势族',MOMENTUM_REQUIRED:'完整动量族',
    RELATIVE_STRENGTH_REQUIRED:'相对强弱族',LIQUIDITY_REQUIRED:'量能流动性族',VOLUME_LIQUIDITY_REQUIRED:'量能流动性族',
    STRUCTURE_REQUIRED:'结构族',PRICE_STRUCTURE_REQUIRED:'结构族',RISK_REQUIRED:'完整风险项',NUMERIC_UNAVAILABLE:'数值不可表示'};
  const esc = v => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const value = v => v === null || (finite(v) && Number.isInteger(v) && v >= 0 && v <= 100);
  const text = v => typeof v === 'string' && v.length <= 512;
  const texts = v => Array.isArray(v) && v.length <= 64 && v.every(text);
  const sha = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
  const keysEqual = (obj, keys) => object(obj) && Object.keys(obj).length === keys.length && keys.every(k => Object.hasOwn(obj,k));
  const num = v => finite(v) ? (Number.isInteger(v) ? String(v) : (v !== 0 && Math.abs(v) < .001 ? v.toExponential(2) : String(Number(v.toFixed(3))))) : '—';
  const unavailable = () => '<section class="ec-panel ec-unavailable" role="status">评分依据核对暂不可用；既有信号不受影响，候选未启用。</section>';
  function groupValid(group, key) {
    if (!object(group) || group.key !== key || !value(group.value) || !texts(group.missing) ||
        !Array.isArray(group.terms) || !group.terms.length || group.terms.length > 16) return false;
    if ((group.value === null) !== (group.missing.length > 0)) return false;
    if (group.multiplier !== null && (!finite(group.multiplier) || ![.8,.9,1].includes(group.multiplier))) return false;
    if (group.multiplier === null && group.value !== null) return false;
    if (group.status !== (group.value === null ? 'MISSING_INPUT' : 'NUMERIC_ONLY')) return false;
    if (!Object.hasOwn(termSchema,key)) return false;
    const shape = termSchema[key];
    if (group.terms.length !== Object.keys(shape).length) return false;
    const names = new Set();
    const termsValid = group.terms.every(t => {
      if (!object(t) || !text(t.key) || names.has(t.key) || !text(t.label) || !texts(t.missing) || !object(t.inputs)) return false;
      names.add(t.key);
      if (!Object.hasOwn(shape,t.key)) return false;
      let required = shape[t.key];
      if (key === 'volume_liquidity') {
        const turnover = t.inputs.TURNOVER_REQUIRED;
        if (turnover !== null && (!finite(turnover) || turnover < 0)) return false;
        required = turnover !== null && turnover > 0 ? ['TURNOVER_REQUIRED'] : ['TURNOVER_REQUIRED','AMOUNT_REQUIRED'];
      }
      if (!keysEqual(t.inputs,required)) return false;
      const inputTypesValid = Object.entries(t.inputs).every(([dependency,input]) => {
        if (input === null) return true;
        if (key === 'risk' && t.key === 'regime_risk' && dependency === 'REGIME_REQUIRED') {
          return ['RISK_OFF','OVERHEATED','PANIC_REBOUND','ROTATION','RISK_ON_TREND'].includes(input);
        }
        return finite(input);
      });
      if (!inputTypesValid) return false;
      if (t.value !== null && (!finite(t.value) || Math.abs(t.value)>1e100)) return false;
      if ((t.value === null) !== (t.missing.length>0)) return false;
      if (!['NUMERIC_ONLY','MISSING_INPUT','NUMERIC_UNAVAILABLE'].includes(t.status)) return false;
      if ((t.status === 'NUMERIC_ONLY') !== (t.value !== null)) return false;
      const deps = Object.values(t.inputs);
      return deps.length <= 12 && deps.every(v => v === null || finite(v) || text(v)) &&
        (t.value === null || deps.every(v => v !== null)) &&
        Object.entries(t.inputs).every(([key,val]) => val !== null || t.missing.includes(key));
    });
    if (!termsValid) return false;
    // A missing leaf cannot coexist with a numeric parent or disappear from its reasons.
    if (group.terms.some(t => t.value === null) && group.value !== null) return false;
    return group.terms.every(t => t.missing.every(code => group.missing.includes(code)));
  }
  function valid(d, symbol, market) {
    if (!object(d) || d.schema !== schema || d.policy_id !== policy || d.legacy_recipe_id !== recipe ||
        d.symbol !== symbol || d.market !== market || d.interval !== '1d' || d.assurance !== 'RUNTIME_DIAGNOSTIC_ONLY') return false;
    if (!['A','HK','US'].includes(market) || typeof symbol !== 'string' || !/^[A-Z0-9._-]+\.(SH|SZ|HK|US)$/.test(symbol)) return false;
    const suffix = symbol.split('.').pop();
    if (!({A:['SH','SZ'],HK:['HK'],US:['US']}[market] || []).includes(suffix)) return false;
    if (!['COMPARABLE_NUMERIC','PARTIAL','INVALID_INPUT','NO_QUOTE'].includes(d.status) ||
        !['auto_trade','execution_authorized','affects_live_scores','investment_performance_claim'].every(k => d[k] === false) ||
        d.success_probability !== null || !texts(d.issues) || !texts(d.findings) || !texts(d.warnings)) return false;
    if (!object(d.candidate) || !object(d.legacy) || !Array.isArray(d.differences)) return false;
    if (['INVALID_INPUT','NO_QUOTE'].includes(d.status)) {
      return d.input_id === null && d.candidate.families?.length === 0 && d.candidate.scores?.length === 0 && d.differences.length === 0;
    }
    if (!sha(d.input_id) || !sha(d.report_id) || !object(d.sample_info) || d.sample_info.input_id !== d.input_id ||
        !text(d.computed_at) || !/(Z|[+-]\d\d:\d\d)$/.test(d.computed_at) || !Number.isFinite(Date.parse(d.computed_at))) return false;
    const info = d.sample_info;
    if (info.computed_at !== d.computed_at) return false;
    if (typeof info.quote_declared_status !== 'string' || !Object.hasOwn(quoteStatuses, info.quote_declared_status) ||
        (info.quote_declared_status !== 'LIVE') !== d.warnings.includes('QUOTE_NOT_DECLARED_LIVE')) return false;
    if (!['input_count','selected_count','same_day_excluded','older_excluded'].every(k => Number.isInteger(info[k]) && info[k]>=0) ||
        info.input_count > 260 || info.selected_count > 80 || info.input_count !== info.selected_count+info.same_day_excluded+info.older_excluded ||
        !text(info.quote_source) || !text(info.quote_source_timestamp) || !text(info.quote_received_at)) return false;
    if (!['NUMERIC_ONLY','UNAVAILABLE'].includes(d.legacy.status)) return false;
    for (const [section, keys] of [['families',families],['scores',scores]]) {
      const groups = d.candidate[section];
      if (!Array.isArray(groups) || groups.length !== keys.length || !groups.every((g,i) => groupValid(g,keys[i]))) return false;
      if (d.legacy.status === 'NUMERIC_ONLY' && (!keysEqual(d.legacy[section],keys) || !keys.every(k => value(d.legacy[section][k]) && d.legacy[section][k] !== null))) return false;
      if (d.legacy.status === 'UNAVAILABLE' && !keysEqual(d.legacy[section],[])) return false;
    }
    const valuesByFamily = Object.fromEntries(d.candidate.families.map(g => [g.key,g.value]));
    const risk = d.candidate.scores.find(g => g.key === 'risk');
    for (const group of d.candidate.scores) {
      for (const term of group.terms) {
        for (const [dependency,input] of Object.entries(term.inputs)) {
          if (Object.hasOwn(familyDependencies,dependency) && input !== valuesByFamily[familyDependencies[dependency]]) return false;
          // Risk contribution uses its unrounded value, so compare availability, not rounded equality.
          if (dependency === 'RISK_REQUIRED' && (input === null) !== (risk.value === null)) return false;
        }
      }
    }
    if (!['UNKNOWN','UP','DOWN','FLAT'].includes(d.candidate.macd_direction) || d.differences.length !== 9) return false;
    let index = 0;
    for (const [section, keys] of [['families',families],['scores',scores]]) {
      for (let i=0; i<keys.length; i++) {
        const diff = d.differences[index++];
        const old = d.legacy.status === 'NUMERIC_ONLY' ? d.legacy[section][keys[i]] : null;
        const candidate = d.candidate[section][i].value;
        if (!object(diff) || diff.section !== section || diff.key !== keys[i] || diff.legacy_value !== old || diff.candidate_value !== candidate ||
            diff.delta !== (old === null || candidate === null ? null : candidate-old)) return false;
      }
    }
    return d.status !== 'COMPARABLE_NUMERIC' || (d.legacy.status === 'NUMERIC_ONLY' && d.candidate.scores.every(g => g.value !== null));
  }
  function render(d, symbol, market) {
    if (d === null || d === undefined) return ''; // Older Engine: optional additive field.
    if (!valid(d, symbol, market)) return unavailable();
    if (['INVALID_INPUT','NO_QUOTE'].includes(d.status)) return unavailable();
    const info=d.sample_info;
    const table = d.differences.map(diff => '<tr data-ec-key="'+esc(diff.key)+'"><th scope="row">'+esc(labels[diff.key])+'</th><td>'+num(diff.legacy_value)+
      '</td><td>'+num(diff.candidate_value)+'</td><td>'+num(diff.delta)+'</td></tr>').join('');
    const terms = [...d.candidate.families,...d.candidate.scores].map(g => '<details class="ec-group"><summary>'+esc(labels[g.key])+' · '+num(g.value)+
      (g.missing.length ? ' · 缺少 '+g.missing.map(x=>esc(Object.hasOwn(missing,x)?missing[x]:x)).join('、') : '')+'</summary><ul>'+g.terms.map(t =>
      '<li><span>'+esc(t.label)+'</span><strong>'+num(t.value)+'</strong>'+(t.missing.length?'<small>需要 '+t.missing.map(x=>esc(Object.hasOwn(missing,x)?missing[x]:x)).join('、')+'</small>':'')+'</li>').join('')+'</ul></details>').join('');
    const warnings = [];
    if(d.warnings.includes('QUOTE_NOT_DECLARED_LIVE')) warnings.push('报价未声明为实时；本表仅解释所提供输入，不授权交易或提升数据等级。');
    if(d.warnings.includes('NAIVE_TIME_NOT_PIT')) warnings.push('来源时间包含无时区旧格式，仅作为原日期标签展示。');
    if(d.warnings.includes('FLAT_MACD_NEGATIVE_TERM_RETAINED')) warnings.push('MACD 为零仍沿用旧规则负项，未在本候选中调整该偏差。');
    if(d.warnings.includes('FLAT_RSI_100_CONVENTION_RETAINED')) warnings.push('平盘 RSI=100 是旧公式约定，不等于看涨判断。');
    return '<details class="ec-panel" data-evidence-comparison><summary><span>评分依据核对</span><span class="ec-mode">只读候选 · 未启用</span></summary>'+
      '<p>同一输入下的旧规则重算与缺失语义候选对照。<b>不是当前信号的历史分数，不改变排序或仓位；置信分不是成功概率。</b></p>'+
      '<p class="ec-meta">过去日期日线 '+info.selected_count+' 根；同日排除 '+info.same_day_excluded+' 根。'+
      '计算时间 '+esc(d.computed_at)+'<br>报价来源 '+esc(info.quote_source||'未知')+'；源时间 '+esc(info.quote_source_timestamp)+
      '；接收时间 '+esc(info.quote_received_at)+'<br>报价状态：'+esc(quoteStatuses[info.quote_declared_status])+
      '（'+esc(info.quote_declared_status)+'），仅来源声明，未经独立认证。</p>'+
      '<p>数据获知时间、复权和交易日连续性尚未独立验证；这里不是实时做 T 信号。</p>'+
      '<div class="ec-scroll" tabindex="0" aria-label="评分对照表"><table><caption>缺依赖显示 —；真实零值保留</caption><thead><tr><th>项目</th><th>旧重算</th><th>候选</th><th>差值</th></tr></thead><tbody>'+table+'</tbody></table></div>'+
      (d.findings.length?'<ul class="ec-findings">'+d.findings.map(k=>'<li>'+esc(Object.hasOwn(reasons,k)?reasons[k]:k)+'</li>').join('')+'</ul>':'')+
      warnings.map(w=>'<p class="ec-meta">'+esc(w)+'</p>').join('')+terms+
      '<p class="ec-meta">候选版本 '+esc(d.policy_id)+'；输入摘要 '+esc(d.input_id.slice(0,12))+'（仅重现标识，不是可信准入）</p></details>';
  }
  root.EvidenceComparison = Object.freeze({render, valid});
})(typeof window !== 'undefined' ? window : globalThis);
