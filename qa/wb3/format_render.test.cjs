/* =========================================================================
 * format_render.test.cjs —— WB3 展示边界红测（Node，直接加载真实 format.js/components.js）
 * 断言依据：W5 冻结修复合同 §4.2 A/B/C（缺失≠0；全缺→暂无评分；部分缺→'—'；
 *           缺失风险分不得绿色；change 优先 change_pct，仅用 last+prev_close 推导，
 *           无 prev_close 只有 open 保持未知；真实 0 仍显示 0）。
 * 运行：node qa/wb3/format_render.test.cjs   （在 clone 根目录执行）
 * 退出码：0=全绿，1=存在失败（先红后绿由执行顺序决定）。
 * ========================================================================= */
'use strict';
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const files = {
  format: path.join(ROOT, 'web', 'js', 'format.js'),
  components: path.join(ROOT, 'web', 'js', 'components.js')
};
const fs = require('fs');

// 构造共享 global，加载真实 IIFE 模块
const sandbox = { console: console };
sandbox.window = sandbox;
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(files.format, 'utf8'), sandbox, { filename: 'format.js' });
vm.runInContext(fs.readFileSync(files.components, 'utf8'), sandbox, { filename: 'components.js' });

const Fmt = sandbox.Fmt;
const UI = sandbox.UI;
const fnum = (Fmt && typeof Fmt.fnum === 'function') ? Fmt.fnum.bind(Fmt) : function () { return null; };

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, got) {
  if (cond) { pass += 1; }
  else { fail += 1; failures.push(name + ' => ' + JSON.stringify(got)); }
}
function hasText(html, needle) { return String(html).indexOf(needle) !== -1; }

/* ---- C：日涨跌幅推导 ---- */
check('change_pct 缺失且无 prev_close → null（不显示为0）', Fmt.quoteChangePct({ last: 10, change_pct: null }) === null, Fmt.quoteChangePct({ last: 10, change_pct: null }));
check('change_pct 有效优先使用', Fmt.quoteChangePct({ last: 11, change_pct: 2.5 }) === 2.5, Fmt.quoteChangePct({ last: 11, change_pct: 2.5 }));
check('仅 open 无 prev_close → 保持未知 null', Fmt.quoteChangePct({ last: 11, open: 10 }) === null, Fmt.quoteChangePct({ last: 11, open: 10 }));
check('合法 last+prev_close 推导', Math.abs(Fmt.quoteChangePct({ last: 11, prev_close: 10 }) - 10) < 1e-9, Fmt.quoteChangePct({ last: 11, prev_close: 10 }));
check('缺失 last 不得推导成 -100%', Fmt.quoteChangePct({ prev_close: 10, last: null }) === null, Fmt.quoteChangePct({ prev_close: 10, last: null }));
check('change_pct 为布尔不视为数字', Fmt.quoteChangePct({ last: 11, change_pct: true }) === null, Fmt.quoteChangePct({ last: 11, change_pct: true }));
check('负 last 不得推导（last=-10）', Fmt.quoteChangePct({ last: -10, prev_close: 10 }) === null, Fmt.quoteChangePct({ last: -10, prev_close: 10 }));
check('负 prev_close 不得推导（prev_close=-10）', Fmt.quoteChangePct({ last: 10, prev_close: -10 }) === null, Fmt.quoteChangePct({ last: 10, prev_close: -10 }));
check('last=0 视为未知（不推导）', Fmt.quoteChangePct({ last: 0, prev_close: 10 }) === null, Fmt.quoteChangePct({ last: 0, prev_close: 10 }));
check('quoteChangePct 数组输入→null', Fmt.quoteChangePct([10, 11]) === null, Fmt.quoteChangePct([10, 11]));
check('change_pct 数字字符串兼容 "2.5"→2.5', Fmt.quoteChangePct({ last: 11, change_pct: '2.5' }) === 2.5, Fmt.quoteChangePct({ last: 11, change_pct: '2.5' }));

/* ---- WB3-01：价格与报价边界（finite + 合法正值）---- */
check('fmtPrice(null)→"—"', Fmt.fmtPrice(null) === '—', Fmt.fmtPrice(null));
check('fmtPrice(undefined)→"—"', Fmt.fmtPrice(undefined) === '—', Fmt.fmtPrice(undefined));
check('fmtPrice(true)→"—"', Fmt.fmtPrice(true) === '—', Fmt.fmtPrice(true));
check('fmtPrice([10])→"—"', Fmt.fmtPrice([10]) === '—', Fmt.fmtPrice([10]));
check('fmtPrice(0)→"0.00"（真实 0 保留）', Fmt.fmtPrice(0) === '0.00', Fmt.fmtPrice(0));
check('fmtPrice("10.5")→"10.50"（数字字符串兼容）', Fmt.fmtPrice('10.5') === '10.50', Fmt.fmtPrice('10.5'));
check('quotePrice({last:true})→"—"（bool 未知）', Fmt.quotePrice({ last: true }) === '—', Fmt.quotePrice({ last: true }));
check('quotePrice({last:-5})→"—"（负值未知）', Fmt.quotePrice({ last: -5 }) === '—', Fmt.quotePrice({ last: -5 }));
check('quotePrice({last:[10]})→"—"（数组未知）', Fmt.quotePrice({ last: [10] }) === '—', Fmt.quotePrice({ last: [10] }));
check('quotePrice({last:0})→"—"（0 最新价未知）', Fmt.quotePrice({ last: 0 }) === '—', Fmt.quotePrice({ last: 0 }));
check('quotePrice({last:10.456})→"10.46"（合法正值）', Fmt.quotePrice({ last: 10.456 }) === '10.46', Fmt.quotePrice({ last: 10.456 }));
check('quotePrice({last:"10.5"})→"10.50"（数字字符串兼容）', Fmt.quotePrice({ last: '10.5' }) === '10.50', Fmt.quotePrice({ last: '10.5' }));
check('quotePrice(null)→"—"', Fmt.quotePrice(null) === '—', Fmt.quotePrice(null));

/* ---- A：fnum 有限数边界 ---- */
check('fnum(0)=0（真实 0 保留）', fnum(0) === 0, fnum(0));
check('fnum(null)=null', fnum(null) === null, fnum(null));
check('fnum(undefined)=null', fnum(undefined) === null, fnum(undefined));
check('fnum("")=null', fnum('') === null, fnum(''));
check('fnum("  ")=null（纯空白）', fnum('   ') === null, fnum('   '));
check('fnum(true)=null', fnum(true) === null, fnum(true));
check('fnum(NaN)=null', fnum(NaN) === null, fnum(NaN));
check('fnum(Infinity)=null', fnum(Infinity) === null, fnum(Infinity));
check('fnum({})=null', fnum({}) === null, fnum({}));
check('fnum([])=null', fnum([]) === null, fnum([]));
check('fnum("abc")=null', fnum('abc') === null, fnum('abc'));
check('fnum("10.5")=10.5（兼容数字字符串）', fnum('10.5') === 10.5, fnum('10.5'));

/* ---- B：fmtPct 显示边界 ---- */
check('fmtPct(null)="—"', Fmt.fmtPct(null) === '—', Fmt.fmtPct(null));
check('fmtPct(undefined)="—"', Fmt.fmtPct(undefined) === '—', Fmt.fmtPct(undefined));
check('fmtPct(0)="+0.00%"（真实 0）', Fmt.fmtPct(0) === '+0.00%', Fmt.fmtPct(0));
check('fmtPct(-2.5)="-2.50%"', Fmt.fmtPct(-2.5) === '-2.50%', Fmt.fmtPct(-2.5));

/* ---- B：renderScores 全缺/部分缺 ---- */
check('renderScores(null)→暂无评分', hasText(UI.renderScores(null), '暂无评分'), UI.renderScores(null));
check('renderScores({})→暂无评分', hasText(UI.renderScores({}), '暂无评分'), UI.renderScores({}));
check('renderScores(全 null 字段)→暂无评分', hasText(UI.renderScores({ opportunity: null, timing: null, risk: null, confidence: null }), '暂无评分'), UI.renderScores({ opportunity: null, timing: null, risk: null, confidence: null }));
const partial = UI.renderScores({ opportunity: 72, timing: null, risk: null, confidence: 58 });
check('部分缺失→对应字段显示「—」', (partial.match(/—/g) || []).length >= 2, partial);
check('部分缺失→不出现「0机会/0时机/0风险/0置信」0 值', !hasText(partial, '>0<'), partial);
check('部分缺失风险分→不是绿色（score-good）', partial.indexOf('--score-good') === -1, partial);
check('完整分数仍渲染 4 个 score-cell', (UI.renderScores({ opportunity: 1, timing: 2, risk: 3, confidence: 4 }).match(/score-cell/g) || []).length === 4, UI.renderScores({ opportunity: 1, timing: 2, risk: 3, confidence: 4 }));

/* ---- B：renderRiskCard 缺失风险分不得显示「风险 0」---- */
const riskNull = UI.renderRiskCard([{ symbol: '600000.SH', market: 'A', level: 'MEDIUM', risk_score: null, state: 'OVEREXTENDED', reason: 'x' }]);
check('risk_score null → 显示「风险 —」而非「风险 0」', hasText(riskNull, '风险 —') && !hasText(riskNull, '风险 0</'), riskNull);

/* ---- 真实 0 不得被误当缺失（score 真实 0 仍显示 0）---- */
const zeroScore = UI.renderScores({ opportunity: 0, timing: 0, risk: 0, confidence: 0 });
check('真实 0 分数仍渲染 4 个 score-cell（不折叠为暂无评分）', (zeroScore.match(/score-cell/g) || []).length === 4, zeroScore);

console.log('PASS=' + pass + ' FAIL=' + fail);
if (fail) { failures.forEach(function (f) { console.log('  FAIL ' + f); }); process.exitCode = 1; }
else { console.log('FORMAT_RENDER_GREEN'); }
