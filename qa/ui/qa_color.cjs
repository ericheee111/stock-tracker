// 聚焦：全文档扫描含“数据不足暂不发信号”的元素，确认其计算颜色为琥珀（非红）
const PLAYWRIGHT_PATH = 'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright';
const { chromium } = require(PLAYWRIGHT_PATH);
const path = require('path');
const fs = require('fs');
const OUT = path.join(__dirname, 'shots');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => process.stderr.write('[LOG] ' + a.map(String).join(' ') + '\n');

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto('http://127.0.0.1:8080/', { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(e => log('goto-err', e.message));
  await sleep(4500);

  const found = await page.evaluate(() => {
    const targets = [];
    const walk = (node) => {
      if (node.nodeType === 3) { // text node
        if (/数据不足暂不发信号|数据异常不给信号/.test(node.textContent)) {
          // 向上找到最近的含该文本的元素
          let el = node.parentElement;
          const cs = getComputedStyle(el);
          let color = cs.color, borderColor = cs.borderColor, backgroundColor = cs.backgroundColor;
          // 检查自身及子元素的颜色是否命中琥珀
          const collect = (e) => {
            const c = getComputedStyle(e);
            if (/255,\s*159,\s*10/.test(c.color) || /ff9f0a/i.test(c.color)) color = c.color + ' (amber-match)';
            if (/255,\s*159,\s*10/.test(c.borderColor) || /ff9f0a/i.test(c.borderColor)) borderColor = c.borderColor + ' (amber-match)';
            if (/255,\s*69,\s*58/.test(c.color) || /ff453a/i.test(c.color)) color = c.color + ' (RED-match)';
          };
          collect(el);
          el.querySelectorAll('*').forEach(collect);
          targets.push({ text: node.textContent.slice(0, 50), cls: el.className, color, borderColor, backgroundColor });
        }
      } else if (node.nodeType === 1) {
        node.childNodes.forEach(walk);
      }
    };
    document.body && walk(document.body);
    return targets;
  });

  const out = { dataInvalidCards: found };
  log('cards', JSON.stringify(found, null, 2));
  fs.writeFileSync(OUT + '/qa_color.json', JSON.stringify(out, null, 2));
  process.stdout.write(JSON.stringify(out, null, 2) + '\n');
  browser.close().catch(() => {});
  process.exit(0); // 不等待 close（SSE 长连接导致 close 挂起）
})().catch(e => { log('FATAL', e.message); process.exit(1); });
