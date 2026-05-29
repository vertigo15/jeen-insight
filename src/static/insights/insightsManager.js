/**
 * Insights Manager
 * Handles fetching and displaying insights for query results
 */

class InsightsManager {
    constructor() {
        this.state = {
            currentInsights: null,
            isLoading: false
        };
    }

    /**
     * Generate and display insights for query results.
     * Streams the LLM response via SSE for real TTFT + progressive UX.
     * Falls back to the non-streaming endpoint on transport failure.
     *
     * @param {Object} results - Query results with rows and columns
     * @param {string} question - Original user question
     * @param {string} queryId - Query ID for linking to history (optional)
     */
    async generateInsights(results, question, queryId = null) {
        const container = document.getElementById('insights-container');
        if (!container) {
            console.error('[InsightsManager] Insights container not found');
            return;
        }

        const connection = (typeof getActiveConnection === 'function') ? getActiveConnection() : '';
        const requestBody = { connection, dataset: results, question };
        if (queryId) requestBody.query_id = queryId;

        this.showStreamingPlaceholder(container);
        this.state.isLoading = true;

        try {
            await this._streamInsights(container, requestBody);
        } catch (error) {
            // SSE failed (network, parse error, abort). Try the non-streaming
            // endpoint once before giving up so a transient transport problem
            // doesn't lose the user's insights.
            console.warn('[InsightsManager] Stream failed, falling back to non-streaming:', error);
            try {
                await this._fetchInsightsFallback(container, requestBody);
            } catch (fallbackErr) {
                console.error('[InsightsManager] Fallback also failed:', fallbackErr);
                this.showError(container, fallbackErr.message || String(fallbackErr));
            }
        } finally {
            this.state.isLoading = false;
        }
    }

    /**
     * Drive the SSE stream and update the placeholder progressively.
     * Resolves on `done`, throws on `error` or transport failure.
     */
    async _streamInsights(container, requestBody) {
        const response = await fetch('/api/generate-insights/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
            body: JSON.stringify(requestBody),
        });
        if (!response.ok || !response.body) {
            throw new Error(`Stream returned ${response.status}: ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let charsReceived = 0;
        let ttftMs = null;
        let finalInsights = null;

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            // SSE frames are separated by a blank line.
            let sep;
            while ((sep = buffer.indexOf('\n\n')) !== -1) {
                const frame = buffer.slice(0, sep);
                buffer = buffer.slice(sep + 2);
                const event = this._parseSseFrame(frame);
                if (!event) continue;

                if (event.name === 'ttft') {
                    ttftMs = (event.data && event.data.ms) || null;
                    this._setStreamingTtft(container, ttftMs);
                } else if (event.name === 'delta') {
                    const t = (event.data && event.data.text) || '';
                    charsReceived += t.length;
                    this._setStreamingProgress(container, charsReceived);
                } else if (event.name === 'done') {
                    finalInsights = event.data && event.data.insights;
                    const metrics = event.data && event.data.metrics;
                    if (finalInsights) {
                        this.state.currentInsights = finalInsights;
                        this.displayInsights(container, finalInsights, { ttftMs, metrics });
                        if (finalInsights.prompt) this.displayInsightsPrompt(finalInsights);
                    }
                } else if (event.name === 'error') {
                    const msg = (event.data && event.data.error) || 'streaming failed';
                    throw new Error(msg);
                }
                // 'open' is informational; ignore.
            }
        }

        if (!finalInsights) {
            throw new Error('Stream ended without a done event');
        }
    }

    /**
     * Parse a single SSE frame into { name, data } where data is the parsed
     * JSON payload. Returns null for comment-only frames (':' prefix).
     */
    _parseSseFrame(frame) {
        const lines = frame.split('\n');
        let name = 'message';
        const dataLines = [];
        for (const line of lines) {
            if (!line || line.startsWith(':')) continue;
            if (line.startsWith('event:')) {
                name = line.slice(6).trim();
            } else if (line.startsWith('data:')) {
                dataLines.push(line.slice(5).trimStart());
            }
        }
        if (dataLines.length === 0) return null;
        try {
            return { name, data: JSON.parse(dataLines.join('\n')) };
        } catch (_) {
            return { name, data: null };
        }
    }

    /**
     * Last-resort path when the SSE endpoint is unreachable.
     */
    async _fetchInsightsFallback(container, requestBody) {
        const response = await fetch('/api/generate-insights', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
            signal: AbortSignal.timeout(30000),
        });
        if (!response.ok) {
            throw new Error(`API returned ${response.status}: ${response.statusText}`);
        }
        const insights = await response.json();
        if (insights.error) throw new Error(insights.error);
        this.state.currentInsights = insights;
        this.displayInsights(container, insights);
        if (insights.prompt) this.displayInsightsPrompt(insights);
    }

    /**
     * Initial placeholder shown while waiting for the first byte from the LLM.
     */
    showStreamingPlaceholder(container) {
        container.innerHTML = `
            <div class="insights-section">
                <div class="insights-header">
                    <h3>✨ Insights</h3>
                    <span id="insights-stream-meta" class="insights-stream-meta"></span>
                </div>
                <div class="insights-loading" role="status" aria-label="Generating insights…">
                    <div id="insights-stream-status" class="insights-stream-status">Thinking…</div>
                    <div class="skeleton" style="height: 1rem; width: 80%;"></div>
                    <div class="skeleton" style="height: 1rem; width: 60%;"></div>
                    <div class="skeleton" style="height: 1rem; width: 72%;"></div>
                </div>
            </div>
        `;
    }

    _setStreamingTtft(container, ttftMs) {
        const el = container.querySelector('#insights-stream-meta');
        if (!el || ttftMs == null) return;
        const txt = ttftMs >= 1000 ? (ttftMs / 1000).toFixed(1) + 's' : ttftMs + 'ms';
        // textContent (not innerHTML) keeps this XSS-safe.
        el.textContent = `TTFT ${txt}`;
    }

    _setStreamingProgress(container, charsReceived) {
        const el = container.querySelector('#insights-stream-status');
        if (!el) return;
        el.textContent = `Generating… ${charsReceived.toLocaleString('en-US')} chars`;
    }

    /**
     * Display insights in the container.
     * @param {HTMLElement} container
     * @param {Object} insights
     * @param {{ttftMs?: number|null, metrics?: object|null}} [meta]
     */
    displayInsights(container, insights, meta = {}) {
        // Check if insights are empty
        if (!insights.findings || insights.findings.length === 0) {
            container.innerHTML = `
                <div class="insights-section">
                    <div class="insights-header">
                        <h3>💡 Insights</h3>
                    </div>
                    <div class="insights-empty">
                        <p>${insights.summary || 'No significant insights found for this dataset'}</p>
                    </div>
                </div>
            `;
            return;
        }

        // Build HTML for insights
        let metaTxt = '';
        const ttftMs = meta && meta.ttftMs;
        if (ttftMs != null && Number.isFinite(ttftMs)) {
            const t = ttftMs >= 1000 ? (ttftMs / 1000).toFixed(1) + 's' : ttftMs + 'ms';
            metaTxt = `TTFT ${t}`;
        }
        let html = `
            <div class="insights-section">
                <div class="insights-header">
                    <h3>✨ Insights</h3>
                    <span class="insights-stream-meta">${this.escapeHtml(metaTxt)}</span>
                </div>
                
                <div class="insights-content">
        `;

        // Summary
        if (insights.summary) {
            html += `
                <div class="insights-summary">
                    <strong>Summary:</strong> ${this.escapeHtml(insights.summary)}
                </div>
            `;
        }

        // Key Findings
        if (insights.findings && insights.findings.length > 0) {
            html += `<div class="insights-findings">
                <strong>Key Findings:</strong>
                <ul>`;
            
            insights.findings.forEach(finding => {
                html += `<li>${this.escapeHtml(finding)}</li>`;
            });
            
            html += `</ul></div>`;
        }

        // Follow-up questions chip grid
        if (insights.suggestions && insights.suggestions.length > 0) {
            html += `<div class="follow-up-section">
                <div class="follow-up-label">FOLLOW-UP QUESTIONS</div>
                <div class="follow-up-grid">`;

            insights.suggestions.forEach(q => {
                const safe = this.escapeHtml(q);
                // data-q carries the raw text so the onclick can prefill the input
                html += `<button class="follow-up-chip" data-q="${safe}"
                    onclick="window._fillFollowUp(this.dataset.q)">
                    <span class="follow-up-chip-text">${safe}</span>
                    <span class="follow-up-chip-arrow" aria-hidden="true">→</span>
                </button>`;
            });

            html += `</div></div>`;
        }

        html += `</div></div>`;

        container.innerHTML = html;
    }

    /**
     * Show loading state
     */
    showLoading(container) {
        container.innerHTML = `
            <div class="insights-section">
                <div class="insights-header">
                    <h3>Insights</h3>
                </div>
                <div class="insights-loading" role="status" aria-label="Analyzing data...">
                    <div class="skeleton" style="height: 1rem; width: 80%;"></div>
                    <div class="skeleton" style="height: 1rem; width: 60%;"></div>
                    <div class="skeleton" style="height: 1rem; width: 72%;"></div>
                </div>
            </div>
        `;
    }

    /**
     * Show error state
     */
    showError(container, message) {
        container.innerHTML = `
            <div class="insights-section">
                <div class="insights-header">
                    <h3>💡 Insights</h3>
                </div>
                <div class="insights-error">
                    <p>⚠️ ${this.escapeHtml(message)}</p>
                </div>
            </div>
        `;
    }

    /**
     * Display insights prompt in the Insights Prompt tab with collapsible sections
     */
    displayInsightsPrompt(insights) {
        const promptContent = document.getElementById('insights-prompt-content');
        if (!promptContent) {
            console.warn('[InsightsManager] Insights prompt content element not found');
            return;
        }
        
        if (!insights.prompt) {
            promptContent.innerHTML = '<p style="color: #999;">No prompt available</p>';
            return;
        }
        
        // Parse the prompt into sections
        const sections = this.parseInsightsPrompt(insights.prompt);
        
        let html = '<div class="structured-prompt">';
        
        // Section 1: Main Instructions (Rules and Thresholds)
        if (sections.mainInstructions) {
            html += this.createPromptSection('insights-main', 'Instructions & Rules', 
                `<pre class="prompt-text">${this.escapeHtml(sections.mainInstructions)}</pre>`, true);
        }
        
        // Section 2: Dataset Summary
        if (sections.datasetSummary) {
            html += this.createPromptSection('insights-dataset', 'Dataset Summary', 
                `<pre class="prompt-text">${this.escapeHtml(sections.datasetSummary)}</pre>`, false);
        }
        
        // Section 3: Column Statistics
        if (sections.columnStats) {
            html += this.createPromptSection('insights-stats', 'Column Statistics', 
                `<pre class="prompt-text">${this.escapeHtml(sections.columnStats)}</pre>`, false);
        }
        
        // Section 4: Output Format
        if (sections.outputFormat) {
            html += this.createPromptSection('insights-format', 'Output Format', 
                `<pre class="prompt-text">${this.escapeHtml(sections.outputFormat)}</pre>`, false);
        }
        
        // Section 5: Full Prompt
        html += this.createPromptSection('insights-full', 'Full Prompt Text', 
            `<pre class="prompt-text">${this.escapeHtml(insights.prompt)}</pre>`, false);
        
        html += '</div>';
        promptContent.innerHTML = html;
        
        console.log('[InsightsManager] Insights prompt displayed in structured format');
    }
    
    /**
     * Parse insights prompt into sections
     */
    parseInsightsPrompt(prompt) {
        const sections = {
            mainInstructions: '',
            datasetSummary: '',
            columnStats: '',
            outputFormat: ''
        };
        
        // Split by section headers
        const datasetSummaryIndex = prompt.indexOf('## DATASET SUMMARY:');
        const columnStatsIndex = prompt.indexOf('## COLUMN STATISTICS:');
        const outputFormatIndex = prompt.indexOf('## OUTPUT FORMAT');
        
        // Extract main instructions (everything before DATASET SUMMARY)
        if (datasetSummaryIndex !== -1) {
            sections.mainInstructions = prompt.substring(0, datasetSummaryIndex).trim();
        } else {
            // Fallback: if no sections found, put everything in main instructions
            sections.mainInstructions = prompt;
            return sections;
        }
        
        // Extract dataset summary (between DATASET SUMMARY and COLUMN STATISTICS)
        if (datasetSummaryIndex !== -1 && columnStatsIndex !== -1) {
            sections.datasetSummary = prompt.substring(datasetSummaryIndex + 19, columnStatsIndex).trim();
        } else if (datasetSummaryIndex !== -1) {
            sections.datasetSummary = prompt.substring(datasetSummaryIndex + 19).trim();
        }
        
        // Extract column statistics (between COLUMN STATISTICS and OUTPUT FORMAT)
        if (columnStatsIndex !== -1 && outputFormatIndex !== -1) {
            sections.columnStats = prompt.substring(columnStatsIndex + 22, outputFormatIndex).trim();
        } else if (columnStatsIndex !== -1) {
            sections.columnStats = prompt.substring(columnStatsIndex + 22).trim();
        }
        
        // Extract output format (from OUTPUT FORMAT to end)
        if (outputFormatIndex !== -1) {
            sections.outputFormat = prompt.substring(outputFormatIndex + 16).trim();
        }
        
        return sections;
    }
    
    /**
     * Create a collapsible prompt section
     */
    createPromptSection(id, title, content, expanded = false) {
        const expandedClass = expanded ? 'expanded' : '';
        const displayStyle = expanded ? 'block' : 'none';
        const arrow = expanded ? '▼' : '▶';
        
        return `
            <div class="prompt-section ${expandedClass}">
                <div class="prompt-section-header" onclick="toggleInsightsPromptSection('${id}')">
                    <span class="section-arrow" id="arrow-${id}">${arrow}</span>
                    <span class="section-title">${title}</span>
                </div>
                <div class="prompt-section-content" id="content-${id}" style="display: ${displayStyle};">
                    ${content}
                </div>
            </div>
        `;
    }
    
    /**
     * Toggle a prompt section
     */
    togglePromptSection(sectionId) {
        const content = document.getElementById(`content-${sectionId}`);
        const arrow = document.getElementById(`arrow-${sectionId}`);
        
        if (content && arrow) {
            if (content.style.display === 'none') {
                content.style.display = 'block';
                arrow.textContent = '▼';
            } else {
                content.style.display = 'none';
                arrow.textContent = '▶';
            }
        }
    }
    
    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Export for use in script.js
window.InsightsManager = InsightsManager;

// Expose toggle function globally for onclick handlers
window.toggleInsightsPromptSection = function(sectionId) {
    const content = document.getElementById(`content-${sectionId}`);
    const arrow = document.getElementById(`arrow-${sectionId}`);
    
    if (content && arrow) {
        if (content.style.display === 'none') {
            content.style.display = 'block';
            arrow.textContent = '▼';
        } else {
            content.style.display = 'none';
            arrow.textContent = '▶';
        }
    }
};
