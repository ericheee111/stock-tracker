'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');
const { computeVerdict } = require('./wb3_verdict.cjs');

test('U1 invalid explicit market and key conflict fail closed', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../web/js/app.js'), 'utf8');
  const functions = source.slice(source.indexOf('  function isMarketKey'), source.indexOf('  function hasSpecificRuntimeStatus'));
  const check = vm.runInNewContext(functions + '; marketsHaveData');
  for (const input of [[{market:'NOT_A_MARKET',index:{}}], {a:{market:'US',index:{symbol:'AAPL.US'}}}, [{index:{}}]]) {
    assert.equal(check(input), false, JSON.stringify(input));
  }
  assert.equal(check({a:{index:{symbol:'000001.SH'}}}), true);
  assert.equal(check([{market:'A',index:{}}]), true);
});

test('U3 empty and fabricated capture rows cannot pass', () => {
  const combo = {n:1,scenario:'full',page:'today',width:390,theme:'dark',tab:null};
  assert.notEqual(computeVerdict([], 0).exitCode, 0);
  assert.notEqual(computeVerdict([{combo}], [combo]).exitCode, 0);
  assert.notEqual(computeVerdict([{combo},{combo}], [combo,{...combo,n:2,page:'watch'}]).exitCode, 0);
});


test('U2 matching child READY triggers exactly one launch and cleanup', async () => {
  const child = new EventEmitter();
  Object.assign(child,{pid:123,exitCode:null,signalCode:null,stdout:new EventEmitter(),stderr:new EventEmitter()});
  let launches=0,kills=0;
  child.kill=()=>{kills++;child.exitCode=0;child.emit('close',0);};
  const fakeFs={mkdirSync(){},existsSync(){return false;},writeFileSync(){}};
  const page={goto:async()=>{},evaluate:async()=> 'width=device-width, initial-scale=1.0'};
  const browser={newPage:async()=>page,close:async()=>{}};
  const runtimeModule={exports:{}};
  const processMock={execPath:process.execPath,env:{WB3_REPORT_DIR:'vm-output'}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'wb3_runtime.cjs'),'utf8'),{
    __dirname,module:runtimeModule,process:processMock,setTimeout,clearTimeout,
    require(name){
      if(name==='node:child_process')return {spawn:()=>child};
      if(name==='node:crypto')return {randomUUID:()=> 'test-owned-run'};
      if(name==='node:fs')return fakeFs;
      if(name==='playwright')return {chromium:{launch:async()=>{launches++;return browser;}}};
      return require(name);
    }
  });
  const viewportModule={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'wb3_verify_viewport.cjs'),'utf8'),{
    __dirname,module:viewportModule,process:processMock,console:{log(){},error(){}},
    require(name){
      if(name==='./wb3_runtime.cjs')return runtimeModule.exports;
      if(name==='node:fs')return fakeFs;
      return require(name);
    }
  });
  const done=viewportModule.exports.main();
  const ready={runId:'test-owned-run',pid:123,scenario:'full',port:12345};
  child.stdout.emit('data','WB3_READY '+JSON.stringify({...ready,runId:'unrelated'})+'\n');
  await Promise.resolve();assert.equal(launches,0);
  child.stdout.emit('data','WB3_READY '+JSON.stringify(ready)+'\n');
  child.stdout.emit('data','WB3_READY '+JSON.stringify(ready)+'\n');
  assert.equal(await done,0);assert.equal(launches,1);assert.equal(kills,1);
});

const {EXPECTED_COMBOS,ALL_COMBOS,expectedState} = require('./wb3_matrix.cjs');
function completed(combo){
  return {combo,completed:true,dom:{collected:true,issues:[],activePage:'page-'+combo.page,
    runtimeState:expectedState(combo),viewportWidth:combo.width,theme:combo.theme,activeMonitorTab:combo.tab},
    exceptionCollection:{completed:true},pageErrors:[],failedRequests:[],consoleErrors:[],
    apiStatus:[{path:'/api/runtime/health',status:200},
      {path:'/api/brief/today',status:combo.scenario==='auth'?401:combo.scenario==='error'?500:200}]};
}
test('U3 exact complete matrix passes; missing, duplicate and uncollected rows fail',()=>{
  assert.equal(EXPECTED_COMBOS.length,54);
  const good=EXPECTED_COMBOS.map(completed);
  assert.equal(computeVerdict(good,EXPECTED_COMBOS).exitCode,0);
  const variants=[good.slice(1),[good[0],good[0],...good.slice(2)]];
  for(const update of [{dom:null},{completed:false},{exceptionCollection:null},{pageErrors:undefined},
    {dom:{...good[0].dom,activePage:'page-watch'}},{dom:{...good[0].dom,runtimeState:'STALE'}}]){
    variants.push([{...good[0],...update},...good.slice(1)]);
  }
  for(const rows of variants)assert.equal(computeVerdict(rows,EXPECTED_COMBOS).exitCode,1);
  assert.equal(computeVerdict([],EXPECTED_COMBOS).status,'NOT_VALIDATED');
  assert.equal(computeVerdict(good,[]).exitCode,2);
});
test('U3 expected HTTP errors are scoped and recorded; page/DOM/request failures remain failures',()=>{
  for(const scenario of ['auth','error']){
    const combo=ALL_COMBOS.find(c=>c.scenario===scenario);
    const row=completed(combo);
    row.consoleErrors=['Failed to load resource: the server responded with a status of '+(scenario==='auth'?401:500)];
    assert.equal(computeVerdict([row],[combo]).exitCode,0);
    row.pageErrors=['intentional'];assert.equal(computeVerdict([row],[combo]).exitCode,1);
  }
  for(const update of [{consoleErrors:['Failed to load resource: the server responded with a status of 500']},
    {pageErrors:['boom']},{failedRequests:[{url:'/api/markets',error:'net::ERR'}]},
    {error:'timeout'}, {dom:{...completed(EXPECTED_COMBOS[0]).dom,issues:[{type:'H_OVERFLOW',detail:'synthetic'}]}}]){
    assert.equal(computeVerdict([{...completed(EXPECTED_COMBOS[0]),...update}],[EXPECTED_COMBOS[0]]).exitCode,1);
  }
});
