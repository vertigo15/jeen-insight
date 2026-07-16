/**
 * Column mapping + quick visual toggles for charts.
 * @module ChartOptionsPanel
 */

import { applyQuickOptions, detectToggles } from '../utils/chartQuickOptions.js?v=70';

const TOGGLE_DEFS = [
    { key: 'dataLabels', label: 'Labels', title: 'Show data labels on series' },
    { key: 'legend', label: 'Legend', title: 'Show chart legend' },
    { key: 'dataZoom', label: 'Zoom', title: 'Enable slider + scroll zoom' },
    { key: 'sortDesc', label: 'Sort ↓', title: 'Sort categories high to low' },
];

// Numeric columns whose NAME ends in an identifier/ordinal token are dimensions
// (e.g. month_number, year, order_id), not measures — don't default Y to them.
const IDENTIFIER_RE = /(^|_)(id|number|no|num|year|month|day|quarter|qtr|week|rank|index|idx|seq)s?$/i;

function looksLikeIdentifier(name) {
    return IDENTIFIER_RE.test(name || '');
}

/**
 * @param {Array<{name: string, type: string}>} columns
 * @param {{ xAxisColumn?: object, yAxisColumn?: object }|null} analysis
 */
function defaultMapping(columns, analysis) {
    const numeric = columns.filter((c) => c.type === 'numeric');
    // Prefer a "real" measure (skip id/ordinal columns like month_number).
    const measure = numeric.find((c) => !looksLikeIdentifier(c.name)) || numeric[0];
    const analysisY = analysis?.yAxisColumn?.name;
    const xDefault = analysis?.xAxisColumn?.name || columns.find((c) => c.type !== 'numeric')?.name || columns[0]?.name || '';
    const yDefault = (analysisY && !looksLikeIdentifier(analysisY))
        ? analysisY
        : (measure?.name || analysisY || '');
    return { xColumn: xDefault, yColumn: yDefault, seriesColumn: '' };
}

export class ChartOptionsPanel {
    /**
     * @param {string} containerId
     * @param {{
     *   onColumnsChange: (mapping: { xColumn: string, yColumn: string, seriesColumn: string }) => void,
     *   onQuickToggle: (toggles: object, applyToConfig: (cfg: object) => object) => void,
     * }} hooks
     */
    constructor(containerId, hooks) {
        this.containerId = containerId;
        this.hooks = hooks || {};
        this.columns = [];
        this.mapping = { xColumn: '', yColumn: '', seriesColumn: '' };
        // Which fields the USER explicitly chose. Only these are sent as
        // overrides — auto-defaults must NOT clobber the LLM's column choice.
        this.userSet = { xColumn: false, yColumn: false, seriesColumn: false };
        this.toggles = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };
        // Column pickers are collapsed by default (mockup: "Columns ▾" disclosure).
        this._columnsOpen = false;
        this._mounted = false;
    }

    /**
     * @param {Array<{name: string, type: string}>} columns
     * @param {object|null} analysis
     */
    setColumns(columns, analysis = null) {
        this.columns = columns || [];
        this.mapping = defaultMapping(this.columns, analysis);
        // New dataset → nothing is user-chosen yet.
        this.userSet = { xColumn: false, yColumn: false, seriesColumn: false };
        if (this._mounted) this._syncSelects();
    }

    getMapping() {
        return { ...this.mapping };
    }

    /**
     * Column overrides to send to the server — ONLY fields the user explicitly
     * changed. On the initial Auto run this is empty, so the LLM decides x/y/series.
     */
    getOverrides() {
        const out = {};
        for (const k of ['xColumn', 'yColumn', 'seriesColumn']) {
            if (this.userSet[k] && this.mapping[k]) out[k] = this.mapping[k];
        }
        return out;
    }

    /**
     * Reflect the LLM's resolved spec in the dropdowns (without marking the
     * fields as user-chosen), so the panel shows what was actually charted and
     * later tweaks start from there.
     * @param {{x?: string, y?: string|string[], series?: string|null}} spec
     */
    syncFromSpec(spec) {
        if (!spec) return;
        const y = Array.isArray(spec.y) ? spec.y[0] : spec.y;
        if (spec.x) this.mapping.xColumn = spec.x;
        if (y) this.mapping.yColumn = y;
        this.mapping.seriesColumn = spec.series || '';
        if (this._mounted) this._syncSelects();
    }

    getToggles() {
        return { ...this.toggles };
    }

    /**
     * Mirror the toggle state encoded in a (chat-edited) config so re-applying
     * quick options doesn't undo an LLM change like "add data labels". Refreshes
     * the toggle buttons so they reflect what's actually on the chart.
     * @param {object} config
     */
    syncTogglesFromConfig(config) {
        const detected = detectToggles(config);
        for (const k of ['dataLabels', 'legend', 'dataZoom']) {
            if (typeof detected[k] === 'boolean') this.toggles[k] = detected[k];
        }
        document.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            const key = btn.dataset.key;
            if (key in this.toggles) btn.classList.toggle('is-on', !!this.toggles[key]);
        });
    }

    /** Apply current toggles to a config copy (uses baseline for sort restore). */
    applyTogglesTo(config, baselineConfig = null) {
        return applyQuickOptions(config, this.toggles, baselineConfig);
    }

    render() {
        const container = document.getElementById(this.containerId);
        if (!container) return;

        const caret = '<svg class="chart-cols-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
            '<path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

        // Slim toolbar: Columns disclosure + divider + quick-option pills, with
        // the X/Y/Series selects collapsed into an inline panel that spans the
        // full row when expanded.
        container.innerHTML = `
            <button type="button" class="chart-cols-btn${this._columnsOpen ? ' is-open' : ''}" id="chart-cols-btn"
                    aria-expanded="${this._columnsOpen ? 'true' : 'false'}" aria-controls="chart-cols-expand"
                    title="Choose X / Y / Series columns">
                <span>Columns</span>${caret}
            </button>
            <div class="chart-opts-divider" aria-hidden="true"></div>
            <div class="chart-opts-toggles" id="chart-opt-toggles"></div>
            <div class="chart-cols-expand" id="chart-cols-expand"${this._columnsOpen ? '' : ' hidden'}>
                <label class="chart-col-field">
                    <span>X / Category</span>
                    <select id="chart-opt-x" class="chart-options-select"></select>
                </label>
                <label class="chart-col-field">
                    <span>Y / Value</span>
                    <select id="chart-opt-y" class="chart-options-select"></select>
                </label>
                <label class="chart-col-field">
                    <span>Series (optional)</span>
                    <select id="chart-opt-series" class="chart-options-select"></select>
                </label>
            </div>
        `;

        const numericCols = this.columns.filter((c) => c.type === 'numeric');
        const yCols = numericCols.length ? numericCols : this.columns;
        this._fillSelect('chart-opt-x', this.columns, this.mapping.xColumn, () => true);
        this._fillSelect('chart-opt-y', yCols, this.mapping.yColumn, () => true, numericCols.length > 0);
        this._fillSelect('chart-opt-series', this.columns.filter((c) => c.type === 'category'), this.mapping.seriesColumn, () => true, false, true);

        const togglesHost = document.getElementById('chart-opt-toggles');
        if (togglesHost) {
            togglesHost.innerHTML = TOGGLE_DEFS.map((t) =>
                `<button type="button" class="chart-opt-toggle${this.toggles[t.key] ? ' is-on' : ''}"
                    data-key="${t.key}" title="${t.title}">${t.label}</button>`
            ).join('');
        }

        document.getElementById('chart-cols-btn')?.addEventListener('click', () => this._toggleColumns());
        document.getElementById('chart-opt-x')?.addEventListener('change', (e) => this._onColumnChange('xColumn', e.target.value));
        document.getElementById('chart-opt-y')?.addEventListener('change', (e) => this._onColumnChange('yColumn', e.target.value));
        document.getElementById('chart-opt-series')?.addEventListener('change', (e) => this._onColumnChange('seriesColumn', e.target.value));
        togglesHost?.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            btn.addEventListener('click', () => this._onToggle(btn.dataset.key));
        });

        this._mounted = true;
    }

    /** Expand/collapse the X/Y/Series column pickers. */
    _toggleColumns() {
        this._columnsOpen = !this._columnsOpen;
        const btn = document.getElementById('chart-cols-btn');
        const panel = document.getElementById('chart-cols-expand');
        if (btn) {
            btn.classList.toggle('is-open', this._columnsOpen);
            btn.setAttribute('aria-expanded', this._columnsOpen ? 'true' : 'false');
        }
        if (panel) panel.hidden = !this._columnsOpen;
    }

    _fillSelect(id, cols, selected, filterFn, numericOnly = false, allowEmpty = false) {
        const sel = document.getElementById(id);
        if (!sel) return;
        const list = cols.filter((c) => filterFn(c));
        let html = allowEmpty ? '<option value="">— none —</option>' : '';
        html += list.map((c) =>
            `<option value="${c.name}"${c.name === selected ? ' selected' : ''}>${c.name}${numericOnly && c.type === 'numeric' ? ' (#)' : ''}</option>`
        ).join('');
        sel.innerHTML = html || '<option value="">—</option>';
    }

    _syncSelects() {
        const numericCols = this.columns.filter((c) => c.type === 'numeric');
        const yCols = numericCols.length ? numericCols : this.columns;
        this._fillSelect('chart-opt-x', this.columns, this.mapping.xColumn, () => true);
        this._fillSelect('chart-opt-y', yCols, this.mapping.yColumn, () => true, numericCols.length > 0);
        this._fillSelect('chart-opt-series', this.columns.filter((c) => c.type === 'category'), this.mapping.seriesColumn, () => true, false, true);
    }

    _onColumnChange(field, value) {
        this.mapping[field] = value;
        if (field in this.userSet) this.userSet[field] = true;
        if (this.hooks.onColumnsChange) {
            this.hooks.onColumnsChange(this.getMapping());
        }
    }

    _onToggle(key) {
        if (!(key in this.toggles)) return;
        this.toggles[key] = !this.toggles[key];
        const btn = document.querySelector(`.chart-opt-toggle[data-key="${key}"]`);
        if (btn) btn.classList.toggle('is-on', this.toggles[key]);
        if (this.hooks.onQuickToggle) {
            this.hooks.onQuickToggle(this.getToggles(), (cfg, baseline) => this.applyTogglesTo(cfg, baseline));
        }
    }

    show() {
        // Clear the inline override so the container falls back to its CSS
        // `display: contents` — its children (Columns button, pills, expand
        // panel) then flow directly into the shared toolbar row.
        const el = document.getElementById(this.containerId);
        if (el) el.style.display = '';
    }

    hide() {
        const el = document.getElementById(this.containerId);
        if (el) el.style.display = 'none';
    }

    resetToggles() {
        this.toggles = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };
        document.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            const key = btn.dataset.key;
            btn.classList.toggle('is-on', !!this.toggles[key]);
        });
    }
}
