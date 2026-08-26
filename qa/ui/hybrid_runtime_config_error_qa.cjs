/* Hybrid H1/H2 fail-closed Runtime Config acceptance. */
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
const FORBIDDEN_VALUE = 'forbidden-runtime-config-value';

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
  const page = await context.newPage();
  const apiRequests = [];
  page.on('request', request => {
    if (request.url().startsWith(API_ORIGIN + '/api/')) {
      apiRequests.push(new URL(request.url()).pathname);
    }
  });
  await page.route('**/runtime-config.js', route => route.fulfill({
    status: 200,
    contentType: 'application/javascript; charset=utf-8',
    body: 'window.STOCK_TRACKER_RUNTIME=Object.freeze({' +
      'deploymentMode:"HYBRID_PRIVATE",' +
      'apiBaseUrl:' + JSON.stringify(API_ORIGIN) + ',' +
      'allowedApiOrigins:[' + JSON.stringify(API_ORIGIN) + '],' +
      'ssePath:"/api/stream",frontendBuild:"development",' +
      'expectedApiMajor:1,expectedEngineId:"hybrid-h1-h2-browser-fixture",' +
      'allowApiOriginOverride:false,allowPrivateBrowserCache:false,healthPollMs:15000,' +
      'privateAccess:' + JSON.stringify(FORBIDDEN_VALUE) +
      '});'
  }));

  try {
    await page.goto(WEB_BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#runtimeStatus[data-runtime-state="RUNTIME_CONFIG_ERROR"]', {
      timeout: 10000
    });
    await new Promise(resolve => setTimeout(resolve, 300));
    const snapshot = await page.evaluate(() => window.Runtime.snapshot());
    const blocked = snapshot.status === 'RUNTIME_CONFIG_ERROR' &&
      snapshot.handshakeReady === false;
    console.log((blocked ? 'PASS' : 'FAIL') +
      ' Unknown Runtime Config field fails closed | ' + JSON.stringify(snapshot));
    const noApi = apiRequests.length === 0;
    console.log((noApi ? 'PASS' : 'FAIL') +
      ' Invalid Runtime Config blocks all API requests | ' + JSON.stringify(apiRequests));
    const noValueEcho = snapshot.detail.indexOf(FORBIDDEN_VALUE) === -1 &&
      (await page.textContent('body')).indexOf(FORBIDDEN_VALUE) === -1;
    console.log((noValueEcho ? 'PASS' : 'FAIL') +
      ' Invalid config value is not echoed into UI');
    process.exitCode = blocked && noApi && noValueEcho ? 0 : 1;
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => {
  console.error('FATAL ' + (error && error.stack || error));
  process.exit(2);
});
