-- 0015: replace the cancellation grace-hours free window with a configurable
-- default deletion operation (immediate / end-of-period) for plan & agent
-- subscriptions.  The grace snapshot columns are dropped entirely — no
-- history compatibility (uniform new rule: any touched current period is billed).
--
-- `cancellation_mode` on plan_billing_config:
--   'immediate'     — deleting a plan/agent account or plan key sets
--                      deleted_at = now (本期计费, 立即停止路由).
--   'end_of_period' — deleting sets deleted_at = end of the current billing
--                      period (本期计费, 下期不计费); the entity keeps routing
--                      until that time, then stops (routing treats a future
--                      deleted_at as still active).
-- `deferred_cleanup_mode` on upstream_accounts records the detach/cascade
-- intent of an end-of-period account deletion; the deletion finalizer runs the
-- local-key / aggregate cleanup once deleted_at has passed.

ALTER TABLE plan_billing_config ADD COLUMN cancellation_mode TEXT NOT NULL DEFAULT 'immediate'
    CHECK (cancellation_mode IN ('immediate', 'end_of_period'));

ALTER TABLE upstream_accounts ADD COLUMN deferred_cleanup_mode TEXT;

ALTER TABLE plan_billing_config DROP COLUMN cancellation_grace_hours;

ALTER TABLE upstream_keys DROP COLUMN cancellation_grace_hours;

ALTER TABLE upstream_keys_cloud DROP COLUMN cancellation_grace_hours;
