// 回归验证：真实报价在 UI 正确展示（A2 + B 的 DOM 部分）
// 解析 playwright：优先项目依赖，回退本机 Temp 安装路径
function loadPlaywright() {
  try { return require('playwright'); } catch (e) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright',
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) {}
  }
  console.error('[FATAL] 未找到 playwright');
  process.exit(2);
}
const { chromium } = loadPlaywright();
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE || 'http://127.0.0.1:8080';
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

const result = {
  base: BASE,
  markets: {},
  consoleErrors: [],
  pageErrors: [],
  faviconRequests: [],
  screenshots: [],
  analysis: {},
};
const isDash = t => t === '—' || t === '' || t == null;

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  page.on('console', m => { if (m.type() === 'error') result.consoleErrors.push(m.text()); });
  page.on('pageerror', e => result.pageErrors.push(e.message));
  page.on('request', r => { if (/favicon\.ico/i.test(r.url())) result.faviconRequests.push(r.url()); });

  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => { result.fatal = e.message; });
  // 等待机会卡片渲染（A 市场应有真实报价卡）
  try { await page.waitForSelector('#topList .opp-card', { timeout: 12000 }); } catch (e) {}
  try { await page.waitForSelector('#indexGrid .index-card', { timeout: 12000 }); } catch (e) {}
  await sleep(2500);

  const extractOpp = () => page.$$eval('#topList .opp-card', cards => cards.map(c => ({
    name: (c.querySelector('.opp-name') ? c.querySelector('.opp-name').textContent : '').trim(),
    code: (c.querySelector('.opp-code') ? c.querySelector('.opp-code').textContent : '').trim(),
    price: (c.querySelector('.opp-price') ? c.querySelector('.opp-price').textContent : '').trim(),
  }))).catch(() => []);
  const extractIndex = () => page.$$eval('#indexGrid .index-card', cards => cards.map(c => ({
    name: (c.querySelector('.index-name') ? c.querySelector('.index-name').textContent : '').trim(),
    value: (c.querySelector('.index-value') ? c.querySelector('.index-value').textContent : '').trim(),
    status: (c.querySelector('.index-status') ? c.querySelector('.index-status').textContent : '').trim(),
  }))).catch(() => []);
  const topListText = () => page.$eval('#topList', e => e.textContent || '').catch(() => '');

  for (const m of ['A', 'HK', 'US']) {
    if (m !== 'A') {
      await page.click(`.market-tab[data-market="${m}"]`).catch(e => { result.note = 'click ' + m + ' ' + e.message; });
      await sleep(2000);
    }
    const opp = await extractOpp();
    const idx = await extractIndex();
    const tl = await topListText();
    result.markets[m] = { oppCards: opp, indexCards: idx, topListEmpty: /暂无重点机会/.test(tl) };
    const shot = path.join(OUT, `verify-${m}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    result.screenshots.push('qa/ui/shots/verify-' + m + '.png');
  }

  // 早期持久化：在 browser.close 之前写文件，避免 headless-shell 退出挂起导致丢数据
  fs.writeFileSync(OUT + '/verify_result.json', JSON.stringify(result, null, 2));
  process.stderr.write('[LOG] data written, closing browser\n');

  // close 带超时保护
  const closeP = browser.close();
  const timeoutP = sleep(8000).then(() => { throw new Error('browser.close timeout'); });
  await Promise.race([closeP, timeoutP]).catch(e => process.stderr.write('[LOG] close warn: ' + e.message + '\n'));
  process.exit(0);

  // ---- 分析 ----
  const aOpp = result.markets.A.oppCards;
  result.analysis.A_oppCount = aOpp.length;
  result.analysis.A_allPricesReal = aOpp.length > 0 && aOpp.every(c => !isDash(c.price) && parseFloat(c.price) > 0);
  result.analysis.A_allHaveName = aOpp.length > 0 && aOpp.every(c => !isDash(c.name));
  result.analysis.A_sampleCards = aOpp.slice(0, 3).map(c => ({ symbol: c.code, name: c.name, price: c.price }));

  const allIdx = Object.values(result.markets).flatMap(x => x.indexCards);
  result.analysis.indexCardCount = allIdx.length;
  result.analysis.indexRealValues = allIdx.length > 0 && allIdx.every(c => !isDash(c.value) && parseFloat(c.value) > 0);
  result.analysis.indexNoUnknown = allIdx.every(c => !/未知|数据不足/.test(c.status));

  // HK/US 机会列表是否数据缺失（无机会信号）vs 修复前“有但价格空”
  result.analysis.HK_oppCount = result.markets.HK.oppCards.length;
  result.analysis.US_oppCount = result.markets.US.oppCards.length;
  result.analysis.HKUS_oppEmpty = (result.markets.HK.oppCards.length === 0 && result.markets.US.oppCards.length === 0);

  fs.writeFileSync(OUT + '/verify_result.json', JSON.stringify(result, null, 2));
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
})().catch(e => { fs.writeFileSync(path.join(OUT, 'fatal.txt'), String(e)); process.exit(1); });
