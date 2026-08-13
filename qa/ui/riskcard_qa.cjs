// 独立 QA 视觉回归（硬化版，参照 verify_realdata.cjs 的 close 超时保护）。
// 验证风险事件卡不再渲染原始 JSON：
// 1) 定位风险事件卡，截全卡到 qa/ui/shots/riskcard_qa.png
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
  const browser = await chromium.launch({ args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'], headless: true });
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
    await riskCard.screenshot({ path: OUT + '/riskcard_qa.png' });
    L('saved', OUT + '/riskcard_qa.png');
  } else {
    L('[warn] risk card not found, fallback full screenshot');
    await page.screenshot({ path: OUT + '/riskcard_qa.png', fullPage: true });
  }

  // 早期持久化：在 browser.close 之前写文件，避免 headless-shell 退出挂起导致丢数据
  fs.writeFileSync(OUT + '/riskcard_qa_report.txt', log.join('\n'));
  process.stderr.write('[LOG] data written, closing browser\n');

  // close 带超时保护
  const closeP = browser.close();
  const timeoutP = sleep(8000).then(() => { throw new Error('browser.close timeout'); });
  await Promise.race([closeP, timeoutP]).catch(e => process.stderr.write('[LOG] close warn: ' + e.message + '\n'));
  process.exit(hasRawJSON === true ? 3 : 0);
})().catch(e => { fs.writeFileSync(OUT + '/riskcard_qa_fatal.txt', String(e)); process.stderr.write('[FATAL] ' + e.message + '\n'); process.exit(1); });
