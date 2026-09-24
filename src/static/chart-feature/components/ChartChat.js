/**
 * Chart Chat ("Refine this chart") component
 *
 * Renders one slim row under the chart that lets the user request
 * visualization-only changes in natural language. Each message hits
 * /api/edit-chart, which returns a new ECharts config (and optionally a
 * list of derived-series specs computed locally from the existing data).
 *
 * Layout (matches design handoff): a hairline-separated row with a sparkle
 * AI icon, a single-line rounded inline input, and a small purple "Apply →"
 * button (Enter also applies). After a refinement the input remains available,
 * with a compact success summary and icon-only Reset control beside it.
 *
 * UX note: the conversation transcript is intentionally NOT shown. Errors and
 * out-of-scope requests surface in a small inline status line under the row.
 * The internal `messages` array is still kept so we can pass `recent_messages`
 * to the LLM for short-term context.
 *
 * Lifecycle:
 *   - mount()   — build DOM, attach listeners. Idempotent.
 *   - enable()  — turn on input after the first chart renders.
 *   - disable() — grey out (e.g. while the chart is loading).
 *   - reset()   — clear messages, revert to the input state.
 *
 * State is in-memory only. Nothing is persisted.
 *
 * @module ChartChat
 */

const MAX_INSTRUCTION_LEN = 500;
const MAX_TRANSCRIPT_MESSAGES = 30;

// Interface strings come from the locale catalog (static/i18n/i18n.js, loaded first).
const t = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const th = (key, args) => (typeof window !== 'undefined' && window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : String(key));

const CHART_PLACEHOLDER = () => t('charts.chat.placeholder');
// ML results: the same bar re-runs the *analysis* (a new child turn), not the chart.
const ANALYSIS_PLACEHOLDER = () => t('charts.chat.analysisPlaceholder');

const SPARKLE_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9L19 16z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const ARROW_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const RESET_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export class ChartChat {
    /**
     * @param {string} containerId
     * @param {{
     *   getCurrentConfig: () => object|null,
     *   getCurrentResults: () => object|null,
     *   getConnection: () => string,
     *   getCurrentSpec?: () => object|null,
     *   getCurrentDerivedSpecs?: () => Array,
     *   getQueryId?: () => string|null,
     *   onQuickEdit?: (instruction: string) => {applied:boolean, summary?:string}|null,
     *   onApply: (config: object, derivedSeries: Array, notes?: string|null, edit?: object) => void|Promise<void>,
     *   onReset: () => void|Promise<void>
     * }} hooks
     */
    constructor(containerId, hooks) {
        this.containerId = containerId;
        this.hooks = hooks || {};
        this.messages = [];     // [{ role, content }]
        this.mounted = false;
        this.enabled = false;
        this.externalBusy = false;
        this.inFlight = null;   // AbortController
        this._localBusy = false;
        this.idCounter = 0;
    }

    mount() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.warn('[ChartChat] Container not found:', this.containerId);
            return;
        }
        if (this.mounted) return;
        this.mounted = true;

        container.classList.add('chart-refine');
        container.innerHTML = '';

        // Entry row: sparkle · input · Apply. Confirmation is rendered below.
        const row = document.createElement('div');
        row.className = 'chart-refine-row';

        const icon = document.createElement('span');
        icon.className = 'chart-refine-icon';
        icon.innerHTML = SPARKLE_SVG;

        // Entry state — input + Apply
        const entry = document.createElement('div');
        entry.className = 'chart-refine-entry';

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'chart-refine-input';
        input.placeholder = CHART_PLACEHOLDER();
        input.setAttribute('dir', 'auto');
        input.maxLength = MAX_INSTRUCTION_LEN;
        input.disabled = true;
        input.setAttribute('aria-label', t('charts.chat.refine'));
        input.setAttribute('aria-busy', 'false');
        input.addEventListener('input', () => this._syncControls());
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this._handleSend();
            }
        });

        const applyBtn = document.createElement('button');
        applyBtn.type = 'button';
        applyBtn.className = 'chart-refine-apply';
        applyBtn.innerHTML = `<span>${th('charts.chat.enhance')}</span>${ARROW_SVG}`;
        applyBtn.disabled = true;
        applyBtn.setAttribute('aria-disabled', 'true');
        applyBtn.addEventListener('click', () => this._handleSend());

        entry.appendChild(input);
        entry.appendChild(applyBtn);

        // Applied state — "✓ <summary> [reset icon]"
        const applied = document.createElement('div');
        applied.className = 'chart-refine-applied';
        applied.hidden = true;
        applied.setAttribute('role', 'status');
        applied.setAttribute('aria-live', 'polite');

        const appliedCheck = document.createElement('span');
        appliedCheck.className = 'chart-refine-applied-check';
        appliedCheck.setAttribute('aria-hidden', 'true');
        appliedCheck.textContent = '✓';

        const appliedPrefix = document.createElement('span');
        appliedPrefix.className = 'chart-refine-applied-label';
        appliedPrefix.textContent = t('charts.chat.appliedPrefix');

        const appliedInstruction = document.createElement('bdi');
        appliedInstruction.className = 'chart-refine-applied-instruction';
        appliedInstruction.setAttribute('dir', 'auto');

        const appliedHint = document.createElement('span');
        appliedHint.className = 'chart-refine-session-hint';
        appliedHint.textContent = t('charts.chat.sessionOnlyHint');

        const dot = document.createElement('span');
        dot.className = 'chart-refine-dot';
        dot.textContent = '·';

        const resetBtn = document.createElement('button');
        resetBtn.type = 'button';
        resetBtn.className = 'chart-refine-reset';
        resetBtn.innerHTML = RESET_SVG;
        resetBtn.title = t('charts.chat.resetTitle');
        resetBtn.setAttribute('aria-label', t('charts.chat.reset'));
        resetBtn.addEventListener('click', () => this._handleReset());

        applied.appendChild(appliedCheck);
        applied.appendChild(appliedPrefix);
        applied.appendChild(appliedInstruction);
        applied.appendChild(dot);
        applied.appendChild(appliedHint);
        applied.appendChild(dot.cloneNode(true));
        applied.appendChild(resetBtn);

        row.appendChild(icon);
        row.appendChild(entry);

        // Keep one live region mounted for every status transition. An empty
        // region is hidden visually by CSS without removing it from the DOM.
        const status = document.createElement('div');
        status.className = 'chart-refine-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.setAttribute('aria-atomic', 'true');

        container.appendChild(row);
        container.appendChild(applied);
        container.appendChild(status);

        this._rowEl = row;
        this._inputEl = input;
        this._applyBtnEl = applyBtn;
        this._entryEl = entry;
        this._appliedEl = applied;
        this._appliedLabelEl = appliedPrefix;
        this._appliedInstructionEl = appliedInstruction;
        this._resetBtnEl = resetBtn;
        this._statusEl = status;
        this.setAnalysisMode(this._analysisMode);
        this._syncControls();
    }

    enable() {
        this.enabled = true;
        if (!this.mounted) return;
        this._syncControls();
    }

    disable() {
        this.enabled = false;
        if (!this.mounted) return;
        this._syncControls();
    }

    reset() {
        this.messages = [];
        this.idCounter += 1;
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
            this.inFlight = null;
        }
        if (!this.mounted) return;
        this._clearStatus();
        this._inputEl.value = '';
        this._showEntry();
        this._localBusy = false;
        this._syncControls();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Internals
    // ─────────────────────────────────────────────────────────────────────

    _showEntry() {
        if (!this.mounted) return;
        this._appliedEl.hidden = true;
        this._entryEl.hidden = false;
    }

    _showApplied(summary) {
        if (!this.mounted) return;
        this._appliedLabelEl.textContent = t('charts.chat.appliedPrefix');
        this._appliedInstructionEl.textContent = summary || t('charts.chat.updated');
        // Keep the edit box available after a successful change. Chart chat is
        // conversational: users commonly follow an edit with "open layers" or
        // another styling adjustment before deciding whether to reset.
        this._entryEl.hidden = false;
        this._appliedEl.hidden = false;
    }

    _setStatus(content, kind) {
        if (!this._statusEl) return;
        const text = (content || '').toString().trim();
        if (!text) {
            this._clearStatus();
            return;
        }
        // textContent — never innerHTML — to avoid XSS from LLM output.
        this._statusEl.textContent = text;
        this._statusEl.dataset.kind = kind || '';
        this._statusEl.hidden = false;
        if ((kind === 'warn' || kind === 'error') && this._appliedEl) {
            this._appliedEl.hidden = true;
        }
    }

    _clearStatus() {
        if (!this._statusEl) return;
        this._statusEl.textContent = '';
        delete this._statusEl.dataset.kind;
    }

    _appendMessage(role, content) {
        const text = (content || '').toString().trim();
        if (!text) return;
        this.messages.push({ role, content: text });
        if (this.messages.length > MAX_TRANSCRIPT_MESSAGES) {
            this.messages.splice(0, this.messages.length - MAX_TRANSCRIPT_MESSAGES);
        }
    }

    _setBusy(busy) {
        if (!this.mounted) return;
        this._localBusy = Boolean(busy);
        this._syncControls();
    }

    _syncControls() {
        if (!this.mounted) return;
        const active = Boolean(this._localBusy || this.externalBusy || this.inFlight);
        // The renderer temporarily disables chart controls while an edit/reset
        // it owns is drawing. Keep this input available and read-only for that
        // interval so the browser does not discard its current focus.
        const available = Boolean(this.enabled || this._localBusy || this.inFlight);
        const hasInstruction = Boolean((this._inputEl.value || '').trim());
        const canSubmit = available && !active && hasInstruction;

        // Native disabled is reserved for a genuinely unavailable component.
        // Busy inputs stay focused and become read-only; blank/busy Apply stays
        // in the tab order while aria-disabled communicates that it cannot run.
        this._inputEl.disabled = !available;
        this._inputEl.readOnly = available && active;
        this._inputEl.setAttribute('aria-busy', active ? 'true' : 'false');
        this._applyBtnEl.disabled = !available;
        this._applyBtnEl.setAttribute('aria-disabled', canSubmit ? 'false' : 'true');
        this._applyBtnEl.classList.toggle('is-busy', active);
        if (this._resetBtnEl) {
            const resetDisabled = !available || this.externalBusy;
            this._resetBtnEl.disabled = resetDisabled;
            this._resetBtnEl.setAttribute('aria-disabled', resetDisabled ? 'true' : 'false');
        }
        const label = this._applyBtnEl.querySelector('span');
        if (label) {
            label.textContent = active
                ? (this._analysisMode ? t('charts.chat.rerunningButton') : t('charts.chat.applying'))
                : (this._analysisMode ? t('charts.chat.rerunAnalysis') : t('charts.chat.apply'));
        }
    }

    _canSubmit() {
        return Boolean(
            this.mounted
            && this.enabled
            && !this._localBusy
            && !this.externalBusy
            && !this.inFlight
            && (this._inputEl.value || '').trim()
        );
    }

    _focusInput() {
        if (!this.mounted || !this.enabled || typeof this._inputEl.focus !== 'function') return;
        this._inputEl.focus({ preventScroll: true });
    }

    setExternalBusy(busy) {
        this.externalBusy = Boolean(busy);
        this._syncControls();
    }

    /**
     * Switch between chart-edit mode and analysis re-run mode. In analysis mode
     * Apply hands the instruction to `hooks.onAnalysisRerun`, which appends a
     * new result turn (never mutates the current one).
     */
    setAnalysisMode(on) {
        this._analysisMode = Boolean(on);
        if (!this.mounted) return;
        this._inputEl.placeholder = this._analysisMode ? ANALYSIS_PLACEHOLDER() : CHART_PLACEHOLDER();
        this._inputEl.setAttribute('aria-label', this._analysisMode ? t('charts.chat.adjustAnalysis') : t('charts.chat.refine'));
        this._applyBtnEl.title = this._analysisMode ? t('charts.chat.rerunTitle') : '';
        const label = this._applyBtnEl.querySelector('span');
        if (label && !this._applyBtnEl.classList.contains('is-busy')) {
            label.textContent = this._analysisMode ? t('charts.chat.rerunAnalysis') : t('charts.chat.apply');
        }
        this._syncControls();
    }

    async _handleSend() {
        if (!this._canSubmit()) return;
        const instruction = (this._inputEl.value || '').trim();

        if (this._analysisMode && typeof this.hooks.onAnalysisRerun === 'function') {
            this._setStatus(t('charts.chat.rerunning'), 'progress');
            this._setBusy(true);
            try {
                await this.hooks.onAnalysisRerun(instruction);
                this._inputEl.value = '';
                this._setStatus(t('charts.chat.rerunCompleted'), 'success');
            } catch (error) {
                if (error && error.name === 'AbortError') return;
                this._setStatus(t('charts.chat.rerunFailed', { detail: String(error && error.message ? error.message : error) }), 'error');
            } finally {
                this._setBusy(false);
                this.setAnalysisMode(this._analysisMode);
                this._focusInput();
            }
            return;
        }

        const config = this.hooks.getCurrentConfig && this.hooks.getCurrentConfig();
        if (!config) {
            this._setStatus(t('charts.chat.needChart'), 'warn');
            this._focusInput();
            return;
        }
        if (typeof this.hooks.onQuickEdit === 'function') {
            try {
                const quick = await this.hooks.onQuickEdit(instruction);
                if (quick && quick.applied) {
                    const summary = quick.summary || t('charts.chat.updated');
                    this._appendMessage('user', instruction);
                    this._appendMessage('assistant', summary);
                    this._clearStatus();
                    this._inputEl.value = '';
                    this._showApplied(summary);
                    return;
                }
                if (quick && quick.noChange) {
                    this._setStatus(t('charts.chat.noVisibleChange'), 'warn');
                    return;
                }
            } catch (error) {
                console.error('[ChartChat] quick edit failed', error);
                this._setStatus(t('charts.chat.renderFailed'), 'error');
                return;
            }
        }

        const results = this.hooks.getCurrentResults && this.hooks.getCurrentResults();
        const connection = this.hooks.getConnection ? this.hooks.getConnection() : '';
        if (!connection) {
            this._setStatus(t('errors.selectConnectionFirst'), 'warn');
            this._focusInput();
            return;
        }

        this._appendMessage('user', instruction);
        this._setStatus(t('charts.chat.working'), 'progress');
        this._setBusy(true);

        // Cancel any in-flight request before starting a new one.
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
        }
        this.inFlight = new AbortController();
        const myRequestId = ++this.idCounter;

        try {
            const payload = this._buildPayload(connection, instruction, config, results);
            let resp = await fetch('/api/edit-chart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: this.inFlight.signal,
            });
            if (resp.status === 409) {
                // Semantic edits and map rebuilds require the full result set.
                // Result caches are short-lived and replica-local, so retry
                // once with rows after a cache miss.
                resp = await fetch('/api/edit-chart', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...payload, ...this._fallbackRows(results) }),
                    signal: this.inFlight.signal,
                });
            }

            if (myRequestId !== this.idCounter) return; // superseded

            const data = await resp.json().catch(() => ({}));

            if (!resp.ok) {
                const detail = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
                this._setStatus(t('charts.chat.applyFailed', { detail: window.I18n ? window.I18n.isolate(detail) : detail }), 'error');
                return;
            }

            const newConfig = data.chart_config && typeof data.chart_config === 'object'
                ? data.chart_config
                : null;
            const derived = Array.isArray(data.derived_series) ? data.derived_series : [];
            const note = (data.notes && String(data.notes).trim()) || '';
            const outOfScope = !!data.out_of_scope;

            if (outOfScope || !newConfig) {
                const fallback = note || t('charts.chat.outOfScope');
                this._setStatus(fallback, 'warn');
                return;
            }
            if (!derived.length && stableJson(newConfig) === stableJson(config)) {
                this._setStatus(note || t('charts.chat.noVisibleChange'), 'warn');
                return;
            }

            // Apply via the parent (ChartManager owns the render loop + undo).
            if (this.hooks.onApply) {
                try {
                    const result = await this.hooks.onApply(newConfig, derived, note || null, data);
                    if (result && result.noChange) {
                        this._setStatus(note || t('charts.chat.noVisibleChange'), 'warn');
                        return;
                    }
                } catch (e) {
                    console.error('[ChartChat] onApply threw', e);
                    this._setStatus(t('charts.chat.renderFailed'), 'error');
                    return;
                }
            }

            this._appendMessage('assistant', note || t('charts.chat.updated'));
            this._inputEl.value = '';
            this._showApplied(note || t('charts.chat.updated'));
            this._setStatus(t('charts.chat.appliedAnnouncement'), 'success');
        } catch (e) {
            if (e && e.name === 'AbortError') return; // silent — superseded or reset
            console.error('[ChartChat] send failed', e);
            this._setStatus(t('charts.chat.networkError', { detail: e && e.message ? e.message : t('common.unknownError') }), 'error');
        } finally {
            if (myRequestId === this.idCounter) {
                this.inFlight = null;
                this._setBusy(false);
                this._focusInput();
            }
        }
    }

    _buildPayload(connection, instruction, config, results) {
        const cols = (results && Array.isArray(results.columns)) ? results.columns : [];
        const rows = (results && (results.data || results.rows)) || [];
        const sample = rows.slice(0, 10).map(row => {
            if (Array.isArray(row)) return row;
            return cols.map(c => row[c]);
        });
        // Best-effort type guess so the LLM has something to ground on.
        const typed = cols.map(name => ({ name, type: guessType(sample, cols.indexOf(name)) }));

        return {
            connection,
            instruction,
            current_config: config,
            chart_spec: this.hooks.getCurrentSpec ? this.hooks.getCurrentSpec() : null,
            active_derived_series: this.hooks.getCurrentDerivedSpecs
                ? this.hooks.getCurrentDerivedSpecs()
                : [],
            query_id: this.hooks.getQueryId ? this.hooks.getQueryId() : null,
            columns: typed,
            column_names: cols,
            sample_data: sample,
            recent_messages: this.messages.slice(-6).map(m => ({ role: m.role, content: m.content })),
        };
    }

    _fallbackRows(results) {
        const cols = (results && Array.isArray(results.columns)) ? results.columns : [];
        const rows = (results && (results.data || results.rows)) || [];
        return {
            all_data: rows.map((row) => (
                Array.isArray(row) ? row : cols.map((column) => row[column])
            )),
        };
    }

    async _handleReset() {
        if (!this.mounted || !this.enabled || this.externalBusy) return;
        if (this.inFlight) {
            this.idCounter += 1;
            try { this.inFlight.abort(); } catch (_) { /* already settled */ }
            this.inFlight = null;
            this._localBusy = false;
        } else if (this._localBusy) {
            return;
        }
        this._setStatus(t('charts.chat.resetting'), 'progress');
        this._setBusy(true);
        try {
            if (this.hooks.onReset) await this.hooks.onReset();
            this.messages = [];
            this._inputEl.value = '';
            this._showEntry();
            this._setStatus(t('charts.chat.resetComplete'), 'success');
        } catch (e) {
            console.error('[ChartChat] onReset threw', e);
            this._setStatus(t('charts.chat.resetFailed'), 'error');
        } finally {
            this._setBusy(false);
            this._focusInput();
        }
    }
}

function guessType(sampleRows, idx) {
    if (idx < 0 || !Array.isArray(sampleRows) || sampleRows.length === 0) return 'string';
    let numeric = 0;
    let nonNull = 0;
    for (const row of sampleRows) {
        const cell = row[idx];
        if (cell === null || cell === undefined || cell === '') continue;
        nonNull++;
        const cleaned = String(cell).replace(/[$€£¥,\s]/g, '');
        if (Number.isFinite(Number(cleaned))) numeric++;
    }
    if (nonNull === 0) return 'string';
    return numeric / nonNull >= 0.7 ? 'number' : 'string';
}

function stableJson(value) {
    const normalize = (item) => {
        if (Array.isArray(item)) return item.map(normalize);
        if (!item || typeof item !== 'object') return item;
        return Object.fromEntries(
            Object.keys(item).sort().map((key) => [key, normalize(item[key])])
        );
    };
    try { return JSON.stringify(normalize(value)); } catch (_) { return ''; }
}
