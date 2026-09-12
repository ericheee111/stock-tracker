'use strict';
/* Actual PlanningUI in Chromium, synthetic API/runtime only. No external requests. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||process.env.PLAYWRIGHT_PATH||'playwright');
const ROOT=path.resolve(__dirname,'../..');
const output=process.env.PLANNING_LIFECYCLE_REPORT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'planning-lifecycle-'));
fs.mkdirSync(output,{recursive:true});
const results=[];
let browser;
async function fixture() {
 const page=await browser.newPage();
 await page.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><details id="planningPanel"><summary>计划</summary><div id="planningWorkspace"></div></details>'}));
 await page.goto('https://planning-fixture.invalid/');
 await page.clock.install();
 await page.addScriptTag({path:path.join(ROOT,'web/js/format.js')});
 await page.evaluate(()=>{
  const now=Date.now(),end=new Date(now+9*60000).toISOString();
  const positions=['p1','p2'].map((id,i)=>({position_id:id,symbol:i?'000001.SZ':'600519.SH',market:'A',shares:1000,cost:'10',added_at:'2026-01-01T00:00:00Z',parent_hash:String(i+1).repeat(64)}));
  window.fixture={schema:'manual-planning-api-v1',enabled:true,status:'READY',store_id:'fixture-store',positions,book:{schema:'manual-plan-book-v1',revision:3,as_of:new Date(now).toISOString(),allocations:{},inventory:{},cash:{CNY:{currency:'CNY',available_cash:'10000',fresh:true,observed_at:new Date(now-1000).toISOString(),expires_at:end}},plans:{}},auto_trade:false};
  for(const p of positions){fixture.book.allocations[p.position_id]={parent:p,parent_matches:true,sleeves:[{purpose:'SWING',quantity:1000,core_quantity:400,thesis:'private-thesis-fixture',invalidation:'日线复核',review_at:null}]};fixture.book.inventory[p.position_id]={parent:p,parent_matches:true,fresh:true,sellable_gross:1000,external_reserved_sell:0,lot_size:100,maximum_position_quantity:1500,currency:'CNY',observed_at:new Date(now-1000).toISOString(),expires_at:end};}
  let access='fixture-access-A';const listeners=[];let state={handshakeReady:true,status:'ONLINE',authState:null,apiOrigin:'https://engine-a.invalid',health:{engine_id:'fixture-engine'}};
  window.Runtime={snapshot:()=>structuredClone(state),privateAccessValue:()=>access,isHardFailure:s=>['ENGINE_OFFLINE','VERSION_MISMATCH','RUNTIME_CONFIG_ERROR'].includes(s),onChange:fn=>{listeners.push(fn);return()=>{};}};
  window.emitRuntime=(change={},token)=>{if(token!==undefined)access=token;state={...state,...change};listeners.forEach(fn=>fn(structuredClone(state)));};
  window.calls=[];window.previewMode='normal';window.commandMode='normal';window.commandAttempts=0;
  window.API={
   getPlanningBook:async()=>{if(window.delayLoad)return new Promise(resolve=>window.resolveLoad=()=>resolve(structuredClone(fixture)));return structuredClone(fixture);},
   planningPreview:async cmd=>{window.lastPreview=cmd;if(previewMode==='fail'||(previewMode==='fail-p2'&&cmd.data.position_id==='p2'))throw Object.assign(new Error('fixture preview rejected'),{status:409,code:'CASH_EXCEEDED'});const p=fixture.positions.find(p=>p.position_id===cmd.data.position_id);return {schema:'manual-t-preview-v1',revision:cmd.expected_revision,reservation_created:false,preview:{...cmd.data,parent:p,currency:'CNY',reserved_cash:'2010',peak_quantity:1200,scenario_net_spread:'190',...(previewMode==='wrong-member'?{position_id:'p2'}:{})}};},
   planningCommand:async cmd=>{
    calls.push(structuredClone(cmd));commandAttempts++;
    if(commandMode==='initial400')throw Object.assign(new Error('invalid amount'),{status:400,code:'INVALID_AMOUNT'});
    if(commandMode==='loss-auth'&&commandAttempts===2){emitRuntime({authState:'AUTH_REQUIRED'});throw Object.assign(new Error('authentication required'),{status:401,code:'PRIVATE_API_AUTH_REQUIRED'});}
    if(!fixture.book.plans[cmd.data.plan_id]&&cmd.kind==='RESERVE'){
     fixture.book.revision++;fixture.book.plans[cmd.data.plan_id]={...cmd.data,parent:positions.find(p=>p.position_id===cmd.data.position_id),currency:'CNY',status:'RESERVED',reserved_cash:'2010',scenario_net_spread:'190',peak_quantity:1200,created_at:new Date(Date.now()).toISOString(),parent_matches:true,expired:false};
    }
    if(['loss-auth','loss-only'].includes(commandMode)&&commandAttempts===1)throw Object.assign(new Error('response lost'),{status:0,code:'NETWORK_REQUEST_FAILED'});
    return {schema:'manual-planning-command-response-v1',store_id:fixture.store_id,book:structuredClone(fixture.book),idempotent:commandAttempts>1};
   },
   planningAttribution:async()=>new Promise(resolve=>window.resolveScenario=()=>resolve({currency:'CNY',relative_hold_delta:'private-scenario-result',cash_delta:'0',quantity_delta:0,unpaired_quantity:0}))
  };
 });
 await page.addScriptTag({path:path.join(ROOT,'web/js/planning.js')});
 await page.evaluate(()=>PlanningUI.load());
 await page.locator('#planningPanel').evaluate(el=>el.open=true);
 return page;
}
async function fillPreview(page,pid='p1') {
 const f=page.locator('form[data-form="t-preview"][data-position="'+pid+'"]');
 await f.evaluate(el=>el.closest('details').open=true);
 for(const [n,v] of Object.entries({quantity:'200',buy:'10',sell:'11',fees:'10'}))await f.locator('[name="'+n+'"]').fill(v);
 await f.locator('[name="checked"]').check();return f;
}
async function preview(page,pid='p1') {const f=await fillPreview(page,pid);await f.locator('button').click();await page.locator('[data-mp="reserve"]').waitFor();return f;}
async function idle(page){await page.waitForTimeout(80);}
const cases={
 'resource-capacity-expires-and-reminder-becomes-due':async p=>{
  await p.evaluate(()=>{const expiry=fixture.book.cash.CNY.expires_at;fixture.resources={schema:'manual-planning-resources-v1',revision:fixture.book.revision,as_of:fixture.book.as_of,currency_pools:[{currency:'CNY',state:'MANUAL_CONFIRMED',confirmed_cash:'10000',reserved_cash:'2010',remaining_cash:'7990',expires_at:expiry}],positions:[{position_id:'p1',symbol:'600519.SH',state:'MANUAL_CONFIRMED',allocated_quantity:1000,core_quantity:400,unclassified_quantity:0,reserved_old_quantity:200,remaining_old_quantity:800,remaining_tactical_quantity:400,expires_at:expiry}],review_items:[{symbol:'600519.SH',purpose:'SWING',review_at:new Date(Date.now()+60000).toISOString(),action:'REVIEW_ONLY',due:false}]};});
  await p.evaluate(()=>PlanningUI.load());assert.equal((await p.locator('.mp-cash-remaining').innerText()).trim(),'7990');
  await p.clock.fastForward(10*60000);await idle(p);assert.equal((await p.locator('.mp-cash-remaining').innerText()).trim(),'—');assert.equal((await p.locator('.mp-old-remaining').innerText()).trim(),'—');
  assert((await p.locator('.mp-review-reminders').innerText()).includes('复核原计划，不自动退出'));
  await p.evaluate(()=>emitRuntime({},''));await idle(p);assert.equal(await p.locator('#planningResources').count(),0);
 },
 'clock-rollback-cannot-revive-expired-snapshot':async p=>{
  await preview(p);const start=await p.evaluate(()=>Date.now());await p.clock.setSystemTime(start+11*60000);await p.clock.runFor(1100);
  assert.equal(await p.locator('[data-mp="reserve"]').count(),0);await p.clock.setSystemTime(start);await p.clock.runFor(1100);
  assert(!(await p.locator('#planningWorkspace').innerText()).includes('人工确认有效'));
 },
 'scenario-edit-failure-and-negative-recalculation':async p=>{
  await p.evaluate(()=>{API.planningAttribution=async data=>{if(data.mark_price==='bad')throw Object.assign(new Error('invalid mark'),{status:400});return {currency:'CNY',relative_hold_delta:data.fees==='150'?'-50':'90',cash_delta:'90',quantity_delta:0,unpaired_quantity:0};};});
  const f=p.locator('form[data-form="scenario"]');await f.evaluate(el=>el.closest('details').open=true);
  for(const [n,v] of Object.entries({starting_quantity:'1000',sellable_old_quantity:'1000',buy_quantity:'100',sell_quantity:'100',average_buy:'10',average_sell:'11',fees:'10',mark_price:'12'}))await f.locator('[name="'+n+'"]').fill(v);
  await f.locator('button').click();await idle(p);assert((await p.locator('#planningScenario').innerText()).includes('90'));
  await f.locator('[name="fees"]').fill('150');assert.equal(await p.locator('#planningScenario .mp-result').count(),0);
  await f.locator('[name="mark_price"]').fill('bad');await f.locator('button').click();await idle(p);assert.equal(await p.locator('#planningScenario .mp-result').count(),0);
  await f.locator('[name="mark_price"]').fill('12');await f.locator('button').click();await idle(p);assert((await p.locator('#planningScenario').innerText()).includes('-50'));
 },
 'unavailable-store-preserves-recovery-boundary':async p=>{
  await p.evaluate(()=>{fixture.enabled=false;fixture.book=null;fixture.status='UNAVAILABLE';});await p.evaluate(()=>PlanningUI.load());
  const text=await p.locator('#planningWorkspace').innerText();assert(text.includes('原计划库'));assert(!text.includes('portfolio_planning init'));assert.equal(await p.locator('.mp-unavailable').count(),1);
 },
 'unconfigured-store-shows-explicit-init-only':async p=>{
  await p.evaluate(()=>{fixture.enabled=false;fixture.book=null;fixture.status='NOT_CONFIGURED';});await p.evaluate(()=>PlanningUI.load());
  assert((await p.locator('#planningWorkspace').innerText()).includes('portfolio_planning init'));assert.equal(await p.locator('form[data-form="allocation"]').count(),0);
 },
 'preview-failure-clears-other-position':async p=>{
  const b=await fillPreview(p,'p2');await preview(p);await p.evaluate(()=>previewMode='fail-p2');await b.locator('button').click();await idle(p);
  assert.equal(await p.locator('[data-mp="reserve"]').count(),0);assert.equal(await p.evaluate(()=>calls.length),0);
 },
 'preview-result-names-parent-and-direction':async p=>{
  await preview(p);const text=await p.locator('#planningPreview').innerText();assert(text.includes('600519.SH'));assert(text.includes('波段'));assert(text.includes('先卖'));assert(text.includes('10')&&text.includes('11'));
 },
 'preview-member-mismatch-rejected':async p=>{
  await p.evaluate(()=>previewMode='wrong-member');const f=await fillPreview(p);await f.locator('button').click();await idle(p);assert.equal(await p.locator('[data-mp="reserve"]').count(),0);
 },
 'uncertain-write-survives-auth-failure':async p=>{
  await p.evaluate(()=>commandMode='loss-auth');await preview(p);await p.locator('[data-mp="reserve"]').click();await p.locator('[data-mp="retry"]:visible').waitFor();await p.locator('[data-mp="retry"]').click();await idle(p);
  assert(!(await p.locator('#planningWorkspace').innerText()).includes('未保存'));
  await p.evaluate(()=>emitRuntime({authState:null},'fixture-access-B'));await p.evaluate(()=>PlanningUI.load());
  assert.equal(await p.locator('[data-mp="retry"]').isVisible(),true);await p.locator('[data-mp="retry"]').click();await idle(p);
  const calls=await p.evaluate(()=>window.calls);assert.equal(calls.length,3);assert.equal(new Set(calls.map(x=>x.command_id)).size,1);assert.equal(await p.evaluate(()=>Object.keys(fixture.book.plans).length),1);
 },
 'credential-clear-erases-private-dom':async p=>{
  await preview(p);await p.evaluate(()=>emitRuntime({},''));await idle(p);
  const html=await p.locator('#planningWorkspace').innerHTML();assert(!html.includes('private-thesis-fixture'));assert(!html.includes('600519.SH'));assert(!html.includes('10000'));assert.equal(await p.locator('[data-mp="reserve"]').count(),0);
 },
 'late-load-cannot-restore-cleared-session':async p=>{
  await p.evaluate(()=>{window.delayLoad=true;window.oldLoad=PlanningUI.load();});await p.waitForFunction(()=>typeof resolveLoad==='function');
  await p.evaluate(()=>emitRuntime({},''));await p.evaluate(()=>{resolveLoad();});await idle(p);assert.equal(await p.locator('.mp-position').count(),0);
 },
 'engine-offline-erases-private-dom':async p=>{
  await preview(p);await p.evaluate(()=>emitRuntime({status:'ENGINE_OFFLINE',handshakeReady:false}));await idle(p);assert.equal(await p.locator('.mp-position').count(),0);assert.equal(await p.locator('[data-mp="reserve"]').count(),0);
 },
 'uncertain-request-cannot-cross-origin':async p=>{
  await p.evaluate(()=>commandMode='loss-only');await preview(p);await p.locator('[data-mp="reserve"]').click();await p.locator('[data-mp="retry"]:visible').waitFor();
  await p.evaluate(()=>emitRuntime({apiOrigin:'https://engine-b.invalid'}));await p.evaluate(()=>PlanningUI.load());
  const retry=p.locator('[data-mp="retry"]');assert.equal(await retry.isDisabled(),true);assert.equal(await p.evaluate(()=>calls.length),1);
  await p.evaluate(()=>emitRuntime({apiOrigin:'https://engine-a.invalid'}));await p.evaluate(()=>PlanningUI.load());assert.equal(await retry.isDisabled(),false);await retry.click();await idle(p);assert.equal(await p.evaluate(()=>new Set(calls.map(c=>c.command_id)).size),1);
 },
 'expiry-invalidates-preview-without-refresh':async p=>{
  await preview(p);await p.clock.fastForward(10*60000);await idle(p);
  assert.equal(await p.locator('[data-mp="reserve"]').count(),0);assert(!(await p.locator('#planningWorkspace').innerText()).includes('人工确认有效'));assert((await p.locator('#planningWorkspace').innerText()).includes('过期'));
 },
 'expiry-preserves-existing-reservation':async p=>{
  await preview(p);await p.locator('[data-mp="reserve"]').click();await idle(p);await p.clock.fastForward(10*60000);await idle(p);
  assert((await p.locator('#planningWorkspace').innerText()).includes('额度仍保留'));assert.equal(await p.evaluate(()=>calls.length),1);assert.equal(await p.locator('[data-plan-action="CANCEL"]').isEnabled(),true);
 },
 'expiry-does-not-destroy-unsaved-input':async p=>{
  await p.locator('form[data-form="allocation"]').first().evaluate(el=>el.closest('details').open=true);
  const field=p.locator('[name="SWING_thesis"]').first();await field.fill('unsaved-user-plan');await p.clock.fastForward(10*60000);await idle(p);assert.equal(await field.inputValue(),'unsaved-user-plan');
 },
 'initial-validation-error-allows-correction':async p=>{
  await p.evaluate(()=>commandMode='initial400');await preview(p);await p.locator('[data-mp="reserve"]').click();await idle(p);assert.equal(await p.locator('[data-mp="retry"]').isVisible(),false);assert.equal(await p.locator('form[data-form="cash"] button').isEnabled(),true);
 },
 'late-scenario-cannot-restore-private-data':async p=>{
  const f=p.locator('form[data-form="scenario"]');await f.evaluate(el=>el.closest('details').open=true);
  for(const [n,v] of Object.entries({starting_quantity:'1000',sellable_old_quantity:'1000',fees:'0',mark_price:'10'}))await f.locator('[name="'+n+'"]').fill(v);
  await f.locator('button').click();await p.waitForFunction(()=>typeof resolveScenario==='function');await p.evaluate(()=>emitRuntime({},''));await p.evaluate(()=>resolveScenario());await idle(p);
  assert(!(await p.locator('#planningWorkspace').innerText()).includes('private-scenario-result'));assert.equal(await p.locator('.mp-position').count(),0);
 }
};
(async()=>{
 let fatal=null;
 try{
  browser=await chromium.launch({headless:true});
  for(const [id,fn] of Object.entries(cases)){
   let page;const pageErrors=[];
   try{page=await fixture();page.on('pageerror',e=>pageErrors.push(String(e)));await fn(page);assert.deepEqual(pageErrors,[]);results.push({id,status:'PASS'});console.log('PASS '+id);}
   catch(error){results.push({id,status:'FAIL',error:String(error.stack||error),pageErrors});console.error('FAIL '+id+': '+error);}
   finally{if(page)await page.close();}
  }
 }catch(error){fatal=String(error.stack||error);console.error(fatal);}
 finally{if(browser)try{await browser.close();}catch(error){fatal=String(error);}}
 const passed=results.filter(r=>r.status==='PASS').length;
 const report={schema:'planning-lifecycle-qa-v1',expected:Object.keys(cases),results,passed,fatal,assurance:'SYNTHETIC_BROWSER_WITH_MOCK_API'};
 fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({executed:results.length,expected:Object.keys(cases).length,passed,fatal}));
 process.exitCode=!fatal&&results.length===Object.keys(cases).length&&passed===results.length?0:1;
})();
