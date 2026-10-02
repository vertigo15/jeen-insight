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
    runs: '/api/admin/analytics/runs',
};

const fmtNum = (v) => (window.I18n && window.I18n.formatNumber ? window.I18n.formatNumber(v) : String(v ?? '—'));
const fmtCompact = (v) => (window.I18n && window.I18n.formatCompact ? window.I18n.formatCompact(v) : String(v ?? '—'));
const fmtRelative = (v) => (window.I18n && window.I18n.formatRelative ? window.I18n.formatRelative(v) : String(v ?? ''));
const fmtDateTime = (v) => (window.I18n && window.I18n.formatDate ? window.I18n.formatDate(v, 'dateTime') : String(v ?? ''));
const fmtCalendar = (v) => (window.I18n && window.I18n.formatCalendarDate ? window.I18n.formatCalendarDate(v) : String(v ?? ''));
const fmtPct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const fmtMs = (v) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);
const fmtRating = (v) => (v == null ? '—' : Number(v).toFixed(1));
const fmtOptionalNum = (v) => (v == null ? '—' : fmtNum(v));
const fmtOptionalCompact = (v) => (v == null ? '—' : fmtCompact(v));

const LINEAGE_STAGES = [
    { id: 'setup', nodes: ['pre_graph_setup'] },
    { id: 'understand', nodes: ['context_composer', 'memory_answer_generator', 'history_search', 'fused_router', 'capability_answer'] },
    { id: 'find', nodes: ['catalog_lookup', 'dax_catalog_lookup', 'filter_planner', 'filter_grounder', 'catalog_help_answer', 'dax_entity_resolver'] },
    { id: 'write', nodes: ['prior_data_binder', 'prompt_builder', 'analysis_planner', 'analysis_guard', 'analysis_sql', 'sql_generator', 'sqlglot_validate', 'dlp_check', 'dax_query_planner', 'dax_prompt_builder', 'dax_generator', 'dax_static_validate', 'dax_repair'] },
    { id: 'run', nodes: ['execute_query', 'pbi_execute_query', 'execute_dax', 'analysis_run', 'empty_filter_result_check', 'empty_result_check', 'result_integrity_check', 'trivial_result_check'] },
    { id: 'check', nodes: ['fused_eval_analytics', 'feedback_classifier', 'dax_feedback_router'] },
    { id: 'final', nodes: ['response_formatter', 'save_to_memory', 'observability_log'] },
];

function lineageStageIndex(node) {
    const index = LINEAGE_STAGES.findIndex((stage) => stage.nodes.includes(node));
    return index === -1 ? LINEAGE_STAGES.length : index;
}

function stepMs(node) {
    const value = Number(node.duration_ms ?? node.elapsed_ms ?? node.latency_ms);
    return Number.isFinite(value) ? value : 0;
}

export function buildLineage(run, trace) {
    const attempts = [];
    let attempt = null;
    let lastIndex = -1;
    let elapsed = 0;
    let shownCount = 0;
    const ml = trace.some((node) => String(node.node || '').startsWith('analysis_'));
    const rows = run.row_count == null || run.row_count === '' ? null : Number(run.row_count);
    const hasRows = rows != null && Number.isFinite(rows) && rows > 0;
    const zeroRows = rows != null && Number.isFinite(rows) && rows === 0;
    const explicit = trace.some((node) => node.shown === true);
    const trivialAt = trace.reduce((found, node, index) => (node.node === 'trivial_result_check' ? index : found), -1);
    let estimated = false;
    trace.forEach((node, index) => {
        const stageIndex = lineageStageIndex(node.node || '');
        if (!attempt || (lastIndex >= 0 && stageIndex < lastIndex)) {
            attempt = { stages: [] };
            attempts.push(attempt);
            lastIndex = -1;
        }
        let stage = attempt.stages[attempt.stages.length - 1];
        if (!stage || stage.index !== stageIndex) {
            const known = LINEAGE_STAGES[stageIndex];
            stage = { index: stageIndex, id: known ? known.id : 'other', steps: [], ms: 0 };
            attempt.stages.push(stage);
        }
        elapsed += stepMs(node);
        const step = { node, at: elapsed };
        if (node.shown === true) {
            shownCount += 1;
            step.marker = shownCount === 1 ? 'shown' : 'updated';
        } else if (!explicit && !ml && hasRows && index === trivialAt) {
            estimated = true;
            step.marker = 'estimated';
        }
        stage.steps.push(step);
        stage.ms += stepMs(node);
        lastIndex = stageIndex;
    });
    const finalTable = !explicit && !estimated && ml && hasRows && run.outcome === 'success' && attempts.length > 0;
    if (finalTable) {
        const lastAttempt = attempts[attempts.length - 1];
        const stage = [...lastAttempt.stages].reverse().find((item) => item.id === 'final') || lastAttempt.stages[lastAttempt.stages.length - 1];
        const step = stage.steps[stage.steps.length - 1];
        if (step) step.marker = 'final';
    }
    let note = '';
    if (!explicit && !estimated && !finalTable) note = zeroRows ? 'zero' : 'none';
    return { attempts, note };
}

function lineageRouteLabel(route) {
    if (!route) return '';
    const key = `settings.analytics.runs.lineage.route.${route}`;
    const label = t(key);
    return label && label !== key ? label : route;
}

function renderLineage(run, trace) {
    if (!trace.length) return `<div class="sp-an-empty">${h('settings.analytics.runs.noTrace')}</div>`;
    const lineage = buildLineage(run, trace);
    const failed = run.outcome === 'error' || run.outcome === 'refused';
    const route = lineageRouteLabel(run.route);
    const routeLine = route
        ? `<p class="sp-an-lineage-route">${h('settings.analytics.runs.lineage.routeLabel', { route })}</p>`
        : '';
    const note = lineage.note
        ? `<p class="sp-an-lineage-note">${h(`settings.analytics.runs.lineage.none${lineage.note === 'zero' ? 'Zero' : ''}`)}</p>`
        : '';
    const attempts = lineage.attempts.map((item, attemptIndex) => {
        const heading = lineage.attempts.length > 1
            ? `<p class="sp-an-lineage-attempt">${h('settings.analytics.runs.lineage.attempt', { number: attemptIndex + 1 })}</p>`
            : '';
        const stages = item.stages.map((stage, stageIndex) => {
            const panelId = `sp-an-stage-${attemptIndex}-${stageIndex}`;
            const open = attemptIndex > 0
                || stage.steps.some((step) => step.marker)
                || (failed && attemptIndex === lineage.attempts.length - 1 && stageIndex === item.stages.length - 1)
                || (attemptIndex === 0 && stageIndex === 0);
            const steps = stage.steps.map((step, stepIndex) => {
                const node = step.node;
                const name = node.node || node.name || '—';
                const duration = node.duration_ms ?? node.elapsed_ms ?? node.latency_ms;
                const detailText = node.detail || node.message || node.error || node.route;
                const stepFailed = node.status === 'error' || Boolean(node.error);
                const marker = step.marker
                    ? `<li class="sp-an-lineage-marker">${h(`settings.analytics.runs.lineage.${step.marker === 'final' ? 'finalTable' : step.marker}`)} <bdi dir="ltr">${esc(fmtMs(step.at))}</bdi></li>`
                    : '';
                return `<li class="sp-an-run-trace-item${stepFailed ? ' is-error' : ''}">
                    <span class="sp-an-run-trace-index" aria-hidden="true"></span><span class="sp-an-sr-only">${h('settings.analytics.runs.step', { number: stepIndex + 1 })}</span>
                    <div><div class="sp-an-run-trace-main"><bdi dir="ltr" class="sp-an-mono">${esc(name)}</bdi>
                        ${node.type ? `<span class="sp-an-badge"><bdi dir="ltr">${esc(node.type)}</bdi></span>` : ''}
                        ${node.status ? `<span class="sp-an-badge${stepFailed ? ' sp-an-run-outcome-error' : ''}">${esc(node.status)}</span>` : ''}
                        ${duration != null ? `<bdi dir="ltr" class="sp-an-muted">${esc(fmtMs(duration))}</bdi>` : ''}
                    </div>${detailText ? `<p class="sp-an-plaintext">${esc(formatRunValue(detailText))}</p>` : ''}</div>
                </li>${marker}`;
            }).join('');
            return `<section class="sp-an-stage">
                <button type="button" class="sp-an-stage-toggle" aria-expanded="${open ? 'true' : 'false'}" aria-controls="${panelId}">
                    <span>${h(`settings.analytics.runs.lineage.stage.${stage.id}`)}</span>
                    <bdi dir="ltr" class="sp-an-muted">${esc(fmtMs(stage.ms))}</bdi>
                </button>
                <p class="sp-an-stage-hint">${h(`settings.analytics.runs.lineage.hint.${stage.id}`)}</p>
                <ol id="${panelId}" class="sp-an-run-trace"${open ? '' : ' hidden'}>${steps}</ol>
            </section>`;
        }).join('');
        return `${heading}${stages}`;
    }).join('');
    return `<div class="sp-an-lineage">${routeLine}${note}${attempts}</div>`;
}

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
        this._view = 'overview';
        this.runFilters = { outcome: '', connection: '' };
        this._runItems = [];
        this._runNext = null;
        this._runsLoading = false;
        this._runsLoadedFor = null;
        this._runConnections = [];
        this._selectedRunId = null;
        this._runReturnFocusId = null;
        this._overviewDays = null;
    }

    /**
     * Render the whole tab into `content`. `isCurrent` must return false once
     * the user has moved to another tab (or a newer render started).
     */
    async render({ content, isCurrent }) {
        this._content = content;
        this._isCurrent = isCurrent || (() => true);
        this._view = 'overview';
        this._selectedRunId = null;
        this._runsLoadedFor = null;
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
            <div class="sp-an-views" role="tablist" aria-label="${h('settings.analytics.views.label')}">
                <button type="button" class="sp-an-view-btn is-active" id="sp-an-tab-overview" role="tab" aria-selected="true" aria-controls="sp-an-overview-panel" data-an-view="overview">${h('settings.analytics.views.overview')}</button>
                <button type="button" class="sp-an-view-btn" id="sp-an-tab-runs" role="tab" aria-selected="false" aria-controls="sp-an-runs-panel" data-an-view="runs" tabindex="-1">${h('settings.analytics.views.runs')}</button>
            </div>
            <div id="sp-an-overview-panel" role="tabpanel" aria-labelledby="sp-an-tab-overview">
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
                </div>
            </div>
            <div id="sp-an-runs-panel" role="tabpanel" aria-labelledby="sp-an-tab-runs" hidden>
                <section class="sp-card sp-an-card sp-an-runs-card">
                    <div class="sp-an-card-head sp-an-card-head-wrap">
                        <div>
                            <h3 class="sp-an-card-title" id="sp-an-runs-title">${h('settings.analytics.runs.title')}</h3>
                            <p class="sp-an-card-sub sp-an-runs-desc">${h('settings.analytics.runs.desc')}</p>
                        </div>
                        <div class="sp-an-filters" id="sp-an-runs-filters">
                            <select class="sp-role-select" id="sp-an-run-outcome" aria-label="${h('settings.analytics.runs.filterOutcome')}">
                                <option value="">${h('settings.analytics.runs.allOutcomes')}</option>
                                ${['success', 'error', 'refused'].map((outcome) => `<option value="${outcome}"${outcome === this.runFilters.outcome ? ' selected' : ''}>${h(`settings.analytics.runs.outcome.${outcome}`)}</option>`).join('')}
                            </select>
                            <select class="sp-role-select" id="sp-an-run-connection" aria-label="${h('settings.analytics.runs.filterConnection')}">
                                <option value="">${h('settings.analytics.filter.allConnections')}</option>
                            </select>
                        </div>
                    </div>
                    <div id="sp-an-runs-list">
                        <div id="sp-an-runs-status" role="status" aria-live="polite"></div>
                        <div id="sp-an-runs-table" aria-busy="false"></div>
                        <div class="sp-an-feed-foot">
                            <button type="button" class="sp-btn-secondary sp-an-more" id="sp-an-runs-more" hidden>${h('settings.analytics.runs.loadOlder')}</button>
                            <span id="sp-an-runs-foot-status" class="sp-an-runs-foot-status" role="status" aria-live="polite"></span>
                        </div>
                    </div>
                    <div id="sp-an-run-detail" hidden aria-busy="false">
                        <div class="sp-an-run-detail-head">
                            <button type="button" class="sp-btn-ghost sp-an-run-back" id="sp-an-run-back">${h('settings.analytics.runs.back')}</button>
                            <div class="sp-an-run-detail-heading">
                                <h3 class="sp-an-run-detail-title" id="sp-an-run-detail-title" tabindex="-1">${h('settings.analytics.runs.detailTitle')}</h3>
                                <div class="sp-an-run-id-line">
                                    <bdi dir="ltr" class="sp-an-mono sp-an-muted" id="sp-an-run-detail-id"></bdi>
                                    <button type="button" class="sp-btn-ghost sp-btn-ghost-sm sp-an-copy" id="sp-an-copy-run-id">${h('settings.analytics.runs.copyId')}</button>
                                </div>
                                <div class="sp-an-run-context" id="sp-an-run-context"></div>
                            </div>
                        </div>
                        <div id="sp-an-run-detail-body" role="region" aria-labelledby="sp-an-run-detail-title"></div>
                    </div>
                </section>
            </div>`;

        this._wireToolbar();
        this._fillRunConnectionFilter();
        await this._loadAll();
    }

    dispose() {
        this._loadToken = Symbol('disposed');
        this._feedToken = Symbol('disposed');
        this._runsToken = Symbol('disposed');
        this._detailToken = Symbol('disposed');
        this._disposeChart();
    }

    // ── Wiring ────────────────────────────────────────────────────────────

    _wireToolbar() {
        const root = this._content;
        root.querySelectorAll('[data-an-view]').forEach((btn) => {
            btn.addEventListener('click', () => this._selectView(btn.dataset.anView));
            btn.addEventListener('keydown', (event) => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const tabs = Array.from(root.querySelectorAll('[data-an-view]'));
                const current = tabs.indexOf(btn);
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
                    : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
                tabs[next].focus();
                this._selectView(tabs[next].dataset.anView);
            });
        });
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
                this._runsLoadedFor = null;
                if (this._view === 'runs') this._loadRuns({ reset: true });
                else this._loadAll();
            });
        });
        const refresh = root.querySelector('#sp-an-refresh');
        if (refresh) refresh.addEventListener('click', () => {
            if (this._view === 'runs') this._loadRuns({ reset: true });
            else this._loadAll();
        });

        root.querySelectorAll('[data-filter]').forEach((sel) => {
            sel.addEventListener('change', () => {
                this.feedbackFilters[sel.dataset.filter] = sel.value;
                this._loadFeedback({ reset: true });
            });
        });
        const more = root.querySelector('#sp-an-more');
        if (more) more.addEventListener('click', () => this._loadFeedback({ reset: false }));
        const runsMore = root.querySelector('#sp-an-runs-more');
        if (runsMore) runsMore.addEventListener('click', () => this._loadRuns({ reset: false }));
        const runBack = root.querySelector('#sp-an-run-back');
        if (runBack) runBack.addEventListener('click', () => this._closeRunDetail());
        const copyRunId = root.querySelector('#sp-an-copy-run-id');
        if (copyRunId) copyRunId.addEventListener('click', () => this._copyRunText(this._selectedRunId, copyRunId));
        const runOutcome = root.querySelector('#sp-an-run-outcome');
        if (runOutcome) runOutcome.addEventListener('change', () => {
            this.runFilters.outcome = runOutcome.value;
            this._loadRuns({ reset: true });
        });
        const runConnection = root.querySelector('#sp-an-run-connection');
        if (runConnection) runConnection.addEventListener('change', () => {
            this.runFilters.connection = runConnection.value;
            this._loadRuns({ reset: true });
        });

        root.querySelectorAll('[data-export]').forEach((btn) => {
            btn.addEventListener('click', () => this._export(btn.dataset.export));
        });
        root.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && this._selectedRunId) {
                event.preventDefault();
                event.stopPropagation();
                this._closeRunDetail();
            }
        });
    }

    _selectView(view) {
        if (!['overview', 'runs'].includes(view) || view === this._view) return;
        this._view = view;
        this._content.querySelectorAll('[data-an-view]').forEach((btn) => {
            const active = btn.dataset.anView === view;
            btn.classList.toggle('is-active', active);
            btn.setAttribute('aria-selected', String(active));
            btn.tabIndex = active ? 0 : -1;
        });
        const overview = this._content.querySelector('#sp-an-overview-panel');
        const runs = this._content.querySelector('#sp-an-runs-panel');
        if (overview) overview.hidden = view !== 'overview';
        if (runs) runs.hidden = view !== 'runs';
        if (view === 'runs') {
            if (this._runsLoadedFor !== this._runsLoadKey()) this._loadRuns({ reset: true });
        } else {
            if (this._overviewDays !== this.days) this._loadAll();
            else if (this._chart) setTimeout(() => this._chart && this._chart.resize(), 0);
        }
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
        this._overviewDays = this.days;
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
        this._fillRunConnectionFilter();
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

    // ── Execution runs ────────────────────────────────────────────────────

    _runsLoadKey() {
        return `${this.days}|${this.runFilters.outcome}|${this.runFilters.connection}`;
    }

    async _loadRuns({ reset } = {}) {
        const table = this._content.querySelector('#sp-an-runs-table');
        const status = this._content.querySelector('#sp-an-runs-status');
        const footStatus = this._content.querySelector('#sp-an-runs-foot-status');
        const more = this._content.querySelector('#sp-an-runs-more');
        const list = this._content.querySelector('#sp-an-runs-list');
        const detail = this._content.querySelector('#sp-an-run-detail');
        if (!table || !status) return;
        if (this._runsLoading && !reset) return;
        const moreHadFocus = more && document.activeElement === more;
        this._runsLoading = true;
        const token = this._runsToken = Symbol('runs');
        const alive = () => this._isCurrent() && this._runsToken === token;
        table.setAttribute('aria-busy', 'true');
        if (reset) {
            this._detailToken = Symbol('detail-cancelled');
            this._selectedRunId = null;
            this._runItems = [];
            this._runNext = null;
            table.innerHTML = '';
            status.innerHTML = `<span class="sp-an-sr-only">${h('settings.analytics.runs.loading')}</span><div class="skeleton sp-an-skeleton" aria-hidden="true"></div>`;
            if (list) list.hidden = false;
            if (detail) detail.hidden = true;
        } else {
            if (footStatus) footStatus.textContent = t('settings.analytics.runs.loadingOlder');
        }
        if (more) more.disabled = true;
        try {
            const data = await this._fetch(API.runs, {
                outcome: this.runFilters.outcome,
                connection: this.runFilters.connection,
                limit: 50,
                before: reset ? null : this._runNext,
            });
            if (!alive()) return;
            const incoming = Array.isArray(data.items) ? data.items : [];
            this._runItems = reset ? incoming : this._runItems.concat(incoming);
            this._runNext = data.next_before;
            this._runsLoadedFor = this._runsLoadKey();
            this._rememberRunConnections(incoming);
            this._fillRunConnectionFilter();
            this._renderRuns();
            status.innerHTML = `<span class="sp-an-sr-only">${h(reset ? 'settings.analytics.runs.shown' : 'settings.analytics.runs.loadedOlder', { count: incoming.length })}</span>`;
            if (footStatus) footStatus.textContent = '';
            if (moreHadFocus && !this._runNext) {
                const buttons = Array.from(this._content.querySelectorAll('.sp-an-run-open'));
                const lastOpen = buttons[buttons.length - 1];
                if (lastOpen) lastOpen.focus();
            }
        } catch (e) {
            if (!alive()) return;
            const detailText = window.I18n && window.I18n.errorText
                ? window.I18n.errorText({ status: e && e.status, payload: e && e.body, error: e })
                : (e && e.message) || '';
            const target = reset ? status : footStatus;
            if (target) target.innerHTML = `<div class="sp-an-notice sp-an-notice-error sp-an-run-load-error">
                <strong>${h('settings.analytics.runs.loadFailed')}</strong>
                ${detailText ? `<p class="sp-an-plaintext">${esc(detailText)}</p>` : ''}
                <button type="button" class="sp-btn-secondary sp-an-run-retry" data-run-list-retry>${h('settings.analytics.runs.tryAgain')}</button>
            </div>`;
            if (reset) table.innerHTML = '';
            const retry = this._content.querySelector('[data-run-list-retry]');
            if (retry) retry.addEventListener('click', () => this._loadRuns({ reset }));
        } finally {
            if (this._runsToken === token) {
                this._runsLoading = false;
                table.setAttribute('aria-busy', 'false');
                if (more) {
                    more.disabled = false;
                    more.hidden = !this._runNext;
                }
            }
        }
    }

    _rememberRunConnections(items) {
        const known = new Set(this._runConnections);
        items.forEach((item) => {
            if (item && item.source_key) known.add(String(item.source_key));
        });
        if (this.runFilters.connection) known.add(this.runFilters.connection);
        this._runConnections = Array.from(known).sort((a, b) => a.localeCompare(b));
    }

    _fillRunConnectionFilter() {
        const select = this._content.querySelector('#sp-an-run-connection');
        if (!select) return;
        const keys = new Set(this._runConnections);
        for (const item of this._data.connections || []) {
            if (item && item.source_key) keys.add(String(item.source_key));
        }
        if (this.runFilters.connection) keys.add(this.runFilters.connection);
        this._runConnections = Array.from(keys).sort((a, b) => a.localeCompare(b));
        select.innerHTML = `<option value="">${h('settings.analytics.filter.allConnections')}</option>`
            + this._runConnections.map((key) => `<option value="${esc(key)}"${key === this.runFilters.connection ? ' selected' : ''}>${esc(key)}</option>`).join('');
    }

    _renderRuns() {
        const el = this._content.querySelector('#sp-an-runs-table');
        if (!el) return;
        if (!this._runItems.length) {
            const filtered = Boolean(this.runFilters.outcome || this.runFilters.connection);
            el.innerHTML = `<div class="sp-an-empty sp-an-run-empty">
                ${h(filtered ? 'settings.analytics.runs.emptyFiltered' : 'settings.analytics.runs.emptyPeriod', { days: this.days })}
                ${filtered ? `<button type="button" class="sp-btn-ghost sp-an-clear-run-filters">${h('settings.analytics.runs.clearFilters')}</button>` : ''}
            </div>`;
            const clear = el.querySelector('.sp-an-clear-run-filters');
            if (clear) clear.addEventListener('click', () => {
                this.runFilters = { outcome: '', connection: '' };
                const outcome = this._content.querySelector('#sp-an-run-outcome');
                const connection = this._content.querySelector('#sp-an-run-connection');
                if (outcome) outcome.value = '';
                if (connection) connection.value = '';
                this._loadRuns({ reset: true });
            });
            return;
        }
        const columns = [
            ['question', 'settings.analytics.runs.col.question'],
            ['status', 'settings.analytics.runs.col.status'],
            ['time', 'settings.analytics.runs.col.time'],
            ['user', 'settings.analytics.runs.col.user'],
            ['connection', 'settings.analytics.runs.col.connection'],
            ['duration', 'settings.analytics.runs.col.duration'],
            ['rows', 'settings.analytics.runs.col.rows'],
            ['tokens', 'settings.analytics.runs.col.tokens'],
        ];
        el.innerHTML = `<div class="sp-an-table-wrap sp-an-runs-table-wrap"><table class="sp-users-table sp-an-table sp-an-runs-table" aria-labelledby="sp-an-runs-title">
            <thead><tr>${columns.map(([key, label]) => `<th${['duration', 'rows', 'tokens'].includes(key) ? ' class="is-num"' : ''}>${h(label)}</th>`).join('')}</tr></thead>
            <tbody>${this._runItems.map((run) => this._runRow(run)).join('')}</tbody>
        </table></div>`;
        el.querySelectorAll('.sp-an-run-open').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.stopPropagation();
                this._openRun(button.dataset.queryId);
            });
        });
        el.querySelectorAll('[data-run-row]').forEach((row) => {
            row.addEventListener('click', (event) => {
                if (event.target.closest('button, a, select, input')) return;
                const selection = window.getSelection && String(window.getSelection());
                if (selection) return;
                this._openRun(row.dataset.queryId);
            });
        });
    }

    _runRow(run) {
        const id = String(run.query_id || '');
        const user = run.name || run.email || run.user_id || '—';
        const userSub = run.name && run.email ? `<span class="sp-an-run-user-email"><bdi dir="ltr">${esc(run.email)}</bdi></span>` : '';
        const route = run.route || '—';
        const skill = run.skill ? `<span class="sp-an-badge"><bdi dir="ltr">${esc(run.skill)}</bdi></span>` : '';
        const label = t('settings.analytics.runs.openRun', { question: run.question || id });
        const mobileMeta = [
            user,
            run.source_key,
            route !== '—' ? route : null,
            run.graph_time_ms != null ? fmtMs(run.graph_time_ms) : null,
        ].filter(Boolean).join(' · ');
        return `<tr class="sp-an-run-row" data-run-row data-query-id="${esc(id)}">
            <td data-label="${h('settings.analytics.runs.col.question')}">
                <button type="button" class="sp-an-run-open" data-query-id="${esc(id)}" aria-label="${esc(label)}">
                    <span class="sp-an-run-question sp-an-plaintext" title="${esc(run.question || '')}">${esc(run.question || '—')}</span>
                    <span class="sp-an-run-mobile-meta sp-an-plaintext">${esc(mobileMeta)}</span>
                    <span class="sp-an-run-open-icon" aria-hidden="true">›</span>
                </button>
                <span class="sp-an-run-route"><bdi dir="ltr" class="sp-an-mono">${esc(route)}</bdi>${skill}</span>
            </td>
            <td data-label="${h('settings.analytics.runs.col.status')}">${runOutcomeBadge(run.outcome)}</td>
            <td data-label="${h('settings.analytics.runs.col.time')}"><span class="sp-an-run-time"><span>${esc(fmtRelative(run.occurred_at))}</span><span>${esc(fmtDateTime(run.occurred_at))}</span></span></td>
            <td data-label="${h('settings.analytics.runs.col.user')}"><span class="sp-an-run-user"><bdi class="sp-an-plaintext">${esc(user)}</bdi>${userSub}</span></td>
            <td data-label="${h('settings.analytics.runs.col.connection')}"><bdi dir="ltr" class="sp-an-mono">${esc(run.source_key || '—')}</bdi></td>
            <td class="is-num" data-label="${h('settings.analytics.runs.col.duration')}"><bdi dir="ltr">${esc(fmtMs(run.graph_time_ms))}</bdi></td>
            <td class="is-num" data-label="${h('settings.analytics.runs.col.rows')}"><bdi dir="ltr">${esc(fmtOptionalNum(run.row_count))}</bdi></td>
            <td class="is-num" data-label="${h('settings.analytics.runs.col.tokens')}"><bdi dir="ltr">${esc(fmtOptionalCompact(run.total_tokens))}</bdi></td>
        </tr>`;
    }

    _openRun(queryId) {
        if (!queryId || this._selectedRunId) return;
        const run = this._runItems.find((item) => String(item.query_id) === String(queryId));
        if (!run) return;
        this._runReturnFocusId = String(queryId);
        this._selectedRunId = String(queryId);
        this._showRunDetailLoading(run);
        if (run.detail_available === false) {
            this._renderRunDetailUnavailable(run);
            return;
        }
        this._loadRunDetail(run);
    }

    _showRunDetailLoading(run) {
        const list = this._content.querySelector('#sp-an-runs-list');
        const detail = this._content.querySelector('#sp-an-run-detail');
        const filters = this._content.querySelector('#sp-an-runs-filters');
        if (list) list.hidden = true;
        if (filters) filters.hidden = true;
        if (!detail) return;
        detail.hidden = false;
        detail.setAttribute('aria-busy', 'true');
        this._updateRunDetailHeader(run);
        const body = this._detailBody();
        if (body) body.innerHTML = `<span class="sp-an-sr-only">${h('settings.analytics.runs.loadingDetail')}</span><div class="skeleton sp-an-skeleton" aria-hidden="true"></div>`;
        const heading = detail.querySelector('#sp-an-run-detail-title');
        if (heading) heading.focus();
    }

    async _loadRunDetail(summary) {
        const token = this._detailToken = Symbol('run-detail');
        const alive = () => this._isCurrent() && this._detailToken === token
            && this._selectedRunId === String(summary.query_id);
        this._setDetailBusy(true);
        const loadingBody = this._detailBody();
        if (loadingBody) loadingBody.innerHTML = `<span class="sp-an-sr-only">${h('settings.analytics.runs.loadingDetail')}</span><div class="skeleton sp-an-skeleton" aria-hidden="true"></div>`;
        try {
            const endpoint = `${API.runs}/${encodeURIComponent(summary.query_id)}`;
            const res = await fetch(endpoint);
            if (!res.ok) {
                const error = new Error(`HTTP ${res.status}`);
                error.status = res.status;
                try { error.body = await res.json(); } catch (_) { error.body = null; }
                throw error;
            }
            const data = await res.json();
            if (!alive()) return;
            if (data.detail_available === false) {
                this._renderRunDetailUnavailable(Object.assign({}, summary, data));
                return;
            }
            this._renderRunDetail(Object.assign({}, summary, data));
        } catch (e) {
            if (!alive()) return;
            const detailText = window.I18n && window.I18n.errorText
                ? window.I18n.errorText({ status: e && e.status, payload: e && e.body, error: e })
                : (e && e.message) || '';
            const body = this._detailBody();
            if (body) body.innerHTML = `<div class="sp-an-notice sp-an-notice-error sp-an-run-detail-error">
                <strong>${h('settings.analytics.runs.detailFailed')}</strong>
                ${detailText ? `<p class="sp-an-plaintext">${esc(detailText)}</p>` : ''}
                <button type="button" class="sp-btn-secondary" data-run-detail-retry>${h('settings.analytics.runs.tryAgain')}</button>
            </div>`;
            const retry = body && body.querySelector('[data-run-detail-retry]');
            if (retry) retry.addEventListener('click', () => this._loadRunDetail(summary));
            this._setDetailBusy(false);
        }
    }

    _renderRunDetailUnavailable(run) {
        this._updateRunDetailHeader(run);
        const body = this._detailBody();
        if (!body) return;
        body.innerHTML = `<div class="sp-an-notice">
            <strong>${h('settings.analytics.runs.detailUnavailable')}</strong>
            <p>${h('settings.analytics.runs.detailUnavailableHelp')}</p>
        </div>${this._runMetricsHtml(run)}`;
        this._setDetailBusy(false);
    }

    _closeRunDetail() {
        const focusId = this._runReturnFocusId;
        this._detailToken = Symbol('detail-cancelled');
        this._selectedRunId = null;
        const list = this._content.querySelector('#sp-an-runs-list');
        const detail = this._content.querySelector('#sp-an-run-detail');
        const filters = this._content.querySelector('#sp-an-runs-filters');
        if (list) list.hidden = false;
        if (filters) filters.hidden = false;
        if (detail) detail.hidden = true;
        const button = Array.from(this._content.querySelectorAll('.sp-an-run-open'))
            .find((candidate) => candidate.dataset.queryId === focusId);
        if (button) button.focus();
    }

    _renderRunDetail(run) {
        this._updateRunDetailHeader(run);
        const bodyTarget = this._detailBody();
        if (!bodyTarget) return;
        const value = (key) => (run.metrics && run.metrics[key]) ?? run[key];
        const error = run.error_message || run.error_type;
        const query = run.generated_query;
        const language = runQueryLanguage(run);
        const trace = Array.isArray(run.node_trace) ? run.node_trace : [];
        const traceHtml = renderLineage(run, trace);
        const answer = formatRunAnswer(run.answer);
        const failureMessage = error || (run.outcome === 'refused' ? answer : '');
        const errorNotice = ['error', 'refused'].includes(run.outcome) ? `<div class="sp-an-run-failure sp-an-run-failure-${esc(run.outcome)}">
            <strong>${h(run.outcome === 'refused' ? 'settings.analytics.runs.refusedTitle' : 'settings.analytics.runs.errorTitle')}</strong>
            ${failureMessage ? `<p class="sp-an-plaintext">${esc(failureMessage)}</p>` : ''}
        </div>` : '';
        const body = `<div class="sp-an-run-detail-grid">
            ${errorNotice ? `<div class="sp-an-run-block-wide">${errorNotice}</div>` : ''}
            <section class="sp-an-run-block sp-an-run-block-wide">
                <h4>${h('settings.analytics.runs.question')}</h4>
                <p class="sp-an-run-copy sp-an-plaintext">${esc(run.question || '—')}</p>
            </section>
            ${answer ? `<section class="sp-an-run-block sp-an-run-block-wide">
                <h4>${h('settings.analytics.runs.answer')}</h4>
                <p class="sp-an-run-copy sp-an-plaintext">${esc(answer)}</p>
            </section>` : ''}
            <section class="sp-an-run-block">
                <h4>${h('settings.analytics.runs.status')}</h4>
                <div class="sp-an-run-summary">${runOutcomeBadge(run.outcome)}
                    ${run.llm_model ? `<bdi dir="ltr" class="sp-an-mono">${esc(run.llm_model)}</bdi>` : ''}
                    ${run.route ? `<bdi dir="ltr" class="sp-an-mono">${esc(run.route)}</bdi>` : ''}
                    ${run.skill ? `<span class="sp-an-badge"><bdi dir="ltr">${esc(run.skill)}</bdi></span>` : ''}
                </div>
            </section>
            <section class="sp-an-run-block">
                <h4>${h('settings.analytics.runs.timing')}</h4>
                ${this._runMetricsHtml(run, value)}
            </section>
            ${query ? `<section class="sp-an-run-block sp-an-run-block-wide">
                <div class="sp-an-run-block-head"><h4>${h(`settings.analytics.runs.query.${language}`)}</h4>
                    <button type="button" class="sp-btn-ghost sp-btn-ghost-sm sp-an-copy" data-copy-query>${h('settings.analytics.runs.copyQuery')}</button>
                </div>
                <pre class="sp-an-run-query" dir="ltr" tabindex="0" role="region" aria-label="${h('settings.analytics.runs.queryRegion', { language: language.toUpperCase() })}"><code>${esc(formatRunValue(query))}</code></pre>
            </section>` : ''}
            <section class="sp-an-run-block sp-an-run-block-wide">
                <h4>${h('settings.analytics.runs.lineage.title')}</h4>
                ${traceHtml}
            </section>
        </div>`;
        bodyTarget.innerHTML = body;
        bodyTarget.querySelectorAll('.sp-an-stage-toggle').forEach((button) => {
            button.addEventListener('click', () => {
                const open = button.getAttribute('aria-expanded') !== 'true';
                button.setAttribute('aria-expanded', open ? 'true' : 'false');
                const panel = document.getElementById(button.getAttribute('aria-controls'));
                if (panel) panel.hidden = !open;
            });
        });
        const copyQuery = bodyTarget.querySelector('[data-copy-query]');
        if (copyQuery) copyQuery.addEventListener('click', () => this._copyRunText(query, copyQuery));
        this._setDetailBusy(false);
    }

    _detailBody() {
        return this._content.querySelector('#sp-an-run-detail-body');
    }

    _setDetailBusy(busy) {
        const detail = this._content.querySelector('#sp-an-run-detail');
        if (detail) detail.setAttribute('aria-busy', String(Boolean(busy)));
    }

    _updateRunDetailHeader(run) {
        const title = this._content.querySelector('#sp-an-run-detail-title');
        const id = this._content.querySelector('#sp-an-run-detail-id');
        const context = this._content.querySelector('#sp-an-run-context');
        if (title) title.textContent = run.question || t('settings.analytics.runs.detailTitle');
        if (id) id.textContent = run.query_id || '';
        if (!context) return;
        const user = run.name || run.email || run.user_id || '—';
        const fields = [
            [t('settings.analytics.runs.context.user'), user, false],
            [t('settings.analytics.runs.context.email'), run.email || '—', true],
            [t('settings.analytics.runs.context.connection'), run.source_key || '—', true],
            [t('settings.analytics.runs.context.time'), `${fmtDateTime(run.occurred_at)} · ${fmtRelative(run.occurred_at)}`, false],
            [t('settings.analytics.runs.context.model'), run.llm_model || '—', true],
            [t('settings.analytics.runs.context.route'), run.skill || run.route || '—', true],
        ];
        context.innerHTML = fields.map(([label, value, ltr]) => `<span><strong>${esc(label)}</strong> <bdi${ltr ? ' dir="ltr"' : ' class="sp-an-plaintext"'}>${esc(value)}</bdi></span>`).join('');
    }

    _runMetricsHtml(run, valueFn) {
        const value = valueFn || ((key) => run[key]);
        const metrics = [
            ['settings.analytics.runs.metric.total', value('graph_time_ms'), 'time'],
            ['settings.analytics.runs.metric.llm', value('llm_latency_ms'), 'time'],
            ['settings.analytics.runs.metric.execution', value('execution_time_ms'), 'time'],
            ['settings.analytics.runs.metric.rows', value('row_count'), 'number'],
            ['settings.analytics.runs.metric.tokens', value('total_tokens'), 'number'],
            ['settings.analytics.runs.metric.inputTokens', value('input_tokens'), 'number'],
        ];
        return `<div class="sp-an-run-metrics">${metrics.map(([label, metric, kind]) => `<div class="sp-an-run-metric">
            <span>${h(label)}</span>
            <strong><bdi dir="ltr">${esc(kind === 'time' ? fmtMs(metric) : fmtOptionalNum(metric))}</bdi></strong>
        </div>`).join('')}</div>`;
    }

    async _copyRunText(value, button) {
        if (!value || !button) return;
        const original = button.textContent;
        try {
            await navigator.clipboard.writeText(String(value));
            button.textContent = t('settings.analytics.runs.copied');
        } catch (_) {
            button.textContent = t('settings.analytics.runs.copyFailed');
        }
        setTimeout(() => {
            if (button.isConnected) button.textContent = original;
        }, 1600);
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

function runOutcomeBadge(outcome) {
    const value = ['success', 'error', 'refused'].includes(outcome) ? outcome : 'unknown';
    return `<span class="sp-an-badge sp-an-run-outcome sp-an-run-outcome-${esc(value)}">${h(`settings.analytics.runs.outcome.${value}`)}</span>`;
}

function runQueryLanguage(run) {
    const explicit = String(run.query_language || run.generated_query_type || '').toLowerCase();
    if (explicit === 'dax' || explicit === 'sql') return explicit;
    const query = typeof run.generated_query === 'string' ? run.generated_query.trim() : '';
    return /(?:^|\s)EVALUATE(?:\s|$)/i.test(query) || /dax/i.test(String(run.route || '')) ? 'dax' : 'sql';
}

function formatRunValue(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

function formatRunAnswer(answer) {
    if (typeof answer === 'string') return answer;
    if (!Array.isArray(answer)) return formatRunValue(answer);
    return answer
        .filter((fragment) => fragment && typeof fragment.t === 'string')
        .map((fragment) => fragment.t)
        .join('');
}
