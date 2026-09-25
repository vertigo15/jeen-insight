/**
 * Code-only chart intent matcher: exact intents resolve without a model call,
 * anything uncertain returns null so the LLM handles it.
 * Run with: node tests/js/test_chart_intent_matcher.mjs
 */
import assert from 'node:assert/strict';
import { matchChartIntent } from '../../src/static/chart-feature/utils/chartIntentMatcher.js';

const ops = (text, options) => (matchChartIntent(text, options)?.operations || []).map((operation) => operation.op);

// Chart types, including the compound ones.
assert.deepEqual(matchChartIntent('pie chart').operations, [{ op: 'set_chart_type', chart_type: 'pie' }]);
assert.deepEqual(matchChartIntent('make it a donut').operations, [{ op: 'set_chart_type', chart_type: 'donut' }]);
assert.deepEqual(matchChartIntent('stacked bar').operations, [{ op: 'set_chart_type', chart_type: 'stacked_bar' }]);
assert.deepEqual(ops('horizontal bars, biggest first'), ['set_sort', 'set_chart_type']);
assert.equal(matchChartIntent('horizontal bars, biggest first').operations[1].chart_type, 'horizontal_bar');
assert.deepEqual(matchChartIntent('הפוך לגרף עוגה').operations, [{ op: 'set_chart_type', chart_type: 'pie' }]);

// Toggles, sort, stack, format.
assert.deepEqual(matchChartIntent('hide the legend').operations, [{ op: 'set_toggle', key: 'legend', value: false }]);
assert.deepEqual(matchChartIntent('הסתר את המקרא').operations, [{ op: 'set_toggle', key: 'legend', value: false }]);
assert.deepEqual(matchChartIntent('show the values on top of the bars').operations, [{ op: 'set_toggle', key: 'dataLabels', value: true }]);
assert.deepEqual(matchChartIntent('add a zoom slider').operations, [{ op: 'set_toggle', key: 'dataZoom', value: true }]);
assert.deepEqual(matchChartIntent('sort high to low').operations, [{ op: 'set_sort', direction: 'desc' }]);
assert.deepEqual(matchChartIntent('סדר מהקטן לגדול').operations, [{ op: 'set_sort', direction: 'asc' }]);
assert.deepEqual(matchChartIntent('unstack').operations, [{ op: 'set_stack', stacked: false }]);
assert.deepEqual(matchChartIntent('format as USD').operations, [{ op: 'set_format', kind: 'currency', compact: true, symbol: '$' }]);
assert.deepEqual(matchChartIntent('בשקלים').operations, [{ op: 'set_format', kind: 'currency', compact: true, symbol: '₪' }]);
assert.deepEqual(matchChartIntent('show as percent').operations, [{ op: 'set_format', kind: 'percent', compact: true, symbol: '' }]);
assert.deepEqual(matchChartIntent('full numbers').operations, [{ op: 'set_format', kind: 'number', compact: false, symbol: '' }]);

// Colours: a colour word recolours every series; palettes need a qualifier or
// a generic name; "shades of" produces a tint ramp.
assert.deepEqual(matchChartIntent('make it green').operations, [{ op: 'set_color', target: 'all', color: '#16a34a' }]);
assert.deepEqual(matchChartIntent('צבע ירוק').operations, [{ op: 'set_color', target: 'all', color: '#16a34a' }]);
assert.deepEqual(ops('pastel colors'), ['set_palette']);
assert.deepEqual(ops('blue palette'), ['set_palette']);
assert.deepEqual(ops('shades of orange'), ['set_palette']);
assert.equal(matchChartIntent('shades of orange').operations[0].colors.length, 5);
assert.deepEqual(ops('גווני כחול'), ['set_palette']);
assert.deepEqual(ops('change to pie chart with pastel colors'), ['set_chart_type', 'set_palette']);
assert.deepEqual(ops('line chart in green with labels'), ['set_toggle', 'set_chart_type', 'set_color']);

// Typos and negation (carried over from the earlier quick-intent parser).
assert.deepEqual(ops('all lables and change colore to red'), ['set_toggle', 'set_color']);
assert.deepEqual(matchChartIntent("don't show labels").operations, [{ op: 'set_toggle', key: 'dataLabels', value: false }]);
assert.deepEqual(matchChartIntent('do not show the legend').operations, [{ op: 'set_toggle', key: 'legend', value: false }]);
assert.equal(matchChartIntent("don't hide the legend"), null, 'double negation defers to the model');
assert.deepEqual(matchChartIntent('piechart').operations, [{ op: 'set_chart_type', chart_type: 'pie' }]);

// Overlays, scenarios, reset.
assert.deepEqual(matchChartIntent('remove the trend line').operations, [{ op: 'remove_overlay', operator: 'linear_trend' }]);
assert.deepEqual(matchChartIntent('remove the scenario').operations, [{ op: 'scenario_clear' }]);
assert.deepEqual(matchChartIntent('reset'), { operations: [], reset: true });
assert.deepEqual(matchChartIntent('undo'), { operations: [], reset: true });
assert.equal(matchChartIntent('reset and make it green'), null, 'reset never combines with edits');

// Uncertain requests defer to the model.
for (const text of [
    'group by quarter',
    'what if bikes were 30K',
    'make the line thicker',
    'add a 3 month moving average',
    'make it better',
    'india colors',
    'white background',
    'pie chart and bar chart',
    '',
    'x',
]) {
    assert.equal(matchChartIntent(text), null, `"${text}" must fall back to the LLM`);
}

// ML charts: structural intents defer (the model explains the lock); view edits still apply.
assert.equal(matchChartIntent('pie chart', { chartKind: 'ml_band' }), null);
assert.equal(matchChartIntent('pastel colors', { chartKind: 'ml_band' }), null);
assert.deepEqual(ops('hide legend', { chartKind: 'ml_band' }), ['set_toggle']);
assert.deepEqual(ops('show as percent', { chartKind: 'ml_basic' }), ['set_format']);

console.log('chart intent matcher JS tests passed');
