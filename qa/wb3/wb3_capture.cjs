'use strict';
const fs = require('node:fs');
const path = require('node:path');
const runtime = require('./wb3_runtime.cjs');
const {HEIGHTS,selectCombos} = require('./wb3_matrix.cjs');
const {computeVerdict} = require('./wb3_verdict.cjs');

async function inspectPage(page, combo) {
  return page.evaluate((cfg) => {
    const de = document.documentElement;
    const issues = [];
    // 1. 横向溢出
    const overflowDoc = de.scrollWidth - de.clientWidth;
    if (overflowDoc > 1) {
      // 找出溢出源（近似：右边界超出视口的可见元素，取前 5 个）
      const offenders = [];
      document.querySelectorAll('body *').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > de.clientWidth + 1 && offenders.length < 5) {
          offenders.push(el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ').slice(0, 2).join('.') : '') + '@' + Math.round(r.right));
        }
      });
      issues.push({ type: 'H_OVERFLOW', detail: 'scrollW=' + de.scrollWidth + ' clientW=' + de.clientWidth + ' offenders=' + offenders.join('|') });
    }
    // 2. 脏字符串（关键容器可见文本）
    const dirty = [];
    const containers = document.querySelectorAll('.content, #banner, #runtimeStatus, .disclaimer');
    containers.forEach((c) => {
      const t = c.innerText || '';
      if (t.indexOf('[object Object]') !== -1) dirty.push('[object Object]@' + (c.id || c.className));
      if (/(^|\s)undefined(\s|$|[，。])/.test(t)) dirty.push('undefined@' + (c.id || c.className));
      if (/(^|\s)null(\s|$|[，。])/.test(t)) dirty.push('null@' + (c.id || c.className));
    });
    if (dirty.length) issues.push({ type: 'DIRTY_TEXT', detail: dirty.join('; ') });
    // 3. 数据状态徽章 & 时间
    const badges = document.querySelectorAll('.ds-badge').length;
    const ageText = /(\d+s|\d+m|\d+h) 前/.test(document.body.innerText);
    // 4. 可交互元素可聚焦性（native button/role）
    const navBtns = document.querySelectorAll('.bottom-nav .nav-btn');
    const nonButtonNav = Array.from(navBtns).filter((b) => b.tagName !== 'BUTTON').length;
    // 5. aria 标签
    const aria = {
      bottomNav: !!(document.querySelector('.bottom-nav') && document.querySelector('.bottom-nav').getAttribute('aria-label')),
      marketTabs: !!(document.querySelector('#marketTabs') && document.querySelector('#marketTabs').getAttribute('aria-label')),
      themeToggle: !!(document.querySelector('#themeToggle') && document.querySelector('#themeToggle').getAttribute('aria-label'))
    };
    // 6. 遮挡粗检：banner / runtimeStatus / bottom-nav 可见尺寸
    const vis = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return { w: Math.round(r.width), h: Math.round(r.height), display: st.display, hidden: el.hidden || st.visibility === 'hidden' };
    };
    return {
      collected: true,
      runtimeState: document.querySelector('#runtimeStatus')?.dataset.runtimeState || null,
      viewportWidth: window.innerWidth,
      theme: document.documentElement.getAttribute('data-theme') || 'dark',
      activeMonitorTab: document.querySelector('[data-monitor-tab][aria-selected="true"]')?.dataset.monitorTab || null,
      issues: issues,
      badges: badges,
      ageTextVisible: ageText,
      nonButtonNav: nonButtonNav,
      aria: aria,
      banner: vis('#banner'),
      bottomNav: vis('.bottom-nav'),
      activePage: (document.querySelector('.page.active') || {}).id || null,
      scrollH: de.scrollHeight
    };
  }, { page: combo.page });
}


async function keyboardFocusProbe(page) {
  const sequence = [];
  for (let i=0; i<8; i++) {
    await page.keyboard.press('Tab');
    sequence.push(await page.evaluate(() => {
      const el = document.activeElement;
      return {tag:el?.tagName,id:el?.id,label:el?.getAttribute('aria-label')};
    }));
  }
  return {method:'Playwright keyboard.press(Tab)',sequence};
}

async function main() {
  const report = runtime.reportDirectory('capture');
  const shots = path.join(report,'screenshots'), logs = path.join(report,'logs');
  fs.mkdirSync(shots,{recursive:true}); fs.mkdirSync(logs,{recursive:true});
  const output = path.join(logs,'capture-results.json');
  if (fs.existsSync(output)) throw new Error('REFUSE_OVERWRITE: ' + output);
  const results = [];
  let browser, server, expected = [], verdict;
  try {
    expected = selectCombos(process.env.WB3_SCENARIOS);
    fs.writeFileSync(path.join(logs,'expected-combos.json'),JSON.stringify(expected,null,2),'utf8');
    browser = await runtime.loadPlaywright().chromium.launch({headless:true});
    for (const scenario of [...new Set(expected.map(c=>c.scenario))]) {
      server = await runtime.startServer(scenario);
      try {
        for (const combo of expected.filter(c=>c.scenario===scenario)) {
          let context;
          const consoleErrors=[],pageErrors=[],failedRequests=[],apiStatus=[];
          try {
            context = await browser.newContext({viewport:{width:combo.width,height:HEIGHTS[combo.width]},deviceScaleFactor:1});
            await context.addInitScript(cfg=>{
              localStorage.setItem('stk-theme',cfg.theme);
              if(cfg.scenario!=='auth') {
                sessionStorage.setItem('stockTrackerPrivateAccess::'+encodeURIComponent(''),'wb3-synthetic-private-access-value-0123456789abcdef');
                sessionStorage.setItem('stockTrackerPrivateAccess','wb3-synthetic-private-access-value-0123456789abcdef');
              }
            },{theme:combo.theme,scenario});
            const page = await context.newPage();
            page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
            page.on('pageerror',e=>pageErrors.push(e.stack||String(e)));
            page.on('requestfailed',r=>{
              if(new URL(r.url()).pathname==='/api/stream')return;
              failedRequests.push({url:r.url(),error:r.failure()?.errorText});
            });
            page.on('response',r=>{
              const pathname=new URL(r.url()).pathname;
              if(pathname.startsWith('/api/'))apiStatus.push({path:pathname,status:r.status()});
            });
            await page.goto(server.url,{waitUntil:'domcontentloaded',timeout:20000});
            await page.waitForSelector('#runtimeStatus[data-runtime-state]',{timeout:12000});
            await page.waitForTimeout(1200);
            if(combo.page!=='today') {
              await page.locator('.nav-btn[data-page="'+combo.page+'"]').click();
              await page.waitForSelector('#page-'+combo.page+'.active',{state:'visible',timeout:12000});
              await page.waitForTimeout(1000);
            }
            if(combo.page==='monitor') {
              await page.waitForTimeout(1500);
              if(combo.tab) {
                await page.locator('[role="tab"][data-monitor-tab="'+combo.tab+'"]').click();
                await page.waitForTimeout(800);
              }
            }
            const dom = await inspectPage(page,combo);
            const focus = await keyboardFocusProbe(page);
            const shot = String(combo.n).padStart(3,'0')+'_'+scenario+'_'+combo.page+'_'+combo.width+'_'+combo.theme+(combo.tab?'_'+combo.tab:'')+'.png';
            await page.screenshot({path:path.join(shots,shot),fullPage:false});
            results.push({combo,shot,completed:true,dom,focus,consoleErrors,pageErrors,failedRequests,apiStatus,
              exceptionCollection:{completed:true},serverRunId:server.runId});
            console.log('CAPTURED '+shot+' state='+dom.runtimeState+' issues='+dom.issues.length);
          } catch(error) {
            results.push({combo,completed:false,error:error.stack||String(error),consoleErrors,pageErrors,failedRequests,apiStatus});
            console.error('SCENARIO_ERROR '+combo.n+' '+error);
          } finally {
            if(context)await runtime.bounded(()=>context.close());
            fs.writeFileSync(output,JSON.stringify(results,null,2),'utf8');
          }
        }
      } finally {await server.stop();server=null;}
    }
    verdict=computeVerdict(results,expected);
  } catch(error) {
    verdict={exitCode:2,status:'RUNNER_ERROR',problems:['CAPTURE_RUNNER_ERROR '+(error.stack||String(error))]};
  } finally {
    for(const cleanup of [()=>server&&server.stop(),()=>browser&&runtime.bounded(()=>browser.close())]) {
      try {await cleanup();} catch(error) {
        verdict.problems.push('CLEANUP_ERROR '+error);
        if(verdict.exitCode===0)Object.assign(verdict,{exitCode:2,status:'RUNNER_ERROR'});
      }
    }
    fs.writeFileSync(output,JSON.stringify(results,null,2),'utf8');
    fs.writeFileSync(path.join(logs,'capture-verdict.json'),JSON.stringify(verdict,null,2),'utf8');
    console.log('WB3_CAPTURE_DONE '+JSON.stringify({total:results.length,...verdict}));
  }
  return verdict.exitCode;
}
module.exports={main};
if(require.main===module)main().then(code=>process.exit(code)).catch(error=>{
  console.error('CAPTURE_RUNNER_ERROR '+(error.stack||error));process.exit(2);
});
