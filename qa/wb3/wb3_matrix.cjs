'use strict';
const HEIGHTS = Object.freeze({360:740,390:844,768:1024,1440:900});
const combos = [];
function add(scenario, page, width, theme = 'dark', tab = null) {
  combos.push(Object.freeze({n:combos.length+1,scenario,page,width,theme,tab}));
}
for (const page of ['today','overview','watch','radar','monitor','research']) {
  for (const width of [360,390,768,1440]) add('full',page,width);
}
for (const page of ['today','overview','radar']) add('full',page,390,'light');
for (const page of ['overview','watch','radar','monitor']) for (const width of [390,1440]) add('empty',page,width);
for (const page of ['today','monitor']) for (const width of [390,1440]) add('error',page,width);
for (const page of ['today','overview']) add('stale',page,390);
add('auth','today',390);
for (const page of ['today','watch','overview']) add('nulls',page,390);
for (const page of ['watch','radar','overview']) add('longnames',page,390);
for (const tab of ['rules','data','replay']) for (const width of [390,1440]) add('full','monitor',width,'dark',tab);
const EXPECTED_COMBOS = Object.freeze([...combos]);
add('broken','today',390); add('locked','today',390); add('servererror','overview',390);
const ALL_COMBOS = Object.freeze([...combos]);
function comboKey(c) {
  return JSON.stringify(['n','scenario','page','width','theme','tab'].map(k => c?.[k]));
}
function selectCombos(requested) {
  if (requested == null) return EXPECTED_COMBOS;
  const names = requested.split(',').map(s=>s.trim()).filter(Boolean);
  if (!names.length || names.some(s=>!ALL_COMBOS.some(c=>c.scenario===s))) throw new Error('UNKNOWN_OR_EMPTY_SCENARIO');
  return ALL_COMBOS.filter(c=>names.includes(c.scenario));
}
function expectedState(combo) { return combo.scenario === 'stale' ? 'STALE' : combo.scenario === 'auth' ? 'AUTH_REQUIRED' : 'ONLINE'; }
module.exports = {HEIGHTS,EXPECTED_COMBOS,ALL_COMBOS,comboKey,selectCombos,expectedState};
