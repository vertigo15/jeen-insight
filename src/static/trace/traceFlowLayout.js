/**
 * Layout of the Run Details transition map, one per LangGraph.
 *
 * `edges` must list exactly the arrows of the compiled graph (with START drawn
 * from `pre_graph_setup`, the request pre-load that runs before it):
 * tests/unit/test_trace_flow_layout.py compares them with
 * `build_graph().get_graph()` / `build_dax_graph().get_graph()`, whose branch
 * targets come from the routing functions' `Literal` return types.
 * `optional` nodes are drawn only when the run used one of them.
 */
(function () {
    'use strict';

    const SQL = {
        columns: [
            {
                title: 'Setup',
                hint: 'Runs before the pipeline: loads the catalog, conversation history, audit record and filter choices in parallel.',
                nodes: ['pre_graph_setup'],
            },
            {
                title: 'Memory',
                hint: 'Builds the turn ledger, answers from stored prior results, or searches past questions.',
                nodes: ['context_composer', 'memory_answer_generator', 'history_search'],
            },
            {
                title: 'Routing',
                hint: 'Classifies the request and chooses the branch; "what can you do?" is answered here.',
                nodes: ['fused_router', 'capability_answer'],
            },
            {
                title: 'Catalog + Filters',
                hint: 'Gets the catalog (or answers catalog-help questions from it), then plans and checks the filters.',
                nodes: ['catalog_lookup', 'filter_planner', 'filter_grounder', 'catalog_help_answer'],
            },
            {
                title: 'Prompt',
                hint: 'Binds values from earlier results and builds the prompt; ML questions plan and guard their analysis here.',
                nodes: ['prior_data_binder', 'prompt_builder', 'analysis_planner', 'analysis_guard', 'analysis_sql'],
            },
            {
                title: 'SQL + Safety',
                hint: 'Generates SQL, validates syntax and tables, and checks DLP rules.',
                nodes: ['sql_generator', 'sqlglot_validate', 'dlp_check'],
            },
            {
                title: 'Execution + Checks',
                hint: 'Runs the SQL (or the ML analysis) and checks empty and trivial results; the table is sent at the trivial check.',
                nodes: ['execute_query', 'analysis_run', 'empty_filter_result_check', 'empty_result_check', 'trivial_result_check'],
            },
            {
                title: 'Eval + Retry',
                hint: 'Checks the result answers the question and writes insights, or decides what to retry.',
                nodes: ['fused_eval_analytics', 'feedback_classifier'],
            },
            {
                title: 'Output',
                hint: 'Formats the answer, saves memory, and writes observability logs.',
                nodes: ['response_formatter', 'save_to_memory', 'observability_log'],
            },
        ],
        optional: ['analysis_planner', 'analysis_guard', 'analysis_sql', 'analysis_run'],
        edges: [
            ['pre_graph_setup', 'context_composer', 'start'],
            ['pre_graph_setup', 'catalog_lookup', 'confirmed analysis'],
            ['context_composer', 'fused_router', 'ledger'],
            ['fused_router', 'memory_answer_generator', 'from memory'],
            ['fused_router', 'history_search', 'history lookup'],
            ['fused_router', 'capability_answer', 'capability'],
            ['fused_router', 'catalog_lookup', 'needs query'],
            ['fused_router', 'response_formatter', 'greeting / blocked'],
            ['capability_answer', 'response_formatter', 'help answer'],
            ['catalog_help_answer', 'response_formatter', 'catalog answer'],
            ['history_search', 'response_formatter', 'matches'],
            ['memory_answer_generator', 'catalog_lookup', 'needs fresh data'],
            ['memory_answer_generator', 'trivial_result_check', 'computed table'],
            ['memory_answer_generator', 'response_formatter', 'answer ready'],
            ['catalog_lookup', 'filter_planner', 'catalog ready'],
            ['catalog_lookup', 'catalog_help_answer', 'catalog help'],
            ['catalog_lookup', 'analysis_guard', 'confirmed analysis'],
            ['catalog_lookup', 'response_formatter', 'no catalog'],
            ['filter_planner', 'filter_grounder', 'filter plan'],
            ['filter_planner', 'response_formatter', 'which field?'],
            ['filter_grounder', 'prompt_builder', 'filters ready'],
            ['filter_grounder', 'prior_data_binder', 'uses prior result'],
            ['filter_grounder', 'analysis_planner', 'ML question'],
            ['filter_grounder', 'response_formatter', 'which value?'],
            ['prior_data_binder', 'prompt_builder', 'bound values'],
            ['prior_data_binder', 'response_formatter', 'too many values'],
            ['analysis_planner', 'analysis_guard', 'plan ready'],
            ['analysis_planner', 'prompt_builder', 'fall back to SQL'],
            ['analysis_planner', 'response_formatter', 'clarification'],
            ['analysis_guard', 'analysis_sql', 'guards passed'],
            ['analysis_guard', 'response_formatter', 'confirm / blocked'],
            ['analysis_sql', 'sqlglot_validate', 'series SQL'],
            ['analysis_sql', 'response_formatter', 'analysis error'],
            ['prompt_builder', 'sql_generator', 'system prompt'],
            ['sql_generator', 'sqlglot_validate', 'SQL'],
            ['sql_generator', 'response_formatter', 'clarification'],
            ['sqlglot_validate', 'dlp_check', 'valid'],
            ['sqlglot_validate', 'feedback_classifier', 'syntax / table issue'],
            ['sqlglot_validate', 'response_formatter', 'invalid ML SQL'],
            ['dlp_check', 'execute_query', 'safe'],
            ['dlp_check', 'response_formatter', 'blocked'],
            ['execute_query', 'empty_filter_result_check', 'rows'],
            ['execute_query', 'analysis_run', 'ML rows'],
            ['execute_query', 'feedback_classifier', 'exec error'],
            ['execute_query', 'response_formatter', 'ML exec error'],
            ['empty_filter_result_check', 'trivial_result_check', 'rows ok'],
            ['empty_filter_result_check', 'empty_result_check', '0 rows'],
            ['empty_filter_result_check', 'feedback_classifier', 're-check filters'],
            ['empty_result_check', 'trivial_result_check', 'empty is plausible'],
            ['empty_result_check', 'feedback_classifier', 'SQL looks wrong'],
            ['analysis_run', 'trivial_result_check', 'analysis done'],
            ['analysis_run', 'response_formatter', 'analysis error'],
            ['trivial_result_check', 'fused_eval_analytics', 'needs eval'],
            ['trivial_result_check', 'response_formatter', 'trivial / eval off'],
            ['fused_eval_analytics', 'response_formatter', 'answers intent'],
            ['fused_eval_analytics', 'feedback_classifier', 'wrong result'],
            ['feedback_classifier', 'sql_generator', 'retry SQL'],
            ['feedback_classifier', 'catalog_lookup', 'missing table'],
            ['feedback_classifier', 'filter_grounder', 're-ground filters'],
            ['feedback_classifier', 'response_formatter', 'exhausted'],
            ['response_formatter', 'save_to_memory', 'final payload'],
            ['save_to_memory', 'observability_log', 'persisted'],
        ],
    };

    const DAX = {
        columns: [
            {
                title: 'Setup',
                hint: 'Runs before the pipeline: loads the model catalog, conversation history and audit record in parallel.',
                nodes: ['pre_graph_setup'],
            },
            {
                title: 'Memory',
                hint: 'Builds the turn ledger, answers from stored prior results, or searches past questions.',
                nodes: ['context_composer', 'memory_answer_generator', 'history_search'],
            },
            {
                title: 'Routing',
                hint: 'Classifies the request and chooses the branch; "what can you do?" is answered here.',
                nodes: ['fused_router', 'capability_answer'],
            },
            {
                title: 'Catalog',
                hint: 'Loads the Power BI model catalog, or answers catalog-help questions from it.',
                nodes: ['dax_catalog_lookup', 'catalog_help_answer'],
            },
            {
                title: 'Plan',
                hint: 'Builds a typed query plan, checks its filter values against the dataset, and assembles the DAX prompt.',
                nodes: ['dax_query_planner', 'dax_entity_resolver', 'dax_prompt_builder'],
            },
            {
                title: 'DAX + Safety',
                hint: 'Generates DAX, statically validates it (read-only gate, symbols, DLP, TOPN), and repairs on failure.',
                nodes: ['dax_generator', 'dax_static_validate', 'dax_repair'],
            },
            {
                title: 'Execution + Checks',
                hint: 'Runs executeQueries against the dataset, checks result integrity, then decides whether to evaluate.',
                nodes: ['pbi_execute_query', 'result_integrity_check', 'trivial_result_check'],
            },
            {
                title: 'Eval + Retry',
                hint: 'Checks the result answers the question and writes insights, or picks the next repair step.',
                nodes: ['fused_eval_analytics', 'dax_feedback_router'],
            },
            {
                title: 'Output',
                hint: 'Formats the answer, saves memory, and writes observability logs.',
                nodes: ['response_formatter', 'save_to_memory', 'observability_log'],
            },
        ],
        optional: [],
        edges: [
            ['pre_graph_setup', 'context_composer', 'start'],
            ['context_composer', 'fused_router', 'ledger'],
            ['fused_router', 'memory_answer_generator', 'from memory'],
            ['fused_router', 'history_search', 'history lookup'],
            ['fused_router', 'capability_answer', 'capability'],
            ['fused_router', 'dax_catalog_lookup', 'needs query'],
            ['fused_router', 'response_formatter', 'greeting / blocked'],
            ['capability_answer', 'response_formatter', 'help answer'],
            ['catalog_help_answer', 'response_formatter', 'catalog answer'],
            ['history_search', 'response_formatter', 'matches'],
            ['memory_answer_generator', 'dax_catalog_lookup', 'needs fresh data'],
            ['memory_answer_generator', 'trivial_result_check', 'computed table'],
            ['memory_answer_generator', 'response_formatter', 'answer ready'],
            ['dax_catalog_lookup', 'dax_query_planner', 'catalog ready'],
            ['dax_catalog_lookup', 'catalog_help_answer', 'catalog help'],
            ['dax_catalog_lookup', 'response_formatter', 'blocked'],
            ['dax_query_planner', 'dax_entity_resolver', 'plan ready'],
            ['dax_query_planner', 'response_formatter', 'clarification'],
            ['dax_entity_resolver', 'dax_prompt_builder', 'values checked'],
            ['dax_entity_resolver', 'response_formatter', 'which value?'],
            ['dax_prompt_builder', 'dax_generator', 'system prompt'],
            ['dax_generator', 'dax_static_validate', 'DAX'],
            ['dax_generator', 'response_formatter', 'clarification'],
            ['dax_static_validate', 'pbi_execute_query', 'valid'],
            ['dax_static_validate', 'dax_repair', 'repairable'],
            ['dax_static_validate', 'response_formatter', 'invalid / blocked'],
            ['dax_repair', 'dax_static_validate', 're-validate'],
            ['pbi_execute_query', 'result_integrity_check', 'rows'],
            ['pbi_execute_query', 'dax_feedback_router', 'exec error'],
            ['pbi_execute_query', 'response_formatter', 'needs connect'],
            ['result_integrity_check', 'trivial_result_check', 'ok'],
            ['result_integrity_check', 'dax_feedback_router', 'empty diagnostic'],
            ['trivial_result_check', 'fused_eval_analytics', 'needs eval'],
            ['trivial_result_check', 'response_formatter', 'trivial / eval off'],
            ['fused_eval_analytics', 'response_formatter', 'answers intent'],
            ['fused_eval_analytics', 'dax_feedback_router', 'wrong result'],
            ['dax_feedback_router', 'dax_repair', 'local repair'],
            ['dax_feedback_router', 'dax_generator', 'regenerate'],
            ['dax_feedback_router', 'dax_query_planner', 'replan'],
            ['dax_feedback_router', 'dax_entity_resolver', 're-check values'],
            ['dax_feedback_router', 'dax_catalog_lookup', 'refresh catalog'],
            ['dax_feedback_router', 'response_formatter', 'exhausted'],
            ['response_formatter', 'save_to_memory', 'final payload'],
            ['save_to_memory', 'observability_log', 'persisted'],
        ],
    };

    const layouts = { sql: SQL, dax: DAX };
    if (typeof window !== 'undefined') window.TraceFlowLayout = layouts;
    if (typeof module !== 'undefined' && module.exports) module.exports = layouts;
})();
