/**
 * Tests for ChartManager's value-formatting pass.
 * Run with:  node tests/js/test_chart_value_formatting.mjs
 *
 * Regression guard: the workspace theme, palette and quick-toggle steps
 * deep-clone the ECharts option through JSON, which drops the function
 * formatters attached by `_applyValueFormatting`. The workspace then showed raw
 * numbers (4147192.9) on data labels, tooltips and gauges. Formatting must be
 * re-applicable after such a clone and must cover gauge series.
 */
import assert from 'node:assert/strict';

// chartManager.js touches the DOM at import time only through these globals.
globalThis.window = globalThis;
globalThis.document = {
    addEventListener() {},
    removeEventListener() {},
    documentElement: { dataset: {} },
    getElementById() { return null; },
};

const { ChartManager } = await import('../../src/static/chart-feature/chartManager.js');

// A manager without its constructor (no DOM, no ECharts); the methods under
// test only rely on `this._valueFormat` and `this._isOsmMapOption`.
function bareManager() {
    return Object.create(ChartManager.prototype);
}

const jsonClone = (o) => JSON.parse(JSON.stringify(o));

function lineOption() {
    return {
        jeenFormat: { kind: 'number', compact: true },
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: ['1', '2', '3'] },
        yAxis: { type: 'value' },
        series: [{ type: 'line', data: [1309563.4, 2762527.53, 4147192.9], label: { show: true } }],
    };
}

// ── formatting survives a JSON clone when re-applied ─────────────────────────
{
    const mgr = bareManager();
    const first = mgr._applyValueFormatting(lineOption());
    assert.equal(typeof first.series[0].label.formatter, 'function');
    assert.equal(typeof first.yAxis.axisLabel.formatter, 'function');
    assert.equal(typeof first.tooltip.valueFormatter, 'function');

    // What `_withWorkspaceTheme` / `applyPalette` do to the option.
    const cloned = jsonClone(first);
    assert.equal(cloned.series[0].label.formatter, undefined, 'JSON clone drops functions');
    assert.ok(cloned.jeenFormat, 'the plain-data hint must survive the clone');

    const restored = mgr._finalizeForRender(cloned);
    assert.equal(restored.series[0].label.formatter({ value: 4147192.9 }), '4.1M');
    assert.equal(restored.series[0].label.formatter({ value: 1309563.4 }), '1.3M');
    assert.equal(restored.yAxis.axisLabel.formatter(5000000), '5M');
    assert.equal(restored.tooltip.valueFormatter(2762527.53), '2.8M');
}

// ── the remembered hint carries formatting through a hint-less edit ──────────
{
    const mgr = bareManager();
    mgr._applyValueFormatting({
        jeenFormat: { kind: 'currency', compact: true, symbol: '$' },
        yAxis: { type: 'value' },
        series: [{ type: 'bar', data: [1500] }],
    });
    // A chat edit may return a config without jeenFormat.
    const edited = mgr._finalizeForRender({
        yAxis: { type: 'value' },
        tooltip: { trigger: 'axis' },
        series: [{ type: 'bar', data: [1500, 2500] }],
    });
    assert.equal(edited.yAxis.axisLabel.formatter(1500), '$1.5K');
    assert.equal(edited.series[0].label.formatter({ value: 2500 }), '$2.5K');
}

// ── per-series hints (combo: currency bars + percent line) survive a clone ───
{
    const mgr = bareManager();
    const combo = mgr._applyValueFormatting({
        jeenFormat: { kind: 'currency', compact: true, symbol: '$' },
        tooltip: { trigger: 'axis' },
        yAxis: [
            { type: 'value', jeenFormat: { kind: 'currency', compact: true, symbol: '$' } },
            { type: 'value', jeenFormat: { kind: 'percent', compact: false } },
        ],
        series: [
            { type: 'bar', data: [1200000, 900000] },
            { type: 'line', yAxisIndex: 1, data: [12.5, 9.25], jeenFormat: { kind: 'percent', compact: false } },
        ],
    });
    const restored = mgr._finalizeForRender(jsonClone(combo));
    assert.equal(restored.yAxis[0].axisLabel.formatter(1200000), '$1.2M');
    assert.equal(restored.yAxis[1].axisLabel.formatter(12.5), '12.5%');
    assert.equal(restored.series[1].label.formatter({ value: 9.25 }), '9.25%');
    // Per-series difference forces a function tooltip that formats each row.
    const tip = restored.tooltip.formatter([
        { seriesIndex: 0, seriesName: 'Sales', axisValueLabel: 'Q1', marker: '', value: 1200000 },
        { seriesIndex: 1, seriesName: 'Margin', marker: '', value: 12.5 },
    ]);
    assert.match(tip, /Sales: \$1\.2M/);
    assert.match(tip, /Margin: 12\.5%/);
}

// ── gauges: server "{value}" templates are replaced by the formatter ─────────
{
    const mgr = bareManager();
    const gauge = mgr._finalizeForRender(jsonClone({
        jeenFormat: { kind: 'number', compact: true },
        tooltip: { formatter: '{b}: {c}' },
        series: [{
            type: 'gauge', min: 0, max: 5000000,
            detail: { valueAnimation: true, formatter: '{value}' },
            data: [{ value: 4147192.9, name: 'Total Sales' }],
        }],
    }));
    const s = gauge.series[0];
    assert.equal(s.detail.formatter(4147192.9), '4.1M');
    assert.equal(s.detail.valueAnimation, true, 'other detail settings are kept');
    assert.equal(s.axisLabel.formatter(500000), '500K');
    assert.equal(typeof gauge.tooltip.formatter, 'function');
    assert.equal(
        gauge.tooltip.formatter({ seriesIndex: 0, name: 'Total Sales', value: 4147192.9 }),
        'Total Sales: 4.1M'
    );
}

// ── string label templates set by the server are left alone ─────────────────
{
    const mgr = bareManager();
    const pie = mgr._finalizeForRender({
        jeenFormat: { kind: 'number', compact: true },
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        series: [{ type: 'pie', data: [{ value: 1, name: 'a' }], label: { formatter: '{b}: {d}%' } }],
    });
    assert.equal(pie.series[0].label.formatter, '{b}: {d}%');
    assert.equal(pie.tooltip.formatter, '{b}: {c} ({d}%)');
}

// ── OSM maps format on their own renderer; finalize must not touch them ──────
{
    const mgr = bareManager();
    const osm = { jeenOsmMap: {}, jeenFormat: { kind: 'number', compact: true }, series: [] };
    assert.equal(mgr._finalizeForRender(osm), osm);
    assert.equal(osm.tooltip, undefined);
}

// ── right-to-left: formatted numbers are wrapped in bidi isolates ────────────
// Otherwise the RTL canvas moves a leading minus to the end ("20K-").
{
    globalThis.I18n = { isRtl: true, isolate: (s) => (s ? `\u2068${s}\u2069` : s) };
    try {
        const out = bareManager()._finalizeForRender(lineOption());
        assert.equal(out.yAxis.axisLabel.formatter(-20000), '\u2068-20K\u2069');
        assert.equal(out.series[0].label.formatter({ value: 4147192.9 }), '\u20684.1M\u2069');
        assert.equal(out.tooltip.valueFormatter(2762527.53), '\u20682.8M\u2069');
        assert.equal(out.yAxis.axisLabel.formatter(null), '', 'empty stays empty');

        // Time-series tooltips build their own rows; the date header is isolated too.
        const band = bareManager()._finalizeForRender({
            jeenFormat: { kind: 'number', compact: true },
            tooltip: { trigger: 'axis' },
            yAxis: { type: 'value' },
            series: [{ type: 'line', name: 'Actual', data: [['2026-06-21', -38000]] }],
        });
        const html = band.tooltip.formatter([
            { seriesIndex: 0, seriesName: 'Actual', axisValueLabel: '2026-06-21 00:00:00', marker: '', value: ['2026-06-21', -38000] },
        ]);
        assert.equal(html, '\u20682026-06-21 00:00:00\u2069<br/> Actual: \u2068-38K\u2069');
    } finally {
        delete globalThis.I18n;
    }
    const ltr = bareManager()._finalizeForRender(lineOption());
    assert.equal(ltr.yAxis.axisLabel.formatter(-20000), '-20K', 'no isolates outside RTL');
}

// ── band charts: series names follow the interface language on every render ─
{
    const catalog = { 'charts.series.actual': 'בפועל', 'charts.series.band': 'טווח צפוי {percent}%' };
    globalThis.I18n = {
        isRtl: false,
        has: (key) => key in catalog,
        t: (key, args) => catalog[key].replace('{percent}', args?.percent ?? ''),
    };
    try {
        const out = bareManager()._finalizeForRender({
            legend: { data: ['95% band', 'Actual'] },
            yAxis: { type: 'value' },
            series: [
                { name: '95% band', type: 'line', jeenRole: 'interval', data: [1, 2] },
                { name: 'Actual', type: 'line', jeenRole: 'actual', data: [1, 2] },
            ],
        });
        assert.deepEqual(out.legend.data, ['טווח צפוי 95%', 'בפועל']);
        assert.deepEqual(out.series.map((s) => s.name), ['טווח צפוי 95%', 'בפועל']);
    } finally {
        delete globalThis.I18n;
    }
}

console.log('test_chart_value_formatting: all assertions passed');
