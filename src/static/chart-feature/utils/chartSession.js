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

function stableStringify(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
    return `{${Object.keys(value).sort().map((key) => (
        `${JSON.stringify(key)}:${stableStringify(value[key])}`
    )).join(',')}}`;
}

function shortHash(value) {
    const text = String(value || '');
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
}

/**
 * Stable key for presentation overrides. Explicit semantic identifiers win;
 * the fallback intentionally excludes data and array position.
 */
export function seriesIdentity(series, index = 0, siblings = []) {
    const item = object(series);
    if (item.id !== undefined && item.id !== null && String(item.id).trim()) {
        return `id:${String(item.id).trim()}`;
    }
    if (item.jeenRole !== undefined && item.jeenRole !== null && String(item.jeenRole).trim()) {
        return `role:${String(item.jeenRole).trim()}`;
    }
    if (item.name !== undefined && item.name !== null && String(item.name).trim()) {
        return `name:${String(item.name).trim()}`;
    }
    const semantic = {
        encode: item.encode || null,
        dimensions: item.dimensions || null,
        datasetIndex: item.datasetIndex ?? null,
        xAxisIndex: item.xAxisIndex ?? null,
        yAxisIndex: item.yAxisIndex ?? null,
    };
    const base = `fallback:${shortHash(stableStringify(semantic))}`;
    const list = array(siblings);
    if (!list.length) return base;
    let ordinal = 0;
    for (let i = 0; i < Math.min(index, list.length); i++) {
        const sibling = object(list[i]);
        if (sibling.id || sibling.jeenRole || sibling.name) continue;
        const siblingSemantic = {
            encode: sibling.encode || null,
            dimensions: sibling.dimensions || null,
            datasetIndex: sibling.datasetIndex ?? null,
            xAxisIndex: sibling.xAxisIndex ?? null,
            yAxisIndex: sibling.yAxisIndex ?? null,
        };
        if (stableStringify(siblingSemantic) === stableStringify(semantic)) ordinal += 1;
    }
    return ordinal ? `${base}:${ordinal}` : base;
}

function normalizeToggles(value) {
    return { ...DEFAULT_CHART_TOGGLES, ...object(value) };
}

function normalizeMapView(value) {
    return clone(object(value));
}

/** Presentation annotations (reference lines, highlights) added by chart chat. */
function normalizeAnnotations(value) {
    const source = object(value);
    return {
        referenceLines: clone(array(source.referenceLines)),
        highlights: clone(array(source.highlights)),
    };
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
        annotations: normalizeAnnotations(baselineRaw.annotations),
        mapView: normalizeMapView(baselineRaw.mapView || viewRaw.mapView),
    };
    return {
        baseline,
        working: {
            config: clone(workingRaw.config || baseline.config),
            spec: clone(workingRaw.spec ?? baseline.spec),
            derivedSpecs: clone(array(workingRaw.derivedSpecs ?? baseline.derivedSpecs)),
            styleOverrides: clone(object(workingRaw.styleOverrides, baseline.styleOverrides)),
            annotations: normalizeAnnotations(workingRaw.annotations ?? baseline.annotations),
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
            annotations: value.annotations !== undefined
                ? normalizeAnnotations(value.annotations)
                : clone(this._working.annotations),
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
            annotations: clone(this._baseline.annotations),
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
    const previousByIdentity = new Map(prevSeries.map((item, index) => [
        seriesIdentity(item, index, prevSeries),
        item,
    ]));
    const seriesByIdentity = {};
    nextSeries.forEach((item, index) => {
        const identity = seriesIdentity(item, index, nextSeries);
        const patch = pickChanged(item, previousByIdentity.get(identity), SERIES_STYLE_KEYS);
        if (Object.keys(patch).length) seriesByIdentity[identity] = patch;
    });
    if (Object.keys(seriesByIdentity).length) out.seriesByIdentity = seriesByIdentity;
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
    const hasIdentitySeries = Object.keys(object(overrides?.seriesByIdentity)).length > 0;
    const rootOverrides = Object.fromEntries(
        Object.entries(object(overrides)).filter(([key]) => (
            !['xAxis', 'yAxis', 'series', 'seriesByIdentity'].includes(key)
        )),
    );
    const out = applyOverridePatch(config, rootOverrides);
    for (const key of ['xAxis', 'yAxis', 'series']) {
        // Identity overrides supersede the duplicated index form emitted by
        // newer snapshots. A legacy snapshot with only `series` still applies.
        if (key === 'series' && hasIdentitySeries) continue;
        if (!Array.isArray(overrides?.[key])) continue;
        const originalWasArray = Array.isArray(config[key]);
        const values = originalWasArray ? array(config[key]) : config[key] ? [config[key]] : [];
        const merged = values.map((value, index) => applyOverridePatch(value, overrides[key][index]));
        out[key] = originalWasArray ? merged : merged[0];
    }
    if (Array.isArray(config.series) && overrides?.seriesByIdentity) {
        out.series = array(out.series).map((value, index) => {
            const identity = seriesIdentity(config.series[index], index, config.series);
            return applyOverridePatch(value, overrides.seriesByIdentity[identity]);
        });
    }
    return out;
}
