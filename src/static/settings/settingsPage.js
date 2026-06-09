/**
 * Settings Page
 *
 * Full-screen two-column layout:
 *   Left  — grouped navigation (like the reference design)
 *   Right — content area (General params · Prompt editor · About)
 *
 * Navigation groups
 * -----------------
 *   USER        General (runtime preferences)
 *   AI AGENT    9 LangGraph prompts
 *   OTHER       At the bottom — About · Logout · Close
 *
 * @module settingsPage
 */

import { Preferences } from './preferences.js';
import { CHART_TYPE_OPTIONS } from '../chart-feature/chartTypes.js';

// ── Icons (inline SVG snippets, 18×18) ────────────────────────────────────────
const ICONS = {
    models:   `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="oklch(52% 0.22 280)"/><text x="50%" y="55%" font-family="system-ui,-apple-system,sans-serif" font-weight="800" font-size="18" fill="#FFFFFF" dominant-baseline="middle" text-anchor="middle">J</text></svg>`,
    general:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
    prompt:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    about:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    close:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
    logout:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
    catalog:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg>`,
};

// ── Navigation definition ─────────────────────────────────────────────────────
const ICONS_USERS = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`;

const NAV = [
        {
        group: 'USER',
        items: [
            { id: 'general',           label: 'General',              icon: ICONS.general,  type: 'general' },
            { id: 'metadata-catalog',  label: 'Metadata & Catalog',   icon: ICONS.catalog,  type: 'metadata-catalog' },
            { id: 'ai-models',         label: 'AI Models',            icon: ICONS.models,   type: 'ai-models' },
            { id: 'users',             label: 'Users',                icon: ICONS_USERS,    type: 'users' },
        ],
    },
    {
        group: 'AI AGENT',
        items: [
            { id: 'prompt:jeen_insights_system', label: 'System Prompt', icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:fused_router',         label: 'Router',         icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:fused_eval_analytics', label: 'Eval & Analytics', icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:memory_answer',        label: 'Memory Answer',  icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:memory_summarizer',    label: 'Memory Summary', icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:sql_generator',        label: 'SQL Retry',      icon: ICONS.prompt, type: 'prompt' },
        ],
    },
    {
        group: 'OTHER FEATURES',
        items: [
            { id: 'prompt:chart_editor',             label: 'Chart Editor',  icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:insights',                 label: 'Insights',      icon: ICONS.prompt, type: 'prompt' },
            { id: 'prompt:autocomplete_suggestions', label: 'Autocomplete',  icon: ICONS.prompt, type: 'prompt' },
        ],
    },
];

// Bottom items (rendered separately, pinned to bottom of sidebar)
const BOTTOM_NAV = [
    { id: 'about', label: 'About', icon: ICONS.about, type: 'about' },
];

// ── SettingsPage class ────────────────────────────────────────────────────────

export class SettingsPage {
    constructor() {
        this._open       = false;
        this._root       = null;    // overlay wrapper
        this._content    = null;    // right content panel
        this._activeId   = 'general';
        this._prompts    = {};      // name → {meta, content, dirty, editing}
        this._models     = null;    // cached model list (Array)
        this._onApplyTheme = null;
        // Metadata & Catalog state
        this._mcpStatus   = null;   // last /api/mcp/status response
        this._mcpConn     = null;   // currently viewed connection
        this._mcpEditing  = null;   // server id being edited, or 'new'
        this._mcpDraft    = null;   // form draft object
        this._mcpTesting  = false;  // health check in progress
        this._mcpReloading= false;  // background status reload in progress
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
        this._loadPrompts();
        this._activate(this._activeId);
    }

    close() {
        if (!this._open) return;
        this._root.hidden = true;
        this._open = false;
        document.body.style.overflow = '';
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
            <button class="sp-header-back-btn" aria-label="Close settings" title="Back">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M15 18l-6-6 6-6"/>
                </svg>
            </button>
            <span class="sp-sidebar-title">SETTINGS</span>
        `;
        sidebarHeader.querySelector('.sp-header-back-btn').addEventListener('click', () => this.close());
        sidebar.appendChild(sidebarHeader);

        const navBody = document.createElement('div');
        navBody.className = 'sp-nav-body';

        // Main groups
        NAV.forEach(group => {
            const groupLabel = document.createElement('div');
            groupLabel.className = 'sp-nav-group';
            groupLabel.textContent = group.group;
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
        bottomGroup.textContent = 'OTHER';
        navBottom.appendChild(bottomGroup);

        BOTTOM_NAV.forEach(item => {
            navBottom.appendChild(this._buildNavItem(item));
        });

        const logoutBtn = document.createElement('a');
        logoutBtn.href = '/logout';
        logoutBtn.className = 'sp-nav-item sp-logout-btn';
        logoutBtn.innerHTML = `${ICONS.logout}<span class="sp-nav-label">Logout</span>`;
        logoutBtn.addEventListener('click', () => this.close());
        navBottom.appendChild(logoutBtn);

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'sp-nav-item sp-close-btn';
        closeBtn.innerHTML = `${ICONS.close}<span class="sp-nav-label">Close</span>`;
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
        el.className = 'sp-nav-item';
        el.dataset.id = item.id;
        el.innerHTML = `${item.icon}<span class="sp-nav-label">${_esc(item.label)}</span><span class="sp-nav-dot" hidden></span>`;
        el.addEventListener('click', () => this._activate(item.id));
        return el;
    }

    // ── Navigation ────────────────────────────────────────────────────────────

    _activate(id) {
        this._activeId = id;

        // Update active state in sidebar
        this._root.querySelectorAll('.sp-nav-item').forEach(el => {
            el.classList.toggle('is-active', el.dataset.id === id);
        });

        // Render content
        if (id === 'general') {
            this._renderGeneral();
        } else if (id === 'metadata-catalog') {
            this._renderMetadataCatalog();
        } else if (id === 'ai-models') {
            this._renderModels();
        } else if (id === 'users') {
            this._renderUsers();
        } else if (id === 'about') {
            this._renderAbout();
        } else if (id.startsWith('prompt:')) {
            const name = id.slice(7);
            this._renderPrompt(name);
        }
    }

    // ── Load prompts from API ─────────────────────────────────────────────────

    async _loadPrompts() {
        try {
            const res = await fetch('/api/settings/prompts');
            if (!res.ok) return;
            const list = await res.json();
            list.forEach(p => {
                if (!this._prompts[p.name]) {
                    this._prompts[p.name] = { meta: p, content: null, dirty: false, editing: false };
                } else {
                    this._prompts[p.name].meta = p;
                }
                this._updateDot(p.name, p.is_custom);
            });
        } catch (e) {
            console.warn('[SettingsPage] could not load prompts:', e);
        }
    }

    async _fetchPromptContent(name) {
        if (this._prompts[name]?.content !== null) return;
        try {
            const res = await fetch(`/api/settings/prompts/${name}`);
            if (!res.ok) return;
            const data = await res.json();
            if (!this._prompts[name]) this._prompts[name] = {};
            this._prompts[name].content = data.content;
            this._prompts[name].meta = data;
        } catch (e) {
            console.warn('[SettingsPage] could not fetch prompt:', name, e);
        }
    }

    _updateDot(name, isCustom) {
        const btn = this._root.querySelector(`[data-id="prompt:${name}"]`);
        if (!btn) return;
        const dot = btn.querySelector('.sp-nav-dot');
        if (dot) dot.hidden = !isCustom;
    }

    // ── AI Models ─────────────────────────────────────────────────────────────

    async _renderModels() {
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">AI Models</h2>
                <p class="sp-section-desc">Select the language model used for all SQL generation and analytics. The selection is applied live and persisted across restarts.</p>
            </div>
            <div class="sp-card" id="sp-models-list">
                <div class="sp-model-loading">
                    <div class="skeleton" style="height:72px;border-radius:8px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:72px;border-radius:8px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:72px;border-radius:8px;"></div>
                </div>
            </div>`;

        try {
            const res = await fetch('/api/settings/models');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            this._models = await res.json();
        } catch (e) {
            document.getElementById('sp-models-list').innerHTML =
                `<p style="color:var(--color-muted)">Could not load models: ${_esc(e.message)}</p>`;
            return;
        }

        this._renderModelCards();
    }

    _renderModelCards() {
        const container = document.getElementById('sp-models-list');
        if (!container || !this._models) return;

        const available = this._models.filter(m => m.available);
        const unavailable = this._models.filter(m => !m.available);

        container.innerHTML = [
            available.length ? `<div class="sp-model-group-label">Available with current credentials</div>` : '',
            ...available.map(m => this._modelCard(m)),
            unavailable.length ? `<div class="sp-model-group-label sp-model-group-label--dim" style="margin-top:16px">Not configured</div>` : '',
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
            ? `<span class="sp-badge sp-badge-default" style="margin-left:6px;">default</span>` : '';
        const activeMark = m.is_active
            ? `<span class="sp-model-check">
                 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
               </span>` : '';
        const unavailBadge = !m.available
            ? `<span class="sp-model-unavail">Not configured</span>` : '';

        return `
        <div class="sp-model-card ${m.is_active ? 'is-active' : ''} ${!m.available ? 'is-unavailable' : ''}"
             data-model="${_esc(m.name)}">
            <div class="sp-model-info">
                <div class="sp-model-name">
                    ${_esc(m.display_name)}${defaultBadge}${unavailBadge}
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
            _showToast(`Model switched to ${name}`, 'success');
        } catch (e) {
            console.error('[SettingsPage] model select failed:', e);
            _showToast('Could not switch model — ' + e.message, 'error');
            // Reload to restore true state
            await this._renderModels();
        }
    }

    // ── Metadata & Catalog ───────────────────────────────────────────────────

    async _renderMetadataCatalog() {
        // Use the active connection from the main app (localStorage) first,
        // then fall back to whatever was last viewed, then first available.
        if (!this._mcpConn) {
            this._mcpConn =
                (typeof getActiveConnection === 'function' && getActiveConnection()) || null;
        }

        // Show skeleton while loading
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">Metadata &amp; Catalog</h2>
                <p class="sp-section-desc">Where the text-to-SQL agent loads every connection's curated catalog — tables, columns, relationships and business terms — for prompt context. A single, application-wide source.${
                    this._mcpConn ? ` Showing stats for <strong>${_esc(this._mcpConn)}</strong>.` : ''}</p>
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

    _mcpRender() {
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
                <h2 class="sp-section-title">Metadata &amp; Catalog</h2>
                <p class="sp-section-desc">Where the text-to-SQL agent loads every connection's curated catalog — tables, columns, relationships and business terms — for prompt context. A single, application-wide source.${
                    this._mcpConn ? ` Showing stats for <strong>${_esc(this._mcpConn)}</strong>.` : ''}</p>
            </div>

            <div class="sp-card" id="mc-src-card">
                <div class="sp-row sp-row--seg" style="padding-bottom:0">
                    <div>
                        <div class="sp-row-label">Catalog source</div>
                        <div class="sp-row-help">Applies to the whole application. Switching takes effect on the next query.</div>
                    </div>
                    <div class="mc-seg" id="mc-seg">
                        <button data-src="db" class="${src==='db'?'is-active':''}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg> Metadata DB</button>
                        <button data-src="mcp" class="${src==='mcp'?'is-active':''}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/></svg> MCP Server</button>
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
            ? `<span class="mc-status mc-status-ok"><span class="mc-dot"></span> Synced · cache HIT${expiry ? ` · ${_humanTime(expiry)} remaining` : ''}</span>`
            : `<span class="mc-status mc-status-muted"><span class="mc-dot"></span> Cache MISS — will fetch on next query</span>`;

        return `
            <div class="mc-info-grid">
                <div><span class="mc-info-k">Provider</span><span class="mc-info-v">Schema Modeler</span></div>
                <div><span class="mc-info-k">Metadata database</span><span class="mc-info-v mc-mono">${_esc(db.database || '—')}</span></div>
                <div><span class="mc-info-k">Tables · columns</span><span class="mc-info-v">${db.tables ?? '—'} · ${db.columns ?? '—'}</span></div>
                <div><span class="mc-info-k">Business terms · pairs</span><span class="mc-info-v">${db.business_terms ?? '—'} · ${db.knowledge_pairs ?? '—'}</span></div>
            </div>
            <div class="mc-info-foot">
                ${statusLabel}
                <span style="flex:1"></span>
                <button class="sp-btn-ghost" id="mc-refresh-btn">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><polyline points="21 3 21 8 16 8"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><polyline points="3 21 3 16 8 16"/></svg>
                    Refresh metadata
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
                <span class="mc-srv-dot ${dotCls}" title="${_esc(hStatus||'not checked')}"></span>
                <div class="mc-srv-main">
                    <div class="mc-srv-name"><span class="mc-srv-name-txt">${_esc(s.server_name)}</span>${isAct?'<span class="mc-srv-tag">active</span>':''}</div>
                    <div class="mc-srv-ep">${_esc(s.endpoint)}</div>
                </div>
                <span class="mc-srv-badge">${_esc((s.transport||'').toUpperCase())}</span>
                <div class="mc-srv-actions">
                    <button class="mc-srv-ico" data-edit="${s.id}" title="Edit"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg></button>
                    <button class="mc-srv-ico mc-srv-del" data-del="${s.id}" title="Delete" ${servers.length<=1?'disabled':''}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
                </div>
            </div>`;
        }).join('') : '<div class="mc-empty">No MCP servers yet. Add one to source this catalog.</div>';

        const formHtml = this._mcpEditing ? this._mcpFormHtml() : '';
        const healthHtml = (!this._mcpEditing && activeServ) ? this._mcpHealthSection(activeServ) : '';

        return `
            <div class="mc-srv-listhead">
                <span class="mc-srv-listtitle">MCP servers <span class="mc-srv-count">${servers.length}</span></span>
                <button class="sp-btn-ghost sp-btn-ghost-sm" id="mc-add-btn">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Add server
                </button>
            </div>
            <div class="mc-srv-list">${listRows}</div>
            ${formHtml}
            ${healthHtml}
            <div class="mc-callout">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                <p>MCP delivers live, federated metadata — but bypasses Schema Modeler's curation layer. The DLP / governance scan and SELECT-only execution still apply. Results are cached per the TTL below.</p>
            </div>`;
    }

    _mcpFormHtml() {
        const d      = this._mcpDraft || {};
        const isNew  = this._mcpEditing === 'new';
        const bearer = d.auth_type === 'bearer';
        const saveOk = (d.server_name||'').trim() && (d.endpoint||'').trim();
        return `
        <div class="mc-form">
            <div class="mc-form-head">${isNew ? 'Add MCP server' : 'Edit MCP server'}</div>
            <div class="mc-field">
                <label class="mc-field-label">Server name <span class="mc-req">*</span></label>
                <input id="mc-f-name" class="mc-input" value="${_esc(d.server_name||'')}" placeholder="jeen-catalog-mcp" />
            </div>
            <div class="mc-field">
                <label class="mc-field-label">Endpoint <span class="mc-req">*</span></label>
                <input id="mc-f-ep" class="mc-input" value="${_esc(d.endpoint||'')}" placeholder="https://mcp.jeen.internal/catalog" />
                <div class="mc-field-help">For stdio transport, give the launch command (e.g. <code>npx @jeen/catalog-mcp</code>).</div>
            </div>
            <div class="mc-field-row">
                <div class="mc-field">
                    <label class="mc-field-label">Transport</label>
                    <div class="mc-mini-seg" id="mc-f-transport">
                        ${['stdio','sse','http'].map(t=>`<button data-t="${t}" class="${(d.transport||'http')===t?'is-active':''}">${t.toUpperCase()}</button>`).join('')}
                    </div>
                </div>
                <div class="mc-field">
                    <label class="mc-field-label">Authentication</label>
                    <select id="mc-f-auth" class="settings-select" style="width:100%;min-width:0">
                        <option value="none"${(d.auth_type||'none')==='none'?' selected':''}>None</option>
                        <option value="bearer"${d.auth_type==='bearer'?' selected':''}>Bearer token</option>
                        <option value="oauth"${d.auth_type==='oauth'?' selected':''}>OAuth 2.1</option>
                    </select>
                </div>
            </div>
            ${bearer ? `<div class="mc-field">
                <label class="mc-field-label">Bearer token</label>
                <div class="mc-input-wrap">
                    <input id="mc-f-token" class="mc-input" type="password" value="" placeholder="${d.has_token ? '•••••••• (saved — leave blank to keep)' : '••••••••••••••••'}" data-has-token="${d.has_token ? '1' : '0'}" data-srv-id="${d.id || ''}" autocomplete="off" />
                    <button type="button" class="mc-pw-toggle" id="mc-f-token-eye" aria-label="Show or hide token" title="Show / hide token" tabindex="-1">
                        <svg id="mc-eye-show" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        <svg id="mc-eye-hide" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                    </button>
                </div>
                <div class="mc-field-help">${d.has_token ? 'A token is saved. Type to replace it, or click the eye to reveal the saved value.' : 'Stored server-side; never sent to the LLM.'}</div>
            </div>` : ''}
            <div class="mc-form-foot">
                <button class="sp-btn-ghost" id="mc-f-cancel">Cancel</button>
                <button class="sp-btn-primary-sm" id="mc-f-save" ${saveOk?'':'disabled'}>${isNew ? 'Add server' : 'Save changes'}</button>
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
            : h ? 'Re-run health check' : 'Test &amp; health check';

        // ─ Status labels: Healthy / Degraded / Unreachable (never raw lowercase) ──
        const _statusLabel = { healthy: 'Healthy', degraded: 'Degraded', down: 'Unreachable' };
        const statusText = testing
            ? `<span class="mc-status mc-status-checking"><span class="mc-dot mc-dot-pulse"></span> Handshaking with ${_esc(server.server_name)}…</span>`
            : h?.status === 'healthy'
                ? `<span class="mc-status mc-status-ok"><span class="mc-dot"></span> Healthy · ${h.latency_ms||0}ms · checked ${_relativeTime(h.checked_at)}</span>`
            : h?.status === 'degraded'
                ? `<span class="mc-status mc-status-warn"><span class="mc-dot"></span> Degraded · ${h.latency_ms||0}ms · checked ${_relativeTime(h.checked_at)}</span>`
            : h?.status === 'down'
                ? `<span class="mc-status mc-status-err"><span class="mc-dot"></span> Unreachable · checked ${_relativeTime(h.checked_at)}</span>`
            : `<span class="mc-status mc-status-muted"><span class="mc-dot"></span> ${_esc(server.server_name)} not checked</span>`;

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
                    <span class="mc-health-badge mc-health-checking"><span class="mc-dot mc-dot-pulse"></span> Checking…</span>
                    <span class="mc-health-impl">${_esc(server.server_name)}</span>
                </div>
                <p class="mc-health-pending-msg">Running handshake and tool discovery. Previous results are hidden until this check completes.</p>
            </div>`;
        } else if (h && h.status !== 'down') {
            const cells = [
                ['Protocol',  h.protocol || '—'],
                ['SDK',       h.sdk || '—'],
                ['Handshake', (h.latency_ms||0) + 'ms'],
                ['Ping',      (h.ping_ms||0) + 'ms'],
                ['Uptime',    h.uptime || '—'],
                ['Transport', `${(server.transport||'').toUpperCase()}${h.tls ? ' · ' + h.tls : ''}`],
            ];
            const tools   = h.tools || [];
            const resCnt  = typeof h.resources === 'number' ? h.resources : null;
            const prmCnt  = typeof h.prompts   === 'number' ? h.prompts   : null;
            const capsMeta = (resCnt !== null || prmCnt !== null)
                ? `<span class="mc-cap-meta">${[resCnt !== null ? `${resCnt} resources` : '', prmCnt !== null ? `${prmCnt} prompts` : ''].filter(Boolean).join(' · ')}</span>`
                : '';

            // Capabilities: always show all four — on (✓) or off (×)
            const CHECK = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
            const CROSS = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
            const caps = ['tools','resources','prompts','logging'].map(c => {
                const on = (h.capabilities||[]).includes(c);
                return `<span class="mc-cap-chip ${on ? '' : 'mc-cap-off'}">${on ? CHECK : CROSS} ${_esc(c)}</span>`;
            }).join('');

            // Tool rows with friendly need labels
            const toolRows = tools.map(t =>
                `<div class="mc-tool-row">
                    <svg class="mc-tool-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
                    <div class="mc-tool-main">
                        <div class="mc-tool-name">${_esc(t.name)}</div>
                        <div class="mc-tool-desc">${_esc(t.description||'')}</div>
                    </div>
                    ${t.need
                        ? `<span class="mc-tool-need">↳ ${_esc(_NEED_LABEL[t.need] || t.need)}</span>`
                        : '<span class="mc-tool-need mc-map-muted">unmapped</span>'}
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
                    <span class="mc-health-when">checked ${_relativeTime(h.checked_at)}</span>
                </div>
                ${noteHtml}
                <div class="mc-health-grid">${cells.map(([k,v]) =>
                    `<div class="mc-health-cell"><span class="mc-hk">${k}</span><span class="mc-hv">${_esc(String(v))}</span></div>`
                ).join('')}</div>
                <div class="mc-health-sub">Capabilities</div>
                <div class="mc-cap-row">${caps}${capsMeta}</div>
                <div class="mc-health-sub">Tools exposed <span class="mc-srv-count">${tools.length}</span></div>
                <div class="mc-tools">${toolRows || '<div style="padding:14px;color:var(--color-faint);font-size:.8rem">No tools discovered.</div>'}</div>
            </div>`;

        } else if (h?.status === 'down') {
            // Unreachable: proper card with header, not a bare error box
            diagnosticsHtml = `
            <div class="mc-health">
                <div class="mc-health-head">
                    <span class="mc-health-badge mc-health-down"><span class="mc-dot"></span> Unreachable</span>
                    <span class="mc-health-impl">${_esc(server.endpoint)}</span>
                    <span class="mc-health-when">checked ${_relativeTime(h.checked_at)}</span>
                </div>
                <div class="mc-err-box" style="margin:12px 14px 14px">${_esc(h.error || 'Health check failed')}</div>
            </div>`;
        }

        // ─ Catalog tool mapping ─ ALWAYS VISIBLE ──────────────────────────────────
        // Needs come from the backend (single source of truth) so the required
        // gate here always matches what activation actually enforces.
        const NEEDS = this._catalogNeeds();
        const tools = h?.tools || [];
        const mapRows = NEEDS.map(n => {
            const t = tools.find(x => x.need === n.key);
            return `<div class="mc-map-row">
                <span class="mc-map-need">${n.label}${n.req ? '<span class="mc-req"> *</span>' : ''}</span>
                ${t
                    ? `<span class="mc-map-tool">${_esc(t.name)}</span>`
                    : `<span class="mc-map-tool mc-map-muted">awaiting health check</span>`}
            </div>`;
        }).join('');

        // Required-need unmapped warning (only after a health check)
        const missingReq = h ? NEEDS.filter(n => n.req && !tools.find(t => t.need === n.key)) : [];
        const missingWarn = missingReq.length
            ? `<div class="mc-err-box" style="margin-top:8px">
                <b>${missingReq.map(n => n.label).join(', ')}</b> — required but unmapped.
                This server cannot be the active catalog source until satisfied.
               </div>`
            : '';

        return `
        <div class="mc-active-srv">
            <div class="mc-test-bar">
                <button class="sp-btn-ghost" id="mc-test-btn" ${testing ? 'disabled' : ''}>${btnLabel}</button>
                ${statusText}
            </div>
            ${diagnosticsHtml}
            <div class="mc-field" style="margin-top:14px">
                <label class="mc-field-label">Catalog tool mapping</label>
                <div class="mc-map">${mapRows}</div>
                ${missingWarn}
                <div class="mc-field-help">Tools are auto-discovered from the server's <code>tools/list</code> and mapped to each catalog need.</div>
            </div>
        </div>`;
    }

    _mcpCacheCard(S) {
        const src     = S.catalog_source || 'db';
        const ttl     = S.cache_ttl_seconds ?? 900;  // per-connection TTL from API
        const ttlOpts = [
            [0,     'No cache (live)'],
            [300,   '5 minutes'],
            [900,   '15 minutes'],
            [3600,  '1 hour'],
            [86400, '24 hours'],
        ];
        const badgeLabel = src === 'mcp' ? 'MCP' : 'DB';
        const hasActive  = !!S.active_server_id;
        const ttlDisabled = !hasActive;  // caching is MCP-only; needs an active server
        const isLive     = ttl === 0;
        const cacheHit   = src === 'db'
            ? (S.db?.cache_status?.hit ?? false)
            : (S.mcp_cache?.cache_hit ?? false);
        const pillHtml = isLive
            ? `<span class="mc-cache-pill mc-cache-live"><span class="mc-cache-dot"></span> next run · <b>live fetch</b></span>
               <span class="mc-cache-pill">TTL <b>none</b></span>
               <span class="mc-cache-pill">re-fetches every query · freshest, slower</span>`
            : `<span class="mc-cache-pill ${cacheHit ? 'mc-cache-hit' : ''}"><span class="mc-cache-dot"></span> next run · <b>${cacheHit ? 'cache HIT' : 'cache MISS'}</b></span>
               <span class="mc-cache-pill">TTL <b>${_formatTtl(ttl)}</b></span>
               <span class="mc-cache-pill">invalidate → <b>Refresh metadata</b></span>`;
        return `
        <div class="mc-cache-card">
            <div class="sp-row" style="border:none;padding-bottom:0">
                <div>
                    <div class="sp-row-label">Catalog cache TTL <span class="mc-badge">${badgeLabel}</span></div>
                    <div class="sp-row-help">How long the catalog is held before re-fetching. The pipeline's <code>catalog.load</code> step reports HIT / MISS each run.</div>
                </div>
                <select class="settings-select" id="mc-ttl-sel"${ttlDisabled ? ' disabled' : ''}>
                    ${ttlOpts.map(([v,l])=>`<option value="${v}"${ttl===v?' selected':''}>${_esc(l)}</option>`).join('')}
                </select>
            </div>
            ${ttlDisabled ? '<div class="mc-cache-state"><span class="mc-cache-pill">Caching applies to MCP sources · activate a server to configure</span></div>' : `<div class="mc-cache-state" id="mc-cache-pills">${pillHtml}</div>`}
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
                } catch (e) { _showToast('Could not switch source — ' + e.message, 'error'); }
            });
        });

        // Refresh metadata button (DB panel)
        const refreshBtn = $('#mc-refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', async () => {
                const orig = refreshBtn.innerHTML;
                refreshBtn.disabled = true;
                refreshBtn.textContent = 'Refreshing…';
                try {
                    const qs = this._mcpConn ? `?connection=${encodeURIComponent(this._mcpConn)}` : '';
                    await fetch(`/api/mcp/refresh${qs}`, {method:'POST'});
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast('Cache cleared', 'success');
                } catch (e) { _showToast('Refresh failed — ' + e.message, 'error'); }
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
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                } catch (e2) { _showToast('Could not select server — ' + e2.message, 'error'); }
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
                if (!confirm('Delete this server? This cannot be undone.')) return;
                try {
                    const r = await fetch(`/api/mcp/servers/${id}`, {method:'DELETE'});
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    this._mcpStatus = null;
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast('Server deleted', 'info');
                } catch (e2) { _showToast('Delete failed — ' + e2.message, 'error'); }
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

        // Form: bearer-token reveal (eye). Reveals what's typed, and for an
        // existing server with a saved token, fetches the stored value on demand.
        const tokenEye = $('#mc-f-token-eye');
        if (tokenEye) {
            tokenEye.addEventListener('click', async () => {
                const input   = $('#mc-f-token');
                const showIco = $('#mc-eye-show');
                const hideIco = $('#mc-eye-hide');
                if (!input) return;
                if (input.type === 'password') {
                    if (!input.value && input.dataset.hasToken === '1' && input.dataset.srvId) {
                        try {
                            const r = await fetch(`/api/mcp/servers/${input.dataset.srvId}/token`);
                            if (r.ok) { const j = await r.json(); input.value = j.bearer_token || ''; }
                            else throw new Error(`HTTP ${r.status}`);
                        } catch (e2) { _showToast('Could not load saved token — ' + e2.message, 'error'); return; }
                    }
                    input.type = 'text';
                    if (showIco) showIco.style.display = 'none';
                    if (hideIco) hideIco.style.display = '';
                } else {
                    input.type = 'password';
                    if (showIco) showIco.style.display = '';
                    if (hideIco) hideIco.style.display = 'none';
                }
            });
        }

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
                    await this._mcpLoadStatus();
                    this._mcpRender();
                    _showToast(isNew ? 'Server added' : 'Server updated', 'success');
                } catch (e2) { _showToast('Save failed — ' + e2.message, 'error'); }
            });
        }

        // Health check button
        const testBtn = $('#mc-test-btn');
        if (testBtn) {
            testBtn.addEventListener('click', async () => {
                const activeServ = (S.servers||[]).find(s => s.is_active);
                if (!activeServ) return;
                this._mcpTesting = true;
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
                } catch (e2) { _showToast('Could not save TTL — ' + e2.message, 'error'); }
            });
        }
    }

    // ── General (parameters) ─────────────────────────────────────────────────

    _renderGeneral() {
        const prefs = Preferences.getAll();

        const TEMPERATURE_NOTE = 'Higher = more creative SQL (riskier). 0.2–0.4 is the sweet spot for accuracy.';

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">General</h2>
                <p class="sp-section-desc">Application preferences and runtime behaviour.</p>
            </div>
            <div class="sp-card">
                <div class="sp-card-title">Appearance &amp; Display</div>
                ${this._row('Theme', 'Light is the default; System follows your OS preference.', `
                    <select class="settings-select" id="sp-theme">
                        <option value="light"${prefs.theme==='light'?' selected':''}>Light</option>
                        <option value="dark"${prefs.theme==='dark'?' selected':''}>Dark</option>
                        <option value="system"${prefs.theme==='system'?' selected':''}>System</option>
                    </select>`)}
                ${this._row('Row limit', 'Maximum rows returned per query. Server-enforced.', `
                    <select class="settings-select" id="sp-rowlimit">
                        ${[25,100,500,1000].map(n=>`<option value="${n}"${prefs.rowLimit===n?' selected':''}>${n} rows</option>`).join('')}
                    </select>`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">Chart &amp; Insights</div>
                ${this._row('Default chart type', 'Used when switching to chart view. Auto lets the LLM choose.', `
                    <select class="settings-select" id="sp-charttype">
                        ${CHART_TYPE_OPTIONS.map(t =>
                            `<option value="${t.value}"${prefs.chartType === t.value ? ' selected' : ''}>${t.label}</option>`
                        ).join('')}
                    </select>`)}
                ${this._row('Auto-insights', 'Automatically generate AI insights after every result set.', `
                    <select class="settings-select" id="sp-insights">
                        <option value="on"${prefs.autoInsights==='on'?' selected':''}>On</option>
                        <option value="off"${prefs.autoInsights==='off'?' selected':''}>Off</option>
                    </select>`)}
            </div>
            <div class="sp-card">
                <div class="sp-card-title">AI Model</div>
                ${this._row('LLM temperature', TEMPERATURE_NOTE, `
                    <select class="settings-select" id="sp-temp">
                        <option value="auto">Auto (use defaults)</option>
                        ${[0.0,0.2,0.4,0.6,0.8,1.0].map(n=>{
                            const v=n.toFixed(1);
                            const label = n===0?'0.0 (deterministic)':n===0.2?'0.2 (recommended)':n===1.0?'1.0 (most creative)':v;
                            const sel = prefs.temperature===n?' selected':'';
                            return `<option value="${v}"${sel}>${label}</option>`;
                        }).join('')}
                    </select>`)}
            </div>
            <div class="sp-card-footer">
                <button class="sp-btn-ghost" id="sp-reset-prefs">Reset all to defaults</button>
            </div>
        `;

        // Wire up events
        this._content.querySelector('#sp-theme')?.addEventListener('change', e => {
            Preferences.setTheme(e.target.value);
            if (this._onApplyTheme) this._onApplyTheme(e.target.value);
        });
        this._content.querySelector('#sp-rowlimit')?.addEventListener('change', e => Preferences.setRowLimit(e.target.value));
        this._content.querySelector('#sp-charttype')?.addEventListener('change', e => Preferences.setChartType(e.target.value));
        this._content.querySelector('#sp-insights')?.addEventListener('change', e => Preferences.setAutoInsights(e.target.value));
        this._content.querySelector('#sp-temp')?.addEventListener('change', e => Preferences.setTemperature(e.target.value));
        this._content.querySelector('#sp-reset-prefs')?.addEventListener('click', () => {
            if (!confirm('Reset all preferences to defaults?')) return;
            Preferences.resetAll();
            this._renderGeneral();
            if (this._onApplyTheme) this._onApplyTheme(Preferences.DEFAULTS.theme);
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
            _showToast('Could not update model — ' + e.message, 'error');
        }
    }

    // ── Prompt editor ─────────────────────────────────────────────────────────

    async _renderPrompt(name) {
        // Show loading skeleton while fetching
        if (!this._prompts[name] || this._prompts[name].content === null) {
            this._content.innerHTML = `
                <div class="sp-section-header">
                    <div class="skeleton" style="height:24px;width:200px;margin-bottom:8px;"></div>
                    <div class="skeleton" style="height:14px;width:400px;"></div>
                </div>
                <div class="sp-card" style="padding:24px">
                    <div class="skeleton" style="height:400px;width:100%;border-radius:8px;"></div>
                </div>`;
            await this._fetchPromptContent(name);
        }

        // Ensure the model list is available for the model selector.
        await this._ensureModels();

        const entry = this._prompts[name];
        if (!entry) return;

        const meta    = entry.meta || {};
        const content = entry.content || '';
        const isCustom = meta.is_custom || false;
        const placeholders = meta.placeholders || _extractPlaceholders(content);
        const isDirty = entry.dirty || false;
        const isEditing = entry.editing || false;
        const currentModelName = meta.model_name || null;

        // Build model selector options.
        const availableModels = (this._models || []).filter(m => m.available);
        const modelOptions = [
            `<option value=""${!currentModelName ? ' selected' : ''}>Default (global active model)</option>`,
            ...availableModels.map(m =>
                `<option value="${_esc(m.name)}"${
                    m.name === currentModelName ? ' selected' : ''
                }>${_esc(m.display_name)}</option>`
            ),
        ].join('');

        this._content.innerHTML = `
            <div class="sp-section-header">
                <div class="sp-prompt-title-row">
                    <h2 class="sp-section-title">${_esc(meta.label || name)}</h2>
                    <span class="sp-badge ${isCustom ? 'sp-badge-custom' : 'sp-badge-default'}">${isCustom ? 'Custom' : 'Default'}</span>
                </div>
                <p class="sp-section-desc">${_esc(meta.description || '')}</p>
                <div class="sp-prompt-model-row">
                    <span class="sp-prompt-model-label">Run with model:</span>
                    <select class="settings-select sp-prompt-model-sel" id="sp-prompt-model-${_esc(name)}">${modelOptions}</select>
                </div>
            </div>

            ${placeholders.length ? `
            <div class="sp-placeholder-row">
                <span class="sp-placeholder-label">Placeholders injected at runtime:</span>
                <div class="sp-placeholder-chips">
                    ${placeholders.map(p => `<code class="sp-ph-chip">{${_esc(p)}}</code>`).join('')}
                </div>
            </div>` : ''}

            <div class="sp-prompt-body" id="sp-prompt-body-${name}">
                ${isEditing
                    ? `<textarea class="sp-prompt-textarea" id="sp-prompt-ta-${name}" spellcheck="false">${_esc(content)}</textarea>`
                    : `<div class="sp-prompt-view" id="sp-prompt-view-${name}">${_renderPromptView(content)}</div>`
                }
            </div>

            <div class="sp-prompt-footer">
                <button class="sp-btn-ghost sp-btn-reset" id="sp-reset-${name}"
                    ${isCustom ? '' : 'disabled'}
                    title="${isCustom ? 'Restore original default' : 'Already at default'}">
                    Reset to default
                </button>
                <div class="sp-prompt-footer-right">
                    ${isEditing
                        ? `<button class="sp-btn-secondary" id="sp-cancel-${name}">Cancel</button>
                           <button class="sp-btn-primary" id="sp-save-${name}" ${isDirty ? '' : ''}>Save prompt</button>`
                        : `<button class="sp-btn-secondary" id="sp-edit-${name}">Edit</button>`
                    }
                </div>
            </div>

            ${meta.version > 1 ? `
            <details class="sp-version-history" id="sp-vh-${name}">
                <summary class="sp-vh-summary">
                    <span>Version history</span>
                    <span class="sp-vh-count">${meta.version} versions saved</span>
                </summary>
                <div class="sp-vh-body" id="sp-vh-body-${name}">
                    <p class="sp-vh-loading">Loading&#8230;</p>
                </div>
            </details>` : ''}
        `;

        // Textarea live-dirty tracking
        const ta = this._content.querySelector(`#sp-prompt-ta-${name}`);
        if (ta) {
            ta.addEventListener('input', () => {
                entry.dirty = (ta.value !== content);
            });
        }

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
            if (!confirm('Reset this prompt to its original default? Your custom changes will be lost.')) return;
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
        if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
        try {
            const res = await fetch(`/api/settings/prompts/${name}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updateDot(name, true);
            this._renderPrompt(name);
            _showToast('Prompt saved', 'success');
        } catch (e) {
            console.error('[SettingsPage] save failed:', e);
            _showToast('Save failed — ' + e.message, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Save prompt'; }
        }
    }

    async _resetPrompt(name) {
        try {
            const res = await fetch(`/api/settings/prompts/${name}`, { method: 'DELETE' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updateDot(name, false);
            this._renderPrompt(name);
            _showToast('Reset to default', 'info');
        } catch (e) {
            console.error('[SettingsPage] reset failed:', e);
            _showToast('Reset failed — ' + e.message, 'error');
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
            body.innerHTML = '<p class="sp-vh-loading">No history available.</p>';
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
                if (!confirm('Restore this version? It will become the new active version.')) return;
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
            btnEl.textContent = 'Preview';
            return;
        }
        // Lazy-load content on first open
        if (!area.dataset.loaded) {
            const origText = btnEl.textContent;
            btnEl.disabled = true;
            btnEl.textContent = 'Loading…';
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
        btnEl.textContent = 'Hide';
    }

    async _restoreVersion(name, versionId) {
        try {
            const res = await fetch(`/api/settings/prompts/${name}/restore/${versionId}`, {
                method: 'POST',
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._prompts[name] = { meta: data, content: data.content, dirty: false, editing: false };
            this._updateDot(name, true);
            this._renderPrompt(name);
            _showToast(`Restored to v${data.version - 1} (now saved as v${data.version})`, 'success');
        } catch (e) {
            console.error('[SettingsPage] restore failed:', e);
            _showToast('Restore failed — ' + e.message, 'error');
        }
    }

    // ── Users management ───────────────────────────────────────────────────

    async _renderUsers() {
        const me = window._currentUser || {};

        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">Users</h2>
                <p class="sp-section-desc">Manage workspace members and their roles.</p>
            </div>
            <div class="sp-card" style="padding:var(--space-4)">
                <div id="sp-users-loading"><div class="skeleton" style="height:40px;border-radius:8px;margin-bottom:8px;"></div></div>
                <div id="sp-users-body" style="display:none">
                    <table class="sp-users-table">
                        <thead><tr>
                            <th>Member</th><th>Role</th><th style="width:40px"></th>
                        </tr></thead>
                        <tbody id="sp-users-rows"></tbody>
                    </table>
                    <!-- Add user form -->
                    <div class="sp-add-user-form" id="sp-add-user-form">
                        <input id="sp-add-name"     class="sp-add-user-input sp-add-full" type="text" placeholder="Full name" />
                        <input id="sp-add-email"    class="sp-add-user-input" type="text" placeholder="Email / username" />
                        <input id="sp-add-password" class="sp-add-user-input" type="password" placeholder="Password (min 4 chars)" />
                        <select id="sp-add-role" class="sp-add-user-select">
                            <option value="viewer">Viewer</option>
                            <option value="editor" selected>Editor</option>
                            <option value="admin">Admin</option>
                        </select>
                        <div class="sp-add-full" style="display:flex;align-items:center;gap:var(--space-3)">
                            <button class="sp-add-user-submit" id="sp-add-submit">Add user</button>
                            <span class="sp-users-error" id="sp-add-error"></span>
                        </div>
                    </div>
                </div>
            </div>`;

        await this._loadUsers();
    }

    async _loadUsers() {
        try {
            const res = await fetch('/api/users');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const users = await res.json();

            document.getElementById('sp-users-loading').style.display = 'none';
            document.getElementById('sp-users-body').style.display = 'block';

            this._renderUserRows(users);
            this._wireAddForm();
        } catch (e) {
            document.getElementById('sp-users-loading').innerHTML =
                `<p style="color:var(--color-muted);font-size:13px">Could not load users: ${_esc(e.message)}</p>`;
        }
    }

    _renderUserRows(users) {
        const me = window._currentUser || {};
        const ROLE_LABEL = { admin: 'Admin', editor: 'Editor', viewer: 'Viewer' };
        const tbody = document.getElementById('sp-users-rows');
        if (!tbody) return;

        tbody.innerHTML = users.map(u => {
            const isMe = u.id === me.id;
            const initials = _initials(u.name || u.email);
            const hueStyle = `background:hsl(${u.avatar_hue ?? 220},55%,52%);color:#fff`;
            const youBadge = isMe ? `<span class="sp-user-you">(you)</span>` : '';

            const roleSelect = `
                <select class="sp-role-select" data-uid="${u.id}" ${isMe ? 'disabled' : ''}>
                    <option value="admin"   ${u.role === 'admin'   ? 'selected' : ''}>Admin</option>
                    <option value="editor"  ${u.role === 'editor'  ? 'selected' : ''}>Editor</option>
                    <option value="viewer"  ${u.role === 'viewer'  ? 'selected' : ''}>Viewer</option>
                </select>`;

            const delBtn = isMe ? '' : `
                <button class="sp-user-del-btn" data-uid="${u.id}" title="Remove user">
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
                            <div class="sp-user-name">${_esc(u.name || u.email)}${youBadge}</div>
                            <div class="sp-user-email">${_esc(u.email)}</div>
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
                    _showToast(`Role updated to ${role}`, 'success');
                } catch (e) {
                    _showToast('Could not update role — ' + e.message, 'error');
                    await this._loadUsers(); // revert UI
                }
            });
        });

        // Wire delete buttons
        tbody.querySelectorAll('.sp-user-del-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const uid = Number(btn.dataset.uid);
                const user = users.find(u => u.id === uid);
                if (!confirm(`Remove ${user?.name || user?.email}? This cannot be undone.`)) return;
                try {
                    const r = await fetch(`/api/users/${uid}`, { method: 'DELETE' });
                    if (!r.ok) throw new Error((await r.json()).error || `HTTP ${r.status}`);
                    _showToast('User removed', 'info');
                    await this._loadUsers();
                } catch (e) {
                    _showToast('Could not remove user — ' + e.message, 'error');
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

            if (!name || !email || !password) { err.textContent = 'All fields are required.'; return; }
            if (password.length < 4)           { err.textContent = 'Password must be at least 4 characters.'; return; }

            btn.disabled = true; btn.textContent = 'Adding…';
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
                _showToast(`${name} added`, 'success');
                await this._loadUsers();
            } catch (e) {
                err.textContent = e.message;
            } finally {
                btn.disabled = false; btn.textContent = 'Add user';
            }
        });
    }

    // ── About ─────────────────────────────────────────────────────────

    async _renderAbout() {
        this._content.innerHTML = `
            <div class="sp-section-header">
                <h2 class="sp-section-title">About</h2>
                <p class="sp-section-desc">Application information and configuration.</p>
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
            document.getElementById('sp-about-card').innerHTML = `
                <div class="sp-card-title">${_esc(info.name)}</div>
                <div class="sp-about-grid">
                    ${_aboutRow('Version',       info.version)}
                    ${_aboutRow('LLM Model',     info.llm_model)}
                    ${_aboutRow('Endpoint',      info.llm_endpoint)}
                    ${_aboutRow('API Version',   info.api_version)}
                    ${_aboutRow('LLM Timeout',   info.llm_timeout + 's per call')}
                    ${_aboutRow('Prompts',       info.prompt_count + ' registered')}
                </div>
            `;
        } catch (e) {
            document.getElementById('sp-about-card').innerHTML = `<p style="color:var(--color-muted)">Could not load app info.</p>`;
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _esc(text) {
    const d = document.createElement('div');
    d.textContent = String(text || '');
    return d.innerHTML;
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

function _aboutRow(label, value) {
    return `<div class="sp-about-row">
        <span class="sp-about-label">${_esc(label)}</span>
        <span class="sp-about-value">${_esc(String(value || '—'))}</span>
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
    if (seconds === 0)     return 'No cache';
    if (seconds < 3600)   return `${Math.round(seconds/60)} min`;
    if (seconds < 86400)  return `${Math.round(seconds/3600)} h`;
    return '24 h';
}

function _relativeTime(isoStr) {
    if (!isoStr) return 'never';
    try {
        const diff = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
        if (diff < 60)   return 'just now';
        if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
        return `${Math.floor(diff/86400)}d ago`;
    } catch { return ''; }
}
