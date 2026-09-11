/* =========================================================================
 * wb3_probe_markets.cjs —— WB3-01 marketsHaveData 边界探针（走真实前端代码路径）
 * 通过 route 拦截 /api/markets（构造 dict/array/metadata/非法 market）与
 * /api/overview（500，令 meta=null、runtime 保持 ONLINE），观察横幅是否误报
 * 「连接后端失败」。marketsHaveData 为 app.js 内部函数，只能通过其真实消费路径验证。
 * 断言：非法市场形状（false/'bad'/NOT_A_MARKET/仅元数据）→ 视为无数据（横幅 error）；
 *       合法 dict/array → 视为有数据（横幅不 error）。
 * 运行：node qa/wb3/wb3_probe_markets.cjs；退出 0=全绿 1=失败。
 * ========================================================================= */
'use strict';
const runtime=require('./wb3_runtime.cjs');
let server;
const VALID_DICT = { a: { index: { symbol: '000001.SH', name: '上证指数', last: 3128.43, change_pct: 0.62, data_status: 'LIVE' } } };
const VALID_ARRAY = [{ market: 'A', index: { symbol: '000001.SH', name: '上证指数', last: 3128.43 } }];

const cases = [
  { name: 'dict {a:false}', markets: { a: false }, expectData: false },
  { name: 'dict {a:"bad"}', markets: { a: 'bad' }, expectData: false },
  { name: 'array [{market:NOT_A_MARKET}]', markets: [{ market: 'NOT_A_MARKET' }], expectData: false },
  { name: 'invalid market with index', markets: [{market:'NOT_A_MARKET',index:{}}], expectData:false },
  { name: 'dict key and market conflict', markets: {a:{market:'US',index:{}}}, expectData:false },
  { name: 'array missing market', markets: [{index:{}}], expectData:false },
  { name: 'metadata only {observed_age_ms}', markets: { observed_age_ms: 12000 }, expectData: false },
  { name: 'valid dict {a:{index}}', markets: VALID_DICT, expectData: true },
  { name: 'valid array [{market:A,index}]', markets: VALID_ARRAY, expectData: true }
];

let pass = 0, fail = 0;
const failures = [];

async function probeCase(browser, c) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await ctx.addInitScript(() => {
    try { sessionStorage.setItem('stockTrackerPrivateAccess::' + encodeURIComponent(''), 'wb3-synthetic-private-access-value-0123456789abcdef'); } catch (e) {}
    try { sessionStorage.setItem('stockTrackerPrivateAccess', 'wb3-synthetic-private-access-value-0123456789abcdef'); } catch (e) {}
  });
  const page = await ctx.newPage();
  await page.route('**/api/overview', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { code: 'INTERNAL', message: 'probe' } }) }));
  await page.route('**/api/markets', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ markets: c.markets }) }));
  await page.goto(server.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.waitForSelector('#runtimeStatus[data-runtime-state]', { timeout: 12000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const bannerText = await page.evaluate(() => (document.querySelector('#banner') ? document.querySelector('#banner').innerText : ''));
  const bannerIsError = bannerText.indexOf('连接后端失败') !== -1;
  await ctx.close();
  return { name: c.name, bannerIsError: bannerIsError, bannerText: bannerText.slice(0, 60) };
}

async function run() {
  server=await runtime.startServer('full');
  let browser;
  try {
  browser = await runtime.loadPlaywright().chromium.launch({headless:true});
  for (const c of cases) {
    let r;
    try { r = await probeCase(browser, c); } catch (e) { r = { name: c.name, error: true, bannerIsError: false, bannerText: 'PROBE ERROR ' + (e && e.message || e) }; }
    const ok = !r.error && (r.bannerIsError === !c.expectData);  // 无数据→error 横幅；有数据→非 error
    if (ok) { pass += 1; console.log('PASS ' + c.name + ' | banner=' + r.bannerText); }
    else { fail += 1; failures.push(c.name + ' expectData=' + c.expectData + ' bannerIsError=' + r.bannerIsError + ' text=' + r.bannerText); console.log('FAIL ' + c.name + ' | ' + r.bannerText); }
  }
  } finally {
    try { if(browser)await runtime.bounded(()=>browser.close()); } finally { await server.stop(); }
  }
  console.log('PASS='+pass+' FAIL='+fail);
  failures.forEach(f=>console.error(f));
  return fail?1:0;
}
run().then(code=>process.exit(code)).catch(error=>{console.error(error.stack||error);process.exit(2);});
