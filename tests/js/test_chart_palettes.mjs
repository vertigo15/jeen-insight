/**
 * Chart colour palettes: the pure transform applied to ECharts options.
 * Run with: node tests/js/test_chart_palettes.mjs
 */
import assert from 'node:assert/strict';
import {
    CHART_PALETTES,
    DEFAULT_PALETTE_ID,
    PALETTE_IDS,
    applyPalette,
    getPalette,
    isKnownPalette,
    paletteSwatches,
} from '../../src/static/chart-feature/utils/chartPalettes.js';

// ── catalogue ───────────────────────────────────────────────────────────────
assert.equal(DEFAULT_PALETTE_ID, 'jeen');
assert.equal(getPalette('jeen').colors, null, 'default palette means "no override"');
assert.ok(PALETTE_IDS.includes('purple') && PALETTE_IDS.includes('blue') && PALETTE_IDS.includes('green'));
assert.equal(getPalette('nope').id, 'jeen', 'unknown ids fall back to the default');
assert.equal(isKnownPalette('warm'), true);
assert.equal(isKnownPalette('neon'), false);
for (const p of CHART_PALETTES) {
    if (p.colors) {
        assert.ok(p.colors.length >= 6, `${p.id} has enough colours for multi-series charts`);
        assert.ok(p.colors.every((c) => /^#[0-9a-f]{6}$/i.test(c)), `${p.id} uses hex colours`);
    }
}
assert.deepEqual(paletteSwatches('purple').length, 4);
assert.deepEqual(paletteSwatches('jeen', ['#111111', '#222222', '#333333', '#444444', '#555555']), ['#111111', '#222222', '#333333', '#444444']);
assert.deepEqual(paletteSwatches('jeen'), getPalette('jeen').swatches);

// ── single-colour series get a pinned colour, in palette order ─────────────
{
    const purple = getPalette('purple').colors;
    const option = {
        color: ['#5470c6', '#91cc75'],
        series: [
            { type: 'bar', data: [1, 2], itemStyle: { color: '#8878c4' } },   // stale theme colour
            { type: 'line', data: [1, 2], lineStyle: { width: 2 } },
        ],
        xAxis: { type: 'category' },
    };
    const out = applyPalette(option, purple);
    assert.notEqual(out, option, 'returns a copy');
    assert.deepEqual(option.color, ['#5470c6', '#91cc75'], 'input untouched');
    assert.deepEqual(out.color, purple);
    assert.equal(out.series[0].itemStyle.color, purple[0], 'previous series colour is overridden');
    assert.equal(out.series[1].itemStyle.color, purple[1]);
    assert.equal(out.series[1].lineStyle.color, purple[1]);
    assert.equal(out.series[1].lineStyle.width, 2, 'other line styling preserved');
}

// ── pie-like series follow option.color; a stale series colour is removed ──
{
    const blue = getPalette('blue').colors;
    const out = applyPalette({ series: [{ type: 'pie', data: [{ value: 1 }], itemStyle: { color: '#8878c4', borderWidth: 1 } }] }, blue);
    assert.deepEqual(out.color, blue);
    assert.equal(out.series[0].itemStyle.color, undefined, 'a single colour would flatten the pie');
    assert.equal(out.series[0].itemStyle.borderWidth, 1);
}

// ── per-datum colours (negative bars) are kept ─────────────────────────────
{
    const out = applyPalette({ series: [{ type: 'bar', data: [{ value: -1, itemStyle: { color: '#d9534f' } }, 2] }] }, getPalette('green').colors);
    assert.equal(out.series[0].data[0].itemStyle.color, '#d9534f');
}

// ── maps, heatmaps, gauges and OSM maps are exempt ─────────────────────────
{
    const green = getPalette('green').colors;
    const map = { visualMap: {}, series: [{ type: 'map' }] };
    assert.equal(applyPalette(map, green), map);
    const heat = { series: [{ type: 'heatmap' }] };
    assert.equal(applyPalette(heat, green), heat);
    const osm = { jeenOsmMap: { points: [] } };
    assert.equal(applyPalette(osm, green), osm);
}

// ── no-ops ──────────────────────────────────────────────────────────────────
{
    const option = { series: [{ type: 'bar' }] };
    assert.equal(applyPalette(option, null), option);
    assert.equal(applyPalette(option, []), option);
    assert.equal(applyPalette(null, ['#000']), null);
    // Single (non-array) series is handled.
    const single = applyPalette({ series: { type: 'bar' } }, getPalette('teal').colors);
    assert.equal(single.series.itemStyle.color, getPalette('teal').colors[0]);
}

console.log('chart palettes JS tests passed');
