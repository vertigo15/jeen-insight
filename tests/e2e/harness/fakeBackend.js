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
    const conversationResults = [];
    let favoriteItems = [];
    let conversationItems = [];
    window.__seedFavoriteItems = (items) => { favoriteItems = Array.isArray(items) ? items.slice() : []; };
    window.__seedConversations = (items) => { conversationItems = Array.isArray(items) ? items.slice() : []; };
    const json = (obj, status) => new Response(JSON.stringify(obj), {
        status: status || 200, headers: { 'Content-Type': 'application/json' },
    });

    /**
     * Opt-in chart API script used by chart component specs. Keeping it behind
     * `__CHART_FIXTURES__` means the normal SQL/ML harness still exercises its
     * existing static chart disclosure surface without starting ChartManager.
     *
     * Shape:
     * {
     *   capabilities: { ... },
     *   generate: [{ response: { ... }, status?: 200, delayMs?: 0 }],
     *   edit: [{ response: { ... }, status?: 200, delayMs?: 0 }]
     * }
     */
    async function scriptedChartResponse(kind, body) {
        const fixtures = window.__CHART_FIXTURES__;
        if (!fixtures || typeof fixtures !== 'object') return null;
        if (kind === 'capabilities') {
            return json(fixtures.capabilities || {
                map: { enabled: false },
                osm_map: { enabled: false },
            });
        }
        const queue = Array.isArray(fixtures[kind]) ? fixtures[kind] : [];
        const entry = queue.shift();
        if (!entry) return json({ detail: `No scripted ${kind} response` }, 500);
        const delay = Math.max(0, Number(entry.delayMs) || 0);
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
        const payload = typeof entry.response === 'function'
            ? entry.response(body)
            : entry.response;
        return json(payload || {}, entry.status || 200);
    }

    function routingTruth() {
        return (window.__ROUTING_TRUTH__ && window.__ROUTING_TRUTH__.enabled) || {};
    }

    // Stamp a streamed result's `routing` from the real prediction for its
    // question, so the visible path badge cannot drift from backend logic.
    function stampRouting(result, question) {
        const t = routingTruth()[question];
        if (!t) return result;
        const wr = t.would_route;
        // Mirror response_formatter: path is 'ml' for analysis, 'sql' for a data
        // query, and the route name itself for everything else (capability,
        // greeting, …) so the visible path badge matches production.
        const path = wr === 'needs_analysis'
            ? 'ml'
            : (wr === 'needs_query' || wr === 'router_decides') ? 'sql' : wr;
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
        const stamped = withExpiry(stampRouting(result, question));
        if (stamped && stamped.query_id && !conversationResults.some((item) => item.query_id === stamped.query_id)) {
            conversationResults.push(stamped);
        }
        return stamped;
    }

    function turnDto(result, index) {
        return {
            turn_id: result.query_id,
            sequence_number: index + 1,
            question: result.question,
            sql: result.sql || null,
            execution_status: 'success',
            result_kind: result.results ? 'table' : 'text',
            answer: result.answer || null,
            metrics: result.metrics || {},
            findings: result.findings || [],
            suggestions: result.suggestions || [],
            followups: result.followups || [],
            snapshot_status: result.results ? 'stored' : 'not_applicable',
            row_count: result.results?.row_count || 0,
            has_chart: false,
            has_rerunnable_query: Boolean(result.sql),
            is_favorite: favoriteItems.some((item) => item.turn_id === result.query_id),
        };
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
        if (url.indexOf('/api/chart-capabilities') >= 0 && window.__CHART_FIXTURES__) {
            return scriptedChartResponse('capabilities', body);
        }
        if (url.indexOf('/api/generate-chart') >= 0 && window.__CHART_FIXTURES__) {
            return scriptedChartResponse('generate', body);
        }
        if (url.indexOf('/api/edit-chart') >= 0 && window.__CHART_FIXTURES__) {
            return scriptedChartResponse('edit', body);
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
            if (method === 'GET' && window.__ONBOARDING_GET_DELAY_MS__) {
                await new Promise((resolve) => setTimeout(resolve, Number(window.__ONBOARDING_GET_DELAY_MS__) || 0));
            }
            return json(method === 'PATCH' ? mergeOnboarding(body) : onboardingRow);
        }
        if (url.indexOf('/api/last') >= 0) return json({});
        if (url.indexOf('/api/conversations/favorites') >= 0) {
            const params = new URL(url, location.origin).searchParams;
            const limit = Math.max(1, Number(params.get('limit')) || 50);
            const offset = Math.max(0, Number(params.get('before')) || 0);
            const items = favoriteItems.slice(offset, offset + limit);
            const next = offset + items.length < favoriteItems.length ? String(offset + items.length) : null;
            return json({ items, next_cursor: next });
        }
        const favoriteMatch = url.match(/\/api\/conversations\/([^/]+)\/turns\/([^/]+)\/favorite/);
        if (favoriteMatch) {
            const method = (opts.method || 'GET').toUpperCase();
            const turnId = decodeURIComponent(favoriteMatch[2]);
            const result = conversationResults.find((item) => String(item.query_id) === turnId);
            if (method === 'PUT' && result) {
                favoriteItems = favoriteItems.filter((item) => item.turn_id !== turnId);
                favoriteItems.unshift({
                    conversation_id: F.SESSION,
                    turn_id: turnId,
                    sequence_number: Math.max(1, conversationResults.indexOf(result) + 1),
                    conversation_title: conversationResults[0]?.question || result.question,
                    question: result.question,
                    answer: result.answer || null,
                    result_kind: result.results ? 'table' : 'text',
                    snapshot_status: result.results ? 'stored' : 'not_applicable',
                    source_key: 'sales_db',
                    source_label: 'Sales DB',
                    connection_available: true,
                    created_at: new Date().toISOString(),
                    favorited_at: new Date().toISOString(),
                });
                return json({ success: true, is_favorite: true });
            }
            if (method === 'DELETE') {
                favoriteItems = favoriteItems.filter((item) => item.turn_id !== turnId);
                return json({ success: true, is_favorite: false });
            }
            return json({ detail: 'not found' }, 404);
        }
        const artifactMatch = url.match(/\/api\/conversations\/([^/]+)\/turns\/([^/]+)\/artifact/);
        if (artifactMatch) {
            const result = conversationResults.find((item) => String(item.query_id) === decodeURIComponent(artifactMatch[2]));
            return result ? json({
                turn_id: result.query_id,
                results: result.results || null,
                chart_spec: null,
                chart_config: null,
                snapshot_status: result.results ? 'stored' : 'not_applicable',
                snapshot_at: new Date().toISOString(),
            }) : json({ detail: 'not found' }, 404);
        }
        const turnMatch = url.match(/\/api\/conversations\/([^/]+)\/turns\/([^/?]+)(?:\?|$)/);
        if (turnMatch) {
            const turnId = decodeURIComponent(turnMatch[2]);
            const index = conversationResults.findIndex((item) => String(item.query_id) === turnId);
            return index >= 0 ? json(turnDto(conversationResults[index], index)) : json({ detail: 'not found' }, 404);
        }
        const conversationUrl = new URL(url, location.origin);
        if (conversationUrl.pathname === '/api/conversations' && (opts.method || 'GET').toUpperCase() === 'GET') {
            return json({ items: conversationItems, next_cursor: null });
        }
        const detailMatch = url.match(/\/api\/conversations\/([^/?]+)(?:\?|$)/);
        if (detailMatch && decodeURIComponent(detailMatch[1]) === F.SESSION) {
            if ((opts.method || 'GET').toUpperCase() === 'DELETE') {
                const conversationId = decodeURIComponent(detailMatch[1]);
                const savedCount = favoriteItems.filter((item) => item.conversation_id === conversationId).length;
                const deleteSaved = conversationUrl.searchParams.get('delete_saved') === 'true';
                if (savedCount && !deleteSaved) {
                    return json({
                        detail: {
                            code: 'conversation_has_saved_answers',
                            saved_answer_count: savedCount,
                        },
                    }, 409);
                }
                conversationItems = conversationItems.filter((item) => item.id !== conversationId);
                if (deleteSaved) favoriteItems = favoriteItems.filter((item) => item.conversation_id !== conversationId);
                return json({ success: true, deleted_saved_answer_count: deleteSaved ? savedCount : 0 });
            }
            return json({
                conversation: {
                    id: F.SESSION,
                    title: conversationResults[0]?.question || 'Conversation',
                    source_key: 'sales_db',
                    source_label: 'Sales DB',
                    connection_available: true,
                    turn_count: conversationResults.length,
                },
                turns: conversationResults.map(turnDto).reverse(),
                next_cursor: null,
            });
        }
        if (url.indexOf('/api/conversations') >= 0) return json({ items: conversationItems, next_cursor: null });

        // Anything else that is same-origin gets a benign empty object; let true
        // cross-origin requests (none expected) fall through to the real fetch.
        if (url.startsWith('/') || url.startsWith(location.origin)) return json({});
        return realFetch(input, init);
    };
})();
