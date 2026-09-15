/*
 * Deterministic backend fixtures for the ML e2e harness.
 *
 * The routing labels (ML vs SQL) are NOT here — they come from
 * routing.generated.js, produced by the real backend rule. This file only holds
 * the *shapes* the frontend renders: a text-to-SQL answer, the three ML stops
 * (confirm / clarify / guard) and the completed run/rerun results. Each shape
 * matches the `QueryResponse` / proposal contract the controller reads.
 */
(function () {
    'use strict';

    const SESSION = '11111111-1111-1111-1111-111111111111';

    // Canonical questions — keep identical to QUESTIONS in generate_fixtures.py.
    const Q = {
        sqlAggregate: 'Show me total sales by region last month',
        sqlCount: 'How many orders shipped yesterday?',
        forecast: 'Forecast profit for the next 8 weeks',
        anomaly: 'Is anything unusual in weekly profit?',
        guard: 'Forecast weekly revenue for a brand-new product line',
        clarify: 'Show the correlation between marketing spend and revenue',
        greeting: 'hello',
    };

    // Which fixture a typed question streams back. Routing (ML vs SQL) is decided
    // by routing.generated.js; the *outcome* (confirm vs guard vs clarify) is a
    // planner/guard decision and is fixed per question here.
    const QUESTION_TO_SCENARIO = {
        [Q.sqlAggregate]: 'sql_region',
        [Q.sqlCount]: 'sql_count',
        [Q.forecast]: 'forecast_confirm',
        [Q.anomaly]: 'anomaly_confirm',
        [Q.guard]: 'forecast_guard',
        [Q.clarify]: 'correlation_clarify',
        [Q.greeting]: 'greeting',
    };

    const sqlResult = (question, sql, columns, rows, answer) => ({
        question,
        query_id: 'sql-' + Math.random().toString(16).slice(2, 10),
        session_id: SESSION,
        sql,
        results: { columns, rows, row_count: rows.length },
        answer,
        status: 'completed',
        metrics: { execution_time_ms: 41, llm_latency_ms: 320, total_tokens: 640, retry_count: 0, route: 'needs_query' },
        routing: { route: 'needs_query', path: 'sql', source: 'router_llm', reason: 'lookup answered with SQL', skill: null },
        trace: [
            { node: 'fused_router', status: 'node_finished', route: 'needs_query' },
            { node: 'sql_generator', status: 'node_finished' },
            { node: 'sqlglot_validate', status: 'node_finished' },
            { node: 'execute_query', status: 'node_finished' },
        ],
    });

    // Every real ML result carries a server-built chart_spec; the controller
    // fetches its chart_config once (via /api/analysis/chart) and stops. Omitting
    // it would make renderWorkspace re-request a chart forever.
    const CHART_SPEC = { kind: 'band', x: 'ts', y: 'value' };

    const anomalyAnalysis = {
        skill: 'anomaly_detection',
        method_used: 'MSTL, m=52, robust residual band',
        chart_spec: CHART_SPEC,
        params: {
            series: { table: 'FactInternetSales', schema_name: 'dbo', measure_column: 'Profit', agg: 'sum', grain: 'week', filters: [], start: '2026-03-02', end: '2026-08-31' },
            window: 26, sensitivity: 0.95, method: 'auto',
        },
        validation: { metric: 'WAPE', value: 0.064, band: 'good', coverage: 0.923, coverage_n: 26, basis: 'in-sample fit' },
        egress: { tier: 'A', rows_sent_to_model: 26, columns: ['ts', 'value'] },
        engine: { name: 'statsmodels-mstl', version: '0.15.0' },
        details: {
            method_used: 'MSTL', seasonal_periods: [52],
            candidates: [{ name: 'MSTL', metric: 'fit WAPE', value: 0.064, selected: true }],
            notes: ['3 of 26 weeks fall outside the band'],
        },
        provenance: { missing_policy: '0 of 26 periods zero-filled', span_start: '2026-03-02', span_end: '2026-08-31', query_ts: '2026-09-14T00:00:00Z' },
        guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        facts: { n_points: 26, n_flagged: 3 },
        caveats: ['92% of history sits inside the band'],
        low_confidence: false,
    };

    const forecastAnalysis = {
        skill: 'forecast',
        method_used: 'AutoETS(ZZN) + MSTL, m=52',
        chart_spec: CHART_SPEC,
        params: {
            series: { table: 'FactInternetSales', schema_name: 'dbo', measure_column: 'Profit', agg: 'sum', grain: 'week', filters: [], start: '2026-03-02', end: '2026-08-31' },
            horizon: 8, interval: 0.9, method: 'auto',
        },
        validation: { metric: 'MASE', value: 0.71, band: 'good', coverage: 0.9, coverage_n: 8, basis: 'rolling CV, 3 folds' },
        egress: { tier: 'A', rows_sent_to_model: 26, columns: ['ts', 'value'] },
        engine: { name: 'statsforecast', version: '1.7.0' },
        details: {
            method_used: 'AutoETS', seasonal_periods: [52],
            candidates: [
                { name: 'SeasonalNaive', metric: 'MASE', value: 1.0, is_baseline: true },
                { name: 'AutoETS', metric: 'MASE', value: 0.71, selected: true },
            ],
            notes: ['8-week horizon; 90% interval'],
        },
        provenance: { missing_policy: '0 of 26 periods zero-filled', span_start: '2026-03-02', span_end: '2026-08-31', query_ts: '2026-09-14T00:00:00Z' },
        guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        facts: { n_points: 26, horizon: 8 },
        caveats: [],
        low_confidence: false,
    };

    const correlationAnalysis = {
        skill: 'correlation',
        method_used: 'Pearson r, Bartlett-adjusted, Bonferroni over lags',
        chart_spec: CHART_SPEC,
        params: {
            series: { table: 'FactMarketing', schema_name: 'dbo', measure_column: 'Revenue', agg: 'sum', grain: 'week', filters: [] },
            against: 'MarketingSpend', max_lag: 8,
        },
        validation: { metric: 'r', value: 0.62, band: 'moderate', coverage: 0.9, coverage_n: 26, basis: 'lag 2' },
        egress: { tier: 'A', rows_sent_to_model: 26, columns: ['ts', 'a', 'b'] },
        engine: { name: 'statsmodels', version: '0.15.0' },
        details: { method_used: 'Pearson r', candidates: [{ name: 'lag 2', metric: 'r', value: 0.62, selected: true }], notes: ['strongest at a 2-week lag'] },
        provenance: { missing_policy: '0 of 26 periods zero-filled', span_start: '2026-03-02', span_end: '2026-08-31', query_ts: '2026-09-14T00:00:00Z' },
        guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        facts: { n_points: 26 },
        caveats: [],
        low_confidence: false,
    };

    const analysisResult = (question, analysis, over) => Object.assign({
        question,
        query_id: 'child-' + Math.random().toString(16).slice(2, 10),
        session_id: SESSION,
        parent_query_id: 'parent-1',
        sql: 'SELECT DATE_TRUNC(\'week\', OrderDate) AS ts, SUM(Profit) AS value\nFROM dbo.FactInternetSales GROUP BY 1 ORDER BY 1',
        results: { columns: ['ts', 'value'], rows: [['2026-08-17', 910], ['2026-08-24', 1180], ['2026-08-31', 240]], row_count: 3 },
        answer: 'Three weeks fall outside the expected band; the last week is the largest drop.',
        status: 'completed',
        low_confidence: false,
        routing: { route: 'needs_analysis', path: 'ml', source: 'keyword_cue', reason: 'keyword cue for ' + analysis.skill, skill: analysis.skill },
        analysis,
        metrics: { execution_time_ms: 55, llm_latency_ms: 210, total_tokens: 900, retry_count: 0, route: 'needs_analysis' },
        trace: [
            { node: 'fused_router', status: 'node_finished', route: 'needs_analysis' },
            { node: 'analysis_planner', status: 'node_finished' },
            { node: 'analysis_sql', status: 'node_finished' },
            { node: 'execute_query', status: 'node_finished' },
            { node: 'fused_eval_analytics', status: 'node_finished' },
        ],
    }, over || {});

    // A proposal STOP (no dataset yet). `expires_at` is injected live by the fake
    // backend so cards are never accidentally stale in a test run.
    const proposalResult = (question, proposal, status) => ({
        question,
        query_id: 'prop-' + Math.random().toString(16).slice(2, 10),
        session_id: SESSION,
        sql: null,
        results: null,
        answer: proposal.message,
        status: status || 'proposal',
        routing: {
            route: 'needs_analysis', path: 'ml',
            source: 'keyword_cue', reason: 'keyword cue for ' + proposal.skill, skill: proposal.skill,
        },
        proposal,
        metrics: { route: 'needs_analysis' },
        trace: [
            { node: 'fused_router', status: 'node_finished', route: 'needs_analysis' },
            { node: 'analysis_planner', status: 'node_finished' },
        ],
    });

    const SCENARIOS = {
        greeting: () => ({
            question: Q.greeting,
            query_id: 'greet-1', session_id: SESSION,
            sql: null, results: null,
            answer: 'Hello! Ask me anything about your data.',
            status: 'completed',
            routing: { route: 'greeting', path: 'greeting', source: 'greeting', reason: 'greeting regex matched', skill: null },
            metrics: { route: 'greeting' },
            trace: [{ node: 'fused_router', status: 'node_finished', route: 'greeting' }],
        }),

        sql_region: () => sqlResult(
            Q.sqlAggregate,
            'SELECT region, SUM(sales) AS total\nFROM sales GROUP BY region ORDER BY total DESC',
            ['region', 'total'],
            [['West', 12400], ['East', 9800], ['South', 7200]],
            'West leads with 12,400 in total sales.',
        ),

        sql_count: () => sqlResult(
            Q.sqlCount,
            'SELECT COUNT(*) AS shipped FROM orders WHERE ship_date = CURRENT_DATE - 1',
            ['shipped'], [[128]],
            '128 orders shipped yesterday.',
        ),

        // Forecast asked with `analysis:false` ("Answer with SQL instead"): the
        // same question comes back as a plain SQL answer.
        forecast_sql: () => sqlResult(
            Q.forecast,
            'SELECT DATE_TRUNC(\'week\', OrderDate) AS ts, SUM(Profit) AS value\nFROM dbo.FactInternetSales GROUP BY 1 ORDER BY 1',
            ['ts', 'value'],
            [['2026-08-17', 910], ['2026-08-24', 1180], ['2026-08-31', 240]],
            'Here are the weekly Profit totals.',
        ),

        forecast_confirm: () => proposalResult(Q.forecast, {
            proposal_id: 'prop-forecast-1', kind: 'confirm', skill: 'forecast', tier: 'A',
            message: 'Forecast weekly Profit 8 weeks ahead from the last 26 weeks.',
            estimated_seconds: 6,
            egress_summary: 'SQL rolls Profit up to 26 weekly totals; only those rows are sent to the analysis service.',
            chips: [
                { key: 'horizon', label: 'Horizon (weeks)', value: 8 },
                { key: 'grain', label: 'Grain', value: 'week', options: ['day', 'week', 'month'] },
                { key: 'interval', label: 'Interval', value: 0.9 },
            ],
            params: forecastAnalysis.params,
            guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        }),

        anomaly_confirm: () => proposalResult(Q.anomaly, {
            proposal_id: 'prop-anomaly-1', kind: 'confirm', skill: 'anomaly_detection', tier: 'A',
            message: 'Scan weekly Profit for points outside a robust seasonal band.',
            estimated_seconds: 5,
            egress_summary: 'SQL rolls Profit up to 26 weekly totals; only those rows are sent to the analysis service.',
            chips: [
                { key: 'window', label: 'Window (weeks)', value: 26 },
                { key: 'sensitivity', label: 'Sensitivity', value: 0.95 },
            ],
            params: anomalyAnalysis.params,
            guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        }),

        forecast_guard: () => proposalResult(Q.guard, {
            proposal_id: 'prop-guard-1', kind: 'guard', skill: 'forecast', tier: 'A',
            message: '0 rows of dbo.FactSales match this filter over the requested window — a forecast needs at least 12 weekly points.',
            guard_results: [
                { name: 'series_length', passed: false, detail: '5 of 12 weeks needed' },
                { name: 'nonzero_span', passed: false, detail: '0 rows in span' },
            ],
            options: [
                { kind: 'answer_with_sql', label: 'Show the 5 weeks as a table', recommended: true, description: 'Return the raw rows instead of a model.' },
                { kind: 'override', label: 'Run anyway (low confidence)', description: 'Force the model past the guard; result is marked low confidence.' },
            ],
        }, 'blocked'),

        correlation_clarify: () => proposalResult(Q.clarify, {
            proposal_id: 'prop-clarify-1', kind: 'clarify', skill: 'correlation', tier: 'A',
            message: 'Which measure should I correlate with marketing spend?',
            options: [
                { kind: 'patch', label: 'Revenue', params_patch: { series: { measure_column: 'Revenue' } }, description: 'Correlate against Revenue.' },
                { kind: 'patch', label: 'Profit', params_patch: { series: { measure_column: 'Profit' } }, description: 'Correlate against Profit.' },
            ],
        }, 'needs_clarification'),

        // Returned by POST /api/analysis/run after a confirm or a clarify pick.
        // Which analysis comes back is keyed off the confirmed proposal id.
        run_result: (body) => {
            const pid = (body && body.proposal_id) || '';
            const override = Boolean(body && body.override_guards);
            if (pid.indexOf('forecast') >= 0) {
                return analysisResult(Q.forecast, Object.assign({}, forecastAnalysis, { low_confidence: override }), {
                    low_confidence: override, answer: 'Forecast the next 8 weeks; the trend holds.',
                });
            }
            if (pid.indexOf('clarify') >= 0) {
                return analysisResult(Q.clarify, Object.assign({}, correlationAnalysis), {
                    answer: 'Marketing spend and revenue move together (r 0.62) at a 2-week lag.',
                });
            }
            const analysis = Object.assign({}, anomalyAnalysis, { low_confidence: override });
            return analysisResult(Q.anomaly, analysis, {
                low_confidence: override,
                answer: override
                    ? 'Ran past the guard: 3 weeks look unusual, but treat this as indicative.'
                    : 'Three weeks fall outside the expected band; the last week is the largest drop.',
            });
        },

        // Returned by POST /api/analysis/rerun — a child turn with a param diff.
        rerun_result: () => {
            const analysis = Object.assign({}, forecastAnalysis, {
                params: Object.assign({}, forecastAnalysis.params, { horizon: 12 }),
                param_diff: { horizon: { from: 8, to: 12 } },
            });
            return analysisResult(Q.forecast + ' — horizon 12', analysis, {
                answer: 'Extended the forecast to 12 weeks; the trend holds.',
            });
        },
    };

    window.__FIXTURES__ = { SESSION, Q, QUESTION_TO_SCENARIO, SCENARIOS };
})();
