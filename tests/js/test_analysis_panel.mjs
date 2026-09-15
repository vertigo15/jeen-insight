/**
 * ML skills answer-pane helpers (pure rendering).
 * Run with: node tests/js/test_analysis_panel.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '../../src/static/analysis/analysisPanel.js'), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const UI = sandbox.window.JeenAnalysisUI;

const analysis = {
    skill: 'anomaly_detection',
    method_used: 'MSTL, m=52, robust residual band',
    params: { series: { table: 'FactInternetSales', measure_column: 'Profit', agg: 'sum', grain: 'week', filters: [] }, sensitivity: 0.95 },
    validation: { metric: 'WAPE', value: 0.064, band: 'good', coverage: 0.923, coverage_n: 26, basis: 'in-sample fit' },
    egress: { tier: 'A', rows_sent_to_model: 26, columns: ['ts', 'value'] },
    engine: { name: 'statsmodels-mstl', version: '0.15.0' },
    details: { method_used: 'MSTL', seasonal_periods: [52], candidates: [{ name: 'MSTL', metric: 'fit WAPE', value: 0.064, selected: true }], notes: ['n1'] },
    provenance: { missing_policy: 'SUM of an empty week is zero: 0 of 26 periods zero-filled', span_start: '2026-03-02', span_end: '2026-08-31', query_ts: '2026-09-14T00:00:00Z' },
    guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
    facts: { n_points: 26, n_flagged: 3 },
    caveats: ['92% of history sits inside the band'],
    low_confidence: false,
};

// ── status strip ─────────────────────────────────────────────────────────────
{
    const html = UI.stripSegments({ analysis, low_confidence: false });
    assert.match(html, /v3-skill-chip/);
    assert.match(html, /anomaly detection/);
    assert.match(html, /26 pts · 3 flagged · WAPE 6\.4% · 26 rows sent to model/);
    assert.doesNotMatch(html, /low confidence/);
    assert.match(UI.stripSegments({ analysis, low_confidence: true }), /v3-lowconf-pill/);
    assert.equal(UI.stripSegments({}), '');
    // Never MAPE.
    assert.doesNotMatch(html, /MAPE/);
    // The method is named visibly in the strip (spec §8), not only on hover.
    assert.match(html, /v3-ml-method[^>]*>MSTL, m=52, robust residual band</);
    assert.doesNotMatch(UI.stripSegments({ analysis: { ...analysis, method_used: '' } }), /v3-ml-method/);
}

// ── proposal expiry ──────────────────────────────────────────────────────────
{
    const now = Date.parse('2026-09-15T12:00:00Z');
    assert.equal(UI.proposalExpired({ proposal_id: '' }, now), true, 'no id → not resumable');
    assert.equal(UI.proposalExpired({ proposal_id: 'p1' }, now), false, 'no expiry → resumable');
    assert.equal(UI.proposalExpired({ proposal_id: 'p1', expires_at: '2026-09-15T12:30:00Z' }, now), false);
    assert.equal(UI.proposalExpired({ proposal_id: 'p1', expires_at: '2026-09-15T11:59:00Z' }, now), true);
    assert.equal(UI.proposalExpired(null, now), true);
}

// ── confirm card ─────────────────────────────────────────────────────────────
{
    const proposal = {
        proposal_id: 'p1', kind: 'confirm', skill: 'anomaly_detection', title: 'Anomaly detection',
        message: 'Reading this as an anomaly check of SUM(Profit) by week over the last 26 weeks. Confirm or adjust before I run it.',
        params: { series: { grain: 'week' } },
        chips: [
            { key: 'measure_column', label: 'measure', value: 'Profit', options: ['Profit', 'SalesAmount'] },
            { key: 'grain', label: 'grain', value: 'week', options: ['day', 'week', 'month'] },
            { key: 'window', label: 'window', value: 26, options: [] },
            { key: 'sensitivity', label: 'sensitivity', value: 0.95, options: [0.8, 0.9, 0.95, 0.99] },
        ],
        egress_summary: 'SQL rolls SUM(Profit) up to about 26 weekly totals on AW. Only those rows are sent to the analysis service; no FactInternetSales rows are read.',
        guard_results: [], tier: 'A', estimated_seconds: 4,
    };
    const html = UI.proposalHtml(proposal);
    assert.match(html, /is-confirm/);
    assert.match(html, /data-run/);
    assert.match(html, /data-sql-instead/);
    assert.match(html, /data-remember/);
    assert.match(html, /sent to the analysis service/);
    assert.match(html, /<select data-chip="measure_column"/);
    assert.match(html, /<input data-chip="window"[^>]*type="number"/);
    assert.match(html, /~4s/);
    // No inline styles or hex colours: tokens only.
    assert.doesNotMatch(html, /style="/);
    assert.doesNotMatch(html, /#[0-9a-f]{6}/i);
    // Escaping.
    const evil = UI.proposalHtml({ ...proposal, message: '<img src=x onerror=alert(1)>' });
    assert.doesNotMatch(evil, /<img/);
}

// ── guard refusal + clarification ────────────────────────────────────────────
{
    const guard = {
        proposal_id: 'p2', kind: 'guard', skill: 'forecast', title: 'Forecast',
        message: '90 days requested; 62 days of history support at most 20.',
        params: {}, tier: 'A',
        guard_results: [{ name: 'max_horizon', passed: false, detail: '90 days requested; 62 days of history support at most 20' },
                        { name: 'series_length', passed: true, detail: '62 of 12 days needed' }],
        options: [
            { kind: 'patch', label: 'Forecast 20 days instead', params_patch: { horizon: 20 }, recommended: true },
            { kind: 'patch', label: 'Look back 270 days', params_patch: { window: 270 } },
            { kind: 'override', label: 'Forecast 90 anyway', description: 'low confidence' },
        ],
    };
    const html = UI.proposalHtml(guard);
    assert.match(html, /is-guard/);
    assert.match(html, /62 days of history support at most 20/);
    assert.match(html, /is-recommended[^>]*data-exit="0"/);
    assert.match(html, /is-override[^>]*data-exit="2"/);
    assert.match(html, /0 rows sent to the model/);
    // Only the failed guard is listed on a refusal.
    assert.match(html, /max_horizon/);
    assert.doesNotMatch(html, /series_length/);
    assert.doesNotMatch(html, /insufficient data/i);

    const clarify = UI.proposalHtml({ proposal_id: 'p3', kind: 'clarify', skill: 'forecast', message: 'Which date should I use?',
        params: {}, options: [{ kind: 'patch', label: 'OrderDate', params_patch: { series: { date_column: 'OrderDate' } }, recommended: true },
                              { kind: 'patch', label: 'ShipDate', params_patch: { series: { date_column: 'ShipDate' } } }] });
    assert.match(clarify, /is-clarify/);
    assert.match(clarify, /OrderDate/);
    assert.match(clarify, /ShipDate/);
}

// ── chip patch collection (DOM-less: emulate elements) ───────────────────────
{
    const fakeCard = {
        querySelectorAll() {
            return [
                { dataset: { chip: 'grain', original: 'week' }, value: 'month', type: 'select-one' },
                { dataset: { chip: 'window', original: '26' }, value: '52', type: 'number' },
                { dataset: { chip: 'sensitivity', original: '0.95' }, value: '0.95', type: 'select-one' },
                { dataset: { chip: 'measure_column', original: 'Profit' }, value: 'Profit', type: 'select-one' },
            ];
        },
    };
    // Objects come from another vm context, so compare their JSON shape.
    const patch = JSON.parse(JSON.stringify(UI.collectPatch(fakeCard)));
    assert.deepEqual(patch, { grain: 'month', window: 52 }, 'only changed chips are sent; numbers are typed');
    assert.deepEqual(JSON.parse(JSON.stringify(UI.collectPatch(null))), {});
}

// ── model details ────────────────────────────────────────────────────────────
{
    const html = UI.modelDetailsHtml({ analysis, low_confidence: true });
    assert.match(html, /MSTL, m=52/);
    assert.match(html, /6\.4%/);
    assert.match(html, /92% of 26/);
    assert.match(html, /m=52/);
    assert.match(html, /statsmodels-mstl 0\.15\.0/);
    assert.match(html, /series_length/);
    assert.match(html, /zero-filled/);
    assert.match(html, /Low confidence: a guard was overridden/);
    assert.match(html, /<td>sensitivity<\/td>/);
    assert.doesNotMatch(html, /style="/);
    assert.match(UI.modelDetailsHtml({}), /Model details appear here/);
    const withDiff = UI.modelDetailsHtml({ analysis: { ...analysis, param_diff: { grain: { from: 'week', to: 'month' } } } });
    assert.match(withDiff, /Changed from the previous run/);
    assert.match(withDiff, /grain: <s>week<\/s> → month/);
}

// ── caption + formatting ─────────────────────────────────────────────────────
{
    assert.equal(UI.chartCaption({ analysis }), 'SUM(Profit) · weekly · MSTL, m=52, robust residual band');
    assert.equal(UI.fmtNum(1470000), '1.47M');
    assert.equal(UI.fmtNum(640000), '640k');
    assert.equal(UI.fmtPct(0.064), '6.4%');
    assert.equal(UI.metricText({ metric: 'MASE', value: 0.83 }), 'MASE 0.83');
    assert.equal(UI.metricText({ metric: 'MASE', value: null, coverage: 0.9 }), 'coverage 90%');
}

console.log('analysis panel JS tests passed');
