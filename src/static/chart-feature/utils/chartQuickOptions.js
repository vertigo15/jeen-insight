/**
 * Client-side ECharts option tweaks (no LLM round-trip).
 * @module chartQuickOptions
 */

/**
 * @param {object} config
 * @param {{ dataLabels?: boolean, legend?: boolean, dataZoom?: boolean, sortDesc?: boolean }} toggles
 * @returns {object}
 */
export function applyQuickOptions(config, toggles) {
    if (!config || typeof config !== 'object') return config;
    let out;
    try {
        out = JSON.parse(JSON.stringify(config));
    } catch (_) {
        return config;
    }

    const series = Array.isArray(out.series) ? out.series : [];

    if (typeof toggles.legend === 'boolean') {
        out.legend = out.legend && typeof out.legend === 'object' ? out.legend : {};
        out.legend.show = toggles.legend;
    }

    if (typeof toggles.dataLabels === 'boolean') {
        out.series = series.map((s) => {
            const next = { ...s };
            next.label = next.label && typeof next.label === 'object' ? { ...next.label } : {};
            next.label.show = toggles.dataLabels;
            if (toggles.dataLabels && !next.label.position) {
                next.label.position = next.type === 'pie' ? 'outside' : 'top';
            }
            return next;
        });
    }

    if (typeof toggles.dataZoom === 'boolean') {
        if (toggles.dataZoom) {
            out.dataZoom = [
                { type: 'slider', start: 0, end: 100, height: 18, bottom: 4 },
                { type: 'inside', start: 0, end: 100 },
            ];
            out.grid = out.grid && typeof out.grid === 'object' ? { ...out.grid } : {};
            if (out.grid.bottom === undefined || out.grid.bottom === '3%') {
                out.grid.bottom = '14%';
            }
        } else {
            delete out.dataZoom;
        }
    }

    if (typeof toggles.sortDesc === 'boolean') {
        if (toggles.sortDesc) {
            _sortCategorySeriesDesc(out);
        }
    }

    return out;
}

function _getCategoryAxis(config) {
    if (Array.isArray(config.xAxis)) {
        return config.xAxis.find((a) => a && a.type === 'category') || config.xAxis[0];
    }
    if (config.xAxis && config.xAxis.type === 'category') return config.xAxis;
    if (Array.isArray(config.yAxis)) {
        return config.yAxis.find((a) => a && a.type === 'category') || null;
    }
    if (config.yAxis && config.yAxis.type === 'category') return config.yAxis;
    return null;
}

function _setCategoryData(config, axisKey, categories) {
    if (Array.isArray(config[axisKey])) {
        const idx = config[axisKey].findIndex((a) => a && a.type === 'category');
        const target = idx >= 0 ? idx : 0;
        config[axisKey] = config[axisKey].map((a, i) =>
            i === target ? { ...a, data: categories.slice() } : a
        );
    } else if (config[axisKey]) {
        config[axisKey] = { ...config[axisKey], data: categories.slice() };
    }
}

function _sortCategorySeriesDesc(config) {
    const axis = _getCategoryAxis(config);
    if (!axis || !Array.isArray(axis.data) || axis.data.length === 0) return;

    const categories = axis.data.slice();
    const series = Array.isArray(config.series) ? config.series : [];
    const primary = series.find((s) => Array.isArray(s.data) && s.data.length === categories.length);
    if (!primary) return;

    const pairs = categories.map((cat, i) => ({
        index: i,
        cat,
        val: _seriesValue(primary.data[i]),
    }));
    pairs.sort((a, b) => (b.val - a.val) || (a.index - b.index));

    const sortedCats = pairs.map((p) => p.cat);
    const axisKey = config.xAxis && (Array.isArray(config.xAxis) ? config.xAxis.some((a) => a === axis || a.type === 'category') : config.xAxis === axis)
        ? 'xAxis'
        : 'yAxis';
    _setCategoryData(config, axisKey, sortedCats);

    config.series = series.map((s) => {
        if (!Array.isArray(s.data) || s.data.length !== categories.length) return s;
        return { ...s, data: pairs.map((p) => s.data[p.index]) };
    });
}

function _seriesValue(point) {
    if (point === null || point === undefined) return -Infinity;
    if (typeof point === 'number') return point;
    if (Array.isArray(point)) return point[1] ?? point[0] ?? -Infinity;
    if (typeof point === 'object' && point.value != null) return Number(point.value) || -Infinity;
    return Number(point) || -Infinity;
}

const MULTI_COLOUR_SERIES = new Set(['pie', 'funnel', 'treemap', 'sunburst']);

/**
 * How many entries a legend would list for this config: the explicit
 * `legend.data`, the slices of a lone pie-like series, otherwise the number of
 * series. A single entry only repeats the measure already named on the value
 * axis, so the default is to hide the legend in that case.
 * @param {object} config
 * @returns {number}
 */
export function countLegendEntries(config) {
    if (!config || typeof config !== 'object') return 0;
    if (config.legend && Array.isArray(config.legend.data)) return config.legend.data.length;
    const series = Array.isArray(config.series) ? config.series : config.series ? [config.series] : [];
    const real = series.filter((s) => s && typeof s === 'object');
    if (real.length === 1 && MULTI_COLOUR_SERIES.has(real[0].type)) {
        return Array.isArray(real[0].data) ? real[0].data.length : 0;
    }
    return real.length;
}

/** Default legend visibility for a freshly built config. */
export function defaultLegendVisible(config) {
    return countLegendEntries(config) > 1;
}

/**
 * Read the visual-toggle state that a (possibly chat-edited) config already
 * encodes, so the quick-toggle layer can mirror it instead of overwriting it.
 * Without this, an LLM edit like "add data labels" (label.show=true) gets wiped
 * by the default-off `dataLabels` toggle on the next re-render.
 *
 * sortDesc is intentionally not detected — it isn't reliably recoverable from a
 * config — so the user's current sort toggle is preserved as-is.
 *
 * @param {object} config
 * @returns {{ dataLabels?: boolean, legend?: boolean, dataZoom?: boolean }}
 */
export function detectToggles(config) {
    const out = {};
    if (!config || typeof config !== 'object') return out;
    const series = Array.isArray(config.series) ? config.series : [];
    out.dataLabels = series.some((s) => s && s.label && s.label.show === true);
    out.legend = !(config.legend && config.legend.show === false);
    out.dataZoom = Array.isArray(config.dataZoom) && config.dataZoom.length > 0;
    return out;
}
