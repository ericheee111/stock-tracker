'use strict';
const fs = require('node:fs');
const path = require('node:path');
const runtime = require('./wb3_runtime.cjs');

async function main() {
  let server, browser;
  let result = { exitCode: 2, status: 'RUNNER_ERROR', reason: 'NOT_STARTED' };
  const report = runtime.reportDirectory('viewport');
  const output = path.join(report, 'viewport-result.json');
  if (fs.existsSync(output)) throw new Error('REFUSE_OVERWRITE: ' + output);
  try {
    server = await runtime.startServer(process.env.WB3_SCENARIO || 'full');
    browser = await runtime.loadPlaywright().chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.goto(server.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    const meta = await page.evaluate(() => document.querySelector('meta[name="viewport"]')?.getAttribute('content'));
    const maxScale = /maximum-scale\s*=\s*([\d.]+)/i.exec(meta || '');
    const zoomable = !!meta && !/user-scalable\s*=\s*(no|0)(?:\s|,|$)/i.test(meta) &&
      (!maxScale || Number(maxScale[1]) > 1);
    result = { exitCode: zoomable ? 0 : 1, status: zoomable ? 'PASS' : 'FAILED',
      reason: zoomable ? 'ZOOM_ALLOWED' : 'VIEWPORT_LOCKED', meta, serverRunId: server.runId };
  } catch (error) {
    result = { exitCode: 2, status: 'RUNNER_ERROR', reason: 'VIEWPORT_RUNNER_ERROR', error: error.stack || String(error) };
  } finally {
    for (const cleanup of [() => browser && runtime.bounded(() => browser.close()), () => server && server.stop()]) {
      try { await cleanup(); }
      catch (error) {
        result.cleanupError = error.stack || String(error);
        if (result.exitCode === 0) Object.assign(result, {exitCode: 2, status: 'RUNNER_ERROR', reason: 'CLEANUP_ERROR'});
      }
    }
    fs.writeFileSync(output, JSON.stringify(result, null, 2), 'utf8');
    console.log('WB3_VIEWPORT_RESULT ' + JSON.stringify(result));
  }
  return result.exitCode;
}

module.exports = { main };
if (require.main === module) main().then(code => process.exit(code)).catch(error => {
  console.error('VIEWPORT_RUNNER_ERROR ' + (error.stack || error)); process.exit(2);
});
