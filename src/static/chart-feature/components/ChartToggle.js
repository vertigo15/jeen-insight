/**
 * Chart Toggle Component
 * Provides UI for switching between table and chart views
 * 
 * @module ChartToggle
 */

// Interface strings come from the locale catalog (static/i18n/i18n.js, loaded first).
const t = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const th = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : String(key));

/// <reference path="../types/chart.types.js" />

/**
 * Creates a toggle button group for table/chart views
 */
export class ChartToggle {
    /**
     * @param {string} containerId - ID of container to render toggle in
     * @param {Function} onToggle - Callback when view mode changes (viewMode) => void
     */
    constructor(containerId, onToggle) {
        this.containerId = containerId;
        this.onToggle = onToggle;
        this.currentView = 'table';
        
        console.log('[ChartToggle] Initialized');
    }
    
    /**
     * Renders the toggle buttons
     */
    render() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.error(`[ChartToggle] Container ${this.containerId} not found`);
            return;
        }
        
        container.innerHTML = `
            <div class="view-toggle-group">
                <button 
                    id="toggle-table-btn" 
                    class="view-toggle-btn active" 
                    data-view="table"
                >
                    📊 ${th('charts.toggle.table')}
                </button>
                <button 
                    id="toggle-chart-btn" 
                    class="view-toggle-btn" 
                    data-view="chart"
                >
                    📈 ${th('charts.toggle.chart')}
                </button>
            </div>
        `;
        
        // Attach event listeners
        this.attachEventListeners();
        
        console.log('[ChartToggle] Rendered');
    }
    
    /**
     * Attaches event listeners to toggle buttons
     */
    attachEventListeners() {
        const tableBtn = document.getElementById('toggle-table-btn');
        const chartBtn = document.getElementById('toggle-chart-btn');
        
        if (tableBtn) {
            tableBtn.addEventListener('click', () => this.handleToggle('table'));
        }
        
        if (chartBtn) {
            chartBtn.addEventListener('click', () => this.handleToggle('chart'));
        }
    }
    
    /**
     * Handles toggle button click
     * 
     * @param {import('../types/chart.types.js').ViewMode} viewMode - Selected view mode
     */
    handleToggle(viewMode) {
        if (viewMode === this.currentView) {
            return; // Already in this view
        }
        
        console.log('[ChartToggle] Switching to view:', viewMode);
        
        this.currentView = viewMode;
        this.updateButtonStates();
        
        if (this.onToggle) {
            this.onToggle(viewMode);
        }
    }
    
    /**
     * Updates button visual states
     */
    updateButtonStates() {
        const tableBtn = document.getElementById('toggle-table-btn');
        const chartBtn = document.getElementById('toggle-chart-btn');
        
        if (tableBtn && chartBtn) {
            if (this.currentView === 'table') {
                tableBtn.classList.add('active');
                chartBtn.classList.remove('active');
            } else {
                tableBtn.classList.remove('active');
                chartBtn.classList.add('active');
            }
        }
    }
    
    /**
     * Sets the current view programmatically
     * 
     * @param {import('../types/chart.types.js').ViewMode} viewMode - View mode to set
     */
    setView(viewMode) {
        this.currentView = viewMode;
        this.updateButtonStates();
    }
    
    /**
     * Disables the chart toggle button (when data can't be charted)
     */
    disableChartButton() {
        const chartBtn = document.getElementById('toggle-chart-btn');
        if (chartBtn) {
            chartBtn.disabled = true;
            chartBtn.title = t('charts.toggle.unavailable');
            console.log('[ChartToggle] Chart button disabled');
        }
    }
    
    /**
     * Enables the chart toggle button
     */
    enableChartButton() {
        const chartBtn = document.getElementById('toggle-chart-btn');
        if (chartBtn) {
            chartBtn.disabled = false;
            chartBtn.title = '';
            console.log('[ChartToggle] Chart button enabled');
        }
    }
}
