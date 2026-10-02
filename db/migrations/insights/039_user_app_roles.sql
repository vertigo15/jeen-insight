-- ============================================================================
-- Jeen Insights: per-application role
-- ============================================================================
-- auth_users is the shared account table. Schema Modeler still authorizes
-- from auth_users.role. Insights used to read and write that same column, so
-- an Insights role change also changed the person's Metadata role.
--
-- insights_user_app_roles holds the Insights role only. auth_users.role stays
-- the Metadata role. Existing accounts keep their current role in Insights
-- (copied once, below). A missing Insights row means viewer, so a person
-- created later in Metadata is not granted their Metadata role inside Insights.
-- Idempotent.
-- ============================================================================

CREATE TABLE IF NOT EXISTS insights_user_app_roles (
    user_id     INTEGER     NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    app         VARCHAR(32) NOT NULL,
    role        VARCHAR(20) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, app),
    CONSTRAINT insights_user_app_roles_app_check
        CHECK (app IN ('insights')),
    CONSTRAINT insights_user_app_roles_role_check
        CHECK (role IN ('admin', 'editor', 'viewer'))
);

INSERT INTO insights_user_app_roles (user_id, app, role)
SELECT id,
       'insights',
       CASE
           WHEN role IN ('admin', 'editor', 'viewer') THEN role
           WHEN role = 'user' THEN 'editor'
           ELSE 'viewer'
       END
FROM auth_users
ON CONFLICT (user_id, app) DO NOTHING;
