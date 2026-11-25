-- Token-Board V2.9: retire the V2.1 Agent allocation ledger.
--
-- V2.1 stored an Agent subscription period charge as allocation rows keyed by
-- (period_charge_id, software_id).  V2.2 attributes each charge directly to
-- the Agent that was effective when the period was materialized
-- (agent_subscription_period_charges.software_id).  This migration folds the
-- remaining V2.1 division into that model, keeps every already-exported fact
-- exact-once, and drops the allocation table.

PRAGMA foreign_keys=OFF;

-- 1) Give every allocated charge its direct owner.  A legacy period could
--    carry more than one share; the largest share names the owner and the
--    charge keeps the full frozen amount.
UPDATE agent_subscription_period_charges
SET software_id=(
    SELECT a.software_id
      FROM agent_subscription_charge_allocations a
     WHERE a.period_charge_id=agent_subscription_period_charges.id
     ORDER BY a.normalized_recurring_cost DESC, a.software_id
     LIMIT 1
)
WHERE software_id IS NULL
  AND EXISTS (
      SELECT 1 FROM agent_subscription_charge_allocations a
       WHERE a.period_charge_id=agent_subscription_period_charges.id
  );

-- 2) Remember the surviving allocation event for every charge.  Only the
--    owning share keeps its immutable row; non-owning shares and events whose
--    charge is gone are dropped so no allocation source key remains.
CREATE TEMP TABLE _v29_owner_events AS
SELECT e.id event_id, c.id charge_id
  FROM billing_export_events e
  JOIN agent_subscription_period_charges c
    ON c.id=CAST(substr(e.source_key,1,instr(e.source_key,':')-1) AS INTEGER)
   AND c.software_id=CAST(substr(e.source_key,
                                 instr(e.source_key,':')+1) AS INTEGER)
 WHERE e.source_table='agent_subscription_charge_allocations';

DELETE FROM billing_export_events
 WHERE source_table='agent_subscription_charge_allocations'
   AND id NOT IN (SELECT event_id FROM _v29_owner_events);

-- 3) Re-key each surviving event to the direct charge source and align its
--    snapshot with the charge row.  The exporter looks events up by
--    (source_table, source_key) and compares payloads, so the rewrite keeps
--    the high-water mark from resending history.
UPDATE billing_export_events
SET source_table='agent_subscription_period_charges',
    source_key=CAST(ev.charge_id AS TEXT),
    account_id=c.software_id,
    account_uuid=(SELECT ai.uuid FROM account_identities ai
                   WHERE ai.id=c.software_id),
    account_kind=(SELECT ai.account_kind FROM account_identities ai
                   WHERE ai.id=c.software_id),
    month=substr(c.period_start,1,7),
    period_start=c.period_start,
    billing_unit_id='agent-subscription-instance:' || COALESCE(
        c.instance_uuid_snapshot,
        (SELECT ii.uuid FROM agent_subscription_instance_identities ii
          WHERE ii.id=c.instance_id),
        (SELECT i.uuid FROM agent_subscription_instances i
          WHERE i.id=c.instance_id),
        'instance:' || c.instance_id),
    recurring_charge=c.recurring_charge,
    normalized_recurring_cost=c.normalized_recurring_cost,
    currency=COALESCE(c.currency,'CNY'),
    base_currency=COALESCE(c.base_currency,'CNY'),
    fx_rate_date=c.fx_rate_date,
    frozen_on=substr(c.finalized_on,1,10)
FROM _v29_owner_events ev
JOIN agent_subscription_period_charges c ON c.id=ev.charge_id
WHERE billing_export_events.id=ev.event_id;

DROP TABLE _v29_owner_events;
DROP TABLE agent_subscription_charge_allocations;

PRAGMA foreign_keys=ON;
