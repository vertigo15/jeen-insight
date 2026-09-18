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
    // The model is changed on the setup card, never from a strip control; and no
    // "Edit setup" without a definition (older persisted turns).
    assert.doesNotMatch(html, /data-ml-method/);
    assert.doesNotMatch(html, /<select/);
    assert.doesNotMatch(html, /data-ml-edit/);
}

// Production-shaped chips (what nodes/analysis.py _chips() emits for a forecast).
const forecastChips = [
    { key: 'measure_column', label: 'Measure', value: 'SalesAmount', options: ['SalesAmount', 'Profit'], group: 'Data', required: true, help: 'The numeric column to analyse.' },
    { key: 'agg', label: 'Aggregate', value: 'sum', options: ['sum', 'count', 'avg'], group: 'Data', option_labels: { sum: 'Sum', count: 'Count', avg: 'Average' } },
    { key: 'date_column', label: 'Date column', value: 'OrderDate', options: ['OrderDate', 'ShipDate'], group: 'Data', required: true },
    { key: 'group_by', label: 'Split by', value: 'none', options: ['none', 'Territory'], group: 'Data', option_labels: { none: '— none —' } },
    { key: 'grain', label: 'Grain', value: 'month', options: ['day', 'week', 'month'], group: 'Model', option_labels: { day: 'Day', week: 'Week', month: 'Month' } },
    { key: 'window', label: 'Look-back window', value: 24, kind: 'number', step: 1, min: 12, max: 1500, unit_from: 'grain', group: 'Model',
      defaults_by_grain: { day: 90, week: 26, month: 24 }, help: 'How much history the model learns from.' },
    { key: 'method', label: 'Model', value: 'auto', options: ['auto', 'drift'], group: 'Model', option_labels: { auto: 'Auto', drift: 'Drift' } },
    { key: 'horizon', label: 'Horizon', value: 8, kind: 'number', step: 1, min: 1, max: 104, unit_from: 'grain', group: 'Output' },
    { key: 'interval', label: 'Interval', value: 0.8, options: [0.5, 0.8, 0.9], group: 'Output', option_labels: { '0.5': '50%', '0.8': '80%', '0.9': '90%' } },
];

// ── setup card ("Edit setup") ────────────────────────────────────────────────
{
    const withSetup = {
        ...analysis, skill: 'forecast', method_used: 'Drift',
        params: { series: { grain: 'month', measure_column: 'SalesAmount' }, window: 24, horizon: 8, method: 'auto' },
        definition: {
            chips: forecastChips,
            egress_summary: 'SQL rolls SUM(SalesAmount) up to about 24 monthly totals on AW. Only those rows are sent to the analysis service.',
        },
    };
    const strip = UI.stripSegments({ analysis: withSetup });
    assert.match(strip, /<button [^>]*data-ml-edit[^>]*>Edit setup<\/button>/);
    assert.doesNotMatch(strip, /<select/, 'the strip carries no model control');
    const html = UI.definitionHtml(withSetup);
    assert.match(html, /v3-ml-card is-confirm is-definition/);
    assert.match(html, /<select id="[^"]+" data-chip="measure_column"[^>]*data-original="SalesAmount"/);
    assert.match(html, /<option value="SalesAmount" selected>/);
    // The model lives here, labelled "Model", keyed `method`, with human option names.
    assert.match(html, /<div class="v3-ml-field" data-field="method">\s*<label class="v3-ml-label"[^>]*>Model</);
    assert.match(html, /<select id="[^"]+" data-chip="method"[^>]*data-original="auto"/);
    assert.match(html, /<option value="drift">Drift<\/option>/);
    // Number inputs carry the contract's bounds and a unit that follows the grain.
    assert.match(html, /data-chip="window"[^>]*data-original="24" type="number" min="12" max="1500" step="1"/);
    assert.match(html, /data-chip="horizon"[^>]*data-original="8" type="number" min="1" max="104" step="1"/);
    assert.match(html, /data-field="window">[\s\S]*?<span class="v3-ml-unit" data-unit>months<\/span>/);
    assert.match(html, /How much history the model learns from\. <span class="v3-ml-bounds">\(12–1500\)<\/span>/);
    // Sections in first-occurrence order, one fieldset each.
    const legends = [...html.matchAll(/<legend class="v3-ml-section-title">([^<]+)<\/legend>/g)].map((m) => m[1]);
    assert.deepEqual(legends, ['Data', 'Model', 'Output']);
    assert.match(html, /<fieldset class="v3-ml-group" data-group="Data">/);
    // The summary line reads the setup back.
    assert.match(html, /<span class="v3-ml-summary" data-summary>SUM\(<bdi>SalesAmount<\/bdi>\) · per month · last 24 months · 8 months ahead · 80% interval · Auto model<\/span>/);
    assert.match(html, /Split by/);
    assert.match(html, /<option value="none" selected>— none —<\/option>/);
    assert.match(html, /data-run>Re-run</);
    assert.match(html, /data-cancel>Cancel</);
    assert.match(html, /data-reset-all hidden>Reset all</);
    assert.match(html, /sent to the analysis service/);
    assert.match(html, /v3-ml-tiermeta" title="Tier A:[^"]*">Aggregates only</);
    assert.doesNotMatch(html, /TIER A · AGGREGATE/);
    assert.doesNotMatch(html, /data-remember/, 'consent belongs to the first-run card only');
    assert.doesNotMatch(html, /style="/);
    assert.equal(UI.definitionHtml({ ...withSetup, definition: { chips: [] } }), '');
    assert.equal(UI.definitionHtml(analysis), '');
    // A decimal typed into the window is rounded before it becomes a patch.
    const fakeCard = {
        querySelectorAll() {
            return [
                { dataset: { chip: 'window', original: '24' }, value: '36.6', type: 'number' },
                { dataset: { chip: 'method', original: 'auto' }, value: 'drift', type: 'select-one' },
            ];
        },
    };
    assert.deepEqual(JSON.parse(JSON.stringify(UI.collectPatch(fakeCard))), { window: 37, method: 'drift' });
}

// ── legacy chips (persisted before the metadata existed) ─────────────────────
{
    const legacy = [
        { key: 'measure_column', label: 'measure', value: 'Profit', options: ['Profit', 'SalesAmount'] },
        { key: 'window', label: 'window', value: 26, options: [] },
        { key: 'features', label: 'features', value: 'YearlyIncome, Age', options: [] },
    ];
    const html = UI.setupFormHtml(legacy, 'ml-x');
    const legends = [...html.matchAll(/<legend class="v3-ml-section-title">([^<]+)<\/legend>/g)].map((m) => m[1]);
    assert.deepEqual(legends, ['Setup'], 'no metadata → one section, never an invented "Model"');
    assert.match(html, /data-chip="window"[^>]*type="number" step="any"/, 'a numeric value is a number input even without bounds');
    assert.doesNotMatch(html, /min="/);
    // A comma string with no options renders as text; the patch still becomes a list server-side.
    assert.match(html, /data-chip="features"[^>]*type="text" value="YearlyIncome, Age"/);
    assert.equal(UI.normalizeChip({ key: 'k', value: 3 }).group, 'Setup');
    assert.equal(UI.normalizeChip({ key: 'k', value: 3 }).kind, 'number');
}

// ── multiselect + generic summary (entity family) ────────────────────────────
{
    const chips = [
        { key: 'entity_key', label: 'Entity', value: 'CustomerKey', options: ['CustomerKey'], group: 'Data', required: true },
        { key: 'features', label: 'Features', value: ['YearlyIncome', 'Age'], kind: 'multiselect', options: ['YearlyIncome', 'Age', 'TotalChildren'], min: 2, max: 8, group: 'Data' },
        { key: 'row_cap', label: 'Row cap', value: 50000, options: [1000, 50000], group: 'Data', unit: 'rows', option_labels: { '50000': '50,000', '1000': '1,000' } },
        { key: 'k', label: 'Segments', value: 'auto', options: ['auto', 2, 3], group: 'Model', option_labels: { auto: 'Auto' } },
    ];
    const html = UI.setupFormHtml(chips, 'ml-c');
    assert.match(html, /<input type="hidden" data-chip="features" data-kind="multiselect"[^>]*data-original="YearlyIncome,Age" value="YearlyIncome,Age">/);
    assert.match(html, /class="v3-ml-toggle is-on" data-toggle="YearlyIncome" aria-pressed="true"/);
    assert.match(html, /class="v3-ml-toggle" data-toggle="TotalChildren" aria-pressed="false"/);
    assert.match(html, /<span class="v3-ml-count" data-count>2 of 2–8<\/span>/);
    assert.match(html, /<option value="50000" selected>50,000<\/option>/);
    const legends = [...html.matchAll(/<legend class="v3-ml-section-title">([^<]+)<\/legend>/g)].map((m) => m[1]);
    assert.deepEqual(legends, ['Data', 'Model']);
    // Non-series skills list their filled fields.
    const summary = UI.summarySentence('clustering', UI.valuesOf(chips), chips);
    assert.equal(summary, 'Entity <bdi>CustomerKey</bdi> · Features <bdi>YearlyIncome, Age</bdi> · Row cap <bdi>50,000</bdi> rows · Segments <bdi>Auto</bdi>');
    // A multiselect patch is a list.
    const fakeCard = { querySelectorAll() { return [{ dataset: { chip: 'features', original: 'YearlyIncome,Age', kind: 'multiselect' }, value: 'YearlyIncome,Age,TotalChildren', type: 'hidden' }]; } };
    assert.deepEqual(JSON.parse(JSON.stringify(UI.collectPatch(fakeCard))), { features: ['YearlyIncome', 'Age', 'TotalChildren'] });
}

// ── summary sentence (series family) ─────────────────────────────────────────
{
    const values = UI.valuesOf(forecastChips);
    assert.equal(UI.summarySentence('forecast', values, forecastChips),
        'SUM(<bdi>SalesAmount</bdi>) · per month · last 24 months · 8 months ahead · 80% interval · Auto model');
    assert.equal(UI.summarySentence('forecast', { ...values, grain: 'week', window: 26, group_by: 'Territory', method: 'drift' }, forecastChips),
        'SUM(<bdi>SalesAmount</bdi>) · per week · last 26 weeks · 8 weeks ahead · 80% interval · Drift model · per <bdi>Territory</bdi>');
    assert.equal(UI.summarySentence('anomaly_detection', { measure_column: 'Profit', agg: 'avg', grain: 'day', window: 90, sensitivity: 0.95 },
        [{ key: 'sensitivity', label: 'Sensitivity', value: 0.95, options: [0.95], option_labels: { '0.95': '95%' } }]),
        'AVG(<bdi>Profit</bdi>) · per day · last 90 days · 95% sensitivity');
    // Escaped: a column name cannot inject markup.
    assert.doesNotMatch(UI.summarySentence('forecast', { ...values, measure_column: '<img src=x>' }, forecastChips), /<img/);
}

// ── validation against the declared bounds ───────────────────────────────────
{
    const values = UI.valuesOf(forecastChips);
    assert.deepEqual(JSON.parse(JSON.stringify(UI.validateValues(forecastChips, values))), { ok: true, errors: {} });
    let r = UI.validateValues(forecastChips, { ...values, window: 5, horizon: 200 });
    assert.equal(r.ok, false);
    assert.equal(r.errors.window, 'At least 12 months.');
    assert.equal(r.errors.horizon, 'At most 104 months.');
    r = UI.validateValues(forecastChips, { ...values, grain: 'week', window: 5 });
    assert.equal(r.errors.window, 'At least 12 weeks.', 'the unit follows the grain');
    r = UI.validateValues(forecastChips, { ...values, window: '', measure_column: '' });
    assert.equal(r.errors.window, undefined, 'an emptied optional number is not an error');
    assert.equal(r.errors.measure_column, 'Required.');
    r = UI.validateValues(forecastChips, { ...values, window: 'abc' });
    assert.equal(r.errors.window, 'Enter a number.');
    const multi = [{ key: 'features', label: 'Features', value: ['a', 'b'], kind: 'multiselect', options: ['a', 'b', 'c'], min: 2, max: 8 }];
    assert.equal(UI.validateValues(multi, { features: ['a'] }).errors.features, 'Pick at least 2.');
    assert.equal(UI.validateValues(multi, { features: ['a', 'b'] }).ok, true);
    // Read-only fields are never validated.
    assert.equal(UI.validateValues([{ key: 'x', value: '', required: true, editable: false }], { x: '' }).ok, true);
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
    assert.match(html, /data-skill="anomaly_detection" data-tier="A"/);
    assert.match(html, /data-run/);
    assert.match(html, /data-sql-instead/);
    assert.match(html, /data-switch-skill[^>]*>Rephrase the question</);
    assert.doesNotMatch(html, /Use a different skill/);
    // Consent sits inside the framed panel, after the actions.
    assert.match(html, /<div class="v3-ml-actions">[\s\S]*<\/div>\s*<label class="v3-ml-remember"><input type="checkbox" data-remember>/);
    assert.match(html, /data-egress data-egress-original="SQL rolls SUM\(Profit\)/);
    assert.match(html, /sent to the analysis service/);
    assert.match(html, /<select id="ml-confirm-measure_column" data-chip="measure_column"/);
    assert.match(html, /data-chip="window"[^>]*type="number"/);
    // The planning line is the live summary; the server sentence stays on hover.
    assert.match(html, /<div class="v3-ml-plan" title="Reading this as an anomaly check[^"]*">/);
    assert.match(html, /data-summary>SUM\(<bdi>Profit<\/bdi>\) · per week · last 26 weeks · 0\.95 sensitivity</);
    assert.match(html, /Aggregates only · ~4s/);
    assert.doesNotMatch(html, /TIER A/);
    // No inline styles or hex colours: tokens only.
    assert.doesNotMatch(html, /style="/);
    assert.doesNotMatch(html, /#[0-9a-f]{6}/i);
    // Escaping.
    const evil = UI.proposalHtml({ ...proposal, message: '<img src=x onerror=alert(1)>' });
    assert.doesNotMatch(evil, /<img/);
    // Without chips the summary falls back to the server's sentence.
    assert.match(UI.proposalHtml({ ...proposal, chips: [] }), /data-summary>Reading this as an anomaly check/);
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
