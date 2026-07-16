/**
 * Insights Manager
 * Handles fetching and displaying insights for query results
 */

class InsightsManager {
    /**
     * @param {Object} [opts]
     * @param {HTMLElement} [opts.container]  Render target. Defaults to #insights-container (Ask mode).
     * @param {Function}    [opts.onFollowUp] Called with the question text when a follow-up chip is clicked.
     *                                        Defaults to window._fillFollowUp (Ask mode auto-submit).
     * @param {boolean}     [opts.skipSummary] When true, the summary is NOT rendered inside the card;
     *                                        instead it is passed to opts.onSummary (Chat places it above the chart).
     * @param {Function}    [opts.onSummary]  Receives the rendered summary HTML when skipSummary is set.
     * @param {boolean}     [opts.showPromptInDevPanel] When false, don't push the prompt to the dev panel (Chat).
     * @param {boolean}     [opts.devTrace]   When false, don't emit post-query dev-trace events (Chat).
     */
    constructor(opts = {}) {
        this.state = {
            currentInsights: null,
            isLoading: false
        };
        this.containerEl          = opts.container || null;
        this.onFollowUp           = opts.onFollowUp || null;
        this.skipSummary          = opts.skipSummary === true;
        this.onSummary            = opts.onSummary || null;
        this.showPromptInDevPanel = opts.showPromptInDevPanel !== false;
        this.devTraceEnabled      = opts.devTrace !== false;
    }

    /** Resolve the render target: an injected element (Chat) or the shared #insights-container (Ask). */
    _getContainer() {
        return this.containerEl || document.getElementById('insights-container');
    }

    /**
     * Generate and display insights for query results.
     * Streams the LLM response via SSE for real TTFT + progressive UX.
     * Falls back to the non-streaming endpoint on transport failure.
     *
     * When `sql` is provided the server routes the request through the
     * LangGraph eval node (fused_eval_analytics) which returns richer
     * follow-up questions alongside the summary and findings.
     *
     * @param {Object} results - Query results with rows and columns
     * @param {string} question - Original user question
     * @param {string|null} queryId - Query ID for linking to history (optional)
     * @param {string|null} sql - SQL that produced the results (optional)
     */
    async generateInsights(results, question, queryId = null, sql = null) {
        const container = this._getContainer();
        if (!container) {
            console.error('[InsightsManager] Insights container not found');
            return;
        }

        const connection = (typeof getActiveConnection === 'function') ? getActiveConnection() : '';

        // The server analyses the FULL result set from its result cache (keyed by
        // query_id) and computes whole-dataset statistics — so insights reflect
        // ALL the data, not a sample. The dataset we send here is only a
        // cache-miss fallback, so we send a generous sample (not the whole frame)
        // to keep the upload small while staying useful if the cache missed.
        const MAX_FALLBACK_ROWS = 200;
        const fallbackResults = results && results.rows && results.rows.length > MAX_FALLBACK_ROWS
            ? { ...results, rows: results.rows.slice(0, MAX_FALLBACK_ROWS), row_count: results.rows.length }
            : results;

        const requestBody = { connection, dataset: fallbackResults, question };
        if (queryId) requestBody.query_id = queryId;
        // Forward the SQL so the server can use the LangGraph eval node.
        if (sql) requestBody.sql = sql;

        this._devTrace('running', {
            detail: sql
                ? 'Streaming LangGraph eval insights after the SQL answer rendered.'
                : 'Streaming legacy insights after the answer rendered.',
        });
        this.showStreamingPlaceholder(container);
        this.state.isLoading = true;

        try {
            await this._streamInsights(container, requestBody);
        } catch (error) {
            // SSE failed (network, parse error, abort). Try the non-streaming
            // endpoint once before giving up so a transient transport problem
            // doesn't lose the user's insights.
            console.warn('[InsightsManager] Stream failed, falling back to non-streaming:', error);
            this._devTrace('running', { detail: 'Streaming failed; retrying with non-streaming insights endpoint.' });
            try {
                await this._fetchInsightsFallback(container, requestBody);
            } catch (fallbackErr) {
                console.error('[InsightsManager] Fallback also failed:', fallbackErr);
                this._devTrace('error', { detail: fallbackErr.message || String(fallbackErr) });
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
            signal: AbortSignal.timeout(90000),  // 90s — previously had NO timeout
        });
        if (!response.ok || !response.body) {
            throw new Error(`Stream returned ${response.status}: ${response.statusText}`);
        }
        this._devTrace('running', { detail: 'Insights stream opened.' });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let charsReceived = 0;
        let ttftMs = null;
        let finalInsights = null;
        let sawDelta = false;

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
                    this._devTrace('running', { metrics: { ttft_ms: ttftMs }, detail: 'First insight token received.' });
                    this._setStreamingTtft(container, ttftMs);
                } else if (event.name === 'delta') {
                    const t = (event.data && event.data.text) || '';
                    charsReceived += t.length;
                    if (!sawDelta) {
                        sawDelta = true;
                        this._devTrace('running', { detail: 'Insights content is streaming.' });
                    }
                    this._setStreamingProgress(container, charsReceived);
                } else if (event.name === 'done') {
                    finalInsights = event.data && event.data.insights;
                    const metrics = event.data && event.data.metrics;
                    if (finalInsights) {
                        this.state.currentInsights = finalInsights;
                        this.displayInsights(container, finalInsights, { ttftMs, metrics });
                        if (finalInsights.prompt) this.displayInsightsPrompt(finalInsights);
                        this._devTrace('done', {
                            metrics: { ...(metrics || {}), ttft_ms: ttftMs },
                            detail: `Generated ${(finalInsights.findings || []).length} finding(s).`,
                        });
                    }
                } else if (event.name === 'error') {
                    const msg = (event.data && event.data.error) || 'streaming failed';
                    this._devTrace('error', { detail: msg });
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
            signal: AbortSignal.timeout(90000),  // 90s — matches server LLM timeout
        });
        if (!response.ok) {
            throw new Error(`API returned ${response.status}: ${response.statusText}`);
        }
        const insights = await response.json();
        if (insights.error) throw new Error(insights.error);
        this.state.currentInsights = insights;
        this.displayInsights(container, insights);
        if (insights.prompt) this.displayInsightsPrompt(insights);
        this._devTrace('done', {
            detail: `Generated ${(insights.findings || []).length} finding(s) via fallback endpoint.`,
        });
    }

    _devTrace(status, payload = {}) {
        if (!this.devTraceEnabled) return;
        if (typeof window !== 'undefined' && typeof window._devPostQueryUpdate === 'function') {
            window._devPostQueryUpdate('insights', { status, ...payload });
        }
    }

    getSaveState() {
        return this.state.currentInsights || null;
    }

    // ── SVG constants ──────────────────────────────────────────────────────
    static _SVG_SPARK = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/></svg>`;
    static _SVG_CHECK = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>`;
    static _SVG_ARROW = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`;
    static _SVG_ARROW_SM = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`;

    /**
     * Render a content value as HTML.
     *
     * Accepts either a plain string or a fragment array
     * [{t: "text", hl?: "accent|pos|neg|num"}, …].
     * The fragment format gives the backend full control (backend-driven highlights
     * win; auto-highlight is used only when content is a plain string).
     *
     * @param {string|Array} content
     * @param {'summary'|'finding'} context
     *   'summary'  — accent budget of 3 for headline figures; first bare $/$% get accent
     *   'finding'  — direction rules only (pos/neg for %); $ always hl-num
     */
    renderText(content, context = 'finding') {
        if (Array.isArray(content)) {
            // Explicit fragment format — honour hl hints directly.
            return content.map(frag => {
                const text = this.escapeHtml(frag.t || '');
                if (!frag.hl) return text;
                return `<span class="hl-${this.escapeHtml(frag.hl)}">${text}</span>`;
            }).join('');
        }
        return this._autoHighlight(this.escapeHtml(String(content || '')), context);
    }

    /**
     * Auto-apply the 4-treatment color spec to an already HTML-escaped string.
     *
     * Four treatments (color = direction):
     *   hl-accent — headline answer figures, summary only, max 3
     *   hl-pos    — favorable direction, always signed  (+89.8%, +$2.48M)
     *   hl-neg    — unfavorable direction, signed, sparingly (−2.4%, −$49.9K)
     *   hl-num    — exact value for precision; $ amounts in findings
     *
     * Rules:
     *   • % changes   → pos/neg based on sign; unsigned % in summary → accent
     *   • $ amounts   → hl-num in findings; hl-accent (up to 3) in summary
     *   • Comma counts → hl-num
     *   • Neutral numbers ("12 rows", "7 territories") → no color
     *   • Never stack accent with pos/neg
     *
     * Execution order matters — specific signed patterns before bare ones.
     *
     * @param {string} escaped  — HTML-escaped input text
     * @param {'summary'|'finding'} context
     */
    _autoHighlight(escaped, context = 'finding') {
        const isSummary = (context === 'summary');
        let accentLeft  = isSummary ? 3 : 0;   // headline budget (summary only)

        // ─ 1. Signed positive % (+89.8%, +56.3%) ─────────── hl-pos ──────
        escaped = escaped.replace(
            /\+(\d+(?:\.\d+)?%)/g,
            '<span class="hl-pos">+$1</span>'
        );

        // ─ 2. Signed negative % (−2.4% or -2.4%, not a hyphen) ─ hl-neg ─
        //     Real minus \u2212 is unambiguous.
        //     ASCII - : guard with (?<!\w) so hyphens in words are safe.
        escaped = escaped.replace(
            /\u2212(\d+(?:\.\d+)?%)/g,
            (_, pct) => `<span class="hl-neg">\u2212${pct}</span>`
        );
        escaped = escaped.replace(
            /(?<!\w)-(\d+(?:\.\d+)?%)/g,
            (_, pct) => `<span class="hl-neg">-${pct}</span>`
        );

        // ─ 3. Dollar amounts — all treated by precision, not direction ────
        //
        //   Findings : ALL $ → hl-num  (the $ shows the amount; the % shows the move)
        //   Summary  : first 3 → hl-accent; remainder → hl-num
        //
        //   Lookbehind (?<![>]) prevents re-wrapping $ that's already inside a span.

        // Signed positive +$ (e.g. +$2.48M)
        escaped = escaped.replace(
            /\+(\$[\d,]+(?:\.\d+)?[KMBb]?)/g,
            (_, amount) => {
                const cls = (isSummary && accentLeft > 0)
                    ? (accentLeft--, 'hl-accent') : 'hl-num';
                return `<span class="${cls}">+${amount}</span>`;
            }
        );

        // Signed negative −$ / -$ (e.g. −$49.9K)
        escaped = escaped.replace(
            /\u2212(\$[\d,]+(?:\.\d+)?[KMBb]?)/g,
            (_, amount) => {
                const cls = (isSummary && accentLeft > 0)
                    ? (accentLeft--, 'hl-accent') : 'hl-num';
                return `<span class="${cls}">\u2212${amount}</span>`;
            }
        );
        escaped = escaped.replace(
            /(?<!\w)-(\$[\d,]+(?:\.\d+)?[KMBb]?)/g,
            (_, amount) => {
                const cls = (isSummary && accentLeft > 0)
                    ? (accentLeft--, 'hl-accent') : 'hl-num';
                return `<span class="${cls}">-${amount}</span>`;
            }
        );

        // Bare (unsigned) $ — not already inside a span (not preceded by >)
        escaped = escaped.replace(
            /(?<![>+\u2212-])(\$[\d,]+(?:\.\d+)?[KMBb]?)/g,
            (match) => {
                const cls = (isSummary && accentLeft > 0)
                    ? (accentLeft--, 'hl-accent') : 'hl-num';
                return `<span class="${cls}">${match}</span>`;
            }
        );

        // ─ 4. Unsigned % in SUMMARY (shares / total rates) ─── hl-accent ─
        //     e.g. "36.9% growth" or "lifted revenue 36.9%"
        //     In findings, unsigned % is a share and gets no color.
        if (isSummary) {
            escaped = escaped.replace(
                /(?<![>+\u2212-])(\d+(?:\.\d+)?%)/g,
                (match) => {
                    if (accentLeft > 0) { accentLeft--; return `<span class="hl-accent">${match}</span>`; }
                    return match;  // accent budget exhausted — leave plain
                }
            );
        }
        // Unsigned % in findings: spec says no color (it\'s a share, not a change).

        // ─ 5. Comma-separated counts (≥1,000) ──────────────── hl-num ─────
        //     Not already inside a tag (\$) or span (>), not followed by more digits.
        escaped = escaped.replace(
            /(?<![>$\d])(\d{1,3}(?:,\d{3})+)(?!\d)/g,
            '<span class="hl-num">$1</span>'
        );

        return escaped;
    }

    /** Format milliseconds as a human-readable string. */
    _fmtMs(ms) {
        if (ms == null || !Number.isFinite(ms)) return null;
        return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
    }

    /** Format a token count as e.g. "21.9K" or "227". */
    _fmtTok(n) {
        if (n == null || !Number.isFinite(n)) return null;
        if (n >= 10000) return (n / 1000).toFixed(1) + 'K';
        return n.toLocaleString('en-US');
    }

    /**
     * Header HTML — shows title, TTFT, and (when available) LLM latency +
     * token counts from the metrics payload.
     *
     * @param {string|null}  ttftLabel  — formatted TTFT string or null
     * @param {Object|null}  metrics    — { llm_latency_ms?, input_tokens?, output_tokens? }
     */
    _headerHtml(ttftLabel, metrics) {
        const llmStr = metrics && this._fmtMs(metrics.llm_latency_ms);
        const inTok  = metrics && this._fmtTok(metrics.input_tokens);
        const outTok = metrics && this._fmtTok(metrics.output_tokens);

        // Right-side badges: TTFT (streaming), then LLM time + tokens (once done)
        let right = '';
        if (llmStr || inTok || outTok) {
            // Final render — show full metrics
            if (llmStr)  right += `<span class="ins-meta-chip">LLM ${this.escapeHtml(llmStr)}</span>`;
            if (inTok)   right += `<span class="ins-meta-chip">in ${this.escapeHtml(inTok)}</span>`;
            if (outTok)  right += `<span class="ins-meta-chip">out ${this.escapeHtml(outTok)}</span>`;
        } else if (ttftLabel) {
            // During streaming — show TTFT
            right = `<span class="ttft">TTFT ${this.escapeHtml(ttftLabel)}</span>`;
        } else {
            // Placeholder — live-updating TTFT element
            right = '<span class="ttft" id="ins-ttft"></span>';
        }

        return `
        <div class="ins-head">
            <span class="ins-title">
                <span class="ins-spark">${InsightsManager._SVG_SPARK}</span>
                Insights
            </span>
            <span class="ins-meta-row">${right}</span>
        </div>`;
    }

    /**
     * Initial placeholder shown while waiting for the first byte from the LLM.
     */
    showStreamingPlaceholder(container) {
        container.innerHTML = `
        <div class="ins-card">
            ${this._headerHtml(null, null)}
            <div class="ins-loading" role="status" aria-label="Generating insights…">
                <p id="ins-stream-status" class="ins-stream-status">Thinking…</p>
                <div class="skeleton" style="height:0.9rem;width:85%;border-radius:4px;"></div>
                <div class="skeleton" style="height:0.9rem;width:68%;border-radius:4px;"></div>
                <div class="skeleton" style="height:0.9rem;width:76%;border-radius:4px;"></div>
            </div>
        </div>`;
    }

    _setStreamingTtft(container, ttftMs) {
        const el = container.querySelector('#ins-ttft');
        if (!el || ttftMs == null) return;
        const txt = ttftMs >= 1000 ? (ttftMs / 1000).toFixed(1) + 's' : ttftMs + 'ms';
        el.textContent = `TTFT ${txt}`;
    }

    _setStreamingProgress(container, charsReceived) {
        const el = container.querySelector('#ins-stream-status');
        if (!el) return;
        el.textContent = `Generating\u2026 ${charsReceived.toLocaleString('en-US')} chars`;
    }

    /**
     * Display insights in the container — new design (matches Insights Mockup.html).
     *
     * Data contract (all arrays may be empty — section is omitted if so):
     *   insights.summary    string | fragment[]   — lead paragraph
     *   insights.findings   string[] | fragment[][] — key findings with icons
     *   insights.suggestions string[]             — recommended actions (0-2)
     *   insights.followups  string[]              — clickable follow-up questions
     *                       (falls back to insights.suggestions for old format)
     *
     * @param {HTMLElement} container
     * @param {Object}      insights
     * @param {{ttftMs?: number|null}} [meta]
     */
    displayInsights(container, insights, meta = {}) {
        const summary    = insights.summary    || '';
        const findings   = insights.findings   || [];
        const suggestions = insights.suggestions || [];
        // followups: new field; fall back to suggestions if it's the old format
        // (old format had follow-up questions in .suggestions)
        const followups  = insights.followups  != null
            ? insights.followups
            : (suggestions.length && !insights.followups ? suggestions : []);
        // In the old format suggestions = follow-up questions; new format separates them.
        // If followups came from fallback, clear suggestions to avoid duplicating.
        const actionSuggestions = insights.followups != null ? suggestions : [];

        const ttftMs  = meta && meta.ttftMs;
        const metrics = meta && meta.metrics;   // { llm_latency_ms, input_tokens, output_tokens }
        let ttftLabel = null;
        if (ttftMs != null && Number.isFinite(ttftMs)) {
            ttftLabel = ttftMs >= 1000 ? (ttftMs / 1000).toFixed(1) + 's' : ttftMs + 'ms';
        }

        // Hoist the summary out of the card when the caller wants to place it
        // elsewhere (Chat renders it above the chart, matching the mockup).
        const summaryHtml = summary ? this.renderText(summary, 'summary') : '';
        if (summary && this.skipSummary && typeof this.onSummary === 'function') {
            this.onSummary(summaryHtml);
        }
        const showSummaryInCard = summary && !this.skipSummary;

        const hasContent = showSummaryInCard || findings.length || actionSuggestions.length || followups.length;
        if (!hasContent) {
            // If the summary was hoisted out (Chat places it above the chart),
            // don't also render an empty "no insights" card beneath it.
            if (summary && this.skipSummary) {
                container.innerHTML = '';
            } else {
                container.innerHTML = `<div class="ins-card">${this._headerHtml(ttftLabel, metrics)}
                    <p class="ins-empty">No significant insights found for this result.</p>
                </div>`;
            }
            return;
        }

        let html = `<div class="ins-card">${this._headerHtml(ttftLabel, metrics)}`;

        // ── Summary ──────────────────────────────────────────────────────────
        if (showSummaryInCard) {
            html += `<p class="ins-summary">${summaryHtml}</p>`;
        }

        // ── Divider (only when there are sections below) ──────────────────────
        const hasSections = findings.length || actionSuggestions.length || followups.length;
        if (showSummaryInCard && hasSections) {
            html += `<div class="ins-divider"></div>`;
        }

        // ── Key findings ─────────────────────────────────────────────────────
        if (findings.length) {
            html += `<div>`;
            html += `<div class="ins-subhead">What we found</div>`;
            html += `<div class="ins-list">`;
            findings.forEach(f => {
                html += `<div class="ins-item">
                    <span class="ins-item-icon ins-item-icon--check">${InsightsManager._SVG_CHECK}</span>
                    <span class="ins-item-body">${this.renderText(f, 'finding')}</span>
                </div>`;
            });
            html += `</div></div>`;
        }

        // ── Recommended next (action suggestions) ────────────────────────────
        if (actionSuggestions.length) {
            html += `<div>`;
            html += `<div class="ins-subhead">Recommended next</div>`;
            html += `<div class="ins-list">`;
            actionSuggestions.forEach(s => {
                html += `<div class="ins-item ins-item--suggest">
                    <span class="ins-item-icon ins-item-icon--arrow">${InsightsManager._SVG_ARROW}</span>
                    <span class="ins-item-body">${this.renderText(s, 'finding')}</span>
                </div>`;
            });
            html += `</div></div>`;
        }

        // ── Follow-up questions ──────────────────────────────────────────────
        if (followups.length) {
            html += `<div>`;
            html += `<div class="ins-subhead">Follow-up questions</div>`;
            html += `<div class="ins-followups">`;
            followups.forEach(q => {
                const safe = this.escapeHtml(q);
                html += `<button class="ins-followup" type="button" data-q="${safe}">
                    <span class="ins-followup-q">${safe}</span>
                    ${InsightsManager._SVG_ARROW_SM}
                </button>`;
            });
            html += `</div></div>`;
        }

        html += `</div>`; // close ins-card
        container.innerHTML = html;

        // Wire follow-up chips. Ask mode falls back to the global auto-submit
        // helper; Chat mode passes its own per-turn handler via opts.onFollowUp.
        container.querySelectorAll('.ins-followup').forEach(btn => {
            btn.addEventListener('click', () => {
                const q = btn.dataset.q || '';
                if (typeof this.onFollowUp === 'function') this.onFollowUp(q);
                else if (typeof window._fillFollowUp === 'function') window._fillFollowUp(q);
            });
        });
    }

    /**
     * Show error state
     */
    showError(container, message) {
        container.innerHTML = `
        <div class="ins-card">
            ${this._headerHtml(null, null)}
            <div class="ins-divider"></div>
            <p class="ins-error">&#x26a0;&#xfe0f; ${this.escapeHtml(message)}</p>
        </div>`;
    }

    /**
     * Display insights prompt in the Insights Prompt tab with collapsible sections
     */
    displayInsightsPrompt(insights) {
        // Chat turns don't own the shared dev-panel prompt pane.
        if (this.showPromptInDevPanel === false) return;
        const promptContent = document.getElementById('insights-prompt-content');
        if (!promptContent) {
            console.warn('[InsightsManager] Insights prompt content element not found');
            return;
        }

        if (!insights.prompt) {
            promptContent.innerHTML = '<p style="color: #999;">No prompt available</p>';
            return;
        }

        // Light up the </> button badge so the user knows there's content to view
        if (typeof window._devDrawerShowBadge === 'function') {
            window._devDrawerShowBadge();
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
