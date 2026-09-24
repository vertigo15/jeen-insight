/**
 * Canonical in-page chart state.
 *
 * The server persists only the generated semantic config/spec. This module
 * keeps that immutable baseline next to the page-local working and view state
 * so turn switching can preserve edits while Reset remains exact.
 */

export const CHART_SESSION_VERSION = 1;

export const DEFAULT_CHART_TOGGLES = Object.freeze({
    dataLabels: false,
    legend: true,
    dataZoom: false,
    sortDesc: false,
});

function clone(value) {
    if (value === undefined) return undefined;
    if (value === null) return null;
    try {
        return structuredClone(value);
    } catch (_) {
        return JSON.parse(JSON.stringify(value));
    }
}

function deepFreeze(value) {
    if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
    Object.freeze(value);
    Object.values(value).forEach(deepFreeze);
    return value;
}

function object(value, fallback = {}) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : fallback;
}

function array(value) {
    return Array.isArray(value) ? value : [];
}

function normalizeToggles(value) {
    return { ...DEFAULT_CHART_TOGGLES, ...object(value) };
}

function normalizeMapView(value) {
    return clone(object(value));
}

function normalizeCanonical(raw) {
    const baselineRaw = object(raw.baseline);
    const workingRaw = object(raw.working);
    const viewRaw = object(raw.view);
    const baseline = {
        config: clone(baselineRaw.config || workingRaw.config || null),
        spec: clone(baselineRaw.spec ?? workingRaw.spec ?? null),
        toggles: normalizeToggles(baselineRaw.toggles || viewRaw.toggles),
        legendUserSet: Boolean(baselineRaw.legendUserSet),
        derivedSpecs: clone(array(baselineRaw.derivedSpecs)),
        styleOverrides: clone(object(baselineRaw.styleOverrides)),
        mapView: normalizeMapView(baselineRaw.mapView || viewRaw.mapView),
    };
    return {
        baseline,
        working: {
            config: clone(workingRaw.config || baseline.config),
            spec: clone(workingRaw.spec ?? baseline.spec),
            derivedSpecs: clone(array(workingRaw.derivedSpecs ?? baseline.derivedSpecs)),
            styleOverrides: clone(object(workingRaw.styleOverrides, baseline.styleOverrides)),
        },
        view: {
            toggles: normalizeToggles(viewRaw.toggles || baseline.toggles),
            legendUserSet: typeof viewRaw.legendUserSet === 'boolean'
                ? viewRaw.legendUserSet
                : baseline.legendUserSet,
            mapView: normalizeMapView(viewRaw.mapView || baseline.mapView),
        },
    };
}

/**
 * Accept a versioned page snapshot or the legacy server artifact shape.
 */
export function normalizeChartSnapshot(snapshot, defaults = {}) {
    const source = object(snapshot);
    if (source.chart_session?.version === CHART_SESSION_VERSION) {
        return normalizeCanonical(source.chart_session);
    }
    if (source.version === CHART_SESSION_VERSION && source.baseline && source.working) {
        return normalizeCanonical(source);
    }

    const config = clone(source.chart_config || source.config || defaults.config || null);
    const spec = clone(source.chart_spec ?? source.spec ?? defaults.spec ?? null);
    const toggles = normalizeToggles(
        source.chart_toggles || source.toggles || defaults.toggles,
    );
    const derivedSpecs = clone(array(
        source.derived_specs || source.derivedSpecs || defaults.derivedSpecs,
    ));
    const legendUserSet = Boolean(source.legend_user_set ?? defaults.legendUserSet);
    const mapView = normalizeMapView(source.map_view || defaults.mapView);
    return normalizeCanonical({
        baseline: { config, spec, toggles, legendUserSet, derivedSpecs, mapView },
        working: { config, spec, derivedSpecs, styleOverrides: defaults.styleOverrides },
        view: { toggles, legendUserSet, mapView },
    });
}

export class ChartSession {
    constructor(snapshot, defaults = {}) {
        const normalized = normalizeChartSnapshot(snapshot, defaults);
        this._baseline = deepFreeze(clone(normalized.baseline));
        this._working = clone(normalized.working);
        this._view = clone(normalized.view);
    }

    get baseline() { return clone(this._baseline); }
    get working() { return clone(this._working); }
    get view() { return clone(this._view); }

    replaceWorking(next = {}) {
        const value = object(next);
        this._working = {
            config: clone(value.config ?? this._working.config),
            spec: clone(value.spec !== undefined ? value.spec : this._working.spec),
            derivedSpecs: clone(value.derivedSpecs !== undefined
                ? array(value.derivedSpecs)
                : this._working.derivedSpecs),
            styleOverrides: clone(value.styleOverrides !== undefined
                ? object(value.styleOverrides)
                : this._working.styleOverrides),
        };
        return this.working;
    }

    replaceView(next = {}) {
        const value = object(next);
        this._view = {
            toggles: value.toggles
                ? normalizeToggles(value.toggles)
                : clone(this._view.toggles),
            legendUserSet: typeof value.legendUserSet === 'boolean'
                ? value.legendUserSet
                : this._view.legendUserSet,
            mapView: value.mapView !== undefined
                ? normalizeMapView(value.mapView)
                : clone(this._view.mapView),
        };
        return this.view;
    }

    reset() {
        this._working = {
            config: clone(this._baseline.config),
            spec: clone(this._baseline.spec),
            derivedSpecs: clone(this._baseline.derivedSpecs),
            styleOverrides: clone(this._baseline.styleOverrides),
        };
        this._view = {
            toggles: clone(this._baseline.toggles),
            legendUserSet: this._baseline.legendUserSet,
            mapView: clone(this._baseline.mapView),
        };
        return this.snapshot();
    }

    snapshot() {
        const canonical = {
            version: CHART_SESSION_VERSION,
            baseline: this.baseline,
            working: this.working,
            view: this.view,
        };
        // Keep the legacy pair at the top level for existing turn/artifact
        // plumbing. It deliberately points at semantic working state, never
        // theme- or formatter-baked rendered output.
        return {
            chart_config: clone(canonical.working.config),
            chart_spec: clone(canonical.working.spec),
            chart_toggles: clone(canonical.view.toggles),
            derived_specs: clone(canonical.working.derivedSpecs),
            chart_session: canonical,
        };
    }
}

export function createChartSession(snapshot, defaults = {}) {
    return new ChartSession(snapshot, defaults);
}

const OPTION_STYLE_KEYS = new Set([
    'color', 'backgroundColor', 'textStyle', 'grid',
    'visualMap', 'animation', 'animationDuration', 'animationEasing',
]);
const AXIS_STYLE_KEYS = new Set([
    'nameTextStyle', 'axisLine', 'axisTick', 'axisLabel', 'splitLine',
    'splitArea', 'nameGap', 'nameRotate', 'offset',
]);
const SERIES_STYLE_KEYS = new Set([
    'itemStyle', 'lineStyle', 'areaStyle', 'label', 'labelLine', 'labelLayout',
    'emphasis', 'blur', 'select', 'symbol', 'symbolSize', 'smooth',
    'barWidth', 'barMaxWidth', 'barGap', 'barCategoryGap', 'opacity',
]);

function changed(next, previous) {
    try { return JSON.stringify(next) !== JSON.stringify(previous); } catch (_) { return true; }
}

function pickChanged(next, previous, keys) {
    const out = {};
    keys.forEach((key) => {
        if (next && key in next && changed(next[key], previous?.[key])) {
            out[key] = clone(next[key]);
        } else if (previous && key in previous && (!next || !(key in next))) {
            // Null is a deletion marker for presentation overrides. It lets a
            // later edit such as "use the default color" remove an earlier
            // explicit style instead of merging the old value forever.
            out[key] = null;
        }
    });
    return out;
}

/**
 * Capture only user-edited presentation fields. Applying this delta after the
 * workspace theme lets explicit edit requests win without making theme output
 * part of semantic working state.
 */
export function extractStyleOverrides(nextConfig, previousConfig = null) {
    if (!nextConfig || typeof nextConfig !== 'object') return {};
    const out = pickChanged(nextConfig, previousConfig, OPTION_STYLE_KEYS);
    for (const axisKey of ['xAxis', 'yAxis']) {
        const nextAxes = Array.isArray(nextConfig[axisKey])
            ? nextConfig[axisKey]
            : nextConfig[axisKey] ? [nextConfig[axisKey]] : [];
        const prevAxes = Array.isArray(previousConfig?.[axisKey])
            ? previousConfig[axisKey]
            : previousConfig?.[axisKey] ? [previousConfig[axisKey]] : [];
        const axes = nextAxes.map((axis, index) => pickChanged(axis, prevAxes[index], AXIS_STYLE_KEYS));
        if (axes.some((axis) => Object.keys(axis).length)) out[axisKey] = axes;
    }
    const nextSeries = array(nextConfig.series);
    const prevSeries = array(previousConfig?.series);
    const series = nextSeries.map((item, index) => pickChanged(item, prevSeries[index], SERIES_STYLE_KEYS));
    if (series.some((item) => Object.keys(item).length)) out.series = series;
    return out;
}

function mergeObject(target, patch) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return clone(patch);
    const out = target && typeof target === 'object' && !Array.isArray(target)
        ? { ...target }
        : {};
    Object.entries(patch).forEach(([key, value]) => {
        out[key] = value && typeof value === 'object' && !Array.isArray(value)
            ? mergeObject(out[key], value)
            : clone(value);
    });
    return out;
}

export function mergeStyleOverrides(current, delta) {
    return mergeObject(object(current), object(delta));
}

function applyOverridePatch(target, patch) {
    const out = target && typeof target === 'object' && !Array.isArray(target)
        ? { ...target }
        : {};
    Object.entries(object(patch)).forEach(([key, value]) => {
        if (value === null) {
            delete out[key];
        } else {
            out[key] = value && typeof value === 'object' && !Array.isArray(value)
                ? applyOverridePatch(out[key], value)
                : clone(value);
        }
    });
    return out;
}

export function applyStyleOverrides(config, overrides) {
    if (!config || typeof config !== 'object') return config;
    const out = applyOverridePatch(config, overrides);
    for (const key of ['xAxis', 'yAxis', 'series']) {
        if (!Array.isArray(overrides?.[key])) continue;
        const originalWasArray = Array.isArray(config[key]);
        const values = originalWasArray ? array(config[key]) : config[key] ? [config[key]] : [];
        const merged = values.map((value, index) => applyOverridePatch(value, overrides[key][index]));
        out[key] = originalWasArray ? merged : merged[0];
    }
    return out;
}
