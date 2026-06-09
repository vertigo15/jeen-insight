/**
 * Column mapping + quick visual toggles for charts.
 * @module ChartOptionsPanel
 */

import { applyQuickOptions } from '../utils/chartQuickOptions.js';

const TOGGLE_DEFS = [
    { key: 'dataLabels', label: 'Labels', title: 'Show data labels on series' },
    { key: 'legend', label: 'Legend', title: 'Show chart legend' },
    { key: 'dataZoom', label: 'Zoom', title: 'Enable slider + scroll zoom' },
    { key: 'sortDesc', label: 'Sort ↓', title: 'Sort categories high to low' },
];

/**
 * @param {Array<{name: string, type: string}>} columns
 * @param {{ xAxisColumn?: object, yAxisColumn?: object }|null} analysis
 */
function defaultMapping(columns, analysis) {
    const xDefault = analysis?.xAxisColumn?.name || columns.find((c) => c.type !== 'numeric')?.name || columns[0]?.name || '';
    const yDefault = analysis?.yAxisColumn?.name || columns.find((c) => c.type === 'numeric')?.name || '';
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
        this.toggles = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };
        this._mounted = false;
    }

    /**
     * @param {Array<{name: string, type: string}>} columns
     * @param {object|null} analysis
     */
    setColumns(columns, analysis = null) {
        this.columns = columns || [];
        this.mapping = defaultMapping(this.columns, analysis);
        if (this._mounted) this._syncSelects();
    }

    getMapping() {
        return { ...this.mapping };
    }

    getToggles() {
        return { ...this.toggles };
    }

    /** Apply current toggles to a config copy (uses baseline for sort restore). */
    applyTogglesTo(config, baselineConfig = null) {
        return applyQuickOptions(config, this.toggles, baselineConfig);
    }

    render() {
        const container = document.getElementById(this.containerId);
        if (!container) return;

        container.innerHTML = `
            <div class="chart-options-panel">
                <div class="chart-options-section">
                    <span class="chart-options-heading">Columns</span>
                    <div class="chart-options-row">
                        <label class="chart-options-field">
                            <span>X / category</span>
                            <select id="chart-opt-x" class="chart-options-select"></select>
                        </label>
                        <label class="chart-options-field">
                            <span>Y / value</span>
                            <select id="chart-opt-y" class="chart-options-select"></select>
                        </label>
                        <label class="chart-options-field">
                            <span>Series (optional)</span>
                            <select id="chart-opt-series" class="chart-options-select"></select>
                        </label>
                    </div>
                </div>
                <div class="chart-options-section">
                    <span class="chart-options-heading">Quick options</span>
                    <div class="chart-options-toggles" id="chart-opt-toggles"></div>
                </div>
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

        document.getElementById('chart-opt-x')?.addEventListener('change', (e) => this._onColumnChange('xColumn', e.target.value));
        document.getElementById('chart-opt-y')?.addEventListener('change', (e) => this._onColumnChange('yColumn', e.target.value));
        document.getElementById('chart-opt-series')?.addEventListener('change', (e) => this._onColumnChange('seriesColumn', e.target.value));
        togglesHost?.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            btn.addEventListener('click', () => this._onToggle(btn.dataset.key));
        });

        this._mounted = true;
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
        const el = document.getElementById(this.containerId);
        if (el) el.style.display = 'block';
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
