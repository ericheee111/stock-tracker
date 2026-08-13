// N1 + N2 浏览器回归验证（结构化断言 + 分步日志）
const PLAYWRIGHT_PATH = 'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright';
const { chromium } = require(PLAYWRIGHT_PATH);
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE || 'http://127.0.0.1:8080';
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => process.stderr.write('[LOG] ' + a.map(String).join(' ') + '\n');

(async () => {
  const result = { base: BASE, steps: [], consoleErrors: [], pageErrors: [], faviconRequests: [] };
  log('launching');
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  log('launched');
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  page.on('console', m => { if (m.type() === 'error') result.consoleErrors.push(m.text()); });
  page.on('pageerror', e => result.pageErrors.push(e.message));
  page.on('request', r => { if (/favicon\.ico/i.test(r.url())) result.faviconRequests.push({ url: r.url(), method: r.method() }); });
  page.on('requestfailed', r => { if (/favicon\.ico/i.test(r.url())) result.faviconRequests.push({ url: r.url(), failed: (r.failure() && r.failure().errorText) || 'failed' }); });

  log('goto', BASE);
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => { log('goto-error', e.message); result.steps.push({ step: 'goto', ok: false, note: e.message }); });
  log('goto done; waiting 4s');
  await sleep(4000);

  const lenOf = sel => page.$eval(sel, e => e.innerText.trim().length).catch(() => -1);
  const exists = sel => page.$(sel).then(h => !!h).catch(() => false);

  // N1
  const n1_ok = result.faviconRequests.length === 0;
  result.steps.push({ step: 'N1 favicon', ok: n1_ok, faviconRequestCount: result.faviconRequests.length,
    detail: n1_ok ? '浏览器未发起任何 /favicon.ico 请求（data-URI 图标生效）' : '发现 favicon.ico 请求: ' + JSON.stringify(result.faviconRequests) });
  log('N1 done', JSON.stringify(result.steps[result.steps.length-1]));

  // N2
  const radarExists = await exists('#radarGroups');
  log('radarExists', radarExists);
  const radarLen = await lenOf('#radarGroups');
  const radarText = await page.$eval('#radarGroups', e => e.textContent).catch(() => '');
  const hasDataInsufficientGroup = /数据不足/.test(radarText);
  log('radarLen', radarLen, 'hasGroup', hasDataInsufficientGroup);

  const navOk = { watch: false, radar: false, research: false };
  for (const p of ['watch', 'radar', 'research']) {
    await page.click(`.nav-btn[data-page="${p}"]`).catch(e => log('nav ' + p + ' err', e.message));
    await sleep(1000);
    navOk[p] = await page.$eval(`#page-${p}`, e => e.classList.contains('active')).catch(() => false);
    log('nav', p, navOk[p]);
  }

  let dataInvalidCard = null;
  try {
    const cards = await page.$$eval('#radarGroups .row, #radarGroups [data-symbol], #topList .row',
      els => els.map(e => ({ text: e.textContent || '', cls: e.className || '' })));
    for (const c of cards) { if (/数据不足暂不发信号|数据异常不给信号/.test(c.text)) { dataInvalidCard = c; break; } }
  } catch (e) { log('cards err', e.message); }

  await page.screenshot({ path: OUT + '/qa01-overview.png' }).catch(e => log('shot1 err', e.message));
  log('shot1 done');
  await page.click(`.nav-btn[data-page="radar"]`).catch(() => {});
  await sleep(800);
  await page.screenshot({ path: OUT + '/qa02-radar.png' }).catch(e => log('shot2 err', e.message));
  log('shot2 done');

  const n2_ok = radarExists && navOk.watch && navOk.radar && navOk.research && result.consoleErrors.length === 0 && result.pageErrors.length === 0;
  result.steps.push({ step: 'N2 DOM 健全性', ok: n2_ok, radarGroupsExists: radarExists, radarGroupsLen: radarLen,
    radarHasDataInsufficientGroup: hasDataInsufficientGroup, navActive: navOk,
    consoleErrorCount: result.consoleErrors.length, pageErrorCount: result.pageErrors.length,
    liveDataInvalidCard: dataInvalidCard ? dataInvalidCard.text.slice(0, 80) : '(交易时段未触发 DATA_INVALID，属预期)',
    detail: n2_ok ? '无 JS 报错、雷达区域渲染、三页导航正常' : 'DOM 健全性检查未通过' });
  log('N2 done', JSON.stringify(result.steps[result.steps.length-1]));

  await browser.close();
  log('closed');
  fs.writeFileSync(OUT + '/qa_result.json', JSON.stringify(result, null, 2));
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  log('DONE');
})().catch(e => { log('FATAL', e.message, e.stack); fs.writeFileSync(path.join(OUT, 'fatal.txt'), String(e)); process.exit(1); });
