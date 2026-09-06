-- V1.20: display names are attributes, never business identity keys.
--
-- The V1.0/V1.6 tables used UNIQUE(name) for convenience.  Names are now
-- deliberately non-unique for every live proxy/Agent configuration table;
-- stable ids/UUIDs remain the only identities used by references and APIs.

-- SQLite validates dependent views while a referenced table is dropped.
-- Recreate these compatibility views after rebuilding their live tables.
DROP VIEW IF EXISTS upstream_accounts;
DROP VIEW IF EXISTS aggregate_entries;

-- Rebuild the shared account table without UNIQUE(name).  Account identity
-- history is preserved separately in account_identities.
CREATE TABLE accounts_v20 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    valid_from TEXT,
    disabled_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    account_kind TEXT NOT NULL DEFAULT 'proxy'
        CHECK (account_kind IN ('proxy','agent','legacy'))
);
INSERT INTO accounts_v20
    (id,uuid,name,lifecycle_state,valid_from,disabled_at,deleted_at,
     created_at,updated_at,account_kind)
SELECT id,uuid,name,lifecycle_state,valid_from,disabled_at,deleted_at,
       created_at,updated_at,account_kind
FROM accounts;
DROP TABLE accounts;
ALTER TABLE accounts_v20 RENAME TO accounts;

CREATE INDEX idx_accounts_kind_lifecycle
    ON accounts(account_kind, lifecycle_state, id);

CREATE TRIGGER config_accounts_ai AFTER INSERT ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_accounts_au AFTER UPDATE ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_accounts_ad AFTER DELETE ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER accounts_identity_ai AFTER INSERT ON accounts
WHEN NOT EXISTS (SELECT 1 FROM account_identities WHERE id=NEW.id)
BEGIN
  INSERT INTO account_identities(id,uuid,name,account_kind,created_at,updated_at)
  VALUES(NEW.id,NEW.uuid,NEW.name,COALESCE(NEW.account_kind,'proxy'),
         NEW.created_at,NEW.updated_at);
END;

CREATE TRIGGER accounts_identity_au
AFTER UPDATE OF name,account_kind,updated_at ON accounts
WHEN EXISTS (SELECT 1 FROM account_identities WHERE id=NEW.id)
BEGIN
  UPDATE account_identities SET name=NEW.name,
    account_kind=COALESCE(NEW.account_kind,'proxy'),updated_at=NEW.updated_at
  WHERE id=NEW.id;
END;

-- Route-set names are also display attributes.  UUID and id remain stable.
CREATE TABLE route_sets_v20 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    account_id INTEGER,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
INSERT INTO route_sets_v20
    (id,uuid,account_id,name,enabled,created_at,updated_at)
SELECT id,uuid,account_id,name,enabled,created_at,updated_at
FROM route_sets;
DROP TABLE route_sets;
ALTER TABLE route_sets_v20 RENAME TO route_sets;

CREATE TRIGGER config_route_sets_ai AFTER INSERT ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_sets_au AFTER UPDATE ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_sets_ad AFTER DELETE ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

-- Agent software names are display attributes as well.  Its shared account
-- identity is already retained by account_identities.
CREATE TABLE agent_software_v20 (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO agent_software_v20
    (id,uuid,name,agent_kind,config_json,enabled,created_at,updated_at)
SELECT id,uuid,name,agent_kind,config_json,enabled,created_at,updated_at
FROM agent_software;
DROP TABLE agent_software;
ALTER TABLE agent_software_v20 RENAME TO agent_software;

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

CREATE VIEW aggregate_entries AS
SELECT rr.id,rr.route_set_id AS account_id,rr.priority AS sort_order,
       rr.model_pattern AS pattern,u.account_id AS upstream_account_id,
       COALESCE(rr.target_model,rr.model_pattern) AS upstream_model
FROM route_rules rr JOIN route_sets rs ON rs.id=rr.route_set_id
JOIN upstreams u ON u.id=rr.upstream_id WHERE rs.account_id IS NULL;

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
