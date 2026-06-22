/**
 * Tests for context-aware "Refine this chart" suggestions.
 * Run with:  node tests/js/test_chart_suggestions.mjs
 */
import assert from 'node:assert/strict';
import { buildChartSuggestions, __test } from '../../src/static/chart-feature/utils/chartSuggestions.js';

const has = (list, s) => list.includes(s);

// ── time-series line chart → offers moving average / trend, not bar-stacking ──
{
    const config = { series: [{ type: 'line', data: [10, 20, 30] }], xAxis: { type: 'category', data: ['2024-01-01', '2024-02-01', '2024-03-01'] } };
    const results = { columns: ['day', 'sales'], data: [['2024-01-01', 10], ['2024-02-01', 20], ['2024-03-01', 30]] };
    const s = buildChartSuggestions(config, results);
    assert.ok(has(s, 'Add a 3-month moving average'), 'time series should offer moving average');
    assert.ok(has(s, 'Switch to a bar chart'), 'line offers switch to bar');
    assert.ok(!has(s, 'Stack the bars'), 'no bar-stacking on a line chart');
}

// ── categorical bar chart (NO time) → never offers moving average ────────────
{
    const config = { series: [{ type: 'bar', data: [5, 3, 8] }], xAxis: { type: 'category', data: ['West', 'East', 'North'] } };
    const results = { columns: ['region', 'revenue'], data: [['West', 5], ['East', 3], ['North', 8]] };
    const s = buildChartSuggestions(config, results);
    assert.ok(!has(s, 'Add a 3-month moving average'), 'no moving average without a time dimension');
    assert.ok(!has(s, 'Add a trend line'), 'no trend line without a time dimension');
    assert.ok(has(s, 'Sort highest to lowest'), 'categorical magnitudes offer sorting');
    assert.ok(has(s, 'Format values as currency'), 'money column offers currency format');
}

// ── combo (months + revenue + %) → moving avg + %-axis + currency, no stack ──
{
    const config = {
        series: [
            { type: 'bar', name: 'revenue_2006', data: [1, 2] },
            { type: 'bar', name: 'revenue_2007', data: [2, 3] },
            { type: 'line', name: 'yoy_change_pct', data: [50, -10] },
        ],
        xAxis: { type: 'category', data: ['January', 'February'] },
        yAxis: [{}, {}],
    };
    const results = {
        columns: ['month_name', 'revenue_2006', 'revenue_2007', 'yoy_change_pct'],
        data: [['January', 1, 2, 50], ['February', 2, 3, -10]],
    };
    const s = buildChartSuggestions(config, results);
    assert.ok(has(s, 'Add a 3-month moving average'), 'month names count as a time dimension');
    assert.ok(has(s, 'Format the right axis as a percentage'), 'combo with % column offers %-axis');
    assert.ok(has(s, 'Format values as currency'), 'revenue columns offer currency');
    assert.ok(!has(s, 'Stack the bars'), 'combo is not offered bar-stacking');
}

// ── pie chart → donut/bar swaps + percentages, no time overlays ──────────────
{
    const config = { series: [{ type: 'pie', data: [{ value: 1, name: 'a' }, { value: 2, name: 'b' }] }] };
    const results = { columns: ['cat', 'val'], data: [['a', 1], ['b', 2]] };
    const s = buildChartSuggestions(config, results);
    assert.ok(has(s, 'Turn this into a donut chart'), 'pie offers donut');
    assert.ok(has(s, 'Switch to a bar chart'), 'pie offers bar');
    assert.ok(!has(s, 'Add a 3-month moving average'), 'pie never offers moving average');
}

// ── data-label phrasing reflects current state ───────────────────────────────
{
    const base = { series: [{ type: 'bar', data: [1, 2] }], xAxis: { type: 'category', data: ['a', 'b'] } };
    const off = buildChartSuggestions(base, { columns: ['c', 'v'], data: [['a', 1], ['b', 2]] });
    assert.ok(has(off, 'Show the data values'), 'labels off → offer to show');

    const withLabels = { series: [{ type: 'bar', data: [1, 2], label: { show: true } }], xAxis: { type: 'category', data: ['a', 'b'] } };
    const on = buildChartSuggestions(withLabels, { columns: ['c', 'v'], data: [['a', 1], ['b', 2]] });
    assert.ok(has(on, 'Hide the data labels'), 'labels on → offer to hide');
}

// ── empty / no chart → safe defaults ─────────────────────────────────────────
{
    assert.deepEqual(buildChartSuggestions(null, null), __test.DEFAULT_SUGGESTIONS);
    assert.deepEqual(buildChartSuggestions({}, null), __test.DEFAULT_SUGGESTIONS);
}

// ── year column counts as time even though it's numeric ──────────────────────
{
    const config = { series: [{ type: 'bar', data: [1, 2] }], xAxis: { type: 'category', data: ['2006', '2007'] } };
    const results = { columns: ['year', 'revenue'], data: [['2006', 1], ['2007', 2]] };
    const ctx = __test.analyzeContext(config, results);
    assert.equal(ctx.hasTime, true, 'a column named "year" is a time dimension');
}

console.log('chart_suggestions JS tests passed');
