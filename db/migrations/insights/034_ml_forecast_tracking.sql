-- Forecast-vs-actual tracking.
--
-- Every completed forecast is captured at the moment it is shown (one run row,
-- one point row per forecast period, per series), so that once the forecast
-- periods have elapsed the same aggregation can be re-run over them and the
-- realized error and interval coverage compared with what the model claimed.
--
-- The run row keeps the ORIGINAL validated parameters (with filter values —
-- the envelope's copy is redacted for the sandbox) because the realized-actual
-- query is rebuilt from them; the frozen MASE scale, so a MASE can be
-- reproduced without the history; and the engine hash, so a later engine
-- change never gets credit or blame for an older forecast.
--
-- Rows are owned by the turn: deleting a conversation (CONVERSATION_KEEP_LAST
-- pruning or a user delete) cascades here. Insights-owned objects only.

CREATE TABLE IF NOT EXISTS insights_forecast_runs (
    query_id          UUID PRIMARY KEY
                      REFERENCES insights_conversation_sessions(id) ON DELETE CASCADE,
    user_id           VARCHAR(255) NOT NULL,
    source_key        VARCHAR(255) NOT NULL,
    session_id        UUID,
    skill             VARCHAR(40)  NOT NULL DEFAULT 'forecast',
    contract_version  TEXT         NOT NULL,
    params            JSONB        NOT NULL,
    grain             VARCHAR(10)  NOT NULL,
    horizon           INT          NOT NULL,
    interval_level    NUMERIC(4,3) NOT NULL,
    method            VARCHAR(80),
    interval_method   VARCHAR(40),
    metric            VARCHAR(10),
    mase_scale        DOUBLE PRECISION,
    measure_label     TEXT,
    engine_hash       VARCHAR(20),
    history_end       DATE,
    multi_series      BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    evaluated_at      TIMESTAMPTZ,
    evaluation        JSONB
);

CREATE INDEX IF NOT EXISTS idx_insights_forecast_runs_owner
    ON insights_forecast_runs (user_id, source_key, created_at DESC);

CREATE TABLE IF NOT EXISTS insights_forecast_points (
    query_id   UUID  NOT NULL REFERENCES insights_forecast_runs(query_id) ON DELETE CASCADE,
    series_id  TEXT  NOT NULL DEFAULT '',
    ts         DATE  NOT NULL,
    forecast   DOUBLE PRECISION NOT NULL,
    lower      DOUBLE PRECISION,
    upper      DOUBLE PRECISION,
    PRIMARY KEY (query_id, series_id, ts)
);

COMMENT ON TABLE insights_forecast_runs IS
'Jeen Insights: one row per completed forecast turn — the original params, the model, and the latest realized-accuracy evaluation.';
COMMENT ON TABLE insights_forecast_points IS
'Jeen Insights: the forecast periods (point, lower, upper) as shown to the user, per series, for later comparison with actuals.';
COMMENT ON COLUMN insights_forecast_runs.params IS
'The validated ForecastParams that ran (filters unredacted); the realized-actual query is rebuilt from these.';
COMMENT ON COLUMN insights_forecast_runs.mase_scale IS
'In-sample MAE of the (seasonal) naive forecaster at forecast time — the MASE denominator, frozen.';
