/* =========================================================================
 * portfolio_crud_qa.cjs —— Stage 1.1 真实 Portfolio UI + REST CRUD 验收
 * 必须由 run_stage1_today_integration.py 提供临时 SQLite 真实后端。
 * ========================================================================= */
const path = require('path');

const unhandledRejections = [];
process.on('unhandledRejection', function (reason) {
  const text = reason && (reason.stack || reason.message || reason);
  unhandledRejections.push(String(text));
  console.error('[unhandledRejection]', text);
});

function loadPlaywright() {
  try { return require('playwright'); } catch (e) {}
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'C:/Users/Administrator/AppData/Local/Temp/uitest/node_modules/playwright'
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch (e) {}
  }
  console.error('[FATAL] 未找到 playwright');
  process.exit(2);
}

const { chromium } = loadPlaywright();
const BASE = (process.env.TODAY_QA_BASE_URL || '').replace(/\/$/, '');
if (!BASE) {
  console.error('[FATAL] portfolio_crud_qa.cjs 需要 TODAY_QA_BASE_URL');
  process.exit(2);
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function readPortfolio(page) {
  const response = await page.request.get(BASE + '/api/portfolio');
  if (!response.ok()) throw new Error('GET /api/portfolio failed: ' + response.status());
  return response.json();
}

async function waitForPortfolio(page, predicate, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 8000);
  let latest = null;
  while (Date.now() < deadline) {
    latest = await readPortfolio(page);
    if (predicate(latest)) return latest;
    await sleep(120);
  }
  throw new Error('portfolio condition timed out: ' + JSON.stringify(latest));
}

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'], headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const text = message.text();
    if (!/Failed to load resource|favicon/i.test(text)) consoleErrors.push(text);
  });
  const steps = [];
  function add(step, ok, detail) {
    steps.push({ step, ok, detail });
    console.log((ok ? 'PASS ' : 'FAIL ') + step + (detail ? ' | ' + detail : ''));
  }

  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#portfolioSummaryCard [data-portfolio-open]', { timeout: 10000 });

    add('Portfolio 摘要卡可见', await page.isVisible('#portfolioSummaryCard'), '');
    await page.click('[data-portfolio-open]');
    await page.waitForSelector('#portfolioProfileForm', { timeout: 8000 });
    add('账户与持仓 Sheet 可打开', await page.isVisible('#portfolioProfileForm'), '');

    const privateInputValue = await page.inputValue('#privateAccessInput');
    add('私有访问值不回显', privateInputValue === '', 'length=' + privateInputValue.length);

    await page.fill('#pf-account_equity', '120000');
    await page.fill('#pf-available_cash', '60000');
    await page.selectOption('#portfolioRiskMode', 'BALANCED');
    await page.fill('#pf-per_trade_risk_pct', '0.7');
    await page.fill('#pf-max_position_pct', '25');
    await page.fill('#pf-max_portfolio_heat_pct', '8');
    await page.fill('#pf-max_sector_pct', '40');
    await page.fill('#pf-max_theme_pct', '40');
    await page.click('#portfolioProfileForm button[type="submit"]');

    const afterProfile = await waitForPortfolio(
      page,
      portfolio => portfolio.profile && portfolio.profile.account_equity === 120000 &&
        portfolio.profile.available_cash === 60000 &&
        Math.abs(portfolio.profile.per_trade_risk_pct - 0.007) < 1e-12,
      10000
    );
    add('账户参数通过真实 PUT 持久化', true,
      'equity=' + afterProfile.profile.account_equity + ' risk=' + afterProfile.profile.per_trade_risk_pct);
    await page.waitForFunction(function () {
      const profile = document.querySelector('#portfolioProfileForm');
      const create = document.querySelector('#portfolioPositionCreateForm');
      const equity = document.querySelector('#pf-account_equity');
      const profileSubmit = profile && profile.querySelector('button[type="submit"]');
      const createSubmit = create && create.querySelector('button[type="submit"]');
      return profile && create && equity && equity.value === '120000' &&
        profileSubmit && !profileSubmit.disabled &&
        createSubmit && !createSubmit.disabled;
    }, null, { timeout: 10000 });

    await page.selectOption('#newPositionMarket', 'A');
    await page.fill('#pf-symbol', '600519.SH');
    await page.fill('#pf-shares', '37');
    await page.fill('#pf-average_cost', '1600');
    await page.click('#portfolioPositionCreateForm button[type="submit"]');

    const afterCreate = await waitForPortfolio(
      page,
      portfolio => portfolio.positions.some(position =>
        position.symbol === '600519.SH' && position.shares === 37 && position.average_cost === 1600),
      10000
    );
    const created = afterCreate.positions.find(position => position.symbol === '600519.SH');
    add('37 股零碎持仓通过真实 POST 持久化', !!created, 'id=' + (created && created.id));

    await page.waitForSelector('[data-position-form][data-position-id="' + created.id + '"]', { timeout: 8000 });
    const row = page.locator('[data-position-form][data-position-id="' + created.id + '"]');
    await row.locator('input[name="shares"]').fill('13');
    await row.locator('input[name="average_cost"]').fill('1550.5');
    await row.locator('button[type="submit"]').click();

    const afterPatch = await waitForPortfolio(
      page,
      portfolio => portfolio.positions.some(position =>
        position.id === created.id && position.shares === 13 &&
        Math.abs(position.average_cost - 1550.5) < 1e-12),
      10000
    );
    add('零碎持仓通过真实 PATCH 更新', true,
      'shares=' + afterPatch.positions.find(position => position.id === created.id).shares);

    await page.waitForSelector('[data-position-delete][data-position-id="' + created.id + '"]', { timeout: 8000 });
    const deleteButton = page.locator('[data-position-delete][data-position-id="' + created.id + '"]');
    await deleteButton.click();
    const confirmText = await deleteButton.textContent();
    add('删除需要二次确认', /再次点击确认删除/.test(confirmText || ''), 'text=' + confirmText);
    await deleteButton.click();

    await waitForPortfolio(
      page,
      portfolio => !portfolio.positions.some(position => position.id === created.id),
      10000
    );
    add('持仓通过真实 DELETE 删除', true, created.id);

    await page.waitForFunction(function () {
      const card = document.querySelector('#portfolioSummaryCard');
      if (!card) return false;
      const text = card.innerText || '';
      const button = card.querySelector('[data-portfolio-open]');
      return /120,000/.test(text) && /当前持仓\s*1\s*只/.test(text) &&
        button && !button.disabled && !/处理中/.test(button.textContent || '');
    }, null, { timeout: 10000 });
    const summaryText = await page.textContent('#portfolioSummaryCard');
    add('主页面摘要进入最终稳定态',
      /120,000/.test(summaryText || '') && /当前持仓\s*1\s*只/.test(summaryText || '') &&
        !/处理中/.test(summaryText || ''),
      'summary=' + summaryText);

    await page.setViewportSize({ width: 390, height: 844 });
    await sleep(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    add('Portfolio 390px 无横向溢出', overflow <= 2, 'overflowPx=' + overflow);

    add('无 pageerror', pageErrors.length === 0, 'count=' + pageErrors.length);
    add('无未捕获控制台错误', consoleErrors.length === 0, 'count=' + consoleErrors.length);
    add('无未处理 Promise rejection', unhandledRejections.length === 0,
      'count=' + unhandledRejections.length);
  } catch (error) {
    add('FATAL', false, error.message);
    console.error(error.stack);
  } finally {
    const closePromise = browser.close().catch(function () {});
    await Promise.race([closePromise, sleep(4000)]);
  }

  const failed = steps.filter(step => !step.ok);
  console.log('\n==== PORTFOLIO CRUD QA SUMMARY ====');
  console.log('total=' + steps.length + ' pass=' + (steps.length - failed.length) + ' fail=' + failed.length);
  if (failed.length) {
    failed.forEach(item => console.log('  FAIL: ' + item.step + ' | ' + item.detail));
    process.exit(1);
  }
  console.log('ALL PASS');
  process.exit(0);
})().catch(error => {
  console.error('FATAL', error.message, error.stack);
  process.exit(2);
});
