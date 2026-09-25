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
    chartTypeAppliesLocally,
    guardMlPresentation,
    partitionRebuildOperations,
} from '../../src/static/chart-feature/utils/chartEditOperations.js';
import { applyAnnotations } from '../../src/static/chart-feature/utils/chartScenarios.js';
import {
    applyDerivedSeries,
    stripDerivedSeries,
} from '../../src/static/chart-feature/utils/chartOperators.js';

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
// Small axes ship every label so the model can name categories ("Day 12");
// long axes fall back to a sample so the payload stays bounded.
assert.equal(shortManifest.axes.x[0].categories.complete, true);
assert.equal(shortManifest.axes.x[0].categories.values.length, 30);
assert.equal(shortManifest.axes.x[0].categories.values[11], 'Day 12');
assert.equal(longManifest.axes.x[0].categories.complete, false);
assert.equal(longManifest.axes.x[0].categories.sample.length, 6);
assert.equal(longManifest.axes.x[0].categories.last, 'Day 365');
const longer = sqlSession(Array.from({ length: 2000 }, (_, index) => index + 1));
const longerManifest = buildChartManifest(longer, { chartKind: 'sql', columns: [] });
assert.ok(
    Math.abs(JSON.stringify(longerManifest).length - JSON.stringify(longManifest).length) < 80,
    'manifest size is independent of point count once the axis is sampled',
);
assert.ok(JSON.stringify(longManifest).length < 6000, 'manifest stays below the payload target');
assert.equal(JSON.stringify(longManifest).includes('"data":'), false);
assert.equal(JSON.stringify(longManifest).includes('Day 364'), false);
const objectCategories = createChartSession({
    chart_config: {
        xAxis: { type: 'category', data: [{ value: 'A', textStyle: {} }, { value: 'B' }] },
        yAxis: { type: 'value' },
        series: [{ name: 'S', type: 'bar', data: [1, 2] }],
    },
});
assert.deepEqual(buildChartManifest(objectCategories).axes.x[0].categories.values, ['A', 'B']);

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

// Chart types the browser cannot flip in place are routed to the server rebuild;
// styling stays local so it lands on the rebuilt chart.
const barConfig = short.working.config;
assert.equal(chartTypeAppliesLocally({ op: 'set_chart_type', chart_type: 'line' }, barConfig), true);
assert.equal(chartTypeAppliesLocally({ op: 'set_chart_type', chart_type: 'pie' }, barConfig), false);
assert.equal(chartTypeAppliesLocally({ op: 'set_chart_type', chart_type: 'horizontal_bar' }, barConfig), false);
const pieConfig = {
    legend: {},
    series: [{ name: 'Sales', type: 'pie', data: [{ name: 'A', value: 1 }, { name: 'B', value: 2 }] }],
};
assert.equal(
    chartTypeAppliesLocally({ op: 'set_chart_type', chart_type: 'bar' }, pieConfig),
    false,
    'pie → bar needs the axes rebuilt server-side',
);
const split = partitionRebuildOperations([
    { op: 'set_chart_type', chart_type: 'pie' },
    { op: 'set_palette', colors: ['#ff9933', '#ffffff', '#138808'] },
    { op: 'set_binding', y: 'profit' },
    { op: 'set_toggle', key: 'legend', value: false },
], barConfig);
assert.deepEqual(split.rebuild.map((operation) => operation.op), ['set_chart_type', 'set_binding']);
assert.deepEqual(split.local.map((operation) => operation.op), ['set_palette', 'set_toggle']);
const localSplit = partitionRebuildOperations([
    { op: 'set_chart_type', chart_type: 'area' },
], barConfig);
assert.deepEqual(localSplit.rebuild, []);
assert.equal(localSplit.local.length, 1);
assert.throws(
    () => partitionRebuildOperations([{ op: 'set_chart_type', chart_type: 'osm_map' }], barConfig),
    (error) => error instanceof ChartEditOperationError && error.code === 'incompatible_chart_type',
);

// A palette on a pie recolours slices via option.color and drops any pinned
// series colour (the deletion marker survives the theme pass).
const pieSession = createChartSession({
    chart_config: pieConfig,
    chart_spec: { chart_type: 'pie', x: 'day', y: ['sales'] },
});
const paletted = applyChartEditOperations(pieSession, [
    { op: 'set_palette', colors: ['#ff9933', '#ffffff', '#138808'] },
], { chartKind: 'sql' });
const themedPie = structuredClone(paletted.working.config);
themedPie.color = ['#111111', '#222222'];
themedPie.series[0].itemStyle = { color: '#123456' };
const pieRendered = applyStyleOverrides(themedPie, paletted.working.styleOverrides);
assert.deepEqual(pieRendered.color, ['#ff9933', '#ffffff', '#138808']);
assert.equal(pieRendered.series[0].itemStyle.color, undefined);
assert.deepEqual(pieRendered.series[0].data, pieConfig.series[0].data);

// A single bar series colours per category; several series get one colour each.
const singleBar = applyChartEditOperations(short, [
    { op: 'set_palette', colors: ['#ff9933', '#138808'] },
], { chartKind: 'sql' });
const singleThemed = structuredClone(singleBar.working.config);
singleThemed.series[0].itemStyle = { color: '#123456' };
const singleRendered = applyStyleOverrides(singleThemed, singleBar.working.styleOverrides);
assert.equal(singleRendered.series[0].colorBy, 'data');
assert.equal(singleRendered.series[0].itemStyle.color, undefined);
assert.deepEqual(singleRendered.color, ['#ff9933', '#138808']);
const multiSession = createChartSession({
    chart_config: {
        xAxis: { type: 'category', data: ['A', 'B'] },
        yAxis: { type: 'value' },
        series: [
            { id: 's1', name: 'Sales', type: 'bar', data: [1, 2] },
            { id: 's2', name: 'Profit', type: 'line', data: [3, 4] },
        ],
    },
    chart_spec: { chart_type: 'combo', x: 'day', y: ['sales', 'profit'] },
});
const multi = applyChartEditOperations(multiSession, [
    { op: 'set_palette', colors: ['#ff9933', '#138808'] },
], { chartKind: 'sql' });
const multiRendered = applyStyleOverrides(multi.working.config, multi.working.styleOverrides);
assert.equal(multiRendered.series[0].itemStyle.color, '#ff9933');
assert.equal(multiRendered.series[0].colorBy, undefined);
assert.equal(multiRendered.series[1].itemStyle.color, '#138808');
assert.equal(multiRendered.series[1].lineStyle.color, '#138808');
assert.throws(
    () => applyChartEditOperations(short, [
        { op: 'set_palette', colors: ['#ff9933', 'url(javascript:alert(1))'] },
    ], { chartKind: 'sql' }),
    (error) => error.code === 'invalid_color',
);
assert.throws(
    () => applyChartEditOperations(ml, [
        { op: 'set_palette', colors: ['#ff9933'] },
    ], { chartKind: 'ml_band' }),
    (error) => error.code === 'sql_only_operation',
);

// What-if scenarios: a labelled derived copy, the real series untouched.
const categorySession = createChartSession({
    chart_config: {
        xAxis: { type: 'category', data: ['Accessories', 'Bikes', 'Clothing'] },
        yAxis: { type: 'value' },
        series: [{ id: 'units', name: 'Units Sold', type: 'bar', data: [36000, 15000, 8500] }],
    },
    chart_spec: { chart_type: 'bar', x: 'category', y: ['units'] },
});
const categoryBefore = categorySession.snapshot();
const whatIf = applyChartEditOperations(categorySession, [
    { op: 'scenario_set_point', target: 'all', category: 'bikes', value: 30000, label: 'Bikes at 30K' },
], { chartKind: 'sql' });
assert.deepEqual(whatIf.working.config.series[0].data, [36000, 15000, 8500], 'real data never changes');
assert.equal(whatIf.working.derivedSpecs.length, 1);
assert.equal(whatIf.working.derivedSpecs[0].kind, 'scenario');
assert.equal(whatIf.working.derivedSpecs[0].category, 'Bikes', 'category resolved to the axis label');
const whatIfRendered = applyDerivedSeries(whatIf.working.config, whatIf.working.derivedSpecs, null).config;
assert.equal(whatIfRendered.series.length, 2);
assert.equal(whatIfRendered.series[1].name, 'Scenario: Bikes at 30K');
assert.deepEqual(whatIfRendered.series[1].data, [36000, 30000, 8500]);
assert.equal(whatIfRendered.series[1].lineStyle.type, 'dashed');
assert.equal(whatIfRendered.series[1].__derived.kind, 'scenario');
assert.deepEqual(stripDerivedSeries(whatIfRendered).series, whatIf.working.config.series);
assert.deepEqual(categorySession.snapshot(), categoryBefore);

const growth = applyChartEditOperations(whatIf, [
    { op: 'scenario_scale', target: 'id:units', percent: 10, from_category: 'Bikes', label: '+10% from Bikes' },
], { chartKind: 'sql' });
const growthRendered = applyDerivedSeries(growth.working.config, growth.working.derivedSpecs, null).config;
assert.equal(growthRendered.series.length, 3);
assert.deepEqual(growthRendered.series[2].data, [36000, 16500, 9350]);
assert.throws(
    () => applyChartEditOperations(growth, [
        { op: 'scenario_shift', target: 'all', delta: 500, label: 'third' },
    ], { chartKind: 'sql' }),
    (error) => error.code === 'too_many_scenarios',
);
assert.throws(
    () => applyChartEditOperations(categorySession, [
        { op: 'scenario_set_point', target: 'all', category: 'Boats', value: 1 },
    ], { chartKind: 'sql' }),
    (error) => error.code === 'unknown_category',
);
assert.throws(
    () => applyChartEditOperations(ml, [
        { op: 'scenario_shift', target: 'role:actual', delta: 5 },
    ], { chartKind: 'ml_band' }),
    (error) => error.code === 'sql_only_operation',
);
const cleared = applyChartEditOperations(growth, [{ op: 'scenario_clear' }], { chartKind: 'sql' });
assert.deepEqual(cleared.working.derivedSpecs, []);
const clearedOne = applyChartEditOperations(growth, [{ op: 'scenario_clear', label: 'Bikes at 30K' }], { chartKind: 'sql' });
assert.equal(clearedOne.working.derivedSpecs.length, 1);

// Pie scenarios draw as an outer dashed ring with the same slice names.
const pieWhatIf = applyChartEditOperations(pieSession, [
    { op: 'scenario_set_point', target: 'all', category: 'B', value: 5 },
], { chartKind: 'sql' });
const pieScenario = applyDerivedSeries(pieWhatIf.working.config, pieWhatIf.working.derivedSpecs, null).config;
assert.equal(pieScenario.series[1].type, 'pie');
assert.deepEqual(pieScenario.series[1].data, [{ name: 'A', value: 1 }, { name: 'B', value: 5 }]);
assert.deepEqual(pieScenario.series[0].data, pieConfig.series[0].data);

// Annotations live in working.annotations and are drawn on the display copy only.
const annotated = applyChartEditOperations(categorySession, [
    { op: 'add_reference_line', axis: 'y', value: 20000, label: 'Target 20K' },
    { op: 'add_reference_line', axis: 'y', stat: 'avg', label: 'Average' },
    { op: 'highlight_points', target: 'all', predicate: { op: 'lt', value: 10000 }, label: 'Below 10K' },
], { chartKind: 'sql' });
assert.equal(annotated.working.annotations.referenceLines.length, 2);
assert.equal(annotated.working.annotations.highlights.length, 1);
assert.deepEqual(annotated.working.config.series[0].data, [36000, 15000, 8500]);
const drawn = applyAnnotations(annotated.working.config, annotated.working.annotations);
assert.equal(drawn.series[0].markLine.data.length, 2);
assert.equal(drawn.series[0].markLine.data[0].yAxis, 20000);
assert.equal(Math.round(drawn.series[0].markLine.data[1].yAxis), 19833);
assert.equal(drawn.series[0].data[2].itemStyle.color, '#d4574a');
assert.equal(drawn.series[0].data[1], 15000, 'unmatched points are left as scalars');
assert.equal(annotated.working.config.series[0].markLine, undefined, 'working config carries no markLine');
const byName = applyChartEditOperations(annotated, [
    { op: 'highlight_points', target: 'id:units', categories: ['bikes'], color: '#2563eb' },
    { op: 'remove_reference_line', label: 'Average' },
], { chartKind: 'sql' });
assert.deepEqual(byName.working.annotations.highlights[1].categories, ['Bikes']);
assert.equal(byName.working.annotations.referenceLines.length, 1);
const unhighlighted = applyChartEditOperations(byName, [{ op: 'clear_highlights' }], { chartKind: 'sql' });
assert.deepEqual(unhighlighted.working.annotations.highlights, []);
assert.throws(
    () => applyChartEditOperations(categorySession, [
        { op: 'highlight_points', target: 'all', categories: ['Boats'] },
    ], { chartKind: 'sql' }),
    (error) => error.code === 'unknown_category',
);
assert.throws(
    () => applyChartEditOperations(categorySession, [
        { op: 'add_reference_line', axis: 'y', label: 'x' },
    ], { chartKind: 'sql' }),
    (error) => error.code === 'invalid_scenario',
);
// Reset drops scenarios and annotations with everything else.
const resetSession = createChartSession(annotated.snapshot());
resetSession.reset();
assert.deepEqual(resetSession.working.annotations, { referenceLines: [], highlights: [] });

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
