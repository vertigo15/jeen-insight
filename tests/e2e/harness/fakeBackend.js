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
        if (url.indexOf('/api/last') >= 0) return json({});
        if (url.indexOf('/api/conversations') >= 0) return json({ conversations: [] });

        // Anything else that is same-origin gets a benign empty object; let true
        // cross-origin requests (none expected) fall through to the real fetch.
        if (url.startsWith('/') || url.startsWith(location.origin)) return json({});
        return realFetch(input, init);
    };
})();
