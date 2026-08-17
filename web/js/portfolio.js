/* =========================================================================
 * portfolio.js —— Stage 1.1 账户资料与持仓编辑 UI
 * 只负责渲染、表单读取和客户端基础校验；所有持久化与最终合同校验由后端完成。
 * 全局对象：PortfolioUI
 * ========================================================================= */
(function (global) {
  'use strict';

  const F = global.Fmt;
  const esc = F.esc;
  const RISK_LABELS = {
    CONSERVATIVE: '保守',
    BALANCED: '平衡',
    AGGRESSIVE: '激进'
  };

  function finiteNumber(value) {
    if (value === '' || value === null || value === undefined || typeof value === 'boolean') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function positiveNumber(label, value) {
    const number = finiteNumber(value);
    if (number === null || number <= 0) throw new Error(label + '必须是大于 0 的有限数字');
    return number;
  }

  function nonNegativeNumber(label, value) {
    const number = finiteNumber(value);
    if (number === null || number < 0) throw new Error(label + '必须是大于等于 0 的有限数字');
    return number;
  }

  function percentFraction(label, value) {
    const percent = positiveNumber(label, value);
    if (percent > 100) throw new Error(label + '不能超过 100%');
    return percent / 100;
  }

  function positiveInteger(label, value) {
    const number = finiteNumber(value);
    if (number === null || !Number.isInteger(number) || number <= 0) {
      throw new Error(label + '必须是正整数');
    }
    return number;
  }

  function valueOf(form, name) {
    const field = form.elements.namedItem(name);
    return field ? field.value : '';
  }

  function formatMoney(value) {
    const number = finiteNumber(value);
    if (number === null) return '—';
    return '¥' + number.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  function percentInput(value, fallback) {
    const number = finiteNumber(value);
    return number === null ? fallback : Number((number * 100).toFixed(4));
  }

  function localDateTimeValue(iso) {
    const date = iso ? new Date(iso) : new Date();
    if (Number.isNaN(date.getTime())) return '';
    const pad = function (number) { return String(number).padStart(2, '0'); };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
      'T' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function privateErrorText(error) {
    if (!error) return '';
    const code = error.code || '';
    const map = {
      PRIVATE_API_DISABLED: '公网私有接口尚未在服务端启用。请先配置运行环境中的私有访问值。',
      PRIVATE_API_AUTH_REQUIRED: '当前会话缺少或使用了错误的私有访问值。',
      REQUEST_TIMEOUT: '私有接口请求超时。',
      NON_JSON_RESPONSE: '私有接口返回了非 JSON 内容。'
    };
    return map[code] || error.message || '私有账户数据暂不可用';
  }

  function accessStatus() {
    return global.API && global.API.hasPrivateAccess()
      ? '当前浏览器会话已配置私有访问值'
      : '本机直连无需访问值；公网访问需要当前会话私有访问值';
  }

  function renderPanel(portfolio, error, busy) {
    const profile = portfolio && portfolio.profile;
    const positions = portfolio && Array.isArray(portfolio.positions) ? portfolio.positions : [];
    const errorText = privateErrorText(error);
    let summary;
    if (profile) {
      summary = '<div class="pf-summary-grid">' +
        '<div><span>账户净值</span><strong>' + esc(formatMoney(profile.account_equity)) + '</strong></div>' +
        '<div><span>可用现金</span><strong>' + esc(formatMoney(profile.available_cash)) + '</strong></div>' +
        '<div><span>风险模式</span><strong>' + esc(RISK_LABELS[profile.risk_mode] || profile.risk_mode) + '</strong></div>' +
        '<div><span>当前持仓</span><strong>' + positions.length + ' 只</strong></div>' +
        '</div>';
    } else if (errorText) {
      summary = '<div class="pf-error">' + esc(errorText) + '</div>';
    } else {
      summary = '<div class="pf-empty">账户参数尚未设置。设置后系统才能按风险预算给出建议仓位和股数。</div>';
    }
    return '<section class="tb-card pf-panel" id="portfolioSummaryCard">' +
      '<div class="pf-panel-head"><div><div class="tb-card-title">账户与持仓</div>' +
      '<div class="pf-access-state">' + esc(accessStatus()) + '</div></div>' +
      '<button type="button" class="pf-primary pf-open" data-portfolio-open' + (busy ? ' disabled' : '') + '>' +
      (busy ? '处理中…' : '管理账户与持仓') + '</button></div>' + summary +
      '<div class="pf-honesty">持仓为手工录入事实；页面不会自行下单，也不会在浏览器端伪造建议股数。</div>' +
      '</section>';
  }

  function renderAccessSection(error) {
    const errorText = privateErrorText(error);
    return '<section class="pf-sheet-section">' +
      '<h3>私有访问</h3>' +
      (errorText ? '<div class="pf-error" id="privateAccessError">' + esc(errorText) + '</div>' : '') +
      '<div class="pf-help">访问值只保存在当前浏览器会话的 sessionStorage，不写入项目文件或长期本地存储。本机直连通常无需填写。</div>' +
      '<form id="privateAccessForm" class="pf-inline-form">' +
      '<label><span>当前会话访问值</span><input type="password" name="private_access" id="privateAccessInput" autocomplete="current-password" placeholder="公网私有部署时填写" /></label>' +
      '<div class="pf-actions"><button class="pf-primary" type="submit">连接私有接口</button>' +
      '<button class="pf-secondary" type="button" data-private-access-clear>清除当前会话值</button></div>' +
      '</form></section>';
  }

  function renderProfileForm(profile) {
    const p = profile || {};
    return '<section class="pf-sheet-section">' +
      '<h3>账户与风险参数</h3>' +
      '<div class="pf-help">这些参数只用于风险预算和仓位上限。百分数字段在页面按 % 显示，后端以 0–1 保存。</div>' +
      '<form id="portfolioProfileForm" class="pf-form-grid">' +
      input('账户净值', 'account_equity', p.account_equity, 'number', '100000', '0.01') +
      input('可用现金', 'available_cash', p.available_cash, 'number', '50000', '0.01') +
      '<label><span>风险模式</span><select name="risk_mode" id="portfolioRiskMode">' +
      option('CONSERVATIVE', p.risk_mode) + option('BALANCED', p.risk_mode || 'BALANCED') + option('AGGRESSIVE', p.risk_mode) +
      '</select></label>' +
      input('单笔风险 %', 'per_trade_risk_pct', percentInput(p.per_trade_risk_pct, 0.5), 'number', '0.5', '0.01') +
      input('单股最大仓位 %', 'max_position_pct', percentInput(p.max_position_pct, 20), 'number', '20', '0.1') +
      input('Portfolio Heat 上限 %', 'max_portfolio_heat_pct', percentInput(p.max_portfolio_heat_pct, 6), 'number', '6', '0.1') +
      input('板块上限 %', 'max_sector_pct', percentInput(p.max_sector_pct, 35), 'number', '35', '0.1') +
      input('主题上限 %', 'max_theme_pct', percentInput(p.max_theme_pct, 35), 'number', '35', '0.1') +
      '<div class="pf-form-wide pf-actions"><button class="pf-primary" type="submit">保存账户参数</button></div>' +
      '</form></section>';
  }

  function input(label, name, value, type, placeholder, step) {
    const normalized = value === undefined || value === null ? '' : value;
    return '<label><span>' + esc(label) + '</span><input type="' + esc(type) + '" name="' + esc(name) +
      '" id="pf-' + esc(name) + '" value="' + esc(normalized) + '" placeholder="' + esc(placeholder) +
      '"' + (step ? ' step="' + esc(step) + '"' : '') + ' /></label>';
  }

  function option(value, selected) {
    return '<option value="' + value + '"' + (value === selected ? ' selected' : '') + '>' +
      esc(RISK_LABELS[value] || value) + '</option>';
  }

  function renderNewPositionForm() {
    return '<section class="pf-sheet-section">' +
      '<h3>新增当前持仓</h3>' +
      '<div class="pf-help">录入的是实际持仓事实，允许送股、部分成交等形成的零碎股；这不代表系统发出买入订单。</div>' +
      '<form id="portfolioPositionCreateForm" class="pf-form-grid">' +
      '<label><span>市场</span><select name="market" id="newPositionMarket">' +
      '<option value="A">A 股</option><option value="HK">港股</option><option value="US">美股</option></select></label>' +
      input('股票代码', 'symbol', '', 'text', '600000.SH', '') +
      input('实际股数', 'shares', '', 'number', '1000', '1') +
      input('平均成本', 'average_cost', '', 'number', '10.50', '0.001') +
      '<label class="pf-form-wide"><span>持仓录入时间</span><input type="datetime-local" name="added_at" id="newPositionAddedAt" value="' +
      esc(localDateTimeValue()) + '" /></label>' +
      '<div class="pf-form-wide pf-actions"><button class="pf-primary" type="submit">新增持仓记录</button></div>' +
      '</form></section>';
  }

  function renderPositions(positions) {
    const items = Array.isArray(positions) ? positions : [];
    const rows = items.map(function (position) {
      const id = esc(position.id || '');
      return '<form class="pf-position-row" data-position-form data-position-id="' + id + '">' +
        '<div class="pf-position-head"><div><strong>' + esc(position.symbol || '') + '</strong>' +
        '<span>' + esc(position.market || '') + '</span></div>' +
        '<small>录入 ' + esc(position.added_at ? String(position.added_at).slice(0, 16).replace('T', ' ') : '—') + '</small></div>' +
        '<div class="pf-position-fields">' +
        '<label><span>实际股数</span><input type="number" name="shares" value="' + esc(position.shares) + '" step="1" /></label>' +
        '<label><span>平均成本</span><input type="number" name="average_cost" value="' + esc(position.average_cost) + '" step="0.001" /></label>' +
        '</div><div class="pf-actions"><button class="pf-secondary" type="submit">保存修改</button>' +
        '<button class="pf-danger" type="button" data-position-delete data-position-id="' + id + '">删除记录</button></div>' +
        '</form>';
    }).join('');
    return '<section class="pf-sheet-section"><h3>当前持仓（' + items.length + '）</h3>' +
      (rows || '<div class="pf-empty">尚未录入持仓。</div>') + '</section>';
  }

  function renderSheet(portfolio, error) {
    const profile = portfolio && portfolio.profile;
    const positions = portfolio && Array.isArray(portfolio.positions) ? portfolio.positions : [];
    return '<div class="sheet-header"><div class="sheet-title">账户与持仓管理</div>' +
      '<div class="sheet-sub">本页面只维护本地账户事实与风险约束，不连接券商、不自动下单。</div></div>' +
      '<div class="sheet-body pf-sheet-body">' + renderAccessSection(error) +
      (portfolio ? renderProfileForm(profile) + renderNewPositionForm() + renderPositions(positions) :
        '<section class="pf-sheet-section"><div class="pf-empty">连接私有接口后才能读取或修改账户数据。</div></section>') +
      '</div><div class="sheet-footer"><button class="sheet-close" id="sheetClose" type="button">关闭</button></div>';
  }

  function readProfile(form) {
    const accountEquity = positiveNumber('账户净值', valueOf(form, 'account_equity'));
    const availableCash = nonNegativeNumber('可用现金', valueOf(form, 'available_cash'));
    if (availableCash > accountEquity) throw new Error('可用现金不能超过账户净值');
    const perTrade = percentFraction('单笔风险', valueOf(form, 'per_trade_risk_pct'));
    const heat = percentFraction('Portfolio Heat 上限', valueOf(form, 'max_portfolio_heat_pct'));
    if (perTrade > heat) throw new Error('单笔风险不能超过 Portfolio Heat 上限');
    return {
      account_equity: accountEquity,
      available_cash: availableCash,
      risk_mode: valueOf(form, 'risk_mode'),
      per_trade_risk_pct: perTrade,
      max_position_pct: percentFraction('单股最大仓位', valueOf(form, 'max_position_pct')),
      max_portfolio_heat_pct: heat,
      max_sector_pct: percentFraction('板块上限', valueOf(form, 'max_sector_pct')),
      max_theme_pct: percentFraction('主题上限', valueOf(form, 'max_theme_pct'))
    };
  }

  function readNewPosition(form) {
    const rawTime = valueOf(form, 'added_at');
    const date = new Date(rawTime);
    if (!rawTime || Number.isNaN(date.getTime())) throw new Error('持仓录入时间无效');
    return {
      symbol: String(valueOf(form, 'symbol') || '').trim().toUpperCase(),
      market: valueOf(form, 'market'),
      shares: positiveInteger('实际股数', valueOf(form, 'shares')),
      average_cost: positiveNumber('平均成本', valueOf(form, 'average_cost')),
      added_at: date.toISOString()
    };
  }

  function readPositionPatch(form) {
    return {
      shares: positiveInteger('实际股数', valueOf(form, 'shares')),
      average_cost: positiveNumber('平均成本', valueOf(form, 'average_cost'))
    };
  }

  global.PortfolioUI = {
    renderPanel: renderPanel,
    renderSheet: renderSheet,
    readProfile: readProfile,
    readNewPosition: readNewPosition,
    readPositionPatch: readPositionPatch,
    privateErrorText: privateErrorText
  };
})(window);
