-- V1.15: transient key cooldown is no longer a persisted account policy.
-- Keep explicit subscription_5h contracts unchanged; only normalize legacy
-- API/metered metadata so configuration and runtime semantics agree.
UPDATE billing_contracts
SET cooldown_policy_json = '{"kind":"none"}'
WHERE json_extract(COALESCE(cooldown_policy_json, '{}'), '$.kind') = 'transient';
