/**
 * Data Profiling Manager
 * Handles lazy loading and caching of ydata-profiling and Sweetviz reports
 */

class ProfilingManager {
    constructor() {
        this.cachedReports = {};  // Cache per report type: { ydata: html, sweetviz: html }
        this.currentQueryId = null;
        this.isExpanded = false;
        this.isGenerating = false;
        this.selectedReportType = 'ydata';  // Default to ydata-profiling
    }

    /**
     * Initialize profiling section after query results are displayed
     * @param {Object} results - Query results with rows and columns
     */
    initialize(results) {
        const section = document.getElementById('profiling-section');
        if (!section) return;

        // Generate unique ID for this query
        const queryId = this.generateQueryId(results);

        // Clear cache if this is a new query
        if (this.currentQueryId !== queryId) {
            this.cachedReports = {};
            this.currentQueryId = queryId;
            this.isExpanded = false;
        }

        // Show the section
        section.style.display = 'block';

        // Reset to collapsed state
        this.collapse();
    }

    /**
     * Generate unique ID for query results to detect cache invalidation
     * @param {Object} results - Query results
     * @returns {string} Unique query ID
     */
    generateQueryId(results) {
        const str = JSON.stringify({
            columns: results.columns,
            rowCount: results.rows?.length || 0,
            firstRow: results.rows?.[0] || null
        });
        return this.hashCode(str);
    }

    /**
     * Simple hash function for string
     * @param {string} str - String to hash
     * @returns {number} Hash code
     */
    hashCode(str) {
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            const char = str.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash; // Convert to 32bit integer
        }
        return hash;
    }

    /**
     * Toggle profiling section (expand/collapse)
     */
    toggle() {
        if (this.isExpanded) {
            this.collapse();
        } else {
            this.expand();
        }
    }

    /**
     * Expand profiling section and generate report if needed
     */
    async expand() {
        this.isExpanded = true;

        const header = document.getElementById('profiling-header');
        const content = document.getElementById('profiling-content');
        const arrow = document.getElementById('profiling-arrow');

        if (arrow) arrow.textContent = '▼';
        if (content) content.style.display = 'block';

        // If report is cached for selected type, display it
        if (this.cachedReports[this.selectedReportType]) {
            this.displayReport(this.cachedReports[this.selectedReportType]);
            return;
        }

        // Otherwise, show generate button with library selector
        this.showGenerateButton();
    }

    /**
     * Collapse profiling section
     */
    collapse() {
        this.isExpanded = false;

        const content = document.getElementById('profiling-content');
        const arrow = document.getElementById('profiling-arrow');

        if (arrow) arrow.textContent = '▶';
        if (content) content.style.display = 'none';
    }

    /**
     * Show generate button in content area with library selector
     */
    showGenerateButton() {
        const content = document.getElementById('profiling-content');
        if (!content) return;

        content.innerHTML = `
            <div class="profiling-prompt">
                <p>📊 Generate a comprehensive data profile report including:</p>
                <ul>
                    <li>Dataset statistics and variable types</li>
                    <li>Distribution histograms for each column</li>
                    <li>Correlation matrix and heatmap</li>
                    <li>Missing values analysis</li>
                    <li>Common values and outliers</li>
                </ul>
                
                <div class="profiling-library-selector">
                    <label class="library-option">
                        <input type="radio" name="profiling-library" value="ydata" 
                            ${this.selectedReportType === 'ydata' ? 'checked' : ''}
                            onchange="profilingManager.setReportType('ydata')">
                        <span class="library-label">
                            <strong>YData Profiling</strong>
                            <small>Detailed statistical analysis with correlations</small>
                        </span>
                    </label>
                    <label class="library-option">
                        <input type="radio" name="profiling-library" value="sweetviz" 
                            ${this.selectedReportType === 'sweetviz' ? 'checked' : ''}
                            onchange="profilingManager.setReportType('sweetviz')">
                        <span class="library-label">
                            <strong>Sweetviz</strong>
                            <small>Visual EDA with feature analysis</small>
                        </span>
                    </label>
                </div>
                
                <button class="profile-generate-btn" onclick="profilingManager.generateReport()">
                    🔍 Generate Profile Report
                </button>
            </div>
        `;
    }

    /**
     * Set the selected report type
     * @param {string} reportType - 'ydata' or 'sweetviz'
     */
    setReportType(reportType) {
        this.selectedReportType = reportType;
        
        // If we have a cached report for this type, display it
        if (this.cachedReports[reportType] && this.isExpanded) {
            this.displayReport(this.cachedReports[reportType]);
        }
    }

    /**
     * Generate profile report by calling backend API
     */
    async generateReport() {
        if (this.isGenerating) return;

        this.isGenerating = true;
        const content = document.getElementById('profiling-content');
        if (!content) return;

        const reportTypeName = this.selectedReportType === 'sweetviz' ? 'Sweetviz' : 'YData Profiling';

        try {
            // Show loading state
            content.innerHTML = `
                <div class="profiling-loading" role="status" aria-label="Generating ${reportTypeName} report...">
                    <div class="skeleton" style="height: 1rem; width: 70%;"></div>
                    <div class="skeleton" style="height: 1rem; width: 55%;"></div>
                    <div class="skeleton" style="height: 200px; width: 100%;"></div>
                </div>
            `;

            // Get current results from global state
            const results = window.currentResults;
            if (!results || !results.rows || !results.columns) {
                throw new Error('No results available to profile');
            }

            // The server profiles the FULL result set from its result cache
            // (keyed by query_id). Hot path sends only the query_id; on a cache
            // miss (409) we re-send the rows as the fallback.
            const connection = (typeof getActiveConnection === 'function') ? getActiveConnection() : '';
            const basePayload = {
                report_type: this.selectedReportType,
                connection,
                query_id: window.currentQueryId || null,
            };

            let data = await this._postProfile(basePayload);
            if (data === '__CACHE_MISS__') {
                data = await this._postProfile({
                    ...basePayload,
                    dataset: { rows: results.rows, columns: results.columns },
                });
            }
            const htmlReport = data.html;

            // Cache the report for this type
            this.cachedReports[this.selectedReportType] = htmlReport;

            // Display the report
            this.displayReport(htmlReport);

        } catch (error) {
            console.error('Profile generation error:', error);
            content.innerHTML = `
                <div class="profiling-error">
                    <p>❌ Failed to generate ${reportTypeName} report</p>
                    <p class="error-detail">${error.message}</p>
                    <button class="profile-retry-btn" onclick="profilingManager.generateReport()">
                        🔄 Retry
                    </button>
                </div>
            `;
        } finally {
            this.isGenerating = false;
        }
    }

    /**
     * POST to /api/generate-profile. Returns parsed JSON, or '__CACHE_MISS__'
     * when the server has no cached rows (409) so the caller can re-send them.
     */
    async _postProfile(payload) {
        const response = await fetch('/api/generate-profile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (response.status === 409) return '__CACHE_MISS__';
        if (!response.ok) {
            let msg = 'Failed to generate profile report';
            try {
                const e = await response.json();
                msg = e.error || e.detail || msg;
            } catch (_) { /* non-JSON error body */ }
            throw new Error(msg);
        }
        return await response.json();
    }

    /**
     * Display profile report in iframe
     * @param {string} htmlReport - HTML content of the report
     */
    displayReport(htmlReport) {
        const content = document.getElementById('profiling-content');
        if (!content) return;

        const reportTypeName = this.selectedReportType === 'sweetviz' ? 'Sweetviz' : 'YData Profiling';
        const otherType = this.selectedReportType === 'sweetviz' ? 'ydata' : 'sweetviz';
        const otherTypeName = otherType === 'sweetviz' ? 'Sweetviz' : 'YData Profiling';

        content.innerHTML = `
            <div class="profiling-report">
                <div class="profiling-actions">
                    <div class="profiling-report-info">
                        <span class="report-type-badge">${reportTypeName}</span>
                        <button class="profile-switch-btn" onclick="profilingManager.switchReportType('${otherType}')">
                            🔄 Switch to ${otherTypeName}
                        </button>
                    </div>
                    <button class="profile-download-btn" onclick="profilingManager.downloadReport()">
                        📥 Download Full Report
                    </button>
                </div>
                <iframe 
                    id="profile-iframe"
                    class="profile-iframe"
                    srcdoc="${this.escapeHtml(htmlReport)}"
                    sandbox="allow-scripts allow-same-origin"
                ></iframe>
            </div>
        `;
    }

    /**
     * Switch to a different report type
     * @param {string} reportType - 'ydata' or 'sweetviz'
     */
    switchReportType(reportType) {
        this.selectedReportType = reportType;
        
        // If cached, display it; otherwise generate new report
        if (this.cachedReports[reportType]) {
            this.displayReport(this.cachedReports[reportType]);
        } else {
            this.generateReport();
        }
    }

    /**
     * Download cached report as HTML file
     */
    downloadReport() {
        const cachedReport = this.cachedReports[this.selectedReportType];
        if (!cachedReport) return;

        const reportTypeSuffix = this.selectedReportType === 'sweetviz' ? 'sweetviz' : 'ydata';
        const blob = new Blob([cachedReport], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `data_profile_${reportTypeSuffix}_${Date.now()}.html`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    /**
     * Escape HTML for safe insertion into srcdoc
     * @param {string} html - HTML string
     * @returns {string} Escaped HTML
     */
    escapeHtml(html) {
        return html
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Hide profiling section (when no results)
     */
    hide() {
        const section = document.getElementById('profiling-section');
        if (section) {
            section.style.display = 'none';
        }
    }

    /**
     * Reset manager state
     */
    reset() {
        this.cachedReports = {};
        this.currentQueryId = null;
        this.isExpanded = false;
        this.isGenerating = false;
        this.selectedReportType = 'ydata';
        this.hide();
    }
}

// Global instance
const profilingManager = new ProfilingManager();
