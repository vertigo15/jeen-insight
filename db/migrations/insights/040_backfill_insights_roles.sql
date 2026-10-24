-- ============================================================================
-- Jeen Insights: backfill missing insights_user_app_roles rows
-- ============================================================================
-- Migration 039 created insights_user_app_roles and seeded it from
-- auth_users.role for every account that existed at the time.  Any account
-- created afterwards in Jeen Metadata (Schema Modeler) without going through
-- Insights has no row, so its Insights role is an invisible COALESCE('viewer')
-- default that admins cannot see or manage in the user panel.
--
-- This migration backfills those gaps: every auth_users row that still lacks
-- an 'insights' entry gets a 'viewer' row.  Existing rows are untouched
-- (ON CONFLICT DO NOTHING).  Idempotent.
-- ============================================================================

INSERT INTO insights_user_app_roles (user_id, app, role)
SELECT u.id, 'insights', 'viewer'
FROM   auth_users u
WHERE  NOT EXISTS (
    SELECT 1
    FROM   insights_user_app_roles r
    WHERE  r.user_id = u.id
      AND  r.app     = 'insights'
)
ON CONFLICT (user_id, app) DO NOTHING;
