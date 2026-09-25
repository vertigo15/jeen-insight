/**
 * Regression checks for derived overlays aligned to rendered chart series.
 * Run with: node tests/js/test_chart_operators.mjs
 */
import assert from 'node:assert/strict';

const { applyDerivedSeries, stripDerivedSeries } = await import(
    '../../src/static/chart-feature/utils/chartOperators.js'
);

const config = {
    xAxis: { type: 'category', data: ['B', 'A'] },
    yAxis: { type: 'value' },
    // Already aggregated and sorted by the deterministic chart builder.
    series: [{ name: 'revenue', type: 'bar', data: [30, 10] }],
};
const rawResults = {
    columns: ['region', 'revenue'],
    rows: [['A', 4], ['B', 10], ['B', 20], ['A', 6]],
};

const { config: withAverage, applied } = applyDerivedSeries(
    config,
    [{ operator: 'moving_avg', source_column: 'revenue', params: { window: 2 } }],
    rawResults,
);
assert.equal(applied, 1);
assert.equal(withAverage.series.length, 2);
assert.deepEqual(
    withAverage.series[1].data,
    [null, 20],
    'overlay follows the rendered [30, 10] series, not four raw result rows',
);

const { config: withoutDuplicate, applied: duplicateCount } = applyDerivedSeries(
    withAverage,
    [{ operator: 'moving_avg', source_column: 'revenue', params: { window: 2 } }],
    rawResults,
);
assert.equal(duplicateCount, 0);
assert.equal(withoutDuplicate.series.length, 2);

const stripped = stripDerivedSeries(withAverage);
assert.equal(stripped.series.length, 1);
assert.deepEqual(stripped.series[0].data, [30, 10]);

const timeConfig = {
    xAxis: { type: 'time' },
    yAxis: { type: 'value' },
    series: [{
        name: 'Actual',
        jeenRole: 'actual',
        type: 'line',
        data: [['2026-01-01', 10], ['2026-02-01', 20], ['2026-03-01', 30]],
    }],
};
const { config: withTimeAverage } = applyDerivedSeries(
    timeConfig,
    [{ operator: 'moving_avg', source_column: 'Actual', params: { window: 2 } }],
    { columns: [], rows: [] },
);
assert.deepEqual(withTimeAverage.series[1].data, [
    ['2026-01-01', null],
    ['2026-02-01', 15],
    ['2026-03-01', 25],
]);

console.log('chart operator JS tests passed');
