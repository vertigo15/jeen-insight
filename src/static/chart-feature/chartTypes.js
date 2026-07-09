/**
 * Canonical chart type list for the selector, settings, and preferences.
 * @module chartTypes
 */

/** @typedef {{ value: string, label: string }} ChartTypeOption */

/** @type {ChartTypeOption[]} */
export const CHART_TYPE_OPTIONS = [
    { value: 'auto',           label: '🤖 Auto (LLM Recommended)' },
    { value: 'bar',            label: '📊 Bar Chart' },
    { value: 'line',           label: '📈 Line Chart' },
    { value: 'pie',            label: '🥧 Pie Chart' },
    { value: 'area',           label: '📉 Area Chart' },
    { value: 'scatter',        label: '⚫ Scatter Plot' },
    { value: 'horizontal_bar', label: '📊 Horizontal Bar' },
    { value: 'stacked_bar',    label: '📊 Stacked Bar' },
    { value: 'stacked_area',   label: '📉 Stacked Area' },
    { value: 'donut',          label: '🍩 Donut Chart' },
    { value: 'combo',          label: '📊 Combo (Bar + Line)' },
    { value: 'heatmap',        label: '🟦 Heatmap' },
    { value: 'gauge',          label: '🎯 Gauge / KPI' },
    { value: 'map',            label: '🗺️ Flat Map' },
];

/** @type {Set<string>} */
export const CHART_TYPE_VALUES = new Set(CHART_TYPE_OPTIONS.map((t) => t.value));
