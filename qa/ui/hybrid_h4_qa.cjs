/* Hybrid H4 generated-static-site browser acceptance. */
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
const WEB_BASE = process.env.H4_WEB_BASE_URL;
const API_ORIGIN = process.env.H4_API_ORIGIN;
const OFFLINE_WEB_BASE = process.env.H4_OFFLINE_WEB_BASE_URL;
const OFFLINE_API_ORIGIN = process.env.H4_OFFLINE_API_ORIGIN;
const PRIVATE_ACCESS = process.env.H4_PRIVATE_ACCESS;
const EXPECTED_ENGINE = process.env.H4_EXPECTED_ENGINE;
const EXPECTED_BUILD = process.env.H4_EXPECTED_BUILD;

if (!WEB_BASE || !API_ORIGIN || !OFFLINE_WEB_BASE || !OFFLINE_API_ORIGIN ||
    !PRIVATE_ACCESS || !EXPECTED_ENGINE || !EXPECTED_BUILD) {
  console.error('H4 acceptance environment is incomplete');
  process.exit(2);
}

const results = [];
function check(name, passed, detail) {
  const record = { name, passed: Boolean(passed), detail: String(detail || '') };
  results.push(record);
  console.log((passed ? 'PASS ' : 'FAIL ') + name + (detail ? ' | ' + detail : ''));
  if (!passed) throw new Error(name + ': ' + detail);
}
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function onlineScenario(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const requests = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !/favicon|Failed to load resource/i.test(message.text())) {
      consoleErrors.push(message.text());
    }
  });
  page.on('request', request => {
    if (request.url().startsWith(API_ORIGIN + '/api/')) {
      requests.push({
        method: request.method(),
        pathname: new URL(request.url()).pathname,
        auth: Boolean(request.headers()['authorization'])
      });
    }
  });

  try {
    const navigation = await page.goto(WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    const headers = navigation ? navigation.headers() : {};
    check('Cloudflare-style CSP response header is present and exact',
      Boolean(headers['content-security-policy']) &&
        headers['content-security-policy'].includes("connect-src 'self' " + API_ORIGIN) &&
        !headers['content-security-policy'].includes('connect-src *'),
      headers['content-security-policy']);
    check('Referrer and MIME hardening headers are present',
      headers['referrer-policy'] === 'no-referrer' &&
        headers['x-content-type-options'] === 'nosniff',
      JSON.stringify(headers));

    await page.waitForSelector('#runtimeStatus[data-runtime-state="AUTH_REQUIRED"]', {
      timeout: 15000
    });
    let snapshot = await page.evaluate(() => window.Runtime.snapshot());
    check('Generated Runtime Config targets exact API/Engine/Build',
      snapshot.apiOrigin === API_ORIGIN && snapshot.health.engine_id === EXPECTED_ENGINE &&
        snapshot.frontendBuild === EXPECTED_BUILD && snapshot.handshakeReady === true,
      JSON.stringify(snapshot));
    const staticContract = await page.evaluate(async () => {
      const manifest = await (await fetch('/deployment-manifest.json', { cache: 'no-store' })).json();
      const runtimeText = await (await fetch('/runtime-config.js', { cache: 'no-store' })).text();
      const meta = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
      return {
        manifest,
        runtimeText,
        metaCsp: meta && meta.content,
        referrer: document.querySelector('meta[name="referrer"]')?.content || ''
      };
    });
    check('Static manifest and meta policy are no-secret',
      staticContract.manifest.contains_private_access === false &&
        !staticContract.runtimeText.includes(PRIVATE_ACCESS) &&
        staticContract.metaCsp.includes(API_ORIGIN) &&
        !staticContract.metaCsp.includes('connect-src *') &&
        staticContract.referrer === 'no-referrer',
      JSON.stringify({ manifest: staticContract.manifest, metaCsp: staticContract.metaCsp }));

    const noAuthBeforeHandshake = requests
      .filter(item => item.pathname === '/api/runtime/health')
      .every(item => item.auth === false);
    check('Runtime Health never carries Authorization', noAuthBeforeHandshake, JSON.stringify(requests));

    const authRace = await page.evaluate(async ({ token }) => {
      window.API.clearPrivateAccess();
      const staleRequest = window.API.getPortfolio().then(
        () => ({ status: 200, code: 'UNEXPECTED_SUCCESS' }),
        error => ({ status: error && error.status || 0, code: error && error.code || '' })
      );
      window.API.setPrivateAccess(token);
      const staleResult = await staleRequest;
      const finalPortfolio = await window.API.getPortfolio();
      return {
        staleResult,
        retained: window.API.privateAccessValue() === token,
        finalSchema: finalPortfolio && finalPortfolio.schema_version,
        runtime: window.Runtime.snapshot()
      };
    }, { token: PRIVATE_ACCESS });
    check('Late 401 from an old no-token request cannot overwrite a newer session value',
      authRace.staleResult.status === 401 && authRace.retained === true &&
        authRace.finalSchema === 'stage1-v1',
      JSON.stringify(authRace));

    const crud = await page.evaluate(async ({ token }) => {
      try {
        window.API.setPrivateAccess(token);
        const portfolio = await window.API.getPortfolio();
        const profile = await window.API.requestJSON('/api/portfolio/profile', {
          method: 'PUT', private: true,
          body: {
            account_equity: 120000, available_cash: 60000, risk_mode: 'BALANCED',
            per_trade_risk_pct: 0.007, max_position_pct: 0.2,
            max_portfolio_heat_pct: 0.08, max_sector_pct: 0.35, max_theme_pct: 0.35
          }
        });
        const created = await window.API.requestJSON('/api/portfolio/positions', {
          method: 'POST', private: true,
          body: {
            symbol: '600000.SH', market: 'A', shares: 37, average_cost: 10,
            added_at: new Date().toISOString()
          }
        });
        const id = created.id || created.position?.id;
        const patched = await window.API.requestJSON('/api/portfolio/positions/' + encodeURIComponent(id), {
          method: 'PATCH', private: true,
          body: { shares: 13 }
        });
        const removed = await window.API.requestJSON('/api/portfolio/positions/' + encodeURIComponent(id), {
          method: 'DELETE', private: true
        });
        window.SSE.reconnect();
        const deadline = Date.now() + 10000;
        while (!window.SSE.isConnected() && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        return { portfolio, profile, created, patched, removed, sse: window.SSE.isConnected() };
      } catch (error) {
        return {
          error: {
            name: error && error.name || '',
            message: error && error.message || '',
            status: error && error.status || 0,
            code: error && error.code || '',
            field: error && error.field || null,
            url: error && error.url || ''
          },
          runtime: window.Runtime.snapshot(),
          hasPrivateAccess: window.API.hasPrivateAccess()
        };
      }
    }, { token: PRIVATE_ACCESS });
    check('Generated static site completes cross-origin Portfolio CRUD',
      !crud.error && crud.profile.account_equity === 120000 && crud.created.shares === 37 &&
        crud.patched.shares === 13 && Boolean(crud.removed),
      JSON.stringify(crud));
    check('Generated static site connects header-authenticated SSE', crud.sse, JSON.stringify(crud));

    snapshot = await page.evaluate(() => window.Runtime.snapshot());
    check('Online H4 runtime identity remains ready',
      snapshot.handshakeReady === true && snapshot.status === 'ONLINE' && snapshot.sseConnected === true,
      JSON.stringify(snapshot));
    const body = await page.textContent('body');
    check('Private access is absent from DOM and URL',
      !body.includes(PRIVATE_ACCESS) && !page.url().includes(PRIVATE_ACCESS),
      'DOM/URL checked');

    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await sleep(100);
      const overflow = await page.evaluate(() =>
        Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth));
      check('H4 layout has no horizontal overflow at ' + width + 'px', overflow === 0, String(overflow));
    }
    check('Online H4 has no pageerror', pageErrors.length === 0, JSON.stringify(pageErrors));
    check('Online H4 has no unexpected console error', consoleErrors.length === 0, JSON.stringify(consoleErrors));
  } finally {
    await context.close();
  }
}

async function offlineScenario(browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  const requests = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('request', request => {
    if (request.url().startsWith(OFFLINE_API_ORIGIN + '/api/')) {
      requests.push(new URL(request.url()).pathname);
    }
  });
  try {
    await page.goto(OFFLINE_WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state="ENGINE_OFFLINE"]', {
      timeout: 20000
    });
    const snapshot = await page.evaluate(() => window.Runtime.snapshot());
    check('Static site loads while Local Engine is offline',
      snapshot.status === 'ENGINE_OFFLINE' && snapshot.handshakeReady === false,
      JSON.stringify(snapshot));
    const shell = await page.evaluate(() => ({
      bodyLength: document.body.innerText.length,
      overflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      actionCount: document.querySelectorAll('[data-action-state="EXECUTABLE"]').length,
      url: location.href
    }));
    check('Offline static shell remains usable without stale executable actions',
      shell.bodyLength > 100 && shell.overflow === 0 && shell.actionCount === 0,
      JSON.stringify(shell));
    check('Offline classification only probes Runtime Health boundary',
      requests.length >= 1 && requests.every(path => path === '/api/runtime/health'),
      JSON.stringify(requests));
    check('Offline H4 has no pageerror', pageErrors.length === 0, JSON.stringify(pageErrors));
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--no-proxy-server']
  });
  try {
    await onlineScenario(browser);
    await offlineScenario(browser);
  } finally {
    await browser.close();
  }
  const failed = results.filter(item => !item.passed);
  console.log('\n==== HYBRID H4 QA SUMMARY ====');
  console.log('total=' + results.length + ' pass=' + (results.length - failed.length) + ' fail=' + failed.length);
  process.exitCode = failed.length ? 1 : 0;
})().catch(error => {
  console.error('FATAL ' + (error && error.stack || error));
  process.exit(2);
});
