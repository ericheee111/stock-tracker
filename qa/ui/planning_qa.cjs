/* Real temporary API/browser acceptance. Every named case must execute once. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_PATH || 'playwright');
const base = process.env.PLANNING_QA_BASE_URL;
const disabledBase = process.env.PLANNING_QA_DISABLED_URL;
for (const address of [base, disabledBase]) {
  if (!address || !['127.0.0.1','localhost'].includes(new URL(address).hostname)) throw new Error('Use run_planning_integration.py with private temporary fixture servers');
}
const output = process.env.PLANNING_QA_REPORT_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'planning-qa-'));
fs.mkdirSync(output,{recursive:true});
const expected = ['dual-primary','empty-holdings-visible','mobile-summary','planner-enabled','allocation','xss-escaped','cash','inventory','core-protected','preview-no-write','preview-invalidates','lost-response','retry-idempotent','mark-started','no-implicit-release','reconcile','relative-hold','stale-banner','responsive-360','responsive-768','responsive-1440','disabled-mode','error-mode','no-page-errors'];
const results = [];
const errors = [];
let browser;
async function check(id, fn) {
  assert(expected.includes(id) && !results.some(x=>x.id===id),'unexpected/duplicate case');
  try { await fn(); results.push({id, status:'PASS'}); console.log('PASS '+id); }
  catch(error) { results.push({id,status:'FAIL',error:String(error.stack||error)}); throw error; }
}
async function getBook(page) { return page.evaluate(()=>API.getPlanningBook()); }
async function openForm(page, type) {
  const form=page.locator('form[data-form="'+type+'"]');
  await form.evaluate(el=>el.closest('details').open=true);
  return form;
}
async function save(page, form, expectedStatus=200) {
  const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/planning/commands') && r.request().method()==='POST');
  await form.locator('button[type="submit"]').click();
  const response=await responsePromise;
  assert.equal(response.status(),expectedStatus,await response.text());
  if(expectedStatus===200) await page.waitForFunction(()=>document.getElementById('planningMessage').textContent.includes('已保存'));
  return response.json();
}
async function main() {
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('dialog',d=>d.accept());
  await page.goto(base,{waitUntil:'domcontentloaded'});
  await page.locator('.tb-dual-jumps').waitFor();
  await check('dual-primary',async()=>{
    assert.equal(await page.locator('.tb-dual-jumps a').count(),2);
    assert.equal(await page.locator('#todayHoldings').count(),1);
    assert.equal(await page.locator('#todayOpportunities').count(),1);
    assert(await page.locator('#todayOpportunities .tb-core').count()>0);
  });
  await check('empty-holdings-visible',async()=>{
    const html=await page.evaluate(()=>Today.render({holding_actions:[],core_opportunities:[]}));
    assert(html.includes('id="todayHoldings"') && html.includes('id="todayOpportunities"'));
  });
  await check('mobile-summary',async()=>{
    await page.setViewportSize({width:360,height:800});
    const boxes=await page.locator('.tb-dual-jumps a').evaluateAll(items=>items.map(x=>({x:x.getBoundingClientRect().x,w:x.getBoundingClientRect().width})));
    assert(boxes.every(x=>x.x>=0 && x.x+x.w<=361));
    await page.setViewportSize({width:1440,height:1000});
  });
  await page.locator('#planningPanel > summary').click();
  await page.locator('.mp-position').waitFor();
  await check('planner-enabled',async()=>{const b=await getBook(page);assert.equal(b.enabled,true);assert.equal(b.book.revision,0);assert.equal(b.book.execution_authorized,false);});
  await check('allocation',async()=>{
    const f=await openForm(page,'allocation');
    for(const [name,value] of Object.entries({SWING_quantity:'600',SWING_core:'200',SWING_thesis:'波段 <img src=x onerror="window.fixtureXss=true">',SWING_invalidation:'日线结构失效后复核',LONG_TERM_quantity:'400',LONG_TERM_core:'400',LONG_TERM_thesis:'长期逻辑',LONG_TERM_invalidation:'逻辑改变后人工复核'})) await f.locator('[name="'+name+'"]').fill(value);
    await save(page,f);
    const b=await getBook(page);const a=Object.values(b.book.allocations)[0];assert.equal(a.sleeves.length,2);assert.equal(a.sleeves.reduce((n,s)=>n+s.quantity,0),1000);
  });
  await check('xss-escaped',async()=>{assert.equal(await page.evaluate(()=>window.fixtureXss),undefined);assert.equal(await page.locator('#planningWorkspace img').count(),0);});
  await check('cash',async()=>{const f=await openForm(page,'cash');await f.locator('[name="cash"]').fill('10000');await f.locator('[name="checked"]').check();await save(page,f);assert.equal((await getBook(page)).book.cash.CNY.available_cash,'10000');});
  await check('inventory',async()=>{
    const f=await openForm(page,'inventory');
    for(const [name,val] of Object.entries({sellable:'800',external:'100',lot:'100',maximum:'1400',rule_note:'仅合成的人工库存核对测试'}))await f.locator('[name="'+name+'"]').fill(val);
    await f.locator('[name="checked"]').check();await save(page,f);
    assert.equal(Object.values((await getBook(page)).book.inventory)[0].sellable_gross,800);
  });
  async function preparePreview(purpose='SWING') {
    const f=await openForm(page,'t-preview');await f.locator('[name="purpose"]').selectOption(purpose);
    for(const [name,val] of Object.entries({quantity:'200',buy:'10',sell:'11',fees:'10'}))await f.locator('[name="'+name+'"]').fill(val);
    await f.locator('[name="checked"]').check();return f;
  }
  await check('core-protected',async()=>{
    const f=await preparePreview('LONG_TERM');const response=page.waitForResponse(r=>r.url().endsWith('/api/planning/preview'));
    await f.locator('button').click();assert.equal((await response).status(),409);
    await page.waitForFunction(()=>document.getElementById('planningMessage').textContent.includes('核心仓'));
  });
  await check('preview-no-write',async()=>{
    const f=await preparePreview();await f.locator('button').click();await page.locator('[data-mp="reserve"]').waitFor();
    const b=await getBook(page);assert.equal(Object.keys(b.book.plans).length,0);assert.equal(b.book.revision,3);
  });
  await check('preview-invalidates',async()=>{
    const f=page.locator('form[data-form="t-preview"]');await f.locator('[name="quantity"]').fill('100');
    assert.equal(await page.locator('[data-mp="reserve"]').count(),0);
    await f.locator('[name="quantity"]').fill('200');await f.locator('button').click();await page.locator('[data-mp="reserve"]').waitFor();
  });
  let intercepted=false;const retryIds=[];
  await page.route('**/api/planning/commands',async route=>{
    const data=route.request().postDataJSON();
    if(data.kind==='RESERVE') {
      retryIds.push(data.command_id);
      if(!intercepted){intercepted=true;const response=await route.fetch();assert.equal(response.status(),200);await route.abort('connectionreset');return;}
    }
    await route.continue();
  });
  await check('lost-response',async()=>{
    await page.locator('[data-mp="reserve"]').click();await page.locator('[data-mp="retry"]:visible').waitFor();
    assert.equal(Object.keys((await getBook(page)).book.plans).length,1);
    assert.equal(await page.locator('form[data-form="cash"] button').isDisabled(),true);
  });
  await check('retry-idempotent',async()=>{
    await page.locator('[data-mp="retry"]').click();await page.waitForFunction(()=>document.getElementById('planningMessage').textContent.includes('已保存'));
    const b=await getBook(page);assert.equal(Object.keys(b.book.plans).length,1);assert.equal(b.book.revision,4);assert.equal(retryIds.length,2);assert.equal(retryIds[0],retryIds[1]);
  });
  await page.unroute('**/api/planning/commands');
  await check('mark-started',async()=>{
    await page.locator('[data-plan-action="MARK_EXECUTED"]').click();await page.waitForFunction(()=>document.getElementById('planningMessage').textContent.includes('已保存') && !document.querySelector('[data-plan-action="MARK_EXECUTED"]'));
    assert.equal(Object.values((await getBook(page)).book.plans)[0].status,'RECONCILIATION_REQUIRED');
  });
  await check('no-implicit-release',async()=>{const b=await getBook(page);assert.equal(Object.values(b.book.plans)[0].reserved_cash,'2010');assert.equal(await page.locator('[data-plan-action="CANCEL"]').count(),0);});
  await check('reconcile',async()=>{
    const f=await openForm(page,'reconcile');await f.locator('[name="reason"]').fill('合成核对：账户记录和外部委托均已处理');await f.locator('[name="orders"]').check();await f.locator('[name="accounting"]').check();await save(page,f);
    const b=await getBook(page);assert.equal(Object.keys(b.book.cash).length,0);assert.equal(Object.keys(b.book.inventory).length,0);assert.equal(Object.values(b.book.plans)[0].status,'CLOSED_MANUAL_RECONCILIATION');
  });
  await check('relative-hold',async()=>{
    const f=await openForm(page,'scenario');
    for(const [name,val] of Object.entries({starting_quantity:'1000',sellable_old_quantity:'1000',buy_quantity:'0',sell_quantity:'100',average_sell:'11',fees:'10',mark_price:'12'}))await f.locator('[name="'+name+'"]').fill(val);
    await f.locator('button').click();await page.locator('#planningScenario .mp-result').waitFor();
    assert((await page.locator('#planningScenario').innerText()).includes('-110'));
  });
  await check('stale-banner',async()=>{
    const b=await getBook(page);const p=Object.values(b.book.plans)[0];p.status='RESERVED';p.expired=true;
    const html=await page.evaluate(data=>PlanningUI.render(data),b);assert(html.includes('额度仍保留'));
  });
  for(const width of [360,768,1440]) await check('responsive-'+width,async()=>{
    await page.setViewportSize({width,height:1000});
    await page.locator('#planningPanel').scrollIntoViewIfNeeded();
    const dimensions=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,width:innerWidth}));
    assert(dimensions.scroll<=dimensions.width+1,JSON.stringify(dimensions));
    await page.screenshot({path:path.join(output,'planning-'+width+'.png'),fullPage:true});
  });
  const disabled=await browser.newPage({viewport:{width:390,height:844}});disabled.on('pageerror',e=>errors.push(String(e)));
  await check('disabled-mode',async()=>{
    await disabled.goto(disabledBase,{waitUntil:'domcontentloaded'});await disabled.locator('#planningPanel > summary').click();await disabled.locator('.mp-disabled').waitFor();
    assert.equal(await disabled.locator('form[data-form="allocation"]').count(),0);
    assert.equal(await disabled.locator('form[data-form="scenario"] button').isDisabled(),false);
    assert.equal((await getBook(disabled)).book,null);
  });
  await check('error-mode',async()=>{
    await disabled.route('**/api/planning/book',r=>r.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:{code:'AUTH_FAILED',message:'合成认证失败'}})}));
    await disabled.locator('[data-mp="refresh"]').click();await disabled.locator('#planningMessage[role="alert"]').waitFor();
    assert.equal(await disabled.locator('form').filter({has:disabled.locator('[name="sellable"]')}).count(),0);
    assert((await disabled.locator('#planningMessage').innerText()).includes('认证失败'));
  });
  await check('no-page-errors',async()=>assert.deepEqual(errors,[]));
  assert.equal(results.length,expected.length);
  assert.deepEqual(new Set(results.map(x=>x.id)),new Set(expected));
}
(async()=>{
  let code=0;
  try {await main();}
  catch(error){console.error(error.stack||error);code=1;}
  finally {if(browser)try{await browser.close();}catch(error){console.error(error);code=1;}}
  const report={expected:expected.length,executed:results.length,passed:results.filter(x=>x.status==='PASS').length,results,pageErrors:errors,exit_code:code,assurance:'SYNTHETIC_TEMPORARY_API_BROWSER'};
  fs.writeFileSync(path.join(output,'planning-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  process.exitCode=code || (report.passed===expected.length?0:1);
})();
