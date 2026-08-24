-- V1.6: independent agent subscriptions, software sources and usage metadata.

CREATE TABLE agent_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL DEFAULT 'CNY',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE agent_subscription_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    recurring_price REAL NOT NULL,
    effective_at TEXT NOT NULL,
    effective_rule TEXT NOT NULL DEFAULT 'immediate'
        CHECK (effective_rule IN ('immediate','next_period')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    UNIQUE(subscription_id, effective_at, effective_rule)
);
CREATE INDEX idx_agent_subscription_rates
    ON agent_subscription_rate_events(subscription_id, effective_at);

-- Generated locally and exported to dashboard.db; never copied by proxy config sync.
CREATE TABLE agent_subscription_period_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id),
    UNIQUE(subscription_id, period_start)
);

CREATE TABLE agent_software (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    agent_kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_agent_software_kind_enabled
    ON agent_software(agent_kind, enabled, id);

-- Machine-local parser state.  The configuration itself remains in
-- agent_software.config_json and is included in cloud configuration snapshots.
CREATE TABLE agent_software_runtime (
    software_id INTEGER PRIMARY KEY,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    status_json TEXT NOT NULL DEFAULT '{}',
    last_scan_at TEXT,
    last_error TEXT,
    FOREIGN KEY (software_id) REFERENCES agent_software(id) ON DELETE CASCADE
);

ALTER TABLE request_log ADD COLUMN agent_software_id INTEGER;
ALTER TABLE request_log ADD COLUMN project TEXT;
ALTER TABLE request_log ADD COLUMN session_id TEXT;
CREATE INDEX idx_request_log_agent_time
    ON request_log(agent_software_id, requested_at);

CREATE TRIGGER config_agent_subscriptions_ai AFTER INSERT ON agent_subscriptions BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_subscriptions_au AFTER UPDATE ON agent_subscriptions BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_agent_subscriptions_ad AFTER DELETE ON agent_subscriptions BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
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

-- Agent rows are no longer upstream identities. Keep account_importers only
-- as a source for the one-time migration and never expose it to the proxy.
DROP VIEW upstream_accounts;
CREATE VIEW upstream_accounts AS
SELECT rs.id AS id,rs.name,u.base_url,u.api_format,rs.created_at,
       u.endpoint_path,u.auth_scheme AS auth_header,
       CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END AS is_aggregate,
       CASE WHEN bc.charge_type='recurring' THEN 'plan' ELSE 'api' END AS account_type,
       u.max_concurrency,a.deleted_at,bc.currency,NULL AS agent_kind,
       NULL AS deferred_cleanup_mode,a.valid_from
FROM route_sets rs
LEFT JOIN accounts a ON a.id=rs.account_id
LEFT JOIN upstreams u ON u.account_id=a.id
LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL
WHERE rs.enabled=1 AND (rs.account_id IS NULL OR a.lifecycle_state='active');
