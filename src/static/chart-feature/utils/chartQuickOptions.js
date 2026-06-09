/**
 * Client-side ECharts option tweaks (no LLM round-trip).
 * @module chartQuickOptions
 */

/**
 * @param {object} config
 * @param {{ dataLabels?: boolean, legend?: boolean, dataZoom?: boolean, sortDesc?: boolean }} toggles
 * @param {object|null} baselineConfig - original config for restoring category order
 * @returns {object}
 */
export function applyQuickOptions(config, toggles, baselineConfig = null) {
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
        } else if (baselineConfig) {
            _restoreCategoryOrder(out, baselineConfig);
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
        cat,
        val: _seriesValue(primary.data[i]),
    }));
    pairs.sort((a, b) => b.val - a.val);

    const sortedCats = pairs.map((p) => p.cat);
    const axisKey = config.xAxis && (Array.isArray(config.xAxis) ? config.xAxis.some((a) => a === axis || a.type === 'category') : config.xAxis === axis)
        ? 'xAxis'
        : 'yAxis';
    _setCategoryData(config, axisKey, sortedCats);

    config.series = series.map((s) => {
        if (!Array.isArray(s.data) || s.data.length !== categories.length) return s;
        const byCat = new Map(categories.map((c, i) => [c, s.data[i]]));
        return { ...s, data: sortedCats.map((c) => byCat.get(c)) };
    });
}

function _restoreCategoryOrder(config, baseline) {
    const baseAxis = _getCategoryAxis(baseline);
    if (!baseAxis || !Array.isArray(baseAxis.data)) return;
    const categories = baseAxis.data.slice();
    const axisKey = baseline.xAxis && (Array.isArray(baseline.xAxis)
        ? baseline.xAxis.some((a) => a && a.type === 'category')
        : baseline.xAxis.type === 'category')
        ? 'xAxis'
        : 'yAxis';
    _setCategoryData(config, axisKey, categories);

    const series = Array.isArray(config.series) ? config.series : [];
    const baseSeries = Array.isArray(baseline.series) ? baseline.series : [];
    config.series = series.map((s, idx) => {
        const base = baseSeries[idx];
        if (base && Array.isArray(base.data) && base.data.length === categories.length) {
            return { ...s, data: base.data.slice() };
        }
        if (!Array.isArray(s.data) || s.data.length !== categories.length) return s;
        const byCat = new Map(categories.map((c, i) => [c, s.data[i]]));
        return { ...s, data: categories.map((c) => byCat.get(c)) };
    });
}

function _seriesValue(point) {
    if (point === null || point === undefined) return -Infinity;
    if (typeof point === 'number') return point;
    if (Array.isArray(point)) return point[1] ?? point[0] ?? -Infinity;
    if (typeof point === 'object' && point.value != null) return Number(point.value) || -Infinity;
    return Number(point) || -Infinity;
}
