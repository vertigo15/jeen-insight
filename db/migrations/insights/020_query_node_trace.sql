-- ============================================================================
-- Jeen Insights: per-node graph timings
-- ============================================================================
-- Every LangGraph node is already wrapped by a timing helper that records
-- {node, elapsed_ms, type} into the run's `trace`, but until now that trace was
-- returned to the browser and then discarded: only end-to-end totals
-- (`graph_time_ms`, `llm_latency_ms`, `execution_time_ms`) survived. That made
-- "which node is slowest in production" unanswerable from stored data.
--
-- `node_trace` keeps a slim projection of that trace — node name, wall time and
-- node class, in execution order. Deliberately slim: the in-memory trace also
-- carries the fully rendered LLM prompt for each node, which must never be
-- persisted here (it contains catalog schema and user data, and would dwarf the
-- row). Repeated node names are expected and meaningful: order is what
-- distinguishes a first attempt from a repair retry.
--
-- Stored as JSONB on the existing per-query row rather than as a child table:
-- a run is 13-40 events, and this table already grows without a retention
-- policy, so adding 13-40 rows per query would be the more expensive choice.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

ALTER TABLE insights_conversation_sessions
    ADD COLUMN IF NOT EXISTS node_trace JSONB;

COMMENT ON COLUMN insights_conversation_sessions.node_trace IS
'Slim per-node execution trace for this run: [{node, elapsed_ms, type}, …] in execution order. Repeated node names indicate retries. Never contains prompts or result data.';

-- Per-node latency distribution across all stored runs. Answers "which node is
-- slowest" directly. Unnesting a JSONB array means a sequential scan, which is
-- fine for periodic analysis; promote to a rollup table if this is ever put on
-- a request path.
CREATE OR REPLACE VIEW v_insights_node_performance AS
SELECT
    e->>'node' AS node,
    e->>'type' AS node_type,
    COUNT(*) AS runs,
    ROUND(AVG((e->>'elapsed_ms')::numeric), 1) AS avg_ms,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (e->>'elapsed_ms')::numeric) AS p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY (e->>'elapsed_ms')::numeric) AS p95_ms,
    MAX((e->>'elapsed_ms')::numeric) AS max_ms,
    SUM((e->>'elapsed_ms')::numeric) AS total_ms
FROM insights_conversation_sessions s
CROSS JOIN LATERAL jsonb_array_elements(s.node_trace) AS e
WHERE s.node_trace IS NOT NULL
GROUP BY e->>'node', e->>'type';
