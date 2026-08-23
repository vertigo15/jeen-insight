/**
 * Chart Manager
 * Main orchestrator for chart feature
 *
 * @module chartManager
 */

/// <reference path="./types/chart.types.js" />

import { analyzeData } from './utils/dataAnalyzer.js';
import { makeValueFormatter } from './utils/valueFormat.js?v=73';
import { ChartContainer } from './components/ChartContainer.js?v=78';
import { ChartToggle } from './components/ChartToggle.js';
import { ChartTypeSelector } from './components/ChartTypeSelector.js?v=78';
import { ChartOptionsPanel } from './components/ChartOptionsPanel.js?v=71';
import { MapOptionsPanel, MAP_PALETTES } from './components/MapOptionsPanel.js?v=1';
import { ChartChat } from './components/ChartChat.js?v=73';
import { applyDerivedSeries, stripDerivedSeries } from './utils/chartOperators.js';
import { ensureMapsForOption, isMapOption } from './utils/mapAssets.js?v=81';
import { OsmMapRenderer } from './utils/osmMapRenderer.js?v=1';
import { CHART_TYPE_VALUES } from './chartTypes.js?v=79';

/**
 * Main chart manager class
 */
export class ChartManager {
    constructor() {
        /** @type {import('./types/chart.types.js').ChartState} */
        this.state = {
            currentView: 'table',
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
        this.currentChartSpec = null;
        // Baseline (LLM-generated) config — Reset reverts to this.
        this.originalConfig = null;
        // The current ECharts options object actually rendered.
        this.currentEchartsOptions = null;

        // Per-instance query context + view elements. When set (Chat mode)
        // these override the Ask-mode window globals / fixed element IDs so a
        // single engine can be bound to whichever conversation turn is active.
        this.ctx = null;          // { queryId, question, sql }
        this.tableEl = null;      // element shown/hidden as the "table" view
        this.chartViewEl = null;  // element shown/hidden as the "chart" view

        // When false (Chat mode), skip rendering the segmented ChartToggle into
        // the Ask-only #chart-toggle-container so a Chat engine can't overwrite
        // Ask mode's toggle/listeners. Chat supplies its own per-turn toggle.
        this.manageToggle = true;
        // In-flight chart request control so a superseded / disposed engine
        // cannot render a late response into a relocated or returned-home node.
        this._chartAbort = null;
        this._disposed = false;

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

    /**
     * Initializes chart feature for given query results
     *
     * @param {import('./types/chart.types.js').QueryResults} results - Query results
     */
    async initialize(results) {
        console.log('[ChartManager] Initializing with results');

        this.state.currentData = results;
        await this._loadChartCapabilities();

        // Analyze data (for type detection only)
        this.dataAnalysis = analyzeData(results);
        console.log('[ChartManager] Data analysis complete:', this.dataAnalysis);

        // Initialize UI components
        this.initializeComponents();

        // Check if data can be charted
        if (!this.dataAnalysis.canChart) {
            console.log('[ChartManager] Data cannot be charted:', this.dataAnalysis.reason);
            if (this.chartToggle) this.chartToggle.disableChartButton();
            this.showNotChartableMessage(this.dataAnalysis.reason);
            this._devTrace('skipped', { detail: `Not chartable: ${this.dataAnalysis.reason}` });
            return;
        }

        // Always default to table view (no auto-switch to chart)
        console.log('[ChartManager] Initialization complete - defaulting to table view');
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
                    if (this.state.currentView === 'chart') {
                        const selectedType = this.chartTypeSelector.getSelectedType();
                        this.handleChartTypeChange(selectedType);
                    }
                },
                onQuickToggle: () => this._reapplyQuickToggles(),
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

        // Chart export toolbar (Save PNG / Copy).
        this._mountChartActionsToolbar();

        // Chart chat panel (under the chart). Mounted once; enabled after
        // the first successful render. Lives only when the chart-chat
        // container exists in the DOM, so omitting it from the page is fine.
        if (document.getElementById('chart-chat-container')) {
            this.chartChat = new ChartChat('chart-chat-container', {
                getCurrentConfig: () => this.currentEchartsOptions,
                getCurrentResults: () => this.state.currentData,
                getConnection: () => (typeof getActiveConnection === 'function' ? getActiveConnection() : ''),
                onApply: (newConfig, derivedSpecs) => this.applyEditedConfig(newConfig, derivedSpecs),
                onReset: () => this.resetChartEdits(),
            });
            this.chartChat.mount();
            this.chartChat.disable();
        }

        console.log('[ChartManager] Components initialized');
    }

    async _loadChartCapabilities() {
        try {
            const response = await fetch('/api/chart-capabilities', { credentials: 'same-origin' });
            if (!response.ok) throw new Error(`API returned ${response.status}`);
            const capabilities = await response.json();
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

        if (viewMode === 'table') {
            // Show table, hide chart
            if (tableContainer) tableContainer.style.display = 'block';
            if (chartViewContainer) chartViewContainer.style.display = 'none';

            // Hide chart type selector
            const selectorContainer = document.getElementById('chart-type-selector-container');
            if (selectorContainer) selectorContainer.style.display = 'none';
            if (this.chartOptionsPanel) this.chartOptionsPanel.hide();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
        } else {
            // Show chart, hide table
            if (tableContainer) tableContainer.style.display = 'none';
            if (chartViewContainer) chartViewContainer.style.display = 'flex';

            // Show chart type selector + options panel
            const selectorContainer = document.getElementById('chart-type-selector-container');
            if (selectorContainer) selectorContainer.style.display = 'block';

            // Load ECharts if not loaded
            if (!this.state.isEChartsLoaded) {
                await this.loadECharts();
            }

            // Get selected chart type
            const selectedType = this.chartTypeSelector.getSelectedType();
            this._syncMapControls(selectedType);

            // Call LLM to generate chart
            this.generateChartWithLLM(selectedType);
        }
    }

    /**
     * Handles chart type selection change
     *
     * @param {string} chartType - Selected chart type
     */
    async handleChartTypeChange(chartType) {
        console.log('[ChartManager] Chart type changed to:', chartType);

        // Clear cache for this chart type to force regeneration
        const cacheKey = this.getLLMCacheKey(chartType);
        sessionStorage.removeItem(cacheKey);
        console.log('[ChartManager] Cleared cache for:', chartType);

        // Show visual feedback
        this.showToast(`Generating ${chartType === 'auto' ? 'LLM-recommended' : chartType} chart...`, 'info');
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
        this._devTrace('running', {
            metrics: { chart_type: chartType },
            detail: `Preparing ${chartType === 'auto' ? 'auto' : chartType} chart generation.`,
        });

        // Regenerating from scratch — clear any prior chat-edit state so the
        // user starts on a clean baseline.
        if (this.chartChat) this.chartChat.reset();
        this.originalConfig = null;

        // On a fresh Auto request, drop any stale recommendation so the button
        // shows "Auto" while loading (not the previous turn's "Bar · Auto").
        if (chartType === 'auto' && this.chartTypeSelector) {
            this.chartTypeSelector.setRecommendation(null);
        }

        // Show loading state
        this.chartContainer.showLoading();
        if (this.chartChat) this.chartChat.disable();

        // Per-activation abort handle so dispose() (turn switch / leaving Chat)
        // cancels the request and no stale response renders into a moved node.
        this._chartAbort = new AbortController();
        const _abortSignal = this._chartAbort.signal;

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
                    sessionStorage.removeItem(cacheKey);
                } else {
                    if (this._disposed) return;
                    // Restore the "<Type> · Auto" button label from cache so it
                    // survives cache hits (setRecommendation is only called on a
                    // fresh network response otherwise).
                    if (chartType === 'auto' && cachedRec && this.chartTypeSelector) {
                        this.chartTypeSelector.setRecommendation(cachedRec);
                    }
                    const renderStart = performance.now();
                    await this.renderChart(config);
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
            const overrides = (this.chartOptionsPanel && chartType !== 'map' && chartType !== 'osm_map')
                ? this.chartOptionsPanel.getOverrides()
                : {};
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

            let serverMs = 0;
            let cacheStatus = 'result hit';
            const requestStart = performance.now();
            let data = await this._postChart(payload, _abortSignal);
            serverMs += Math.round(performance.now() - requestStart);
            if (this._disposed || _abortSignal.aborted) return;
            if (data === '__REDIRECT__') return;
            if (data === '__CACHE_MISS__') {
                console.log('[ChartManager] Cache miss — re-sending full rows');
                cacheStatus = 'result miss';
                this._devTrace('running', { metrics: { cache: 'result miss' }, detail: 'Server result cache missed; re-sending full rows for chart build.' });
                const fallbackStart = performance.now();
                data = await this._postChart({ ...payload, ...this._fallbackRows() }, _abortSignal);
                serverMs += Math.round(performance.now() - fallbackStart);
                if (this._disposed || _abortSignal.aborted) return;
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
                throw new Error('Could not build a chart for this data');
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

            // Reflect the LLM's actual column choices in the mapping dropdowns so
            // the panel matches the chart and later tweaks start from there.
            if (this.chartOptionsPanel && data.chart_spec) {
                this.chartOptionsPanel.syncFromSpec(data.chart_spec);
            }
            this.currentChartSpec = data.chart_spec || null;

            // Display chart prompt in Chart Prompt tab if available
            if (data.prompt || data.system_message) {
                this.displayChartPrompt(data);
            }

            // Cache the built config (keyed on data + type + mapping) plus the
            // type the LLM actually picked, so an Auto cache hit can restore the
            // "<Type> · Auto" button label.
            sessionStorage.setItem(cacheKey, JSON.stringify({
                chartConfig,
                recommendedType: data.chart_type || null,
            }));

            if (this._disposed || _abortSignal.aborted) return;

            // Render the chart
            const renderStart = performance.now();
            await this.renderChart(chartConfig);
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
            if (this._disposed || (error && error.name === 'AbortError')) return;
            console.error('[ChartManager] Failed to generate chart with LLM:', error);
            this._devTrace('error', { detail: error.message || String(error) });
            this.chartContainer.showError(
                'Failed to generate chart: ' + error.message
            );
        } finally {
            this._chartAbort = null;
        }
    }

    /**
     * POST to /api/generate-chart. Returns parsed JSON, or a sentinel:
     *   '__CACHE_MISS__' when the server has no cached rows (409),
     *   '__REDIRECT__'   when the session expired (401, redirecting to login).
     */
    async _postChart(payload, signal = null) {
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
        if (response.status === 409) return '__CACHE_MISS__';
        if (response.status === 401) {
            const next = encodeURIComponent(location.pathname + location.search);
            window.location.replace('/login?next=' + next);
            return '__REDIRECT__';
        }
        if (!response.ok) {
            throw new Error(`API returned ${response.status}: ${response.statusText}`);
        }
        return await response.json();
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

    /**
     * Renders chart with given config (the original LLM-generated config).
     * Snapshots the config as the Reset baseline.
     */
    async renderChart(echartsConfig) {
        console.log('[ChartManager] Rendering chart with LLM config');

        // Capture the unmodified baseline so Reset can always restore it.
        // Deep-clone so later edits to currentEchartsOptions don't mutate it.
        try {
            this.originalConfig = JSON.parse(JSON.stringify(echartsConfig));
        } catch (_) {
            this.originalConfig = echartsConfig;
        }

        const displayConfig = this._withQuickToggles(echartsConfig);
        if (this._isOsmMapOption(displayConfig)) {
            try {
                this.chartContainer?.dispose();
                this.osmMapRenderer?.dispose();
                this.osmMapRenderer = new OsmMapRenderer('chart-display-container');
                this.osmMapRenderer.render(displayConfig);
                const chartConfig = {
                    type: 'osm_map',
                    options: displayConfig,
                    isEnhanced: true,
                };
                this.state.currentConfig = chartConfig;
                this.currentEchartsOptions = displayConfig;
                this._syncMapControls('osm_map');
                this._renderMapFeedback(displayConfig);
                if (this.chartChat) this.chartChat.disable();
                this._enableChartActions(false);
                console.log('[ChartManager] OpenStreetMap chart rendered successfully');
            } catch (error) {
                console.error('[ChartManager] Failed to render OpenStreetMap chart:', error);
                this.chartContainer?.showError('Failed to render map: ' + error.message);
            }
            return;
        }
        this.osmMapRenderer?.dispose();
        if (!this.state.isEChartsLoaded) {
            await this.loadECharts();
        }
        await ensureMapsForOption(displayConfig);

        const chartConfig = {
            type: this._optionChartType(displayConfig),
            options: displayConfig,
            isEnhanced: true
        };

        this.state.currentConfig = chartConfig;
        this.currentEchartsOptions = displayConfig;

        try {
            await this.chartContainer.init();
            this.chartContainer.render(chartConfig);
            this._syncMapControls(chartConfig.type);
            if (chartConfig.type === 'map' && this.mapOptionsPanel) {
                this.mapOptionsPanel.syncFromConfig(displayConfig);
            }
            this._renderMapFeedback(displayConfig);
            if (this.chartChat) this.chartChat.enable();
            this._enableChartActions(true);
            console.log('[ChartManager] Chart rendered successfully');
        } catch (error) {
            console.error('[ChartManager] Failed to render chart:', error);
            this.chartContainer.showError('Failed to render chart: ' + error.message);
            this._enableChartActions(false);
        }
    }

    getSaveState() {
        return {
            chart_spec: this.currentChartSpec,
            chart_config: this.currentEchartsOptions || this.originalConfig || null,
        };
    }

    async restoreSavedChart(echartsConfig, chartSpec = null) {
        if (!echartsConfig) return;
        this.currentChartSpec = chartSpec || null;
        if (this.chartOptionsPanel && chartSpec) {
            try { this.chartOptionsPanel.syncFromSpec(chartSpec); } catch (_) {}
        }
        await this.renderChart(echartsConfig);
        if (this.chartToggle && typeof this.chartToggle.showChartView === 'function') {
            this.chartToggle.showChartView();
        }
        const table = document.getElementById('results-display');
        const chart = document.getElementById('chart-view-container');
        if (table) table.style.display = 'none';
        if (chart) chart.style.display = 'block';
    }

    /**
     * Applies a chat-edited config to the chart.
     *
     * - Strips any previously-applied derived overlays so they don't stack.
     * - Computes the requested derived overlays locally from currentData.
     * - Re-renders via ChartContainer; falls back to the previous config on
     *   failure so the user never ends up with a broken chart.
     *
     * @param {object} newConfig
     * @param {Array} derivedSpecs
     */
    applyEditedConfig(newConfig, derivedSpecs) {
        if (!newConfig || typeof newConfig !== 'object') return;

        const previous = this.currentEchartsOptions;
        // Strip any prior __derived series the LLM may have echoed back, then
        // re-apply only the freshly-described overlays.
        const cleaned = stripDerivedSeries(newConfig);
        const { config: withDerived } = applyDerivedSeries(
            cleaned,
            derivedSpecs || [],
            this.state.currentData
        );
        // Mirror the edit's visual state (e.g. label.show=true from "add data
        // labels") into the quick toggles BEFORE re-applying them — otherwise a
        // default-off toggle would immediately strip the change the user asked for.
        if (this.chartOptionsPanel) {
            this.chartOptionsPanel.syncTogglesFromConfig(withDerived);
        }
        const displayConfig = this._withQuickToggles(withDerived);

        const chartConfig = {
            type: this._optionChartType(displayConfig),
            options: displayConfig,
            isEnhanced: true,
        };

        try {
            this.chartContainer.render(chartConfig);
            this.currentEchartsOptions = displayConfig;
            this.state.currentConfig = chartConfig;
        } catch (error) {
            console.error('[ChartManager] Failed to apply edited config:', error);
            // Roll back to the last known-good config so the user keeps a
            // working chart.
            if (previous) {
                try {
                    this.chartContainer.render({
                        type: this._optionChartType(previous),
                        options: previous,
                        isEnhanced: true,
                    });
                } catch (_) { /* swallow — we already logged the original error */ }
            }
            throw error;
        }
    }

    /**
     * Reverts the chart to the original LLM-generated config.
     * The chat transcript is cleared by ChartChat itself.
     */
    resetChartEdits() {
        if (!this.originalConfig) return;
        let baseline;
        try {
            baseline = JSON.parse(JSON.stringify(this.originalConfig));
        } catch (_) {
            baseline = this.originalConfig;
        }
        const chartConfig = {
            type: this._optionChartType(baseline),
            options: this._withQuickToggles(baseline),
            isEnhanced: true,
        };
        try {
            this.chartContainer.render(chartConfig);
            this.currentEchartsOptions = chartConfig.options;
            this.state.currentConfig = chartConfig;
        } catch (error) {
            console.error('[ChartManager] Failed to reset chart:', error);
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
        if (!host || host.dataset.mounted === '1') return;

        host.classList.add('chart-actions-toolbar');
        host.innerHTML = '';

        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.className = 'chart-action-btn';
        saveBtn.id = 'chart-save-png-btn';
        saveBtn.title = 'Download chart as PNG';
        saveBtn.disabled = true;
        saveBtn.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>Save PNG</span>
        `;
        saveBtn.addEventListener('click', () => this._handleSavePng());

        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'chart-action-btn';
        copyBtn.id = 'chart-copy-png-btn';
        copyBtn.title = 'Copy chart image to clipboard';
        copyBtn.disabled = true;
        copyBtn.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            <span>Copy</span>
        `;
        copyBtn.addEventListener('click', () => this._handleCopyPng(copyBtn));

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
        if (!this.chartContainer || !this.chartContainer.hasChart()) return null;
        try {
            return this.chartContainer.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
        } catch (_) {
            return null;
        }
    }

    _handleSavePng() {
        if (!this.chartContainer || !this.chartContainer.hasChart()) {
            this.showToast('No chart to export yet.', 'error');
            return;
        }
        const dataUrl = this.chartContainer.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
        if (!dataUrl) {
            this.showToast('Could not generate chart image.', 'error');
            return;
        }
        const link = document.createElement('a');
        link.href = dataUrl;
        link.download = 'jeen_insights_chart_' + new Date().getTime() + '.png';
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        this.showToast('Chart saved as PNG.', 'success');
    }

    async _handleCopyPng(btn) {
        if (!this.chartContainer || !this.chartContainer.hasChart()) {
            this.showToast('No chart to copy yet.', 'error');
            return;
        }
        // Clipboard image API requires a secure context (HTTPS or localhost).
        const canCopyImage = !!(navigator.clipboard && window.ClipboardItem);
        if (!canCopyImage) {
            this.showToast('Clipboard image copy is not supported in this browser/context.', 'error');
            return;
        }
        const originalLabel = btn ? btn.querySelector('span')?.textContent : null;
        try {
            if (btn) {
                btn.disabled = true;
                const span = btn.querySelector('span');
                if (span) span.textContent = 'Copying…';
            }
            const blob = await this.chartContainer.getBlob({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
            if (!blob) throw new Error('No image data');
            await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
            this.showToast('Chart copied to clipboard.', 'success');
        } catch (e) {
            console.error('[ChartManager] Copy chart failed:', e);
            this.showToast('Could not copy chart: ' + (e && e.message ? e.message : 'unknown'), 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
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
                reject(new Error('Failed to load ECharts library'));
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
                    <p>📊 Chart view not available</p>
                    <p class="reason">${reason}</p>
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
            promptContent.innerHTML = '<p style="color: #999;">No prompt available</p>';
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
        const mapKey = [o.xColumn || '', o.yColumn || '', o.seriesColumn || ''].join('|');
        // Scope to the turn (Chat) so two differently-worded turns with the same
        // data don't reuse each other's chart. Ask mode (no ctx) is unscoped.
        const ctxSeed = this.ctx ? String(this.ctx.queryId || this.ctx.question || '') : '';
        const ctxKey = ctxSeed ? '_' + this.simpleHash(ctxSeed) : '';
        return `chart_llm_${dataHash}_${chartType}_${mapKey}${ctxKey}`;
    }

    _withQuickToggles(config) {
        if (!config) return config;
        if (this._isOsmMapOption(config)) return config;
        let out = config;
        if (this.chartOptionsPanel && !isMapOption(config)) {
            out = this.chartOptionsPanel.applyTogglesTo(config, this.originalConfig);
        }
        // Apply value formatting last so it covers initial render, chat edits,
        // quick toggles, and reset uniformly.
        out = this._applyValueFormatting(out);
        // Runs on the final config for every render path (initial, chat edit,
        // quick toggle, reset) so the header/legend never overprint.
        if (!isMapOption(out)) {
            out = this._suppressCanvasTitle(out);
        }
        return out;
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
            if (this.chartOptionsPanel) this.chartOptionsPanel.hide();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
        } else {
            if (this.chartOptionsPanel) this.chartOptionsPanel.show();
            if (this.mapOptionsPanel) this.mapOptionsPanel.hide();
            this._renderMapFeedback(null);
        }
    }

    _handleMapControl(action, value) {
        if (!this.currentEchartsOptions || !this.chartContainer) return;
        const chart = this.chartContainer.getInstance?.();
        if (!chart) return;

        if (action === 'zoom-in') {
            this._patchMapView((target) => { target.zoom = Math.min((target.zoom || 1) * 1.2, 12); });
        } else if (action === 'zoom-out') {
            this._patchMapView((target) => { target.zoom = Math.max((target.zoom || 1) / 1.2, 0.7); });
        } else if (action === 'fit' || action === 'reset') {
            if (action === 'reset') {
                try { chart.dispatchAction({ type: 'restore' }); } catch (_) {}
            }
            const view = this.currentEchartsOptions.jeenMap?.defaultView || {};
            this._patchMapView((target) => {
                for (const key of ['layoutCenter', 'layoutSize', 'aspectScale', 'zoom', 'scaleLimit']) {
                    if (view[key] !== undefined) target[key] = Array.isArray(view[key]) ? view[key].slice() : view[key];
                }
            });
            setTimeout(() => this.chartContainer.resize(), 0);
        } else if (action === 'labels') {
            this._patchMapView((target) => {
                target.label = target.label && typeof target.label === 'object' ? { ...target.label } : {};
                target.label.show = !!value;
            });
            if (this.currentEchartsOptions.jeenMap) this.currentEchartsOptions.jeenMap.showLabels = !!value;
        } else if (action === 'roam') {
            this._patchMapView((target) => { target.roam = !!value; });
        } else if (action === 'noData') {
            this._patchMapView((target) => {
                target.itemStyle = target.itemStyle && typeof target.itemStyle === 'object' ? { ...target.itemStyle } : {};
                target.itemStyle.areaColor = value ? '#eef2f7' : 'rgba(0,0,0,0)';
            });
        } else if (action === 'palette') {
            this._applyMapPalette(value);
        }
    }

    _patchMapView(mutator) {
        const options = this.currentEchartsOptions;
        const patch = {};
        if (Array.isArray(options.series)) {
            patch.series = options.series.map((series) => {
                if (!series || (series.type !== 'map' && series.coordinateSystem !== 'geo')) return {};
                mutator(series);
                return { ...series };
            });
        }
        if (options.geo && typeof options.geo === 'object' && !Array.isArray(options.geo)) {
            mutator(options.geo);
            patch.geo = { ...options.geo };
        }
        this.chartContainer.getInstance()?.setOption(patch, false);
        if (this.state.currentConfig) this.state.currentConfig.options = options;
    }

    _applyMapPalette(name) {
        const palette = MAP_PALETTES[name] || MAP_PALETTES.blue;
        const options = this.currentEchartsOptions;
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
        this.chartContainer.getInstance()?.setOption({
            visualMap: options.visualMap,
            series: Array.isArray(options.series) ? options.series.map((series) => ({ ...series })) : undefined,
        }, false);
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
        host.textContent = `${meta.unmatchedCount} location${meta.unmatchedCount === 1 ? '' : 's'} not matched: ${shown}${suffix}`;
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
     * Attach the compact/currency/percent value formatter to value axes and the
     * tooltip, driven by the server's `jeenFormat` hint. The hint is remembered
     * so chat-edited configs (which may drop it) keep consistent formatting.
     * Mutates and returns the option object; strips `jeenFormat` before ECharts.
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
            delete options.jeenFormat;
        }
        const primaryMeta = this._valueFormat || { kind: 'number', compact: true, symbol: '' };
        const primaryFmt = makeValueFormatter(primaryMeta);

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
                const f = axis.jeenFormat ? makeValueFormatter(axis.jeenFormat) : primaryFmt;
                axis.axisLabel = { ...(axis.axisLabel || {}), formatter: f };
            }
            if (axis.jeenFormat) delete axis.jeenFormat;
        };
        applyAxis(options.xAxis);
        applyAxis(options.yAxis);

        // Per-series formatter (so a combo's % line formats as % while its bars
        // format as currency). Data labels reuse the same formatter and get
        // overlap protection so dense charts stay readable.
        const series = Array.isArray(options.series) ? options.series : [];
        const seriesFmts = [];
        let perSeriesDiff = false;
        series.forEach((s, i) => {
            if (!s || typeof s !== 'object') { seriesFmts[i] = primaryFmt; return; }
            let f = primaryFmt;
            if (s.jeenFormat) {
                f = makeValueFormatter(s.jeenFormat);
                perSeriesDiff = true;
                delete s.jeenFormat;
            }
            seriesFmts[i] = f;
            if ((s.type === 'bar' || s.type === 'line')
                && !(s.label && typeof s.label.formatter === 'string')) {
                s.label = (s.label && typeof s.label === 'object') ? s.label : {};
                s.label.formatter = (p) => f(pickValue(p));
                if (s.label.fontSize == null) s.label.fontSize = 11;
                // Drop labels that would collide instead of overprinting them.
                if (!s.labelLayout) s.labelLayout = { hideOverlap: true };
            }
        });

        // Tooltip: a single valueFormatter can't express per-series formats or
        // unwrap [x, y] pairs, so use a formatter fn when either is in play.
        const hasGeoVisual = isMapOption(options);
        if (hasGeoVisual && options.visualMap && typeof options.visualMap === 'object' && !Array.isArray(options.visualMap)) {
            options.visualMap.formatter = (value) => primaryFmt(value);
        }

        const tip = options.tooltip;
        if (tip && !Array.isArray(tip) && typeof tip.formatter !== 'string') {
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
                tip.formatter = (params) => {
                    const arr = Array.isArray(params) ? params : [params];
                    const head = arr.length ? (arr[0].axisValueLabel ?? arr[0].name ?? '') : '';
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

    _reapplyQuickToggles() {
        if (!this.originalConfig || !this.chartContainer) return;
        let baseline;
        try {
            baseline = JSON.parse(JSON.stringify(this.originalConfig));
        } catch (_) {
            baseline = this.originalConfig;
        }
        const displayConfig = this._withQuickToggles(baseline);
        const chartConfig = {
            type: this._optionChartType(displayConfig),
            options: displayConfig,
            isEnhanced: true,
        };
        try {
            this.chartContainer.render(chartConfig);
            this.currentEchartsOptions = displayConfig;
            this.state.currentConfig = chartConfig;
        } catch (error) {
            console.error('[ChartManager] Failed to apply quick toggles:', error);
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
