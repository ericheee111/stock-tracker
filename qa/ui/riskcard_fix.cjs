// 针对风险事件卡渲染修复的可视化验证：
// 1) 定位「风险事件」卡片，截全卡存 qa/ui/shots/riskcard_fix.png
// 2) 断言渲染结果不再出现原始 JSON 字符串（如 {"symbol" 或行首 "{" ）
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
const OUT = process.env.SHOT_OUT || path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = [];
const L = (...a) => { const s = a.map(String).join(' '); log.push(s); console.log(s); };

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', e => L('[pageerror]', e.message));
  page.on('console', m => { if (m.type() === 'error') L('[console.error]', m.text()); });

  L('goto', BASE);
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => L('[goto-error]', e.message));
  await page.waitForSelector('.risk-list', { timeout: 15000 }).catch(e => L('[wait-risk-list-error]', e.message));
  await sleep(3500); // 等 SSE/overview 数据写入

  const riskCount = await page.$$eval('.risk-item', els => els.length).catch(() => -1);
  L('risk-item count=', riskCount);

  const hasRawJSON = await page.$$eval('.risk-item', els =>
    els.some(e => /\{\s*"symbol"/.test(e.innerText) || e.innerText.trim().startsWith('{'))
  ).catch(() => null);
  L('hasRawJSON=', hasRawJSON);

  const firstRisk = await page.$eval('.risk-item', e => e.innerText).catch(() => '(none)');
  L('firstRisk.innerText=', JSON.stringify(firstRisk).slice(0, 400));

  const riskCard = await page.$('xpath=//div[contains(@class,"card")][.//*[contains(text(),"风险事件")]]');
  if (riskCard) {
    await riskCard.screenshot({ path: OUT + '/riskcard_fix.png' });
    L('saved', OUT + '/riskcard_fix.png');
  } else {
    L('[warn] risk card not found, fallback full screenshot');
    await page.screenshot({ path: OUT + '/riskcard_fix.png', fullPage: true });
  }

  fs.writeFileSync(OUT + '/riskcard_fix_report.txt', log.join('\n'));
  await browser.close();
  L('DONE hasRawJSON=' + hasRawJSON);
  process.exit(hasRawJSON === true ? 3 : 0);
})().catch(e => { L('FATAL', e.message, e.stack); fs.writeFileSync(OUT + '/riskcard_fix_fatal.txt', String(e)); process.exit(1); });
