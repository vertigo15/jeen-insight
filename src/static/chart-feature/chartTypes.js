/**
 * Canonical chart type list for the selector, settings, and preferences.
 * @module chartTypes
 */

/**
 * @typedef {Object} ChartTypeOption
 * @property {string} value - Stable identifier persisted in preferences / sent to the API.
 * @property {string} label - Human-readable name (no emoji).
 * @property {string} icon  - Monochrome stroke SVG path data (viewBox 0 0 24 24).
 */

/** @type {ChartTypeOption[]} */
export const CHART_TYPE_OPTIONS = [
    { value: 'auto',           label: 'Auto (LLM Recommended)', icon: 'M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.5 2.5M15.2 15.2l2.5 2.5M17.7 6.3l-2.5 2.5M8.8 15.2l-2.5 2.5' },
    { value: 'bar',            label: 'Bar Chart',              icon: 'M5 20V10M12 20V5M19 20V13' },
    { value: 'line',           label: 'Line Chart',             icon: 'M3 17l5-6 4 3 6-8' },
    { value: 'pie',            label: 'Pie Chart',              icon: 'M12 3a9 9 0 109 9h-9V3z' },
    { value: 'area',           label: 'Area Chart',             icon: 'M3 17l5-6 4 3 6-8v11H3v-6z' },
    { value: 'scatter',        label: 'Scatter Plot',           icon: 'M6 16h.01M10 10h.01M15 13h.01M18 6h.01' },
    { value: 'horizontal_bar', label: 'Horizontal Bar',         icon: 'M4 6h10M4 12h16M4 18h7' },
    { value: 'stacked_bar',    label: 'Stacked Bar',            icon: 'M6 20v-5m0 0V8M12 20v-7m0 0V4M18 20v-4m0 0v-6' },
    { value: 'stacked_area',   label: 'Stacked Area',           icon: 'M3 19l5-4 4 2 6-5v7H3zM3 13l5-5 4 2 6-6' },
    { value: 'donut',          label: 'Donut Chart',            icon: 'M12 3a9 9 0 110 18 9 9 0 010-18zM12 8a4 4 0 100 8 4 4 0 000-8z' },
    { value: 'combo',          label: 'Combo (Bar + Line)',     icon: 'M5 20v-6M11 20V11M17 20v-4M3 8l6-3 5 4 7-5' },
    { value: 'heatmap',        label: 'Heatmap',                icon: 'M4 4h16v16H4zM4 12h16M12 4v16' },
    { value: 'gauge',          label: 'Gauge / KPI',            icon: 'M12 13l4-4M4 17a8 8 0 0116 0' },
    { value: 'map',            label: 'Flat Map',               icon: 'M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2zM9 4v14M15 6v14' },
];

/** @type {Set<string>} */
export const CHART_TYPE_VALUES = new Set(CHART_TYPE_OPTIONS.map((t) => t.value));

/**
 * Look up an option by value.
 * @param {string} value
 * @returns {ChartTypeOption|undefined}
 */
export function getChartTypeOption(value) {
    return CHART_TYPE_OPTIONS.find((t) => t.value === value);
}
