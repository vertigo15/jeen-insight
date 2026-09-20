/*
 * Fake backend for the ML e2e harness.
 *
 * Replaces window.fetch so the REAL frontend modules (workspaceController.js,
 * analysisPanel.js) run against deterministic data:
 *   - POST /api/ask/stream  -> an SSE stream ending in a `result` event.
 *   - POST /api/analysis/run|rerun -> a completed analysis turn.
 *   - GET  /api/analysis/routing   -> the real routing prediction (generated).
 *
 * The path each streamed answer takes (ML vs SQL) is stamped from
 * window.__ROUTING_TRUTH__ (produced by the real backend rule), so the UI shows
 * exactly what production routing decides. Everything else returns benign JSON.
 */
(function () {
    'use strict';

    // ── Minimal environment the controller expects ────────────────────────────
    window._currentUser = { name: 'Test User', email: 'test@jeen.ai' };
    window.getActiveConnection = () => 'sales_db';
    window.showToast = (msg, kind) => { (window.__toasts = window.__toasts || []).push({ msg, kind }); };
    window.escapeHtml = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
    let _session = null;
    window._jeenGetSessionId = () => _session;
    window._jeenSetSessionId = (id) => { _session = id; };
    window.JeenPreferences = {
        getAll: () => ({ aiAnalytics: 'on' }),
        getLlmTimeoutSeconds: () => null,
    };

    const F = window.__FIXTURES__;
    const enc = new TextEncoder();
    const json = (obj, status) => new Response(JSON.stringify(obj), {
        status: status || 200, headers: { 'Content-Type': 'application/json' },
    });

    function routingTruth() {
        return (window.__ROUTING_TRUTH__ && window.__ROUTING_TRUTH__.enabled) || {};
    }

    // Stamp a streamed result's `routing` from the real prediction for its
    // question, so the visible path badge cannot drift from backend logic.
    function stampRouting(result, question) {
        const t = routingTruth()[question];
        if (!t) return result;
        const wr = t.would_route;
        const path = wr === 'needs_analysis' ? 'ml' : wr === 'greeting' ? 'greeting' : 'sql';
        const route = wr === 'router_decides' ? 'needs_query' : wr;
        result.routing = { route, path, source: t.source, reason: t.reason, skill: t.skill_hint };
        return result;
    }

    function withExpiry(result) {
        if (result && result.proposal && !result.proposal.expires_at) {
            result.proposal.expires_at = new Date(Date.now() + 3600 * 1000).toISOString();
        }
        return result;
    }

    function sqlFallback(question) {
        return {
            question, query_id: 'sqlfb-' + Math.random().toString(16).slice(2, 8), session_id: F.SESSION,
            sql: 'SELECT 1', results: { columns: ['n'], rows: [[1]], row_count: 1 },
            answer: 'Answered with SQL as requested.', status: 'completed',
            routing: { route: 'needs_query', path: 'sql', source: 'request_override', reason: 'answer with SQL instead', skill: null },
            metrics: { route: 'needs_query' }, trace: [{ node: 'fused_router', status: 'node_finished', route: 'needs_query' }],
        };
    }

    function resultForAsk(body) {
        const question = body.question;
        // "Answer with SQL instead" (analysis:false) forces the SQL path.
        if (body.analysis === false) {
            const sql = F.SCENARIOS.forecast_sql && question === F.Q.forecast
                ? F.SCENARIOS.forecast_sql() : sqlFallback(question);
            sql.question = question;
            sql.routing = { route: 'needs_query', path: 'sql', source: 'request_override', reason: 'answer with SQL instead', skill: null };
            return sql;
        }
        const name = F.QUESTION_TO_SCENARIO[question];
        const build = name && F.SCENARIOS[name];
        const result = build ? build(body) : sqlFallback(question);
        return withExpiry(stampRouting(result, question));
    }

    function sseStream(result) {
        const blocks = [];
        // A couple of node events so the run strip shows progress, then the result.
        (result.trace || []).forEach((t) => {
            blocks.push('event: node\ndata: ' + JSON.stringify({ node: t.node, status: 'node_started' }) + '\n\n');
        });
        blocks.push('event: result\ndata: ' + JSON.stringify(result) + '\n\n');
        let i = 0;
        const stream = new ReadableStream({
            pull(controller) {
                if (i < blocks.length) {
                    controller.enqueue(enc.encode(blocks[i++]));
                } else {
                    controller.close();
                }
            },
        });
        return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }

    // ── Onboarding (FTUE) state: one row per "user", merged like the real service.
    // Specs simulate a server that already persisted something by seeding
    // window.__ONBOARDING_ROW__ via page.addInitScript() before navigation.
    const STAMP_FOR = {
        welcome_seen: 'welcome_seen_at', tour_completed: 'tour_completed_at',
        checklist_dismissed: 'checklist_dismissed_at', nudge_dismissed: 'nudge_dismissed_at',
        ftue_opted_out: 'ftue_opted_out_at',
    };
    let onboardingRow = Object.assign({
        user_id: 'user-e2e', welcome_seen_at: null, tour_completed_at: null, checklist: {},
        checklist_dismissed_at: null, nudge_dismissed_at: null, ftue_opted_out_at: null, updated_at: null,
    }, window.__ONBOARDING_ROW__ || {});

    function mergeOnboarding(body) {
        const now = new Date().toISOString();
        Object.keys(STAMP_FOR).forEach((flag) => { if (body[flag]) onboardingRow[STAMP_FOR[flag]] = now; });
        if (body.checklist) onboardingRow.checklist = Object.assign({}, onboardingRow.checklist, body.checklist);
        onboardingRow.updated_at = now;
        return onboardingRow;
    }

    const realFetch = window.fetch.bind(window);

    window.fetch = async function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const opts = init || (typeof input === 'object' ? input : {}) || {};
        let body = {};
        if (opts.body) { try { body = JSON.parse(opts.body); } catch (_) { body = {}; } }
        (window.__calls = window.__calls || []).push({ url, method: (opts.method || 'GET').toUpperCase(), body });

        if (url.indexOf('/api/ask/stream') >= 0) {
            return sseStream(resultForAsk(body));
        }
        // A structured 422 as the real route returns it ({message, field, loc}):
        // a horizon of exactly 60 passes the card's own bounds (1–104) but the
        // "server" refuses it, so the message must land on the horizon field.
        const patch = (body && body.params_patch) || {};
        if ((url.indexOf('/api/analysis/run') >= 0 || url.indexOf('/api/analysis/rerun') >= 0) && Number(patch.horizon) === 60) {
            return json({ detail: { message: 'Invalid parameter: 26 weeks of history support a horizon of at most 13', field: 'horizon', loc: ['horizon'] } }, 422);
        }
        if (url.indexOf('/api/analysis/run') >= 0) {
            return json(withExpiry(F.SCENARIOS.run_result(body)));
        }
        if (url.indexOf('/api/analysis/rerun') >= 0) {
            return json(withExpiry(F.SCENARIOS.rerun_result(body)));
        }
        if (url.indexOf('/api/analysis/routing') >= 0) {
            const q = new URL(url, location.origin).searchParams.get('q') || '';
            const t = routingTruth()[q] || { would_route: 'router_decides', source: 'router_llm', skill_hint: null, reason: '' };
            return json(Object.assign({ question: q, contract_version: 'e2e' }, t));
        }
        if (url.indexOf('/api/analysis/suggestions') >= 0) return json({ suggestions: [] });
        if (url.indexOf('/api/analysis/skills') >= 0) return json({ skills: [], contract_version: 'e2e' });
        if (url.indexOf('/api/analysis/chart') >= 0) {
            // Return a chart_config so the controller caches it and stops asking.
            return json({ chart_spec: body.chart_spec || null, chart_config: { option: { series: [] } } });
        }
        if (url.indexOf('/api/user/onboarding') >= 0) {
            const method = (opts.method || 'GET').toUpperCase();
            // Specs set __ONBOARDING_GET_FAILS__ (via addInitScript) to exercise the
            // client's fail-soft boot path, where the row (and its user_id) is unknown.
            if (method === 'GET' && window.__ONBOARDING_GET_FAILS__) return json({ detail: 'down' }, 503);
            return json(method === 'PATCH' ? mergeOnboarding(body) : onboardingRow);
        }
        if (url.indexOf('/api/last') >= 0) return json({});
        if (url.indexOf('/api/conversations') >= 0) return json({ conversations: [] });

        // Anything else that is same-origin gets a benign empty object; let true
        // cross-origin requests (none expected) fall through to the real fetch.
        if (url.startsWith('/') || url.startsWith(location.origin)) return json({});
        return realFetch(input, init);
    };
})();
