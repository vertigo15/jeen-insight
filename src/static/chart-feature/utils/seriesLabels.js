/**
 * Interface-language names for the ML band-chart series (forecast, anomaly,
 * changepoint). The server names these series in English and tags each with a
 * `jeenRole` (see src/api/chart_builder.py); translating them at render time
 * means saved and restored charts follow the current interface language too.
 *
 * Keyed by the server's English name, not the role: the changepoint chart
 * reuses the `expected` / `flagged` roles for "Segment level" / "Changepoint".
 *
 * @module seriesLabels
 */

const FIXED = {
    Actual: (t) => t('charts.series.actual'),
    Expected: (t) => t('charts.series.expected'),
    Forecast: (t) => t('charts.series.forecast'),
    Flagged: (t) => t('charts.series.flagged'),
    'Segment level': (t) => t('charts.series.segmentLevel'),
    Changepoint: (t) => t('charts.series.changepoint'),
    Lower: (t) => t('charts.series.lower'),
    Upper: (t) => t('charts.series.upper'),
};
const BAND_RE = /^(\d+(?:\.\d+)?)% band$/;
const INTERVAL_RE = /^(\d+(?:\.\d+)?)% interval$/;

function translatedName(name, t) {
    if (Object.prototype.hasOwnProperty.call(FIXED, name)) return FIXED[name](t);
    const band = BAND_RE.exec(name);
    if (band) return t('charts.series.band', { percent: band[1] });
    const interval = INTERVAL_RE.exec(name);
    if (interval) return t('charts.series.interval', { percent: interval[1] });
    return null;
}

/**
 * Rename role-tagged series, their legend entries and the forecast-start marker.
 * Idempotent: the server's English name is kept in `jeenName` (plain data, so it
 * survives the JSON clones in the render pipeline).
 *
 * @param {object} option  ECharts option; mutated and returned
 * @param {(key: string, args?: object) => (string|null)} t  catalog lookup that
 *   returns null for a missing key, so an absent catalog leaves names alone
 * @returns {object}
 */
export function localizeSeriesLabels(option, t) {
    const series = Array.isArray(option?.series) ? option.series : null;
    if (!series || typeof t !== 'function') return option;
    const renamed = new Map();
    series.forEach((s) => {
        if (!s || !s.jeenRole) return;
        const source = typeof s.jeenName === 'string' ? s.jeenName : s.name;
        if (typeof source !== 'string') return;
        const label = translatedName(source, t);
        if (label) {
            s.jeenName = source;
            if (label !== s.name) renamed.set(s.name, label);
            s.name = label;
        }
        const mark = s.jeenRole === 'forecast' && s.markLine && s.markLine.label;
        if (mark && typeof mark.formatter === 'string') {
            if (typeof mark.jeenFormatter !== 'string') mark.jeenFormatter = mark.formatter;
            const start = mark.jeenFormatter === 'forecast' ? t('charts.series.forecastStart') : null;
            if (start) mark.formatter = start;
        }
    });
    if (!renamed.size) return option;
    const legends = Array.isArray(option.legend) ? option.legend : option.legend ? [option.legend] : [];
    legends.forEach((legend) => {
        if (!legend) return;
        if (Array.isArray(legend.data)) {
            legend.data = legend.data.map((entry) => {
                if (typeof entry === 'string') return renamed.get(entry) || entry;
                if (entry && typeof entry.name === 'string' && renamed.has(entry.name)) {
                    return { ...entry, name: renamed.get(entry.name) };
                }
                return entry;
            });
        }
        // Hidden series are keyed by name; keep them hidden under the new name.
        if (legend.selected && typeof legend.selected === 'object') {
            legend.selected = Object.fromEntries(Object.entries(legend.selected)
                .map(([name, shown]) => [renamed.get(name) || name, shown]));
        }
    });
    return option;
}
