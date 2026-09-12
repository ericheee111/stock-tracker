/* Manual holding plans and T reservations. Private API only; no order endpoints. */
(function (global) {
  'use strict';
  const F = global.Fmt;
  const esc = F.esc;
  const PURPOSES = { SWING: '波段 · 数周至数月', LONG_TERM: '长持核心仓', SHORT_TERM: '独立短线' };
  const STATUS = { RESERVED: '仅计划预留', RECONCILIATION_REQUIRED: '实际操作待对账', CANCELLED: '未执行取消', CLOSED_MANUAL_RECONCILIATION: '人工对账关闭（非成交验证）' };
  const ACTIVE = ['RESERVED', 'RECONCILIATION_REQUIRED'];
  let snapshot = null;
  let busy = false;
  let pending = null;
  let previewCommand = null;
  let requestedAt = 0;
  let requestedMono = 0;
  let requestDuration = 0;
  let generation = 0;
  let snapshotScope = '';
  let snapshotAccess = '';

  function runtimeContext() {
    try {
      const r = global.Runtime;
      const state = r.snapshot();
      return {scope: JSON.stringify([state.apiOrigin, state.health && state.health.engine_id]),
        access: r.privateAccessValue(), ready: state.handshakeReady === true &&
          !state.authState && !r.isHardFailure(state.status)};
    } catch (_) { return {scope:'', access:'', ready:false}; }
  }
  let lastContext = runtimeContext();
  function stamp() { return Object.assign({generation:generation}, runtimeContext()); }
  function current(ticket) {
    const ctx = runtimeContext();
    return ticket.generation === generation && ticket.scope === ctx.scope && ticket.access === ctx.access && ctx.ready;
  }
  function inSession() {
    const ctx = runtimeContext();
    return Boolean(snapshot && ctx.ready && ctx.scope === snapshotScope && ctx.access === snapshotAccess);
  }
  function clearPreview() {
    previewCommand = null;
    const element = document.getElementById('planningPreview');
    if (element) element.replaceChildren();
  }
  function lockedView() {
    return '<div id="planningMessage" role="alert" class="mp-message mp-error"></div>' +
      '<button data-mp="refresh" type="button">重新加载</button>' +
      '<button data-mp="retry" type="button" hidden>重试未确认请求（原ID）</button>';
  }
  function invalidatePrivate() {
    generation++;
    snapshot = null; snapshotScope = ''; snapshotAccess = ''; clearPreview();
    if (root()) { root().innerHTML = lockedView(); message((global.Runtime && global.Runtime.snapshot().authState ? '认证失败或需重新认证，私有展示已清除。' : '连接或认证已变化，私有展示已清除。') +
      (pending ? '原请求结果仍未确认；恢复原引擎和计划库后，刷新并复用原ID重试。' : '请重新确认连接后加载。'), true); }
    freezeControls();
  }
  function runtimeChanged() {
    const next = runtimeContext();
    const changed = next.scope !== lastContext.scope || next.access !== lastContext.access;
    const lost = !next.ready && (lastContext.ready || snapshot !== null);
    lastContext = next;
    if (changed || lost) invalidatePrivate();
  }
  function estimatedTime(conservative) {
    const origin = book() && Date.parse(book().as_of);
    if (!Number.isFinite(origin)) return NaN;
    const mono = Math.max(0, global.performance.now() - requestedMono);
    const elapsed = conservative ? Math.max(mono, Date.now() - requestedAt, 0) + requestDuration : mono;
    return origin + elapsed;
  }
  function isFresh(item) {
    const now = estimatedTime(true);
    return Boolean(item && Number.isFinite(now) && Date.parse(item.observed_at) <= now && now < Date.parse(item.expires_at));
  }
  function previewInputsFresh(pid) {
    const b = book(); const inv = b && b.inventory[pid];
    return Boolean(inv && inv.parent_matches && isFresh(inv) && isFresh(b.cash[inv.currency]));
  }
  function refreshTemporalState() {
    runtimeChanged();
    if (!inSession() || !book() || !root()) return;
    const b = book();
    root().querySelectorAll('[data-inventory-fresh]').forEach(function(el) {
      const inv = b.inventory[el.dataset.inventoryFresh];
      el.textContent = inv && inv.parent_matches && isFresh(inv) ? '人工确认有效' : '未确认或已过期';
    });
    root().querySelectorAll('[data-cash-fresh]').forEach(function(el) {
      el.textContent = isFresh(b.cash[el.dataset.cashFresh]) ? '确认有效' : '已过期';
    });
    root().querySelectorAll('[data-plan-expiry]').forEach(function(el) {
      const plan = b.plans[el.dataset.planExpiry];
      el.hidden = !plan || !ACTIVE.includes(plan.status) || estimatedTime(true) < Date.parse(plan.valid_until);
    });
    if (previewCommand && (!previewInputsFresh(previewCommand.data.position_id) ||
        estimatedTime(true) >= Date.parse(previewCommand.data.valid_until))) {
      clearPreview(); message('预演条件已过期；原预留不自动释放，请重新核对库存和现金。', true);
    }
    freezeControls();
  }

  function root() { return document.getElementById('planningWorkspace'); }
  function uid() {
    if (!global.crypto || !global.crypto.randomUUID) throw new Error('浏览器缺少安全随机ID能力');
    return global.crypto.randomUUID();
  }
  function input(label, name, value, type, extra) {
    return '<label class="mp-field"><span>' + esc(label) + '</span><input name="' + name + '" type="' + (type || 'text') + '" value="' + esc(value == null ? '' : String(value)) + '" ' + (extra || '') + '></label>';
  }
  function select(label, name, pairs, current) {
    return '<label class="mp-field"><span>' + esc(label) + '</span><select name="' + name + '">' + pairs.map(function (p) {
      return '<option value="' + esc(p[0]) + '"' + (p[0] === current ? ' selected' : '') + '>' + esc(p[1]) + '</option>';
    }).join('') + '</select></label>';
  }
  function acknowledgement(name, label) {
    return '<label class="mp-ack"><input type="checkbox" name="' + name + '" required> <span>' + esc(label) + '</span></label>';
  }
  function numberText(value) {
    return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—';
  }
  function book() { return snapshot && snapshot.book; }
  function currentPosition(pid) {
    const p = snapshot && snapshot.positions.find(function (x) { return x.position_id === pid; });
    if (!p) throw new Error('持仓不存在，请刷新');
    return p;
  }
  function anchor(pid) {
    const p = currentPosition(pid);
    return { position_id: p.position_id, parent_hash: p.parent_hash };
  }
  function message(value, error) {
    const element = document.getElementById('planningMessage');
    if (element) { element.textContent = value; element.className = 'mp-message' + (error ? ' mp-error' : ''); }
  }
  function timeWindow() {
    // Server observation plus elapsed local duration; final authority is server validation.
    const now = estimatedTime(false);
    if (!Number.isFinite(now)) throw new Error('缺少服务器时间，请重新加载');
    return { observed_at: new Date(now - 1000).toISOString(), expires_at: new Date(now + 9 * 60000).toISOString() };
  }
  function quantity(form, name) {
    const raw = form.elements[name].value.trim();
    if (!/^(0|[1-9][0-9]{0,9})$/.test(raw)) throw new Error('数量必须是非负整数：' + name);
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value > 1000000000) throw new Error('数量超出范围');
    return value;
  }
  function value(form, name) { return form.elements[name].value.trim(); }
  function command(kind, data) {
    if (!book()) throw new Error('计划库尚未启用');
    return { store_id: snapshot.store_id, command_id: uid(), expected_revision: book().revision, kind: kind, data: data };
  }
  function actionable() { return !busy && !pending && inSession() && snapshot.enabled === true; }
  function freezeControls() {
    if (!root()) return;
    root().querySelectorAll('form button, [data-plan-action], [data-mp="reserve"]').forEach(function (button) {
      const scenario = button.closest('form[data-form="scenario"]');
      button.disabled = scenario ? Boolean(busy || pending || !inSession()) : !actionable();
      if (button.dataset.mp === 'reserve') button.disabled = button.disabled || !previewCommand;
      const form = button.closest('form[data-form="t-preview"]');
      if (form && !previewInputsFresh(form.dataset.position)) button.disabled = true;
    });
    root().querySelectorAll('form input, form select').forEach(function (control) {
      control.disabled = Boolean(busy || pending);
    });
    const retry = root().querySelector('[data-mp="retry"]');
    if (retry) {
      retry.hidden = !pending;
      retry.disabled = Boolean(busy || !pending || !inSession() || !book() ||
        pending.scope !== snapshotScope || pending.command.store_id !== snapshot.store_id);
    }
  }

  function allocationForm(p, allocation) {
    const sleeves = allocation && allocation.parent_matches ? allocation.sleeves : [];
    let html = '<form data-form="allocation" data-position="' + esc(p.position_id) + '"><div class="mp-sleeves">';
    Object.keys(PURPOSES).forEach(function (purpose) {
      const s = sleeves.find(function (x) { return x.purpose === purpose; }) || {};
      html += '<fieldset><legend>' + esc(PURPOSES[purpose]) + '</legend><div class="mp-fields">' +
        input('分配股数（0为不分配）', purpose + '_quantity', s.quantity == null ? 0 : s.quantity, 'number', 'min="0" step="1" required') +
        input('其中核心保留股数', purpose + '_core', s.core_quantity == null ? 0 : s.core_quantity, 'number', 'min="0" step="1" required') +
        input('持有依据', purpose + '_thesis', s.thesis || '', 'text', 'maxlength="1000"') +
        input('失效/复核条件（不是自动止损）', purpose + '_invalidation', s.invalidation || '', 'text', 'maxlength="1000"') +
        input('下次复核日期（可选，不强制清仓）', purpose + '_review', s.review_at ? s.review_at.slice(0,10) : '', 'date') +
        '</div></fieldset>';
    });
    return html + '</div><button type="submit" class="mp-primary">保存持仓分层</button></form>';
  }

  function inventoryForm(p, inv) {
    const currency = { A: 'CNY', HK: 'HKD', US: 'USD' }[p.market];
    const matching = inv && inv.parent_matches;
    return '<form data-form="inventory" data-position="' + esc(p.position_id) + '"><div class="mp-fields">' +
      input('可卖旧仓总股数（未扣外部委托）', 'sellable', matching ? inv.sellable_gross : '', 'number', 'min="0" step="1" required') +
      input('外部未成交卖单占用股数', 'external', '', 'number', 'min="0" step="1" required') +
      input('该证券交易单位（股）', 'lot', matching ? inv.lot_size : '', 'number', 'min="1" step="1" required') +
      input('容许的中途最高总股数', 'maximum', matching ? inv.maximum_position_quantity : p.shares, 'number', 'min="1" step="1" required') +
      input('当次证券规则/库存核对说明', 'rule_note', '', 'text', 'maxlength="400" required') +
      '</div><p class="mp-note">币种 ' + esc(currency) + '。只支持本版普通整股股票范围；不把当日新买量算作可卖旧仓。</p>' +
      acknowledgement('checked', '我刚核对了当前可卖旧量、外部委托占用和该证券交易单位；这是人工未验证快照') +
      '<button type="submit">确认库存（有效9分钟）</button></form>';
  }

  function tForm(p) {
    return '<form data-form="t-preview" data-position="' + esc(p.position_id) + '"><div class="mp-fields">' +
      select('父计划用途', 'purpose', Object.entries(PURPOSES), 'SWING') +
      select('操作方向', 'direction', [['SELL_THEN_BUY','先卖旧仓，再考虑买回'],['BUY_THEN_SELL','先买，再卖可卖旧仓']], 'SELL_THEN_BUY') +
      input('计划数量（股）', 'quantity', '', 'number', 'min="1" step="1" required') +
      input('人工设定买入限价', 'buy', '', 'text', 'inputmode="decimal" required') +
      input('人工设定卖出限价', 'sell', '', 'text', 'inputmode="decimal" required') +
      input('本轮全部费用/滑点预留', 'fees', '', 'text', 'inputmode="decimal" required') +
      '</div>' + acknowledgement('checked', '我确认这是人工条件预演，不是系统买卖信号或成交授权；两腿未完成可能增加风险') +
      '<button type="submit">检查条件，不预留</button></form>';
  }

  function positionCard(p, b) {
    const a = b && b.allocations[p.position_id];
    const inv = b && b.inventory[p.position_id];
    const valid = a && a.parent_matches;
    const allocated = valid ? a.sleeves.reduce(function (n,s) { return n+s.quantity; },0) : 0;
    return '<article class="mp-position" data-position-card="' + esc(p.position_id) + '"><h3>' + esc(p.symbol) + ' · ' + esc(p.market) + '</h3>' +
      '<p>已录入总仓 ' + esc(numberText(p.shares)) + ' 股 · 未分类 ' + esc(numberText(p.shares - allocated)) + ' 股</p>' +
      ((a && !a.parent_matches) || (inv && !inv.parent_matches) ? '<p class="mp-error">原持仓已变化：旧分配或库存必须重新核对。</p>' : '') +
      (valid ? '<div class="mp-purpose-tags">' + a.sleeves.map(function (s) { return '<span>' + esc(PURPOSES[s.purpose]) + ' ' + esc(s.quantity) + '股 / 核心 ' + esc(s.core_quantity) + '</span>'; }).join('') + '</div>' : '<p class="mp-note">尚未分类；不会根据盈亏推断长持或短线目的。</p>') +
      (b ? '<details><summary>持仓目的与原计划</summary>' + allocationForm(p,a) + '</details>' +
      '<details><summary>可卖库存 · <span data-inventory-fresh="' + esc(p.position_id) + '">' + (inv && inv.fresh && inv.parent_matches ? '人工确认有效' : '未确认或已过期') + '</span></summary>' +
      (inv ? '<p class="mp-note">人工快照：' + esc(inv.observed_at) + ' 至 ' + esc(inv.expires_at) + '</p>' : '') + inventoryForm(p,inv) + '</details>' +
      '<details><summary>做 T 条件预演</summary>' + tForm(p) + '</details>' : '') + '</article>';
  }

  function cashPanel(b) {
    return '<details class="mp-block"><summary>币种现金确认与对账</summary><p class="mp-note">只填单一本地资金池；已扣外部委托占用，尚未扣本工具预留。不要把多个币种或券商账户相加。</p>' +
      Object.values(b.cash).map(function (c) { return '<p>' + esc(c.currency) + ' · 人工现金 ' + esc(c.available_cash) + ' · <span data-cash-fresh="' + esc(c.currency) + '">' + (c.fresh ? '确认有效' : '已过期') + '</span></p><p class="mp-note">快照：' + esc(c.observed_at) + ' 至 ' + esc(c.expires_at) + '</p>'; }).join('') +
      '<form data-form="cash"><div class="mp-fields">' + select('币种','currency',[['CNY','CNY'],['HKD','HKD'],['USD','USD']],'CNY') +
      input('当前可用现金','cash','','text','inputmode="decimal" required') + '</div>' + acknowledgement('checked','我刚核对了对应币种可用现金；未混用其他资金池') +
      '<button type="submit">确认现金（有效9分钟）</button></form><hr>' +
      '<form data-form="reconcile"><div class="mp-fields">' + select('对账币种','currency',[['CNY','CNY'],['HKD','HKD'],['USD','USD']],'CNY') +
      input('对账原因/记录','reason','','text','maxlength="1000" required') + '</div>' +
      acknowledgement('orders','对应币种的外部委托已清空') + acknowledgement('accounting','已在持仓管理核对实际数量、成本与现金，处理该币种全部未关闭计划') +
      '<button type="submit">对账关闭并清空旧库存/现金确认</button></form></details>';
  }

  function plansPanel(b) {
    const plans = Object.values(b.plans).sort(function (a,c) { return c.created_at.localeCompare(a.created_at); });
    return '<section class="mp-block"><h3>做 T 计划与未完成操作</h3>' + (plans.length ? plans.map(function (p) {
      const active = ACTIVE.includes(p.status);
      return '<article class="mp-plan"><strong>' + esc(p.parent.symbol) + ' · ' + esc(STATUS[p.status] || '未知状态') + '</strong>' +
        '<p>' + esc(PURPOSES[p.purpose]) + ' / ' + (p.direction === 'BUY_THEN_SELL' ? '先买后卖旧仓' : '先卖旧仓后买回') + ' · ' + esc(p.quantity) + '股</p>' +
        '<p>预留现金 ' + esc(p.reserved_cash) + ' ' + esc(p.currency) + ' · 计划价差测算（非收益预测） ' + esc(p.scenario_net_spread) + '</p>' +
        '<p class="mp-error" data-plan-expiry="' + esc(p.plan_id) + '"' + (p.expired && active ? '' : ' hidden') + '>条件已过期，额度仍保留。先确认外部操作，再取消或对账。</p>' +
        (!p.parent_matches && active ? '<p class="mp-error">原持仓变化或已关闭，必须对账。</p>' : '') +
        (p.status === 'RESERVED' ? '<button type="button" data-plan-action="CANCEL" data-plan="' + esc(p.plan_id) + '">确认未执行并取消</button> <button type="button" data-plan-action="MARK_EXECUTED" data-plan="' + esc(p.plan_id) + '">标记已转实际操作</button>' : '') +
        '<p class="mp-note">' + esc(p.plan_id) + ' · 不创建订单，不证明实际成交。</p></article>';
    }).join('') : '<p class="mp-note">暂无 T 计划。不做 T 是正常状态。</p>') + '</section>';
  }

  function scenarioPanel() {
    return '<details class="mp-block"><summary>相对继续持有的情景测算（不是实际绩效）</summary>' +
      '<p class="mp-note">假定同证券、同币种、同起点和估值时点，无外部资金流/公司行为。价格和费用由你填写；未配对腿也计入净值差。</p>' +
      '<form data-form="scenario"><div class="mp-fields">' +
      select('币种','currency',[['CNY','CNY'],['HKD','HKD'],['USD','USD']],'CNY') +
      input('起始持仓','starting_quantity','','number','min="0" step="1" required') +
      input('起始可卖旧量','sellable_old_quantity','','number','min="0" step="1" required') +
      input('买入数量（没有填0）','buy_quantity','0','number','min="0" step="1" required') +
      input('卖出数量（没有填0）','sell_quantity','0','number','min="0" step="1" required') +
      input('买入均价（零数量留空）','average_buy','','text','inputmode="decimal"') +
      input('卖出均价（零数量留空）','average_sell','','text','inputmode="decimal"') +
      input('全部费用','fees','','text','inputmode="decimal" required') +
      input('统一估值价格','mark_price','','text','inputmode="decimal" required') +
      '</div><button type="submit">计算手工情景</button></form><div id="planningScenario" role="status"></div></details>';
  }

  function render(data) {
    const b = data && data.enabled === true ? data.book : null;
    const positions = data && Array.isArray(data.positions) ? data.positions : [];
    let html = '<div class="mp-top"><h2>持仓计划与做 T</h2><button type="button" data-mp="refresh">刷新</button></div>' +
      '<p class="mp-note">几周至数月波段为主；长持、短线与做 T 分层。此处只管理人工计划，不改变原信号、真实持仓或券商委托。</p>' +
      '<div id="planningMessage" class="mp-message" role="status" aria-live="polite"></div>' +
      '<button type="button" data-mp="retry" hidden>重试未确认请求（复用原命令ID）</button>';
    if (!b) {
      html += '<div class="mp-disabled"><h3>手工计划库未启用</h3><p>保留原持仓及机会功能。请先在本地显式创建独立计划库，配置路径和 Store ID 后重启引擎。</p>' +
        '<code>python -m stock_tracker.portfolio_planning init --database &lt;绝对路径&gt;</code><p>配置 STOCK_TRACKER_PLANNING_DB 与 STOCK_TRACKER_PLANNING_STORE_ID。不要使用 stock_tracker.db；浏览器不会创建数据库。</p></div>';
    } else {
      html += '<p class="mp-note">人工未验证 · 计划版本 ' + esc(b.revision) + ' · 没有执行授权</p>' + cashPanel(b);
    }
    html += '<div class="mp-positions">' + (positions.length ? positions.map(function (p) { return positionCard(p,b); }).join('') : '<p>暂无持仓，请先使用上方持仓管理录入。</p>') + '</div>';
    if (Array.isArray(data && data.position_issues) && data.position_issues.length) {
      html += '<div class="mp-error">以下旧持仓需单独核对，本工具未替其推断数量或用途：' + data.position_issues.map(function(item) {
        return esc(item.symbol) + '（' + esc(item.code) + '）';
      }).join('、') + '</div>';
    }
    if (b) html += '<div id="planningPreview" role="status"></div>' + plansPanel(b);
    return html + scenarioPanel();
  }

  async function load() {
    runtimeChanged();
    if (!root() || busy) return false;
    if (!runtimeContext().ready) { invalidatePrivate(); return false; }
    const ticket = stamp(); const started = performance.now();
    busy = true; clearPreview(); freezeControls();
    try {
      const value = await global.API.getPlanningBook();
      if (!current(ticket)) return false;
      if (!value || value.schema !== 'manual-planning-api-v1' || typeof value.enabled !== 'boolean' || !Array.isArray(value.positions) ||
          (value.enabled && (!value.book || value.book.schema !== 'manual-plan-book-v1' || !Number.isSafeInteger(value.book.revision) ||
          !Number.isFinite(Date.parse(value.book.as_of)) || ['allocations','inventory','cash','plans'].some(function(k) {
            return !value.book[k] || typeof value.book[k] !== 'object' || Array.isArray(value.book[k]);
          })))) throw new Error('计划接口版本或数据无效，已暂停编辑');
      snapshot = value; snapshotScope = ticket.scope; snapshotAccess = ticket.access;
      requestedAt = Date.now(); requestedMono = performance.now(); requestDuration = requestedMono - started;
      root().innerHTML = render(value);
      refreshTemporalState();
      if (pending) message('上一请求结果仍未确认；只可在原引擎/计划库复用原ID重试，勿另建相同操作。', true);
      return true;
    } catch (error) {
      if (!current(ticket)) return false;
      snapshot = null; clearPreview();
      root().innerHTML = lockedView();
      message((pending ? '原请求结果仍未确认。' : '') + (error.message || '无法读取计划；请检查认证与连接'), true);
      return false;
    } finally { busy = false; freezeControls(); }
  }

  async function commit(cmd) {
    runtimeChanged();
    if (busy || !inSession() || (pending && pending.command.command_id !== cmd.command_id)) return;
    if (cmd.store_id !== snapshot.store_id || (pending && pending.scope !== snapshotScope)) {
      message('未确认请求属于另一引擎或计划库，禁止转发。请先返回原连接核对。', true); return;
    }
    const ticket = stamp();
    const wasUncertain = Boolean(pending && pending.uncertain);
    pending = pending || {command:JSON.parse(JSON.stringify(cmd)), scope:ticket.scope, uncertain:false};
    const attempt = pending;
    attempt.uncertain = true;
    busy = true; clearPreview(); freezeControls();
    try {
      const result = await global.API.planningCommand(attempt.command);
      if (!current(ticket)) return;
      if (!result || result.schema !== 'manual-planning-command-response-v1' || result.store_id !== cmd.store_id || !result.book) {
        throw new Error('写入回执身份无效，保持未确认状态');
      }
      pending = null; busy = false;
      if (await load()) message('已保存到本地手工计划日志；没有发送任何交易指令。');
    } catch (error) {
      if (!current(ticket)) return;
      // A later rejection cannot establish that a previous lost response did not commit.
      const definiteInitialRejection = !wasUncertain && [400,409,413].includes(error.status) &&
        typeof error.code === 'string' && !error.code.startsWith('HTTP_');
      if (definiteInitialRejection) pending = null;
      message((pending ? '写入结果仍未确认，请恢复原连接并复用原ID重试。' : '未保存：') + (error.message || '请求失败'), true);
    } finally { busy = false; freezeControls(); }
  }

  async function submit(form) {
    runtimeChanged();
    if (busy || pending || !inSession()) return;
    const type = form.dataset.form;
    if (type === 't-preview') clearPreview();
    const pid = form.dataset.position;
    let data;
    if (type === 'scenario') {
      data = {currency:value(form,'currency')};
      ['starting_quantity','sellable_old_quantity','buy_quantity','sell_quantity'].forEach(function (k) { data[k]=quantity(form,k); });
      ['fees','mark_price'].forEach(function (k) { data[k]=value(form,k); });
      data.average_buy=data.buy_quantity ? value(form,'average_buy') : null;
      data.average_sell=data.sell_quantity ? value(form,'average_sell') : null;
      const ticket=stamp();
      busy=true;freezeControls();
      try {
        const result=await global.API.planningAttribution(data);
        if (!current(ticket)) return;
        document.getElementById('planningScenario').innerHTML='<div class="mp-result"><strong>相对继续持有的净值差：' + esc(result.relative_hold_delta) + ' ' + esc(result.currency) + '</strong><p>现金变化 ' + esc(result.cash_delta) + ' · 持仓变化 ' + esc(result.quantity_delta) + '股 · 未配对 ' + esc(result.unpaired_quantity) + '股</p><p>仅手工情景，不进入真实收益或胜率。</p></div>';
      } finally {busy=false;freezeControls();}
      return;
    }
    if (!actionable()) throw new Error('计划库未就绪');
    if (type === 'allocation') {
      data=Object.assign(anchor(pid),{sleeves:[]});
      Object.keys(PURPOSES).forEach(function (purpose) {
        const q=quantity(form,purpose+'_quantity'); const core=quantity(form,purpose+'_core');
        if (!q) {if(core) throw new Error('未分配用途不能保留核心数量');return;}
        const day=value(form,purpose+'_review');
        data.sleeves.push({purpose:purpose,quantity:q,core_quantity:core,thesis:value(form,purpose+'_thesis'),invalidation:value(form,purpose+'_invalidation'),review_at:day?day+'T00:00:00+00:00':null});
      });
      await commit(command('SET_ALLOCATION',data));
    } else if (type === 'inventory') {
      data=Object.assign(anchor(pid),timeWindow(),{sellable_gross:quantity(form,'sellable'),external_reserved_sell:quantity(form,'external'),lot_size:quantity(form,'lot'),maximum_position_quantity:quantity(form,'maximum'),currency:{A:'CNY',HK:'HKD',US:'USD'}[currentPosition(pid).market],rule_note:value(form,'rule_note')});
      await commit(command('CONFIRM_INVENTORY',data));
    } else if (type === 'cash') {
      await commit(command('CONFIRM_CASH',Object.assign(timeWindow(),{currency:value(form,'currency'),available_cash:value(form,'cash')})));
    } else if (type === 't-preview') {
      const inv=book().inventory[pid];const cash=inv && book().cash[inv.currency];
      if (!inv || !cash || !previewInputsFresh(pid)) throw new Error('先重新确认该持仓库存和对应币种现金');
      data=Object.assign(anchor(pid),{plan_id:uid(),purpose:value(form,'purpose'),direction:value(form,'direction'),quantity:quantity(form,'quantity'),buy_limit:value(form,'buy'),sell_limit:value(form,'sell'),fee_buffer:value(form,'fees'),valid_until:new Date(Math.min(Date.parse(inv.expires_at),Date.parse(cash.expires_at))).toISOString(),manual_conditions_acknowledged:form.elements.checked.checked===true});
      const cmd=command('RESERVE',data);const ticket=stamp();
      busy=true;freezeControls();
      try {
        const result=await global.API.planningPreview(cmd);
        if (!current(ticket)) return;
        const resultPlan = result && result.preview;
        if (!resultPlan || result.schema !== 'manual-t-preview-v1' || result.revision !== cmd.expected_revision ||
            !resultPlan.parent || resultPlan.parent.symbol !== currentPosition(pid).symbol ||
            Object.keys(cmd.data).some(function(k) {return resultPlan[k] !== cmd.data[k];})) {
          throw new Error('预演回执与所选持仓/输入不一致，已清除');
        }
        if (!previewInputsFresh(pid) || estimatedTime(true) >= Date.parse(cmd.data.valid_until)) throw new Error('预演条件已过期');
        previewCommand=cmd;
        document.getElementById('planningPreview').innerHTML='<div class="mp-result"><h3>人工条件预演 · 尚未预留</h3><p><strong>' + esc(resultPlan.parent.symbol) + ' · ' + esc(PURPOSES[cmd.data.purpose]) + ' · ' + (cmd.data.direction === 'BUY_THEN_SELL' ? '先买后卖旧仓' : '先卖旧仓后买回') + '</strong></p><p>买入限价 ' + esc(cmd.data.buy_limit) + ' / 卖出限价 ' + esc(cmd.data.sell_limit) + ' · 股数 ' + esc(resultPlan.quantity) + ' · 中途峰值 ' + esc(resultPlan.peak_quantity) + '股 · 预留现金 ' + esc(resultPlan.reserved_cash) + ' ' + esc(resultPlan.currency) + '</p><p>按填写的目标价差扣费用：' + esc(resultPlan.scenario_net_spread) + '。不是市场预测，尚未验证行情、实际交易规则或成交。</p><button type="button" data-mp="reserve">确认仅在本工具预留额度</button></div>';
      } catch (error) { clearPreview(); if (current(ticket)) throw error; }
      finally {busy=false;freezeControls();}
    } else if(type==='reconcile') {
      const currency=value(form,'currency');
      const ids=Object.values(book().plans).filter(function (p) {return p.currency===currency && ACTIVE.includes(p.status);}).map(function(p){return p.plan_id;});
      if(!ids.length) throw new Error('该币种没有未关闭计划');
      if(!global.confirm('将关闭该币种全部 '+ids.length+' 个计划，并清空旧库存/现金确认。不会更改真实持仓或委托。继续？')) return;
      await commit(command('RECONCILE',{currency:currency,plan_ids:ids,no_open_orders_confirmed:form.elements.orders.checked===true,accounting_checked:form.elements.accounting.checked===true,reason:value(form,'reason')}));
    }
  }

  function attach() {
    const panel=document.getElementById('planningPanel');const element=root();
    if(!panel || !element) return;
    panel.addEventListener('toggle',function(){if(panel.open && !snapshot) load();});
    element.addEventListener('submit',function(event){const form=event.target;if(!form.matches('form[data-form]'))return;event.preventDefault();submit(form).catch(function(error){message(error.message || '输入无效',true);});});
    function edited(event) { if (event.target.closest('form[data-form="t-preview"]')) clearPreview(); }
    element.addEventListener('input',edited);
    element.addEventListener('change',edited);
    if (global.Runtime && global.Runtime.onChange) global.Runtime.onChange(runtimeChanged);
    global.setInterval(refreshTemporalState,1000);
    document.addEventListener('visibilitychange',refreshTemporalState);
    global.addEventListener('pageshow',refreshTemporalState);
    element.addEventListener('click',function(event){
      const button=event.target.closest('button');if(!button)return;
      const operation=button.dataset.mp;
      if(operation==='refresh'){load();return;}
      if(operation==='retry' && pending){commit(pending.command);return;}
      if(operation==='reserve'){
        refreshTemporalState();
        if(previewCommand && actionable()) commit(previewCommand);
        return;
      }
      const action=button.dataset.planAction;
      if(!action || !actionable())return;
      const prompt=action==='CANCEL'?'确认这个计划完全没有执行，且不存在相关未成交委托？':'确认已转到实际操作？工具不会报单或记入成交，额度将保留至人工对账。';
      if(!global.confirm(prompt))return;
      const data={plan_id:button.dataset.plan,reason:action==='CANCEL'?'用户确认未执行且无外部委托':'用户标记转实际操作，待人工对账'};
      if(action==='CANCEL'){data.no_execution_confirmed=true;data.no_open_orders_confirmed=true;}
      commit(command(action,data));
    });
  }
  global.PlanningUI={render:render,load:load};
  attach();
})(window);
