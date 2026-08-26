/* =========================================================================
 * today_action_qa.cjs —— Stage 1 Lane D 前端契约验收
 * 自包含：内嵌静态服务器（托管 web/）+ Playwright 拦截 /api/brief/today → fixture。
 * 不依赖运行中的后端；其余 /api/* 由静态服务器返回 404（被 fetchJSON 捕获，不白屏）。
 * 退出码：0=通过，1=失败，2=严重错误。
 * 断言覆盖：Brief 存在 / Core 1—5 / Action label 可见 / 概率空免责声明 /
 *          无伪百分比 / Big Trend 未启用无虚构候选 / Strategy 不伪装真实战绩 /
 *          持仓动作与新机会分开 / 无 [object Object] / 无原始 JSON /
 *          无 pageerror / 390px+1280px 布局可用。
 * ========================================================================= */
const http = require('http');
const fs = require('fs');
const path = require('path');

// 防御：静态服务器在浏览器关闭后可能仍有迟到请求，响应对象可能已断开。
// 未处理 rejection 必须进入最终失败统计，不能只打印后继续 PASS。
const unhandledRejections = [];
process.on('unhandledRejection', function (reason) {
  const text = reason && (reason.stack || reason.message || reason);
  unhandledRejections.push(String(text));
  console.error('[unhandledRejection]', text);
});
function safeRes(res) {
  res.on('error', function () {});
  return res;
}

// playwright 解析（与 shot.cjs 同策略）
function loadPlaywright() {
  try { return require('playwright'); } catch (e) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright'
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) {}
  }
  console.error('[FATAL] 未找到 playwright');
  process.exit(2);
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(__dirname, '..', '..');           // 仓库根
const WEB = path.join(ROOT, 'web');
const FIXTURE = path.join(ROOT, 'qa', 'fixtures', 'today-brief-v1.json');
const EXTERNAL_BASE = (process.env.TODAY_QA_BASE_URL || '').replace(/\/$/, '');

function validateBriefContract(brief) {
  const errors = [];
  const coreActions = new Set(['EXECUTABLE', 'WAIT_PULLBACK', 'WAIT_BREAKOUT', 'WATCH', 'AVOID', 'DATA_BLOCKED']);
  const holdingActions = new Set(['HOLD', 'WARNING', 'TRIM', 'PARTIAL_TAKE_PROFIT', 'TREND_RUNNER', 'EXIT', 'DATA_BLOCKED']);
  if (!brief || typeof brief !== 'object' || Array.isArray(brief)) errors.push('brief must be an object');
  if (brief && brief.schema_version !== 'stage1-v1') errors.push('schema_version must be stage1-v1');
  if (!brief || !Array.isArray(brief.core_opportunities) || brief.core_opportunities.length > 5) {
    errors.push('core_opportunities must be an array with at most five items');
  }
  if (!brief || !Array.isArray(brief.holding_actions)) errors.push('holding_actions must be an array');
  const seen = new Set();
  (brief && Array.isArray(brief.core_opportunities) ? brief.core_opportunities : []).forEach(function (item, index) {
    if (!item || typeof item !== 'object') { errors.push('core[' + index + '] must be an object'); return; }
    if (!coreActions.has(item.action_state)) errors.push('core[' + index + '] has invalid action_state');
    if (seen.has(item.symbol)) errors.push('core symbols must be unique');
    seen.add(item.symbol);
    if (item.action_state === 'EXECUTABLE' && item.data_status !== 'LIVE') {
      errors.push('EXECUTABLE core item must be LIVE');
    }
    const scores = item.scores || {};
    ['opportunity', 'timing', 'risk', 'confidence'].forEach(function (key) {
      if (!Number.isInteger(scores[key]) || scores[key] < 0 || scores[key] > 100) {
        errors.push('core[' + index + '].scores.' + key + ' must be an integer in [0,100]');
      }
    });
    const model = item.model || {};
    if (model.calibrated_probability == null && model.probability_evidence_level !== 'INSUFFICIENT') {
      errors.push('null probability requires INSUFFICIENT evidence');
    }
    ['hard_blockers', 'soft_blockers'].forEach(function (key) {
      const blockers = Array.isArray(item[key]) ? item[key] : [];
      blockers.forEach(function (blocker) {
        if (!blocker || typeof blocker !== 'object' || typeof blocker.message !== 'string') {
          errors.push('core[' + index + '].' + key + ' must contain blocker objects');
        }
      });
    });
    if (item.freshness != null && (!Number.isFinite(Number(item.freshness)) || Number(item.freshness) < 0 || Number(item.freshness) > 1)) {
      errors.push('core[' + index + '].freshness must be in [0,1]');
    }
  });
  (brief && Array.isArray(brief.holding_actions) ? brief.holding_actions : []).forEach(function (item, index) {
    if (!item || !holdingActions.has(item.action_state)) errors.push('holding[' + index + '] has invalid action_state');
    if (item && seen.has(item.symbol)) errors.push('symbol cannot appear in both core and holding lanes');
  });
  return errors;
}

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.png': 'image/png'
};

function startStaticServer() {
  return new Promise(function (resolve) {
    const server = http.createServer(function (req, res) {
      safeRes(res);
      let urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
      if (urlPath === '/') urlPath = '/index.html';
      // 仅服务 web/ 下的静态资源；API 由 Playwright 拦截，其余 /api/* 返回 404
      if (urlPath.indexOf('/api/') === 0) {
        res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('not found');
        return;
      }
      const filePath = path.normalize(path.join(WEB, urlPath));
      if (filePath.indexOf(WEB) !== 0) { res.writeHead(403); res.end('forbidden'); return; }
      fs.readFile(filePath, function (err, buf) {
        if (err) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('not found'); return; }
        const ext = path.extname(filePath).toLowerCase();
        res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
        res.end(buf);
      });
    });
    server.listen(0, '127.0.0.1', function () {
      const port = server.address().port;
      resolve({ server: server, base: 'http://127.0.0.1:' + port });
    });
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const KNOWN_ACTIONS = ['当前可执行', '等回踩', '等突破', '继续持有', '风险预警', '建议减仓', '部分止盈', '保留趋势仓', '退出', '当前回避', '值得观察', '数据阻断·禁止决策'];

(async () => {
  const fixtureJson = fs.readFileSync(FIXTURE, 'utf-8');
  const fixtureBrief = JSON.parse(fixtureJson);
  const fixtureContractErrors = validateBriefContract(fixtureBrief);
  const serverInfo = EXTERNAL_BASE
    ? { server: null, base: EXTERNAL_BASE }
    : await startStaticServer();
  const server = serverInfo.server;
  const base = serverInfo.base;
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('console', m => {
    if (m.type() === 'error') {
      const t = m.text();
      if (!/Failed to load resource|favicon/i.test(t)) consoleErrors.push(t);
    }
  });

  // 默认使用 fixture；设置 TODAY_QA_BASE_URL 时直接验证真实后端。
  if (!EXTERNAL_BASE) {
    await page.route('**/api/runtime/health', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'hybrid-runtime-v1', status: 'ONLINE',
        engine_id: 'stock-tracker-local', engine_version: '0.1.0', commit_id: 'development',
        deployment_mode: 'HYBRID_PRIVATE', started_at: '2026-08-14T09:00:00+08:00',
        last_heartbeat_at: '2026-08-14T09:45:00+08:00', last_collection_at: '2026-08-14T09:45:00+08:00',
        data_as_of: '2026-08-14T09:45:00+08:00', data_status: 'LIVE', scheduler_state: 'RUNNING',
        provider_summary: { count: 1, closed: 1, half_open: 0, open: 0 },
        database_state: 'READY', sse_available: true, api_major: 1
      })
    }));
    await page.route('**/api/brief/today', route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: fixtureJson }));
  }

  const steps = [];
  const add = (step, ok, detail) => { steps.push({ step, ok, detail }); console.log((ok ? 'PASS ' : 'FAIL ') + step + (detail ? ' | ' + detail : '')); };

  try {
    if (EXTERNAL_BASE) {
      const contractResponse = await page.request.get(base + '/api/brief/today');
      const liveBrief = contractResponse.ok() ? await contractResponse.json() : null;
      const liveContractErrors = validateBriefContract(liveBrief);
      add('真实 API schema 合同', contractResponse.ok() && liveContractErrors.length === 0,
        'status=' + contractResponse.status() + ' errors=' + liveContractErrors.join('; '));
    } else {
      add('Mock fixture schema 合同', fixtureContractErrors.length === 0,
        'errors=' + fixtureContractErrors.join('; '));
    }
    await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#todayBrief .tb-summary', { timeout: 8000 }).catch(() => {});
    await sleep(600);
    if (EXTERNAL_BASE) {
      const streamConnected = await page.waitForFunction(
        () => window.SSE && window.SSE.isConnected(),
        null,
        { timeout: 5000 }
      ).then(() => true).catch(() => false);
      add('真实私有 fetch-SSE 已连接', streamConnected, 'connected=' + streamConnected);
    }

    const text = () => page.$eval('#todayBrief', e => e.innerText).catch(() => '');
    const count = sel => page.$$eval(sel, els => els.length).catch(() => 0);
    const visible = sel => page.$(sel).then(h => !!h).catch(() => false);

    // 1. Brief 存在
    const briefLen = await page.$eval('#todayBrief', e => e.innerText.trim().length).catch(() => 0);
    const hasSummary = await visible('#todayBrief .tb-summary');
    add('首页 Brief 存在', briefLen > 50 && hasSummary, 'len=' + briefLen + ' summary=' + hasSummary);

    // 2. Core 1—5
    const coreN = await count('#todayBrief .tb-core');
    add('Core Opportunities 1—5', coreN >= 1 && coreN <= 5, 'coreN=' + coreN);

    // 3. Action label 可见且为已知动作
    const actTexts = await page.$$eval('#todayBrief .act-badge', els => els.map(e => e.textContent.trim())).catch(() => []);
    const knownAct = actTexts.filter(t => KNOWN_ACTIONS.indexOf(t) >= 0);
    add('Action label 可见', actTexts.length > 0 && knownAct.length > 0,
      'badges=' + actTexts.length + ' known=' + knownAct.length + ' sample=' + (actTexts[0] || ''));

    // 4. 概率空免责声明可见
    const txt1 = await text();
    const hasProbDisc = /真实样本或校准证据不足，暂不展示/.test(txt1);
    add('概率空值免责声明可见', hasProbDisc, 'hasDisclaimer=' + hasProbDisc);

    // 5. 无伪百分比（校准概率 null 时不出现 "校准成功概率 xx%" / 0% / 100% / 历史成功率 / Opportunity/100）
    const fakePct = /校准成功概率[^\n]{0,20}\d{1,3}\s*%/.test(txt1) ||
                    /历史成功率/.test(txt1) || /Opportunity\s*\/\s*100/.test(txt1);
    const zeroOrHundred = /(^|\s)0\s*%|(^|\s)100\s*%/.test(txt1);
    add('无伪百分比/无0%/无100%', !fakePct && !zeroOrHundred,
      'fakePct=' + fakePct + ' zeroOrHundred=' + zeroOrHundred);

    // 6. Big Trend NOT_AVAILABLE：无虚构候选
    const btNa = await visible('#todayBrief .tb-bigtrend .tb-notavail');
    const btNaTxt = await page.$eval('#todayBrief .tb-bigtrend', e => e.innerText).catch(() => '');
    const btItems = await count('#todayBrief .tb-bt-item');
    add('Big Trend 未启用·无虚构候选', btNa && /正式算法尚未启用/.test(btNaTxt) && btItems === 0,
      'notAvail=' + btNa + ' items=' + btItems);

    // 7. Strategy evidence 不伪装真实战绩
    const stratTxt = await page.$eval('#todayBrief .tb-strat', e => e.innerText).catch(() => '');
    const stratFake = /胜率\s*\d+%/.test(stratTxt) || /年化.*\d+%/.test(stratTxt);
    add('Strategy 不伪装真实战绩', /暂不展示真实策略战绩/.test(stratTxt) && !stratFake,
      'hasDisclaimer=' + /暂不展示真实策略战绩/.test(stratTxt) + ' fake=' + stratFake);

    // 8. 持仓动作与新机会分开
    const coreN2 = await count('#todayBrief .tb-core');
    const holdN = await count('#todayBrief .tb-holding');
    const orderOk = await page.evaluate(() => {
      const c = document.querySelector('#todayBrief .tb-core');
      const h = document.querySelector('#todayBrief .tb-holding');
      if (!c || !h) return false;
      return c.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING;
    }).catch(() => false);
    add('持仓动作与新机会分开', coreN2 > 0 && holdN > 0 && orderOk,
      'core=' + coreN2 + ' holding=' + holdN + ' orderOk=' + orderOk);

    // 9. 无 [object Object]
    add('无 [object Object]', !/\[object Object\]/.test(txt1), 'found=' + /\[object Object\]/.test(txt1));
    if (!EXTERNAL_BASE) {
      add('仓位比例按 0-1 合同转为百分比', /建议仓位\s*10\.2%/.test(txt1),
        'contains10.2=' + /建议仓位\s*10\.2%/.test(txt1));
    }

    // 10. 无原始 JSON（未渲染字段名 / 裸对象）
    const rawJson = /calibrated_probability|probability_evidence_level|"action_state"|\{\s*"/.test(txt1);
    add('无原始 JSON 渲染', !rawJson, 'rawJson=' + rawJson);

    // 11. 无 pageerror / 无 JS 控制台错误
    add('无 pageerror', pageErrors.length === 0, 'pageErrors=' + pageErrors.length);
    add('无未捕获控制台错误', consoleErrors.length === 0, 'consoleErrors=' + consoleErrors.length);
    add('无未处理 Promise rejection', unhandledRejections.length === 0,
      'unhandledRejections=' + unhandledRejections.length);

    // 12. 移动端 390px 布局可用（无横向溢出 + 内容渲染）
    await page.setViewportSize({ width: 390, height: 844 });
    await sleep(500);
    const overflow390 = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    const coreMobile = await count('#todayBrief .tb-core');
    add('移动端 390px 布局可用', overflow390 <= 2 && coreMobile > 0,
      'overflowPx=' + overflow390 + ' core=' + coreMobile);

    // 13. 桌面端 1280px 布局可用
    await page.setViewportSize({ width: 1280, height: 900 });
    await sleep(400);
    const overflow1280 = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    const coreDesktop = await count('#todayBrief .tb-core');
    add('桌面端 1280px 布局可用', overflow1280 <= 2 && coreDesktop > 0,
      'overflowPx=' + overflow1280 + ' core=' + coreDesktop);

    // 截图留存（不阻断断言）
    await page.setViewportSize({ width: 1280, height: 900 });
    await sleep(300);
    const shotDir = path.join(ROOT, 'qa', 'shots');
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, 'today-qa.png'), fullPage: true }).catch(() => {});

  } catch (e) {
    add('FATAL', false, e.message);
    console.error(e.stack);
  } finally {
    // 防止 SSE（/api/stream 404 后 EventSource 重试）导致 browser.close() 挂起：
    // 给关闭加超时护栏，且始终显式 process.exit，避免进程因未关闭的 socket 不退出。
    const closeP = browser
      ? browser.close().catch(function () {}).then(function () { return 'closed'; })
      : Promise.resolve('no-browser');
    await Promise.race([closeP, new Promise(function (r) { setTimeout(r, 4000); })]);
    if (server) { try { server.close(); } catch (e) {} }
  }

  const failed = steps.filter(s => !s.ok);
  console.log('\n==== TODAY QA SUMMARY ====');
  console.log('total=' + steps.length + ' pass=' + (steps.length - failed.length) + ' fail=' + failed.length);
  if (consoleErrors.length) { console.log('CONSOLE ERRORS:'); consoleErrors.forEach(e => console.log('  > ' + e)); }
  if (pageErrors.length) { console.log('PAGE ERRORS:'); pageErrors.forEach(e => console.log('  > ' + e)); }
  if (failed.length) { failed.forEach(f => console.log('  FAIL: ' + f.step + ' | ' + f.detail)); process.exit(1); }
  console.log('ALL PASS');
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message, e.stack); process.exit(2); });
