/**
 * Settings panel
 *
 * A small modal that lets the user tune runtime behaviour without touching
 * code or env vars. All values persist via the Preferences module.
 *
 * UI policies:
 *   - Esc / click-on-backdrop / Done button all close the modal.
 *   - Saves are immediate per-field (no separate Save button).
 *   - Theme changes apply live; the rest take effect on the next query
 *     or the next chart render.
 *
 * @module settingsPanel
 */

import { Preferences } from './preferences.js';

const TEMPERATURE_NOTE =
    'Higher = more creative SQL (riskier). 0.2–0.4 is the sweet spot for accuracy.';

export class SettingsPanel {
    constructor() {
        this._open = false;
        this._root = null;          // <div class="settings-overlay">
        this._dialog = null;        // <div class="settings-modal">
        this._lastFocused = null;
        this._onApplyTheme = null;  // optional callback (newTheme) => void
    }

    /**
     * Build the DOM once and attach to <body>. Hidden until `open()`.
     * @param {{ onApplyTheme?: (newTheme: string) => void }} hooks
     */
    mount(hooks = {}) {
        if (this._root) return;
        this._onApplyTheme = hooks.onApplyTheme || null;

        const overlay = document.createElement('div');
        overlay.className = 'settings-overlay';
        overlay.hidden = true;
        overlay.setAttribute('role', 'presentation');
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) this.close();
        });

        const dialog = document.createElement('div');
        dialog.className = 'settings-modal';
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-labelledby', 'settings-title');
        dialog.tabIndex = -1;

        // Header
        const header = document.createElement('div');
        header.className = 'settings-header';

        const title = document.createElement('h2');
        title.id = 'settings-title';
        title.className = 'settings-title';
        title.textContent = 'Settings';

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'settings-close-btn';
        closeBtn.setAttribute('aria-label', 'Close settings');
        closeBtn.textContent = '×';
        closeBtn.addEventListener('click', () => this.close());

        header.appendChild(title);
        header.appendChild(closeBtn);

        // Body (form rows)
        const body = document.createElement('div');
        body.className = 'settings-body';

        const prefs = Preferences.getAll();

        body.appendChild(this._row({
            label: 'Theme',
            help: 'Light is the default; System follows your OS.',
            control: this._select(
                prefs.theme,
                [
                    { value: 'light', label: 'Light' },
                    { value: 'dark', label: 'Dark' },
                    { value: 'system', label: 'System' },
                ],
                (v) => {
                    Preferences.setTheme(v);
                    if (this._onApplyTheme) this._onApplyTheme(v);
                },
            ),
        }));

        body.appendChild(this._row({
            label: 'Row limit',
            help: 'Maximum rows returned by every query. Server-enforced.',
            control: this._select(
                String(prefs.rowLimit),
                [
                    { value: '25', label: '25 rows' },
                    { value: '100', label: '100 rows' },
                    { value: '500', label: '500 rows' },
                    { value: '1000', label: '1000 rows' },
                ],
                (v) => Preferences.setRowLimit(v),
            ),
        }));

        body.appendChild(this._row({
            label: 'Default chart type',
            help: 'Used when you switch to chart view. "Auto" lets the LLM choose.',
            control: this._select(
                prefs.chartType,
                [
                    { value: 'auto', label: '🤖 Auto (LLM picks)' },
                    { value: 'bar', label: '📊 Bar' },
                    { value: 'line', label: '📈 Line' },
                    { value: 'pie', label: '🥧 Pie' },
                    { value: 'area', label: '📉 Area' },
                    { value: 'scatter', label: '⚫ Scatter' },
                    { value: 'horizontal_bar', label: '📊 Horizontal bar' },
                ],
                (v) => Preferences.setChartType(v),
            ),
        }));

        body.appendChild(this._row({
            label: 'AI Analytics',
            help: 'Generate insights after query results (runs in background — data appears immediately).',
            control: this._select(
                prefs.aiAnalytics,
                [
                    { value: 'on',  label: '✨ On (background)' },
                    { value: 'off', label: 'Off' },
                ],
                (v) => Preferences.setAiAnalytics(v),
            ),
        }));

        body.appendChild(this._row({
            label: 'LLM timeout',
            help: 'Max seconds per LLM call. Lower = fail fast on slow days. "Server" uses the .env default.',
            control: this._select(
                prefs.llmTimeout,
                [
                    { value: 'server', label: 'Server default' },
                    { value: '10',    label: '10 s' },
                    { value: '15',    label: '15 s' },
                    { value: '20',    label: '20 s' },
                    { value: '30',    label: '30 s (recommended)' },
                    { value: '60',    label: '60 s' },
                    { value: '120',   label: '120 s' },
                ],
                (v) => Preferences.setLlmTimeout(v),
            ),
        }));

        body.appendChild(this._row({
            label: 'LLM temperature',
            help: TEMPERATURE_NOTE,
            control: this._temperatureControl(prefs.temperature),
        }));

        // Footer
        const footer = document.createElement('div');
        footer.className = 'settings-footer';

        const resetBtn = document.createElement('button');
        resetBtn.type = 'button';
        resetBtn.className = 'settings-reset-btn';
        resetBtn.textContent = 'Reset to defaults';
        resetBtn.addEventListener('click', () => this._handleReset());

        const doneBtn = document.createElement('button');
        doneBtn.type = 'button';
        doneBtn.className = 'settings-done-btn';
        doneBtn.textContent = 'Done';
        doneBtn.addEventListener('click', () => this.close());

        footer.appendChild(resetBtn);
        footer.appendChild(doneBtn);

        dialog.appendChild(header);
        dialog.appendChild(body);
        dialog.appendChild(footer);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        this._root = overlay;
        this._dialog = dialog;

        // Esc closes.
        document.addEventListener('keydown', (e) => {
            if (this._open && e.key === 'Escape') {
                e.preventDefault();
                this.close();
            }
        });
    }

    open() {
        if (!this._root) this.mount();
        this._refreshValues();
        this._lastFocused = document.activeElement;
        this._root.hidden = false;
        this._open = true;
        // Move focus into the dialog for keyboard users.
        requestAnimationFrame(() => {
            const first = this._dialog.querySelector('select, input, button');
            if (first) first.focus();
        });
    }

    close() {
        if (!this._open) return;
        this._root.hidden = true;
        this._open = false;
        if (this._lastFocused && typeof this._lastFocused.focus === 'function') {
            this._lastFocused.focus();
        }
    }

    toggle() {
        this._open ? this.close() : this.open();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Internals
    // ─────────────────────────────────────────────────────────────────────

    _row({ label, help, control }) {
        const row = document.createElement('div');
        row.className = 'settings-row';

        const labelBlock = document.createElement('div');
        labelBlock.className = 'settings-row-label-block';

        const labelEl = document.createElement('label');
        labelEl.className = 'settings-row-label';
        labelEl.textContent = label;
        labelBlock.appendChild(labelEl);

        if (help) {
            const helpEl = document.createElement('div');
            helpEl.className = 'settings-row-help';
            helpEl.textContent = help;
            labelBlock.appendChild(helpEl);
        }

        const controlBlock = document.createElement('div');
        controlBlock.className = 'settings-row-control';
        controlBlock.appendChild(control);

        row.appendChild(labelBlock);
        row.appendChild(controlBlock);
        return row;
    }

    _select(currentValue, options, onChange) {
        const select = document.createElement('select');
        select.className = 'settings-select';
        for (const opt of options) {
            const o = document.createElement('option');
            o.value = opt.value;
            o.textContent = opt.label;
            if (String(opt.value) === String(currentValue)) o.selected = true;
            select.appendChild(o);
        }
        select.addEventListener('change', (e) => {
            try { onChange(e.target.value); } catch (err) { console.error('[Settings] save failed', err); }
        });
        return select;
    }

    _temperatureControl(currentValue) {
        // currentValue is null | number in [0,1]
        const wrap = document.createElement('div');
        wrap.className = 'settings-temp-wrap';

        const select = document.createElement('select');
        select.className = 'settings-select';
        const presets = [
            { value: 'auto', label: 'Auto (use defaults)' },
            { value: '0.0', label: '0.0 (deterministic)' },
            { value: '0.2', label: '0.2 (recommended)' },
            { value: '0.4', label: '0.4' },
            { value: '0.6', label: '0.6' },
            { value: '0.8', label: '0.8 (creative)' },
            { value: '1.0', label: '1.0 (most creative)' },
        ];
        const current = currentValue === null
            ? 'auto'
            : presets.find(p => Number(p.value) === currentValue)?.value || 'auto';
        for (const p of presets) {
            const o = document.createElement('option');
            o.value = p.value;
            o.textContent = p.label;
            if (p.value === current) o.selected = true;
            select.appendChild(o);
        }
        select.addEventListener('change', (e) => {
            Preferences.setTemperature(e.target.value);
        });
        wrap.appendChild(select);
        return wrap;
    }

    _handleReset() {
        if (!confirm('Reset all settings to defaults?')) return;
        Preferences.resetAll();
        // Re-render the form so each control reflects the new defaults.
        this._refreshValues();
        if (this._onApplyTheme) {
            this._onApplyTheme(Preferences.DEFAULTS.theme);
        }
    }

    _refreshValues() {
        if (!this._dialog) return;
        const prefs = Preferences.getAll();
        const selects = this._dialog.querySelectorAll('.settings-select');
        // Order matches the order rows were appended:
        // [0] theme  [1] rowLimit  [2] chartType  [3] aiAnalytics  [4] llmTimeout  [5] temperature
        if (selects[0]) selects[0].value = prefs.theme;
        if (selects[1]) selects[1].value = String(prefs.rowLimit);
        if (selects[2]) selects[2].value = prefs.chartType;
        if (selects[3]) selects[3].value = prefs.aiAnalytics;
        if (selects[4]) selects[4].value = prefs.llmTimeout;
        if (selects[5]) {
            selects[5].value = prefs.temperature === null
                ? 'auto'
                : String(prefs.temperature.toFixed(1));
        }
    }
}
