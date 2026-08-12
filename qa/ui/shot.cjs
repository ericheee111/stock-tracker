// 多策略解析 playwright：优先项目依赖，回退本机 Temp 安装路径，再回退 PLAYWRIGHT_PATH 环境变量
function loadPlaywright() {
  try { return require('playwright'); } catch (e) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright',
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) {}
  }
  console.error('[FATAL] 未找到 playwright：请 `npm i -D playwright`，或设置环境变量 PLAYWRIGHT_PATH 指向其安装目录');
  process.exit(2);
}
const { chromium } = loadPlaywright();
const path = require('path');
const fs = require('fs');
const BASE = process.env.BASE || 'http://127.0.0.1:8080';
const OUT = process.env.SHOT_OUT || path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = [];
const L = (...a) => { const s = a.map(String).join(' '); log.push(s); console.log(s); };

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') L('[console.' + m.type() + ']', m.text()); });
  page.on('pageerror', e => L('[pageerror]', e.message));
  page.on('requestfailed', r => L('[reqfail]', r.url(), (r.failure() && r.failure().errorText) || ''));

  L('goto', BASE);
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => L('[goto-error]', e.message));
  await sleep(4000);

  const len = (sel) => page.$eval(sel, e => e.innerText.trim().length).catch(() => -1);
  const text = (sel) => page.$eval(sel, e => e.textContent.trim().slice(0, 160)).catch(() => '(not found)');

  await page.screenshot({ path: OUT + '/01-overview-A.png', fullPage: true });
  L('overview | indexGrid.len=', await len('#indexGrid'));
  L('overview | overviewGrid.len=', await len('#overviewGrid'));
  L('overview | topList.len=', await len('#topList'));
  L('overview | banner=', await text('#banner'));
  L('overview | topList.sample=', await text('#topList'));

  for (const m of ['HK', 'US', 'A']) {
    await page.click(`.market-tab[data-market="${m}"]`).catch(e => L('click market ' + m + ' err', e.message));
    await sleep(1800);
    await page.screenshot({ path: OUT + `/02-market-${m}.png`, fullPage: true });
    L(`market ${m} | topList.len=`, await len('#topList'), '| sample=', (await text('#topList')).slice(0, 80));
  }

  for (const p of ['watch', 'radar', 'research']) {
    await page.click(`.nav-btn[data-page="${p}"]`).catch(e => L('click page ' + p + ' err', e.message));
    await sleep(1400);
    await page.screenshot({ path: OUT + `/03-page-${p}.png`, fullPage: true });
    const active = await page.$eval('#page-' + p, e => e.classList.contains('active')).catch(() => false);
    L(`page ${p} | active=${active} | watchGroups.len=${await len('#watchGroups')} | radarGroups.len=${await len('#radarGroups')} | research.len=${await len('#page-research')}`);
  }

  await page.click(`.nav-btn[data-page="overview"]`).catch(() => {});
  await sleep(600);
  const rowSel = '#topList .row, #topList [data-symbol], #topList .opp-item, #radarGroups .row, #radarGroups [data-symbol]';
  const hasRow = await page.$(rowSel);
  L('topList/radar has clickable row?', !!hasRow);
  if (hasRow) {
    await hasRow.click().catch(e => L('click row err', e.message));
    await sleep(900);
    const sheetOpen = await page.$eval('#sheetMask', e => !e.hasAttribute('hidden')).catch(() => false);
    L('signal sheet open?', sheetOpen, '| sheet.len=', await len('#sheet'));
    await page.screenshot({ path: OUT + '/04-sheet.png', fullPage: true });
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await sleep(800);
  await page.screenshot({ path: OUT + '/05-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });

  fs.writeFileSync(OUT + '/report.txt', log.join('\n'));
  await browser.close();
  L('DONE');
})().catch(e => { L('FATAL', e.message, e.stack); fs.writeFileSync(OUT + '/fatal.txt', String(e)); process.exit(1); });
