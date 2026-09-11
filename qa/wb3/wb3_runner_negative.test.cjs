'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const {spawn}=require('node:child_process');
const {reportDirectory}=require('./wb3_runtime.cjs');
const report=reportDirectory('controls');
const observations=[];
async function runCase(label,script,env={}) {
  const output=path.join(report,label);
  if(fs.existsSync(output))throw new Error('REFUSE_OVERWRITE '+output);
  fs.mkdirSync(output,{recursive:true});
  const args=[path.join(__dirname,script)];
  const result=await new Promise(resolve=>{
    const child=spawn(process.execPath,args,{env:{...process.env,...env,WB3_REPORT_DIR:output},stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',timedOut=false;
    child.stdout.on('data',d=>stdout+=d);child.stderr.on('data',d=>stderr+=d);
    const timer=setTimeout(()=>{timedOut=true;child.kill();},60000);
    child.on('error',error=>{clearTimeout(timer);resolve({code:null,error:String(error),stdout,stderr});});
    child.on('close',(code,signal)=>{clearTimeout(timer);resolve({code,signal,timedOut,stdout,stderr});});
  });
  fs.writeFileSync(path.join(output,'stdout.log'),result.stdout,'utf8');
  fs.writeFileSync(path.join(output,'stderr.log'),result.stderr,'utf8');
  const payload=path.join(output,script==='wb3_capture.cjs'?'logs/capture-verdict.json':'viewport-result.json');
  result.report=fs.existsSync(payload)?JSON.parse(fs.readFileSync(payload,'utf8')):null;
  observations.push({label,argv:[process.execPath,...args],...result});
  fs.writeFileSync(path.join(report,'control-results.json'),JSON.stringify(observations,null,2),'utf8');
  assert.equal(result.timedOut,false, label+' harness timed out');
  assert.ok(result.report,label+' missing result');
  assert.equal(result.code,result.report.exitCode,label+' exit/report mismatch');
  return result;
}
async function main(){
  let r=await runCase('viewport-full','wb3_verify_viewport.cjs',{WB3_SCENARIO:'full'});
  assert.equal(r.code,0);assert.equal(r.report.reason,'ZOOM_ALLOWED');
  r=await runCase('viewport-locked','wb3_verify_viewport.cjs',{WB3_SCENARIO:'locked'});
  assert.equal(r.code,1);assert.equal(r.report.reason,'VIEWPORT_LOCKED');
  r=await runCase('capture-broken','wb3_capture.cjs',{WB3_SCENARIOS:'broken'});
  assert.equal(r.code,1);assert.ok(r.report.problems.some(p=>p.startsWith('PAGEERROR ')&&p.includes('wb3-deliberate-broken-page')));
  const occupied=net.createServer();
  await new Promise(resolve=>occupied.listen(0,'127.0.0.1',resolve));
  try {
    r=await runCase('occupied-port','wb3_verify_viewport.cjs',{WB3_PORT:String(occupied.address().port)});
    assert.equal(r.code,2);assert.match(r.report.error,/EADDRINUSE/);assert.equal(occupied.listening,true);
  } finally {await new Promise(resolve=>occupied.close(resolve));}
  r=await runCase('missing-executable','wb3_verify_viewport.cjs',{WB3_NODE:path.join(report,'missing-node.exe')});
  assert.equal(r.code,2);assert.match(r.report.error,/SERVER_SPAWN_ERROR/);
  r=await runCase('ready-timeout','wb3_verify_viewport.cjs',{WB3_READY_TIMEOUT_MS:'1'});
  assert.equal(r.code,2);assert.match(r.report.error,/SERVER_READY_TIMEOUT/);
  r=await runCase('missing-playwright','wb3_capture.cjs',{PLAYWRIGHT_MODULE:path.join(report,'missing-playwright.cjs')});
  assert.equal(r.code,2);assert.ok(r.report.problems.some(p=>p.includes('PLAYWRIGHT_UNAVAILABLE')));
  const failedBrowser=path.join(report,'failed-browser.cjs');
  fs.writeFileSync(failedBrowser,"exports.chromium={launch:async()=>{throw new Error('DELIBERATE_BROWSER_LAUNCH_FAILURE')}};",'utf8');
  r=await runCase('browser-launch-error','wb3_verify_viewport.cjs',{PLAYWRIGHT_MODULE:failedBrowser});
  assert.equal(r.code,2);assert.match(r.report.error,/DELIBERATE_BROWSER_LAUNCH_FAILURE/);
  console.log('RUNNER_CONTROLS_PASS cases='+observations.length);
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
