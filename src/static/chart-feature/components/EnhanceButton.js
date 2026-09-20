/**
 * Enhance Button Component
 * Button for triggering AI-powered chart enhancement
 * 
 * @module EnhanceButton
 */

// Interface strings come from the locale catalog (static/i18n/i18n.js, loaded first).
const t = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const th = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : String(key));

/// <reference path="../types/chart.types.js" />

/**
 * Creates an "Enhance with AI" button
 */
export class EnhanceButton {
    /**
     * @param {string} containerId - ID of container to render button in
     * @param {Function} onClick - Callback when button is clicked
     */
    constructor(containerId, onClick) {
        this.containerId = containerId;
        this.onClick = onClick;
        this.isLoading = false;
        
        console.log('[EnhanceButton] Initialized');
    }
    
    /**
     * Renders the enhance button
     */
    render() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.error(`[EnhanceButton] Container ${this.containerId} not found`);
            return;
        }
        
        container.innerHTML = `
            <button 
                id="enhance-chart-btn" 
                class="enhance-chart-btn"
                title="${th('charts.enhance.title')}"
            >
                ✨ ${th('charts.enhance.label')}
            </button>
        `;
        
        // Attach event listener
        this.attachEventListener();
        
        console.log('[EnhanceButton] Rendered');
    }
    
    /**
     * Attaches event listener to button
     */
    attachEventListener() {
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            button.addEventListener('click', () => {
                if (!this.isLoading && this.onClick) {
                    this.onClick();
                }
            });
        }
    }
    
    /**
     * Shows loading state
     */
    showLoading() {
        this.isLoading = true;
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            button.disabled = true;
            button.innerHTML = `
                <span class="button-spinner"></span>
                ${th('charts.enhance.loading')}
            `;
            console.log('[EnhanceButton] Loading state shown');
        }
    }
    
    /**
     * Hides loading state
     */
    hideLoading() {
        this.isLoading = false;
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            button.disabled = false;
            button.innerHTML = `✨ ${th('charts.enhance.label')}`;
            console.log('[EnhanceButton] Loading state hidden');
        }
    }
    
    /**
     * Shows success state temporarily
     */
    showSuccess() {
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            const originalHtml = button.innerHTML;
            button.innerHTML = `✓ ${th('charts.enhance.done')}`;
            button.style.background = '#28a745';
            
            setTimeout(() => {
                button.innerHTML = originalHtml;
                button.style.background = '';
            }, 2000);
            
            console.log('[EnhanceButton] Success state shown');
        }
    }
    
    /**
     * Shows error state temporarily
     * 
     * @param {string} message - Error message
     */
    showError(message) {
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            const originalHtml = button.innerHTML;
            button.innerHTML = '⚠️ Enhancement Failed';
            button.style.background = '#dc3545';
            
            setTimeout(() => {
                button.innerHTML = originalHtml;
                button.style.background = '';
                this.hideLoading();
            }, 3000);
            
            console.error('[EnhanceButton] Error:', message);
        }
    }
    
    /**
     * Disables the button
     */
    disable() {
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            button.disabled = true;
            console.log('[EnhanceButton] Button disabled');
        }
    }
    
    /**
     * Enables the button
     */
    enable() {
        const button = document.getElementById('enhance-chart-btn');
        if (button) {
            button.disabled = false;
            console.log('[EnhanceButton] Button enabled');
        }
    }
}
