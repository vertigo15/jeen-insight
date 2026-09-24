/**
 * Canonical chart session state and page-snapshot compatibility.
 * Run with: node tests/js/test_chart_session_state.mjs
 */
import assert from 'node:assert/strict';
import {
    applyStyleOverrides,
    createChartSession,
    extractStyleOverrides,
    mergeStyleOverrides,
} from '../../src/static/chart-feature/utils/chartSession.js';

const baselineConfig = {
    xAxis: { type: 'category', data: ['A', 'B'] },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', name: 'Sales', data: [1, 2] }],
};
const baselineSpec = { chart_type: 'bar', x: 'region', y: 'sales' };
const baselineToggles = { dataLabels: false, legend: false, dataZoom: false, sortDesc: false };

const session = createChartSession({
    chart_config: baselineConfig,
    chart_spec: baselineSpec,
    chart_toggles: baselineToggles,
    derived_specs: [{ operator: 'cumulative_sum', source_column: 'Sales' }],
    legend_user_set: true,
    map_view: { echarts: { zoom: 1.5 } },
});

// Caller mutations never alter the immutable reset target.
baselineConfig.series[0].data[0] = 999;
assert.deepEqual(session.baseline.config.series[0].data, [1, 2]);
const leaked = session.baseline;
leaked.config.series[0].data[0] = 777;
assert.deepEqual(session.baseline.config.series[0].data, [1, 2]);

session.replaceWorking({
    config: {
        ...session.working.config,
        series: [{ type: 'line', name: 'Sales', data: [10, 20], lineStyle: { width: 4 } }],
    },
    spec: { ...baselineSpec, chart_type: 'line' },
    derivedSpecs: [{ operator: 'moving_avg', source_column: 'Sales', params: { window: 2 } }],
    styleOverrides: { series: [{ lineStyle: { width: 4 } }] },
});
session.replaceView({
    toggles: { ...baselineToggles, dataLabels: true, sortDesc: true },
    legendUserSet: false,
    mapView: { echarts: { zoom: 3 } },
});

const snapshot = session.snapshot();
assert.equal(snapshot.chart_config.series[0].type, 'line', 'legacy pair exposes semantic working config');
assert.equal(snapshot.chart_session.version, 1);
const roundTrip = createChartSession(snapshot);
assert.deepEqual(roundTrip.snapshot(), snapshot, 'versioned page snapshot round-trips exactly');

roundTrip.reset();
assert.deepEqual(roundTrip.working.config, {
    xAxis: { type: 'category', data: ['A', 'B'] },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', name: 'Sales', data: [1, 2] }],
});
assert.deepEqual(roundTrip.working.spec, baselineSpec);
assert.deepEqual(roundTrip.working.derivedSpecs, [{ operator: 'cumulative_sum', source_column: 'Sales' }]);
assert.deepEqual(roundTrip.view.toggles, baselineToggles);
assert.equal(roundTrip.view.legendUserSet, true);
assert.deepEqual(roundTrip.view.mapView, { echarts: { zoom: 1.5 } });

// Explicit edited style survives a later workspace theme pass.
const beforeEdit = { series: [{ type: 'bar', data: [1], itemStyle: { color: '#old' } }] };
const afterEdit = { series: [{ type: 'bar', data: [1], itemStyle: { color: '#user' }, label: { fontSize: 18 } }] };
const delta = extractStyleOverrides(afterEdit, beforeEdit);
const combined = mergeStyleOverrides({}, delta);
const themed = { series: [{ type: 'bar', data: [1], itemStyle: { color: '#theme' }, label: { fontSize: 11 } }] };
const rendered = applyStyleOverrides(themed, combined);
assert.equal(rendered.series[0].itemStyle.color, '#user');
assert.equal(rendered.series[0].label.fontSize, 18);
assert.equal(themed.series[0].itemStyle.color, '#theme', 'style application does not mutate theme output');

console.log('chart_session_state JS tests passed');
