/**
 * Column mapping + quick visual toggles for charts.
 * @module ChartOptionsPanel
 */

import { applyQuickOptions, defaultLegendVisible, detectToggles } from '../utils/chartQuickOptions.js?v=73';
import { CHART_PALETTES, DEFAULT_PALETTE_ID, isKnownPalette, paletteSwatches } from '../utils/chartPalettes.js?v=1';

// Interface strings come from the locale catalog (static/i18n/i18n.js, loaded first).
const t = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const hasKey = (key) => Boolean(typeof window !== 'undefined' && window.I18n && typeof window.I18n.has === 'function' && window.I18n.has(key));
const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const TOGGLE_DEFS = [
    { key: 'dataLabels', label: 'charts.options.labels', title: 'charts.options.labelsTitle' },
    { key: 'legend', label: 'charts.options.legend', title: 'charts.options.legendTitle' },
    { key: 'dataZoom', label: 'charts.options.zoom', title: 'charts.options.zoomTitle' },
    { key: 'sortDesc', label: 'charts.options.sortDesc', title: 'charts.options.sortDescTitle' },
];

// Numeric columns whose NAME ends in an identifier/ordinal token are dimensions
// (e.g. month_number, year, order_id), not measures — don't default Y to them.
const IDENTIFIER_RE = /(id|key|code|number|no|num|year|month|day|quarter|qtr|week|rank|index|idx|seq)s?$/i;

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
     *   onQuickToggle: (toggles: object, applyToConfig: (cfg: object) => object, change: {key:string,value:boolean}) => void,
     *   onPaletteChange?: (paletteId: string) => void,
     *   getThemeColors?: () => string[],
     *   initialPalette?: string,
     * }} hooks
     */
    constructor(containerId, hooks) {
        this.containerId = containerId;
        this.hooks = hooks || {};
        this.palette = isKnownPalette(this.hooks.initialPalette) ? this.hooks.initialPalette : DEFAULT_PALETTE_ID;
        this.customPalette = null;
        this._paletteOpen = false;
        this.columns = [];
        this.mapping = { xColumn: '', yColumn: '', seriesColumn: '' };
        this.mapMapping = {
            locationColumn: '', latitudeColumn: '', longitudeColumn: '',
            placeColumn: '', admin1Column: '', countryColumn: '', postalColumn: '',
            valueColumn: '', value2Column: '', aggregate: 'sum',
        };
        // Which fields the USER explicitly chose. Only these are sent as
        // overrides — auto-defaults must NOT clobber the LLM's column choice.
        this.userSet = { xColumn: false, yColumn: false, seriesColumn: false };
        this.mapUserSet = Object.fromEntries(
            Object.keys(this.mapMapping).map((key) => [key, false])
        );
        this.chartType = 'bar';
        this.analysisMode = false;
        this.toggles = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };
        // The legend default is derived per chart (hidden when it would list a
        // single entry) until the user flips the pill themselves.
        this.legendUserSet = false;
        // Column pickers are collapsed by default (mockup: "Columns ▾" disclosure).
        this._columnsOpen = false;
        this._mounted = false;
    }

    /**
     * Apply the automatic legend default for a (re)built config: hidden when
     * the legend would only repeat the single measure named on the axis, shown
     * for multi-series and pie-like charts. Skipped once the user has toggled
     * the legend pill for this dataset. Saved charts are re-evaluated too, so
     * older configs that recorded the previous always-on default follow the
     * same rule.
     */
    applyLegendDefault(config) {
        if (this.legendUserSet || !config || typeof config !== 'object') return;
        this.toggles.legend = defaultLegendVisible(config);
        const btn = document.querySelector('.chart-opt-toggle[data-key="legend"]');
        if (btn) btn.classList.toggle('is-on', this.toggles.legend);
    }

    /**
     * @param {Array<{name: string, type: string}>} columns
     * @param {object|null} analysis
     */
    setColumns(columns, analysis = null) {
        this.columns = columns || [];
        this.mapping = defaultMapping(this.columns, analysis);
        this.mapMapping = {
            ...this.mapMapping,
            locationColumn: this.mapping.xColumn,
            placeColumn: this.mapping.xColumn,
            valueColumn: this.mapping.yColumn,
        };
        // New dataset → nothing is user-chosen yet.
        this.userSet = { xColumn: false, yColumn: false, seriesColumn: false };
        this.legendUserSet = false;
        this.mapUserSet = Object.fromEntries(
            Object.keys(this.mapMapping).map((key) => [key, false])
        );
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
        if (this.chartType === 'osm_map') {
            const out = {};
            const keys = [
                'locationColumn', 'latitudeColumn', 'longitudeColumn', 'placeColumn',
                'admin1Column', 'countryColumn', 'postalColumn', 'valueColumn',
                'value2Column', 'aggregate',
            ];
            for (const key of keys) {
                if (this.mapUserSet[key]) out[key] = this.mapMapping[key];
            }
            const parts = {
                place: out.placeColumn,
                admin1: out.admin1Column,
                country: out.countryColumn,
                postal: out.postalColumn,
            };
            if (Object.values(parts).some(Boolean)) out.locationParts = parts;
            return out;
        }
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
        if (spec.location) this.mapMapping.locationColumn = spec.location;
        if (spec.latitude) this.mapMapping.latitudeColumn = spec.latitude;
        if (spec.longitude) this.mapMapping.longitudeColumn = spec.longitude;
        if (spec.value) this.mapMapping.valueColumn = spec.value;
        this.mapMapping.value2Column = spec.value2 || '';
        this.mapMapping.aggregate = spec.aggregate || this.mapMapping.aggregate || 'sum';
        const parts = spec.location_parts || {};
        this.mapMapping.placeColumn = parts.place || spec.location || '';
        this.mapMapping.admin1Column = parts.admin1 || '';
        this.mapMapping.countryColumn = parts.country || '';
        this.mapMapping.postalColumn = parts.postal || '';
        if (this._mounted) this._syncSelects();
    }

    setChartType(chartType) {
        const next = chartType === 'osm_map' ? 'osm_map' : 'standard';
        if (this.chartType === next) return;
        this.chartType = next;
        if (this._mounted) this.render();
    }

    getToggles() {
        return { ...this.toggles };
    }

    setToggles(patch = {}) {
        for (const [key, value] of Object.entries(patch)) {
            if (key in this.toggles && typeof value === 'boolean') this.toggles[key] = value;
        }
        if (Object.prototype.hasOwnProperty.call(patch, 'legend')) this.legendUserSet = true;
        document.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            const key = btn.dataset.key;
            if (key in this.toggles) btn.classList.toggle('is-on', !!this.toggles[key]);
        });
    }

    /**
     * Mirror the toggle state encoded in a (chat-edited) config so re-applying
     * quick options doesn't undo an LLM change like "add data labels". Refreshes
     * the toggle buttons so they reflect what's actually on the chart.
     * @param {object} config
     */
    syncTogglesFromConfig(config) {
        const detected = detectToggles(config);
        for (const k of ['dataLabels', 'dataZoom']) {
            if (typeof detected[k] === 'boolean') this.toggles[k] = detected[k];
        }
        // Legend: an edit that carries a legend object states intent (explicit
        // `show`, or its presence). An edit without any legend leaves the
        // current toggle alone instead of resurrecting a single-entry legend.
        if (config && config.legend && typeof config.legend === 'object') {
            this.toggles.legend = typeof config.legend.show === 'boolean' ? config.legend.show : true;
        }
        document.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            const key = btn.dataset.key;
            if (key in this.toggles) btn.classList.toggle('is-on', !!this.toggles[key]);
        });
    }

    /** Apply current toggles to a config copy (uses baseline for sort restore). */
    applyTogglesTo(config, baselineConfig = null, options = {}) {
        return applyQuickOptions(config, this.toggles, baselineConfig, options);
    }

    render() {
        const container = document.getElementById(this.containerId);
        if (!container) return;
        if (this.chartType === 'osm_map') {
            this._renderMapBindings(container);
            this._mounted = true;
            return;
        }

        const caret = '<svg class="chart-cols-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
            '<path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

        // Slim toolbar: Columns disclosure + divider + quick-option pills, with
        // the X/Y/Series selects collapsed into an inline panel that spans the
        // full row when expanded.
        container.innerHTML = `
            <button type="button" class="chart-cols-btn${this._columnsOpen ? ' is-open' : ''}" id="chart-cols-btn"
                    aria-expanded="${this._columnsOpen ? 'true' : 'false'}" aria-controls="chart-cols-expand"
                    title="${esc(this.analysisMode ? t('charts.options.analysisBindingLocked') : t('charts.options.columnsTitle'))}"
                    ${this.analysisMode ? 'disabled aria-disabled="true" aria-describedby="chart-analysis-lock-note"' : ''}>
                <span>${esc(t('charts.options.columns'))}</span>${caret}
            </button>
            <button type="button" class="chart-cols-btn chart-palette-btn${this._paletteOpen ? ' is-open' : ''}" id="chart-palette-btn"
                    aria-expanded="${this._paletteOpen ? 'true' : 'false'}" aria-controls="chart-palette-expand"
                    title="${esc(t('charts.options.paletteTitle'))}">
                <span class="chart-palette-dots" id="chart-palette-dots" aria-hidden="true"></span><span>${esc(t('charts.options.colors'))}</span>${caret}
            </button>
            <div class="chart-opts-divider" aria-hidden="true"></div>
            <div class="chart-opts-toggles" id="chart-opt-toggles"></div>
            ${this.analysisMode ? `<span class="chart-analysis-lock-note" id="chart-analysis-lock-note" role="note">${esc(t('charts.options.analysisLockedHint'))}</span>` : ''}
            <div class="chart-cols-expand chart-palette-expand" id="chart-palette-expand" role="radiogroup"
                 aria-label="${esc(t('charts.options.paletteTitle'))}"${this._paletteOpen ? '' : ' hidden'}></div>
            <div class="chart-cols-expand" id="chart-cols-expand"${this._columnsOpen ? '' : ' hidden'}>
                <label class="chart-col-field">
                    <span>${esc(t('charts.options.xCategory'))}</span>
                    <select id="chart-opt-x" class="chart-options-select"></select>
                </label>
                <label class="chart-col-field">
                    <span>${esc(t('charts.options.yValue'))}</span>
                    <select id="chart-opt-y" class="chart-options-select"></select>
                </label>
                <label class="chart-col-field">
                    <span>${esc(t('charts.options.seriesOptional'))}</span>
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
            togglesHost.innerHTML = TOGGLE_DEFS.map((def) => {
                const disabled = this.analysisMode && def.key === 'sortDesc';
                const title = disabled ? t('charts.options.analysisSortLocked') : t(def.title);
                return (
                `<button type="button" class="chart-opt-toggle${this.toggles[def.key] ? ' is-on' : ''}"
                    data-key="${def.key}" title="${esc(title)}"${disabled ? ' disabled aria-disabled="true" aria-describedby="chart-analysis-lock-note"' : ''}>${esc(t(def.label))}</button>`
                );
            }).join('');
        }

        this._renderPaletteChips();

        document.getElementById('chart-cols-btn')?.addEventListener('click', () => this._toggleColumns());
        document.getElementById('chart-palette-btn')?.addEventListener('click', () => this._togglePalette());
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
        if (this.analysisMode) return;
        this._columnsOpen = !this._columnsOpen;
        if (this._columnsOpen && this._paletteOpen) this._togglePalette();
        const btn = document.getElementById('chart-cols-btn');
        const panel = document.getElementById('chart-cols-expand');
        if (btn) {
            btn.classList.toggle('is-open', this._columnsOpen);
            btn.setAttribute('aria-expanded', this._columnsOpen ? 'true' : 'false');
        }
        if (panel) panel.hidden = !this._columnsOpen;
    }

    // ── Colour palette ──────────────────────────────────────────────────────

    getPalette() {
        return this.palette;
    }

    /** Reflect an externally chosen palette (e.g. restored preference). */
    setPalette(id) {
        if (!isKnownPalette(id)) return;
        this.palette = id;
        this.customPalette = null;
        if (this._mounted) this._renderPaletteChips();
    }

    setCustomPalette(name, colors) {
        if (!Array.isArray(colors) || !colors.length) return;
        this.customPalette = { name: String(name || 'custom'), colors: colors.slice() };
        if (this._mounted) this._renderPaletteChips();
    }

    clearCustomPalette() {
        this.customPalette = null;
        if (this._mounted) this._renderPaletteChips();
    }

    _togglePalette() {
        this._paletteOpen = !this._paletteOpen;
        if (this._paletteOpen && this._columnsOpen) this._toggleColumns();
        const btn = document.getElementById('chart-palette-btn');
        const panel = document.getElementById('chart-palette-expand');
        if (btn) {
            btn.classList.toggle('is-open', this._paletteOpen);
            btn.setAttribute('aria-expanded', this._paletteOpen ? 'true' : 'false');
        }
        if (panel) panel.hidden = !this._paletteOpen;
    }

    _themeColors() {
        try {
            const colors = this.hooks.getThemeColors ? this.hooks.getThemeColors() : null;
            return Array.isArray(colors) && colors.length ? colors : null;
        } catch (_) {
            return null;
        }
    }

    _dotsHtml(colors) {
        return colors.map((c) => `<i style="background:${c}"></i>`).join('');
    }

    _renderPaletteChips() {
        const theme = this._themeColors();
        const dots = document.getElementById('chart-palette-dots');
        const activeColors = this.customPalette?.colors || paletteSwatches(this.palette, theme);
        if (dots) dots.innerHTML = this._dotsHtml(activeColors.slice(0, 4));
        const host = document.getElementById('chart-palette-expand');
        if (!host) return;
        const custom = this.customPalette
            ? `<button type="button" class="chart-palette-chip is-active" role="radio" aria-checked="true"
                       data-palette="__custom__" title="${esc(t('charts.options.colorsCustomTitle'))}">
                   <span class="chart-palette-dots" aria-hidden="true">${this._dotsHtml(this.customPalette.colors.slice(0, 4))}</span>
                   <span>${esc(t('charts.options.colorsCustom'))}</span>
               </button>`
            : '';
        host.innerHTML = custom + CHART_PALETTES.map((p) => {
            const active = !this.customPalette && p.id === this.palette;
            return `<button type="button" class="chart-palette-chip${active ? ' is-active' : ''}" role="radio"
                        aria-checked="${active ? 'true' : 'false'}" data-palette="${p.id}" title="${p.label} palette">
                        <span class="chart-palette-dots" aria-hidden="true">${this._dotsHtml(paletteSwatches(p.id, theme))}</span>
                        <span>${p.label}</span>
                    </button>`;
        }).join('');
        host.querySelectorAll('.chart-palette-chip').forEach((chip) => {
            chip.addEventListener('click', () => this._onPaletteChange(chip.dataset.palette));
        });
    }

    _onPaletteChange(id) {
        if (id === '__custom__') return;
        if (!isKnownPalette(id)) return;
        this.palette = id;
        this.customPalette = null;
        this._renderPaletteChips();
        if (this.hooks.onPaletteChange) this.hooks.onPaletteChange(id);
    }

    _fillSelect(id, cols, selected, filterFn, numericOnly = false, allowEmpty = false) {
        const sel = document.getElementById(id);
        if (!sel) return;
        const list = cols.filter((c) => filterFn(c));
        let html = allowEmpty ? `<option value="">${esc(t('charts.options.none'))}</option>` : '';
        // Column names come from query results / schemas — never trust them as markup.
        html += list.map((c) =>
            `<option value="${esc(c.name)}"${c.name === selected ? ' selected' : ''}>${esc(c.name)}${numericOnly && c.type === 'numeric' ? ' (#)' : ''}</option>`
        ).join('');
        sel.innerHTML = html || '<option value="">—</option>';
    }

    _syncSelects() {
        if (this.chartType === 'osm_map') {
            this._fillMapSelects();
            return;
        }
        const numericCols = this.columns.filter((c) => c.type === 'numeric');
        const yCols = numericCols.length ? numericCols : this.columns;
        this._fillSelect('chart-opt-x', this.columns, this.mapping.xColumn, () => true);
        this._fillSelect('chart-opt-y', yCols, this.mapping.yColumn, () => true, numericCols.length > 0);
        this._fillSelect('chart-opt-series', this.columns.filter((c) => c.type === 'category'), this.mapping.seriesColumn, () => true, false, true);
    }

    _onColumnChange(field, value) {
        if (this.analysisMode) return;
        this.mapping[field] = value;
        if (field in this.userSet) this.userSet[field] = true;
        if (this.hooks.onColumnsChange) {
            this.hooks.onColumnsChange(this.getMapping());
        }
    }

    _renderMapBindings(container) {
        const caret = '<svg class="chart-cols-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
            '<path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        container.innerHTML = `
            <button type="button" class="chart-cols-btn${this._columnsOpen ? ' is-open' : ''}" id="chart-cols-btn"
                    aria-expanded="${this._columnsOpen ? 'true' : 'false'}" aria-controls="chart-cols-expand"
                    title="${esc(t('charts.options.mapBindingsTitle'))}">
                <span>${esc(t('charts.options.mapBindings'))}</span>${caret}
            </button>
            <div class="chart-cols-expand chart-map-bindings" id="chart-cols-expand"${this._columnsOpen ? '' : ' hidden'}>
                <label class="chart-col-field"><span>${esc(t('charts.options.locationLabel'))}</span><select id="map-opt-location" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.cityPlace'))}</span><select id="map-opt-place" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.stateProvince'))}</span><select id="map-opt-admin1" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.country'))}</span><select id="map-opt-country" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.postalCode'))}</span><select id="map-opt-postal" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.latitude'))}</span><select id="map-opt-latitude" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.longitude'))}</span><select id="map-opt-longitude" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.colorValue'))}</span><select id="map-opt-value" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.sizeOptional'))}</span><select id="map-opt-value2" class="chart-options-select"></select></label>
                <label class="chart-col-field"><span>${esc(t('charts.options.aggregate'))}</span>
                    <select id="map-opt-aggregate" class="chart-options-select">
                        ${['sum', 'avg', 'count', 'min', 'max', 'none'].map((value) =>
                            `<option value="${value}"${value === this.mapMapping.aggregate ? ' selected' : ''}>${value}</option>`
                        ).join('')}
                    </select>
                </label>
            </div>
        `;
        this._fillMapSelects();
        document.getElementById('chart-cols-btn')?.addEventListener('click', () => this._toggleColumns());
        const fields = {
            'map-opt-location': 'locationColumn',
            'map-opt-place': 'placeColumn',
            'map-opt-admin1': 'admin1Column',
            'map-opt-country': 'countryColumn',
            'map-opt-postal': 'postalColumn',
            'map-opt-latitude': 'latitudeColumn',
            'map-opt-longitude': 'longitudeColumn',
            'map-opt-value': 'valueColumn',
            'map-opt-value2': 'value2Column',
            'map-opt-aggregate': 'aggregate',
        };
        Object.entries(fields).forEach(([id, field]) => {
            document.getElementById(id)?.addEventListener('change', (event) => {
                this.mapMapping[field] = event.target.value;
                this.mapUserSet[field] = true;
                if (field === 'placeColumn') {
                    this.mapMapping.locationColumn = event.target.value;
                    this.mapUserSet.locationColumn = true;
                }
                if (this.hooks.onColumnsChange) this.hooks.onColumnsChange(this.getMapping());
            });
        });
    }

    _fillMapSelects() {
        const all = this.columns;
        const numeric = this.columns.filter((column) => column.type === 'numeric');
        const measure = numeric.filter((column) => !looksLikeIdentifier(column.name));
        const values = measure.length ? measure : numeric;
        const fill = (id, columns, selected, empty = true, virtualCount = false) => {
            const select = document.getElementById(id);
            if (!select) return;
            let html = empty ? `<option value="">${esc(t('charts.options.none'))}</option>` : '';
            if (virtualCount) {
                html += `<option value="__row_count__"${selected === '__row_count__' ? ' selected' : ''}>${esc(t('charts.options.rowCount'))}</option>`;
            }
            html += columns.map((column) =>
                `<option value="${esc(column.name)}"${column.name === selected ? ' selected' : ''}>${esc(column.name)}</option>`
            ).join('');
            select.innerHTML = html || '<option value="">—</option>';
        };
        fill('map-opt-location', all, this.mapMapping.locationColumn);
        fill('map-opt-place', all, this.mapMapping.placeColumn);
        fill('map-opt-admin1', all, this.mapMapping.admin1Column);
        fill('map-opt-country', all, this.mapMapping.countryColumn);
        fill('map-opt-postal', all, this.mapMapping.postalColumn);
        fill('map-opt-latitude', numeric, this.mapMapping.latitudeColumn);
        fill('map-opt-longitude', numeric, this.mapMapping.longitudeColumn);
        fill('map-opt-value', values, this.mapMapping.valueColumn, false, true);
        fill('map-opt-value2', values, this.mapMapping.value2Column);
    }

    _onToggle(key) {
        if (!(key in this.toggles)) return;
        if (this.analysisMode && key === 'sortDesc') return;
        this.toggles[key] = !this.toggles[key];
        if (key === 'legend') this.legendUserSet = true;
        const btn = document.querySelector(`.chart-opt-toggle[data-key="${key}"]`);
        if (btn) btn.classList.toggle('is-on', this.toggles[key]);
        if (this.hooks.onQuickToggle) {
            this.hooks.onQuickToggle(
                this.getToggles(),
                (cfg, baseline, options) => this.applyTogglesTo(cfg, baseline, options),
                { key, value: this.toggles[key] },
            );
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

    setAnalysisMode(on) {
        const next = Boolean(on);
        if (this.analysisMode === next) return;
        this.analysisMode = next;
        if (next) this.closeDisclosures();
        if (this._mounted) this.render();
    }

    closeDisclosures() {
        this._columnsOpen = false;
        this._paletteOpen = false;
        const columnsButton = document.getElementById('chart-cols-btn');
        const columnsPanel = document.getElementById('chart-cols-expand');
        const paletteButton = document.getElementById('chart-palette-btn');
        const palettePanel = document.getElementById('chart-palette-expand');
        if (columnsButton) {
            columnsButton.classList.remove('is-open');
            columnsButton.setAttribute('aria-expanded', 'false');
        }
        if (columnsPanel) columnsPanel.hidden = true;
        if (paletteButton) {
            paletteButton.classList.remove('is-open');
            paletteButton.setAttribute('aria-expanded', 'false');
        }
        if (palettePanel) palettePanel.hidden = true;
    }

    resetToggles() {
        this.toggles = { dataLabels: false, legend: true, dataZoom: false, sortDesc: false };
        this.legendUserSet = false;
        document.querySelectorAll('.chart-opt-toggle').forEach((btn) => {
            const key = btn.dataset.key;
            btn.classList.toggle('is-on', !!this.toggles[key]);
        });
    }
}
