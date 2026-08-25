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

const SPARKLE_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9L19 16z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const ARROW_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export class ChartChat {
    /**
     * @param {string} containerId
     * @param {{
     *   getCurrentConfig: () => object|null,
     *   getCurrentResults: () => object|null,
     *   getConnection: () => string,
     *   getCurrentSpec?: () => object|null,
     *   getQueryId?: () => string|null,
     *   onApply: (config: object, derivedSeries: Array, notes?: string|null, edit?: object) => void,
     *   onReset: () => void
     * }} hooks
     */
    constructor(containerId, hooks) {
        this.containerId = containerId;
        this.hooks = hooks || {};
        this.messages = [];     // [{ role, content }]
        this.mounted = false;
        this.enabled = false;
        this.inFlight = null;   // AbortController
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

        // Slim single row: sparkle · (input + Apply) | (Applied · Reset)
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
        input.placeholder = 'Edit this chart in words — “stack by channel, log scale”';
        input.maxLength = MAX_INSTRUCTION_LEN;
        input.disabled = true;
        input.setAttribute('aria-label', 'Refine this chart');
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this._handleSend();
            }
        });

        const applyBtn = document.createElement('button');
        applyBtn.type = 'button';
        applyBtn.className = 'chart-refine-apply';
        applyBtn.innerHTML = `<span>Enhance</span>${ARROW_SVG}`;
        applyBtn.disabled = true;
        applyBtn.addEventListener('click', () => this._handleSend());

        entry.appendChild(input);
        entry.appendChild(applyBtn);

        // Applied state — "✓ Applied: <refinement> · Reset chart"
        const applied = document.createElement('div');
        applied.className = 'chart-refine-applied';
        applied.hidden = true;

        const appliedLabel = document.createElement('span');
        appliedLabel.className = 'chart-refine-applied-label';

        const dot = document.createElement('span');
        dot.className = 'chart-refine-dot';
        dot.textContent = '·';

        const resetBtn = document.createElement('button');
        resetBtn.type = 'button';
        resetBtn.className = 'chart-refine-reset';
        resetBtn.textContent = 'Reset chart';
        resetBtn.title = 'Revert to the original chart. Edits are session-only — your data is never changed.';
        resetBtn.addEventListener('click', () => this._handleReset());

        applied.appendChild(appliedLabel);
        applied.appendChild(dot);
        applied.appendChild(resetBtn);

        row.appendChild(icon);
        row.appendChild(entry);
        row.appendChild(applied);

        // Inline single-line status (progress / warning / error). Success is
        // conveyed by the "Applied" state instead, so this stays hidden then.
        const status = document.createElement('div');
        status.className = 'chart-refine-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.hidden = true;

        container.appendChild(row);
        container.appendChild(status);

        this._rowEl = row;
        this._inputEl = input;
        this._applyBtnEl = applyBtn;
        this._entryEl = entry;
        this._appliedEl = applied;
        this._appliedLabelEl = appliedLabel;
        this._resetBtnEl = resetBtn;
        this._statusEl = status;
    }

    enable() {
        this.enabled = true;
        if (!this.mounted) return;
        this._inputEl.disabled = false;
        this._applyBtnEl.disabled = false;
    }

    disable() {
        this.enabled = false;
        if (!this.mounted) return;
        this._inputEl.disabled = true;
        this._applyBtnEl.disabled = true;
    }

    reset() {
        this.messages = [];
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
            this.inFlight = null;
        }
        if (!this.mounted) return;
        this._clearStatus();
        this._inputEl.value = '';
        this._showEntry();
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
        this._appliedLabelEl.textContent = `Applied: ${label}`;
        this._entryEl.hidden = true;
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
    }

    _clearStatus() {
        if (!this._statusEl) return;
        this._statusEl.textContent = '';
        this._statusEl.hidden = true;
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
        this._inputEl.disabled = busy || !this.enabled;
        this._applyBtnEl.disabled = busy || !this.enabled;
        this._applyBtnEl.classList.toggle('is-busy', !!busy);
        const label = this._applyBtnEl.querySelector('span');
        if (label) label.textContent = busy ? 'Applying…' : 'Apply';
    }

    async _handleSend() {
        if (!this.enabled || !this.mounted) return;
        const instruction = (this._inputEl.value || '').trim();
        if (!instruction) return;

        const config = this.hooks.getCurrentConfig && this.hooks.getCurrentConfig();
        const results = this.hooks.getCurrentResults && this.hooks.getCurrentResults();
        const connection = this.hooks.getConnection ? this.hooks.getConnection() : '';

        if (!config) {
            this._setStatus('Generate a chart first, then I can refine it.', 'warn');
            return;
        }
        if (!connection) {
            this._setStatus('Pick a connection first.', 'warn');
            return;
        }

        this._appendMessage('user', instruction);
        this._setStatus('Working on it…', 'progress');
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
            if (resp.status === 409 && config?.jeenOsmMap) {
                // Result caches are deliberately short-lived. Only resend full
                // rows for a map rebuild after that rare miss, never on the
                // normal view-only edit path.
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
                this._setStatus(`Couldn't apply that change: ${detail}`, 'error');
                return;
            }

            const newConfig = data.chart_config && typeof data.chart_config === 'object'
                ? data.chart_config
                : null;
            const derived = Array.isArray(data.derived_series) ? data.derived_series : [];
            const note = (data.notes && String(data.notes).trim()) || '';
            const outOfScope = !!data.out_of_scope;

            if (outOfScope || !newConfig) {
                const fallback = note || 'That request needs a new query — please ask it in the main question box.';
                this._setStatus(fallback, 'warn');
                return;
            }

            // Apply via the parent (ChartManager owns the render loop + undo).
            if (this.hooks.onApply) {
                try {
                    this.hooks.onApply(newConfig, derived, note || null, data);
                } catch (e) {
                    console.error('[ChartChat] onApply threw', e);
                    this._setStatus('Got a config back but failed to render it. The chart was not changed.', 'error');
                    return;
                }
            }

            this._appendMessage('assistant', note || 'Updated the chart.');
            this._clearStatus();
            this._inputEl.value = '';
            this._showApplied(instruction);
        } catch (e) {
            if (e && e.name === 'AbortError') return; // silent — superseded or reset
            console.error('[ChartChat] send failed', e);
            this._setStatus(`Network error: ${e && e.message ? e.message : 'unknown'}.`, 'error');
        } finally {
            if (myRequestId === this.idCounter) {
                this._setBusy(false);
                this.inFlight = null;
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

    _handleReset() {
        this.reset();
        if (this.hooks.onReset) {
            try { this.hooks.onReset(); } catch (e) { console.error('[ChartChat] onReset threw', e); }
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
