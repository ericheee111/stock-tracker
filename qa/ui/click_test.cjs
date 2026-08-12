/* 验证：点击"重点机会"卡片能弹出信号详情 sheet 且内容非空。
 * 依赖与 shot.cjs 相同的 playwright 模块路径。
 * 退出码：0=通过，1=失败，2=未找到卡片，3=致命错误。
 */
const { chromium } = require('C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright');
const fs = require('fs');
const BASE = 'http://127.0.0.1:8080';
const OUT = 'C:/Users/Administrator/AppData/Local/Temp/uitest/shots';
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(a.map(String).join(' '));

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => {
    if (m.type() === 'error') {
      const t = m.text();
      // 忽略 favicon 等静态资源 404（非 JS 异常）
      if (!/Failed to load resource|favicon/i.test(t)) errors.push('[console] ' + t);
    }
  });

  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => log('goto-error', e.message));
  await sleep(4000);

  // 确保在总览页
  await page.click('.nav-btn[data-page="overview"]').catch(() => {});
  await sleep(800);

  // 取首张带 data-signal 的重点机会卡片
  const cardSel = '#topList .opp-card[data-signal]';
  const card = await page.$(cardSel);
  if (!card) {
    log('FAIL: 未找到可点击的重点机会卡片（缺少 data-signal）');
    await browser.close();
    process.exit(2);
  }
  const sigKey = await page.$eval(cardSel, el => el.getAttribute('data-signal'));
  log('click card data-signal=', sigKey);

  await card.click().catch(e => log('click err', e.message));
  await sleep(1000);

  const open = await page.$eval('#sheetMask', e => !e.hasAttribute('hidden')).catch(() => false);
  const sheetLen = await page.$eval('#sheet', e => e.innerText.trim().length).catch(() => 0);
  const sheetHasTitle = await page.$eval('#sheet', e => !!e.querySelector('.sheet-title')).catch(() => false);
  log('sheet.open=', open, '| sheet.len=', sheetLen, '| hasTitle=', sheetHasTitle);

  await page.screenshot({ path: OUT + '/06-sheet-click.png', fullPage: true });

  const pass = open && sheetLen > 0 && sheetHasTitle;
  log(pass ? 'PASS' : 'FAIL', '| pageerrors=', errors.length);
  if (errors.length) errors.slice(0, 5).forEach(e => log('  err:', e));

  await browser.close();
  process.exit(pass ? 0 : 1);
})().catch(e => { log('FATAL', e.message, e.stack); process.exit(3); });
