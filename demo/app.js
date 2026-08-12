/* =========================================================================
 * app.js —— 《私享股池》纯前端交互逻辑
 * 负责：底部导航切换 / 顶部市场切换 / 标签筛选 / 加入自选 / 信号详情弹层
 * 全部基于 MOCK（mock-data.js）渲染，无任何网络请求。
 * ========================================================================= */

/* ---------- 全局运行状态 ---------- */
const state = {
  market: 'A',                 // 当前市场 A | HK | US
  watchlist: new Set(MOCK.DEFAULT_WATCHLIST), // 自选股（内存态，点一下变已加入）
  recoTag: null,               // 推荐页当前筛选标签（null = 全部）
  filter: { market: 'A', industry: '全部', shape: '全部' }, // 市场筛选页条件
};

/* ---------- 工具函数 ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// 涨跌幅着色 class
const chgClass = (pct) => (pct >= 0 ? 'up' : 'down');
// 格式化带正负号的涨跌幅
const fmtPct = (pct) => (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
// 信号级别 -> {label, color}
const sig = (lv) => MOCK.SIGNAL_LEVELS[lv];

// 轻提示
let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 1400);
}

// 取某市场的个股
const stocksOf = (m) => MOCK.STOCKS.filter((s) => s.market === m);
// 取某市场指数
const indicesOf = (m) => MOCK.INDICES.filter((i) => i.market === m);
// 按 code 取个股
const byCode = (code) => MOCK.STOCKS.find((s) => s.code === code);

/* =======================================================================
 * 渲染：① 行情总览
 * ===================================================================== */
function renderOverview() {
  // 指数卡片
  const grid = $('#indexGrid');
  grid.innerHTML = indicesOf(state.market).map((i) => `
    <div class="index-card">
      <div class="index-name">${i.name}</div>
      <div class="index-value">${i.value.toFixed(2)}</div>
      <div class="index-chg ${chgClass(i.changePct)}">${fmtPct(i.changePct)}</div>
    </div>`).join('');

  // 涨跌家数分布：根据该市场个股 changePct 统计
  const list = stocksOf(state.market);
  const up = list.filter((s) => s.changePct > 0).length;
  const down = list.filter((s) => s.changePct < 0).length;
  const flat = list.length - up - down;
  const total = list.length;
  $('#upDownBar').innerHTML = `
    <div class="seg-up" style="width:${(up / total) * 100}%">${up}</div>
    <div class="seg-flat" style="width:${(flat / total) * 100}%">${flat || ''}</div>
    <div class="seg-down" style="width:${(down / total) * 100}%">${down}</div>`;
  $('#upDownLegend').innerHTML = `
    <span class="up">涨 ${up}</span><span style="color:#999">平 ${flat}</span><span class="down">跌 ${down}</span>`;

  // 今日异动（该市场）
  const movers = MOCK.MOVER.filter((m) => m.market === state.market);
  $('#moverList').innerHTML = movers.map((m) => `
    <div class="mover-card">
      <div class="mover-head">
        <div><span class="mover-name">${m.name}</span><span class="mover-code">${m.code}</span></div>
        <div class="mover-chg ${chgClass(m.changePct)}">${fmtPct(m.changePct)}</div>
      </div>
      <div class="mover-desc">${m.desc}</div>
    </div>`).join('');
  if (!movers.length) $('#moverList').innerHTML = '<div class="card">该市场暂无异动数据</div>';
}

/* =======================================================================
 * 渲染：② 自选股监控
 * ===================================================================== */
function renderWatch() {
  // 当前市场的自选股
  const mine = stocksOf(state.market).filter((s) => state.watchlist.has(s.code));
  // 今日触发信号（强买 / 关注买 / 关注卖 / 强卖，即非"观察"级别）
  const triggered = mine.filter((s) => s.signalLevel !== 3).length;

  $('#watchSummary').innerHTML = `今日有 <b>${triggered}</b> 只触发信号，记得盯一下`;

  if (!mine.length) {
    $('#watchGroups').innerHTML = '<div class="card">还没有自选股，去「推荐」或「筛选」里加几只吧</div>';
    return;
  }

  // 按 group 分组
  const groups = {};
  mine.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });

  $('#watchGroups').innerHTML = Object.keys(groups).map((g) => `
    <div class="group-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-3)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="12" x2="12" y2="17"/></svg> ${g}（${groups[g].length}）</div>
    ${groups[g].map(rowHtml).join('')}
  `).join('');
}

// 列表行 HTML（用于自选 / 筛选结果）
function rowHtml(s) {
  const s2 = sig(s.signalLevel);
  return `
    <div class="row" data-code="${s.code}">
      <div class="row-main">
        <div class="row-name">${s.name} <span class="row-code">${s.code}</span></div>
        <div class="row-industry">${s.industry}</div>
      </div>
      <div class="row-right">
        <div class="row-price">${s.price.toFixed(2)}</div>
        <div class="row-chg ${chgClass(s.changePct)}">${fmtPct(s.changePct)}</div>
      </div>
      <span class="badge" style="background:${s2.color}">${s2.label}</span>
    </div>`;
}

/* =======================================================================
 * 渲染：③ 买卖点信号
 * ===================================================================== */
function renderSignal() {
  // 当前市场所有个股，按信号级别升序（强买在前）
  const list = stocksOf(state.market).slice().sort((a, b) => a.signalLevel - b.signalLevel);
  $('#signalList').innerHTML = list.map((s) => {
    const s2 = sig(s.signalLevel);
    // 一句话人话理由：取 reasons 里第一个 ok 的作为主因
    const okReason = s.reasons.find((r) => r.ok);
    const reasonText = okReason
      ? `${okReason.name}，量也跟上来了，可以盯一下`
      : `信号偏谨慎，先观察一下再说`;
    return `
      <div class="signal-card" data-code="${s.code}">
        <div class="signal-card-head">
          <div><span class="row-name">${s.name}</span> <span class="row-code">${s.code}</span></div>
          <span class="badge" style="background:${s2.color}">${s2.label}</span>
        </div>
        <div class="signal-reason">${reasonText}</div>
      </div>`;
  }).join('');
}

/* =======================================================================
 * 渲染：④ 个股推荐（种草流）+ 标签筛选
 * ===================================================================== */
function renderRecoTagFilter() {
  $('#recoTagFilter').innerHTML = `
    <div class="tag-chip ${state.recoTag === null ? 'active' : ''}" data-tag="__all">全部</div>
    ${MOCK.ALL_TAGS.map((t) => `
      <div class="tag-chip ${state.recoTag === t ? 'active' : ''}" data-tag="${t}">${t}</div>`).join('')}`;
}

function renderReco() {
  let list = MOCK.RECOMMEND;
  // 顶部市场切换也会影响推荐页：只显示当前市场
  list = list.filter((r) => r.market === state.market);
  if (state.recoTag) list = list.filter((r) => r.tags.includes(state.recoTag));

  $('#recoHint').textContent = '当前市场：' + MOCK.MARKET_NAME[state.market] + ' · 共 ' + list.length + ' 只推荐';

  $('#recoGrid').innerHTML = list.map((r) => {
    const added = state.watchlist.has(r.code);
    return `
      <div class="reco-card">
        <div class="reco-top">
          <span class="reco-name">${r.name}</span>
          <span class="reco-chg ${chgClass(r.changePct)}">${fmtPct(r.changePct)}</span>
        </div>
        <div class="reco-desc">${r.desc}</div>
        <div class="reco-tags">${r.tags.map((t) => `<span class="mini-tag">${t}</span>`).join('')}</div>
        <button class="reco-add ${added ? 'added' : ''}" data-code="${r.code}">${added ? '已加入 ⭐' : '+ 加入自选'}</button>
      </div>`;
  }).join('');
  if (!list.length) $('#recoGrid').innerHTML = '<div class="card" style="grid-column:1/-1">该条件下暂无推荐</div>';
}

/* =======================================================================
 * 渲染：⑤ 市场筛选
 * ===================================================================== */
function renderFilterBar() {
  const shapes = ['全部', '金叉', '多头', '超卖', '突破'];
  $('#filterBar').innerHTML = `
    <div class="filter-row">
      <span class="filter-label">市场</span>
      ${['A', 'HK', 'US'].map((m) => `
        <span class="filter-opt ${state.filter.market === m ? 'active' : ''}" data-k="market" data-v="${m}">${MOCK.MARKET_NAME[m]}</span>`).join('')}
    </div>
    <div class="filter-row">
      <span class="filter-label">行业</span>
      <span class="filter-opt ${state.filter.industry === '全部' ? 'active' : ''}" data-k="industry" data-v="全部">全部</span>
      ${MOCK.INDUSTRIES.map((ind) => `
        <span class="filter-opt ${state.filter.industry === ind ? 'active' : ''}" data-k="industry" data-v="${ind}">${ind}</span>`).join('')}
    </div>
    <div class="filter-row">
      <span class="filter-label">形态</span>
      ${shapes.map((sh) => `
        <span class="filter-opt ${state.filter.shape === sh ? 'active' : ''}" data-k="shape" data-v="${sh}">${sh}</span>`).join('')}
    </div>`;
}

// 技术形态 -> 用 tags 粗略匹配
function matchShape(s, shape) {
  if (shape === '全部') return true;
  if (shape === '金叉') return s.tags.includes('#刚金叉');
  if (shape === '多头') return s.tags.includes('#突破平台');
  if (shape === '超卖') return s.tags.includes('#超卖反弹');
  if (shape === '突破') return s.tags.includes('#突破平台');
  return true;
}

// 筛选结果行：在通用行基础上附加「+加入自选」按钮
// 注意：与 rowHtml 分开，避免影响「自选股监控」页的既有结构
function filterRowHtml(s) {
  const s2 = sig(s.signalLevel);
  const added = state.watchlist.has(s.code);
  return `
    <div class="row" data-code="${s.code}">
      <div class="row-main">
        <div class="row-name">${s.name} <span class="row-code">${s.code}</span></div>
        <div class="row-industry">${s.industry}</div>
      </div>
      <div class="row-right">
        <div class="row-price">${s.price.toFixed(2)}</div>
        <div class="row-chg ${chgClass(s.changePct)}">${fmtPct(s.changePct)}</div>
      </div>
      <span class="badge" style="background:${s2.color}">${s2.label}</span>
      <button class="reco-add ${added ? 'added' : ''}" data-code="${s.code}"
        style="margin-left:8px;padding:8px 12px;white-space:nowrap">${added ? '已加入⭐' : '+加入自选'}</button>
    </div>`;
}

function renderFilterResult() {
  let list = MOCK.STOCKS.filter((s) => s.market === state.filter.market);
  if (state.filter.industry !== '全部') list = list.filter((s) => s.industry === state.filter.industry);
  list = list.filter((s) => matchShape(s, state.filter.shape));

  $('#filterResult').innerHTML = list.length
    ? list.map(filterRowHtml).join('') +
      `<div class="card" style="text-align:center;color:var(--sub);font-size:12px">共 ${list.length} 只 · 点行看理由，按钮可直接加自选</div>`
    : '<div class="card">没有匹配的股票，换个条件试试</div>';
}

/* =======================================================================
 * 信号详情弹层
 * ===================================================================== */
function openSheet(code) {
  const s = byCode(code);
  if (!s) return;
  const s2 = sig(s.signalLevel);
  $('#sheet').innerHTML = `
    <div class="sheet-header">
      <div class="sheet-title">${s.name} <span class="row-code">${s.code}</span>
        <span class="badge" style="background:${s2.color}">${s2.label}</span></div>
      <div class="sheet-sub">现价 ${s.price.toFixed(2)} · 涨跌 ${fmtPct(s.changePct)} · ${s.industry}</div>
    </div>
    <div class="sheet-body">
      <div style="font-size:13px;font-weight:700;margin-bottom:8px;display:flex;align-items:center;gap:6px"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text-2)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg> 理由清单</div>
      ${s.reasons.map((r) => `
        <div class="reason-item">
          <span class="reason-ico">${r.ok
            ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--up)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
            : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--down)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'}</span>
          <div>
            <div class="reason-name">${r.name}</div>
            ${r.note ? `<div class="reason-note">${r.note}</div>` : ''}
          </div>
        </div>`).join('')}
      <button class="reco-add" id="sheetWatch" style="width:100%;margin-top:10px">${state.watchlist.has(s.code) ? '已加入自选 ⭐' : '+ 加入自选'}</button>
    </div>
    <div class="sheet-footer">
      <button class="sheet-close" id="sheetClose">知道啦</button>
    </div>`;
  $('#sheetMask').hidden = false;
  $('#sheetClose').onclick = closeSheet;
  // 弹层内也可加入/移出自选（与推荐页一致），点击后就地更新按钮文案
  $('#sheetWatch').onclick = () => {
    toggleWatch(code);
    const nowAdded = state.watchlist.has(code);
    const b = $('#sheetWatch');
    b.textContent = nowAdded ? '已加入自选 ⭐' : '+ 加入自选';
    b.classList.toggle('added', nowAdded);
  };
}
function closeSheet() { $('#sheetMask').hidden = true; }

/* =======================================================================
 * 加入自选（内存态）
 * ===================================================================== */
function toggleWatch(code) {
  if (state.watchlist.has(code)) {
    state.watchlist.delete(code);
    toast('已移出自选');
  } else {
    state.watchlist.add(code);
    toast('已加入自选 ⭐');
  }
  // 重渲染受影响的页面
  renderWatch();
  renderReco();
  renderFilterResult();
}

/* =======================================================================
 * 页面切换
 * ===================================================================== */
const PAGES = ['overview', 'watch', 'signal', 'reco', 'filter'];
function showPage(name) {
  PAGES.forEach((p) => $('#page-' + p).classList.toggle('active', p === name));
  $$('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
}

/* =======================================================================
 * 事件绑定（事件委托，挂载一次即可）
 * ===================================================================== */
function bindEvents() {
  // 底部导航
  $$('.nav-btn').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.page)));

  // 顶部市场切换
  $$('.market-tab').forEach((t) => t.addEventListener('click', () => {
    state.market = t.dataset.market;
    $$('.market-tab').forEach((x) => x.classList.toggle('active', x === t));
    // 市场变了，重渲染所有依赖市场的页面
    renderOverview();
    renderWatch();
    renderSignal();
    renderReco();
    renderRecoTagFilter();
  }));

  // 推荐页标签筛选（委托到容器）
  $('#recoTagFilter').addEventListener('click', (e) => {
    const chip = e.target.closest('.tag-chip');
    if (!chip) return;
    state.recoTag = chip.dataset.tag === '__all' ? null : chip.dataset.tag;
    renderRecoTagFilter();
    renderReco();
  });

  // 推荐卡片"加入自选"按钮
  $('#recoGrid').addEventListener('click', (e) => {
    const btn = e.target.closest('.reco-add');
    if (!btn) return;
    toggleWatch(btn.dataset.code);
  });

  // 筛选结果行"加入自选"按钮（stopPropagation，避免同时触发详情弹层）
  $('#filterResult').addEventListener('click', (e) => {
    const btn = e.target.closest('.reco-add');
    if (!btn) return;
    e.stopPropagation();
    toggleWatch(btn.dataset.code);
  });

  // 信号详情：自选行 / 信号卡片 / 筛选结果行 点击 -> 打开弹层
  document.addEventListener('click', (e) => {
    const row = e.target.closest('.row, .signal-card');
    if (row && row.dataset.code) openSheet(row.dataset.code);
  });

  // 市场筛选页条件选择
  $('#filterBar').addEventListener('click', (e) => {
    const opt = e.target.closest('.filter-opt');
    if (!opt) return;
    state.filter[opt.dataset.k] = opt.dataset.v;
    renderFilterBar();
    renderFilterResult();
  });

  // 弹层遮罩点击关闭
  $('#sheetMask').addEventListener('click', (e) => { if (e.target === $('#sheetMask')) closeSheet(); });

  // ESC 键关闭弹层（标准弹层 UX，作为可靠关闭方式）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#sheetMask').hidden) closeSheet();
  });
}

/* =======================================================================
 * 启动
 * ===================================================================== */
function init() {
  renderOverview();
  renderWatch();
  renderSignal();
  renderRecoTagFilter();
  renderReco();
  renderFilterBar();
  renderFilterResult();
  bindEvents();
}
document.addEventListener('DOMContentLoaded', init);
