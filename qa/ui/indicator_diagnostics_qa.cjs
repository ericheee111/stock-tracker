'use strict';
/* Actual production renderers in Chromium; all fixtures synthetic and network denied. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||process.env.PLAYWRIGHT_PATH||'playwright');
const ROOT=path.resolve(__dirname,'../..');
const output=process.env.NUMERICAL_QA_REPORT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'numerical-qa-'));
fs.mkdirSync(output,{recursive:true});
const expected=['full-windows','short-history','zero-change','nonfinite-hidden','wrong-symbol','wrong-market','wrong-interval','unknown-schema','untrusted-assurance','duplicate-window','empty-input','invalid-input','escaping','honest-rank-label','legacy-invalid-scalar','mobile-360','desktop-1440','keyboard-details'];
const results=[];let browser;let page;const pageErrors=[];
function fixture(n=260){return {schema:'daily-window-diagnostics-v1',symbol:'600519.SH',market:'A',interval:'1d',assurance:'RUNTIME_DIAGNOSTIC_ONLY',auto_trade:false,execution_authorized:false,calendar_coverage_verified:false,status:'NUMERIC_ONLY',computed_at:'2026-09-14T04:00:00+00:00',sample_count:n,same_day_excluded:1,first_date:'2025-12-01',last_date:'2026-09-13',time_basis:'MARKET_LOCAL_DATE',issues:[],windows:[20,60,120,252].map(w=>({sample_window:w,available_samples:Math.min(n,w),state:n>=w?'NUMERIC_ONLY':'INSUFFICIENT_SAMPLES',mean_close:n>=w?20:null,change_percent:n>w?0:null})),methods:[{key:'rsi14',label:'RSI14',required_samples:15,available_samples:Math.min(n,15)}]};}
async function render(data,symbol='600519.SH') {await page.evaluate(({data,symbol})=>{document.querySelector('#target').innerHTML=IndicatorDiagnostics.render(data,symbol);},{data,symbol});}
async function text(){return page.locator('#target').innerText();}
async function check(id,fn){try{await fn();results.push({id,status:'PASS'});console.log('PASS '+id);}catch(error){results.push({id,status:'FAIL',error:String(error.stack||error)});console.error('FAIL '+id+': '+error);}}
(async()=>{
 let fatal=null;
 try{
  browser=await chromium.launch({headless:true});page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>pageErrors.push(String(e)));
  await page.route('**/*',route=>route.abort());
  await page.setContent('<!doctype html><meta charset="utf-8"><main id="target"></main>');
  await page.addStyleTag({path:path.join(ROOT,'web/css/indicator_diagnostics.css')});
  for(const file of ['format.js','components.js','indicator_diagnostics.js'])await page.addScriptTag({path:path.join(ROOT,'web/js',file)});
  await check('full-windows',async()=>{await render(fixture());assert.equal(await page.locator('[data-window]').count(),4);assert((await text()).includes('不是周线策略'));assert((await text()).includes('252 根日线'));});
  await check('short-history',async()=>{await render(fixture(20));assert.equal((await page.locator('[data-window="60"] .nd-mean').innerText()).trim(),'—');assert.equal((await page.locator('[data-window="20"] .nd-change').innerText()).trim(),'—');});
  await check('zero-change',async()=>{await render(fixture());assert.equal((await page.locator('[data-window="20"] .nd-change').innerText()).trim(),'0.00%');});
  await check('nonfinite-hidden',async()=>{const f=fixture();f.windows[0].mean_close=Infinity;f.windows[0].change_percent=true;await render(f);assert.equal((await page.locator('[data-window="20"] .nd-mean').innerText()).trim(),'—');assert.equal((await page.locator('[data-window="20"] .nd-change').innerText()).trim(),'—');});
  await check('wrong-symbol',async()=>{await render(fixture(),'000001.SZ');assert.equal(await page.locator('table').count(),0);});
  await check('wrong-market',async()=>{await render({...fixture(),market:'US'});assert.equal(await page.locator('table').count(),0);});
  await check('wrong-interval',async()=>{await render({...fixture(),interval:'1m'});assert.equal(await page.locator('table').count(),0);});
  await check('unknown-schema',async()=>{await render({...fixture(),schema:'future'});assert.equal(await page.locator('table').count(),0);});
  await check('untrusted-assurance',async()=>{await render({...fixture(),assurance:'TRUSTED_ADMITTED'});assert.equal(await page.locator('table').count(),0);});
  await check('duplicate-window',async()=>{const f=fixture();f.windows[1]=f.windows[0];await render(f);assert.equal(await page.locator('table').count(),0);});
  await check('empty-input',async()=>{await render({...fixture(0),status:'NO_DATA',windows:[]});assert((await text()).includes('暂无可用'));assert.equal(await page.locator('table').count(),0);});
  await check('invalid-input',async()=>{await render({...fixture(),status:'INVALID_INPUT',issues:['FUTURE_BAR_DATE'],windows:[]});assert((await text()).includes('未来日期'));assert.equal(await page.locator('table').count(),0);});
  await check('escaping',async()=>{const f=fixture();f.methods[0].label='<img src=x onerror=alert(1)>';await render(f);await page.locator('details').evaluate(el=>el.open=true);assert.equal(await page.locator('img').count(),0);assert((await text()).includes('<img'));});
  await check('honest-rank-label',async()=>{await page.evaluate(()=>document.querySelector('#target').innerHTML=UI.renderIndicators({pos52w:.5,bar_count:60}));assert((await text()).includes('近60根日线收盘排名'));assert(!(await text()).includes('52周位置'));});
  await check('legacy-invalid-scalar',async()=>{await page.evaluate(()=>document.querySelector('#target').innerHTML=UI.renderIndicators({rsi14:true,ann_vol:'1',pos52w:Infinity}));assert(!(await text()).includes('Infinity'));assert.equal(await page.locator('.ind-posbar').count(),0);});
  await check('mobile-360',async()=>{await page.setViewportSize({width:360,height:800});await render(fixture());assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});});
  await check('desktop-1440',async()=>{await page.setViewportSize({width:1440,height:1000});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});});
  await check('keyboard-details',async()=>{await page.locator('summary').focus();await page.keyboard.press('Enter');assert.equal(await page.locator('details').evaluate(el=>el.open),true);});
 }catch(error){fatal=String(error.stack||error);console.error(fatal);}
 finally{if(browser)try{await browser.close();}catch(error){fatal=String(error);}}
 const ids=results.map(r=>r.id);const pass=!fatal&&!pageErrors.length&&ids.length===expected.length&&new Set(ids).size===expected.length&&expected.every(id=>ids.includes(id))&&results.every(r=>r.status==='PASS');
 fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({expected,results,pageErrors,fatal,passed:pass,scope:'SYNTHETIC_RENDERER_BROWSER_ONLY'},null,2));
 console.log(JSON.stringify({executed:results.length,expected:expected.length,passed:results.filter(r=>r.status==='PASS').length,pageErrors,fatal}));process.exitCode=pass?0:1;
})();
