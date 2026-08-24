-- V1.8: agents share the global account id namespace and subscriptions have
-- independently billable instances.
--
-- This migration is deliberately data preserving.  The old V1.6 agent tables
-- are copied into the instance tables before they are removed; imported
-- request rows are reattached to the new account id and parser cursors are
-- copied to the rebuilt software table.

ALTER TABLE accounts ADD COLUMN account_kind TEXT NOT NULL DEFAULT 'proxy'
    CHECK (account_kind IN ('proxy','agent','legacy'));

CREATE INDEX idx_accounts_kind_lifecycle
    ON accounts(account_kind, lifecycle_state, id);

-- Reserve ids that do not collide with accounts or route-set ids.  In the
-- normal case the old software id is retained, which keeps the dashboard
-- archive and request history stable.  A colliding old id is moved above the
-- complete V1 id range.
CREATE TEMP TABLE agent_id_map (
    old_id INTEGER PRIMARY KEY,
    new_id INTEGER NOT NULL UNIQUE
);

INSERT INTO agent_id_map(old_id,new_id)
SELECT s.id,s.id
FROM agent_software s
WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id=s.id)
  AND NOT EXISTS (SELECT 1 FROM route_sets rs WHERE rs.id=s.id);

INSERT INTO agent_id_map(old_id,new_id)
SELECT s.id,base.max_id + ROW_NUMBER() OVER (ORDER BY s.id)
FROM agent_software s
JOIN (
    SELECT max(value) AS max_id FROM (
        SELECT COALESCE(MAX(id),0) AS value FROM accounts
        UNION ALL SELECT COALESCE(MAX(id),0) FROM route_sets
        UNION ALL SELECT COALESCE(MAX(id),0) FROM agent_software
    )
) base
WHERE NOT EXISTS (SELECT 1 FROM agent_id_map m WHERE m.old_id=s.id);

-- A disabled importer account is a historical upstream shell.  It remains in
-- the database for old foreign keys, but it is no longer a routable proxy.
UPDATE accounts SET account_kind='legacy'
WHERE id IN (
    SELECT account_id FROM account_importers
    WHERE enabled=0 AND importer_kind IS NOT NULL
);

-- The agent identity is now an account row.  An INSERT conflict is intentional:
-- it prevents silently merging two configured identities with the same UUID or
-- name and leaves the source database untouched for manual repair.
INSERT INTO accounts
    (id,uuid,name,lifecycle_state,valid_from,disabled_at,deleted_at,created_at,updated_at,account_kind)
SELECT m.new_id,s.uuid,s.name,
       CASE WHEN s.enabled=1 THEN 'active' ELSE 'disabled' END,
       s.created_at,
       CASE WHEN s.enabled=1 THEN NULL ELSE s.updated_at END,
       NULL,s.created_at,s.updated_at,'agent'
FROM agent_software s JOIN agent_id_map m ON m.old_id=s.id;

-- Request rows used the software extension column while V1.6 was active.
-- Keep that column for parser/idempotency compatibility, but make the stable
-- account_id the only accounting identity.
UPDATE request_log
SET account_id=(SELECT new_id FROM agent_id_map WHERE old_id=request_log.agent_software_id),
    agent_software_id=(SELECT new_id FROM agent_id_map WHERE old_id=request_log.agent_software_id)
WHERE agent_software_id IN (SELECT old_id FROM agent_id_map);

CREATE TABLE agent_software_v18 (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    agent_kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE agent_software_runtime_v18 (
    software_id INTEGER PRIMARY KEY,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    status_json TEXT NOT NULL DEFAULT '{}',
    last_scan_at TEXT,
    last_error TEXT,
    FOREIGN KEY (software_id) REFERENCES agent_software_v18(id) ON DELETE CASCADE
);

INSERT INTO agent_software_v18
    (id,uuid,name,agent_kind,config_json,enabled,created_at,updated_at)
SELECT m.new_id,s.uuid,s.name,s.agent_kind,s.config_json,s.enabled,s.created_at,s.updated_at
FROM agent_software s JOIN agent_id_map m ON m.old_id=s.id;

INSERT INTO agent_software_runtime_v18
    (software_id,cursor_json,status_json,last_scan_at,last_error)
SELECT m.new_id,r.cursor_json,r.status_json,r.last_scan_at,r.last_error
FROM agent_software_runtime r JOIN agent_id_map m ON m.old_id=r.software_id;

DROP TRIGGER config_agent_software_ai;
DROP TRIGGER config_agent_software_au;
DROP TRIGGER config_agent_software_ad;
DROP TABLE agent_software_runtime;
DROP TABLE agent_software;
ALTER TABLE agent_software_v18 RENAME TO agent_software;
ALTER TABLE agent_software_runtime_v18 RENAME TO agent_software_runtime;

CREATE INDEX idx_agent_software_kind_enabled
    ON agent_software(agent_kind, enabled, id);

CREATE TRIGGER config_agent_software_ai AFTER INSERT ON agent_software BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_software_au AFTER UPDATE ON agent_software BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_software_ad AFTER DELETE ON agent_software BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

-- V1.6 had one rate stream and one charge stream directly under a parent
-- subscription.  Turn that stream into the first instance without changing
-- any recorded price, FX date, provisional value or finalized timestamp.
CREATE TABLE agent_subscription_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    subscription_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '默认实例',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    UNIQUE(subscription_id, label)
);
CREATE INDEX idx_agent_subscription_instances_parent
    ON agent_subscription_instances(subscription_id, lifecycle_state, valid_from, id);

CREATE TEMP TABLE agent_instance_map (
    subscription_id INTEGER PRIMARY KEY,
    instance_id INTEGER NOT NULL UNIQUE
);

INSERT INTO agent_subscription_instances
    (uuid,subscription_id,label,valid_from,valid_until,lifecycle_state,created_at,updated_at)
SELECT lower(hex(randomblob(16))),id,'默认实例',valid_from,valid_until,lifecycle_state,
       created_at,updated_at
FROM agent_subscriptions;

INSERT INTO agent_instance_map(subscription_id,instance_id)
SELECT subscription_id,id FROM agent_subscription_instances;

CREATE TABLE agent_subscription_instance_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    recurring_price REAL NOT NULL,
    effective_at TEXT NOT NULL,
    effective_rule TEXT NOT NULL DEFAULT 'immediate'
        CHECK (effective_rule IN ('immediate','next_period')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (instance_id) REFERENCES agent_subscription_instances(id),
    UNIQUE(instance_id, effective_at, effective_rule)
);
INSERT INTO agent_subscription_instance_rate_events
    (instance_id,recurring_price,effective_at,effective_rule,created_at)
SELECT m.instance_id,r.recurring_price,r.effective_at,r.effective_rule,r.created_at
FROM agent_subscription_rate_events r
JOIN agent_instance_map m ON m.subscription_id=r.subscription_id;

CREATE TABLE agent_subscription_instance_period_charges (
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
    FOREIGN KEY (instance_id) REFERENCES agent_subscription_instances(id),
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    UNIQUE(instance_id, period_start)
);
INSERT INTO agent_subscription_instance_period_charges
    (instance_id,subscription_id,period_start,period_end,recurring_charge,currency,
     normalized_recurring_cost,base_currency,fx_rate_date,finalized_at,generated_at)
SELECT m.instance_id,c.subscription_id,c.period_start,c.period_end,c.recurring_charge,c.currency,
       c.normalized_recurring_cost,c.base_currency,c.fx_rate_date,c.finalized_at,c.generated_at
FROM agent_subscription_period_charges c
JOIN agent_instance_map m ON m.subscription_id=c.subscription_id;

DROP TABLE agent_subscription_period_charges;
DROP TABLE agent_subscription_rate_events;
ALTER TABLE agent_subscription_instance_rate_events
    RENAME TO agent_subscription_rate_events;
ALTER TABLE agent_subscription_instance_period_charges
    RENAME TO agent_subscription_period_charges;
CREATE INDEX idx_agent_subscription_rates
    ON agent_subscription_rate_events(instance_id, effective_at);
CREATE INDEX idx_agent_subscription_period_charges
    ON agent_subscription_period_charges(instance_id, period_start);

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

CREATE TABLE agent_subscription_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    valid_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    valid_until TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','deleted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    FOREIGN KEY (software_id) REFERENCES agent_software(id),
    UNIQUE(subscription_id, software_id)
);
CREATE INDEX idx_agent_subscription_bindings_software
    ON agent_subscription_bindings(software_id, lifecycle_state, valid_from, valid_until);
CREATE INDEX idx_agent_subscription_bindings_subscription
    ON agent_subscription_bindings(subscription_id, lifecycle_state, valid_from, valid_until);

CREATE TRIGGER config_agent_instances_ai AFTER INSERT ON agent_subscription_instances BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_instances_au AFTER UPDATE ON agent_subscription_instances BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_instances_ad AFTER DELETE ON agent_subscription_instances BEGIN
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

DROP VIEW upstream_accounts;
CREATE VIEW upstream_accounts AS
SELECT rs.id AS id,rs.name,u.base_url,u.api_format,rs.created_at,
       u.endpoint_path,u.auth_scheme AS auth_header,
       CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END AS is_aggregate,
       CASE WHEN bc.charge_type='recurring' THEN 'plan' ELSE 'api' END AS account_type,
       u.max_concurrency,a.deleted_at,bc.currency,NULL AS agent_kind,
       NULL AS deferred_cleanup_mode,a.valid_from
FROM route_sets rs
LEFT JOIN accounts a ON a.id=rs.account_id AND a.account_kind='proxy'
LEFT JOIN upstreams u ON u.account_id=a.id
LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL
WHERE rs.enabled=1 AND (rs.account_id IS NULL OR a.lifecycle_state='active');

DROP TABLE agent_id_map;
DROP TABLE agent_instance_map;
