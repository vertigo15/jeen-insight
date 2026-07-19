-- ============================================================================
-- Jeen Insights: distributed fixed-window rate/budget counters (cross-cutting)
-- ============================================================================
-- Cross-replica caps for connector action proposals/executions and daily cost
-- budgets. A fixed-window counter keyed per (scope, window). Coarse but enough to
-- bound abuse + cost; the security controls (typed policy, confirm gate) are
-- independent of this table. See src/connectors/rate_limiter.py.
-- ============================================================================

CREATE TABLE IF NOT EXISTS connector_rate_counters (
    key           TEXT        PRIMARY KEY,
    window_start  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    count         INTEGER     NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_counters_window ON connector_rate_counters(window_start);
