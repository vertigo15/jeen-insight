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
 *   OTHER       At the bottom — About + Close
 *
 * @module settingsPage
 */

import { Preferences } from './preferences.js';

// ── Icons (inline SVG snippets, 18×18) ────────────────────────────────────────
const ICONS = {
    models:     `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="oklch(52% 0.22 280)"/><text x="50%" y="55%" font-family="system-ui,-apple-system,sans-serif" font-weight="800" font-size="18" fill="#FFFFFF" dominant-baseline="middle" text-anchor="middle">J</text></svg>`,
    general:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
    prompt:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    about:      `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    close:      `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
};

// ── Navigation definition ─────────────────────────────────────────────────────
const NAV = [
    {
        group: 'USER',
        items: [
            { id: 'general',    label: 'General',   icon: ICONS.general, type: 'general' },
            { id: 'ai-models',  label: 'AI Models', icon: ICONS.models,  type: 'ai-models' },
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
        sidebarHeader.innerHTML = `<span class="sp-sidebar-title">SETTINGS</span>`;
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

        // Bottom nav
        const navBottom = document.createElement('div');
        navBottom.className = 'sp-nav-bottom';

        BOTTOM_NAV.forEach(item => {
            navBottom.appendChild(this._buildNavItem(item));
        });

        // Close button
        const closeBtn = document.createElement('button');
        closeBtn.className = 'sp-nav-item sp-close-btn';
        closeBtn.innerHTML = `${ICONS.close}<span>Close</span>`;
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
        } else if (id === 'ai-models') {
            this._renderModels();
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
                        <option value="auto"${prefs.chartType==='auto'?' selected':''}>🤖 Auto (LLM picks)</option>
                        <option value="bar"${prefs.chartType==='bar'?' selected':''}>📊 Bar</option>
                        <option value="line"${prefs.chartType==='line'?' selected':''}>📈 Line</option>
                        <option value="pie"${prefs.chartType==='pie'?' selected':''}>🥧 Pie</option>
                        <option value="area"${prefs.chartType==='area'?' selected':''}>📉 Area</option>
                        <option value="scatter"${prefs.chartType==='scatter'?' selected':''}>⚫ Scatter</option>
                        <option value="horizontal_bar"${prefs.chartType==='horizontal_bar'?' selected':''}>📊 Horizontal bar</option>
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

    // ── About ─────────────────────────────────────────────────────────────────

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

function _showToast(msg, type) {
    if (typeof window.showToast === 'function') {
        window.showToast(msg, type);
    }
}
