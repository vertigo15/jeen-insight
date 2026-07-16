/**
 * Chat Controller
 * ---------------
 * Drives JEEN Insights "Chat" mode: a conversation thread that reuses the
 * exact same NL->SQL pipeline as the Ask screen, rendered one turn at a time.
 *
 * Each user turn is a bubble; each assistant turn is a card with a prose
 * summary, a Table/Chart block, and a streamed insights block (findings /
 * recommended next / follow-up chips). Reuses:
 *   - POST /api/ask                         (same endpoint + payload as Ask mode)
 *   - InsightsManager (per-turn instance)   -> /api/generate-insights/stream
 *   - ChartManager (single reused engine)   -> /api/generate-chart
 *
 * Chart strategy: there is exactly ONE ECharts apparatus in the app
 * (#chart-view-container). It lives in the Ask results card by default; when a
 * chat turn is switched to Chart view, the node is relocated into that turn and
 * the engine is bound to the turn's context. Only one turn shows a live chart
 * at a time (single active) — switching another turn to Chart moves the engine
 * and reverts the previous turn to its inline table.
 *
 * Loaded as a classic script AFTER script.js so window helpers
 * (setAppMode, getActiveConnection, escapeHtml, formatNumeric,
 * deriveResultTitle, _jeenGetSessionId/_jeenSetSessionId) and InsightsManager
 * are already defined.
 */
(function () {
    'use strict';

    // Pipeline labels for the "thinking" indicator. The real /api/query call is
    // a single blocking request, so these are approximated from the milestones
    // we do have (send -> query returns -> insights stream inside the card).
    const PIPELINE = [
        'Understanding your question',
        'Loading schema & metadata',
        'Generating SQL',
        'Running query',
        'Generating insights',
    ];

    const CHART_MANAGER_URL = '../chart-feature/chartManager.js?v=87';
    const MAX_TABLE_ROWS = 50;

    // Viz-head action icons (stroke inherits currentColor for hover/dark mode).
    const COPY_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.8"/></svg>';
    const DOWNLOAD_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const esc = (s) => (typeof window.escapeHtml === 'function'
        ? window.escapeHtml(String(s == null ? '' : s))
        : String(s == null ? '' : s));
    const fmtNum = (v) => (typeof window.formatNumeric === 'function'
        ? window.formatNumeric(v)
        : String(v));

    const ChatController = {
        initialized: false,
        threadEl: null,
        inputEl: null,
        sendBtn: null,
        emptyEl: null,

        messages: [],            // ordered turn records (for potential future use)
        _seq: 0,
        _sending: false,         // one in-flight /api/ask at a time (ordering + session continuity)

        // Single active chart engine + its home anchor.
        _ChartManagerClass: null,
        _chartMgr: null,
        _chartHome: null,        // { parent, next } where #chart-view-container lives by default
        activeChartTurnId: null,
        _activeChartTurn: null,
        _chartActivationId: 0,   // supersedes stale async chart activations

        // ── Avatars ─────────────────────────────────────────────────────────
        _assistantAvatar() {
            return '<div class="chat-avatar chat-avatar-assistant" aria-hidden="true">J</div>';
        },
        /** Wrap an assistant bubble/card with the "J" avatar to its left. */
        _wrapAssistant(innerHtml) {
            return `<div class="chat-row chat-row-assistant">${this._assistantAvatar()}${innerHtml}</div>`;
        },
        /** Initials for the current user's avatar (falls back to a person glyph). */
        _userInitials() {
            const name = (window._currentUser && window._currentUser.name || '').trim();
            if (!name) return '\uD83D\uDC64';
            const parts = name.split(/\s+/);
            return (parts.length === 1
                ? parts[0][0]
                : parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        },

        // ── Empty-state starter chips ─────────────────────────────────────────
        _ARROW_SVG: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',

        /** (Re)render the empty-state starter chips from the shared suggestions. */
        refreshStarters() {
            const box = document.getElementById('chat-empty-chips');
            if (!box) return;
            const items = (typeof window.getStarterSuggestions === 'function')
                ? window.getStarterSuggestions(4) : [];
            if (!items.length) { box.innerHTML = ''; return; }
            box.innerHTML = '';
            items.forEach((item) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'chat-empty-chip';
                btn.setAttribute('role', 'listitem');
                btn.title = item.text;
                btn.innerHTML = esc(item.text) + this._ARROW_SVG;
                btn.addEventListener('click', () => this.send(item.text));
                box.appendChild(btn);
            });
        },

        // ── Lifecycle ───────────────────────────────────────────────────────
        init() {
            if (this.initialized) return;
            this.threadEl = document.getElementById('chat-thread');
            this.inputEl  = document.getElementById('chat-input');
            this.sendBtn  = document.getElementById('chat-send-btn');
            this.emptyEl  = document.getElementById('chat-empty');

            if (this.sendBtn) {
                this.sendBtn.addEventListener('click', () => this._submitFromComposer());
            }
            if (this.inputEl) {
                this.inputEl.addEventListener('keydown', (e) => {
                    // Enter (without Shift) sends; Shift+Enter inserts a newline.
                    if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        this._submitFromComposer();
                    }
                });
                this.inputEl.addEventListener('input', () => this._autoGrow());
            }
            this.initialized = true;
        },

        /** Called by setAppMode('chat'). */
        activate() {
            if (this.emptyEl) this.emptyEl.hidden = this.messages.length > 0;
            this.refreshStarters();
            this._scrollToBottom();
            if (this.inputEl) setTimeout(() => this.inputEl.focus(), 0);
        },

        /** Called by setAppMode('ask') — return the shared chart engine home. */
        deactivate() {
            this._teardownChart();
        },

        // ── Composer ────────────────────────────────────────────────────────
        _submitFromComposer() {
            if (this._sending) return;
            const v = (this.inputEl && this.inputEl.value || '').trim();
            if (!v) return;
            this.inputEl.value = '';
            this._autoGrow();
            this.send(v);
        },

        _autoGrow() {
            const el = this.inputEl;
            if (!el) return;
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 160) + 'px';
        },

        /** Reflect in-flight state on the composer (send disabled; typing still allowed). */
        _setComposerBusy(busy) {
            if (this.sendBtn) {
                this.sendBtn.disabled = !!busy;
                this.sendBtn.setAttribute('aria-busy', busy ? 'true' : 'false');
            }
        },

        /** Clear the thread + cancel work when the active connection changes. */
        reset() {
            this._teardownChart();          // also bumps _chartActivationId
            this._sending = false;
            this._setComposerBusy(false);
            this.messages = [];
            if (this.threadEl) {
                this.threadEl.querySelectorAll('.chat-turn').forEach((n) => n.remove());
            }
            if (this.emptyEl) this.emptyEl.hidden = false;
            this.refreshStarters();
        },

        // ── Send flow ───────────────────────────────────────────────────────
        async send(text) {
            const q = (text || '').trim();
            if (!q) return;
            // Serialize: one turn must establish the session before the next, so
            // rapid sends can't spawn parallel server sessions or land out of order.
            if (this._sending) {
                this._toast('Please wait for the current answer to finish\u2026');
                return;
            }

            // Sending from another surface (e.g. a sidebar recent question) should
            // bring the chat panel forward.
            if (typeof window.getAppMode === 'function' && window.getAppMode() !== 'chat'
                && typeof window.setAppMode === 'function') {
                window.setAppMode('chat');
            }

            const connection = (typeof window.getActiveConnection === 'function')
                ? window.getActiveConnection() : '';
            if (!connection) {
                this._hideEmpty();
                this._appendError('Please pick a connection from the sidebar.');
                return;
            }

            this._sending = true;
            this._setComposerBusy(true);
            this._hideEmpty();
            this._appendUserBubble(q);
            const thinking = this._appendThinking();

            const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
            const payload = {
                question: q,
                connection,
                session_id: (typeof window._jeenGetSessionId === 'function') ? window._jeenGetSessionId() : null,
            };
            if (prefs.rowLimit) payload.limit = prefs.rowLimit;
            if (prefs.temperature !== null && prefs.temperature !== undefined) {
                payload.temperature = prefs.temperature;
            }
            // Skip in-graph eval so the table appears immediately; insights stream
            // separately into the turn's card (mirrors Ask mode).
            payload.eval_analytics = false;
            const llmTimeout = window.JeenPreferences ? window.JeenPreferences.getLlmTimeoutSeconds() : null;
            if (llmTimeout !== null && llmTimeout !== undefined) payload.llm_timeout = llmTimeout;

            const start = performance.now();
            try {
                const response = await fetch('/api/ask', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await response.json();
                const durMs = performance.now() - start;

                // Keep the developer "Run Details" panel (SQL / Prompt / Trace /
                // run header) in sync with this turn — same as Ask mode does.
                if (typeof window.updateRunDetails === 'function') {
                    try { window.updateRunDetails(data, durMs); }
                    catch (e) { console.warn('[ChatController] run details update failed:', e); }
                }

                if (!response.ok) throw new Error(data.error || 'Failed to process question');

                if (typeof window._jeenSetSessionId === 'function' && data.session_id) {
                    window._jeenSetSessionId(data.session_id);
                }
                thinking.stop();
                this._renderAssistant(data, durMs);
                // The server persisted the question during /api/ask — refresh the
                // sidebar Recent list so it shows up immediately (parity with Ask).
                if (typeof window.saveToHistory === 'function') {
                    try { window.saveToHistory(q); } catch (_) { /* noop */ }
                }
            } catch (error) {
                thinking.stop();
                this._appendError(error && error.message ? error.message : String(error));
                console.error('[ChatController] send failed:', error);
            } finally {
                this._sending = false;
                this._setComposerBusy(false);
            }
            this._scrollToBottom();
        },

        // ── Rendering ─────────────────────────────────────────────────────────
        _renderAssistant(data, durMs) {
            const id = 'a' + (++this._seq) + '-' + Date.now();
            const results = (data.results && data.results.columns
                && (data.results.data || data.results.rows)) ? data.results : null;

            const turn = {
                id,
                role: 'assistant',
                question: data.question || '',
                queryId: data.query_id || null,
                sql: data.sql || '',
                results,
                answer: data.answer || null,
            };
            this.messages.push(turn);

            const wrap = document.createElement('div');
            wrap.className = 'chat-turn chat-assistant';
            wrap.dataset.turnId = id;

            if (results) {
                const rows = results.data || results.rows || [];
                const title = (typeof window.deriveResultTitle === 'function')
                    ? window.deriveResultTitle(turn.question) : 'Results';
                const seconds = durMs / 1000;
                const durStr = seconds >= 0.1 ? seconds.toFixed(1) + 's' : Math.max(1, Math.round(durMs)) + 'ms';
                const m = data.metrics || {};
                const fmtTok = (t) => (typeof window._formatTokens === 'function') ? window._formatTokens(Number(t)) : String(t);
                let meta = `${rows.length} row${rows.length !== 1 ? 's' : ''} \u00b7 ${durStr}`;
                if (m.input_tokens != null)  meta += ` \u00b7 in: ${fmtTok(m.input_tokens)} tok`;
                if (m.output_tokens != null) meta += ` \u00b7 out: ${fmtTok(m.output_tokens)} tok`;

                wrap.innerHTML = this._wrapAssistant(`
                    <div class="chat-card">
                        <div class="chat-card-summary" data-role="summary" hidden></div>
                        <div class="chat-card-viz">
                            <div class="chat-viz-head">
                                <div class="chat-viz-titles">
                                    <div class="chat-viz-title">${esc(title)}</div>
                                    <div class="chat-viz-meta">${esc(meta)}</div>
                                </div>
                                <div class="chat-viz-actions">
                                    <button class="chat-icon-btn" type="button" data-act="copy" title="Copy table" aria-label="Copy table">${COPY_SVG}</button>
                                    <button class="chat-icon-btn" type="button" data-act="download" title="Download CSV" aria-label="Download CSV">${DOWNLOAD_SVG}</button>
                                    <span class="chat-copied" data-role="copied" hidden>Copied!</span>
                                    <div class="chat-view-toggle" role="tablist" aria-label="View">
                                        <button class="chat-view-btn active" type="button" data-view="table" role="tab" aria-selected="true">Table</button>
                                        <button class="chat-view-btn" type="button" data-view="chart" role="tab" aria-selected="false">Chart</button>
                                    </div>
                                </div>
                            </div>
                            <div class="chat-table-slot" data-role="table">${this._buildTableHtml(results)}</div>
                            <div class="chat-chart-slot" data-role="chart" hidden></div>
                        </div>
                        <div class="chat-insights-slot" data-role="insights"></div>
                    </div>`);

                this.threadEl.appendChild(wrap);

                turn.el        = wrap;
                turn.tableSlot = wrap.querySelector('[data-role="table"]');
                turn.chartSlot = wrap.querySelector('[data-role="chart"]');
                turn.summaryEl = wrap.querySelector('[data-role="summary"]');
                const insSlot  = wrap.querySelector('[data-role="insights"]');

                const tableBtn = wrap.querySelector('[data-view="table"]');
                const chartBtn = wrap.querySelector('[data-view="chart"]');
                if (tableBtn) tableBtn.addEventListener('click', () => this._showTable(turn));
                if (chartBtn) chartBtn.addEventListener('click', () => this._showChart(turn));

                // Copy (TSV → clipboard) / Download (CSV) of this turn's result set.
                const copyBtn   = wrap.querySelector('[data-act="copy"]');
                const dlBtn     = wrap.querySelector('[data-act="download"]');
                const copiedEl  = wrap.querySelector('[data-role="copied"]');
                if (copyBtn) copyBtn.addEventListener('click', () => this._copyTurn(turn, copiedEl));
                if (dlBtn)   dlBtn.addEventListener('click', () => this._downloadTurn(turn));

                // Stream insights into this turn (gated by the AI Analytics preference).
                const aiAnalytics = (window.JeenPreferences && window.JeenPreferences.getAll().aiAnalytics) || 'on';
                if (aiAnalytics === 'on' && typeof window.InsightsManager === 'function') {
                    const mgr = new window.InsightsManager({
                        container: insSlot,
                        skipSummary: true,
                        onSummary: (html) => { turn.summaryEl.innerHTML = html; turn.summaryEl.hidden = false; },
                        onFollowUp: (fq) => this.send(fq),
                        showPromptInDevPanel: false,
                        devTrace: false,
                    });
                    turn.insightsManager = mgr;
                    setTimeout(() => mgr.generateInsights(turn.results, turn.question, turn.queryId, turn.sql), 0);
                }
            } else if (turn.answer) {
                // Conversational text answer (no SQL executed).
                wrap.innerHTML = this._wrapAssistant(`<div class="chat-card"><div class="chat-answer-text">${esc(turn.answer).replace(/\n/g, '<br>')}</div></div>`);
                this.threadEl.appendChild(wrap);
            } else {
                wrap.innerHTML = this._wrapAssistant(`<div class="chat-card"><div class="chat-answer-text chat-muted">${esc(data.error || 'No results to display.')}</div></div>`);
                this.threadEl.appendChild(wrap);
            }

            this._scrollToBottom();
        },

        _buildTableHtml(results) {
            const cols = results.columns || [];
            const rows = results.data || results.rows || [];
            if (!cols.length || !rows.length) {
                return '<div class="chat-no-data">No rows returned.</div>';
            }
            const shown = rows.slice(0, MAX_TABLE_ROWS);

            // Per-column numeric detection (>=70% of sampled non-null cells parse).
            const numeric = cols.map((col, i) => {
                let num = 0, nonNull = 0;
                for (let r = 0; r < shown.length; r++) {
                    const cell = Array.isArray(shown[r]) ? shown[r][i] : shown[r][col];
                    if (cell === null || cell === undefined || cell === '') continue;
                    nonNull++;
                    if (Number.isFinite(Number(cell)) && /^[-+]?\d/.test(String(cell).trim())) num++;
                }
                return nonNull > 0 && (num / nonNull) >= 0.7;
            });

            let html = '<div class="chat-table-scroll"><table class="chat-table"><thead><tr>';
            cols.forEach((col, i) => {
                html += `<th class="${numeric[i] ? 'chat-num' : ''}">${esc(col)}</th>`;
            });
            html += '</tr></thead><tbody>';
            shown.forEach((row) => {
                html += '<tr>';
                cols.forEach((col, i) => {
                    const cell = Array.isArray(row) ? row[i] : row[col];
                    const cls = numeric[i] ? 'chat-num' : '';
                    if (cell === null || cell === undefined || cell === '') {
                        html += `<td class="${cls} chat-null">\u2014</td>`;
                    } else {
                        const disp = numeric[i] ? fmtNum(cell) : String(cell);
                        html += `<td class="${cls}">${esc(disp)}</td>`;
                    }
                });
                html += '</tr>';
            });
            html += '</tbody></table></div>';
            if (rows.length > MAX_TABLE_ROWS) {
                html += `<div class="chat-table-more">Showing ${MAX_TABLE_ROWS} of ${rows.length.toLocaleString('en-US')} rows</div>`;
            }
            return html;
        },

        // ── Copy / Download the result set ────────────────────────────────────
        /** Serialize the full result set with a delimiter (\t for TSV, , for CSV). */
        _serializeResults(results, sep) {
            const cols = results.columns || [];
            const rows = results.data || results.rows || [];
            const cell = (v) => {
                let s = (v === null || v === undefined) ? '' : String(v);
                // CSV: quote fields containing the delimiter, quotes or newlines.
                if (sep === ',' && /[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
                else if (sep === '\t') s = s.replace(/\t/g, ' ').replace(/\r?\n/g, ' ');
                return s;
            };
            const lines = [cols.map(cell).join(sep)];
            rows.forEach((r) => {
                lines.push(cols.map((c, i) => cell(Array.isArray(r) ? r[i] : r[c])).join(sep));
            });
            return lines.join('\r\n');
        },

        async _copyText(text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
            }
            try {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                const ok = document.execCommand('copy');
                document.body.removeChild(ta);
                return ok;
            } catch (_) { return false; }
        },

        async _copyTurn(turn, copiedEl) {
            if (!turn || !turn.results) return;
            const ok = await this._copyText(this._serializeResults(turn.results, '\t'));
            if (!ok) { this._toast('Copy failed.'); return; }
            if (copiedEl) {
                copiedEl.hidden = false;
                clearTimeout(turn._copiedTimer);
                turn._copiedTimer = setTimeout(() => { copiedEl.hidden = true; }, 1600);
            }
        },

        /**
         * Download the visible artifact for this turn: the chart as PNG when the
         * turn is in chart view, otherwise the result set as CSV.
         */
        _downloadTurn(turn) {
            if (!turn || !turn.results) return;
            const title = (typeof window.deriveResultTitle === 'function')
                ? window.deriveResultTitle(turn.question) : 'results';
            const base = ((title || 'results').replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 60) || 'results');

            const chartActive = this.activeChartTurnId === turn.id && this._chartMgr;
            if (chartActive) {
                const dataUrl = (typeof this._chartMgr.getChartDataURL === 'function')
                    ? this._chartMgr.getChartDataURL() : null;
                if (!dataUrl) { this._toast('Chart is still rendering — try again in a moment.'); return; }
                const a = document.createElement('a');
                a.href = dataUrl;
                a.download = base + '.png';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                return;
            }

            // Table view → CSV. Prepend a BOM so Excel opens UTF-8 correctly.
            const csv = this._serializeResults(turn.results, ',');
            const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = base + '.csv';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 0);
        },

        // ── Table / Chart toggle (single active chart) ────────────────────────
        _showTable(turn) {
            this._setToggle(turn.el, 'table');
            if (this.activeChartTurnId === turn.id) {
                // This turn owns the chart engine — tear it down + return home.
                this._teardownChart();
            }
            if (turn.chartSlot) turn.chartSlot.hidden = true;
            // handleViewChange() may have left an inline display:none on the
            // table element — clear it so the `hidden` attribute governs again.
            if (turn.tableSlot) { turn.tableSlot.style.display = ''; turn.tableSlot.hidden = false; }
        },

        async _showChart(turn) {
            if (!turn.results) return;
            if (this.activeChartTurnId === turn.id) return; // already charting this turn

            // Move the single engine off any other turn first (bumps the token).
            this._teardownChart();
            const activationId = ++this._chartActivationId;

            if (!this._ChartManagerClass) {
                try {
                    const mod = await import(CHART_MANAGER_URL);
                    this._ChartManagerClass = mod.ChartManager;
                } catch (e) {
                    console.error('[ChatController] Failed to load ChartManager:', e);
                    this._toast('Chart engine failed to load.');
                    return;
                }
            }
            // A newer activation (or teardown) superseded us during the import.
            if (activationId !== this._chartActivationId) return;

            const chartView = document.getElementById('chart-view-container');
            if (!chartView) {
                this._toast('Chart view is unavailable.');
                return;
            }
            // Remember the engine's home so we can restore it for Ask mode.
            if (!this._chartHome) {
                this._chartHome = { parent: chartView.parentNode, next: chartView.nextSibling };
            }

            // Relocate the apparatus into this turn and flip the view.
            turn.chartSlot.appendChild(chartView);
            turn.chartSlot.hidden = false;
            turn.tableSlot.hidden = true;
            this._setToggle(turn.el, 'chart');

            const mgr = new this._ChartManagerClass();
            mgr.setContext({
                queryId: turn.queryId,
                question: turn.question,
                sql: turn.sql,
                tableEl: turn.tableSlot,
                chartViewEl: chartView,
                // Chat has its own per-turn toggle — don't let this engine render
                // into (and clobber) Ask mode's shared #chart-toggle-container.
                manageToggle: false,
            });
            this._chartMgr = mgr;
            this.activeChartTurnId = turn.id;
            this._activeChartTurn = turn;

            try {
                await mgr.initialize(turn.results);
                // Superseded while initializing (turn switch / left chat) — bail.
                if (activationId !== this._chartActivationId) {
                    try { mgr.dispose(); } catch (_) { /* noop */ }
                    return;
                }
                if (mgr.dataAnalysis && mgr.dataAnalysis.canChart === false) {
                    // Not chartable — revert cleanly to the table.
                    this._teardownChart();
                    this._showTable(turn);
                    this._toast('Chart view is not available for this data.');
                    return;
                }
                await mgr.handleViewChange('chart');
            } catch (e) {
                console.error('[ChatController] Chart activation failed:', e);
                this._teardownChart();
                this._showTable(turn);
                this._toast('Failed to render chart.');
            }
            this._scrollToBottom();
        },

        /** Dispose the active engine and move #chart-view-container back home. */
        _teardownChart() {
            // Supersede any in-flight activation so its post-await guards bail.
            this._chartActivationId++;
            if (this._chartMgr) {
                try { this._chartMgr.dispose(); } catch (_) { /* noop */ }
                this._chartMgr = null;
            }
            const chartView = document.getElementById('chart-view-container');
            if (chartView && this._chartHome) {
                chartView.style.display = 'none';
                this._chartHome.parent.insertBefore(chartView, this._chartHome.next);
            }
            if (this._activeChartTurn) {
                const t = this._activeChartTurn;
                if (t.chartSlot) t.chartSlot.hidden = true;
                if (t.tableSlot) { t.tableSlot.style.display = ''; t.tableSlot.hidden = false; }
                if (t.el) this._setToggle(t.el, 'table');
            }
            this.activeChartTurnId = null;
            this._activeChartTurn = null;
        },

        _setToggle(cardEl, view) {
            if (!cardEl) return;
            cardEl.querySelectorAll('.chat-view-btn').forEach((btn) => {
                const on = btn.dataset.view === view;
                btn.classList.toggle('active', on);
                btn.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            // The single Download button exports whatever is visible: PNG for the
            // chart, CSV for the table. Keep its tooltip/label in sync.
            const dl = cardEl.querySelector('[data-act="download"]');
            if (dl) {
                const label = view === 'chart' ? 'Download PNG' : 'Download CSV';
                dl.title = label;
                dl.setAttribute('aria-label', label);
            }
        },

        // ── Thread primitives ─────────────────────────────────────────────────
        _hideEmpty() {
            if (this.emptyEl) this.emptyEl.hidden = true;
        },

        _appendUserBubble(text) {
            const el = document.createElement('div');
            el.className = 'chat-turn chat-user';
            el.innerHTML = `<div class="chat-row chat-row-user">`
                + `<div class="chat-user-bubble">${esc(text)}</div>`
                + `<div class="chat-avatar chat-avatar-user" aria-hidden="true">${esc(this._userInitials())}</div>`
                + `</div>`;
            this.threadEl.appendChild(el);
            this.messages.push({ role: 'user', text });
            this._scrollToBottom();
        },

        _appendThinking() {
            const el = document.createElement('div');
            el.className = 'chat-turn chat-assistant';
            el.innerHTML = this._wrapAssistant(`<div class="chat-thinking" role="status" aria-label="Working on your question">` +
                PIPELINE.map((label, i) =>
                    `<div class="chat-think-step" data-step="${i}"><span class="chat-think-dot"></span><span class="chat-think-label">${esc(label)}</span></div>`
                ).join('') + `</div>`);
            this.threadEl.appendChild(el);
            this._scrollToBottom();

            let step = 0;
            const apply = () => {
                el.querySelectorAll('.chat-think-step').forEach((s, i) => {
                    s.classList.toggle('is-done', i < step);
                    s.classList.toggle('is-active', i === step);
                    s.classList.toggle('is-pending', i > step);
                });
            };
            apply();
            // Advance through the early phases on a light timer, then hold at
            // "Generating SQL" until the real response arrives.
            const timers = [];
            timers.push(setTimeout(() => { step = 1; apply(); }, 500));
            timers.push(setTimeout(() => { step = 2; apply(); }, 1100));

            return {
                el,
                stop: () => {
                    timers.forEach(clearTimeout);
                    if (el.parentNode) el.parentNode.removeChild(el);
                },
            };
        },

        _appendError(message) {
            const el = document.createElement('div');
            el.className = 'chat-turn chat-assistant';
            el.innerHTML = this._wrapAssistant(`<div class="chat-card chat-error"><span class="chat-error-icon">\u26a0\ufe0f</span><span>${esc(message)}</span></div>`);
            this.threadEl.appendChild(el);
            this._scrollToBottom();
        },

        _scrollToBottom() {
            if (!this.threadEl) return;
            // rAF so layout (freshly-inserted node) is settled before we measure.
            requestAnimationFrame(() => {
                this.threadEl.scrollTop = this.threadEl.scrollHeight;
            });
        },

        _toast(message) {
            if (typeof window.showToast === 'function') window.showToast(message, 'info');
            else console.warn('[ChatController]', message);
        },
    };

    window.ChatController = ChatController;
})();
