/**
 * Value formatting for chart axes and tooltips.
 *
 * The server decides WHAT kind of value it is (number / currency / percent) and
 * whether to abbreviate large numbers; this builds the actual ECharts formatter
 * function. Kept as a pure function so it's unit-testable and reused on every
 * render (initial generation, chat edits, quick toggles, reset).
 *
 * @module valueFormat
 */

/** Intl tag for grouping/decimals: the interface language's formatting locale. */
const formatLocale = () => (typeof window !== 'undefined' && window.I18n && window.I18n.formatLocale) || 'en-US';

/**
 * @param {{kind?: string, compact?: boolean, symbol?: string, scale?: number}} meta
 *   kind: "number" | "currency" | "percent"
 *   symbol: currency symbol to PREFIX (e.g. "$", "€", "₪"). Only used for
 *           kind="currency"; when empty we do NOT assume a currency (no symbol),
 *           because the data could be any currency.
 *   scale: multiplier applied before formatting (e.g. 100 to render 0–1
 *          fractions as 0–100 percent). Defaults to 1.
 * @returns {(value:number|string)=>string}
 */
export function makeValueFormatter(meta = {}) {
    const kind = meta.kind || 'number';
    const compact = meta.compact !== false;
    const symbol = typeof meta.symbol === 'string' ? meta.symbol : '';
    // Multiply before formatting — used to show 0–1 fractions as 0–100 percent.
    const scale = (typeof meta.scale === 'number' && isFinite(meta.scale) && meta.scale > 0) ? meta.scale : 1;

    const trim = (n, d) => {
        const s = n.toFixed(d);
        return d > 0 ? s.replace(/\.?0+$/, '') : s;
    };
    // Thousands separators for the non-abbreviated path: 1,234,567.
    const grouped = (n) => Math.round(n).toLocaleString(formatLocale());

    return function format(value) {
        if (value === null || value === undefined) return '';
        let n = typeof value === 'number' ? value : Number(value);
        if (!isFinite(n)) return typeof value === 'string' ? value : '';
        if (scale !== 1) n = n * scale;

        const abs = Math.abs(n);
        let body;
        if (compact && abs >= 1000) {
            // Abbreviate huge numbers: 1.2K, 3.4M, 1.1B, 2T.
            const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
            for (const [base, suffix] of units) {
                if (abs >= base) {
                    const scaled = n / base;
                    body = trim(scaled, Math.abs(scaled) < 10 ? 1 : 0) + suffix;
                    break;
                }
            }
        } else if (abs >= 1000) {
            // Big numbers: drop the decimals (they're noise at this scale) and
            // group thousands → 1,000 / 10,000,000, never 10000000.34.
            body = grouped(n);
        } else if (Number.isInteger(n)) {
            body = String(n);
        } else {
            // Small fractional values keep more precision than large ones.
            body = trim(n, abs < 1 ? 3 : 2);
        }

        // Currency: prefix the known symbol only. Never assume "$".
        if (kind === 'currency') return symbol ? symbol + body : body;
        if (kind === 'percent') return body + '%';
        return body;
    };
}

/**
 * Flatten an ECharts series `data` array into finite numbers: plain numbers,
 * `{value}` objects, `[x, y]` pairs (last element) and numeric strings.
 * @param {Array} data
 * @returns {number[]}
 */
export function collectNumericValues(data) {
    const out = [];
    for (const point of Array.isArray(data) ? data : []) {
        let raw = point;
        if (Array.isArray(raw)) raw = raw[raw.length - 1];
        else if (raw && typeof raw === 'object') raw = raw.value;
        if (Array.isArray(raw)) raw = raw[raw.length - 1];
        const n = typeof raw === 'number' ? raw : Number(raw);
        if (raw !== null && raw !== undefined && raw !== '' && isFinite(n)) out.push(n);
    }
    return out;
}

/**
 * Data-label formatter that keeps every label of a series in the SAME unit.
 *
 * Rules (best practice for on-chart labels):
 *   - if every value is at least in the thousands, abbreviate all of them with
 *     ONE shared unit chosen from the smallest value (K / M / B / T), one
 *     decimal while the scaled value is below 100: 90,220 / 73,598 / 49,827
 *     -> "90.2K" / "73.6K" / "49.8K"; 1,250,000 next to 980,000 -> "1,250K" /
 *     "980K" (same unit, so bars stay comparable at a glance).
 *   - otherwise show the full number with thousands separators: 4,300 / 950 /
 *     12.5. Mixing "4.3K" and "950" on one chart is avoided on purpose.
 *   - `compact: false` from the server always means full numbers.
 *   - percent / currency kinds keep their suffix / prefix.
 *
 * @param {{kind?: string, compact?: boolean, symbol?: string, scale?: number}} meta
 * @param {number[]} values - the series values the labels will show
 * @returns {(value:number|string)=>string}
 */
export function makeLabelFormatter(meta = {}, values = []) {
    const kind = meta.kind || 'number';
    const symbol = typeof meta.symbol === 'string' ? meta.symbol : '';
    const scale = (typeof meta.scale === 'number' && isFinite(meta.scale) && meta.scale > 0) ? meta.scale : 1;
    const allowCompact = meta.compact !== false && kind !== 'percent';

    const scaled = values.map((v) => Math.abs(v * scale)).filter((v) => v > 0);
    const minAbs = scaled.length ? Math.min(...scaled) : 0;
    const UNITS = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    let unit = null;
    if (allowCompact && scaled.length && minAbs >= 1000) {
        unit = UNITS.find(([base]) => minAbs >= base) || null;
    }

    const trim = (n, d) => {
        const s = n.toFixed(d);
        return d > 0 ? s.replace(/\.?0+$/, '') : s;
    };
    const grouped = (n) => {
        if (Number.isInteger(n)) return n.toLocaleString(formatLocale());
        const abs = Math.abs(n);
        const decimals = abs >= 1000 ? 0 : abs < 1 ? 3 : 2;
        return n.toLocaleString(formatLocale(), { minimumFractionDigits: 0, maximumFractionDigits: decimals });
    };

    return function formatLabel(value) {
        if (value === null || value === undefined) return '';
        let n = typeof value === 'number' ? value : Number(value);
        if (!isFinite(n)) return typeof value === 'string' ? value : '';
        if (scale !== 1) n = n * scale;

        let body;
        if (unit) {
            const [base, suffix] = unit;
            const q = n / base;
            const decimals = Math.abs(q) < 100 ? 1 : 0;
            body = (Math.abs(q) >= 1000 ? Math.round(q).toLocaleString(formatLocale()) : trim(q, decimals)) + suffix;
        } else {
            body = grouped(n);
        }
        if (kind === 'currency') return symbol ? symbol + body : body;
        if (kind === 'percent') return body + '%';
        return body;
    };
}
