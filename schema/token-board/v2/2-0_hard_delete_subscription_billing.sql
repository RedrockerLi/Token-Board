-- Token-Board V2: live configuration is represented by physical rows.
--
-- This migration is intentionally destructive only for live configuration.
-- Request rows, account identities, frozen charges and export events remain
-- immutable facts.  The upgrade runner applies this file to a shadow copy.

PRAGMA foreign_keys = ON;

-- These views/triggers are V1 definitions and mention columns that disappear
-- below.  Recreate the public compatibility views after the physical schema
-- has been converted.
DROP VIEW IF EXISTS upstream_accounts;
DROP VIEW IF EXISTS upstream_keys;
DROP VIEW IF EXISTS upstream_keys_cloud;
DROP TRIGGER IF EXISTS price_usage_event;

-- The old dashboard tombstone/legacy model must not survive into V2.  Detach
-- all runtime foreign keys before deleting any live parent rows.
CREATE TEMP TABLE v2_purge_accounts(id INTEGER PRIMARY KEY);
INSERT INTO v2_purge_accounts(id)
SELECT id FROM accounts
WHERE account_kind='legacy'
   OR lifecycle_state<>'active'
   OR (deleted_at IS NOT NULL
       AND deleted_at<=strftime('%Y-%m-%dT%H:%M:%SZ','now'));

UPDATE request_log
SET account_identity_id=COALESCE(account_identity_id,account_id),
    account_id=NULL, route_set_id=NULL, client_key_id=NULL,
    upstream_key_id=NULL, credential_uuid=NULL, agent_software_id=NULL
WHERE account_id IN (SELECT id FROM v2_purge_accounts);

UPDATE request_attempts
SET account_id=NULL, upstream_id=NULL, upstream_key_id=NULL,
    credential_uuid=NULL
WHERE account_id IN (SELECT id FROM v2_purge_accounts)
   OR upstream_id IN (SELECT id FROM upstreams
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts));

UPDATE request_log
SET route_set_id=NULL, client_key_id=NULL, credential_uuid=NULL,
    upstream_key_id=NULL
WHERE route_set_id IN (SELECT id FROM route_sets
                       WHERE account_id IN (SELECT id FROM v2_purge_accounts))
   OR credential_uuid IN (SELECT c.uuid FROM upstream_credentials c
                          JOIN upstreams u ON u.id=c.upstream_id
                          WHERE u.account_id IN (SELECT id FROM v2_purge_accounts));
UPDATE request_attempts
SET upstream_id=NULL, upstream_key_id=NULL, credential_uuid=NULL
WHERE upstream_id IN (SELECT id FROM upstreams
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts))
   OR credential_uuid IN (SELECT c.uuid FROM upstream_credentials c
                          JOIN upstreams u ON u.id=c.upstream_id
                          WHERE u.account_id IN (SELECT id FROM v2_purge_accounts));

-- Historical facts belonging to legacy accounts are explicitly hidden, not
-- merely detached.  All other account facts survive without a live parent.
DELETE FROM request_log
WHERE account_identity_id IN (
    SELECT id FROM account_identities WHERE account_kind='legacy'
);
DELETE FROM request_attempts
WHERE account_id IN (
    SELECT id FROM account_identities WHERE account_kind='legacy'
);
DELETE FROM billing_period_charges
WHERE account_identity_id IN (
    SELECT id FROM account_identities WHERE account_kind='legacy'
);
DELETE FROM billing_export_events WHERE account_kind='legacy';
DELETE FROM agent_subscription_charge_allocations
WHERE software_id IN (
    SELECT id FROM account_identities WHERE account_kind='legacy'
);

-- A future V1 account deletion becomes a future end for its Plan units and
-- contract.  A credential-level future marker wins over the account marker.
UPDATE upstream_credentials
SET deleted_at=(SELECT a.deleted_at FROM accounts a
                JOIN upstreams u ON u.account_id=a.id
                WHERE u.id=upstream_credentials.upstream_id
                  AND a.deleted_at IS NOT NULL
                  AND a.deleted_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))
WHERE deleted_at IS NULL
  AND upstream_id IN (
      SELECT u.id FROM upstreams u JOIN accounts a ON a.id=u.account_id
      WHERE a.deleted_at IS NOT NULL
        AND a.deleted_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')
  );

UPDATE billing_contracts
SET valid_until=(SELECT a.deleted_at FROM accounts a
                 WHERE a.id=billing_contracts.account_id
                   AND a.deleted_at IS NOT NULL
                   AND a.deleted_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))
WHERE valid_until IS NULL
  AND account_id IN (
      SELECT id FROM accounts
      WHERE deleted_at IS NOT NULL
        AND deleted_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')
  );

-- Remove individually deleted keys and credentials while preserving their
-- historical identity on request rows and frozen ledger rows.
CREATE TEMP TABLE v2_purge_credentials(uuid TEXT PRIMARY KEY);
INSERT INTO v2_purge_credentials(uuid)
SELECT uuid FROM upstream_credentials
WHERE deleted_at IS NOT NULL
  AND deleted_at<=strftime('%Y-%m-%dT%H:%M:%SZ','now');
UPDATE request_log SET credential_uuid=NULL, upstream_key_id=NULL
WHERE credential_uuid IN (SELECT uuid FROM v2_purge_credentials);
UPDATE request_attempts SET credential_uuid=NULL, upstream_key_id=NULL
WHERE credential_uuid IN (SELECT uuid FROM v2_purge_credentials);
DELETE FROM upstream_secrets
WHERE credential_uuid IN (SELECT uuid FROM v2_purge_credentials);
DELETE FROM upstream_credentials
WHERE uuid IN (SELECT uuid FROM v2_purge_credentials);

-- Deleted client keys are not billing facts and are physically removed.
UPDATE request_log SET client_key_id=NULL
WHERE client_key_id IN (SELECT id FROM client_keys WHERE deleted_at IS NOT NULL);
DELETE FROM client_keys WHERE deleted_at IS NOT NULL;

-- Purge agent subscriptions/instances/bindings that are already terminal;
-- future valid_until values become ends_at below.
CREATE TEMP TABLE v2_purge_agent_subscriptions(id INTEGER PRIMARY KEY);
INSERT INTO v2_purge_agent_subscriptions(id)
SELECT id FROM agent_subscriptions
WHERE lifecycle_state='deleted'
   OR (valid_until IS NOT NULL
       AND valid_until<=strftime('%Y-%m-%dT%H:%M:%SZ','now'));
DELETE FROM agent_subscription_rate_events
WHERE instance_id IN (SELECT id FROM agent_subscription_instances
                      WHERE subscription_id IN
                            (SELECT id FROM v2_purge_agent_subscriptions));
DELETE FROM agent_subscription_bindings
WHERE subscription_id IN (SELECT id FROM v2_purge_agent_subscriptions);
DELETE FROM agent_subscription_instances
WHERE subscription_id IN (SELECT id FROM v2_purge_agent_subscriptions);
DELETE FROM agent_subscriptions
WHERE id IN (SELECT id FROM v2_purge_agent_subscriptions);

DELETE FROM agent_subscription_bindings
WHERE lifecycle_state='deleted'
   OR (valid_until IS NOT NULL
       AND valid_until<=strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- Agent software is a live account graph.  Preserve allocations as facts;
-- the V2 allocation table below deliberately has no live-account FK.
DELETE FROM agent_software_runtime
WHERE software_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM agent_subscription_bindings
WHERE software_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM agent_software
WHERE id IN (SELECT id FROM v2_purge_accounts);

-- Purge all remaining children of terminal/legacy proxy accounts.
DELETE FROM account_importers
WHERE account_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM billing_rate_events
WHERE contract_id IN (SELECT id FROM billing_contracts
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM billing_contracts
WHERE account_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM upstream_model_catalog
WHERE upstream_id IN (SELECT id FROM upstreams
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM upstream_secrets
WHERE credential_uuid IN (SELECT c.uuid FROM upstream_credentials c
                          JOIN upstreams u ON u.id=c.upstream_id
                          WHERE u.account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM upstream_credentials
WHERE upstream_id IN (SELECT id FROM upstreams
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM route_rules
WHERE upstream_id IN (SELECT id FROM upstreams
                      WHERE account_id IN (SELECT id FROM v2_purge_accounts))
   OR route_set_id IN (SELECT id FROM route_sets
                       WHERE account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM client_keys
WHERE route_set_id IN (SELECT id FROM route_sets
                       WHERE account_id IN (SELECT id FROM v2_purge_accounts));
DELETE FROM route_sets
WHERE account_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM upstreams
WHERE account_id IN (SELECT id FROM v2_purge_accounts);
DELETE FROM accounts WHERE id IN (SELECT id FROM v2_purge_accounts);

-- Legacy identities have no V2 meaning after their hidden historical facts
-- have been removed.  Other identities are stable audit anchors.
DELETE FROM account_identities WHERE account_kind='legacy';

DROP TRIGGER IF EXISTS accounts_identity_au;
DROP INDEX IF EXISTS idx_accounts_kind_lifecycle;

-- Replace V1 lifecycle columns with the V2 physical-row contract.
ALTER TABLE accounts DROP COLUMN lifecycle_state;
ALTER TABLE accounts DROP COLUMN disabled_at;
ALTER TABLE accounts DROP COLUMN deleted_at;

CREATE TRIGGER accounts_identity_au
AFTER UPDATE OF name,account_kind,updated_at ON accounts
WHEN EXISTS (SELECT 1 FROM account_identities WHERE id=NEW.id)
BEGIN
  UPDATE account_identities SET name=NEW.name,
    account_kind=NEW.account_kind,updated_at=NEW.updated_at
  WHERE id=NEW.id;
END;

CREATE TRIGGER accounts_v2_kind_ai
BEFORE INSERT ON accounts WHEN NEW.account_kind NOT IN ('proxy','agent')
BEGIN SELECT RAISE(ABORT,'V2 accounts require proxy or agent account_kind'); END;
CREATE TRIGGER accounts_v2_kind_au
BEFORE UPDATE OF account_kind ON accounts
WHEN NEW.account_kind NOT IN ('proxy','agent')
BEGIN SELECT RAISE(ABORT,'V2 accounts require proxy or agent account_kind'); END;
CREATE INDEX idx_accounts_kind ON accounts(account_kind,id);

DROP INDEX IF EXISTS idx_credentials_upstream;
ALTER TABLE upstream_credentials ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
    CHECK (enabled IN (0,1));
ALTER TABLE upstream_credentials ADD COLUMN ends_at TEXT;
UPDATE upstream_credentials SET enabled=CASE WHEN disabled_at IS NULL THEN 1 ELSE 0 END,
    ends_at=CASE WHEN deleted_at IS NOT NULL
                   AND deleted_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')
                THEN deleted_at ELSE NULL END;
ALTER TABLE upstream_credentials DROP COLUMN disabled_at;
ALTER TABLE upstream_credentials DROP COLUMN deleted_at;
CREATE INDEX idx_credentials_upstream
    ON upstream_credentials(upstream_id, enabled, position, uuid);

ALTER TABLE client_keys DROP COLUMN deleted_at;

DROP INDEX IF EXISTS billing_contracts_one_open;
DROP INDEX IF EXISTS idx_contracts_account_period;
ALTER TABLE billing_contracts ADD COLUMN ends_at TEXT;
UPDATE billing_contracts SET ends_at=valid_until;
ALTER TABLE billing_contracts DROP COLUMN valid_until;
CREATE UNIQUE INDEX billing_contracts_one_open
    ON billing_contracts(account_id) WHERE ends_at IS NULL;
CREATE INDEX idx_contracts_account_period
    ON billing_contracts(account_id, valid_from, ends_at);

DROP INDEX IF EXISTS idx_agent_subscription_bindings_software;
DROP INDEX IF EXISTS idx_agent_subscription_bindings_subscription;
ALTER TABLE agent_subscription_bindings ADD COLUMN ends_at TEXT;
UPDATE agent_subscription_bindings SET ends_at=valid_until;
ALTER TABLE agent_subscription_bindings DROP COLUMN valid_until;
ALTER TABLE agent_subscription_bindings DROP COLUMN lifecycle_state;
CREATE INDEX idx_agent_subscription_bindings_software
    ON agent_subscription_bindings(software_id, valid_from, ends_at);
CREATE INDEX idx_agent_subscription_bindings_subscription
    ON agent_subscription_bindings(subscription_id, valid_from, ends_at);

DROP INDEX IF EXISTS idx_agent_subscription_instances_parent;
ALTER TABLE agent_subscription_instances ADD COLUMN ends_at TEXT;
UPDATE agent_subscription_instances SET ends_at=valid_until;
ALTER TABLE agent_subscription_instances DROP COLUMN valid_until;
ALTER TABLE agent_subscription_instances DROP COLUMN lifecycle_state;
CREATE INDEX idx_agent_subscription_instances_parent
    ON agent_subscription_instances(subscription_id, valid_from, ends_at, id);

ALTER TABLE agent_subscriptions ADD COLUMN ends_at TEXT;
UPDATE agent_subscriptions SET ends_at=valid_until;
ALTER TABLE agent_subscriptions DROP COLUMN valid_until;
ALTER TABLE agent_subscriptions DROP COLUMN lifecycle_state;

-- Allocations are frozen facts and therefore cannot depend on a live software
-- account.  Rebuild this one table to remove that FK.
DROP INDEX IF EXISTS idx_agent_charge_allocations_software;
ALTER TABLE agent_subscription_charge_allocations
    RENAME TO agent_subscription_charge_allocations_v1;
CREATE TABLE agent_subscription_charge_allocations (
    period_charge_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    finalized_at TEXT,
    PRIMARY KEY (period_charge_id, software_id)
);
INSERT INTO agent_subscription_charge_allocations
SELECT period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
       currency,base_currency,fx_rate_date,finalized_at
FROM agent_subscription_charge_allocations_v1;
DROP TABLE agent_subscription_charge_allocations_v1;
CREATE INDEX idx_agent_charge_allocations_software
    ON agent_subscription_charge_allocations(software_id, period_charge_id);

DROP TABLE IF EXISTS v2_purge_credentials;
DROP TABLE IF EXISTS v2_purge_agent_subscriptions;
DROP TABLE IF EXISTS v2_purge_accounts;

CREATE VIEW upstream_keys AS
SELECT c.runtime_id AS id,u.account_id,s.secret_value AS key_value,c.position,
       c.created_at,c.valid_from,c.ends_at
FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id
JOIN upstream_secrets s ON s.credential_uuid=c.uuid;

CREATE VIEW upstream_keys_cloud AS
SELECT u.account_id,c.key_masked,c.position,c.valid_from,c.ends_at
FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id;

CREATE VIEW upstream_accounts AS
SELECT rs.id AS id,rs.name,u.base_url,u.api_format,rs.created_at,
       u.endpoint_path,u.auth_scheme AS auth_header,
       CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END AS is_aggregate,
       CASE WHEN bc.charge_type='recurring' THEN 'plan' ELSE 'api' END AS account_type,
       u.max_concurrency,a.valid_from,bc.ends_at,bc.currency,NULL AS agent_kind,
       NULL AS deferred_cleanup_mode
FROM route_sets rs
LEFT JOIN accounts a ON a.id=rs.account_id AND a.account_kind='proxy'
LEFT JOIN upstreams u ON u.account_id=a.id
LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.ends_at IS NULL
WHERE rs.enabled=1 AND (rs.account_id IS NULL OR a.id IS NOT NULL);

-- Imported usage still uses the SQLite pricing authority.  Its only V2
-- change is the contract end column.
CREATE TRIGGER price_usage_event AFTER INSERT ON request_log
WHEN NEW.pricing_status='pending'
BEGIN
  UPDATE request_log SET
    pricing_status=CASE WHEN EXISTS(
      SELECT 1 FROM pricing_rules pr
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=pr.currency
          AND fx.quote_currency='CNY' AND fx.date<=date('now')
        ))
    ) THEN 'rated' ELSE 'unrated' END,
    equivalent_cost=COALESCE((
      SELECT ((max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)
          *COALESCE(t.input_price,pr.input_price)
        +(NEW.cache_read_tokens/1000000.0)*COALESCE(t.cache_read_price,pr.cache_read_price)
        +(NEW.completion_tokens/1000000.0)*COALESCE(t.output_price,pr.output_price))
      *COALESCE((SELECT ps.multiplier FROM pricing_slots ps
        WHERE ps.pricing_rule_id=pr.id ORDER BY ps.id LIMIT 1),1.0)
      *CASE WHEN pr.currency='CNY' THEN 1.0 ELSE COALESCE((
        SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency=pr.currency
        AND fx.quote_currency='CNY' AND fx.date<=date('now')
        ORDER BY fx.date DESC LIMIT 1),NULL) END
      FROM pricing_rules pr
      LEFT JOIN pricing_length_tiers t ON t.pricing_rule_id=pr.id
       AND t.threshold_tokens=(SELECT max(t2.threshold_tokens)
          FROM pricing_length_tiers t2 WHERE t2.pricing_rule_id=pr.id
          AND NEW.prompt_tokens>=t2.threshold_tokens)
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
      ORDER BY pr.priority,pr.id LIMIT 1
    ),0)
  WHERE id=NEW.id;
  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
      AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
      AND (bc.ends_at IS NULL OR bc.ends_at>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;

-- Export events carry the exact period-start date.  V1 events only exposed a
-- month; recover the precise date from their immutable source facts when it
-- is still available and use the first of the month only for old seeds.
ALTER TABLE billing_export_events ADD COLUMN period_start TEXT;
UPDATE billing_export_events
SET period_start=(SELECT c.period_start FROM billing_period_charges c
                  WHERE billing_export_events.source_table='billing_period_charges'
                    AND c.id=CAST(billing_export_events.source_key AS INTEGER));
UPDATE billing_export_events
SET period_start=(SELECT c.period_start FROM agent_subscription_period_charges c
                  WHERE billing_export_events.source_table='agent_subscription_charge_allocations'
                    AND c.id=CAST(substr(billing_export_events.source_key,1,
                                         instr(billing_export_events.source_key,':')-1) AS INTEGER))
WHERE period_start IS NULL;
UPDATE billing_export_events
SET period_start=substr(month,1,7) || '-01T00:00:00Z'
WHERE period_start IS NULL;
CREATE INDEX idx_billing_export_events_period_start
    ON billing_export_events(period_start, id);
