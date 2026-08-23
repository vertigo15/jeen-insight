/**
 * Type definitions for chart feature
 * Using JSDoc for type safety in vanilla JavaScript
 */

/**
 * @typedef {'numeric' | 'category' | 'date'} ColumnType
 */

/**
 * @typedef {Object} ColumnInfo
 * @property {string} name - Column name
 * @property {ColumnType} type - Detected column type
 * @property {number} index - Column index in results
 * @property {number} uniqueCount - Number of unique values (for categories)
 */

/**
 * @typedef {'line' | 'bar' | 'pie' | 'area' | 'scatter' | 'horizontal_bar' | 'stacked_bar' | 'stacked_area' | 'donut' | 'combo' | 'heatmap' | 'gauge' | 'map' | 'osm_map'} ChartType
 */

/**
 * @typedef {Object} DataAnalysis
 * @property {ColumnInfo[]} columns - Analysis of each column
 * @property {ChartType} suggestedChartType - Recommended chart type
 * @property {ColumnInfo|null} xAxisColumn - Column to use for X-axis/labels
 * @property {ColumnInfo|null} yAxisColumn - Column to use for Y-axis/values
 * @property {boolean} canChart - Whether data is suitable for charting
 * @property {string} reason - Why chart is/isn't suitable
 */

/**
 * @typedef {Object} QueryResults
 * @property {string[]} columns - Column names
 * @property {Array<Array<any>>|Array<Object>} data - Data rows (array or object format)
 */

/**
 * @typedef {Object} EChartsOption
 * @property {Object} [title] - Chart title configuration
 * @property {Object} [xAxis] - X-axis configuration
 * @property {Object} [yAxis] - Y-axis configuration
 * @property {Array<Object>} [series] - Data series configuration
 * @property {Object} [tooltip] - Tooltip configuration
 * @property {Object} [legend] - Legend configuration
 * @property {Object} [grid] - Grid configuration
 * @property {Array<string>} [color] - Color palette
 */

/**
 * @typedef {Object} ChartConfig
 * @property {ChartType} type - Chart type
 * @property {EChartsOption} options - ECharts configuration object
 * @property {boolean} isEnhanced - Whether this config was enhanced by LLM
 * @property {string} [cacheKey] - Key for caching enhanced configs
 */

/**
 * @typedef {Object} EnhanceChartRequest
 * @property {Array<{name: string, type: ColumnType}>} columns - Column information
 * @property {Array<Array<any>>} sample_data - First 10 rows of data
 * @property {ChartType} chart_type - Type of chart to enhance
 * @property {EChartsOption} [current_config] - Current auto-generated config
 */

/**
 * @typedef {Object} EnhanceChartResponse
 * @property {EChartsOption} [enhanced_config] - Enhanced ECharts configuration
 * @property {string} [error] - Error message if enhancement failed
 */

/**
 * @typedef {'table' | 'chart'} ViewMode
 */

/**
 * @typedef {Object} ChartState
 * @property {ViewMode} currentView - Current view mode
 * @property {ChartType} currentChartType - Current chart type
 * @property {ChartConfig|null} currentConfig - Current chart configuration
 * @property {any|null} chartInstance - ECharts instance
 * @property {boolean} isEChartsLoaded - Whether ECharts library is loaded
 * @property {QueryResults|null} currentData - Current query results
 */

// Export empty object to make this a module
export {};
