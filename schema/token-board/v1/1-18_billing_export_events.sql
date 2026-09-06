-- V1.18: immutable billing export events with an independent dashboard stream.
-- Existing finalized rows are treated as already delivered.  Their export
-- history predates this stream and cannot be inferred without replaying old
-- dashboard artifacts; future finalized rows get ids above this baseline.

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
    frozen_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_billing_export_events_stream
    ON billing_export_events(id, event_kind);
CREATE INDEX idx_billing_export_events_account
    ON billing_export_events(account_id, month, billing_unit_id);

INSERT OR IGNORE INTO billing_export_events
    (event_key,event_kind,source_table,source_key,account_id,account_uuid,
     account_name,account_kind,month,billing_unit_id,recurring_charge,
     normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_at)
SELECT
    'proxy:' || COALESCE(c.billing_unit_id,c.credential_uuid,
                         'legacy:' || COALESCE(c.contract_uuid_snapshot,
                                               CAST(c.contract_id AS TEXT))) || ':' || c.period_start,
    'proxy', 'billing_period_charges', CAST(c.id AS TEXT), ai.id,ai.uuid,
    ai.name,ai.account_kind,substr(c.period_start,1,7),
    COALESCE(c.billing_unit_id,c.credential_uuid,
             'contract:' || COALESCE(c.contract_uuid_snapshot,CAST(c.contract_id AS TEXT))),
    c.recurring_charge,c.normalized_recurring_cost,c.currency,
    COALESCE(c.base_currency,'CNY'),c.fx_rate_date,c.finalized_at
FROM billing_period_charges c
JOIN account_identities ai
  ON ai.id=COALESCE(c.account_identity_id,
                    (SELECT bc.account_id FROM billing_contracts bc
                     WHERE bc.id=c.contract_id))
WHERE c.finalized_at IS NOT NULL AND ai.account_kind='proxy';

INSERT OR IGNORE INTO billing_export_events
    (event_key,event_kind,source_table,source_key,account_id,account_uuid,
     account_name,account_kind,month,billing_unit_id,recurring_charge,
     normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_at)
SELECT
    'agent:' || COALESCE(c.subscription_uuid_snapshot,s.uuid,
                         'subscription:' || CAST(c.subscription_id AS TEXT)) || ':' ||
        COALESCE(ai.uuid,CAST(a.software_id AS TEXT)) || ':' || c.period_start,
    'agent', 'agent_subscription_charge_allocations',
    CAST(a.period_charge_id AS TEXT) || ':' || CAST(a.software_id AS TEXT),
    ai.id,ai.uuid,ai.name,ai.account_kind,substr(c.period_start,1,7),
    'agent-subscription:' || COALESCE(c.subscription_uuid_snapshot,s.uuid,
                                      CAST(c.subscription_id AS TEXT)),
    a.recurring_charge,a.normalized_recurring_cost,a.currency,
    COALESCE(a.base_currency,'CNY'),a.fx_rate_date,a.finalized_at
FROM agent_subscription_charge_allocations a
JOIN agent_subscription_period_charges c ON c.id=a.period_charge_id
LEFT JOIN agent_subscription_instances i ON i.id=c.instance_id
LEFT JOIN agent_subscriptions s
  ON s.id=COALESCE(c.subscription_id,i.subscription_id)
JOIN account_identities ai ON ai.id=a.software_id
WHERE c.finalized_at IS NOT NULL AND a.finalized_at IS NOT NULL
  AND ai.account_kind='agent';

INSERT OR IGNORE INTO sync_state(key,value)
SELECT 'last_exported_billing_event_id',
       CAST(COALESCE(MAX(id),0) AS TEXT)
FROM billing_export_events;
