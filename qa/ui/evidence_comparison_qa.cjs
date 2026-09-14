'use strict';
/* Actual Python candidate documents + actual production renderer in Chromium.
   All inputs synthetic; page network denied. Every attempt has a real exit code. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const cp=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||process.env.PLAYWRIGHT_PATH||'playwright');
const ROOT=path.resolve(__dirname,'../..');
const output=process.env.EVIDENCE_QA_REPORT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'evidence-qa-'));
fs.mkdirSync(output,{recursive:true});
const scenarioIds=['complete','rsi-zero','ma60-short','rsi-short','macd-short','dq-missing','dq-degraded','contexts-missing','no-bars','quote-missing','zero-turnover','invalid-identity'];
const extraIds=['wrong-symbol','wrong-market','wrong-interval','unknown-schema','unknown-policy','untrusted-assurance','auto-trade','probability','duplicate-family','missing-score','wrong-delta','bool-score','nonfinite-term','false-sample-count','numeric-term-missing-dependency','nonfinite-multiplier','null-multiplier-with-total','split-capture-clock','prototype-market','missing-enhancement','escaping','unknown-not-zero','real-zero-term','keyboard','mobile-360','desktop-1440','app-wiring'];
const reviewIds=['missing-term-with-total','missing-group-dependency','missing-family-with-numeric-dependent-score','missing-quote-status','unknown-quote-status','nonlive-without-warning','live-with-false-warning','status-live','status-delayed','status-stale','status-unknown'];
const expected=[...scenarioIds,...extraIds,...reviewIds];
const results=[],pageErrors=[];let browser,page,fatal=null;
const clone=v=>JSON.parse(JSON.stringify(v));
async function check(id,fn){try{await fn();results.push({id,status:'PASS'});console.log('PASS '+id);}catch(e){results.push({id,status:'FAIL',error:String(e.stack||e)});console.error('FAIL '+id+': '+e);}}
async function render(d,symbol='600000.SH',market='A'){
  return page.evaluate(({d,symbol,market})=>{
    document.querySelector('#target').innerHTML=EvidenceComparison.render(d,symbol,market);
    return {accepted:EvidenceComparison.valid(d,symbol,market),tableRows:document.querySelectorAll('[data-ec-key]').length};
  },{d,symbol,market});
}
async function expand(){const panel=page.locator('details.ec-panel');if(await panel.count())await panel.evaluate(el=>el.open=true);}
(async()=>{
  try{
    const exe=process.env.EVIDENCE_PYTHON_EXE||(process.platform==='win32'?'py':'python3');
    const args=[...(exe==='py'?['-3.14']:[]),'-X','utf8','-B','scripts/run_evidence_comparison.py','--details'];
    const child=cp.spawnSync(exe,args,{cwd:ROOT,encoding:'utf8',timeout:30000,maxBuffer:8*1024*1024,env:{...process.env,PYTHONIOENCODING:'utf-8',PYTHONDONTWRITEBYTECODE:'1'}});
    fs.writeFileSync(path.join(output,'fixture-stdout.json'),child.stdout||'');fs.writeFileSync(path.join(output,'fixture-stderr.log'),child.stderr||'');
    assert.equal(child.status,0,String(child.error||child.stderr));
    const fixture=JSON.parse(child.stdout);assert.equal(fixture.passed,true);assert.deepEqual(fixture.expected_cases,scenarioIds);
    assert.equal(fixture.executed,12);assert.deepEqual(fixture.cases.map(c=>c.case),scenarioIds);
    const reports=Object.fromEntries(fixture.cases.map(c=>[c.case,c.report]));
    browser=await chromium.launch({headless:true});page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>pageErrors.push(String(e)));
    await page.route('**/*',route=>route.abort());
    await page.setContent('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><main id="target"></main>');
    await page.addStyleTag({path:path.join(ROOT,'web/css/evidence_comparison.css')});
    await page.addScriptTag({path:path.join(ROOT,'web/js/evidence_comparison.js')});
    for(const id of scenarioIds)await check(id,async()=>{
      const result=await render(reports[id]);assert.equal(result.accepted,true);
      assert.equal(result.tableRows,id==='invalid-identity'?0:9);
      if(id!=='invalid-identity'){
        assert.equal(await page.locator('details.ec-panel').evaluate(e=>e.open),false);
        await expand();const text=await page.locator('#target').innerText();
        assert(text.includes('不是当前信号的历史分数'));assert(text.includes('不改变排序或仓位'));assert(text.includes('不是实时做 T 信号'));
      }
    });
    const negative=[
      ['wrong-symbol',d=>{d.symbol='AAPL.US';}],['wrong-market',d=>{d.market='US';}],['wrong-interval',d=>{d.interval='1m';}],
      ['unknown-schema',d=>{d.schema='future';}],['unknown-policy',d=>{d.policy_id='auto';}],['untrusted-assurance',d=>{d.assurance='TRUSTED_ADMITTED';}],
      ['auto-trade',d=>{d.auto_trade=true;}],['probability',d=>{d.success_probability=.9;}],
      ['duplicate-family',d=>{d.candidate.families[1]=d.candidate.families[0];}],['missing-score',d=>{d.candidate.scores.pop();}],
      ['wrong-delta',d=>{d.differences[0].delta=99;}],['bool-score',d=>{d.candidate.scores[0].value=true;}],
      ['nonfinite-term',d=>{d.candidate.families[0].terms[0].value=Infinity;}],['false-sample-count',d=>{d.sample_info.selected_count=81;}],
      ['numeric-term-missing-dependency',d=>{d.candidate.families[0].terms[1].inputs.LAST_REQUIRED=null;}],
      ['nonfinite-multiplier',d=>{d.candidate.scores[1].multiplier=Infinity;}],
      ['null-multiplier-with-total',d=>{d.candidate.scores[1].multiplier=null;}],
      ['split-capture-clock',d=>{d.sample_info.computed_at='2026-01-01T00:00:00+00:00';}]
    ];
    for(const [id,mutate] of negative)await check(id,async()=>{const d=clone(reports.complete);mutate(d);const r=await render(d);assert.equal(r.accepted,false);assert.equal(r.tableRows,0);assert((await page.locator('#target').innerText()).includes('暂不可用'));});
    const reviewNegative=[
      ['missing-term-with-total',d=>{const t=d.candidate.families[0].terms[1];t.inputs.MA20_REQUIRED=null;t.value=null;t.status='MISSING_INPUT';t.missing=['MA20_REQUIRED'];}],
      ['missing-group-dependency',d=>{const g=d.candidate.families[0],t=g.terms[1];t.inputs.MA20_REQUIRED=null;t.value=null;t.status='MISSING_INPUT';t.missing=['MA20_REQUIRED'];g.value=null;g.status='MISSING_INPUT';g.missing=['NUMERIC_UNAVAILABLE'];d.status='PARTIAL';d.differences[0].candidate_value=null;d.differences[0].delta=null;}],
      ['missing-family-with-numeric-dependent-score',d=>{const g=d.candidate.families[0];g.value=null;g.status='MISSING_INPUT';g.missing=['NUMERIC_UNAVAILABLE'];d.status='PARTIAL';d.differences[0].candidate_value=null;d.differences[0].delta=null;}],
      ['missing-quote-status',d=>{delete d.sample_info.quote_declared_status;}],
      ['unknown-quote-status',d=>{d.sample_info.quote_declared_status='VERIFIED_LIVE';}],
      ['nonlive-without-warning',d=>{d.sample_info.quote_declared_status='STALE';d.warnings=d.warnings.filter(k=>k!=='QUOTE_NOT_DECLARED_LIVE');}],
      ['live-with-false-warning',d=>{d.sample_info.quote_declared_status='LIVE';d.warnings=[...d.warnings.filter(k=>k!=='QUOTE_NOT_DECLARED_LIVE'),'QUOTE_NOT_DECLARED_LIVE'];}]
    ];
    for(const [id,mutate] of reviewNegative)await check(id,async()=>{const d=clone(reports.complete);mutate(d);const r=await render(d);assert.equal(r.accepted,false);assert.equal(r.tableRows,0);});
    for(const [state,label] of [['LIVE','声明实时'],['DELAYED','声明延迟'],['STALE','声明过期'],['UNKNOWN','声明未知']])await check('status-'+state.toLowerCase(),async()=>{
      const d=clone(reports.complete);d.sample_info.quote_declared_status=state;d.warnings=d.warnings.filter(k=>k!=='QUOTE_NOT_DECLARED_LIVE');
      if(state!=='LIVE')d.warnings.push('QUOTE_NOT_DECLARED_LIVE');
      assert.equal((await render(d)).accepted,true);await expand();const text=await page.locator('#target').innerText();
      assert(text.includes(label));assert(text.includes(state));assert(text.includes('未经独立认证'));
      if(state!=='LIVE')assert(text.includes('报价未声明为实时'));
      assert(text.includes('不改变排序或仓位'));
    });
    await check('prototype-market',async()=>{const d=clone(reports.complete);d.market='__proto__';const r=await render(d,d.symbol,'__proto__');assert.equal(r.accepted,false);});
    await check('missing-enhancement',async()=>{await render(null);assert.equal(await page.locator('#target').innerHTML(),'');});
    await check('escaping',async()=>{
      const d=clone(reports.complete);d.candidate.families[0].terms[0].label='<img src=x onerror="window.bad=1">';d.sample_info.quote_source='<script>window.bad=1</script>';d.findings=['__proto__','<img src=x>'];
      assert.equal((await render(d)).accepted,true);await expand();await page.locator('details.ec-group').first().evaluate(e=>e.open=true);
      assert.equal(await page.locator('#target img,#target script').count(),0);assert.equal(await page.evaluate(()=>window.bad),undefined);
      const text=await page.locator('#target').innerText();assert(text.includes('<img'));assert(!text.includes('[object Object]'));
    });
    await check('unknown-not-zero',async()=>{await render(reports['contexts-missing']);await expand();assert.equal((await page.locator('[data-ec-key="confidence"] td').nth(1).innerText()).trim(),'—');});
    await check('real-zero-term',async()=>{await render(reports.complete);await expand();const deltas=await page.locator('[data-ec-key] td:last-child').allTextContents();assert(deltas.every(t=>t.trim()==='0'));});
    await check('keyboard',async()=>{await render(reports.complete);await page.locator('details.ec-panel > summary').focus();await page.keyboard.press('Enter');assert.equal(await page.locator('details.ec-panel').evaluate(e=>e.open),true);});
    await check('mobile-360',async()=>{await page.setViewportSize({width:360,height:800});await render(reports['contexts-missing']);await expand();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});});
    await check('desktop-1440',async()=>{await page.setViewportSize({width:1440,height:1000});await render(reports.complete);await expand();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});});
    await check('app-wiring',async()=>{
      const html=fs.readFileSync(path.join(ROOT,'web/index.html'),'utf8');const app=fs.readFileSync(path.join(ROOT,'web/js/app.js'),'utf8');
      assert(html.includes('css/evidence_comparison.css'));assert(html.indexOf('js/evidence_comparison.js')<html.indexOf('js/app.js'));
      assert(app.includes('window.EvidenceComparison.render(d.evidence_comparison, d.symbol, d.market)'));
    });
  }catch(error){fatal=String(error.stack||error);console.error(fatal);}
  finally{if(browser)try{await browser.close();}catch(error){fatal=String(error);}}
  const ids=results.map(r=>r.id);
  const passed=!fatal&&!pageErrors.length&&ids.length===expected.length&&new Set(ids).size===expected.length&&expected.every(id=>ids.includes(id))&&results.every(r=>r.status==='PASS');
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({expected,results,pageErrors,fatal,passed,scope:'SYNTHETIC_PYTHON_DOCUMENTS_AND_PRODUCTION_RENDERER_BROWSER'},null,2));
  console.log(JSON.stringify({executed:results.length,expected:expected.length,passed:results.filter(r=>r.status==='PASS').length,pageErrors,fatal}));
  process.exitCode=passed?0:1;
})();
