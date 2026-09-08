-- Token-Board V2.1: Agent billing is day-grained and export recovery is
-- keyed by immutable allocation identity.  This file is only run on a
-- shadow database by the schema-upgrade boundary.

PRAGMA foreign_keys=OFF;

-- Do not silently turn an unparseable legacy value into NULL while removing
-- its clock portion.  This guard runs before any table is rebuilt, so the
-- shadow upgrade aborts without publishing a partially converted database.
CREATE TEMP TABLE v21_invalid_agent_dates(
    table_name TEXT NOT NULL,
    row_id INTEGER,
    column_name TEXT NOT NULL,
    value TEXT
);
INSERT INTO v21_invalid_agent_dates(table_name,row_id,column_name,value)
SELECT 'agent_subscriptions',id,'valid_from',valid_from
FROM agent_subscriptions
WHERE valid_from IS NULL OR date(valid_from) IS NULL
UNION ALL
SELECT 'agent_subscriptions',id,'ends_at',ends_at
FROM agent_subscriptions
WHERE ends_at IS NOT NULL AND date(ends_at) IS NULL
UNION ALL
SELECT 'agent_subscription_instances',id,'valid_from',valid_from
FROM agent_subscription_instances
WHERE valid_from IS NULL OR date(valid_from) IS NULL
UNION ALL
SELECT 'agent_subscription_instances',id,'ends_at',ends_at
FROM agent_subscription_instances
WHERE ends_at IS NOT NULL AND date(ends_at) IS NULL
UNION ALL
SELECT 'agent_subscription_instance_identities',id,'valid_from',valid_from
FROM agent_subscription_instance_identities
WHERE valid_from IS NULL OR date(valid_from) IS NULL
UNION ALL
SELECT 'agent_subscription_bindings',id,'valid_from',valid_from
FROM agent_subscription_bindings
WHERE valid_from IS NULL OR date(valid_from) IS NULL
UNION ALL
SELECT 'agent_subscription_bindings',id,'ends_at',ends_at
FROM agent_subscription_bindings
WHERE ends_at IS NOT NULL AND date(ends_at) IS NULL
UNION ALL
SELECT 'agent_subscription_rate_events',id,'effective_at',effective_at
FROM agent_subscription_rate_events
WHERE effective_at IS NULL OR date(effective_at) IS NULL
UNION ALL
SELECT 'agent_subscription_period_charges',id,'finalized_at',finalized_at
FROM agent_subscription_period_charges
WHERE finalized_at IS NOT NULL AND date(finalized_at) IS NULL
UNION ALL
SELECT 'agent_subscription_charge_allocations',period_charge_id,
       'finalized_at',finalized_at
FROM agent_subscription_charge_allocations
WHERE finalized_at IS NOT NULL AND date(finalized_at) IS NULL
UNION ALL
SELECT 'billing_export_events',id,'frozen_at',frozen_at
FROM billing_export_events
WHERE frozen_at IS NULL OR date(frozen_at) IS NULL;
CREATE TEMP TABLE v21_date_guard(id INTEGER);
CREATE TEMP TRIGGER v21_abort_invalid_agent_dates
BEFORE INSERT ON v21_date_guard
WHEN EXISTS (SELECT 1 FROM v21_invalid_agent_dates)
BEGIN
  SELECT RAISE(ABORT, 'invalid Agent billing date in V2.0 database');
END;
INSERT INTO v21_date_guard VALUES (1);
DROP TRIGGER v21_abort_invalid_agent_dates;
DROP TABLE v21_date_guard;
DROP TABLE v21_invalid_agent_dates;

-- The V2 identity triggers copied exact configuration timestamps into the
-- historical identity tables.  Identity rows remain stable, but their
-- operation time is not part of the Agent billing contract.
DROP TRIGGER IF EXISTS agent_subscriptions_identity_ai;
DROP TRIGGER IF EXISTS agent_subscriptions_identity_au;
DROP TRIGGER IF EXISTS agent_subscription_instances_identity_ai;
DROP TRIGGER IF EXISTS agent_subscription_instances_identity_au;
DROP TRIGGER IF EXISTS config_agent_subscription_rates_ai;
DROP TRIGGER IF EXISTS config_agent_subscription_rates_au;
DROP TRIGGER IF EXISTS config_agent_subscription_rates_ad;
DROP TRIGGER IF EXISTS config_agent_bindings_ai;
DROP TRIGGER IF EXISTS config_agent_bindings_au;
DROP TRIGGER IF EXISTS config_agent_bindings_ad;

ALTER TABLE agent_subscription_identities DROP COLUMN created_at;
ALTER TABLE agent_subscription_identities DROP COLUMN updated_at;
ALTER TABLE agent_subscription_instance_identities DROP COLUMN created_at;
ALTER TABLE agent_subscription_instance_identities DROP COLUMN updated_at;

ALTER TABLE agent_subscriptions RENAME COLUMN ends_at TO ends_on;
ALTER TABLE agent_subscriptions DROP COLUMN created_at;
ALTER TABLE agent_subscriptions DROP COLUMN updated_at;

ALTER TABLE agent_subscription_instances RENAME COLUMN ends_at TO ends_on;
ALTER TABLE agent_subscription_instances DROP COLUMN created_at;
ALTER TABLE agent_subscription_instances DROP COLUMN updated_at;

UPDATE agent_subscriptions SET valid_from=date(valid_from);
UPDATE agent_subscription_instances SET valid_from=date(valid_from);
UPDATE agent_subscription_instance_identities SET valid_from=date(valid_from);

-- A binding can have several date intervals over its lifetime.  A future
-- ends_on is retained only for an explicit end_of_period schedule; immediate
-- deletion removes the live row after the current charge is materialized.
DROP INDEX IF EXISTS idx_agent_subscription_bindings_software;
DROP INDEX IF EXISTS idx_agent_subscription_bindings_subscription;
ALTER TABLE agent_subscription_bindings
    RENAME TO agent_subscription_bindings_v20;
CREATE TABLE agent_subscription_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    ends_on TEXT,
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    UNIQUE(subscription_id, software_id, valid_from)
);
INSERT INTO agent_subscription_bindings
    (id,subscription_id,software_id,valid_from,ends_on)
SELECT id,subscription_id,software_id,date(valid_from),
       CASE WHEN ends_at IS NULL THEN NULL
            WHEN strftime('%H:%M:%S',ends_at)='23:59:59'
              THEN date(ends_at,'+1 day')
            ELSE date(ends_at) END
FROM agent_subscription_bindings_v20;
DROP TABLE agent_subscription_bindings_v20;
CREATE INDEX idx_agent_subscription_bindings_software
    ON agent_subscription_bindings(software_id, valid_from, ends_on);
CREATE INDEX idx_agent_subscription_bindings_subscription
    ON agent_subscription_bindings(subscription_id, valid_from, ends_on);

-- One effective price exists per instance and UTC date.  If V2.0 contains
-- duplicate same-day rows, the highest existing id is the last persisted
-- write and is retained.
DROP INDEX IF EXISTS idx_agent_subscription_rates;
ALTER TABLE agent_subscription_rate_events
    RENAME TO agent_subscription_rate_events_v20;
CREATE TABLE agent_subscription_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    recurring_price REAL NOT NULL,
    effective_on TEXT NOT NULL,
    FOREIGN KEY (instance_id) REFERENCES agent_subscription_instances(id),
    UNIQUE(instance_id, effective_on)
);
INSERT INTO agent_subscription_rate_events
    (id,instance_id,recurring_price,effective_on)
SELECT id,instance_id,recurring_price,date(effective_at)
FROM agent_subscription_rate_events_v20 r
WHERE id=(SELECT max(r2.id) FROM agent_subscription_rate_events_v20 r2
          WHERE r2.instance_id=r.instance_id
            AND date(r2.effective_at)=date(r.effective_at));
DROP TABLE agent_subscription_rate_events_v20;
CREATE INDEX idx_agent_subscription_rates
    ON agent_subscription_rate_events(instance_id, effective_on, id);

-- Agent charge finalization retains the immutable calendar day and explicit
-- state, but no operation timestamp or generated timestamp.
ALTER TABLE agent_subscription_period_charges
    RENAME TO agent_subscription_period_charges_v20;
CREATE TABLE agent_subscription_period_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    recurring_charge REAL NOT NULL,
    currency TEXT NOT NULL,
    normalized_recurring_cost REAL,
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    is_finalized INTEGER NOT NULL DEFAULT 0 CHECK (is_finalized IN (0,1)),
    finalized_on TEXT,
    subscription_uuid_snapshot TEXT,
    instance_uuid_snapshot TEXT,
    subscription_name_snapshot TEXT,
    instance_label_snapshot TEXT,
    UNIQUE(instance_id, period_start)
);
INSERT INTO agent_subscription_period_charges
    (id,instance_id,subscription_id,period_start,period_end,recurring_charge,currency,
     normalized_recurring_cost,base_currency,fx_rate_date,is_finalized,finalized_on,
     subscription_uuid_snapshot,instance_uuid_snapshot,subscription_name_snapshot,
     instance_label_snapshot)
SELECT id,instance_id,subscription_id,period_start,period_end,recurring_charge,currency,
       normalized_recurring_cost,base_currency,fx_rate_date,
       CASE WHEN finalized_at IS NULL THEN 0 ELSE 1 END,
       CASE WHEN finalized_at IS NULL THEN NULL ELSE date(finalized_at) END,
       subscription_uuid_snapshot,instance_uuid_snapshot,subscription_name_snapshot,
       instance_label_snapshot
FROM agent_subscription_period_charges_v20;
DROP TABLE agent_subscription_period_charges_v20;
CREATE INDEX idx_agent_subscription_period_charges
    ON agent_subscription_period_charges(instance_id, period_start);

ALTER TABLE agent_subscription_charge_allocations
    RENAME TO agent_subscription_charge_allocations_v20;
CREATE TABLE agent_subscription_charge_allocations (
    period_charge_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    is_finalized INTEGER NOT NULL DEFAULT 0 CHECK (is_finalized IN (0,1)),
    finalized_on TEXT,
    PRIMARY KEY (period_charge_id, software_id)
);
INSERT INTO agent_subscription_charge_allocations
    (period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
     currency,base_currency,fx_rate_date,is_finalized,finalized_on)
SELECT period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
       currency,base_currency,fx_rate_date,
       CASE WHEN finalized_at IS NULL THEN 0 ELSE 1 END,
       CASE WHEN finalized_at IS NULL THEN NULL ELSE date(finalized_at) END
FROM agent_subscription_charge_allocations_v20;
DROP TABLE agent_subscription_charge_allocations_v20;
CREATE INDEX idx_agent_charge_allocations_software
    ON agent_subscription_charge_allocations(software_id, period_charge_id);

-- Before adding the source-key uniqueness boundary, reject rows that describe
-- the same immutable source with different accounting payloads.  Exact
-- duplicate rows are collapsed below, retaining the smallest event id.
CREATE TEMP TABLE v21_event_conflicts AS
SELECT DISTINCT a.source_table,a.source_key
FROM billing_export_events a
JOIN billing_export_events b
  ON b.source_table=a.source_table AND b.source_key=a.source_key AND b.id>a.id
WHERE a.event_kind IS NOT b.event_kind
   OR a.account_id IS NOT b.account_id
   OR a.account_uuid IS NOT b.account_uuid
   OR a.account_name IS NOT b.account_name
   OR a.account_kind IS NOT b.account_kind
   OR a.month IS NOT b.month
   OR a.period_start IS NOT b.period_start
   OR a.billing_unit_id IS NOT b.billing_unit_id
   OR a.recurring_charge IS NOT b.recurring_charge
   OR a.normalized_recurring_cost IS NOT b.normalized_recurring_cost
   OR a.currency IS NOT b.currency
   OR a.base_currency IS NOT b.base_currency
   OR a.fx_rate_date IS NOT b.fx_rate_date
   OR date(a.frozen_at) IS NOT date(b.frozen_at);
CREATE TEMP TABLE v21_migration_guard(id INTEGER);
CREATE TEMP TRIGGER v21_abort_event_conflicts
BEFORE INSERT ON v21_migration_guard
WHEN EXISTS (SELECT 1 FROM v21_event_conflicts)
BEGIN
  SELECT RAISE(ABORT, 'conflicting billing export source payload');
END;
INSERT INTO v21_migration_guard VALUES (1);

DELETE FROM billing_export_events
WHERE id NOT IN (
    SELECT min(id) FROM billing_export_events
    GROUP BY source_table,source_key
);

DROP INDEX IF EXISTS idx_billing_export_events_account;
DROP INDEX IF EXISTS idx_billing_export_events_period_start;
DROP INDEX IF EXISTS idx_billing_export_events_stream;
ALTER TABLE billing_export_events RENAME TO billing_export_events_v20;
CREATE TABLE billing_export_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('proxy','agent')),
    source_table TEXT NOT NULL,
    source_key TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    account_uuid TEXT,
    account_name TEXT NOT NULL,
    account_kind TEXT NOT NULL CHECK (account_kind IN ('proxy','agent','legacy')),
    month TEXT NOT NULL,
    billing_unit_id TEXT NOT NULL,
    recurring_charge REAL NOT NULL,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    frozen_on TEXT NOT NULL,
    period_start TEXT,
    UNIQUE(source_table, source_key)
);
INSERT INTO billing_export_events
    (id,event_key,event_kind,source_table,source_key,account_id,account_uuid,
     account_name,account_kind,month,period_start,billing_unit_id,recurring_charge,
     normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_on)
SELECT id,event_key,event_kind,source_table,source_key,account_id,account_uuid,
       account_name,account_kind,month,period_start,billing_unit_id,recurring_charge,
       normalized_recurring_cost,currency,base_currency,fx_rate_date,date(frozen_at)
FROM billing_export_events_v20;
DROP TABLE billing_export_events_v20;
CREATE INDEX idx_billing_export_events_account
    ON billing_export_events(account_id, month, billing_unit_id);
CREATE INDEX idx_billing_export_events_period_start
    ON billing_export_events(period_start, id);
CREATE INDEX idx_billing_export_events_stream
    ON billing_export_events(id, event_kind);

DROP TABLE v21_migration_guard;
DROP TABLE v21_event_conflicts;

CREATE TRIGGER agent_subscriptions_identity_ai
AFTER INSERT ON agent_subscriptions
WHEN NOT EXISTS (SELECT 1 FROM agent_subscription_identities WHERE id=NEW.id)
BEGIN
  INSERT INTO agent_subscription_identities(id,uuid,name,currency)
  VALUES(NEW.id,NEW.uuid,NEW.name,NEW.currency);
END;
CREATE TRIGGER agent_subscriptions_identity_au
AFTER UPDATE OF uuid,name,currency ON agent_subscriptions
WHEN EXISTS (SELECT 1 FROM agent_subscription_identities WHERE id=NEW.id)
BEGIN
  UPDATE agent_subscription_identities
  SET uuid=NEW.uuid,name=NEW.name,currency=NEW.currency
  WHERE id=NEW.id;
END;
CREATE TRIGGER agent_subscription_instances_identity_ai
AFTER INSERT ON agent_subscription_instances
WHEN NOT EXISTS (SELECT 1 FROM agent_subscription_instance_identities WHERE id=NEW.id)
BEGIN
  INSERT INTO agent_subscription_instance_identities
      (id,uuid,subscription_id,label,valid_from)
  VALUES(NEW.id,NEW.uuid,NEW.subscription_id,NEW.label,NEW.valid_from);
END;
CREATE TRIGGER agent_subscription_instances_identity_au
AFTER UPDATE OF uuid,subscription_id,label,valid_from
ON agent_subscription_instances
WHEN EXISTS (SELECT 1 FROM agent_subscription_instance_identities WHERE id=NEW.id)
BEGIN
  UPDATE agent_subscription_instance_identities
  SET uuid=NEW.uuid,subscription_id=NEW.subscription_id,label=NEW.label,
      valid_from=NEW.valid_from
  WHERE id=NEW.id;
END;

CREATE TRIGGER config_agent_subscription_rates_ai AFTER INSERT ON agent_subscription_rate_events BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_subscription_rates_au AFTER UPDATE ON agent_subscription_rate_events BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_subscription_rates_ad AFTER DELETE ON agent_subscription_rate_events BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_bindings_ai AFTER INSERT ON agent_subscription_bindings BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_bindings_au AFTER UPDATE ON agent_subscription_bindings BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_bindings_ad AFTER DELETE ON agent_subscription_bindings BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

PRAGMA foreign_keys=ON;
