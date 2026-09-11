/* =========================================================================
 * wb3_verdict.cjs —— WB3 QA 退出码判定（纯函数，可单测）
 * 把逐组合采集结果归一为 { exitCode, problems }：
 *   - 意外 pageerror / requestfailed / DOM 问题 / 场景异常 / 组合数不匹配 → 非零
 *   - console error 按场景白名单：error 场景允许 500、auth 场景允许 401/403，
 *     其余 console error 一律计为问题（不把所有 console error 当成成功或失败）。
 * 用途：wb3_capture.cjs 据此设置 process.exitCode；wb3_runner_negative.test.cjs 单测此判定。
 * ========================================================================= */
'use strict';
const {ALL_COMBOS,comboKey,expectedState} = require('./wb3_matrix.cjs');

function isExpectedConsoleError(scenario, text) {
  if (!text) return false;
  const s = String(scenario || '');
  if (s === 'error') return /the server responded with a status of 500/.test(text);
  if (s === 'auth') return /the server responded with a status of (401|403)/.test(text);
  return false;
}

function computeVerdict(results, expectedCombos) {
  const problems = [];
  const list = Array.isArray(results) ? results : [];
  if (!Array.isArray(expectedCombos) || !expectedCombos.length || !list.length) {
    return {exitCode:2,status:'NOT_VALIDATED',problems:['EMPTY_OR_UNSPECIFIED_MATRIX']};
  }
  const expected = new Map(expectedCombos.map(c => [comboKey(c), c]));
  if (expected.size !== expectedCombos.length || expectedCombos.some(c => !ALL_COMBOS.some(a => comboKey(a) === comboKey(c)))) {
    return {exitCode:2,status:'NOT_VALIDATED',problems:['INVALID_EXPECTED_MATRIX']};
  }
  const seen = new Set();
  if (list.length !== expectedCombos.length) {
    problems.push('COMBO_COUNT_MISMATCH expected=' + expectedCombos.length + ' actual=' + list.length);
  }
  list.forEach(function (r) {
    const combo = r && r.combo ? r.combo : {};
    const key = comboKey(combo);
    if (!expected.has(key)) problems.push('UNEXPECTED_COMBO ' + key);
    if (seen.has(key)) problems.push('DUPLICATE_COMBO ' + key);
    seen.add(key);
    const tag = '#' + (combo.n != null ? combo.n : '?') + ' ' + (combo.scenario || '?') + '/' + (combo.page || '?');
    if (!r || r.completed !== true || !r.dom || r.dom.collected !== true ||
        !Array.isArray(r.dom.issues) || r.exceptionCollection?.completed !== true ||
        !['pageErrors','failedRequests','consoleErrors','apiStatus'].every(k => Array.isArray(r[k]))) {
      problems.push('INCOMPLETE_CAPTURE ' + tag);
      return;
    }
    if (r.dom.activePage !== 'page-' + combo.page || r.dom.runtimeState !== expectedState(combo) ||
        r.dom.viewportWidth !== combo.width || r.dom.theme !== combo.theme ||
        (combo.tab && r.dom.activeMonitorTab !== combo.tab)) problems.push('UNEXPECTED_PAGE_STATE ' + tag);
    if (!r.apiStatus.some(s=>s.path === '/api/runtime/health' && s.status === 200)) problems.push('MISSING_HEALTH_CAPTURE ' + tag);
    if (combo.scenario === 'auth' && !r.apiStatus.some(s=>s.status === 401)) problems.push('MISSING_AUTH_CONTROL ' + tag);
    if (combo.scenario === 'error' && !r.apiStatus.some(s=>s.status === 500)) problems.push('MISSING_ERROR_CONTROL ' + tag);
    if (r.error) {
      problems.push('SCENARIO_ERROR ' + tag + ': ' + String(r.error).slice(0, 160));
    }
    if (r.pageErrors && r.pageErrors.length) {
      r.pageErrors.forEach(function (pe) { problems.push('PAGEERROR ' + tag + ': ' + String(pe).slice(0, 160)); });
    }
    if (r.failedRequests && r.failedRequests.length) {
      r.failedRequests.forEach(function (f) { problems.push('REQUESTFAILED ' + tag + ': ' + String(f.url).slice(0, 120) + ' ' + (f.error || '')); });
    }
    if (r.dom && r.dom.issues && r.dom.issues.length) {
      r.dom.issues.forEach(function (iss) { problems.push('DOM_ISSUE ' + tag + ': ' + (iss.type || '?') + ' ' + (iss.detail || '').slice(0, 120)); });
    }
    if (r.consoleErrors && r.consoleErrors.length) {
      r.consoleErrors.forEach(function (ce) {
        if (!isExpectedConsoleError(combo.scenario, ce)) {
          problems.push('CONSOLE_ERROR ' + tag + ': ' + String(ce).slice(0, 120));
        }
      });
    }
  });
  for (const key of expected.keys()) if (!seen.has(key)) problems.push('MISSING_COMBO ' + key);
  return { exitCode: problems.length ? 1 : 0, status: problems.length ? 'FAILED' : 'PASS', problems: problems };
}

module.exports = { computeVerdict: computeVerdict, isExpectedConsoleError: isExpectedConsoleError };
