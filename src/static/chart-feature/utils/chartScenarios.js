/**
 * What-if scenarios and annotations for the chart mini chat.
 *
 * A scenario is a derived series: a labelled, dashed copy of a real series
 * with one rule applied (a point set to a value, a percentage/factor scale, or
 * a constant shift). The real series is never modified, so every safety
 * invariant of the chart session ("data is immutable") still holds. Specs live
 * in `working.derivedSpecs` with `kind: 'scenario'` and are computed here at
 * render time from the current chart config, exactly like the moving-average
 * overlays in chartOperators.js.
 *
 * Annotations (reference lines, highlighted points) are presentation-only and
 * are applied to a cloned display config just before ECharts renders.
 *
 * No DOM access: shared by the browser and the node tests.
 * @module chartScenarios
 */

export const MAX_SCENARIOS = 2;
export const MAX_HIGHLIGHT_CATEGORIES = 12;
const SCENARIO_RULES = new Set(['set_point', 'scale', 'shift']);
const ML_HELPER_ROLES = new Set([
    'interval_base', 'interval_bound', 'lower_bound', 'upper_bound', 'helper', 'interval',
]);
const DEFAULT_HIGHLIGHT = '#d4574a';

function clone(value) {
    if (value === undefined) return undefined;
    if (value === null) return null;
    try { return structuredClone(value); } catch (_) { return JSON.parse(JSON.stringify(value)); }
}

function toNumber(cell) {
    if (cell === null || cell === undefined || cell === '') return null;
    if (typeof cell === 'number') return Number.isFinite(cell) ? cell : null;
    const n = Number(String(cell).replace(/[$€£¥₪,\s]/g, ''));
    return Number.isFinite(n) ? n : null;
}

function rawPoint(point) {
    return point && typeof point === 'object' && !Array.isArray(point) && 'value' in point
        ? point.value
        : point;
}

function pointNumber(point) {
    const raw = rawPoint(point);
    return Array.isArray(raw) ? toNumber(raw[raw.length - 1]) : toNumber(raw);
}

/** Rebuild a data point with a new numeric value, keeping its shape ([x, y] or {value}). */
function withValue(point, value) {
    const raw = rawPoint(point);
    const nextRaw = Array.isArray(raw) ? [...raw.slice(0, -1), value] : value;
    if (point && typeof point === 'object' && !Array.isArray(point) && 'value' in point) {
        // Per-point styling belongs to the real series; the copy gets its own.
        const rest = { ...point };
        delete rest.itemStyle;
        delete rest.label;
        return { ...rest, value: nextRaw };
    }
    return nextRaw;
}

export function categoryLabel(value) {
    if (value && typeof value === 'object' && !Array.isArray(value) && 'value' in value) {
        return String(value.value ?? '');
    }
    return value === null || value === undefined ? '' : String(value);
}

function sameLabel(a, b) {
    return String(a ?? '').trim().toLowerCase() === String(b ?? '').trim().toLowerCase();
}

export function isScenarioSpec(spec) {
    return Boolean(spec && typeof spec === 'object' && spec.kind === 'scenario');
}

export function isScenarioSeries(series) {
    return Boolean(series?.__derived && series.__derived.kind === 'scenario');
}

/** Category axis of a cartesian config (x for vertical, y for horizontal bars). */
export function categoryAxis(config) {
    for (const key of ['xAxis', 'yAxis']) {
        const axes = Array.isArray(config?.[key]) ? config[key] : config?.[key] ? [config[key]] : [];
        const axis = axes.find((item) => item?.type === 'category' && Array.isArray(item.data));
        if (axis) return { key, axis, labels: axis.data.map(categoryLabel) };
    }
    return null;
}

/** Labels of the points of one series (pie names, or the category axis). */
export function seriesCategories(series, config) {
    if (series?.type === 'pie') {
        return (series.data || []).map((point) => String(point?.name ?? ''));
    }
    const axis = categoryAxis(config);
    if (axis) return axis.labels;
    return (series?.data || []).map((point) => {
        const raw = rawPoint(point);
        return Array.isArray(raw) && raw.length >= 2 ? String(raw[0]) : '';
    });
}

function targetString(target) {
    if (typeof target === 'string') return target.trim() || 'all';
    if (target && typeof target === 'object') {
        if (target.role != null) return `role:${target.role}`;
        if (target.name != null) return `name:${target.name}`;
        if (target.id != null) return `id:${target.id}`;
    }
    return 'all';
}

/** Real (non-derived, non-helper) series matched by a chart-edit target. */
export function resolveTargetSeries(config, target) {
    const all = Array.isArray(config?.series) ? config.series : [];
    const real = all
        .map((series, index) => ({ series, index }))
        .filter(({ series }) => series && !series.__derived && Array.isArray(series.data)
            && !ML_HELPER_ROLES.has(series.jeenRole));
    const raw = targetString(target);
    if (raw === 'all') return real;
    const separator = raw.indexOf(':');
    if (separator <= 0) return [];
    const kind = raw.slice(0, separator);
    const expected = raw.slice(separator + 1);
    return real.filter(({ series }) => {
        if (kind === 'role') return sameLabel(series.jeenRole, expected);
        if (kind === 'name') return sameLabel(series.name, expected);
        if (kind === 'id') return String(series.id ?? '') === expected;
        return false;
    });
}

/**
 * Normalise an LLM/matcher scenario operation into a stored derived spec.
 * Throws {code} on anything unusable; the caller maps codes to messages.
 */
export function scenarioSpecFromOperation(operation, config) {
    const rule = String(operation?.op || '').replace(/^scenario_/, '');
    if (!SCENARIO_RULES.has(rule)) throw scenarioError('invalid_scenario');
    const matches = resolveTargetSeries(config, operation.target);
    if (!matches.length) throw scenarioError('target_not_found');
    const spec = {
        kind: 'scenario',
        scenario: rule,
        target: targetString(operation.target),
        label: safeLabel(operation.label) || defaultLabel(rule, operation),
    };
    const first = matches[0].series;
    const categories = seriesCategories(first, config);
    const findCategory = (value) => {
        const index = categories.findIndex((label) => sameLabel(label, value));
        if (index < 0) throw scenarioError('unknown_category');
        return categories[index];
    };
    if (rule === 'set_point') {
        const value = toNumber(operation.value);
        if (value === null) throw scenarioError('invalid_scenario');
        spec.category = findCategory(operation.category);
        spec.value = value;
    } else if (rule === 'scale') {
        if (operation.factor !== undefined && operation.factor !== null) {
            const factor = toNumber(operation.factor);
            if (factor === null || factor < 0 || factor > 20) throw scenarioError('invalid_scenario');
            spec.factor = factor;
        } else {
            const percent = toNumber(operation.percent);
            if (percent === null || percent < -100 || percent > 1000) throw scenarioError('invalid_scenario');
            spec.percent = percent;
        }
        if (operation.from_category) spec.from_category = findCategory(operation.from_category);
    } else if (rule === 'shift') {
        const delta = toNumber(operation.delta);
        if (delta === null) throw scenarioError('invalid_scenario');
        spec.delta = delta;
        if (operation.from_category) spec.from_category = findCategory(operation.from_category);
    }
    return spec;
}

function scenarioError(code) {
    const error = new Error(code);
    error.name = 'ChartEditOperationError';
    error.code = code;
    return error;
}

function safeLabel(value) {
    const text = String(value ?? '').trim();
    if (!text || text.length > 80 || /[<>\r\n\0]/.test(text)) return '';
    return text;
}

function formatNumber(value) {
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${trim(value / 1e9)}B`;
    if (abs >= 1e6) return `${trim(value / 1e6)}M`;
    if (abs >= 1e3) return `${trim(value / 1e3)}K`;
    return trim(value);
}

function trim(value) {
    return String(Math.round(value * 100) / 100);
}

function defaultLabel(rule, operation) {
    if (rule === 'set_point') return `${operation.category} = ${formatNumber(toNumber(operation.value) ?? 0)}`;
    if (rule === 'scale') {
        if (operation.factor !== undefined && operation.factor !== null) return `×${trim(toNumber(operation.factor) ?? 1)}`;
        const percent = toNumber(operation.percent) ?? 0;
        return `${percent >= 0 ? '+' : ''}${trim(percent)}%${operation.from_category ? ` from ${operation.from_category}` : ''}`;
    }
    const delta = toNumber(operation.delta) ?? 0;
    return `${delta >= 0 ? '+' : ''}${formatNumber(delta)}${operation.from_category ? ` from ${operation.from_category}` : ''}`;
}

/** Apply one scenario rule to a value array (nulls stay null). */
export function applyScenarioRule(spec, values, categories) {
    const out = values.slice();
    const startIndex = spec.from_category
        ? Math.max(0, categories.findIndex((label) => sameLabel(label, spec.from_category)))
        : 0;
    if (spec.scenario === 'set_point') {
        const index = categories.findIndex((label) => sameLabel(label, spec.category));
        if (index < 0) return null;
        out[index] = spec.value;
        return out;
    }
    const multiplier = spec.scenario === 'scale'
        ? (spec.factor !== undefined && spec.factor !== null ? spec.factor : 1 + (spec.percent || 0) / 100)
        : null;
    for (let i = startIndex; i < out.length; i++) {
        if (out[i] === null || out[i] === undefined) continue;
        out[i] = spec.scenario === 'scale'
            ? Math.round(out[i] * multiplier * 1e6) / 1e6
            : Math.round((out[i] + (spec.delta || 0)) * 1e6) / 1e6;
    }
    return out;
}

/**
 * Build the dashed scenario copies for one spec. Returns [] when the source
 * series or category is no longer present (e.g. after a rebuild).
 */
export function buildScenarioSeries(spec, config, { prefix = 'Scenario' } = {}) {
    if (!isScenarioSpec(spec)) return [];
    const matches = resolveTargetSeries(config, spec.target);
    const out = [];
    for (const { series } of matches) {
        const categories = seriesCategories(series, config);
        const values = series.data.map(pointNumber);
        const next = applyScenarioRule(spec, values, categories);
        if (!next) continue;
        const data = series.data.map((point, index) => (
            next[index] === null || next[index] === undefined ? point : withValue(point, next[index])
        ));
        const name = matches.length > 1
            ? `${prefix}: ${spec.label} (${series.name || ''})`.trim()
            : `${prefix}: ${spec.label}`;
        const derived = { kind: 'scenario', operator: spec.scenario, sourceColumn: String(series.name || ''), label: spec.label, params: clone(spec) };
        if (series.type === 'pie') {
            const inner = Array.isArray(series.radius) ? series.radius : ['0%', series.radius || '62%'];
            out.push({
                name,
                type: 'pie',
                radius: [inner[1], `${Math.min(parseFloat(inner[1]) + 14, 92)}%`],
                center: series.center,
                data: data.map((point) => ({ name: point?.name, value: rawPoint(point) })),
                label: { show: false },
                itemStyle: { opacity: 0.55, borderType: 'dashed', borderWidth: 1.5, borderColor: '#ffffff' },
                emphasis: { scale: false },
                z: 6,
                __derived: derived,
            });
            continue;
        }
        const type = series.type === 'bar' ? 'bar' : 'line';
        out.push({
            name,
            type,
            data,
            ...(Number.isInteger(series.xAxisIndex) ? { xAxisIndex: series.xAxisIndex } : {}),
            ...(Number.isInteger(series.yAxisIndex) ? { yAxisIndex: series.yAxisIndex } : {}),
            ...(series.stack ? { stack: `${series.stack}__scenario` } : {}),
            itemStyle: { opacity: 0.55, borderType: 'dashed', borderWidth: 1.5 },
            lineStyle: { type: 'dashed', width: 2 },
            symbol: 'circle',
            symbolSize: 6,
            smooth: Boolean(series.smooth),
            z: 6,
            __derived: derived,
        });
    }
    return out;
}

// ─────────────────────────────────────────────────────────────────────────
// Annotations: reference lines and highlighted points
// ─────────────────────────────────────────────────────────────────────────

export function emptyAnnotations() {
    return { referenceLines: [], highlights: [] };
}

export function normalizeAnnotations(value) {
    const source = value && typeof value === 'object' ? value : {};
    return {
        referenceLines: Array.isArray(source.referenceLines) ? clone(source.referenceLines) : [],
        highlights: Array.isArray(source.highlights) ? clone(source.highlights) : [],
    };
}

export function hasAnnotations(annotations) {
    return Boolean(annotations?.referenceLines?.length || annotations?.highlights?.length);
}

function stat(values, kind) {
    const numbers = values.filter((value) => value !== null && value !== undefined);
    if (!numbers.length) return null;
    if (kind === 'min') return Math.min(...numbers);
    if (kind === 'max') return Math.max(...numbers);
    if (kind === 'median') {
        const sorted = numbers.slice().sort((a, b) => a - b);
        const mid = Math.floor(sorted.length / 2);
        return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    }
    return numbers.reduce((sum, value) => sum + value, 0) / numbers.length;
}

function matchesPredicate(value, predicate) {
    if (value === null || value === undefined || !predicate) return false;
    const { op, value: a, value2: b } = predicate;
    if (op === 'lt') return value < a;
    if (op === 'lte') return value <= a;
    if (op === 'gt') return value > a;
    if (op === 'gte') return value >= a;
    if (op === 'eq') return value === a;
    if (op === 'between') return value >= Math.min(a, b) && value <= Math.max(a, b);
    return false;
}

/**
 * Presentation pass: draws reference lines (markLine) and highlighted points
 * onto a copy of the display config. The working config is never touched.
 */
export function applyAnnotations(config, annotations) {
    if (!config || !hasAnnotations(annotations)) return config;
    const out = { ...config, series: (config.series || []).map((series) => ({ ...series })) };
    const axis = categoryAxis(out);
    const real = out.series.filter((series) => series && !series.__derived && Array.isArray(series.data)
        && !ML_HELPER_ROLES.has(series.jeenRole));
    const isCartesian = Boolean(axis);

    for (const line of annotations.referenceLines || []) {
        if (!isCartesian) continue;
        const targets = line.target ? resolveTargetSeries(out, line.target).map(({ series }) => series) : real.slice(0, 1);
        const host = targets[0] || real[0];
        if (!host || host.type === 'pie') continue;
        let value = line.value;
        if (line.stat) value = stat(host.data.map(pointNumber), line.stat);
        if (value === null || value === undefined || !Number.isFinite(Number(value))) continue;
        const valueAxis = line.axis === 'x'
            ? (axis.key === 'xAxis' ? 'xAxis' : 'yAxis')
            : (axis.key === 'xAxis' ? 'yAxis' : 'xAxis');
        const existing = host.markLine && Array.isArray(host.markLine.data) ? host.markLine.data : [];
        host.markLine = {
            ...(host.markLine || {}),
            silent: true,
            symbol: 'none',
            lineStyle: { type: 'dashed', width: 1.5, ...(host.markLine?.lineStyle || {}) },
            label: { formatter: '{b}', position: 'insideEndTop', ...(host.markLine?.label || {}) },
            data: existing.concat([{ name: line.label, [valueAxis]: Number(value), lineStyle: { type: 'dashed' } }]),
        };
    }

    for (const highlight of annotations.highlights || []) {
        const targets = resolveTargetSeries(out, highlight.target).map(({ series }) => series);
        const color = highlight.color || DEFAULT_HIGHLIGHT;
        for (const series of targets) {
            const categories = seriesCategories(series, out);
            const wanted = new Set((highlight.categories || []).map((label) => String(label).trim().toLowerCase()));
            const hit = (point, index) => (
                wanted.size
                    ? wanted.has(String(categories[index] ?? '').trim().toLowerCase())
                    : matchesPredicate(pointNumber(point), highlight.predicate)
            );
            const indexes = series.data.map((point, index) => (hit(point, index) ? index : -1)).filter((index) => index >= 0);
            if (!indexes.length) continue;
            if (series.type === 'line') {
                const existing = series.markPoint && Array.isArray(series.markPoint.data) ? series.markPoint.data : [];
                series.markPoint = {
                    ...(series.markPoint || {}),
                    symbol: 'pin',
                    symbolSize: 34,
                    itemStyle: { color },
                    label: { show: false },
                    data: existing.concat(indexes.map((index) => ({
                        name: highlight.label || categories[index],
                        coord: axis?.key === 'yAxis' ? [pointNumber(series.data[index]), categories[index]] : [categories[index], pointNumber(series.data[index])],
                    }))),
                };
                continue;
            }
            const marked = new Set(indexes);
            series.data = series.data.map((point, index) => {
                if (!marked.has(index)) return point;
                const base = point && typeof point === 'object' && !Array.isArray(point) ? { ...point } : { value: point };
                return {
                    ...base,
                    itemStyle: { ...(base.itemStyle || {}), color, borderColor: color, borderWidth: 2 },
                    label: { ...(base.label || {}), show: true },
                };
            });
        }
    }
    return out;
}

export const __test__ = { stat, matchesPredicate, withValue, defaultLabel };
