/**
 * Context-aware "Refine this chart" suggestions.
 *
 * Pure function: given the current ECharts config and the result set, return a
 * short, ordered list of refinement prompts that actually make sense for THIS
 * chart and THIS data. e.g. never offer "add a 3-month moving average" unless
 * the data has a time/ordered dimension; never offer "stack the bars" unless
 * there are multiple bar series.
 *
 * Kept dependency-free so it's trivially unit-testable.
 *
 * @module chartSuggestions
 */

const TIME_NAME_RE = /(date|time|year|month|day|quarter|qtr|week|period|fiscal)/i;
const MONEY_NAME_RE = /(revenue|sales|price|amount|cost|profit|income|spend|gmv|usd|eur|gbp|payment|charge|fee)/i;
const PCT_NAME_RE = /(pct|percent|ratio|rate|change|growth|margin|share|yoy|mom|qoq)/i;
const MONTHS = new Set([
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
    'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september',
    'october', 'november', 'december',
]);

const DEFAULT_SUGGESTIONS = [
    'Show the data values',
    'Sort highest to lowest',
    'Use a different color palette',
];

function looksLikeDate(v) {
    const s = String(v).trim();
    // ISO-ish dates only; bare integers (e.g. 2006) are handled by column name.
    return /^\d{4}[-/]\d{1,2}([-/]\d{1,2})?/.test(s) || /^\d{4}-\d{2}-\d{2}T/.test(s);
}

function looksLikeMonths(values) {
    if (!Array.isArray(values) || !values.length) return false;
    let hits = 0;
    for (const v of values.slice(0, 12)) {
        if (MONTHS.has(String(v).trim().toLowerCase())) hits++;
    }
    return hits >= Math.min(2, values.length);
}

function detectColumns(columns, rows) {
    const sample = Array.isArray(rows) ? rows.slice(0, 25) : [];
    return (columns || []).map((name, idx) => {
        let nums = 0, dates = 0, nonNull = 0;
        for (const row of sample) {
            const v = Array.isArray(row) ? row[idx] : (row ? row[name] : undefined);
            if (v === null || v === undefined || v === '') continue;
            nonNull++;
            const cleaned = String(v).replace(/[$€£¥,\s%]/g, '');
            if (cleaned !== '' && isFinite(Number(cleaned))) nums++;
            else if (looksLikeDate(v)) dates++;
        }
        if (nonNull === 0) return { name, kind: 'category' };
        if (dates / nonNull >= 0.6) return { name, kind: 'date' };
        if (nums / nonNull >= 0.7) return { name, kind: 'numeric' };
        return { name, kind: 'category' };
    });
}

function categoryAxisData(config) {
    const pick = (ax) => {
        if (!ax) return null;
        if (Array.isArray(ax)) {
            for (const a of ax) { const r = pick(a); if (r) return r; }
            return null;
        }
        return ax.type === 'category' && Array.isArray(ax.data) ? ax.data : null;
    };
    return pick(config && config.xAxis) || pick(config && config.yAxis) || [];
}

function analyzeContext(config, results) {
    const series = Array.isArray(config && config.series) ? config.series : [];
    const types = series.map((s) => s && s.type).filter(Boolean);
    const typeSet = new Set(types);

    const cats = categoryAxisData(config);
    const pointCount = cats.length
        || (series[0] && Array.isArray(series[0].data) ? series[0].data.length : 0);

    const cols = detectColumns(
        results && results.columns,
        (results && (results.data || results.rows)) || []
    );
    const hasDate = cols.some((c) => c.kind === 'date');
    const numericCount = cols.filter((c) => c.kind === 'numeric').length;
    const hasTime = hasDate
        || cols.some((c) => TIME_NAME_RE.test(c.name))
        || looksLikeMonths(cats);

    return {
        hasBar: typeSet.has('bar'),
        hasLine: typeSet.has('line'),
        hasPie: typeSet.has('pie'),
        hasScatter: typeSet.has('scatter'),
        isCombo: typeSet.has('bar') && typeSet.has('line'),
        multiSeries: series.length > 1,
        stacked: series.some((s) => s && s.stack),
        labelsShown: series.some((s) => s && s.label && s.label.show === true),
        smooth: series.some((s) => s && s.smooth),
        isDonut: series.some((s) => s && s.type === 'pie' && Array.isArray(s.radius)),
        pointCount,
        numericCount,
        hasTime,
        hasMoney: cols.some((c) => MONEY_NAME_RE.test(c.name)),
        hasPct: cols.some((c) => PCT_NAME_RE.test(c.name)),
    };
}

/**
 * @param {object|null} config   current ECharts option
 * @param {object|null} results  { columns: string[], data|rows: any[] }
 * @param {number} [max=7]
 * @returns {string[]} ordered, de-duplicated refinement prompts
 */
export function buildChartSuggestions(config, results, max = 7) {
    if (!config || typeof config !== 'object' || !Array.isArray(config.series) || !config.series.length) {
        return DEFAULT_SUGGESTIONS.slice(0, max);
    }
    const ctx = analyzeContext(config, results);
    const out = [];
    const add = (s) => { if (s && !out.includes(s)) out.push(s); };

    // 1. Time / ordered-series overlays — only when there's a time dimension.
    if (ctx.hasTime && (ctx.hasLine || ctx.hasBar)) {
        add('Add a 3-month moving average');
        add('Show the running total');
        if (!ctx.hasScatter) add('Add a trend line');
    }

    // 2. Chart-type changes that fit the current encoding.
    if (ctx.hasBar && !ctx.hasLine) {
        if (ctx.hasTime) add('Switch to a line chart');
        if (ctx.pointCount > 12) add('Make it a horizontal bar chart');
        if (ctx.multiSeries && !ctx.stacked) add('Stack the bars');
    }
    if (ctx.hasLine && !ctx.hasBar) {
        add('Switch to a bar chart');
        if (!ctx.smooth) add('Smooth the line');
    }
    if (ctx.hasPie) {
        add('Switch to a bar chart');
        add(ctx.isDonut ? 'Turn this into a pie chart' : 'Turn this into a donut chart');
        add('Show percentages instead of values');
    }
    if (ctx.hasScatter) {
        add('Add a trend line');
        if (ctx.numericCount > 2) add('Color the points by category');
    }

    // 3. Ranking for categorical magnitude charts (not time-ordered).
    if ((ctx.hasBar || ctx.hasPie) && !ctx.hasTime && ctx.pointCount > 2) {
        add('Sort highest to lowest');
        if (ctx.pointCount > 12) add('Show only the top 10');
    }

    // 4. Data labels — phrased to match the current state.
    add(ctx.labelsShown ? 'Hide the data labels' : 'Show the data values');

    // 5. Value formatting, only when the data warrants it.
    if (ctx.hasMoney) add('Format values as currency');
    if (ctx.isCombo && ctx.hasPct) add('Format the right axis as a percentage');

    // 6. Always-safe finisher.
    add('Use a different color palette');

    return out.slice(0, max);
}

export const __test = { analyzeContext, detectColumns, looksLikeMonths, looksLikeDate, DEFAULT_SUGGESTIONS };
