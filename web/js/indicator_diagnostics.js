/* Numerical sample diagnostics only. No scores, trading quantities or probabilities. */
(function(global) {
  'use strict';
  const esc = global.Fmt.esc;
  const WINDOWS = [20,60,120,252];
  const ISSUES = {
    BAR_IDENTITY_OR_INTERVAL_MISMATCH:'证券或日线周期不一致',
    INVALID_TIMESTAMP:'时间字段无效', DUPLICATE_OR_UNORDERED_DAILY_DATE:'重复日期或顺序异常',
    FUTURE_BAR_DATE:'包含未来日期', INVALID_OHLCV:'价格或成交量输入无效',
    SOURCE_UNAVAILABLE:'来源未记录', MIXED_SOURCE_SERIES:'混合来源尚未对齐',
    MIXED_TIMESTAMP_BASIS:'时间基准混用', INVALID_CONTAINER_OR_LIMIT:'数据形状或数量超限',
    NO_PRIOR_DAY_SAMPLES:'尚无此前日期的日线'
  };
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 && value <= 260; }
  function numeric(value, percent) {
    return finite(value) ? value.toFixed(percent ? 2 : 4)+(percent?'%':'') : '—';
  }
  function unavailable() { return '<div class="ind-empty nd-unavailable">窗口核对暂不可用；不推断缺失指标。</div>'; }
  function render(data, symbol) {
    const suffix = typeof symbol === 'string' ? symbol.split('.').pop() : '';
    const market = {SH:'A', SZ:'A', BJ:'A', HK:'HK', US:'US'}[suffix];
    if (!data || !market || data.market !== market || data.interval !== '1d' ||
        data.schema !== 'daily-window-diagnostics-v1' || data.symbol !== symbol ||
        data.assurance !== 'RUNTIME_DIAGNOSTIC_ONLY' || data.auto_trade !== false ||
        data.execution_authorized !== false || data.calendar_coverage_verified !== false ||
        !count(data.sample_count) || !count(data.same_day_excluded) ||
        !['NUMERIC_ONLY','NO_DATA','INVALID_INPUT'].includes(data.status)) return unavailable();
    let html='<section class="nd-panel" aria-label="波段窗口与样本核对"><h3>波段窗口与样本核对</h3>'+
      '<p>仅日线数值，不是周线策略、买卖信号或做 T 指令；样本数不证明交易日连续，复权和获知时间尚未核验。</p>';
    if (data.status !== 'NUMERIC_ONLY') {
      const issues=Array.isArray(data.issues)?data.issues.slice(0,12).map(x=>ISSUES[x]||'输入需核对').join('、'):'';
      return html+'<div class="ind-empty">'+(data.status==='NO_DATA'?'暂无可用日线窗口':'数据需核对')+(issues?'：'+esc(issues):'')+'</div></section>';
    }
    if (!Array.isArray(data.windows) || data.windows.length!==WINDOWS.length ||
        data.windows.some((w,i)=>!w||w.sample_window!==WINDOWS[i]||!count(w.available_samples)||
          w.available_samples!==Math.min(data.sample_count,w.sample_window)||
          !['NUMERIC_ONLY','INSUFFICIENT_SAMPLES','NUMERIC_UNAVAILABLE'].includes(w.state)) ||
        !/^\d{4}-\d{2}-\d{2}$/.test(data.last_date) || !/^\d{4}-\d{2}-\d{2}$/.test(data.first_date) ||
        !Number.isFinite(Date.parse(data.computed_at))) return unavailable();
    html+='<p class="nd-asof">'+esc(symbol)+' · 数据日期 '+esc(data.first_date)+' 至 '+esc(data.last_date)+
      ' · '+data.sample_count+' 根；计算于 '+esc(data.computed_at)+'</p>'+
      '<p>已保守排除当日 '+data.same_day_excluded+' 根日线；不会把盘中未完结数据当收盘。</p>';
    if(data.time_basis==='LEGACY_NAIVE_DATE') html+='<p>历史无时区日期，仅作样本标签，不代表可证明的获知时点。</p>';
    html+='<div class="nd-scroll"><table><caption>日线窗口：变化率比均线多需一根基准样本</caption><thead><tr><th scope="col">窗口</th><th scope="col">样本</th><th scope="col">均价</th><th scope="col">窗口变化</th></tr></thead><tbody>';
    for(const row of data.windows){
      const meanOk=row.state==='NUMERIC_ONLY'&&data.sample_count>=row.sample_window;
      const changeOk=meanOk&&data.sample_count>row.sample_window;
      html+='<tr data-window="'+row.sample_window+'"><th scope="row">'+row.sample_window+' 根日线</th><td>'+row.available_samples+'/'+row.sample_window+
        (meanOk?'':' · 样本不足或不可计算')+'</td><td class="nd-mean">'+numeric(meanOk?row.mean_close:null,false)+
        '</td><td class="nd-change">'+numeric(changeOk?row.change_percent:null,true)+'</td></tr>';
    }
    html+='</tbody></table></div><details><summary>指标方法与预热边界</summary><p>RSI14/ATR14使用滚动算术平均，不是Wilder平滑；历史平盘RSI约定为100，不代表看涨。MACD柱为DIF−DEA，不是两倍柱值。EMA短样本均值不是充分预热的EMA。</p>';
    if(Array.isArray(data.methods)&&data.methods.length<=20){
      html+='<ul>'+data.methods.filter(m=>m&&count(m.available_samples)&&count(m.required_samples)&&m.required_samples>0)
        .map(m=>'<li>'+esc(m.label)+'：'+m.available_samples+'/'+m.required_samples+' 根'+
          (m.available_samples<m.required_samples?'（待预热）':'（仅满足数量）')+'</li>').join('')+'</ul>';
    }
    return html+'</details><p>既有指标卡仍使用最近最多80根；上表独立读取最多260根，仅辅助核对，不改变默认评分和风险参数。</p></section>';
  }
  global.IndicatorDiagnostics=Object.freeze({render:render});
})(window);
