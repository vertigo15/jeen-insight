/**
 * Chart editor v2 compact-manifest and operation invariants.
 * Run with: node tests/js/test_chart_edit_operations.mjs
 */
import assert from 'node:assert/strict';
import {
    applyStyleOverrides,
    createChartSession,
    seriesIdentity,
} from '../../src/static/chart-feature/utils/chartSession.js';
import {
    applyChartEditOperations,
    applyLocalSort,
    buildChartManifest,
    ChartEditOperationError,
    guardMlPresentation,
} from '../../src/static/chart-feature/utils/chartEditOperations.js';

function sqlSession(points) {
    return createChartSession({
        chart_config: {
            xAxis: { type: 'category', data: points.map((_, index) => `Day ${index + 1}`) },
            yAxis: { type: 'value' },
            legend: { show: true },
            jeenFormat: { kind: 'number', compact: true },
            series: [{
                id: 'sales-series',
                name: 'Sales',
                type: 'bar',
                data: points,
                label: { show: false },
            }],
        },
        chart_spec: { chart_type: 'bar', x: 'day', y: ['sales'] },
        chart_toggles: { dataLabels: false, legend: true, dataZoom: false, sortDesc: false },
    });
}

const short = sqlSession(Array.from({ length: 30 }, (_, index) => index + 1));
const long = sqlSession(Array.from({ length: 365 }, (_, index) => index + 1));
const shortManifest = buildChartManifest(short, {
    chartKind: 'sql',
    columns: [{ name: 'day', type: 'date' }, { name: 'sales', type: 'numeric' }],
});
const longManifest = buildChartManifest(long, {
    chartKind: 'sql',
    columns: [{ name: 'day', type: 'date' }, { name: 'sales', type: 'numeric' }],
});
assert.equal(shortManifest.series[0].data_summary.count, 30);
assert.equal(longManifest.series[0].data_summary.count, 365);
assert.equal(shortManifest.axes.x[0].categories.sample.length, 6);
assert.equal(longManifest.axes.x[0].categories.sample.length, 6);
assert.ok(
    Math.abs(JSON.stringify(shortManifest).length - JSON.stringify(longManifest).length) < 80,
    'manifest size is independent of point count',
);
assert.ok(JSON.stringify(longManifest).length < 6000, 'manifest stays below the payload target');
assert.equal(JSON.stringify(longManifest).includes('"data":'), false);
assert.equal(JSON.stringify(longManifest).includes('Day 364'), false);

// A multi-operation answer composes locally without touching source data.
const beforeSql = short.snapshot();
const combined = applyChartEditOperations(short, [
    { op: 'set_chart_type', chart_type: 'line' },
    { op: 'set_color', target: 'id:sales-series', color: '#22c55e' },
    { op: 'set_toggle', key: 'dataLabels', value: true },
], { chartKind: 'sql' });
assert.equal(combined.working.config.series[0].type, 'line');
assert.equal(combined.view.toggles.dataLabels, true);
assert.deepEqual(combined.working.config.series[0].data, beforeSql.chart_config.series[0].data);
const combinedRendered = applyStyleOverrides(
    combined.working.config,
    combined.working.styleOverrides,
);
assert.equal(combinedRendered.series[0].itemStyle.color, '#22c55e');
assert.equal(combinedRendered.series[0].lineStyle.color, '#22c55e');
assert.deepEqual(short.snapshot(), beforeSql, 'source session is not mutated');

const semanticEdits = applyChartEditOperations(short, [
    { op: 'set_format', format: { kind: 'currency', symbol: '$', compact: false } },
    { op: 'rename_series', target: 'id:sales-series', new_name: 'Revenue' },
    { op: 'hide_series', target: 'id:sales-series', hidden: true },
    { op: 'set_stack', stacked: true },
    { op: 'set_sort', direction: 'desc' },
    {
        op: 'add_overlay',
        operator: 'moving_avg',
        source_column: 'sales',
        params: { window: 3 },
        label: '3-month moving average',
    },
], { chartKind: 'sql' });
assert.deepEqual(semanticEdits.working.config.jeenFormat, {
    kind: 'currency', symbol: '$', compact: false,
});
assert.equal(semanticEdits.working.config.series[0].name, 'Revenue');
assert.equal(semanticEdits.working.config.legend.selected.Revenue, false);
assert.equal(semanticEdits.working.config.series[0].stack, '__jeen_stack__');
assert.equal(semanticEdits.view.toggles.sortDesc, true);
assert.equal(semanticEdits.working.derivedSpecs[0].label, '3-month moving average');
assert.deepEqual(semanticEdits.working.config.series[0].data, beforeSql.chart_config.series[0].data);
const ascendingEdit = applyChartEditOperations(sqlSession([3, 1, 2]), [
    { op: 'set_sort', direction: 'asc' },
], { chartKind: 'sql' });
const ascendingDisplay = applyLocalSort(
    ascendingEdit.working.config,
    ascendingEdit.view.toggles.sortDirection,
);
assert.deepEqual(ascendingDisplay.series[0].data, [1, 2, 3]);
assert.deepEqual(ascendingEdit.working.config.series[0].data, [3, 1, 2]);
const removedOverlay = applyChartEditOperations(semanticEdits, [
    { op: 'remove_overlay', operator: 'moving_avg', label: '3-month moving average' },
], { chartKind: 'sql' });
assert.deepEqual(removedOverlay.working.derivedSpecs, []);

// Unsupported work rolls the complete operation list back.
assert.throws(
    () => applyChartEditOperations(short, [
        { op: 'set_color', target: 'all', color: '#2563eb' },
        { op: 'set_binding', target: 'all', y: 'profit' },
    ], { chartKind: 'sql' }),
    (error) => error instanceof ChartEditOperationError && error.code === 'binding_unsupported',
);
assert.deepEqual(short.snapshot(), beforeSql);

const mlConfig = {
    xAxis: { type: 'category', data: ['A', 'B'] },
    yAxis: { type: 'value' },
    series: [
        {
            id: 'actual-id', name: 'Actual', jeenRole: 'actual', type: 'line',
            data: [10, 12], markLine: { data: [{ xAxis: 'B' }] },
        },
        {
            name: 'Expected', jeenRole: 'expected', type: 'line',
            data: [11, 13], stack: 'model',
        },
        {
            name: 'Interval base', jeenRole: 'interval_base', type: 'line',
            data: [8, 9], stack: 'band', lineStyle: { opacity: 0 },
        },
        {
            name: 'Interval bound', jeenRole: 'interval_bound', type: 'line',
            data: [4, 6], stack: 'band', lineStyle: { opacity: 0 },
        },
        {
            name: 'Interval', jeenRole: 'interval', type: 'line',
            data: [4, 6], stack: 'band', areaStyle: { opacity: 0.2 },
        },
    ],
};
const ml = createChartSession({
    chart_config: mlConfig,
    chart_spec: { chart_type: 'band', x_column: 'date' },
});
const mlBefore = ml.snapshot();
assert.throws(
    () => applyChartEditOperations(ml, [
        { op: 'set_chart_type', chart_type: 'bar' },
    ], { chartKind: 'ml_band' }),
    (error) => error.code === 'sql_only_operation',
);
assert.throws(
    () => applyChartEditOperations(ml, [
        { op: 'set_color', target: 'role:interval_base', color: '#ef4444' },
    ], { chartKind: 'ml_band' }),
    (error) => error.code === 'target_not_found',
);

const mlEdited = applyChartEditOperations(ml, [
    { op: 'set_color', target: 'role:expected', color: '#00aa00' },
    { op: 'set_style', target: 'role:interval', path: 'areaStyle.opacity', value: 0.35 },
    { op: 'set_toggle', key: 'dataLabels', value: true, target: 'all' },
], { chartKind: 'ml_band' });
assert.deepEqual(
    mlEdited.working.config.series.map((series) => series.data),
    mlConfig.series.map((series) => series.data),
);
assert.deepEqual(
    mlEdited.working.config.series.slice(2, 4),
    mlConfig.series.slice(2, 4),
    'ML helper series remain structurally exact',
);
assert.deepEqual(mlEdited.working.config.series[0].markLine, mlConfig.series[0].markLine);

// Simulate quick-label rendering, then apply identity overrides: helpers stay hidden.
const quickLabels = structuredClone(mlEdited.working.config);
quickLabels.series.forEach((series) => {
    series.label = { ...(series.label || {}), show: true };
});
const styledLabels = applyStyleOverrides(quickLabels, mlEdited.working.styleOverrides);
const mlRendered = guardMlPresentation(styledLabels, 'ml_band');
assert.equal(mlRendered.series[0].label.show, true);
assert.equal(mlRendered.series[1].itemStyle.color, '#00aa00');
assert.equal(mlRendered.series[2].label.show, false);
assert.equal(mlRendered.series[3].label.show, false);
assert.equal(mlRendered.series[4].areaStyle.opacity, 0.35);
assert.equal(mlRendered.series[4].label.show, false);
assert.deepEqual(ml.snapshot(), mlBefore);

// Identity-keyed styles follow a series across reordering.
const styled = applyChartEditOperations(short, [
    { op: 'set_color', target: 'id:sales-series', color: '#123456' },
], { chartKind: 'sql' });
const extra = {
    id: 'profit-series', name: 'Profit', type: 'line', data: [3, 4],
};
const reordered = {
    ...styled.working.config,
    series: [extra, styled.working.config.series[0]],
};
const reorderedRendered = applyStyleOverrides(reordered, styled.working.styleOverrides);
assert.equal(reorderedRendered.series[1].itemStyle.color, '#123456');
assert.equal(reorderedRendered.series[0].itemStyle, undefined);
assert.equal(seriesIdentity(reordered.series[1], 1, reordered.series), 'id:sales-series');

console.log('chart edit operations JS tests passed');
