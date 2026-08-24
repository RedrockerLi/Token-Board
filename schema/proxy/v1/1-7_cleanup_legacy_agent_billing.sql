-- V1.7: finish the split between agent subscriptions and upstream billing.
--
-- V1.6 copied legacy importer-account charges into the independent agent
-- subscription ledger, but left the old upstream contract and its materialized
-- periods behind.  Disabled account_importers are the marker for those old
-- agent accounts.  Delete children first because the tables use foreign keys.

DELETE FROM billing_period_charges
WHERE contract_id IN (
    SELECT bc.id
    FROM billing_contracts bc
    JOIN account_importers i ON i.account_id=bc.account_id
    WHERE i.enabled=0 AND i.importer_kind IS NOT NULL
      AND EXISTS (SELECT 1 FROM sync_settings
                  WHERE key='agent_migration_v1_6' AND value='done')
);

DELETE FROM billing_rate_events
WHERE contract_id IN (
    SELECT bc.id
    FROM billing_contracts bc
    JOIN account_importers i ON i.account_id=bc.account_id
    WHERE i.enabled=0 AND i.importer_kind IS NOT NULL
      AND EXISTS (SELECT 1 FROM sync_settings
                  WHERE key='agent_migration_v1_6' AND value='done')
);

DELETE FROM billing_contracts
WHERE id IN (
    SELECT bc.id
    FROM billing_contracts bc
    JOIN account_importers i ON i.account_id=bc.account_id
    WHERE i.enabled=0 AND i.importer_kind IS NOT NULL
      AND EXISTS (SELECT 1 FROM sync_settings
                  WHERE key='agent_migration_v1_6' AND value='done')
);
