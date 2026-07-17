// Chart manager will be dynamically imported when needed
let ChartManager = null;

// Global state
let currentQuestion = '';
let currentSql = '';
let currentPrompt = '';
let currentResults = null;
let currentQueryId = null;  // For conversation history tracking
let currentSessionId = null;  // For conversation continuity
let _isRestoringSavedAnalysis = false;
let _restoredSavedQuestion = '';
let allTables = [];
let promptExpanded = false;
let sqlExpanded = false;
let sortColumn = null;
let sortDirection = 'asc';
let filterText = '';
let chartManager = null;
let insightsManager = null;

// ── Developer Panel state ───────────────────────────────────────────────────
let _allTraceEvents   = [];   // full event list from most recent query
let _traceMetrics     = {};   // metrics from most recent query
let _activeTraceFilter = 'all'; // current log level filter
let _traceSearchQ     = '';   // current log text search query
let _postQueryTrace   = {};   // async work that starts after /api/ask returns
let _traceViewMode = (function () {
    try {
        return localStorage.getItem('jeen_trace_view_mode') || 'flow';
    } catch (_) { return 'flow'; }
})();
// Whether the log legend is expanded. Open by default on first view; the
// user's choice is then remembered across sessions.
let _traceLegendOpen = (function () {
    try {
        const v = localStorage.getItem('jeen_log_legend_open');
        return v === null ? true : v === '1';
    } catch (_) { return true; }
})();

// ── Table display window ──────────────────────────────────────────────────────
// Number of rows rendered at once. User can change it via the footer input.
// Kept across queries so the user's preference sticks for the session.
let _displayLimit = 25;

// ── Connection (Jeen Insights) ──────────────────────────────
const CONNECTION_STORAGE_KEY = 'jeen_insights_connection';
const SIDEBAR_TAB_KEY        = 'jeen_sidebar_tab'; // 'tables' | 'recent'
let availableConnections = [];
let activeTable = null;
let lastQueryDurationMs = 0;
let lastTotalDurationMs = 0;   // end-to-end: click Ask → results table painted
let _lastAskStart       = 0;   // performance.now() captured when Ask was clicked
let _lastResultData     = null;// last response payload, for the post-paint re-render

// ── Autocomplete v3 state (used by SuggestionController) ────
let recentQuestionsCache = [];      // string[]
let pinnedQuestionsCache = [];      // string[]
let knowledgeQuestionsCache = null; // { sourceKey, questions: [{question, category, tags}] } | null
let _kqLoading = false;
let _kqLoadedFor = null;
let lastInsertedTable = null;
const _llmSuggestCache = new Map(); // key: sourceKey + '|' + partial.toLowerCase() => { ts, suggestions, corrections }
let _llmAbort = null;
let _llmRequestId = 0;
let _llmDebounceTimer = null;
// `#` trigger — columns. Cache keyed by sourceKey + '|' + table_or_ALL.
const _columnsCache = new Map();
const _columnsLoading = new Set(); // keys currently in flight

const TABLE_ICON_SVG = '<svg class="table-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="9" y1="4" x2="9" y2="20"/></svg>';

// Set of table names that are currently expanded in the sidebar
const _tableExpandedSet = new Set();

// Rich table data from the metadata catalog: [{name, description, col_count}]
// Stays in sync with allTables (name-only string[]) which is kept for autocomplete.
let allTablesRich = [];

// ── Column presentation state (resets per query) ──────────────
let _colFormats  = {};  // colIndex → { type, icon, label }
let _derivedCols = [];  // [{ sourceIndex, type, name }]
let _colSums     = {};  // colIndex → numeric sum (for % of total)

// Rows currently visible in the results table (respects sort + filter).
// Updated by renderTable(); used by the row context menu.
let _currentVisibleRows = [];

function getActiveConnection() {
    return localStorage.getItem(CONNECTION_STORAGE_KEY) || '';
}

function setActiveConnection(sourceKey) {
    if (sourceKey) {
        localStorage.setItem(CONNECTION_STORAGE_KEY, sourceKey);
    } else {
        localStorage.removeItem(CONNECTION_STORAGE_KEY);
    }
}

function requireConnection() {
    const c = getActiveConnection();
    if (!c) {
        showError('Please pick a connection from the sidebar.');
        return null;
    }
    return c;
}

function setConnectionStatus(status) {
    const dot = document.getElementById('connection-status-dot');
    if (dot) dot.setAttribute('data-status', status); // ok | connecting | error
}

function setConnectionPillName(name) {
    const el = document.getElementById('connection-pill-name');
    if (el) el.textContent = name;
}

async function _updateMcpBadge(sourceKey) {
    const badge = document.getElementById('connection-mcp-badge');
    if (!badge || !sourceKey) return;
    try {
        const r = await fetch(`/api/mcp/status?connection=${encodeURIComponent(sourceKey)}`);
        if (!r.ok) return;
        const d = await r.json();
        badge.hidden = (d.catalog_source !== 'mcp');
    } catch { badge.hidden = true; }
}

async function loadConnections() {
    setConnectionStatus('connecting');
    setConnectionPillName('Loading\u2026');
    try {
        const response = await fetch('/api/connections');
        const data = await response.json();
        availableConnections = (data && data.connections) || [];
        if (availableConnections.length === 0) {
            setActiveConnection('');
            setConnectionStatus('error');
            setConnectionPillName('No connections');
            return;
        }
        const stored = getActiveConnection();
        const validStored = availableConnections.find(c => c.source_key === stored);
        const active = validStored ? validStored.source_key : availableConnections[0].source_key;
        setActiveConnection(active);
    const activeRow = availableConnections.find(c => c.source_key === active);
        setConnectionPillName(activeRow ? activeRow.display_name : active);
        _updateMcpBadge(active);
        // Pill stays in 'connecting' until tables come back — set in loadTables.
    } catch (e) {
        console.error('Failed to load connections', e);
        setConnectionStatus('error');
        setConnectionPillName('Failed to load');
    }
}

// Switch the active connection. Accepts an explicit source_key argument so
// the new ConnectionPanel can call it directly; falls back to localStorage.
function onConnectionChange(sourceKey) {
    const newConnection = sourceKey || getActiveConnection();
    if (!newConnection) return;
    if (newConnection === getActiveConnection() && allTables.length > 0) {
        // Same connection, already populated — no-op.
        return;
    }
    setActiveConnection(newConnection);
    // Reset session and clear caches that are connection-specific.
    currentSessionId = null;
    // Chat thread is tied to the (now-reset) session — clear it and cancel any
    // in-flight chat/chart work so turns from different connections never mix.
    if (window.ChatController && typeof window.ChatController.reset === 'function') {
        window.ChatController.reset();
    }
    allTables = [];
    activeTable = null;
    // Back to the empty/hero landing for the freshly selected connection.
    setUiState('empty');
    if (typeof hideResults === 'function') hideResults();
    if (typeof hideAskMetrics === 'function') hideAskMetrics();
    recentQuestionsCache = [];
    pinnedQuestionsCache = [];
    const tablesList = document.getElementById('tables-list');
    if (tablesList) tablesList.innerHTML = '';
    const searchInput = document.getElementById('table-search');
    if (searchInput) { searchInput.style.display = 'none'; searchInput.value = ''; }
    const activeRow = availableConnections.find(c => c.source_key === newConnection);
    setConnectionPillName(activeRow ? activeRow.display_name : newConnection);
    _updateMcpBadge(newConnection);
    setConnectionStatus('connecting');
        // Reset autocomplete caches, rich table data, and table expand state.
        _tableExpandedSet.clear();
        allTablesRich = [];
        if (typeof SuggestionController !== 'undefined') SuggestionController.reset();
    // Reflect the reset immediately (skeleton chips) while data reloads.
    refreshHeroEmptyState();
    // Auto-load tables for the new connection.
    loadTables();
    if (typeof displayHistory === 'function') displayHistory();
    // Fire-and-forget: pre-warm the metadata cache on the API server so the
    // first query after a connection switch doesn't pay the fetch penalty.
    fetch(`/api/connections/${encodeURIComponent(newConnection)}/warm-cache`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
    }).catch(() => {}); // non-critical
}

function setPageTitle(text) {
    const el = document.getElementById('page-title');
    if (el) el.textContent = text || 'New query';
}

// ----------------------------------------------------------------
// Empty / hero landing state
// ----------------------------------------------------------------
//
// Before the first question is asked we show a centered hero with the
// active connection and a set of starter suggestions sourced from the
// user's own history (pinned + recent) topped up with table-derived
// prompts. The first query flips the layout to the docked "results"
// view; switching connection / session brings the hero back.

const MAX_HERO_SUGGESTIONS = 6;

function setUiState(state) {
    const main = document.getElementById('main-content');
    if (main) main.setAttribute('data-ui-state', state === 'results' ? 'results' : 'empty');
}

// ── Interaction mode (Ask vs Chat) ──────────────────────────────────────────
// Ask mode = the classic single-result screen (unchanged). Chat mode = a
// conversation thread that reuses the same NL->SQL pipeline per turn. The
// header, sidebar and connection state stay mounted across both modes; only
// the main panel swaps.
const APP_MODE_KEY = 'jeen_app_mode'; // 'ask' | 'chat'
let appMode = 'ask';

function setAppMode(mode) {
    const next = (mode === 'chat') ? 'chat' : 'ask';
    appMode = next;
    try { localStorage.setItem(APP_MODE_KEY, next); } catch (_) {}

    const askBtn  = document.getElementById('mode-btn-ask');
    const chatBtn = document.getElementById('mode-btn-chat');
    if (askBtn)  { askBtn.classList.toggle('active', next === 'ask');   askBtn.setAttribute('aria-selected', next === 'ask' ? 'true' : 'false'); }
    if (chatBtn) { chatBtn.classList.toggle('active', next === 'chat'); chatBtn.setAttribute('aria-selected', next === 'chat' ? 'true' : 'false'); }

    const main      = document.getElementById('main-content');
    const askInner  = document.querySelector('#main-content .main-inner');
    const chatPanel = document.getElementById('chat-mode-panel');

    if (next === 'chat') {
        if (main) main.classList.add('mode-chat');
        if (askInner) askInner.style.display = 'none';
        if (chatPanel) chatPanel.hidden = false;
        if (window.ChatController && typeof window.ChatController.activate === 'function') {
            window.ChatController.activate();
        }
    } else {
        if (main) main.classList.remove('mode-chat');
        if (askInner) askInner.style.display = '';
        if (chatPanel) chatPanel.hidden = true;
        if (window.ChatController && typeof window.ChatController.deactivate === 'function') {
            window.ChatController.deactivate();
        }
    }
}
window.setAppMode = setAppMode;
window.getAppMode = () => appMode;

// Session-id accessors shared with the Chat controller (a separate script
// file) so both modes thread the same conversation via currentSessionId.
window._jeenGetSessionId = () => currentSessionId;
window._jeenSetSessionId = (v) => { currentSessionId = v || null; };

// Recent/pinned sidebar question click: fill the Ask box in Ask mode, or send
// it as a new turn when in Chat mode.
window._jeenQuestionClick = function (q) {
    if (appMode === 'chat' && window.ChatController && typeof window.ChatController.send === 'function') {
        window.ChatController.send(q);
    } else {
        fillQuestion(q);
    }
};

// Reusable helpers the Chat controller borrows (hoisted function decls).
window.escapeHtml       = escapeHtml;
window.formatNumeric    = formatNumeric;
window.deriveResultTitle = deriveResultTitle;

// Mirror the sidebar connection pill into the hero line.
function updateHeroConnection() {
    const box = document.getElementById('hero-connection');
    const txt = document.getElementById('hero-connection-text');
    if (!box || !txt) return;
    const nameEl = document.getElementById('connection-pill-name');
    const name = nameEl ? nameEl.textContent.trim() : '';
    if (!name || /loading/i.test(name) || /no connections|failed/i.test(name)) {
        box.hidden = true;
        return;
    }
    const countEl = document.getElementById('table-count-badge');
    const count = countEl ? countEl.textContent.trim() : '';
    const tablesPart = count ? ` \u00b7 ${count} tables` : '';
    txt.innerHTML = `Connected to <strong>${escapeHtml(name)}</strong>${tablesPart}`;
    box.hidden = false;
}

// Derive starter prompts from the table catalog (no hardcoded questions):
// favor fact/measure tables, then the richer (more-columns) tables.
function _heroTableStarters() {
    const tables = Array.isArray(allTablesRich) ? allTablesRich.slice() : [];
    if (tables.length === 0) return [];
    const score = (t) => {
        const n = (t.name || '').toLowerCase();
        let s = t.col_count || 0;
        if (/fact|sales|order|transaction|revenue|event|invoice/.test(n)) s += 1000;
        return s;
    };
    tables.sort((a, b) => score(b) - score(a));
    const templates = [
        (name) => `Summarize ${name}`,
        (name) => `Show the top 10 rows from ${name}`,
        (name) => `How many records are in ${name}?`,
    ];
    const out = [];
    for (let i = 0; i < tables.length && out.length < 3; i++) {
        const name = tables[i].name;
        if (!name) continue;
        out.push(templates[out.length % templates.length](name));
    }
    return out;
}

// Merge starter prompts: pinned -> recent -> table-derived, deduped. Shared by
// the Ask-mode hero chips and the Chat-mode empty state.
function getStarterSuggestions(max) {
    const limit = (typeof max === 'number' && max > 0) ? max : MAX_HERO_SUGGESTIONS;
    const pinned   = (pinnedQuestionsCache || []).map(q => ({ text: q, kind: 'pinned' }));
    const recent   = (recentQuestionsCache || []).map(q => ({ text: q, kind: 'recent' }));
    const starters = _heroTableStarters().map(q => ({ text: q, kind: 'table' }));

    const seen = new Set();
    const merged = [];
    for (const item of [...pinned, ...recent, ...starters]) {
        const text = (item.text || '').trim();
        const key = text.toLowerCase();
        if (!text || seen.has(key)) continue;
        seen.add(key);
        merged.push({ text, kind: item.kind });
        if (merged.length >= limit) break;
    }
    return merged;
}
window.getStarterSuggestions = getStarterSuggestions;

// Render the starter chips: pinned -> recent -> table-derived, deduped.
function renderHeroSuggestions() {
    const wrap = document.getElementById('hero-suggestions');
    const chips = document.getElementById('hero-suggestions-chips');
    if (!wrap || !chips) return;

    const merged = getStarterSuggestions(MAX_HERO_SUGGESTIONS);

    if (merged.length === 0) {
        // Only show skeletons while a connection's data is genuinely loading.
        // With no active connection (or once tables have loaded empty), hide.
        const hasConnection = !!(typeof getActiveConnection === 'function' && getActiveConnection());
        const tablesReady   = Array.isArray(allTablesRich) && allTablesRich.length > 0;
        if (!hasConnection || tablesReady) {
            wrap.hidden = true;
            chips.innerHTML = '';
        } else {
            wrap.hidden = false;
            chips.innerHTML = Array.from({ length: 4 })
                .map(() => '<span class="hero-chip is-skeleton" aria-hidden="true"></span>')
                .join('');
        }
        return;
    }

    const icon = { pinned: '\uD83D\uDCCC', recent: '\uD83D\uDD52', table: '\u2728' };
    wrap.hidden = false;
    chips.innerHTML = merged.map(item => {
        const safe = escapeHtml(item.text);
        const js   = escapeHtml(item.text).replace(/'/g, "\\'");
        return `<button type="button" class="hero-chip" role="listitem" title="${safe}"`
            + ` onclick="fillQuestion('${js}')">`
            + `<span class="hero-chip-icon" aria-hidden="true">${icon[item.kind] || ''}</span>${safe}</button>`;
    }).join('');

    // Keep the Chat-mode empty-state starter chips in sync as data loads.
    if (window.ChatController && typeof window.ChatController.refreshStarters === 'function') {
        window.ChatController.refreshStarters();
    }
}

// Recompute the hero connection line + suggestions from current caches.
function refreshHeroEmptyState() {
    updateHeroConnection();
    renderHeroSuggestions();
}
window.refreshHeroEmptyState = refreshHeroEmptyState;

window.addEventListener('DOMContentLoaded', () => {
    refreshHeroEmptyState();
    loadConnections().then(() => {
        if (typeof displayHistory === 'function') displayHistory();
        // Auto-load tables once we know the active connection.
        if (getActiveConnection()) loadTables();
        refreshHeroEmptyState();
    });
});

// Ask question
async function askQuestion() {
    const questionInput = document.getElementById('question-input');
    const question = questionInput.value.trim();

    if (!question) {
        showError('Please enter a question');
        return;
    }

    // Immediately dismiss any open autocomplete dropdown so it doesn't block
    // the query submission or remain visible while loading.
    if (typeof SuggestionController !== 'undefined') SuggestionController.close();

    // Save to history
    saveToHistory(question);

    // Show loading state + dock the layout immediately (leave the hero).
    hideError();
    hideResults();
    hideAskMetrics();
    setUiState('results');
    showLoading();

    const connection = requireConnection();
    if (!connection) {
        hideLoading();
        return;
    }

    // Read user preferences (settings panel). Server enforces bounds; if any
    // value is missing or invalid the server falls back to its defaults.
    const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
    const askPayload = {
        question,
        connection,
        session_id: currentSessionId,  // Maintain conversation continuity
    };
    if (prefs.rowLimit) askPayload.limit = prefs.rowLimit;
    if (prefs.temperature !== null && prefs.temperature !== undefined) {
        askPayload.temperature = prefs.temperature;
    }
    // Always skip in-graph eval so the table appears as soon as SQL returns.
    // When aiAnalytics=on, the InsightsManager runs the analysis separately
    // in the background and updates the Insights panel when done.
    askPayload.eval_analytics = false;
    // Per-request LLM timeout override from the settings panel.
    const _llmTimeoutSecs = window.JeenPreferences ? window.JeenPreferences.getLlmTimeoutSeconds() : null;
    if (_llmTimeoutSecs !== null) askPayload.llm_timeout = _llmTimeoutSecs;

    const askStart = performance.now();
    _lastAskStart = askStart;
    lastTotalDurationMs = 0;   // reset; set once the table actually paints
    try {
        const response = await fetch('/api/ask', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(askPayload),
        });
        lastQueryDurationMs = performance.now() - askStart;

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || 'Failed to process question');
        }

        hideLoading();
        displayResults(data);

        // Measure the true end-to-end wait: stop the timer on the animation
        // frame after displayResults paints the table, then refresh the run
        // header so the `total` chip shows the final value.
        requestAnimationFrame(() => {
            lastTotalDurationMs = performance.now() - askStart;
            if (_lastResultData) _updateDevRunHeader(_lastResultData);
        });

    } catch (error) {
        hideLoading();
        showError(`Error: ${error.message}`);
        console.error('Error:', error);
    }
}

// Display results
function displayResults(data) {
    _lastResultData = data;  // kept so the post-paint frame can rebuild the header
    _resetPostQueryTrace();
    // Reset column presentation state for every new result set
    _colFormats  = {};
    _derivedCols = [];
    _colSums     = {};

    // Reset toggle states
    sqlExpanded = false;
    promptExpanded = false;

    // Store current question and IDs for history tracking
    currentQuestion = data.question;
    currentQueryId = data.query_id || null;
    currentSessionId = data.session_id || null;
    // Opaque handle authorizing outbound actions on THIS result (server snapshot).
    window._resultHandle = data.result_handle || null;
    // Expose to feature modules (chart/profiling) so they can reference the
    // server-side cached result by query_id and pass the question as intent.
    window.currentQueryId = currentQueryId;
    window.currentQuestion = currentQuestion;

    // Log conversation IDs
    if (currentQueryId) {
        console.log('[History] Query ID:', currentQueryId);
    }
    if (currentSessionId) {
        console.log('[History] Session ID:', currentSessionId);
    }

    // Show results section + dock the layout (leave the empty/hero state).
    const resultsSection = document.getElementById('results-section');
    resultsSection.style.display = 'flex';
    setUiState('results');

    // Render token-usage + LLM-latency under the Ask card.
    showAskMetrics(data.metrics);

    // Derive a human-readable result title from the question + bind page title
    const derivedTitle = deriveResultTitle(data.question);
    setResultTitle(derivedTitle);
    setPageTitle(derivedTitle);

    // Display SQL via CodeMirror (or fallback)
    if (data.sql) {
        currentSql = data.sql;
        initCodeMirror(data.sql);
    } else {
        currentSql = '';
        initCodeMirror('-- No SQL generated');
    }

    // Display results
    const resultsDisplay = document.getElementById('results-display');
    const exportBtn = document.getElementById('export-btn');
    const copyResultsBtn = document.getElementById('copy-results-btn');
    const saveAnalysisBtn = document.getElementById('save-analysis-btn');
    const rerunFreshBtn = document.getElementById('rerun-fresh-btn');
    const sendResultBtn = document.getElementById('send-result-btn');

    const describeBtn = document.getElementById('describe-btn');

    // The Send action needs the connector feature ON, an SSO (Entra) identity,
    // and a server-issued result handle. Otherwise it stays hidden.
    const _canSend = () => {
        const me = window._currentUser || {};
        return !!(me.connectors_enabled && me.is_entra && window._resultHandle);
    };

    if (data.error) {
        resultsDisplay.innerHTML = `<div class="error-message">${data.error}</div>`;
        exportBtn.style.display = 'none';
        copyResultsBtn.style.display = 'none';
        if (saveAnalysisBtn) saveAnalysisBtn.style.display = 'none';
        if (rerunFreshBtn) rerunFreshBtn.style.display = 'none';
        if (sendResultBtn) sendResultBtn.style.display = 'none';
        describeBtn.style.display = 'none';
        currentResults = null;
        window.currentResults = null;
        if (typeof profilingManager !== 'undefined') {
            profilingManager.hide();
        }
    } else if (data.results && data.results.columns && (data.results.data || data.results.rows)) {
        currentResults = data.results;
        resultsDisplay.innerHTML = formatResultsAsTable(data.results);
        showResultsToolbar(true);
        exportBtn.style.display = 'inline-block';
        copyResultsBtn.style.display = 'inline-block';
        if (saveAnalysisBtn) saveAnalysisBtn.style.display = 'inline-block';
        if (rerunFreshBtn) rerunFreshBtn.style.display = _isRestoringSavedAnalysis ? 'inline-block' : 'none';
        if (sendResultBtn) sendResultBtn.style.display = _canSend() ? 'inline-block' : 'none';
        describeBtn.style.display = 'inline-block';
        // Result meta line: "<n> rows · 0.3s"
        const rows = data.results.data || data.results.rows || [];
        setResultMeta(rows.length, lastQueryDurationMs);

        // Store results globally for profiling manager
        window.currentResults = data.results;

        // Initialize chart feature. Saved restores initialise explicitly so
        // they can await the manager before rendering the saved config.
        if (!_isRestoringSavedAnalysis) initializeChartFeature(data.results);

        // Generate insights in background — gated by the AI Analytics preference.
        // The table is already visible; InsightsManager streams the analysis
        // and updates the Insights panel when each chunk arrives.
        const _aiAnalytics = (window.JeenPreferences && window.JeenPreferences.getAll().aiAnalytics) || 'on';
        if (_aiAnalytics === 'on' && !_isRestoringSavedAnalysis) {
            generateInsights(data.results, currentQuestion, currentQueryId, currentSql);
        } else {
            // Hide the insights container when analytics is off.
            const ic = document.getElementById('insights-container');
            if (ic) ic.style.display = 'none';
        }

        // Initialize profiling section (collapsed by default)
        if (typeof profilingManager !== 'undefined') {
            profilingManager.initialize(data.results);
        }
    } else if (data.answer) {
        // LLM returned a conversational text response (no SQL executed)
        resultsDisplay.innerHTML = `<div class="llm-text-answer">${escapeHtml(data.answer).replace(/\n/g, '<br>')}</div>`;
        showResultsToolbar(false);
        exportBtn.style.display = 'none';
        copyResultsBtn.style.display = 'none';
        if (saveAnalysisBtn) saveAnalysisBtn.style.display = 'none';
        if (rerunFreshBtn) rerunFreshBtn.style.display = 'none';
        if (sendResultBtn) sendResultBtn.style.display = 'none';
        describeBtn.style.display = 'none';
        currentResults = null;
        window.currentResults = null;
        if (typeof profilingManager !== 'undefined') {
            profilingManager.hide();
        }
        setResultMeta(null, lastQueryDurationMs);
    } else {
        resultsDisplay.innerHTML = '<div class="no-results">No results to display</div>';
        showResultsToolbar(false);
        exportBtn.style.display = 'none';
        copyResultsBtn.style.display = 'none';
        if (saveAnalysisBtn) saveAnalysisBtn.style.display = 'none';
        if (rerunFreshBtn) rerunFreshBtn.style.display = 'none';
        if (sendResultBtn) sendResultBtn.style.display = 'none';
        describeBtn.style.display = 'none';
        currentResults = null;
        window.currentResults = null;
        if (typeof profilingManager !== 'undefined') {
            profilingManager.hide();
        }
        setResultMeta(0, lastQueryDurationMs);
    }

    // Display structured prompt in Query Prompt tab
    if (data.prompt) {
        currentPrompt = data.prompt;
        displayStructuredPrompt(data.prompt);
    }

    // Render execution trace into the Trace tab of the dev drawer
    if (data.trace) {
        renderTrace(data.trace, data.metrics);
    }

    // ── Developer Panel: run header + SQL stats bar ──────────────────────
    _updateDevRunHeader(data);
    _updateSqlStats(data);

    // Display SQL in SQL tab
    // (Already handled above in the SQL display section)

    // Scroll to results
    resultsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Format results as HTML table with sorting and filtering.
// The filter input now lives in the external toolbar; the internal
// .table-controls block was removed as part of the toolbar consolidation.
function formatResultsAsTable(results) {
    // Handle both 'data' and 'rows' field names
    let rows = results.data || results.rows;
    if (!rows || rows.length === 0) {
        return '<div class="no-results">No data found</div>';
    }

    // Reset sort/filter on new results
    sortColumn = null;
    sortDirection = 'asc';
    filterText = '';
    const externalFilter = document.getElementById('result-filter');
    if (externalFilter) externalFilter.value = '';

    // Apply display window — user can change this via the footer input.
    const totalRows   = rows.length;
    const visibleRows = rows.slice(0, _displayLimit);

    let html = '<div id="table-container">';
    html += renderTable(results, visibleRows);
    html += '</div>';
    html += _buildDisplayLimitBar(totalRows, _displayLimit);
    return html;
}

// Render the actual table with dim badges + tabular-nums + context menu + derived cols.
function renderTable(results, rows) {
    const profile = profileColumns(results, rows);

    // Pre-compute column sums for derived ‘% of total’ columns.
    _derivedCols.forEach(d => {
        if (d.type === 'pct_total' && _colSums[d.sourceIndex] === undefined) {
            let sum = 0;
            rows.forEach(row => {
                const v = Number(Array.isArray(row) ? row[d.sourceIndex] : row[results.columns[d.sourceIndex]]);
                if (Number.isFinite(v)) sum += v;
            });
            _colSums[d.sourceIndex] = sum || 1;
        }
    });

    let html = '<table id="results-table"><thead><tr>';
    results.columns.forEach((column, index) => {
        const sortIcon = sortColumn === index ? (sortDirection === 'asc' ? ' \u25b2' : ' \u25bc') : '';
        const isNum = profile.numericCols.has(index);
        const cls = isNum ? ' class="num-cell"' : '';
        const fmt = _colFormats[index];
        const fmtTag = fmt ? `<span class="col-fmt-tag">${escapeHtml(fmt.icon)}</span>` : '';
        html += `<th${cls}
          onclick="sortTable(${index})"
          oncontextmenu="showColMenu(event,${index});return false;"
          title="Click to sort \u00b7 Right-click for options"
        >${escapeHtml(column)}${fmtTag}${sortIcon}</th>`;
        // Derived column header immediately after its source.
        const derived = _derivedCols.find(d => d.sourceIndex === index);
        if (derived) {
            html += `<th class="derived-col-header" oncontextmenu="return false;">${escapeHtml(derived.name)}</th>`;
        }
    });
    html += '</tr></thead><tbody>';

    // Snapshot so the row context menu can look up any row by index.
    _currentVisibleRows = rows;

    const runTotals = {};
    rows.forEach((row, rowIdx) => {
        html += `<tr class="data-row" oncontextmenu="showRowMenu(event,${rowIdx});return false;">`;
        html += `<td style="display:none" data-row-idx="${rowIdx}"></td>`;
        results.columns.forEach((column, idx) => {
            const cell = Array.isArray(row) ? row[idx] : row[column];
            html += renderCellHtml(cell, idx, profile);

            // Derived cell after source column.
            const derived = _derivedCols.find(d => d.sourceIndex === idx);
            if (derived) {
                const numVal = Number(Array.isArray(row) ? row[idx] : row[column]);
                let derivedText, derivedCls = 'derived-col';

                if (derived.type === 'pct_total') {
                    derivedText = Number.isFinite(numVal)
                        ? (numVal / (_colSums[idx] || 1) * 100).toFixed(1) + '%'
                        : '\u2014';
                } else if (derived.type === 'running_total') {
                    if (!runTotals[idx]) runTotals[idx] = 0;
                    if (Number.isFinite(numVal)) runTotals[idx] += numVal;
                    derivedText = Number.isFinite(numVal) ? formatNumeric(runTotals[idx]) : '\u2014';
                } else if (derived.type === 'delta') {
                    if (rowIdx === 0) {
                        derivedText = _EM_DASH;
                    } else {
                        const prev = Number(Array.isArray(rows[rowIdx-1]) ? rows[rowIdx-1][idx] : rows[rowIdx-1][column]);
                        if (Number.isFinite(numVal) && Number.isFinite(prev)) {
                            const d = numVal - prev;
                            derivedText = _fmtSigned(d);
                            derivedCls = d > 0 ? 'derived-col derived-positive' : d < 0 ? 'derived-col derived-negative' : 'derived-col';
                        } else {
                            derivedText = _EM_DASH;
                        }
                    }
                } else {
                    derivedText = '\u2014';
                }
                html += `<td class="${derivedCls}">${escapeHtml(String(derivedText))}</td>`;
            }
        });
        html += '</tr>';
    });

    html += '</tbody></table>';
    return html;
}

// ----------------------------------------------------------------
// Result-rendering helpers
// ----------------------------------------------------------------

// Real minus (U+2212) and em-dash for table cells.
const _MINUS   = '\u2212';
const _EM_DASH = '\u2014';

/**
 * Format a number for a standard data cell.
 * Uses real minus \u2212 so decimal points form a clean vertical rail.
 * Non-finite values become an em-dash.
 */
function formatNumeric(value) {
    const n = (typeof value === 'number') ? value : Number(value);
    if (!Number.isFinite(n)) return (typeof value === 'string' && value.trim() !== '') ? String(value) : _EM_DASH;
    const abs = Math.abs(n);
    // Big numbers drop noise decimals (1,309,863 not 1,309,863.4); thousands are
    // grouped; small values keep precision. Mirrors the chart formatter.
    const maxFrac = abs >= 1000 ? 0 : 4;
    const body = abs.toLocaleString('en-US', { maximumFractionDigits: maxFrac });
    return n < 0 ? _MINUS + body : body;
}

/**
 * Format a signed delta/change value.
 * Always shows + or \u2212 so the sign is visible even for positives.
 */
function _fmtSigned(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return _EM_DASH;
    const sign = n > 0 ? '+' : n < 0 ? _MINUS : '';
    const abs  = Math.abs(n);
    const maxFrac = abs >= 1000 ? 0 : 4;
    return sign + abs.toLocaleString('en-US', { maximumFractionDigits: maxFrac });
}

function renderCellHtml(value, colIndex, profile) {
    // NULL → faint em-dash aligned with the numeric rail.
    if (value === null || value === undefined || value === '') {
        const cls = profile.numericCols.has(colIndex) ? 'num-cell' : '';
        return `<td${cls ? ` class="${cls}"` : ''}><span class="cell-null">${_EM_DASH}</span></td>`;
    }
    // Custom format override takes priority.
    if (_colFormats[colIndex]) {
        const fmt = applyColFormatValue(value, _colFormats[colIndex].type);
        if (fmt !== null) return `<td class="num-cell">${escapeHtml(fmt)}</td>`;
    }
    // ID columns — mono, faint; don't badge or format numerically.
    if (profile.idCols && profile.idCols.has(colIndex)) {
        return `<td class="cell-id">${escapeHtml(String(value))}</td>`;
    }
    // Dimension badge — categorical identity (month, territory, category …).
    if (profile.dimCols.has(colIndex)) {
        return `<td><span class="dim-badge">${escapeHtml(String(value))}</span></td>`;
    }
    if (profile.numericCols.has(colIndex)) {
        // Delta / change columns — the only cells that get green / red.
        if (profile.deltaCols && profile.deltaCols.has(colIndex)) {
            const n  = Number(value);
            const cc = Number.isFinite(n)
                ? (n > 0 ? ' cell-delta-pos' : n < 0 ? ' cell-delta-neg' : '')
                : '';
            return `<td class="num-cell${cc}">${escapeHtml(_fmtSigned(value))}</td>`;
        }
        return `<td class="num-cell">${escapeHtml(formatNumeric(value))}</td>`;
    }
    return `<td>${escapeHtml(String(value))}</td>`;
}

/**
 * Profile every column: numeric vs categorical, plus new column kinds
 * that drive targeted rendering.
 *
 *   numericCols  — ≥70 % of non-null values parse as numbers
 *   dimCols      — categorical: <20 distinct values, not numeric, not id
 *   idCols       — id/key/pk-style: rendered mono+faint, no badge/format
 *   deltaCols    — change/delta columns: rendered signed with direction color
 */
function profileColumns(results, rows) {
    const numericCols = new Set();
    const dimCols     = new Set();
    const idCols      = new Set();   // NEW
    const deltaCols   = new Set();   // NEW

    const numCols    = results.columns.length;
    const sampleSize = Math.min(rows.length, 200);

    // Column-name heuristics
    const ID_RE    = /(^id$|_id$|^key$|_key$|_pk$|^pk$|^uuid$)/i;
    const DELTA_RE = /(yoy|mom|wow|qoq|_change$|change_|delta|variance|_diff$|diff_|growth_rate|pct_change|_chg$|_var$|change_pct|_delta$|_delta_)/i;

    for (let i = 0; i < numCols; i++) {
        const colName   = results.columns[i];
        const lowerName = String(colName).toLowerCase();

        const isIdLike = ID_RE.test(lowerName);
        if (isIdLike) { idCols.add(i); continue; }  // IDs skip all other processing

        let numCount = 0, nonNullCount = 0;
        const distinct = new Set();
        for (let r = 0; r < sampleSize; r++) {
            const row  = rows[r];
            const cell = Array.isArray(row) ? row[i] : row[colName];
            if (cell === null || cell === undefined || cell === '') continue;
            nonNullCount++;
            distinct.add(String(cell));
            const num = Number(cell);
            if (Number.isFinite(num) && /^[-+]?\d/.test(String(cell).trim())) numCount++;
        }

        const isNumeric = nonNullCount > 0 && numCount / nonNullCount >= 0.7;
        if (isNumeric) {
            numericCols.add(i);
            if (DELTA_RE.test(lowerName)) deltaCols.add(i);
        } else if (nonNullCount > 0 && distinct.size > 0 && distinct.size < 20) {
            // Dim: categorical, few distinct values.
            dimCols.add(i);
        }
    }

    return { numericCols, dimCols, idCols, deltaCols };
}

// ----------------------------------------------------------------
// Result title / meta + toolbar visibility
// ----------------------------------------------------------------
function setResultTitle(text) {
    const el = document.getElementById('result-title');
    if (el) el.textContent = text || 'Results';
}
function setResultMeta(rowCount, durationMs) {
    const el = document.getElementById('result-meta');
    if (!el) return;
    const seconds = (durationMs / 1000);
    const durStr = seconds >= 0.1 ? seconds.toFixed(1) + 's' : Math.max(1, Math.round(durationMs)) + 'ms';
    if (rowCount === null || rowCount === undefined) {
        el.textContent = durStr;
    } else {
        el.textContent = `${rowCount} row${rowCount !== 1 ? 's' : ''} \u00b7 ${durStr}`;
    }
}
function showResultsToolbar(visible) {
    const el = document.getElementById('results-toolbar');
    if (el) el.style.display = visible ? 'flex' : 'none';
}

function deriveResultTitle(question) {
    if (!question) return 'Results';
    let q = String(question).trim();
    // Strip leading filler words / question marks.
    q = q.replace(/[?\.!]+$/g, '');
    q = q.replace(/^\s*(please\s+)?(can you\s+|could you\s+)?(show me|show|give me|tell me|list|fetch|get me|get|what is|whats|what's|what are|how many|how much|count|find)\s+/i, '');
    if (!q) return 'Results';
    // Title-case but keep small words lowercase (except first word).
    const small = new Set(['a','an','and','as','at','but','by','for','in','of','on','or','the','to','vs']);
    const words = q.split(/\s+/);
    return words.map((w, i) => {
        const lower = w.toLowerCase();
        if (i > 0 && small.has(lower)) return lower;
        return lower.charAt(0).toUpperCase() + lower.slice(1);
    }).join(' ').slice(0, 80);
}

// Sort table by column
function sortTable(columnIndex) {
    console.log('[Sort] Sorting column:', columnIndex);
    if (!currentResults) {
        console.warn('[Sort] No current results');
        return;
    }

    // Clone the data array to avoid modifying original
    let rows = [...(currentResults.data || currentResults.rows)];
    const column = currentResults.columns[columnIndex];

    // Toggle sort direction if clicking same column
    if (sortColumn === columnIndex) {
        sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortColumn = columnIndex;
        sortDirection = 'asc';
    }

    // Sort rows
    rows.sort((a, b) => {
        let valA, valB;

        if (Array.isArray(a)) {
            valA = a[columnIndex];
            valB = b[columnIndex];
        } else {
            valA = a[column];
            valB = b[column];
        }

        // Handle nulls
        if (valA === null || valA === undefined) return 1;
        if (valB === null || valB === undefined) return -1;

        // Try numeric comparison first
        const numA = parseFloat(String(valA).replace(/[^0-9.-]/g, ''));
        const numB = parseFloat(String(valB).replace(/[^0-9.-]/g, ''));

        if (!isNaN(numA) && !isNaN(numB)) {
            return sortDirection === 'asc' ? numA - numB : numB - numA;
        }

        // String comparison
        const strA = String(valA).toLowerCase();
        const strB = String(valB).toLowerCase();

        if (sortDirection === 'asc') {
            return strA < strB ? -1 : strA > strB ? 1 : 0;
        } else {
            return strA > strB ? -1 : strA < strB ? 1 : 0;
        }
    });

    // Apply current filter if exists
    const filterInput = document.getElementById('result-filter');
    const filterValue = filterInput ? filterInput.value.toLowerCase() : '';

    if (filterValue) {
        rows = rows.filter(row => {
            if (Array.isArray(row)) {
                return row.some(cell =>
                    cell !== null && cell !== undefined &&
                    String(cell).toLowerCase().includes(filterValue)
                );
            } else {
                return currentResults.columns.some(col => {
                    const cell = row[col];
                    return cell !== null && cell !== undefined &&
                        String(cell).toLowerCase().includes(filterValue);
                });
            }
        });
    }

    // Apply display limit and update footer bar
    const totalSorted = rows.length;
    document.getElementById('table-container').innerHTML = renderTable(currentResults, rows.slice(0, _displayLimit));
    _updateDisplayLimitBar(totalSorted, _displayLimit);
}

// Filter results
function filterResults() {
    console.log('[Filter] Filter triggered');
    if (!currentResults) {
        console.warn('[Filter] No current results');
        return;
    }

    filterText = document.getElementById('result-filter').value.toLowerCase();
    // Clone the data array
    let rows = [...(currentResults.data || currentResults.rows)];

    // Apply current sort if exists
    if (sortColumn !== null) {
        const column = currentResults.columns[sortColumn];
        rows.sort((a, b) => {
            let valA, valB;

            if (Array.isArray(a)) {
                valA = a[sortColumn];
                valB = b[sortColumn];
            } else {
                valA = a[column];
                valB = b[column];
            }

            // Handle nulls
            if (valA === null || valA === undefined) return 1;
            if (valB === null || valB === undefined) return -1;

            // Try numeric comparison first
            const numA = parseFloat(String(valA).replace(/[^0-9.-]/g, ''));
            const numB = parseFloat(String(valB).replace(/[^0-9.-]/g, ''));

            if (!isNaN(numA) && !isNaN(numB)) {
                return sortDirection === 'asc' ? numA - numB : numB - numA;
            }

            // String comparison
            const strA = String(valA).toLowerCase();
            const strB = String(valB).toLowerCase();

            if (sortDirection === 'asc') {
                return strA < strB ? -1 : strA > strB ? 1 : 0;
            } else {
                return strA > strB ? -1 : strA < strB ? 1 : 0;
            }
        });
    }

    if (!filterText) {
        // No filter, show all (with current sort) — apply display limit
        document.getElementById('table-container').innerHTML = renderTable(currentResults, rows.slice(0, _displayLimit));
        _updateDisplayLimitBar(rows.length, _displayLimit);
        return;
    }

    // Filter rows
    const filtered = rows.filter(row => {
        if (Array.isArray(row)) {
            return row.some(cell =>
                cell !== null && cell !== undefined &&
                String(cell).toLowerCase().includes(filterText)
            );
        } else {
            return currentResults.columns.some(col => {
                const cell = row[col];
                return cell !== null && cell !== undefined &&
                    String(cell).toLowerCase().includes(filterText);
            });
        }
    });

    // Apply display limit and update footer bar
    document.getElementById('table-container').innerHTML = renderTable(currentResults, filtered.slice(0, _displayLimit));
    _updateDisplayLimitBar(filtered.length, _displayLimit);
}

// Copy SQL to clipboard
function copySql() {
    if (!currentSql) return;

    navigator.clipboard.writeText(currentSql).then(() => {
        const button = document.querySelector('.sql-copy-btn') || document.querySelector('.copy-button');
        if (button) {
            const originalHTML = button.innerHTML;
            button.textContent = '✓ Copied!';
            button.style.color = 'var(--color-success)';
            setTimeout(() => {
                button.innerHTML = originalHTML;
                button.style.color = '';
            }, 2000);
        }
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

// Copy Results to clipboard
function copyResults() {
    if (!currentResults) return;

    const rows = currentResults.data || currentResults.rows;
    if (!rows || rows.length === 0) return;

    // Create tab-separated text (better for pasting into Excel/Sheets)
    let text = '';

    // Add headers
    text += currentResults.columns.join('\t') + '\n';

    // Add data rows
    rows.forEach(row => {
        if (Array.isArray(row)) {
            text += row.join('\t') + '\n';
        } else {
            text += currentResults.columns.map(col => row[col] || '').join('\t') + '\n';
        }
    });

    navigator.clipboard.writeText(text).then(() => {
        const button = document.getElementById('copy-results-btn');
        if (button) {
            const originalText = button.textContent;
            button.textContent = '✓ Copied!';
            button.style.color = 'var(--color-success)';
            setTimeout(() => {
                button.textContent = originalText;
                button.style.color = '';
            }, 2000);
        }
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

// Toggle SQL display
function toggleSql() {
    const sqlContent = document.getElementById('sql-content');
    const toggleBtn = document.getElementById('toggle-sql-btn');

    sqlExpanded = !sqlExpanded;

    if (sqlExpanded) {
        sqlContent.style.display = 'block';
        toggleBtn.textContent = '▲ Hide SQL';
    } else {
        sqlContent.style.display = 'none';
        toggleBtn.textContent = '▼ View SQL';
    }
}

// Toggle prompt display
function togglePrompt() {
    const promptContent = document.getElementById('prompt-content');
    const toggleBtn = document.getElementById('toggle-prompt-btn');

    promptExpanded = !promptExpanded;

    if (promptExpanded) {
        promptContent.style.display = 'block';
        toggleBtn.textContent = '▲ Hide Prompt';
        if (currentPrompt) {
            // Check if it's structured prompt or plain text
            if (typeof currentPrompt === 'object') {
                displayStructuredPrompt(currentPrompt);
            } else {
                const promptDisplay = document.getElementById('prompt-display');
                if (promptDisplay) {
                    promptDisplay.textContent = currentPrompt;
                } else {
                    promptContent.innerHTML = `<pre id="prompt-display" class="prompt-display">${escapeHtml(currentPrompt)}</pre>`;
                }
            }
        } else {
            promptContent.innerHTML = '<p>No prompt information available</p>';
        }
    } else {
        promptContent.style.display = 'none';
        toggleBtn.textContent = '▼ View Prompt';
    }
}

// Load tables from the metadata catalog (description + col count included).
// Drives the connection-status dot: connecting → ok → error.
async function loadTables() {
    const tablesList = document.getElementById('tables-list');
    const searchInput = document.getElementById('table-search');
    tablesList.innerHTML = '<p style="color: var(--color-faint); font-size: var(--text-xs); padding: 4px 0;">Loading\u2026</p>';

    const connection = requireConnection();
    if (!connection) {
        tablesList.innerHTML = '<p style="color: var(--color-faint); font-size: var(--text-xs); padding: 4px 0;">Pick a connection first</p>';
        setConnectionStatus('error');
        return;
    }

    setConnectionStatus('connecting');
    try {
        const response = await fetch('/api/tables-rich?connection=' + encodeURIComponent(connection));
        const data = await response.json();

        if (data.tables && data.tables.length > 0) {
            allTablesRich = data.tables;                          // [{name, description, col_count}]
            allTables     = data.tables.map(t => t.name);        // string[] kept for autocomplete
            searchInput.style.display = 'block';
            displayFilteredTables(allTablesRich);
            const countBadge = document.getElementById('table-count-badge');
            if (countBadge) countBadge.textContent = allTables.length;
        } else {
            allTablesRich = [];
            allTables     = [];
            const msg = data.tables && data.tables.length === 0
                ? 'No tables in catalog \u2014 refresh metadata first'
                : 'No tables found';
            tablesList.innerHTML = `<p style="color: var(--color-faint); font-size: var(--text-xs); padding: 4px 0;">${msg}</p>`;
            const countBadge = document.getElementById('table-count-badge');
            if (countBadge) countBadge.textContent = '';
        }
        setConnectionStatus('ok');
        refreshHeroEmptyState();
    } catch (error) {
        tablesList.innerHTML = '<p style="color: var(--color-error); font-size: var(--text-xs); padding: 4px 0;">Failed to load tables</p>';
        console.error('Error loading tables:', error);
        setConnectionStatus('error');
    }
}

// Filter tables — searches both name AND description from the metadata catalog.
function filterTables() {
    const searchTerm = document.getElementById('table-search').value.toLowerCase();
    if (!searchTerm) {
        displayFilteredTables(allTablesRich);
        return;
    }
    const filtered = allTablesRich.filter(t =>
        t.name.toLowerCase().includes(searchTerm) ||
        (t.description && t.description.toLowerCase().includes(searchTerm))
    );
    displayFilteredTables(filtered);
}

// Highlight a search term inside `text`. Returns HTML-escaped string with <mark> tags.
function _highlightTableSearch(text, term) {
    if (!term || !text) return escapeHtml(text || '');
    const idx = text.toLowerCase().indexOf(term.toLowerCase());
    if (idx === -1) return escapeHtml(text);
    return escapeHtml(text.slice(0, idx))
        + '<mark>' + escapeHtml(text.slice(idx, idx + term.length)) + '</mark>'
        + escapeHtml(text.slice(idx + term.length));
}

// Display filtered tables — accordion layout with description, col count, hover actions.
// `tables` is [{name, description, col_count}] from the metadata catalog.
function displayFilteredTables(tables) {
    const tablesList = document.getElementById('tables-list');
    if (tables.length === 0) {
        tablesList.innerHTML = '<p style="color: var(--color-muted); font-size: var(--text-xs); padding: 4px 0;">No matching tables</p>';
        return;
    }
    const conn = getActiveConnection();
    // Current search term (for highlighting)
    const searchInput = document.getElementById('table-search');
    const term = (searchInput && searchInput.value || '').toLowerCase();

    tablesList.innerHTML = tables.map(tableObj => {
        // Support both object ({name,...}) and plain string (legacy fallback)
        const name        = (tableObj && tableObj.name) ? tableObj.name : String(tableObj);
        const description = tableObj && tableObj.description ? tableObj.description : null;
        const catalogColCount = (tableObj && tableObj.col_count) ? tableObj.col_count : 0;

        const safe   = escapeHtml(name);
        const safeJS = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        const isActive   = activeTable === name ? ' is-active' : '';
        const isExpanded = _tableExpandedSet.has(name);

        // Col count: prefer catalog value, fall back to already-fetched cache
        const cacheKey = conn + '|' + name;
        const cached   = _columnsCache.get(cacheKey);
        const colCount = catalogColCount > 0 ? catalogColCount : (cached ? cached.length : null);
        const colCountBadge = colCount ? `<span class="table-col-count">${colCount}</span>` : '';

        const arrowClass = 'table-expand-arrow' + (isExpanded ? ' open' : '');
        const colsHtml = isExpanded && cached
            ? renderTableColumns(cached)
            : (isExpanded ? '<div class="table-cols-loading">Loading\u2026</div>' : '');

        // Description subtitle with search-term highlighting
        const descHtml = description
            ? `<div class="table-description" title="${escapeHtml(description)}">${_highlightTableSearch(description, term)}</div>`
            : '';

        // Highlighted table name
        const highlightedName = term ? _highlightTableSearch(name, term) : safe;

        return `<div class="table-item${isActive}" data-table="${safe}">
  <div class="table-item-header" onclick="toggleTableExpand('${safeJS}')">
    ${TABLE_ICON_SVG}
    <span class="table-name" title="${safe}">${highlightedName}</span>
    ${colCountBadge}
    <div class="table-item-actions">
      <button class="table-action-btn" onclick="event.stopPropagation();selectTableExplore('${safeJS}')" title="Explore data">→</button>
      <button class="table-action-btn" onclick="event.stopPropagation();selectTableSchema('${safeJS}')" title="Show schema">≡</button>
      <button class="table-action-btn" onclick="event.stopPropagation();copyTableName('${safeJS}')" title="Copy name">⧉</button>
    </div>
    <span class="${arrowClass}">&#9656;</span>
  </div>
  ${descHtml}
  <div class="table-columns-list${isExpanded ? ' open' : ''}" id="tbl-cols-${safe}">${colsHtml}</div>
</div>`;
    }).join('');
}

// Render the column rows inside an expanded table.
function renderTableColumns(cols) {
    if (!cols || cols.length === 0) {
        return '<div class="table-cols-loading">No columns found</div>';
    }
    return cols.map(c => {
        const typeLower = (c.data_type || '').toLowerCase();
        let typeClass;
        if (/int|float|decimal|numeric|double|real|number|bigint|smallint/.test(typeLower)) typeClass = 'numeric';
        else if (/char|text|string|varchar|nvarchar|clob/.test(typeLower)) typeClass = 'text';
        else if (/date|time|timestamp/.test(typeLower)) typeClass = 'date';
        else if (/bool/.test(typeLower)) typeClass = 'bool';
        else typeClass = 'other';
        // Strip length specifiers: varchar(255) → varchar
        const typeShort = (c.data_type || '?').replace(/\(.*\)/, '').toLowerCase();
        const desc = c.description ? ` title="${escapeHtml(c.description)}"` : '';
        const safeCol = escapeHtml(c.column || '');
        const safeTable = (c.table || '').replace(/'/g, "\\'");
        return `<div class="table-col-row"${desc} onclick="fillQuestion('Show ${safeCol} from ${escapeHtml(c.table || '')}')">
  <span class="table-col-name">${safeCol}</span>
  <span class="table-col-type-badge table-col-type-${typeClass}">${escapeHtml(typeShort)}</span>
</div>`;
    }).join('');
}

// Toggle expand/collapse a table row. Fetches columns on first open.
function toggleTableExpand(table) {
    const conn = getActiveConnection();
    const isExpanded = _tableExpandedSet.has(table);

    // Find the item DOM node by dataset
    const allItems = document.querySelectorAll('.table-item');
    let el = null;
    for (const item of allItems) {
        if (item.dataset.table === table) { el = item; break; }
    }

    if (isExpanded) {
        _tableExpandedSet.delete(table);
        if (el) {
            el.querySelector('.table-columns-list').classList.remove('open');
            const arrow = el.querySelector('.table-expand-arrow');
            if (arrow) arrow.classList.remove('open');
        }
        return;
    }

    _tableExpandedSet.add(table);
    if (!el) return;

    const colList = el.querySelector('.table-columns-list');
    const arrow = el.querySelector('.table-expand-arrow');
    if (arrow) arrow.classList.add('open');

    const cacheKey = conn + '|' + table;
    const cached = _columnsCache.get(cacheKey);
    if (cached) {
        colList.innerHTML = renderTableColumns(cached);
        colList.classList.add('open');
        // Update col count badge if not yet shown
        const badge = el.querySelector('.table-col-count');
        if (!badge) {
            const nameEl = el.querySelector('.table-name');
            if (nameEl) {
                const newBadge = document.createElement('span');
                newBadge.className = 'table-col-count';
                newBadge.textContent = cached.length;
                nameEl.after(newBadge);
            }
        }
    } else {
        colList.innerHTML = '<div class="table-cols-loading">Loading…</div>';
        colList.classList.add('open');
        fetchKnowledgeColumns(table).then(cols => {
            if (!cols) {
                colList.innerHTML = '<div class="table-cols-loading">Could not load columns</div>';
                return;
            }
            // Only update if still expanded
            if (!_tableExpandedSet.has(table)) return;
            colList.innerHTML = renderTableColumns(cols);
            // Add/update col count badge
            const freshItems = document.querySelectorAll('.table-item');
            for (const item of freshItems) {
                if (item.dataset.table !== table) continue;
                const badge = item.querySelector('.table-col-count');
                if (badge) {
                    badge.textContent = cols.length;
                } else {
                    const nameEl = item.querySelector('.table-name');
                    if (nameEl) {
                        const newBadge = document.createElement('span');
                        newBadge.className = 'table-col-count';
                        newBadge.textContent = cols.length;
                        nameEl.after(newBadge);
                    }
                }
                break;
            }
        });
    }
}

// Set a table as active (highlights it) without auto-filling a question.
function selectTable(table) {
    activeTable = table;
    const searchInput = document.getElementById('table-search');
    const term = (searchInput && searchInput.value || '').toLowerCase();
    const filtered = term
        ? allTablesRich.filter(t =>
            t.name.toLowerCase().includes(term) ||
            (t.description && t.description.toLowerCase().includes(term)))
        : allTablesRich;
    displayFilteredTables(filtered);
}

// Fill question: "Show me data from {table}"
function selectTableExplore(table) {
    activeTable = table;
    fillQuestion('Show me data from ' + table);
    selectTable(table);
}

// Fill question: "Describe the columns in {table}"
function selectTableSchema(table) {
    fillQuestion('Describe the columns in ' + table);
}

// Copy table name to clipboard and show toast.
function copyTableName(table) {
    navigator.clipboard.writeText(table).then(() => {
        showToast(table + ' copied', 'info');
    }).catch(() => {
        showToast('Could not copy', 'error');
    });
}

// Export to Excel
function exportToExcel() {
    if (!currentResults) return;

    const rows = currentResults.data || currentResults.rows;
    if (!rows || rows.length === 0) return;

    // Create CSV content
    let csv = '';

    // Add headers
    csv += currentResults.columns.join(',') + '\n';

    // Add data rows
    rows.forEach(row => {
        if (Array.isArray(row)) {
            csv += row.map(cell => escapeCSV(cell)).join(',') + '\n';
        } else {
            csv += currentResults.columns.map(col => escapeCSV(row[col])).join(',') + '\n';
        }
    });

    // Create download link
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', 'jeen_insights_results_' + new Date().getTime() + '.csv');
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

// Escape CSV values
function escapeCSV(value) {
    if (value === null || value === undefined) return '';
    const str = String(value);
    if (str.includes(',') || str.includes('"') || str.includes('\n')) {
        return '"' + str.replace(/"/g, '""') + '"';
    }
    return str;
}

// Fill question input
function fillQuestion(question) {
    document.getElementById('question-input').value = question;
    document.getElementById('question-input').focus();
}

// Show/hide UI elements (skeleton-based loading)
function showLoading() {
    const el = document.getElementById('loading');
    if (el) el.style.display = 'flex';
    const btn = document.getElementById('ask-button');
    if (btn) btn.classList.add('btn-loading');
}

function hideLoading() {
    const el = document.getElementById('loading');
    if (el) el.style.display = 'none';
    const btn = document.getElementById('ask-button');
    if (btn) btn.classList.remove('btn-loading');
}

function showError(message) {
    const errorEl = document.getElementById('error-message');
    errorEl.textContent = message;
    errorEl.style.display = 'block';
}

function hideError() {
    document.getElementById('error-message').style.display = 'none';
}

function hideResults() {
    document.getElementById('results-section').style.display = 'none';
}

// ----------------------------------------------------------------
// Ask-card metrics readout (token usage + LLM latency)
// ----------------------------------------------------------------
//
// We deliberately call this `LLM` (latency) and not `TTFT`. Real TTFT
// requires streaming, which the agent doesn't do today — the value here
// is the total time spent inside `llm.generate`. Honest label, accurate
// number.
function _formatTokens(n) {
    if (n === null || n === undefined) return '—';
    if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
    if (n >= 100000) return (n / 1000).toFixed(0) + 'K';
    if (n >= 10000)  return (n / 1000).toFixed(1) + 'K';
    return n.toLocaleString('en-US');
}
function _formatLatency(ms) {
    if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
    if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
    return Math.max(1, Math.round(ms)) + 'ms';
}
function showAskMetrics(metrics) {
    const el = document.getElementById('ask-metrics');
    if (!el) return;
    if (!metrics || typeof metrics !== 'object') {
        el.hidden = true;
        el.textContent = '';
        return;
    }
    const inTok  = _formatTokens(metrics.input_tokens);
    const outTok = _formatTokens(metrics.output_tokens);
    const lat    = _formatLatency(metrics.llm_latency_ms);
    // textContent (not innerHTML) keeps this XSS-safe.
    el.textContent = `in: ${inTok} tok \u00b7 out: ${outTok} tok \u00b7 LLM ${lat}`;
    el.hidden = false;
}
function hideAskMetrics() {
    const el = document.getElementById('ask-metrics');
    if (el) { el.hidden = true; el.textContent = ''; }
}

// Utility: Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Display structured prompt with collapsible sections
function displayStructuredPrompt(promptData) {
    const promptContent = document.getElementById('prompt-content');

    // ── Estimate token count + build copy-all header ─────────────────────
    const rawText = promptData.full_text ||
        [promptData.system_instructions, promptData.tables, promptData.columns,
         promptData.knowledge_pairs, promptData.business_terms,
         promptData.current_question].filter(Boolean).join('\n');
    window._lastPromptRawText = rawText;
    const estTokens = Math.round((rawText || '').length / 4);
    const tokenLabel = estTokens > 0 ? `~${_formatTokens(estTokens)} tok` : '';

    let html = '<div class="dp-prompt-hdr">';
    if (tokenLabel) html += `<span class="dp-token-est">${tokenLabel}</span>`;
    html += `<button class="dp-copy-all" onclick="_copyAllPrompt()" title="Copy full prompt">&#10697; Copy all</button>`;
    html += '</div>';
    html += '<div class="structured-prompt">';

    // Section 1: System Instructions
    if (promptData.system_instructions) {
        html += createPromptSection('system-instructions', 'System Instructions',
            `<pre class="prompt-text">${escapeHtml(promptData.system_instructions)}</pre>`, true);
    }

    // Section 2: Active Connection
    if (promptData.connection) {
        const conn = promptData.connection;
        const connContent = `<pre class="prompt-text">${escapeHtml(`${conn.display_name} (${conn.database_type}) — source_key: ${conn.source_key}`)}</pre>`;
        html += createPromptSection('connection', 'Active Connection', connContent, true);
    }

    // Section 3: Tables
    if (promptData.tables) {
        html += createPromptSection('tables', 'Tables', `<pre class="prompt-text">${escapeHtml(promptData.tables)}</pre>`, false);
    }

    // Section 4: Columns
    if (promptData.columns) {
        html += createPromptSection('columns', 'Columns', `<pre class="prompt-text">${escapeHtml(promptData.columns)}</pre>`, false);
    }

    // Section 5: Relationships
    if (promptData.relationships) {
        html += createPromptSection('relationships', 'Relationships', `<pre class="prompt-text">${escapeHtml(promptData.relationships)}</pre>`, false);
    }

    // Section 6: Sources
    if (promptData.sources) {
        html += createPromptSection('sources', 'Sources', `<pre class="prompt-text">${escapeHtml(promptData.sources)}</pre>`, false);
    }

    // Section 7: Knowledge Pairs
    if (promptData.knowledge_pairs) {
        html += createPromptSection('knowledge-pairs', 'Knowledge Pairs', `<pre class="prompt-text">${escapeHtml(promptData.knowledge_pairs)}</pre>`, false);
    }

    // Section 8: Business Terms
    if (promptData.business_terms) {
        html += createPromptSection('business-terms', 'Business Terms', `<pre class="prompt-text">${escapeHtml(promptData.business_terms)}</pre>`, false);
    }

    // Section 5: Tool Description
    if (promptData.tool_description) {
        const toolContent = `<pre class="prompt-text">${escapeHtml(JSON.stringify(promptData.tool_description, null, 2))}</pre>`;
        html += createPromptSection('tool-description', 'Tool Description', toolContent, false);
    }

    // Section 6: Conversation History
    if (promptData.conversation_history && promptData.conversation_history.length > 0) {
        const historyContent = promptData.conversation_history.map(qa =>
            `<div class="conversation-item">
                <div class="conv-question"><strong>Previous Q:</strong> ${escapeHtml(qa.question)}</div>
                <div class="conv-sql"><strong>Previous SQL:</strong><pre>${escapeHtml(qa.sql)}</pre></div>
            </div>`
        ).join('');
        html += createPromptSection('conversation-history', `Conversation History (${promptData.conversation_history.length} Q&As)`, historyContent, true);
    }

    // Section 7: Current Question
    if (promptData.current_question) {
        html += createPromptSection('current-question', 'Current Question',
            `<div class="current-question-text">${escapeHtml(promptData.current_question)}</div>`, true);
    }

    // Section 8: Full Text (complete prompt)
    if (promptData.full_text) {
        html += createPromptSection('full-text', 'Full Prompt Text',
            `<pre class="prompt-text">${escapeHtml(promptData.full_text)}</pre>`, false);
    }

    html += '</div>';
    promptContent.innerHTML = html;
}

// Create a collapsible prompt section
function createPromptSection(id, title, content, expanded = false) {
    const expandedClass = expanded ? 'expanded' : '';
    const displayStyle = expanded ? 'block' : 'none';
    const arrow = expanded ? '▼' : '▶';

    return `
        <div class="prompt-section ${expandedClass}">
            <div class="prompt-section-header" onclick="togglePromptSection('${id}')">
                <span class="section-arrow" id="arrow-${id}">${arrow}</span>
                <span class="section-title">${title}</span>
            </div>
            <div class="prompt-section-content" id="content-${id}" style="display: ${displayStyle};">
                ${content}
            </div>
        </div>
    `;
}

// Toggle a prompt section
function togglePromptSection(sectionId) {
    const content = document.getElementById(`content-${sectionId}`);
    const arrow = document.getElementById(`arrow-${sectionId}`);

    if (content.style.display === 'none') {
        content.style.display = 'block';
        arrow.textContent = '▼';
    } else {
        content.style.display = 'none';
        arrow.textContent = '▶';
    }
}

// Switch between prompt tabs
function switchPromptTab(tabName) {
    const tabContent = document.querySelector('.prompt-tab-content');
    const promptSubTabs = new Set(['query', 'insights', 'chart']);
    let promptSubTab = null;
    if (promptSubTabs.has(tabName)) {
        promptSubTab = tabName;
        tabName = 'prompts';
    }

    // Show tab content container on first interaction
    if (tabContent && tabContent.style.display === 'none') {
        tabContent.style.display = 'block';
    }

    // Hide all tab panes
    const allPanes = document.querySelectorAll('.tab-pane');
    allPanes.forEach(pane => {
        pane.style.display = 'none';
        pane.classList.remove('active');
    });

    // Remove active class from all tabs
    const allTabs = document.querySelectorAll('.prompt-tab');
    allTabs.forEach(tab => {
        tab.classList.remove('active');
    });

    // Show selected tab pane
    const selectedPane = document.getElementById(`content-${tabName}`);
    if (selectedPane) {
        selectedPane.style.display = 'block';
        selectedPane.classList.add('active');
    }

    // Add active class to selected tab
    const selectedTab = document.getElementById(`tab-${tabName}`);
    if (selectedTab) {
        selectedTab.classList.add('active');
        selectedTab.setAttribute('aria-selected', 'true');
    }
    allTabs.forEach(tab => {
        if (tab !== selectedTab) tab.setAttribute('aria-selected', 'false');
    });

    if (tabName === 'prompts') {
        switchPromptSubTab(promptSubTab || _activePromptSubTab || 'query');
    }
}

let _activePromptSubTab = 'query';

function switchPromptSubTab(tabName) {
    _activePromptSubTab = ['query', 'insights', 'chart'].includes(tabName) ? tabName : 'query';

    document.querySelectorAll('.prompt-subpane').forEach(pane => {
        pane.style.display = 'none';
        pane.classList.remove('active');
    });
    document.querySelectorAll('.prompt-subtab').forEach(tab => {
        tab.classList.remove('active');
        tab.setAttribute('aria-selected', 'false');
    });

    const pane = document.getElementById(`content-${_activePromptSubTab}`);
    if (pane) {
        pane.style.display = 'block';
        pane.classList.add('active');
    }
    const tab = document.getElementById(`tab-${_activePromptSubTab}`);
    if (tab) {
        tab.classList.add('active');
        tab.setAttribute('aria-selected', 'true');
    }
}
window.switchPromptSubTab = switchPromptSubTab;

// Question History Management
function saveToHistory(question) {
    // History is now stored in database automatically when queries are made
    // Just refresh the display to show updated history from DB
    displayHistory();
}

async function loadSavedAnalyses() {
    const list = document.getElementById('saved-analyses-list');
    if (!list) return;
    const connection = getActiveConnection();
    if (!connection) {
        list.innerHTML = '<p class="history-empty">Pick a connection to see saved analyses.</p>';
        return;
    }
    list.innerHTML = '<p class="history-empty">Loading saved analyses…</p>';
    try {
        const res = await fetch(`/api/saved-analyses?connection=${encodeURIComponent(connection)}&limit=50`);
        if (!res.ok) throw new Error('Failed to fetch saved analyses');
        const data = await res.json();
        const items = data.items || [];
        if (!items.length) {
            list.innerHTML = '<p class="history-empty">No saved analyses yet.</p>';
            return;
        }
        list.innerHTML = items.map(item => {
            const name = escapeHtml(item.name || item.question || 'Saved analysis');
            const question = escapeHtml(item.question || '');
            const rows = Number.isFinite(item.row_count) ? `${item.row_count} rows` : '';
            const meta = [rows, item.has_chart ? 'chart' : '', item.has_insights ? 'insights' : '']
                .filter(Boolean).join(' · ');
            return `<div class="saved-analysis-item" role="button" tabindex="0" onclick="restoreSavedAnalysis('${escapeHtml(item.id)}')">
                <div class="saved-analysis-title">${name}</div>
                <div class="saved-analysis-question">${question}</div>
                <div class="saved-analysis-meta">${escapeHtml(meta)}</div>
            </div>`;
        }).join('');
    } catch (err) {
        console.error('[SavedAnalyses]', err);
        list.innerHTML = '<p class="history-empty">Unable to load saved analyses.</p>';
    }
}
window.loadSavedAnalyses = loadSavedAnalyses;

function _analysisSnapshotName() {
    const fallback = currentQuestion || 'Saved analysis';
    if (fallback.length <= 80) return fallback;
    return fallback.slice(0, 77) + '...';
}

async function saveCurrentAnalysis() {
    const connection = requireConnection();
    if (!connection || !currentResults) return;
    const btn = document.getElementById('save-analysis-btn');
    const oldText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving…';
    }
    try {
        const chartState = (chartManager && typeof chartManager.getSaveState === 'function')
            ? chartManager.getSaveState()
            : {};
        const insightsState = (insightsManager && typeof insightsManager.getSaveState === 'function')
            ? insightsManager.getSaveState()
            : null;
        const res = await fetch('/api/saved-analyses', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                connection,
                name: _analysisSnapshotName(),
                question: currentQuestion,
                sql: currentSql,
                query_id: currentQueryId,
                results: currentResults,
                chart_spec: chartState.chart_spec || null,
                chart_config: chartState.chart_config || null,
                insights: insightsState,
            }),
        });
        if (!res.ok) throw new Error(await res.text());
        showToast('Analysis saved', 'success');
        await loadSavedAnalyses();
    } catch (err) {
        console.error('[SavedAnalyses] save failed', err);
        showToast('Save failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = oldText || 'Save';
        }
    }
}
window.saveCurrentAnalysis = saveCurrentAnalysis;

// ── Send / Share a result (per-user connector action gate) ──────────────────
//
// The browser only proposes a NAMED action against an opaque result handle.
// Recipients + subject are validated server-side against the connector's policy,
// and the payload is rendered from the server-held snapshot — never from these
// browser rows.

async function openSendResult() {
    if (!window._resultHandle) {
        showToast('This result cannot be sent (no server snapshot).', 'error');
        return;
    }
    let connections = [];
    try {
        const r = await fetch('/api/me/connections');
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
        connections = (await r.json()).connections || [];
    } catch (e) {
        showToast('Could not load your connections — ' + e.message, 'error');
        return;
    }

    // v1 action is email; offer connected email connectors.
    const emailConns = connections.filter(c => c.category === 'email');
    const connected = emailConns.filter(c => c.connected);

    if (!emailConns.length) {
        showToast('No email connector is available to you. Ask an admin to grant access.', 'info');
        return;
    }
    if (!connected.length) {
        _sendResultModal({ needsConnect: true, connector: emailConns[0] });
        return;
    }
    _sendResultModal({ connectors: connected });
}
window.openSendResult = openSendResult;

function _sendResultModal(opts) {
    document.getElementById('sr-overlay')?.remove();

    const rows = (currentResults && (currentResults.data || currentResults.rows)) || [];
    const rowCount = rows.length;

    const overlay = document.createElement('div');
    overlay.className = 'sr-overlay';
    overlay.id = 'sr-overlay';

    if (opts.needsConnect) {
        overlay.innerHTML = `
            <div class="sr-modal" role="dialog" aria-modal="true" aria-labelledby="sr-title">
                <div class="sr-head"><h3 id="sr-title">Connect ${escapeHtml(opts.connector.display_name)}</h3></div>
                <div class="sr-body">
                    <p class="sr-note">To email results as yourself, first connect your ${escapeHtml(opts.connector.display_name)} account. You'll be redirected to sign in and grant permission.</p>
                </div>
                <div class="sr-foot">
                    <button class="sp-btn-ghost-sm" id="sr-cancel">Cancel</button>
                    <button class="sp-btn-primary-sm" id="sr-connect">Connect</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        overlay.querySelector('#sr-cancel').addEventListener('click', () => overlay.remove());
        overlay.querySelector('#sr-connect').addEventListener('click', () => {
            window.location.href = `/integrations/${encodeURIComponent(opts.connector.connector_id)}/connect`;
        });
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
        return;
    }

    const conns = opts.connectors;
    const selHtml = conns.length > 1
        ? `<select class="sp-conn-input" id="sr-conn">${conns.map(c => `<option value="${escapeHtml(c.connector_id)}">${escapeHtml(c.display_name)} — ${escapeHtml(c.external_account || '')}</option>`).join('')}</select>`
        : `<div class="sr-note">Sending from <strong>${escapeHtml(conns[0].external_account || conns[0].display_name)}</strong></div>`;

    const defaultSubject = (window.currentQuestion || 'Query results').slice(0, 120);

    overlay.innerHTML = `
        <div class="sr-modal" role="dialog" aria-modal="true" aria-labelledby="sr-title">
            <div class="sr-head"><h3 id="sr-title">Email this result</h3></div>
            <div class="sr-body">
                <div class="sr-field">
                    <label>From</label>
                    ${selHtml}
                </div>
                <div class="sr-field">
                    <label>Recipients</label>
                    <input class="sp-conn-input" id="sr-recipients" placeholder="name@example.com, other@example.com">
                    <div class="sr-hint">Comma-separated. Recipients are validated against your organization's policy.</div>
                </div>
                <div class="sr-field">
                    <label>Subject</label>
                    <input class="sp-conn-input" id="sr-subject" value="${escapeHtml(defaultSubject)}">
                </div>
                <div class="sr-field">
                    <label>Note (optional)</label>
                    <textarea class="sp-conn-input" id="sr-note" rows="3" placeholder="Add a short message…"></textarea>
                </div>
                <div class="sr-summary">
                    A server-rendered summary of <strong>${rowCount.toLocaleString()}</strong> row${rowCount === 1 ? '' : 's'} will be sent from your mailbox. No attachments or raw data links are included.
                </div>
                <div class="sr-error" id="sr-error"></div>
            </div>
            <div class="sr-foot">
                <button class="sp-btn-ghost-sm" id="sr-cancel">Cancel</button>
                <button class="sp-btn-primary-sm" id="sr-send">Send email</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    const err = overlay.querySelector('#sr-error');
    const close = () => overlay.remove();
    overlay.querySelector('#sr-cancel').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', function onEsc(e) {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onEsc); }
    });

    overlay.querySelector('#sr-send').addEventListener('click', async () => {
        err.textContent = '';
        const connectorId = conns.length > 1 ? overlay.querySelector('#sr-conn').value : conns[0].connector_id;
        const recipients = (overlay.querySelector('#sr-recipients').value || '')
            .split(',').map(s => s.trim()).filter(Boolean);
        const subject = (overlay.querySelector('#sr-subject').value || '').trim();
        const note = (overlay.querySelector('#sr-note').value || '').trim();
        if (!recipients.length) { err.textContent = 'Enter at least one recipient.'; return; }
        if (!subject) { err.textContent = 'Enter a subject.'; return; }

        const btn = overlay.querySelector('#sr-send');
        btn.disabled = true;
        btn.textContent = 'Checking…';
        try {
            // 1) Propose the named action against the opaque result handle.
            const pRes = await fetch('/api/actions/propose', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    connector_id: connectorId,
                    action: 'send_email',
                    result_handle: window._resultHandle,
                }),
            });
            if (!pRes.ok) throw new Error((await pRes.json().catch(() => ({}))).detail || `HTTP ${pRes.status}`);
            const proposal = await pRes.json();

            // 2) Preview: the server validates recipients + policy and returns the
            //    exact, server-derived summary (no side effects yet).
            const vRes = await fetch(`/api/actions/${encodeURIComponent(proposal.proposal_id)}/preview`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nonce: proposal.nonce, recipients, subject, note }),
            });
            if (!vRes.ok) throw new Error((await vRes.json().catch(() => ({}))).detail || `HTTP ${vRes.status}`);
            const preview = await vRes.json();

            // 3) Show the confirmation step; only on explicit confirm do we execute.
            _renderSendConfirm(overlay, { proposal, preview, note });
        } catch (e) {
            err.textContent = e.message;
            btn.disabled = false;
            btn.textContent = 'Send email';
        }
    });
}

function _renderSendConfirm(overlay, { proposal, preview, note }) {
    const recips = preview.recipients || [];
    const external = new Set(preview.external_recipients || []);
    const recipHtml = recips.map(r =>
        external.has(r)
            ? `<span class="sr-recip sr-recip-ext" title="External recipient">${escapeHtml(r)} ⚠</span>`
            : `<span class="sr-recip">${escapeHtml(r)}</span>`
    ).join(' ');
    const extWarn = preview.has_external
        ? `<div class="sr-warn" role="alert">⚠ This email includes <strong>external</strong> recipient(s) outside your organization. Review carefully before sending.</div>`
        : '';
    const snap = preview.snapshot || {};

    overlay.innerHTML = `
        <div class="sr-modal" role="dialog" aria-modal="true" aria-labelledby="sr-title">
            <div class="sr-head"><h3 id="sr-title">Confirm send</h3></div>
            <div class="sr-body">
                ${extWarn}
                <div class="sr-field"><label>From</label>
                    <div class="sr-note"><strong>${escapeHtml(preview.sender || '')}</strong></div></div>
                <div class="sr-field"><label>To</label><div class="sr-recips">${recipHtml}</div></div>
                <div class="sr-field"><label>Subject</label>
                    <div class="sr-note">${escapeHtml(preview.subject || '')}</div></div>
                <div class="sr-summary">
                    A server-rendered summary of <strong>${(snap.row_count || 0).toLocaleString()}</strong>
                    row${snap.row_count === 1 ? '' : 's'} will be sent from your mailbox.
                    No attachments or raw data links are included.
                </div>
                <div class="sr-error" id="sr-error"></div>
            </div>
            <div class="sr-foot">
                <button class="sp-btn-ghost-sm" id="sr-back">Back</button>
                <button class="sp-btn-primary-sm" id="sr-confirm">Confirm &amp; send</button>
            </div>
        </div>`;

    const err = overlay.querySelector('#sr-error');
    overlay.querySelector('#sr-back').addEventListener('click', () => overlay.remove());

    overlay.querySelector('#sr-confirm').addEventListener('click', async () => {
        err.textContent = '';
        const btn = overlay.querySelector('#sr-confirm');
        btn.disabled = true;
        btn.textContent = 'Sending…';
        try {
            // 4) Execute: single-use nonce, server re-validates and sends once.
            const eRes = await fetch(`/api/actions/${encodeURIComponent(proposal.proposal_id)}/execute`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    nonce: proposal.nonce,
                    recipients: preview.recipients,
                    subject: preview.subject,
                    note,
                    confirmed: true,
                }),
            });
            if (!eRes.ok) throw new Error((await eRes.json().catch(() => ({}))).detail || `HTTP ${eRes.status}`);
            const result = await eRes.json();
            overlay.remove();
            if (result.accepted) {
                showToast('Email accepted for delivery.', 'success');
            } else {
                showToast('Send failed: ' + (result.message || 'unknown outcome'), 'error');
            }
        } catch (e) {
            err.textContent = e.message;
            btn.disabled = false;
            btn.textContent = 'Confirm & send';
        }
    });
}

async function restoreSavedAnalysis(savedId) {
    if (!savedId) return;
    try {
        const res = await fetch(`/api/saved-analyses/${encodeURIComponent(savedId)}`);
        if (!res.ok) throw new Error(await res.text());
        const item = await res.json();
        const snapshot = item.result_snapshot || {};
        const results = {
            columns: snapshot.columns || item.columns || [],
            rows: snapshot.rows || [],
        };
        const restored = {
            question: item.question,
            query_id: item.query_id,
            session_id: null,
            sql: item.generated_sql,
            results,
            prompt: null,
            metrics: { restored: true },
            trace: [],
        };
        _isRestoringSavedAnalysis = true;
        _restoredSavedQuestion = item.question || '';
        displayResults(restored);
        _isRestoringSavedAnalysis = false;
        const input = document.getElementById('question-input');
        if (input && item.question) input.value = item.question;
        if (item.insights_payload) {
            if (!insightsManager) insightsManager = new window.InsightsManager();
            const container = document.getElementById('insights-container');
            if (container) {
                container.style.display = 'block';
                insightsManager.state.currentInsights = item.insights_payload;
                insightsManager.displayInsights(container, item.insights_payload);
                if (item.insights_payload.prompt) insightsManager.displayInsightsPrompt(item.insights_payload);
            }
        }
        if (item.chart_config) {
            try {
                await initializeChartFeature(results);
                if (!chartManager || typeof chartManager.restoreSavedChart !== 'function') {
                    throw new Error('Chart manager unavailable');
                }
                await chartManager.restoreSavedChart(item.chart_config, item.chart_spec || null);
            } catch (chartErr) {
                console.warn('[SavedAnalyses] chart restore failed', chartErr);
            }
        } else {
            await initializeChartFeature(results);
        }
        showToast('Saved analysis restored', 'success');
    } catch (err) {
        _isRestoringSavedAnalysis = false;
        console.error('[SavedAnalyses] restore failed', err);
        showToast('Restore failed', 'error');
    }
}
window.restoreSavedAnalysis = restoreSavedAnalysis;

function rerunFreshFromSaved() {
    const input = document.getElementById('question-input');
    if (input && _restoredSavedQuestion) input.value = _restoredSavedQuestion;
    askQuestion();
}
window.rerunFreshFromSaved = rerunFreshFromSaved;

async function displayHistory() {
    const historyDiv = document.getElementById('question-history');
    const clearBtn = document.getElementById('clear-history-btn');

    const connection = getActiveConnection();
    if (!connection) {
        historyDiv.innerHTML = '<p class="history-empty">Pick a connection to see your recent questions.</p>';
        if (clearBtn) clearBtn.style.display = 'none';
        loadSavedAnalyses();
        return;
    }

    try {
        loadSavedAnalyses();
        // Fetch both pinned and recent questions for the active connection
        const qs = `?connection=${encodeURIComponent(connection)}`;
        const [pinnedResponse, recentResponse] = await Promise.all([
            fetch(`/api/user/pinned-questions${qs}`),
            fetch(`/api/user/recent-questions${qs}&limit=15`)
        ]);

        if (!pinnedResponse.ok || !recentResponse.ok) {
            throw new Error('Failed to fetch history');
        }

        const pinnedData = await pinnedResponse.json();
        const recentData = await recentResponse.json();

        const pinnedQuestions = pinnedData.questions || [];
        const recentQuestions = recentData.questions || [];

        // Mirror into autocomplete caches so Tier 1 (Recent) is instant.
        recentQuestionsCache = recentQuestions.slice();
        pinnedQuestionsCache = pinnedQuestions.slice();

        // Refresh the hero starter chips now that history is available.
        refreshHeroEmptyState();

        // Show/hide the sidebar search input depending on whether there are items.
        const questionSearchInput = document.getElementById('question-search');
        if (pinnedQuestions.length === 0 && recentQuestions.length === 0) {
            historyDiv.innerHTML = '<p class="history-empty">No questions yet — ask one above and it\'ll show up here.</p>';
            clearBtn.style.display = 'none';
            if (questionSearchInput) { questionSearchInput.style.display = 'none'; questionSearchInput.value = ''; }
            return;
        }
        if (questionSearchInput) questionSearchInput.style.display = 'block';

        clearBtn.style.display = 'none';  // Hide clear button since history is from DB

        let html = '';

        // Show pinned questions first with pin icon
        if (pinnedQuestions.length > 0) {
            html += pinnedQuestions.map(q =>
                `<div class="history-item pinned-item">
                    <span class="pin-icon" onclick="unpinQuestion(event, '${escapeHtml(q).replace(/'/g, "\\'")}')">📌</span>
                    <span class="question-text" onclick="_jeenQuestionClick('${escapeHtml(q).replace(/'/g, "\\'")}')"
                          title="${escapeHtml(q)}">${escapeHtml(q)}</span>
                </div>`
            ).join('');
        }

        // Show recent questions below pinned ones with unpin icon
        if (recentQuestions.length > 0) {
            html += recentQuestions.map(q =>
                `<div class="history-item">
                    <span class="pin-icon" onclick="pinQuestion(event, '${escapeHtml(q).replace(/'/g, "\\'")}')">📍</span>
                    <span class="question-text" onclick="_jeenQuestionClick('${escapeHtml(q).replace(/'/g, "\\'")}')"
                          title="${escapeHtml(q)}">${escapeHtml(q)}</span>
                </div>`
            ).join('');
        }

        historyDiv.innerHTML = html;
    } catch (error) {
        console.error('Error loading history:', error);
        historyDiv.innerHTML = '<p class="history-empty">Unable to load history right now.</p>';
        clearBtn.style.display = 'none';
    }
}

// Pin a question
async function pinQuestion(event, question) {
    event.stopPropagation();  // Prevent triggering fillQuestion
    const connection = requireConnection();
    if (!connection) return;

    try {
        const response = await fetch('/api/user/pin-question', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ connection, question: question })
        });

        if (response.ok) {
            displayHistory();  // Refresh the list
        } else {
            console.error('Failed to pin question');
        }
    } catch (error) {
        console.error('Error pinning question:', error);
    }
}

// Unpin a question
async function unpinQuestion(event, question) {
    event.stopPropagation();  // Prevent triggering fillQuestion
    const connection = requireConnection();
    if (!connection) return;

    try {
        const response = await fetch('/api/user/unpin-question', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ connection, question: question })
        });

        if (response.ok) {
            displayHistory();  // Refresh the list
        } else {
            console.error('Failed to unpin question');
        }
    } catch (error) {
        console.error('Error unpinning question:', error);
    }
}

function clearHistory() {
    // History is now managed in the database
    // Clearing history would require database operations
    // This function is kept for compatibility but does nothing
    console.log('History is managed in the database');
}

// Switch the left sidebar between the Tables pane and the Recent Questions pane.
// `tab` is either 'tables' or 'recent'. The choice is persisted to localStorage.
function switchSidebarTab(tab) {
    ['tables', 'recent'].forEach(t => {
        const btn  = document.getElementById('sidebar-tab-'  + t);
        const pane = document.getElementById('sidebar-pane-' + t);
        const active = (t === tab);
        if (btn)  {
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', active ? 'true' : 'false');
        }
        if (pane) pane.style.display = active ? '' : 'none';
    });
    try { localStorage.setItem(SIDEBAR_TAB_KEY, tab); } catch (_) {}
}

// Filter the sidebar recent-questions list in real time.
function filterQuestionHistory() {
    const q = (document.getElementById('question-search')?.value || '').toLowerCase().trim();
    const items = document.querySelectorAll('#question-history .history-item');
    let shown = 0;
    items.forEach(item => {
        const text = (item.querySelector('.question-text')?.textContent || '').toLowerCase();
        const match = !q || text.includes(q);
        item.style.display = match ? '' : 'none';
        if (match) shown++;
    });
    // If nothing matches, show a soft message.
    let noMsg = document.getElementById('question-search-empty');
    if (!noMsg) {
        noMsg = document.createElement('p');
        noMsg.id = 'question-search-empty';
        noMsg.className = 'history-empty';
        noMsg.textContent = 'No matching questions.';
        document.getElementById('question-history')?.after(noMsg);
    }
    noMsg.style.display = (q && shown === 0) ? '' : 'none';
}

// ── History Log Drawer ───────────────────────────────────────────────
let _historyLogEntries = []; // full list from API, used for client-side filter

function _relTime(isoStr) {
    if (!isoStr) return '';
    const diff = Date.now() - new Date(isoStr).getTime();
    const s = Math.floor(diff / 1000);
    if (s < 60)  return 'just now';
    const m = Math.floor(s / 60);
    if (m < 60)  return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24)  return `${h}h ago`;
    const d = Math.floor(h / 24);
    return `${d}d ago`;
}

function _shortSessionId(sessionId) {
    if (!sessionId) return '';
    const s = String(sessionId).replace(/-/g, '');
    return s.length > 8 ? `${s.slice(0, 8)}…` : s;
}

function _renderHistoryEntries(entries) {
    const body = document.getElementById('history-drawer-body');
    if (!body) return;
    if (!entries.length) {
        body.innerHTML = '<p class="history-log-empty">No queries yet for this connection.</p>';
        return;
    }
    body.innerHTML = entries.map(e => {
        const statusLabel = e.status || 'unknown';
        const time  = _relTime(e.asked_at);
        const parts = [];
        if (e.session_id) {
            parts.push(
                `<span class="history-log-session" title="Session ${escapeHtml(e.session_id)}">sess ${_shortSessionId(e.session_id)}</span>`
            );
        }
        if (e.tokens)   parts.push(`${_formatTokens(e.tokens)} tok`);
        if (e.llm_ms)   parts.push(`LLM ${_fmtMs(e.llm_ms)}`);
        if (e.exec_ms)  parts.push(`DB ${_fmtMs(e.exec_ms)}`);
        // net+proxy = graph_time_ms - llm_ms - exec_ms (only when graph_ms is stored)
        if (e.graph_ms && e.graph_ms > 0) {
            const netMs = Math.max(0, e.graph_ms - (e.llm_ms || 0) - (e.exec_ms || 0));
            if (netMs > 50) parts.push(`net ${_fmtMs(netMs)}`);
        }
        if (e.row_count !== null && e.row_count !== undefined) parts.push(`${e.row_count} rows`);
        const metaStr = [time, ...parts].filter(Boolean).join('<span class="history-log-dot">&nbsp;·&nbsp;</span>');
        const question = escapeHtml(e.question || '(empty)');
        const safeQ = (e.question || '').replace(/'/g, "\\'");
        return `<div class="history-log-entry" onclick="if(window._historyDrawerClose)window._historyDrawerClose();fillQuestion('${escapeHtml(safeQ)}')">
  <div class="history-log-entry-question">${question}</div>
  <div class="history-log-meta">
    <span class="history-log-status" data-status="${statusLabel}">${statusLabel}</span>
    ${metaStr}
  </div>
</div>`;
    }).join('');
}

async function loadHistoryLog() {
    const connection = getActiveConnection();
    const body = document.getElementById('history-drawer-body');
    if (!connection) {
        if (body) body.innerHTML = '<p class="history-log-empty">Pick a connection first.</p>';
        return;
    }
    if (body) body.innerHTML = '<p class="history-log-empty">Loading…</p>';
    try {
        const res = await fetch(`/api/user/history-log?connection=${encodeURIComponent(connection)}&limit=100`);
        const data = await res.json();
        _historyLogEntries = data.entries || [];
        _renderHistoryEntries(_historyLogEntries);
    } catch (err) {
        console.error('[HistoryLog]', err);
        if (body) body.innerHTML = '<p class="history-log-empty">Failed to load history.</p>';
    }
}

function filterHistoryLog() {
    const q = (document.getElementById('history-log-search')?.value || '').toLowerCase().trim();
    if (!q) {
        _renderHistoryEntries(_historyLogEntries);
        return;
    }
    _renderHistoryEntries(_historyLogEntries.filter(e =>
        (e.question || '').toLowerCase().includes(q)
        || (e.session_id || '').toLowerCase().includes(q)
        || (e.query_id || '').toLowerCase().includes(q)
    ));
}

// ── Execution Trace Panel ────────────────────────────────────────────────────

/**
 * Render the per-node execution trace into the Trace tab of the dev drawer.
 * Called from displayResults() after each query.
 *
 * @param {Array} traceEvents  - Array of trace event objects from the API.
 * @param {Object} metrics     - The metrics dict from the API response.
 */
function renderTrace(traceEvents, metrics) {
    const panel   = document.getElementById('trace-panel');
    const toolbar = document.getElementById('dp-log-toolbar');
    if (!panel) return;

    // Store for filtering
    _allTraceEvents  = traceEvents || [];
    _traceMetrics    = metrics || {};
    _activeTraceFilter = 'all';
    _traceSearchQ    = '';

    if (!traceEvents || traceEvents.length === 0) {
        panel.innerHTML = '<p class="trace-empty">No trace data for this query.</p>';
        if (toolbar) toolbar.hidden = true;
        return;
    }

    // Build the filter toolbar
    if (toolbar) {
        _buildTraceToolbar(toolbar, traceEvents);
        toolbar.hidden = false;
    }

    // Render the timeline + summary chips
    _renderTraceEvents();

    // Show the trace badge on the dev-panel button
    const badge = document.getElementById('dev-panel-badge');
    if (badge) badge.hidden = false;
}

/**
 * Determine a log level for a trace event.
 * Returns 'error' | 'warn' | 'llm' | 'db' | 'info'
 */
function _traceEventLevel(ev) {
    if (ev.status === 'error') return 'error';
    if (ev.feedback_type || ev.status === 'blocked' || ev.status === 'retry') return 'warn';
    // Catalog loaded via MCP is not a database query — keep it out of the DB
    // filter. A metadata-DB catalog load is still a real DB read, so stays 'db'.
    if (ev.node === 'catalog_lookup') {
        const src = ev.catalog_source || (/mcp/i.test(ev.detail || '') ? 'mcp' : 'db');
        return src === 'mcp' ? 'info' : 'db';
    }
    if ((ev.type || '').toLowerCase() === 'llm') return 'llm';
    if ((ev.type || '').toLowerCase() === 'db')  return 'db';
    return 'info';
}

/**
 * Build (or rebuild) the filter toolbar element.
 */
function _buildTraceToolbar(toolbar, events) {
    const counts = { all: events.length, llm: 0, db: 0, warn: 0, error: 0 };
    events.forEach(ev => {
        const lv = _traceEventLevel(ev);
        if (counts[lv] !== undefined) counts[lv]++;
    });

    const LEVELS  = ['all', 'llm', 'db', 'warn', 'error'];
    const LABELS  = { all: 'All', llm: 'LLM', db: 'DB', warn: 'Warn', error: 'Error' };

    let html = '<div class="trace-view-seg" role="tablist" aria-label="Trace view mode">';
    html += `<button class="${_traceViewMode === 'flow' ? 'active' : ''}" onclick="_switchTraceView('flow')" role="tab" aria-selected="${_traceViewMode === 'flow'}">Flow</button>`;
    html += `<button class="${_traceViewMode === 'log' ? 'active' : ''}" onclick="_switchTraceView('log')" role="tab" aria-selected="${_traceViewMode === 'log'}">Log</button>`;
    html += '</div>';

    if (_traceViewMode === 'log') {
        html += '<div class="dp-log-filters">';
        LEVELS.forEach(lv => {
            const c = counts[lv] || 0;
            if (lv !== 'all' && c === 0) return;
            const active = _activeTraceFilter === lv ? ' active' : '';
            html += `<button class="dp-log-filter${active}" data-level="${lv}" onclick="_switchTraceFilter('${lv}')">`
                + `${LABELS[lv]} <span class="dp-lf-count">${c}</span></button>`;
        });
        html += '</div>';
        html += `<input type="text" class="dp-log-search" id="dp-log-search" `
            + `placeholder="Search log…" value="${escapeHtml(_traceSearchQ)}" `
            + `oninput="_traceSearchInput(this.value)" aria-label="Search execution log" />`;
        html += `<button class="dp-log-legend-btn${_traceLegendOpen ? ' active' : ''}" id="dp-log-legend-btn" `
            + `title="What does each row and number mean?" aria-label="Toggle log legend" `
            + `onclick="_toggleTraceLegend()">?</button>`;
    }

    toolbar.innerHTML = html;
}

function _switchTraceView(mode) {
    _traceViewMode = mode === 'log' ? 'log' : 'flow';
    try { localStorage.setItem('jeen_trace_view_mode', _traceViewMode); } catch (_) {}
    const toolbar = document.getElementById('dp-log-toolbar');
    if (toolbar) _buildTraceToolbar(toolbar, _allTraceEvents || []);
    _renderTraceEvents();
}
window._switchTraceView = _switchTraceView;

/**
 * Toggle the explanatory legend that documents the log rows and timings.
 */
function _toggleTraceLegend() {
    _traceLegendOpen = !_traceLegendOpen;
    try { localStorage.setItem('jeen_log_legend_open', _traceLegendOpen ? '1' : '0'); } catch (_) {}
    const b = document.getElementById('dp-log-legend-btn');
    if (b) b.classList.toggle('active', _traceLegendOpen);
    _renderTraceEvents();
}
window._toggleTraceLegend = _toggleTraceLegend;

/**
 * Build the legend HTML explaining what each row / number in the log means.
 */
function _buildTraceLegendHtml() {
    return '<div class="trace-legend">'
        + '<div class="trace-legend-title">How to read this log</div>'
        + '<ul class="trace-legend-list">'
        + '<li>Each row is one step (node) in the LangGraph pipeline, in execution order. '
        + '<strong>Hover a step name</strong> to see what it does.</li>'
        + '<li>The coloured dot is severity: '
        + '<span class="trace-lg-dot trace-lv-info"></span> info · '
        + '<span class="trace-lg-dot trace-lv-llm"></span> LLM call · '
        + '<span class="trace-lg-dot trace-lv-db"></span> database · '
        + '<span class="trace-lg-dot trace-lv-warn"></span> warning/retry · '
        + '<span class="trace-lg-dot trace-lv-error"></span> error.</li>'
        + '<li>The text after the bar is the step\u2019s result detail. '
        + '<strong>catalog_lookup</strong> shows where the metadata came from (MCP or metadata DB), cache HIT/MISS and load time.</li>'
        + `<li>The number on the right is per-step time. ${escapeHtml(_TIMING_TIPS.nodeMs)}</li>`
        + '<li>Summary chips at the top: '
        + `<strong>wall</strong> = total time in the browser; <strong>main graph</strong> = sum of the /api/ask LangGraph step times; `
        + `<strong>LLM</strong> = time waiting on the model; <strong>DB</strong> = SQL execution time; `
        + `<strong>net</strong> = wall − main graph (HTTP + proxy overhead). Hover any chip for the exact formula.</li>`
        + '</ul></div>';
}

/**
 * Switch the active level filter and re-render events.
 */
function _switchTraceFilter(level) {
    _activeTraceFilter = level;
    document.querySelectorAll('.dp-log-filter').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.level === level);
    });
    _renderTraceEvents();
}
window._switchTraceFilter = _switchTraceFilter;

/**
 * Handle search input in the log toolbar.
 */
function _traceSearchInput(val) {
    _traceSearchQ = (val || '').trim();
    _renderTraceEvents();
}
window._traceSearchInput = _traceSearchInput;

const _TRACE_FLOW_COLUMNS = [
    {
        title: 'Memory',
        hint: 'Conversation context and optional summarisation.',
        nodes: ['memory_shrink_check', 'memory_summarizer', 'memory_answer_generator'],
    },
    {
        title: 'Routing',
        hint: 'Classifies the request and chooses the logical branch.',
        nodes: ['fused_router'],
    },
    {
        title: 'Catalog + Prompt',
        hint: 'Loads MCP/DB metadata and builds the system prompt.',
        nodes: ['catalog_lookup', 'prompt_builder'],
    },
    {
        title: 'SQL + Safety',
        hint: 'Generates SQL, validates syntax/tables, and checks DLP rules.',
        nodes: ['sql_generator', 'sqlglot_validate', 'dlp_check'],
    },
    {
        title: 'Execution + Eval',
        hint: 'Runs SQL, decides whether eval is needed, and checks intent.',
        nodes: ['execute_query', 'trivial_result_check', 'fused_eval_analytics', 'feedback_classifier'],
    },
    {
        title: 'Output',
        hint: 'Formats the answer, saves memory, and writes observability logs.',
        nodes: ['response_formatter', 'save_to_memory', 'observability_log'],
    },
];

const _TRACE_FLOW_EDGES = [
    ['memory_shrink_check', 'memory_summarizer', 'over budget'],
    ['memory_shrink_check', 'fused_router', 'within budget'],
    ['memory_summarizer', 'fused_router', 'then route'],
    ['fused_router', 'memory_answer_generator', 'from memory'],
    ['fused_router', 'catalog_lookup', 'needs query'],
    ['fused_router', 'response_formatter', 'blocked / greeting'],
    ['memory_answer_generator', 'catalog_lookup', 'needs fresh data'],
    ['memory_answer_generator', 'response_formatter', 'answer ready'],
    ['catalog_lookup', 'prompt_builder', 'catalog bundle'],
    ['prompt_builder', 'sql_generator', 'system prompt'],
    ['sql_generator', 'sqlglot_validate', 'SQL'],
    ['sql_generator', 'response_formatter', 'clarification'],
    ['sqlglot_validate', 'dlp_check', 'valid'],
    ['sqlglot_validate', 'feedback_classifier', 'syntax / table issue'],
    ['dlp_check', 'execute_query', 'safe'],
    ['dlp_check', 'response_formatter', 'blocked'],
    ['execute_query', 'trivial_result_check', 'rows'],
    ['execute_query', 'feedback_classifier', 'exec error'],
    ['trivial_result_check', 'fused_eval_analytics', 'needs eval'],
    ['trivial_result_check', 'response_formatter', 'trivial / eval off'],
    ['fused_eval_analytics', 'response_formatter', 'answers intent'],
    ['fused_eval_analytics', 'feedback_classifier', 'wrong result'],
    ['feedback_classifier', 'sql_generator', 'retry SQL'],
    ['feedback_classifier', 'catalog_lookup', 'missing table'],
    ['feedback_classifier', 'response_formatter', 'exhausted'],
    ['response_formatter', 'save_to_memory', 'final payload'],
    ['save_to_memory', 'observability_log', 'persisted'],
];

function _traceStatsByNode(events) {
    const stats = {};
    events.forEach((ev, idx) => {
        const node = ev.node || '?';
        const s = stats[node] || {
            count: 0,
            totalMs: 0,
            firstIdx: idx,
            lastIdx: idx,
            events: [],
        };
        s.count += 1;
        s.totalMs += ev.elapsed_ms || 0;
        s.lastIdx = idx;
        s.events.push(ev);
        stats[node] = s;
    });
    return stats;
}

function _traceRanEdges(events) {
    const edges = new Set();
    for (let i = 0; i < events.length - 1; i += 1) {
        const from = events[i]?.node;
        const to = events[i + 1]?.node;
        if (from && to) edges.add(`${from}->${to}`);
    }
    return edges;
}

function _traceFlowLayout() {
    const nodeW = 122;
    const nodeH = 34;
    const colGap = 170;
    const rowGap = 58;
    const marginX = 44;
    const marginY = 56;
    const maxRows = Math.max(..._TRACE_FLOW_COLUMNS.map(col => col.nodes.length), 1);
    const positions = {};

    _TRACE_FLOW_COLUMNS.forEach((col, colIdx) => {
        const colOffset = Math.max(0, (maxRows - col.nodes.length) * rowGap / 2);
        col.nodes.forEach((node, rowIdx) => {
            positions[node] = {
                x: marginX + colIdx * colGap,
                y: marginY + colOffset + rowIdx * rowGap,
                col: colIdx,
                row: rowIdx,
            };
        });
    });

    return {
        positions,
        nodeW,
        nodeH,
        width: marginX * 2 + (_TRACE_FLOW_COLUMNS.length - 1) * colGap + nodeW,
        height: marginY * 2 + maxRows * rowGap,
    };
}

function _shortTraceLabel(label, max = 17) {
    const str = String(label || '');
    return str.length > max ? `${str.slice(0, max - 1)}…` : str;
}

function _buildTraceTransitionsHtml(ranEdges, ranNodes) {
    const layout = _traceFlowLayout();
    const { positions, nodeW, nodeH, width, height } = layout;

    let html = '<div class="trace-flow-edges trace-transition-map">';
    html += '<div class="trace-flow-edge-title">Transitions</div>';
    html += '<p class="trace-flow-edge-help">Directed transition map. The layout reads left-to-right, but retry/back edges can loop, so this is not a strict DAG.</p>';
    html += `<svg class="trace-transition-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="LangGraph transition map">`;
    html += '<defs>'
        + '<marker id="trace-arrow-run" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 Z" fill="var(--color-accent)"/></marker>'
        + '<marker id="trace-arrow-near" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 Z" fill="var(--color-muted)"/></marker>'
        + '<marker id="trace-arrow-skip" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 Z" fill="var(--color-faint)"/></marker>'
        + '</defs>';

    _TRACE_FLOW_EDGES.forEach(([from, to, label], idx) => {
        const a = positions[from];
        const b = positions[to];
        if (!a || !b) return;

        const key = `${from}->${to}`;
        const ran = ranEdges.has(key);
        const near = !ran && (ranNodes.has(from) || ranNodes.has(to));
        const marker = ran ? 'trace-arrow-run' : near ? 'trace-arrow-near' : 'trace-arrow-skip';
        const isBack = b.col <= a.col;
        const startX = isBack ? a.x : a.x + nodeW;
        const endX = isBack ? b.x + nodeW : b.x;
        const startY = a.y + nodeH / 2;
        const endY = b.y + nodeH / 2;
        const className = `trace-transition-edge${ran ? ' is-run' : near ? ' is-near' : ' is-skipped'}${isBack ? ' is-back' : ''}`;
        let d;
        let labelX;
        let labelY;

        if (isBack) {
            const lift = 40 + (idx % 3) * 14;
            const controlX = Math.min(startX, endX) - lift;
            d = `M ${startX} ${startY} C ${controlX} ${startY}, ${controlX} ${endY}, ${endX} ${endY}`;
            labelX = controlX + 4;
            labelY = (startY + endY) / 2 - 6;
        } else {
            const midX = (startX + endX) / 2;
            d = `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;
            labelX = midX;
            labelY = (startY + endY) / 2 - 7;
        }

        html += `<g class="${className}">`;
        html += `<title>${escapeHtml(_nodeLabel(from))} → ${escapeHtml(_nodeLabel(to))}: ${escapeHtml(label)}</title>`;
        html += `<path d="${d}" marker-end="url(#${marker})"></path>`;
        html += `<text x="${labelX}" y="${labelY}" text-anchor="${isBack ? 'end' : 'middle'}">${escapeHtml(_shortTraceLabel(label, isBack ? 14 : 18))}</text>`;
        html += '</g>';
    });

    Object.entries(positions).forEach(([node, pos]) => {
        const ran = ranNodes.has(node);
        html += `<g class="trace-transition-node${ran ? ' is-run' : ' is-skipped'}">`;
        html += `<title>${escapeHtml(node)} — ${escapeHtml(_NODE_INFO[node] || 'Pipeline step.')}</title>`;
        html += `<rect x="${pos.x}" y="${pos.y}" width="${nodeW}" height="${nodeH}" rx="8"></rect>`;
        html += `<text x="${pos.x + nodeW / 2}" y="${pos.y + nodeH / 2 + 4}" text-anchor="middle">${escapeHtml(_shortTraceLabel(_nodeLabel(node), 18))}</text>`;
        html += '</g>';
    });

    html += '</svg>';
    html += '</div>';
    return html;
}

function _buildTraceFlowHtml(events, metrics) {
    const stats = _traceStatsByNode(events);
    const ranEdges = _traceRanEdges(events);
    const ranNodes = new Set(Object.keys(stats));
    const repeated = Object.entries(stats).filter(([, s]) => s.count > 1);
    const slowestEntry = Object.entries(stats).reduce((best, entry) =>
        (!best || entry[1].totalMs > best[1].totalMs) ? entry : best, null);
    const slowestNode = slowestEntry && slowestEntry[1].totalMs > 0 ? slowestEntry[0] : null;
    const route = metrics.route || '—';

    let html = '<div class="trace-flow">';
    html += '<div class="trace-flow-note">'
        + '<strong>Logical flow.</strong> Columns show possible branches side-by-side; highlighted cards are the nodes that ran for this query. '
        + 'This view does not claim runtime parallelism.'
        + ` <span>route: <b>${escapeHtml(String(route))}</b></span>`
        + '</div>';
    html += '<div class="trace-flow-legend" aria-label="Flow color legend">'
        + '<span><i class="trace-flow-key trace-flow-key-llm"></i>LLM call</span>'
        + '<span><i class="trace-flow-key trace-flow-key-db"></i>DB / tool call</span>'
        + '<span><i class="trace-flow-key trace-flow-key-logic"></i>logic step</span>'
        + '<span><i class="trace-flow-key trace-flow-key-skipped"></i>not run</span>'
        + '<span><i class="trace-flow-key trace-flow-key-slowest"></i>slowest step</span>'
        + '</div>';

    html += '<div class="trace-flow-grid">';
    _TRACE_FLOW_COLUMNS.forEach(col => {
        html += `<section class="trace-flow-col" title="${escapeHtml(col.hint)}">`;
        html += `<div class="trace-flow-col-head"><span>${escapeHtml(col.title)}</span></div>`;
        col.nodes.forEach(node => {
            const s = stats[node];
            const ran = !!s;
            const last = s ? s.events[s.events.length - 1] : null;
            const lv = last ? _traceEventLevel(last) : 'info';
            const type = last?.type || _traceNodeType(node);
            const detail = last?.detail || (ran ? 'executed' : 'not run in this query');
            const title = `${node} — ${_NODE_INFO[node] || 'Pipeline step.'}`;
            const isSlowest = node === slowestNode;
            html += `<div class="trace-flow-node${ran ? ' is-run' : ' is-skipped'}${isSlowest ? ' is-slowest' : ''} trace-flow-${escapeHtml(type)}" title="${escapeHtml(title)}">`;
            html += `  <div class="trace-flow-node-main">`;
            html += `    <span class="trace-lv-dot trace-lv-${lv}" aria-hidden="true"></span>`;
            html += `    <span class="trace-flow-node-name">${escapeHtml(_nodeLabel(node))}</span>`;
            html += `  </div>`;
            html += `  <div class="trace-flow-node-meta">`;
            html += ran
                ? `<span>${_fmtMs(s.totalMs)}</span>${isSlowest ? '<span class="trace-flow-slowest">slowest</span>' : ''}${s.count > 1 ? `<span class="trace-flow-repeat">x${s.count}</span>` : ''}`
                : '<span>not run</span>';
            html += `  </div>`;
            html += `  <div class="trace-flow-node-detail">${escapeHtml(detail)}</div>`;
            html += '</div>';
        });
        html += '</section>';
    });
    html += '</div>';

    html += _buildTraceTransitionsHtml(ranEdges, ranNodes);

    if (repeated.length) {
        html += '<div class="trace-flow-retries"><strong>Repeated nodes:</strong> '
            + repeated.map(([node, s]) => `<code>${escapeHtml(_nodeLabel(node))}</code> x${s.count}`).join(' · ')
            + '</div>';
    }

    // Reconcile the per-step times with the wall clock so the numbers "add up".
    // Node cards below sum to the main-graph time; the remainder up to wall is
    // network + proxy/serialization overhead (net), not charged to any node.
    const graphMs = Object.values(stats).reduce((sum, s) => sum + (s.totalMs || 0), 0);
    const wallMs  = lastQueryDurationMs;
    if (Number.isFinite(wallMs) && wallMs > 0 && graphMs > 0) {
        const netMs = Math.max(0, wallMs - graphMs);
        html += '<div class="trace-flow-reconcile">'
            + `The step times above add up to the <b>main graph</b> (${_fmtMs(graphMs)}). `
            + `The <b>wall</b> time (${_fmtMs(wallMs)})`
            + (netMs > 0
                ? ` also includes <b>${_fmtMs(netMs)}</b> of network + proxy/serialization overhead (<b>net</b>) that isn\u2019t charged to any single step.`
                : ` matches the summed step times.`)
            + '</div>';
    }

    html += '</div>';
    return html;
}

function _traceNodeType(node) {
    if (['memory_summarizer', 'fused_router', 'memory_answer_generator', 'sql_generator', 'fused_eval_analytics'].includes(node)) return 'llm';
    if (['catalog_lookup', 'execute_query', 'save_to_memory'].includes(node)) return 'db';
    return 'logic';
}

const _POST_QUERY_SPECS = {
    insights: {
        label: 'Insights calculation',
        kind: 'LLM analytics',
        idle: 'Starts after the table renders when AI Analytics is enabled.',
    },
    chart: {
        label: 'Chart calculation',
        kind: 'LLM chart spec + render',
        idle: 'Starts when the Chart view asks the server to build a chart.',
    },
};

function _resetPostQueryTrace() {
    _postQueryTrace = {};
}

function _updatePostQueryTrace(kind, update = {}) {
    if (!_POST_QUERY_SPECS[kind]) return;
    const now = performance.now();
    const prev = _postQueryTrace[kind] || {
        kind,
        status: 'running',
        startedAt: now,
        updatedAt: now,
        details: [],
        metrics: {},
    };
    const next = {
        ...prev,
        ...update,
        updatedAt: now,
        metrics: { ...(prev.metrics || {}), ...(update.metrics || {}) },
    };

    if (update.status === 'running' && !prev.startedAt) next.startedAt = now;
    if (update.detail) {
        next.details = [...(prev.details || []), update.detail].slice(-4);
    } else if (update.details) {
        next.details = update.details.slice(-4);
    }
    if ((update.status === 'done' || update.status === 'error') && !next.endedAt) {
        next.endedAt = now;
    }
    if (next.startedAt && next.endedAt && !Number.isFinite(next.elapsedMs)) {
        next.elapsedMs = Math.max(0, Math.round(next.endedAt - next.startedAt));
    }

    _postQueryTrace[kind] = next;
    if (_allTraceEvents.length) _renderTraceEvents();
}
window._devPostQueryUpdate = _updatePostQueryTrace;
window._devPostQueryReset = _resetPostQueryTrace;

function _postQueryStatusLabel(status) {
    return {
        idle: 'not started',
        running: 'running',
        done: 'done',
        error: 'error',
        skipped: 'skipped',
    }[status || 'idle'] || status;
}

function _postQueryElapsed(item) {
    if (!item) return null;
    if (Number.isFinite(item.elapsedMs)) return item.elapsedMs;
    if (item.startedAt && item.status === 'running') {
        return Math.max(0, Math.round(performance.now() - item.startedAt));
    }
    return null;
}

function _buildPostQueryWorkHtml() {
    let html = '<div class="post-query-work">';
    html += '<div class="post-query-head">'
        + '<div><strong>Post-query work</strong><span>Insights and charts run after the main SQL answer, so they are tracked separately from the LangGraph trace above.</span></div>'
        + '</div>';
    html += '<div class="post-query-grid">';

    Object.entries(_POST_QUERY_SPECS).forEach(([kind, spec]) => {
        const item = _postQueryTrace[kind] || { status: 'idle', details: [spec.idle], metrics: {} };
        const status = item.status || 'idle';
        const elapsed = _postQueryElapsed(item);
        const metrics = item.metrics || {};
        const detail = item.details?.length ? item.details[item.details.length - 1] : spec.idle;

        html += `<section class="post-query-card post-query-${escapeHtml(status)}">`;
        html += '<div class="post-query-card-top">';
        html += `<div><div class="post-query-title">${escapeHtml(spec.label)}</div><div class="post-query-kind">${escapeHtml(spec.kind)}</div></div>`;
        html += `<span class="post-query-status">${escapeHtml(_postQueryStatusLabel(status))}</span>`;
        html += '</div>';
        html += '<div class="post-query-chips">';
        if (elapsed !== null) html += `<span>${_fmtMs(elapsed)}</span>`;
        if (Number.isFinite(metrics.ttft_ms)) html += `<span>TTFT ${_fmtMs(metrics.ttft_ms)}</span>`;
        if (Number.isFinite(metrics.llm_latency_ms)) html += `<span>LLM ${_fmtMs(metrics.llm_latency_ms)}</span>`;
        if (Number.isFinite(metrics.server_ms)) html += `<span>server ${_fmtMs(metrics.server_ms)}</span>`;
        if (Number.isFinite(metrics.render_ms)) html += `<span>render ${_fmtMs(metrics.render_ms)}</span>`;
        if (Number.isFinite(metrics.input_tokens)) html += `<span>in ${_formatTokens(metrics.input_tokens)}</span>`;
        if (Number.isFinite(metrics.output_tokens)) html += `<span>out ${_formatTokens(metrics.output_tokens)}</span>`;
        if (metrics.cache) html += `<span>cache ${escapeHtml(metrics.cache)}</span>`;
        if (metrics.chart_type) html += `<span>${escapeHtml(metrics.chart_type)}</span>`;
        if (elapsed === null && !Object.keys(metrics).length) html += '<span>waiting</span>';
        html += '</div>';
        html += `<div class="post-query-detail">${escapeHtml(detail || '')}</div>`;
        html += '</section>';
    });

    html += '</div></div>';
    return html;
}

/**
 * Render the trace event timeline into #trace-panel,
 * respecting the current filter + search query.
 */
function _renderTraceEvents() {
    const panel = document.getElementById('trace-panel');
    if (!panel || !_allTraceEvents.length) return;

    const events  = _allTraceEvents;
    const metrics = _traceMetrics;

    // ── Filter ────────────────────────────────────────────────────────────
    let filtered = events;
    if (_activeTraceFilter !== 'all') {
        filtered = filtered.filter(ev => _traceEventLevel(ev) === _activeTraceFilter);
    }
    const sq = _traceSearchQ.toLowerCase();
    if (sq) {
        filtered = filtered.filter(ev =>
            (ev.node   || '').toLowerCase().includes(sq) ||
            (ev.detail || '').toLowerCase().includes(sq) ||
            (ev.prompt || '').toLowerCase().includes(sq) ||
            (ev.sql    || '').toLowerCase().includes(sq)
        );
    }

    const graphMs  = events.reduce((s, e) => s + (e.elapsed_ms || 0), 0);
    const totalMs  = graphMs;  // alias kept for bar scaling below
    const maxMs    = Math.max(...events.map(e => e.elapsed_ms || 0), 1);
    const llmMs    = metrics.llm_latency_ms || 0;
    const dbMs     = metrics.execution_time_ms;   // actual DB execution time
    const retries  = metrics.retry_count    || 0;
    const wallMs   = lastQueryDurationMs;          // client wall time
    const netMs    = (Number.isFinite(wallMs) && wallMs > 0 && graphMs > 0)
        ? Math.max(0, wallMs - graphMs) : null;

    // ── Summary line ───────────────────────────────────────────────────────
    // The persistent run header above already shows route + wall/graph/LLM/DB/
    // net, so here we only render what's unique to the log (node count, retries)
    // plus the stacked breakdown bar below — no duplicated chips.
    let html = '<div class="trace-summary">';
    html += `<span class="trace-summary-chip" title="${_METRIC_TIPS.nodes}">nodes: <strong>${events.length}</strong></span>`;
    if (retries > 0) html += `<span class="trace-summary-chip" title="${_METRIC_TIPS.retries}">retries: <strong>${retries}</strong></span>`;
    html += '</div>';

    // Stacked breakdown bar (where the time went).
    html += _buildTimingBar(wallMs, graphMs, llmMs, dbMs);

    if (_traceViewMode === 'flow') {
        html += _buildTraceFlowHtml(events, metrics);
        html += _buildPostQueryWorkHtml();
        panel.innerHTML = html;
        return;
    }

    if (_traceLegendOpen) html += _buildTraceLegendHtml();

    if (filtered.length === 0) {
        html += '<p class="trace-empty">No events match the current filter.</p>';
        html += _buildPostQueryWorkHtml();
        panel.innerHTML = html;
        return;
    }

    // Identify the single slowest step so it can be flagged in the timeline.
    const slowestIdx = (events.length > 1 && maxMs > 0)
        ? events.reduce((best, e, i, arr) =>
            (e.elapsed_ms || 0) > (arr[best].elapsed_ms || 0) ? i : best, 0)
        : -1;

    // Count how many times each node ran so repeated steps (e.g. retries) can
    // be marked with an occurrence ordinal (#1, #2, …).
    const _nodeCounts = {};
    events.forEach(e => { _nodeCounts[e.node] = (_nodeCounts[e.node] || 0) + 1; });
    const _repeatOrdinal = {};
    const _seenSoFar = {};
    events.forEach((e, i) => {
        _seenSoFar[e.node] = (_seenSoFar[e.node] || 0) + 1;
        if (_nodeCounts[e.node] > 1) _repeatOrdinal[i] = _seenSoFar[e.node];
    });

    // ── Node timeline ─────────────────────────────────────────────────────
    filtered.forEach((ev, idx) => {
        // Use the original index for copy operations
        const origIdx = events.indexOf(ev);
        const ms      = ev.elapsed_ms || 0;
        const barPct  = Math.max(2, Math.round((ms / maxMs) * 100));
        const icon    = escapeHtml(ev.icon  || '●');
        const ntype   = escapeHtml(ev.type  || 'logic');
        const name    = escapeHtml(_nodeLabel(ev.node));
        const detail  = ev.detail ? escapeHtml(ev.detail) : '';
        const status  = ev.status || '';
        const lv      = _traceEventLevel(ev);
        const isSlowest = origIdx === slowestIdx;

        // Tooltip keeps the raw node name accessible alongside its description.
        const nodeTip = escapeHtml(`${ev.node || '?'} — ${_NODE_INFO[ev.node] || 'Pipeline step.'}`);
        html += `<div class="trace-event${isSlowest ? ' trace-event-slowest' : ''}" data-idx="${idx}" onclick="_toggleTraceEvent(this)">`;
        html += `  <span class="trace-lv-dot trace-lv-${lv}" title="severity: ${lv}"></span>`;
        html += `  <span class="trace-event-icon">${icon}</span>`;
        html += `  <span class="trace-event-name" title="${nodeTip}">${name}</span>`;
        const repeatOrd = _repeatOrdinal[origIdx];
        html += `  <div class="trace-event-bar-wrap">`;
        if (isSlowest)
            html += `    <span class="trace-slowest-badge" title="Slowest step in this run">slowest</span>`;
        if (repeatOrd)
            html += `    <span class="trace-event-count" title="This step ran ${_nodeCounts[ev.node]}\u00d7 in this run \u2014 occurrence ${repeatOrd}">#${repeatOrd}</span>`;
        html += `    <div class="trace-event-bar" data-ntype="${ntype}" style="width:${barPct}%"></div>`;
        if (detail) {
            const detailClass = status
                ? `trace-event-detail trace-event-status" data-status="${escapeHtml(status)}`
                : 'trace-event-detail';
            html += `    <span class="${detailClass}">${detail}</span>`;
        }
        html += `  </div>`;
        html += `  <span class="trace-event-ms${_slowCls(ms)}" title="${_TIMING_TIPS.nodeMs}">${_fmtMs(ms)}</span>`;

        // Expandable extra detail (SQL preview, full detail, prompt)
        const expandParts = [];
        if (ev.sql)             expandParts.push(`SQL: ${ev.sql}`);
        if (ev.route)           expandParts.push(`route: ${ev.route}`);
        if (ev.feedback_type)   expandParts.push(`feedback: ${ev.feedback_type}`);
        if (ev.answers_intent !== undefined) expandParts.push(`answers_intent: ${ev.answers_intent}`);

        const hasExpand = expandParts.length > 0 || ev.prompt;
        if (hasExpand) {
            html += `  <div class="trace-event-expanded">`;
            if (expandParts.length) {
                html += escapeHtml(expandParts.join('\n'));
            }
            if (ev.prompt) {
                if (expandParts.length) html += '\n';
                html += `<div class="trace-prompt-label">\uD83D\uDCC4 Prompt sent to LLM`
                    + ` <button class="trace-prompt-copy" onclick="event.stopPropagation();_copyTracePrompt(${origIdx})" title="Copy prompt">&#10697; Copy</button></div>`;
                html += `<pre class="trace-prompt-pre">${escapeHtml(ev.prompt)}</pre>`;
            }
            html += `  </div>`;
        }

        html += `</div>`;
    });

    // ── Synthetic net+proxy overhead row ──────────────────────────────────
    // Only show if overhead is non-trivial (>50 ms) to avoid noise.
    if (netMs !== null && netMs > 50) {
        const barPct = Math.max(2, Math.round((netMs / maxMs) * 100));
        html += `<div class="trace-event trace-event-synthetic" title="Client wall time minus graph total: Flask proxy + FastAPI routing + network round-trip">`;
        html += `  <span class="trace-lv-dot trace-lv-info" title="overhead"></span>`;
        html += `  <span class="trace-event-icon">\uD83C\uDF10</span>`;
        html += `  <span class="trace-event-name">flask + network</span>`;
        html += `  <div class="trace-event-bar-wrap">`;
        html += `    <div class="trace-event-bar" data-ntype="overhead" style="width:${barPct}%"></div>`;
        html += `    <span class="trace-event-detail">proxy + routing + round-trip overhead</span>`;
        html += `  </div>`;
        html += `  <span class="trace-event-ms">${_fmtMs(netMs)}</span>`;
        html += `</div>`;
    }

    html += _buildPostQueryWorkHtml();
    panel.innerHTML = html;
}

/**
 * Copy the prompt text from a specific trace event to the clipboard.
 */
function _copyTracePrompt(origIdx) {
    const ev = _allTraceEvents[origIdx];
    if (!ev || !ev.prompt) return;
    navigator.clipboard.writeText(ev.prompt).then(() => {
        showToast('Prompt copied', 'info');
    }).catch(() => showToast('Copy failed', 'error'));
}
window._copyTracePrompt = _copyTracePrompt;

// ── Developer Panel: Run Header ──────────────────────────────────────────────

/**
 * Populate the #dp-run-header block with question + status + meta chips
 * drawn from the full query API response.
 */
function _updateDevRunHeader(data) {
    const header = document.getElementById('dp-run-header');
    const qEl    = document.getElementById('dp-run-question');
    const metaEl = document.getElementById('dp-run-meta');
    if (!header || !qEl || !metaEl) return;

    qEl.textContent = data.question || '—';

    const hasError    = !!(data.error);
    const statusClass = hasError ? 'dp-status-error' : 'dp-status-ok';
    const statusText  = hasError ? 'error' : 'success';

    const m        = data.metrics || {};
    const route    = m.route || '—';
    const graphMs  = (_allTraceEvents || []).reduce((s, e) => s + (e.elapsed_ms || 0), 0);
    const llmMs    = m.llm_latency_ms;
    const dbMs     = m.execution_time_ms;
    const inTok    = m.input_tokens;
    const outTok   = m.output_tokens;
    const rows     = data.results ? (data.results.data || data.results.rows || []).length : null;
    const wallMs   = lastQueryDurationMs;
    const netMs    = (Number.isFinite(wallMs) && wallMs > 0 && graphMs > 0)
        ? Math.max(0, wallMs - graphMs) : null;

    const chips = [];
    chips.push(`<span class="dp-chip dp-chip-status ${statusClass}" title="${_METRIC_TIPS.status}">${statusText}</span>`);
    chips.push(`<span class="dp-chip${_routeChipCls(route)}" title="${escapeHtml(_routeTip(route))}">route: <strong>${escapeHtml(String(route))}</strong></span>`);
    if (Number.isFinite(lastTotalDurationMs) && lastTotalDurationMs > 0)
        chips.push(`<span class="dp-chip dp-chip-total${_slowCls(lastTotalDurationMs)}" title="${_TIMING_TIPS.total}">total: <strong>${_fmtMs(lastTotalDurationMs)}</strong></span>`);
    if (Number.isFinite(wallMs) && wallMs > 0)
        chips.push(`<span class="dp-chip dp-chip-wall${_slowCls(wallMs)}" title="${_TIMING_TIPS.wall}">wall: <strong>${_fmtMs(wallMs)}</strong></span>`);
    if (Number.isFinite(graphMs) && graphMs > 0)
        chips.push(`<span class="dp-chip${_slowCls(graphMs)}" title="${_TIMING_TIPS.graph}">main graph: <strong>${_fmtMs(graphMs)}</strong></span>`);
    if (Number.isFinite(llmMs))
        chips.push(`<span class="dp-chip${_slowCls(llmMs)}" title="${_TIMING_TIPS.llm}">LLM: <strong>${_fmtMs(llmMs)}</strong></span>`);
    if (Number.isFinite(dbMs) && dbMs > 0)
        chips.push(`<span class="dp-chip dp-chip-db${_slowCls(dbMs)}" title="${_TIMING_TIPS.db}">DB: <strong>${_fmtMs(dbMs)}</strong></span>`);
    if (netMs !== null && netMs > 50)
        chips.push(`<span class="dp-chip dp-chip-net${_slowCls(netMs)}" title="${_TIMING_TIPS.net}">net: <strong>${_fmtMs(netMs)}</strong></span>`);
    if (inTok)  chips.push(`<span class="dp-chip" title="${_METRIC_TIPS.in}">in: <strong>${_formatTokens(inTok)}</strong></span>`);
    if (outTok) chips.push(`<span class="dp-chip" title="${_METRIC_TIPS.out}">out: <strong>${_formatTokens(outTok)}</strong></span>`);
    if (rows !== null) chips.push(`<span class="dp-chip" title="${_METRIC_TIPS.rows}">rows: <strong>${rows}</strong></span>`);

    metaEl.innerHTML = chips.join('');
    header.hidden = false;
}

// ── Developer Panel: SQL Stats Bar ───────────────────────────────────────────

/**
 * Populate the #dp-sql-stats bar above the CodeMirror editor.
 */
function _updateSqlStats(data) {
    const bar = document.getElementById('dp-sql-stats');
    if (!bar) return;

    const m     = data.metrics || {};
    const llmMs = m.llm_latency_ms;
    const dbMs  = m.execution_time_ms;  // actual DB query execution time
    const rows  = data.results
        ? (data.results.data || data.results.rows || []).length
        : null;

    const parts = [];
    if (rows !== null)
        parts.push(`<span class="dp-stat-chip"><strong>${rows}</strong> row${rows !== 1 ? 's' : ''}</span>`);
    if (Number.isFinite(dbMs) && dbMs > 0)
        parts.push(`<span class="dp-stat-chip" title="${_TIMING_TIPS.db}">DB <strong>${_fmtMs(dbMs)}</strong></span>`);
    if (Number.isFinite(llmMs))
        parts.push(`<span class="dp-stat-chip" title="${_TIMING_TIPS.llm}">LLM <strong>${_fmtMs(llmMs)}</strong></span>`);
    if (m.retry_count > 0)
        parts.push(`<span class="dp-stat-chip dp-stat-warn">retries <strong>${m.retry_count}</strong></span>`);

    if (parts.length) {
        bar.innerHTML = parts.join('');
        bar.hidden = false;
    } else {
        bar.hidden = true;
    }
}

// ── Developer Panel: shared "Run Details" refresh ────────────────────────────

/**
 * Refresh the developer "Run Details" panel (SQL / Prompt / Trace tabs + run
 * header + SQL stats) from a query response. Shared so both Ask mode
 * (displayResults) and Chat mode (per assistant turn) keep the panel in sync
 * with the most recent run.
 *
 * @param {Object} data   - The /api/ask response payload.
 * @param {number} wallMs - Client-measured wall-clock duration for this run.
 */
function updateRunDetails(data, wallMs) {
    if (!data) return;

    // Reflect the latest run for feature modules + the trace prompt-copy button.
    currentQueryId = data.query_id || null;
    window.currentQueryId = currentQueryId;
    if (data.question) {
        currentQuestion = data.question;
        window.currentQuestion = currentQuestion;
    }

    // Chat measures its own wall clock; use it for the wall + total chips
    // (there's no separate post-paint measurement step as in Ask mode).
    if (Number.isFinite(wallMs) && wallMs > 0) {
        lastQueryDurationMs = wallMs;
        lastTotalDurationMs = wallMs;
    }

    // SQL tab
    currentSql = data.sql || '';
    initCodeMirror(data.sql || '-- No SQL generated');

    // Query Prompt tab
    if (data.prompt) {
        currentPrompt = data.prompt;
        displayStructuredPrompt(data.prompt);
    }

    // Execution trace (also feeds "main graph" timing into the run header).
    if (data.trace && data.trace.length) {
        renderTrace(data.trace, data.metrics);
    } else {
        renderTrace([], data.metrics);
        const badge = document.getElementById('dev-panel-badge');
        if (badge) badge.hidden = true;
    }

    // Run header + SQL stats bar
    _lastResultData = data;
    _updateDevRunHeader(data);
    _updateSqlStats(data);
}
window.updateRunDetails = updateRunDetails;

// ── Timing chip tooltip explanations ──────────────────────────────────────────
// Used as HTML title= attributes on all timing chips so developers can
// hover any number to understand exactly how it was computed.
const _TIMING_TIPS = {
    total: 'total — Full end-to-end wait the user experiences: from clicking Ask '
         + 'until the results table is painted in the browser. Equals wall (API '
         + 'response) plus client-side JSON parsing and table/chart rendering.',
    wall:  'wall — Client wall time: measured in the browser from the moment '
         + '"Ask" was clicked to the last byte of the API response. '
         + 'Covers: network round-trip + Flask proxy + FastAPI routing + full graph pipeline.',
    graph: 'main graph — The /api/ask LangGraph pipeline time: sum of elapsed_ms '
         + 'across every node in the main query trace. This does not include '
         + 'post-query Insights or Chart work.',
    llm:   'LLM — Cumulative LLM latency: total time waiting for the language '
         + 'model (Azure OpenAI) to respond across all LLM calls in this query. '
         + 'Comes directly from the llm_latency_ms metric.',
    db:    'DB — Database execution time: total time spent running SQL against '
         + 'the data warehouse, summed across every execution in this request '
         + '(including retries). Comes from execution_time_ms in the LangGraph state.',
    net:   'net — Flask + network overhead: wall time minus main graph time. '
         + 'Covers HTTP request/response transit, Flask proxy routing, '
         + 'FastAPI middleware, and serialisation overhead. '
         + 'Formula: net = wall − main graph.',
    nodeMs: 'Wall time spent inside this pipeline step, measured server-side '
         + 'around the node function — it includes any LLM, database, or MCP '
         + 'call that step makes.',
};

// ── Non-timing chip explanations ──────────────────────────────────────────────
// The remaining chip names (route, nodes, retries, in/out tokens, rows, status)
// also get a hover tooltip so every label in the log is self-documenting.
const _METRIC_TIPS = {
    route:   'route — How the router classified your question; this decides which '
           + 'pipeline path runs.',
    nodes:   'nodes — Number of pipeline steps (LangGraph nodes) that executed for '
           + 'this query.',
    retries: 'retries — Times SQL generation was retried after a validation or '
           + 'execution failure.',
    in:      'in — Input tokens sent to the LLM across all calls in this query.',
    out:     'out — Output tokens generated by the LLM across all calls in this query.',
    rows:    'rows — Number of rows returned by the executed SQL.',
    status:  'status — Whether the run finished successfully or ended in an error.',
};

// Meaning of each router classification, appended to the route chip tooltip.
const _ROUTE_INFO = {
    needs_query:  'generates and runs new SQL against the warehouse.',
    from_memory:  'answers from earlier results in the conversation — no new SQL.',
    greeting:     'greeting / small talk — no query is run.',
    out_of_scope: 'the question is outside the scope of the connected data.',
    unsafe:       'blocked by the safety / governance check.',
};
function _routeTip(route) {
    const spec = _ROUTE_INFO[route];
    return spec ? `${_METRIC_TIPS.route}  ·  "${route}" ${spec}` : _METRIC_TIPS.route;
}

// Colour the route chip by classification so blocked / out-of-scope answers
// stand out, while normal query/memory/greeting routes stay calm.
function _routeChipCls(route) {
    switch (route) {
        case 'unsafe':       return ' dp-route-unsafe';
        case 'out_of_scope': return ' dp-route-warn';
        case 'greeting':
        case 'from_memory':  return ' dp-route-info';
        default:             return '';  // needs_query etc. — neutral default
    }
}

// Any timing value slower than this (ms) is visually flagged so the bottleneck
// in a query is immediately obvious.
const _SLOW_MS = 3000;
function _slowCls(ms) {
    return (Number.isFinite(ms) && ms > _SLOW_MS) ? ' is-slow' : '';
}

// Build a single stacked bar that partitions the total query time into its
// constituents — LLM, DB, the rest of the pipeline ("pipeline"), and network +
// proxy overhead ("net") — so where the time went is obvious at a glance.
// wall = main graph + net ; main graph = llm + db + pipeline(other).
function _buildTimingBar(wallMs, graphMs, llmMs, dbMs) {
    const hasWall = Number.isFinite(wallMs) && wallMs > 0;
    const total   = hasWall ? wallMs : graphMs;
    if (!Number.isFinite(total) || total <= 0) return '';

    const llm   = Math.max(0, llmMs || 0);
    const db    = Math.max(0, dbMs  || 0);
    const other = Math.max(0, (graphMs || 0) - llm - db);
    const net   = hasWall ? Math.max(0, wallMs - (graphMs || 0)) : 0;

    const segs = [
        { key: 'llm',   ms: llm,   label: 'LLM' },
        { key: 'db',    ms: db,    label: 'DB' },
        { key: 'other', ms: other, label: 'pipeline' },
        { key: 'net',   ms: net,   label: 'net' },
    ].filter(s => s.ms > 0);
    if (!segs.length) return '';

    const basis = hasWall ? 'wall' : 'main graph';
    const pct   = (ms) => (ms / total) * 100;

    let bar = '<div class="timing-bar" role="img" aria-label="Query time breakdown">';
    segs.forEach(s => {
        const p = pct(s.ms);
        bar += `<span class="timing-bar-seg timing-seg-${s.key}" style="width:${p.toFixed(1)}%"`
            + ` title="${s.label}: ${_fmtMs(s.ms)} (${Math.round(p)}% of ${basis})"></span>`;
    });
    bar += '</div>';

    let legend = '<div class="timing-bar-legend">';
    segs.forEach(s => {
        legend += `<span class="timing-bar-key"><span class="timing-bar-dot timing-seg-${s.key}"></span>`
            + `${s.label} ${Math.round(pct(s.ms))}%</span>`;
    });
    legend += '</div>';

    return `<div class="timing-bar-wrap">${bar}${legend}</div>`;
}

// ── Per-node explanations ─────────────────────────────────────────────────────
// Hovering a node name in the log shows what that pipeline step does, so the
// log is self-documenting. Keep in sync with the LangGraph nodes in graph.py.
const _NODE_INFO = {
    memory_shrink_check:     'Checks whether the conversation history exceeds the token budget and needs summarising.',
    memory_summarizer:       'LLM call that compresses older conversation turns into a short summary to stay within the token budget.',
    fused_router:            'LLM router that classifies the question (needs_query / from_memory / greeting / out_of_scope / unsafe) and picks the path.',
    memory_answer_generator: 'Answers directly from conversation memory when no new SQL is required.',
    catalog_lookup:          'Loads the metadata catalog (tables, columns, relationships) from the MCP server or the metadata DB. Detail shows the source, cache HIT/MISS and load time.',
    prompt_builder:          'Assembles the system prompt and the structured prompt shown in the Prompt tab.',
    sql_generator:           'LLM call that writes the SQL for the question (and repairs it on retries).',
    sqlglot_validate:        'Parses the SQL with sqlglot and checks table names before anything runs.',
    dlp_check:               'Data-loss-prevention / governance check that can block queries touching governed data.',
    execute_query:           'Runs the read-only SQL against the data warehouse. Detail shows rows × columns; ms is the DB execution time.',
    trivial_result_check:    'Decides whether the result is trivial enough to skip the analytics/eval LLM call.',
    fused_eval_analytics:    'LLM call that evaluates the result against the question and writes the answer, insights and follow-ups.',
    feedback_classifier:     'Classifies failures and decides whether to retry SQL generation.',
    response_formatter:      'Pure-Python step that assembles the final API response object.',
    save_to_memory:          'Persists the SQL, token usage and execution result to the conversation history.',
    observability_log:       'Emits the structured QUERY_EVENT log line at the end of every run.',
};

// Friendly, human-readable label for each pipeline node. The raw node name is
// still shown on hover (and is searchable), but the timeline reads in plain
// English. Unknown nodes fall back to a prettified version of the raw name.
const _NODE_LABELS = {
    memory_shrink_check:     'Memory check',
    memory_summarizer:       'Memory summarize',
    fused_router:            'Router',
    memory_answer_generator: 'Answer from memory',
    catalog_lookup:          'Catalog lookup',
    prompt_builder:          'Prompt build',
    sql_generator:           'SQL generation',
    sqlglot_validate:        'SQL validate',
    dlp_check:               'Governance check',
    execute_query:           'Run SQL',
    trivial_result_check:    'Trivial check',
    fused_eval_analytics:    'Analyze & insights',
    feedback_classifier:     'Retry classify',
    response_formatter:      'Format response',
    save_to_memory:          'Save to memory',
    observability_log:       'Log event',
};
function _nodeLabel(node) {
    if (_NODE_LABELS[node]) return _NODE_LABELS[node];
    return (node || '?').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _fmtMs(ms) {
    if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
    if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
    return Math.max(1, Math.round(ms)) + 'ms';
}

// ── Table row display limit ───────────────────────────────────────────────────

/**
 * Build the footer bar HTML that lets the user control how many rows are shown.
 *
 *   Showing [25] of 487 rows   [Show all]   37%
 *
 * The input accepts any integer; pressing Enter or blurring applies it.
 */
function _buildDisplayLimitBar(totalRows, currentLimit) {
    const showing    = Math.min(currentLimit, totalRows);
    const overLimit  = showing < totalRows;
    const pct        = totalRows > 0 ? Math.round((showing / totalRows) * 100) : 100;
    const showAllBtn = overLimit
        ? `<button class="dlb-show-all" onclick="_showAllRows()">Show all</button>`
        : '';
    const pctBadge = overLimit
        ? `<span class="dlb-pct">${pct}%</span>`
        : '';
    return `<div class="display-limit-bar" id="display-limit-bar">
        <span class="dlb-label">Showing</span>
        <input class="dlb-input" id="dlb-input" type="number" min="1"
               value="${currentLimit}"
               onchange="_changeDisplayLimit(this.value)"
               onkeydown="if(event.key==='Enter')this.blur()"
               title="Rows to display \u2014 press Enter or Tab to apply"
               aria-label="Rows to display">
        <span class="dlb-label">of <strong>${totalRows.toLocaleString('en-US')}</strong> row${totalRows !== 1 ? 's' : ''}</span>
        ${showAllBtn}${pctBadge}
    </div>`;
}

/** Update the footer bar in place without replacing the whole table. */
function _updateDisplayLimitBar(totalRows, currentLimit) {
    const bar = document.getElementById('display-limit-bar');
    if (!bar) return;
    const showing    = Math.min(currentLimit, totalRows);
    const overLimit  = showing < totalRows;
    const pct        = totalRows > 0 ? Math.round((showing / totalRows) * 100) : 100;
    bar.innerHTML = `
        <span class="dlb-label">Showing</span>
        <input class="dlb-input" id="dlb-input" type="number" min="1"
               value="${currentLimit}"
               onchange="_changeDisplayLimit(this.value)"
               onkeydown="if(event.key==='Enter')this.blur()"
               title="Rows to display \u2014 press Enter or Tab to apply"
               aria-label="Rows to display">
        <span class="dlb-label">of <strong>${totalRows.toLocaleString('en-US')}</strong> row${totalRows !== 1 ? 's' : ''}</span>
        ${overLimit ? `<button class="dlb-show-all" onclick="_showAllRows()">Show all</button>` : ''}
        ${overLimit ? `<span class="dlb-pct">${pct}%</span>` : ''}
    `;
}

/** User typed a new limit into the input. */
function _changeDisplayLimit(n) {
    const num = Math.max(1, Math.min(1000000, Math.round(Number(n) || 25)));
    _displayLimit = num;
    reRenderTable();
}
window._changeDisplayLimit = _changeDisplayLimit;

/** One-click button to render all rows. */
function _showAllRows() {
    if (!currentResults) return;
    _displayLimit = (currentResults.data || currentResults.rows || []).length;
    reRenderTable();
}
window._showAllRows = _showAllRows;

function _toggleTraceEvent(el) {
    el.classList.toggle('is-open');
}
window._toggleTraceEvent = _toggleTraceEvent;

// Chart Feature Initialization
async function initializeChartFeature(results) {
    // Dynamically import ChartManager if not already loaded
    if (!ChartManager) {
        const module = await import('./chart-feature/chartManager.js?v=87');
        ChartManager = module.ChartManager;
    }

    // Dispose previous chart manager if exists
    if (chartManager) {
        chartManager.dispose();
    }

    // Create new chart manager
    chartManager = new ChartManager();
    await chartManager.initialize(results);
}

// Insights Feature
function generateInsights(results, question, queryId = null, sql = null) {
    // Initialize insights manager if needed
    if (!insightsManager) {
        insightsManager = new window.InsightsManager();
    }

    // Show insights container
    const insightsContainer = document.getElementById('insights-container');
    if (insightsContainer) {
        insightsContainer.style.display = 'block';
    }

    // Generate insights asynchronously (non-blocking) with query_id + sql for
    // the LangGraph eval node path.
    setTimeout(() => {
        insightsManager.generateInsights(results, question, queryId, sql);
    }, 0);
}

// Describe Feature - Statistical Summary
let describeExpanded = false;

function toggleDescribe() {
    const describeSection = document.getElementById('describe-section');
    const describeBtn = document.getElementById('describe-btn');

    if (!currentResults) return;

    if (describeExpanded) {
        // Hide describe section
        describeSection.style.display = 'none';
        describeBtn.textContent = '📊 Describe';
        describeExpanded = false;
    } else {
        // Generate and show statistics
        const stats = calculateStatistics(currentResults);
        describeSection.innerHTML = formatStatistics(stats);
        describeSection.style.display = 'block';
        describeBtn.textContent = '📊 Hide Description';
        describeExpanded = true;
    }
}

// Calculate statistics similar to pandas df.describe()
function calculateStatistics(results) {
    const rows = results.data || results.rows;
    const columns = results.columns;
    const stats = {};
    const totalRows = rows.length;

    // Helper function to parse currency values
    const parseCurrency = (val) => {
        if (typeof val === 'number') return val;
        if (typeof val === 'string') {
            // Remove currency symbols ($, €, £, ¥, etc.) and commas
            const cleaned = val.replace(/[$€£¥,]/g, '').trim();
            return parseFloat(cleaned);
        }
        return NaN;
    };

    columns.forEach((column, colIndex) => {
        // Skip columns that start with or end with "key" (case insensitive)
        const columnLower = column.toLowerCase();
        if (columnLower.startsWith('key') || columnLower.endsWith('key')) {
            return; // Skip this column
        }

        const values = [];
        const allValues = []; // Include null/undefined for missing value analysis

        // Extract column values
        rows.forEach(row => {
            const value = Array.isArray(row) ? row[colIndex] : row[column];
            allValues.push(value);
            if (value !== null && value !== undefined && value !== '') {
                values.push(value);
            }
        });

        // Count missing values
        const missingCount = allValues.filter(v => v === null || v === undefined || v === '').length;
        const missingPct = (missingCount / totalRows * 100).toFixed(2);

        // Determine if numeric (including currency values)
        const numericValues = values
            .map(v => parseCurrency(v))
            .filter(v => !isNaN(v));
        const isNumeric = numericValues.length > values.length * 0.5;

        if (isNumeric && numericValues.length > 0) {
            // Calculate numeric statistics
            const sorted = numericValues.slice().sort((a, b) => a - b);
            const sum = numericValues.reduce((a, b) => a + b, 0);
            const mean = sum / numericValues.length;
            const median = sorted.length % 2 === 0
                ? (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2
                : sorted[Math.floor(sorted.length / 2)];

            // Calculate standard deviation
            const variance = numericValues.reduce((acc, val) => acc + Math.pow(val - mean, 2), 0) / numericValues.length;
            const std = Math.sqrt(variance);

            // Calculate quartiles
            const q1 = sorted[Math.floor(sorted.length * 0.25)];
            const q3 = sorted[Math.floor(sorted.length * 0.75)];

            // Calculate IQR and outliers
            const iqr = q3 - q1;
            const lowerBound = q1 - 1.5 * iqr;
            const upperBound = q3 + 1.5 * iqr;
            const outliers = numericValues.filter(v => v < lowerBound || v > upperBound);

            stats[column] = {
                type: 'numeric',
                count: numericValues.length,
                mean: mean,
                std: std,
                min: sorted[0],
                q25: q1,
                median: median,
                q75: q3,
                max: sorted[sorted.length - 1],
                iqr: iqr,
                lowerBound: lowerBound,
                upperBound: upperBound,
                outliers: outliers,
                sortedValues: sorted,
                missingCount: missingCount,
                missingPct: missingPct
            };
        } else {
            // Calculate categorical statistics
            const uniqueValues = new Set(values);
            const valueCounts = {};
            values.forEach(v => {
                valueCounts[v] = (valueCounts[v] || 0) + 1;
            });
            const topValue = Object.entries(valueCounts).sort((a, b) => b[1] - a[1])[0];

            stats[column] = {
                type: 'categorical',
                count: values.length,
                unique: uniqueValues.size,
                top: topValue ? topValue[0] : null,
                freq: topValue ? topValue[1] : null,
                missingCount: missingCount,
                missingPct: missingPct
            };
        }
    });

    return stats;
}

// Format statistics as HTML
function formatStatistics(stats) {
    let html = '<h3 style="margin-bottom: 15px;">📊 Statistical Analysis</h3>';

    // Tab Navigation
    html += '<div class="stats-tabs">';
    html += '<button class="stats-tab active" onclick="switchStatsTab(\'summary\')">📊 Summary</button>';
    html += '<button class="stats-tab" onclick="switchStatsTab(\'outliers\')">🔍 Outliers</button>';
    html += '<button class="stats-tab" onclick="switchStatsTab(\'missing\')">❓ Missing Values</button>';
    html += '<button class="stats-tab" onclick="switchStatsTab(\'correlation\')">🔗 Correlation Matrix</button>';
    html += '</div>';

    // Tab Content Container
    html += '<div class="stats-tab-content-container">';

    // Summary Tab (default visible)
    html += '<div id="stats-tab-summary" class="stats-tab-content active">';
    html += '<div class="stats-container">';
    Object.entries(stats).forEach(([column, stat]) => {
        html += '<div class="stat-column">';
        html += `<h4>${escapeHtml(column)}</h4>`;

        if (stat.type === 'numeric') {
            html += '<table class="stats-table">';
            html += `<tr><td>Count</td><td>${stat.count}</td></tr>`;
            html += `<tr><td>Mean</td><td>${stat.mean.toFixed(2)}</td></tr>`;
            html += `<tr><td>Std</td><td>${stat.std.toFixed(2)}</td></tr>`;
            html += `<tr><td>Min</td><td>${stat.min.toFixed(2)}</td></tr>`;
            html += `<tr><td>25%</td><td>${stat.q25.toFixed(2)}</td></tr>`;
            html += `<tr><td>50% (Median)</td><td>${stat.median.toFixed(2)}</td></tr>`;
            html += `<tr><td>75%</td><td>${stat.q75.toFixed(2)}</td></tr>`;
            html += `<tr><td>Max</td><td>${stat.max.toFixed(2)}</td></tr>`;
            html += '</table>';
        } else {
            html += '<table class="stats-table">';
            html += `<tr><td>Count</td><td>${stat.count}</td></tr>`;
            html += `<tr><td>Unique</td><td>${stat.unique}</td></tr>`;
            html += `<tr><td>Top</td><td>${escapeHtml(String(stat.top))}</td></tr>`;
            html += `<tr><td>Freq</td><td>${stat.freq}</td></tr>`;
            html += '</table>';
        }
        html += '</div>';
    });
    html += '</div></div>';

    // Outliers Tab
    html += '<div id="stats-tab-outliers" class="stats-tab-content">';
    html += formatOutliersSection(stats);
    html += '</div>';

    // Missing Values Tab
    html += '<div id="stats-tab-missing" class="stats-tab-content">';
    html += formatMissingValuesSection(stats);
    html += '</div>';

    // Correlation Matrix Tab
    html += '<div id="stats-tab-correlation" class="stats-tab-content">';
    html += formatCorrelationSection(stats);
    html += '</div>';

    html += '</div>'; // Close tab content container

    return html;
}

// Format Outliers Analysis Section
function formatOutliersSection(stats) {
    const numericStats = Object.entries(stats).filter(([_, stat]) => stat.type === 'numeric');
    if (numericStats.length === 0) return '<p style="text-align: center; padding: 40px; color: #999;">No numeric columns available for outlier analysis.</p>';

    let html = '';

    numericStats.forEach(([column, stat]) => {
        html += '<div class="outlier-column-section">';
        html += `<h4>${escapeHtml(column)}</h4>`;

        // Quartiles and IQR table
        html += '<table class="stats-table" style="margin-bottom: 15px;">';
        html += `<tr><td>Q1 (25%)</td><td>${stat.q25.toFixed(2)}</td></tr>`;
        html += `<tr><td>Q2 (50% - Median)</td><td>${stat.median.toFixed(2)}</td></tr>`;
        html += `<tr><td>Q3 (75%)</td><td>${stat.q75.toFixed(2)}</td></tr>`;
        html += `<tr><td>IQR (Q3-Q1)</td><td>${stat.iqr.toFixed(2)}</td></tr>`;
        html += `<tr><td>Lower Bound</td><td>${stat.lowerBound.toFixed(2)}</td></tr>`;
        html += `<tr><td>Upper Bound</td><td>${stat.upperBound.toFixed(2)}</td></tr>`;
        html += '</table>';

        // Outliers
        if (stat.outliers.length > 0) {
            html += `<p><strong>Outliers Detected: ${stat.outliers.length}</strong></p>`;
            html += '<div class="outliers-list">';
            stat.outliers.slice(0, 10).forEach(outlier => {
                html += `<span class="outlier-badge">${outlier.toFixed(2)}</span>`;
            });
            if (stat.outliers.length > 10) {
                html += `<span class="outlier-badge">+${stat.outliers.length - 10} more</span>`;
            }
            html += '</div>';
        } else {
            html += '<p style="color: #28a745;">✓ No outliers detected</p>';
        }

        // Simple boxplot visualization
        html += '<div class="boxplot-container">';
        html += renderBoxplot(stat);
        html += '</div>';

        html += '</div>';
    });

    return html;
}

// Render simple boxplot — uses CSS variables for colors
function renderBoxplot(stat) {
    const range = stat.max - stat.min;
    const scale = 100 / range;

    const minPos = 0;
    const q1Pos = (stat.q25 - stat.min) * scale;
    const medianPos = (stat.median - stat.min) * scale;
    const q3Pos = (stat.q75 - stat.min) * scale;
    const maxPos = 100;

    let html = '<div class="boxplot" style="position: relative; height: 60px; margin-top: 10px;">';

    // Whisker line
    html += `<div style="position: absolute; top: 29px; left: ${minPos}%; width: ${maxPos - minPos}%; height: 2px; background: var(--color-border-2);"></div>`;

    // Box
    html += `<div style="position: absolute; top: 15px; left: ${q1Pos}%; width: ${q3Pos - q1Pos}%; height: 30px; background: var(--color-accent); border: 2px solid var(--color-accent-2); border-radius: var(--radius-sm);"></div>`;

    // Median line
    html += `<div style="position: absolute; top: 15px; left: ${medianPos}%; width: 2px; height: 30px; background: var(--color-error);"></div>`;

    // Min/Max markers
    html += `<div style="position: absolute; top: 25px; left: ${minPos}%; width: 2px; height: 10px; background: var(--color-border-2);"></div>`;
    html += `<div style="position: absolute; top: 25px; left: ${maxPos}%; width: 2px; height: 10px; background: var(--color-border-2);"></div>`;

    // Labels
    html += `<div style="position: absolute; top: 45px; left: ${minPos}%; font-size: var(--text-xs); color: var(--color-muted);">${stat.min.toFixed(1)}</div>`;
    html += `<div style="position: absolute; top: 45px; left: ${medianPos}%; font-size: var(--text-xs); color: var(--color-muted); transform: translateX(-50%);">${stat.median.toFixed(1)}</div>`;
    html += `<div style="position: absolute; top: 45px; right: ${100 - maxPos}%; font-size: var(--text-xs); color: var(--color-muted);">${stat.max.toFixed(1)}</div>`;

    html += '</div>';
    return html;
}

// Format Missing Values Analysis Section
function formatMissingValuesSection(stats) {
    let html = '';

    // Filter columns with missing values
    const columnsWithMissing = Object.entries(stats).filter(([_, stat]) => stat.missingCount > 0);

    if (columnsWithMissing.length === 0) {
        html += '<p style="color: #28a745; text-align: center; padding: 20px;">✓ No missing values detected in any column</p>';
    } else {
        html += '<table class="missing-values-table">';
        html += '<thead><tr><th>Column</th><th>Missing Count</th><th>Missing %</th><th>Visual</th></tr></thead>';
        html += '<tbody>';

        columnsWithMissing.forEach(([column, stat]) => {
            const severity = stat.missingPct < 5 ? 'low' : stat.missingPct < 20 ? 'medium' : 'high';
            html += '<tr>';
            html += `<td><strong>${escapeHtml(column)}</strong></td>`;
            html += `<td>${stat.missingCount}</td>`;
            html += `<td>${stat.missingPct}%</td>`;
            html += '<td>';
            html += `<div class="missing-bar-container">`;
            html += `<div class="missing-bar missing-${severity}" style="width: ${stat.missingPct}%"></div>`;
            html += `</div>`;
            html += '</td>';
            html += '</tr>';
        });

        html += '</tbody></table>';
    }

    return html;
}

// Format Correlation Matrix Section
function formatCorrelationSection(stats) {
    const numericColumns = Object.entries(stats).filter(([_, stat]) => stat.type === 'numeric');
    if (numericColumns.length < 2) return '<p style="text-align: center; padding: 40px; color: #999;">Need at least 2 numeric columns for correlation analysis.</p>';

    let html = '';

    // Calculate correlation matrix
    const correlations = calculateCorrelationMatrix(numericColumns);

    // Render heatmap
    html += '<div class="correlation-heatmap">';
    html += '<table class="correlation-table">';

    // Header row
    html += '<thead><tr><th></th>';
    numericColumns.forEach(([column]) => {
        html += `<th class="correlation-header">${escapeHtml(column)}</th>`;
    });
    html += '</tr></thead>';

    // Data rows
    html += '<tbody>';
    numericColumns.forEach(([rowColumn], rowIdx) => {
        html += '<tr>';
        html += `<th class="correlation-row-header">${escapeHtml(rowColumn)}</th>`;
        numericColumns.forEach(([colColumn], colIdx) => {
            const corr = correlations[rowIdx][colIdx];
            const color = getCorrelationColor(corr);
            html += `<td class="correlation-cell" style="background-color: ${color};" title="${corr.toFixed(3)}">`;
            html += corr.toFixed(2);
            html += '</td>';
        });
        html += '</tr>';
    });
    html += '</tbody></table>';
    html += '</div>';

    return html;
}

// Calculate correlation matrix
function calculateCorrelationMatrix(numericColumns) {
    const n = numericColumns.length;
    const correlations = Array(n).fill(0).map(() => Array(n).fill(0));

    for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
            if (i === j) {
                correlations[i][j] = 1.0;
            } else {
                const [, stat1] = numericColumns[i];
                const [, stat2] = numericColumns[j];
                correlations[i][j] = calculateCorrelation(stat1.sortedValues, stat2.sortedValues);
            }
        }
    }

    return correlations;
}

// Calculate Pearson correlation coefficient
function calculateCorrelation(values1, values2) {
    const n = Math.min(values1.length, values2.length);
    if (n === 0) return 0;

    const mean1 = values1.reduce((a, b) => a + b, 0) / values1.length;
    const mean2 = values2.reduce((a, b) => a + b, 0) / values2.length;

    let numerator = 0;
    let sum1 = 0;
    let sum2 = 0;

    for (let i = 0; i < n; i++) {
        const diff1 = values1[i] - mean1;
        const diff2 = values2[i] - mean2;
        numerator += diff1 * diff2;
        sum1 += diff1 * diff1;
        sum2 += diff2 * diff2;
    }

    const denominator = Math.sqrt(sum1 * sum2);
    return denominator === 0 ? 0 : numerator / denominator;
}

// Get color for correlation value — uses oklch matching design tokens
function getCorrelationColor(corr) {
    if (corr >= 0.7)  return `oklch(50% 0.18 145 / ${(0.3 + corr * 0.7).toFixed(2)})`;
    if (corr >= 0.3)  return `oklch(50% 0.18 145 / ${(corr * 0.5).toFixed(2)})`;
    if (corr >= -0.3) return `oklch(80% 0.005 260 / 0.25)`;
    if (corr >= -0.7) return `oklch(52% 0.22 25 / ${(-corr * 0.5).toFixed(2)})`;
    return `oklch(52% 0.22 25 / ${(0.3 + -corr * 0.7).toFixed(2)})`;
}

// Switch between stats tabs
function switchStatsTab(tabName) {
    // Hide all tab contents
    const allTabContents = document.querySelectorAll('.stats-tab-content');
    allTabContents.forEach(content => {
        content.classList.remove('active');
    });

    // Remove active class from all tabs
    const allTabs = document.querySelectorAll('.stats-tab');
    allTabs.forEach(tab => {
        tab.classList.remove('active');
    });

    // Show selected tab content
    const selectedContent = document.getElementById(`stats-tab-${tabName}`);
    if (selectedContent) {
        selectedContent.classList.add('active');
    }

    // Add active class to clicked tab
    event.target.classList.add('active');
}

// ======================================================
// TOAST NOTIFICATION (global)
// ======================================================
function showToast(message, type) {
    const t = document.createElement('div');
    t.className = 'toast toast-' + (type || 'info');
    t.textContent = message;
    document.body.appendChild(t);
    setTimeout(() => t.classList.add('show'), 50);
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 300);
    }, 2800);
}

// ======================================================
// COLUMN CONTEXT MENU
// ======================================================

// Apply a named format to a raw cell value. Returns formatted string or null.
function applyColFormatValue(value, type) {
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    switch (type) {
        case 'currency': return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        case 'percent':  return n.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + '%';
        case 'compact':  return compactNumber(n);
        case 'integer':  return Math.round(n).toLocaleString('en-US');
        case 'dec0':     return n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
        case 'dec1':     return n.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
        case 'dec2':     return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        case 'dec4':     return n.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
        default: return null;
    }
}

function compactNumber(n) {
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

// Show the column context menu at the cursor position.
function showColMenu(event, colIndex) {
    event.preventDefault();
    event.stopPropagation();
    if (!currentResults) return;

    const colName   = currentResults.columns[colIndex];
    const rows      = currentResults.data || currentResults.rows;
    const profile   = profileColumns(currentResults, rows);
    const isNumeric = profile.numericCols.has(colIndex);
    const currentFmt = _colFormats[colIndex];
    const hasDerived = _derivedCols.some(d => d.sourceIndex === colIndex);

    // Pre-compute sum for % of total
    if (isNumeric && _colSums[colIndex] === undefined) {
        let sum = 0;
        rows.forEach(row => {
            const v = Number(Array.isArray(row) ? row[colIndex] : row[colName]);
            if (Number.isFinite(v)) sum += v;
        });
        _colSums[colIndex] = sum || 1;
    }

    // ── Build menu HTML ──────────────────────────────────────────
    const FORMATS = [
        { type: 'dec2',     icon: '1,23',  label: 'Number (2 dec)'   },
        { type: 'currency', icon: '$',     label: 'Currency ($)'     },
        { type: 'percent',  icon: '%',     label: 'Percentage (%)'   },
        { type: 'compact',  icon: '1K',    label: 'Compact (1.2K)'   },
        { type: 'integer',  icon: '123',   label: 'Integer'           },
        { type: 'dec0',     icon: '.0',    label: '0 decimals'        },
        { type: 'dec1',     icon: '.0',    label: '1 decimal'         },
        { type: 'dec4',     icon: '.0000', label: '4 decimals'        },
    ];

    let html = '';

    // Sort
    html += '<div class="col-ctx-header">Sort</div>';
    html += `<div class="col-ctx-item" onclick="sortTableDir(${colIndex},'asc');closeColMenu()"><span class="col-ctx-icon">▲</span>Ascending</div>`;
    html += `<div class="col-ctx-item" onclick="sortTableDir(${colIndex},'desc');closeColMenu()"><span class="col-ctx-icon">▼</span>Descending</div>`;

    // Format (numeric only)
    if (isNumeric) {
        html += '<div class="col-ctx-sep"></div><div class="col-ctx-header">Format</div>';
        FORMATS.forEach(f => {
            const tick = currentFmt && currentFmt.type === f.type ? ' \u2713' : '';
            html += `<div class="col-ctx-item" onclick="setColFormat(${colIndex},'${f.type}','${f.label}','${f.icon}');closeColMenu()">`
                  + `<span class="col-ctx-icon col-ctx-icon-mono">${f.icon}</span>${escapeHtml(f.label)}${tick}</div>`;
        });
        if (currentFmt) {
            html += `<div class="col-ctx-item col-ctx-item-danger" onclick="clearColFormat(${colIndex});closeColMenu()"><span class="col-ctx-icon">×</span>Reset format</div>`;
        }

        // Calculate
        html += '<div class="col-ctx-sep"></div><div class="col-ctx-header">Calculate</div>';
        html += `<div class="col-ctx-item" onclick="addDerivedCol(${colIndex},'pct_total');closeColMenu()"><span class="col-ctx-icon">%</span>Add % of total</div>`;
        html += `<div class="col-ctx-item" onclick="addDerivedCol(${colIndex},'running_total');closeColMenu()"><span class="col-ctx-icon">Σ</span>Add running total</div>`;
        html += `<div class="col-ctx-item" onclick="addDerivedCol(${colIndex},'delta');closeColMenu()"><span class="col-ctx-icon">Δ</span>Add change (Δ)</div>`;
        if (hasDerived) {
            html += `<div class="col-ctx-item col-ctx-item-danger" onclick="removeDerivedCol(${colIndex});closeColMenu()"><span class="col-ctx-icon">×</span>Remove derived column</div>`;
        }
    }

    // Filter / Copy
    html += '<div class="col-ctx-sep"></div><div class="col-ctx-header">Filter · Copy</div>';
    html += `<div class="col-ctx-item" onclick="filterColNonNull(${colIndex});closeColMenu()"><span class="col-ctx-icon">≠∅</span>Filter non-null</div>`;
    html += `<div class="col-ctx-item" onclick="copyColValues(${colIndex});closeColMenu()"><span class="col-ctx-icon">⧉</span>Copy column values</div>`;

    // Analyze
    html += '<div class="col-ctx-sep"></div><div class="col-ctx-header">Analyze</div>';
    html += `<div class="col-ctx-item" onclick="askAboutCol(${colIndex});closeColMenu()"><span class="col-ctx-icon">→</span>Ask about this column</div>`;

    // ── Build / position menu element ─────────────────────────
    let menu = document.getElementById('col-ctx-menu');
    if (!menu) {
        menu = document.createElement('div');
        menu.id = 'col-ctx-menu';
        menu.className = 'col-ctx-menu';
        document.body.appendChild(menu);
    }
    menu.innerHTML = html;
    menu.style.display = 'block';

    // Position near cursor, clamped to viewport.
    const vw = window.innerWidth, vh = window.innerHeight;
    const mw = 220, mh = menu.scrollHeight || 320;
    menu.style.left = (event.clientX + mw > vw ? vw - mw - 8 : event.clientX) + 'px';
    menu.style.top  = (event.clientY + mh > vh ? vh - mh - 8 : event.clientY) + 'px';

    // Close on next outside click or Esc
    const _close = (e) => {
        if (e.type === 'keydown' && e.key !== 'Escape') return;
        closeColMenu();
        document.removeEventListener('click',   _close);
        document.removeEventListener('keydown', _close);
        document.removeEventListener('contextmenu', _close);
    };
    setTimeout(() => {
        document.addEventListener('click',       _close);
        document.addEventListener('keydown',     _close);
        document.addEventListener('contextmenu', _close);
    }, 0);
}

function closeColMenu() {
    const m = document.getElementById('col-ctx-menu');
    if (m) m.style.display = 'none';
}

// ── Format actions ────────────────────────────────────────────────

function setColFormat(colIndex, type, label, icon) {
    _colFormats[colIndex] = { type, label, icon: icon || type };
    reRenderTable();
}

function clearColFormat(colIndex) {
    delete _colFormats[colIndex];
    reRenderTable();
}

// ── Derived column actions ────────────────────────────────────────

function addDerivedCol(sourceIndex, type) {
    _derivedCols = _derivedCols.filter(d => d.sourceIndex !== sourceIndex);
    delete _colSums[sourceIndex]; // recalculate on next render
    const base  = currentResults ? currentResults.columns[sourceIndex] : 'col';
    const names = { pct_total: base + ' %', running_total: base + ' \u03a3', delta: base + ' \u0394' };
    _derivedCols.push({ sourceIndex, type, name: names[type] || base + ' (calc)' });
    reRenderTable();
}

function removeDerivedCol(sourceIndex) {
    _derivedCols = _derivedCols.filter(d => d.sourceIndex !== sourceIndex);
    reRenderTable();
}

// ── Filter / utility actions ──────────────────────────────────────

function filterColNonNull(colIndex) {
    if (!currentResults) return;
    const colName = currentResults.columns[colIndex];
    const rows = currentResults.data || currentResults.rows;
    const filtered = rows.filter(row => {
        const v = Array.isArray(row) ? row[colIndex] : row[colName];
        return v !== null && v !== undefined && v !== '';
    });
    const container = document.getElementById('table-container');
    if (container) container.innerHTML = renderTable(currentResults, filtered.slice(0, _displayLimit));
    _updateDisplayLimitBar(filtered.length, _displayLimit);
}

function copyColValues(colIndex) {
    if (!currentResults) return;
    const colName = currentResults.columns[colIndex];
    const rows    = currentResults.data || currentResults.rows;
    const text    = rows.map(row => {
        const v = Array.isArray(row) ? row[colIndex] : row[colName];
        return (v === null || v === undefined) ? '' : String(v);
    }).join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast(colName + ' copied (' + rows.length + ' values)', 'info');
    });
}

function askAboutCol(colIndex) {
    if (!currentResults) return;
    const colName = currentResults.columns[colIndex];
    const q = currentQuestion
        ? 'Analyze the "' + colName + '" column from: ' + currentQuestion
        : 'Analyze the "' + colName + '" column';
    fillQuestion(q);
}

// ── Sort with explicit direction ──────────────────────────────────

function sortTableDir(colIndex, direction) {
    sortColumn    = colIndex;
    sortDirection = direction;
    reRenderTable();
}

// ── Re-render preserving current sort + filter + formats ─────────

function reRenderTable() {
    if (!currentResults) return;
    let rows = [...(currentResults.data || currentResults.rows)];

    // Apply sort
    if (sortColumn !== null) {
        const col = currentResults.columns[sortColumn];
        rows.sort((a, b) => {
            const vA = Array.isArray(a) ? a[sortColumn] : a[col];
            const vB = Array.isArray(b) ? b[sortColumn] : b[col];
            if (vA === null || vA === undefined) return 1;
            if (vB === null || vB === undefined) return -1;
            const nA = parseFloat(String(vA).replace(/[^0-9.-]/g, ''));
            const nB = parseFloat(String(vB).replace(/[^0-9.-]/g, ''));
            if (!isNaN(nA) && !isNaN(nB)) return sortDirection === 'asc' ? nA - nB : nB - nA;
            return sortDirection === 'asc'
                ? String(vA).toLowerCase().localeCompare(String(vB).toLowerCase())
                : String(vB).toLowerCase().localeCompare(String(vA).toLowerCase());
        });
    }

    // Apply current text filter
    const filterInput = document.getElementById('result-filter');
    const fv = filterInput ? filterInput.value.toLowerCase() : '';
    if (fv) {
        rows = rows.filter(row =>
            (Array.isArray(row) ? row : currentResults.columns.map(c => row[c]))
                .some(cell => cell !== null && cell !== undefined && String(cell).toLowerCase().includes(fv))
        );
    }

    // Apply display limit and update footer bar
    const totalAfterFilter = rows.length;
    const container = document.getElementById('table-container');
    if (container) container.innerHTML = renderTable(currentResults, rows.slice(0, _displayLimit));
    _updateDisplayLimitBar(totalAfterFilter, _displayLimit);
}

// ======================================================
// ROW CONTEXT MENU
// ======================================================

// Build a short human-readable label for a row (used in question templates).
// Uses the first 1–3 non-null columns: "month = Jan, revenue = 100"
function _rowLabel(rowIdx) {
    if (!currentResults || rowIdx >= _currentVisibleRows.length) return 'this row';
    const row  = _currentVisibleRows[rowIdx];
    const cols = currentResults.columns;
    const parts = [];
    for (let i = 0; i < Math.min(3, cols.length) && parts.length < 3; i++) {
        const v = Array.isArray(row) ? row[i] : row[cols[i]];
        if (v !== null && v !== undefined && v !== '') {
            parts.push(cols[i] + ' = ' + String(v));
        }
    }
    return parts.length ? parts.join(', ') : 'this row';
}

// Show the row context menu at the cursor position.
function showRowMenu(event, rowIdx) {
    event.preventDefault();
    event.stopPropagation();
    if (!currentResults || rowIdx >= _currentVisibleRows.length) return;

    // Close any open column menu first.
    closeColMenu();

    // Value of the specific cell that was right-clicked.
    const clickedTd    = event.target.closest('td');
    const cellText     = clickedTd ? clickedTd.textContent.trim() : null;
    const hasCellValue = cellText && cellText !== 'NULL' && cellText.length > 0;

    // Determine the column index of the clicked cell (skip the hidden helper td).
    let clickedColIdx = -1;
    if (clickedTd) {
        // The first child <td> is the hidden data-row-idx helper; offset by 1.
        const siblings = Array.from(clickedTd.parentElement.querySelectorAll('td:not([style*="display:none"])'));
        clickedColIdx  = siblings.indexOf(clickedTd);
    }

    // ── Build menu HTML ──────────────────────────────────────────
    let html = '';

    // Explore
    html += '<div class="col-ctx-header">Explore</div>';
    html += `<div class="col-ctx-item" onclick="askAboutRow(${rowIdx});closeRowMenu()"><span class="col-ctx-icon">→</span>Ask about this row</div>`;
    html += `<div class="col-ctx-item" onclick="explainRow(${rowIdx});closeRowMenu()"><span class="col-ctx-icon">❓</span>Explain this row</div>`;
    html += `<div class="col-ctx-item" onclick="compareRowToAvg(${rowIdx});closeRowMenu()"><span class="col-ctx-icon">~</span>Compare to average</div>`;
    html += `<div class="col-ctx-item" onclick="findSimilarRows(${rowIdx});closeRowMenu()"><span class="col-ctx-icon">≡</span>Find similar rows</div>`;

    // Filter / Copy
    html += '<div class="col-ctx-sep"></div><div class="col-ctx-header">Filter \u00b7 Copy</div>';
    if (hasCellValue && clickedColIdx >= 0 && clickedColIdx < currentResults.columns.length) {
        const safeVal = escapeHtml(cellText);
        html += `<div class="col-ctx-item" onclick="filterByRowCell(${rowIdx},${clickedColIdx});closeRowMenu()"><span class="col-ctx-icon">⋄</span>Filter by <em>&ldquo;${safeVal}&rdquo;</em></div>`;
    }
    html += `<div class="col-ctx-item" onclick="copyRowData(${rowIdx});closeRowMenu()"><span class="col-ctx-icon">⧉</span>Copy row</div>`;

    // ── Position menu ─────────────────────────────────────────────
    let menu = document.getElementById('row-ctx-menu');
    if (!menu) {
        menu = document.createElement('div');
        menu.id        = 'row-ctx-menu';
        menu.className = 'col-ctx-menu';   // reuse same visual style
        document.body.appendChild(menu);
    }
    menu.innerHTML = html;
    menu.style.display = 'block';

    const vw = window.innerWidth, vh = window.innerHeight;
    const mw = 230, mh = menu.scrollHeight || 240;
    menu.style.left = (event.clientX + mw > vw ? vw - mw - 8 : event.clientX) + 'px';
    menu.style.top  = (event.clientY + mh > vh ? vh - mh - 8 : event.clientY) + 'px';

    // Close on outside click, Esc, or next right-click.
    const _close = (e) => {
        if (e.type === 'keydown' && e.key !== 'Escape') return;
        closeRowMenu();
        document.removeEventListener('click',       _close);
        document.removeEventListener('keydown',     _close);
        document.removeEventListener('contextmenu', _close);
    };
    setTimeout(() => {
        document.addEventListener('click',       _close);
        document.addEventListener('keydown',     _close);
        document.addEventListener('contextmenu', _close);
    }, 0);
}

function closeRowMenu() {
    const m = document.getElementById('row-ctx-menu');
    if (m) m.style.display = 'none';
}

// ── Row action implementations ───────────────────────────────────────

function askAboutRow(rowIdx) {
    const label = _rowLabel(rowIdx);
    const q = currentQuestion
        ? `Tell me about the row where ${label}, in the context of: ${currentQuestion}`
        : `Tell me about the row where ${label}`;
    fillQuestion(q);
}

function explainRow(rowIdx) {
    const label = _rowLabel(rowIdx);
    const q = currentQuestion
        ? `Explain why the row (${label}) has these values, in the context of: ${currentQuestion}`
        : `Explain the values in the row where ${label}`;
    fillQuestion(q);
}

function compareRowToAvg(rowIdx) {
    const label = _rowLabel(rowIdx);
    const q = currentQuestion
        ? `How does the row (${label}) compare to the average across all results? Context: ${currentQuestion}`
        : `Compare the row (${label}) to the average across all results`;
    fillQuestion(q);
}

function findSimilarRows(rowIdx) {
    const label = _rowLabel(rowIdx);
    const q = currentQuestion
        ? `Find other rows similar to (${label}) in the data. Context: ${currentQuestion}`
        : `Find rows similar to the row where ${label}`;
    fillQuestion(q);
}

function filterByRowCell(rowIdx, colIdx) {
    if (!currentResults || rowIdx >= _currentVisibleRows.length) return;
    const row    = _currentVisibleRows[rowIdx];
    const col    = currentResults.columns[colIdx];
    const val    = Array.isArray(row) ? row[colIdx] : row[col];
    if (val === null || val === undefined) return;
    const valStr = String(val).toLowerCase();

    // Filter all ORIGINAL rows (not just visible) so the count is relative to full data.
    const allRows = currentResults.data || currentResults.rows;
    const filtered = allRows.filter(r => {
        const v = Array.isArray(r) ? r[colIdx] : r[col];
        return v !== null && v !== undefined && String(v).toLowerCase() === valStr;
    });
    const container = document.getElementById('table-container');
    if (container) container.innerHTML = renderTable(currentResults, filtered);
    const rc = document.getElementById('row-count');
    if (rc) rc.textContent = filtered.length + ' of ' + allRows.length + ' rows \u2014 ' + escapeHtml(col) + ' = ' + escapeHtml(String(val));
}

function copyRowData(rowIdx) {
    if (!currentResults || rowIdx >= _currentVisibleRows.length) return;
    const row  = _currentVisibleRows[rowIdx];
    const cols = currentResults.columns;
    // Tab-separated: header\tvalue pairs, then newline-separated col=val pairs
    const text = cols.map((col, i) => {
        const v = Array.isArray(row) ? row[i] : row[col];
        return col + '\t' + (v === null || v === undefined ? '' : String(v));
    }).join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast('Row copied', 'info');
    });
}

/**
 * Copy the full raw prompt text (from window._lastPromptRawText) to clipboard.
 * Called by the "Copy all" button in the Query Prompt tab header.
 */
function _copyAllPrompt() {
    const text = window._lastPromptRawText || '';
    if (!text) { showToast('No prompt to copy', 'info'); return; }
    navigator.clipboard.writeText(text).then(() => {
        showToast('Prompt copied', 'info');
    }).catch(() => showToast('Copy failed', 'error'));
}
window._copyAllPrompt = _copyAllPrompt;

// Make functions globally accessible for onclick handlers
window.askQuestion = askQuestion;

// Fill the question input from a follow-up chip and auto-submit
window._fillFollowUp = function(question) {
    const input = document.getElementById('question-input');
    if (!input) return;
    input.value = question;
    input.focus();
    // Small delay so the value is set before askQuestion reads it
    setTimeout(() => askQuestion(), 50);
};
window.toggleSql = toggleSql;
window.togglePrompt = togglePrompt;
window.copySql = copySql;
window.copyResults = copyResults;
window.exportToExcel = exportToExcel;
window.loadTables = loadTables;
window.filterTables = filterTables;
window.fillQuestion = fillQuestion;
window.clearHistory = clearHistory;
window.toggleDescribe = toggleDescribe;
window.switchStatsTab = switchStatsTab;
window.showToast         = showToast;
window.showColMenu       = showColMenu;
window.closeColMenu      = closeColMenu;
window.setColFormat      = setColFormat;
window.clearColFormat    = clearColFormat;
window.addDerivedCol     = addDerivedCol;
window.removeDerivedCol  = removeDerivedCol;
window.filterColNonNull  = filterColNonNull;
window.copyColValues     = copyColValues;
window.askAboutCol       = askAboutCol;
window.sortTableDir      = sortTableDir;
window.reRenderTable     = reRenderTable;
window.showRowMenu       = showRowMenu;
window.closeRowMenu      = closeRowMenu;
window.askAboutRow       = askAboutRow;
window.explainRow        = explainRow;
window.compareRowToAvg   = compareRowToAvg;
window.findSimilarRows   = findSimilarRows;
window.filterByRowCell   = filterByRowCell;
window.copyRowData       = copyRowData;

// ======================================================
// THEME SYSTEM
// ======================================================

const ICON_SUN  = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>`;
const ICON_MOON = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>`;

function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'light';
}

/**
 * Apply a theme preference value ('light' | 'dark' | 'system').
 * 'system' resolves to the OS preference at apply-time and reflows on change.
 */
function applyThemeFromPreference(pref) {
    let resolved = pref;
    if (pref === 'system') {
        const mq = window.matchMedia('(prefers-color-scheme: dark)');
        resolved = mq.matches ? 'dark' : 'light';
        // Re-apply on OS change (only one listener is enough; replace any prior).
        try {
            mq.removeEventListener('change', _systemThemeListener);
        } catch (_) { /* older browsers ignore */ }
        mq.addEventListener('change', _systemThemeListener);
    }
    document.documentElement.setAttribute('data-theme', resolved);
    updateThemeIcon(resolved);
    if (currentSql) initCodeMirror(currentSql);
}

function _systemThemeListener(e) {
    // Only reflow when the user's saved pref is still 'system'.
    const stored = localStorage.getItem('theme');
    if (stored === 'system') {
        const next = e.matches ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        updateThemeIcon(next);
        if (currentSql) initCodeMirror(currentSql);
    }
}

function updateThemeIcon(theme) {
    const iconEl = document.getElementById('theme-icon');
    const btn    = document.getElementById('theme-toggle');
    if (!iconEl || !btn) return;
    if (theme === 'dark') {
        iconEl.innerHTML = ICON_SUN;
        btn.setAttribute('aria-label', 'Switch to light mode');
    } else {
        iconEl.innerHTML = ICON_MOON;
        btn.setAttribute('aria-label', 'Switch to dark mode');
    }
}

function toggleTheme() {
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.classList.add('theme-toggling');
    setTimeout(() => {
        const next = getTheme() === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        updateThemeIcon(next);
        if (btn) btn.classList.remove('theme-toggling');
        // Re-init CodeMirror with new theme
        if (currentSql) initCodeMirror(currentSql);
    }, 100);
}

// ======================================================
// CODEMIRROR 6
// ======================================================

function initCodeMirror(sqlContent) {
    const container = document.getElementById('sql-editor');
    if (!container) return;

    // Destroy previous instance
    if (window._cmEditor) {
        try { window._cmEditor.destroy(); } catch (e) { /* ignore */ }
        window._cmEditor = null;
    }

    const cm = window.CodeMirrorService;
    if (!cm || !cm.ready) {
        // Fallback: plain pre block
        container.innerHTML = `<pre class="sql-display" style="margin:0;border:none;border-radius:0;">${escapeHtml(sqlContent || '')}</pre>`;
        return;
    }

    const { EditorView, EditorState, lineNumbers, sql, oneDark } = cm;
    const isDark = getTheme() === 'dark';

    const extensions = [
        lineNumbers(),
        sql(),
        EditorView.lineWrapping,
        EditorView.editable.of(false),
        ...(isDark ? [oneDark] : []),
    ];

    window._cmEditor = new EditorView({
        state: EditorState.create({ doc: sqlContent || '', extensions }),
        parent: container
    });

    // Signal that SQL is available — show badge on the topbar button.
    if (typeof window._devDrawerShowBadge === 'function') {
        window._devDrawerShowBadge();
    }
}

// ======================================================
// CONNECTION PANEL — custom listbox replacing native <select>
// ======================================================
//
// Wires up the pill (#connection-pill) to a panel (#connection-panel) listing
// every active connection (loaded from /api/connections into
// `availableConnections`). Behaviour:
//   • Click pill / Enter / Space / ArrowDown → open panel.
//   • Click outside / Escape → close.
//   • ArrowUp/ArrowDown → navigate items (focusable rows).
//   • Enter / Space / click on row → switch connection.
//   • Search input shown when there are >= 5 connections.
//   • Footer button refreshes the metadata cache for the active connection
//     via POST /api/connections/{src}/refresh-metadata.
const ConnectionPanel = (function () {
    let isOpen = false;
    let searchTerm = '';

    const pillEl = () => document.getElementById('connection-pill');
    const panelEl = () => document.getElementById('connection-panel');

    function open() {
        if (isOpen) return;
        if (!availableConnections || availableConnections.length === 0) return;
        isOpen = true;
        const p = pillEl();
        if (p) p.setAttribute('aria-expanded', 'true');
        render();
        const pan = panelEl();
        if (pan) pan.hidden = false;
        // Focus search input if present, otherwise first item.
        const search = pan && pan.querySelector('.connection-panel-search');
        if (search) {
            search.focus();
        } else {
            const first = pan && pan.querySelector('.connection-panel-item');
            if (first) first.focus();
        }
    }

    function close() {
        if (!isOpen) return;
        isOpen = false;
        searchTerm = '';
        const pan = panelEl();
        if (pan) { pan.hidden = true; pan.innerHTML = ''; }
        const p = pillEl();
        if (p) {
            p.setAttribute('aria-expanded', 'false');
            // Return focus to the trigger for keyboard users.
            p.focus();
        }
    }

    function toggle() { isOpen ? close() : open(); }

    function render() {
        const pan = panelEl();
        if (!pan) return;
        const active = getActiveConnection();
        const term = searchTerm.toLowerCase();
        const all = availableConnections || [];
        const filtered = all.filter(c =>
            !term ||
            (c.display_name || '').toLowerCase().includes(term) ||
            (c.source_key || '').toLowerCase().includes(term) ||
            (c.database_type || '').toLowerCase().includes(term)
        );
        const showSearch = all.length >= 5;

        let html = '';
        if (showSearch) {
            html += `<input type="text" class="connection-panel-search" placeholder="Search connections\u2026" value="${escapeHtml(searchTerm)}" aria-label="Search connections" />`;
        }
        if (filtered.length === 0) {
            const msg = term ? `No connections match "${escapeHtml(term)}"` : 'No connections available';
            html += `<div class="connection-panel-empty">${msg}</div>`;
        } else {
            html += filtered.map(c => {
                const isCurrent = c.source_key === active;
                const cls = 'connection-panel-item' + (isCurrent ? ' is-current' : '');
                const dbType = (c.database_type || 'unknown').toLowerCase();
                const check = isCurrent
                    ? '<svg class="connection-panel-item-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="5 12 10 17 19 8"/></svg>'
                    : '';
                return `<div class="${cls}" role="option" tabindex="0" data-source-key="${escapeHtml(c.source_key)}" aria-selected="${isCurrent}">` +
                    `<span class="connection-panel-item-name">${escapeHtml(c.display_name || c.source_key)}</span>` +
                    `<span class="connection-panel-item-type">${escapeHtml(dbType)}</span>` +
                    `${check}</div>`;
            }).join('');
        }
        if (active) {
            const activeRow = all.find(c => c.source_key === active);
            if (activeRow) {
                html += `<div class="connection-panel-footer"><button type="button" class="connection-panel-footer-btn" data-action="refresh">\u21bb Refresh metadata for ${escapeHtml(activeRow.display_name || active)}</button></div>`;
            }
        }
        pan.innerHTML = html;

        // Wire item interactions.
        pan.querySelectorAll('.connection-panel-item').forEach(node => {
            node.addEventListener('click', () => pick(node.dataset.sourceKey));
            node.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    pick(node.dataset.sourceKey);
                }
            });
        });
        // Search input.
        const search = pan.querySelector('.connection-panel-search');
        if (search) {
            search.addEventListener('input', (e) => {
                searchTerm = e.target.value;
                render();
                const fresh = pan.querySelector('.connection-panel-search');
                if (fresh) {
                    fresh.focus();
                    fresh.setSelectionRange(searchTerm.length, searchTerm.length);
                }
            });
            search.addEventListener('keydown', (e) => {
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    const first = pan.querySelector('.connection-panel-item');
                    if (first) first.focus();
                }
            });
        }
        // Footer refresh.
        const refresh = pan.querySelector('.connection-panel-footer-btn[data-action="refresh"]');
        if (refresh) {
            refresh.addEventListener('click', () => refreshActiveMetadata());
        }
    }

    function pick(sourceKey) {
        if (!sourceKey) { close(); return; }
        if (sourceKey === getActiveConnection()) { close(); return; }
        // Close first so focus returns to pill before tables refresh kicks in.
        close();
        onConnectionChange(sourceKey);
    }

    async function refreshActiveMetadata() {
        const active = getActiveConnection();
        if (!active) { close(); return; }
        close();
        try {
            setConnectionStatus('connecting');
            await fetch('/api/connections/' + encodeURIComponent(active) + '/refresh-metadata', { method: 'POST' });
            if (typeof SuggestionController !== 'undefined') SuggestionController.reset();
            allTables = [];
            await loadTables();
        } catch (e) {
            console.error('Failed to refresh metadata', e);
            setConnectionStatus('error');
        }
    }

    function onPanelKeydown(e) {
        if (!isOpen) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            close();
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            const items = Array.from(panelEl().querySelectorAll('.connection-panel-item'));
            if (items.length === 0) return;
            e.preventDefault();
            const cur = document.activeElement;
            let i = items.indexOf(cur);
            i = e.key === 'ArrowDown'
                ? (i + 1) % items.length
                : (i - 1 + items.length) % items.length;
            items[i].focus();
        }
    }

    function init() {
        const p = pillEl();
        if (p) {
            p.addEventListener('click', () => toggle());
            p.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
                else if (e.key === 'ArrowDown') { e.preventDefault(); open(); }
            });
        }
        const pan = panelEl();
        if (pan) pan.addEventListener('keydown', onPanelKeydown);
        document.addEventListener('click', (e) => {
            if (!isOpen) return;
            const switcher = document.querySelector('.connection-switcher');
            if (switcher && switcher.contains(e.target)) return;
            close();
        });
    }

    return { init, open, close, toggle, render };
})();

// ======================================================
// SIDEBAR TOGGLE
// ======================================================

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    if (!sidebar) return;
    const isMobile = window.innerWidth < 768;
    if (isMobile) {
        const isOpen = sidebar.classList.contains('mobile-open');
        sidebar.classList.toggle('mobile-open', !isOpen);
        if (overlay) overlay.classList.toggle('active', !isOpen);
    } else {
        sidebar.classList.toggle('collapsed');
    }
}

// ======================================================
// VIEWSCHEMA STUB
// ======================================================

function viewSchema(tableName) {
    fillQuestion('DESCRIBE ' + tableName);
}

// ======================================================
// AUTOCOMPLETE v3 — Tiered + Special-Character Triggers
// ======================================================
//
// Trigger registry: extending this array (with `#`, `$`, `!`, etc.) does
// NOT require controller changes. Each entry describes a special-char
// trigger and where to source its items.
const TRIGGERS = [
    {
        char: '@',
        label: 'Tables',
        hint: 'select to insert',
        getItems: () => (allTables || []).map(t => ({ text: t, tier: 'table' })),
        onPick: (item) => { lastInsertedTable = item.text; },
    },
    {
        char: '/',
        label: 'Templates',
        hint: 'from your knowledge base',
        getItems: () => {
            const list = (knowledgeQuestionsCache && knowledgeQuestionsCache.questions) || [];
            return list.map(q => ({ text: q.question, tier: 'template', category: q.category }));
        },
        onPick: () => {},
    },
    {
        char: '#',
        label: () => {
            const t = resolveColumnScope();
            return t ? `Columns of ${t}` : 'Columns (all tables)';
        },
        hint: () => {
            const t = resolveColumnScope();
            return t ? 'select to insert' : 'type @TableName first to scope';
        },
        // Provided by the controller so it can interleave group headers in
        // unscoped mode. Returning null tells the controller to use the
        // dedicated buildColumnsItems() pipeline instead of the default.
        getItems: null,
        scoped: () => !!resolveColumnScope(),
        onPick: (item) => { if (item.table) lastInsertedTable = item.table; },
    },
    // Future stubs (uncomment + flesh out as we ship them):
    // { char: '$', label: 'Measures', hint: 'coming soon', getItems: () => [], onPick: () => {} },
    // { char: '!', label: 'Filters',  hint: 'coming soon', getItems: () => [], onPick: () => {} },
];

function getTriggerByChar(c) {
    for (let i = 0; i < TRIGGERS.length; i++) {
        if (TRIGGERS[i].char === c) return TRIGGERS[i];
    }
    return null;
}

// Walk back from caret to nearest whitespace/start; if the resulting token
// starts with a registered trigger char, we're in trigger mode.
function getTriggerContext(textarea) {
    if (!textarea) return null;
    const value = textarea.value || '';
    const pos = textarea.selectionStart || 0;
    let start = pos;
    while (start > 0 && !/\s/.test(value[start - 1])) start--;
    const token = value.slice(start, pos);
    if (!token) return null;
    const ch = token[0];
    const trigger = getTriggerByChar(ch);
    if (!trigger) return null;
    return { trigger, query: token.slice(1), tokenStart: start, tokenEnd: pos };
}

function insertReplacingTrigger(textarea, ctx, replacement) {
    const before = textarea.value.slice(0, ctx.tokenStart);
    const after = textarea.value.slice(ctx.tokenEnd);
    // Add a trailing space so the user can keep typing naturally,
    // unless the cursor is already followed by whitespace.
    const sep = (after.length === 0 || /\s/.test(after[0])) ? '' : ' ';
    textarea.value = before + replacement + sep + after;
    const newPos = before.length + replacement.length + sep.length;
    textarea.selectionStart = textarea.selectionEnd = newPos;
    textarea.focus();
}

function highlightMatch(text, query) {
    if (!query) return escapeHtml(text);
    const lowText = String(text).toLowerCase();
    const lowQ = String(query).toLowerCase();
    const idx = lowText.indexOf(lowQ);
    if (idx === -1) return escapeHtml(text);
    return escapeHtml(text.slice(0, idx)) +
        '<mark>' + escapeHtml(text.slice(idx, idx + query.length)) + '</mark>' +
        escapeHtml(text.slice(idx + query.length));
}

async function fetchKnowledgeQuestions() {
    const conn = getActiveConnection();
    if (!conn) return;
    if (_kqLoadedFor === conn && knowledgeQuestionsCache) return;
    if (_kqLoading) return;
    _kqLoading = true;
    try {
        const resp = await fetch('/api/knowledge-questions?connection=' + encodeURIComponent(conn));
        const data = await resp.json();
        if (resp.ok) {
            knowledgeQuestionsCache = { sourceKey: conn, questions: data.questions || [] };
            _kqLoadedFor = conn;
        }
    } catch (e) {
        console.warn('[Autocomplete] Failed to load knowledge questions:', e);
    } finally {
        _kqLoading = false;
    }
}

// Resolve which table should scope `#` results. Order:
//   1. The most recent qualified `Table.` token left of the caret.
//   2. `lastInsertedTable` (set when the user picked an `@` suggestion).
//   3. null (unscoped — show columns from all tables).
function resolveColumnScope() {
    const t = document.getElementById('question-input');
    if (t && (allTables || []).length > 0) {
        const value = t.value || '';
        const pos = t.selectionStart || value.length;
        const left = value.slice(0, pos);
        // Match `Word.` directly before the caret (no whitespace between).
        const m = left.match(/([A-Za-z_][A-Za-z0-9_]*)\.[#A-Za-z0-9_]*$/);
        if (m && allTables.includes(m[1])) return m[1];
    }
    return lastInsertedTable || null;
}

async function fetchKnowledgeColumns(table) {
    const conn = getActiveConnection();
    if (!conn) return null;
    const scope = (table || '').trim() || 'ALL';
    const key = conn + '|' + scope;
    if (_columnsCache.has(key)) return _columnsCache.get(key);
    if (_columnsLoading.has(key)) return null;
    _columnsLoading.add(key);
    try {
        const url = '/api/knowledge-columns?connection=' + encodeURIComponent(conn)
            + (scope !== 'ALL' ? ('&table=' + encodeURIComponent(scope)) : '');
        const resp = await fetch(url);
        const data = await resp.json();
        if (resp.ok) {
            const cols = data.columns || [];
            _columnsCache.set(key, cols);
            // LRU cap (keep at most 200 entries; usually one per table).
            if (_columnsCache.size > 200) {
                const firstKey = _columnsCache.keys().next().value;
                _columnsCache.delete(firstKey);
            }
            return cols;
        }
    } catch (e) {
        console.warn('[Autocomplete] Failed to load columns:', e);
    } finally {
        _columnsLoading.delete(key);
    }
    return null;
}

const SuggestionController = (function () {
    const MIN_FREE_CHARS = 3;
    const LLM_MIN_CHARS = 10;
    const LLM_DEBOUNCE_MS = 300;
    const LLM_CACHE_TTL_MS = 60000;
    const LLM_CACHE_MAX = 50;
    const MAX_LOCAL_RESULTS = 8;

    let mode = 'closed';        // 'trigger' | 'tiered' | 'closed'
    let activeTrigger = null;   // TRIGGERS entry
    let activeIndex = -1;
    let currentItems = [];
    let currentCorrections = [];
    let currentQuery = '';
    let header = null;          // { label, hint } | null
    let loading = false;

    const ta = () => document.getElementById('question-input');
    const dd = () => document.getElementById('question-suggestions');

    function close() {
        mode = 'closed';
        activeTrigger = null;
        activeIndex = -1;
        currentItems = [];
        currentCorrections = [];
        loading = false;
        header = null;
        if (_llmAbort) { try { _llmAbort.abort(); } catch (e) {} _llmAbort = null; }
        if (_llmDebounceTimer) { clearTimeout(_llmDebounceTimer); _llmDebounceTimer = null; }
        const d = dd();
        if (d) { d.hidden = true; d.innerHTML = ''; }
        const t = ta();
        if (t) t.setAttribute('aria-expanded', 'false');
    }

    function reset() {
        // Connection switched — drop connection-specific caches.
        knowledgeQuestionsCache = null;
        _kqLoadedFor = null;
        _llmSuggestCache.clear();
        _columnsCache.clear();
        _columnsLoading.clear();
        recentQuestionsCache = [];
        pinnedQuestionsCache = [];
        lastInsertedTable = null;
        close();
    }

    function render() {
        const d = dd();
        const t = ta();
        if (!d || !t) return;
        const hasContent = currentItems.length > 0 || currentCorrections.length > 0 || loading || !!header;
        if (!hasContent) {
            d.hidden = true;
            d.innerHTML = '';
            t.setAttribute('aria-expanded', 'false');
            return;
        }
        d.hidden = false;
        t.setAttribute('aria-expanded', 'true');

        let html = '';
        if (header) {
            const hint = header.hint ? `<span class="suggestion-trigger-hint">${escapeHtml(header.hint)}</span>` : '';
            html += `<div class="suggestion-trigger-header"><span>${escapeHtml(header.label)}</span>${hint}</div>`;
        }
        if (currentCorrections.length) {
            html += currentCorrections.map((c, ci) => {
                // Always coerce to plain strings — the LLM has been observed to
                // wrap correction values in nested objects which would render
                // as [object Object] if passed straight to escapeHtml().
                const wrong = c && c.wrong != null ? String(c.wrong) : '';
                const right = c && c.right != null ? String(c.right) : '';
                if (!right || right === '[object Object]') return '';
                const wrongHtml = wrong && wrong !== '[object Object]'
                    ? `<code>${escapeHtml(wrong)}</code> \u2192 <code>${escapeHtml(right)}</code>`
                    : `<code>${escapeHtml(right)}</code>`;
                return `<div class="suggestion-correction-note" data-correction-index="${ci}" role="button" tabindex="0" title="Click to apply">Did you mean: ${wrongHtml}?</div>`;
            }).join('');
        }
        if (loading) {
            html += `<div class="suggestion-loading"><span class="suggestion-loading-dot"></span><span class="suggestion-loading-dot"></span><span class="suggestion-loading-dot"></span><span>Thinking\u2026</span></div>`;
        }
        if (currentItems.length) {
            html += currentItems.map((it, i) => {
                if (it.kind === 'group') {
                    return `<div class="suggestion-group-header" data-role="group" aria-hidden="true">${escapeHtml(it.text)}</div>`;
                }
                const cls = i === activeIndex ? 'suggestion-item is-active' : 'suggestion-item';
                const tagText = it.tag != null ? it.tag : it.tier;
                const tier = it.tier || (tagText || '').toLowerCase();
                const tag = tagText
                    ? `<span class="suggestion-tier-tag" data-tier="${escapeHtml(tier)}">${escapeHtml(tagText)}</span>`
                    : '';
                const titleAttr = it.tooltip ? ` title="${escapeHtml(it.tooltip)}"` : '';
                return `<div class="${cls}" data-index="${i}" role="option"${titleAttr}>` +
                    `<span class="suggestion-item-text">${highlightMatch(it.text, currentQuery)}</span>${tag}</div>`;
            }).join('');
        } else if (!loading && mode === 'trigger') {
            html += `<div class="suggestion-empty">No matches</div>`;
        }
        d.innerHTML = html;

        // Wire mousedown (NOT click) so the textarea's blur doesn't kill us first.
        d.querySelectorAll('.suggestion-item').forEach(node => {
            node.addEventListener('mousedown', (e) => {
                e.preventDefault();
                const i = parseInt(node.dataset.index, 10);
                pick(i);
            });
        });
        d.querySelectorAll('.suggestion-correction-note').forEach(node => {
            node.addEventListener('mousedown', (e) => {
                e.preventDefault();
                const i = parseInt(node.dataset.correctionIndex, 10);
                applyCorrection(i);
            });
        });
        if (activeIndex >= 0) {
            const active = d.querySelector('.suggestion-item.is-active');
            if (active && typeof active.scrollIntoView === 'function') {
                active.scrollIntoView({ block: 'nearest' });
            }
        }
    }

    function isSelectable(item) {
        return item && item.kind !== 'group';
    }

    function firstSelectableIndex() {
        for (let i = 0; i < currentItems.length; i++) {
            if (isSelectable(currentItems[i])) return i;
        }
        return -1;
    }

    function applyCorrection(i) {
        const t = ta();
        if (!t) return;
        const c = currentCorrections[i];
        if (!c) return;
        const value = t.value || '';
        if (c.wrong) {
            // Replace first case-insensitive occurrence of `wrong` with `right`.
            const re = new RegExp(c.wrong.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
            if (re.test(value)) {
                t.value = value.replace(re, c.right);
                t.selectionStart = t.selectionEnd = t.value.length;
                t.focus();
                onInput();
                return;
            }
        }
        // Fallback: append the corrected token at the caret.
        t.value = (value ? value.replace(/\s*$/, ' ') : '') + c.right;
        t.selectionStart = t.selectionEnd = t.value.length;
        t.focus();
        onInput();
    }

    function setItems(items, opts) {
        opts = opts || {};
        const cap = opts.cap || MAX_LOCAL_RESULTS;
        // Cap counts only selectable rows (group headers are free).
        const out = [];
        let selected = 0;
        for (const it of items) {
            if (it && it.kind === 'group') {
                out.push(it);
                continue;
            }
            if (selected >= cap) break;
            out.push(it);
            selected += 1;
        }
        currentItems = out;
        activeIndex = firstSelectableIndex();
        currentCorrections = opts.corrections || [];
        loading = !!opts.loading;
        render();
    }

    function pick(i) {
        if (i < 0 || i >= currentItems.length) return;
        const item = currentItems[i];
        if (!isSelectable(item)) return;
        const t = ta();
        if (!t) return;
        if (mode === 'trigger') {
            const ctx = getTriggerContext(t);
            // Decide what to insert. Columns are special: scoped picks insert
            // the bare column, unscoped picks insert `Table.column`.
            let replacement = item.text;
            if (item.kind === 'column') {
                replacement = item.scoped ? item.column : (item.table + '.' + item.column);
            }
            if (ctx && activeTrigger && ctx.trigger.char === activeTrigger.char) {
                insertReplacingTrigger(t, ctx, replacement);
            } else {
                t.value = (t.value || '') + replacement;
            }
            if (activeTrigger && typeof activeTrigger.onPick === 'function') {
                activeTrigger.onPick(item);
            }
        } else if (mode === 'tiered') {
            // Replace whole textarea content (typical autocomplete UX)
            t.value = item.text;
            t.selectionStart = t.selectionEnd = item.text.length;
        }
        close();
    }

    function moveActive(delta) {
        if (currentItems.length === 0) return;
        const dir = delta >= 0 ? 1 : -1;
        let idx = activeIndex >= 0 ? activeIndex : (dir > 0 ? -1 : currentItems.length);
        for (let step = 0; step < currentItems.length; step++) {
            idx = (idx + dir + currentItems.length) % currentItems.length;
            if (isSelectable(currentItems[idx])) {
                activeIndex = idx;
                render();
                return;
            }
        }
    }

    function collectLocalMatches(partial) {
        const q = partial.toLowerCase();
        const out = [];
        const seen = new Set();

        // Tier 1: pinned + recent
        const recents = [].concat(pinnedQuestionsCache || [], recentQuestionsCache || []);
        for (let i = 0; i < recents.length && out.length < MAX_LOCAL_RESULTS; i++) {
            const r = recents[i];
            if (typeof r === 'string' && r.toLowerCase().includes(q) && !seen.has(r)) {
                out.push({ text: r, tier: 'recent' });
                seen.add(r);
            }
        }
        // Tier 2a: knowledge_pairs questions
        const kqs = (knowledgeQuestionsCache && knowledgeQuestionsCache.questions) || [];
        for (let i = 0; i < kqs.length && out.length < MAX_LOCAL_RESULTS; i++) {
            const txt = kqs[i] && kqs[i].question;
            if (typeof txt === 'string' && txt.toLowerCase().includes(q) && !seen.has(txt)) {
                out.push({ text: txt, tier: 'catalog' });
                seen.add(txt);
            }
        }
        // Tier 2b: table names
        const tbls = allTables || [];
        for (let i = 0; i < tbls.length && out.length < MAX_LOCAL_RESULTS; i++) {
            const t2 = tbls[i];
            if (typeof t2 === 'string' && t2.toLowerCase().includes(q) && !seen.has(t2)) {
                out.push({ text: t2, tier: 'table' });
                seen.add(t2);
            }
        }
        return out;
    }

    function evictLLMCache() {
        while (_llmSuggestCache.size > LLM_CACHE_MAX) {
            const firstKey = _llmSuggestCache.keys().next().value;
            _llmSuggestCache.delete(firstKey);
        }
    }

    function scheduleLLM(partial) {
        if (_llmDebounceTimer) clearTimeout(_llmDebounceTimer);
        _llmDebounceTimer = setTimeout(() => fireLLM(partial), LLM_DEBOUNCE_MS);
    }

    async function fireLLM(partial) {
        const conn = getActiveConnection();
        if (!conn) return;
        // Send only the last line up to caret to keep signal sharp.
        const lastLine = partial.split('\n').pop().trim();
        if (lastLine.length < LLM_MIN_CHARS) return;

        const cacheKey = conn + '|' + lastLine.toLowerCase();
        const cached = _llmSuggestCache.get(cacheKey);
        if (cached && (Date.now() - cached.ts) < LLM_CACHE_TTL_MS) {
            const items = (cached.suggestions || []).map(s => ({ text: s, tier: 'llm' }));
            currentQuery = lastLine;
            header = { label: 'Suggested', hint: 'AI completions' };
            setItems(items, { corrections: cached.corrections || [] });
            return;
        }

        if (_llmAbort) { try { _llmAbort.abort(); } catch (e) {} }
        _llmAbort = new AbortController();
        const requestId = ++_llmRequestId;

        loading = true;
        currentItems = [];
        activeIndex = -1;
        header = { label: 'Suggested', hint: 'AI completions' };
        currentQuery = lastLine;
        render();

        try {
            const recent = [].concat(pinnedQuestionsCache || [], recentQuestionsCache || []).slice(0, 10);
            const resp = await fetch('/api/suggest-questions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    connection: conn,
                    partial: lastLine,
                    table_names: allTables || [],
                    recent_questions: recent,
                }),
                signal: _llmAbort.signal,
            });
            if (requestId !== _llmRequestId) return; // stale
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Suggest failed');

            _llmSuggestCache.set(cacheKey, {
                ts: Date.now(),
                suggestions: data.suggestions || [],
                corrections: data.corrections || [],
            });
            evictLLMCache();

            const items = (data.suggestions || []).map(s => ({ text: s, tier: 'llm' }));
            loading = false;
            // Defensive: server already normalises, but accept legacy shapes too.
            const corr = (data.corrections || []).map(c => {
                if (c && typeof c === 'object' && (c.wrong || c.right)) {
                    const w = String(c.wrong != null ? c.wrong : '');
                    const r = String(c.right != null ? c.right : '');
                    if (w === '[object Object]' || r === '[object Object]') return null;
                    return { wrong: w, right: r };
                }
                if (typeof c === 'string') {
                    const m = c.split(/->|\u2192/);
                    if (m.length === 2) return { wrong: m[0].trim(), right: m[1].trim() };
                    return { wrong: '', right: c.trim() };
                }
                return null;
            }).filter(c => c && c.right);
            if (items.length === 0 && corr.length === 0) {
                close();
                return;
            }
            setItems(items, { corrections: corr });
        } catch (e) {
            if (e && e.name === 'AbortError') return;
            console.warn('[Autocomplete] LLM tier failed:', e);
            loading = false;
            close();
        }
    }

    // Build the items array for `#` (columns), supporting scoped vs unscoped
    // modes. Returns null when columns aren't loaded yet so the caller can
    // schedule a fetch and re-render.
    function buildColumnsItems(query) {
        const conn = getActiveConnection();
        if (!conn) return [];
        const scopeTable = resolveColumnScope();
        const cacheKey = conn + '|' + (scopeTable || 'ALL');
        const cached = _columnsCache.get(cacheKey);
        if (!cached) return null; // controller will fetch
        const q = (query || '').toLowerCase();

        const matches = cached.filter(c => {
            if (!q) return true;
            const name = (c.column || '').toLowerCase();
            const desc = (c.description || '').toLowerCase();
            return name.includes(q) || desc.includes(q);
        });
        // Rank: prefix-on-name first, then substring-on-name, then description-only.
        const rankOf = (c) => {
            const n = (c.column || '').toLowerCase();
            if (q && n.startsWith(q)) return 0;
            if (q && n.includes(q))   return 1;
            return 2;
        };
        // In unscoped mode, prefer the last-mentioned table.
        const tableBoost = (c) => (!scopeTable && lastInsertedTable && c.table === lastInsertedTable) ? -1 : 0;
        matches.sort((a, b) => {
            const ra = rankOf(a) + tableBoost(a);
            const rb = rankOf(b) + tableBoost(b);
            if (ra !== rb) return ra - rb;
            if (a.table !== b.table) return String(a.table).localeCompare(String(b.table));
            return String(a.column).localeCompare(String(b.column));
        });

        const items = [];
        if (scopeTable) {
            for (const c of matches) {
                items.push({
                    kind: 'column',
                    text: c.column,
                    table: c.table,
                    column: c.column,
                    scoped: true,
                    tier: 'column',
                    tag: c.data_type || 'col',
                    tooltip: c.description || `${c.table}.${c.column}`,
                });
            }
        } else {
            // Unscoped — group by table.
            let lastTable = null;
            let cap = 60; // soft cap on rows so the dropdown stays scannable
            for (const c of matches) {
                if (cap <= 0) break;
                if (c.table !== lastTable) {
                    items.push({ kind: 'group', text: c.table });
                    lastTable = c.table;
                }
                items.push({
                    kind: 'column',
                    text: c.table + '.' + c.column,
                    table: c.table,
                    column: c.column,
                    scoped: false,
                    tier: 'column',
                    tag: c.data_type || 'col',
                    tooltip: c.description || `${c.table}.${c.column}`,
                });
                cap -= 1;
            }
        }
        return items;
    }

    function onInput() {
        const t = ta();
        if (!t) return;

        // 1) Trigger detection wins.
        const ctx = getTriggerContext(t);
        if (ctx) {
            mode = 'trigger';
            activeTrigger = ctx.trigger;
            currentQuery = ctx.query;
            const headerLabel = typeof ctx.trigger.label === 'function' ? ctx.trigger.label() : ctx.trigger.label;
            const headerHint  = typeof ctx.trigger.hint  === 'function' ? ctx.trigger.hint()  : ctx.trigger.hint;
            header = { label: headerLabel, hint: headerHint };

            // Lazy-fetch templates the first time `/` is used.
            if (ctx.trigger.char === '/' && !knowledgeQuestionsCache && !_kqLoading) {
                fetchKnowledgeQuestions().then(() => {
                    if (mode === 'trigger' && activeTrigger && activeTrigger.char === '/') onInput();
                });
            }

            // `#` columns: bespoke build w/ lazy fetch.
            if (ctx.trigger.char === '#') {
                const scopeTable = resolveColumnScope();
                const built = buildColumnsItems(ctx.query);
                if (built === null) {
                    // Cache miss — show loading and fetch.
                    loading = true;
                    currentItems = [];
                    activeIndex = -1;
                    currentCorrections = [];
                    render();
                    fetchKnowledgeColumns(scopeTable).then(() => {
                        if (mode === 'trigger' && activeTrigger && activeTrigger.char === '#') onInput();
                    });
                    return;
                }
                setItems(built, { cap: 60 });
                return;
            }

            const items = ctx.trigger.getItems()
                .filter(it => !ctx.query || (it.text || '').toLowerCase().includes(ctx.query.toLowerCase()))
                .slice(0, MAX_LOCAL_RESULTS);
            setItems(items, {});
            return;
        }

        // 2) Tiered free-text mode.
        const value = t.value || '';
        const partial = value.trim();
        if (partial.length < MIN_FREE_CHARS) {
            close();
            return;
        }
        mode = 'tiered';
        activeTrigger = null;
        currentQuery = partial;
        header = null;

        const local = collectLocalMatches(partial);
        if (local.length > 0) {
            // Cancel any pending LLM since we already have results.
            if (_llmAbort) { try { _llmAbort.abort(); } catch (e) {} _llmAbort = null; }
            if (_llmDebounceTimer) { clearTimeout(_llmDebounceTimer); _llmDebounceTimer = null; }
            setItems(local, {});
            return;
        }

        // No local hits — try LLM if long enough.
        if (partial.length < LLM_MIN_CHARS) {
            close();
            return;
        }
        // Clear any visible suggestions while we wait, but keep dropdown closed
        // until the LLM responds (no spinner flicker on every keystroke).
        if (_llmAbort) { try { _llmAbort.abort(); } catch (e) {} _llmAbort = null; }
        scheduleLLM(partial);
    }

    function onKeydown(e) {
        if (mode === 'closed') return false;
        if (e.key === 'Escape')   { e.preventDefault(); close(); return true; }
        if (e.key === 'ArrowDown'){ e.preventDefault(); moveActive(1); return true; }
        if (e.key === 'ArrowUp')  { e.preventDefault(); moveActive(-1); return true; }
        // Tab always accepts when there's an active suggestion.
        if (e.key === 'Tab' && activeIndex >= 0 && currentItems.length) {
            e.preventDefault();
            pick(activeIndex);
            return true;
        }
        // Enter accepts ONLY in trigger mode (so plain Enter still submits a
        // free-text question even if a tiered suggestion is highlighted).
        if (e.key === 'Enter' && mode === 'trigger' && activeIndex >= 0 && currentItems.length) {
            e.preventDefault();
            pick(activeIndex);
            return true;
        }
        return false;
    }

    function onFocus() {
        // Lazy-fetch templates so `/` is instant on first use.
        if (!knowledgeQuestionsCache && !_kqLoading && getActiveConnection()) {
            fetchKnowledgeQuestions().catch(() => {});
        }
        // If the user lands back in a textarea that already has content,
        // re-run input to re-open suggestions.
        onInput();
    }

    function onBlur() {
        // Delay so click on an item registers first.
        setTimeout(() => close(), 150);
    }

    return { onInput, onKeydown, onFocus, onBlur, close, reset, pick, moveActive };
})();

// ======================================================
// DOMContentLoaded
// ======================================================

document.addEventListener('DOMContentLoaded', () => {
    // Remove no-transitions class after first render to enable theme transitions
    requestAnimationFrame(() => requestAnimationFrame(() => {
        document.documentElement.classList.remove('no-transitions');
    }));

    // Initialize theme icon
    updateThemeIcon(getTheme());

    // Theme toggle button
    const themeToggleBtn = document.getElementById('theme-toggle');
    if (themeToggleBtn) themeToggleBtn.addEventListener('click', toggleTheme);

    // Sidebar toggle
    const sidebarToggleBtn = document.getElementById('sidebar-toggle');
    if (sidebarToggleBtn) sidebarToggleBtn.addEventListener('click', toggleSidebar);

    // Sidebar overlay dismiss
    const overlay = document.getElementById('sidebar-overlay');
    if (overlay) overlay.addEventListener('click', toggleSidebar);

    // Custom connection switcher (replaces the native <select>).
    if (typeof ConnectionPanel !== 'undefined') ConnectionPanel.init();

    // Restore sidebar tab preference (Tables vs Recent) from localStorage.
    (function () {
        const saved = (function () {
            try { return localStorage.getItem(SIDEBAR_TAB_KEY); } catch (_) { return null; }
        })();
        switchSidebarTab(saved === 'recent' ? 'recent' : 'tables');
    })();

    // Wire the Chat controller, then restore the last-used interaction mode.
    if (window.ChatController && typeof window.ChatController.init === 'function') {
        window.ChatController.init();
    }
    (function () {
        let savedMode = 'ask';
        try { savedMode = localStorage.getItem(APP_MODE_KEY) || 'ask'; } catch (_) {}
        setAppMode(savedMode === 'chat' ? 'chat' : 'ask');
    })();

    // ── History Drawer ──────────────────────────────────────────────────
    (function () {
        const drawer  = document.getElementById('history-drawer');
        const overlay = document.getElementById('history-drawer-overlay');
        const btn     = document.getElementById('history-btn');
        const closeBtn = document.getElementById('history-drawer-close');
        if (!drawer || !overlay || !btn) return;

        let isOpen = false;

        function open() {
            if (isOpen) return;
            isOpen = true;
            drawer.classList.add('open');
            overlay.classList.add('open');
            btn.classList.add('is-active');
            drawer.setAttribute('aria-hidden', 'false');
            overlay.setAttribute('aria-hidden', 'false');
            loadHistoryLog();
        }

        function close() {
            if (!isOpen) return;
            isOpen = false;
            drawer.classList.remove('open');
            overlay.classList.remove('open');
            btn.classList.remove('is-active');
            drawer.setAttribute('aria-hidden', 'true');
            overlay.setAttribute('aria-hidden', 'true');
            btn.focus();
        }

        function toggle() { isOpen ? close() : open(); }

        btn.addEventListener('click', toggle);
        if (closeBtn) closeBtn.addEventListener('click', close);
        overlay.addEventListener('click', close);
        document.addEventListener('keydown', (e) => {
            if (isOpen && e.key === 'Escape') { e.preventDefault(); close(); }
        });

        // Expose so history-entry onclick can close the drawer before fillQuestion.
        window._historyDrawerClose = close;
    })();

    // ── Run Details Drawer ──────────────────────────────────────────────
    (function () {
        const drawer  = document.getElementById('dev-drawer');
        const overlay = document.getElementById('dev-drawer-overlay');
        const btn     = document.getElementById('dev-panel-btn');
        const closeBtn = document.getElementById('dev-drawer-close');
        const badge   = document.getElementById('dev-panel-badge');
        const resizeHandle = document.getElementById('dev-drawer-resize');
        if (!drawer || !overlay || !btn) return;

        let isOpen = false;
        const widthKey = 'jeen_dev_drawer_width';

        function _clampDrawerWidth(width) {
            const viewport = window.innerWidth || 1024;
            const min = Math.min(420, Math.max(320, viewport - 24));
            const max = Math.max(min, Math.min(Math.round(viewport * 0.92), 1400));
            return Math.min(Math.max(Math.round(width), min), max);
        }

        function _setDrawerWidth(width, persist = true) {
            const clamped = _clampDrawerWidth(width);
            drawer.style.setProperty('--dev-drawer-width', `${clamped}px`);
            if (persist) {
                try { localStorage.setItem(widthKey, String(clamped)); } catch (_) {}
            }
        }

        function _applySavedDrawerWidth() {
            let stored = null;
            try { stored = Number(localStorage.getItem(widthKey)); } catch (_) {}
            if (Number.isFinite(stored) && stored > 0) _setDrawerWidth(stored, false);
        }

        _applySavedDrawerWidth();

        function open() {
            if (isOpen) return;
            isOpen = true;
            _applySavedDrawerWidth();
            drawer.classList.add('open');
            overlay.classList.add('open');
            btn.classList.add('is-active');
            drawer.setAttribute('aria-hidden', 'false');
            overlay.setAttribute('aria-hidden', 'false');
            // Ensure tab content container is visible.
            const tabContent = drawer.querySelector('.prompt-tab-content');
            if (tabContent && tabContent.style.display === 'none') {
                tabContent.style.display = 'block';
            }
            // Always auto-switch to the Log tab when trace data is present.
            // This means every time the drawer opens after a query the user
            // immediately sees the execution log rather than the empty SQL tab.
            const tracePanel = document.getElementById('trace-panel');
            const hasTrace = tracePanel && !tracePanel.querySelector('.trace-empty');
            if (hasTrace) {
                switchPromptTab('trace');
            } else if (tabContent && tabContent.style.display === 'block' && !document.getElementById('tab-sql')?.classList.contains('active')) {
                // First open and no trace yet — default to SQL tab
                switchPromptTab('sql');
            }
        }

        function close() {
            if (!isOpen) return;
            isOpen = false;
            drawer.classList.remove('open');
            overlay.classList.remove('open');
            btn.classList.remove('is-active');
            drawer.setAttribute('aria-hidden', 'true');
            overlay.setAttribute('aria-hidden', 'true');
            btn.focus();
        }

        function toggle() { isOpen ? close() : open(); }

        btn.addEventListener('click', toggle);
        if (closeBtn) closeBtn.addEventListener('click', close);
        overlay.addEventListener('click', close);
        document.addEventListener('keydown', (e) => {
            if (isOpen && e.key === 'Escape') { e.preventDefault(); close(); }
        });
        window.addEventListener('resize', () => {
            const current = drawer.getBoundingClientRect().width;
            if (current > 0) _setDrawerWidth(current, false);
        });

        if (resizeHandle) {
            const startResize = (e) => {
                if (!isOpen) return;
                if (e.type === 'mousedown' && e.button !== 0) return;
                e.preventDefault();
                if (e.pointerId !== undefined) resizeHandle.setPointerCapture?.(e.pointerId);
                drawer.classList.add('is-resizing');
                document.body.classList.add('dev-drawer-resizing');

                const onMove = (moveEvent) => {
                    _setDrawerWidth((window.innerWidth || 0) - moveEvent.clientX);
                };
                const onUp = (upEvent) => {
                    if (upEvent.pointerId !== undefined) resizeHandle.releasePointerCapture?.(upEvent.pointerId);
                    drawer.classList.remove('is-resizing');
                    document.body.classList.remove('dev-drawer-resizing');
                    window.removeEventListener('pointermove', onMove);
                    window.removeEventListener('pointerup', onUp);
                    window.removeEventListener('pointercancel', onUp);
                    window.removeEventListener('mousemove', onMove);
                    window.removeEventListener('mouseup', onUp);
                };

                if (e.type === 'pointerdown') {
                    window.addEventListener('pointermove', onMove);
                    window.addEventListener('pointerup', onUp);
                    window.addEventListener('pointercancel', onUp);
                } else {
                    window.addEventListener('mousemove', onMove);
                    window.addEventListener('mouseup', onUp);
                }
            };

            resizeHandle.addEventListener('pointerdown', startResize);
            resizeHandle.addEventListener('mousedown', startResize);

            resizeHandle.addEventListener('dblclick', () => {
                drawer.style.removeProperty('--dev-drawer-width');
                try { localStorage.removeItem(widthKey); } catch (_) {}
            });
        }

        // Expose so initCodeMirror (called after SQL loads) can show the badge.
        window._devDrawerShowBadge = function () {
            if (badge) badge.hidden = false;
        };
        window._devDrawerOpen  = open;
        window._devDrawerClose = close;
    })();

    // Preferences module — load preferences and apply persisted theme.
    // The settings button is now wired to SettingsPage (settingsPage.js module,
    // loaded inline in index.html) so we only need to expose JeenPreferences here.
    (async () => {
        try {
            const prefsModule = await import('./settings/preferences.js');
            window.JeenPreferences = prefsModule.Preferences;
            // Apply persisted theme on page load (handles 'system' too).
            applyThemeFromPreference(prefsModule.Preferences.getAll().theme);
        } catch (e) {
            console.warn('[Settings] Failed to initialise preferences:', e);
        }
    })();

    // Question input keyboard shortcuts + autocomplete wiring
    const questionInput = document.getElementById('question-input');
    if (questionInput) {
        questionInput.addEventListener('keydown', (e) => {
            // Let the SuggestionController consume keys it cares about first.
            if (SuggestionController.onKeydown(e)) return;

            // Ctrl/Cmd+Enter or plain Enter (without shift) = submit
            if (((e.ctrlKey || e.metaKey) && e.key === 'Enter') ||
                (e.key === 'Enter' && !e.shiftKey)) {
                e.preventDefault();
                askQuestion();
            }
            // Escape = clear (only when controller didn't already handle it)
            if (e.key === 'Escape') {
                questionInput.value = '';
                questionInput.blur();
            }
        });
        questionInput.addEventListener('input', () => SuggestionController.onInput());
        questionInput.addEventListener('focus', () => SuggestionController.onFocus());
        questionInput.addEventListener('blur',  () => SuggestionController.onBlur());
        // Re-evaluate when caret moves via mouse click.
        questionInput.addEventListener('click', () => SuggestionController.onInput());
    }

    // Click outside the query input wrap closes the suggestions.
    document.addEventListener('click', (e) => {
        const wrap = document.querySelector('.query-input-wrap');
        if (!wrap) return;
        if (!wrap.contains(e.target)) SuggestionController.close();
    });

    // Load history on page load
    displayHistory();

    // Expose functions to window for inline onclick/onkeyup handlers
    window.askQuestion = askQuestion;
    window.fillQuestion = fillQuestion;
    window.copySql = copySql;
    window.copyResults = copyResults;
    window.toggleSql = toggleSql;
    window.togglePrompt = togglePrompt;
    window.toggleDescribe = toggleDescribe;
    window.switchStatsTab = switchStatsTab;
    window.loadTables = loadTables;
    window.filterTables = filterTables;
    window.viewSchema = viewSchema;
    window.saveToHistory = saveToHistory;
    window.clearHistory = clearHistory;
    window.sortTable = sortTable;
    window.filterResults = filterResults;
    window.pinQuestion = pinQuestion;
    window.unpinQuestion = unpinQuestion;
    window.filterQuestionHistory = filterQuestionHistory;
    window.loadHistoryLog = loadHistoryLog;
    window.filterHistoryLog = filterHistoryLog;
    window.switchSidebarTab = switchSidebarTab;
    window.togglePromptSection = togglePromptSection;
    window.switchPromptTab = switchPromptTab;
    window.toggleSidebar = toggleSidebar;
    window.toggleTheme = toggleTheme;
    window.getActiveConnection = getActiveConnection;
    window.setActiveConnection = setActiveConnection;
    window.onConnectionChange = onConnectionChange;
    window.loadConnections = loadConnections;
    window.selectTable = selectTable;
    window.selectTableExplore = selectTableExplore;
    window.selectTableSchema = selectTableSchema;
    window.copyTableName = copyTableName;
    window.toggleTableExpand = toggleTableExpand;
    window.setPageTitle = setPageTitle;

    console.log('[Module] Functions exposed to window:', Object.keys(window).filter(k => ['askQuestion', 'sortTable', 'filterResults'].includes(k)));
});
