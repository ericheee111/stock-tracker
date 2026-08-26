/* Hybrid H1/H2 malformed Runtime Health acceptance. */
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
const EXPECTED_ENGINE = process.env.HYBRID_EXPECTED_ENGINE || 'hybrid-h1-h2-browser-fixture';
const EXPECTED_COMMIT = process.env.HYBRID_EXPECTED_COMMIT || 'hybrid-h1-h2-fixture-commit';

if (!WEB_BASE || !API_ORIGIN) {
  console.error('HYBRID_WEB_BASE_URL and HYBRID_API_ORIGIN are required');
  process.exit(2);
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--no-proxy-server']
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addInitScript(({ apiOrigin }) => {
    sessionStorage.setItem('stockTrackerPrivateAccessOrigin', apiOrigin);
    sessionStorage.setItem(
      'stockTrackerPrivateAccess::' + encodeURIComponent(apiOrigin),
      'invalid-health-private-access-0123456789abcdef'
    );
  }, { apiOrigin: API_ORIGIN });
  const page = await context.newPage();
  const laterRequests = [];
  page.on('request', request => {
    if (!request.url().startsWith(API_ORIGIN + '/api/')) return;
    const pathname = new URL(request.url()).pathname;
    if (pathname !== '/api/runtime/health') laterRequests.push(pathname);
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
      provider_summary: { count: 1, closed: 2, half_open: 0, open: 0 },
      database_state: 'READY',
      sse_available: true,
      api_major: 1
    })
  }));

  try {
    await page.goto(WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state="RUNTIME_HEALTH_INVALID"]', {
      timeout: 10000
    });
    await new Promise(resolve => setTimeout(resolve, 500));
    const snapshot = await page.evaluate(() => window.Runtime.snapshot());
    const blocked = snapshot.status === 'RUNTIME_HEALTH_INVALID' &&
      snapshot.handshakeReady === false && snapshot.health === null;
    console.log((blocked ? 'PASS' : 'FAIL') +
      ' Invalid Runtime Health is a hard block | ' + JSON.stringify(snapshot));
    const tokenCleared = await page.evaluate(apiOrigin =>
      sessionStorage.getItem('stockTrackerPrivateAccess::' + encodeURIComponent(apiOrigin)) === null,
      API_ORIGIN);
    console.log((tokenCleared ? 'PASS' : 'FAIL') +
      ' Invalid Runtime Health clears the scoped access value');
    const noFollowup = laterRequests.length === 0;
    console.log((noFollowup ? 'PASS' : 'FAIL') +
      ' Invalid Runtime Health blocks later API requests | ' + JSON.stringify(laterRequests));
    process.exitCode = blocked && tokenCleared && noFollowup ? 0 : 1;
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => {
  console.error('FATAL ' + (error && error.stack || error));
  process.exit(2);
});
