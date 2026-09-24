/**
 * Regression checks for semantic chart session state.
 * Run with: node tests/js/test_chart_session.mjs
 */
import assert from 'node:assert/strict';

const {
    createChartSession,
    extractStyleOverrides,
    applyStyleOverrides,
} = await import('../../src/static/chart-feature/utils/chartSession.js');

const baselineConfig = {
    grid: { top: 32 },
    xAxis: { type: 'category', data: ['A', 'B'] },
    yAxis: { type: 'value' },
    series: [{ name: 'sales', type: 'bar', data: [1, 2], label: { show: false } }],
};
const baselineSpec = { chart_type: 'bar', x: 'region', y: ['sales'] };
const baselineToggles = {
    dataLabels: false,
    legend: false,
    dataZoom: true,
    sortDesc: false,
};

const session = createChartSession({
    chart_config: baselineConfig,
    chart_spec: baselineSpec,
    chart_toggles: baselineToggles,
});

const edited = structuredClone(baselineConfig);
edited.series[0].type = 'line';
edited.series[0].label.show = true;
edited.series[0].itemStyle = { color: '#0a0' };
const styleOverrides = extractStyleOverrides(edited, baselineConfig);
session.replaceWorking({
    config: edited,
    spec: { ...baselineSpec, chart_type: 'line' },
    derivedSpecs: [{ operator: 'moving_avg', source_column: 'sales', params: { window: 2 } }],
    styleOverrides,
});
session.replaceView({
    toggles: { ...baselineToggles, dataLabels: true, legend: true },
    legendUserSet: true,
});

const saved = session.snapshot();
assert.equal(saved.chart_config.series[0].type, 'line');
assert.equal(saved.chart_session.baseline.config.series[0].type, 'bar');
assert.equal(saved.chart_session.working.derivedSpecs.length, 1);
assert.equal(saved.chart_session.view.legendUserSet, true);

// A turn switch round-trip retains the edit and its original Reset target.
const restored = createChartSession(saved);
assert.equal(restored.working.config.series[0].type, 'line');
assert.equal(restored.baseline.config.series[0].type, 'bar');
assert.equal(restored.view.toggles.dataLabels, true);

// Explicit edit styling wins when applied after theme/presentation styling.
const themed = structuredClone(edited);
themed.series[0].itemStyle = { color: '#00f' };
assert.equal(applyStyleOverrides(themed, restored.working.styleOverrides).series[0].itemStyle.color, '#0a0');

// A later "use defaults" edit replaces, rather than merges, old overrides.
const defaultsAgain = extractStyleOverrides(baselineConfig, baselineConfig);
assert.equal(
    applyStyleOverrides(themed, defaultsAgain).series[0].itemStyle.color,
    '#00f',
);
const baselineWithStyle = structuredClone(baselineConfig);
baselineWithStyle.series[0].itemStyle = { color: '#f00' };
const withoutBaselineStyle = structuredClone(baselineConfig);
const deletion = extractStyleOverrides(withoutBaselineStyle, baselineWithStyle);
assert.equal(deletion.series[0].itemStyle, null);
assert.equal(
    'itemStyle' in applyStyleOverrides(baselineWithStyle, deletion).series[0],
    false,
);

// Reset is atomic across semantic config, spec, overlays and view state.
restored.reset();
assert.deepEqual(restored.working.config, baselineConfig);
assert.deepEqual(restored.working.spec, baselineSpec);
assert.deepEqual(restored.working.derivedSpecs, []);
assert.deepEqual(restored.view.toggles, baselineToggles);
assert.equal(restored.view.legendUserSet, false);

// Legacy server artifacts remain valid baselines.
const legacy = createChartSession({
    chart_config: baselineConfig,
    chart_spec: baselineSpec,
});
assert.deepEqual(legacy.baseline.config, baselineConfig);
assert.deepEqual(legacy.working.config, baselineConfig);

console.log('chart session JS tests passed');
