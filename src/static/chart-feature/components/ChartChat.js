/**
 * Chart Chat component
 *
 * Renders a small panel under the chart that lets the user request
 * visualization-only changes in natural language. Each message hits
 * /api/edit-chart, which returns a new ECharts config (and optionally a
 * list of derived-series specs computed locally from the existing data).
 *
 * UX note: the conversation transcript is intentionally NOT shown — only
 * the most recent assistant note (or error) appears in a small inline
 * status line. The internal `messages` array is still kept so we can
 * pass `recent_messages` to the LLM for short-term context.
 *
 * Lifecycle:
 *   - mount()   — build DOM, attach listeners. Idempotent.
 *   - enable()  — turn on input after the first chart renders.
 *   - disable() — grey out (e.g. while the chart is loading).
 *   - reset()   — clear messages and the status line.
 *
 * State is in-memory only. Nothing is persisted.
 *
 * @module ChartChat
 */

import { buildChartSuggestions } from '../utils/chartSuggestions.js?v=72';

const MAX_INSTRUCTION_LEN = 500;
const MAX_TRANSCRIPT_MESSAGES = 30;

export class ChartChat {
    /**
     * @param {string} containerId
     * @param {{
     *   getCurrentConfig: () => object|null,
     *   getCurrentResults: () => object|null,
     *   getConnection: () => string,
     *   onApply: (config: object, derivedSeries: Array, notes?: string|null) => void,
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

        container.classList.add('chart-chat');
        container.innerHTML = '';

        const header = document.createElement('div');
        header.className = 'chart-chat-header';

        const title = document.createElement('div');
        title.className = 'chart-chat-title';
        title.textContent = 'Refine this chart';

        const subtitle = document.createElement('div');
        subtitle.className = 'chart-chat-subtitle';
        subtitle.textContent = 'Edits are session-only. Data is not changed.';

        const titleBlock = document.createElement('div');
        titleBlock.className = 'chart-chat-title-block';
        titleBlock.appendChild(title);
        titleBlock.appendChild(subtitle);

        const resetBtn = document.createElement('button');
        resetBtn.type = 'button';
        resetBtn.className = 'chart-chat-reset-btn';
        resetBtn.textContent = 'Reset chart';
        resetBtn.title = 'Revert to the original chart and clear this conversation';
        resetBtn.addEventListener('click', () => this._handleReset());

        header.appendChild(titleBlock);
        header.appendChild(resetBtn);

        // Inline single-line status (latest assistant note / error). The
        // full transcript is intentionally hidden per product decision.
        const status = document.createElement('div');
        status.className = 'chart-chat-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.hidden = true;

        const chipRow = document.createElement('div');
        chipRow.className = 'chart-chat-chips';

        const inputRow = document.createElement('div');
        inputRow.className = 'chart-chat-input-row';

        const input = document.createElement('textarea');
        input.className = 'chart-chat-input';
        input.rows = 1;
        input.placeholder = 'Ask for a change… (e.g. "make it a line chart" or "add a 3-month moving average")';
        input.maxLength = MAX_INSTRUCTION_LEN;
        input.disabled = true;
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this._handleSend();
            }
        });
        // Auto-grow up to 4 lines.
        input.addEventListener('input', () => {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 96) + 'px';
        });

        const sendBtn = document.createElement('button');
        sendBtn.type = 'button';
        sendBtn.className = 'chart-chat-send-btn';
        sendBtn.textContent = 'Send';
        sendBtn.disabled = true;
        sendBtn.addEventListener('click', () => this._handleSend());

        inputRow.appendChild(input);
        inputRow.appendChild(sendBtn);

        container.appendChild(header);
        container.appendChild(status);
        container.appendChild(chipRow);
        container.appendChild(inputRow);

        this._statusEl = status;
        this._inputEl = input;
        this._sendBtnEl = sendBtn;
        this._chipRowEl = chipRow;
        this._resetBtnEl = resetBtn;

        // Generic chips until a chart exists; refreshSuggestions() tailors them.
        this._renderChips(buildChartSuggestions(null, null));
    }

    enable() {
        this.enabled = true;
        if (!this.mounted) return;
        this._inputEl.disabled = false;
        this._sendBtnEl.disabled = false;
        this._chipRowEl.querySelectorAll('button').forEach(b => { b.disabled = false; });
        // The chart just (re)rendered — tailor chips to its type + data.
        this.refreshSuggestions();
    }

    /**
     * Rebuild the suggestion chips from the CURRENT chart config + data, so
     * they only offer changes that make sense (e.g. moving average only for
     * time/ordered data). Safe to call repeatedly; no-op until mounted.
     */
    refreshSuggestions() {
        if (!this.mounted || !this._chipRowEl) return;
        let list;
        try {
            const config = this.hooks.getCurrentConfig && this.hooks.getCurrentConfig();
            const results = this.hooks.getCurrentResults && this.hooks.getCurrentResults();
            list = buildChartSuggestions(config, results);
        } catch (e) {
            console.warn('[ChartChat] suggestion build failed', e);
            list = buildChartSuggestions(null, null);
        }
        this._renderChips(list);
    }

    _renderChips(list) {
        if (!this._chipRowEl) return;
        this._chipRowEl.innerHTML = '';
        (list || []).forEach((text) => {
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'chart-chat-chip';
            chip.textContent = text;
            chip.disabled = !this.enabled;
            chip.addEventListener('click', () => {
                this._inputEl.value = text;
                this._inputEl.focus();
            });
            this._chipRowEl.appendChild(chip);
        });
    }

    disable() {
        this.enabled = false;
        if (!this.mounted) return;
        this._inputEl.disabled = true;
        this._sendBtnEl.disabled = true;
        this._chipRowEl.querySelectorAll('button').forEach(b => { b.disabled = true; });
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
    }

    // ─────────────────────────────────────────────────────────────────────
    // Internals
    // ─────────────────────────────────────────────────────────────────────

    _setStatus(role, content, kind) {
        if (!this._statusEl) return;
        const text = (content || '').toString().trim();
        if (!text) {
            this._clearStatus();
            return;
        }
        // textContent — never innerHTML — to avoid XSS from LLM output.
        this._statusEl.textContent = text;
        this._statusEl.dataset.role = role || '';
        this._statusEl.dataset.kind = kind || '';
        this._statusEl.hidden = false;
    }

    _clearStatus() {
        if (!this._statusEl) return;
        this._statusEl.textContent = '';
        this._statusEl.hidden = true;
        delete this._statusEl.dataset.role;
        delete this._statusEl.dataset.kind;
    }

    _appendMessage(role, content, kind) {
        const text = (content || '').toString().trim();
        if (!text) return;
        this.messages.push({ role, content: text });
        if (this.messages.length > MAX_TRANSCRIPT_MESSAGES) {
            this.messages.splice(0, this.messages.length - MAX_TRANSCRIPT_MESSAGES);
        }
        // Only the latest assistant note is surfaced in the UI; the user's
        // own input doesn't need to be echoed.
        if (role === 'assistant') {
            this._setStatus(role, text, kind);
        }
    }

    _setBusy(busy) {
        if (!this.mounted) return;
        this._inputEl.disabled = busy || !this.enabled;
        this._sendBtnEl.disabled = busy || !this.enabled;
        this._sendBtnEl.textContent = busy ? 'Sending…' : 'Send';
    }

    async _handleSend() {
        if (!this.enabled || !this.mounted) return;
        const instruction = (this._inputEl.value || '').trim();
        if (!instruction) return;

        const config = this.hooks.getCurrentConfig && this.hooks.getCurrentConfig();
        const results = this.hooks.getCurrentResults && this.hooks.getCurrentResults();
        const connection = this.hooks.getConnection ? this.hooks.getConnection() : '';

        if (!config) {
            this._appendMessage('assistant', 'Generate a chart first, then I can refine it.', 'warn');
            return;
        }
        if (!connection) {
            this._appendMessage('assistant', 'Pick a connection first.', 'warn');
            return;
        }

        this._appendMessage('user', instruction);
        // Show progress where the assistant note will land.
        this._setStatus('assistant', 'Working on it…', 'progress');
        this._inputEl.value = '';
        this._inputEl.style.height = 'auto';
        this._setBusy(true);

        // Cancel any in-flight request before starting a new one.
        if (this.inFlight) {
            try { this.inFlight.abort(); } catch (_) { /* ignore */ }
        }
        this.inFlight = new AbortController();
        const myRequestId = ++this.idCounter;

        try {
            const payload = this._buildPayload(connection, instruction, config, results);
            const resp = await fetch('/api/edit-chart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: this.inFlight.signal,
            });

            if (myRequestId !== this.idCounter) return; // superseded

            const data = await resp.json().catch(() => ({}));

            if (!resp.ok) {
                const detail = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
                this._appendMessage('assistant', `Couldn't apply that change: ${detail}`, 'error');
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
                this._appendMessage('assistant', fallback, 'warn');
                return;
            }

            // Apply via the parent (ChartManager owns the render loop + undo).
            if (this.hooks.onApply) {
                try {
                    this.hooks.onApply(newConfig, derived, note || null);
                } catch (e) {
                    console.error('[ChartChat] onApply threw', e);
                    this._appendMessage('assistant', 'Got a config back but failed to render it. The chart was not changed.', 'error');
                    return;
                }
                // The chart changed (maybe its type) — re-tailor the chips.
                this.refreshSuggestions();
            }

            const summaryParts = [];
            if (note) summaryParts.push(note);
            if (derived.length > 0) {
                const labels = derived
                    .map(d => (d && d.label) || (d && d.operator) || '')
                    .filter(Boolean)
                    .join(', ');
                if (labels) summaryParts.push(`Added overlay: ${labels}.`);
            }
            this._appendMessage(
                'assistant',
                summaryParts.length ? summaryParts.join(' ') : 'Updated the chart.',
                'ok'
            );
        } catch (e) {
            if (e && e.name === 'AbortError') return; // silent — superseded or reset
            console.error('[ChartChat] send failed', e);
            this._appendMessage('assistant', `Network error: ${e && e.message ? e.message : 'unknown'}.`, 'error');
        } finally {
            if (myRequestId === this.idCounter) {
                this._setBusy(false);
                this.inFlight = null;
                this._inputEl.focus();
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
            columns: typed,
            column_names: cols,
            sample_data: sample,
            recent_messages: this.messages.slice(-6).map(m => ({ role: m.role, content: m.content })),
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
