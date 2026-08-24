-- V1.5: deletion mode is a global runtime setting, not account lifecycle data.
-- Existing V1 databases lose the redundant per-contract copy; the current
-- setting is read from sync_settings when a key/account is deleted.

ALTER TABLE billing_contracts DROP COLUMN cancellation_policy;
