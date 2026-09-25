/**
 * Chart operators
 *
 * Pure functions that compute derived series (moving average, trend line,
 * cumulative sum, ...) from the existing query result rows.
 *
 * Why these run client-side:
 *   - The LLM should describe INTENT (operator name + params), not invent
 *     numeric arrays. Computing locally avoids hallucinated values.
 *   - The query result already lives on the page; the math is cheap.
 *
 * @module chartOperators
 */

import { buildScenarioSeries, isScenarioSeries, isScenarioSpec } from './chartScenarios.js?v=1';

const ALLOWED_OPERATORS = new Set([
    'moving_avg',
    'cumulative_sum',
    'percent_change',
    'linear_trend',
    'normalize_0_1',
    'log_scale',
]);

/**
 * Coerce a cell to a finite number, or null if not numeric.
 * Strips currency symbols and thousands separators.
 *
 * @param {*} cell
 * @returns {number|null}
 */
function toNumber(cell) {
    if (cell === null || cell === undefined || cell === '') return null;
    if (typeof cell === 'number') return Number.isFinite(cell) ? cell : null;
    const cleaned = String(cell).replace(/[$€£¥,\s]/g, '');
    const n = Number(cleaned);
    return Number.isFinite(n) ? n : null;
}

/**
 * Pull a numeric column out of the result rows.
 *
 * @param {{columns: string[], data?: any[][], rows?: any[][]}} results
 * @param {string} columnName
 * @returns {Array<number|null>}
 */
function extractColumn(results, columnName) {
    if (!results || !Array.isArray(results.columns)) return [];
    const idx = results.columns.indexOf(columnName);
    if (idx === -1) return [];
    const rows = results.data || results.rows || [];
    return rows.map(row => {
        const cell = Array.isArray(row) ? row[idx] : row[columnName];
        return toNumber(cell);
    });
}

function pointNumber(point) {
    const raw = point && typeof point === 'object' && !Array.isArray(point) && 'value' in point
        ? point.value
        : point;
    if (Array.isArray(raw)) return toNumber(raw[raw.length - 1]);
    return toNumber(raw);
}

/**
 * Prefer the already aligned/aggregated values in the semantic chart option.
 * Raw result-row order can differ after grouping, category completion or a
 * deterministic rebuild, so overlays must follow the chart series whenever
 * one can be identified.
 */
function extractChartSeries(config, spec) {
    const series = Array.isArray(config?.series)
        ? config.series.filter((item) => item && !item.__derived && Array.isArray(item.data))
        : [];
    if (!series.length) return null;
    const requested = String(spec?.source_series || spec?.source_column || '').trim();
    const source = (
        (requested && series.find((item) => String(item.name || '') === requested))
        || series.find((item) => item.jeenRole === 'actual')
        || series.find((item) => item.type !== 'pie' && item.data.some((point) => pointNumber(point) !== null))
    );
    if (!source || source.type === 'pie') return null;
    const values = source.data.map(pointNumber);
    if (!values.length || values.every((value) => value === null)) return null;
    const xValues = source.data.map((point) => {
        const raw = point && typeof point === 'object' && !Array.isArray(point) && 'value' in point
            ? point.value
            : point;
        return Array.isArray(raw) && raw.length >= 2 ? raw[0] : undefined;
    });
    return {
        values,
        sourceName: requested || String(source.name || 'series'),
        source,
        xValues: xValues.every((value) => value !== undefined) ? xValues : null,
    };
}

/**
 * Choose the first numeric column in the dataset.
 * Used as a fallback when the LLM's source_column is missing or invalid.
 *
 * @param {{columns: string[], data?: any[][], rows?: any[][]}} results
 * @returns {string|null}
 */
function inferNumericColumn(results) {
    if (!results || !Array.isArray(results.columns)) return null;
    const rows = results.data || results.rows || [];
    if (rows.length === 0) return null;
    const sampleSize = Math.min(rows.length, 50);
    for (const col of results.columns) {
        const idx = results.columns.indexOf(col);
        let numeric = 0;
        let nonNull = 0;
        for (let r = 0; r < sampleSize; r++) {
            const cell = Array.isArray(rows[r]) ? rows[r][idx] : rows[r][col];
            if (cell === null || cell === undefined || cell === '') continue;
            nonNull++;
            if (toNumber(cell) !== null) numeric++;
        }
        if (nonNull > 0 && numeric / nonNull >= 0.7) return col;
    }
    return null;
}

// ─────────────────────────────────────────────────────────────────────────
// Operators
// Each returns Array<number|null>, same length as the input.
// ─────────────────────────────────────────────────────────────────────────

function movingAverage(values, window) {
    const w = Math.max(1, Math.floor(window || 3));
    const out = new Array(values.length).fill(null);
    let sum = 0;
    let count = 0;
    const buf = [];
    for (let i = 0; i < values.length; i++) {
        const v = values[i];
        buf.push(v);
        if (v !== null) { sum += v; count += 1; }
        if (buf.length > w) {
            const dropped = buf.shift();
            if (dropped !== null) { sum -= dropped; count -= 1; }
        }
        if (i >= w - 1 && count > 0) out[i] = +(sum / count).toFixed(4);
    }
    return out;
}

function cumulativeSum(values) {
    const out = new Array(values.length).fill(null);
    let acc = 0;
    let seen = false;
    for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v === null) {
            out[i] = seen ? +acc.toFixed(4) : null;
            continue;
        }
        acc += v;
        seen = true;
        out[i] = +acc.toFixed(4);
    }
    return out;
}

function percentChange(values) {
    const out = new Array(values.length).fill(null);
    let prev = null;
    for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v === null || prev === null || prev === 0) {
            out[i] = null;
        } else {
            out[i] = +(((v - prev) / prev) * 100).toFixed(4);
        }
        if (v !== null) prev = v;
    }
    return out;
}

function linearTrend(values) {
    // Ordinary least squares y = a + b*x, where x is the index.
    const xs = [];
    const ys = [];
    for (let i = 0; i < values.length; i++) {
        if (values[i] !== null) {
            xs.push(i);
            ys.push(values[i]);
        }
    }
    const n = xs.length;
    if (n < 2) return new Array(values.length).fill(null);
    const meanX = xs.reduce((a, b) => a + b, 0) / n;
    const meanY = ys.reduce((a, b) => a + b, 0) / n;
    let num = 0;
    let den = 0;
    for (let i = 0; i < n; i++) {
        num += (xs[i] - meanX) * (ys[i] - meanY);
        den += (xs[i] - meanX) ** 2;
    }
    const slope = den === 0 ? 0 : num / den;
    const intercept = meanY - slope * meanX;
    return values.map((_, i) => +(intercept + slope * i).toFixed(4));
}

function normalize01(values) {
    const numeric = values.filter(v => v !== null);
    if (numeric.length === 0) return values.slice();
    const min = Math.min(...numeric);
    const max = Math.max(...numeric);
    const span = max - min;
    if (span === 0) return values.map(v => (v === null ? null : 0));
    return values.map(v => (v === null ? null : +((v - min) / span).toFixed(4)));
}

function logScale(values) {
    return values.map(v => {
        if (v === null) return null;
        if (v <= 0) return null; // log undefined
        return +Math.log10(v).toFixed(4);
    });
}

const OPERATORS = {
    moving_avg: (values, params) => movingAverage(values, params && params.window),
    cumulative_sum: (values) => cumulativeSum(values),
    percent_change: (values) => percentChange(values),
    linear_trend: (values) => linearTrend(values),
    normalize_0_1: (values) => normalize01(values),
    log_scale: (values) => logScale(values),
};

/**
 * Compute one derived ECharts series from a spec + the underlying result.
 * Returns null when the spec is invalid or the column is not numeric.
 *
 * @param {{operator: string, source_column?: string, params?: object, label?: string}} spec
 * @param {{columns: string[], data?: any[][], rows?: any[][]}} results
 * @returns {object|null} ECharts series object, or null
 */
export function buildDerivedSeries(spec, results, config = null) {
    if (!spec || typeof spec !== 'object') return null;
    const operator = String(spec.operator || '').toLowerCase();
    if (!ALLOWED_OPERATORS.has(operator)) return null;
    const fn = OPERATORS[operator];
    if (!fn) return null;

    const aligned = extractChartSeries(config, spec);
    const sourceColumn = aligned?.sourceName || spec.source_column || inferNumericColumn(results);
    if (!sourceColumn) return null;
    const values = aligned?.values || extractColumn(results, sourceColumn);
    if (values.length === 0) return null;
    if (values.every(v => v === null)) return null;

    let computed;
    try {
        computed = fn(values, spec.params || {});
    } catch (e) {
        console.warn('[chartOperators] Operator failed:', operator, e);
        return null;
    }
    if (!Array.isArray(computed) || computed.length !== values.length) return null;

    const label = spec.label && String(spec.label).trim()
        ? String(spec.label).trim()
        : `${operator} (${sourceColumn})`;

    // log_scale and normalize_0_1 sit best on a separate Y axis.
    const wantsAuxAxis = operator === 'log_scale' || operator === 'normalize_0_1';

    const derivedData = aligned?.xValues
        ? computed.map((value, index) => [aligned.xValues[index], value])
        : computed;
    return {
        name: label,
        type: 'line',
        data: derivedData,
        smooth: true,
        symbol: 'none',
        lineStyle: { type: 'dashed', width: 2 },
        emphasis: { focus: 'series' },
        z: 5,
        ...(wantsAuxAxis ? { yAxisIndex: 1 } : {}),
        __derived: { operator, sourceColumn, params: spec.params || {} },
    };
}

/**
 * Append derived series to an ECharts config without mutating the input.
 * If any operator wants an auxiliary axis, a second yAxis is added.
 *
 * @param {object} config - ECharts option object
 * @param {Array} specs - array of derived-series specs from the LLM
 * @param {object} results - query results (with columns + rows)
 * @returns {{config: object, applied: number}}
 */
export function applyDerivedSeries(config, specs, results) {
    if (!config || typeof config !== 'object') {
        return { config, applied: 0 };
    }
    if (!Array.isArray(specs) || specs.length === 0) {
        return { config, applied: 0 };
    }
    // Shallow clone the parts we mutate.
    const next = { ...config };
    next.series = Array.isArray(config.series) ? config.series.slice() : [];

    let needsAuxAxis = false;
    let applied = 0;
    for (const spec of specs) {
        if (isScenarioSpec(spec)) {
            // What-if copies are computed from the chart's own series so they
            // stay aligned with whatever the real series currently shows.
            for (const scenario of buildScenarioSeries(spec, next)) {
                const dup = next.series.some((s) => isScenarioSeries(s) && s.name === scenario.name);
                if (dup) continue;
                next.series.push(scenario);
                applied += 1;
            }
            continue;
        }
        const series = buildDerivedSeries(spec, results, next);
        if (!series) continue;
        if (series.yAxisIndex === 1) needsAuxAxis = true;
        // Avoid duplicating an identical derived series the user already has.
        const dup = next.series.some(s =>
            s && s.__derived &&
            s.__derived.operator === series.__derived.operator &&
            s.__derived.sourceColumn === series.__derived.sourceColumn
        );
        if (dup) continue;
        next.series.push(series);
        applied += 1;
    }

    if (needsAuxAxis) {
        const yAxis = config.yAxis;
        if (Array.isArray(yAxis)) {
            // Already a list — only add if there isn't a second one.
            if (yAxis.length < 2) next.yAxis = yAxis.concat([{ type: 'value', show: false }]);
            else next.yAxis = yAxis;
        } else if (yAxis && typeof yAxis === 'object') {
            next.yAxis = [yAxis, { type: 'value', show: false }];
        }
    }

    return { config: next, applied };
}

/**
 * Strip every derived series previously added by `applyDerivedSeries`.
 * Used when the user types a fresh edit so overlays don't accumulate.
 *
 * @param {object} config
 * @returns {object}
 */
export function stripDerivedSeries(config) {
    if (!config || !Array.isArray(config.series)) return config;
    const next = { ...config };
    next.series = config.series.filter(s => !(s && s.__derived));
    return next;
}
