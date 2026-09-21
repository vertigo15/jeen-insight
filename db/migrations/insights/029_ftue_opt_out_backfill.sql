-- ============================================================================
-- Jeen Insights: grandfather completed FTUE users onto ftue_opted_out_at
-- ============================================================================
-- 028 added the permanent opt-out column but did not backfill. Before that
-- column existed, "Don't show this again" only stamped welcome_seen_at, and
-- users who finished the rest of onboarding dismissed the checklist and nudge
-- independently. After 028, those accounts are not opted out, so remaining
-- surfaces (quick-start cards, and the nudge if it races the GET) come back.
--
-- Forward path is unchanged: new independent dismissals still do not imply a
-- global opt-out. This UPDATE is a one-time migration of the old completed
-- state. Idempotent (no-ops once ftue_opted_out_at is set).
-- ============================================================================

UPDATE insights_user_onboarding
   SET ftue_opted_out_at = COALESCE(
           ftue_opted_out_at,
           GREATEST(
               welcome_seen_at,
               checklist_dismissed_at,
               nudge_dismissed_at,
               updated_at
           ),
           NOW()
       ),
       updated_at = NOW()
 WHERE ftue_opted_out_at IS NULL
   AND (
        welcome_seen_at IS NOT NULL
        OR (
            checklist_dismissed_at IS NOT NULL
            AND nudge_dismissed_at IS NOT NULL
        )
       );
