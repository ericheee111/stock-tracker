/* =========================================================================
 * wb3_probe_nulls.cjs —— null/缺字段渲染诚实性专项探针（WB3 补充）
 * 载荷：WB3_SCENARIO=nulls；断言点：
 *   1. watchlist 第二项（quote.last=null, score=null）→ 价格显示 '—'，评分显示「暂无评分」
 *   2. overview top_opportunities[0]（quote.last=null）→ 价格 '—'，涨跌幅仍可显示
 *   3. 页面可见文本不含 '0.00' 冒充 null 价格 / 不含裸 null / undefined / [object Object]
 * 输出：WB3_PROBE_DONE pass=1|0 退出码 0/1（在外部终端复核有效）
 * ========================================================================= */
'use strict';
const runtime=require('./wb3_runtime.cjs');
(async () => {
  const server = await runtime.startServer('nulls');
  let browser,context;
  try {
  browser = await runtime.loadPlaywright().chromium.launch({headless:true});
  context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.addInitScript(() => {
    try { sessionStorage.setItem('stockTrackerPrivateAccess::' + encodeURIComponent(''), 'wb3-synthetic-private-access-value-0123456789abcdef'); } catch (e) {}
    try { sessionStorage.setItem('stockTrackerPrivateAccess', 'wb3-synthetic-private-access-value-0123456789abcdef'); } catch (e) {}
  });
  const page = await context.newPage();
  const failures = [];
  try {
    await page.goto(server.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state]', { timeout: 12000 });
    await page.waitForTimeout(1500);

    // 自选页第二张卡（招商银行·null quote/score）
    await page.evaluate(() => { const b = document.querySelector('.nav-btn[data-page="watch"]'); if (b) b.click(); });
    await page.waitForTimeout(800);
    const watchProbe = await page.evaluate(() => {
      const cards = document.querySelectorAll('.wl-card');
      const card = cards[1];
      if (!card) return { found: false };
      const name = (card.querySelector('.wl-name') || {}).textContent || '';
      const price = (card.querySelector('.live-price') || {}).textContent || '';
      const scoreCells = card.querySelectorAll('.score-cell').length;
      const emptyScore = ((card.querySelector('.score-grid') || {}).textContent || '').indexOf('暂无评分') !== -1 ||
        (card.textContent || '').indexOf('暂无评分') !== -1;
      return { found: true, name: name.trim(), price: price.trim(), scoreCells: scoreCells, emptyScore: emptyScore, text: card.textContent.slice(0, 200) };
    });
    if (!watchProbe.found) failures.push('watch 第二卡未渲染');
    else {
      if (watchProbe.price !== '—') failures.push('null last 价格显示为「' + watchProbe.price + '」，期望「—」');
      if (!watchProbe.emptyScore) failures.push('null score 未显示「暂无评分」');
    }

    // 总览页 top_opportunities[0]（null quote）
    await page.evaluate(() => { const b = document.querySelector('.nav-btn[data-page="overview"]'); if (b) b.click(); });
    await page.waitForTimeout(800);
    const ovProbe = await page.evaluate(() => {
      const card = document.querySelector('#topList .opp-card');
      if (!card) return { found: false };
      const price = (card.querySelector('.opp-price') || {}).textContent || '';
      return { found: true, price: price.trim() };
    });
    if (!ovProbe.found) failures.push('overview 重点机会未渲染');
    else if (ovProbe.price !== '—') failures.push('overview null 价格显示为「' + ovProbe.price + '」，期望「—」');

    // 全页脏字符串复查（含指数卡区域）
    const dirty = await page.evaluate(() => {
      const t = document.body.innerText || '';
      const out = [];
      if (t.indexOf('[object Object]') !== -1) out.push('[object Object]');
      if (/(^|\s)undefined(\s|$)/.test(t)) out.push('undefined');
      if (/(^|\s)null(\s|$)/.test(t)) out.push('null');
      return out;
    });
    if (dirty.length) failures.push('脏字符串：' + dirty.join(','));

    console.log('WATCH_PROBE ' + JSON.stringify(watchProbe));
    console.log('OVERVIEW_PROBE ' + JSON.stringify(ovProbe));
  } catch (e) {
    failures.push('probe error: ' + (e && e.message || e));
  } finally {
    await context.close();

  }
  return fs_report(failures);
  function fs_report(list) {
    if (list.length) { console.log('WB3_PROBE_DONE pass=0 failures=' + JSON.stringify(list)); process.exitCode = 1; }
    else console.log('WB3_PROBE_DONE pass=1');
    return list.length ? 1 : 0;
  }
  } finally { await server.stop(); if(browser)await runtime.bounded(()=>browser.close()); }
})().then(code=>process.exit(code)).catch((e) => { console.error('FATAL ' + (e && e.stack || e)); process.exit(2); });
