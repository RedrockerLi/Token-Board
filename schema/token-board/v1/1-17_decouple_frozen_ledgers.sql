-- V1.17: frozen financial facts no longer own live subscription parents.
-- The source IDs and UUID snapshots remain for audit/export, but deleting a
-- contract, credential, subscription or instance cannot invalidate charges.

ALTER TABLE billing_period_charges RENAME TO billing_period_charges_v116;
CREATE TABLE billing_period_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    credential_uuid TEXT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    recurring_charge REAL NOT NULL,
    currency TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finalized_at TEXT,
    normalized_recurring_cost REAL,
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    account_identity_id INTEGER,
    contract_uuid_snapshot TEXT,
    billing_unit_id TEXT,
    UNIQUE(contract_id, credential_uuid, period_start)
);
INSERT INTO billing_period_charges
    (id,contract_id,credential_uuid,period_start,period_end,recurring_charge,currency,
     generated_at,finalized_at,normalized_recurring_cost,base_currency,fx_rate_date,
     account_identity_id,contract_uuid_snapshot,billing_unit_id)
SELECT id,contract_id,credential_uuid,period_start,period_end,recurring_charge,currency,
       generated_at,finalized_at,normalized_recurring_cost,base_currency,fx_rate_date,
       account_identity_id,contract_uuid_snapshot,billing_unit_id
FROM billing_period_charges_v116;
DROP TABLE billing_period_charges_v116;
CREATE INDEX idx_billing_period_charges_contract
    ON billing_period_charges(contract_id, period_start);

ALTER TABLE agent_subscription_period_charges
    RENAME TO agent_subscription_period_charges_v116;
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
    finalized_at TEXT,
    generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    subscription_uuid_snapshot TEXT,
    instance_uuid_snapshot TEXT,
    subscription_name_snapshot TEXT,
    instance_label_snapshot TEXT,
    UNIQUE(instance_id, period_start)
);
INSERT INTO agent_subscription_period_charges
    (id,instance_id,subscription_id,period_start,period_end,recurring_charge,currency,
     normalized_recurring_cost,base_currency,fx_rate_date,finalized_at,generated_at,
     subscription_uuid_snapshot,instance_uuid_snapshot,subscription_name_snapshot,
     instance_label_snapshot)
SELECT id,instance_id,subscription_id,period_start,period_end,recurring_charge,currency,
       normalized_recurring_cost,base_currency,fx_rate_date,finalized_at,generated_at,
       subscription_uuid_snapshot,instance_uuid_snapshot,subscription_name_snapshot,
       instance_label_snapshot
FROM agent_subscription_period_charges_v116;
DROP TABLE agent_subscription_period_charges_v116;
CREATE INDEX idx_agent_subscription_period_charges
    ON agent_subscription_period_charges(instance_id, period_start);

-- Rebind the allocation FK to the rebuilt charge table.  SQLite rewrites a
-- referencing FK to the temporary renamed table during ALTER TABLE RENAME;
-- rebuilding this small table avoids leaving a dangling *_v116 reference.
ALTER TABLE agent_subscription_charge_allocations
    RENAME TO agent_subscription_charge_allocations_v116;
CREATE TABLE agent_subscription_charge_allocations (
    period_charge_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    finalized_at TEXT,
    PRIMARY KEY (period_charge_id, software_id),
    FOREIGN KEY (period_charge_id)
        REFERENCES agent_subscription_period_charges(id),
    FOREIGN KEY (software_id) REFERENCES accounts(id)
);
INSERT INTO agent_subscription_charge_allocations
    (period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
     currency,base_currency,fx_rate_date,finalized_at)
SELECT period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
       currency,base_currency,fx_rate_date,finalized_at
FROM agent_subscription_charge_allocations_v116;
DROP TABLE agent_subscription_charge_allocations_v116;
CREATE INDEX idx_agent_charge_allocations_software
    ON agent_subscription_charge_allocations(software_id, period_charge_id);
