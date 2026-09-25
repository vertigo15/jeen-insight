/**
 * Pure helpers for the chart-editor v2 contract.
 *
 * The editor sees a compact, data-free manifest and returns a small operation
 * list. Operations are applied to a cloned ChartSession; callers only publish
 * the candidate after a successful render.
 */

import {
    createChartSession,
    mergeStyleOverrides,
    seriesIdentity,
} from './chartSession.js?v=4';
import {
    MAX_HIGHLIGHT_CATEGORIES,
    MAX_SCENARIOS,
    isScenarioSpec,
    normalizeAnnotations,
    resolveTargetSeries,
    scenarioSpecFromOperation,
    seriesCategories,
} from './chartScenarios.js?v=1';

const MAX_REFERENCE_LINES = 4;
const REFERENCE_STATS = new Set(['avg', 'min', 'max', 'median']);
const PREDICATE_OPS = new Set(['lt', 'lte', 'gt', 'gte', 'eq', 'between']);

const VISIBLE_ML_ROLES = new Set(['actual', 'expected', 'forecast', 'flagged']);
const ML_HELPER_ROLES = new Set([
    'interval_base', 'interval_bound', 'lower_bound', 'upper_bound', 'helper',
]);
const ML_LABEL_HIDDEN_ROLES = new Set(['interval', ...ML_HELPER_ROLES]);
const OVERLAY_OPERATORS = new Set([
    'moving_avg', 'cumulative_sum', 'percent_change',
    'linear_trend', 'normalize_0_1', 'log_scale',
]);
const STYLE_PATHS = new Set([
    'itemStyle.color', 'itemStyle.opacity', 'itemStyle.borderColor',
    'itemStyle.borderWidth', 'lineStyle.color', 'lineStyle.opacity',
    'lineStyle.width', 'lineStyle.type', 'areaStyle.color',
    'areaStyle.opacity', 'label.show', 'label.position', 'label.color',
    'label.fontSize', 'smooth', 'symbol', 'symbolSize', 'opacity',
    'label.formatter',
]);
const INTERVAL_STYLE_PATHS = new Set([
    'areaStyle.color', 'areaStyle.opacity', 'itemStyle.color', 'itemStyle.opacity',
]);
const FORMAT_KINDS = new Set(['number', 'currency', 'percent']);
// Cartesian types the browser flips in place; every other type is rebuilt by
// the server from the cached rows (see ChartManager.applyEditedOperations).
const LOCAL_CHART_TYPES = new Set(['bar', 'line', 'area']);
const REBUILD_CHART_TYPES = new Set([
    'bar', 'line', 'area', 'pie', 'donut', 'scatter', 'horizontal_bar',
    'stacked_bar', 'stacked_area', 'combo', 'heatmap', 'gauge',
]);
// Series that draw one colour each; a palette pins colours[i] on them so the
// choice survives the theme pass. Pie-like series take slices from option.color.
const SINGLE_COLOUR_SERIES = new Set(['bar', 'line', 'scatter', 'effectScatter']);
const MAX_PALETTE_COLORS = 12;
const FORBIDDEN_KEYS = new Set([
    'data', 'rows', 'all_data', 'sample_data', 'current_config',
    'dataset', 'source', 'values',
]);

function clone(value) {
    if (value === undefined) return undefined;
    if (value === null) return null;
    try { return structuredClone(value); } catch (_) {
        return JSON.parse(JSON.stringify(value));
    }
}

function object(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function array(value) {
    return Array.isArray(value) ? value : [];
}

function valueAt(operation, ...keys) {
    for (const key of keys) {
        if (operation[key] !== undefined) return operation[key];
    }
    return undefined;
}

function compactValue(value, depth = 0) {
    if (depth > 5 || value === undefined || typeof value === 'function') return undefined;
    if (value === null || typeof value === 'boolean' || typeof value === 'number') return value;
    if (typeof value === 'string') return value.slice(0, 160);
    if (Array.isArray(value)) {
        return value.slice(0, 12)
            .map((item) => compactValue(item, depth + 1))
            .filter((item) => item !== undefined);
    }
    if (typeof value !== 'object') return String(value).slice(0, 160);
    const out = {};
    for (const [key, item] of Object.entries(value)) {
        if (FORBIDDEN_KEYS.has(key)) continue;
        const compact = compactValue(item, depth + 1);
        if (compact !== undefined) out[key] = compact;
    }
    return out;
}

function finiteNumber(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value !== 'string' || !value.trim()) return null;
    const parsed = Number(value.replace(/[$€£¥,\s]/g, ''));
    return Number.isFinite(parsed) ? parsed : null;
}

function rawPointValue(point) {
    if (point && typeof point === 'object' && !Array.isArray(point) && 'value' in point) {
        return point.value;
    }
    return point;
}

function summarizeData(data) {
    const values = array(data);
    const numeric = [];
    let nulls = 0;
    let dimensions = 1;
    for (const point of values) {
        const raw = rawPointValue(point);
        const parts = Array.isArray(raw) ? raw : [raw];
        dimensions = Math.max(dimensions, parts.length);
        let found = false;
        for (const part of parts) {
            const n = finiteNumber(part);
            if (n !== null) {
                numeric.push(n);
                found = true;
            }
        }
        if (!found && (raw === null || raw === undefined || raw === '')) nulls += 1;
    }
    const summary = { count: values.length, null_count: nulls };
    if (dimensions > 1) summary.dimensions = dimensions;
    if (numeric.length) {
        summary.numeric_range = {
            min: Math.min(...numeric),
            max: Math.max(...numeric),
        };
    }
    return summary;
}

const MAX_MANIFEST_CATEGORIES = 200;

/** Axis category as displayed: ECharts allows `{ value, textStyle }` entries. */
export function categoryLabel(value) {
    if (value && typeof value === 'object' && !Array.isArray(value) && 'value' in value) {
        return String(value.value ?? '');
    }
    return value === null || value === undefined ? '' : String(value);
}

function summarizeAxis(axis) {
    if (!axis || typeof axis !== 'object') return null;
    const out = {};
    for (const key of ['id', 'name', 'type', 'inverse', 'min', 'max', 'boundaryGap']) {
        if (axis[key] !== undefined) out[key] = compactValue(axis[key]);
    }
    if (Array.isArray(axis.data)) {
        const labels = axis.data.map((value) => categoryLabel(value).slice(0, 80));
        out.categories = { count: labels.length };
        // The model can only reference categories it has seen ("what if Bikes
        // were 30K"), so send the complete list when it is small enough.
        if (labels.length <= MAX_MANIFEST_CATEGORIES) {
            out.categories.values = labels;
            out.categories.complete = true;
        } else {
            out.categories.sample = labels.slice(0, 6);
            out.categories.last = labels[labels.length - 1];
            out.categories.complete = false;
        }
    }
    if (axis.jeenFormat) out.format = compactValue(axis.jeenFormat);
    return out;
}

function styleSummary(series, stylePatch) {
    const patch = object(stylePatch);
    const out = {};
    for (const key of ['itemStyle', 'lineStyle', 'areaStyle', 'label', 'smooth', 'symbol', 'symbolSize']) {
        const value = patch[key] !== undefined ? patch[key] : series?.[key];
        if (value !== undefined) out[key] = compactValue(value);
    }
    return out;
}

function mergePatch(base, patch) {
    const out = { ...object(base) };
    for (const [key, value] of Object.entries(object(patch))) {
        out[key] = value && typeof value === 'object' && !Array.isArray(value)
            ? mergePatch(out[key], value)
            : clone(value);
    }
    return out;
}

function normalizeColumns(columns) {
    return array(columns).slice(0, 128).map((column) => {
        if (typeof column === 'string') return { name: column.slice(0, 160), type: 'unknown' };
        return {
            name: String(column?.name || '').slice(0, 160),
            type: String(column?.type || 'unknown').slice(0, 40),
        };
    }).filter((column) => column.name);
}

export function buildChartManifest(session, {
    chartKind = 'sql',
    columns = [],
} = {}) {
    const working = session?.working || {};
    const view = session?.view || {};
    const config = object(working.config);
    const overrides = object(working.styleOverrides);
    const byIdentity = object(overrides.seriesByIdentity);
    const legacySeries = array(overrides.series);
    const series = array(config.series).map((item, index) => {
        const identity = seriesIdentity(item, index, config.series);
        return {
            identity,
            ...(item?.id != null
                ? { id: String(item.id).slice(0, 160) }
                : identity.startsWith('fallback:')
                    ? { id: identity }
                    : {}),
            ...(item?.jeenRole ? { role: String(item.jeenRole).slice(0, 80) } : {}),
            ...(item?.name ? { name: String(item.name).slice(0, 160) } : {}),
            type: String(item?.type || '').slice(0, 40),
            stack: item?.stack ?? null,
            ...(Number.isInteger(item?.xAxisIndex) ? { xAxisIndex: item.xAxisIndex } : {}),
            ...(Number.isInteger(item?.yAxisIndex) ? { yAxisIndex: item.yAxisIndex } : {}),
            hidden: Boolean(item?.name && config.legend?.selected?.[item.name] === false),
            data_summary: summarizeData(item?.data),
            style: styleSummary(item, mergePatch(legacySeries[index], byIdentity[identity])),
            locked: ML_HELPER_ROLES.has(item?.jeenRole),
        };
    });
    const axisList = (value) => (Array.isArray(value) ? value : value ? [value] : [])
        .map(summarizeAxis)
        .filter(Boolean);
    return {
        series,
        axes: {
            x: axisList(config.xAxis),
            y: axisList(config.yAxis),
        },
        toggles: Object.fromEntries(
            Object.entries(view.toggles || {}).filter(([, value]) => typeof value === 'boolean'),
        ),
        format: compactValue(config.jeenFormat || null),
        overlays: compactValue(working.derivedSpecs || []),
        annotations: compactValue(working.annotations || null),
        chart_spec: compactValue(working.spec || null),
        columns: chartKind === 'sql' ? normalizeColumns(columns) : [],
        locks: {
            chart_kind: chartKind,
            immutable_fields: [
                'data', 'axis.type', 'axis.index', 'jeenRole',
                'markLine', 'markArea', 'markPoint', 'metadata',
            ],
            helper_roles: [...ML_HELPER_ROLES],
        },
    };
}

function targetString(target) {
    if (typeof target === 'string') return target.trim();
    if (target && typeof target === 'object') {
        if (target.role != null) return `role:${target.role}`;
        if (target.name != null) return `name:${target.name}`;
        if (target.id != null) return `id:${target.id}`;
    }
    return '';
}

function targetSeries(config, target, chartKind, { allowInterval = false } = {}) {
    const all = array(config.series);
    const rawTarget = targetString(target) || 'all';
    let matched;
    if (rawTarget === 'all') {
        matched = all.map((series, index) => ({ series, index }));
    } else {
        const separator = rawTarget.indexOf(':');
        if (separator <= 0) throw new ChartEditOperationError('invalid_target');
        const kind = rawTarget.slice(0, separator);
        const expected = rawTarget.slice(separator + 1);
        matched = all.map((series, index) => ({ series, index })).filter(({ series, index }) => {
            if (kind === 'role') return String(series?.jeenRole || '') === expected;
            if (kind === 'name') return String(series?.name || '') === expected;
            if (kind === 'id') {
                return String(series?.id ?? '') === expected
                    || seriesIdentity(series, index, all) === `id:${expected}`
                    || seriesIdentity(series, index, all) === expected;
            }
            return false;
        });
    }
    if (chartKind !== 'sql') {
        matched = matched.filter(({ series }) => (
            VISIBLE_ML_ROLES.has(series?.jeenRole)
            || (allowInterval && series?.jeenRole === 'interval')
        ));
    }
    if (!matched.length) throw new ChartEditOperationError('target_not_found');
    if (matched.some(({ series }) => ML_HELPER_ROLES.has(series?.jeenRole))) {
        throw new ChartEditOperationError('locked_ml_helper');
    }
    return matched;
}

function validColor(color) {
    if (typeof color !== 'string') return false;
    const value = color.trim();
    if (!value || value.length > 64) return false;
    if (/^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(value)) return true;
    if (!/^(?:rgb|rgba|hsl|hsla)\(\s*[-+.%\d,\s]+\)$/i.test(value)) return false;
    const components = (value.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
    if (value.toLowerCase().startsWith('rgb')) {
        return components.length >= 3
            && components.slice(0, 3).every((component) => component >= 0 && component <= 255);
    }
    return components.length >= 3
        && components[0] >= 0 && components[0] <= 360
        && components[1] >= 0 && components[1] <= 100
        && components[2] >= 0 && components[2] <= 100;
}

function setPath(target, path, value) {
    const parts = path.split('.');
    if (parts.length === 1) {
        target[parts[0]] = clone(value);
        return;
    }
    const [head, tail] = parts;
    target[head] = { ...object(target[head]), [tail]: clone(value) };
}

function validateStyleValue(path, value) {
    if (path.endsWith('.color')) {
        if (!validColor(value)) throw new ChartEditOperationError('invalid_color');
        return;
    }
    if (path === 'label.show' || path === 'smooth') {
        if (typeof value !== 'boolean' && !(path === 'smooth'
            && typeof value === 'number' && value >= 0 && value <= 1)) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path.endsWith('.opacity')) {
        if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (/(?:Width|width|fontSize|symbolSize)$/.test(path)) {
        const bounds = path === 'symbolSize'
            ? [1, 80]
            : path === 'label.fontSize'
                ? [8, 40]
                : [0, 12];
        if (typeof value !== 'number' || !Number.isFinite(value)
            || value < bounds[0] || value > bounds[1]) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path === 'lineStyle.type') {
        if (!['solid', 'dashed', 'dotted'].includes(value)) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path === 'label.position') {
        if (!['top', 'bottom', 'left', 'right', 'inside', 'insideTop', 'insideBottom'].includes(value)) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path === 'symbol') {
        if (!['circle', 'rect', 'roundRect', 'triangle', 'diamond', 'pin', 'arrow', 'none'].includes(value)) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path === 'label.formatter') {
        if (typeof value !== 'string' || value.length < 1 || value.length > 80
            || /[<>\r\n\0]/.test(value)
            || /[{}]/.test(value.replace(/\{(?:a|b|c|d|value|seriesName|name)\}/g, ''))) {
            throw new ChartEditOperationError('invalid_style_value');
        }
        return;
    }
    if (path === 'opacity') {
        if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
            throw new ChartEditOperationError('invalid_style_value');
        }
    }
}

function mergeIdentityPatches(overrides, config, matches, patchForSeries) {
    const delta = { seriesByIdentity: {} };
    for (const { series, index } of matches) {
        const identity = seriesIdentity(series, index, config.series);
        delta.seriesByIdentity[identity] = patchForSeries(series);
    }
    return mergeStyleOverrides(overrides, delta);
}

function upgradeLegacyOverrides(overrides, config) {
    const legacy = array(overrides?.series);
    if (!legacy.some((patch) => Object.keys(object(patch)).length)) return overrides;
    const delta = { seriesByIdentity: {} };
    legacy.forEach((patch, index) => {
        if (!Object.keys(object(patch)).length || !config.series?.[index]) return;
        const identity = seriesIdentity(config.series[index], index, config.series);
        delta.seriesByIdentity[identity] = patch;
    });
    const upgraded = mergeStyleOverrides(delta, overrides);
    delete upgraded.series;
    return upgraded;
}

function normalizeFormat(operation) {
    const raw = valueAt(operation, 'format', 'value') ?? (
        operation.kind ? {
            kind: operation.kind,
            compact: operation.compact,
            symbol: operation.symbol,
            scale: operation.scale,
        } : undefined
    );
    const format = typeof raw === 'string' ? { kind: raw } : object(raw);
    const kind = String(format.kind || '').toLowerCase();
    if (!FORMAT_KINDS.has(kind)) throw new ChartEditOperationError('invalid_format');
    const out = { kind };
    if (typeof format.compact === 'boolean') out.compact = format.compact;
    if (format.symbol !== undefined) out.symbol = String(format.symbol).slice(0, 4);
    if (out.kind !== 'currency' && out.symbol) throw new ChartEditOperationError('invalid_format');
    if (out.symbol && ![
        '$', '€', '£', '₪', '¥', '₹', '₩', '₽', '₺', '₴', '₫', '₱', '฿', '₦', 'R$',
    ].includes(out.symbol)) {
        throw new ChartEditOperationError('invalid_format');
    }
    if (format.scale !== undefined) {
        const scale = Number(format.scale);
        if (!Number.isFinite(scale) || scale <= 0 || scale > 1e12) {
            throw new ChartEditOperationError('invalid_format');
        }
        out.scale = scale;
    }
    return out;
}

function normalizeOverlay(operation) {
    const raw = object(valueAt(operation, 'overlay', 'spec', 'value'));
    const operator = String(raw.operator || operation.operator || '').toLowerCase();
    if (!OVERLAY_OPERATORS.has(operator)) throw new ChartEditOperationError('invalid_overlay');
    const out = { operator };
    const source = raw.source_column ?? raw.source_series ?? operation.source_column;
    if (source) out.source_column = String(source).slice(0, 160);
    const label = raw.label ?? operation.label;
    if (label) out.label = String(label).slice(0, 160);
    if (raw.id || operation.id) out.id = String(raw.id || operation.id).slice(0, 160);
    const params = raw.params ?? operation.params;
    if (params && typeof params === 'object') {
        out.params = compactValue(params);
    }
    return out;
}

function safeAnnotationLabel(value, fallback = '') {
    const text = String(value ?? '').trim();
    if (!text) return fallback;
    if (text.length > 80 || /[<>\r\n\0]/.test(text)) throw new ChartEditOperationError('invalid_series_name');
    return text;
}

function normalizeReferenceLine(operation, config) {
    const axis = operation.axis === 'x' ? 'x' : 'y';
    const hasValue = operation.value !== undefined && operation.value !== null;
    const stat = operation.stat ? String(operation.stat).toLowerCase() : '';
    if (hasValue === Boolean(stat)) throw new ChartEditOperationError('invalid_scenario');
    if (stat && !REFERENCE_STATS.has(stat)) throw new ChartEditOperationError('invalid_scenario');
    const value = hasValue ? finiteNumber(operation.value) : null;
    if (hasValue && value === null) throw new ChartEditOperationError('invalid_scenario');
    if (operation.target && !resolveTargetSeries(config, operation.target).length) {
        throw new ChartEditOperationError('target_not_found');
    }
    const line = { axis, label: safeAnnotationLabel(operation.label, stat || String(value)) };
    if (hasValue) line.value = value;
    else line.stat = stat;
    if (operation.target) line.target = typeof operation.target === 'string' ? operation.target : clone(operation.target);
    return line;
}

function normalizeHighlight(operation, config) {
    const matches = resolveTargetSeries(config, operation.target);
    if (!matches.length) throw new ChartEditOperationError('target_not_found');
    const hasCategories = Array.isArray(operation.categories);
    const predicate = operation.predicate && typeof operation.predicate === 'object' ? operation.predicate : null;
    if (hasCategories === Boolean(predicate)) throw new ChartEditOperationError('invalid_scenario');
    const highlight = { target: typeof operation.target === 'string' ? operation.target.trim() || 'all' : 'all' };
    if (hasCategories) {
        if (!operation.categories.length || operation.categories.length > MAX_HIGHLIGHT_CATEGORIES) {
            throw new ChartEditOperationError('too_many_highlights');
        }
        const known = seriesCategories(matches[0].series, config);
        const resolved = operation.categories.map((wanted) => known.find((label) => (
            String(label).trim().toLowerCase() === String(wanted).trim().toLowerCase()
        )));
        if (resolved.some((label) => label === undefined)) throw new ChartEditOperationError('unknown_category');
        highlight.categories = resolved;
    } else {
        const op = String(predicate.op || '').toLowerCase();
        const value = finiteNumber(predicate.value);
        const value2 = predicate.value2 === undefined || predicate.value2 === null ? null : finiteNumber(predicate.value2);
        if (!PREDICATE_OPS.has(op) || value === null || (op === 'between') !== (value2 !== null)) {
            throw new ChartEditOperationError('invalid_scenario');
        }
        highlight.predicate = { op, value, ...(value2 !== null ? { value2 } : {}) };
    }
    if (operation.color) {
        const color = String(operation.color).trim();
        if (!validColor(color)) throw new ChartEditOperationError('invalid_color');
        highlight.color = color;
    }
    const label = safeAnnotationLabel(operation.label);
    if (label) highlight.label = label;
    return highlight;
}

function updateConfigSeries(config, matches, update) {
    const indexes = new Set(matches.map(({ index }) => index));
    return {
        ...config,
        series: array(config.series).map((series, index) => (
            indexes.has(index) ? update({ ...series }, index) : series
        )),
    };
}

function remapIdentityOverrides(overrides, beforeConfig, afterConfig, indexes) {
    const byIdentity = { ...object(overrides?.seriesByIdentity) };
    for (const index of indexes) {
        const before = seriesIdentity(beforeConfig.series[index], index, beforeConfig.series);
        const after = seriesIdentity(afterConfig.series[index], index, afterConfig.series);
        if (before === after || !byIdentity[before]) continue;
        byIdentity[after] = mergePatch(byIdentity[before], byIdentity[after]);
        delete byIdentity[before];
    }
    return { ...overrides, seriesByIdentity: byIdentity };
}

function operationName(operation) {
    return String(operation?.op || operation?.operation || operation?.type || '').trim().toLowerCase();
}

function ensureSql(chartKind) {
    if (chartKind !== 'sql') throw new ChartEditOperationError('sql_only_operation');
}

function ensureBoolean(value) {
    if (typeof value !== 'boolean') throw new ChartEditOperationError('invalid_boolean');
    return value;
}

function protectedSeriesState(config) {
    return array(config?.series).map((series, index) => ({
        identity: seriesIdentity(series, index, config.series),
        data: clone(series?.data),
        xAxisIndex: series?.xAxisIndex,
        yAxisIndex: series?.yAxisIndex,
        jeenRole: series?.jeenRole,
        markLine: clone(series?.markLine),
        markArea: clone(series?.markArea),
        markPoint: clone(series?.markPoint),
        encode: clone(series?.encode),
        dimensions: clone(series?.dimensions),
    }));
}

function assertProtectedState(before, after) {
    const next = new Map(protectedSeriesState(after).map((item) => [item.identity, item]));
    for (const previous of before) {
        const current = next.get(previous.identity);
        if (!current) continue; // hide/rename may change a name-only identity.
        for (const key of [
            'data', 'xAxisIndex', 'yAxisIndex', 'jeenRole',
            'markLine', 'markArea', 'markPoint', 'encode', 'dimensions',
        ]) {
            if (JSON.stringify(previous[key]) !== JSON.stringify(current[key])) {
                throw new ChartEditOperationError('protected_chart_state');
            }
        }
    }
}

export class ChartEditOperationError extends Error {
    constructor(code, detail = '') {
        super(detail || code);
        this.name = 'ChartEditOperationError';
        this.code = code;
    }
}

function requestedChartType(operation) {
    return String(valueAt(operation, 'chart_type', 'value') || '').toLowerCase();
}

/**
 * True when a set_chart_type operation can be flipped in the browser: the
 * target is bar/line/area and every visible series is already cartesian
 * bar/line. Anything else (pie, scatter, horizontal bar, or a pie → bar flip)
 * needs the server rebuild from the full result set.
 */
export function chartTypeAppliesLocally(operation, config) {
    const type = requestedChartType(operation);
    if (!LOCAL_CHART_TYPES.has(type)) return false;
    const visible = array(config?.series).filter((series) => !series?.__derived);
    return visible.length > 0
        && visible.every((series) => ['bar', 'line'].includes(series?.type));
}

/** Server-rebuildable chart type names accepted from the editor. */
export function isRebuildChartType(type) {
    return REBUILD_CHART_TYPES.has(String(type || '').toLowerCase());
}

/**
 * Split an operation list into the part that needs the deterministic server
 * rebuild (bindings, incompatible chart types) and the part that applies
 * locally. Order inside each group is preserved; rebuild runs first so local
 * styling lands on the rebuilt chart.
 */
export function partitionRebuildOperations(operations, config) {
    const rebuild = [];
    const local = [];
    for (const operation of array(operations)) {
        const op = operationName(operation);
        if (op === 'set_binding') {
            rebuild.push(operation);
        } else if (op === 'set_chart_type' && !chartTypeAppliesLocally(operation, config)) {
            if (!isRebuildChartType(requestedChartType(operation))) {
                throw new ChartEditOperationError('incompatible_chart_type');
            }
            rebuild.push(operation);
        } else {
            local.push(operation);
        }
    }
    return { rebuild, local };
}

/**
 * Apply the complete response transactionally. Any invalid/unsupported
 * operation rejects the whole list and leaves the source session untouched.
 */
export function applyChartEditOperations(session, operations, {
    chartKind = 'sql',
} = {}) {
    if (!session?.working?.config) throw new ChartEditOperationError('missing_chart_session');
    if (!Array.isArray(operations) || operations.length === 0) {
        throw new ChartEditOperationError('empty_operations');
    }
    if (operations.length > 12) throw new ChartEditOperationError('too_many_operations');

    const candidate = createChartSession(session.snapshot());
    let working = candidate.working;
    let view = candidate.view;
    let config = clone(working.config);
    let overrides = upgradeLegacyOverrides(clone(working.styleOverrides || {}), config);
    let overlays = clone(working.derivedSpecs || []);
    let spec = clone(working.spec);
    let annotations = normalizeAnnotations(working.annotations);
    const protectedBefore = protectedSeriesState(config);

    for (const raw of operations) {
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
            throw new ChartEditOperationError('invalid_operation');
        }
        const operation = raw;
        const op = operationName(operation);
        if (op === 'set_color') {
            const color = String(operation.color || '').trim();
            if (!validColor(color)) throw new ChartEditOperationError('invalid_color');
            const matches = targetSeries(config, operation.target, chartKind, { allowInterval: true });
            overrides = mergeIdentityPatches(overrides, config, matches, (series) => {
                if (series.jeenRole === 'interval') {
                    return { areaStyle: { color }, itemStyle: { color } };
                }
                const patch = { itemStyle: { color } };
                if (series.type === 'line') patch.lineStyle = { color };
                if (series.areaStyle) patch.areaStyle = { color };
                return patch;
            });
        } else if (op === 'set_palette') {
            ensureSql(chartKind);
            const colors = array(operation.colors).map((color) => String(color || '').trim());
            if (!colors.length || colors.length > MAX_PALETTE_COLORS || !colors.every(validColor)) {
                throw new ChartEditOperationError('invalid_color');
            }
            const visible = array(config.series)
                .map((series, index) => ({ series, index }))
                .filter(({ series }) => !series?.__derived && !ML_HELPER_ROLES.has(series?.jeenRole));
            if (!visible.length) throw new ChartEditOperationError('target_not_found');
            const singles = visible.filter(({ series }) => SINGLE_COLOUR_SERIES.has(series?.type));
            // One bar/scatter series with many categories: colour per data point,
            // otherwise the palette would be invisible. Lines stay one colour.
            const colorByData = singles.length === 1
                && visible.length === 1
                && ['bar', 'scatter'].includes(singles[0].series?.type);
            overrides = mergeStyleOverrides(overrides, { color: colors.slice() });
            overrides = mergeIdentityPatches(overrides, config, visible, (series) => {
                const position = singles.findIndex(({ series: item }) => item === series);
                if (position < 0 || colorByData) {
                    // null is the deletion marker: drop any pinned colour so the
                    // slices / points follow option.color.
                    return {
                        itemStyle: { color: null },
                        lineStyle: { color: null },
                        ...(colorByData ? { colorBy: 'data' } : {}),
                    };
                }
                const color = colors[position % colors.length];
                const patch = { itemStyle: { color }, colorBy: null };
                if (series.type === 'line') patch.lineStyle = { color };
                if (series.areaStyle) patch.areaStyle = { color };
                return patch;
            });
        } else if (op === 'set_style') {
            const path = String(operation.path || '').trim();
            if (!STYLE_PATHS.has(path)) throw new ChartEditOperationError('unsafe_style_path');
            const matches = targetSeries(config, operation.target, chartKind, { allowInterval: true });
            if (matches.some(({ series }) => series.jeenRole === 'interval')
                && !INTERVAL_STYLE_PATHS.has(path)) {
                throw new ChartEditOperationError('interval_style_locked');
            }
            const value = clone(operation.value);
            validateStyleValue(path, value);
            overrides = mergeIdentityPatches(overrides, config, matches, () => {
                const patch = {};
                setPath(patch, path, value);
                return patch;
            });
        } else if (op === 'set_toggle') {
            const key = String(operation.key || '').trim();
            const value = ensureBoolean(operation.value);
            const target = targetString(operation.target) || 'all';
            if (!['dataLabels', 'legend', 'dataZoom'].includes(key)) {
                throw new ChartEditOperationError('invalid_toggle');
            }
            if (target !== 'all' && key !== 'dataLabels') {
                throw new ChartEditOperationError('targeted_toggle_unsupported');
            }
            if (key === 'dataLabels' && (target !== 'all' || chartKind !== 'sql')) {
                const matches = targetSeries(config, target, chartKind);
                overrides = mergeIdentityPatches(overrides, config, matches, () => ({
                    label: { show: value },
                }));
                if (target === 'all') {
                    view.toggles.dataLabels = value;
                }
            } else {
                view.toggles[key] = value;
                if (key === 'legend') view.legendUserSet = true;
            }
        } else if (op === 'set_format') {
            config = { ...config, jeenFormat: normalizeFormat(operation) };
        } else if (op === 'rename_series') {
            const nextName = String(valueAt(operation, 'name', 'new_name', 'value') || '').trim();
            if (!nextName || nextName.length > 80 || /[<>\r\n\0]/.test(nextName)) {
                throw new ChartEditOperationError('invalid_series_name');
            }
            const matches = targetSeries(config, operation.target, chartKind);
            if (matches.length !== 1) throw new ChartEditOperationError('ambiguous_series_target');
            const beforeRename = config;
            const oldName = String(matches[0].series?.name || '');
            config = updateConfigSeries(config, matches, (series) => ({ ...series, name: nextName }));
            overrides = remapIdentityOverrides(
                overrides,
                beforeRename,
                config,
                matches.map(({ index }) => index),
            );
            if (oldName && config.legend?.selected && oldName in config.legend.selected) {
                const selected = { ...config.legend.selected };
                selected[nextName] = selected[oldName];
                delete selected[oldName];
                config = { ...config, legend: { ...config.legend, selected } };
            }
        } else if (op === 'hide_series') {
            const hidden = operation.hidden === undefined ? true : ensureBoolean(operation.hidden);
            const matches = targetSeries(config, operation.target, chartKind);
            const names = matches.map(({ series }) => String(series?.name || '')).filter(Boolean);
            if (!names.length) throw new ChartEditOperationError('unnamed_series');
            const legend = { ...object(config.legend), selected: { ...object(config.legend?.selected) } };
            names.forEach((name) => { legend.selected[name] = !hidden; });
            config = { ...config, legend };
        } else if (op === 'add_overlay') {
            const overlay = normalizeOverlay(operation);
            const duplicate = overlays.some((item) => (
                (overlay.id && item?.id === overlay.id)
                || (!overlay.id && item?.operator === overlay.operator
                    && item?.source_column === overlay.source_column)
            ));
            if (!duplicate) overlays.push(overlay);
        } else if (op === 'remove_overlay') {
            const id = valueAt(operation, 'id', 'overlay_id');
            const operator = operation.operator;
            const label = operation.label;
            if (!id && !operator && !label) throw new ChartEditOperationError('invalid_overlay');
            const previousLength = overlays.length;
            overlays = overlays.filter((item) => (
                id ? String(item?.id || '') !== String(id)
                    : !((!operator || item?.operator === operator)
                        && (!label || item?.label === label))
            ));
            if (overlays.length === previousLength) {
                throw new ChartEditOperationError('overlay_not_found');
            }
        } else if (op === 'set_sort') {
            ensureSql(chartKind);
            const direction = String(valueAt(operation, 'direction', 'value') || '').toLowerCase();
            if (!['desc', 'descending', 'none', 'off', 'original', 'asc', 'ascending'].includes(direction)) {
                throw new ChartEditOperationError('invalid_sort');
            }
            view.toggles.sortDesc = direction === 'desc' || direction === 'descending';
            view.toggles.sortDirection = direction === 'asc' || direction === 'ascending'
                ? 'asc'
                : null;
        } else if (op === 'set_chart_type') {
            ensureSql(chartKind);
            const type = requestedChartType(operation);
            if (!chartTypeAppliesLocally(operation, config)) {
                throw new ChartEditOperationError('incompatible_chart_type');
            }
            const visible = array(config.series).filter((series) => !series?.__derived);
            const matches = visible.map((series) => ({
                series,
                index: config.series.indexOf(series),
            }));
            config = updateConfigSeries(config, matches, (series) => {
                const next = { ...series, type: type === 'bar' ? 'bar' : 'line' };
                if (type === 'area') next.areaStyle = { ...object(next.areaStyle) };
                else if (series.type !== 'line' || type === 'bar') delete next.areaStyle;
                return next;
            });
            spec = spec && typeof spec === 'object'
                ? { ...spec, chart_type: type === 'area' ? 'line' : type }
                : spec;
        } else if (op === 'set_stack') {
            ensureSql(chartKind);
            const matches = targetSeries(config, operation.target, chartKind);
            if (matches.some(({ series }) => !['bar', 'line'].includes(series?.type))) {
                throw new ChartEditOperationError('incompatible_stack');
            }
            const stacked = operation.stacked !== undefined
                ? operation.stacked
                : valueAt(operation, 'stack', 'value');
            if (typeof stacked !== 'boolean') {
                throw new ChartEditOperationError('invalid_stack');
            }
            config = updateConfigSeries(config, matches, (series) => {
                if (!stacked) delete series.stack;
                else series.stack = '__jeen_stack__';
                return series;
            });
        } else if (op === 'set_binding') {
            ensureSql(chartKind);
            // Rebinding requires a deterministic local chart builder. This
            // frontend deliberately has no SQL/connector fallback, so reject
            // instead of guessing or accepting LLM-provided values.
            throw new ChartEditOperationError('binding_unsupported');
        } else if (op === 'scenario_set_point' || op === 'scenario_scale' || op === 'scenario_shift') {
            ensureSql(chartKind);
            // Scenarios are derived copies; the real series stays untouched.
            const scenario = scenarioSpecFromOperation(operation, config);
            const active = overlays.filter(isScenarioSpec);
            if (active.some((item) => item.label === scenario.label)) {
                overlays = overlays.map((item) => (
                    isScenarioSpec(item) && item.label === scenario.label ? scenario : item
                ));
            } else {
                if (active.length >= MAX_SCENARIOS) throw new ChartEditOperationError('too_many_scenarios');
                overlays.push(scenario);
            }
        } else if (op === 'scenario_clear') {
            ensureSql(chartKind);
            const label = operation.label ? String(operation.label).trim() : '';
            const before = overlays.length;
            overlays = overlays.filter((item) => !isScenarioSpec(item) || (label && item.label !== label));
            if (overlays.length === before) throw new ChartEditOperationError('overlay_not_found');
        } else if (op === 'add_reference_line') {
            const line = normalizeReferenceLine(operation, config);
            const lines = annotations.referenceLines.filter((item) => item.label !== line.label);
            if (lines.length >= MAX_REFERENCE_LINES) throw new ChartEditOperationError('too_many_reference_lines');
            annotations = { ...annotations, referenceLines: [...lines, line] };
        } else if (op === 'remove_reference_line') {
            const label = operation.label ? String(operation.label).trim() : '';
            const before = annotations.referenceLines.length;
            const lines = annotations.referenceLines.filter((item) => label && item.label !== label);
            if (lines.length === before) throw new ChartEditOperationError('overlay_not_found');
            annotations = { ...annotations, referenceLines: lines };
        } else if (op === 'highlight_points') {
            const highlight = normalizeHighlight(operation, config);
            annotations = { ...annotations, highlights: [...annotations.highlights, highlight] };
        } else if (op === 'clear_highlights') {
            annotations = { ...annotations, highlights: [] };
        } else {
            throw new ChartEditOperationError('unsupported_operation');
        }
    }

    assertProtectedState(protectedBefore, config);
    candidate.replaceWorking({
        config,
        spec,
        derivedSpecs: overlays,
        styleOverrides: overrides,
        annotations,
    });
    candidate.replaceView(view);
    return candidate;
}

/**
 * Last-mile ML presentation guard. Quick toggles are intentionally generic;
 * this keeps hidden band-helper labels off without changing canonical state.
 */
export function guardMlPresentation(config, chartKind) {
    if (chartKind === 'sql' || !Array.isArray(config?.series)) return config;
    return {
        ...config,
        series: config.series.map((series) => {
            if (!ML_LABEL_HIDDEN_ROLES.has(series?.jeenRole)) return series;
            return {
                ...series,
                label: { ...object(series.label), show: false },
            };
        }),
    };
}

function sortablePoint(point) {
    const raw = rawPointValue(point);
    const value = Array.isArray(raw) ? raw[raw.length - 1] : raw;
    const number = finiteNumber(value);
    return number === null ? -Infinity : number;
}

/** Stable presentation-only category sort; canonical series data is untouched. */
export function applyLocalSort(config, direction) {
    if (!['asc', 'desc'].includes(direction) || !config || typeof config !== 'object') return config;
    const axisEntries = [];
    for (const key of ['xAxis', 'yAxis']) {
        const values = Array.isArray(config[key]) ? config[key] : config[key] ? [config[key]] : [];
        values.forEach((axis, index) => {
            if (axis?.type === 'category' && Array.isArray(axis.data)) {
                axisEntries.push({ key, index, axis, originalWasArray: Array.isArray(config[key]) });
            }
        });
    }
    const category = axisEntries[0];
    if (!category || !category.axis.data.length) return config;
    const count = category.axis.data.length;
    const source = array(config.series).find((series) => Array.isArray(series?.data)
        && series.data.length === count && !ML_HELPER_ROLES.has(series.jeenRole));
    if (!source) return config;
    const order = source.data.map((point, index) => ({
        index,
        value: sortablePoint(point),
    }));
    order.sort((left, right) => (
        direction === 'asc'
            ? (left.value - right.value) || (left.index - right.index)
            : (right.value - left.value) || (left.index - right.index)
    ));
    const out = { ...config };
    const axes = category.originalWasArray ? config[category.key].slice() : [config[category.key]];
    axes[category.index] = {
        ...axes[category.index],
        data: order.map(({ index }) => category.axis.data[index]),
    };
    out[category.key] = category.originalWasArray ? axes : axes[0];
    out.series = array(config.series).map((series) => (
        Array.isArray(series?.data) && series.data.length === count
            ? { ...series, data: order.map(({ index }) => series.data[index]) }
            : series
    ));
    return out;
}
