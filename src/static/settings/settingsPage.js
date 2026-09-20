/**
 * Settings Page
 *
 * Full-screen two-column layout:
 *   Left  — grouped navigation (like the reference design)
 *   Right — content area (General params · Prompts list/editor · About)
 *
 * Navigation groups
 * -----------------
 *   USER        General (personal prefs) + admin-only workspace settings
 *   AI AGENT    Prompts — one entry; the list of editable prompts comes from
 *               GET /api/settings/prompts (list → editor with a back link)
 *   OTHER       At the bottom — About · Logout · Close
 *
 * Rendering discipline
 * --------------------
 *   Every renderer starts with `_onTab(id)` and re-checks `_renderSeq` after
 *   each await, so a slow fetch (or a stale event handler) can never paint
 *   over the tab the user has since switched to.
 *
 * @module settingsPage
 */

import { Preferences } from './preferences.js';
import { CHART_TYPE_OPTIONS } from '../chart-feature/chartTypes.js?v=78';

// Interface strings come from the locale catalog (static/i18n/i18n.js, a classic
// script that is loaded before this module). `t` is plain text; `h` is
// HTML-escaped for template literals and attributes.
const t = (key, args) => (window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const h = (key, args) => (window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : _escAttr(t(key, args)));
const iso = (value) => (window.I18n && typeof window.I18n.isolate === 'function' ? window.I18n.isolate(value) : String(value == null ? '' : value));
function _escAttr(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

// ── Icons (inline SVG snippets, 18×18) ────────────────────────────────────────
const ICONS = {
    models:   `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#1e2026"/><text x="50%" y="55%" font-family="Urbanist,system-ui,-apple-system,sans-serif" font-weight="800" font-size="18" fill="#FFFFFF" dominant-baseline="middle" text-anchor="middle">J</text></svg>`,
    general:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
    prompt:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    about:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    close:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
    logout:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
    catalog:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg>`,
    link:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`,
    shield:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg>`,
    plug:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a6 6 0 0 1-12 0V8z"/></svg>`,
};

// ── Navigation definition ─────────────────────────────────────────────────────
const ICONS_USERS = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`;

// Workspace-wide settings (catalog source, global LLM, guardrails, prompts)
// are admin-only: every write behind them is admin-gated server-side, and
// /api/mcp/* is admin-only even for reads, so showing them to members only
// produces blank panes and 403 toasts.
const NAV = [
        {
        group: 'settings.nav.groupUser',
        items: [
            { id: 'general',           label: 'settings.nav.general',         icon: ICONS.general },
            { id: 'metadata-catalog',  label: 'settings.nav.metadataCatalog', icon: ICONS.catalog,  gate: 'admin' },
            { id: 'ai-models',         label: 'settings.nav.aiModels',        icon: ICONS.models,   gate: 'admin' },
            { id: 'query-safety',      label: 'settings.nav.querySafety',     icon: ICONS.shield,   gate: 'admin' },
            { id: 'my-connections',    label: 'settings.nav.myConnections',   icon: ICONS.link,     gate: 'connections' },
            { id: 'integrations',      label: 'settings.nav.integrations',    icon: ICONS.plug,     gate: 'admin' },
            { id: 'users',             label: 'settings.nav.users',           icon: ICONS_USERS,    gate: 'admin' },
        ],
    },
    {
        group: 'settings.nav.groupAgent',
        items: [
            // One entry for every editable prompt; the list itself comes from
            // GET /api/settings/prompts so new registry entries appear without
            // touching this file.
            { id: 'prompts', label: 'settings.nav.prompts', icon: ICONS.prompt, gate: 'admin' },
        ],
    },
];

// Bottom items (rendered separately, pinned to bottom of sidebar)
const BOTTOM_NAV = [
    { id: 'about', label: 'settings.nav.about', icon: ICONS.about, type: 'about' },
];

// ── SettingsPage class ────────────────────────────────────────────────────────

export class SettingsPage {
    constructor() {
        this._open       = false;
        this._root       = null;    // overlay wrapper
        this._content    = null;    // right content panel
        this._activeId   = 'general';
        // Monotonic render generation. Bumped on every tab switch so a renderer
        // that resumes after an await can tell the user has moved on.
        this._renderSeq  = 0;
        this._prompts    = {};      // name → {meta, content, dirty, editing}
        this._promptOpen = null;    // prompt name shown in the editor, or null for the list
        this._promptsLoading = null; // in-flight _loadPrompts() promise (shared, never duplicated)
        this._promptsError = null;  // last _loadPrompts() failure message, if any
        this._models     = null;    // cached model list (Array)
        this._onApplyTheme = null;
        this._promptContexts = null; // connection choices for resolved prompt view
        this._promptContextSource = 'db';
        this._promptResolveConnection = null;
        // Metadata & Catalog state
        this._mcpStatus   = null;   // last /api/mcp/status response
        this._mcpConn     = null;   // currently viewed connection
        this._mcpEditing  = null;   // server id being edited, or 'new'
        this._mcpDraft    = null;   // form draft object
        this._mcpTesting  = false;  // health check in progress
        this._mcpReloading= false;  // background status reload in progress
        this._mcpToolsServerId = null; // server id for loaded tool schemas
        this._mcpTools    = null;   // live tool descriptors for inspector
        this._mcpToolsLoading = false;
        this._mcpSelectedTool = null;
        this._mcpToolArgs = null;   // editable JSON string
        this._mcpToolArgsByTool = {};
        this._mcpToolInputMode = 'guided';
        this._mcpToolResult = null; // last test-call response/error
        this._mcpToolResultView = 'content';
        this._mcpToolCalling = false;
    }

    mount(hooks = {}) {
        if (this._root) return;
        this._onApplyTheme = hooks.onApplyTheme || null;
        this._buildDOM();
        document.addEventListener('keydown', (e) => {
            if (this._open && e.key === 'Escape') { e.preventDefault(); this.close(); }
        });
    }

    open() {
        if (!this._root) this.mount();
        this._root.hidden = false;
        this._open = true;
        document.body.style.overflow = 'hidden';
        this._applyNavGating();
        // Prompt metadata is admin-only server-side; don't fetch it for members.
        if (_isAdmin()) this._loadPrompts();
        // If the previously-active section is now gated off, fall back to General.
        const activeEl = this._root.querySelector(`.sp-nav-item[data-id="${this._activeId}"]`);
        if (activeEl && activeEl.hidden) this._activeId = 'general';
        this._activate(this._activeId);
    }

    close() {
        if (!this._open) return;
        if (!this._confirmLeavePrompt()) return;
        this._root.hidden = true;
        this._open = false;
        document.body.style.overflow = '';
    }

    /** True while Settings is open on tab `id` — renderers bail out otherwise. */
    _onTab(id) { return this._open && this._activeId === id; }

    /**
     * Guard every exit from a dirty prompt editor (back link, nav click, Close,
     * Escape). Returns false when the user chose to stay.
     */
    _confirmLeavePrompt() {
        const name = this._promptOpen;
        const entry = name ? this._prompts[name] : null;
        if (!entry || !entry.editing || !entry.dirty) return true;
        const label = entry.meta?.label || name;
        if (!confirm(t('settings.prompts.discardConfirm', { label: iso(label) }))) return false;
        entry.editing = false;
        entry.dirty = false;
        return true;
    }

    toggle() { this._open ? this.close() : this.open(); }

    // ── DOM construction ──────────────────────────────────────────────────────

    _buildDOM() {
        const overlay = document.createElement('div');
        overlay.className = 'sp-overlay';
        overlay.hidden = true;

        const page = document.createElement('div');
        page.className = 'sp-page';

        // ── Sidebar ──────────────────────────────────────────────────────────
        const sidebar = document.createElement('nav');
        sidebar.className = 'sp-sidebar';

        const sidebarHeader = document.createElement('div');
        sidebarHeader.className = 'sp-sidebar-header';
        sidebarHeader.innerHTML = `
            <button class="sp-header-back-btn" aria-label="${h('settings.shell.close')}" title="${h('common.back')}">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M15 18l-6-6 6-6"/>
                </svg>
            </button>
            <span class="sp-sidebar-title">${h('settings.shell.title')}</span>
        `;
        sidebarHeader.querySelector('.sp-header-back-btn').addEventListener('click', () => this.close());
        sidebar.appendChild(sidebarHeader);

        const navBody = document.createElement('div');
        navBody.className = 'sp-nav-body';

        // Main groups
        NAV.forEach(group => {
            const groupLabel = document.createElement('div');
            groupLabel.className = 'sp-nav-group';
            groupLabel.textContent = t(group.group);
            navBody.appendChild(groupLabel);

            group.items.forEach(item => {
                navBody.appendChild(this._buildNavItem(item));
            });
        });

        sidebar.appendChild(navBody);

        // Bottom nav — pinned under scrollable groups
        const navBottom = document.createElement('div');
        navBottom.className = 'sp-nav-bottom';

        const bottomGroup = document.createElement('div');
        bottomGroup.className = 'sp-nav-group sp-nav-group-bottom';
        bottomGroup.textContent = t('settings.nav.groupOther');
        navBottom.appendChild(bottomGroup);

        BOTTOM_NAV.forEach(item => {
            navBottom.appendChild(this._buildNavItem(item));
        });

        const logoutBtn = document.createElement('a');
        logoutBtn.href = '/logout';
        logoutBtn.className = 'sp-nav-item sp-logout-btn';
        logoutBtn.innerHTML = `${ICONS.logout}<span class="sp-nav-label">${h('settings.shell.logout')}</span>`;
        // /logout is POST-only + CSRF-protected. Issue a token-bearing POST
        // (csrf.js wraps fetch to attach the header) then redirect to /login.
        logoutBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            this.close();
            try {
                await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
            } catch (_err) { /* ignore — redirect regardless */ }
            window.location.replace('/login');
        });
        navBottom.appendChild(logoutBtn);

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'sp-nav-item sp-close-btn';
        closeBtn.innerHTML = `${ICONS.close}<span class="sp-nav-label">${h('common.close')}</span>`;
        closeBtn.addEventListener('click', () => this.close());
        navBottom.appendChild(closeBtn);

        sidebar.appendChild(navBottom);

        // ── Content area ──────────────────────────────────────────────────────
        const content = document.createElement('div');
        content.className = 'sp-content';
        this._content = content;

        page.appendChild(sidebar);
        page.appendChild(content);
        overlay.appendChild(page);
        document.body.appendChild(overlay);
        this._root = overlay;
    }

    _buildNavItem(item) {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'sp-nav-item';
        el.dataset.id = item.id;
        // Labels are hidden at narrow widths; the tooltip is the only hint left.
        const label = t(item.label);
        el.title = label;
        if (item.gate) el.dataset.gate = item.gate;
        el.innerHTML = `${item.icon}<span class="sp-nav-label">${_esc(label)}</span>`
            + `<span class="sp-nav-count" hidden></span><span class="sp-nav-dot" hidden></span>`;
        el.addEventListener('click', () => this._activate(item.id, { fromNav: true }));
        return el;
    }

    _applyNavGating() {
        const me = window._currentUser || {};
        const isAdmin = me.role === 'admin';
        const connectorsEnabled = !!me.connectors_enabled;
        const isEntra = !!me.is_entra;
        this._root.querySelectorAll('.sp-nav-item[data-gate]').forEach(el => {
            const gate = el.dataset.gate;
            let show = true;
            if (gate === 'admin') show = isAdmin;
            // "My Connections" needs the feature ON and an Entra (SSO) identity.
            if (gate === 'connections') show = connectorsEnabled && isEntra;
            el.hidden = !show;
        });
    }

    // ── Navigation ────────────────────────────────────────────────────────────

    _activate(id, { fromNav = false } = {}) {
        // Leaving a dirty prompt editor needs consent, whichever tab is next.
        if (fromNav && this._promptOpen && id !== this._activeId && !this._confirmLeavePrompt()) return;
        // Clicking "Prompts" in the sidebar always returns to the list; only
        // open()/re-render keep the remembered editor.
        if (id === 'prompts' && fromNav) {
            if (this._promptOpen && !this._confirmLeavePrompt()) return;
            this._promptOpen = null;
        }

        this._activeId = id;
        this._renderSeq += 1;

        // Update active state in sidebar
        this._root.querySelectorAll('.sp-nav-item').forEach(el => {
            const active = el.dataset.id === id;
            el.classList.toggle('is-active', active);
            if (active) el.setAttribute('aria-current', 'page');
            else el.removeAttribute('aria-current');
        });

        // Render content
        if (id === 'general') {
            this._renderGeneral();
        } else if (id === 'metadata-catalog') {
            this._renderMetadataCatalog();
        } else if (id === 'ai-models') {
            this._renderModels();
        } else if (id === 'query-safety') {
            this._renderQuerySafety();
        } else if (id === 'users') {
            this._renderUsers();
        } else if (id === 'integrations') {
            this._renderIntegrations();
        } else if (id === 'my-connections') {
            this._renderMyConnections();
        } else if (id === 'about') {
            this._renderAbout();
        } else if (id === 'prompts') {
            if (this._promptOpen) this._renderPrompt(this._promptOpen);
            else this._renderPromptList();
        }
    }

    // ── Load prompts from API ─────────────────────────────────────────────────

    /**
     * Fetch prompt metadata for every registered prompt. Shared promise so the
     * un-awaited call in open() and the list page never race two requests.
     */
    _loadPrompts() {
        if (this._promptsLoading) return this._promptsLoading;
        this._promptsLoading = (async () => {
            try {
                const res = await fetch('/api/settings/prompts');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const list = await res.json();
                list.forEach(p => {
                    if (!this._prompts[p.name]) {
                        this._prompts[p.name] = { meta: p, content: null, dirty: false, editing: false };
                    } else {
                        this._prompts[p.name].meta = p;
                    }
                });
                this._promptsError = null;
                this._updatePromptsCount();
            } catch (e) {
                this._promptsError = e.message || String(e);
                console.warn('[SettingsPage] could not load prompts:', e);
            } finally {
                this._promptsLoading = null;
            }
        })();
        return this._promptsLoading;
    }

    async _fetchPromptContent(name) {
        if (this._prompts[name] && this._prompts[name].content !== null) return;
        if (!this._prompts[name]) this._prompts[name] = { meta: {}, content: null, dirty: false, editing: false };
        const entry = this._prompts[name];
        entry.loadError = null;
        try {
            const res = await fetch(`/api/settings/prompts/${encodeURIComponent(name)}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            entry.content = data.content;
            entry.meta = data;
        } catch (e) {
            // Leave content null: the editor must not offer Edit/Save over an
            // empty body, or one click would wipe the prompt.
            entry.loadError = e.message || String(e);
            console.warn('[SettingsPage] could not fetch prompt:', name, e);
        }
    }

    /** Number of prompts whose active version is custom (not the file default). */
    _customPromptCount() {
        return Object.values(this._prompts).filter(p => p.meta?.is_custom).length;
    }

    /**
     * Reflect the custom-prompt count on the "Prompts" nav item: a numeric
     * pill at full width, the plain dot when the sidebar is icon-only.
     */
    _updatePromptsCount() {
        const btn = this._root?.querySelector('.sp-nav-item[data-id="prompts"]');
        if (!btn) return;
        const n = this._customPromptCount();
        const pill = btn.querySelector('.sp-nav-count');
        const dot = btn.querySelector('.sp-nav-dot');
        if (pill) {
            pill.hidden = n === 0;
            pill.textContent = String(n);
            pill.setAttribute('aria-label', t('settings.prompts.customCount', { count: n }));
        }
        if (dot) dot.hidden = n === 0;
    }

    // ── AI Models ─────────────────────────────────────────────────────────────

    async _renderModels() {
        if (!this._onTab('ai-models')) return;
        const seq = this._renderSeq;
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.models.title')}</h2>
                <p class="sp-section-desc">${h('settings.models.desc')}</p>
                <div class="sp-models-toolbar">
                    <span class="sp-models-health-summary" id="sp-models-health-summary"></span>
                    <button class="sp-btn-ghost sp-btn-ghost-sm" id="sp-models-recheck">${h('settings.models.recheck')}</button>
                </div>
            </div>
            <div class="sp-card" id="sp-models-list">
                <div class="sp-model-loading">
                    <div class="skeleton" style="height:72px;border-radius:8px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:72px;border-radius:8px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:72px;border-radius:8px;"></div>
                </div>
            </div>`;

        let loadError = null;
        try {
            const res = await fetch('/api/settings/models');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            this._models = await res.json();
        } catch (e) {
            loadError = e;
        }
        if (seq !== this._renderSeq) return;
        if (loadError) {
            const list = document.getElementById('sp-models-list');
            if (list) list.innerHTML = `<p style="color:var(--color-muted)">${h('settings.models.loadFailed')} <bdi dir="ltr">${_esc(loadError.message)}</bdi></p>`;
            return;
        }

        this._renderModelCards();

        const recheck = document.getElementById('sp-models-recheck');
        if (recheck) recheck.addEventListener('click', () => this._loadHealth(true));

        // Pull the (cached) health snapshot and update the dots without blocking
        // the initial render.
        this._loadHealth(false);
    }

    async _loadHealth(refresh) {
        if (!this._onTab('ai-models')) return;
        const seq = this._renderSeq;
        const btn = document.getElementById('sp-models-recheck');
        const summary = document.getElementById('sp-models-health-summary');
        if (btn) { btn.disabled = true; btn.textContent = refresh ? t('settings.models.checking') : btn.textContent; }
        if (summary && refresh) summary.textContent = t('settings.models.probing');
        try {
            const res = await fetch(`/api/settings/models/health${refresh ? '?refresh=true' : ''}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const byName = {};
            (data.models || []).forEach(h => { byName[h.name] = h; });
            (this._models || []).forEach(m => {
                const h = byName[m.name];
                if (h) { m.healthy = h.healthy; m.health_detail = h.detail; }
            });
            // The cache update above is worth keeping; the DOM is not ours anymore.
            if (seq !== this._renderSeq) return;
            this._renderModelCards();
            if (summary) {
                const age = data.checked_age_seconds;
                const ago = (age == null) ? '' :
                    ` · ${t('settings.models.checkedAgo', { when: window.I18n ? window.I18n.formatRelative(Date.now() - age * 1000) : `${Math.round(age)}s` })}`;
                const parts = [t('settings.models.working', { count: Number(data.healthy_count) || 0 })];
                if (data.failing_count) parts.push(t('settings.models.failing', { count: Number(data.failing_count) }));
                if (data.skipped_count) parts.push(t('settings.models.notApplicable', { count: Number(data.skipped_count) }));
                summary.textContent = parts.join(' · ') + ago;
            }
        } catch (e) {
            if (seq === this._renderSeq && summary) summary.textContent = t('settings.models.healthUnavailable');
            console.error('[SettingsPage] health load failed:', e);
        } finally {
            if (seq === this._renderSeq) {
                const b = document.getElementById('sp-models-recheck');
                if (b) { b.disabled = false; b.textContent = t('settings.models.recheck'); }
            }
        }
    }

    _renderModelCards() {
        if (!this._onTab('ai-models')) return;
        const container = document.getElementById('sp-models-list');
        if (!container || !this._models) return;

        const available = this._models.filter(m => m.available);
        const unavailable = this._models.filter(m => !m.available);

        container.innerHTML = [
            available.length ? `<div class="sp-model-group-label">${h('settings.models.available')}</div>` : '',
            ...available.map(m => this._modelCard(m)),
            unavailable.length ? `<div class="sp-model-group-label sp-model-group-label--dim" style="margin-top:16px">${h('settings.models.notConfigured')}</div>` : '',
            ...unavailable.map(m => this._modelCard(m)),
        ].join('');

        // Wire clicks only on available cards
        available.forEach(m => {
            const card = container.querySelector(`[data-model="${m.name}"]`);
            if (!card) return;
            card.addEventListener('click', () => this._selectModel(m.name));
        });
    }

    _modelCard(m) {
        const defaultBadge = m.is_default
            ? `<span class="sp-badge sp-badge-default" style="margin-inline-start:6px;">${h('settings.models.defaultBadge')}</span>` : '';
        const activeMark = m.is_active
            ? `<span class="sp-model-check">
                 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
               </span>` : '';
        const unavailBadge = !m.available
            ? `<span class="sp-model-unavail">${h('settings.models.notConfigured')}</span>` : '';

        // Live credential health: green = probe passed, red = failing (with the
        // reason on hover), grey = not yet probed.
        const state = m.healthy === true ? 'ok'
            : (m.healthy === false ? 'fail' : 'unknown');
        const healthTitle = m.healthy === true
            ? t('settings.models.healthOk')
            : (m.healthy === false
                ? t('settings.models.healthFail', { detail: iso(m.health_detail || t('settings.models.healthFailDefault')) })
                : (m.health_detail || t('settings.models.healthUnknown')));
        const healthDot = `<span class="sp-model-health sp-model-health--${state}" title="${_esc(healthTitle)}"></span>`;

        return `
        <div class="sp-model-card ${m.is_active ? 'is-active' : ''} ${!m.available ? 'is-unavailable' : ''}"
             data-model="${_esc(m.name)}">
            <div class="sp-model-info">
                <div class="sp-model-name">
                    ${healthDot}${_esc(m.display_name)}${defaultBadge}${unavailBadge}
                </div>
                <div class="sp-model-desc">${_esc(m.description)}</div>
            </div>
            ${activeMark}
        </div>`;
    }

    async _selectModel(name) {
        // Optimistic update
        if (this._models) {
            this._models.forEach(m => { m.is_active = (m.name === name); });
            this._renderModelCards();
        }
        try {
            const res = await fetch('/api/settings/models/active', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            _showToast(t('settings.models.switched', { name: iso(name) }), 'success');
        } catch (e) {
            console.error('[SettingsPage] model select failed:', e);
            _showToast(t('settings.models.switchFailed', { detail: iso(e.message) }), 'error');
            // Reload to restore true state
            await this._renderModels();
        }
    }

    // ── Metadata & Catalog ───────────────────────────────────────────────────

    async _renderMetadataCatalog() {
        if (!this._onTab('metadata-catalog')) return;
        const seq = this._renderSeq;
        // Use the active connection from the main app (localStorage) first,
        // then fall back to whatever was last viewed, then first available.
        if (!this._mcpConn) {
            this._mcpConn =
                (typeof getActiveConnection === 'function' && getActiveConnection()) || null;
        }

        // Show skeleton while loading
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.catalog.title')}</h2>
                <p class="sp-section-desc">${h('settings.catalog.desc')}${
                    this._mcpConn ? ` ${h('settings.catalog.showingStats', { connection: iso(this._mcpConn) })}` : ''}</p>
            </div>
            <div class="sp-card" style="padding:24px">
                <div class="skeleton" style="height:180px;border-radius:8px;"></div>
            </div>`;

        // If we still don't have a connection, fetch the list once to get the first.
        if (!this._mcpConn) {
            try {
                const r = await fetch('/api/connections');
                if (r.ok) {
                    const d = await r.json();
                    const conns = d.connections || [];
                    if (conns.length) this._mcpConn = conns[0].source_key;
                }
            } catch (e) { /* ignore */ }
        }

        // Load status
        await this._mcpLoadStatus();
        if (seq !== this._renderSeq) return;
        this._mcpRender();
    }

    async _mcpLoadStatus() {
        try {
            const qs = this._mcpConn ? `?connection=${encodeURIComponent(this._mcpConn)}` : '';
            const r  = await fetch(`/api/mcp/status${qs}`, { credentials: 'same-origin' });
            if (r.ok) this._mcpStatus = await r.json();
        } catch (e) {
            console.warn('[MCP] status load failed:', e);
        }
    }

    /** Merge a health-check API response into cached status before reload. */
    _applyMcpHealthResponse(serverId, data) {
        if (!data?.server || !this._mcpStatus?.servers) return;
        const idx = this._mcpStatus.servers.findIndex(s => s.id === serverId);
        if (idx === -1) return;
        this._mcpStatus.servers[idx] = { ...this._mcpStatus.servers[idx], ...data.server };
    }

    _mcpClearToolInspector() {
        this._mcpToolsServerId = null;
        this._mcpTools = null;
        this._mcpSelectedTool = null;
        this._mcpToolArgs = null;
        this._mcpToolArgsByTool = {};
        this._mcpToolInputMode = 'guided';
        this._mcpToolResult = null;
        this._mcpToolResultView = 'content';
    }

    _mcpRender() {
        // Every MCP action handler (activate, delete, health check, tool call…)
        // re-renders through here after its own await, so this single check is
        // what keeps a slow MCP call from painting over another tab.
        if (!this._onTab('metadata-catalog')) return;
        const S   = this._mcpStatus || {};
        const src = S.catalog_source || 'db';

        // If we have a connection but no DB stats yet, trigger a background reload.
        if (this._mcpConn && S.connection !== this._mcpConn && !this._mcpReloading) {
            this._mcpReloading = true;
            this._mcpLoadStatus().then(() => {
                this._mcpReloading = false;
                this._mcpRender();
            });
        } else {
            this._mcpReloading = false;
        }

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.catalog.title')}</h2>
                <p class="sp-section-desc">${h('settings.catalog.desc')}${
                    this._mcpConn ? ` ${h('settings.catalog.showingStats', { connection: iso(this._mcpConn) })}` : ''}</p>
            </div>

            <div class="sp-card" id="mc-src-card">
                <div class="sp-row sp-row--seg" style="padding-bottom:0">
                    <div>
                        <div class="sp-row-label">${h('settings.catalog.source')}</div>
                        <div class="sp-row-help">${h('settings.catalog.sourceHelp')}</div>
                    </div>
                    <div class="mc-seg" id="mc-seg">
                        <button data-src="db" class="${src==='db'?'is-active':''}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg> ${h('settings.catalog.sourceDb')}</button>
                        <button data-src="mcp" class="${src==='mcp'?'is-active':''}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/></svg> ${h('settings.catalog.sourceMcp')}</button>
                    </div>
                </div>

                <!-- DB source panel -->
                <div id="mc-db-panel" class="mc-panel${src!=='db'?' mc-hidden':''}">
                    ${this._mcpDbPanel(S.db || {})}
                </div>

                <!-- MCP source panel -->
                <div id="mc-mcp-panel" class="mc-panel${src!=='mcp'?' mc-hidden':''}">
                    ${this._mcpMcpPanel(S)}
                </div>
            </div>

            <!-- Cache TTL -->
            ${this._mcpCacheCard(S)}
        `;

        this._mcpWireEvents(src, S);
    }

    _mcpDbPanel(db) {
        const hit    = db.cache_status?.hit;
        const expiry = db.cache_status?.expires_in_s;
        const statusLabel = hit
            ? `<span class="mc-status mc-status-ok"><span class="mc-dot"></span> ${h('settings.catalog.db.synced')}${expiry ? ` · ${h('settings.catalog.db.remaining', { time: _humanTime(expiry) })}` : ''}</span>`
            : `<span class="mc-status mc-status-muted"><span class="mc-dot"></span> ${h('settings.catalog.db.miss')}</span>`;

        return `
            <div class="mc-info-grid">
                <div><span class="mc-info-k">${h('settings.catalog.db.provider')}</span><span class="mc-info-v">Schema Modeler</span></div><!-- i18n-ignore: product name -->
                <div><span class="mc-info-k">${h('settings.catalog.db.database')}</span><span class="mc-info-v mc-mono">${_esc(db.database || '—')}</span></div>
                <div><span class="mc-info-k">${h('settings.catalog.db.tablesColumns')}</span><span class="mc-info-v">${db.tables ?? '—'} · ${db.columns ?? '—'}</span></div>
                <div><span class="mc-info-k">${h('settings.catalog.db.termsPairs')}</span><span class="mc-info-v">${db.business_terms ?? '—'} · ${db.knowledge_pairs ?? '—'}</span></div>
            </div>
            <div class="mc-info-foot">
                ${statusLabel}
                <span style="flex:1"></span>
                <button class="sp-btn-ghost" id="mc-refresh-btn">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><polyline points="21 3 21 8 16 8"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><polyline points="3 21 3 16 8 16"/></svg>
                    ${h('settings.catalog.refreshMetadata')}
                </button>
            </div>`;
    }

    _mcpMcpPanel(S) {
        const servers    = S.servers || [];
        const activeId   = S.active_server_id;
        const activeServ = servers.find(s => s.id === activeId) || null;

        const listRows = servers.length ? servers.map(s => {
            const hStatus = s.health?.status;
            const dotCls  = hStatus === 'healthy' ? 'mc-srv-dot-ok'
                          : hStatus === 'degraded' ? 'mc-srv-dot-warn'
                          : hStatus === 'down'     ? 'mc-srv-dot-err'
                          : '';
            const isAct   = s.id === activeId;
            // cursor: pointer indicates clickable; active row has accent highlight
            return `<div class="mc-srv-row${isAct ? ' mc-srv-row-active' : ''}" data-srv-id="${s.id}" style="cursor:pointer">
                <span class="mc-srv-dot ${dotCls}" title="${_esc(hStatus||t('settings.catalog.mcp.notChecked'))}"></span>
                <div class="mc-srv-main">
                    <div class="mc-srv-name"><span class="mc-srv-name-txt">${_esc(s.server_name)}</span>${isAct?`<span class="mc-srv-tag">${h('settings.catalog.mcp.active')}</span>`:''}</div>
                    <div class="mc-srv-ep">${_esc(s.endpoint)}</div>
                </div>
                <span class="mc-srv-badge">${_esc((s.transport||'').toUpperCase())}</span>
                <div class="mc-srv-actions">
                    <button class="mc-srv-ico" data-edit="${s.id}" title="${h('common.edit')}"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg></button>
                    <button class="mc-srv-ico mc-srv-del" data-del="${s.id}" title="${h('common.delete')}" ${servers.length<=1?'disabled':''}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
                </div>
            </div>`;
        }).join('') : `<div class="mc-empty">${h('settings.catalog.mcp.empty')}</div>`;

        const formHtml = this._mcpEditing ? this._mcpFormHtml() : '';
        const healthHtml = (!this._mcpEditing && activeServ) ? this._mcpHealthSection(activeServ) : '';

        return `
            <div class="mc-srv-listhead">
                <span class="mc-srv-listtitle">${h('settings.catalog.mcp.servers')} <span class="mc-srv-count">${servers.length}</span></span>
                <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-add-btn">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> ${h('settings.catalog.mcp.addServer')}
                </button>
            </div>
            <div class="mc-srv-list">${listRows}</div>
            ${formHtml}
            ${healthHtml}
            <div class="mc-callout">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                <p>${h('settings.catalog.mcp.callout')}</p>
            </div>`;
    }

    _mcpFormHtml() {
        const d      = this._mcpDraft || {};
        const isNew  = this._mcpEditing === 'new';
        const bearer = d.auth_type === 'bearer';
        const saveOk = (d.server_name||'').trim() && (d.endpoint||'').trim();
        return `
        <div class="mc-form">
            <div class="mc-form-head">${isNew ? h('settings.catalog.form.addTitle') : h('settings.catalog.form.editTitle')}</div>
            <div class="mc-field">
                <label class="mc-field-label">${h('settings.catalog.form.serverName')} <span class="mc-req">*</span></label>
                <input id="mc-f-name" class="mc-input" value="${_esc(d.server_name||'')}" placeholder="jeen-catalog-mcp" />
            </div>
            <div class="mc-field">
                <label class="mc-field-label">${h('settings.catalog.form.endpoint')} <span class="mc-req">*</span></label>
                <input id="mc-f-ep" class="mc-input" dir="ltr" value="${_esc(d.endpoint||'')}" placeholder="https://mcp.jeen.internal/catalog" />
                <div class="mc-field-help">${h('settings.catalog.form.endpointHelp')} <code>npx @jeen/catalog-mcp</code></div>
            </div>
            <div class="mc-field-row">
                <div class="mc-field">
                    <label class="mc-field-label">${h('settings.catalog.form.transport')}</label>
                    <div class="mc-mini-seg" id="mc-f-transport">
                        ${['stdio','sse','http'].map(t=>`<button data-t="${t}" class="${(d.transport||'http')===t?'is-active':''}">${t.toUpperCase()}</button>`).join('')}
                    </div>
                </div>
                <div class="mc-field">
                    <label class="mc-field-label">${h('settings.catalog.form.auth')}</label>
                    <select id="mc-f-auth" class="settings-select" style="width:100%;min-width:0">
                        <option value="none"${(d.auth_type||'none')==='none'?' selected':''}>${h('common.none')}</option>
                        <option value="bearer"${d.auth_type==='bearer'?' selected':''}>${h('settings.catalog.form.bearer')}</option>
                        <option value="oauth"${d.auth_type==='oauth'?' selected':''}>OAuth 2.1</option>
                    </select>
                </div>
            </div>
            ${bearer ? `<div class="mc-field">
                <label class="mc-field-label">${h('settings.catalog.form.bearer')}${d.has_token ? ` <span class="sp-conn-pill on">${h('common.saved')}</span>` : ''}</label>
                <input id="mc-f-token" class="mc-input" type="password" dir="ltr" value="" placeholder="${d.has_token ? h('settings.catalog.form.tokenReplace') : h('settings.catalog.form.tokenPaste')}" autocomplete="new-password" />
                <div class="mc-field-help">${d.has_token ? h('settings.catalog.form.tokenSavedHelp') : h('settings.catalog.form.tokenHelp')}</div>
            </div>` : ''}
            <div class="mc-form-foot">
                <button class="sp-btn-ghost" id="mc-f-cancel">${h('common.cancel')}</button>
                <button class="sp-btn-primary-sm" id="mc-f-save" ${saveOk?'':'disabled'}>${isNew ? h('settings.catalog.mcp.addServer') : h('settings.catalog.form.saveChanges')}</button>
            </div>
        </div>`;
    }

    _catalogNeeds() {
        // Single source of truth: the backend's canonical needs (from /status).
        // Fall back to a local default so the panel still renders if absent.
        const fromApi = this._mcpStatus && this._mcpStatus.catalog_needs;
        const list = (Array.isArray(fromApi) && fromApi.length) ? fromApi : [
            { key: 'list_sources',       label: 'List connections',                 required: true  },
            { key: 'list_tables',        label: 'Catalog prompt (tables, columns)', required: true  },
            { key: 'list_relationships', label: 'Relationships',                    required: false },
            { key: 'business_glossary',  label: 'Business terms &amp; glossary',    required: false },
        ];
        return list.map(n => ({ key: n.key, label: n.label, req: !!n.required }));
    }

    _mcpHealthSection(server) {
        const h       = server.health;
        const testing = this._mcpTesting;
        const btnLabel = testing
            ? `<svg class="mc-spin" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><polyline points="21 3 21 8 16 8"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><polyline points="3 21 3 16 8 16"/></svg> Checking…`
            : h ? _escAttr(t('settings.catalog.health.rerun')) : _escAttr(t('settings.catalog.health.test'));

        // ─ Status labels: Healthy / Degraded / Unreachable (never raw lowercase) ──
        const _statusLabel = { healthy: _escAttr(t('settings.catalog.health.healthy')), degraded: _escAttr(t('settings.catalog.health.degraded')), down: _escAttr(t('settings.catalog.health.unreachable')) };
        const checked = (at) => _escAttr(t('settings.catalog.health.checked', { when: _relativeTime(at) }));
        const statusText = testing
            ? `<span class="mc-status mc-status-checking"><span class="mc-dot mc-dot-pulse"></span> ${_escAttr(t('settings.catalog.health.handshaking', { name: iso(server.server_name) }))}</span>`
            : h?.status === 'healthy'
                ? `<span class="mc-status mc-status-ok"><span class="mc-dot"></span> ${_statusLabel.healthy} · ${h.latency_ms||0}ms · ${checked(h.checked_at)}</span>`
            : h?.status === 'degraded'
                ? `<span class="mc-status mc-status-warn"><span class="mc-dot"></span> ${_statusLabel.degraded} · ${h.latency_ms||0}ms · ${checked(h.checked_at)}</span>`
            : h?.status === 'down'
                ? `<span class="mc-status mc-status-err"><span class="mc-dot"></span> ${_statusLabel.down} · ${checked(h.checked_at)}</span>`
            : `<span class="mc-status mc-status-muted"><span class="mc-dot"></span> ${_escAttr(t('settings.catalog.health.serverNotChecked', { name: iso(server.server_name) }))}</span>`;

        // ─ Friendly labels for tool→need chips ─────────────────────────────────
        const _NEED_LABEL = {
            list_sources:         'List sources',
            list_tables:          'List tables',
            describe_table:       'Describe table / columns',
            list_relationships:   'Relationships',
            business_glossary:    'Business terms',
            knowledge_pairs:      'Knowledge pairs',
        };

        // ─ Health diagnostics card (only when health data exists) ────────────────
        let diagnosticsHtml = '';
        if (testing) {
            diagnosticsHtml = `
            <div class="mc-health mc-health-pending">
                <div class="mc-health-head">
                    <span class="mc-health-badge mc-health-checking"><span class="mc-dot mc-dot-pulse"></span> ${_escAttr(t('settings.models.checking'))}</span>
                    <span class="mc-health-impl">${_esc(server.server_name)}</span>
                </div>
                <p class="mc-health-pending-msg">${_escAttr(t('settings.catalog.health.pending'))}</p>
            </div>`;
        } else if (h && h.status !== 'down') {
            const cells = [
                [t('settings.catalog.health.protocol'),  h.protocol || '—'],
                ['SDK',       h.sdk || '—'],
                [t('settings.catalog.health.handshake'), (h.latency_ms||0) + 'ms'],
                ['Ping',      (h.ping_ms||0) + 'ms'],
                [t('settings.catalog.health.uptime'),    h.uptime || '—'],
                [t('settings.catalog.form.transport'), `${(server.transport||'').toUpperCase()}${h.tls ? ' · ' + h.tls : ''}`],
            ];
            const tools   = h.tools || [];
            const resCnt  = typeof h.resources === 'number' ? h.resources : null;
            const prmCnt  = typeof h.prompts   === 'number' ? h.prompts   : null;
            const capsMeta = (resCnt !== null || prmCnt !== null)
                ? `<span class="mc-cap-meta">${[resCnt !== null ? _escAttr(t('settings.catalog.health.resources', { count: resCnt })) : '', prmCnt !== null ? _escAttr(t('settings.catalog.health.prompts', { count: prmCnt })) : ''].filter(Boolean).join(' · ')}</span>`
                : '';

            // Capabilities: always show all four — on (✓) or off (×)
            const CHECK = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
            const CROSS = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
            const caps = ['tools','resources','prompts','logging'].map(c => {
                const on = (h.capabilities||[]).includes(c);
                return `<span class="mc-cap-chip ${on ? '' : 'mc-cap-off'}">${on ? CHECK : CROSS} ${_esc(c)}</span>`;
            }).join('');

            // Tool rows with friendly need labels
            const toolRows = tools.map(tool =>
                `<div class="mc-tool-row">
                    <svg class="mc-tool-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
                    <div class="mc-tool-main">
                        <div class="mc-tool-name">${_esc(tool.name)}</div>
                        <div class="mc-tool-desc">${_esc(tool.description||'')}</div>
                    </div>
                    ${tool.need
                        ? `<span class="mc-tool-need">↳ ${_esc(_NEED_LABEL[tool.need] || tool.need)}</span>`
                        : `<span class="mc-tool-need mc-map-muted">${_escAttr(t('settings.catalog.health.unmapped'))}</span>`}
                </div>`
            ).join('');

            // Degraded warning banner (shown above diagnostics grid)
            const noteHtml = (h.status === 'degraded' && h.note)
                ? `<div class="mc-note">⚠ ${_esc(h.note)}</div>`
                : '';

            diagnosticsHtml = `
            <div class="mc-health">
                <div class="mc-health-head">
                    <span class="mc-health-badge mc-health-${h.status}">
                        <span class="mc-dot"></span> ${_statusLabel[h.status] || _esc(h.status)}
                    </span>
                    <span class="mc-health-impl">${_esc(server.server_name)} · v${_esc(h.server_version||'')}</span>
                    <span class="mc-health-when">${checked(h.checked_at)}</span>
                </div>
                ${noteHtml}
                <div class="mc-health-grid">${cells.map(([k,v]) =>
                    `<div class="mc-health-cell"><span class="mc-hk">${_esc(k)}</span><span class="mc-hv">${_esc(String(v))}</span></div>`
                ).join('')}</div>
                <div class="mc-health-sub">${_escAttr(t('settings.catalog.health.capabilities'))}</div>
                <div class="mc-cap-row">${caps}${capsMeta}</div>
                <div class="mc-health-sub">${_escAttr(t('settings.catalog.health.toolsExposed'))} <span class="mc-srv-count">${tools.length}</span></div>
                <div class="mc-tools">${toolRows || `<div style="padding:14px;color:var(--color-faint);font-size:.8rem">${_escAttr(t('settings.catalog.health.noTools'))}</div>`}</div>
            </div>`;

        } else if (h?.status === 'down') {
            // Unreachable: proper card with header, not a bare error box
            diagnosticsHtml = `
            <div class="mc-health">
                <div class="mc-health-head">
                    <span class="mc-health-badge mc-health-down"><span class="mc-dot"></span> ${_statusLabel.down}</span>
                    <span class="mc-health-impl">${_esc(server.endpoint)}</span>
                    <span class="mc-health-when">${checked(h.checked_at)}</span>
                </div>
                <div class="mc-err-box" style="margin:12px 14px 14px" dir="auto">${_esc(h.error || t('settings.catalog.health.failed'))}</div>
            </div>`;
        }

        // ─ Catalog tool mapping ─ ALWAYS VISIBLE ──────────────────────────────────
        // Needs come from the backend (single source of truth) so the required
        // gate here always matches what activation actually enforces.
        const NEEDS = this._catalogNeeds();
        const tools = h?.tools || [];
        const mapRows = NEEDS.map(n => {
            const matched = tools.find(x => x.need === n.key);
            return `<div class="mc-map-row">
                <span class="mc-map-need">${n.label}${n.req ? '<span class="mc-req"> *</span>' : ''}</span>
                ${matched
                    ? `<span class="mc-map-tool">${_esc(matched.name)}</span>`
                    : `<span class="mc-map-tool mc-map-muted">${_escAttr(t('settings.catalog.mapping.awaiting'))}</span>`}
            </div>`;
        }).join('');

        // Required-need unmapped warning (only after a health check)
        const missingReq = h ? NEEDS.filter(n => n.req && !tools.find(x => x.need === n.key)) : [];
        const missingWarn = missingReq.length
            ? `<div class="mc-err-box" style="margin-top:8px">
                <b>${missingReq.map(n => n.label).join(', ')}</b> — ${_escAttr(t('settings.catalog.mapping.requiredUnmapped'))}
               </div>`
            : '';
        const toolInspectorHtml = this._mcpToolInspector(server, tools);

        return `
        <div class="mc-active-srv">
            <div class="mc-test-bar">
                <button class="sp-btn-ghost" id="mc-test-btn" ${testing ? 'disabled' : ''}>${btnLabel}</button>
                ${statusText}
            </div>
            ${diagnosticsHtml}
            ${toolInspectorHtml}
            <div class="mc-field" style="margin-top:14px">
                <label class="mc-field-label">${_escAttr(t('settings.catalog.mapping.title'))}</label>
                <div class="mc-map">${mapRows}</div>
                ${missingWarn}
                <div class="mc-field-help">${_escAttr(t('settings.catalog.mapping.help'))} <code>tools/list</code></div>
            </div>
        </div>`;
    }

    _mcpToolInspector(server, healthTools = []) {
        const liveTools = (this._mcpToolsServerId === server.id && Array.isArray(this._mcpTools))
            ? this._mcpTools
            : null;
        const tools = liveTools || healthTools || [];
        const selectedName = (
            this._mcpSelectedTool && tools.some(t => t.name === this._mcpSelectedTool)
        ) ? this._mcpSelectedTool : (tools[0]?.name || '');
        const tool = tools.find(t => t.name === selectedName) || null;
        const inputSchema = tool?.input_schema || tool?.inputSchema || {};
        const outputSchema = tool?.output_schema || tool?.outputSchema || {};
        const defaultArgs = _prettyJson(_sampleArgsFromSchema(inputSchema));
        const argsText = this._mcpToolArgsByTool[selectedName] ?? this._mcpToolArgs ?? defaultArgs;
        const hasGuidedFields = _mcpHasGuidedFields(inputSchema);
        const inputMode = hasGuidedFields ? this._mcpToolInputMode : 'json';
        const args = _mcpParseArguments(argsText) ?? _sampleArgsFromSchema(inputSchema);
        const inputErrors = _mcpInputValidationErrors(inputSchema, args);
        const resultHtml = this._mcpToolResult
            ? _mcpToolResultViewer(this._mcpToolResult, this._mcpToolResultView)
            : '';
        const schemaSource = liveTools ? t('settings.catalog.inspector.sourceLive') : t('settings.catalog.inspector.sourceHealth');
        const loadLabel = this._mcpToolsLoading
            ? t('settings.catalog.inspector.loading')
            : liveTools ? t('settings.catalog.inspector.refreshSchemas') : t('settings.catalog.inspector.loadSchemas');

        return `
        <div class="mc-tool-inspector">
            <div class="mc-tool-inspector-head">
                <div>
                    <div class="mc-field-label">${h('settings.catalog.inspector.title')}</div>
                    <div class="mc-field-help">${h('settings.catalog.inspector.help')}</div>
                </div>
                <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tools-load-btn" ${this._mcpToolsLoading ? 'disabled' : ''}>${_esc(loadLabel)}</button>
            </div>
            ${tools.length ? `
                <div class="mc-tool-test-grid">
                    <div class="mc-field">
                        <label class="mc-field-label">${h('settings.catalog.inspector.function')} <span class="mc-badge">${_esc(schemaSource)}</span></label>
                        <select class="settings-select" id="mc-tool-select">
                            ${tools.map(t => `<option value="${_esc(t.name)}"${t.name === selectedName ? ' selected' : ''}>${_esc(t.name)}</option>`).join('')}
                        </select>
                        <div class="mc-field-help">${_esc(tool?.description || t('settings.catalog.inspector.noDescription'))}</div>
                        ${_mcpToolAnnotationBadges(tool)}
                    </div>
                    <div class="mc-tool-schema-pair">
                        <div>
                            <div class="mc-field-label">${h('settings.catalog.inspector.inputSchema')}</div>
                            <pre class="mc-schema-pre">${_esc(_prettyJson(inputSchema || {}))}</pre>
                        </div>
                        <div>
                            <div class="mc-field-label">${h('settings.catalog.inspector.outputSchema')}</div>
                            <pre class="mc-schema-pre">${_esc(_prettyJson(outputSchema || {}))}</pre>
                        </div>
                    </div>
                    <div class="mc-field">
                        <label class="mc-field-label">${h('settings.catalog.inspector.testArguments')}</label>
                        <div class="mc-input-mode-tabs" role="tablist" aria-label="${h('settings.catalog.inspector.editorMode')}">
                            ${hasGuidedFields ? _mcpInputModeButton('guided', h('settings.catalog.inspector.guided'), inputMode) : ''}
                            ${_mcpInputModeButton('json', h('settings.catalog.inspector.advancedJson'), inputMode)}
                        </div>
                        ${inputMode === 'guided'
                            ? _mcpGuidedArgumentsForm(inputSchema, args)
                            : `<textarea id="mc-tool-args" class="mc-json-area" spellcheck="false">${_esc(argsText)}</textarea>`}
                        ${inputErrors.length
                            ? `<div class="mc-input-errors">${inputErrors.map(error => `<div>${_esc(error)}</div>`).join('')}</div>`
                            : `<div class="mc-field-help">${h('settings.catalog.inspector.optionalHelp')}</div>`}
                        <div class="mc-tool-actions">
                            <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tool-sample-btn">${h('settings.catalog.inspector.useSample')}</button>
                            <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tool-copy-input-btn">${h('settings.catalog.inspector.copyInput')}</button>
                            <button class="sp-btn-primary-sm" id="mc-tool-call-btn" ${this._mcpToolCalling || this._mcpTesting ? 'disabled' : ''}>${this._mcpToolCalling ? h('conversation.proposal.running') : h('settings.catalog.inspector.run')}</button>
                        </div>
                    </div>
                    ${resultHtml}
                </div>
            ` : `
                <div class="mc-empty mc-tool-empty">${h('settings.catalog.inspector.empty')}</div>
            `}
        </div>`;
    }

    _mcpCacheCard(S) {
        const src     = S.catalog_source || 'db';
        const ttl     = S.cache_ttl_seconds ?? 900;  // per-connection TTL from API
        const ttlOpts = [
            [0,     t('settings.catalog.cache.live')],
            [300,   t('settings.catalog.minutes', { count: 5 })],
            [900,   t('settings.catalog.minutes', { count: 15 })],
            [3600,  t('settings.catalog.hours', { count: 1 })],
            [86400, t('settings.catalog.hours', { count: 24 })],
        ];
        const badgeLabel = src === 'mcp' ? 'MCP' : 'DB';
        const hasActive  = !!S.active_server_id;
        const ttlDisabled = !hasActive;  // caching is MCP-only; needs an active server
        const isLive     = ttl === 0;
        const cacheHit   = src === 'db'
            ? (S.db?.cache_status?.hit ?? false)
            : (S.mcp_cache?.cache_hit ?? false);
        const pillHtml = isLive
            ? `<span class="mc-cache-pill mc-cache-live"><span class="mc-cache-dot"></span> ${h('settings.catalog.cache.nextRun')} · <b>${h('settings.catalog.cache.liveFetch')}</b></span>
               <span class="mc-cache-pill">TTL <b>${h('common.none')}</b></span>
               <span class="mc-cache-pill">${h('settings.catalog.cache.refetchNote')}</span>`
            : `<span class="mc-cache-pill ${cacheHit ? 'mc-cache-hit' : ''}"><span class="mc-cache-dot"></span> ${h('settings.catalog.cache.nextRun')} · <b>${cacheHit ? h('settings.catalog.cache.hit') : h('settings.catalog.cache.miss')}</b></span>
               <span class="mc-cache-pill">TTL <b>${_formatTtl(ttl)}</b></span>
               <span class="mc-cache-pill">${h('settings.catalog.cache.invalidate')} → <b>${h('settings.catalog.refreshMetadata')}</b></span>`;
        return `
        <div class="mc-cache-card">
            <div class="sp-row" style="border:none;padding-bottom:0">
                <div>
                    <div class="sp-row-label">${h('settings.catalog.cache.title')} <span class="mc-badge">${badgeLabel}</span></div>
                    <div class="sp-row-help">${h('settings.catalog.cache.help')} <code>catalog.load</code></div>
                </div>
                <select class="settings-select" id="mc-ttl-sel"${ttlDisabled ? ' disabled' : ''}>
                    ${ttlOpts.map(([v,l])=>`<option value="${v}"${ttl===v?' selected':''}>${_esc(l)}</option>`).join('')}
                </select>
            </div>
            ${ttlDisabled ? `<div class="mc-cache-state"><span class="mc-cache-pill">${h('settings.catalog.cache.mcpOnly')}</span></div>` : `<div class="mc-cache-state" id="mc-cache-pills">${pillHtml}</div>`}
        </div>`;
    }

    // ── MCP event wiring ─────────────────────────────────────────────────────

    _mcpWireEvents(src, S) {
        const $ = id => this._content.querySelector(id);

        // Source segment switch — global (one source for the whole app)
        this._content.querySelectorAll('#mc-seg button').forEach(btn => {
            btn.addEventListener('click', async () => {
                const newSrc = btn.dataset.src;
                if (newSrc === src) return;
                try {
                    const r = await fetch('/api/mcp/catalog-source', {
                        method: 'PUT',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({catalog_source: newSrc}),
                    });
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                } catch (e) { _showToast(t('settings.catalog.toasts.switchFailed', { detail: iso(e.message) }), 'error'); }
            });
        });

        // Refresh metadata button (DB panel)
        const refreshBtn = $('#mc-refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', async () => {
                const orig = refreshBtn.innerHTML;
                refreshBtn.disabled = true;
                refreshBtn.textContent = t('settings.catalog.refreshing');
                try {
                    const qs = this._mcpConn ? `?connection=${encodeURIComponent(this._mcpConn)}` : '';
                    await fetch(`/api/mcp/refresh${qs}`, {method:'POST'});
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast(t('settings.catalog.cacheCleared'), 'success');
                } catch (e) { _showToast(t('settings.catalog.toasts.refreshFailed', { detail: iso(e.message) }), 'error'); }
                finally { refreshBtn.disabled = false; refreshBtn.innerHTML = orig; }
            });
        }

        // Add server button
        const addBtn = $('#mc-add-btn');
        if (addBtn) {
            addBtn.addEventListener('click', () => {
                this._mcpEditing = 'new';
                this._mcpDraft   = {server_name:'', endpoint:'', transport:'http', auth_type:'none'};
                this._mcpRender();
            });
        }

        // Server list row click = ACTIVATE server
        this._content.querySelectorAll('.mc-srv-row').forEach(row => {
            row.addEventListener('click', async e => {
                if (e.target.closest('[data-edit],[data-del]') || this._mcpEditing) return;
                const id = Number(row.dataset.srvId);
                if (!id || (S.active_server_id === id)) return;
                try {
                    const r = await fetch(`/api/mcp/servers/${id}/activate`, {method:'POST'});
                    if (!r.ok) { const err = await r.json().catch(()=>({})); throw new Error(err.detail||`HTTP ${r.status}`); }
                    this._mcpClearToolInspector();
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                } catch (e2) { _showToast(t('settings.catalog.toasts.selectFailed', { detail: iso(e2.message) }), 'error'); }
            });
        });

        // Edit buttons
        this._content.querySelectorAll('[data-edit]').forEach(btn => {
            btn.addEventListener('click', e => {
                e.stopPropagation();
                const srv = (S.servers||[]).find(s => s.id === Number(btn.dataset.edit));
                if (!srv) return;
                this._mcpEditing = srv.id;
                this._mcpDraft   = {id: srv.id, has_token: !!srv.has_token,
                                    server_name: srv.server_name, endpoint: srv.endpoint,
                                    transport: srv.transport, auth_type: srv.auth_type};
                this._mcpRender();
            });
        });

        // Delete buttons
        this._content.querySelectorAll('[data-del]').forEach(btn => {
            btn.addEventListener('click', async e => {
                e.stopPropagation();
                const id = Number(btn.dataset.del);
                if (!confirm(t('settings.catalog.deleteServerConfirm'))) return;
                try {
                    const r = await fetch(`/api/mcp/servers/${id}`, {method:'DELETE'});
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    if (this._mcpToolsServerId === id) this._mcpClearToolInspector();
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast(t('settings.catalog.serverDeleted'), 'info');
                } catch (e2) { _showToast(t('settings.catalog.toasts.deleteFailed', { detail: iso(e2.message) }), 'error'); }
            });
        });

        // Form: cancel
        const cancelBtn = $('#mc-f-cancel');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => {
                this._mcpEditing = null; this._mcpDraft = null; this._mcpRender();
            });
        }

        // Form: transport mini-seg
        this._content.querySelectorAll('#mc-f-transport button').forEach(btn => {
            btn.addEventListener('click', () => { this._mcpDraft.transport = btn.dataset.t; this._mcpRender(); });
        });

        // Form: live-validate Save button
        ['#mc-f-name', '#mc-f-ep'].forEach(sel => {
            const el = $(sel);
            if (el) el.addEventListener('input', () => {
                if (sel === '#mc-f-name') this._mcpDraft.server_name = el.value;
                else                      this._mcpDraft.endpoint     = el.value;
                const saveBtn = $('#mc-f-save');
                if (saveBtn) saveBtn.disabled = !(this._mcpDraft.server_name?.trim() && this._mcpDraft.endpoint?.trim());
            });
        });
        const authSel = $('#mc-f-auth');
        if (authSel) authSel.addEventListener('change', () => { this._mcpDraft.auth_type = authSel.value; this._mcpRender(); });

        // Form: save
        const saveBtn = $('#mc-f-save');
        if (saveBtn) {
            saveBtn.addEventListener('click', async () => {
                const d = this._mcpDraft;
                if (!d.server_name?.trim() || !d.endpoint?.trim()) return;
                const token = $('#mc-f-token')?.value || null;
                const body  = {...d};
                delete body.id;          // UI-only fields, not part of the API contract
                delete body.has_token;
                if (token) body.bearer_token = token;
                try {
                    const isNew = this._mcpEditing === 'new';
                    const url   = isNew ? '/api/mcp/servers' : `/api/mcp/servers/${this._mcpEditing}`;
                    const r = await fetch(url, {
                        method: isNew ? 'POST' : 'PUT',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify(body),
                    });
                    if (!r.ok) { const err = await r.json().catch(()=>({})); throw new Error(err.detail||`HTTP ${r.status}`); }
                    this._mcpEditing = null; this._mcpDraft = null;
                    this._mcpStatus  = null;
                    this._mcpClearToolInspector();
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast(isNew ? t('settings.catalog.serverAdded') : t('settings.catalog.serverUpdated'), 'success');
                } catch (e2) { _showToast(t('settings.saveFailed', { detail: iso(e2.message) }), 'error'); }
            });
        }

        // Health check button
        const testBtn = $('#mc-test-btn');
        if (testBtn) {
            testBtn.addEventListener('click', async () => {
                const activeServ = (S.servers||[]).find(s => s.is_active);
                if (!activeServ) return;
                this._mcpTesting = true;
                this._mcpClearToolInspector();
                this._mcpRender();
                let toastMsg = null;
                let toastType = 'error';
                try {
                    const r = await fetch(
                        `/api/mcp/servers/${activeServ.id}/health-check`,
                        { method: 'POST', credentials: 'same-origin' },
                    );
                    const data = await r.json().catch(() => ({}));
                    this._applyMcpHealthResponse(activeServ.id, data);
                    await this._mcpLoadStatus();

                    if (data.ok === false) {
                        toastMsg = data.error || 'Health check failed';
                    } else if (!r.ok) {
                        toastMsg = data.error || data.detail || `HTTP ${r.status}`;
                    } else {
                        const freshTools = data.health?.tools || data.server?.health?.tools || [];
                        if (freshTools.length) {
                            this._mcpToolsServerId = activeServ.id;
                            this._mcpTools = freshTools;
                            this._mcpSelectedTool = freshTools[0].name || null;
                            this._mcpToolArgsByTool = {};
                            this._mcpToolArgs = _prettyJson(
                                _sampleArgsFromSchema(
                                    freshTools[0].input_schema || freshTools[0].inputSchema || {}
                                )
                            );
                            if (this._mcpSelectedTool) {
                                this._mcpToolArgsByTool[this._mcpSelectedTool] = this._mcpToolArgs;
                            }
                            this._mcpToolInputMode = _mcpHasGuidedFields(
                                freshTools[0].input_schema || freshTools[0].inputSchema || {}
                            ) ? 'guided' : 'json';
                            this._mcpToolResult = null;
                        }
                        toastType = 'success';
                        toastMsg = 'Health check complete';
                    }
                } catch (e2) {
                    toastMsg = e2.message || 'Health check failed';
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                } finally {
                    this._mcpTesting = false;
                    this._mcpRender();
                    if (toastMsg) _showToast(toastMsg, toastType);
                }
            });
        }

        // Tool inspector: fetch live schemas from tools/list.
        const loadToolsBtn = $('#mc-tools-load-btn');
        if (loadToolsBtn) {
            loadToolsBtn.addEventListener('click', async () => {
                const activeServ = (S.servers||[]).find(s => s.is_active);
                if (!activeServ) return;
                this._mcpToolsLoading = true;
                this._mcpToolResult = null;
                this._mcpRender();
                try {
                    const r = await fetch(`/api/mcp/servers/${activeServ.id}/tools`, {
                        credentials: 'same-origin',
                    });
                    const data = await r.json().catch(() => ({}));
                    if (!r.ok) throw new Error(data.detail || data.error || `HTTP ${r.status}`);
                    const tools = data.tools || [];
                    this._mcpToolsServerId = activeServ.id;
                    this._mcpTools = tools;
                    this._mcpSelectedTool = tools[0]?.name || null;
                    this._mcpToolArgsByTool = {};
                    this._mcpToolInputMode = 'guided';
                    if (tools[0]?.name) {
                        this._mcpToolArgs = _prettyJson(
                            _sampleArgsFromSchema(
                                tools[0]?.input_schema || tools[0]?.inputSchema || {}
                            )
                        );
                        this._mcpToolArgsByTool[tools[0].name] = this._mcpToolArgs;
                    }
                    _showToast(t('settings.catalog.toasts.loadedFunctions', { count: tools.length }), 'success');
                } catch (e2) {
                    _showToast(t('settings.catalog.toasts.loadFunctionsFailed', { detail: iso(e2.message) }), 'error');
                } finally {
                    this._mcpToolsLoading = false;
                    this._mcpRender();
                }
            });
        }

        const toolSelect = $('#mc-tool-select');
        if (toolSelect) {
            toolSelect.addEventListener('change', () => {
                const tools = (this._mcpToolsServerId === S.active_server_id && this._mcpTools)
                    ? this._mcpTools
                    : ((S.servers||[]).find(s => s.is_active)?.health?.tools || []);
                const tool = tools.find(t => t.name === toolSelect.value);
                if (this._mcpSelectedTool) {
                    this._mcpToolArgsByTool[this._mcpSelectedTool] = this._mcpToolArgs || '{}';
                }
                this._mcpSelectedTool = toolSelect.value;
                this._mcpToolArgs = this._mcpToolArgsByTool[toolSelect.value]
                    ?? _prettyJson(_sampleArgsFromSchema(tool?.input_schema || tool?.inputSchema || {}));
                this._mcpToolArgsByTool[toolSelect.value] = this._mcpToolArgs;
                this._mcpToolInputMode = _mcpHasGuidedFields(tool?.input_schema || tool?.inputSchema || {})
                    ? 'guided'
                    : 'json';
                this._mcpToolResult = null;
                this._mcpRender();
            });
        }

        const toolArgs = $('#mc-tool-args');
        if (toolArgs) {
            toolArgs.addEventListener('input', () => {
                this._mcpToolArgs = toolArgs.value;
                if (this._mcpSelectedTool) this._mcpToolArgsByTool[this._mcpSelectedTool] = toolArgs.value;
            });
        }

        this._content.querySelectorAll('[data-mc-tool-input-mode]').forEach(btn => {
            btn.addEventListener('click', () => {
                this._mcpToolInputMode = btn.dataset.mcToolInputMode;
                this._mcpRender();
            });
        });

        this._content.querySelectorAll('[data-mc-tool-arg]').forEach(input => {
            input.addEventListener('change', () => {
                const tools = (this._mcpToolsServerId === S.active_server_id && this._mcpTools)
                    ? this._mcpTools
                    : ((S.servers||[]).find(s => s.is_active)?.health?.tools || []);
                const tool = tools.find(t => t.name === this._mcpSelectedTool) || tools[0];
                const schema = tool?.input_schema || tool?.inputSchema || {};
                const args = _mcpParseArguments(this._mcpToolArgs) ?? _sampleArgsFromSchema(schema);
                const key = input.dataset.mcToolArg;
                const fieldSchema = (schema.properties || {})[key] || {};
                _mcpApplyGuidedArgument(args, key, fieldSchema, input);
                this._mcpToolArgs = _prettyJson(args);
                if (this._mcpSelectedTool) this._mcpToolArgsByTool[this._mcpSelectedTool] = this._mcpToolArgs;
                this._mcpToolResult = null;
                this._mcpRender();
            });
        });

        const sampleBtn = $('#mc-tool-sample-btn');
        if (sampleBtn) {
            sampleBtn.addEventListener('click', () => {
                const tools = (this._mcpToolsServerId === S.active_server_id && this._mcpTools)
                    ? this._mcpTools
                    : ((S.servers||[]).find(s => s.is_active)?.health?.tools || []);
                const tool = tools.find(t => t.name === this._mcpSelectedTool) || tools[0];
                this._mcpToolArgs = _prettyJson(
                    _sampleArgsFromSchema(tool?.input_schema || tool?.inputSchema || {})
                );
                if (this._mcpSelectedTool) this._mcpToolArgsByTool[this._mcpSelectedTool] = this._mcpToolArgs;
                this._mcpToolResult = null;
                this._mcpRender();
            });
        }

        const copyInputBtn = $('#mc-tool-copy-input-btn');
        if (copyInputBtn) {
            copyInputBtn.addEventListener('click', async () => {
                try {
                    const toolName = this._mcpSelectedTool || $('#mc-tool-select')?.value;
                    const toolArgs = this._mcpToolArgsByTool[toolName] ?? this._mcpToolArgs ?? '{}';
                    await navigator.clipboard.writeText(toolArgs);
                    _showToast(t('settings.catalog.toasts.inputCopied'), 'success');
                } catch {
                    _showToast(t('settings.catalog.toasts.inputCopyFailed'), 'error');
                }
            });
        }

        const callBtn = $('#mc-tool-call-btn');
        if (callBtn) {
            callBtn.addEventListener('click', async () => {
                const activeServ = (S.servers||[]).find(s => s.is_active);
                const toolName = this._mcpSelectedTool || $('#mc-tool-select')?.value;
                const rawArgs = $('#mc-tool-args')?.value
                    ?? this._mcpToolArgsByTool[toolName]
                    ?? this._mcpToolArgs
                    ?? '{}';
                if (!activeServ || !toolName) return;
                let args;
                try {
                    args = rawArgs.trim() ? JSON.parse(rawArgs) : {};
                    if (!args || Array.isArray(args) || typeof args !== 'object') {
                        throw new Error(t('settings.catalog.inspector.argsMustBeObject'));
                    }
                } catch (e2) {
                    _showToast(t('settings.catalog.toasts.invalidJson', { detail: iso(e2.message) }), 'error');
                    return;
                }
                const tools = (this._mcpToolsServerId === S.active_server_id && this._mcpTools)
                    ? this._mcpTools
                    : ((S.servers||[]).find(s => s.is_active)?.health?.tools || []);
                const tool = tools.find(t => t.name === toolName) || null;
                const inputErrors = _mcpInputValidationErrors(
                    tool?.input_schema || tool?.inputSchema || {}, args
                );
                if (inputErrors.length) {
                    _showToast(inputErrors[0], 'error');
                    return;
                }
                const risk = _mcpToolRisk(tool);
                const confirmed = risk.level === 'confirmation_required'
                    ? confirm(`${risk.reason}\n\n${t('settings.catalog.toasts.runConfirm', { name: iso(toolName) })}`)
                    : false;
                if (risk.level === 'confirmation_required' && !confirmed) return;
                this._mcpToolCalling = true;
                this._mcpToolArgs = _prettyJson(args);
                this._mcpToolArgsByTool[toolName] = this._mcpToolArgs;
                this._mcpToolResult = null;
                this._mcpToolResultView = 'content';
                this._mcpRender();
                try {
                    const r = await fetch(`/api/mcp/servers/${activeServ.id}/tools/call`, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({tool_name: toolName, arguments: args, confirmed}),
                    });
                    const data = await r.json().catch(() => ({}));
                    this._mcpToolResult = r.ok
                        ? {...data, completed_at: new Date().toISOString()}
                        : {
                            ok: false,
                            tool_name: toolName,
                            arguments: args,
                            error: _mcpErrorMessage(data.detail || data.error || `HTTP ${r.status}`),
                            diagnostic: data,
                            completed_at: new Date().toISOString(),
                        };
                    _showToast(r.ok ? t('settings.catalog.toasts.callComplete') : t('settings.catalog.toasts.callFailed'), r.ok ? 'success' : 'error');
                } catch (e2) {
                    this._mcpToolResult = {
                        ok: false,
                        tool_name: toolName,
                        arguments: args,
                        error: e2.message || String(e2),
                        completed_at: new Date().toISOString(),
                    };
                    _showToast(t('settings.catalog.toasts.callFailedDetail', { detail: iso(e2.message) }), 'error');
                } finally {
                    this._mcpToolCalling = false;
                    this._mcpRender();
                }
            });
        }

        this._content.querySelectorAll('[data-mc-tool-result-view]').forEach(btn => {
            btn.addEventListener('click', () => {
                this._mcpToolResultView = btn.dataset.mcToolResultView;
                this._mcpRender();
            });
        });

        const resultCopyBtn = $('#mc-tool-result-copy-btn');
        if (resultCopyBtn && this._mcpToolResult) {
            resultCopyBtn.addEventListener('click', async () => {
                const text = _mcpToolResultCopyText(this._mcpToolResult, this._mcpToolResultView);
                try {
                    await navigator.clipboard.writeText(text);
                    _showToast(t('settings.catalog.toasts.resultCopied'), 'success');
                } catch {
                    _showToast(t('settings.catalog.toasts.resultCopyFailed'), 'error');
                }
            });
        }

        const aiAssistBtn = $('#mc-tool-ai-assist-btn');
        if (aiAssistBtn && this._mcpToolResult) {
            aiAssistBtn.addEventListener('click', async () => {
                const activeServ = (S.servers || []).find(s => s.is_active);
                const toolName = this._mcpToolResult.tool_name || this._mcpSelectedTool;
                if (!activeServ || !toolName) return;
                this._mcpToolResult = {...this._mcpToolResult, ai_assist_loading: true, ai_assist_error: null};
                this._mcpRender();
                try {
                    const r = await fetch(`/api/mcp/servers/${activeServ.id}/tools/assist-error`, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            tool_name: toolName,
                            arguments: this._mcpToolResult.arguments || {},
                            error: _mcpToolErrorForAssist(this._mcpToolResult),
                        }),
                    });
                    const data = await r.json().catch(() => ({}));
                    if (!r.ok) throw new Error(_mcpErrorMessage(data.detail || data.error || `HTTP ${r.status}`));
                    this._mcpToolResult = {...this._mcpToolResult, ai_assist: data, ai_assist_loading: false};
                } catch (e2) {
                    this._mcpToolResult = {
                        ...this._mcpToolResult,
                        ai_assist_loading: false,
                        ai_assist_error: e2.message || 'AI assistance could not analyze this error.',
                    };
                } finally {
                    this._mcpRender();
                }
            });
        }

        const applyAiArgsBtn = $('#mc-tool-ai-apply-args-btn');
        if (applyAiArgsBtn && this._mcpToolResult?.ai_assist?.suggested_arguments) {
            applyAiArgsBtn.addEventListener('click', () => {
                const toolName = this._mcpToolResult.tool_name || this._mcpSelectedTool;
                this._mcpToolArgs = _prettyJson(this._mcpToolResult.ai_assist.suggested_arguments);
                if (toolName) this._mcpToolArgsByTool[toolName] = this._mcpToolArgs;
                this._mcpToolInputMode = 'json';
                _showToast(t('settings.catalog.toasts.aiSuggestionLoaded'), 'info');
                this._mcpRender();
            });
        }

        // Cache TTL — applies to the active MCP server (caching is MCP-only)
        const ttlSel = $('#mc-ttl-sel');
        if (ttlSel) {
            ttlSel.addEventListener('change', async () => {
                try {
                    const r = await fetch(`/api/mcp/cache-ttl?cache_ttl_seconds=${ttlSel.value}`, {method: 'PUT'});
                    if (!r.ok) { const err = await r.json().catch(()=>({})); throw new Error(err.detail || `HTTP ${r.status}`); }
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                } catch (e2) { _showToast(t('settings.catalog.toasts.ttlFailed', { detail: iso(e2.message) }), 'error'); }
            });
        }
    }

    // ── General (parameters) ─────────────────────────────────────────────────

    _renderGeneral() {
        const prefs = Preferences.getAll();
        const i18n = window.I18n || {};
        const currentLocale = i18n.locale || 'en';
        const locales = Array.isArray(i18n.locales) && i18n.locales.length ? i18n.locales : [{ tag: 'en', name: 'English', dir: 'ltr' }];

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.general.title')}</h2>
                <p class="sp-section-desc">${h('settings.general.desc')}</p>
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.general.appearanceCard')}</div>
                ${this._row(t('settings.general.language.label'), t('settings.general.language.help'), `
                    <div class="sp-lang-picker" id="sp-language" role="radiogroup" aria-label="${h('settings.general.language.label')}">
                        ${locales.map((l) => `<button type="button" class="sp-lang-option${l.tag === currentLocale ? ' is-active' : ''}" role="radio"
                            lang="${_esc(l.tag)}" dir="${_esc(l.dir || 'ltr')}" data-locale="${_esc(l.tag)}"
                            aria-checked="${l.tag === currentLocale ? 'true' : 'false'}" tabindex="${l.tag === currentLocale ? 0 : -1}"><bdi>${_esc(l.name)}</bdi></button>`).join('')}
                    </div>
                    <span class="sp-lang-status" id="sp-language-status" role="status" aria-live="polite"></span>`)}
                ${this._row(t('settings.general.theme.label'), t('settings.general.theme.help'), `
                    <select class="settings-select" id="sp-theme">
                        <option value="light"${prefs.theme==='light'?' selected':''}>${h('settings.general.theme.light')}</option>
                        <option value="dark"${prefs.theme==='dark'?' selected':''}>${h('settings.general.theme.dark')}</option>
                        <option value="system"${prefs.theme==='system'?' selected':''}>${h('settings.general.theme.system')}</option>
                    </select>`)}
                ${this._row(t('settings.general.rowLimit.label'), t('settings.general.rowLimit.help'), `
                    <select class="settings-select" id="sp-rowlimit">
                        ${[25,100,500,1000].map(n=>`<option value="${n}"${prefs.rowLimit===n?' selected':''}>${h('settings.general.rowLimit.option', { count: n })}</option>`).join('')}
                    </select>`)}
                ${this._row(t('settings.general.conversationTabs.label'), t('settings.general.conversationTabs.help'), `
                    <select class="settings-select" id="sp-convtabs">
                        <option value="hide"${prefs.conversationTabs==='hide'?' selected':''}>${h('settings.general.conversationTabs.hidden')}</option>
                        <option value="show"${prefs.conversationTabs==='show'?' selected':''}>${h('settings.general.conversationTabs.shown')}</option>
                    </select>`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.general.chartCard')}</div>
                ${this._row(t('settings.general.chartType.label'), t('settings.general.chartType.help'), `
                    <select class="settings-select" id="sp-charttype">
                        ${CHART_TYPE_OPTIONS.map(opt =>
                            `<option value="${opt.value}"${prefs.chartType === opt.value ? ' selected' : ''}>${_esc(window.I18n && window.I18n.has(`charts.types.${opt.value}`) ? t(`charts.types.${opt.value}`) : opt.label)}</option>`
                        ).join('')}
                    </select>`)}
                ${this._row(t('settings.general.autoInsights.label'), t('settings.general.autoInsights.help'), `
                    <select class="settings-select" id="sp-insights">
                        <option value="on"${prefs.autoInsights==='on'?' selected':''}>${h('common.on')}</option>
                        <option value="off"${prefs.autoInsights==='off'?' selected':''}>${h('common.off')}</option>
                    </select>`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.general.modelCard')}</div>
                ${this._row(t('settings.general.temperature.label'), t('settings.general.temperature.help'), `
                    <select class="settings-select" id="sp-temp">
                        <option value="auto">${h('settings.general.temperature.auto')}</option>
                        ${[0.0,0.2,0.4,0.6,0.8,1.0].map(n=>{
                            const v=n.toFixed(1);
                            const label = n===0?t('settings.general.temperature.deterministic', { value: v }):n===0.2?t('settings.general.temperature.recommended', { value: v }):n===1.0?t('settings.general.temperature.creative', { value: v }):v;
                            const sel = prefs.temperature===n?' selected':'';
                            return `<option value="${v}"${sel}>${_esc(label)}</option>`;
                        }).join('')}
                    </select>`)}
            </div>
            <div class="sp-card-footer">
                <button class="sp-btn-ghost" id="sp-reset-prefs">${h('settings.general.resetAll')}</button>
            </div>
        `;

        this._wireLanguagePicker(currentLocale);

        // Wire up events
        this._content.querySelector('#sp-theme')?.addEventListener('change', e => {
            Preferences.setTheme(e.target.value);
            if (this._onApplyTheme) this._onApplyTheme(e.target.value);
        });
        this._content.querySelector('#sp-rowlimit')?.addEventListener('change', e => Preferences.setRowLimit(e.target.value));
        this._content.querySelector('#sp-convtabs')?.addEventListener('change', e => {
            Preferences.setConversationTabs(e.target.value);
            _applyConversationTabs(e.target.value);
        });
        this._content.querySelector('#sp-charttype')?.addEventListener('change', e => Preferences.setChartType(e.target.value));
        this._content.querySelector('#sp-insights')?.addEventListener('change', e => Preferences.setAutoInsights(e.target.value));
        this._content.querySelector('#sp-temp')?.addEventListener('change', e => Preferences.setTemperature(e.target.value));
        this._content.querySelector('#sp-reset-prefs')?.addEventListener('click', () => {
            if (!confirm(t('settings.general.resetConfirm'))) return;
            Preferences.resetAll();
            this._renderGeneral();
            if (this._onApplyTheme) this._onApplyTheme(Preferences.DEFAULTS.theme);
            _applyConversationTabs(Preferences.DEFAULTS.conversationTabs);
        });
    }

    /**
     * Interface language: an account property (PATCH /api/auth/me/locale). The
     * save is NOT optimistic — the page is only reloaded once the server has
     * persisted the value and set the cookie, so a failed save never leaves a
     * half-flipped document. Arrow keys follow the visual order (mirrored in RTL).
     */
    _wireLanguagePicker(currentLocale) {
        const group = this._content.querySelector('#sp-language');
        const status = this._content.querySelector('#sp-language-status');
        if (!group) return;
        const options = [...group.querySelectorAll('.sp-lang-option')];
        const setBusy = (busy) => {
            group.setAttribute('aria-busy', String(busy));
            options.forEach((b) => { b.disabled = busy; });
        };
        const choose = async (tag) => {
            if (!tag || tag === currentLocale) return;
            setBusy(true);
            if (status) status.textContent = t('common.saving');
            try {
                const res = await fetch('/api/auth/me/locale', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ locale: tag }),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                // The server has set the `locale` cookie and session; a reload
                // renders the whole shell (built once at boot) in the new language.
                window.location.reload();
            } catch (err) {
                console.warn('[SettingsPage] language save failed:', err);
                setBusy(false);
                if (status) status.textContent = '';
                _showToast(t('settings.general.language.saveFailed'), 'error');
            }
        };
        options.forEach((button) => button.addEventListener('click', () => choose(button.dataset.locale)));
        group.addEventListener('keydown', (event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const rtl = Boolean(window.I18n && window.I18n.isRtl);
            const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
            const current = Math.max(0, options.indexOf(document.activeElement));
            let next = current;
            if (event.key === forward) next = (current + 1) % options.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = options.length - 1;
            else next = (current - 1 + options.length) % options.length;
            event.preventDefault();
            options[next].focus();
            choose(options[next].dataset.locale);
        });
    }

    _row(label, help, controlHtml) {
        return `<div class="sp-row">
            <div class="sp-row-label-block">
                <div class="sp-row-label">${_esc(label)}</div>
                ${help ? `<div class="sp-row-help">${_esc(help)}</div>` : ''}
            </div>
            <div class="sp-row-control">${controlHtml}</div>
        </div>`;
    }

    // ── Query & Safety (global runtime guardrails) ────────────────────────────

    async _renderQuerySafety() {
        if (!this._onTab('query-safety')) return;
        const seq = this._renderSeq;
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.querySafety.title')}</h2>
                <p class="sp-section-desc">${h('settings.querySafety.descShort')}</p>
            </div>
            <div class="sp-card"><div class="sp-card-title">${h('common.loading')}</div></div>
        `;

        let data;
        try {
            const res = await fetch('/api/settings/runtime');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            data = await res.json();
        } catch (e) {
            console.error('[SettingsPage] load runtime settings failed:', e);
            if (seq !== this._renderSeq) return;
            this._content.innerHTML = `
                <div class="sp-section-header">
                    <h2 class="sp-section-title">${h('settings.querySafety.title')}</h2>
                </div>
                <div class="sp-card"><div class="sp-card-title">${h('settings.querySafety.loadFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></div></div>
            `;
            return;
        }
        if (seq !== this._renderSeq) return;

        const b = data.bounds || {};
        const tb = b.db_statement_timeout_ms || { min: 0, max: 600000 };
        const rb = b.max_result_rows || { min: 1, max: 1000000 };
        const cb = b.conversation_context_turns || { min: 0, max: 50 };
        const eb = b.dax_entity_max_domain_values || { min: 1, max: 100000 };
        const mb = b.dax_entity_match_threshold || { min: 0, max: 100 };
        const sb = b.sql_filter_max_domain_values || { min: 1, max: 100000 };
        const sm = b.sql_filter_match_threshold || { min: 0, max: 100 };
        const st = b.sql_filter_lookup_timeout_ms || { min: 100, max: 60000 };
        const sc = b.sql_filter_cache_ttl_seconds || { min: 1, max: 3600 };
        const fe = b.sql_filter_existence_max_age_hours || { min: 1, max: 8760 };
        const fa = b.sql_filter_absence_max_age_hours || { min: 1, max: 8760 };
        const choices = data.choices || {};
        const visibilityChoices = choices.sql_filter_value_visibility || ['none', 'source_wide', 'user_scoped'];
        const unverifiedChoices = choices.sql_filter_unverified_execution || ['ask', 'allow'];
        const visibilityLabels = {
            none: t('settings.querySafety.visibility.none'),
            source_wide: t('settings.querySafety.visibility.sourceWide'),
            user_scoped: t('settings.querySafety.visibility.userScoped'),
        };
        const unverifiedLabels = {
            ask: t('settings.querySafety.unverified.ask'),
            allow: t('settings.querySafety.unverified.allow'),
        };
        const range = (b) => t('settings.querySafety.range', { min: b.min, max: b.max });
        const qs = (key, args) => t(`settings.querySafety.${key}`, args);
        const options = (values, labels, current) => values.map((v) =>
            `<option value="${_esc(v)}" ${v === current ? 'selected' : ''}>${_esc(labels[v] || v)}</option>`).join('');

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.querySafety.title')}</h2>
                <p class="sp-section-desc">${h('settings.querySafety.desc')}</p>
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.querySafety.sqlExecution')}</div>
                ${this._row(qs('timeout.label'), `${qs('timeout.help')} ${range(tb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-timeout"
                           min="${tb.min}" max="${tb.max}" step="500"
                           value="${data.db_statement_timeout_ms}">`)}
                ${this._row(qs('maxRows.label'), `${qs('maxRows.help')} ${range(rb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-maxrows"
                           min="${rb.min}" max="${rb.max}" step="100"
                           value="${data.max_result_rows}">`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.querySafety.memory')}</div>
                ${this._row(qs('contextTurns.label'), `${qs('contextTurns.help')} ${range(cb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-turns"
                           min="${cb.min}" max="${cb.max}" step="1"
                           value="${data.conversation_context_turns}">`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.querySafety.pbiResolution')}</div>
                ${this._row(qs('pbiResolve.label'), qs('pbiResolve.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-entity-on" ${data.dax_entity_resolution_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
                ${this._row(qs('pbiThreshold.label'), `${qs('pbiThreshold.help')} ${range(mb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-entity-threshold"
                           min="${mb.min}" max="${mb.max}" step="1"
                           value="${data.dax_entity_match_threshold}">`)}
                ${this._row(qs('pbiDomain.label'), `${qs('pbiDomain.help')} ${range(eb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-entity-domain"
                           min="${eb.min}" max="${eb.max}" step="100"
                           value="${data.dax_entity_max_domain_values}">`)}
                ${this._row(qs('pbiCross.label'), qs('pbiCross.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-entity-cross" ${data.dax_entity_cross_column_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.querySafety.sqlGrounding')}</div>
                ${this._row(qs('sqlResolve.label'), qs('sqlResolve.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-sql-filter-on" ${data.sql_filter_resolution_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
                ${this._row(qs('sqlThreshold.label'), `${qs('sqlThreshold.help')} ${range(sm)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-filter-threshold"
                           min="${sm.min}" max="${sm.max}" step="1"
                           value="${data.sql_filter_match_threshold}">`)}
                ${this._row(qs('sqlDomain.label'), `${qs('sqlDomain.help')} ${range(sb)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-filter-domain"
                           min="${sb.min}" max="${sb.max}" step="100"
                           value="${data.sql_filter_max_domain_values}">`)}
                ${this._row(qs('sqlTimeout.label'), `${qs('sqlTimeout.help')} ${range(st)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-filter-timeout"
                           min="${st.min}" max="${st.max}" step="100"
                           value="${data.sql_filter_lookup_timeout_ms}">`)}
                ${this._row(qs('sqlCache.label'), `${qs('sqlCache.help')} ${range(sc)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-filter-cache"
                           min="${sc.min}" max="${sc.max}" step="30"
                           value="${data.sql_filter_cache_ttl_seconds}">`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.querySafety.evidence')}</div>
                ${this._row(qs('metaEvidence.label'), qs('metaEvidence.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-sql-meta-on" ${data.sql_filter_metadata_evidence_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
                ${this._row(qs('visibility.label'), qs('visibility.help'), `
                    <select class="settings-select" id="sp-rt-sql-visibility">${options(visibilityChoices, visibilityLabels, data.sql_filter_value_visibility)}</select>`)}
                ${this._row(qs('unverified.label'), qs('unverified.help'), `
                    <select class="settings-select" id="sp-rt-sql-unverified">${options(unverifiedChoices, unverifiedLabels, data.sql_filter_unverified_execution)}</select>`)}
                ${this._row(qs('probe.label'), qs('probe.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-sql-probe-on" ${data.sql_filter_source_probe_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
                ${this._row(qs('distinct.label'), qs('distinct.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-rt-sql-distinct-on" ${data.sql_filter_source_distinct_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>`)}
                ${this._row(qs('denylist.label'), qs('denylist.help'), `
                    <input class="settings-select" type="text" id="sp-rt-sql-denylist" dir="ltr" placeholder="hr.*, customers.email"
                           value="${_esc(data.sql_filter_probe_denylist || '')}">`)}
                ${this._row(qs('existAge.label'), `${qs('existAge.help')} ${range(fe)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-exist-age"
                           min="${fe.min}" max="${fe.max}" step="1"
                           value="${data.sql_filter_existence_max_age_hours}">`)}
                ${this._row(qs('absentAge.label'), `${qs('absentAge.help')} ${range(fa)}`, `
                    <input class="settings-select" type="number" id="sp-rt-sql-absent-age"
                           min="${fa.min}" max="${fa.max}" step="1"
                           value="${data.sql_filter_absence_max_age_hours}">`)}
            </div>
            <div class="sp-card-footer">
                <button class="sp-btn-primary" id="sp-rt-save">${h('common.save')}</button>
            </div>
        `;

        this._content.querySelector('#sp-rt-save')?.addEventListener('click', async () => {
            const numeric = {
                'sp-rt-timeout': 'db_statement_timeout_ms', 'sp-rt-maxrows': 'max_result_rows', 'sp-rt-turns': 'conversation_context_turns',
                'sp-rt-entity-threshold': 'dax_entity_match_threshold', 'sp-rt-entity-domain': 'dax_entity_max_domain_values',
                'sp-rt-sql-filter-threshold': 'sql_filter_match_threshold', 'sp-rt-sql-filter-domain': 'sql_filter_max_domain_values',
                'sp-rt-sql-filter-timeout': 'sql_filter_lookup_timeout_ms', 'sp-rt-sql-filter-cache': 'sql_filter_cache_ttl_seconds',
                'sp-rt-sql-exist-age': 'sql_filter_existence_max_age_hours', 'sp-rt-sql-absent-age': 'sql_filter_absence_max_age_hours',
            };
            // An emptied number box is a mistake, not a request to keep the old
            // value silently: refuse to save until every number is a number.
            const blank = Object.keys(numeric).filter((id) => !Number.isFinite(Number(this._content.querySelector(`#${id}`).value)) || this._content.querySelector(`#${id}`).value.trim() === '');
            if (blank.length) {
                _showToast(t('settings.querySafety.enterNumber', { fields: iso(blank.map((id) => numeric[id].replace(/_/g, ' ')).join(', ')) }), 'error');
                return;
            }
            const payload = {
                db_statement_timeout_ms: parseInt(this._content.querySelector('#sp-rt-timeout').value, 10),
                max_result_rows: parseInt(this._content.querySelector('#sp-rt-maxrows').value, 10),
                conversation_context_turns: parseInt(this._content.querySelector('#sp-rt-turns').value, 10),
                dax_entity_resolution_enabled: this._content.querySelector('#sp-rt-entity-on').checked,
                dax_entity_match_threshold: parseFloat(this._content.querySelector('#sp-rt-entity-threshold').value),
                dax_entity_max_domain_values: parseInt(this._content.querySelector('#sp-rt-entity-domain').value, 10),
                dax_entity_cross_column_enabled: this._content.querySelector('#sp-rt-entity-cross').checked,
                sql_filter_resolution_enabled: this._content.querySelector('#sp-rt-sql-filter-on').checked,
                sql_filter_match_threshold: parseFloat(this._content.querySelector('#sp-rt-sql-filter-threshold').value),
                sql_filter_max_domain_values: parseInt(this._content.querySelector('#sp-rt-sql-filter-domain').value, 10),
                sql_filter_lookup_timeout_ms: parseInt(this._content.querySelector('#sp-rt-sql-filter-timeout').value, 10),
                sql_filter_cache_ttl_seconds: parseInt(this._content.querySelector('#sp-rt-sql-filter-cache').value, 10),
                sql_filter_metadata_evidence_enabled: this._content.querySelector('#sp-rt-sql-meta-on').checked,
                sql_filter_value_visibility: this._content.querySelector('#sp-rt-sql-visibility').value,
                sql_filter_unverified_execution: this._content.querySelector('#sp-rt-sql-unverified').value,
                sql_filter_source_probe_enabled: this._content.querySelector('#sp-rt-sql-probe-on').checked,
                sql_filter_source_distinct_enabled: this._content.querySelector('#sp-rt-sql-distinct-on').checked,
                sql_filter_probe_denylist: this._content.querySelector('#sp-rt-sql-denylist').value.trim(),
                sql_filter_existence_max_age_hours: parseInt(this._content.querySelector('#sp-rt-sql-exist-age').value, 10),
                sql_filter_absence_max_age_hours: parseInt(this._content.querySelector('#sp-rt-sql-absent-age').value, 10),
            };
            try {
                const res = await fetch('/api/settings/runtime', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                _showToast(t('settings.querySafety.saved'), 'success');
                this._renderQuerySafety();  // reflect clamped values
            } catch (e) {
                console.error('[SettingsPage] save runtime settings failed:', e);
                _showToast(t('settings.saveFailed', { detail: iso(e.message) }), 'error');
            }
        });
    }

    // ── Model helpers ─────────────────────────────────────────────────────────

    async _ensureModels() {
        if (this._models !== null) return;
        try {
            const res = await fetch('/api/settings/models');
            if (res.ok) this._models = await res.json();
            else this._models = [];
        } catch {
            this._models = [];
        }
    }

    async _setPromptModel(name, model_name) {
        try {
            const res = await fetch(`/api/settings/prompts/${name}/model`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_name: model_name || null }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (this._prompts[name]) {
                this._prompts[name].meta.model_name = data.model_name || null;
                this._prompts[name].meta.model_id   = data.model_id   || null;
            }
            _showToast(
                model_name ? `Model set to ${model_name}` : 'Using global active model',
                'success',
            );
        } catch (e) {
            console.error('[SettingsPage] setPromptModel failed:', e);
            _showToast(t('settings.prompts.modelUpdateFailed', { detail: iso(e.message) }), 'error');
        }
    }

    // ── Prompt editor ─────────────────────────────────────────────────────────

    async _ensurePromptContexts() {
        if (this._promptContexts) return;
        try {
            const res = await fetch('/api/settings/prompt-contexts');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._promptContextSource = data.catalog_source || 'db';
            this._promptContexts = data.connections || [];
            if (!this._promptResolveConnection && this._promptContexts.length) {
                this._promptResolveConnection = this._promptContexts[0].source_key;
            }
        } catch (e) {
            console.warn('[SettingsPage] could not load prompt contexts:', e);
            this._promptContexts = [];
        }
    }

    async _loadResolvedPrompt(name, { force = false } = {}) {
        const entry = this._prompts[name];
        const conn = this._promptResolveConnection;
        if (!entry || !conn) return;
        const key = `${name}:${conn}`;
        if (!force && entry.resolvedKey === key && entry.resolved) return;

        entry.resolving = true;
        entry.resolveError = null;
        try {
            const res = await fetch(
                `/api/settings/prompts/${encodeURIComponent(name)}/resolved?connection=${encodeURIComponent(conn)}`,
            );
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            entry.resolvedKey = key;
            entry.resolved = data;
        } catch (e) {
            entry.resolveError = e.message || String(e);
        } finally {
            entry.resolving = false;
        }
    }

    _resolvedPromptControls(name, entry) {
        const mode = entry.viewMode || 'template';
        const contexts = this._promptContexts || [];
        const conn = this._promptResolveConnection || '';
        const resolved = entry.resolvedKey === `${name}:${conn}` ? entry.resolved : null;
        const tok = resolved?.tokens || null;
        const cache = resolved?.catalog_cache;
        const cacheLabel = cache
            ? `${cache.cache_hit && !cache.is_stale ? 'cache HIT' : 'cache MISS'}`
            : 'live';
        const sourceLabel = resolved?.catalog_source || this._promptContextSource || 'db';
        const tokenLabel = tok
            ? `${_formatCount(tok.resolved)} tokens`
            : 'tokens after resolve';

        return `
        <div class="sp-resolve-bar">
            <div class="sp-view-seg" id="sp-prompt-view-mode-${_esc(name)}">
                <button data-mode="template" class="${mode === 'template' ? 'is-active' : ''}">Template</button>
                <button data-mode="resolved" class="${mode === 'resolved' ? 'is-active' : ''}">Resolved</button>
            </div>
            <select class="settings-select sp-resolve-conn" id="sp-resolve-conn-${_esc(name)}" ${contexts.length ? '' : 'disabled'}>
                ${contexts.length
                    ? contexts.map(c => `<option value="${_esc(c.source_key)}"${c.source_key === conn ? ' selected' : ''}>${_esc(c.display_name || c.source_key)} · ${_esc(c.database_type || c.catalog_source || '')}</option>`).join('')
                    : `<option>${h('connection.none')}</option>`}
            </select>
            <button class="sp-btn-ghost sp-btn-ghost-sm" id="sp-resolve-refresh-${_esc(name)}" ${mode === 'resolved' && conn ? '' : 'disabled'}>${h('common.refresh')}</button>
            <div class="sp-resolve-tokens">
                <span class="sp-token-pill">${_esc(sourceLabel.toUpperCase())}</span>
                <span class="sp-token-pill">${_esc(cacheLabel)}</span>
                <span class="sp-token-pill sp-token-pill-strong">${_esc(tokenLabel)}</span>
            </div>
        </div>`;
    }

    _resolvedPromptBody(name, entry, templateContent) {
        const conn = this._promptResolveConnection || '';
        const resolved = entry.resolvedKey === `${name}:${conn}` ? entry.resolved : null;
        if (entry.resolving) {
            return `<div class="sp-prompt-view sp-resolved-empty">Resolving prompt for ${_esc(conn || 'connection')}…</div>`;
        }
        if (entry.resolveError) {
            return `<div class="sp-prompt-view sp-resolved-empty sp-resolved-error">Could not resolve prompt: ${_esc(entry.resolveError)}</div>`;
        }
        if (!resolved) {
            return `<div class="sp-prompt-view sp-resolved-empty">Select a connection and click <b>Resolved</b> or <b>Refresh</b> to load real MCP/DB values.</div>`;
        }
        return `
            <div class="sp-resolved-summary">
                ${this._resolvedTokenCards(resolved)}
            </div>
            <div class="sp-prompt-view sp-prompt-view-resolved" id="sp-prompt-view-${_esc(name)}">${_renderResolvedPromptView(resolved.resolved_content || templateContent)}</div>`;
    }

    _resolvedTokenCards(resolved) {
        const rows = resolved.placeholder_tokens || [];
        const total = resolved.tokens?.resolved ?? 0;
        const catalog = resolved.tokens?.catalog ?? 0;
        const unresolved = resolved.unresolved_placeholders || [];
        return `
            <div class="sp-token-total">
                <span>Total <b>${_formatCount(total)}</b> tokens</span>
                <span>Catalog <b>${_formatCount(catalog)}</b> tokens</span>
                <span class="sp-tokenizer">${_esc(resolved.tokenizer || '')}</span>
            </div>
            ${unresolved.length ? `<div class="sp-resolved-note">Runtime placeholders still shown: ${unresolved.map(p => `<code>{${_esc(p)}}</code>`).join(' ')}</div>` : ''}
            <div class="sp-token-grid">
                ${rows.map(r => `
                    <div class="sp-token-card">
                        <div class="sp-token-card-head">
                            <code>{${_esc(r.name)}}</code>
                            <span>${_esc(r.source)}</span>
                        </div>
                        <div class="sp-token-card-num">${_formatCount(r.tokens)} tokens</div>
                    </div>
                `).join('')}
            </div>`;
    }

    // ── Prompts list ──────────────────────────────────────────────────────────

    /** Eyebrow link shown above the editor title; returns to the prompt list. */
    _promptBackLinkHtml() {
        return `<button type="button" class="sp-back-link" id="sp-prompt-back">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>
            Prompts
        </button>`;
    }

    _wirePromptBackLink(name) {
        this._content.querySelector('#sp-prompt-back')?.addEventListener('click', () => {
            if (!this._confirmLeavePrompt()) return;
            this._promptOpen = null;
            // Return focus to the row the user came from so keyboard users
            // don't lose their place in the list.
            this._renderPromptList({ focusName: name });
        });
    }

    /** Move focus to a list row (after a paint) unless the user has moved focus elsewhere. */
    _focusPromptRow(name) {
        const row = this._content.querySelector(`.sp-prompt-row[data-name="${CSS.escape(name)}"]`);
        if (!row) return;
        const active = document.activeElement;
        const userMoved = active && active !== document.body && !this._content.contains(active);
        if (userMoved) return;
        row.focus({ preventScroll: true });
        row.scrollIntoView({ block: 'nearest' });
    }

    _openPrompt(name) {
        this._promptOpen = name;
        this._renderPrompt(name).then(() => {
            const h2 = this._content.querySelector('.sp-section-title');
            if (h2 && this._onTab('prompts') && this._promptOpen === name) {
                h2.setAttribute('tabindex', '-1');
                h2.focus({ preventScroll: true });
            }
        });
    }

    _promptListHeader(summary) {
        return `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.prompts.title')}</h2>
                <p class="sp-section-desc">${h('settings.prompts.desc')}</p>
                ${summary ? `<div class="sp-models-toolbar"><span class="sp-models-health-summary">${summary}</span></div>` : ''}
            </div>`;
    }

    _knownPromptMetas() {
        return Object.values(this._prompts).map(p => p.meta).filter(m => m && m.name);
    }

    /**
     * Paint the list immediately from whatever metadata is cached (open()
     * usually has it), then refresh from the API and repaint if still on the
     * list. Never leaves the previous pane on screen while a fetch is in flight.
     */
    async _renderPromptList({ focusName = null } = {}) {
        if (!this._onTab('prompts')) return;
        const seq = this._renderSeq;

        const cached = this._knownPromptMetas();
        if (cached.length) {
            this._paintPromptList(cached);
            if (focusName) this._focusPromptRow(focusName);
        } else if (!this._promptsError) {
            this._content.innerHTML = this._promptListHeader('') + `
                <div class="sp-card sp-card--list">
                    <ul class="sp-prompt-list" aria-hidden="true">
                        ${Array.from({ length: 12 }, () => '<li><div class="skeleton" style="height:32px;margin:6px 0;border-radius:6px;"></div></li>').join('')}
                    </ul>
                </div>`;
        }

        await this._loadPrompts();
        if (seq !== this._renderSeq || !this._onTab('prompts') || this._promptOpen) return;

        const prompts = this._knownPromptMetas();
        if (!prompts.length) {
            this._content.innerHTML = this._promptListHeader('') + `
                <div class="sp-card">
                    <div class="sp-card-title">${h('settings.prompts.loadFailed')} <bdi dir="ltr">${_esc(this._promptsError || t('settings.prompts.noneReturned'))}</bdi></div>
                    <div class="sp-card-footer" style="margin-top:var(--space-3)">
                        <button type="button" class="sp-btn-ghost" id="sp-prompts-retry">${h('common.retry')}</button>
                    </div>
                </div>`;
            this._content.querySelector('#sp-prompts-retry')?.addEventListener('click', () => this._renderPromptList());
            return;
        }
        // Repainting replaces the focused row; put focus back on its successor.
        const focusedRow = document.activeElement?.closest?.('.sp-prompt-row')?.dataset.name || focusName;
        this._paintPromptList(prompts);
        if (focusedRow) this._focusPromptRow(focusedRow);
    }

    _paintPromptList(prompts) {
        // Group in order of first appearance; the registry already orders them.
        const groups = new Map();
        prompts.forEach(m => {
            const g = m.group || 'Other';
            if (!groups.has(g)) groups.set(g, []);
            groups.get(g).push(m);
        });

        const customCount = prompts.filter(m => m.is_custom).length;
        const overrideCount = prompts.filter(m => m.model_name).length;
        const summaryParts = [t('settings.prompts.count', { count: prompts.length })];
        summaryParts.push(customCount ? t('settings.prompts.customSummary', { count: customCount }) : t('settings.prompts.allDefault'));
        if (overrideCount) summaryParts.push(t('settings.prompts.overrideCount', { count: overrideCount }));

        const row = (m) => {
            const modelLabel = m.model_name ? this._modelDisplayName(m.model_name) : '';
            const ariaBits = [m.label || m.name];
            if (m.is_custom) ariaBits.push(t('settings.prompts.customBadge'));
            if (modelLabel) ariaBits.push(t('settings.prompts.runsOn', { model: iso(modelLabel) }));
            const ago = m.is_custom && m.updated_at ? _relativeTime(m.updated_at) : '';
            return `<li>
                <button type="button" class="sp-prompt-row" data-name="${_esc(m.name)}" aria-label="${_esc(ariaBits.join(', '))}">
                    <span class="sp-prompt-row-name">${_esc(m.label || m.name)}</span>
                    <span class="sp-prompt-row-desc" title="${_esc(m.description || '')}">${_esc(m.description || '')}</span>
                    <span class="sp-prompt-row-status">${m.is_custom
                        ? `<span class="sp-badge sp-badge-custom">${h('settings.prompts.customBadge')}</span>${ago ? `<span class="sp-prompt-row-ago" title="${_esc(m.updated_at)}">${_esc(ago)}</span>` : ''}`
                        : ''}</span>
                    <span class="sp-prompt-row-model" title="${modelLabel ? h('settings.prompts.runsOn', { model: iso(modelLabel) }) : ''}">${_esc(modelLabel)}</span>
                    <span class="sp-prompt-row-chev" aria-hidden="true">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
                    </span>
                </button>
            </li>`;
        };

        this._content.innerHTML = this._promptListHeader(_esc(summaryParts.join(' · '))) + Array.from(groups.entries()).map(([g, items]) => `
            <div class="sp-card sp-card--list">
                <h3 class="sp-card-title">${_esc(g)} <span class="sp-card-count">${items.length}</span></h3>
                <ul class="sp-prompt-list">${items.map(row).join('')}</ul>
            </div>`).join('');

        this._content.querySelectorAll('.sp-prompt-row').forEach(btn => {
            btn.addEventListener('click', () => this._openPrompt(btn.dataset.name));
        });
    }

    /** Human-friendly model label for a model name, falling back to the name. */
    _modelDisplayName(name) {
        const m = (this._models || []).find(x => x.name === name);
        return m ? (m.display_name || m.name) : name;
    }

    // ── Prompt editor ─────────────────────────────────────────────────────────

    async _renderPrompt(name) {
        if (!this._onTab('prompts') || this._promptOpen !== name) return;
        const seq = this._renderSeq;
        // Show loading skeleton while fetching
        if (!this._prompts[name] || this._prompts[name].content === null) {
            this._content.innerHTML = `
                <div class="sp-section-header">
                    ${this._promptBackLinkHtml()}
                    <div class="skeleton" style="height:24px;width:200px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:14px;width:400px;"></div>
                </div>
                <div class="sp-card" style="padding:24px">
                    <div class="skeleton" style="height:400px;width:100%;border-radius:8px;"></div>
                </div>`;
            this._wirePromptBackLink(name);
            await this._fetchPromptContent(name);
        }

        // Ensure the model list is available for the model selector.
        await this._ensureModels();
        await this._ensurePromptContexts();
        if (seq !== this._renderSeq || !this._onTab('prompts') || this._promptOpen !== name) return;

        const entry = this._prompts[name];
        if (!entry) return;

        const meta    = entry.meta || {};

        // A failed fetch must never offer Edit over an empty body — one Save
        // would overwrite the prompt with nothing.
        if (entry.content === null) {
            this._content.innerHTML = `
                <div class="sp-section-header">
                    ${this._promptBackLinkHtml()}
                    <h2 class="sp-section-title">${_esc(meta.label || name)}</h2>
                </div>
                <div class="sp-card">
                    <div class="sp-card-title">${h('settings.prompts.loadOneFailed')} <bdi dir="ltr">${_esc(entry.loadError || t('common.unknownError'))}</bdi></div>
                    <div class="sp-card-footer" style="margin-top:var(--space-3)">
                        <button type="button" class="sp-btn-ghost" id="sp-prompt-retry">${h('common.retry')}</button>
                    </div>
                </div>`;
            this._wirePromptBackLink(name);
            this._content.querySelector('#sp-prompt-retry')?.addEventListener('click', () => this._renderPrompt(name));
            return;
        }

        const content = entry.content || '';
        const isCustom = meta.is_custom || false;
        const placeholders = meta.placeholders || _extractPlaceholders(content);
        const isDirty = entry.dirty || false;
        const isEditing = entry.editing || false;
        const currentModelName = meta.model_name || null;
        entry.viewMode = entry.viewMode || 'template';
        const viewMode = isEditing ? 'template' : entry.viewMode;
        const promptBodyHtml = isEditing
            ? `<textarea class="sp-prompt-textarea" id="sp-prompt-ta-${name}" spellcheck="false">${_esc(content)}</textarea>`
            : viewMode === 'resolved'
                ? this._resolvedPromptBody(name, entry, content)
                : `<div class="sp-prompt-view" id="sp-prompt-view-${name}">${_renderPromptView(content)}</div>`;

        // Build model selector options.
        const availableModels = (this._models || []).filter(m => m.available);
        const modelOptions = [
            `<option value=""${!currentModelName ? ' selected' : ''}>${h('settings.prompts.defaultModel')}</option>`,
            ...availableModels.map(m =>
                `<option value="${_esc(m.name)}"${
                    m.name === currentModelName ? ' selected' : ''
                }>${_esc(m.display_name)}</option>`
            ),
        ].join('');

        this._content.innerHTML = `
            <div class="sp-section-header">
                ${this._promptBackLinkHtml()}
                <div class="sp-prompt-title-row">
                    <h2 class="sp-section-title">${_esc(meta.label || name)}</h2>
                    <span class="sp-badge ${isCustom ? 'sp-badge-custom' : 'sp-badge-default'}">${isCustom ? h('settings.prompts.customBadge') : h('settings.prompts.defaultBadge')}</span>
                </div>
                <p class="sp-section-desc">${_esc(meta.description || '')}</p>
                <div class="sp-prompt-model-row">
                    <span class="sp-prompt-model-label">${h('settings.prompts.runWithModel')}</span>
                    <select class="settings-select sp-prompt-model-sel" id="sp-prompt-model-${_esc(name)}">${modelOptions}</select>
                </div>
            </div>

            ${placeholders.length ? `
            <div class="sp-placeholder-row">
                <span class="sp-placeholder-label">${h('settings.prompts.placeholders')}</span>
                <div class="sp-placeholder-chips">
                    ${placeholders.map(p => `<code class="sp-ph-chip">{${_esc(p)}}</code>`).join('')}
                </div>
            </div>` : ''}

            ${!isEditing ? this._resolvedPromptControls(name, entry) : ''}

            <div class="sp-prompt-body" id="sp-prompt-body-${name}">
                ${promptBodyHtml}
            </div>

            <div class="sp-prompt-footer">
                <button class="sp-btn-ghost sp-btn-reset" id="sp-reset-${name}"
                    ${isCustom ? '' : 'disabled'}
                    title="${isCustom ? h('settings.prompts.restoreDefaultTitle') : h('settings.prompts.alreadyDefault')}">
                    ${h('settings.prompts.resetToDefault')}
                </button>
                <div class="sp-prompt-footer-right">
                    ${isEditing
                        ? `<button class="sp-btn-secondary" id="sp-cancel-${name}">${h('common.cancel')}</button>
                           <button class="sp-btn-primary" id="sp-save-${name}" ${isDirty ? '' : 'disabled'}>${h('settings.prompts.savePrompt')}</button>`
                        : `<button class="sp-btn-secondary" id="sp-edit-${name}">${h('common.edit')}</button>`
                    }
                </div>
            </div>

            ${meta.version > 1 ? `
            <details class="sp-version-history" id="sp-vh-${name}">
                <summary class="sp-vh-summary">
                    <span>${h('settings.prompts.versionHistory')}</span>
                    <span class="sp-vh-count">${h('settings.prompts.versionsSaved', { count: Number(meta.version) || 0 })}</span>
                </summary>
                <div class="sp-vh-body" id="sp-vh-body-${name}">
                    <p class="sp-vh-loading">${h('common.loading')}</p>
                </div>
            </details>` : ''}
        `;

        this._wirePromptBackLink(name);

        // Textarea live-dirty tracking; Save only lights up once something changed.
        const ta = this._content.querySelector(`#sp-prompt-ta-${name}`);
        const saveBtn = this._content.querySelector(`#sp-save-${name}`);
        if (ta) {
            ta.addEventListener('input', () => {
                entry.dirty = (ta.value !== content);
                if (saveBtn) saveBtn.disabled = !entry.dirty;
            });
        }

        // Template / Resolved view switch
        this._content.querySelectorAll(`#sp-prompt-view-mode-${name} button`).forEach(btn => {
            btn.addEventListener('click', async () => {
                const mode = btn.dataset.mode || 'template';
                entry.viewMode = mode;
                if (mode === 'resolved') {
                    await this._loadResolvedPrompt(name);
                }
                this._renderPrompt(name);
            });
        });

        const connSel = this._content.querySelector(`#sp-resolve-conn-${name}`);
        if (connSel) {
            connSel.addEventListener('change', async () => {
                this._promptResolveConnection = connSel.value;
                if (entry.viewMode === 'resolved') {
                    await this._loadResolvedPrompt(name, { force: true });
                }
                this._renderPrompt(name);
            });
        }

        this._content.querySelector(`#sp-resolve-refresh-${name}`)?.addEventListener('click', async () => {
            entry.viewMode = 'resolved';
            await this._loadResolvedPrompt(name, { force: true });
            this._renderPrompt(name);
        });

        // Edit button
        this._content.querySelector(`#sp-edit-${name}`)?.addEventListener('click', () => {
            entry.editing = true;
            this._renderPrompt(name);
        });

        // Cancel button
        this._content.querySelector(`#sp-cancel-${name}`)?.addEventListener('click', () => {
            entry.editing = false;
            entry.dirty   = false;
            this._renderPrompt(name);
        });

        // Save button
        this._content.querySelector(`#sp-save-${name}`)?.addEventListener('click', async () => {
            const newContent = this._content.querySelector(`#sp-prompt-ta-${name}`)?.value;
            if (newContent === undefined) return;
            await this._savePrompt(name, newContent);
        });

        // Model selector
        const modelSel = this._content.querySelector(`#sp-prompt-model-${name}`);
        if (modelSel) {
            modelSel.addEventListener('change', async () => {
                await this._setPromptModel(name, modelSel.value || null);
            });
        }

        // Reset button
        this._content.querySelector(`#sp-reset-${name}`)?.addEventListener('click', async () => {
            if (!isCustom) return;
            if (!confirm(t('settings.prompts.resetConfirm'))) return;
            await this._resetPrompt(name);
        });

        // Version history — lazy-load on first open
        const vhDetails = this._content.querySelector(`#sp-vh-${name}`);
        if (vhDetails) {
            vhDetails.addEventListener('toggle', () => {
                if (vhDetails.open) this._loadVersionHistory(name);
            }, { once: true });
        }
    }

    async _savePrompt(name, content) {
        const btn = this._content.querySelector(`#sp-save-${name}`);
        if (btn) { btn.disabled = true; btn.textContent = t('common.saving'); }
        try {
            const res = await fetch(`/api/settings/prompts/${name}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updatePromptsCount();
            this._renderPrompt(name);
            _showToast(t('settings.prompts.saved'), 'success');
        } catch (e) {
            console.error('[SettingsPage] save failed:', e);
            _showToast(t('settings.saveFailed', { detail: iso(e.message) }), 'error');
            if (btn) { btn.disabled = false; btn.textContent = t('settings.prompts.savePrompt'); }
        }
    }

    async _resetPrompt(name) {
        try {
            const res = await fetch(`/api/settings/prompts/${name}`, { method: 'DELETE' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updatePromptsCount();
            this._renderPrompt(name);
            _showToast(t('settings.prompts.resetDone'), 'info');
        } catch (e) {
            console.error('[SettingsPage] reset failed:', e);
            _showToast(t('settings.prompts.resetFailed', { detail: iso(e.message) }), 'error');
        }
    }

    // ── Version history ─────────────────────────────────────────────────────

    async _loadVersionHistory(name) {
        const body = this._content.querySelector(`#sp-vh-body-${name}`);
        if (!body) return;
        body.innerHTML = '<p class="sp-vh-loading">Loading&#8230;</p>';
        try {
            const res = await fetch(`/api/settings/prompts/${name}/versions`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const versions = await res.json();
            this._renderVersionRows(name, versions);
        } catch (e) {
            body.innerHTML = `<p class="sp-vh-loading">Failed to load: ${_esc(e.message)}</p>`;
        }
    }

    _renderVersionRows(name, versions) {
        const body = this._content.querySelector(`#sp-vh-body-${name}`);
        if (!body) return;
        if (!versions.length) {
            body.innerHTML = `<p class="sp-vh-loading">${h('settings.prompts.noHistory')}</p>`;
            return;
        }
        body.innerHTML = versions.map(v => {
            const dt = v.created_at ? new Date(v.created_at) : null;
            const dateStr = dt
                ? dt.toLocaleDateString(undefined, { year:'numeric', month:'short', day:'numeric' })
                  + ' ' + dt.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' })
                : '';
            const activeBadge = v.is_active
                ? '<span class="sp-badge sp-badge-active">Active</span>' : '';
            const customBadge = v.is_custom
                ? '<span class="sp-badge sp-badge-custom">Custom</span>'
                : '<span class="sp-badge sp-badge-default">Default</span>';
            const restoreBtn = !v.is_active
                ? `<button class="sp-vh-btn sp-vh-restore" data-vid="${v.id}">Restore</button>` : '';
            return `<div class="sp-vh-row">
                <div class="sp-vh-row-header">
                    <div class="sp-vh-meta">
                        <span class="sp-vh-ver">v${v.version}</span>
                        <span class="sp-vh-date">${_esc(dateStr)}</span>
                        ${activeBadge}${customBadge}
                    </div>
                    <div class="sp-vh-actions">
                        <button class="sp-vh-btn sp-vh-preview" data-vid="${v.id}">Preview</button>
                        ${restoreBtn}
                    </div>
                </div>
                <div class="sp-vh-preview-area" id="sp-vh-pa-${v.id}" style="display:none"></div>
            </div>`;
        }).join('');

        body.querySelectorAll('.sp-vh-preview').forEach(btn => {
            btn.addEventListener('click', async () => {
                await this._previewVersion(name, Number(btn.dataset.vid), btn);
            });
        });
        body.querySelectorAll('.sp-vh-restore').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm(t('settings.prompts.restoreConfirm'))) return;
                await this._restoreVersion(name, Number(btn.dataset.vid));
            });
        });
    }

    async _previewVersion(name, versionId, btnEl) {
        const area = this._content.querySelector(`#sp-vh-pa-${versionId}`);
        if (!area) return;
        // Toggle off if already visible
        if (area.style.display !== 'none') {
            area.style.display = 'none';
            btnEl.textContent = t('settings.prompts.preview');
            return;
        }
        // Lazy-load content on first open
        if (!area.dataset.loaded) {
            const origText = btnEl.textContent;
            btnEl.disabled = true;
            btnEl.textContent = t('common.loading');
            try {
                const res = await fetch(`/api/settings/prompts/${name}/versions/${versionId}`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                area.innerHTML = `<pre class="sp-vh-pre">${_esc(data.content)}</pre>`;
                area.dataset.loaded = '1';
            } catch (e) {
                area.innerHTML = `<p class="sp-vh-loading" style="color:var(--color-error)">${_esc(e.message)}</p>`;
            } finally {
                btnEl.disabled = false;
                btnEl.textContent = origText;
            }
        }
        area.style.display = '';
        btnEl.textContent = t('common.hide');
    }

    async _restoreVersion(name, versionId) {
        try {
            const res = await fetch(`/api/settings/prompts/${name}/restore/${versionId}`, {
                method: 'POST',
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updatePromptsCount();
            this._renderPrompt(name);
            _showToast(t('settings.prompts.restored', { from: data.version - 1, to: data.version }), 'success');
        } catch (e) {
            console.error('[SettingsPage] restore failed:', e);
            _showToast(t('settings.prompts.restoreFailed', { detail: iso(e.message) }), 'error');
        }
    }

    // ── Users management ───────────────────────────────────────────────────

    // ── Integrations (admin: connector registry & authorization) ──────────────

    async _renderIntegrations() {
        if (!this._onTab('integrations')) return;
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.integrations.title')}</h2>
                <p class="sp-section-desc">${h('settings.integrations.desc')}</p>
            </div>
            <div class="sp-card">
                ${this._row(t('settings.integrations.enable.label'), t('settings.integrations.enable.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-conn-feature"><span class="sp-switch-slider"></span></label>`)}
            </div>
            <div class="sp-card">
                ${this._row(t('settings.integrations.agentTools.label'), t('settings.integrations.agentTools.help'), `
                    <label class="sp-switch"><input type="checkbox" id="sp-agent-tools-feature"><span class="sp-switch-slider"></span></label>`)}
            </div>
            <div id="sp-conn-body"><div class="sp-conn-empty">Loading…</div></div>
        `;

        const chk = this._content.querySelector('#sp-conn-feature');
        const agentChk = this._content.querySelector('#sp-agent-tools-feature');
        const body = this._content.querySelector('#sp-conn-body');

        let enabled = false;
        try {
            const r = await fetch('/api/connectors/feature');
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            enabled = !!(await r.json()).enabled;
        } catch (e) {
            body.innerHTML = `<div class="sp-conn-empty">${h('settings.myConnections.loadFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></div>`;
            return;
        }
        chk.checked = enabled;

        // Independent agent-tools switch (only meaningful while the master switch
        // is on). Read its state and wire the toggle; disable it when connectors
        // are off so it cannot be turned on in isolation.
        let agentEnabled = false;
        try {
            const ar = await fetch('/api/connectors/agent-tools-feature');
            if (ar.ok) agentEnabled = !!(await ar.json()).enabled;
        } catch (e) { /* leave off; fail closed */ }
        agentChk.checked = agentEnabled;
        agentChk.disabled = !enabled;
        agentChk.addEventListener('change', async () => {
            const want = agentChk.checked;
            try {
                const r = await fetch('/api/connectors/agent-tools-feature', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: want }),
                });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                _showToast(want ? t('settings.integrations.agentToolsOn') : t('settings.integrations.agentToolsOff'), want ? 'success' : 'info');
            } catch (e) {
                agentChk.checked = !want;
                _showToast(t('settings.updateFailed', { detail: iso(e.message) }), 'error');
            }
        });

        chk.addEventListener('change', async () => {
            const want = chk.checked;
            try {
                const r = await fetch('/api/connectors/feature', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: want }),
                });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                _showToast(want ? t('settings.integrations.connectorsOn') : t('settings.integrations.connectorsOff'), want ? 'success' : 'info');
                this._renderIntegrations();
            } catch (e) {
                chk.checked = !want;
                _showToast(t('settings.updateFailed', { detail: iso(e.message) }), 'error');
            }
        });

        if (enabled) {
            await this._loadIntegrationsBody(body);
        } else {
            body.innerHTML = `<div class="sp-card"><div class="sp-conn-empty">${h('settings.integrations.masterOff')}</div></div>`;
        }
    }

    async _loadIntegrationsBody(body) {
        if (!this._onTab('integrations')) return;
        let catalog = [], connectors = [], groupRoles = [];
        try {
            const [cRes, lRes, gRes] = await Promise.all([
                fetch('/api/connectors/catalog'),
                fetch('/api/connectors'),
                fetch('/api/connectors/group-roles'),
            ]);
            if (cRes.ok) catalog = (await cRes.json()).catalog || [];
            if (lRes.ok) connectors = (await lRes.json()).connectors || [];
            if (gRes.ok) groupRoles = (await gRes.json()).group_roles || [];
        } catch (e) {
            body.innerHTML = `<div class="sp-conn-empty">${h('settings.integrations.loadFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></div>`;
            return;
        }

        const added = new Set(connectors.map(c => c.key));
        const addable = catalog.filter(e => !e.coming_soon && !added.has(e.key));
        const soon = catalog.filter(e => e.coming_soon);

        body.innerHTML = `
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.integrations.connectors')}</div>
                <div id="sp-conn-list">
                    ${connectors.length ? '' : `<div class="sp-conn-empty">${h('settings.integrations.noConnectors')}</div>`}
                </div>
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.integrations.addConnector')}</div>
                <div class="sp-conn-row" id="sp-conn-add">
                    ${addable.length ? `
                        <select class="sp-conn-input" id="sp-conn-add-sel">
                            ${addable.map(e => `<option value="${_esc(e.key)}">${_esc(e.display_name)}</option>`).join('')}
                        </select>
                        <button class="sp-btn-primary-sm" id="sp-conn-add-btn">${h('common.add')}</button>
                    ` : `<div class="sp-conn-empty">${h('settings.integrations.allAdded')}</div>`}
                </div>
                ${soon.length ? `<div class="sp-conn-sub" style="margin-top:10px">${h('settings.integrations.comingSoon', { names: iso(soon.map(e => e.display_name).join(', ')) })}</div>` : ''}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.integrations.groupRoles')}</div>
                <p class="sp-conn-sub" style="margin-bottom:10px">${h('settings.integrations.groupRolesHelp')}</p>
                <div id="sp-conn-roles"></div>
                <div class="sp-conn-row" style="margin-top:10px">
                    <input class="sp-conn-input" id="sp-role-group" dir="ltr" placeholder="${h('settings.integrations.groupIdPlaceholder')}">
                    <select class="sp-conn-input" id="sp-role-role" style="min-width:120px;flex:0 0 auto">
                        <option value="viewer">${h('settings.users.roles.viewer')}</option>
                        <option value="editor">${h('settings.users.roles.editor')}</option>
                        <option value="admin">${h('settings.users.roles.admin')}</option>
                    </select>
                    <button class="sp-btn-primary-sm" id="sp-role-add">${h('settings.integrations.setRole')}</button>
                </div>
            </div>
            <div class="sp-card">
                <div class="sp-card-title">${h('settings.integrations.recentActivity')}</div>
                <div id="sp-conn-audit"><div class="sp-conn-empty">${h('common.loading')}</div></div>
            </div>
        `;

        const list = body.querySelector('#sp-conn-list');
        connectors.forEach(c => {
            const el = document.createElement('div');
            el.innerHTML = this._connectorItemHtml(c);
            const item = el.firstElementChild;
            list.appendChild(item);
            this._wireConnectorItem(item, c);
        });

        const addBtn = body.querySelector('#sp-conn-add-btn');
        if (addBtn) {
            addBtn.addEventListener('click', async () => {
                const key = body.querySelector('#sp-conn-add-sel').value;
                try {
                    const r = await fetch('/api/connectors', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ catalog_key: key }),
                    });
                    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    _showToast(t('settings.integrations.connectorAdded'), 'success');
                    this._loadIntegrationsBody(body);
                } catch (e) {
                    _showToast(t('settings.integrations.addFailed', { detail: iso(e.message) }), 'error');
                }
            });
        }

        this._renderGroupRoles(body.querySelector('#sp-conn-roles'), groupRoles, body);
        body.querySelector('#sp-role-add')?.addEventListener('click', async () => {
            const group = (body.querySelector('#sp-role-group').value || '').trim();
            const role = body.querySelector('#sp-role-role').value;
            if (!group) { _showToast(t('settings.integrations.enterGroupId'), 'error'); return; }
            try {
                const r = await fetch('/api/connectors/group-roles', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ group_object_id: group, role }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.groupRoleSet'), 'success');
                this._loadIntegrationsBody(body);
            } catch (e) {
                _showToast(t('settings.integrations.setRoleFailed', { detail: iso(e.message) }), 'error');
            }
        });

        this._loadConnectorAudit(body.querySelector('#sp-conn-audit'));
    }

    _connectorItemHtml(c) {
        const cfg = (c.current_version && c.current_version.config) || {};
        const allowlist = Array.isArray(cfg.recipient_domain_allowlist) ? cfg.recipient_domain_allowlist : [];
        const allowExternal = !!cfg.allow_external_recipients;
        const clientId = cfg.client_id || '';
        const tenantId = cfg.tenant_id || '';
        const isApiKey = c.auth_kind === 'api_key';
        const isGraphMail = c.key === 'microsoft-graph-mail';
        const pill = isApiKey
            ? (c.has_api_key
                ? `<span class="sp-conn-pill on">${h('settings.integrations.keySet')}</span>`
                : `<span class="sp-conn-pill warn">${h('settings.integrations.noKey')}</span>`)
            : (c.has_client_secret
                ? `<span class="sp-conn-pill on">${h('settings.integrations.secretSet')}</span>`
                : `<span class="sp-conn-pill warn">${h('settings.integrations.noSecret')}</span>`);
        const grants = c.group_grants || [];

        // Credential block adapts to the auth model.
        const credBlock = isApiKey
            ? `
                    <div class="sp-conn-field">
                        <label>${h('settings.integrations.apiKeyLabel')}</label>
                        <div class="sp-conn-row">
                            <input class="sp-conn-input sp-cf-apikey" type="password" dir="ltr" placeholder="${h('settings.integrations.apiKeyPlaceholder')}">
                            <button class="sp-btn-primary-sm sp-cf-apikey-save">${h('settings.integrations.saveKey')}</button>
                        </div>
                    </div>`
            : `
                    <div class="sp-conn-field">
                        <label>${h('settings.integrations.oauthClientLabel')}</label>
                        <div class="sp-conn-row">
                            <input class="sp-conn-input sp-cf-client" dir="ltr" placeholder="${h('settings.integrations.clientId')}" value="${_esc(clientId)}">
                            <input class="sp-conn-input sp-cf-tenant" dir="ltr" placeholder="${h('settings.integrations.tenantId')}" value="${_esc(tenantId)}">
                        </div>
                        <div class="sp-conn-row" style="margin-top:6px">
                            <input class="sp-conn-input sp-cf-secret" type="password" dir="ltr" placeholder="${h('settings.integrations.clientSecret')}">
                            <button class="sp-btn-primary-sm sp-cf-secret-save">${h('settings.integrations.saveSecret')}</button>
                        </div>
                    </div>`;

        // Policy/config block: recipient policy for Graph Mail; a generic JSON
        // config editor for other OAuth connectors (Slack channels / Jira cloud id
        // + projects); nothing extra for API-key connectors.
        let policyBlock = '';
        if (isGraphMail) {
            policyBlock = `
                    <div class="sp-conn-field">
                        <label>${h('settings.integrations.recipientPolicy')}</label>
                        <div class="sp-conn-row">
                            <input class="sp-conn-input sp-cf-allowlist" dir="ltr" placeholder="${h('settings.integrations.allowedDomains')}" value="${_esc(allowlist.join(', '))}">
                        </div>
                        <div class="sp-conn-row" style="margin-top:6px">
                            <label class="sp-conn-sub" style="display:flex;align-items:center;gap:8px;cursor:pointer">
                                <input type="checkbox" class="sp-cf-external" ${allowExternal ? 'checked' : ''}> ${h('settings.integrations.allowExternal')}
                            </label>
                            <button class="sp-btn-ghost-sm sp-cf-policy-save" style="margin-inline-start:auto">${h('settings.integrations.savePolicy')}</button>
                        </div>
                    </div>`;
        } else if (!isApiKey) {
            policyBlock = `
                    <div class="sp-conn-field">
                        <label>${h('settings.integrations.configJson')}</label>
                        <div class="sp-conn-row">
                            <textarea class="sp-conn-input sp-cf-config" rows="5" style="font-family:monospace;font-size:0.8125rem">${_esc(JSON.stringify(cfg, null, 2))}</textarea>
                        </div>
                        <div class="sp-conn-row" style="margin-top:6px">
                            <button class="sp-btn-ghost-sm sp-cf-config-save" style="margin-inline-start:auto">${h('settings.integrations.saveConfig')}</button>
                        </div>
                    </div>`;
        }

        return `
            <div class="sp-conn-item" data-cid="${_esc(c.id)}">
                <div class="sp-conn-head">
                    <div>
                        <div class="sp-conn-title">${_esc(c.display_name)}</div>
                        <div class="sp-conn-sub">${_esc(c.provider)} · ${_esc(c.category || '')} · ${_esc(c.auth_kind || 'oauth')}</div>
                    </div>
                    <div class="sp-conn-actions">
                        ${pill}
                        <span class="sp-conn-health-txt" title="${h('settings.integrations.configHealth')}"></span>
                        <button class="sp-btn-ghost-sm sp-conn-health" title="${h('settings.integrations.checkConfig')}">${h('settings.integrations.check')}</button>
                        <label class="sp-switch"><input type="checkbox" class="sp-conn-enabled" ${c.is_enabled ? 'checked' : ''}><span class="sp-switch-slider"></span></label>
                        <button class="sp-btn-ghost-sm sp-conn-del" title="${h('settings.integrations.removeConnector')}">${h('common.remove')}</button>
                    </div>
                </div>
                <div class="sp-conn-body">
                    ${credBlock}
                    ${policyBlock}
                    <div class="sp-conn-field">
                        <label>${h('settings.integrations.allowedGroups')}</label>
                        <div class="sp-conn-row sp-cf-grants">
                            ${grants.length ? grants.map(g => `<span class="sp-chip" data-g="${_esc(g)}" dir="ltr">${_esc(g)}<button class="sp-grant-del" title="${h('common.remove')}">×</button></span>`).join('') : `<span class="sp-conn-empty" style="padding:0">${h('settings.integrations.noGroups')}</span>`}
                        </div>
                        <div class="sp-conn-row" style="margin-top:6px">
                            <input class="sp-conn-input sp-cf-grant-input" dir="ltr" placeholder="${h('settings.integrations.groupIdPlaceholder')}">
                            <button class="sp-btn-ghost-sm sp-cf-grant-add">${h('settings.integrations.addGroup')}</button>
                        </div>
                    </div>
                </div>
            </div>`;
    }

    _wireConnectorItem(item, c) {
        const cid = c.id;
        const body = this._content.querySelector('#sp-conn-body');
        const reload = () => this._loadIntegrationsBody(body);

        item.querySelector('.sp-conn-enabled')?.addEventListener('change', async (e) => {
            const enabled = e.target.checked;
            try {
                const r = await fetch(`/api/connectors/${cid}/enabled`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled }),
                });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                _showToast(enabled ? 'Connector enabled' : 'Connector disabled', 'success');
            } catch (err) {
                e.target.checked = !enabled;
                _showToast(t('settings.updateFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-conn-del')?.addEventListener('click', async () => {
            if (!confirm(t('settings.integrations.removeConfirm', { name: iso(c.display_name) }))) return;
            try {
                const r = await fetch(`/api/connectors/${cid}`, { method: 'DELETE' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                _showToast(t('settings.integrations.connectorRemoved'), 'info');
                reload();
            } catch (err) {
                _showToast(t('settings.removeFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-conn-health')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const txt = item.querySelector('.sp-conn-health-txt');
            btn.disabled = true;
            if (txt) { txt.textContent = '…'; txt.className = 'sp-conn-health-txt'; }
            try {
                const r = await fetch(`/api/connectors/${cid}/health`);
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
                if (txt) {
                    if (d.ok) {
                        txt.textContent = `${t('settings.integrations.ready')} · v${d.version ?? '?'}`;
                        txt.className = 'sp-conn-health-txt ok';
                    } else {
                        const why = !d.enabled ? t('settings.integrations.disabled')
                            : !d.configured ? (d.auth_kind === 'api_key' ? t('settings.integrations.noKey') : t('settings.integrations.noSecret'))
                            : (d.reason || t('settings.integrations.notReady'));
                        txt.textContent = why;
                        txt.className = 'sp-conn-health-txt warn';
                    }
                }
            } catch (err) {
                if (txt) { txt.textContent = t('settings.integrations.checkFailed'); txt.className = 'sp-conn-health-txt warn'; }
                _showToast(t('settings.integrations.healthFailed', { detail: iso(err.message) }), 'error');
            } finally {
                btn.disabled = false;
            }
        });

        item.querySelector('.sp-cf-secret-save')?.addEventListener('click', async () => {
            const secret = item.querySelector('.sp-cf-secret').value;
            const client_id = item.querySelector('.sp-cf-client').value.trim();
            const tenant_id = item.querySelector('.sp-cf-tenant').value.trim();
            if (!secret) { _showToast(t('settings.integrations.enterSecret'), 'error'); return; }
            try {
                const r = await fetch(`/api/connectors/${cid}/client-secret`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ secret, client_id, tenant_id }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.secretSaved'), 'success');
                reload();
            } catch (err) {
                _showToast(t('settings.integrations.secretSaveFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-cf-apikey-save')?.addEventListener('click', async () => {
            const api_key = item.querySelector('.sp-cf-apikey').value.trim();
            if (!api_key) { _showToast(t('settings.integrations.enterApiKey'), 'error'); return; }
            try {
                const r = await fetch(`/api/connectors/${cid}/api-key`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ api_key }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.apiKeySaved'), 'success');
                reload();
            } catch (err) {
                _showToast(t('settings.integrations.apiKeySaveFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-cf-config-save')?.addEventListener('click', async () => {
            let config;
            try {
                config = JSON.parse(item.querySelector('.sp-cf-config').value || '{}');
            } catch (e) {
                _showToast(t('settings.integrations.configInvalidJson'), 'error');
                return;
            }
            try {
                const r = await fetch(`/api/connectors/${cid}/config`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.configSaved'), 'success');
                reload();
            } catch (err) {
                _showToast(t('settings.integrations.configSaveFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-cf-policy-save')?.addEventListener('click', async () => {
            const allowlist = item.querySelector('.sp-cf-allowlist').value
                .split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
            const allow_external_recipients = item.querySelector('.sp-cf-external').checked;
            const existing = (c.current_version && c.current_version.config) || {};
            const config = {
                ...existing,
                recipient_domain_allowlist: allowlist,
                allow_external_recipients,
            };
            try {
                const r = await fetch(`/api/connectors/${cid}/config`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.policySaved'), 'success');
                reload();
            } catch (err) {
                _showToast(t('settings.integrations.policySaveFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelector('.sp-cf-grant-add')?.addEventListener('click', async () => {
            const group_object_id = (item.querySelector('.sp-cf-grant-input').value || '').trim();
            if (!group_object_id) { _showToast(t('settings.integrations.enterGroupId'), 'error'); return; }
            try {
                const r = await fetch(`/api/connectors/${cid}/groups`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ group_object_id }),
                });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.integrations.groupAdded'), 'success');
                reload();
            } catch (err) {
                _showToast(t('settings.integrations.groupAddFailed', { detail: iso(err.message) }), 'error');
            }
        });

        item.querySelectorAll('.sp-grant-del').forEach(btn => {
            btn.addEventListener('click', async () => {
                const gid = btn.closest('.sp-chip')?.dataset.g;
                if (!gid) return;
                try {
                    const r = await fetch(`/api/connectors/${cid}/groups/${encodeURIComponent(gid)}`, { method: 'DELETE' });
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    _showToast(t('settings.integrations.groupRemoved'), 'info');
                    reload();
                } catch (err) {
                    _showToast(t('settings.integrations.groupRemoveFailed', { detail: iso(err.message) }), 'error');
                }
            });
        });
    }

    _renderGroupRoles(container, roles, body) {
        if (!container) return;
        if (!roles.length) {
            container.innerHTML = `<div class="sp-conn-empty" style="padding:0">${h('settings.integrations.noGroupRoles')}</div>`;
            return;
        }
        container.innerHTML = roles.map(r => `
            <div class="sp-conn-row" style="justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--color-border)">
                <span class="sp-chip" style="background:none;border:none;padding:0">${_esc(r.group_object_id)}</span>
                <span class="sp-conn-row" style="gap:8px">
                    <span class="sp-conn-pill on">${_esc(r.role)}</span>
                    <button class="sp-btn-ghost-sm sp-role-del" data-rid="${_esc(r.id)}">${h('common.remove')}</button>
                </span>
            </div>`).join('');
        container.querySelectorAll('.sp-role-del').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    const r = await fetch(`/api/connectors/group-roles/${encodeURIComponent(btn.dataset.rid)}`, { method: 'DELETE' });
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    _showToast(t('settings.integrations.groupRoleRemoved'), 'info');
                    this._loadIntegrationsBody(body);
                } catch (e) {
                    _showToast(t('settings.removeFailed', { detail: iso(e.message) }), 'error');
                }
            });
        });
    }

    async _loadConnectorAudit(container) {
        if (!container) return;
        try {
            const r = await fetch('/api/connectors/audit?limit=50');
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const events = (await r.json()).events || [];
            if (!events.length) {
                container.innerHTML = `<div class="sp-conn-empty" style="padding:0">${h('settings.integrations.noActivity')}</div>`;
                return;
            }
            container.innerHTML = events.map(ev => {
                const ts = ev.event_time ? (window.I18n ? window.I18n.formatDate(ev.event_time, 'dateTime') : new Date(ev.event_time).toLocaleString()) : '';
                return `<div class="sp-audit-row">
                    <span class="sp-audit-ts">${_esc(ts)}</span>
                    <span class="sp-audit-ev">${_esc(ev.event_type || '')}</span>
                    <span class="sp-audit-out">${_esc(ev.outcome || '')}</span>
                </div>`;
            }).join('');
        } catch (e) {
            container.innerHTML = `<div class="sp-conn-empty" style="padding:0">${h('settings.integrations.activityFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></div>`;
        }
    }

    // ── My Connections (user: connect/disconnect personal integrations) ───────

    async _renderMyConnections() {
        if (!this._onTab('my-connections')) return;
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.myConnections.title')}</h2>
                <p class="sp-section-desc">${h('settings.myConnections.desc')}</p>
            </div>
            <div id="sp-myconn-list"><div class="sp-conn-empty">${h('common.loading')}</div></div>
        `;
        await this._loadMyConnections();
    }

    async _loadMyConnections() {
        if (!this._onTab('my-connections')) return;
        const list = this._content.querySelector('#sp-myconn-list');
        if (!list) return;
        let connections = [];
        try {
            const r = await fetch('/api/me/connections');
            if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
            connections = (await r.json()).connections || [];
        } catch (e) {
            list.innerHTML = `<div class="sp-card"><div class="sp-conn-empty">${h('settings.myConnections.loadFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></div></div>`;
            return;
        }
        if (!connections.length) {
            list.innerHTML = `<div class="sp-card"><div class="sp-conn-empty">${h('settings.myConnections.empty')}</div></div>`;
            return;
        }
        list.innerHTML = `<div class="sp-card">${connections.map(c => this._myConnectionHtml(c)).join('')}</div>`;
        connections.forEach(c => this._wireMyConnection(list, c));
    }

    _myConnectionHtml(c) {
        const connected = !!c.connected;
        const pill = connected
            ? `<span class="sp-conn-pill on">${h('settings.myConnections.connected')}</span>`
            : `<span class="sp-conn-pill off">${h('settings.myConnections.notConnected')}</span>`;
        const acct = connected && c.external_account ? `<div class="sp-conn-sub">${_esc(c.external_account)}</div>` : '';
        const action = connected
            ? `<button class="sp-btn-ghost-sm sp-myconn-revoke" data-cid="${_esc(c.connector_id)}">${h('settings.myConnections.disconnect')}</button>`
            : `<button class="sp-btn-primary-sm sp-myconn-connect" data-cid="${_esc(c.connector_id)}">${h('settings.myConnections.connect')}</button>`;
        return `
            <div class="sp-conn-item" data-cid="${_esc(c.connector_id)}" style="margin-bottom:10px">
                <div class="sp-conn-head">
                    <div>
                        <div class="sp-conn-title">${_esc(c.display_name)}</div>
                        <div class="sp-conn-sub">${_esc(c.category || '')}</div>
                        ${acct}
                    </div>
                    <div class="sp-conn-actions">${pill}${action}</div>
                </div>
            </div>`;
    }

    _wireMyConnection(root, c) {
        const item = root.querySelector(`.sp-conn-item[data-cid="${CSS.escape(c.connector_id)}"]`);
        if (!item) return;
        item.querySelector('.sp-myconn-connect')?.addEventListener('click', () => {
            // Full-page redirect: browser round-trip to the provider for consent.
            window.location.href = `/integrations/${encodeURIComponent(c.connector_id)}/connect`;
        });
        item.querySelector('.sp-myconn-revoke')?.addEventListener('click', async () => {
            if (!confirm(t('settings.myConnections.disconnectConfirm', { name: iso(c.display_name) }))) return;
            try {
                const r = await fetch(`/api/me/connections/${encodeURIComponent(c.connector_id)}/revoke`, { method: 'POST' });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                _showToast(t('settings.myConnections.disconnected'), 'info');
                this._loadMyConnections();
            } catch (e) {
                _showToast(t('settings.myConnections.disconnectFailed', { detail: iso(e.message) }), 'error');
            }
        });
    }

    async _renderUsers() {
        if (!this._onTab('users')) return;

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.users.title')}</h2>
                <p class="sp-section-desc">${h('settings.users.desc')}</p>
            </div>
            <div class="sp-card" style="padding:var(--space-4)">
                <div id="sp-users-loading"><div class="skeleton" style="height:40px;border-radius:8px;margin-bottom:8px;"></div></div>
                <div id="sp-users-body" style="display:none">
                    <table class="sp-users-table">
                        <thead><tr>
                            <th>${h('settings.users.member')}</th><th>${h('settings.users.role')}</th><th style="width:40px"></th>
                        </tr></thead>
                        <tbody id="sp-users-rows"></tbody>
                    </table>
                    <!-- Add user form -->
                    <div class="sp-add-user-form" id="sp-add-user-form">
                        <input id="sp-add-name"     class="sp-add-user-input sp-add-full" type="text" dir="auto" placeholder="${h('settings.users.fullName')}" />
                        <input id="sp-add-email"    class="sp-add-user-input" type="text" dir="ltr" placeholder="${h('login.emailLabel')}" />
                        <input id="sp-add-password" class="sp-add-user-input" type="password" dir="ltr" placeholder="${h('settings.users.passwordPlaceholder')}" />
                        <select id="sp-add-role" class="sp-add-user-select">
                            <option value="viewer">${h('settings.users.roles.viewer')}</option>
                            <option value="editor" selected>${h('settings.users.roles.editor')}</option>
                            <option value="admin">${h('settings.users.roles.admin')}</option>
                        </select>
                        <div class="sp-add-full" style="display:flex;align-items:center;gap:var(--space-3)">
                            <button class="sp-add-user-submit" id="sp-add-submit">${h('settings.users.addUser')}</button>
                            <span class="sp-users-error" id="sp-add-error"></span>
                        </div>
                    </div>
                </div>
            </div>`;

        await this._loadUsers();
    }

    async _loadUsers() {
        if (!this._onTab('users')) return;
        const seq = this._renderSeq;
        try {
            const res = await fetch('/api/users');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const users = await res.json();
            if (seq !== this._renderSeq) return;

            const loading = document.getElementById('sp-users-loading');
            const body = document.getElementById('sp-users-body');
            if (loading) loading.style.display = 'none';
            if (body) body.style.display = 'block';

            this._renderUserRows(users);
            this._wireAddForm();
        } catch (e) {
            if (seq !== this._renderSeq) return;
            const loading = document.getElementById('sp-users-loading');
            if (loading) loading.innerHTML =
                `<p style="color:var(--color-muted);font-size:13px">${h('settings.users.loadFailed')} <bdi dir="ltr">${_esc(e.message)}</bdi></p>`;
        }
    }

    _renderUserRows(users) {
        if (!this._onTab('users')) return;
        const me = window._currentUser || {};
        const tbody = document.getElementById('sp-users-rows');
        if (!tbody) return;

        tbody.innerHTML = users.map(u => {
            const isMe = u.id === me.id;
            const initials = _initials(u.name || u.email);
            const hueStyle = `background:hsl(${u.avatar_hue ?? 220},55%,52%);color:#fff`;
            const youBadge = isMe ? `<span class="sp-user-you">${h('settings.users.you')}</span>` : '';

            const roleSelect = `
                <select class="sp-role-select" data-uid="${u.id}" ${isMe ? 'disabled' : ''}>
                    <option value="admin"   ${u.role === 'admin'   ? 'selected' : ''}>${h('settings.users.roles.admin')}</option>
                    <option value="editor"  ${u.role === 'editor'  ? 'selected' : ''}>${h('settings.users.roles.editor')}</option>
                    <option value="viewer"  ${u.role === 'viewer'  ? 'selected' : ''}>${h('settings.users.roles.viewer')}</option>
                </select>`;

            const delBtn = isMe ? '' : `
                <button class="sp-user-del-btn" data-uid="${u.id}" title="${h('settings.users.removeUser')}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="3 6 5 6 21 6"/>
                        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
                        <path d="M10 11v6"/><path d="M14 11v6"/>
                        <path d="M9 6V4h6v2"/>
                    </svg>
                </button>`;

            return `<tr>
                <td>
                    <div class="sp-user-cell">
                        <div class="sp-user-avatar" style="${hueStyle}">${_esc(initials)}</div>
                        <div>
                            <div class="sp-user-name"><bdi>${_esc(u.name || u.email)}</bdi>${youBadge}</div>
                            <div class="sp-user-email"><bdi dir="ltr">${_esc(u.email)}</bdi></div>
                        </div>
                    </div>
                </td>
                <td>${roleSelect}</td>
                <td>${delBtn}</td>
            </tr>`;
        }).join('');

        // Wire role changes
        tbody.querySelectorAll('.sp-role-select').forEach(sel => {
            sel.addEventListener('change', async () => {
                const uid  = Number(sel.dataset.uid);
                const role = sel.value;
                try {
                    const r = await fetch(`/api/users/${uid}/role`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ role }),
                    });
                    if (!r.ok) throw new Error((await r.json()).error || `HTTP ${r.status}`);
                    _showToast(t('settings.users.roleUpdated', { role: t(`settings.users.roles.${role}`) }), 'success');
                } catch (e) {
                    _showToast(t('settings.users.roleUpdateFailed', { detail: iso(e.message) }), 'error');
                    await this._loadUsers(); // revert UI
                }
            });
        });

        // Wire delete buttons
        tbody.querySelectorAll('.sp-user-del-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const uid = Number(btn.dataset.uid);
                const user = users.find(u => u.id === uid);
                if (!confirm(t('settings.users.removeConfirm', { name: iso(user?.name || user?.email || '') }))) return;
                try {
                    const r = await fetch(`/api/users/${uid}`, { method: 'DELETE' });
                    if (!r.ok) throw new Error((await r.json()).error || `HTTP ${r.status}`);
                    _showToast(t('settings.users.removed'), 'info');
                    await this._loadUsers();
                } catch (e) {
                    _showToast(t('settings.users.removeFailed', { detail: iso(e.message) }), 'error');
                }
            });
        });
    }

    _wireAddForm() {
        const btn = document.getElementById('sp-add-submit');
        const err = document.getElementById('sp-add-error');
        if (!btn) return;

        btn.addEventListener('click', async () => {
            err.textContent = '';
            const name     = (document.getElementById('sp-add-name')?.value     || '').trim();
            const email    = (document.getElementById('sp-add-email')?.value    || '').trim();
            const password = (document.getElementById('sp-add-password')?.value || '');
            const role     =  document.getElementById('sp-add-role')?.value     || 'viewer';

            if (!name || !email || !password) { err.textContent = t('settings.users.allRequired'); return; }
            if (password.length < 8)           { err.textContent = t('settings.users.passwordTooShort'); return; }

            btn.disabled = true; btn.textContent = t('settings.users.adding');
            try {
                const r = await fetch('/api/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, email, password, role }),
                });
                const data = await r.json();
                if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);

                // Clear form
                ['sp-add-name','sp-add-email','sp-add-password'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.value = '';
                });
                _showToast(t('settings.users.added', { name: iso(name) }), 'success');
                await this._loadUsers();
            } catch (e) {
                err.textContent = e.message;
            } finally {
                btn.disabled = false; btn.textContent = t('settings.users.addUser');
            }
        });
    }

    // ── About ─────────────────────────────────────────────────────────

    async _renderAbout() {
        if (!this._onTab('about')) return;
        const seq = this._renderSeq;
        this._content.innerHTML = `
            <!-- This application was developed by Eldad Hertz -->
            <div class="sp-section-header">
                <h2 class="sp-section-title">${h('settings.about.title')}</h2>
                <p class="sp-section-desc">${h('settings.about.desc')}</p>
            </div>
            <div class="sp-card" id="sp-about-card">
                <div class="skeleton" style="height:16px;width:60%;margin-bottom:12px;"></div>
                <div class="skeleton" style="height:16px;width:40%;margin-bottom:8px;"></div>
                <div class="skeleton" style="height:16px;width:50%;"></div>
            </div>`;
        try {
            const res = await fetch('/api/settings/app-info');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const info = await res.json();
            if (seq !== this._renderSeq) return;
            document.getElementById('sp-about-card').innerHTML = `
                <div class="sp-card-title">${_esc(info.name)}</div>
                <div class="sp-about-grid">
                    ${_aboutRow(t('settings.about.version'),    info.version)}
                    ${_aboutRow(t('settings.about.llmModel'),   info.llm_model)}
                    ${_aboutRow(t('settings.about.endpoint'),   info.llm_endpoint)}
                    ${_aboutRow(t('settings.about.apiVersion'), info.api_version)}
                    ${_aboutRow(t('settings.about.llmTimeout'), t('settings.about.secondsPerCall', { seconds: info.llm_timeout }))}
                    ${_aboutRow(t('settings.about.prompts'),    t('settings.about.registered', { count: Number(info.prompt_count) || 0 }))}
                </div>
            `;
        } catch (e) {
            if (seq !== this._renderSeq) return;
            const card = document.getElementById('sp-about-card');
            if (card) card.innerHTML = `<p style="color:var(--color-muted)">${h('settings.about.loadFailed')}</p>`;
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _esc(text) {
    const d = document.createElement('div');
    d.textContent = String(text || '');
    return d.innerHTML;
}

/** Client-side mirror of the server's admin gate (auth.js populates _currentUser). */
function _isAdmin() {
    return (window._currentUser || {}).role === 'admin';
}

/** Tell the live workspace (workspaceController.js) to show/hide the chat tab bar. */
function _applyConversationTabs(value) {
    document.dispatchEvent(new CustomEvent('jeen:conversation-tabs', { detail: { visible: value === 'show' } }));
}

function _prettyJson(value) {
    if (typeof value === 'string') {
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch {
            return value;
        }
    }
    try {
        return JSON.stringify(value ?? {}, null, 2);
    } catch {
        return String(value ?? '');
    }
}

function _mcpHasGuidedFields(schema) {
    return !!(schema?.properties && Object.keys(schema.properties).length);
}

function _mcpInputModeButton(mode, label, selectedMode) {
    return `<button type="button" class="mc-input-mode-tab${mode === selectedMode ? ' is-active' : ''}"
        data-mc-tool-input-mode="${mode}" role="tab" aria-selected="${mode === selectedMode}">${label}</button>`;
}

function _mcpGuidedArgumentsForm(schema, args) {
    const properties = schema?.properties || {};
    const required = Array.isArray(schema?.required) ? schema.required : [];
    return `<div class="mc-guided-args">
        ${Object.entries(properties).map(([key, fieldSchema]) =>
            _mcpGuidedArgumentField(key, fieldSchema || {}, args?.[key], required.includes(key))
        ).join('')}
    </div>`;
}

function _mcpGuidedArgumentField(key, schema, value, required) {
    const type = _mcpSchemaType(schema);
    const label = `${_esc(schema.title || key)}${required ? ' <span class="mc-req">*</span>' : ''}`;
    const description = schema.description ? `<div class="mc-field-help">${_esc(schema.description)}</div>` : '';
    const attributes = `data-mc-tool-arg="${_esc(key)}" data-mc-tool-required="${required ? '1' : '0'}"`;
    let control;

    if (Array.isArray(schema.enum)) {
        control = `<select class="settings-select" ${attributes}>
            ${!required ? `<option value="">${h('settings.catalog.inspector.notSet')}</option>` : ''}
            ${schema.enum.map(option => `<option value="${_esc(String(option))}"${String(option) === String(value) ? ' selected' : ''}>${_esc(String(option))}</option>`).join('')}
        </select>`;
    } else if (type === 'boolean') {
        control = `<label class="mc-guided-checkbox">
            <input type="checkbox" ${attributes} ${value ? 'checked' : ''} />
            <span>${value ? h('common.on') : h('common.off')}</span>
        </label>`;
    } else if (type === 'string' || type === 'number' || type === 'integer') {
        const inputType = type === 'string' ? 'text' : 'number';
        const numericStep = type === 'integer' ? ' step="1"' : type === 'number' ? ' step="any"' : '';
        control = `<input class="mc-input" type="${inputType}" ${attributes}${numericStep}
            value="${_esc(value ?? '')}" placeholder="${_esc(schema.examples?.[0] ?? schema.default ?? '')}" />`;
    } else {
        control = `<div class="mc-guided-unsupported">${h('settings.catalog.inspector.editInJson', { type: type || 'complex' })}</div>`;
    }

    return `<div class="mc-guided-field">
        <label class="mc-field-label">${label}</label>
        ${control}
        ${description}
    </div>`;
}

function _mcpSchemaType(schema) {
    const type = schema?.type;
    return Array.isArray(type) ? type.find(item => item !== 'null') || type[0] : type;
}

function _mcpParseArguments(text) {
    try {
        const parsed = typeof text === 'string' ? JSON.parse(text || '{}') : text;
        return parsed && !Array.isArray(parsed) && typeof parsed === 'object' ? parsed : null;
    } catch {
        return null;
    }
}

function _mcpApplyGuidedArgument(args, key, schema, input) {
    const type = _mcpSchemaType(schema);
    const required = input.dataset.mcToolRequired === '1';
    if (type === 'boolean') {
        args[key] = input.checked;
        return;
    }

    const raw = input.value;
    if (!raw && !required) {
        delete args[key];
        return;
    }
    if (Array.isArray(schema.enum)) {
        args[key] = schema.enum.find(option => String(option) === raw) ?? raw;
    } else if (type === 'number' || type === 'integer') {
        const numeric = Number(raw);
        args[key] = Number.isFinite(numeric) ? numeric : raw;
    } else {
        args[key] = raw;
    }
}

function _mcpInputValidationErrors(schema, args) {
    if (!schema || !args) return [t('settings.catalog.inspector.argsMustBeObject')];
    const properties = schema.properties || {};
    const required = Array.isArray(schema.required) ? schema.required : [];
    const errors = required
        .filter(key => !Object.prototype.hasOwnProperty.call(args, key))
        .map(key => t('settings.catalog.inspector.required', { key }));

    Object.entries(args).forEach(([key, value]) => {
        const field = properties[key];
        if (!field) return;
        const type = _mcpSchemaType(field);
        if (type === 'string' && typeof value !== 'string') errors.push(t('settings.catalog.inspector.mustBeString', { key }));
        if ((type === 'number' || type === 'integer') && (typeof value !== 'number' || !Number.isFinite(value))) {
            errors.push(t('settings.catalog.inspector.mustBeNumber', { key }));
        }
        if (type === 'integer' && Number.isFinite(value) && !Number.isInteger(value)) {
            errors.push(t('settings.catalog.inspector.mustBeInteger', { key }));
        }
        if (type === 'boolean' && typeof value !== 'boolean') errors.push(t('settings.catalog.inspector.mustBeBoolean', { key }));
        if (Array.isArray(field.enum) && !field.enum.some(option => Object.is(option, value))) {
            errors.push(t('settings.catalog.inspector.mustBeAllowed', { key }));
        }
        if (typeof value === 'string' && field.minLength && value.length < field.minLength) {
            errors.push(t('settings.catalog.inspector.minLength', { key, min: field.minLength }));
        }
    });
    return errors;
}

function _mcpToolRisk(tool) {
    const annotations = tool?.annotations || {};
    if (annotations.destructiveHint === true) {
        return {level: 'confirmation_required', reason: t('settings.catalog.inspector.riskDestructive')};
    }
    if (annotations.openWorldHint === true) {
        return {level: 'confirmation_required', reason: t('settings.catalog.inspector.riskExternal')};
    }
    if (annotations.readOnlyHint === true) {
        return {level: 'read_only', reason: t('settings.catalog.inspector.riskReadOnly')};
    }
    return {level: 'confirmation_required', reason: t('settings.catalog.inspector.riskUnknown')};
}

function _mcpToolAnnotationBadges(tool) {
    const annotations = tool?.annotations || {};
    const badges = [
        annotations.readOnlyHint === true ? [t('settings.catalog.inspector.badgeReadOnly'), 'mc-tool-safety-ok'] : null,
        annotations.destructiveHint === true ? [t('settings.catalog.inspector.badgeDestructive'), 'mc-tool-safety-warn'] : null,
        annotations.openWorldHint === true ? [t('settings.catalog.inspector.badgeExternal'), 'mc-tool-safety-warn'] : null,
        annotations.idempotentHint === true ? [t('settings.catalog.inspector.badgeIdempotent'), ''] : null,
    ].filter(Boolean);
    if (!badges.length) badges.push([t('settings.catalog.inspector.badgeConfirm'), 'mc-tool-safety-warn']);
    return `<div class="mc-tool-badges">${badges.map(([label, cls]) =>
        `<span class="mc-tool-safety ${cls}">${_esc(label)}</span>`
    ).join('')}</div>`;
}

function _mcpErrorMessage(error) {
    if (typeof error === 'string') return error;
    if (error?.message) return error.message;
    return _prettyJson(error);
}

function _mcpToolErrorForAssist(toolResult) {
    if (toolResult?.result) return toolResult.result;
    return toolResult?.diagnostic || toolResult?.error || toolResult;
}

function _mcpToolResultViewer(toolResult, selectedView = 'content') {
    const envelope = _mcpToolResultEnvelope(toolResult);
    const contentBlocks = Array.isArray(envelope?.content) ? envelope.content : [];
    const hasStructured = Object.prototype.hasOwnProperty.call(envelope || {}, 'structuredContent');
    const primary = _mcpPrimaryResult(envelope, toolResult);
    const views = ['content', ...(hasStructured ? ['structured'] : []), 'tree', 'formatted', 'raw'];
    const view = views.includes(selectedView) ? selectedView : 'content';
    const isError = toolResult?.ok === false || envelope?.isError === true;
    const heading = isError ? t('settings.catalog.inspector.toolError') : t('settings.catalog.inspector.result');
    const content = view === 'content'
        ? _mcpContentBlocksHtml(contentBlocks, toolResult.error)
        : view === 'structured'
            ? `<div class="mc-json-tree">${_mcpJsonTreeNode(envelope.structuredContent, t('settings.catalog.inspector.structured'), 0)}</div>`
            : view === 'tree'
                ? `<div class="mc-json-tree">${_mcpJsonTreeNode(primary, t('settings.catalog.inspector.result'), 0)}</div>`
                : `<pre class="mc-result-code">${_esc(
                    view === 'raw'
                        ? _prettyJson(_mcpRedactedToolResult(toolResult))
                        : _prettyJson(primary)
                )}</pre>`;
    const runDetails = _mcpToolRunDetails(toolResult);
    const validation = _mcpOutputValidationHtml(toolResult.output_validation);
        const aiAssist = _mcpAiAssistHtml(toolResult);

    return `<div class="mc-tool-result ${isError ? 'mc-tool-result-error' : ''}">
        <div class="mc-tool-result-head">
            <span>${_esc(heading)}</span>
            <div class="mc-result-actions">
                <div class="mc-result-tabs" role="tablist" aria-label="${_esc(heading)}">
                    ${_mcpResultViewButton('content', h('settings.catalog.inspector.viewContent'), view)}
                    ${hasStructured ? _mcpResultViewButton('structured', h('settings.catalog.inspector.structured'), view) : ''}
                    ${_mcpResultViewButton('tree', h('settings.catalog.inspector.viewTree'), view)}
                    ${_mcpResultViewButton('formatted', h('settings.catalog.inspector.viewFormatted'), view)}
                    ${_mcpResultViewButton('raw', h('settings.catalog.inspector.viewRaw'), view)}
                </div>
                <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tool-result-copy-btn">${h('settings.catalog.inspector.copyView')}</button>
                ${isError ? `<button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tool-ai-assist-btn" ${toolResult.ai_assist_loading ? 'disabled' : ''}>${toolResult.ai_assist_loading ? h('settings.catalog.inspector.aiAnalyzing') : h('settings.catalog.inspector.aiHelp')}</button>` : ''}
            </div>
        </div>
        ${validation}
        ${content}
        ${aiAssist}
        ${runDetails ? `<details class="mc-result-details">
            <summary>${h('settings.catalog.inspector.runDetails')}</summary>
            <pre class="mc-result-code">${_esc(_prettyJson(runDetails))}</pre>
        </details>` : ''}
        ${toolResult.diagnostic ? `<details class="mc-result-details">
            <summary>${h('settings.catalog.inspector.requestErrorDetails')}</summary>
            <pre class="mc-result-code">${_esc(_prettyJson(toolResult.diagnostic))}</pre>
        </details>` : ''}
    </div>`;
}

function _mcpAiAssistHtml(toolResult) {
    if (toolResult.ai_assist_loading) {
        return `<div class="mc-ai-assist mc-ai-assist-loading">${h('settings.catalog.inspector.aiAnalyzingNote')}</div>`;
    }
    if (toolResult.ai_assist_error) {
        return `<div class="mc-ai-assist mc-ai-assist-error">${_esc(toolResult.ai_assist_error)}</div>`;
    }
    const assist = toolResult.ai_assist;
    if (!assist) return '';
    const suggestedArguments = assist.suggested_arguments;
    const validationErrors = assist.suggestion_validation?.errors || [];
    return `<details class="mc-ai-assist" open>
        <summary>${h('settings.catalog.inspector.aiAssistance')}</summary>
        <div class="mc-ai-assist-body">
            <div><b>${h('settings.catalog.inspector.aiSummary')}</b> ${_esc(assist.summary || t('settings.catalog.inspector.aiNoSummary'))}</div>
            ${assist.likely_cause ? `<div><b>${h('settings.catalog.inspector.aiLikelyCause')}</b> ${_esc(assist.likely_cause)}</div>` : ''}
            ${assist.next_steps?.length ? `<div><b>${h('settings.catalog.inspector.aiNextSteps')}</b><ul>${assist.next_steps.map(step => `<li>${_esc(step)}</li>`).join('')}</ul></div>` : ''}
            ${suggestedArguments ? `<div>
                <b>${h('settings.catalog.inspector.aiSuggestedArgs')}</b>
                <pre class="mc-result-code">${_esc(_prettyJson(suggestedArguments))}</pre>
                <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-tool-ai-apply-args-btn">${h('settings.catalog.inspector.useSuggestion')}</button>
            </div>` : ''}
            ${validationErrors.length ? `<div class="mc-input-errors">${validationErrors.map(error => `<div>${_esc(error)}</div>`).join('')}</div>` : ''}
        </div>
    </details>`;
}

function _mcpToolResultEnvelope(toolResult) {
    return toolResult?.result && typeof toolResult.result === 'object'
        ? toolResult.result
        : null;
}

function _mcpPrimaryResult(envelope, toolResult) {
    if (Object.prototype.hasOwnProperty.call(envelope || {}, 'structuredContent')) return envelope.structuredContent;
    const firstText = (envelope?.content || []).find(block => block?.type === 'text');
    if (firstText) return _parseJsonResult(firstText.text || '');
    return envelope || toolResult.error || toolResult;
}

function _mcpContentBlocksHtml(blocks, error) {
    if (!blocks.length) {
        return `<div class="mc-result-empty">${_esc(error || t('settings.catalog.inspector.noContentBlocks'))}</div>`;
    }
    return `<div class="mc-content-blocks">${blocks.map((block, index) =>
        _mcpContentBlockHtml(block, index)
    ).join('')}</div>`;
}

function _mcpContentBlockHtml(block, index) {
    const type = block?.type || 'unknown';
    let body;
    if (type === 'text') {
        body = `<pre class="mc-result-code">${_esc(_prettyJson(block.text || ''))}</pre>`;
    } else if (type === 'resource_link' || type === 'resourceLink') {
        body = `<pre class="mc-result-code">${_esc(_prettyJson({
            uri: block.uri, name: block.name, description: block.description, mimeType: block.mimeType,
        }))}</pre>`;
    } else if (type === 'image' || type === 'audio') {
        body = `<pre class="mc-result-code">${_esc(_prettyJson({
            type, mimeType: block.mimeType, size: typeof block.data === 'string' ? `${block.data.length} encoded characters` : undefined,
        }))}</pre>`;
    } else {
        body = `<pre class="mc-result-code">${_esc(_prettyJson(block))}</pre>`;
    }
    return `<details class="mc-content-block" ${index === 0 ? 'open' : ''}>
        <summary>${h('settings.catalog.inspector.block', { type })} <span>${index + 1}</span></summary>
        ${body}
    </details>`;
}

function _mcpResultViewButton(view, label, selectedView) {
    return `<button type="button" class="mc-result-tab${view === selectedView ? ' is-active' : ''}"
        data-mc-tool-result-view="${view}" role="tab" aria-selected="${view === selectedView}">${label}</button>`;
}

function _mcpToolRunDetails(toolResult) {
    const details = {};
    ['server_id', 'tool_name', 'risk', 'duration_ms', 'completed_at'].forEach(key => {
        if (Object.prototype.hasOwnProperty.call(toolResult || {}, key)) details[key] = toolResult[key];
    });
    if (Object.prototype.hasOwnProperty.call(toolResult || {}, 'arguments')) {
        details.arguments = _mcpRedactSecrets(toolResult.arguments);
    }
    return Object.keys(details).length ? details : null;
}

function _mcpOutputValidationHtml(diagnostic) {
    if (!diagnostic?.available) return '';
    if (diagnostic.valid) {
        return `<div class="mc-output-validation mc-output-valid">${h('settings.catalog.inspector.outputValid')}</div>`;
    }
    return `<details class="mc-output-validation mc-output-invalid">
        <summary>${h('settings.catalog.inspector.outputInvalid')}</summary>
        <pre class="mc-result-code">${_esc(_prettyJson(diagnostic.errors || []))}</pre>
    </details>`;
}

function _mcpToolResultCopyText(toolResult, selectedView) {
    const envelope = _mcpToolResultEnvelope(toolResult);
    if (selectedView === 'raw') return _prettyJson(_mcpRedactedToolResult(toolResult));
    if (selectedView === 'structured') return _prettyJson(envelope?.structuredContent);
    if (selectedView === 'content') return _prettyJson(envelope?.content || toolResult.error || []);
    return _prettyJson(_mcpPrimaryResult(envelope, toolResult));
}

function _mcpRedactedToolResult(toolResult) {
    return {
        ...toolResult,
        ...(Object.prototype.hasOwnProperty.call(toolResult || {}, 'arguments')
            ? {arguments: _mcpRedactSecrets(toolResult.arguments)}
            : {}),
    };
}

function _mcpRedactSecrets(value, key = '') {
    const secretKey = /token|secret|password|authorization|api[_-]?key|credential/i.test(key);
    if (secretKey && value !== undefined) return '[redacted]';
    if (Array.isArray(value)) return value.map(item => _mcpRedactSecrets(item));
    if (value && typeof value === 'object') {
        return Object.fromEntries(Object.entries(value).map(([childKey, childValue]) => [
            childKey, _mcpRedactSecrets(childValue, childKey),
        ]));
    }
    return value;
}

function _parseJsonResult(value) {
    if (typeof value !== 'string') return value;
    try {
        return JSON.parse(value);
    } catch {
        return value;
    }
}

function _rawJson(value) {
    if (typeof value === 'string') return value;
    try {
        return JSON.stringify(value ?? {});
    } catch {
        return String(value ?? '');
    }
}

function _mcpJsonTreeNode(value, key, depth) {
    const keyHtml = `<span class="mc-json-key">${_esc(key)}</span>`;
    if (value !== null && typeof value === 'object') {
        const entries = Array.isArray(value)
            ? value.map((item, index) => [String(index), item])
            : Object.entries(value);
        const kind = Array.isArray(value) ? 'Array' : 'Object';
        const children = entries.length
            ? entries.map(([childKey, childValue]) => _mcpJsonTreeNode(childValue, childKey, depth + 1)).join('')
            : `<div class="mc-json-tree-empty">${kind === 'Array' ? h('settings.catalog.inspector.emptyArray') : h('settings.catalog.inspector.emptyObject')}</div>`;
        return `<details class="mc-json-tree-node" ${depth === 0 ? 'open' : ''}>
            <summary>${keyHtml}<span class="mc-json-tree-type">${kind} · ${entries.length}</span></summary>
            <div class="mc-json-tree-children">${children}</div>
        </details>`;
    }
    return `<div class="mc-json-tree-value">
        ${keyHtml}<span class="mc-json-value mc-json-${value === null ? 'null' : typeof value}">${_esc(_jsonPrimitive(value))}</span>
    </div>`;
}

function _jsonPrimitive(value) {
    return typeof value === 'string' ? JSON.stringify(value) : String(value);
}

function _sampleArgsFromSchema(schema) {
    const s = schema && typeof schema === 'object' ? schema : {};
    const props = s.properties && typeof s.properties === 'object' ? s.properties : {};
    if (!Object.keys(props).length) return {};

    const required = Array.isArray(s.required) ? s.required : [];
    const ordered = [
        ...required,
        ...Object.keys(props).filter(k => !required.includes(k)),
    ];
    const sample = {};
    ordered.forEach(key => {
        const field = props[key] || {};
        const hasSuggestedValue = Object.prototype.hasOwnProperty.call(field, 'default')
            || Object.prototype.hasOwnProperty.call(field, 'const')
            || (Array.isArray(field.enum) && field.enum.length)
            || (Array.isArray(field.examples) && field.examples.length);
        if (required.includes(key) || hasSuggestedValue) {
            sample[key] = _sampleValueFromSchema(field);
        }
    });
    return sample;
}

function _sampleValueFromSchema(schema) {
    const s = schema && typeof schema === 'object' ? schema : {};
    if (Object.prototype.hasOwnProperty.call(s, 'default')) return s.default;
    if (Object.prototype.hasOwnProperty.call(s, 'const')) return s.const;
    if (Array.isArray(s.enum) && s.enum.length) return s.enum[0];
    if (Array.isArray(s.examples) && s.examples.length) return s.examples[0];

    const type = Array.isArray(s.type) ? s.type[0] : s.type;
    if (type === 'integer' || type === 'number') return 0;
    if (type === 'boolean') return false;
    if (type === 'array') return [];
    if (type === 'object' || s.properties) return _sampleArgsFromSchema(s);
    if (type === 'null') return null;
    return '';
}

const _PLACEHOLDER_RE  = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
const _ESCAPED_RE      = /\{\{[^}]*\}\}/g;

function _extractPlaceholders(text) {
    const cleaned = text.replace(_ESCAPED_RE, '');
    const names = new Set();
    let m;
    const re = new RegExp(_PLACEHOLDER_RE.source, 'g');
    while ((m = re.exec(cleaned)) !== null) names.add(m[1]);
    return [...names].sort();
}

/**
 * Render prompt text as HTML with {placeholder} tokens highlighted.
 * Double-brace {{ }} literals are left as-is (not highlighted).
 */
function _renderPromptView(text) {
    // Escape HTML first, then highlight single-brace placeholders.
    const escaped = _esc(text)
        .replace(/\n/g, '<br>')
        .replace(/  /g, '&nbsp;&nbsp;');

    // Replace {word} (but not {{word}}) with highlighted chips.
    // After HTML-escaping, {{ became {{ and }} became }} so we can still detect them.
    return escaped.replace(
        /(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})/g,
        (_, name) => `<code class="sp-ph-inline">{${_esc(name)}}</code>`,
    );
}

function _renderResolvedPromptView(text) {
    const escaped = _esc(text)
        .replace(/\n/g, '<br>')
        .replace(/  /g, '&nbsp;&nbsp;');
    return escaped
        .replace(
            /\{runtime: ([^}]+)\}/g,
            (_, name) => `<code class="sp-runtime-inline">{runtime: ${_esc(name)}}</code>`,
        );
}

function _formatCount(value) {
    const n = Number(value || 0);
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K`;
    return String(n);
}

function _aboutRow(label, value) {
    return `<div class="sp-about-row">
        <span class="sp-about-label">${_esc(label)}</span>
        <span class="sp-about-value"><bdi>${_esc(String(value || '—'))}</bdi></span>
    </div>`;
}

function _initials(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    if (parts.length === 1) return parts[0][0].toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function _showToast(msg, type) {
    if (typeof window.showToast === 'function') {
        window.showToast(msg, type);
    }
}

function _humanTime(seconds) {
    if (seconds < 60)  return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds/60)}m`;
    return `${Math.round(seconds/3600)}h`;
}

function _formatTtl(seconds) {
    if (seconds === 0)     return t('settings.catalog.noCache');
    if (seconds < 3600)   return t('settings.catalog.minutes', { count: Math.round(seconds/60) });
    if (seconds < 86400)  return t('settings.catalog.hours', { count: Math.round(seconds/3600) });
    return t('settings.catalog.hours', { count: 24 });
}

/** "3 minutes ago" in the interface language (Intl.RelativeTimeFormat via I18n). */
function _relativeTime(isoStr) {
    if (window.I18n && typeof window.I18n.formatRelative === 'function') return window.I18n.formatRelative(isoStr);
    if (!isoStr) return t('common.never');
    try {
        const diff = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
        if (diff < 60)   return t('common.justNow');
        if (diff < 3600) return `${Math.floor(diff/60)}m`;
        if (diff < 86400) return `${Math.floor(diff/3600)}h`;
        return `${Math.floor(diff/86400)}d`;
    } catch { return ''; }
}
