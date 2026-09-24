/**
 * Render-only transforms: sorting and aligned derived overlays.
 * Run with: node tests/js/test_chart_state_transforms.mjs
 */
import assert from 'node:assert/strict';
import { applyQuickOptions } from '../../src/static/chart-feature/utils/chartQuickOptions.js';
import { applyDerivedSeries } from '../../src/static/chart-feature/utils/chartOperators.js';

const edited = {
    xAxis: { type: 'category', data: ['A', 'A', 'C'] },
    yAxis: { type: 'value' },
    series: [
        { type: 'bar', name: 'Revenue', data: [10, 30, 20] },
        { type: 'line', name: 'Margin', data: [1, 3, 2] },
    ],
};

const sortOff = applyQuickOptions(edited, { sortDesc: false });
assert.deepEqual(sortOff, edited, 'sort-off is identity over semantic working state');
assert.notEqual(sortOff, edited, 'identity semantics still return a safe copy');

const sorted = applyQuickOptions(edited, { sortDesc: true });
assert.deepEqual(sorted.xAxis.data, ['A', 'C', 'A']);
assert.deepEqual(sorted.series[0].data, [30, 20, 10]);
assert.deepEqual(sorted.series[1].data, [3, 2, 1], 'every aligned series uses the same stable permutation');
assert.deepEqual(edited.series[0].data, [10, 30, 20], 'sort does not mutate working data');

// The raw rows are intentionally in a different order and contain unaggregated
// values. The overlay must derive from the chart's aligned Revenue series.
const rawResults = {
    columns: ['region', 'Revenue'],
    rows: [['C', 2], ['A', 1], ['A', 3]],
};
const withDerived = applyDerivedSeries(
    edited,
    [{ operator: 'cumulative_sum', source_column: 'Revenue', label: 'Running revenue' }],
    rawResults,
).config;
assert.deepEqual(withDerived.series.at(-1).data, [10, 40, 60]);

// Derived data participates in the same category permutation.
const sortedWithDerived = applyQuickOptions(withDerived, { sortDesc: true });
assert.deepEqual(sortedWithDerived.series.at(-1).data, [40, 60, 10]);

// ML role series remain valid and are preferred when the requested column does
// not name a rendered series.
const ml = {
    xAxis: { type: 'category', data: ['t1', 't2', 't3'] },
    yAxis: { type: 'value' },
    series: [
        { type: 'line', jeenRole: 'actual', name: 'Observed', data: [2, 4, 8] },
        { type: 'line', jeenRole: 'forecast', name: 'Forecast', data: [null, 5, 9] },
    ],
};
const mlDerived = applyDerivedSeries(
    ml,
    [{ operator: 'percent_change', source_column: 'raw_measure' }],
    { columns: ['raw_measure'], rows: [[100], [50], [25]] },
).config;
assert.deepEqual(mlDerived.series.at(-1).data, [null, 100, 100]);
assert.equal(ml.series.length, 2, 'ML working series are not mutated');

console.log('chart_state_transforms JS tests passed');
