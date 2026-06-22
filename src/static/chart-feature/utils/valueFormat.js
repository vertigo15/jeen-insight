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
    const grouped = (n) => Math.round(n).toLocaleString('en-US');

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
