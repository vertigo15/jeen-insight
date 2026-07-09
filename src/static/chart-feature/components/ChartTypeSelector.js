/**
 * Chart Type Selector Component
 * Provides dropdown for switching between chart types with LLM recommendation support
 * 
 * @module ChartTypeSelector
 */

/// <reference path="../types/chart.types.js" />

import { CHART_TYPE_OPTIONS } from '../chartTypes.js?v=77';

/**
 * Available chart types (shared with settings + preferences)
 */
const CHART_TYPES = CHART_TYPE_OPTIONS;

/**
 * Creates a dropdown selector for chart types
 */
export class ChartTypeSelector {
    /**
     * @param {string} containerId - ID of container to render selector in
     * @param {Function} onChange - Callback when chart type changes (chartType) => void
     */
    constructor(containerId, onChange) {
        this.containerId = containerId;
        // Honour the user's saved default chart type from the settings panel.
        // Falls back to 'auto' if no preference is stored or the value is
        // invalid (Preferences.getAll() validates against the allowed set).
        let defaultType = 'auto';
        try {
            if (typeof window !== 'undefined' && window.JeenPreferences) {
                defaultType = window.JeenPreferences.getAll().chartType || 'auto';
            }
        } catch (_) { /* keep 'auto' on any failure */ }
        this.currentType = defaultType;
        this.onChange = onChange;
        this.llmRecommendation = null;

        console.log('[ChartTypeSelector] Initialized, default=', defaultType);
    }
    
    /**
     * Renders the chart type selector
     */
    render() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.error(`[ChartTypeSelector] Container ${this.containerId} not found`);
            return;
        }
        
        const optionsHtml = CHART_TYPES.map(type => 
            `<option value="${type.value}" ${this.currentType === type.value ? 'selected' : ''}>${type.label}</option>`
        ).join('');
        
        container.innerHTML = `
            <div class="chart-type-selector-wrapper">
                <label for="chart-type-select" class="chart-type-label">Chart Type:</label>
                <select id="chart-type-select" class="chart-type-select">
                    ${optionsHtml}
                </select>
                <div id="chart-type-recommendation" class="chart-type-recommendation" style="display: none;">
                    <span class="recommendation-icon">💡</span>
                    <span class="recommendation-text"></span>
                </div>
            </div>
        `;
        
        // Attach event listener
        this.attachEventListener();
        
        console.log('[ChartTypeSelector] Rendered');
    }
    
    /**
     * Attaches event listener to dropdown
     */
    attachEventListener() {
        const select = document.getElementById('chart-type-select');
        if (select) {
            select.addEventListener('change', (e) => {
                const newType = e.target.value;
                this.handleTypeChange(newType);
            });
        }
    }
    
    /**
     * Handles chart type change
     * 
     * @param {import('../types/chart.types.js').ChartType} chartType - New chart type
     */
    handleTypeChange(chartType) {
        if (chartType === this.currentType) {
            return;
        }
        
        console.log('[ChartTypeSelector] Chart type changed to:', chartType);
        
        this.currentType = chartType;
        
        if (this.onChange) {
            this.onChange(chartType);
        }
    }
    
    /**
     * Sets the chart type programmatically
     * 
     * @param {import('../types/chart.types.js').ChartType} chartType - Chart type to set
     */
    setType(chartType) {
        this.currentType = chartType;
        
        const select = document.getElementById('chart-type-select');
        if (select) {
            select.value = chartType;
        }
        
        this.updateRecommendationDisplay();
        console.log('[ChartTypeSelector] Type set to:', chartType);
    }
    
    /**
     * Sets the LLM's recommended chart type
     * 
     * @param {string} recommendedType - Chart type recommended by LLM
     */
    setRecommendation(recommendedType) {
        this.llmRecommendation = recommendedType;
        console.log('[ChartTypeSelector] LLM recommended:', recommendedType);
        this.updateRecommendationDisplay();
    }
    
    /**
     * Updates the recommendation display text
     */
    updateRecommendationDisplay() {
        const recommendationDiv = document.getElementById('chart-type-recommendation');
        const recommendationText = recommendationDiv?.querySelector('.recommendation-text');
        
        if (!recommendationDiv || !recommendationText) return;
        
        if (this.currentType === 'auto' && this.llmRecommendation) {
            // Show what LLM chose
            const typeName = this.getChartTypeName(this.llmRecommendation);
            recommendationText.textContent = `LLM selected: ${typeName}`;
            recommendationDiv.style.display = 'flex';
        } else if (this.currentType !== 'auto' && this.llmRecommendation && this.currentType !== this.llmRecommendation) {
            // Show that user overrode LLM recommendation
            const recommendedName = this.getChartTypeName(this.llmRecommendation);
            recommendationText.textContent = `LLM originally recommended: ${recommendedName}`;
            recommendationDiv.style.display = 'flex';
        } else {
            recommendationDiv.style.display = 'none';
        }
    }
    
    /**
     * Gets human-readable chart type name
     * 
     * @param {string} chartType - Chart type value
     * @returns {string} Human-readable name
     */
    getChartTypeName(chartType) {
        const type = CHART_TYPES.find(t => t.value === chartType);
        return type ? type.label.replace(/^[^\s]+\s/, '') : chartType; // Remove emoji
    }
    
    /**
     * Resets selector to default state
     */
    reset() {
        this.currentType = 'auto';
        this.llmRecommendation = null;
        
        const select = document.getElementById('chart-type-select');
        if (select) {
            select.value = 'auto';
        }
        
        this.updateRecommendationDisplay();
        console.log('[ChartTypeSelector] Reset');
    }
    
    /**
     * Gets the currently selected chart type
     * 
     * @returns {string} Selected chart type
     */
    getSelectedType() {
        return this.currentType;
    }
    
    /**
     * Shows the selector
     */
    show() {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.style.display = 'block';
        }
    }
    
    /**
     * Hides the selector
     */
    hide() {
        const container = document.getElementById(this.containerId);
        if (container) {
            container.style.display = 'none';
        }
    }
}
