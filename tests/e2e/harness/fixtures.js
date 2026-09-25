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
        sqlByYear: 'Total sales for the Bikes category by year',
        forecast: 'Forecast profit for the next 8 weeks',
        anomaly: 'Is anything unusual in weekly profit?',
        guard: 'Forecast weekly revenue for a brand-new product line',
        clarify: 'Show the correlation between marketing spend and revenue',
        capability: 'which ML models can I use?',
        greeting: 'hello',
    };

    const CHART = {
        results: {
            columns: ['region', 'sales'],
            rows: [['North', 10], ['South', 30], ['West', 20]],
            row_count: 3,
        },
        barSpec: {
            chart_type: 'bar',
            x: 'region',
            y: ['sales'],
            series: null,
            aggregate: 'sum',
            sort: 'none',
            title: 'Sales by region',
            x_label: 'Region',
            y_label: 'Sales',
            value_format: 'number',
            stacked: false,
            smooth: false,
        },
        barConfig: {
            grid: { left: '3%', right: '4%', bottom: '8%', top: 32, containLabel: true },
            xAxis: { type: 'category', data: ['North', 'South', 'West'], name: 'Region' },
            yAxis: { type: 'value', name: 'Sales', axisLabel: {} },
            series: [{ name: 'sales', type: 'bar', data: [10, 30, 20], label: { show: false } }],
        },
        lineEdit: {
            chart_type: 'line',
            chart_spec: {
                chart_type: 'line',
                x: 'region',
                y: ['sales'],
                series: null,
                aggregate: 'sum',
                sort: 'none',
                title: 'Sales by region',
                x_label: 'Region',
                y_label: 'Sales',
                value_format: 'number',
                stacked: false,
                smooth: false,
            },
            chart_config: {
                grid: { left: '3%', right: '4%', bottom: '8%', top: 32, containLabel: true },
                xAxis: { type: 'category', data: ['North', 'South', 'West'], name: 'Region' },
                yAxis: { type: 'value', name: 'Sales', axisLabel: {} },
                series: [{ name: 'sales', type: 'line', data: [10, 30, 20], label: { show: true } }],
            },
            derived_series: [],
            notes: 'Changed the chart to a line and enabled labels.',
            out_of_scope: false,
        },
    };

    // Which fixture a typed question streams back. Routing (ML vs SQL) is decided
    // by routing.generated.js; the *outcome* (confirm vs guard vs clarify) is a
    // planner/guard decision and is fixed per question here.
    const QUESTION_TO_SCENARIO = {
        [Q.sqlAggregate]: 'sql_region',
        [Q.sqlCount]: 'sql_count',
        [Q.sqlByYear]: 'sql_by_year',
        [Q.forecast]: 'forecast_confirm',
        [Q.anomaly]: 'anomaly_confirm',
        [Q.guard]: 'forecast_guard',
        [Q.clarify]: 'correlation_clarify',
        [Q.capability]: 'capability',
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

    // Production-shaped setup chips (what nodes/analysis.py _chips() emits for a
    // forecast): sections, units, help, bounds and human option names.
    const GRAIN_LABELS = { day: 'Day', week: 'Week', month: 'Month' };
    const forecastChips = [
        { key: 'measure_column', label: 'Measure', value: 'Profit', options: ['Profit', 'SalesAmount', 'OrderQuantity'], group: 'Data', required: true, help: 'The numeric column to analyse.' },
        { key: 'agg', label: 'Aggregate', value: 'sum', options: ['sum', 'count', 'avg', 'min', 'max'], group: 'Data',
          option_labels: { sum: 'Sum', count: 'Count', avg: 'Average', min: 'Minimum', max: 'Maximum' }, help: 'How rows are rolled up per period.' },
        { key: 'date_column', label: 'Date column', value: 'OrderDate', options: ['OrderDate', 'ShipDate', 'DueDate'], group: 'Data', required: true, help: 'The date that defines the timeline.' },
        { key: 'group_by', label: 'Split by', value: 'none', options: ['none', 'SalesTerritoryKey', 'CurrencyKey'], group: 'Data',
          option_labels: { none: '— none —' }, help: 'One series per value of this column.' },
        { key: 'grain', label: 'Grain', value: 'week', options: ['day', 'week', 'month'], group: 'Model', option_labels: GRAIN_LABELS, help: 'The size of one period.' },
        { key: 'window', label: 'Look-back window', value: 26, kind: 'number', step: 1, min: 12, max: 1500, unit_from: 'grain', group: 'Model',
          defaults_by_grain: { day: 90, week: 26, month: 24 }, help: 'How much history the model learns from.' },
        { key: 'method', label: 'Model', value: 'auto', options: ['auto', 'auto_arima', 'auto_ets', 'theta', 'drift', 'seasonal_naive'], group: 'Model',
          option_labels: { auto: 'Auto', auto_arima: 'Auto ARIMA', auto_ets: 'Auto ETS', theta: 'Theta', drift: 'Drift', seasonal_naive: 'Seasonal naive' },
          help: 'Auto cross-validates the shortlist and keeps the baseline unless a model beats it.' },
        { key: 'horizon', label: 'Horizon', value: 8, kind: 'number', step: 1, min: 1, max: 104, unit_from: 'grain', group: 'Output', help: 'How far ahead to project.' },
        { key: 'interval', label: 'Interval', value: 0.9, options: [0.5, 0.8, 0.9, 0.95], group: 'Output',
          option_labels: { '0.5': '50%', '0.8': '80%', '0.9': '90%', '0.95': '95%' },
          help: 'Width of the prediction band: 90% means the true value should fall inside it 9 times in 10.' },
    ];

    const forecastAnalysis = {
        skill: 'forecast',
        method_used: 'AutoETS(ZZN) + MSTL, m=52',
        chart_spec: CHART_SPEC,
        params: {
            series: { table: 'FactInternetSales', schema_name: 'dbo', measure_column: 'Profit', agg: 'sum', grain: 'week', filters: [], start: '2026-03-02', end: '2026-08-31' },
            window: 26, horizon: 8, interval: 0.9, method: 'auto',
        },
        // "Edit setup" on the finished result: the same chips with the values that ran.
        definition: {
            chips: forecastChips,
            egress_summary: 'SQL rolls SUM(Profit) up to about 26 weekly totals on sales_db. Only those rows are sent to the analysis service; no FactInternetSales rows are read.',
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
        facts: { n_points: 26, horizon: 8, interval: 0.9, interval_method: 'conformal_scaled', mase_scale: 4200 },
        caveats: [],
        low_confidence: false,
        // "Adjustments to try": what src/analysis/advisor.py attaches in response_formatter.
        adjustments: [
            { code: 'wider_window_for_season', args: { window: 105, unit: 'week' }, params_patch: { window: 105 }, recommended: true },
            { code: 'try_theta', args: { baseline: 'SeasonalNaive' }, params_patch: { method: 'theta' }, recommended: false },
        ],
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

        // Year and key columns are labels, not quantities: the grid must show
        // them without thousands separators. One long follow-up exercises wrapping.
        sql_by_year: () => ({
            ...sqlResult(
                Q.sqlByYear,
                'SELECT d.CalendarYear, MIN(d.DateKey) AS FirstDateKey, SUM(s.SalesAmount) AS total_sales\nFROM sales s JOIN dates d ON d.DateKey = s.OrderDateKey\nGROUP BY d.CalendarYear ORDER BY 1',
                ['CalendarYear', 'FirstDateKey', 'total_sales', 'MonthStart', 'UpdatedAt'],
                [
                    [2005, 20050701, 3266373.86, '2005-07-01 00:00:00', '2005-07-01 00:00:00'],
                    [2006, 20060101, 6530343.49, '2006-01-01 00:00:00', '2006-01-01 13:45:00'],
                    [2007, 20070101, 9359103.12, '2007-01-01 00:00:00', '2007-01-01 00:00:00'],
                    [2008, 20080101, 9162324.85, '2008-01-01 00:00:00', '2008-01-01 00:00:00'],
                ],
                'Bikes sales grew from $3.27M in 2005 to a peak of $9.36M in 2007.',
            ),
            followups: [
                'What were the sales trends for other product categories by year, and how do they compare with Bikes over the same period?',
                'Which regions drove the most Bikes sales?',
            ],
        }),

        // A capability question ("which ML models can I use?") — no SQL, no ML
        // run; the capability node answers it. The path is neither ml nor sql so
        // no route pill renders (like a greeting). The answer is Markdown (the
        // capability route is on MARKDOWN_ROUTES), and it deliberately embeds an
        // HTML/XSS payload and a mixed Hebrew+English table row so the spec can
        // prove the renderer escapes and keeps direction.
        capability: () => ({
            question: Q.capability,
            query_id: 'cap-1', session_id: SESSION,
            sql: null, results: null,
            answer: [
                '## Time Series (aggregates only)',
                '- **Anomaly detection** Auto / Seasonal (MSTL) / Trend (LOWESS) / 3-sigma — flag unusual spikes or drops',
                '- **Forecast** Auto ARIMA / Auto ETS / Theta — project a measure into the future',
                '',
                'A column like `sales_amount` stays intact. <script>alert(1)</script>',
                '',
                '| Skill | אלגוריתם |',
                '|-------|----------|',
                '| Clustering | K-means / HDBSCAN |',
            ].join('\n'),
            status: 'completed',
            routing: { route: 'capability', path: 'capability', source: 'capability_cue', reason: 'question about this assistant\'s ML skills', skill: null },
            metrics: { route: 'capability' },
            trace: [
                { node: 'fused_router', status: 'node_finished', route: 'capability' },
                { node: 'capability_answer', status: 'node_finished' },
            ],
        }),

        // Forecast asked with `analysis:false` ("Answer with SQL instead"): the
        // same question comes back as a plain SQL answer.
        forecast_sql: () => sqlResult(
            Q.forecast,
            'SELECT DATE_TRUNC(\'week\', OrderDate) AS ts, SUM(Profit) AS value\nFROM dbo.FactInternetSales GROUP BY 1 ORDER BY 1',
            ['ts', 'value'],
            [['2026-08-17', 910], ['2026-08-24', 1180], ['2026-08-31', 240]],
            'Here are the weekly Profit totals.',
        ),

        // Production density: every field the real planner emits, with metadata.
        forecast_confirm: () => proposalResult(Q.forecast, {
            proposal_id: 'prop-forecast-1', kind: 'confirm', skill: 'forecast', tier: 'A',
            message: 'Reading this as a forecast of SUM(Profit) by week over the last 26 weeks. Confirm or adjust before I run it.',
            estimated_seconds: 6,
            egress_summary: 'SQL rolls SUM(Profit) up to about 26 weekly totals on sales_db. Only those rows are sent to the analysis service; no FactInternetSales rows are read.',
            chips: forecastChips,
            params: forecastAnalysis.params,
            guard_results: [{ name: 'series_length', passed: true, detail: '26 of 12 weeks needed' }],
        }),

        // Minimal, metadata-less chips — the shape of a card persisted before the
        // setup form existed. Must still render (one "Setup" section) and run.
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

    // ── Admin analytics (Settings › Analytics; GET /api/admin/analytics/*) ────
    // Shapes match the AnalyticsOverview / *Timeseries / *TopUsers / ... models.
    // Values scale with the window so a range change is observable in the DOM.
    const day = (offset) => {
        const d = new Date(Date.UTC(2026, 8, 25));
        d.setUTCDate(d.getUTCDate() - offset);
        return d.toISOString().slice(0, 10);
    };
    const period = (days, mult) => ({
        questions: 40 * mult, text_only_turns: 6 * mult, active_users: 5, active_connections: 2,
        successes: 36 * mult, errors: 4 * mult, refused: 1, success_rate: 0.9, avg_graph_time_ms: 2300,
        total_tokens: 125000 * mult, logins: 12 * mult, comments: 3 * mult, feedback_events: 9 * mult,
        avg_rating: 4.2, thumbs_up: 6 * mult, thumbs_down: 2 * mult, thumbs_up_events: 7 * mult, thumbs_down_events: 2 * mult,
    });
    const FEEDBACK_ITEMS = [
        { id: 42, occurred_at: '2026-09-25T09:00:00+00:00', user_id: '7', name: 'Dana Levi', email: 'dana@jeen.ai', source_key: 'sales_db',
          thumb: 'thumbs_down', rating: 2, feedback_type: 'report_bug', message: '=SUM(A1) the total looks wrong', question: Q.sqlAggregate, query_id: 'q-42' },
        { id: 41, occurred_at: '2026-09-24T15:30:00+00:00', user_id: '8', name: null, email: 'noa@jeen.ai', source_key: 'sales_db',
          thumb: 'thumbs_up', rating: null, feedback_type: null, message: null, question: Q.forecast, query_id: 'q-41' },
        { id: 40, occurred_at: '2026-09-23T11:00:00+00:00', user_id: 'sso-abc', name: null, email: null, source_key: 'hr_db',
          thumb: null, rating: 5, feedback_type: 'general', message: 'Great chart', question: Q.sqlCount, query_id: 'q-40' },
    ];
    const ANALYTICS = {
        overview: (days) => ({
            days, start: day(days) + 'T00:00:00+00:00', end: day(0) + 'T00:00:00+00:00',
            dau: 2, wau: 4, mau: 5, current: period(days, days / 30 >= 1 ? days / 30 : 1), previous: period(days, 0.5),
        }),
        timeseries: (days) => ({
            days,
            points: Array.from({ length: days }, (_, i) => ({
                day: day(days - 1 - i), active_users: (i % 3), questions: (i % 5) * 2, errors: i % 2,
                thumbs_up: i % 4 === 0 ? 1 : 0, thumbs_down: i % 7 === 0 ? 1 : 0, logins: i % 3,
            })),
        }),
        'top-users': (days) => ({
            days,
            items: [
                { user_id: '7', name: 'Dana Levi', email: 'dana@jeen.ai', role: 'editor', questions: 18, analyses: 2, success_rate: 0.94,
                  last_active: '2026-09-25T09:00:00+00:00', thumbs_up: 4, thumbs_down: 1, avg_rating: 4.5 },
                { user_id: 'sso-abc', name: null, email: null, role: null, questions: 9, analyses: 0, success_rate: 0.78,
                  last_active: '2026-09-23T11:00:00+00:00', thumbs_up: 1, thumbs_down: 1, avg_rating: null },
            ],
        }),
        'top-connections': (days) => ({
            days,
            items: [
                { source_key: 'sales_db', questions: 30, distinct_users: 4, success_rate: 0.9, avg_graph_time_ms: 2100,
                  last_used: '2026-09-25T09:00:00+00:00', thumbs_up: 5, thumbs_down: 2, thumbs_down_rate: 0.2857 },
                { source_key: 'hr_db', questions: 10, distinct_users: 2, success_rate: 0.8, avg_graph_time_ms: 3400,
                  last_used: '2026-09-23T11:00:00+00:00', thumbs_up: 1, thumbs_down: 0, thumbs_down_rate: 0 },
            ],
        }),
        feedback: (days, params) => {
            const thumb = params.get('thumb'), type = params.get('type'), connection = params.get('connection');
            const before = Number(params.get('before')) || null;
            const items = FEEDBACK_ITEMS.filter((f) =>
                (!thumb || f.thumb === thumb) && (!type || f.feedback_type === type)
                && (!connection || f.source_key === connection) && (!before || f.id < before));
            return { days, items, next_before: null };
        },
        analysis: (days) => ({
            days,
            items: [
                { skill: 'forecast', runs: 6, ok: 4, guard_failed: 1, errors: 1, avg_execution_ms: 900, distinct_users: 2, thumbs_up: 2, thumbs_down: 1 },
                { skill: 'anomaly', runs: 3, ok: 3, guard_failed: 0, errors: 0, avg_execution_ms: 400, distinct_users: 1, thumbs_up: 1, thumbs_down: 0 },
            ],
        }),
        errors: (days) => ({
            days,
            by_type: [
                { error_type: 'timeout', source_key: 'hr_db', failures: 3 },
                { error_type: 'validation', source_key: 'sales_db', failures: 1 },
            ],
            top_failing_questions: [
                { question: Q.guard, failures: 2, distinct_users: 1, distinct_connections: 1, last_seen: '2026-09-24T15:30:00+00:00' },
            ],
        }),
    };

    window.__FIXTURES__ = { SESSION, Q, CHART, QUESTION_TO_SCENARIO, SCENARIOS, ANALYTICS };
})();
