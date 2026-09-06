-- V1.6: dashboard deletion removes archived history only.
--
-- Account identity is deliberately not tombstoned: later request-log usage
-- for the same account must be able to return to the dashboard archive.
DROP INDEX IF EXISTS idx_account_exclusions_time;
DROP TABLE IF EXISTS account_exclusions;
