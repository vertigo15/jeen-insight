/**
 * Chart Container Component
 * Manages ECharts instance and rendering
 * 
 * @module ChartContainer
 */

/// <reference path="../types/chart.types.js" />

/**
 * Creates and manages a chart container
 */
export class ChartContainer {
    constructor(containerId) {
        this.containerId = containerId;
        this.chartInstance = null;
        this.resizeObserver = null;
        
        console.log('[ChartContainer] Initialized with container:', containerId);
    }
    
    /**
     * Initializes the ECharts instance
     * 
     * @returns {Promise<void>}
     */
    async init() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            throw new Error(`Container ${this.containerId} not found`);
        }
        
        // Ensure ECharts is loaded
        if (typeof echarts === 'undefined') {
            throw new Error('ECharts library not loaded');
        }
        
        // Dispose existing instance if any
        if (this.chartInstance) {
            this.chartInstance.dispose();
            this.chartInstance = null;
        }
        
        // Clear any existing content (e.g., loading spinner)
        container.innerHTML = '';
        
        // Initialize chart instance
        this.chartInstance = echarts.init(container);
        console.log('[ChartContainer] ECharts instance initialized');
        
        // Set up resize observer for responsiveness
        this.setupResizeObserver();
    }
    
    /**
     * Renders a chart with the given configuration
     * 
     * @param {import('../types/chart.types.js').ChartConfig} config - Chart configuration
     */
    render(config) {
        if (!this.chartInstance) {
            console.error('[ChartContainer] Chart instance not initialized');
            return;
        }
        
        console.log('[ChartContainer] Rendering chart:', config.type);
        
        try {
            // Clear previous chart
            this.chartInstance.clear();
            const container = document.getElementById(this.containerId);
            if (container) {
                container.dataset.chartType = config.type || '';
            }
            
            // Set new options
            this.chartInstance.setOption(config.options, true);
            
            console.log('[ChartContainer] Chart rendered successfully');
        } catch (error) {
            console.error('[ChartContainer] Error rendering chart:', error);
            throw error;
        }
    }
    
    /**
     * Updates chart with new configuration (for switching chart types)
     * 
     * @param {import('../types/chart.types.js').ChartConfig} config - New chart configuration
     */
    update(config) {
        console.log('[ChartContainer] Updating chart to type:', config.type);
        this.render(config);
    }
    
    /**
     * Resizes the chart to fit container
     */
    resize() {
        if (this.chartInstance) {
            this.chartInstance.resize();
            console.log('[ChartContainer] Chart resized');
        }
    }
    
    /**
     * Sets up resize observer for responsive charts
     */
    setupResizeObserver() {
        const container = document.getElementById(this.containerId);
        if (!container) return;
        
        // Use ResizeObserver if available, otherwise fall back to window resize
        if (typeof ResizeObserver !== 'undefined') {
            this.resizeObserver = new ResizeObserver(() => {
                this.resize();
            });
            this.resizeObserver.observe(container);
            console.log('[ChartContainer] ResizeObserver attached');
        } else {
            // Fallback: debounced window resize handler
            let resizeTimeout;
            window.addEventListener('resize', () => {
                clearTimeout(resizeTimeout);
                resizeTimeout = setTimeout(() => this.resize(), 250);
            });
            console.log('[ChartContainer] Window resize handler attached');
        }
    }
    
    /**
     * Shows the chart container
     */
    show() {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.style.display = 'block';
            // Resize after showing to ensure proper dimensions
            setTimeout(() => this.resize(), 100);
        }
    }
    
    /**
     * Hides the chart container
     */
    hide() {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.style.display = 'none';
        }
    }
    
    /**
     * Shows error message in chart container
     * 
     * @param {string} message - Error message to display
     */
    showError(message) {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.innerHTML = `
                <div class="chart-error">
                    <p>⚠️ ${message}</p>
                </div>
            `;
        }
    }
    
    /**
     * Shows loading state in chart container
     */
    showLoading() {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.innerHTML = `
                <div class="chart-loading">
                    <div class="spinner"></div>
                    <p>Loading chart...</p>
                </div>
            `;
        }
    }
    
    /**
     * Returns the rendered chart as a data URL (PNG by default).
     *
     * @param {{type?: 'png'|'jpeg'|'svg', pixelRatio?: number, backgroundColor?: string}} [opts]
     * @returns {string|null} data URL, or null if no chart is rendered
     */
    getDataURL(opts = {}) {
        if (!this.chartInstance) return null;
        const {
            type = 'png',
            pixelRatio = 2,
            backgroundColor = '#ffffff',
        } = opts;
        try {
            return this.chartInstance.getDataURL({ type, pixelRatio, backgroundColor });
        } catch (e) {
            console.error('[ChartContainer] getDataURL failed:', e);
            return null;
        }
    }

    /**
     * Returns the rendered chart as a Blob (PNG by default).
     *
     * @param {{type?: 'png'|'jpeg', pixelRatio?: number, backgroundColor?: string}} [opts]
     * @returns {Promise<Blob|null>}
     */
    async getBlob(opts = {}) {
        const dataUrl = this.getDataURL(opts);
        if (!dataUrl) return null;
        try {
            const resp = await fetch(dataUrl);
            return await resp.blob();
        } catch (e) {
            console.error('[ChartContainer] getBlob failed:', e);
            return null;
        }
    }

    /**
     * Returns true once a chart instance exists and has been rendered.
     */
    hasChart() {
        return !!this.chartInstance;
    }

    /**
     * Expose the live ECharts instance for view-only controls such as map roam.
     *
     * @returns {any|null}
     */
    getInstance() {
        return this.chartInstance;
    }

    /**
     * Cleans up resources
     */
    dispose() {
        console.log('[ChartContainer] Disposing');
        
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }
        
        if (this.chartInstance) {
            this.chartInstance.dispose();
            this.chartInstance = null;
        }
    }
}
