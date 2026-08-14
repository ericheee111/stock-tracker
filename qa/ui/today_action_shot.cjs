/* =========================================================================
 * today_action_shot.cjs —— 截图辅助（不强制断言）
 * 同 today_action_qa.cjs 的内嵌静态服务器 + /api/brief/today 拦截，
 * 仅抓取今日页在 390 / 768 / 1280 三档视口的截图，并记录关键区块是否渲染，
 * 便于人工查看视觉与布局。退出码恒为 0（除非发生严重错误）。
 * ========================================================================= */
const http = require('http');
const fs = require('fs');
const path = require('path');

function loadPlaywright() {
  try { return require('playwright'); } catch (e) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright'
  ].filter(Boolean);
  for (const c of candidates) { try { return require(c); } catch (e) {} }
  console.error('[FATAL] 未找到 playwright');
  process.exit(2);
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(__dirname, '..', '..');
const WEB = path.join(ROOT, 'web');
const FIXTURE = path.join(ROOT, 'qa', 'fixtures', 'today-brief-v1.json');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' };

function startStaticServer() {
  return new Promise(function (resolve) {
    const server = http.createServer(function (req, res) {
      let urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
      if (urlPath === '/') urlPath = '/index.html';
      if (urlPath.indexOf('/api/') === 0) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('nf'); return; }
      const fp = path.normalize(path.join(WEB, urlPath));
      if (fp.indexOf(WEB) !== 0) { res.writeHead(403); res.end('no'); return; }
      fs.readFile(fp, function (err, buf) {
        if (err) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('nf'); return; }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(fp).toLowerCase()] || 'application/octet-stream' });
        res.end(buf);
      });
    });
    server.listen(0, '127.0.0.1', function () { resolve({ server, base: 'http://127.0.0.1:' + server.address().port }); });
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const fixtureJson = fs.readFileSync(FIXTURE, 'utf-8');
  const { server, base } = await startStaticServer();
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const shots = path.join(ROOT, 'qa', 'shots');
  fs.mkdirSync(shots, { recursive: true });
  await page.route('**/api/brief/today', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: fixtureJson }));

  const report = [];
  try {
    await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#todayBrief .tb-summary', { timeout: 8000 }).catch(() => {});
    await sleep(600);

    for (const vp of [{ w: 390, h: 844, n: 'mobile' }, { w: 768, h: 1024, n: 'tablet' }, { w: 1280, h: 900, n: 'desktop' }]) {
      await page.setViewportSize({ width: vp.w, height: vp.h });
      await sleep(500);
      const coreN = await page.$$eval('#todayBrief .tb-core', e => e.length).catch(() => 0);
      const holdN = await page.$$eval('#todayBrief .tb-holding', e => e.length).catch(() => 0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      await page.screenshot({ path: path.join(shots, 'today-' + vp.n + '.png'), fullPage: true }).catch(() => {});
      report.push({ viewport: vp.n, core: coreN, holding: holdN, overflowPx: overflow });
      console.log('shot ' + vp.n + ' | core=' + coreN + ' holding=' + holdN + ' overflowPx=' + overflow);
    }

    // 顺带确认其余页面未因今日页改动而崩溃
    for (const p of ['overview', 'watch', 'radar', 'research']) {
      await page.click('.nav-btn[data-page="' + p + '"]').catch(() => {});
      await sleep(900);
      const active = await page.$eval('#page-' + p, e => e.classList.contains('active')).catch(() => false);
      await page.screenshot({ path: path.join(shots, 'page-' + p + '.png'), fullPage: true }).catch(() => {});
      console.log('page ' + p + ' active=' + active);
    }
  } catch (e) {
    console.error('SHOT ERROR', e.message);
  } finally {
    const closeP = browser ? browser.close().catch(function () {}).then(function () { return 'closed'; }) : Promise.resolve('no-browser');
    await Promise.race([closeP, new Promise(function (r) { setTimeout(r, 4000); })]);
    if (server) { try { server.close(); } catch (e) {} }
  }
  fs.writeFileSync(path.join(shots, 'today-shot-report.json'), JSON.stringify(report, null, 2));
  console.log('DONE · shots @ ' + shots);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(2); });
