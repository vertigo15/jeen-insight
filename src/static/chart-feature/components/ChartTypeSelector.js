/**
 * Chart Type Selector Component
 *
 * A custom, designed dropdown (button + floating listbox) for switching chart
 * types. Replaces the old native <select>:
 *   - "Auto (LLM Recommended)" is pinned on top with a hairline divider.
 *   - Monochrome stroke icons (no emoji), purple check + tint on the selection.
 *   - Scrollable at ~380px, hover states, full keyboard navigation.
 *   - The button label reflects the choice; on Auto it shows "<Type> · Auto"
 *     (e.g. "Bar · Auto") once the LLM's pick is known — replacing the old
 *     amber "LLM selected" banner.
 *
 * The menu is portaled to <body> and positioned with position:fixed so it can
 * escape the clipping/scroll contexts of the results card (Ask mode) and the
 * chat thread (Chat mode).
 *
 * @module ChartTypeSelector
 */

/// <reference path="../types/chart.types.js" />

import { CHART_TYPE_OPTIONS, CHART_TYPE_VALUES, getChartTypeOption } from '../chartTypes.js?v=78';

const CARET_SVG =
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
    '<path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const CHECK_SVG =
    '<svg class="ctype-check" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
    '<path d="M5 12l5 5 9-11" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/** Renders a monochrome stroke icon that inherits colour via `currentColor`. */
function iconSvg(pathData, cls) {
    return `<svg class="${cls}" width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">` +
        `<path d="${pathData}" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

/** Short name used for the "· Auto" suffix (mockup shows "Bar · Auto"). */
function shortLabel(label) {
    return String(label).replace(/\s+(Chart|Plot)$/, '');
}

/**
 * Creates a designed dropdown selector for chart types.
 */
export class ChartTypeSelector {
    /**
     * @param {string} containerId - ID of container to render selector in
     * @param {Function} onChange - Callback when chart type changes (chartType) => void
     */
    constructor(containerId, onChange, options = {}) {
        this.containerId = containerId;
        const allowedValues = options.allowedTypes instanceof Set
            ? options.allowedTypes
            : (Array.isArray(options.allowedTypes) ? new Set(options.allowedTypes) : null);
        this.chartTypes = allowedValues
            ? CHART_TYPE_OPTIONS.filter((type) => allowedValues.has(type.value))
            : CHART_TYPE_OPTIONS.slice();
        if (!this.chartTypes.some((type) => type.value === 'auto')) {
            this.chartTypes.unshift(getChartTypeOption('auto'));
        }

        // Honour the user's saved default chart type from the settings panel;
        // fall back to 'auto' if missing or invalid.
        let defaultType = 'auto';
        try {
            if (typeof window !== 'undefined' && window.JeenPreferences) {
                defaultType = window.JeenPreferences.getAll().chartType || 'auto';
            }
        } catch (_) { /* keep 'auto' on any failure */ }
        if (!CHART_TYPE_VALUES.has(defaultType) || !this.chartTypes.some((type) => type.value === defaultType)) {
            defaultType = 'auto';
        }

        this.currentType = defaultType;
        this.onChange = onChange;
        this.llmRecommendation = null;

        this.isOpen = false;
        this.activeIndex = -1;

        this.root = null;
        this.btnEl = null;
        this.menuEl = null;

        this._menuId = `ctype-menu-${Math.random().toString(36).slice(2, 8)}`;

        // Bound handlers so add/remove target the same reference.
        this._onDocPointer = (e) => this._handleDocPointer(e);
        this._onKeyDown = (e) => this._handleKeyDown(e);
        this._onReposition = () => { if (this.isOpen) this._positionMenu(); };

        console.log('[ChartTypeSelector] Initialized, default=', defaultType);
    }

    /**
     * Renders the chart type selector button. The menu is created lazily and
     * portaled to <body> on first open.
     */
    render() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.error(`[ChartTypeSelector] Container ${this.containerId} not found`);
            return;
        }

        // Defensive: a prior instance may have left a portaled menu / listeners.
        this._teardownMenu();

        this.root = container;
        container.classList.add('ctype-host');
        container.innerHTML = `
            <div class="ctype">
                <button type="button" class="ctype-btn" aria-haspopup="listbox" aria-expanded="false" aria-controls="${this._menuId}" title="Chart type">
                    <span class="ctype-btn-icon">${iconSvg(this._buttonIcon(), 'ctype-glyph')}</span>
                    <span class="ctype-btn-label">${escapeHtml(this._buttonLabel())}</span>
                    <span class="ctype-caret">${CARET_SVG}</span>
                </button>
            </div>`;

        this.btnEl = container.querySelector('.ctype-btn');
        this.btnEl.addEventListener('click', (e) => { e.preventDefault(); this.toggle(); });
        this.btnEl.addEventListener('keydown', this._onKeyDown);

        console.log('[ChartTypeSelector] Rendered');
    }

    // ── Menu lifecycle ──────────────────────────────────────────────────────

    _ensureMenu() {
        if (this.menuEl) return;
        const menu = document.createElement('div');
        menu.className = 'ctype-menu';
        menu.id = this._menuId;
        menu.setAttribute('role', 'listbox');
        menu.setAttribute('aria-label', 'Chart type');
        menu.hidden = true;
        menu.innerHTML = this.chartTypes.map((t, i) => this._itemHtml(t, i)).join('');

        menu.addEventListener('mousedown', (e) => e.preventDefault()); // keep button focus
        menu.addEventListener('click', (e) => {
            const item = e.target.closest('.ctype-item');
            if (item) this._selectByValue(item.dataset.value);
        });
        menu.addEventListener('mousemove', (e) => {
            const item = e.target.closest('.ctype-item');
            if (item) this._setActive(Number(item.dataset.index), false);
        });

        document.body.appendChild(menu);
        this.menuEl = menu;
    }

    _itemHtml(t, i) {
        const selected = t.value === this.currentType;
        const divider = t.value === 'auto' ? ' ctype-item--divider' : '';
        return `<button type="button" role="option" tabindex="-1"` +
            ` class="ctype-item${selected ? ' is-selected' : ''}${divider}"` +
            ` data-value="${t.value}" data-index="${i}" id="${this._menuId}-opt-${i}"` +
            ` aria-selected="${selected ? 'true' : 'false'}">` +
            `${iconSvg(t.icon, 'ctype-item-icon')}` +
            `<span class="ctype-item-label">${escapeHtml(t.label)}</span>` +
            `${CHECK_SVG}</button>`;
    }

    _teardownMenu() {
        document.removeEventListener('pointerdown', this._onDocPointer, true);
        window.removeEventListener('scroll', this._onReposition, true);
        window.removeEventListener('resize', this._onReposition);
        if (this.menuEl && this.menuEl.parentNode) {
            this.menuEl.parentNode.removeChild(this.menuEl);
        }
        this.menuEl = null;
        this.isOpen = false;
    }

    // ── Open / close ────────────────────────────────────────────────────────

    open() {
        if (this.isOpen || !this.btnEl) return;
        this._ensureMenu();
        this.isOpen = true;
        this.btnEl.setAttribute('aria-expanded', 'true');
        const wrap = this.root && this.root.querySelector('.ctype');
        if (wrap) wrap.classList.add('is-open');
        this._refreshSelectedStates();
        this.menuEl.hidden = false;
        this._positionMenu();

        document.addEventListener('pointerdown', this._onDocPointer, true);
        window.addEventListener('scroll', this._onReposition, true);
        window.addEventListener('resize', this._onReposition);

        const selIdx = this.chartTypes.findIndex((t) => t.value === this.currentType);
        this._setActive(selIdx >= 0 ? selIdx : 0, true);
    }

    close() {
        if (!this.isOpen) return;
        this.isOpen = false;
        if (this.btnEl) {
            this.btnEl.setAttribute('aria-expanded', 'false');
            this.btnEl.removeAttribute('aria-activedescendant');
        }
        const wrap = this.root && this.root.querySelector('.ctype');
        if (wrap) wrap.classList.remove('is-open');
        if (this.menuEl) this.menuEl.hidden = true;
        this.activeIndex = -1;

        document.removeEventListener('pointerdown', this._onDocPointer, true);
        window.removeEventListener('scroll', this._onReposition, true);
        window.removeEventListener('resize', this._onReposition);
    }

    toggle() { this.isOpen ? this.close() : this.open(); }

    _positionMenu() {
        const menu = this.menuEl;
        const btn = this.btnEl;
        if (!menu || !btn) return;
        const r = btn.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const width = Math.min(230, vw - 16);
        menu.style.width = `${width}px`;

        const menuH = Math.min(menu.scrollHeight, 380);
        let left = r.left;
        if (left + width > vw - 8) left = vw - 8 - width;
        if (left < 8) left = 8;

        const spaceBelow = vh - r.bottom;
        let top;
        if (spaceBelow < menuH + 12 && r.top > spaceBelow) {
            top = Math.max(8, r.top - 6 - menuH); // flip up
        } else {
            top = r.bottom + 6;
        }
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
    }

    // ── Keyboard + pointer ──────────────────────────────────────────────────

    _handleDocPointer(e) {
        if (!this.isOpen) return;
        const t = e.target;
        if (this.menuEl && this.menuEl.contains(t)) return;
        if (this.btnEl && this.btnEl.contains(t)) return;
        this.close();
    }

    _handleKeyDown(e) {
        const key = e.key;
        if (!this.isOpen) {
            if (key === 'ArrowDown' || key === 'Enter' || key === ' ' || key === 'Spacebar') {
                e.preventDefault();
                this.open();
            }
            return;
        }
        switch (key) {
            case 'ArrowDown': e.preventDefault(); this._move(1); break;
            case 'ArrowUp': e.preventDefault(); this._move(-1); break;
            case 'Home': e.preventDefault(); this._setActive(0, true); break;
            case 'End': e.preventDefault(); this._setActive(this.chartTypes.length - 1, true); break;
            case 'Enter':
            case ' ':
            case 'Spacebar':
                e.preventDefault();
                if (this.activeIndex >= 0) this._selectByValue(this.chartTypes[this.activeIndex].value);
                break;
            case 'Escape':
                e.preventDefault();
                this.close();
                if (this.btnEl) this.btnEl.focus();
                break;
            case 'Tab':
                this.close();
                break;
            default:
                break;
        }
    }

    _move(delta) {
        let i = this.activeIndex + delta;
        if (i < 0) i = this.chartTypes.length - 1;
        if (i >= this.chartTypes.length) i = 0;
        this._setActive(i, true);
    }

    _setActive(i, scroll) {
        this.activeIndex = i;
        if (!this.menuEl) return;
        const items = this.menuEl.querySelectorAll('.ctype-item');
        items.forEach((el, idx) => el.classList.toggle('is-active', idx === i));
        const el = items[i];
        if (el && this.btnEl) {
            this.btnEl.setAttribute('aria-activedescendant', el.id);
            if (scroll) el.scrollIntoView({ block: 'nearest' });
        }
    }

    _selectByValue(value) {
        this.close();
        if (this.btnEl) this.btnEl.focus();
        this.handleTypeChange(value);
        this._updateButton();
        this._refreshSelectedStates();
    }

    _refreshSelectedStates() {
        if (!this.menuEl) return;
        this.menuEl.querySelectorAll('.ctype-item').forEach((el) => {
            const sel = el.dataset.value === this.currentType;
            el.classList.toggle('is-selected', sel);
            el.setAttribute('aria-selected', sel ? 'true' : 'false');
        });
    }

    // ── Button label / icon ─────────────────────────────────────────────────

    _buttonLabel() {
        if (this.currentType === 'auto') {
            if (this.llmRecommendation) {
                const o = getChartTypeOption(this.llmRecommendation);
                return `${shortLabel(o ? o.label : this.llmRecommendation)} · Auto`;
            }
            return 'Auto';
        }
        const o = getChartTypeOption(this.currentType);
        return o ? o.label : this.currentType;
    }

    _buttonIcon() {
        let value = this.currentType;
        if (value === 'auto') value = this.llmRecommendation || 'auto';
        const o = getChartTypeOption(value) || getChartTypeOption('auto');
        return o.icon;
    }

    _updateButton() {
        if (!this.btnEl) return;
        const labelEl = this.btnEl.querySelector('.ctype-btn-label');
        const iconEl = this.btnEl.querySelector('.ctype-btn-icon');
        if (labelEl) labelEl.textContent = this._buttonLabel();
        if (iconEl) iconEl.innerHTML = iconSvg(this._buttonIcon(), 'ctype-glyph');
    }

    // ── Public API (unchanged surface) ──────────────────────────────────────

    handleTypeChange(chartType) {
        if (!this.chartTypes.some((type) => type.value === chartType)) return;
        if (chartType === this.currentType) return;
        console.log('[ChartTypeSelector] Chart type changed to:', chartType);
        this.currentType = chartType;
        if (this.onChange) this.onChange(chartType);
    }

    setType(chartType) {
        if (!CHART_TYPE_VALUES.has(chartType) || !this.chartTypes.some((type) => type.value === chartType)) return;
        this.currentType = chartType;
        this._updateButton();
        this._refreshSelectedStates();
        console.log('[ChartTypeSelector] Type set to:', chartType);
    }

    /**
     * Records the LLM's recommended type. Pass a falsy value to clear it (e.g.
     * while a fresh Auto request is in flight). Reflected in the button label.
     *
     * @param {string|null} recommendedType
     */
    setRecommendation(recommendedType) {
        this.llmRecommendation =
            (recommendedType && this.chartTypes.some((type) => type.value === recommendedType))
                ? recommendedType
                : null;
        console.log('[ChartTypeSelector] LLM recommended:', this.llmRecommendation);
        if (this.currentType === 'auto') this._updateButton();
    }

    getChartTypeName(chartType) {
        const type = getChartTypeOption(chartType);
        return type ? type.label : chartType;
    }

    reset() {
        this.currentType = 'auto';
        this.llmRecommendation = null;
        this.close();
        this._updateButton();
        this._refreshSelectedStates();
        console.log('[ChartTypeSelector] Reset');
    }

    getSelectedType() {
        return this.currentType;
    }

    show() {
        const container = document.getElementById(this.containerId);
        if (container) container.style.display = 'block';
    }

    hide() {
        this.close();
        const container = document.getElementById(this.containerId);
        if (container) container.style.display = 'none';
    }

    /** Removes portaled DOM + global listeners. Call before discarding. */
    destroy() {
        this.close();
        this._teardownMenu();
        if (this.btnEl) this.btnEl.removeEventListener('keydown', this._onKeyDown);
        this.btnEl = null;
        this.root = null;
    }
}
