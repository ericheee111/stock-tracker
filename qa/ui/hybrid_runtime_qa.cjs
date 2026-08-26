/* Hybrid H1/H2 cross-origin browser acceptance. */
const path = require('path');

function loadPlaywright() {
  try { return require('playwright'); } catch (error) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright'
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) {}
  }
  throw new Error('playwright is not available');
}

const { chromium } = loadPlaywright();
const WEB_BASE = process.env.HYBRID_WEB_BASE_URL;
const API_ORIGIN = process.env.HYBRID_API_ORIGIN;
const PRIVATE_ACCESS = process.env.HYBRID_PRIVATE_ACCESS;
const EXPECTED_ENGINE = process.env.HYBRID_EXPECTED_ENGINE || 'hybrid-h1-h2-browser-fixture';
const EXPECTED_COMMIT = process.env.HYBRID_EXPECTED_COMMIT || 'hybrid-h1-h2-fixture-commit';

if (!WEB_BASE || !API_ORIGIN || !PRIVATE_ACCESS) {
  console.error('HYBRID_WEB_BASE_URL, HYBRID_API_ORIGIN and HYBRID_PRIVATE_ACCESS are required');
  process.exit(2);
}

const results = [];
function check(name, passed, detail) {
  results.push({ name, passed: Boolean(passed), detail: String(detail || '') });
  console.log((passed ? 'PASS ' : 'FAIL ') + name + ' | ' + String(detail || ''));
}
function assert(name, condition, detail) {
  check(name, condition, detail);
  if (!condition) throw new Error(name + ': ' + detail);
}
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function mismatchScenario(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const privateRequests = [];
  const authorizationRequests = [];
  page.on('request', request => {
    if (!request.url().startsWith(API_ORIGIN + '/api/')) return;
    const pathname = new URL(request.url()).pathname;
    if (!['/api/runtime/health'].includes(pathname)) privateRequests.push(pathname);
    if (request.headers()['authorization']) authorizationRequests.push(pathname);
  });
  await page.route('**/api/runtime/health', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    headers: {
      'Access-Control-Allow-Origin': WEB_BASE,
      'Vary': 'Origin',
      'Cache-Control': 'no-store'
    },
    body: JSON.stringify({
      schema_version: 'hybrid-runtime-v1',
      status: 'ONLINE',
      engine_id: EXPECTED_ENGINE,
      engine_version: '1.1.0',
      commit_id: EXPECTED_COMMIT,
      deployment_mode: 'HYBRID_PRIVATE',
      started_at: new Date().toISOString(),
      last_heartbeat_at: new Date().toISOString(),
      last_collection_at: new Date().toISOString(),
      data_as_of: new Date().toISOString(),
      data_status: 'LIVE',
      scheduler_state: 'RUNNING',
      provider_summary: { count: 1, closed: 1, half_open: 0, open: 0 },
      database_state: 'READY',
      sse_available: true,
      api_major: 99
    })
  }));
  try {
    await page.goto(WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state="API_VERSION_MISMATCH"]', {
      timeout: 10000
    });
    await sleep(500);
    const snapshot = await page.evaluate(() => window.Runtime.snapshot());
    assert('API Major mismatch is a hard block',
      snapshot.status === 'API_VERSION_MISMATCH' && snapshot.handshakeReady === false,
      JSON.stringify(snapshot));
    assert('No private/public data request follows version mismatch',
      privateRequests.length === 0,
      JSON.stringify(privateRequests));
    assert('No Authorization header is sent before handshake',
      authorizationRequests.length === 0,
      JSON.stringify(authorizationRequests));
  } finally {
    await context.close();
  }
}

async function liveScenario(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addInitScript(({ apiOrigin }) => {
    const oldOrigin = 'https://old-engine.example';
    sessionStorage.setItem('stockTrackerPrivateAccess', 'legacy-private-access-0123456789abcdef');
    sessionStorage.setItem('stockTrackerPrivateAccessOrigin', oldOrigin);
    sessionStorage.setItem(
      'stockTrackerPrivateAccess::' + encodeURIComponent(oldOrigin),
      'old-origin-private-access-0123456789abcdef'
    );
    sessionStorage.setItem(
      'stockTrackerPrivateAccess::' + encodeURIComponent(apiOrigin),
      'stale-current-origin-private-access-0123456789abcdef'
    );
  }, { apiOrigin: API_ORIGIN });
  const page = await context.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const apiRequests = [];
  const apiResponses = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('request', request => {
    if (request.url().startsWith(API_ORIGIN + '/api/')) {
      apiRequests.push({
        method: request.method(),
        pathname: new URL(request.url()).pathname,
        hasAuthorization: Boolean(request.headers()['authorization'])
      });
    }
  });
  page.on('response', response => {
    if (response.url().startsWith(API_ORIGIN + '/api/')) {
      apiResponses.push({
        status: response.status(),
        pathname: new URL(response.url()).pathname
      });
    }
  });

  try {
    await page.goto(WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state="AUTH_REQUIRED"]', {
      timeout: 15000
    });

    let snapshot = await page.evaluate(() => window.Runtime.snapshot());
    assert('Runtime Health handshake succeeds cross-origin',
      snapshot.handshakeReady === true && snapshot.health.engine_id === EXPECTED_ENGINE,
      JSON.stringify(snapshot));
    assert('Runtime uses configured API Origin',
      snapshot.apiOrigin === API_ORIGIN,
      snapshot.apiOrigin);
    const strictRuntimeInputs = await page.evaluate(() => {
      let remoteHttpRejected = false;
      let queryPathRejected = false;
      let dotSegmentRejected = false;
      try { window.Runtime.normalizeOrigin('http://engine.example'); } catch (error) {
        remoteHttpRejected = error && error.code === 'RUNTIME_CONFIG_ERROR';
      }
      try { window.Runtime.normalizeApiPath('/api/portfolio?token=bad'); } catch (error) {
        queryPathRejected = error && error.code === 'RUNTIME_CONFIG_ERROR';
      }
      try { window.Runtime.apiUrl('/api/../portfolio'); } catch (error) {
        dotSegmentRejected = error && error.code === 'RUNTIME_CONFIG_ERROR';
      }
      const hardened = window.Runtime.secureFetchOptions({
        mode: 'no-cors',
        credentials: 'include',
        redirect: 'follow',
        referrerPolicy: 'unsafe-url',
        cache: 'force-cache'
      });
      const secureOptionsPinned = hardened.mode === 'cors' &&
        hardened.credentials === 'omit' && hardened.redirect === 'error' &&
        hardened.referrerPolicy === 'no-referrer' && hardened.cache === 'no-store';
      return { remoteHttpRejected, queryPathRejected, dotSegmentRejected, secureOptionsPinned };
    });
    assert('Runtime rejects unsafe URLs and pins private fetch security options',
      strictRuntimeInputs.remoteHttpRejected && strictRuntimeInputs.queryPathRejected &&
        strictRuntimeInputs.dotSegmentRejected && strictRuntimeInputs.secureOptionsPinned,
      JSON.stringify(strictRuntimeInputs));
    assert('Private calls without token become AUTH_REQUIRED',
      snapshot.authState === 'AUTH_REQUIRED',
      JSON.stringify(snapshot));

    const streamsBeforeAccess = apiRequests.filter(item => item.pathname === '/api/stream');
    assert('SSE is not started before private data access succeeds',
      streamsBeforeAccess.length === 0,
      JSON.stringify(streamsBeforeAccess));

    const originScopedBefore = await page.evaluate(apiOrigin => {
      const oldOrigin = 'https://old-engine.example';
      return {
        generic: sessionStorage.getItem('stockTrackerPrivateAccess'),
        activeOrigin: sessionStorage.getItem('stockTrackerPrivateAccessOrigin'),
        oldScoped: sessionStorage.getItem(
          'stockTrackerPrivateAccess::' + encodeURIComponent(oldOrigin)
        ),
        currentScoped: sessionStorage.getItem(
          'stockTrackerPrivateAccess::' + encodeURIComponent(apiOrigin)
        ),
        keys: Object.keys(sessionStorage)
      };
    }, API_ORIGIN);
    assert('Legacy generic token key is absent', originScopedBefore.generic === null,
      JSON.stringify(originScopedBefore));
    assert('Origin change clears previous and current scoped tokens',
      originScopedBefore.oldScoped === null && originScopedBefore.currentScoped === null &&
        originScopedBefore.activeOrigin === API_ORIGIN,
      JSON.stringify(originScopedBefore));

    await page.evaluate(access => window.API.setPrivateAccess(access), PRIVATE_ACCESS);
    const portfolio = await page.evaluate(() => window.API.getPortfolio());
    assert('Exact bearer restores private API access',
      Boolean(portfolio && Array.isArray(portfolio.positions)),
      JSON.stringify(portfolio));
    await page.waitForSelector('#runtimeStatus[data-runtime-state="ONLINE"]', {
      timeout: 10000
    });

    const profile = await page.evaluate(() => window.API.putPortfolioProfile({
      account_equity: 120000,
      available_cash: 60000,
      risk_mode: 'BALANCED',
      per_trade_risk_pct: 0.007,
      max_position_pct: 0.20,
      max_portfolio_heat_pct: 0.08,
      max_sector_pct: 0.35,
      max_theme_pct: 0.35
    }));
    assert('Cross-origin profile PUT passes preflight and persists',
      Number(profile.account_equity) === 120000,
      JSON.stringify(profile));

    const created = await page.evaluate(() => window.API.createPortfolioPosition({
      symbol: '600000.SH',
      market: 'A',
      shares: 37,
      average_cost: 10.0,
      added_at: new Date().toISOString()
    }));
    const positionId = created && created.id;
    assert('Cross-origin position POST succeeds', Boolean(positionId), JSON.stringify(created));

    const patched = await page.evaluate(id => window.API.patchPortfolioPosition(id, {
      shares: 13
    }), positionId);
    assert('Cross-origin position PATCH succeeds',
      Number(patched.shares) === 13,
      JSON.stringify(patched));

    const removed = await page.evaluate(id => window.API.deletePortfolioPosition(id), positionId);
    const finalPortfolio = await page.evaluate(() => window.API.getPortfolio());
    const finalPositions = Array.isArray(finalPortfolio.positions) ? finalPortfolio.positions : [];
    assert('Cross-origin position DELETE succeeds',
      Boolean(removed && removed.ok) && !finalPositions.some(item => item.id === positionId),
      JSON.stringify({ removed, finalPositions }));

    await page.evaluate(() => window.SSE.reconnect());
    await page.waitForFunction(() => window.Runtime.snapshot().sseConnected === true, null, {
      timeout: 10000
    });
    snapshot = await page.evaluate(() => window.Runtime.snapshot());
    assert('Header-authenticated cross-origin fetch-stream connects',
      snapshot.sseConnected === true,
      JSON.stringify(snapshot));

    const storage = await page.evaluate(apiOrigin => {
      const scopedKey = 'stockTrackerPrivateAccess::' + encodeURIComponent(apiOrigin);
      return {
        scopedKey: scopedKey,
        scopedValue: sessionStorage.getItem(scopedKey),
        activeOrigin: sessionStorage.getItem('stockTrackerPrivateAccessOrigin'),
        generic: sessionStorage.getItem('stockTrackerPrivateAccess'),
        body: document.body.innerText,
        href: location.href,
        apiUrl: window.API.apiUrl('/api/portfolio')
      };
    }, API_ORIGIN);
    assert('Token is scoped to normalized API Origin',
      storage.scopedValue === PRIVATE_ACCESS && storage.activeOrigin === API_ORIGIN,
      JSON.stringify({ scopedKey: storage.scopedKey, activeOrigin: storage.activeOrigin }));
    assert('Token is absent from DOM and URL',
      storage.body.indexOf(PRIVATE_ACCESS) === -1 && storage.href.indexOf(PRIVATE_ACCESS) === -1,
      'body/url checked');
    assert('Unified URL builder targets configured Origin',
      storage.apiUrl === API_ORIGIN + '/api/portfolio',
      storage.apiUrl);

    const authorized = apiRequests.filter(item => item.hasAuthorization);
    const writeMethods = new Set(apiRequests
      .filter(item => ['/api/portfolio/profile', '/api/portfolio/positions'].some(prefix =>
        item.pathname.indexOf(prefix) === 0))
      .map(item => item.method));
    assert('Cross-origin browser writes complete through CORS',
      ['PUT', 'POST', 'PATCH', 'DELETE'].every(method => writeMethods.has(method)),
      JSON.stringify(Array.from(writeMethods)));
    assert('Bearer is sent only after successful handshake', authorized.length >= 4,
      JSON.stringify(authorized));

    for (const viewport of [{ width: 390, height: 844 }, { width: 1280, height: 900 }]) {
      await page.setViewportSize(viewport);
      await sleep(250);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert('No horizontal overflow at ' + viewport.width + 'px', overflow <= 0, String(overflow));
    }

    await page.evaluate(() => {
      window.SSE.close();
      window.API.clearPrivateAccess();
    });
    const streamCountBefore = apiRequests.filter(item => item.pathname === '/api/stream').length;
    await page.evaluate(() => window.SSE.reconnect());
    await page.waitForFunction(() => window.SSE.isAuthBlocked() === true, null, {
      timeout: 10000
    });
    await sleep(1600);
    const streamCountAfter = apiRequests.filter(item => item.pathname === '/api/stream').length;
    snapshot = await page.evaluate(() => window.Runtime.snapshot());
    assert('SSE 401 becomes AUTH_REQUIRED',
      snapshot.authState === 'AUTH_REQUIRED',
      JSON.stringify(snapshot));
    assert('SSE auth failure does not hot-retry',
      streamCountAfter - streamCountBefore === 1,
      String(streamCountAfter - streamCountBefore));

    assert('No pageerror', pageErrors.length === 0, JSON.stringify(pageErrors));
    const serverErrors = apiResponses.filter(item => item.status >= 500);
    assert('No unexpected API 5xx response', serverErrors.length === 0,
      JSON.stringify(serverErrors));
    const unexpectedConsoleErrors = consoleErrors.filter(text =>
      !/Failed to load resource: the server responded with a status of 401 \(Unauthorized\)/i.test(text));
    assert('No unexpected console error', unexpectedConsoleErrors.length === 0,
      JSON.stringify(unexpectedConsoleErrors));
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: [
      '--no-sandbox',
      '--no-proxy-server'
    ]
  });
  try {
    await mismatchScenario(browser);
    await liveScenario(browser);
  } finally {
    await browser.close();
  }
  const failed = results.filter(result => !result.passed);
  console.log('\n==== HYBRID H1/H2 QA SUMMARY ====');
  console.log('total=' + results.length + ' pass=' + (results.length - failed.length) + ' fail=' + failed.length);
  process.exit(failed.length ? 1 : 0);
})().catch(error => {
  console.error('FATAL ' + (error && error.stack || error));
  process.exit(2);
});
