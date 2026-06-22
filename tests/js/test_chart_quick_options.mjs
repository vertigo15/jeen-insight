/**
 * Tests for the client-side chart quick-options layer.
 * Run with:  node tests/js/test_chart_quick_options.mjs
 *
 * Regression guard: a chat edit like "add data labels" returns a config with
 * series.label.show=true. The default-off `dataLabels` toggle must NOT silently
 * wipe it — the toggle state has to be synced FROM the edited config first.
 */
import assert from 'node:assert/strict';
import { applyQuickOptions, detectToggles } from '../../src/static/chart-feature/utils/chartQuickOptions.js';

const DEFAULT_TOGGLES = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };

function labelledBarConfig() {
    return {
        legend: { show: true },
        xAxis: { type: 'category', data: ['A', 'B'] },
        yAxis: { type: 'value' },
        series: [{ type: 'bar', data: [1, 2], label: { show: true, position: 'top' } }],
    };
}

// ── the bug: default toggles strip LLM-added labels ──────────────────────────
{
    const stripped = applyQuickOptions(labelledBarConfig(), DEFAULT_TOGGLES);
    assert.equal(stripped.series[0].label.show, false,
        'sanity: default-off dataLabels toggle force-hides labels');
}

// ── detectToggles reads the config's real state ──────────────────────────────
{
    const t = detectToggles(labelledBarConfig());
    assert.equal(t.dataLabels, true, 'labels detected from series.label.show');
    assert.equal(t.legend, true, 'legend detected as visible');
    assert.equal(t.dataZoom, false, 'no dataZoom present');
}

// ── the fix: sync toggles from config, then labels survive re-apply ──────────
{
    const cfg = labelledBarConfig();
    const synced = { ...DEFAULT_TOGGLES, ...detectToggles(cfg) };
    const out = applyQuickOptions(cfg, synced);
    assert.equal(out.series[0].label.show, true,
        'labels preserved after syncing toggles from the edited config');
}

// ── "remove labels" edit (label.show=false) is respected ─────────────────────
{
    const cfg = labelledBarConfig();
    cfg.series[0].label.show = false;
    const t = detectToggles(cfg);
    assert.equal(t.dataLabels, false, 'labels off when no series shows them');
}

// ── legend explicitly hidden is detected as off ──────────────────────────────
{
    const cfg = labelledBarConfig();
    cfg.legend = { show: false };
    assert.equal(detectToggles(cfg).legend, false, 'hidden legend detected');
}

// ── dataZoom present is detected ─────────────────────────────────────────────
{
    const cfg = labelledBarConfig();
    cfg.dataZoom = [{ type: 'slider' }, { type: 'inside' }];
    assert.equal(detectToggles(cfg).dataZoom, true, 'dataZoom detected when present');
}

// ── empty / malformed config is safe ─────────────────────────────────────────
{
    assert.deepEqual(detectToggles(null), {});
    assert.deepEqual(detectToggles({}), { dataLabels: false, legend: true, dataZoom: false });
}

console.log('chart_quick_options JS tests passed');
