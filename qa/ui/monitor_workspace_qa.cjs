'use strict';

const fs = require('fs');
const path = require('path');

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_CORE_PATH,
    path.join(__dirname, '..', 'node_modules', 'playwright-core'),
    'playwright-core'
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) { /* try next */ }
  }
  throw new Error('playwright-core is not installed; run npm ci under qa/')
}

const { chromium } = loadPlaywright();
const baseUrl = String(process.argv[2] || '').replace(/\/$/, '');
if (!/^http:\/\/(127\.0\.0\.1|localhost):[0-9]+$/.test(baseUrl)) {
  throw new Error('monitor QA requires a loopback HTTP base URL');
}

const results = [];
function check(name, passed, detail) {
  results.push({ name, passed: Boolean(passed), detail });
  const prefix = passed ? 'PASS' : 'FAIL';
  console.log(prefix + ' ' + name + ' | ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)));
}

async function waitMonitor(page) {
  await page.waitForFunction(() => window.Runtime && window.Runtime.snapshot().handshakeReady === true, null, { timeout: 10000 });
  await page.locator('.nav-btn[data-page="monitor"]').click();
  await page.locator('#page-monitor.active').waitFor({ state: 'visible', timeout: 8000 });
  await page.locator('#monitorWorkspace .mon-rail').waitFor({ state: 'visible', timeout: 8000 });
}

async function viewportAcceptance(browser, width, height) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const serverErrors = [];
  const externalRequests = [];
  const monitorWrites = [];
  const baseOrigin = new URL(baseUrl).origin;
  page.on('pageerror', error => pageErrors.push(String(error)));
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 500) {
      serverErrors.push({ status: response.status(), url: response.url() });
    }
  });
  page.on('request', request => {
    const url = request.url();
    if (/^https?:/.test(url) && new URL(url).origin !== baseOrigin) externalRequests.push(url);
    if (url.includes('/api/monitor/') && request.method() !== 'GET') {
      monitorWrites.push({ method: request.method(), url, body: request.postData() });
    }
  });

  await page.goto(baseUrl + '/', { waitUntil: 'domcontentloaded', timeout: 10000 });
  await waitMonitor(page);

  const snapshot = await page.evaluate(() => ({
    overflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
    activeNav: Boolean(document.querySelector('.nav-btn[data-page="monitor"].active')),
    metricCount: document.querySelectorAll('#monitorWorkspace .mon-metric').length,
    tabCount: document.querySelectorAll('#monitorWorkspace .mon-tab').length,
    bodyLength: (document.querySelector('#monitorWorkspace') || {}).textContent?.length || 0
  }));
  check(width + 'px monitor page is active', snapshot.activeNav, snapshot);
  check(width + 'px has no horizontal overflow', snapshot.overflowPx === 0, snapshot.overflowPx);
  check(width + 'px status rail is complete', snapshot.metricCount === 9, snapshot.metricCount);
  check(width + 'px workspace tabs are complete', snapshot.tabCount === 4, snapshot.tabCount);
  check(width + 'px monitor content is non-empty', snapshot.bodyLength > 100, snapshot.bodyLength);
  check(width + 'px has no pageerror', pageErrors.length === 0, pageErrors);
  check(width + 'px has no server 5xx response', serverErrors.length === 0, serverErrors);
  check(width + 'px has no unexpected console error', consoleErrors.length === 0, {
    consoleErrors,
    serverErrors
  });
  check(width + 'px has no external network dependency', externalRequests.length === 0, externalRequests);

  if (width === 1280) {
    const escapedInbox = await page.evaluate(() => {
      const inbox = document.querySelector('.mon-inbox-body h3');
      const snapshot = window.MonitorWorkspace && window.MonitorWorkspace.snapshot();
      const item = snapshot && snapshot.inbox && snapshot.inbox[0];
      const facts = item && item.evidence && item.evidence.facts;
      return {
        inboxText: inbox ? inbox.textContent : '',
        nestedMarkup: Boolean(document.querySelector('.mon-inbox-body h3 b')),
        ruleVersion: item && item.rule_version,
        historicalExact: item && item.rule_snapshot && item.rule_snapshot.historical_exact,
        conditionCount: facts && Array.isArray(facts.conditions) ? facts.conditions.length : 0,
        visibleMeta: document.querySelector('.mon-inbox-meta')?.textContent || ''
      };
    });
    check('Inbox API text is escaped rather than interpreted as HTML',
      escapedInbox.inboxText.includes('<b>') && !escapedInbox.nestedMarkup,
      escapedInbox);
    check('Inbox displays exact rule version and condition evidence',
      escapedInbox.ruleVersion === 1 && escapedInbox.historicalExact === true &&
        escapedInbox.conditionCount === 2 && /v1/.test(escapedInbox.visibleMeta),
      escapedInbox);

    const firstInbox = page.locator('.mon-inbox-row').first();
    const inboxId = await firstInbox.locator('[data-inbox-id]').first().getAttribute('data-inbox-id');
    await firstInbox.locator('[data-monitor-transition="ACKNOWLEDGED"]').evaluate(button => button.click());
    await page.waitForTimeout(1000);
    const ackState = await page.evaluate(id => {
      const snapshot = window.MonitorWorkspace && window.MonitorWorkspace.snapshot();
      const item = snapshot && snapshot.inbox && snapshot.inbox.find(row => row.inbox_id === id);
      return {
        state: item && item.state,
        error: snapshot && snapshot.error,
        visibleText: document.querySelector('#monitorWorkspace')?.textContent || ''
      };
    }, inboxId);
    ackState.monitorWrites = monitorWrites.slice();
    check('Inbox ACK transition succeeds through real private API', ackState.state === 'ACKNOWLEDGED', ackState);

    await page.locator('[data-monitor-tab="rules"]').click();
    await page.locator('#monitorRuleForm').waitFor({ state: 'visible' });
    const ruleBuilder = await page.evaluate(() => {
      const form = document.querySelector('#monitorRuleForm');
      const fact = form && form.elements.namedItem('fact');
      const options = fact ? Array.from(fact.options).map(option => option.value) : [];
      const ruleHeading = document.querySelector('.mon-rule-main h3');
      return {
        options,
        ruleText: ruleHeading ? ruleHeading.textContent : '',
        nestedMarkup: Boolean(document.querySelector('.mon-rule-main h3 b'))
      };
    });
    check('Rule API text is escaped rather than interpreted as HTML',
      ruleBuilder.ruleText.includes('<b>') && !ruleBuilder.nestedMarkup,
      ruleBuilder);
    check('Bounded rule builder exposes the frozen fact options',
      ruleBuilder.options.includes('market_event.last_price') &&
        ruleBuilder.options.includes('market_event.latency_p95_ms'),
      ruleBuilder.options);
    await page.evaluate(() => {
      const form = document.querySelector('#monitorRuleForm');
      form.elements.namedItem('name').value = 'QA Price Rule';
      form.elements.namedItem('symbol').value = '600519.SH';
      form.elements.namedItem('fact').value = 'market_event.last_price';
      form.elements.namedItem('operator').value = 'GE';
      form.elements.namedItem('value').value = '10';
      form.requestSubmit();
    });
    await page.waitForFunction(() => {
      const snapshot = window.MonitorWorkspace && window.MonitorWorkspace.snapshot();
      return Boolean(snapshot && snapshot.rules &&
        snapshot.rules.some(rule => rule.name === 'QA Price Rule'));
    }, null, { timeout: 8000 });
    check('Bounded rule builder persists through real API', true, 'QA Price Rule');

    await page.locator('[data-monitor-tab="data"]').click();
    await page.locator('.mon-latency-chart').waitFor({ state: 'visible', timeout: 8000 });
    const dataLink = await page.evaluate(() => {
      const integrityRow = Array.from(document.querySelectorAll('.mon-definition-list > div'))
        .find(row => row.querySelector('span')?.textContent === 'Integrity');
      return {
        chart: Boolean(document.querySelector('.mon-latency-chart')),
        boundaryCount: document.querySelectorAll('.mon-boundary-list li').length,
        integrity: integrityRow?.querySelector('strong')?.textContent || '',
        text: document.querySelector('#monitorWorkspace')?.textContent || ''
      };
    });
    check('Data-link latency chart initializes', dataLink.chart, dataLink);
    check('Latest event-store integrity evidence is visible', dataLink.integrity === 'PASSED', dataLink);
    check('Runtime monitor queue and drop evidence are visible',
      /Monitor worker/.test(dataLink.text) && /processed \/ dropped/.test(dataLink.text),
      dataLink.text.slice(0, 520));
    check('Data-link account/trading boundaries are visible', dataLink.boundaryCount === 5, dataLink.boundaryCount);
    check('Algorithm account remains unused in UI', /算法账户：不使用/.test(dataLink.text), dataLink.text.slice(0, 240));

    await page.locator('[data-monitor-tab="replay"]').click();
    await page.locator('#monitorReplayForm').waitFor({ state: 'visible' });
    await page.locator('#monitorReplayForm input[name="symbol"]').fill('600519.SH');
    await page.locator('#monitorReplayForm').evaluate(form => form.requestSubmit());
    await page.locator('.mon-replay-chart').waitFor({ state: 'visible', timeout: 8000 });
    const replayState = await page.evaluate(() => {
      const snapshot = window.MonitorWorkspace && window.MonitorWorkspace.snapshot();
      const replay = snapshot && snapshot.replay;
      return {
        chart: Boolean(document.querySelector('.mon-replay-chart')),
        text: document.querySelector('#monitorWorkspace')?.textContent || '',
        url: location.href,
        privateValueInUrl: /token|secret|password|access|authorization/i.test(location.search),
        rowCount: replay && replay.row_count,
        minuteBarCount: replay && Array.isArray(replay.minute_bars) ? replay.minute_bars.length : null,
        startAt: replay && replay.start_at,
        endAt: replay && replay.end_at
      };
    });
    check('Replay OHLC chart initializes from local event store', replayState.chart, replayState);
    check('Replay event rows and minute bars share the requested time window',
      /[1-9][0-9]* EVENTS/.test(replayState.text), replayState.text.slice(0, 320));
    check('Replay makes no win-rate or probability claim', !/(胜率|win\s*rate|success\s*probability)/i.test(replayState.text), replayState.text.slice(0, 240));
    check('No private access value enters URL', !replayState.privateValueInUrl, replayState.url);

    const queryGuard = await page.evaluate(() => {
      try {
        window.Runtime.apiUrlWithQuery('/api/monitor/inbox', { token: 'forbidden' });
        return false;
      } catch (error) {
        return error && error.code === 'RUNTIME_CONFIG_ERROR';
      }
    });
    check('Runtime query builder rejects sensitive query keys', queryGuard, queryGuard);
  }

  await context.close();
}

async function authFailureAcceptance(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.route('**/api/monitor/**', async route => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'PRIVATE_API_AUTH_REQUIRED', message: 'fixture auth required' } })
    });
  });
  await page.goto(baseUrl + '/', { waitUntil: 'domcontentloaded', timeout: 10000 });
  await page.locator('.nav-btn[data-page="monitor"]').click();
  await page.locator('#monitorWorkspace .mon-empty').waitFor({ state: 'visible', timeout: 8000 });
  const authState = await page.evaluate(() => ({
    text: document.querySelector('#monitorWorkspace')?.textContent || '',
    url: location.href,
    tokenInDom: /Bearer\s+[A-Za-z0-9_-]{16,}/.test(document.body.textContent || '')
  }));
  check('Monitor auth-required failure is explicit', /加载失败|auth required|fixture auth required/i.test(authState.text), authState.text);
  check('Auth failure does not expose bearer in DOM or URL', !authState.tokenInDom && !/token=|access=|authorization=/i.test(authState.url), authState);
  await context.close();
}

async function offlineAcceptance(browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.route('**/api/runtime/health', route => route.abort('failed'));
  await page.goto(baseUrl + '/', { waitUntil: 'domcontentloaded', timeout: 10000 });
  await page.waitForFunction(() => {
    if (!window.Runtime) return false;
    const snapshot = window.Runtime.snapshot();
    return window.Runtime.isHardFailure(snapshot.status);
  }, null, { timeout: 10000 });
  await page.locator('.nav-btn[data-page="monitor"]').click();
  await page.locator('#monitorWorkspace .mon-empty').waitFor({ state: 'visible', timeout: 8000 });
  const offline = await page.evaluate(() => {
    const snapshot = window.Runtime && window.Runtime.snapshot();
    return {
      text: document.querySelector('#monitorWorkspace')?.textContent || '',
      runtimeStatus: snapshot && snapshot.status,
      hardFailure: Boolean(window.Runtime && window.Runtime.isHardFailure(snapshot && snapshot.status)),
      overflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      executableClaims: document.querySelectorAll('#monitorWorkspace [data-action="EXECUTABLE"]').length
    };
  });
  check('Monitor offline boundary is explicit', offline.hardFailure && /暂不可用|离线|Runtime Health|Engine|加载失败/i.test(offline.text), offline);
  check('Offline monitor has no horizontal overflow', offline.overflow === 0, offline.overflow);
  check('Offline monitor contains no executable action', offline.executableClaims === 0, offline.executableClaims);
  check('Offline monitor has no pageerror', errors.length === 0, errors);
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  try {
    await viewportAcceptance(browser, 390, 844);
    await viewportAcceptance(browser, 768, 900);
    await viewportAcceptance(browser, 1280, 900);
    await authFailureAcceptance(browser);
    await offlineAcceptance(browser);
  } finally {
    await browser.close();
  }
  const failed = results.filter(result => !result.passed);
  console.log('\n==== MONITOR WORKSPACE QA SUMMARY ====');
  console.log('total=' + results.length + ' pass=' + (results.length - failed.length) + ' fail=' + failed.length);
  if (failed.length) process.exit(1);
})().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
