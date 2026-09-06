-- V1.21: subscription effective values are UTC calendar dates.
--
-- A selected subscription date means 00:00Z.  The clock portion is therefore
-- not business data and is removed from live/configuration date columns.  Audit
-- timestamps (created_at/updated_at/deleted_at) and price-event timestamps are
-- intentionally retained at timestamp precision.

UPDATE billing_contracts
SET valid_from=substr(valid_from,1,10)
WHERE charge_type='recurring'
  AND valid_from LIKE '____-__-__T%';

UPDATE upstream_credentials
SET valid_from=substr(valid_from,1,10)
WHERE valid_from LIKE '____-__-__T%';

UPDATE agent_subscriptions
SET valid_from=substr(valid_from,1,10)
WHERE valid_from LIKE '____-__-__T%';

UPDATE agent_subscription_instances
SET valid_from=substr(valid_from,1,10)
WHERE valid_from LIKE '____-__-__T%';

UPDATE agent_subscription_instance_identities
SET valid_from=substr(valid_from,1,10)
WHERE valid_from LIKE '____-__-__T%';

UPDATE agent_subscription_bindings
SET valid_from=substr(valid_from,1,10)
WHERE valid_from LIKE '____-__-__T%';
