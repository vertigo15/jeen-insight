/**
 * Chart Manager
 * Main orchestrator for chart feature
 *
 * @module chartManager
 */

/// <reference path="./types/chart.types.js" />

import { analyzeData } from './utils/dataAnalyzer.js';
import { collectNumericValues, makeLabelFormatter, makeValueFormatter } from './utils/valueFormat.js?v=75';
import { ChartContainer } from './components/ChartContainer.js?v=80';
import { ChartToggle } from './components/ChartToggle.js?v=3';
import { ChartTypeSelector } from './components/ChartTypeSelector.js?v=79';
import { ChartOptionsPanel } from './components/ChartOptionsPanel.js?v=76';
import { MapOptionsPanel, MAP_PALETTES } from './components/MapOptionsPanel.js?v=3';
import { DEFAULT_PALETTE_ID, applyPalette, getPalette, isKnownPalette } from './utils/chartPalettes.js?v=1';
import { ChartChat } from './components/ChartChat.js?v=110';
import { applyDerivedSeries, stripDerivedSeries } from './utils/chartOperators.js?v=2';
import { ensureMapsForOption, isMapOption } from './utils/mapAssets.js?v=81';
import { OsmMapRenderer } from './utils/osmMapRenderer.js?v=9';
import { CHART_TYPE_VALUES } from './chartTypes.js?v=79';
import { localizeSeriesLabels } from './utils/seriesLabels.js?v=1';
import { applyQuickOptions } from './utils/chartQuickOptions.js?v=3';
import {
    applyStyleOverrides,
    createChartSession,
} from './utils/chartSession.js?v=3';
import {
    applyChartEditOperations,
    applyLocalSort,
    buildChartManifest,
    guardMlPresentation,
} from './utils/chartEditOperations.js?v=1';

// Interface strings come from the locale catalog (static/i18n/i18n.js, loaded first).
const t = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const th = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : String(key));

/**
 * On a right-to-left page the chart canvas lays text out right-to-left too, so
 * a leading minus drifts to the end ("20K-"). Bidi isolates keep numbers intact.
 */
function isolateInRtl(format) {
    const i18n = typeof window !== 'undefined' ? window.I18n : null;
    if (!i18n || !i18n.isRtl || typeof i18n.isolate !== 'function') return format;
    return (value) => i18n.isolate(format(value));
}

/** Catalog lookup that yields null (not the raw key) when a message is missing. */
const tOrNull = (key, args) => {
    const i18n = typeof window !== 'undefined' ? window.I18n : null;
    return i18n && typeof i18n.has === 'function' && i18n.has(key) ? i18n.t(key, args) : null;
};

/**
 * Main chart manager class
 */
export class ChartManager {
    constructor(options = {}) {
        this.workspaceMode = Boolean(options.workspaceMode);
        /** @type {import('./types/chart.types.js').ChartState} */
        this.state = {
            currentView: this.workspaceMode ? 'chart' : 'table',
            currentChartType: 'bar',
            currentConfig: null,
            chartInstance: null,
            isEChartsLoaded: false,
            currentData: null
        };

        this.dataAnalysis = null;
        this.chartContainer = null;
        this.chartToggle = null;
        this.chartTypeSelector = null;
        this.chartOptionsPanel = null;
        this.mapOptionsPanel = null;
        this.chartChat = null;
        this.osmMapRenderer = null;
        this.chartCapabilities = { osm_map: { enabled: false, geocoding_enabled: false } };
        this.llmRecommendedType = null;
        this.analysisMode = false;
        this.interactionEnabled = true;
        this.analysisRerunBusy = false;
        this.currentChartSpec = null;
        this.originalChartSpec = null;
        // Baseline (LLM-generated) config — Reset reverts to this.
        this.originalConfig = null;
        // The current ECharts options object actually rendered.
        this.currentEchartsOptions = null;
        this.chartSession = null;
        this._chartEditRevision = 0;
        // User-chosen colour palette (a preference, so it follows the user across
        // charts). 'jeen' means "no override": theme tokens / server palette.
        this.paletteId = this._readPalettePreference();

        // Per-instance query context + view elements. When set (Chat mode)
        // these override the Ask-mode window globals / fixed element IDs so a
        // single engine can be bound to whichever conversation turn is active.
        this.ctx = null;          // { queryId, question, sql }
        this.tableEl = null;      // element shown/hidden as the "table" view
        this.chartViewEl = null;  // element shown/hidden as the "chart" view

        // When false (Chat mode), skip rendering the segmented ChartToggle into
        // the Ask-only #chart-toggle-container so a Chat engine can't overwrite
        // Ask mode's toggle/listeners. Chat supplies its own per-turn toggle.
        this.manageToggle = !this.workspaceMode;
        // In-flight chart request control so a superseded / disposed engine
        // cannot render a late response into a relocated or returned-home node.
        this._chartAbort = null;
        this._chartEpoch = 0;
        this._renderReady = false;
        this._disposed = false;
        this._themeObserver = null;
        this._onOsmTableFocus = (event) => {
            this.osmMapRenderer?.focusRows(event.detail?.rowIndexes || []);
        };
        document.addEventListener('jeen:osm-table-focus', this._onOsmTableFocus);

        console.log('[ChartManager] Initialized');
    }

    /**
     * Bind this engine to a specific turn's context + view elements.
     * Call before initialize()/handleViewChange() when reusing one engine
     * across conversation turns (Chat mode). Ask mode leaves these null and
     * keeps reading the window globals / fixed IDs.
     *
     * @param {{queryId?:string, question?:string, sql?:string,
     *          tableEl?:HTMLElement, chartViewEl?:HTMLElement}} ctx
     */
    setContext(ctx = {}) {
        this.ctx = { queryId: ctx.queryId, question: ctx.question, sql: ctx.sql };
        this.tableEl = ctx.tableEl || null;
        this.chartViewEl = ctx.chartViewEl || null;
        if (typeof ctx.manageToggle === 'boolean') this.manageToggle = ctx.manageToggle;
    }

    _devTrace(status, payload = {}) {
        if (typeof window !== 'undefined' && typeof window._devPostQueryUpdate === 'function') {
            window._devPostQueryUpdate('chart', { status, ...payload });
        }
    }

    _beginOperation({ invalidate = true } = {}) {
        if (this._chartAbort) {
            try { this._chartAbort.abort(); } catch (_) { /* already settled */ }
        }
        const controller = new AbortController();
        const owner = { epoch: ++this._chartEpoch, controller };
        this._chartAbort = controller;
        if (invalidate) this._invalidateRenderedChart();
        return owner;
    }

    _owns(owner) {
        return Boolean(
            owner
            && !this._disposed
            && owner.epoch === this._chartEpoch
            && this._chartAbort === owner.controller
            && !owner.controller.signal.aborted
        );
    }

    _assertOwner(owner) {
        if (!this._owns(owner)) {
            const error = new Error(t('charts.errors.staleOperation'));
            error.name = 'AbortError';
            throw error;
        }
    }

    _finishOperation(owner) {
        if (this._chartAbort === owner?.controller) this._chartAbort = null;
    }

    _invalidateRenderedChart() {
        this._renderReady = false;
        this.currentEchartsOptions = null;
        this.state.currentConfig = null;
        this._enableChartActions(false);
        this.chartChat?.disable?.();
    }

    _setChartSession(snapshot, defaults = {}) {
        this.chartSession = createChartSession(snapshot, defaults);
        this._chartEditRevision += 1;
        this._syncLegacySessionAliases();
        this._syncPanelFromSession();
    }

    _syncLegacySessionAliases() {
        const baseline = this.chartSession?.baseline || {};
        const working = this.chartSession?.working || {};
        this.originalConfig = baseline.config || null;
        this.originalChartSpec = baseline.spec || null;
        this.currentChartSpec = working.spec || null;
    }

    _syncPanelFromSession() {
        if (!this.chartSession || !this.chartOptionsPanel) return;
        const { working, view } = this.chartSession;
        this.chartOptionsPanel.toggles = { ...view.toggles };
        this.chartOptionsPanel.legendUserSet = Boolean(view.legendUserSet);
        if (working.spec) {
            try { this.chartOptionsPanel.syncFromSpec(working.spec); } catch (_) { /* unmounted */ }
        }
        if (typeof this.chartOptionsPanel.render === 'function') this.chartOptionsPanel.render();
    }

    _semanticConfig() {
        return this.chartSession?.working?.config || null;
    }

    _baselineMapView(config) {
        if (this._isOsmMapOption(config)) return {};
        if (!isMapOption(config)) return {};
        const meta = config?.jeenMap || {};
        const target = config?.geo || config?.series?.find((series) => (
            series?.type === 'map' || series?.coordinateSystem === 'geo'
        )) || {};
        const defaultView = meta.defaultView || Object.fromEntries(
            ['layoutCenter', 'layoutSize', 'aspectScale', 'zoom', 'scaleLimit']
                .filter((key) => target[key] !== undefined)
                .map((key) => [key, target[key]]),
        );
        return {
            echarts: JSON.parse(JSON.stringify(defaultView)),
            controls: {
                labels: Boolean(meta.showLabels),
                roam: target.roam !== false,
                noData: target.itemStyle?.areaColor !== 'rgba(0,0,0,0)',
                palette: meta.palette || 'blue',
            },
        };
    }

    _adoptBaseline(config, spec = null, extras = {}) {
        this._valueFormat = null;
        if (this.chartOptionsPanel && !this._isOsmMapOption(config) && !isMapOption(config)) {
            this.chartOptionsPanel.applyLegendDefault(config);
        }
        const toggles = extras.toggles || this.chartOptionsPanel?.getToggles?.();
        this._setChartSession({
            chart_config: config,
            chart_spec: spec,
            chart_toggles: toggles,
            derived_specs: extras.derivedSpecs || [],
            legend_user_set: extras.legendUserSet ?? this.chartOptionsPanel?.legendUserSet,
            map_view: extras.mapView || this._baselineMapView(config),
        });
    }

    /**
     * Initializes chart feature for given query results
     *
     * @param {import('./types/chart.types.js').QueryResults} results - Query results
     */
    async initialize(results, options = {}) {
        console.log('[ChartManager] Initializing with results');
        const owner = this._beginOperation({ invalidate: true });
        try {
            this.state.currentData = results;
            const restore = options && options.restore;
            this._pendingRestore = restore && restore.chart_config ? restore : null;
            await this._loadChartCapabilities(owner);
            this._assertOwner(owner);

            this.dataAnalysis = analyzeData(results);
            console.log('[ChartManager] Data analysis complete:', this.dataAnalysis);
            this.initializeComponents();
            this._assertOwner(owner);

            if (!this.dataAnalysis.canChart) {
                console.log('[ChartManager] Data cannot be charted:', this.dataAnalysis.reason);
                if (this.chartToggle) this.chartToggle.disableChartButton();
                this.showNotChartableMessage(this.dataAnalysis.reason);
                this._devTrace('skipped', { detail: `Not chartable: ${this.dataAnalysis.reason}` });
                return;
            }
        } finally {
            this._finishOperation(owner);
        }

        if (this.workspaceMode && !this._disposed) {
            await this.handleViewChange('chart');
            console.log('[ChartManager] Initialization complete - workspace chart visible');
        } else if (!this._disposed) {
            console.log('[ChartManager] Initialization complete - defaulting to table view');
        }
    }

    /**
     * Initializes UI components
     */
    initializeComponents() {
        // Chart toggle (Ask mode only). Chat mode passes manageToggle:false and
        // drives view changes through its own per-turn toggle, so we must NOT
        // render into the shared Ask-only #chart-toggle-container here.
        if (this.manageToggle) {
            this.chartToggle = new ChartToggle('chart-toggle-container', (viewMode) => {
                this.handleViewChange(viewMode);
            });
            this.chartToggle.render();
        }

        // Chart type selector. Destroy any prior instance first so its portaled
        // menu + global listeners don't leak when components re-initialize.
        if (this.chartTypeSelector && typeof this.chartTypeSelector.destroy === 'function') {
            this.chartTypeSelector.destroy();
        }
        const allowedTypes = new Set(CHART_TYPE_VALUES);
        if (!this.chartCapabilities?.osm_map?.enabled) allowedTypes.delete('osm_map');
        this.chartTypeSelector = new ChartTypeSelector(
            'chart-type-selector-container',
            (chartType) => this.handleChartTypeChange(chartType),
            { allowedTypes },
        );
        this.chartTypeSelector.render();

        // Column mapping + quick visual toggles
        if (document.getElementById('chart-options-panel-container')) {
            this.chartOptionsPanel = new ChartOptionsPanel('chart-options-panel-container', {
                onColumnsChange: () => {
                    if (this.analysisMode) return;
                    if (this.state.currentView === 'chart') {
                        const selectedType = this.chartTypeSelector.getSelectedType();
                        this.handleChartTypeChange(selectedType);
                    }
                },
                onQuickToggle: () => this._reapplyQuickToggles(),
                onPaletteChange: (id) => this.setPalette(id),
                getThemeColors: () => this._themeTokenColors(),
                initialPalette: this.paletteId,
            });
            if (this.dataAnalysis?.columns) {
                this.chartOptionsPanel.setColumns(
                    this.dataAnalysis.columns.map((col) => ({ name: col.name, type: col.type })),
                    this.dataAnalysis
                );
            }
            this.chartOptionsPanel.render();
        }

        const chartOptionsHost = document.getElementById('chart-options-panel-container');
        if (chartOptionsHost && !document.getElementById('map-options-panel-container')) {
            const mapHost = document.createElement('div');
            mapHost.id = 'map-options-panel-container';
            mapHost.className = 'chart-options-panel-container';
            mapHost.style.display = 'none';
            chartOptionsHost.insertAdjacentElement('afterend', mapHost);
        }
        if (document.getElementById('map-options-panel-container')) {
            this.mapOptionsPanel = new MapOptionsPanel('map-options-panel-container', {
                onMapControl: (action, value) => this._handleMapControl(action, value),
            });
            this.mapOptionsPanel.render();
        }

        // Chart container
        this.chartContainer = new ChartContainer('chart-display-container');
        if (this.workspaceMode && !this._themeObserver) {
            this._themeObserver = new MutationObserver(() => this._refreshWorkspaceTheme());
            this._themeObserver.observe(document.documentElement, {
                attributes: true,
                attributeFilter: ['data-theme'],
            });
        }

        // Chart export toolbar (Save PNG / Copy).
        this._mountChartActionsToolbar();

        // Chart chat panel (under the chart). Mounted once; enabled after
        // the first successful render. Lives only when the chart-chat
        // container exists in the DOM, so omitting it from the page is fine.
        if (document.getElementById('chart-chat-container')) {
            this.chartChat = new ChartChat('chart-chat-container', {
                getChartManifest: () => (
                    this._renderReady && this.chartSession
                        ? buildChartManifest(this.chartSession, {
                            chartKind: this._chartEditKind(),
                            columns: this.dataAnalysis?.columns || [],
                        })
                        : null
                ),
                getChartKind: () => this._chartEditKind(),
                getConnection: () => (typeof getActiveConnection === 'function' ? getActiveConnection() : ''),
                getQueryId: () => (
                    this.ctx?.queryId != null ? this.ctx.queryId : window.currentQueryId
                ),
                getRevision: () => this._chartEditToken(),
                isRevisionCurrent: (revision) => revision === this._chartEditToken(),
                onApply: (operations, revision) => (
                    this.applyEditedOperations(operations, revision)
                ),
                onTiming: (timing) => this._devTrace('edit', timing),
                onReset: () => this.resetChartEdits(),
            });
            this.chartChat.mount();
            this.chartChat.disable();
        }
        this.setAnalysisMode(this.analysisMode);
        this.setAnalysisRerunBusy(this.analysisRerunBusy);

        console.log('[ChartManager] Components initialized');
    }

    _chartEditKind() {
        if (!this.analysisMode) return 'sql';
        const roles = new Set(
            (this.chartSession?.working?.config?.series || [])
                .map((series) => series?.jeenRole)
                .filter(Boolean),
        );
        return roles.has('interval')
            || roles.has('interval_base')
            || roles.has('interval_bound')
            ? 'ml_band'
            : 'ml_basic';
    }

    _chartEditToken() {
        const queryId = this.ctx?.queryId != null
            ? this.ctx.queryId
            : (typeof window !== 'undefined' ? window.currentQueryId : '');
        return `${String(queryId || '')}:${this._chartEditRevision}`;
    }

    async _loadChartCapabilities(owner) {
        try {
            const response = await fetch('/api/chart-capabilities', {
                credentials: 'same-origin',
                signal: owner?.controller?.signal,
            });
            this._assertOwner(owner);
            if (!response.ok) throw new Error(`API returned ${response.status}`);
            const capabilities = await response.json();
            this._assertOwner(owner);
            if (capabilities && typeof capabilities === 'object') {
                this.chartCapabilities = capabilities;
            }
        } catch (error) {
            // Keep maps dark when the capability endpoint is unavailable.
            console.warn('[ChartManager] Chart capabilities unavailable:', error);
        }
    }

    /**
     * Handles view mode change (table/chart)
     *
     * @param {import('./types/chart.types.js').ViewMode} viewMode - New view mode
     */
    async handleViewChange(viewMode) {
        console.log('[ChartManager] View changed to:', viewMode);

        this.state.currentView = viewMode;
        this.saveViewPreference(viewMode);

        const tableContainer = this.tableEl || document.getElementById('results-display');
        const chartViewContainer = this.chartViewEl || document.getElementById('chart-view-container');

        if (viewMode === 'table' && !this.workspaceMode) {
            // Show table, hide chart
            if (tableContainer) tableContainer.style.display = 'block';
            if (chartViewContainer) chartViewContainer.style.display = 'none';

            // Hide chart type selector
            const selectorContainer = document.getElementById('chart-type-selector-container');
            if (selectorContainer) selectorContainer.style.display = 'none';
            if (this.chartOptionsPanel) this.chartOptionsPanel.hide();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
        } else {
            // Workspace mode keeps chart and table visible together.
            if (tableContainer) tableContainer.style.display = this.workspaceMode ? 'block' : 'none';
            if (chartViewContainer) chartViewContainer.style.display = this.workspaceMode ? 'block' : 'flex';

            // Show chart type selector + options panel
            const selectorContainer = document.getElementById('chart-type-selector-container');
            if (selectorContainer) selectorContainer.style.display = 'block';

            // Load ECharts if not loaded
            if (!this.state.isEChartsLoaded) {
                await this.loadECharts();
                if (this._disposed) return;
            }

            // Get selected chart type
            const selectedType = this.chartTypeSelector.getSelectedType();
            this.chartOptionsPanel?.setChartType(selectedType);
            this._syncMapControls(selectedType);

            if (this._pendingRestore) {
                const restore = this._pendingRestore;
                this._pendingRestore = null;
                await this.restoreSavedChart(restore);
                return;
            }

            // Call LLM to generate chart
            await this.generateChartWithLLM(selectedType);
        }
    }

    /**
     * Handles chart type selection change
     *
     * @param {string} chartType - Selected chart type
     */
    async handleChartTypeChange(chartType) {
        if (this.analysisMode) {
            this.showToast(t('charts.options.analysisTypeLocked'), 'info');
            return;
        }
        console.log('[ChartManager] Chart type changed to:', chartType);

        // Clear cache for this chart type to force regeneration
        const cacheKey = this.getLLMCacheKey(chartType);
        sessionStorage.removeItem(cacheKey);
        console.log('[ChartManager] Cleared cache for:', chartType);

        // Show visual feedback
        this.showToast(chartType === 'auto' ? t('charts.generatingAuto') : t('charts.generating', { type: chartType }), 'info');
        this.chartOptionsPanel?.setChartType(chartType);
        this._syncMapControls(chartType);

        // Regenerate chart with new type
        await this.generateChartWithLLM(chartType);
    }

    /**
     * Generates chart using LLM
     *
     * @param {string} chartType - Chart type ("auto" for LLM choice, or specific type)
     */
    async generateChartWithLLM(chartType = 'auto') {
        console.log('[ChartManager] Generating chart with LLM, type:', chartType);
        const owner = this._beginOperation({ invalidate: true });
        this._devTrace('running', {
            metrics: { chart_type: chartType },
            detail: `Preparing ${chartType === 'auto' ? 'auto' : chartType} chart generation.`,
        });

        // Regenerating from scratch — clear any prior chat-edit state so the
        // user starts on a clean baseline.
        if (this.chartChat) this.chartChat.reset();
        this.chartSession = null;
        this._chartEditRevision += 1;
        this.originalConfig = null;
        this.originalChartSpec = null;

        // On a fresh Auto request, drop any stale recommendation so the button
        // shows "Auto" while loading (not the previous turn's "Bar · Auto").
        if (chartType === 'auto' && this.chartTypeSelector) {
            this.chartTypeSelector.setRecommendation(null);
        }

        // Show loading state
        this.chartContainer.showLoading();

        try {
            // Check cache first (include chart type in cache key)
            const cacheKey = this.getLLMCacheKey(chartType);
            const cached = sessionStorage.getItem(cacheKey);
            if (cached) {
                console.log('[ChartManager] Using cached chart config for type:', chartType);
                this._devTrace('running', { metrics: { cache: 'browser hit' }, detail: 'Using cached chart config.' });
                const parsed = JSON.parse(cached);
                // New shape: { chartConfig, recommendedType }. Legacy shape: the
                // raw ECharts config (has `series`). Read both.
                const config = (parsed && parsed.chartConfig) ? parsed.chartConfig : parsed;
                const cachedRec = (parsed && parsed.recommendedType) ? parsed.recommendedType : null;
                if (this._isOsmMapOption(config) && !this.chartCapabilities?.osm_map?.enabled) {
                    this._assertOwner(owner);
                    sessionStorage.removeItem(cacheKey);
                } else {
                    this._assertOwner(owner);
                    this._adoptBaseline(config, parsed?.chartSpec || null);
                    // Restore the "<Type> · Auto" button label from cache so it
                    // survives cache hits (setRecommendation is only called on a
                    // fresh network response otherwise).
                    if (chartType === 'auto' && cachedRec && this.chartTypeSelector) {
                        this.chartTypeSelector.setRecommendation(cachedRec);
                    }
                    const renderStart = performance.now();
                    await this._renderWorking(owner);
                    this._assertOwner(owner);
                    this._devTrace('done', {
                        metrics: { cache: 'browser hit', chart_type: chartType, render_ms: Math.round(performance.now() - renderStart) },
                        detail: 'Rendered cached chart config.',
                    });
                    return;
                }
            }

            console.log('[ChartManager] Calling LLM API for type:', chartType);
            this._devTrace('running', { metrics: { cache: 'browser miss' }, detail: 'Requesting chart spec and config from the server.' });

            // The server builds the chart from the FULL result set (kept in its
            // result cache, keyed by query_id). On the hot path we send only the
            // query_id; on a cache miss the server replies 409 and we re-send the
            // rows as the fallback.
            const connection = (typeof getActiveConnection === 'function') ? getActiveConnection() : '';
            // Only fields the user explicitly picked override the LLM. On Auto
            // this is empty, so the LLM is free to choose x/y/series + combo.
            const overrides = this.chartOptionsPanel ? this.chartOptionsPanel.getOverrides() : {};
            const ctxQueryId  = (this.ctx && this.ctx.queryId != null) ? this.ctx.queryId : window.currentQueryId;
            const ctxQuestion = (this.ctx && this.ctx.question != null) ? this.ctx.question : window.currentQuestion;
            const payload = {
                connection,
                query_id: ctxQueryId || null,
                question: ctxQuestion || null,
                chart_type: chartType,
            };
            if (overrides.xColumn) payload.x_column = overrides.xColumn;
            if (overrides.yColumn) payload.y_column = overrides.yColumn;
            if (overrides.seriesColumn) payload.series_column = overrides.seriesColumn;
            if (overrides.locationColumn) payload.location_column = overrides.locationColumn;
            if (overrides.latitudeColumn) payload.latitude_column = overrides.latitudeColumn;
            if (overrides.longitudeColumn) payload.longitude_column = overrides.longitudeColumn;
            if (overrides.locationParts) payload.location_parts = overrides.locationParts;
            if (overrides.valueColumn) payload.value_column = overrides.valueColumn;
            if (overrides.value2Column !== undefined) payload.value2_column = overrides.value2Column;
            if (overrides.aggregate) payload.aggregate = overrides.aggregate;

            let serverMs = 0;
            let cacheStatus = 'result hit';
            const requestStart = performance.now();
            let data = await this._postChart(payload, owner);
            serverMs += Math.round(performance.now() - requestStart);
            this._assertOwner(owner);
            if (data === '__REDIRECT__') return;
            if (data === '__CACHE_MISS__') {
                console.log('[ChartManager] Cache miss — re-sending full rows');
                cacheStatus = 'result miss';
                this._devTrace('running', { metrics: { cache: 'result miss' }, detail: 'Server result cache missed; re-sending full rows for chart build.' });
                const fallbackStart = performance.now();
                data = await this._postChart({ ...payload, ...this._fallbackRows() }, owner);
                serverMs += Math.round(performance.now() - fallbackStart);
                this._assertOwner(owner);
                if (data === '__REDIRECT__') return;
            }

            if (data.error) {
                throw new Error(data.error);
            }

            console.log('[ChartManager] LLM response received');
            console.log('[ChartManager] Requested type:', chartType, '| Generated type:', data.chart_type);

            // The server returns a complete ECharts config built from the full
            // dataset. Just render it.
            const chartConfig = data.chart_config;
            if (!chartConfig || (!chartConfig.series && !chartConfig.jeenOsmMap)) {
                throw new Error(t('charts.errors.cannotBuild'));
            }

            // Store LLM recommendation (when chart_type is auto)
            if (chartType === 'auto' && data.chart_type) {
                this.llmRecommendedType = data.chart_type;
                this.chartTypeSelector.setRecommendation(data.chart_type);
                console.log('[ChartManager] LLM recommended type:', data.chart_type);
            } else if (chartType !== 'auto') {
                console.log('[ChartManager] User-requested type:', chartType);
            }
            this._syncMapControls(data.chart_type || chartType);
            this.chartOptionsPanel?.setChartType(data.chart_type || chartType);

            // Reflect the LLM's actual column choices in the mapping dropdowns so
            // the panel matches the chart and later tweaks start from there.
            if (this.chartOptionsPanel && data.chart_spec) {
                this.chartOptionsPanel.syncFromSpec(data.chart_spec);
            }
            this._assertOwner(owner);
            this._adoptBaseline(chartConfig, data.chart_spec || null);

            // Display chart prompt in Chart Prompt tab if available
            if (data.prompt || data.system_message) {
                this.displayChartPrompt(data);
            }

            // Cache the built config (keyed on data + type + mapping) plus the
            // type the LLM actually picked, so an Auto cache hit can restore the
            // "<Type> · Auto" button label.
            this._assertOwner(owner);
            sessionStorage.setItem(cacheKey, JSON.stringify({
                chartConfig,
                recommendedType: data.chart_type || null,
                chartSpec: data.chart_spec || null,
            }));

            // Render the chart
            const renderStart = performance.now();
            await this._renderWorking(owner);
            this._assertOwner(owner);
            this._devTrace('done', {
                metrics: {
                    cache: cacheStatus,
                    chart_type: data.chart_type || chartType,
                    server_ms: serverMs,
                    render_ms: Math.round(performance.now() - renderStart),
                },
                detail: `Rendered ${data.chart_type || chartType} chart from server-built config.`,
            });

        } catch (error) {
            // A cancelled request (turn switch / disposed engine) is expected;
            // don't surface it as a chart error on a node we no longer own.
            if (!this._owns(owner) || (error && error.name === 'AbortError')) return;
            console.error('[ChartManager] Failed to generate chart with LLM:', error);
            this._devTrace('error', { detail: error.message || String(error) });
            this.chartContainer.showError(
                'Failed to generate chart: ' + error.message
            );
            this._invalidateRenderedChart();
        } finally {
            this._finishOperation(owner);
        }
    }

    /**
     * POST to /api/generate-chart. Returns parsed JSON, or a sentinel:
     *   '__CACHE_MISS__' when the server has no cached rows (409),
     *   '__REDIRECT__'   when the session expired (401, redirecting to login).
     */
    async _postChart(payload, owner) {
        const signal = owner?.controller?.signal || null;
        // Combine the caller's cancellation signal (Chat turn switch / dispose)
        // with the hard 2-minute timeout when the platform supports it.
        let reqSignal;
        if (signal && typeof AbortSignal.any === 'function') {
            reqSignal = AbortSignal.any([signal, AbortSignal.timeout(120000)]);
        } else {
            reqSignal = signal || AbortSignal.timeout(120000);
        }
        const response = await fetch('/api/generate-chart', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: reqSignal,
        });
        this._assertOwner(owner);
        if (response.status === 409) return '__CACHE_MISS__';
        if (response.status === 401) {
            const next = encodeURIComponent(location.pathname + location.search);
            window.location.replace('/login?next=' + next);
            return '__REDIRECT__';
        }
        if (!response.ok) {
            throw new Error(`API returned ${response.status}: ${response.statusText}`);
        }
        const data = await response.json();
        this._assertOwner(owner);
        return data;
    }

    /**
     * Full-rows fallback payload sent only on a server cache miss. Mirrors the
     * shape the server expects (column metadata + every row, in array form).
     */
    _fallbackRows() {
        const cd = this.state.currentData;
        const colNames = cd.columns || [];
        const rows = cd.data || cd.rows || [];
        const toArray = (row) => Array.isArray(row) ? row : colNames.map(c => row[c]);
        const allData = rows.map(toArray);
        const columns = (this.dataAnalysis && this.dataAnalysis.columns)
            ? this.dataAnalysis.columns.map(c => ({ name: c.name, type: c.type }))
            : colNames.map(n => ({ name: n, type: 'string' }));
        return {
            columns,
            column_names: colNames,
            sample_data: allData.slice(0, 8),
            all_data: allData,
        };
    }

    /** Adopt a legacy config as a new baseline, then use the guarded renderer. */
    async renderChart(echartsConfig) {
        const owner = this._beginOperation({ invalidate: true });
        try {
            this._adoptBaseline(echartsConfig, this.currentChartSpec);
            await this._renderWorking(owner);
            this._assertOwner(owner);
        } finally {
            this._finishOperation(owner);
        }
    }

    /**
     * The only path that draws a chart. It always starts from semantic working
     * state and composes derived data and presentation on fresh copies.
     */
    async _renderWorking(owner, session = this.chartSession) {
        this._assertOwner(owner);
        if (!session?.working?.config) throw new Error(t('charts.errors.sessionNotInitialized'));
        const { working, view } = session;
        let displayConfig = stripDerivedSeries(working.config);
        displayConfig = applyDerivedSeries(
            displayConfig,
            working.derivedSpecs || [],
            this.state.currentData,
        ).config;
        displayConfig = this._withQuickToggles(displayConfig, view.toggles);
        displayConfig = applyLocalSort(displayConfig, view.toggles?.sortDirection);
        displayConfig = this._withMapView(displayConfig, view.mapView);

        if (this._isOsmMapOption(displayConfig)) {
            this._assertOwner(owner);
            this.chartContainer?.dispose();
            this.osmMapRenderer?.dispose();
            this.osmMapRenderer = new OsmMapRenderer('chart-display-container', {
                onSelect: (detail) => document.dispatchEvent(new CustomEvent(
                    'jeen:osm-map-select', { detail }
                )),
            });
            this._assertOwner(owner);
            this.osmMapRenderer.render(displayConfig);
            this.osmMapRenderer.restoreViewState?.(view.mapView?.osm || null);
            this._assertOwner(owner);
            document.documentElement.dataset.jeenOsmMapActive = 'true';
            document.dispatchEvent(new CustomEvent('jeen:osm-map-ready'));
            const chartConfig = { type: 'osm_map', options: displayConfig, isEnhanced: true };
            this.state.currentConfig = chartConfig;
            this.currentEchartsOptions = displayConfig;
            this._renderReady = true;
            this._syncMapControls('osm_map');
            this._renderMapFeedback(displayConfig);
            this._syncChartChatEnabled();
            this._enableChartActions(false);
            console.log('[ChartManager] OpenStreetMap chart rendered successfully');
            return displayConfig;
        }

        delete document.documentElement.dataset.jeenOsmMapActive;
        this.osmMapRenderer?.dispose();
        this.osmMapRenderer = null;
        if (!this.state.isEChartsLoaded) {
            await this.loadECharts();
            this._assertOwner(owner);
        }
        displayConfig = this._withWorkspaceTheme(displayConfig);
        displayConfig = applyStyleOverrides(displayConfig, working.styleOverrides);
        displayConfig = guardMlPresentation(displayConfig, this._chartEditKind());
        displayConfig = this._finalizeForRender(displayConfig);
        await ensureMapsForOption(displayConfig);
        this._assertOwner(owner);

        const chartConfig = {
            type: this._optionChartType(displayConfig),
            options: displayConfig,
            isEnhanced: true,
        };
        await this.chartContainer.init();
        this._assertOwner(owner);
        this.chartContainer.render(chartConfig);
        this._assertOwner(owner);
        this.state.currentConfig = chartConfig;
        this.currentEchartsOptions = displayConfig;
        this._renderReady = true;
        this._syncMapControls(chartConfig.type);
        if (chartConfig.type === 'map' && this.mapOptionsPanel) {
            this.mapOptionsPanel.syncFromConfig(displayConfig);
        }
        this._renderMapFeedback(displayConfig);
        this._syncChartChatEnabled();
        this._enableChartActions(this.interactionEnabled);
        console.log('[ChartManager] Chart rendered successfully');
        return displayConfig;
    }

    // ── Colour palette ────────────────────────────────────────────────────

    _readPalettePreference() {
        try {
            const prefs = window.JeenPreferences?.getAll?.();
            const id = prefs && prefs.chartPalette;
            return isKnownPalette(id) ? id : DEFAULT_PALETTE_ID;
        } catch (_) {
            return DEFAULT_PALETTE_ID;
        }
    }

    /** The workspace theme tokens (rose / plum / teal / err), i.e. the "jeen" palette. */
    _themeTokenColors() {
        const style = getComputedStyle(document.documentElement);
        const token = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
        return [
            token('--rose', '#8878c4'),
            token('--plum', '#7a5ea8'),
            token('--teal', '#4bb9c9'),
            token('--err', '#d4574a'),
        ];
    }

    /** Colours to draw with, or null when the server/theme default should stand. */
    _paletteColors() {
        const palette = getPalette(this.paletteId);
        if (palette.colors) return palette.colors;
        return this.workspaceMode ? this._themeTokenColors() : null;
    }

    _withPalette(option) {
        if (!option || this._isOsmMapOption(option) || isMapOption(option)) return option;
        const palette = getPalette(this.paletteId);
        if (!palette.colors) return option; // default: leave theme/server colours alone
        return applyPalette(option, palette.colors);
    }

    /**
     * Switch the palette, remember it as a preference and recolour the current
     * chart in place (no server round-trip, chat edits are preserved).
     */
    async setPalette(id) {
        if (!isKnownPalette(id) || id === this.paletteId) return;
        this.paletteId = id;
        try { window.JeenPreferences?.setChartPalette?.(id); } catch (_) { /* preference is best-effort */ }
        this.chartOptionsPanel?.setPalette(id);
        if (!this.chartSession || this._isOsmMapOption(this._semanticConfig()) || isMapOption(this._semanticConfig())) return;
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner);
            this._assertOwner(owner);
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                this.chartContainer?.showError('Failed to apply palette: ' + error.message);
            }
        } finally {
            this._finishOperation(owner);
        }
    }

    /**
     * Last step before an ECharts render: translate the ML series names and
     * restore the function formatters that the JSON deep-clones in the
     * palette / theme / toggle steps drop.
     */
    _finalizeForRender(displayConfig) {
        if (!displayConfig || this._isOsmMapOption(displayConfig)) return displayConfig;
        return this._applyValueFormatting(localizeSeriesLabels(displayConfig, tOrNull));
    }

    _withWorkspaceTheme(option) {
        if (!this.workspaceMode || !option || typeof option !== 'object') return option;
        let themed;
        try { themed = JSON.parse(JSON.stringify(option)); } catch (_) { themed = { ...option }; }
        const style = getComputedStyle(document.documentElement);
        const token = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
        // The chosen palette (or the theme tokens for the default) drives both
        // the option palette and the per-series colours below.
        const colors = this._paletteColors() || this._themeTokenColors();
        const muted = token('--muted', '#6b6b73');
        const border = token('--border', '#e9e9ec');
        themed.backgroundColor = 'transparent';
        themed.textStyle = {
            ...(themed.textStyle || {}),
            color: muted,
            fontFamily: 'Outfit, system-ui, sans-serif',
            fontWeight: 300,
        };
        ['xAxis', 'yAxis'].forEach((key) => {
            const axes = Array.isArray(themed[key]) ? themed[key] : themed[key] ? [themed[key]] : [];
            axes.forEach((axis) => {
                axis.axisLabel = {
                    ...(axis.axisLabel || {}),
                    color: muted,
                    fontFamily: key === 'xAxis' ? 'Outfit, system-ui, sans-serif' : 'Geist Mono, monospace',
                    fontSize: key === 'xAxis' ? 11 : 10.5,
                };
                axis.axisLine = { ...(axis.axisLine || {}), lineStyle: { ...(axis.axisLine?.lineStyle || {}), color: border } };
                axis.axisTick = { ...(axis.axisTick || {}), lineStyle: { ...(axis.axisTick?.lineStyle || {}), color: border } };
                axis.splitLine = { ...(axis.splitLine || {}), lineStyle: { ...(axis.splitLine?.lineStyle || {}), color: border } };
            });
        });
        // Colours: pin one per single-colour series, let pie-like series take
        // slices from option.color, leave map/heatmap/gauge alone.
        const painted = applyPalette(themed, colors);
        // ML band charts carry semantic roles; those override the sequential
        // palette so the queried series is always ink and only model output is
        // lavender (see docs/ml_skills_handoff/README.md §2).
        return this._withRoleColors(painted, token);
    }

    /**
     * Map `series[i].jeenRole` to design tokens at render time. Applied after
     * the palette so it wins, and re-applied by the theme observer on a
     * light/dark switch.
     */
    _withRoleColors(option, token) {
        const series = Array.isArray(option?.series) ? option.series : null;
        if (!series || !series.some((s) => s && s.jeenRole)) return option;
        // Tokens only — no literal fallbacks. If a token is missing the server's
        // role default stays in place rather than a hardcoded colour winning.
        const ink = token('--text', '');
        const model = token('--rose', '');
        const band = token('--insight-bg', '');
        const flagged = token('--err', '');
        const bandFill = (() => {
            // --insight-bg is a faint background wash; the band is the same hue
            // at a legible alpha, derived from the token, never a literal.
            const match = /rgba?\(([^)]+)\)/.exec(band);
            if (!match) return band;
            const parts = match[1].split(',').map((p) => p.trim());
            return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, 0.16)`;
        })();
        const paint = (s, colour) => {
            if (!colour) return;
            s.itemStyle = { ...(s.itemStyle || {}), color: colour };
            if (s.type === 'line') s.lineStyle = { ...(s.lineStyle || {}), color: colour };
        };
        series.forEach((s) => {
            if (!s || !s.jeenRole) return;
            switch (s.jeenRole) {
                case 'actual': paint(s, ink); break;
                case 'expected':
                case 'forecast': paint(s, model); break;
                case 'flagged': paint(s, flagged); break;
                case 'interval':
                    if (bandFill) {
                        s.areaStyle = { ...(s.areaStyle || {}), color: bandFill, opacity: 1 };
                        s.itemStyle = { ...(s.itemStyle || {}), color: bandFill };
                    }
                    s.lineStyle = { ...(s.lineStyle || {}), opacity: 0 };
                    break;
                case 'interval_base':
                case 'interval_bound':
                    s.lineStyle = { ...(s.lineStyle || {}), opacity: 0 };
                    s.itemStyle = { ...(s.itemStyle || {}), opacity: 0 };
                    break;
                default: break;
            }
        });
        const muted = token('--muted', '');
        if (muted && option.legend && typeof option.legend === 'object') {
            option.legend.textStyle = { ...(option.legend.textStyle || {}), color: muted };
        }
        return option;
    }

    async _refreshWorkspaceTheme() {
        if (!this.workspaceMode || !this.chartContainer || !this.chartSession) return;
        if (this._isOsmMapOption(this._semanticConfig())) return;
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner);
            this._assertOwner(owner);
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                console.error('[ChartManager] Failed to refresh workspace theme:', error);
            }
        } finally {
            this._finishOperation(owner);
        }
    }

    getSaveState() {
        // Semantic session state is safe to capture while a presentation render
        // is in flight. This preserves an accepted edit when the user switches
        // turns before ECharts finishes drawing it.
        if (!this.chartSession) return null;
        const snapshot = this.chartSession.snapshot();
        const osmView = this.osmMapRenderer?.getViewState?.();
        if (osmView) {
            const view = this.chartSession.view;
            this.chartSession.replaceView({ ...view, mapView: { ...view.mapView, osm: osmView } });
            return this.chartSession.snapshot();
        }
        return snapshot;
    }

    setAnalysisMode(on) {
        this.analysisMode = Boolean(on);
        // ML semantics still lock type/column/sort controls, but the compact
        // chat is chart-only. It edits the existing ECharts option through
        // /api/edit-chart and never re-runs SQL or the analysis pipeline.
        this.chartChat?.setAnalysisMode(false);
        this.chartTypeSelector?.setDisabled(
            this.analysisMode,
            this.analysisMode ? t('charts.options.analysisTypeLocked') : ''
        );
        this.chartOptionsPanel?.setAnalysisMode(this.analysisMode);
    }

    setAnalysisRerunBusy(busy) {
        this.analysisRerunBusy = Boolean(busy);
        this.chartChat?.setExternalBusy(this.analysisRerunBusy);
    }

    setInteractionEnabled(enabled) {
        this.interactionEnabled = Boolean(enabled);
        if (!this.interactionEnabled) this._invalidateRenderedChart();
        else if (this._renderReady) {
            this._enableChartActions(!this._isOsmMapOption(this.currentEchartsOptions));
        }
        this._syncChartChatEnabled();
    }

    _syncChartChatEnabled() {
        if (!this.chartChat) return;
        if (this.interactionEnabled && this._renderReady
            && !this._isOsmMapOption(this.currentEchartsOptions)) this.chartChat.enable();
        else this.chartChat.disable();
    }

    setCollapsed(collapsed) {
        if (collapsed) {
            this.chartTypeSelector?.close?.();
            this.chartOptionsPanel?.closeDisclosures?.();
            return;
        }
        requestAnimationFrame(() => {
            this.chartContainer?.resize?.();
            window.dispatchEvent(new Event('resize'));
        });
    }

    async restoreSavedChart(snapshotOrConfig, chartSpec = null) {
        const snapshot = snapshotOrConfig?.chart_config
            ? snapshotOrConfig
            : { chart_config: snapshotOrConfig, chart_spec: chartSpec };
        if (!snapshot.chart_config) return;
        const owner = this._beginOperation({ invalidate: true });
        try {
            this._valueFormat = null;
            this._setChartSession(snapshot);
            const working = this.chartSession.working;
            const restoredSpec = working.spec;
            if (restoredSpec?.chart_type) {
                try {
                    this.chartTypeSelector?.setRecommendation?.(restoredSpec.chart_type);
                    this.chartOptionsPanel?.setChartType?.(restoredSpec.chart_type);
                    this._syncMapControls?.(restoredSpec.chart_type);
                } catch (_) { /* selector state is cosmetic */ }
            }
            await this._renderWorking(owner);
            this._assertOwner(owner);
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                this.chartContainer?.showError('Failed to restore chart: ' + error.message);
                this._invalidateRenderedChart();
            }
            throw error;
        } finally {
            this._finishOperation(owner);
        }
        if (this._disposed) return;
        const restoredSpec = this.chartSession?.working?.spec;
        if (restoredSpec && restoredSpec.chart_type) {
            try {
                this.chartTypeSelector?.setRecommendation?.(restoredSpec.chart_type);
            } catch (_) { /* selector state is cosmetic */ }
        }
        if (this.chartToggle && typeof this.chartToggle.showChartView === 'function') {
            this.chartToggle.showChartView();
        }
        const table = document.getElementById('results-display');
        const chart = document.getElementById('chart-view-container');
        if (table && !this.workspaceMode) table.style.display = 'none';
        if (chart) chart.style.display = 'block';
    }

    /**
     * Applies a v2 chart-editor operation list to a cloned canonical session.
     * Validation is transactional: unsupported operations never publish state.
     */
    async applyEditedOperations(operations, expectedRevision) {
        if (!this.chartSession) return;
        if (expectedRevision !== this._chartEditToken()) {
            const stale = new Error(t('charts.errors.staleOperation'));
            stale.name = 'AbortError';
            throw stale;
        }
        const previousSnapshot = this.chartSession.snapshot();
        const bindingOperations = operations.filter((operation) => operation?.op === 'set_binding');
        const localOperations = operations.filter((operation) => operation?.op !== 'set_binding');
        let sourceSession = this.chartSession;
        let rebuildTelemetry = { request_count: 0, rows_uploaded: 0, server_timing: '' };
        if (bindingOperations.length) {
            const rebuilt = await this._rebuildBindingOperations(
                bindingOperations,
                expectedRevision,
            );
            sourceSession = rebuilt.session;
            rebuildTelemetry = rebuilt.telemetry;
        }
        const candidate = localOperations.length
            ? applyChartEditOperations(sourceSession, localOperations, {
                chartKind: this._chartEditKind(),
            })
            : createChartSession(sourceSession.snapshot());

        // Publish the semantic candidate before rendering so a newer local
        // operation (for example a quick toggle) composes from this edit. Only
        // the current owner may roll it back on an actual render failure.
        this.chartSession = candidate;
        this._chartEditRevision += 1;
        this._syncLegacySessionAliases();
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner, candidate);
            this._assertOwner(owner);
            this.chartSession = candidate;
            this._syncLegacySessionAliases();
            this._syncPanelFromSession();
            return rebuildTelemetry;
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                console.error('[ChartManager] Failed to apply edited config:', error);
                const rollback = createChartSession(previousSnapshot);
                try {
                    await this._renderWorking(owner, rollback);
                    this._assertOwner(owner);
                    this.chartSession = rollback;
                    this._chartEditRevision += 1;
                    this._syncLegacySessionAliases();
                    this._syncPanelFromSession();
                } catch (_) {
                    this._invalidateRenderedChart();
                }
            }
            throw error;
        } finally {
            this._finishOperation(owner);
        }
    }

    async _rebuildBindingOperations(operations, expectedRevision) {
        if (this._chartEditKind() !== 'sql') {
            throw new Error(t('charts.chat.outOfScope'));
        }
        const connection = typeof getActiveConnection === 'function'
            ? getActiveConnection()
            : '';
        const queryId = this.ctx?.queryId != null ? this.ctx.queryId : window.currentQueryId;
        const chartSpec = this.chartSession?.working?.spec;
        if (!connection || !queryId || !chartSpec) {
            throw new Error(t('charts.chat.outOfScope'));
        }
        const baseBody = {
            connection,
            query_id: String(queryId),
            chart_kind: 'sql',
            chart_spec: chartSpec,
            operations,
        };
        let requestCount = 1;
        let rowsUploaded = 0;
        let response = await fetch('/api/edit-chart/rebuild', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(baseBody),
        });
        if (response.status === 409) {
            if (expectedRevision !== this._chartEditToken()) {
                const stale = new Error(t('charts.errors.staleOperation'));
                stale.name = 'AbortError';
                throw stale;
            }
            const fallback = this._bindingFallbackData(operations, chartSpec);
            requestCount += 1;
            rowsUploaded = fallback.all_data.length;
            response = await fetch('/api/edit-chart/rebuild', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...baseBody, ...fallback }),
            });
        }
        const serverTiming = response.headers?.get?.('Server-Timing') || '';
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.chart_config || !data.chart_spec) {
            const detail = data?.detail || data?.error || `HTTP ${response.status}`;
            throw new Error(String(detail));
        }
        if (expectedRevision !== this._chartEditToken()) {
            const stale = new Error(t('charts.errors.staleOperation'));
            stale.name = 'AbortError';
            throw stale;
        }
        const rebuilt = createChartSession(this.chartSession.snapshot());
        rebuilt.replaceWorking({
            ...rebuilt.working,
            config: data.chart_config,
            spec: data.chart_spec,
        });
        return {
            session: rebuilt,
            telemetry: {
                request_count: requestCount,
                rows_uploaded: rowsUploaded,
                server_timing: serverTiming,
            },
        };
    }

    _bindingFallbackData(operations, chartSpec) {
        const data = this.state.currentData || {};
        const columns = (data.columns || []).map((column) => (
            typeof column === 'string' ? column : column?.name
        ));
        const needed = new Set();
        const add = (value) => {
            if (typeof value === 'string' && value) needed.add(value);
            else if (Array.isArray(value)) value.forEach(add);
        };
        add(chartSpec?.x);
        add(chartSpec?.y);
        add(chartSpec?.series);
        operations.forEach((operation) => {
            add(operation?.x);
            add(operation?.y);
            add(operation?.series);
        });
        const indexes = columns
            .map((column, index) => ({ column, index }))
            .filter(({ column }) => needed.has(column));
        if (!indexes.length) throw new Error(t('charts.chat.outOfScope'));
        const rows = data.rows || data.data || [];
        return {
            column_names: indexes.map(({ column }) => column),
            all_data: rows.map((row) => indexes.map(({ column, index }) => (
                Array.isArray(row) ? row[index] : row?.[column]
            ))),
        };
    }

    /**
     * Reverts the chart to the original LLM-generated config.
     * The chat transcript is cleared by ChartChat itself.
     */
    async resetChartEdits() {
        if (!this.chartSession) return;
        const previousSnapshot = this.chartSession.snapshot();
        const candidate = createChartSession(previousSnapshot);
        candidate.reset();
        this.chartSession = candidate;
        this._chartEditRevision += 1;
        this._syncLegacySessionAliases();
        this._syncPanelFromSession();
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner, candidate);
            this._assertOwner(owner);
            this.chartSession = candidate;
            this._syncLegacySessionAliases();
            this._syncPanelFromSession();
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                const rollback = createChartSession(previousSnapshot);
                try {
                    await this._renderWorking(owner, rollback);
                    this._assertOwner(owner);
                    this.chartSession = rollback;
                    this._chartEditRevision += 1;
                    this._syncLegacySessionAliases();
                    this._syncPanelFromSession();
                } catch (_) {
                    this._invalidateRenderedChart();
                }
            }
            throw error;
        } finally {
            this._finishOperation(owner);
        }
    }


    // ─────────────────────────────────────────────────────────────────────
    // Chart export toolbar (Save PNG / Copy)
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Builds the chart-actions toolbar (Save PNG + Copy buttons) into
     * #chart-actions-toolbar if present. Idempotent.
     */
    _mountChartActionsToolbar() {
        const host = document.getElementById('chart-actions-toolbar');
        if (!host) return;
        // The toolbar is built once, but a new ChartManager is created for every
        // result. The buttons therefore act on whichever manager owns the chart
        // now (`host._chartManager`), not on the instance that built them —
        // otherwise Save PNG reports "No chart to export yet" for every chart
        // after the first.
        host._chartManager = this;
        if (host.dataset.mounted === '1') {
            this._chartSavePngBtn = host.querySelector('#chart-save-png-btn');
            this._chartCopyPngBtn = host.querySelector('#chart-copy-png-btn');
            return;
        }

        host.classList.add('chart-actions-toolbar');
        host.innerHTML = '';

        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.className = 'chart-action-btn';
        saveBtn.id = 'chart-save-png-btn';
        saveBtn.title = t('charts.actions.savePngTitle');
        saveBtn.disabled = true;
        saveBtn.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>${th('charts.actions.savePng')}</span>
        `;
        saveBtn.addEventListener('click', () => (host._chartManager || this)._handleSavePng());

        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'chart-action-btn';
        copyBtn.id = 'chart-copy-png-btn';
        copyBtn.title = t('charts.actions.copyTitle');
        copyBtn.disabled = true;
        copyBtn.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            <span>${th('common.copy')}</span>
        `;
        copyBtn.addEventListener('click', () => (host._chartManager || this)._handleCopyPng(copyBtn));

        host.appendChild(saveBtn);
        host.appendChild(copyBtn);
        host.dataset.mounted = '1';
        host.style.display = 'flex';

        this._chartSavePngBtn = saveBtn;
        this._chartCopyPngBtn = copyBtn;
    }

    _enableChartActions(enabled) {
        if (this._chartSavePngBtn) this._chartSavePngBtn.disabled = !enabled;
        if (this._chartCopyPngBtn) this._chartCopyPngBtn.disabled = !enabled;
    }

    /**
     * Returns a PNG data URL of the currently rendered chart, or null if no
     * chart is ready. Used by Chat mode's single per-turn Download button.
     *
     * @returns {string|null}
     */
    getChartDataURL() {
        if (!this._renderReady || !this.chartContainer || !this.chartContainer.hasChart()) return null;
        try {
            return this.chartContainer.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
        } catch (_) {
            return null;
        }
    }

    _handleSavePng() {
        if (!this._renderReady || !this.chartContainer || !this.chartContainer.hasChart()) {
            this.showToast(t('charts.actions.noChartExport'), 'error');
            return;
        }
        const dataUrl = this.chartContainer.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
        if (!dataUrl) {
            this.showToast(t('charts.actions.imageFailed'), 'error');
            return;
        }
        const link = document.createElement('a');
        link.href = dataUrl;
        link.download = 'jeen_insights_chart_' + new Date().getTime() + '.png';
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        this.showToast(t('charts.actions.saved'), 'success');
    }

    async _handleCopyPng(btn) {
        if (!this._renderReady || !this.chartContainer || !this.chartContainer.hasChart()) {
            this.showToast(t('charts.actions.noChartCopy'), 'error');
            return;
        }
        // Clipboard image API requires a secure context (HTTPS or localhost).
        const canCopyImage = !!(navigator.clipboard && window.ClipboardItem);
        if (!canCopyImage) {
            this.showToast(t('charts.actions.clipboardUnsupported'), 'error');
            return;
        }
        const originalLabel = btn ? btn.querySelector('span')?.textContent : null;
        const epoch = this._chartEpoch;
        try {
            if (btn) {
                btn.disabled = true;
                const span = btn.querySelector('span');
                if (span) span.textContent = t('charts.actions.copying');
            }
            const blob = await this.chartContainer.getBlob({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
            if (this._disposed || epoch !== this._chartEpoch || !this._renderReady) return;
            if (!blob) throw new Error(t('charts.errors.noImageData'));
            await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
            if (this._disposed || epoch !== this._chartEpoch || !this._renderReady) return;
            this.showToast(t('charts.actions.copied'), 'success');
        } catch (e) {
            console.error('[ChartManager] Copy chart failed:', e);
            this.showToast(t('charts.actions.copyFailed', { detail: e && e.message ? e.message : t('common.unknownError') }), 'error');
        } finally {
            if (btn) {
                btn.disabled = this._disposed || epoch !== this._chartEpoch || !this._renderReady;
                const span = btn.querySelector('span');
                if (span && originalLabel) span.textContent = originalLabel;
            }
        }
    }

    /**
     * Loads ECharts library
     */
    async loadECharts() {
        if (this.state.isEChartsLoaded) return;

        console.log('[ChartManager] Loading ECharts library');

        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = '/static/vendor/echarts/echarts.min.js';
            script.async = true;
            script.onload = () => {
                this.state.isEChartsLoaded = true;
                console.log('[ChartManager] ECharts loaded successfully');
                resolve();
            };
            script.onerror = () => {
                console.error('[ChartManager] Failed to load ECharts');
                reject(new Error(t('charts.errors.libraryLoadFailed')));
            };
            document.head.appendChild(script);
        });
    }

    /**
     * Shows a message when data can't be charted
     *
     * @param {string} reason - Reason why data can't be charted
     */
    showNotChartableMessage(reason) {
        const container = document.getElementById('chart-display-container');
        if (container) {
            container.innerHTML = `
                <div class="chart-not-available">
                    <p>📊 ${th('charts.toggle.unavailable')}</p>
                    <p class="reason" dir="auto">${this.escapeHtml(String(reason ?? ''))}</p>
                </div>
            `;
        }
    }

    /**
     * Shows a toast message
     *
     * @param {string} message - Toast message
     * @param {string} type - Toast type (success/error/info)
     */
    showToast(message, type = 'info') {
        // Simple toast implementation
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        document.body.appendChild(toast);

        setTimeout(() => {
            toast.classList.add('show');
        }, 100);

        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    /**
     * Display chart prompt in the Chart Prompt tab with collapsible sections
     */
    displayChartPrompt(chartData) {
        const promptContent = document.getElementById('chart-prompt-content');
        if (!promptContent) {
            console.warn('[ChartManager] Chart prompt content element not found');
            return;
        }

        if (!chartData.prompt) {
            promptContent.innerHTML = `<p style="color: #999;">${th('insights.noPrompt')}</p>`;
            return;
        }

        // Parse the prompt into sections
        const sections = this.parseChartPrompt(chartData.prompt);

        let html = '<div class="structured-prompt">';

        // Section 1: Chart Type Override (if present)
        if (sections.chartTypeOverride) {
            html += this.createPromptSection('chart-type-override', 'Chart Type Selection',
                `<pre class="prompt-text">${this.escapeHtml(sections.chartTypeOverride)}</pre>`, true);
        }

        // Section 2: Column Information
        if (sections.columnInfo) {
            html += this.createPromptSection('chart-columns', 'Column Information',
                `<pre class="prompt-text">${this.escapeHtml(sections.columnInfo)}</pre>`, false);
        }

        // Section 3: Data Sample
        if (sections.dataSample) {
            html += this.createPromptSection('chart-data', 'Data Sample',
                `<pre class="prompt-text">${this.escapeHtml(sections.dataSample)}</pre>`, false);
        }

        // Section 4: Instructions
        if (sections.instructions) {
            html += this.createPromptSection('chart-instructions', 'Chart Instructions',
                `<pre class="prompt-text">${this.escapeHtml(sections.instructions)}</pre>`, false);
        }

        // Section 5: Full Prompt
        html += this.createPromptSection('chart-full', 'Full Prompt Text',
            `<pre class="prompt-text">${this.escapeHtml(chartData.prompt)}</pre>`, false);

        html += '</div>';
        promptContent.innerHTML = html;

        console.log('[ChartManager] Chart prompt displayed in structured format');
    }

    /**
     * Parse chart prompt into sections
     */
    parseChartPrompt(prompt) {
        const sections = {
            chartTypeOverride: '',
            columnInfo: '',
            dataSample: '',
            instructions: ''
        };

        // Extract chart type override section
        const chartTypeMatch = prompt.match(/##\s*CHART TYPE OVERRIDE([\s\S]*?)(?=Column Names:|$)/i);
        if (chartTypeMatch) {
            sections.chartTypeOverride = chartTypeMatch[0].trim();
        }

        // Extract column information
        const columnMatch = prompt.match(/Column Names:([\s\S]*?)(?=Data \(first|Instructions:|$)/i);
        if (columnMatch) {
            sections.columnInfo = 'Column Names:' + columnMatch[1].trim();
        }

        // Extract data sample
        const dataMatch = prompt.match(/Data \(first[^:]*\):([\s\S]*?)(?=Instructions:|$)/i);
        if (dataMatch) {
            sections.dataSample = dataMatch[0].trim();
        }

        // Extract instructions
        const instructionsMatch = prompt.match(/Instructions:([\s\S]*?)$/i);
        if (instructionsMatch) {
            sections.instructions = instructionsMatch[0].trim();
        }

        // Fallback: if no sections found, put everything in instructions
        if (!sections.columnInfo && !sections.dataSample && !sections.instructions) {
            sections.instructions = prompt;
        }

        return sections;
    }

    /**
     * Create a collapsible prompt section
     */
    createPromptSection(id, title, content, expanded = false) {
        const expandedClass = expanded ? 'expanded' : '';
        const displayStyle = expanded ? 'block' : 'none';
        const arrow = expanded ? '▼' : '▶';

        return `
            <div class="prompt-section ${expandedClass}">
                <div class="prompt-section-header" onclick="toggleChartPromptSection('${id}')">
                    <span class="section-arrow" id="arrow-${id}">${arrow}</span>
                    <span class="section-title">${title}</span>
                </div>
                <div class="prompt-section-content" id="content-${id}" style="display: ${displayStyle};">
                    ${content}
                </div>
            </div>
        `;
    }

    /**
     * Toggle a prompt section
     */
    togglePromptSection(sectionId) {
        const content = document.getElementById(`content-${sectionId}`);
        const arrow = document.getElementById(`arrow-${sectionId}`);

        if (content && arrow) {
            if (content.style.display === 'none') {
                content.style.display = 'block';
                arrow.textContent = '▼';
            } else {
                content.style.display = 'none';
                arrow.textContent = '▶';
            }
        }
    }

    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Cache and preferences methods

    loadViewPreference() {
        return localStorage.getItem('chartViewPreference') || 'table';
    }

    saveViewPreference(viewMode) {
        localStorage.setItem('chartViewPreference', viewMode);
    }

    saveChartTypePreference(chartType) {
        localStorage.setItem('chartTypePreference', chartType);
    }

    loadCachedConfig(chartType) {
        const cacheKey = this.getCacheKey(chartType);
        const cached = sessionStorage.getItem(cacheKey);
        if (cached) {
            console.log('[ChartManager] Cache hit for', chartType);
            try {
                const parsed = JSON.parse(cached);
                return parsed;
            } catch (e) {
                console.error('[ChartManager] Failed to parse cached config');
                return null;
            }
        }
        console.log('[ChartManager] Cache miss for', chartType);
        return null;
    }

    cacheEnhancedConfig(chartType, config) {
        const cacheKey = this.getCacheKey(chartType);
        sessionStorage.setItem(cacheKey, JSON.stringify(config));
        console.log('[ChartManager] Cached config for', chartType);
    }

    getCacheKey(chartType) {
        // Use SQL as part of cache key (hash it for shorter key). Prefer the
        // per-instance context SQL (Chat) over the Ask-mode global.
        const sqlSeed = (this.ctx && this.ctx.sql) ? this.ctx.sql : window.currentSql;
        const sqlHash = this.simpleHash(sqlSeed || JSON.stringify(this.state.currentData));
        return `chart_${sqlHash}_${chartType}`;
    }

    getLLMCacheKey(chartType = 'auto') {
        const dataHash = this.simpleHash(JSON.stringify(this.state.currentData));
        // Key only on user-chosen overrides (stable across the post-generation
        // dropdown sync), so an Auto chart isn't re-fetched after we mirror the
        // LLM's columns into the panel.
        const o = this.chartOptionsPanel ? this.chartOptionsPanel.getOverrides() : {};
        const mapKey = [
            o.xColumn || '', o.yColumn || '', o.seriesColumn || '',
            o.locationColumn || '', o.latitudeColumn || '', o.longitudeColumn || '',
            JSON.stringify(o.locationParts || {}),
            o.valueColumn || '', o.value2Column || '', o.aggregate || '',
        ].join('|');
        // Scope to the turn (Chat) so two differently-worded turns with the same
        // data don't reuse each other's chart. Ask mode (no ctx) is unscoped.
        const ctxSeed = this.ctx ? String(this.ctx.queryId || this.ctx.question || '') : '';
        const ctxKey = ctxSeed ? '_' + this.simpleHash(ctxSeed) : '';
        // Bump when chart payload semantics change so a prior browser session
        // cannot reuse an OSM result generated before a geocoding fix.
        return `chart_llm_v2_${dataHash}_${chartType}_${mapKey}${ctxKey}`;
    }

    _withQuickToggles(config, toggles = null) {
        if (!config) return config;
        if (this._isOsmMapOption(config)) return config;
        let out = config;
        if (!isMapOption(config)) {
            out = applyQuickOptions(config, toggles || this.chartSession?.view?.toggles || {});
        }
        // Apply value formatting last so it covers initial render, chat edits,
        // quick toggles, and reset uniformly.
        out = this._applyValueFormatting(out);
        // Runs on the final config for every render path (initial, chat edit,
        // quick toggle, reset) so the header/legend never overprint.
        if (!isMapOption(out)) {
            out = this._suppressCanvasTitle(out);
        }
        // The user's palette rides the same pipeline so it survives every path.
        return this._withPalette(out);
    }

    /**
     * The result title is already rendered in the card header above the chart,
     * so ECharts' own centered `title.text` only collides with the legend that
     * sits at the same y-position. Hide the in-canvas title, and give a
     * top-anchored legend enough vertical room that it never overprints the
     * plot area.
     *
     * Shallow-clones only the properties it changes so the Reset baseline
     * (`originalConfig`) is never mutated, even on the map/no-panel code path
     * where `out` may still reference the incoming config.
     */
    _suppressCanvasTitle(config) {
        if (!config || typeof config !== 'object') return config;
        const out = { ...config };

        if (out.title !== undefined && out.title !== null) {
            out.title = Array.isArray(out.title)
                ? out.title.map((t) => ({ ...(t || {}), show: false }))
                : { ...out.title, show: false };
        }

        // Only reflow a top-anchored legend on cartesian charts (those with a
        // `grid`). Pie/gauge/radar/vertical-side legends are left untouched —
        // hiding the in-canvas title above already clears their overlap, and
        // nudging their legend could move it out of place.
        const legend = out.legend && !Array.isArray(out.legend) ? out.legend : null;
        const legendShown = legend && legend.show !== false;
        const legendAtBottom = legend && (legend.bottom !== undefined || legend.top === 'bottom');
        const legendVertical = legend && (legend.orient === 'vertical' || legend.left === 'left' || legend.left === 'right' || legend.right !== undefined);
        const hasGrid = out.grid && !Array.isArray(out.grid);
        if (legendShown && !legendAtBottom && !legendVertical && hasGrid) {
            out.legend = { ...legend };
            // Anchor the legend where the removed title used to sit…
            if (out.legend.top === undefined) out.legend.top = 8;
            // …and push the plot area down so bars/lines clear it.
            const t = out.grid.top;
            const num = typeof t === 'number' ? t : Number(t);
            const isPct = typeof t === 'string' && t.trim().endsWith('%');
            if (isPct || !Number.isFinite(num) || num < 48) {
                out.grid = { ...out.grid, top: 48 };
            }
        }
        return out;
    }

    _syncMapControls(chartType) {
        if (chartType === 'map') {
            if (this.chartOptionsPanel) this.chartOptionsPanel.hide();
            if (this.mapOptionsPanel) this.mapOptionsPanel.show();
        } else if (chartType === 'osm_map') {
            if (this.chartOptionsPanel) this.chartOptionsPanel.show();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
        } else {
            if (this.chartOptionsPanel) this.chartOptionsPanel.show();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
            this._renderMapFeedback(null);
        }
    }

    _withMapView(option, mapView = {}) {
        if (!isMapOption(option)) return option;
        let out;
        try { out = JSON.parse(JSON.stringify(option)); } catch (_) { out = { ...option }; }
        const view = mapView?.echarts || {};
        const controls = mapView?.controls || {};
        const apply = (target) => {
            if (!target || (target.type !== 'map' && target.coordinateSystem !== 'geo' && !target.map)) return;
            for (const key of ['layoutCenter', 'layoutSize', 'aspectScale', 'zoom', 'scaleLimit']) {
                if (view[key] !== undefined) target[key] = Array.isArray(view[key]) ? view[key].slice() : view[key];
            }
            if (typeof controls.roam === 'boolean') target.roam = controls.roam;
            if (typeof controls.labels === 'boolean') {
                target.label = { ...(target.label || {}), show: controls.labels };
            }
            if (typeof controls.noData === 'boolean') {
                target.itemStyle = {
                    ...(target.itemStyle || {}),
                    areaColor: controls.noData ? '#eef2f7' : 'rgba(0,0,0,0)',
                };
            }
        };
        if (Array.isArray(out.series)) out.series.forEach(apply);
        if (out.geo && typeof out.geo === 'object' && !Array.isArray(out.geo)) apply(out.geo);
        if (out.jeenMap) out.jeenMap = { ...out.jeenMap, showLabels: Boolean(controls.labels) };
        return controls.palette ? this._withMapPalette(out, controls.palette) : out;
    }

    async _handleMapControl(action, value) {
        if (!this.chartSession || !isMapOption(this._semanticConfig())) return;
        const previousSnapshot = this.chartSession.snapshot();
        const candidate = createChartSession(previousSnapshot);
        const baselineView = candidate.baseline.mapView || {};
        let mapView = candidate.view.mapView || {};
        let echartsView = { ...(mapView.echarts || {}) };
        let controls = { ...(mapView.controls || {}) };
        const renderedTarget = (
            this.currentEchartsOptions?.geo
            || this.currentEchartsOptions?.series?.find((series) => series?.type === 'map')
            || {}
        );
        if (action === 'zoom-in') {
            echartsView.zoom = Math.min((echartsView.zoom || renderedTarget.zoom || 1) * 1.2, 12);
        } else if (action === 'zoom-out') {
            echartsView.zoom = Math.max((echartsView.zoom || renderedTarget.zoom || 1) / 1.2, 0.7);
        } else if (action === 'fit') {
            echartsView = { ...(this._semanticConfig()?.jeenMap?.defaultView || {}) };
        } else if (action === 'reset') {
            mapView = JSON.parse(JSON.stringify(baselineView));
            echartsView = { ...(mapView.echarts || {}) };
            controls = { ...(mapView.controls || {}) };
        } else if (action === 'labels' || action === 'roam' || action === 'noData') {
            controls[action] = Boolean(value);
        } else if (action === 'palette') {
            controls.palette = value;
        }
        candidate.replaceView({ ...candidate.view, mapView: { ...mapView, echarts: echartsView, controls } });
        this.chartSession = candidate;
        this._chartEditRevision += 1;
        this._syncLegacySessionAliases();
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner, candidate);
            this._assertOwner(owner);
            this.chartSession = candidate;
            this._syncLegacySessionAliases();
        } catch (error) {
            if (!this._owns(owner) || error?.name === 'AbortError') return;
            if (this._owns(owner) && error?.name !== 'AbortError') {
                const rollback = createChartSession(previousSnapshot);
                try {
                    await this._renderWorking(owner, rollback);
                    this._assertOwner(owner);
                    this.chartSession = rollback;
                    this._chartEditRevision += 1;
                    this._syncLegacySessionAliases();
                } catch (_) {
                    this._invalidateRenderedChart();
                }
            }
            console.error('[ChartManager] Failed to apply map control:', error);
        } finally {
            this._finishOperation(owner);
        }
    }

    _withMapPalette(options, name) {
        const palette = MAP_PALETTES[name] || MAP_PALETTES.blue;
        if (options.visualMap && typeof options.visualMap === 'object' && !Array.isArray(options.visualMap)) {
            options.visualMap.inRange = { ...(options.visualMap.inRange || {}), color: palette.slice() };
        }
        if (Array.isArray(options.series)) {
            options.series.forEach((series) => {
                if (!series || typeof series !== 'object') return;
                if (series.type === 'map') {
                    series.emphasis = series.emphasis && typeof series.emphasis === 'object' ? { ...series.emphasis } : {};
                    series.emphasis.itemStyle = { ...(series.emphasis.itemStyle || {}), areaColor: palette[palette.length - 2] };
                } else if (series.coordinateSystem === 'geo') {
                    series.itemStyle = { ...(series.itemStyle || {}), color: palette[palette.length - 2] };
                }
            });
        }
        if (options.jeenMap) options.jeenMap.palette = name;
        return options;
    }

    _renderMapFeedback(config) {
        let host = document.getElementById('map-feedback-container');
        if (!host) {
            const chart = document.getElementById('chart-display-container');
            if (!chart || !chart.parentNode) return;
            host = document.createElement('div');
            host.id = 'map-feedback-container';
            host.className = 'map-feedback';
            chart.insertAdjacentElement('afterend', host);
        }
        const meta = config?.jeenMap || config?.jeenOsmMap;
        if (!meta || meta.showUnmatched === false || !meta.unmatchedCount) {
            host.style.display = 'none';
            host.textContent = '';
            return;
        }
        const shown = Array.isArray(meta.unmatched) ? meta.unmatched.join(', ') : '';
        const suffix = meta.unmatchedCount > (meta.unmatched?.length || 0) ? '...' : '';
        const statuses = Object.entries(meta.unmatchedByStatus || {})
            .filter(([, count]) => Number(count) > 0)
            .map(([status, count]) => `${count} ${status}`)
            .join(', ');
        host.textContent = `${meta.unmatchedCount} location${meta.unmatchedCount === 1 ? '' : 's'} not matched${statuses ? ` (${statuses})` : ''}: ${shown}${suffix}`;
        host.style.display = 'block';
    }

    _optionChartType(option) {
        if (this._isOsmMapOption(option)) return 'osm_map';
        if (isMapOption(option)) return 'map';
        return option?.series?.[0]?.type || 'bar';
    }

    _isOsmMapOption(option) {
        return !!(option && typeof option === 'object' && option.jeenOsmMap);
    }

    /**
     * Attach the compact/currency/percent value formatter to value axes, data
     * labels, gauges and the tooltip, driven by the server's `jeenFormat` hint.
     * The hint is remembered so chat-edited configs (which may drop it) keep
     * consistent formatting.
     *
     * Formatters are functions, and several later steps (palette, workspace
     * theme, quick toggles) deep-clone the option through JSON, which silently
     * drops them. So this is idempotent, leaves the plain-data `jeenFormat`
     * hints in place (ECharts ignores unknown keys, like `jeenRole`), and is
     * re-run as the last step before every render — see `_finalizeForRender`.
     * Mutates and returns the option object.
     */
    _applyValueFormatting(options) {
        if (!options || typeof options !== 'object') return options;

        if (options.jeenFormat) {
            this._valueFormat = {
                kind: options.jeenFormat.kind || 'number',
                compact: options.jeenFormat.compact !== false,
                symbol: options.jeenFormat.symbol || '',
                scale: options.jeenFormat.scale,
            };
        }
        const primaryMeta = this._valueFormat || { kind: 'number', compact: true, symbol: '' };
        const primaryFmt = isolateInRtl(makeValueFormatter(primaryMeta));

        // Pull the numeric value out of a point regardless of shape: plain
        // number, {value}, or [x, y] / time-axis pairs.
        const pickValue = (p) => {
            const raw = p && p.value;
            if (Array.isArray(raw)) return raw[raw.length - 1];
            return (raw !== undefined && raw !== null) ? raw : p;
        };

        // Axes may carry their OWN format (combo dual-axis: $ left, % right).
        const applyAxis = (axis) => {
            if (!axis) return;
            if (Array.isArray(axis)) { axis.forEach(applyAxis); return; }
            if (axis.type === 'value') {
                const f = axis.jeenFormat ? isolateInRtl(makeValueFormatter(axis.jeenFormat)) : primaryFmt;
                axis.axisLabel = { ...(axis.axisLabel || {}), formatter: f };
            }
        };
        applyAxis(options.xAxis);
        applyAxis(options.yAxis);

        // Per-series formatter (so a combo's % line formats as % while its bars
        // format as currency). Data labels reuse the same formatter and get
        // overlap protection so dense charts stay readable.
        const series = Array.isArray(options.series) ? options.series : [];
        const seriesFmts = [];
        let perSeriesDiff = false;
        let hasGauge = false;
        series.forEach((s, i) => {
            if (!s || typeof s !== 'object') { seriesFmts[i] = primaryFmt; return; }
            let f = primaryFmt;
            let meta = primaryMeta;
            if (s.jeenFormat) {
                meta = s.jeenFormat;
                f = isolateInRtl(makeValueFormatter(meta));
                perSeriesDiff = true;
            }
            seriesFmts[i] = f;
            if ((s.type === 'bar' || s.type === 'line' || s.type === 'scatter')
                && !(s.label && typeof s.label.formatter === 'string')) {
                // On-chart labels use one shared unit per series (all-K or all
                // full numbers with thousands separators), unlike the axis,
                // which may abbreviate freely.
                const labelFmt = isolateInRtl(makeLabelFormatter(meta, collectNumericValues(s.data)));
                s.label = (s.label && typeof s.label === 'object') ? s.label : {};
                s.label.formatter = (p) => labelFmt(pickValue(p));
                if (s.label.fontSize == null) s.label.fontSize = 11;
                // Drop labels that would collide instead of overprinting them.
                if (!s.labelLayout) s.labelLayout = { hideOverlap: true };
            } else if (s.type === 'gauge') {
                // The server emits "{value}" templates for gauges; the big
                // KPI number and the dial ticks read as 4.1M / 500K, not
                // 4147192.9 / 500000.
                hasGauge = true;
                s.detail = { ...(s.detail || {}), formatter: (v) => f(v) };
                s.axisLabel = { ...(s.axisLabel || {}), formatter: (v) => f(v) };
            }
        });

        // Tooltip: a single valueFormatter can't express per-series formats or
        // unwrap [x, y] pairs, so use a formatter fn when either is in play.
        const hasGeoVisual = isMapOption(options);
        if (hasGeoVisual && options.visualMap && typeof options.visualMap === 'object' && !Array.isArray(options.visualMap)) {
            options.visualMap.formatter = (value) => primaryFmt(value);
        }

        const tip = options.tooltip;
        if (hasGauge && tip && !Array.isArray(tip)) {
            // Gauge tooltips arrive as a "{b}: {c}" template, which would print
            // the raw number; replace it with the same formatter as the dial.
            tip.formatter = (params) => {
                const p = Array.isArray(params) ? params[0] : params;
                const f = seriesFmts[p?.seriesIndex ?? 0] || primaryFmt;
                const name = (p && (p.name || p.seriesName)) || '';
                return `${p?.marker || ''} ${name}: ${f(pickValue(p))}`.trim();
            };
            delete tip.valueFormatter;
        } else if (tip && !Array.isArray(tip) && typeof tip.formatter !== 'string') {
            const isAxis = tip.trigger === 'axis';
            const hasPairs = series.some((s) => s && Array.isArray(s.data)
                && s.data.length && Array.isArray(s.data[0]));
            if (hasGeoVisual) {
                tip.formatter = (params) => {
                    const p = Array.isArray(params) ? params[0] : params;
                    const raw = p && p.value;
                    const value = Array.isArray(raw) ? raw[2] : raw;
                    const name = (p && (p.name || p.seriesName)) || '';
                    const seriesName = p && p.seriesName ? p.seriesName : 'Value';
                    const formatted = (value === undefined || value === null || value === '-')
                        ? 'No data'
                        : primaryFmt(value);
                    return `${name}<br/>${p?.marker || ''} ${seriesName}: ${formatted}`;
                };
                delete tip.valueFormatter;
            } else if (isAxis && (perSeriesDiff || hasPairs)) {
                const headFmt = isolateInRtl((value) => String(value));
                tip.formatter = (params) => {
                    const arr = Array.isArray(params) ? params : [params];
                    const head = arr.length ? headFmt(arr[0].axisValueLabel ?? arr[0].name ?? '') : '';
                    const rows = arr.map((p) => {
                        const f = seriesFmts[p.seriesIndex] || primaryFmt;
                        return `${p.marker || ''} ${p.seriesName}: ${f(pickValue(p))}`;
                    });
                    return [head, ...rows].join('<br/>');
                };
                delete tip.valueFormatter;
            } else {
                tip.valueFormatter = primaryFmt;
            }
        }
        return options;
    }

    async _reapplyQuickToggles() {
        if (!this.chartSession || !this.chartContainer) return;
        const previousSnapshot = this.chartSession.snapshot();
        const candidate = createChartSession(previousSnapshot);
        candidate.replaceView({
            ...candidate.view,
            toggles: this.chartOptionsPanel?.getToggles?.() || candidate.view.toggles,
            legendUserSet: this.chartOptionsPanel?.legendUserSet ?? candidate.view.legendUserSet,
        });
        this.chartSession = candidate;
        this._chartEditRevision += 1;
        this._syncLegacySessionAliases();
        const owner = this._beginOperation({ invalidate: true });
        try {
            await this._renderWorking(owner, candidate);
            this._assertOwner(owner);
            this.chartSession = candidate;
            this._syncLegacySessionAliases();
        } catch (error) {
            if (this._owns(owner) && error?.name !== 'AbortError') {
                const rollback = createChartSession(previousSnapshot);
                try {
                    await this._renderWorking(owner, rollback);
                    this._assertOwner(owner);
                    this.chartSession = rollback;
                    this._chartEditRevision += 1;
                    this._syncLegacySessionAliases();
                    this._syncPanelFromSession();
                } catch (_) {
                    this.chartContainer?.showError('Failed to apply chart options: ' + error.message);
                    this._invalidateRenderedChart();
                }
            }
        } finally {
            this._finishOperation(owner);
        }
    }

    simpleHash(str) {
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            const char = str.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash;
        }
        return Math.abs(hash).toString(36);
    }

    /**
     * Cleanup method
     */
    dispose() {
        this._disposed = true;
        this._chartEpoch += 1;
        this._invalidateRenderedChart();
        this.chartChat?.disable?.();
        this.chartChat?.reset?.();
        // Cancel any in-flight chart request so its late response can't render
        // into a node this engine no longer owns.
        if (this._chartAbort) {
            try { this._chartAbort.abort(); } catch (_) { /* noop */ }
            this._chartAbort = null;
        }
        if (this.chartContainer) {
            this.chartContainer.dispose();
        }
        if (this.osmMapRenderer) {
            this.osmMapRenderer.dispose();
            this.osmMapRenderer = null;
        }
        // Remove the chart-type dropdown's portaled menu + global listeners.
        if (this.chartTypeSelector && typeof this.chartTypeSelector.destroy === 'function') {
            this.chartTypeSelector.destroy();
        }
        if (this._themeObserver) {
            this._themeObserver.disconnect();
            this._themeObserver = null;
        }
        document.removeEventListener('jeen:osm-table-focus', this._onOsmTableFocus);
        const toolbar = document.getElementById('chart-actions-toolbar');
        if (toolbar?._chartManager === this) toolbar._chartManager = null;
        delete document.documentElement.dataset.jeenOsmMapActive;
        console.log('[ChartManager] Disposed');
    }
}

// Expose toggle function globally for onclick handlers
window.toggleChartPromptSection = function(sectionId) {
    const content = document.getElementById(`content-${sectionId}`);
    const arrow = document.getElementById(`arrow-${sectionId}`);

    if (content && arrow) {
        if (content.style.display === 'none') {
            content.style.display = 'block';
            arrow.textContent = '▼';
        } else {
            content.style.display = 'none';
            arrow.textContent = '▶';
        }
    }
};
