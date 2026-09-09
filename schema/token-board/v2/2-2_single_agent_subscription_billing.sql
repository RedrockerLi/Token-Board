-- Token-Board V2.2: one subscription has one effective Agent at a time.
-- New Agent period charges point directly at the Agent.  The old allocation
-- table is retained only as historical input for already-frozen V2.1 rows.

PRAGMA foreign_keys=OFF;

-- A direct Agent charge carries the owner that was effective when that
-- period was materialized.  NULL is allowed for legacy charges that were
-- previously represented only by allocation rows.
ALTER TABLE agent_subscription_period_charges ADD COLUMN software_id INTEGER;

-- Recover old finalized charges that had no allocation yet.  For a legacy
-- period whose start already had an effective binding, use that binding.  A
-- still-open current period is also recovered from the binding that is live
-- on the upgrade day, because V2.1 could create the charge before its
-- delayed allocation existed.
UPDATE agent_subscription_period_charges
SET software_id=(
    SELECT b.software_id
    FROM agent_subscription_bindings b
    WHERE b.subscription_id=agent_subscription_period_charges.subscription_id
      AND (
          (date(b.valid_from)<=date(agent_subscription_period_charges.period_start)
           AND (b.ends_on IS NULL
                OR date(b.ends_on)>date(agent_subscription_period_charges.period_start)))
          OR
          (date(agent_subscription_period_charges.period_start)<=date('now')
           AND date(agent_subscription_period_charges.period_end)>date('now')
           AND date(b.valid_from)<=date('now')
           AND (b.ends_on IS NULL OR date(b.ends_on)>date('now')))
      )
    ORDER BY b.valid_from,b.id
    LIMIT 1
)
WHERE software_id IS NULL
  AND NOT EXISTS (
      SELECT 1
      FROM agent_subscription_charge_allocations a
      WHERE a.period_charge_id=agent_subscription_period_charges.id
  )
  AND EXISTS (
      SELECT 1
      FROM agent_subscription_bindings b
      WHERE b.subscription_id=agent_subscription_period_charges.subscription_id
  );

CREATE INDEX idx_agent_subscription_period_charges_software
    ON agent_subscription_period_charges(software_id, period_start);

PRAGMA foreign_keys=ON;
