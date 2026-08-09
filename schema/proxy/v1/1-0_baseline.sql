-- Proxy V1.0: normalized identities, routing, credentials and billing.

PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE config_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO config_state(id,generation) VALUES(1,1);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    valid_from TEXT,
    disabled_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE upstreams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_format TEXT NOT NULL DEFAULT 'openai',
    auth_scheme TEXT NOT NULL DEFAULT 'bearer',
    endpoint_path TEXT,
    max_concurrency INTEGER NOT NULL DEFAULT 0 CHECK (max_concurrency >= 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX idx_upstreams_account ON upstreams(account_id, enabled);

CREATE TABLE route_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    account_id INTEGER,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE route_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_set_id INTEGER NOT NULL,
    model_pattern TEXT NOT NULL DEFAULT '*',
    priority INTEGER NOT NULL DEFAULT 0,
    upstream_id INTEGER NOT NULL,
    target_model TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    FOREIGN KEY (route_set_id) REFERENCES route_sets(id),
    FOREIGN KEY (upstream_id) REFERENCES upstreams(id),
    UNIQUE (route_set_id, model_pattern, priority, upstream_id)
);
CREATE INDEX idx_route_rules_resolve
    ON route_rules(route_set_id, enabled, priority, id);

CREATE TABLE client_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    key_value TEXT NOT NULL UNIQUE,
    label TEXT,
    route_set_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_used_at TEXT,
    deleted_at TEXT,
    FOREIGN KEY (route_set_id) REFERENCES route_sets(id)
);
CREATE INDEX idx_client_keys_route_set ON client_keys(route_set_id);

CREATE TABLE upstream_credentials (
    uuid TEXT PRIMARY KEY,
    runtime_id INTEGER NOT NULL UNIQUE,
    upstream_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    key_masked TEXT NOT NULL,
    valid_from TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    disabled_at TEXT,
    deleted_at TEXT,
    FOREIGN KEY (upstream_id) REFERENCES upstreams(id),
    UNIQUE (upstream_id, uuid)
);
CREATE INDEX idx_credentials_upstream
    ON upstream_credentials(upstream_id, position, uuid);

CREATE TABLE upstream_secrets (
    credential_uuid TEXT PRIMARY KEY,
    secret_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (credential_uuid) REFERENCES upstream_credentials(uuid)
);

CREATE TABLE account_importers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    importer_kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    cursor_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    UNIQUE(account_id, importer_kind)
);

CREATE TABLE billing_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    charge_type TEXT NOT NULL CHECK (charge_type IN ('metered','recurring')),
    billing_scope TEXT NOT NULL CHECK (billing_scope IN ('account','credential')),
    currency TEXT NOT NULL DEFAULT 'CNY',
    billing_anchor_day INTEGER NOT NULL DEFAULT 1 CHECK (billing_anchor_day BETWEEN 1 AND 31),
    cancellation_policy TEXT NOT NULL DEFAULT 'period_end'
        CHECK (cancellation_policy IN ('immediate','period_end')),
    cooldown_policy_json TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX idx_contracts_account_period
    ON billing_contracts(account_id, valid_from, valid_until);

CREATE TABLE billing_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    recurring_price REAL NOT NULL,
    effective_at TEXT NOT NULL,
    effective_rule TEXT NOT NULL DEFAULT 'immediate'
        CHECK (effective_rule IN ('immediate','next_period')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (contract_id) REFERENCES billing_contracts(id),
    UNIQUE(contract_id, effective_at, effective_rule)
);

CREATE TABLE billing_period_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    credential_uuid TEXT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    recurring_charge REAL NOT NULL,
    currency TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (contract_id) REFERENCES billing_contracts(id),
    FOREIGN KEY (credential_uuid) REFERENCES upstream_credentials(uuid),
    UNIQUE(contract_id, credential_uuid, period_start)
);

CREATE TABLE pricing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    UNIQUE(model_pattern, priority)
);
CREATE INDEX idx_pricing_rules_priority ON pricing_rules(enabled, priority, id);

CREATE TABLE pricing_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_rule_id INTEGER NOT NULL,
    input_price REAL NOT NULL,
    cache_read_price REAL NOT NULL,
    output_price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    FOREIGN KEY (pricing_rule_id) REFERENCES pricing_rules(id)
);
CREATE INDEX idx_pricing_rates_period
    ON pricing_rates(pricing_rule_id, valid_from, valid_until);

CREATE TABLE pricing_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_rate_id INTEGER NOT NULL,
    start_minute INTEGER NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
    end_minute INTEGER NOT NULL CHECK (end_minute BETWEEN 0 AND 1440),
    multiplier REAL NOT NULL CHECK (multiplier >= 0),
    FOREIGN KEY (pricing_rate_id) REFERENCES pricing_rates(id)
);

CREATE TABLE fx_rates (
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL NOT NULL CHECK (rate > 0),
    PRIMARY KEY(base_currency, quote_currency, date)
);

CREATE TABLE request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL DEFAULT 'proxy' CHECK (source_kind IN ('proxy','import')),
    account_id INTEGER,
    route_set_id INTEGER,
    client_key_id INTEGER,
    upstream_key_id INTEGER,
    credential_uuid TEXT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    equivalent_cost REAL NOT NULL DEFAULT 0,
    api_cost REAL GENERATED ALWAYS AS (equivalent_cost) VIRTUAL,
    billed_usage_cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY',
    is_streaming INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    queue_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ttft_ms INTEGER NOT NULL DEFAULT 0,
    generation_ms INTEGER NOT NULL DEFAULT 0,
    output_tps REAL NOT NULL DEFAULT 0,
    upstream_ttft_ms INTEGER NOT NULL DEFAULT 0,
    upstream_duration_ms INTEGER NOT NULL DEFAULT 0,
    accounting_ms INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    fallback_count INTEGER NOT NULL DEFAULT 0,
    requested_at TEXT NOT NULL,
    local_key_id INTEGER GENERATED ALWAYS AS (client_key_id) VIRTUAL,
    cost_frozen INTEGER GENERATED ALWAYS AS (1) VIRTUAL,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (route_set_id) REFERENCES route_sets(id),
    FOREIGN KEY (client_key_id) REFERENCES client_keys(id),
    FOREIGN KEY (credential_uuid) REFERENCES upstream_credentials(uuid)
);
CREATE INDEX idx_request_log_time_id ON request_log(requested_at DESC, id DESC);
CREATE INDEX idx_request_log_account_time ON request_log(account_id, requested_at);

-- Importers submit the same UsageEvent shape as the proxy but do not carry a
-- C++ price snapshot.  Resolve the historical rate inside SQLite so every
-- importer shares the proxy's requested_at/rate/slot/FX semantics.
CREATE TRIGGER price_imported_usage AFTER INSERT ON request_log
WHEN NEW.source_kind='import'
 AND NEW.equivalent_cost=0
 AND NEW.prompt_tokens+NEW.completion_tokens>0
BEGIN
  UPDATE request_log SET equivalent_cost=COALESCE((
    SELECT (
      (max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)*r.input_price+
      (NEW.cache_read_tokens/1000000.0)*r.cache_read_price+
      (NEW.completion_tokens/1000000.0)*r.output_price
    )*COALESCE((
      SELECT ps.multiplier FROM pricing_slots ps
      WHERE ps.pricing_rate_id=r.id AND (
        (ps.start_minute<=ps.end_minute AND
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute AND
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute) OR
        (ps.start_minute>ps.end_minute AND (
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute OR
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute))
      ) ORDER BY ps.id LIMIT 1
    ),1.0)*CASE WHEN r.currency='USD' THEN COALESCE((
      SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency='USD'
      AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
      ORDER BY fx.date DESC LIMIT 1
    ),1.0) ELSE 1.0 END
    FROM pricing_rules pr JOIN pricing_rates r ON r.pricing_rule_id=pr.id
    WHERE pr.enabled=1 AND LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
      AND r.valid_from<=NEW.requested_at
      AND (r.valid_until IS NULL OR r.valid_until>NEW.requested_at)
    ORDER BY pr.priority,pr.id,r.valid_from DESC LIMIT 1
  ),0)
  WHERE id=NEW.id;
  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
    AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
    AND (bc.valid_until IS NULL OR bc.valid_until>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;

CREATE TABLE request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_log_id INTEGER NOT NULL,
    attempt_index INTEGER NOT NULL,
    upstream_id INTEGER,
    credential_uuid TEXT,
    account_id INTEGER,
    upstream_key_id INTEGER,
    status_code INTEGER,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    dns_ms INTEGER NOT NULL DEFAULT 0,
    connect_ms INTEGER NOT NULL DEFAULT 0,
    tls_ms INTEGER NOT NULL DEFAULT 0,
    lease_wait_ms INTEGER NOT NULL DEFAULT 0,
    ttft_ms INTEGER NOT NULL DEFAULT 0,
    is_timeout INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    requested_at TEXT NOT NULL,
    FOREIGN KEY (request_log_id) REFERENCES request_log(id) ON DELETE CASCADE,
    FOREIGN KEY (upstream_id) REFERENCES upstreams(id),
    FOREIGN KEY (credential_uuid) REFERENCES upstream_credentials(uuid),
    UNIQUE(request_log_id, attempt_index)
);
CREATE INDEX idx_request_attempts_time ON request_attempts(requested_at);

CREATE TABLE proxy_timeout_config (
    endpoint_kind TEXT PRIMARY KEY,
    streaming_first_byte_timeout INTEGER NOT NULL DEFAULT 30,
    streaming_idle_timeout INTEGER NOT NULL DEFAULT 120,
    non_streaming_timeout INTEGER NOT NULL DEFAULT 300
);
INSERT INTO proxy_timeout_config(endpoint_kind) VALUES
    ('chat'),('messages'),('responses'),('embeddings'),('models');

CREATE TABLE sync_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE upstream_model_catalog (
    upstream_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    PRIMARY KEY(upstream_id, model_id),
    FOREIGN KEY(upstream_id) REFERENCES upstreams(id)
);

-- Read-only V0 names form the HTTP compatibility adapter. Runtime routing and
-- billing never query these views; V1 writes are translated by ProxyDatabase.
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
UNION ALL
SELECT a.id,a.name,'','openai',a.created_at,'','auto',0,'agent',0,a.deleted_at,
       COALESCE(bc.currency,'CNY'),i.importer_kind,NULL,a.valid_from
FROM account_importers i JOIN accounts a ON a.id=i.account_id
LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL;

CREATE VIEW local_keys AS
SELECT id,key_value,label,route_set_id AS account_id,created_at,last_used_at
FROM client_keys;

CREATE VIEW upstream_keys AS
SELECT c.runtime_id AS id,u.account_id,s.secret_value AS key_value,c.position,
       c.created_at,c.valid_from,c.deleted_at
FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id
JOIN upstream_secrets s ON s.credential_uuid=c.uuid;

CREATE VIEW upstream_keys_cloud AS
SELECT u.account_id,c.key_masked,c.position,c.valid_from,c.deleted_at
FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id;

CREATE VIEW aggregate_entries AS
SELECT rr.id,rr.route_set_id AS account_id,rr.priority AS sort_order,
       rr.model_pattern AS pattern,u.account_id AS upstream_account_id,
       COALESCE(rr.target_model,rr.model_pattern) AS upstream_model
FROM route_rules rr JOIN route_sets rs ON rs.id=rr.route_set_id
JOIN upstreams u ON u.id=rr.upstream_id WHERE rs.account_id IS NULL;

CREATE VIEW account_models AS
SELECT mc.rowid AS id,u.account_id,mc.model_id
FROM upstream_model_catalog mc JOIN upstreams u ON u.id=mc.upstream_id;

CREATE VIEW model_pricing AS
SELECT pr.id,pr.model_pattern,r.input_price,r.output_price,r.currency,
       r.cache_read_price
FROM pricing_rules pr JOIN pricing_rates r ON r.pricing_rule_id=pr.id
WHERE r.valid_until IS NULL;

CREATE VIEW fx_rate AS
SELECT base_currency AS base,quote_currency AS quote,date,rate,
       NULL AS fetched_at FROM fx_rates;

CREATE VIEW plan_billing_config AS
SELECT 1 AS id,'current_period' AS price_change_effective,
       'period_end' AS cancellation_mode;

CREATE VIEW plan_price_history AS
SELECT bre.id,bc.account_id,bre.recurring_price AS monthly_price,
       bre.effective_at AS changed_at,
       CASE bre.effective_rule WHEN 'next_period' THEN 'next_period'
            ELSE 'current_period' END AS effective_mode
FROM billing_rate_events bre JOIN billing_contracts bc ON bc.id=bre.contract_id;

CREATE VIEW sync_config AS SELECT key,value FROM sync_settings;
CREATE TRIGGER sync_config_insert INSTEAD OF INSERT ON sync_config BEGIN
  INSERT INTO sync_settings(key,value) VALUES(NEW.key,NEW.value)
  ON CONFLICT(key) DO UPDATE SET value=excluded.value;
END;
CREATE TRIGGER sync_config_update INSTEAD OF UPDATE ON sync_config BEGIN
  UPDATE sync_settings SET key=NEW.key,value=NEW.value WHERE key=OLD.key;
END;
CREATE TRIGGER sync_config_delete INSTEAD OF DELETE ON sync_config BEGIN
  DELETE FROM sync_settings WHERE key=OLD.key;
END;

-- Every committed configuration mutation advances the snapshot generation.
CREATE TRIGGER config_upstreams_ai AFTER INSERT ON upstreams BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_upstreams_au AFTER UPDATE ON upstreams BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_upstreams_ad AFTER DELETE ON upstreams BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_sets_ai AFTER INSERT ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_sets_au AFTER UPDATE ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_sets_ad AFTER DELETE ON route_sets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_rules_ai AFTER INSERT ON route_rules BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_rules_au AFTER UPDATE ON route_rules BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_route_rules_ad AFTER DELETE ON route_rules BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_client_keys_ai AFTER INSERT ON client_keys BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_client_keys_au AFTER UPDATE ON client_keys BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_client_keys_ad AFTER DELETE ON client_keys BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_credentials_ai AFTER INSERT ON upstream_credentials BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_credentials_au AFTER UPDATE ON upstream_credentials BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_credentials_ad AFTER DELETE ON upstream_credentials BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_secrets_ai AFTER INSERT ON upstream_secrets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_secrets_au AFTER UPDATE ON upstream_secrets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_secrets_ad AFTER DELETE ON upstream_secrets BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
