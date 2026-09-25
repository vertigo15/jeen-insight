/**
 * Settings › Analytics (admin only)
 *
 * Usage, users, connections, feedback and ML-skill reporting read from
 * `/api/admin/analytics/*` (the durable usage ledger, migration 036). Pure
 * rendering + fetching for one Settings tab; the SettingsPage owns the tab
 * lifecycle and passes an `isCurrent()` guard so a slow response can never
 * paint over another tab.
 *
 * Metric definitions are shown as tooltips (see `KPI_HINTS`), mirroring the
 * server (`src/analytics/usage_ledger.py`).
 *
 * @module analyticsSection
 */

import { loadECharts } from '../vendor-loaders/echarts.js';

const t = (key, args) => (window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
const h = (key, args) => (window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : esc(t(key, args)));

export function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

const RANGES = [7, 30, 90];
// Literal endpoints (the Flask BFF proxies /api/admin/analytics/<path>; the
// proxy-coverage test scans these strings).
const API = {
    overview: '/api/admin/analytics/overview',
    timeseries: '/api/admin/analytics/timeseries',
    topUsers: '/api/admin/analytics/top-users',
    topConnections: '/api/admin/analytics/top-connections',
    feedback: '/api/admin/analytics/feedback',
    analysis: '/api/admin/analytics/analysis',
    errors: '/api/admin/analytics/errors',
};

const fmtNum = (v) => (window.I18n && window.I18n.formatNumber ? window.I18n.formatNumber(v) : String(v ?? '—'));
const fmtCompact = (v) => (window.I18n && window.I18n.formatCompact ? window.I18n.formatCompact(v) : String(v ?? '—'));
const fmtRelative = (v) => (window.I18n && window.I18n.formatRelative ? window.I18n.formatRelative(v) : String(v ?? ''));
const fmtDateTime = (v) => (window.I18n && window.I18n.formatDate ? window.I18n.formatDate(v, 'dateTime') : String(v ?? ''));
const fmtCalendar = (v) => (window.I18n && window.I18n.formatCalendarDate ? window.I18n.formatCalendarDate(v) : String(v ?? ''));
const fmtPct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const fmtMs = (v) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);
const fmtRating = (v) => (v == null ? '—' : Number(v).toFixed(1));

/**
 * RFC 4180 CSV of the given rows. Cells that a spreadsheet would evaluate as a
 * formula (`=`, `+`, `-`, `@`, tab, CR) are prefixed with a quote so an
 * exported comment can never execute when opened in Excel/Sheets.
 */
export function toCsv(columns, rows) {
    const cell = (value) => {
        let s = value == null ? '' : String(value);
        if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
        return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [columns.map((c) => cell(c.label)).join(',')];
    for (const row of rows) lines.push(columns.map((c) => cell(typeof c.value === 'function' ? c.value(row) : row[c.key])).join(','));
    return `\uFEFF${lines.join('\r\n')}`;
}

function downloadCsv(filename, csv) {
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const KPI_HINTS = {
    dau: 'settings.analytics.hint.activeUser',
    wau: 'settings.analytics.hint.activeUser',
    mau: 'settings.analytics.hint.activeUser',
    activeUsers: 'settings.analytics.hint.activeUser',
    questions: 'settings.analytics.hint.question',
    successRate: 'settings.analytics.hint.successRate',
    thumbsUp: 'settings.analytics.hint.thumbs',
    thumbsDown: 'settings.analytics.hint.thumbs',
    logins: 'settings.analytics.hint.logins',
};

export class AnalyticsSection {
    constructor() {
        this.days = 30;
        this.feedbackFilters = { thumb: '', type: '', connection: '' };
        this._content = null;
        this._isCurrent = () => true;
        this._chart = null;
        this._themeObserver = null;
        this._data = {};
        this._feedbackItems = [];
        this._feedbackNext = null;
        this._feedbackLoading = false;
        this._chartPoints = null;
    }

    /**
     * Render the whole tab into `content`. `isCurrent` must return false once
     * the user has moved to another tab (or a newer render started).
     */
    async render({ content, isCurrent }) {
        this._content = content;
        this._isCurrent = isCurrent || (() => true);
        this._disposeChart();

        content.innerHTML = `
            <div class="sp-section-header sp-an-header">
                <div>
                    <h2 class="sp-section-title">${h('settings.analytics.title')}</h2>
                    <p class="sp-section-desc">${h('settings.analytics.desc')}</p>
                </div>
                <div class="sp-an-toolbar">
                    <div class="sp-an-range" role="group" aria-label="${h('settings.analytics.rangeLabel')}">
                        ${RANGES.map((d) => `<button type="button" class="sp-an-range-btn${d === this.days ? ' is-active' : ''}" data-days="${d}" aria-pressed="${d === this.days}">${h(`settings.analytics.range.${d}`)}</button>`).join('')}
                    </div>
                    <button type="button" class="sp-btn-ghost sp-btn-ghost-sm" id="sp-an-refresh">${h('settings.analytics.refresh')}</button>
                </div>
            </div>
            <div id="sp-an-status"></div>
            <div id="sp-an-body" class="sp-an-body" hidden>
                <section class="sp-an-kpis" id="sp-an-kpis" aria-label="${h('settings.analytics.kpisLabel')}"></section>
                <section class="sp-card sp-an-card">
                    <div class="sp-an-card-head">
                        <h3 class="sp-an-card-title">${h('settings.analytics.activity')}</h3>
                        <span class="sp-an-card-sub">${h('settings.analytics.activityDesc')}</span>
                    </div>
                    <div id="sp-an-chart" class="sp-an-chart" role="img" aria-label="${h('settings.analytics.activity')}"></div>
                </section>
                <div class="sp-an-grid-2">
                    <section class="sp-card sp-an-card">
                        <div class="sp-an-card-head">
                            <h3 class="sp-an-card-title">${h('settings.analytics.topUsers')}</h3>
                            <button type="button" class="sp-btn-ghost sp-btn-ghost-sm" data-export="users" title="${h('settings.analytics.exportNote')}">${h('settings.analytics.exportCsv')}</button>
                        </div>
                        <div id="sp-an-users"></div>
                    </section>
                    <section class="sp-card sp-an-card">
                        <div class="sp-an-card-head">
                            <h3 class="sp-an-card-title">${h('settings.analytics.topConnections')}</h3>
                            <button type="button" class="sp-btn-ghost sp-btn-ghost-sm" data-export="connections" title="${h('settings.analytics.exportNote')}">${h('settings.analytics.exportCsv')}</button>
                        </div>
                        <div id="sp-an-connections"></div>
                    </section>
                </div>
                <div class="sp-an-grid-2">
                    <section class="sp-card sp-an-card">
                        <div class="sp-an-card-head"><h3 class="sp-an-card-title">${h('settings.analytics.skills')}</h3></div>
                        <div id="sp-an-skills"></div>
                    </section>
                    <section class="sp-card sp-an-card">
                        <div class="sp-an-card-head"><h3 class="sp-an-card-title">${h('settings.analytics.errors')}</h3></div>
                        <div id="sp-an-errors"></div>
                    </section>
                </div>
                <section class="sp-card sp-an-card" id="sp-an-feedback-card">
                    <div class="sp-an-card-head sp-an-card-head-wrap">
                        <h3 class="sp-an-card-title">${h('settings.analytics.feedback')}</h3>
                        <div class="sp-an-filters">
                            <select class="sp-role-select" data-filter="thumb" aria-label="${h('settings.analytics.filter.thumb')}">
                                <option value="">${h('settings.analytics.filter.allThumbs')}</option>
                                <option value="thumbs_up">${h('settings.analytics.thumb.thumbs_up')}</option>
                                <option value="thumbs_down">${h('settings.analytics.thumb.thumbs_down')}</option>
                                <option value="cleared">${h('settings.analytics.thumb.cleared')}</option>
                            </select>
                            <select class="sp-role-select" data-filter="type" aria-label="${h('settings.analytics.filter.type')}">
                                <option value="">${h('settings.analytics.filter.allTypes')}</option>
                                ${['general', 'report_bug', 'ui_bug', 'other'].map((k) => `<option value="${k}">${h(`conversation.feedback.type.${k}`)}</option>`).join('')}
                            </select>
                            <select class="sp-role-select" data-filter="connection" aria-label="${h('settings.analytics.filter.connection')}">
                                <option value="">${h('settings.analytics.filter.allConnections')}</option>
                            </select>
                            <button type="button" class="sp-btn-ghost sp-btn-ghost-sm" data-export="feedback" title="${h('settings.analytics.exportNote')}">${h('settings.analytics.exportCsv')}</button>
                        </div>
                    </div>
                    <div id="sp-an-feedback"></div>
                    <div class="sp-an-feed-foot">
                        <button type="button" class="sp-btn-secondary sp-an-more" id="sp-an-more" hidden>${h('settings.analytics.loadMore')}</button>
                    </div>
                </section>
            </div>`;

        this._wireToolbar();
        await this._loadAll();
    }

    dispose() {
        this._disposeChart();
    }

    // ── Wiring ────────────────────────────────────────────────────────────

    _wireToolbar() {
        const root = this._content;
        root.querySelectorAll('.sp-an-range-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                const days = Number(btn.dataset.days);
                if (!RANGES.includes(days) || days === this.days) return;
                this.days = days;
                root.querySelectorAll('.sp-an-range-btn').forEach((b) => {
                    const active = Number(b.dataset.days) === days;
                    b.classList.toggle('is-active', active);
                    b.setAttribute('aria-pressed', String(active));
                });
                this._loadAll();
            });
        });
        const refresh = root.querySelector('#sp-an-refresh');
        if (refresh) refresh.addEventListener('click', () => this._loadAll());

        root.querySelectorAll('[data-filter]').forEach((sel) => {
            sel.addEventListener('change', () => {
                this.feedbackFilters[sel.dataset.filter] = sel.value;
                this._loadFeedback({ reset: true });
            });
        });
        const more = root.querySelector('#sp-an-more');
        if (more) more.addEventListener('click', () => this._loadFeedback({ reset: false }));

        root.querySelectorAll('[data-export]').forEach((btn) => {
            btn.addEventListener('click', () => this._export(btn.dataset.export));
        });
    }

    // ── Loading ───────────────────────────────────────────────────────────

    async _fetch(endpoint, params = {}) {
        const qs = new URLSearchParams({ days: String(this.days) });
        for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) qs.set(k, String(v));
        const res = await fetch(`${endpoint}?${qs.toString()}`);
        if (!res.ok) {
            const err = new Error(`HTTP ${res.status}`);
            err.status = res.status;
            try { err.body = await res.json(); } catch (_) { err.body = null; }
            throw err;
        }
        return res.json();
    }

    async _loadAll() {
        const status = this._content.querySelector('#sp-an-status');
        const body = this._content.querySelector('#sp-an-body');
        if (status) status.innerHTML = `<div class="skeleton sp-an-skeleton"></div><div class="skeleton sp-an-skeleton"></div>`;
        if (body) body.hidden = true;
        const token = this._loadToken = Symbol('load');
        const alive = () => this._isCurrent() && this._loadToken === token;

        let overview;
        try {
            overview = await this._fetch(API.overview);
        } catch (e) {
            if (!alive()) return;
            this._renderStatusError(e);
            return;
        }
        if (!alive()) return;
        this._data.overview = overview;
        if (status) status.innerHTML = '';
        if (body) body.hidden = false;
        this._renderKpis(overview);

        const results = await Promise.allSettled([
            this._fetch(API.timeseries),
            this._fetch(API.topUsers, { limit: 20 }),
            this._fetch(API.topConnections, { limit: 20 }),
            this._fetch(API.analysis),
            this._fetch(API.errors, { limit: 20 }),
        ]);
        if (!alive()) return;
        const [ts, users, conns, skills, errors] = results.map((r) => (r.status === 'fulfilled' ? r.value : null));
        this._data.users = users ? users.items : [];
        this._data.connections = conns ? conns.items : [];
        this._renderChart(ts ? ts.points : null, alive);
        this._renderUsers(this._data.users, !users);
        this._renderConnections(this._data.connections, !conns);
        this._renderSkills(skills ? skills.items : [], !skills);
        this._renderErrors(errors, !errors);
        this._fillConnectionFilter(this._data.connections);
        await this._loadFeedback({ reset: true, alive });
    }

    _renderStatusError(e) {
        const status = this._content.querySelector('#sp-an-status');
        if (!status) return;
        if (e && e.status === 503) {
            status.innerHTML = `<div class="sp-card sp-an-notice"><strong>${h('settings.analytics.unavailableTitle')}</strong><p>${h('settings.analytics.unavailable')}</p></div>`;
            return;
        }
        const detail = window.I18n && window.I18n.errorText
            ? window.I18n.errorText({ status: e && e.status, body: e && e.body, message: e && e.message })
            : (e && e.message) || '';
        status.innerHTML = `<div class="sp-card sp-an-notice sp-an-notice-error"><strong>${h('settings.analytics.loadFailed')}</strong> <bdi dir="ltr">${esc(detail)}</bdi></div>`;
    }

    // ── KPIs ──────────────────────────────────────────────────────────────

    _renderKpis(ov) {
        const el = this._content.querySelector('#sp-an-kpis');
        if (!el) return;
        const cur = ov.current || {}, prev = ov.previous || {};
        const empty = !cur.questions && !cur.text_only_turns && !cur.logins && !cur.feedback_events && !ov.mau;
        const cards = [
            { id: 'dau', label: 'settings.analytics.kpi.dau', value: fmtNum(ov.dau) },
            { id: 'wau', label: 'settings.analytics.kpi.wau', value: fmtNum(ov.wau) },
            { id: 'mau', label: 'settings.analytics.kpi.mau', value: fmtNum(ov.mau) },
            { id: 'questions', label: 'settings.analytics.kpi.questions', value: fmtNum(cur.questions), delta: delta(cur.questions, prev.questions) },
            { id: 'activeUsers', label: 'settings.analytics.kpi.activeUsers', value: fmtNum(cur.active_users), delta: delta(cur.active_users, prev.active_users) },
            { id: 'activeConnections', label: 'settings.analytics.kpi.activeConnections', value: fmtNum(cur.active_connections), delta: delta(cur.active_connections, prev.active_connections) },
            { id: 'successRate', label: 'settings.analytics.kpi.successRate', value: fmtPct(cur.success_rate), delta: deltaPts(cur.success_rate, prev.success_rate) },
            { id: 'avgLatency', label: 'settings.analytics.kpi.avgLatency', value: fmtMs(cur.avg_graph_time_ms), delta: delta(cur.avg_graph_time_ms, prev.avg_graph_time_ms, { lowerIsBetter: true }) },
            { id: 'tokens', label: 'settings.analytics.kpi.tokens', value: fmtCompact(cur.total_tokens), delta: delta(cur.total_tokens, prev.total_tokens, { neutral: true }) },
            { id: 'thumbsUp', label: 'settings.analytics.kpi.thumbsUp', value: fmtNum(cur.thumbs_up), delta: delta(cur.thumbs_up, prev.thumbs_up) },
            { id: 'thumbsDown', label: 'settings.analytics.kpi.thumbsDown', value: fmtNum(cur.thumbs_down), delta: delta(cur.thumbs_down, prev.thumbs_down, { lowerIsBetter: true }) },
            { id: 'avgRating', label: 'settings.analytics.kpi.avgRating', value: fmtRating(cur.avg_rating), delta: delta(cur.avg_rating, prev.avg_rating) },
            { id: 'comments', label: 'settings.analytics.kpi.comments', value: fmtNum(cur.comments), delta: delta(cur.comments, prev.comments, { neutral: true }) },
            { id: 'logins', label: 'settings.analytics.kpi.logins', value: fmtNum(cur.logins), delta: delta(cur.logins, prev.logins) },
            { id: 'refused', label: 'settings.analytics.kpi.refused', value: fmtNum(cur.refused), delta: delta(cur.refused, prev.refused, { lowerIsBetter: true }) },
            { id: 'textOnly', label: 'settings.analytics.kpi.textOnly', value: fmtNum(cur.text_only_turns), delta: delta(cur.text_only_turns, prev.text_only_turns, { neutral: true }) },
        ];
        el.innerHTML = (empty ? `<div class="sp-an-empty sp-an-empty-wide">${h('settings.analytics.empty')}</div>` : '') + cards.map((c) => {
            const hint = KPI_HINTS[c.id] ? ` title="${h(KPI_HINTS[c.id])}"` : '';
            const d = c.delta;
            const deltaHtml = d
                ? `<span class="sp-an-kpi-delta ${d.cls}" title="${h('settings.analytics.vsPrevious', { days: this.days })}"><bdi dir="ltr">${esc(d.text)}</bdi></span>`
                : '';
            return `<div class="sp-an-kpi" data-kpi="${c.id}"${hint}>
                <div class="sp-an-kpi-label">${h(c.label)}</div>
                <div class="sp-an-kpi-value"><bdi dir="ltr">${esc(c.value)}</bdi>${deltaHtml}</div>
            </div>`;
        }).join('');

        function delta(curV, prevV, opts = {}) {
            if (curV == null || prevV == null) return null;
            const a = Number(curV), b = Number(prevV);
            if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
            if (a === b) return null;
            const pct = b === 0 ? null : (a - b) / Math.abs(b);
            const text = pct == null ? (a > b ? '+∞' : '') : `${pct > 0 ? '+' : ''}${Math.round(pct * 100)}%`;
            if (!text) return null;
            const up = a > b;
            const good = opts.neutral ? null : (opts.lowerIsBetter ? !up : up);
            return { text, cls: good == null ? 'is-neutral' : good ? 'is-good' : 'is-bad' };
        }
        function deltaPts(curV, prevV) {
            if (curV == null || prevV == null) return null;
            const pts = Math.round((curV - prevV) * 100);
            if (!pts) return null;
            return { text: `${pts > 0 ? '+' : ''}${pts} pt`, cls: pts > 0 ? 'is-good' : 'is-bad' };
        }
    }

    // ── Chart ─────────────────────────────────────────────────────────────

    async _renderChart(points, alive) {
        const el = this._content.querySelector('#sp-an-chart');
        if (!el) return;
        if (!points) { el.innerHTML = `<div class="sp-an-empty">${h('settings.analytics.loadFailed')}</div>`; return; }
        this._chartPoints = points;
        let echarts;
        try { echarts = await loadECharts(); }
        catch (_) { el.innerHTML = `<div class="sp-an-empty">${h('charts.errors.libraryLoadFailed')}</div>`; return; }
        if (alive && !alive()) return;
        if (!el.isConnected) return;
        this._disposeChart();
        this._chart = echarts.init(el, null, { renderer: 'canvas' });
        this._chart.setOption(this._chartOption(points));
        this._onResize = () => this._chart && this._chart.resize();
        window.addEventListener('resize', this._onResize);
        this._themeObserver = new MutationObserver(() => {
            if (this._chart && this._chartPoints) this._chart.setOption(this._chartOption(this._chartPoints), true);
        });
        this._themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }

    _chartOption(points) {
        const css = getComputedStyle(document.documentElement);
        const textColor = css.getPropertyValue('--color-muted').trim() || '#888';
        const gridColor = css.getPropertyValue('--color-border').trim() || '#e5e7eb';
        const rtl = !!(window.I18n && window.I18n.isRtl);
        const days = points.map((p) => p.day);
        const series = (key, name, type, extra = {}) => ({
            name: t(name), type, data: points.map((p) => p[key] ?? 0), smooth: false, showSymbol: false, ...extra,
        });
        return {
            animationDuration: 300,
            textStyle: { color: textColor, fontFamily: 'inherit' },
            tooltip: { trigger: 'axis', confine: true, textStyle: { align: rtl ? 'right' : 'left' } },
            legend: { top: 0, [rtl ? 'right' : 'left']: 0, textStyle: { color: textColor }, icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
            grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
            xAxis: {
                type: 'category', data: days, inverse: rtl, boundaryGap: true,
                axisLine: { lineStyle: { color: gridColor } }, axisTick: { show: false },
                axisLabel: { color: textColor, formatter: (v) => fmtCalendar(v).replace(/^\d{4}-/, ''), hideOverlap: true },
            },
            yAxis: [
                { type: 'value', minInterval: 1, position: rtl ? 'right' : 'left', splitLine: { lineStyle: { color: gridColor, type: 'dashed' } }, axisLabel: { color: textColor } },
                { type: 'value', minInterval: 1, position: rtl ? 'left' : 'right', splitLine: { show: false }, axisLabel: { color: textColor } },
            ],
            series: [
                series('questions', 'settings.analytics.series.questions', 'line', { lineStyle: { width: 2 } }),
                series('active_users', 'settings.analytics.series.activeUsers', 'line', { lineStyle: { width: 2 } }),
                series('errors', 'settings.analytics.series.errors', 'line', { lineStyle: { width: 1.5, type: 'dashed' } }),
                series('thumbs_up', 'settings.analytics.series.thumbsUp', 'bar', { yAxisIndex: 1, stack: 'thumbs', barMaxWidth: 14 }),
                series('thumbs_down', 'settings.analytics.series.thumbsDown', 'bar', { yAxisIndex: 1, stack: 'thumbs', barMaxWidth: 14 }),
                series('logins', 'settings.analytics.series.logins', 'line', { yAxisIndex: 1, lineStyle: { width: 1, type: 'dotted' } }),
            ],
        };
    }

    _disposeChart() {
        if (this._chart) { try { this._chart.dispose(); } catch (_) { /* already gone */ } this._chart = null; }
        if (this._onResize) { window.removeEventListener('resize', this._onResize); this._onResize = null; }
        if (this._themeObserver) { this._themeObserver.disconnect(); this._themeObserver = null; }
    }

    // ── Tables ────────────────────────────────────────────────────────────

    _table(columns, rows, { emptyKey, failed } = {}) {
        if (failed) return `<div class="sp-an-empty">${h('settings.analytics.loadFailed')}</div>`;
        if (!rows.length) return `<div class="sp-an-empty">${h(emptyKey || 'settings.analytics.noData')}</div>`;
        return `<div class="sp-an-table-wrap"><table class="sp-users-table sp-an-table">
            <thead><tr>${columns.map((c) => `<th${c.num ? ' class="is-num"' : ''}>${h(c.label)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map((r) => `<tr>${columns.map((c) => `<td${c.num ? ' class="is-num"' : ''}>${c.render(r)}</td>`).join('')}</tr>`).join('')}</tbody>
        </table></div>`;
    }

    _renderUsers(items, failed) {
        const el = this._content.querySelector('#sp-an-users');
        if (!el) return;
        el.innerHTML = this._table([
            { label: 'settings.analytics.col.user', render: (u) => userCell(u) },
            { label: 'settings.analytics.col.questions', num: true, render: (u) => esc(fmtNum(u.questions)) + (u.analyses ? ` <span class="sp-an-muted" title="${h('settings.analytics.col.analyses')}">(+${esc(fmtNum(u.analyses))})</span>` : '') },
            { label: 'settings.analytics.col.successRate', num: true, render: (u) => esc(fmtPct(u.success_rate)) },
            { label: 'settings.analytics.col.thumbs', num: true, render: (u) => thumbsCell(u) },
            { label: 'settings.analytics.col.rating', num: true, render: (u) => esc(fmtRating(u.avg_rating)) },
            { label: 'settings.analytics.col.lastActive', render: (u) => `<span title="${esc(fmtDateTime(u.last_active))}">${esc(fmtRelative(u.last_active))}</span>` },
        ], items, { failed, emptyKey: 'settings.analytics.noData' });
    }

    _renderConnections(items, failed) {
        const el = this._content.querySelector('#sp-an-connections');
        if (!el) return;
        el.innerHTML = this._table([
            { label: 'settings.analytics.col.connection', render: (c) => `<bdi dir="ltr" class="sp-an-mono">${esc(c.source_key)}</bdi>` },
            { label: 'settings.analytics.col.questions', num: true, render: (c) => esc(fmtNum(c.questions)) },
            { label: 'settings.analytics.col.users', num: true, render: (c) => esc(fmtNum(c.distinct_users)) },
            { label: 'settings.analytics.col.successRate', num: true, render: (c) => esc(fmtPct(c.success_rate)) },
            { label: 'settings.analytics.col.latency', num: true, render: (c) => esc(fmtMs(c.avg_graph_time_ms)) },
            { label: 'settings.analytics.col.thumbsDownRate', num: true, render: (c) => esc(fmtPct(c.thumbs_down_rate)) },
        ], items, { failed, emptyKey: 'settings.analytics.noData' });
    }

    _renderSkills(items, failed) {
        const el = this._content.querySelector('#sp-an-skills');
        if (!el) return;
        el.innerHTML = this._table([
            { label: 'settings.analytics.col.skill', render: (s) => `<bdi dir="ltr" class="sp-an-mono">${esc(s.skill)}</bdi>` },
            { label: 'settings.analytics.col.runs', num: true, render: (s) => esc(fmtNum(s.runs)) },
            { label: 'settings.analytics.col.ok', num: true, render: (s) => esc(fmtNum(s.ok)) },
            { label: 'settings.analytics.col.guardFailed', num: true, render: (s) => esc(fmtNum(s.guard_failed)) },
            { label: 'settings.analytics.col.errors', num: true, render: (s) => esc(fmtNum(s.errors)) },
            { label: 'settings.analytics.col.thumbs', num: true, render: (s) => thumbsCell(s) },
        ], items, { failed, emptyKey: 'settings.analytics.noSkills' });
    }

    _renderErrors(data, failed) {
        const el = this._content.querySelector('#sp-an-errors');
        if (!el) return;
        const byType = data ? data.by_type || [] : [];
        const questions = data ? data.top_failing_questions || [] : [];
        el.innerHTML = this._table([
            { label: 'settings.analytics.col.errorType', render: (e) => `<bdi dir="ltr" class="sp-an-mono">${esc(e.error_type)}</bdi>` },
            { label: 'settings.analytics.col.connection', render: (e) => `<bdi dir="ltr" class="sp-an-mono">${esc(e.source_key || '—')}</bdi>` },
            { label: 'settings.analytics.col.failures', num: true, render: (e) => esc(fmtNum(e.failures)) },
        ], byType, { failed, emptyKey: 'settings.analytics.noErrors' })
        + `<h4 class="sp-an-subtitle">${h('settings.analytics.failingQuestions')}</h4>`
        + this._table([
            { label: 'settings.analytics.col.question', render: (q) => `<bdi class="sp-an-question">${esc(q.question)}</bdi>` },
            { label: 'settings.analytics.col.failures', num: true, render: (q) => esc(fmtNum(q.failures)) },
            { label: 'settings.analytics.col.users', num: true, render: (q) => esc(fmtNum(q.distinct_users)) },
            { label: 'settings.analytics.col.lastSeen', render: (q) => `<span title="${esc(fmtDateTime(q.last_seen))}">${esc(fmtRelative(q.last_seen))}</span>` },
        ], questions, { failed, emptyKey: 'settings.analytics.noFailingQuestions' });
    }

    // ── Feedback feed ─────────────────────────────────────────────────────

    _fillConnectionFilter(connections) {
        const sel = this._content.querySelector('[data-filter="connection"]');
        if (!sel) return;
        const current = this.feedbackFilters.connection;
        const keys = (connections || []).map((c) => c.source_key).filter(Boolean);
        if (current && !keys.includes(current)) keys.push(current);
        sel.innerHTML = `<option value="">${h('settings.analytics.filter.allConnections')}</option>`
            + keys.map((k) => `<option value="${esc(k)}"${k === current ? ' selected' : ''}>${esc(k)}</option>`).join('');
    }

    async _loadFeedback({ reset, alive } = {}) {
        const el = this._content.querySelector('#sp-an-feedback');
        const more = this._content.querySelector('#sp-an-more');
        if (!el) return;
        // "Load more" must not double-fire; a reset (filter/range change)
        // always wins and the stale response is dropped by the alive check.
        if (this._feedbackLoading && !reset) return;
        this._feedbackLoading = true;
        const feedToken = this._feedToken = Symbol('feed');
        const isAlive = () => (alive ? alive() : this._isCurrent()) && this._feedToken === feedToken;
        if (reset) {
            this._feedbackItems = [];
            this._feedbackNext = null;
            el.innerHTML = `<div class="skeleton sp-an-skeleton"></div>`;
        }
        if (more) more.disabled = true;
        try {
            const data = await this._fetch(API.feedback, {
                thumb: this.feedbackFilters.thumb,
                type: this.feedbackFilters.type,
                connection: this.feedbackFilters.connection,
                limit: 50,
                before: reset ? null : this._feedbackNext,
            });
            if (!isAlive()) return;
            this._feedbackItems = reset ? data.items : this._feedbackItems.concat(data.items);
            this._feedbackNext = data.next_before;
            this._renderFeedback();
        } catch (e) {
            if (!isAlive()) return;
            el.innerHTML = `<div class="sp-an-empty">${h('settings.analytics.loadFailed')}</div>`;
        } finally {
            if (this._feedToken === feedToken) {
                this._feedbackLoading = false;
                if (more) { more.disabled = false; more.hidden = !this._feedbackNext; }
            }
        }
    }

    _renderFeedback() {
        const el = this._content.querySelector('#sp-an-feedback');
        if (!el) return;
        const items = this._feedbackItems;
        if (!items.length) { el.innerHTML = `<div class="sp-an-empty">${h('settings.analytics.noFeedback')}</div>`; return; }
        el.innerHTML = `<ul class="sp-an-feed">${items.map((f) => `
            <li class="sp-an-feed-item" data-id="${f.id}">
                <div class="sp-an-feed-meta">
                    ${thumbBadge(f.thumb)}
                    ${f.rating ? `<span class="sp-an-badge sp-an-badge-rating" title="${h('conversation.feedback.rate')}">${'★'.repeat(f.rating)}<span class="sp-an-muted">${'★'.repeat(5 - f.rating)}</span></span>` : ''}
                    ${f.feedback_type ? `<span class="sp-an-badge">${h(`conversation.feedback.type.${f.feedback_type}`)}</span>` : ''}
                    <span class="sp-an-feed-user"><bdi>${esc(f.name || f.email || f.user_id)}</bdi></span>
                    ${f.source_key ? `<bdi dir="ltr" class="sp-an-mono sp-an-muted">${esc(f.source_key)}</bdi>` : ''}
                    <span class="sp-an-muted" title="${esc(fmtDateTime(f.occurred_at))}">${esc(fmtRelative(f.occurred_at))}</span>
                </div>
                ${f.message ? `<p class="sp-an-feed-message"><bdi>${esc(f.message)}</bdi></p>` : ''}
                ${f.question ? `<p class="sp-an-feed-question"><span class="sp-an-muted">${h('settings.analytics.askedLabel')}</span> <bdi>${esc(f.question)}</bdi></p>` : ''}
            </li>`).join('')}</ul>`;
    }

    // ── Export (visible rows only) ────────────────────────────────────────

    _export(kind) {
        const stamp = new Date().toISOString().slice(0, 10);
        if (kind === 'users') {
            downloadCsv(`jeen-analytics-users-${this.days}d-${stamp}.csv`, toCsv([
                { label: 'user_id', key: 'user_id' }, { label: 'name', key: 'name' }, { label: 'email', key: 'email' },
                { label: 'questions', key: 'questions' }, { label: 'analyses', key: 'analyses' },
                { label: 'success_rate', key: 'success_rate' }, { label: 'thumbs_up', key: 'thumbs_up' },
                { label: 'thumbs_down', key: 'thumbs_down' }, { label: 'avg_rating', key: 'avg_rating' },
                { label: 'last_active', key: 'last_active' },
            ], this._data.users || []));
        } else if (kind === 'connections') {
            downloadCsv(`jeen-analytics-connections-${this.days}d-${stamp}.csv`, toCsv([
                { label: 'source_key', key: 'source_key' }, { label: 'questions', key: 'questions' },
                { label: 'distinct_users', key: 'distinct_users' }, { label: 'success_rate', key: 'success_rate' },
                { label: 'avg_graph_time_ms', key: 'avg_graph_time_ms' }, { label: 'thumbs_up', key: 'thumbs_up' },
                { label: 'thumbs_down', key: 'thumbs_down' }, { label: 'thumbs_down_rate', key: 'thumbs_down_rate' },
                { label: 'last_used', key: 'last_used' },
            ], this._data.connections || []));
        } else if (kind === 'feedback') {
            downloadCsv(`jeen-analytics-feedback-${this.days}d-${stamp}.csv`, toCsv([
                { label: 'occurred_at', key: 'occurred_at' }, { label: 'user', value: (f) => f.name || f.email || f.user_id },
                { label: 'email', key: 'email' }, { label: 'connection', key: 'source_key' },
                { label: 'thumb', key: 'thumb' }, { label: 'rating', key: 'rating' }, { label: 'type', key: 'feedback_type' },
                { label: 'message', key: 'message' }, { label: 'question', key: 'question' }, { label: 'query_id', key: 'query_id' },
            ], this._feedbackItems));
        }
    }
}

// ── Cell helpers ──────────────────────────────────────────────────────────

function userCell(u) {
    const name = u.name || u.email || u.user_id;
    const sub = u.name && u.email ? `<div class="sp-user-email"><bdi dir="ltr">${esc(u.email)}</bdi></div>` : '';
    const role = u.role ? `<span class="sp-an-badge sp-an-badge-role">${h(`settings.users.roles.${u.role}`)}</span>` : '';
    return `<div class="sp-user-cell"><div><div class="sp-user-name"><bdi>${esc(name)}</bdi> ${role}</div>${sub}</div></div>`;
}

function thumbsCell(r) {
    return `<span class="sp-an-thumbs"><span class="sp-an-thumb-up" title="${h('settings.analytics.thumb.thumbs_up')}">▲ <bdi dir="ltr">${esc(fmtNum(r.thumbs_up))}</bdi></span> <span class="sp-an-thumb-down" title="${h('settings.analytics.thumb.thumbs_down')}">▼ <bdi dir="ltr">${esc(fmtNum(r.thumbs_down))}</bdi></span></span>`;
}

function thumbBadge(thumb) {
    if (!thumb) return '';
    const cls = thumb === 'thumbs_up' ? 'sp-an-badge-up' : thumb === 'thumbs_down' ? 'sp-an-badge-down' : 'sp-an-badge-cleared';
    return `<span class="sp-an-badge ${cls}">${h(`settings.analytics.thumb.${thumb}`)}</span>`;
}
