/**
 * Chart Chat ("Refine this chart") component
 *
 * Renders one slim row under the chart that lets the user request
 * visualization-only changes in natural language. Each message hits
 * /api/edit-chart exactly once with the compact v2 manifest and receives
 * validated local chart operations.
 *
 * Layout (matches design handoff): a hairline-separated row with a sparkle
 * AI icon, a single-line rounded inline input, and a small purple "Apply →"
 * button (Enter also applies). After a refinement is applied the row swaps to
 * "✓ Applied: <refinement> · Reset chart" (green confirmation + purple Reset
 * link). "Reset chart" only exists once there is something to reset.
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

const SPARKLE_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9L19 16z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const ARROW_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export class ChartChat {
    /**
     * @param {string} containerId
     * @param {{
     *   getChartManifest: () => object|null,
     *   getChartKind: () => 'sql'|'ml_band'|'ml_basic',
     *   getConnection: () => string,
     *   getQueryId?: () => string|null,
     *   getRevision?: () => string|number,
     *   isRevisionCurrent?: (revision: string|number) => boolean,
     *   onApply: (operations: Array, revision: string|number) => void|Promise<void>,
 *   onTiming?: (timing: object) => void,
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

        // Applied state — "✓ Applied: <refinement> · Reset chart"
        const applied = document.createElement('div');
        applied.className = 'chart-refine-applied';
        applied.hidden = true;

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
        resetBtn.textContent = t('charts.chat.reset');
        resetBtn.title = t('charts.chat.resetTitle');
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

    _showApplied(label) {
        if (!this.mounted) return;
        this._appliedLabelEl.textContent = t('charts.chat.appliedPrefix');
        this._appliedInstructionEl.textContent = label;
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
            const resetBlocked = !available || this.externalBusy
                || (this._localBusy && !this.inFlight);
            this._resetBtnEl.disabled = resetBlocked;
            this._resetBtnEl.setAttribute('aria-disabled', resetBlocked ? 'true' : 'false');
        }
        const label = this._applyBtnEl.querySelector('span');
        if (label) {
            label.textContent = active
                ? t('charts.chat.applying')
                : t('charts.chat.apply');
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

    /** Kept as a compatibility no-op: compact chat is always chart-only. */
    setAnalysisMode(_on) {
        this._analysisMode = false;
        if (!this.mounted) return;
        this._inputEl.placeholder = CHART_PLACEHOLDER();
        this._inputEl.setAttribute('aria-label', t('charts.chat.refine'));
        this._applyBtnEl.title = '';
        const label = this._applyBtnEl.querySelector('span');
        if (label && !this._applyBtnEl.classList.contains('is-busy')) {
            label.textContent = t('charts.chat.apply');
        }
        this._syncControls();
    }

    async _handleSend() {
        if (!this._canSubmit()) return;
        const instruction = (this._inputEl.value || '').trim();
        const chartManifest = this.hooks.getChartManifest && this.hooks.getChartManifest();
        const connection = this.hooks.getConnection ? this.hooks.getConnection() : '';
        const revision = this.hooks.getRevision ? this.hooks.getRevision() : 0;

        if (!chartManifest) {
            this._setStatus(t('charts.chat.needChart'), 'warn');
            this._focusInput();
            return;
        }
        if (!connection) {
            this._setStatus(t('errors.selectConnectionFirst'), 'warn');
            this._focusInput();
            return;
        }

        const payload = this._buildPayload(connection, instruction, chartManifest);
        this._appendMessage('user', instruction);
        this._setStatus(t('charts.chat.working'), 'progress');

        // Cancel any in-flight request before starting a new one.
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
        }
        this.inFlight = new AbortController();
        const myRequestId = ++this.idCounter;
        const requestStarted = performance.now();
        this._setBusy(true);

        try {
            const resp = await fetch('/api/edit-chart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: this.inFlight.signal,
            });
            const responseReceived = performance.now();
            const serverTiming = resp.headers?.get?.('Server-Timing') || '';

            if (myRequestId !== this.idCounter) return; // superseded

            const data = await resp.json().catch(() => ({}));
            const parsedAt = performance.now();

            if (!resp.ok) {
                const detail = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
                this._setStatus(t('charts.chat.applyFailed', { detail: window.I18n ? window.I18n.isolate(detail) : detail }), 'error');
                return;
            }

            if (Number(data.contract_version) !== 2) {
                this._setStatus(t('charts.chat.applyFailed', { detail: 'Unsupported chart edit response.' }), 'error');
                return;
            }
            const operations = Array.isArray(data.operations) ? data.operations : [];
            const note = (data.notes && String(data.notes).trim()) || '';
            const reasonCode = (data.reason_code && String(data.reason_code).trim()) || '';
            const outOfScope = !!data.out_of_scope;

            if (outOfScope || operations.length === 0) {
                const fallback = note || reasonCode || t('charts.chat.outOfScope');
                this._setStatus(fallback, 'warn');
                this._appendMessage('assistant', fallback);
                return;
            }
            if (myRequestId !== this.idCounter
                || (this.hooks.isRevisionCurrent && !this.hooks.isRevisionCurrent(revision))) {
                return;
            }

            // Apply via the parent (ChartManager owns validation, render + undo).
            const applyStarted = performance.now();
            let applyTelemetry = {};
            if (this.hooks.onApply) {
                try {
                    applyTelemetry = await this.hooks.onApply(operations, revision) || {};
                } catch (e) {
                    if (e && e.name === 'AbortError') return;
                    console.error('[ChartChat] onApply threw', e);
                    this._setStatus(t('charts.chat.renderFailed'), 'error');
                    return;
                }
            }
            const renderedAt = performance.now();
            this.hooks.onTiming?.({
                wall_ms: Math.round(renderedAt - requestStarted),
                request_ms: Math.round(responseReceived - requestStarted),
                parse_ms: Math.round(parsedAt - responseReceived),
                apply_ms: Math.round(renderedAt - applyStarted),
                server_timing: serverTiming,
                rebuild_server_timing: applyTelemetry.server_timing || '',
                request_count: 1 + Number(applyTelemetry.request_count || 0),
                rows_uploaded: Number(applyTelemetry.rows_uploaded || 0),
                retries: 0,
                operation_count: operations.length,
                stale_dropped: false,
            });

            this._appendMessage('assistant', note || t('charts.chat.updated'));
            this._inputEl.value = '';
            this._showApplied(instruction);
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

    _buildPayload(connection, instruction, chartManifest) {
        const payload = {
            contract_version: 2,
            connection,
            instruction,
            chart_kind: this.hooks.getChartKind ? this.hooks.getChartKind() : 'sql',
            chart_manifest: chartManifest,
            recent_messages: this.messages.slice(-6).map(m => ({ role: m.role, content: m.content })),
        };
        const queryId = this.hooks.getQueryId ? this.hooks.getQueryId() : null;
        if (queryId !== null && queryId !== undefined && queryId !== '') payload.query_id = queryId;
        return payload;
    }

    async _handleReset() {
        if (!this.mounted || !this.enabled || this.externalBusy) return;
        this.idCounter += 1;
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
            this.inFlight = null;
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
