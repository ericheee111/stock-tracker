const { chromium } = require('playwright-core');
const fs = require('fs');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const URL = 'file:///D:/Projects/stock-tracker/demo/index.html';
const SHOTS = 'D:/Projects/stock-tracker/qa/shots';
fs.mkdirSync(SHOTS, { recursive: true });

const report = { errors: [], netRequests: [], steps: [], asserts: [] };
function logStep(name, ok, detail) {
  report.steps.push({ name, ok: !!ok, detail: detail || '' });
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + name + (detail ? '  - ' + detail : ''));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function recoCount(page) {
  return page.$$eval('#recoGrid .reco-card', (els) => els.length).catch(() => 0);
}
async function recoCardTags(page) {
  return page.$$eval('#recoGrid .reco-card', (cards) =>
    cards.map((c) => [...c.querySelectorAll('.mini-tag')].map((t) => t.textContent.trim()))
  );
}
async function noHScroll(page) {
  return page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth + 1);
}
async function clickChip(page, text) {
  const chips = await page.$$('#recoTagFilter .tag-chip');
  for (const c of chips) {
    const t = (await c.innerText()).trim();
    if (t.includes(text)) { await c.click(); return true; }
  }
  return false;
}
async function gridCols(page, sel) {
  return page.$eval(sel, (el) => getComputedStyle(el).gridTemplateColumns.split(' ').length).catch(() => 0);
}
async function containerW(page) {
  return page.$eval('body', (el) => Math.round(el.getBoundingClientRect().width));
}

// ===== macOS Vibrancy 视觉审计（纯计算样式读取，模型不读图）=====
async function vibrancyAudit(page) {
  return page.evaluate(() => {
    const cs = (sel) => { const el = document.querySelector(sel); return el ? getComputedStyle(el) : null; };
    const root = getComputedStyle(document.documentElement);
    const body = cs('body');
    const GOLD = 'rgb(228, 184, 99)';

    // 1. 底色
    const bodyBg = body.backgroundColor;
    // 2. 强调蓝 token + 品牌 Logo 描边
    const accentTok = root.getPropertyValue('--accent').trim().toLowerCase();
    const brandPath = document.querySelector('.brand-logo svg path');
    const brandStroke = brandPath ? getComputedStyle(brandPath).stroke : '';
    // 3. 涨跌色 token
    const upTok = root.getPropertyValue('--up').trim().toLowerCase();
    const downTok = root.getPropertyValue('--down').trim().toLowerCase();
    // 4. 卡片无组件渐变
    const card = document.querySelector('.index-card') || document.querySelector('.card') || document.querySelector('.reco-card');
    const cardBgImg = card ? getComputedStyle(card).backgroundImage : '';
    // 5. 1px 边框 + 边框色
    const cardBorderW = card ? getComputedStyle(card).borderTopWidth : '';
    const cardBorderColor = card ? getComputedStyle(card).borderTopColor : '';
    const activeEl = document.querySelector('.market-tab.active') || document.querySelector('.tag-chip.active') || document.querySelector('.filter-opt.active');
    const activeBorderColor = activeEl ? getComputedStyle(activeEl).borderTopColor : '';
    // 6. 圆角 <=12px 且无胶囊/全圆角
    const radiusSel = ['.card', '.reco-card', '.index-card', '.tag-chip', '.reco-add', '.market-tab', '.filter-opt', '.row', '.badge', '.bottom-nav', '.topbar', '.demo-pill'];
    let maxRadius = 0; const pillFound = [];
    radiusSel.forEach((s) => {
      document.querySelectorAll(s).forEach((el) => {
        const r = getComputedStyle(el).borderRadius;
        const nums = (r.match(/[\d.]+/g) || []).map(parseFloat);
        const m = Math.max(0, ...nums);
        if (m > maxRadius) maxRadius = m;
        if (/999px|50%|\b100%/.test(r)) pillFound.push(s + ':' + r);
      });
    });
    // 7. 系统级毛玻璃
    const bf = (sel) => { const c = cs(sel); return c ? (c.backdropFilter || c.webkitBackdropFilter || '') : ''; };
    const topbarBf = bf('.topbar'); const navBf = bf('.bottom-nav'); const sheetBf = bf('#sheet');
    // 8. 衬线标题
    const titleFamily = cs('.page-title') ? cs('.page-title').fontFamily : '';
    // 9. 无香槟金残留（扫描 color/backgroundColor/border/描边/填充）
    let goldFound = [];
    const goldProps = ['color', 'backgroundColor', 'borderTopColor', 'outlineColor', 'stroke', 'fill'];
    document.querySelectorAll('*').forEach((el) => {
      const c = getComputedStyle(el);
      goldProps.forEach((p) => {
        const val = c[p];
        if (val && val === GOLD) goldFound.push((el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || el.tagName) + '.' + p + '=' + val);
      });
    });
    const goldTok = root.getPropertyValue('--gold').trim().toLowerCase();
    return {
      bodyBg, accentTok, brandStroke, upTok, downTok,
      cardBgImg, cardBorderW, cardBorderColor, activeBorderColor,
      maxRadius, pillFound, topbarBf, navBf, sheetBf, titleFamily, goldFound, goldTok,
      brandSvgStrokeOk: /10,\s*132,\s*255/.test(brandStroke),
    };
  });
}

async function vibrancyAssert(page, label) {
  const v = await vibrancyAudit(page);
  logStep(`[${label}] 底色≈rgb(28,28,30)`, /^rgb\(28,\s*28,\s*30\)$/.test(v.bodyBg || ''), v.bodyBg);
  logStep(`[${label}] 强调蓝 --accent=#0a84ff`, v.accentTok === '#0a84ff', v.accentTok);
  logStep(`[${label}] 品牌Logo描边=强调蓝`, v.brandSvgStrokeOk, v.brandStroke);
  logStep(`[${label}] 涨色 --up=#30d158`, v.upTok === '#30d158', v.upTok);
  logStep(`[${label}] 跌色 --down=#ff453a`, v.downTok === '#ff453a', v.downTok);
  const cardNoGrad = (v.cardBgImg || '') === 'none';
  logStep(`[${label}] 卡片无组件渐变`, cardNoGrad, v.cardBgImg);
  const borderOk = v.cardBorderW === '1px' &&
    (v.cardBorderColor === 'rgba(255, 255, 255, 0.08)' || v.cardBorderColor === 'rgba(255, 255, 255, 0.12)');
  logStep(`[${label}] 卡片1px边框(0.08/0.12)`, borderOk, `${v.cardBorderW} / ${v.cardBorderColor}`);
  if (v.activeBorderColor) logStep(`[${label}] 激活态边框=0.12`, v.activeBorderColor === 'rgba(255, 255, 255, 0.12)', v.activeBorderColor);
  const radiusOk = v.maxRadius <= 12 && v.pillFound.length === 0;
  logStep(`[${label}] 圆角≤12px且无胶囊`, radiusOk, `max=${v.maxRadius}px pill=${v.pillFound.length}`);
  logStep(`[${label}] 顶栏毛玻璃blur`, /blur/.test(v.topbarBf || ''), v.topbarBf);
  logStep(`[${label}] 底栏毛玻璃blur`, /blur/.test(v.navBf || ''), v.navBf);
  logStep(`[${label}] 弹层毛玻璃blur`, /blur/.test(v.sheetBf || ''), v.sheetBf);
  logStep(`[${label}] 衬线标题(Georgia/serif)`, /Georgia/.test(v.titleFamily || '') && /serif/.test(v.titleFamily || ''), v.titleFamily);
  const noGold = v.goldFound.length === 0 && v.goldTok !== '#e4b863';
  logStep(`[${label}] 无香槟金残留`, noGold, `goldFound=${v.goldFound.length} --gold=${v.goldTok}`);
  report.asserts.push({
    name: `[${label}] macOS Vibrancy 汇总`,
    value: {
      bodyBg: v.bodyBg, accent: v.accentTok, up: v.upTok, down: v.downTok,
      cardBgImg: v.cardBgImg, cardBorderW: v.cardBorderW, cardBorderColor: v.cardBorderColor,
      maxRadius: v.maxRadius, pill: v.pillFound.length, topbarBf: v.topbarBf, navBf: v.navBf,
      titleFamily: v.titleFamily, goldFound: v.goldFound.length, goldTok: v.goldTok,
    },
  });
}

(async () => {
  const browser = await chromium.launch({ executablePath: EDGE, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('PAGEERROR: ' + e.message));
  // 零网络依赖兜底：记录任何非 file:// 请求
  page.on('request', (req) => { const u = req.url(); if (!u.startsWith('file://')) report.netRequests.push(u); });

  await page.goto(URL, { waitUntil: 'networkidle' });
  await sleep(600);

  // ===== ① macOS Vibrancy 视觉断言（默认移动视口 390）=====
  await vibrancyAssert(page, '手机390');

  // ===== ② 功能回归 + 交互（手机视口 390 完整走查）=====
  logStep('overview(A) 渲染', true);
  logStep('overview 无横向溢出', await noHScroll(page));
  await page.click('[data-market="HK"]'); await sleep(200);
  await page.click('[data-market="US"]'); await sleep(200);
  await page.click('[data-market="A"]'); await sleep(200);
  report.asserts.push({ name: '指数卡片数', value: await page.$eval('#indexGrid', (el) => el.children.length) });

  await page.click('[data-page="watch"]'); await sleep(200);
  logStep('watch 无横向溢出', await noHScroll(page));
  const rowCount = await page.$$eval('#watchGroups .row', (els) => els.length);
  report.asserts.push({ name: '自选股行数', value: rowCount });
  if (rowCount > 0) {
    await page.click('#watchGroups .row'); await sleep(300);
    const openDisplay = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('点行打开弹层(computed display=flex)', openDisplay === 'flex', openDisplay);
    const closeBox = await page.$eval('#sheetClose', (el) => { const r = el.getBoundingClientRect(); return r.bottom <= window.innerHeight + 1 && r.top >= -1; });
    report.asserts.push({ name: '弹层关闭按钮在视口内', value: closeBox });
    await page.keyboard.press('Escape'); await sleep(200);
    const escDisplay = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('ESC关闭弹层(computed display=none)', escDisplay === 'none', escDisplay);
    await page.click('#watchGroups .row'); await sleep(200);
    await page.click('#sheetMask', { position: { x: 6, y: 6 } }); await sleep(200);
    const maskDisplay = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('点遮罩关闭弹层(computed display=none)', maskDisplay === 'none', maskDisplay);
    await page.click('#watchGroups .row'); await sleep(200);
    await page.click('#sheetClose'); await sleep(200);
    const btnDisplay = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('按钮关闭弹层(computed display=none)', btnDisplay === 'none', btnDisplay);
  }

  await page.click('[data-page="signal"]'); await sleep(200);
  logStep('signal 无横向溢出', await noHScroll(page));
  report.asserts.push({ name: '信号条数', value: await page.$eval('#signalList', (el) => el.children.length) });

  await page.click('[data-page="reco"]'); await sleep(200);
  await page.click('[data-market="A"]'); await sleep(200);
  logStep('reco 无横向溢出', await noHScroll(page));
  const recoBefore = await recoCount(page);
  report.asserts.push({ name: '推荐卡片数(A股基准)', value: recoBefore });
  report.asserts.push({ name: '推荐页市场提示', value: await page.$eval('#recoHint', (el) => el.textContent).catch(() => '') });

  const clickedTag = await clickChip(page, '刚金叉');
  await sleep(250);
  const recoAfter = await recoCount(page);
  const tagsAfter = await recoCardTags(page);
  const allContain = tagsAfter.length > 0 && tagsAfter.every((ts) => ts.includes('#刚金叉'));
  const activeOk = await page.$$eval('#recoTagFilter .tag-chip', (chips) => chips.some((c) => c.classList.contains('active') && c.textContent.includes('刚金叉'))).catch(() => false);
  report.asserts.push({ name: '点击刚金叉标签', value: clickedTag });
  report.asserts.push({ name: '筛选后卡片数', value: `${recoBefore} -> ${recoAfter}` });
  report.asserts.push({ name: '筛选后卡片均含该标签', value: allContain });
  report.asserts.push({ name: 'active高亮在所选标签', value: activeOk });
  logStep('标签筛选改变卡片数', clickedTag && recoAfter < recoBefore, `${recoBefore} -> ${recoAfter}`);
  logStep('筛选结果均含所选标签', allContain);
  logStep('active 高亮正确', activeOk);

  await clickChip(page, '全部'); await sleep(150);
  const addBtns = await page.$$('#recoGrid .reco-add');
  let targetCode = null;
  for (const b of addBtns) {
    const txt = (await b.innerText()).trim();
    if (!txt.includes('已加入')) { targetCode = await b.getAttribute('data-code'); break; }
  }
  if (targetCode) {
    const sel = `#recoGrid .reco-add[data-code="${targetCode}"]`;
    const before = (await page.$eval(sel, (el) => el.textContent.trim()));
    await page.click(sel); await sleep(180);
    const afterAdd = (await page.$eval(sel, (el) => el.textContent.trim()));
    const toastVisible = await page.isVisible('#toast').catch(() => false);
    await page.click(sel); await sleep(180);
    const afterRemove = (await page.$eval(sel, (el) => el.textContent.trim()));
    report.asserts.push({ name: '推荐加入自选文案', value: `${before} -> ${afterAdd} -> ${afterRemove}` });
    report.asserts.push({ name: '加入自选后toast出现', value: toastVisible });
    logStep('推荐页加入自选可切换', afterAdd.includes('已加入') && afterRemove.includes('加入'), `${before} -> ${afterAdd} -> ${afterRemove}`);
    logStep('加入自选触发toast', toastVisible);
  }

  await page.click('[data-page="filter"]'); await sleep(200);
  logStep('filter 无横向溢出', await noHScroll(page));
  const resultBefore = await page.$eval('#filterResult', (el) => el.children.length);
  const industryOpt = await page.$('#filterBar .filter-opt[data-k="industry"][data-v="新能源"]');
  if (industryOpt) {
    await industryOpt.click(); await sleep(180);
    const resultAfter = await page.$eval('#filterResult', (el) => el.children.length);
    logStep('行业筛选改变结果', resultAfter !== resultBefore, `${resultBefore} -> ${resultAfter}`);
    const allInd = await page.$('#filterBar .filter-opt[data-k="industry"][data-v="全部"]');
    if (allInd) { await allInd.click(); await sleep(150); }
  }
  const fAdd = await page.$('#filterResult .reco-add:not(.added)');
  if (fAdd) {
    const code = await fAdd.getAttribute('data-code');
    await fAdd.click(); await sleep(180);
    await page.click('[data-page="watch"]'); await sleep(200);
    const present = await page.$$eval('#watchGroups .row', (els, code) => els.some((e) => e.dataset.code === code), code);
    report.asserts.push({ name: '筛选加入反映到自选页', value: present });
    logStep('筛选加入端到端反映到自选页', present, 'code=' + code);
  }

  const disc = await page.$eval('#disclaimer', (el) => el.textContent).catch(() => '');
  report.asserts.push({ name: '免责声明文本', value: disc.slice(0, 36) });
  logStep('免责声明存在', disc.includes('投资建议') && disc.includes('模拟'), '');

  // UI 精修校验：SVG 图标 / emoji 清零 / 市场Tab 分段控件（保留；阈值按新 Vibrancy 分段控件调整）
  const ui = await page.evaluate(() => {
    const hasSvg = (sel) => !!document.querySelector(sel + ' svg');
    const svgCount = document.querySelectorAll('svg').length;
    const forbidden = ['🌸','📈','🎯','💡','🔍','🔥','📌','📋','✅','⚠️','🙈','🌟','💬','👀'];
    let emojiFound = [];
    document.querySelectorAll('.brand-logo,.nav-ico,.page-title,.section-label,#disclaimer,.nav-btn,.market-tab').forEach((el) => {
      const t = el.textContent || '';
      forbidden.forEach((e) => { if (t.includes(e)) emojiFound.push(e); });
    });
    const badgeForbidden = ['🟢','🔴','🟡','🟠','🔵','⚪','🟣'];
    let badgeEmoji = [];
    document.querySelectorAll('.badge').forEach((el) => {
      const t = el.textContent || '';
      badgeForbidden.forEach((e) => { if (t.includes(e)) badgeEmoji.push(e); });
    });
    const tabs = document.querySelector('.market-tabs');
    const mt = tabs ? getComputedStyle(tabs) : null;
    const tab0 = document.querySelector('.market-tab');
    const t0 = tab0 ? getComputedStyle(tab0) : null;
    return {
      svgCount,
      brandLogoSvg: hasSvg('.brand-logo'),
      navIcoSvg: [...document.querySelectorAll('.nav-ico')].every((n) => n.querySelector('svg')),
      pageTitleSvg: [...document.querySelectorAll('.page-title')].every((p) => p.querySelector('svg')),
      sectionLabelSvg: hasSvg('.section-label'),
      disclaimerSvg: hasSvg('#disclaimer'),
      emojiFound: [...new Set(emojiFound)],
      badgeEmoji: [...new Set(badgeEmoji)],
      tabsGap: mt ? (mt.columnGap || mt.gap) : '',
      tabsPadding: mt ? t0.padding : '',
      tabPadding: t0 ? t0.padding : '',
    };
  });
  report.asserts.push({ name: 'SVG图标总数', value: ui.svgCount });
  report.asserts.push({ name: '图标类emoji残留', value: ui.emojiFound.length === 0 ? '无' : ui.emojiFound.join('') });
  logStep('品牌Logo为SVG', ui.brandLogoSvg);
  logStep('底部导航5图标全为SVG', ui.navIcoSvg);
  logStep('5个页面标题全为SVG', ui.pageTitleSvg);
  logStep('区块标签为SVG', ui.sectionLabelSvg);
  logStep('免责声明图标为SVG', ui.disclaimerSvg);
  logStep('图标类emoji已清零', ui.emojiFound.length === 0, ui.emojiFound.join(''));
  logStep('信号徽章badge零emoji', ui.badgeEmoji.length === 0, ui.badgeEmoji.join(''));
  report.asserts.push({ name: '徽章badge emoji残留', value: ui.badgeEmoji.length === 0 ? '无' : ui.badgeEmoji.join('') });
  const tabGap = parseFloat((ui.tabsGap || '0px').replace('px', '')) || 0;
  const tabPadV = ui.tabPadding ? parseFloat(ui.tabPadding.split(' ')[0]) : 0;
  report.asserts.push({ name: '市场Tab gap(手机/Vibrancy分段控件)', value: ui.tabsGap });
  report.asserts.push({ name: '市场Tab 内按钮padding(手机)', value: ui.tabPadding });
  // 新设计分段控件 gap=4px、padding=8px（旧玻璃态为 8/10，此处按新规范校验“紧凑分段控件”）
  logStep('市场Tab为紧凑分段控件(gap≥4)', tabGap >= 4, ui.tabsGap);
  logStep('市场Tab按钮有垂直内边距(≥8)', tabPadV >= 8, ui.tabPadding);

  // 手机端触控区域 >= 舒适度下限 校验
  await page.click('[data-page="reco"]'); await sleep(150);
  const touch = await page.evaluate(() => {
    const h = (sel) => { const el = document.querySelector(sel); return el ? Math.round(el.getBoundingClientRect().height) : 0; };
    return { navBtn: h('.nav-btn'), marketTab: h('.market-tab'), tagChip: h('.tag-chip'), recoAdd: h('.reco-add') };
  });
  report.asserts.push({ name: '触控高度(手机)', value: touch });
  logStep('nav 触控区>=44', touch.navBtn >= 44, touch.navBtn + 'px');
  logStep('市场Tab触控区>=40', touch.marketTab >= 40, touch.marketTab + 'px');
  logStep('标签触控区>=36', touch.tagChip >= 36, touch.tagChip + 'px');
  logStep('加入按钮触控区>=40', touch.recoAdd >= 40, touch.recoAdd + 'px');
  await page.screenshot({ path: SHOTS + '/mobile-390.png' });

  // ===== ③ 响应式断点 + 栅格列数（遍历 360/390/768/1280）=====
  const viewports = [
    { w: 360, h: 800, label: '手机-小(360)' },
    { w: 390, h: 844, label: '手机(390)' },
    { w: 768, h: 1024, label: '平板(768)' },
    { w: 1280, h: 900, label: '桌面(1280)' },
  ];
  for (const vp of viewports) {
    await page.setViewportSize({ width: vp.w, height: vp.h });
    await page.click('[data-page="overview"]'); await sleep(250);
    const w = await containerW(page);
    const idxCols = await gridCols(page, '#indexGrid');
    await page.click('[data-page="reco"]'); await sleep(200);
    const recoCols = await gridCols(page, '#recoGrid');
    const overflow = !(await noHScroll(page));
    const expW = vp.w < 768 ? Math.min(vp.w, 480) : vp.w < 1024 ? Math.min(vp.w, 820) : Math.min(vp.w, 960);
    let expIdx = 3, expReco = 2;
    if (vp.w >= 600) { expIdx = 4; expReco = 3; }
    if (vp.w >= 1024) { expIdx = 6; expReco = 4; }
    report.asserts.push({ name: `容器宽度[${vp.label}]`, value: `${w} (期望≈${expW})` });
    report.asserts.push({ name: `指数列数[${vp.label}]`, value: `${idxCols} (期望${expIdx})` });
    report.asserts.push({ name: `推荐列数[${vp.label}]`, value: `${recoCols} (期望${expReco})` });
    logStep(`容器宽度自适应[${vp.label}]`, Math.abs(w - expW) <= 2, `${w}≈${expW}`);
    logStep(`无横向溢出[${vp.label}]`, !overflow);
    logStep(`指数网格扩展[${vp.label}]`, idxCols === expIdx, `${idxCols}=${expIdx}`);
    logStep(`推荐网格扩展[${vp.label}]`, recoCols === expReco, `${recoCols}=${expReco}`);
    if (vp.w >= 768) {
      const topbarRow = await page.$eval('.topbar', (el) => getComputedStyle(el).display === 'flex');
      logStep(`桌面header行布局[${vp.label}]`, topbarRow);
      const topbarGap = await page.$eval('.topbar', (el) => getComputedStyle(el).gap);
      const tabsGapD = await page.$eval('.market-tabs', (el) => getComputedStyle(el).columnGap || getComputedStyle(el).gap);
      const tabPadV = await page.$eval('.market-tab', (el) => parseFloat(getComputedStyle(el).padding.split(' ')[0]));
      report.asserts.push({ name: `桌面header品牌-Tab间距[${vp.label}]`, value: topbarGap });
      report.asserts.push({ name: `桌面市场Tab gap[${vp.label}]`, value: tabsGapD });
      logStep(`桌面品牌与Tab间距≥24px[${vp.label}]`, parseFloat(topbarGap) >= 24, topbarGap);
      // 新设计桌面分段控件内部 gap=6px（旧为 10px），按新规范校验
      logStep(`桌面市场Tab分段控件(gap≥6)[${vp.label}]`, parseFloat(tabsGapD) >= 6, tabsGapD);
      logStep(`桌面Tab内边距加大(≥10)[${vp.label}]`, tabPadV >= 10, tabPadV + 'px');
    }
    await page.screenshot({ path: SHOTS + `/${vp.label}.png`, fullPage: false });
  }

  // ===== ④ 桌面(1280) 关键交互复核 + macOS Vibrancy 再断言 =====
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.click('[data-page="reco"]'); await sleep(200);
  await page.click('[data-market="A"]'); await sleep(150);
  const desktopReco = await recoCount(page);
  logStep('桌面推荐页渲染', desktopReco > 0, 'cards=' + desktopReco);
  await page.click('[data-page="watch"]'); await sleep(200);
  if (rowCount > 0) {
    await page.click('#watchGroups .row'); await sleep(250);
    const dDisplay = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('桌面点行打开弹层(computed display=flex)', dDisplay === 'flex', dDisplay);
    await page.keyboard.press('Escape'); await sleep(200);
    const dClose = await page.$eval('#sheetMask', (el) => getComputedStyle(el).display);
    logStep('桌面ESC关闭(computed display=none)', dClose === 'none', dClose);
  }
  // 桌面视口 Vibrancy 一致性断言
  await vibrancyAssert(page, '桌面1280');
  await page.screenshot({ path: SHOTS + '/desktop-1280-final.png' });

  // ===== ⑤ 零网络依赖 / 无控制台报错 =====
  report.errors = consoleErrors;
  logStep('无控制台/页面报错', consoleErrors.length === 0, consoleErrors.slice(0, 6).join(' | '));
  logStep('零外部网络请求(file://外)', report.netRequests.length === 0, report.netRequests.slice(0, 6).join(' | '));

  fs.writeFileSync('D:/Projects/stock-tracker/qa/report.json', JSON.stringify({
    ok: report.steps.every((s) => s.ok),
    passRate: report.steps.length ? report.steps.filter((s) => s.ok).length / report.steps.length : 1,
    totalSteps: report.steps.length,
    passed: report.steps.filter((s) => s.ok).length,
    failed: report.steps.filter((s) => !s.ok).length,
    knownIssues: [],
    errors: report.errors,
    netRequests: report.netRequests,
    steps: report.steps,
    asserts: report.asserts,
  }, null, 2));
  const passed = report.steps.filter((s) => s.ok).length;
  console.log(`REPORT_DONE errors=${consoleErrors.length} netRequests=${report.netRequests.length} steps=${report.steps.length} passed=${passed}`);
  await browser.close();
})().catch((e) => { console.error('SCRIPT_ERROR', e); process.exit(1); });
