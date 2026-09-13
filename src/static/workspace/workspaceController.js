/**
 * Insights Workspace v3
 * One conversation store drives the thread, selected result, chart, table and dock.
 */
(function () {
    'use strict';

    const PHASES = [
        { id: 'memory', label: 'Memory' },
        { id: 'router', label: 'Route question' },
        { id: 'catalog', label: 'Load catalog' },
        { id: 'generation', label: 'Generate query' },
        { id: 'validation', label: 'Validate' },
        { id: 'execution', label: 'Execute' },
        { id: 'analytics', label: 'Analyze' },
        { id: 'format', label: 'Format answer' },
        { id: 'save', label: 'Save memory' },
    ];

    const NODE_PHASE = {
        memory_shrink_check: 'memory',
        memory_summarizer: 'memory',
        memory_answer_generator: 'memory',
        fused_router: 'router',
        catalog_lookup: 'catalog',
        prompt_builder: 'catalog',
        dax_catalog_lookup: 'catalog',
        dax_entity_resolver: 'catalog',
        dax_prompt_builder: 'catalog',
        dax_query_planner: 'generation',
        sql_generator: 'generation',
        dax_generator: 'generation',
        dax_repair: 'generation',
        sqlglot_validate: 'validation',
        dlp_check: 'validation',
        dax_static_validate: 'validation',
        result_integrity_check: 'validation',
        execute_query: 'execution',
        pbi_execute_query: 'execution',
        trivial_result_check: 'execution',
        feedback_classifier: 'execution',
        dax_feedback_router: 'execution',
        fused_eval_analytics: 'analytics',
        response_formatter: 'format',
        save_to_memory: 'save',
        observability_log: 'save',
    };

    const esc = (value) => {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(String(value == null ? '' : value));
        return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    };

    function textOf(value) {
        if (value == null) return '';
        if (typeof value === 'string' || typeof value === 'number') return String(value);
        if (Array.isArray(value)) return value.map(textOf).join('');
        if (typeof value === 'object') return textOf(value.t || value.text || value.content || '');
        return '';
    }

    function directionOf(value) {
        const text = textOf(value);
        const rtlCount = (text.match(/[\u0590-\u05ff\u0600-\u08ff]/g) || []).length;
        const ltrCount = (text.match(/[A-Za-z\u00c0-\u02af]/g) || []).length;
        return rtlCount > ltrCount ? 'rtl' : 'ltr';
    }

    function formatMs(ms) {
        if (ms === null || ms === undefined || ms === '') return '—';
        const n = Number(ms);
        if (!Number.isFinite(n)) return '—';
        return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 1 : 2)}s` : `${Math.round(n)}ms`;
    }

    function formatCompact(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return value == null ? '—' : String(value);
        return new Intl.NumberFormat(undefined, { notation: n >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n);
    }

    function rowValue(row, column, index) {
        return Array.isArray(row) ? row[index] : row && row[column];
    }

    function normalizeRows(results) {
        return (results && (results.rows || results.data)) || [];
    }

    function filterResultRows(results, query) {
        const columns = (results && results.columns) || [];
        const rows = normalizeRows(results);
        const needle = String(query || '').trim().toLowerCase();
        if (!needle) return rows.slice();
        return rows.filter((row) => columns.some((column, index) =>
            String(rowValue(row, column, index) ?? '').toLowerCase().includes(needle)
        ));
    }

    function selectionForTurn(selectedResultId, turn) {
        return {
            selectedTurnId: turn?.id || null,
            selectedResultId: turn?.status === 'success' ? turn.id : selectedResultId,
        };
    }

    function inferColumnType(values) {
        const present = values.filter((v) => v !== null && v !== undefined && v !== '');
        if (!present.length) return 'empty';
        if (present.every((v) => typeof v === 'number' || (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))))) return 'number';
        if (present.every((v) => typeof v === 'boolean')) return 'boolean';
        if (present.every((v) => !Number.isNaN(Date.parse(v)) && /[-/:T]/.test(String(v)))) return 'datetime';
        return 'text';
    }

    function compactProfile(results) {
        const columns = (results && results.columns) || [];
        const rows = normalizeRows(results);
        return columns.map((name, index) => {
            const values = rows.map((row) => rowValue(row, name, index));
            const present = values.filter((v) => v !== null && v !== undefined && v !== '');
            const type = inferColumnType(values);
            let range = 'No non-null values';
            if (present.length) {
                if (type === 'number') {
                    const nums = present.map(Number);
                    range = `${formatCompact(Math.min(...nums))} – ${formatCompact(Math.max(...nums))}`;
                } else {
                    const strings = present.map(String).sort((a, b) => a.localeCompare(b));
                    range = `${strings[0]} – ${strings[strings.length - 1]}`;
                }
            }
            return {
                name,
                type,
                range,
                distinct: new Set(present.map((v) => String(v))).size,
                nullPct: rows.length ? Math.round(((rows.length - present.length) / rows.length) * 1000) / 10 : 0,
                fillPct: rows.length ? Math.max(4, (present.length / rows.length) * 100) : 0,
            };
        });
    }

    function cappedMeta(results) {
        if (!results) return { capped: false };
        const cap = results.cap || results.row_cap || results.max_rows || null;
        const total = results.total_matched || results.total_rows || null;
        const loaded = normalizeRows(results).length;
        const capped = Boolean(results.truncated || results.is_partial || (cap && loaded >= cap));
        return { capped, cap: cap || loaded, total, loaded };
    }

    function safeTraceNote(event) {
        const node = event?.node || '';
        if (/generator|repair|prompt_builder|summarizer|memory_answer/.test(node)) {
            return event.type === 'llm' ? 'model step completed' : 'context prepared';
        }
        if (node === 'fused_eval_analytics') return 'analysis completed';
        if (node === 'execute_query' || node === 'pbi_execute_query') {
            return /^\d+ rows/.test(event.detail || '') ? event.detail : 'read-only query completed';
        }
        const safeNodes = new Set([
            'memory_shrink_check', 'fused_router', 'catalog_lookup',
            'dax_catalog_lookup', 'sqlglot_validate', 'dlp_check',
            'dax_static_validate', 'trivial_result_check', 'feedback_classifier',
            'dax_feedback_router', 'result_integrity_check', 'response_formatter',
            'save_to_memory', 'observability_log',
        ]);
        return safeNodes.has(node) ? String(event.detail || event.type || '') : String(event.type || '');
    }

    const ICON = {
        table: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/></svg>',
        history: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
        settings: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21h-4v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.8-2.8.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3v-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.8-2.8.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5V3h4v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.8 2.8-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.2v4h-.2a1.7 1.7 0 0 0-1.4 1Z"/></svg>',
        conversation: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/></svg>',
        railConversation: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/><path d="M8 10h8M8 14h5"/></svg>',
        pin: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 3h6l-1 6 3 3H7l3-3-1-6Z"/></svg>',
        bell: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg>',
        export: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 3v12M7 8l5-5 5 5M5 14v6h14v-6"/></svg>',
        copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>',
        code: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/></svg>',
    };

    const WorkspaceController = {
        turns: [],
        selectedTurnId: null,
        selectedResultId: null,
        seq: 0,
        sending: false,
        activeTab: 'conversation',
        dockTab: 'sql',
        dockOpen: false,
        _sqlDockWasOpen: false,
        chartCollapsed: true,
        filter: '',
        mapSelectedRows: new Set(),
        desktopPreference: true,
        autoCollapsed: false,
        lastAppliedResultId: null,
        // Conversation persistence: the restored conversation header, the
        // hydration state and the generation guard that lets a connection
        // switch / reset / new hydration invalidate anything still in flight
        // (a live query stream, a /last fetch, an artifact fetch).
        conversation: null,
        hydrating: false,
        readOnly: false,
        _pendingOpen: null,
        _generation: 0,
        _streamAbort: null,
        _hydrateAbort: null,
        _hydration: null,

        init() {
            if (document.getElementById('v3-shell')) return;
            this._buildShell();
            this._moveProductionNodes();
            this._bind();
            document.addEventListener('jeen:connection-resolved', (event) => {
                const sourceKey = event.detail?.source_key;
                if (sourceKey) this.hydrate(sourceKey);
            });
            document.addEventListener('jeen:osm-map-select', (event) => {
                const indexes = Array.isArray(event.detail?.rowIndexes) ? event.detail.rowIndexes : [];
                this.mapSelectedRows = new Set(indexes.map(Number));
                this.renderTable();
                const first = indexes[0];
                const target = document.querySelector(`#v3-grid [data-source-row="${first}"]`);
                target?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            });
            document.addEventListener('jeen:osm-map-ready', () => this.renderTable());
            this.setTab('conversation');
            this._renderEmptySuggestions();
            this._applyResponsive();
            this.render();
            document.body.classList.add('v3-ready');
            document.body.classList.remove('v3-booting');
            window.askQuestion = () => this.submitComposer();
            window._jeenQuestionClick = (q) => this.send(q);
            window._fillFollowUp = (q) => this.send(q);
            window.ChatController = this;
        },

        _buildShell() {
            const shell = document.createElement('div');
            shell.id = 'v3-shell';
            shell.className = 'v3-shell';
            shell.innerHTML = `
              <nav class="v3-rail" aria-label="Primary navigation">
                <img class="v3-logo" src="/static/images/jeen-mark.png" alt="Jeen">
                <div class="v3-rail-divider"></div>
                <button class="v3-rail-btn is-active" data-rail="conversation" aria-label="Conversation" data-tooltip="Conversation">${ICON.railConversation}</button>
                <button class="v3-rail-btn" data-rail="tables" aria-label="Tables" data-tooltip="Tables">${ICON.table}</button>
                <button class="v3-rail-btn" data-rail="pinned" aria-label="Pinned questions" data-tooltip="Pinned">${ICON.pin}</button>
                <button class="v3-rail-btn" data-rail="history" aria-label="Conversation history" data-tooltip="History">${ICON.history}</button>
                <div class="v3-rail-spacer"></div>
                <button id="v3-settings-button" class="v3-rail-btn v3-rail-btn--settings" data-rail="settings" aria-label="Settings" data-tooltip="Settings">${ICON.settings}</button>
              </nav>
              <div class="v3-app">
                <header class="v3-topbar">
                  <button id="v3-conversation-toggle" class="v3-conversation-toggle" aria-expanded="true" aria-controls="v3-conversation">
                    ${ICON.conversation}<span>Hide conversation</span>
                  </button>
                  <div id="v3-connection-slot" class="v3-connection-slot"></div>
                  <div class="v3-topbar-spacer"></div>
                  <div id="v3-theme-slot"></div>
                  <button class="v3-topbar-icon" aria-label="Notifications" title="Notifications">${ICON.bell}</button>
                  <div id="v3-user-slot"></div>
                </header>
                <div class="v3-body">
                  <div id="v3-drawer-overlay" class="v3-drawer-overlay"></div>
                  <aside id="v3-conversation" class="v3-conversation" aria-label="Conversation workspace">
                    <div class="v3-tabs-wrap">
                      <div class="v3-tabs" role="tablist" aria-label="Conversation sections">
                        <button id="v3-tab-conversation" class="v3-tab" data-tab="conversation" role="tab" aria-selected="true" aria-controls="v3-panel-conversation">Conversation</button>
                        <button id="v3-tab-tables" class="v3-tab" data-tab="tables" role="tab" aria-selected="false" aria-controls="v3-panel-tables">Tables</button>
                        <button id="v3-tab-pinned" class="v3-tab" data-tab="pinned" role="tab" aria-selected="false" aria-controls="v3-panel-pinned">Pinned</button>
                        <button id="v3-tab-conversations" class="v3-tab" data-tab="conversations" role="tab" aria-selected="false" aria-controls="v3-panel-conversations">History</button>
                      </div>
                    </div>
                    <section id="v3-panel-conversation" class="v3-panel" data-panel="conversation" role="tabpanel" aria-labelledby="v3-tab-conversation">
                      <div id="v3-thread" class="v3-thread" role="log" aria-live="polite"></div>
                    </section>
                    <section id="v3-panel-conversations" class="v3-panel v3-panel-list" data-panel="conversations" role="tabpanel" aria-labelledby="v3-tab-conversations" hidden>
                      <div class="v3-conversations-head">
                        <span class="v3-thread-empty-label">Previous conversations</span>
                        <button class="v3-text-btn" data-question-log title="Flat log of every question asked">Question log</button>
                      </div>
                      <div id="v3-conversations-list" class="v3-conversations-list"></div>
                    </section>
                    <section id="v3-panel-tables" class="v3-panel v3-panel-list" data-panel="tables" role="tabpanel" aria-labelledby="v3-tab-tables" hidden>
                      <div id="v3-table-search-slot" class="v3-panel-search"></div>
                      <div id="v3-tables-slot"></div>
                    </section>
                    <section id="v3-panel-pinned" class="v3-panel v3-panel-list" data-panel="pinned" role="tabpanel" aria-labelledby="v3-tab-pinned" hidden>
                      <div id="v3-question-search-slot" class="v3-panel-search"></div>
                      <div class="v3-thread-empty-label">Pinned & recent questions</div>
                      <div id="v3-pinned-slot"></div>
                    </section>
                    <div class="v3-composer-wrap">
                      <div class="v3-composer">
                        <div id="v3-input-slot"></div>
                        <div id="v3-suggestions-slot"></div>
                        <div class="v3-composer-bottom">
                          <span class="v3-composer-hint">@ tables · # columns · / templates</span>
                          <span class="v3-composer-spacer"></span>
                          <div id="v3-ask-slot"></div>
                        </div>
                      </div>
                    </div>
                  </aside>
                  <main class="v3-workspace" aria-live="polite">
                    <header class="v3-result-head">
                      <div class="v3-result-copy">
                        <h1 id="v3-result-title" class="v3-result-title">Ask a question to get started</h1>
                        <div id="v3-meta-row" class="v3-meta-row"></div>
                      </div>
                      <div id="v3-actions" class="v3-actions"></div>
                    </header>
                    <div class="v3-scroll">
                      <div id="v3-placeholder" class="v3-placeholder">
                        <strong>No result yet</strong>
                        <span>Ask a question on the left. The chart, rows, SQL and profiling for that answer appear here.</span>
                      </div>
                      <section id="v3-chart-block" class="v3-data-block" hidden>
                        <div class="v3-toolbar">
                          <span id="v3-chart-caption" class="v3-caption">Chart</span>
                          <span class="v3-toolbar-spacer"></span>
                          <div id="v3-chart-types" class="v3-chart-types"></div>
                          <button id="v3-chart-toggle" class="v3-text-btn">Expand</button>
                        </div>
                        <div id="v3-chart-frame" class="v3-chart-frame"></div>
                        <div id="v3-chart-edit" class="v3-chart-edit"></div>
                      </section>
                      <section id="v3-table-block" class="v3-data-block" hidden>
                        <div class="v3-toolbar">
                          <span id="v3-row-caption" class="v3-caption"></span>
                          <span class="v3-toolbar-spacer"></span>
                          <input id="v3-result-filter" type="search" placeholder="Filter rows…" aria-label="Filter result rows">
                          <div id="v3-describe-slot"></div>
                        </div>
                        <div id="v3-cap-banner" class="v3-cap-banner" hidden></div>
                        <div id="v3-grid-wrap" class="v3-grid-wrap"><div id="v3-grid" class="v3-grid"></div></div>
                        <div id="v3-describe-content"></div>
                      </section>
                    </div>
                    <section class="v3-dock">
                      <div class="v3-dock-bar">
                        ${ICON.code}
                        <div class="v3-dock-tabs" role="tablist" aria-label="Result details">
                          <button class="v3-dock-tab" data-dock="sql" role="tab">SQL & run details</button>
                          <button class="v3-dock-tab" data-dock="profiling" role="tab">Profiling</button>
                        </div>
                        <span id="v3-dock-meta" class="v3-dock-meta">no run yet</span>
                        <button id="v3-dock-toggle" class="v3-text-btn" aria-expanded="false">Show</button>
                      </div>
                      <div id="v3-dock-body" class="v3-dock-body" hidden></div>
                    </section>
                  </main>
                </div>
              </div>`;
            document.body.insertBefore(shell, document.body.firstChild);
        },

        _move(selector, targetSelector, setup) {
            const node = document.querySelector(selector);
            const target = document.querySelector(targetSelector);
            if (!node || !target) return null;
            if (setup) setup(node);
            target.appendChild(node);
            return node;
        },

        _moveProductionNodes() {
            this._move('.connection-switcher', '#v3-connection-slot');
            this._move('#theme-toggle', '#v3-theme-slot');
            this._move('#user-menu-wrap', '#v3-user-slot');
            this.input = this._move('#question-input', '#v3-input-slot', (node) => {
                node.rows = 2;
                node.placeholder = 'Ask a follow-up about your data…';
                node.removeAttribute('style');
            });
            this.suggestions = this._move('#question-suggestions', '#v3-suggestions-slot');
            this.askButton = this._move('#ask-button', '#v3-ask-slot', (node) => {
                node.className = 'v3-ask';
                node.style.display = '';
                node.innerHTML = 'Ask <span style="opacity:.6">↵</span>';
            });
            this._move('#table-search', '#v3-table-search-slot', (node) => { node.style.display = ''; });
            this._move('#tables-list', '#v3-tables-slot');
            this._move('#question-search', '#v3-question-search-slot', (node) => {
                node.style.display = '';
                node.placeholder = 'Search your questions…';
            });
            this._move('#question-history', '#v3-pinned-slot');

            const actions = [
                ['#export-btn', 'Export', 'v3-icon-action', ICON.export],
                ['#copy-results-btn', 'Copy', 'v3-icon-action', ICON.copy],
                ['#send-result-btn', 'Send', '', null],
            ];
            actions.forEach(([selector, label, className, icon]) => {
                this._move(selector, '#v3-actions', (node) => {
                    node.style.display = '';
                    node.className = className;
                    node.title = label;
                    node.setAttribute('aria-label', label);
                    if (icon) node.innerHTML = icon;
                });
            });
            this._move('#describe-btn', '#v3-describe-slot', (node) => {
                node.style.display = '';
                node.className = 'v3-text-btn';
                node.textContent = 'Describe';
            });
            this._move('#describe-section', '#v3-describe-content');
            this._move('#chart-type-selector-container', '#v3-chart-types', (node) => { node.style.display = ''; });
            this._move('#chart-options-panel-container', '#v3-chart-types', (node) => { node.style.display = ''; });
            const chart = this._move('#chart-view-container', '#v3-chart-frame', (node) => {
                node.style.display = 'block';
            });
            if (chart) {
                const chat = chart.querySelector('#chart-chat-container');
                if (chat) document.getElementById('v3-chart-edit').appendChild(chat);
            }
            this._setActionsEnabled(false);
        },

        _bind() {
            document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => this.setTab(button.dataset.tab)));
            document.querySelectorAll('[data-dock]').forEach((button) => button.addEventListener('click', () => this.toggleDock(button.dataset.dock)));
            document.querySelector('.v3-tabs').addEventListener('keydown', (event) => this._tabKeydown(event, '[data-tab]', (button) => this.setTab(button.dataset.tab)));
            document.querySelector('.v3-dock-tabs').addEventListener('keydown', (event) => this._tabKeydown(event, '[data-dock]', (button) => {
                this.dockTab = button.dataset.dock;
                this.dockOpen = true;
                this.renderDock();
            }));
            document.querySelectorAll('[data-rail]').forEach((button) => button.addEventListener('click', () => this._rail(button.dataset.rail)));
            document.getElementById('v3-conversation-toggle').addEventListener('click', () => this.toggleConversation());
            document.getElementById('v3-drawer-overlay').addEventListener('click', () => this.setConversation(false, true));
            document.getElementById('v3-dock-toggle').addEventListener('click', () => this.toggleDock(this.dockTab));
            document.querySelector('[data-question-log]')?.addEventListener('click', () => document.getElementById('history-btn')?.click());
            document.getElementById('v3-chart-toggle').addEventListener('click', () => {
                this.chartCollapsed = !this.chartCollapsed;
                this._renderChartCollapse();
            });
            document.getElementById('v3-result-filter').addEventListener('input', (event) => {
                this.filter = event.target.value.toLowerCase();
                this.renderTable();
            });
            if (this.askButton) this.askButton.addEventListener('click', (event) => {
                event.preventDefault();
                this.submitComposer();
            });
            if (this.input) {
                this.input.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                        event.preventDefault();
                        this.submitComposer();
                    }
                }, true);
                this.input.addEventListener('input', () => {
                    this.input.style.height = 'auto';
                    this.input.style.height = `${Math.min(this.input.scrollHeight, 140)}px`;
                });
            }
            window.addEventListener('resize', () => this._applyResponsive());
            document.addEventListener('keydown', (event) => {
                if (event.key === 'Escape' && window.innerWidth < 900) this.setConversation(false, true);
            });
        },

        _tabKeydown(event, selector, activate) {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const buttons = [...event.currentTarget.querySelectorAll(selector)];
            const current = Math.max(0, buttons.indexOf(document.activeElement));
            let next = current;
            if (event.key === 'ArrowRight') next = (current + 1) % buttons.length;
            if (event.key === 'ArrowLeft') next = (current - 1 + buttons.length) % buttons.length;
            if (event.key === 'Home') next = 0;
            if (event.key === 'End') next = buttons.length - 1;
            event.preventDefault();
            buttons[next].focus();
            activate(buttons[next]);
        },

        _rail(action) {
            // 'new' is kept as an alias so older callers (onboarding) still work.
            if (action === 'conversation' || action === 'new') {
                this.setTab('conversation');
                this.setConversation(true);
                if (this.input) this.input.focus();
            } else if (action === 'tables') {
                this.setTab('tables');
                this.setConversation(true);
                if (typeof window.loadTables === 'function') window.loadTables();
            } else if (action === 'pinned') {
                this.setTab('pinned');
                this.setConversation(true);
            } else if (action === 'history') {
                this.setTab('conversations');
                this.setConversation(true);
            } else if (action === 'settings') {
                if (window._settingsPage?.toggle) window._settingsPage.toggle();
                else document.getElementById('settings-btn')?.click();
            }
        },

        setTab(tab) {
            this.activeTab = tab;
            document.querySelectorAll('[data-tab]').forEach((button) => {
                const active = button.dataset.tab === tab;
                button.setAttribute('aria-selected', String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
            // Rail icon <-> panel tab mapping (the History icon opens the
            // 'conversations' panel; 'new' is the legacy id of the first icon).
            const railForTab = { conversation: ['conversation', 'new'], tables: ['tables'], pinned: ['pinned'], conversations: ['history'] };
            const activeRails = railForTab[tab] || [];
            document.querySelectorAll('[data-rail]').forEach((button) => button.classList.toggle('is-active', activeRails.includes(button.dataset.rail)));
            if (tab === 'tables' && typeof window.loadTables === 'function') window.loadTables();
            if (tab === 'pinned' && typeof window.displayHistory === 'function') window.displayHistory();
            if (tab === 'conversations') this.loadConversationList();
        },

        toggleConversation() {
            const panel = document.getElementById('v3-conversation');
            const open = panel.hidden || (!panel.classList.contains('v3-force-open') && window.innerWidth <= 1100);
            this.setConversation(open, !open);
        },

        setConversation(open, restoreFocus = false) {
            const panel = document.getElementById('v3-conversation');
            const overlay = document.getElementById('v3-drawer-overlay');
            if (window.innerWidth <= 1100) {
                panel.hidden = false;
                panel.classList.toggle('v3-force-open', open);
            } else {
                panel.hidden = !open;
                panel.classList.remove('v3-force-open');
                this.desktopPreference = open;
            }
            overlay.classList.toggle('is-open', open && window.innerWidth < 900);
            overlay.setAttribute('aria-hidden', String(!(open && window.innerWidth < 900)));
            panel.setAttribute('aria-hidden', String(!open));
            const toggle = document.getElementById('v3-conversation-toggle');
            toggle.setAttribute('aria-expanded', String(open));
            toggle.querySelector('span').textContent = open ? 'Hide conversation' : 'Conversation';
            if (!open && restoreFocus) toggle.focus();
            setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
        },

        _applyResponsive() {
            if (window.innerWidth <= 1100) {
                if (!this.autoCollapsed) {
                    this.autoCollapsed = true;
                    this.setConversation(false);
                }
            } else if (this.autoCollapsed) {
                this.autoCollapsed = false;
                this.setConversation(this.desktopPreference);
            }
        },

        submitComposer() {
            const question = (this.input && this.input.value || '').trim();
            if (!question) return;
            this.input.value = '';
            this.input.style.height = '';
            this.send(question);
        },

        async send(question) {
            const q = String(question || '').trim();
            if (!q || this.sending) return;
            if (this.readOnly) {
                if (typeof window.showToast === 'function') {
                    window.showToast('This conversation belongs to a connection that is no longer available. Start a new conversation to ask questions.', 'error');
                }
                return;
            }
            const connection = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            if (!connection) {
                if (typeof window.showToast === 'function') window.showToast('Select a connection first', 'error');
                return;
            }

            this.sending = true;
            this.setTab('conversation');
            this.setConversation(true);
            // A live send does not bump the generation (it belongs to the current
            // conversation) but records it, so a switch/reset that happens while
            // the stream is open can abort the fetch and drop late events.
            const generation = this._generation;
            const abort = new AbortController();
            this._streamAbort = abort;
            const turn = {
                id: `turn-${Date.now()}-${++this.seq}`,
                question: q,
                status: 'running',
                startedAt: performance.now(),
                phaseState: Object.fromEntries(PHASES.map((phase) => [phase.id, 'pending'])),
                trace: [],
                traceOpen: false,
                result: null,
                error: null,
            };
            this.turns.push(turn);
            this.selectedTurnId = turn.id;
            this._setComposerBusy(true);
            this.render();
            this._scrollThread();

            const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
            const payload = {
                question: q,
                connection,
                session_id: typeof window._jeenGetSessionId === 'function' ? window._jeenGetSessionId() : null,
                eval_analytics: (prefs.aiAnalytics || 'on') === 'on',
            };
            if (prefs.resultLimit) payload.limit = Number(prefs.resultLimit);
            if (prefs.temperature !== undefined && prefs.temperature !== null) payload.temperature = Number(prefs.temperature);
            const llmTimeout = window.JeenPreferences && window.JeenPreferences.getLlmTimeoutSeconds();
            if (llmTimeout !== null && llmTimeout !== undefined) payload.llm_timeout = llmTimeout;

            const stale = () => generation !== this._generation;
            try {
                await this._stream(payload, (event, data) => {
                    if (stale()) return;
                    if (event === 'node') this._onNode(turn, data);
                    if (event === 'result') this._onResult(turn, data);
                    if (event === 'enrichment') this._onEnrichment(turn, data);
                    if (event === 'error') throw new Error(data.detail || data.error || 'Query failed');
                }, abort.signal);
                if (stale()) return;
                if (turn.status === 'running') throw new Error('Query stream ended before a result arrived');
            } catch (error) {
                if (stale() || abort.signal.aborted) return;
                this._onError(turn, error);
            } finally {
                if (this._streamAbort === abort) this._streamAbort = null;
                if (!stale()) {
                    this.sending = false;
                    this._setComposerBusy(false);
                    this.render();
                }
            }
        },

        async _stream(payload, onEvent, signal) {
            const response = await fetch('/api/ask/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
                body: JSON.stringify(payload),
                signal,
            });
            if (!response.ok) {
                const body = await response.text();
                throw new Error(body || `Query failed (${response.status})`);
            }
            if (!response.body) throw new Error('Streaming is unavailable in this browser');
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { done, value } = await reader.read();
                buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n');
                let boundary;
                while ((boundary = buffer.indexOf('\n\n')) >= 0) {
                    const block = buffer.slice(0, boundary);
                    buffer = buffer.slice(boundary + 2);
                    if (!block || block.startsWith(':')) continue;
                    let event = 'message';
                    const dataLines = [];
                    block.split('\n').forEach((line) => {
                        if (line.startsWith('event:')) event = line.slice(6).trim();
                        if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
                    });
                    let data = {};
                    if (dataLines.length) {
                        try { data = JSON.parse(dataLines.join('\n')); } catch (_) { data = { detail: dataLines.join('\n') }; }
                    }
                    onEvent(event, data);
                }
                if (done) break;
            }
        },

        _onNode(turn, event) {
            const phase = NODE_PHASE[event.node] || 'execution';
            if (event.status === 'node_started') {
                turn.phaseState[phase] = 'running';
                turn.trace.push({ ...event, startedAt: performance.now() });
            } else {
                const open = [...turn.trace].reverse().find((item) => item.node === event.node && item.status === 'node_started' && !item.closed);
                if (open) open.closed = true;
                turn.trace.push({ ...event });
                turn.phaseState[phase] = event.status === 'node_failed' ? 'error' : 'done';
            }
            this.renderConversation();
            this._scrollThread();
        },

        _onResult(turn, data) {
            if (data.error && !data.results) {
                this._onError(turn, new Error(data.error), data);
                return;
            }
            const used = new Set();
            (data.trace || []).forEach((raw) => {
                const index = turn.trace.findIndex((event, eventIndex) =>
                    !used.has(eventIndex)
                    && event.status === 'node_finished'
                    && event.node === raw.node
                );
                if (index >= 0) {
                    turn.trace[index] = { ...turn.trace[index], ...raw, status: 'node_finished' };
                    used.add(index);
                } else {
                    turn.trace.push({ ...raw, status: 'node_finished' });
                }
                turn.phaseState[NODE_PHASE[raw.node] || 'execution'] = 'done';
            });
            turn.status = 'success';
            // Onboarding signal: a question was answered successfully.
            document.dispatchEvent(new CustomEvent('jeen:onboarding:ask_first_question'));
            turn.result = data;
            turn.durationMs = Math.round(performance.now() - turn.startedAt);
            turn.phaseState.format = 'done';
            turn.phaseState.save = 'done';
            this._captureSelectedChart();
            this.selectedTurnId = turn.id;
            this.selectedResultId = turn.id;
            this.filter = '';
            if (data.session_id && typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(data.session_id);
            if (!this.conversation && data.session_id) {
                // First answer of a fresh conversation: mirror the header the
                // server derives (title = first question) until the next hydration.
                this.conversation = { id: String(data.session_id), title: turn.question, source_key: null };
            }
            this.render();
        },

        _onError(turn, error, data) {
            turn.status = 'error';
            turn.error = error && error.message ? error.message : String(error);
            turn.result = data || turn.result;
            turn.durationMs = Math.round(performance.now() - turn.startedAt);
            this.selectedTurnId = turn.id;
            Object.keys(turn.phaseState).forEach((key) => {
                if (turn.phaseState[key] === 'running') turn.phaseState[key] = 'error';
            });
            this.render();
        },

        _onEnrichment(turn, data) {
            if (!turn.result || !data) return;
            Object.assign(turn.result, data);
            if (data.result_handle) window._resultHandle = data.result_handle;
            if (turn.id === this.selectedResultId) this._setActionsEnabled(true);
        },

        selectTurn(id) {
            const turn = this.turns.find((item) => item.id === id);
            if (!turn) return;
            this._captureSelectedChart();
            const selection = selectionForTurn(this.selectedResultId, turn);
            this.selectedTurnId = selection.selectedTurnId;
            this.selectedResultId = selection.selectedResultId;
            if (turn.status === 'success') {
                this.filter = '';
                document.getElementById('v3-result-filter').value = '';
            }
            this.render();
            // Restored turns fetch their rows lazily on selection. The
            // generation + selection guard inside _loadArtifact drops a slow
            // response for a turn the user has already left.
            if (turn.restored && turn.artifactState === 'missing' && turn.snapshotStatus === 'stored') {
                this._loadArtifact(turn);
            }
        },

        _captureSelectedChart() {
            const current = this.turns.find((item) => item.id === this.selectedResultId);
            if (current && current.result?.results && window.JeenLegacyBridge?.getChartState) {
                const state = window.JeenLegacyBridge.getChartState();
                if (state && state.chart_config) current.chartState = state;
            }
        },

        _abortInFlight() {
            if (this._streamAbort) {
                try { this._streamAbort.abort(); } catch (_) { /* already settled */ }
                this._streamAbort = null;
            }
            if (this._hydrateAbort) {
                try { this._hydrateAbort.abort(); } catch (_) { /* already settled */ }
                this._hydrateAbort = null;
            }
        },

        reset() {
            this._generation += 1;
            this._abortInFlight();
            this.turns = [];
            this.conversation = null;
            this.hydrating = false;
            this.readOnly = false;
            this.selectedTurnId = null;
            this.selectedResultId = null;
            this.lastAppliedResultId = null;
            this.filter = '';
            this.sending = false;
            this._setComposerBusy(false);
            if (typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(null);
            this.render();
        },

        /** Explicit "New conversation": clear the thread; the server creates the
         *  conversation row lazily on the first question. */
        newConversation() {
            this.reset();
            this.setTab('conversation');
            this.setConversation(true);
            if (this.input) this.input.focus();
        },

        async _hydrationModule() {
            if (this._hydration) return this._hydration;
            if (window.ConversationHydration) {
                this._hydration = window.ConversationHydration;
                return this._hydration;
            }
            const module = await import('./conversationHydration.js?v=1');
            this._hydration = module;
            return module;
        },

        /**
         * Restore the user's last conversation on `sourceKey`. Called once the
         * active connection is resolved on load and again on every switch.
         * Any earlier hydration or live stream is invalidated first.
         */
        async hydrate(sourceKey, options = {}) {
            // A conversation opened from the History tab on another connection
            // is remembered across the connection switch that precedes it.
            const pendingOpen = this._pendingOpen;
            this._pendingOpen = null;
            const conversationId = options.conversationId
                || (pendingOpen && pendingOpen.sourceKey === sourceKey ? pendingOpen.conversationId : null);
            const readOnly = Boolean(options.readOnly);
            if (!sourceKey && !conversationId) return;
            this._generation += 1;
            const generation = this._generation;
            this._abortInFlight();
            const abort = new AbortController();
            this._hydrateAbort = abort;
            this.turns = [];
            this.conversation = null;
            this.readOnly = readOnly;
            this.selectedTurnId = null;
            this.selectedResultId = null;
            this.lastAppliedResultId = null;
            this.hydrating = true;
            this.render();

            const url = conversationId
                ? `/api/conversations/${encodeURIComponent(conversationId)}`
                : `/api/conversations/last?connection=${encodeURIComponent(sourceKey)}`;
            const stale = () => generation !== this._generation;
            try {
                const [mod, response] = await Promise.all([
                    this._hydrationModule(),
                    fetch(url, { signal: abort.signal }),
                ]);
                if (stale()) return;
                if (response.status === 401) return;
                if (response.status === 404 && conversationId) {
                    if (typeof window.showToast === 'function') window.showToast('That conversation no longer exists', 'error');
                    this.hydrating = false;
                    this.render();
                    return;
                }
                if (!response.ok) throw new Error(`restore failed (${response.status})`);
                const detail = await response.json();
                if (stale()) return;
                this.hydrating = false;
                if (!detail || !detail.conversation) {
                    this.render();
                    return;
                }
                this.conversation = detail.conversation;
                if (detail.conversation.connection_available === false) this.readOnly = true;
                this.turns = mod.turnsFromDetail(detail);
                // Read-only conversations (connection gone) must never receive new
                // turns, so the session id stays cleared.
                if (typeof window._jeenSetSessionId === 'function') {
                    window._jeenSetSessionId(this.readOnly ? null : detail.conversation.id);
                }
                const newest = this.turns[this.turns.length - 1];
                if (newest) {
                    this.selectedTurnId = newest.id;
                    this.selectedResultId = newest.status === 'success' ? newest.id : null;
                    if (!this.selectedResultId) {
                        const lastOk = [...this.turns].reverse().find((item) => item.status === 'success');
                        this.selectedResultId = lastOk ? lastOk.id : null;
                    }
                }
                this.render();
                this._scrollThread();
                const target = this.turns.find((item) => item.id === this.selectedResultId);
                if (target && target.artifactState === 'missing' && target.snapshotStatus === 'stored') {
                    await this._loadArtifact(target);
                }
            } catch (error) {
                if (stale() || abort.signal.aborted) return;
                console.warn('[Workspace] conversation restore failed', error);
                this.hydrating = false;
                this.render();
            } finally {
                if (this._hydrateAbort === abort) this._hydrateAbort = null;
            }
        },

        /** Fetch the rows (+ chart baseline) for one restored turn. */
        async _loadArtifact(turn) {
            if (!turn || !turn.restored || !turn.conversationId || !turn.turnId) return;
            if (turn.artifactState === 'loading' || turn.artifactState === 'loaded') return;
            const generation = this._generation;
            turn.artifactState = 'loading';
            this.renderWorkspace();
            try {
                const mod = await this._hydrationModule();
                const response = await fetch(
                    `/api/conversations/${encodeURIComponent(turn.conversationId)}/turns/${encodeURIComponent(turn.turnId)}/artifact`
                );
                if (generation !== this._generation) return;
                if (!response.ok) throw new Error(`artifact ${response.status}`);
                const artifact = await response.json();
                if (generation !== this._generation) return;
                mod.applyArtifact(turn, artifact);
                if (!turn.result.results) {
                    // Snapshot vanished between listing and fetch (pruned); offer Load data.
                    turn.artifactState = 'missing';
                }
            } catch (error) {
                if (generation !== this._generation) return;
                console.warn('[Workspace] artifact load failed', error);
                turn.artifactState = 'failed';
            }
            // Per-selection guard: the rows are kept on the turn either way, but
            // the result pane is only re-rendered if this turn is still selected.
            if (this.selectedResultId === turn.id) {
                this.lastAppliedResultId = null;
                this.render();
            } else {
                this.renderConversation();
            }
        },

        /** "Load data" / "Refresh": re-execute the stored query for a restored turn. */
        async rerunTurn(turnId) {
            const turn = this.turns.find((item) => item.id === turnId);
            if (!turn || !turn.restored || turn.rerunning) return;
            if (this.readOnly) {
                if (typeof window.showToast === 'function') window.showToast('This connection is no longer available.', 'error');
                return;
            }
            const generation = this._generation;
            turn.rerunning = true;
            turn.rerunError = null;
            this.render();
            try {
                const mod = await this._hydrationModule();
                const response = await fetch(
                    `/api/conversations/${encodeURIComponent(turn.conversationId)}/turns/${encodeURIComponent(turn.turnId)}/rerun`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
                );
                if (generation !== this._generation) return;
                if (!response.ok) {
                    let detail = '';
                    try { detail = (await response.json()).error || ''; } catch (_) { /* plain text */ }
                    throw new Error(this._rerunMessage(response.status, detail));
                }
                const artifact = await response.json();
                if (generation !== this._generation) return;
                mod.applyArtifact(turn, artifact);
                turn.chartState = null;
                turn.hasChart = false;
                // snapshot_at is only set when the fresh rows were persisted; a
                // too_large result is shown but has no durable snapshot time.
                turn.snapshotAt = artifact.snapshot_at || null;
                if (turn.result?.results) turn.artifactState = 'loaded';
                this.selectedTurnId = turn.id;
                this.selectedResultId = turn.id;
                this.lastAppliedResultId = null;
                if (typeof window.showToast === 'function') window.showToast('Data refreshed', 'success');
            } catch (error) {
                if (generation !== this._generation) return;
                turn.rerunError = error && error.message ? error.message : String(error);
                if (typeof window.showToast === 'function') window.showToast(turn.rerunError, 'error');
            } finally {
                turn.rerunning = false;
                if (generation === this._generation) this.render();
            }
        },

        _rerunMessage(status, detail) {
            const text = String(detail || '');
            if (status === 403 || /grant_required/.test(text)) return 'Reconnect your Power BI account to reload this data.';
            if (status === 409 && /connection_unavailable/.test(text)) return 'This connection is no longer available.';
            if (status === 409 && /turn_has_no_query/.test(text)) return 'This answer has no query to re-run.';
            if (status === 504) return 'The query timed out. Try again or narrow the question.';
            if (status === 502) return text.replace(/^\{.*?"detail":\s*"?/, '').slice(0, 200) || 'The data source returned an error.';
            return `Could not reload the data (${status}).`;
        },

        // ── History tab: browse / open / rename / delete conversations ──────
        _conversationList: null,

        async loadConversationList() {
            const host = document.getElementById('v3-conversations-list');
            if (!host) return;
            host.innerHTML = '<div class="v3-thread-empty-label">Loading conversations…</div>';
            try {
                const response = await fetch('/api/conversations?all=true&limit=100');
                if (!response.ok) throw new Error(`list ${response.status}`);
                const data = await response.json();
                this._conversationList = Array.isArray(data.items) ? data.items : [];
            } catch (error) {
                console.warn('[Workspace] conversation list failed', error);
                host.innerHTML = '<div class="v3-thread-empty-label">Could not load conversations.</div>';
                return;
            }
            this.renderConversationList();
        },

        renderConversationList() {
            const host = document.getElementById('v3-conversations-list');
            if (!host) return;
            const items = this._conversationList || [];
            if (!items.length) {
                host.innerHTML = '<div class="v3-thread-empty-label">No previous conversations yet.</div>';
                return;
            }
            const active = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            const groups = [
                { label: 'This connection', items: items.filter((c) => c.connection_available !== false && c.source_key === active) },
                { label: 'Other connections', items: items.filter((c) => c.connection_available !== false && c.source_key !== active) },
                { label: 'Unavailable connections', items: items.filter((c) => c.connection_available === false), readOnly: true },
            ].filter((group) => group.items.length);
            host.innerHTML = groups.map((group) => `
              <div class="v3-conv-group">
                <div class="v3-thread-empty-label">${esc(group.label)}</div>
                ${group.items.map((c) => this._conversationItemHtml(c, group.readOnly)).join('')}
              </div>`).join('');
            host.querySelectorAll('[data-open-conversation]').forEach((node) => node.addEventListener('click', (event) => {
                if (event.target.closest('[data-conv-action]')) return;
                const item = items.find((c) => c.id === node.dataset.openConversation);
                if (item) this.openConversation(item);
            }));
            host.querySelectorAll('[data-conv-action]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const item = items.find((c) => c.id === button.dataset.convId);
                if (!item) return;
                if (button.dataset.convAction === 'rename') this.renameConversation(item);
                if (button.dataset.convAction === 'delete') this.deleteConversation(item);
            }));
        },

        _conversationItemHtml(c, readOnly) {
            const current = this.conversation && this.conversation.id === c.id;
            const when = c.last_activity_at ? this._formatWhen(c.last_activity_at) : '';
            return `<div class="v3-conv-item${current ? ' is-current' : ''}" data-open-conversation="${esc(c.id)}" role="button" tabindex="0">
              <div class="v3-conv-title" dir="${directionOf(c.title)}" title="${esc(c.title)}">${esc(c.title)}</div>
              <div class="v3-conv-meta">
                <span>${esc(c.source_label || c.source_key)}${readOnly ? ' · read-only' : ''}</span>
                <span>${c.turn_count} ${c.turn_count === 1 ? 'question' : 'questions'}${when ? ` · ${esc(when)}` : ''}</span>
              </div>
              ${c.last_question && c.last_question !== c.title ? `<div class="v3-conv-last" dir="${directionOf(c.last_question)}">${esc(c.last_question)}</div>` : ''}
              <div class="v3-conv-actions">
                <button class="v3-text-btn" data-conv-action="rename" data-conv-id="${esc(c.id)}">Rename</button>
                <button class="v3-text-btn" data-conv-action="delete" data-conv-id="${esc(c.id)}">Delete</button>
              </div>
            </div>`;
        },

        openConversation(item) {
            if (!item) return;
            const active = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            this.setTab('conversation');
            if (item.connection_available === false) {
                // Connection is gone: open read-only without switching connections.
                this.hydrate(item.source_key, { conversationId: item.id, readOnly: true });
                return;
            }
            if (item.source_key && item.source_key !== active && typeof window.onConnectionChange === 'function') {
                // Switching connections triggers hydrate() via 'jeen:connection-resolved';
                // remember which conversation to open instead of the newest one.
                this._pendingOpen = { sourceKey: item.source_key, conversationId: item.id };
                window.onConnectionChange(item.source_key);
                return;
            }
            this.hydrate(item.source_key || active, { conversationId: item.id });
        },

        async renameConversation(item) {
            const title = window.prompt('Rename conversation', item.title || '');
            if (title === null) return;
            const clean = title.trim();
            if (!clean) return;
            try {
                const response = await fetch(`/api/conversations/${encodeURIComponent(item.id)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: clean.slice(0, 200) }),
                });
                if (!response.ok) throw new Error(`rename ${response.status}`);
                item.title = clean.slice(0, 200);
                if (this.conversation && this.conversation.id === item.id) {
                    this.conversation.title = item.title;
                    this.renderConversation();
                }
                this.renderConversationList();
            } catch (error) {
                console.warn('[Workspace] rename failed', error);
                if (typeof window.showToast === 'function') window.showToast('Rename failed', 'error');
            }
        },

        async deleteConversation(item) {
            if (!window.confirm(`Delete "${item.title}"? This removes its questions and results.`)) return;
            try {
                const response = await fetch(`/api/conversations/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
                if (!response.ok && response.status !== 404) throw new Error(`delete ${response.status}`);
                this._conversationList = (this._conversationList || []).filter((c) => c.id !== item.id);
                if (this.conversation && this.conversation.id === item.id) this.reset();
                this.renderConversationList();
                if (typeof window.showToast === 'function') window.showToast('Conversation deleted', 'success');
            } catch (error) {
                console.warn('[Workspace] delete failed', error);
                if (typeof window.showToast === 'function') window.showToast('Delete failed', 'error');
            }
        },
        activate() { this.setConversation(true); },
        deactivate() {},
        refreshStarters() { this._renderEmptySuggestions(); },

        _setComposerBusy(busy) {
            if (this.askButton) {
                this.askButton.disabled = busy;
                this.askButton.setAttribute('aria-busy', String(busy));
            }
        },

        _renderEmptySuggestions() {
            if (this.turns.length) return;
            this.renderConversation();
        },

        render() {
            this.renderConversation();
            this.renderWorkspace();
        },

        renderConversation() {
            const thread = document.getElementById('v3-thread');
            if (!thread) return;
            if (!this.turns.length && this.hydrating) {
                thread.innerHTML = `<div class="v3-thread-empty v3-thread-restoring" aria-busy="true">
                  <div class="v3-thread-empty-label">Restoring your last conversation…</div>
                </div>`;
                return;
            }
            if (!this.turns.length) {
                const connectionName = document.getElementById('connection-pill-name')?.textContent?.trim() || 'this dataset';
                const suggestions = typeof window.getStarterSuggestions === 'function' ? window.getStarterSuggestions(4) : [];
                thread.innerHTML = `<div class="v3-thread-empty">
                  <h2>What would you like to know about your data?</h2>
                  <p>Ask in plain language. Answers come back with a chart, the rows behind it, and the SQL that produced them.</p>
                  <div class="v3-thread-empty-label">Suggested for ${esc(connectionName)}</div>
                  <div class="v3-suggestions">${suggestions.map((item) => `<button class="v3-chip" data-suggestion="${esc(item.text)}">${esc(item.text)}</button>`).join('')}</div>
                </div>`;
                thread.querySelectorAll('[data-suggestion]').forEach((button) => button.addEventListener('click', () => this.send(button.dataset.suggestion)));
                return;
            }

            const title = this.conversation ? this.conversation.title : '';
            const readOnlyNote = this.readOnly
                ? `<div class="v3-readonly-note">Read-only: the connection "${esc(this.conversation?.source_label || this.conversation?.source_key || '')}" is no longer available.</div>`
                : '';
            const head = `<div class="v3-thread-head">
                <span class="v3-thread-title" title="${esc(title)}">${esc(title)}</span>
                <button class="v3-text-btn" data-new-conversation>New conversation</button>
              </div>${readOnlyNote}`;
            thread.innerHTML = head + this.turns.map((turn) => this._turnHtml(turn)).join('');
            thread.querySelector('[data-new-conversation]')?.addEventListener('click', (event) => {
                event.stopPropagation();
                this.newConversation();
            });
            thread.querySelectorAll('[data-load-data]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.rerunTurn(button.dataset.loadData);
            }));
            thread.querySelectorAll('[data-turn]').forEach((card) => {
                card.addEventListener('click', () => this.selectTurn(card.dataset.turn));
                // Cards are focusable so keyboard users can bring an answer back too.
                card.addEventListener('keydown', (event) => {
                    if (event.target !== card) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        this.selectTurn(card.dataset.turn);
                    }
                });
            });
            thread.querySelectorAll('[data-trace-toggle]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.traceToggle);
                if (turn) {
                    turn.traceOpen = !turn.traceOpen;
                    this.renderConversation();
                }
            }));
            thread.querySelectorAll('[data-followup]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.send(button.dataset.followup);
            }));
            thread.querySelectorAll('[data-retry]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.retry);
                if (turn) this.send(turn.question);
            }));
            thread.querySelectorAll('[data-report-gap]').forEach((button) => button.addEventListener('click', async (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.reportGap);
                if (!turn) return;
                try {
                    await fetch('/api/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query_id: turn.result?.query_id, feedback: 'catalog_gap', comment: turn.error }) });
                    button.textContent = 'Reported ✓';
                } catch (_) {
                    button.textContent = 'Retry report';
                }
            }));
        },

        _turnHtml(turn) {
            const selected = turn.id === this.selectedTurnId;
            const initials = this._initials();
            if (turn.status === 'running') {
                return `<article class="v3-turn is-running${selected ? ' is-selected' : ''}" data-turn="${turn.id}">
                  <div class="v3-question-row"><span class="v3-mini-avatar">${esc(initials)}</span><div class="v3-question" dir="${directionOf(turn.question)}">${esc(turn.question)}</div></div>
                  <div class="v3-running-list">${PHASES.map((phase) => {
                    const status = turn.phaseState[phase.id];
                    const label = status === 'done' ? 'ok' : status === 'running' ? 'running…' : status === 'error' ? 'failed' : '';
                    return `<div class="v3-running-row is-${status}"><span class="v3-dot is-${status === 'done' ? 'ok' : status}"></span><span>${esc(phase.label)}</span><span class="v3-running-status">${label}</span></div>`;
                }).join('')}</div>
                </article>`;
            }
            if (turn.status === 'error') {
                const failed = [...turn.trace].reverse().find((item) => item.status === 'node_failed');
                const isNewest = this.turns[this.turns.length - 1]?.id === turn.id;
                const meta = turn.restored
                    ? `${esc(turn.executionStatus || 'error')} · restored from history`
                    : `query_failed · failed at ${esc(failed?.node || 'query')} · ${formatMs(turn.durationMs)}`;
                return `<article class="v3-turn${selected || (!turn.restored && isNewest) ? ' is-selected' : ''}" data-turn="${turn.id}" tabindex="0" aria-label="Show answer: ${esc(turn.question)}">
                  <div class="v3-question-row"><span class="v3-mini-avatar">${esc(initials)}</span><div class="v3-question" dir="${directionOf(turn.question)}">${esc(turn.question)}</div></div>
                  <div class="v3-error-block">${esc(turn.error)}
                    <div class="v3-error-meta">${meta}</div>
                  </div>
                  ${isNewest ? '<div class="v3-summary" style="margin-top:12px;color:var(--muted)">The newest question failed. Your last successful result remains in the workspace.</div>' : ''}
                  <div class="v3-error-actions">
                    <button data-retry="${turn.id}">Retry</button>
                    ${turn.restored ? '' : '<button title="SQL editing requires a validated execution contract">Edit SQL</button>'}
                    <button data-report-gap="${turn.id}">Report catalog gap</button>
                  </div>
                </article>`;
            }

            const result = turn.result || {};
            const finished = turn.trace.filter((item) => item.status === 'node_finished');
            const summary = textOf(result.answer);
            const findings = result.findings || [];
            const followups = result.followups || [];
            const insightsDirection = directionOf(findings.map(textOf).join(' '));
            const dots = PHASES.map((phase) => {
                const events = finished.filter((item) => (NODE_PHASE[item.node] || 'execution') === phase.id);
                const elapsed = events.reduce((total, item) => total + Number(item.elapsed_ms || 0), 0);
                const ran = events.length > 0;
                return `<span class="v3-dot${ran ? ' is-done' : ''}" title="${esc(phase.label)}${ran ? ` · ${formatMs(elapsed)}` : ' · not run'}"></span>`;
            }).join('');
            const trace = turn.trace.filter((item) => item.status !== 'node_started');
            const strip = turn.restored ? this._restoredStripHtml(turn) : `<div class="v3-run-strip">${dots}<span class="v3-run-meta">${formatMs(turn.durationMs)} · ${trace.length} nodes</span>
                <button class="v3-text-btn" data-trace-toggle="${turn.id}">${turn.traceOpen ? 'hide run' : 'run details'}</button>
              </div>`;
            return `<article class="v3-turn${selected ? ' is-selected' : ''}${turn.restored ? ' is-restored' : ''}" data-turn="${turn.id}" tabindex="0" aria-label="Show answer: ${esc(turn.question)}" aria-current="${selected ? 'true' : 'false'}">
              <div class="v3-question-row"><span class="v3-mini-avatar">${esc(initials)}</span><div class="v3-question" dir="${directionOf(turn.question)}">${esc(turn.question)}</div></div>
              ${strip}
              ${!turn.restored && turn.traceOpen ? `<div class="v3-trace">${trace.map((item) => `<div class="v3-trace-row">
                <span class="v3-dot ${item.status === 'node_failed' ? '' : 'is-ok'}"></span>
                <span>${esc(item.node)}</span><span class="v3-trace-note">${esc(safeTraceNote(item))}</span>
                <span class="v3-trace-ms">${formatMs(item.elapsed_ms)}</span></div>`).join('')}</div>` : ''}
              <div class="v3-answer">
                ${summary ? `<div class="v3-summary" dir="${directionOf(summary)}">${esc(summary)}</div>` : ''}
                ${findings.length ? `<section class="v3-insights" aria-label="Key insights" dir="${insightsDirection}">
                  <div class="v3-insights-title"><span class="v3-insights-mark" aria-hidden="true">✦</span>Key insights</div>
                  <div class="v3-insights-list">${findings.map((finding, index) => `<div class="v3-finding">
                    <span class="v3-insight-index" aria-hidden="true">${index + 1}</span>
                    <span dir="${directionOf(finding)}">${esc(textOf(finding))}</span>
                  </div>`).join('')}</div>
                </section>` : ''}
                ${followups.length ? `<div class="v3-followups">${followups.map((question) => `<button class="v3-chip" dir="${directionOf(question)}" data-followup="${esc(textOf(question))}">${esc(textOf(question))}</button>`).join('')}</div>` : ''}
              </div>
            </article>`;
        },

        _restoredStripHtml(turn) {
            const when = turn.snapshotAt ? this._formatWhen(turn.snapshotAt) : null;
            if (turn.resultKind === 'text') {
                return '<div class="v3-run-strip"><span class="v3-run-meta">restored · text answer</span></div>';
            }
            if (turn.rerunning) {
                return '<div class="v3-run-strip"><span class="v3-run-meta">Reloading data…</span></div>';
            }
            if (turn.result?.results) {
                const label = when
                    ? `Snapshot from ${esc(when)}`
                    : turn.snapshotStatus === 'too_large' ? 'Fresh result · too large to keep' : 'Restored snapshot';
                return `<div class="v3-run-strip">
                  <span class="v3-run-meta">${label}${turn.hasChart ? ' · chart' : ''}</span>
                  ${turn.canLoadData || turn.result?.sql ? `<button class="v3-text-btn" data-load-data="${turn.id}">Refresh</button>` : ''}
                </div>`;
            }
            if (turn.artifactState === 'loading') {
                return '<div class="v3-run-strip"><span class="v3-run-meta">Loading rows…</span></div>';
            }
            const reason = turn.snapshotStatus === 'too_large'
                ? 'result too large to keep; re-run to view'
                : turn.artifactState === 'failed'
                    ? 'rows could not be loaded'
                    : 'rows not kept; re-run to view';
            return `<div class="v3-run-strip">
              <span class="v3-run-meta">${esc(reason)}</span>
              ${turn.canLoadData || turn.result?.sql ? `<button class="v3-text-btn" data-load-data="${turn.id}">Load data</button>` : ''}
              ${turn.rerunError ? `<span class="v3-run-meta v3-run-error">${esc(turn.rerunError)}</span>` : ''}
            </div>`;
        },

        _formatWhen(iso) {
            const date = new Date(iso);
            if (Number.isNaN(date.getTime())) return String(iso);
            return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
        },

        _placeholderDefault: null,

        /** Result pane for a restored table turn whose rows are not available. */
        _renderPendingResult(turn) {
            const placeholder = document.getElementById('v3-placeholder');
            if (this._placeholderDefault === null) this._placeholderDefault = placeholder.innerHTML;
            const loading = turn.artifactState === 'loading' || turn.rerunning;
            let title = 'Loading rows…';
            let copy = 'Fetching the stored result for this answer.';
            let action = '';
            if (!loading) {
                if (turn.snapshotStatus === 'too_large') {
                    title = 'Result too large to keep';
                    copy = 'This answer returned more rows than the app stores. Re-run the query to view them.';
                } else if (turn.artifactState === 'failed') {
                    title = 'Rows could not be loaded';
                    copy = turn.rerunError || 'Try again, or re-run the query to fetch fresh data.';
                } else {
                    title = 'Rows not kept for this answer';
                    copy = 'Older results are trimmed to save space. Re-run the query to view them.';
                }
                if (turn.canLoadData || turn.result?.sql) {
                    action = `<button class="v3-load-data" data-load-data="${turn.id}">${turn.rerunning ? 'Loading…' : 'Load data'}</button>`;
                }
            }
            document.getElementById('v3-result-title').textContent = turn.question;
            document.getElementById('v3-meta-row').innerHTML = `<span class="v3-status is-empty">${loading ? 'Loading' : 'No rows loaded'}</span>`;
            placeholder.innerHTML = `<strong>${esc(title)}</strong><span>${esc(copy)}</span>${action}`;
            placeholder.querySelector('[data-load-data]')?.addEventListener('click', () => this.rerunTurn(turn.id));
            placeholder.hidden = false;
            document.getElementById('v3-chart-block').hidden = true;
            document.getElementById('v3-table-block').hidden = true;
            document.getElementById('v3-dock-meta').textContent = turn.result?.sql ? 'stored query' : 'no query text';
            this._setActionsEnabled(false);
            this.renderDock();
        },

        _restorePlaceholder() {
            const placeholder = document.getElementById('v3-placeholder');
            if (placeholder && this._placeholderDefault !== null && placeholder.innerHTML !== this._placeholderDefault) {
                placeholder.innerHTML = this._placeholderDefault;
            }
        },

        /** Result pane for a text-only answer (live or restored): no chart, no grid. */
        _renderTextResult(turn) {
            const placeholder = document.getElementById('v3-placeholder');
            if (this._placeholderDefault === null) this._placeholderDefault = placeholder.innerHTML;
            const data = turn.result || {};
            const answer = textOf(data.answer);
            document.getElementById('v3-result-title').textContent = turn.question;
            document.getElementById('v3-meta-row').innerHTML = `
              <span class="v3-status">Answered</span>
              <span class="v3-result-meta">text answer${turn.restored ? ' · restored' : ''}</span>`;
            placeholder.innerHTML = `<strong>Answer</strong><span dir="${directionOf(answer)}">${esc(answer || 'No data was needed for this answer.')}</span>`;
            placeholder.hidden = false;
            document.getElementById('v3-chart-block').hidden = true;
            document.getElementById('v3-table-block').hidden = true;
            const chart = document.getElementById('chart-view-container');
            if (chart) chart.style.display = 'none';
            if (this.lastAppliedResultId !== turn.id && window.JeenLegacyBridge) {
                // Keep the legacy panels (prompt/dev details, session id) in sync,
                // with the answer flattened to text for the legacy renderer.
                this.lastAppliedResultId = turn.id;
                const payload = { ...data, answer, results: null };
                if (turn.restored && typeof window.JeenLegacyBridge.applyRestoredResult === 'function') {
                    window.JeenLegacyBridge.applyRestoredResult(payload, null);
                } else {
                    window.JeenLegacyBridge.applyResult(payload);
                }
            }
            document.getElementById('v3-dock-meta').textContent = 'no query text';
            this._setActionsEnabled(false);
            this.renderDock();
        },

        renderWorkspace() {
            const turn = this.turns.find((item) => item.id === this.selectedResultId && item.status === 'success');
            const placeholder = document.getElementById('v3-placeholder');
            const chartBlock = document.getElementById('v3-chart-block');
            const tableBlock = document.getElementById('v3-table-block');
            if (!turn) {
                this._restorePlaceholder();
                document.getElementById('v3-result-title').textContent = this.hydrating
                    ? 'Restoring your last conversation…'
                    : 'Ask a question to get started';
                document.getElementById('v3-meta-row').innerHTML = '<span class="v3-status is-empty">No result yet</span>';
                placeholder.hidden = false;
                chartBlock.hidden = true;
                tableBlock.hidden = true;
                document.getElementById('v3-dock-meta').textContent = 'no run yet';
                this._setActionsEnabled(false);
                this.renderDock();
                return;
            }

            const data = turn.result;
            // A restored table turn whose rows are not here yet (loading, pruned,
            // too large, failed): show the state instead of an empty table.
            if (turn.restored && turn.resultKind === 'table' && !data.results) {
                this._renderPendingResult(turn);
                return;
            }
            // Text-only answers (greeting, memory answer, clarification) have no
            // dataset: never expose the chart/table blocks, which would otherwise
            // keep showing the previous turn's chart.
            const isText = turn.resultKind === 'text' || (!(data.results && data.results.columns) && !data.sql);
            if (isText) {
                this._renderTextResult(turn);
                return;
            }
            this._restorePlaceholder();
            const results = data.results || {};
            const rows = normalizeRows(results);
            const metrics = data.metrics || {};
            const cap = cappedMeta(results);
            const newest = this.turns[this.turns.length - 1];
            const stale = newest && newest.status === 'error' && newest.id !== turn.id;
            document.getElementById('v3-result-title').textContent = turn.question;
            const restoredNote = turn.restored && turn.snapshotAt
                ? `<span class="v3-result-meta">Snapshot from ${esc(this._formatWhen(turn.snapshotAt))}</span>`
                : '';
            document.getElementById('v3-meta-row').innerHTML = `
              <span class="v3-status">${cap.capped ? 'Completed · capped' : 'Completed'}</span>
              <span class="v3-result-meta">${rows.length} rows · ${formatMs(metrics.execution_time_ms)} exec · ${formatMs(metrics.llm_latency_ms)} llm</span>
              ${restoredNote}
              ${stale ? '<span class="v3-stale-note">Last successful answer — the newest question failed</span>' : ''}`;
            placeholder.hidden = true;
            // An empty result set has nothing to chart; keep the (empty) grid only.
            chartBlock.hidden = rows.length === 0;
            tableBlock.hidden = false;

            if (this.lastAppliedResultId !== turn.id && window.JeenLegacyBridge) {
                this.lastAppliedResultId = turn.id;
                const showChart = () => {
                    const chart = document.getElementById('chart-view-container');
                    if (chart) chart.style.display = rows.length ? 'block' : 'none';
                    window.dispatchEvent(new Event('resize'));
                };
                if ((turn.restored || turn.chartState) && typeof window.JeenLegacyBridge.applyRestoredResult === 'function') {
                    // Known rows (+ optional stored chart): render through the
                    // restore path and wait for the chart machinery instead of
                    // guessing with a timer. No LLM chart call when a baseline exists.
                    const applied = turn.id;
                    window.JeenLegacyBridge.applyRestoredResult(data, turn.chartState).then(() => {
                        if (this.selectedResultId === applied) showChart();
                    });
                } else {
                    window.JeenLegacyBridge.applyResult(data);
                    requestAnimationFrame(showChart);
                }
            }
            this._setActionsEnabled(true);
            document.getElementById('v3-chart-caption').textContent = `${results.columns?.slice(0, 2).join(' by ') || 'result'} · ${rows.length} points`;
            const banner = document.getElementById('v3-cap-banner');
            banner.hidden = !cap.capped;
            if (cap.capped) {
                const totalCopy = cap.total ? ` ${formatCompact(cap.total)} rows matched the query.` : ' The total matched row count is unavailable.';
                banner.textContent = `Result capped at ${formatCompact(cap.cap)} loaded rows.${totalCopy} Exports contain the loaded result only.`;
            }
            const inputTokens = metrics.input_tokens == null ? '—' : formatCompact(metrics.input_tokens);
            const outputTokens = metrics.output_tokens == null ? '—' : formatCompact(metrics.output_tokens);
            const validated = turn.trace.some((event) => ['sqlglot_validate', 'dax_static_validate'].includes(event.node) && event.status === 'node_finished');
            document.getElementById('v3-dock-meta').textContent =
                `${inputTokens} / ${outputTokens} tok · ${metrics.retry_count == null ? '—' : metrics.retry_count} retries · ${validated ? 'validated query' : data.sql ? 'generated query' : 'no query text'}`;
            this._renderChartCollapse();
            this.renderTable();
            this.renderDock();
        },

        renderTable() {
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            if (!turn || !turn.result?.results) return;
            const results = turn.result.results;
            const columns = results.columns || [];
            const allRows = normalizeRows(results);
            const mapActive = document.documentElement.dataset.jeenOsmMapActive === 'true';
            let filtered = filterResultRows(results, this.filter);
            const presentation = window.JeenLegacyBridge?.getTablePresentation?.() || {
                formats: {}, derived: [], sortColumn: null, sortDirection: 'asc',
            };
            if (presentation.sortColumn !== null && presentation.sortColumn !== undefined) {
                const index = presentation.sortColumn;
                const column = columns[index];
                const direction = presentation.sortDirection === 'desc' ? -1 : 1;
                filtered = filtered.slice().sort((left, right) => {
                    const a = rowValue(left, column, index);
                    const b = rowValue(right, column, index);
                    if (a == null) return 1;
                    if (b == null) return -1;
                    const an = Number(a);
                    const bn = Number(b);
                    if (Number.isFinite(an) && Number.isFinite(bn)) return (an - bn) * direction;
                    return String(a).localeCompare(String(b), undefined, { sensitivity: 'base' }) * direction;
                });
            }
            window.JeenLegacyBridge?.setVisibleRows?.(filtered);
            const cap = cappedMeta(results);
            document.getElementById('v3-row-caption').textContent = this.filter
                ? `${filtered.length} of ${allRows.length} loaded`
                : cap.total ? `${allRows.length} loaded · ${formatCompact(cap.total)} matched` : `${allRows.length} rows loaded`;
            const grid = document.getElementById('v3-grid');
            const wrap = document.getElementById('v3-grid-wrap');
            const numeric = new Set(columns.map((column, index) => inferColumnType(filtered.slice(0, 50).map((row) => rowValue(row, column, index))) === 'number' ? index : -1).filter((index) => index >= 0));
            const descriptors = [];
            columns.forEach((name, index) => {
                descriptors.push({ name, sourceIndex: index, numeric: numeric.has(index) });
                const derived = (presentation.derived || []).find((item) => item.sourceIndex === index);
                if (derived) descriptors.push({ name: derived.name, sourceIndex: index, numeric: true, derived });
            });
            const derivedValues = new Map();
            (presentation.derived || []).forEach((derived) => {
                const source = columns[derived.sourceIndex];
                const values = filtered.map((row) => Number(rowValue(row, source, derived.sourceIndex)));
                const sum = values.reduce((total, value) => total + (Number.isFinite(value) ? value : 0), 0);
                let running = 0;
                derivedValues.set(derived.sourceIndex, values.map((value, rowIndex) => {
                    if (!Number.isFinite(value)) return null;
                    if (derived.type === 'pct_total') return sum ? (value / sum) * 100 : 0;
                    if (derived.type === 'running_total') return (running += value);
                    if (derived.type === 'delta') return rowIndex ? value - values[rowIndex - 1] : null;
                    return value;
                }));
            });
            const columnTemplate = descriptors.length
                ? `minmax(180px, 1.4fr)${descriptors.slice(1).map(() => ' minmax(120px, 1fr)').join('')}`
                : 'minmax(180px, 1fr)';
            grid.style.setProperty('--v3-columns', columnTemplate);
            const head = `<div class="v3-grid-row v3-grid-head">${descriptors.map((descriptor) => {
                const sort = !descriptor.derived && presentation.sortColumn === descriptor.sourceIndex
                    ? (presentation.sortDirection === 'desc' ? ' ↓' : ' ↑') : '';
                return `<button class="v3-grid-cell${descriptor.numeric ? ' is-numeric' : ''}${descriptor.derived ? ' is-derived' : ''}" ${descriptor.derived ? '' : `data-col="${descriptor.sourceIndex}"`} title="${esc(descriptor.name)}">${esc(descriptor.name)}${sort}</button>`;
            }).join('')}</div>`;
            const rowHtml = (row, visibleIndex) => {
                const sourceIndex = allRows.indexOf(row);
                const selected = mapActive && this.mapSelectedRows.has(sourceIndex);
                return `<div class="v3-grid-row${selected ? ' is-map-selected' : ''}" data-row="${visibleIndex}" data-source-row="${sourceIndex}"${mapActive ? ' title="Focus map at this location"' : ''}>${descriptors.map((descriptor) => {
                const raw = descriptor.derived
                    ? derivedValues.get(descriptor.sourceIndex)?.[visibleIndex]
                    : rowValue(row, columns[descriptor.sourceIndex], descriptor.sourceIndex);
                const rendered = descriptor.derived
                    ? (descriptor.derived.type === 'pct_total' && raw != null ? `${formatCompact(raw)}%` : formatCompact(raw))
                    : (window.JeenLegacyBridge?.formatTableValue?.(raw, descriptor.sourceIndex, descriptor.numeric) ?? (raw ?? '—'));
                    return `<div class="v3-grid-cell${descriptor.numeric ? ' is-numeric' : ''}${descriptor.derived ? ' is-derived' : ''}" title="${esc(rendered)}">${esc(rendered)}</div>`;
                }).join('')}</div>`;
            };
            const bindGridActions = () => {
                grid.querySelectorAll('[data-col]').forEach((header) => {
                    const index = Number(header.dataset.col);
                    header.addEventListener('click', () => {
                        const next = presentation.sortColumn === index && presentation.sortDirection === 'asc' ? 'desc' : 'asc';
                        window.sortTableDir?.(index, next);
                    });
                    header.addEventListener('contextmenu', (event) => window.showColMenu?.(event, index));
                });
                grid.querySelectorAll('[data-row]').forEach((row) => {
                    row.addEventListener('contextmenu', (event) => window.showRowMenu?.(event, Number(row.dataset.row)));
                    row.addEventListener('click', (event) => {
                        if (event.defaultPrevented || !mapActive) return;
                        const sourceRow = Number(row.dataset.sourceRow);
                        if (Number.isInteger(sourceRow) && sourceRow >= 0) {
                            this.mapSelectedRows = new Set([sourceRow]);
                            this.renderTable();
                            document.dispatchEvent(new CustomEvent('jeen:osm-table-focus', {
                                detail: { rowIndexes: [sourceRow] },
                            }));
                        }
                    });
                });
            };
            wrap.onscroll = null;
            if (filtered.length <= 500) {
                grid.innerHTML = head + filtered.map((row, index) => rowHtml(row, index)).join('');
                bindGridActions();
                return;
            }

            const rowHeight = 45;
            const overscan = 10;
            const renderWindow = () => {
                const visible = Math.ceil(wrap.clientHeight / rowHeight);
                const start = Math.max(0, Math.floor(Math.max(0, wrap.scrollTop - 43) / rowHeight) - overscan);
                const end = Math.min(filtered.length, start + visible + overscan * 2);
                grid.innerHTML = head
                    + `<div class="v3-virtual-spacer" style="height:${start * rowHeight}px"></div>`
                    + filtered.slice(start, end).map((row, index) => rowHtml(row, start + index)).join('')
                    + `<div class="v3-virtual-spacer" style="height:${(filtered.length - end) * rowHeight}px"></div>`;
                bindGridActions();
            };
            wrap.onscroll = renderWindow;
            renderWindow();
        },

        _renderChartCollapse() {
            document.getElementById('v3-chart-frame').hidden = this.chartCollapsed;
            document.getElementById('v3-chart-edit').hidden = this.chartCollapsed;
            document.getElementById('v3-chart-toggle').textContent = this.chartCollapsed ? 'Expand' : 'Collapse';
        },

        toggleDock(tab) {
            if (this.dockOpen && this.dockTab === tab) this.dockOpen = false;
            else {
                this.dockTab = tab;
                this.dockOpen = true;
            }
            this.renderDock();
            setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
        },

        renderDock() {
            // Onboarding signal: fire once on the rising edge of opening the SQL
            // dock for an answered turn (covers both the button and keyboard paths).
            const sqlOpen = this.dockOpen && this.dockTab === 'sql';
            if (sqlOpen && !this._sqlDockWasOpen
                && this.turns.some((item) => item.id === this.selectedResultId && item.status === 'success')) {
                document.dispatchEvent(new CustomEvent('jeen:onboarding:open_sql'));
            }
            this._sqlDockWasOpen = sqlOpen;
            document.querySelectorAll('[data-dock]').forEach((button) => {
                button.classList.toggle('is-active', this.dockOpen && button.dataset.dock === this.dockTab);
                button.setAttribute('aria-selected', String(this.dockOpen && button.dataset.dock === this.dockTab));
                button.tabIndex = button.dataset.dock === this.dockTab ? 0 : -1;
            });
            const body = document.getElementById('v3-dock-body');
            const toggle = document.getElementById('v3-dock-toggle');
            body.hidden = !this.dockOpen;
            toggle.textContent = this.dockOpen ? 'Hide' : 'Show';
            toggle.setAttribute('aria-expanded', String(this.dockOpen));
            if (!this.dockOpen) return;
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            if (!turn) {
                body.innerHTML = '<div class="v3-dock-empty">No run yet — SQL, timings and column profiling appear here after a question is answered.</div>';
                return;
            }
            body.innerHTML = this.dockTab === 'profiling' ? this._profileHtml(turn) : this._sqlHtml(turn);
            body.querySelector('[data-copy-sql]')?.addEventListener('click', () => navigator.clipboard.writeText(turn.result.sql || ''));
            body.querySelector('[data-full-profile]')?.addEventListener('click', () => this._openFullProfile());
            body.querySelector('[data-dev-details]')?.addEventListener('click', () => document.getElementById('dev-panel-btn')?.click());
        },

        _sqlHtml(turn) {
            const data = turn.result;
            const metrics = data.metrics || {};
            const rows = normalizeRows(data.results).length;
            const nodes = new Set(turn.trace.filter((event) => event.status === 'node_finished').map((event) => event.node));
            const validation = nodes.has('sqlglot_validate')
                ? 'sqlglot validated'
                : nodes.has('dax_static_validate') ? 'DAX validated' : 'validation unavailable';
            const stats = [
                ['Rows', rows],
                ['Exec', formatMs(metrics.execution_time_ms)],
                ['LLM', formatMs(metrics.llm_latency_ms)],
                ['Tokens', formatCompact(metrics.total_tokens)],
                ['Retries', metrics.retry_count == null ? '—' : metrics.retry_count],
            ];
            return `<div class="v3-stats">${stats.map(([label, value]) => `<div class="v3-stat"><div class="v3-stat-label">${label}</div><div class="v3-stat-value">${esc(value)}</div></div>`).join('')}</div>
              <div class="v3-sql-card">
                <div class="v3-sql-provenance">generated · ${validation} · read-only <button data-dev-details>Developer details</button><button data-copy-sql>Copy</button></div>
                <pre>${esc(data.sql || '-- No SQL or DAX was generated for this answer.')}</pre>
              </div>`;
        },

        _profileHtml(turn) {
            const profiles = compactProfile(turn.result.results);
            if (!profiles.length) return '<div class="v3-dock-empty">No result columns are available to profile.</div>';
            return `<div class="v3-profile-list">${profiles.map((profile) => `<div class="v3-profile-row">
              <div class="v3-profile-name"><strong title="${esc(profile.name)}">${esc(profile.name)}</strong><span>${esc(profile.type)}</span></div>
              <div class="v3-profile-range"><div class="v3-profile-track"><div class="v3-profile-fill" style="width:${profile.fillPct}%"></div></div><span>${esc(profile.range)}</span></div>
              <div class="v3-profile-figure">${profile.distinct} distinct</div><div class="v3-profile-figure">nulls ${profile.nullPct}%</div>
            </div>`).join('')}</div><div class="v3-profile-foot">Need distributions and correlations? <button class="v3-text-btn" data-full-profile>Open full profile report</button></div>`;
        },

        _openFullProfile() {
            const section = document.getElementById('profiling-section');
            if (!section) return;
            document.getElementById('v3-profile-overlay')?.remove();
            const overlay = document.createElement('div');
            overlay.id = 'v3-profile-overlay';
            overlay.className = 'v3-profile-overlay';
            overlay.innerHTML = '<div class="v3-profile-modal"><button class="v3-profile-close" aria-label="Close profile">×</button><div class="v3-profile-modal-body"></div></div>';
            document.body.appendChild(overlay);
            const opener = document.activeElement;
            overlay.querySelector('.v3-profile-modal-body').appendChild(section);
            section.style.display = 'block';
            const close = () => {
                section.style.display = 'none';
                document.body.appendChild(section);
                overlay.remove();
                if (opener && typeof opener.focus === 'function') opener.focus();
            };
            overlay.querySelector('.v3-profile-close').addEventListener('click', close);
            overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
            document.getElementById('profiling-header')?.click();
        },

        _setActionsEnabled(enabled) {
            ['export-btn', 'copy-results-btn', 'send-result-btn', 'describe-btn'].forEach((id) => {
                const button = document.getElementById(id);
                if (!button) return;
                button.disabled = !enabled;
                button.setAttribute('aria-disabled', String(!enabled));
                button.style.display = '';
                if (id === 'send-result-btn' && enabled) {
                    const me = window._currentUser || {};
                    const canSend = Boolean(me.connectors_enabled && me.is_entra && window._resultHandle);
                    button.disabled = !canSend;
                    button.setAttribute('aria-disabled', String(!canSend));
                    button.title = canSend ? 'Send result' : 'Connect Microsoft Entra and a delivery connector to send this result';
                }
            });
        },

        _initials() {
            const name = String(window._currentUser?.name || window._currentUser?.email || 'You').trim();
            const parts = name.split(/\s+/);
            return (parts.length > 1 ? `${parts[0][0]}${parts[parts.length - 1][0]}` : name.slice(0, 2)).toUpperCase();
        },

        _scrollThread() {
            requestAnimationFrame(() => {
                const thread = document.getElementById('v3-thread');
                if (thread) thread.scrollTop = thread.scrollHeight;
            });
        },
    };

    window.WorkspaceV3Utils = {
        PHASES,
        NODE_PHASE,
        compactProfile,
        cappedMeta,
        textOf,
        directionOf,
        safeTraceNote,
        filterResultRows,
        selectionForTurn,
    };
    window.WorkspaceController = WorkspaceController;
    if (typeof document !== 'undefined' && document.body) {
        WorkspaceController.init();
    } else {
        window.addEventListener('DOMContentLoaded', () => WorkspaceController.init());
    }
})();
