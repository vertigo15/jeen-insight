/**
 * Named chart colour palettes and the pure transform that applies one to an
 * ECharts option.
 *
 * The "jeen" palette is the default: it means "no override", so the workspace
 * theme tokens (rose / plum / teal) or the server's default palette keep
 * deciding the colours. Every other palette sets `option.color` and pins a
 * per-series colour on single-colour series (bar, line, scatter) so the
 * choice survives whatever `itemStyle.color` a previous theme pass or a saved
 * chart_config already carries. Multi-colour series (pie, funnel, treemap,
 * sunburst) take their slice colours from `option.color`, so a stale
 * series-level colour is removed instead.
 *
 * No DOM access: shared by the browser and the node tests.
 * @module chartPalettes
 */

export const DEFAULT_PALETTE_ID = 'jeen';

export const CHART_PALETTES = Object.freeze([
    {
        id: 'jeen',
        label: 'Jeen',
        // Display swatches only; the real colours come from the theme tokens.
        swatches: ['#8878c4', '#7a5ea8', '#4bb9c9', '#d4574a'],
        colors: null,
    },
    {
        id: 'purple',
        label: 'Purple',
        colors: ['#6f5bd6', '#a48ff0', '#4f3fb0', '#c9b8ff', '#8a6fe8', '#3b2d8f', '#d8ccff', '#5c4bc4'],
    },
    {
        id: 'blue',
        label: 'Blue',
        colors: ['#2f6fed', '#7aa6f5', '#1d4bb8', '#a9c6fa', '#4f8bf0', '#12337f', '#cfe0fc', '#3a63d1'],
    },
    {
        id: 'green',
        label: 'Green',
        colors: ['#2e9e6a', '#6cc99a', '#1f7a4f', '#a3ddc0', '#48b583', '#155a3a', '#cdeedd', '#3a8f62'],
    },
    {
        id: 'teal',
        label: 'Teal',
        colors: ['#1f9aa8', '#6cc5cf', '#146f7a', '#a4dde3', '#3fb1bd', '#0d4d55', '#cfeef1', '#2a8a96'],
    },
    {
        id: 'warm',
        label: 'Warm',
        colors: ['#e8703a', '#f2a35e', '#c9512a', '#f7c98a', '#d9863f', '#8f3a1e', '#fbe0bd', '#e0894a'],
    },
    {
        id: 'classic',
        label: 'Classic',
        colors: ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc'],
    },
]);

export const PALETTE_IDS = Object.freeze(CHART_PALETTES.map((p) => p.id));

/** Series types that draw one colour per series (a palette pins it explicitly). */
const SINGLE_COLOUR_SERIES = new Set([
    'bar', 'line', 'scatter', 'effectScatter', 'boxplot', 'candlestick', 'radar',
]);

/** Series whose colours never come from the palette. */
const PALETTE_EXEMPT_SERIES = new Set(['map', 'heatmap', 'gauge']);

export function getPalette(id) {
    return CHART_PALETTES.find((p) => p.id === id) || CHART_PALETTES[0];
}

export function isKnownPalette(id) {
    return PALETTE_IDS.includes(id);
}

/** Swatch colours for the picker (theme tokens for "jeen" when available). */
export function paletteSwatches(id, themeColors = null) {
    const palette = getPalette(id);
    if (palette.colors) return palette.colors.slice(0, 4);
    if (Array.isArray(themeColors) && themeColors.length) return themeColors.slice(0, 4);
    return palette.swatches.slice(0, 4);
}

/**
 * Return a copy of `option` recoloured with `colors`. Returns the input
 * untouched for map-based options or when `colors` is empty.
 */
export function applyPalette(option, colors) {
    if (!option || typeof option !== 'object') return option;
    if (!Array.isArray(colors) || !colors.length) return option;
    if (option.jeenOsmMap) return option;
    const series = Array.isArray(option.series) ? option.series : option.series ? [option.series] : [];
    if (series.some((s) => s && PALETTE_EXEMPT_SERIES.has(s.type)) || option.visualMap) return option;

    let out;
    try {
        out = JSON.parse(JSON.stringify(option));
    } catch (_) {
        return option;
    }
    out.color = colors.slice();
    const outSeries = Array.isArray(out.series) ? out.series : out.series ? [out.series] : [];
    outSeries.forEach((s, index) => {
        if (!s || typeof s !== 'object') return;
        const colour = colors[index % colors.length];
        if (SINGLE_COLOUR_SERIES.has(s.type)) {
            s.itemStyle = { ...(s.itemStyle || {}), color: colour };
            if (s.type === 'line') s.lineStyle = { ...(s.lineStyle || {}), color: colour };
        } else if (s.itemStyle && typeof s.itemStyle.color === 'string') {
            // pie / funnel / treemap / sunburst: slices follow option.color.
            delete s.itemStyle.color;
        }
    });
    return out;
}
