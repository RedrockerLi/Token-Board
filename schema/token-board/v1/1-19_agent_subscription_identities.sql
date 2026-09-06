-- V1.19: separate Agent subscription identities from live configuration.
--
-- A subscription/instance row is a routable/configurable resource, not the
-- owner of a frozen charge.  The identity rows keep the stable UUID, numeric
-- identity and display attributes after the live graph is purged.  Charge
-- rows already carry these numeric identity values and snapshots; V1.17
-- deliberately removed their parent foreign keys.

CREATE TABLE agent_subscription_identities (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO agent_subscription_identities
    (id,uuid,name,currency,created_at,updated_at)
SELECT id,uuid,name,currency,created_at,updated_at
FROM agent_subscriptions;

CREATE TABLE agent_subscription_instance_identities (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    subscription_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id)
        REFERENCES agent_subscription_identities(id)
);

INSERT INTO agent_subscription_instance_identities
    (id,uuid,subscription_id,label,valid_from,created_at,updated_at)
SELECT id,uuid,subscription_id,label,valid_from,created_at,updated_at
FROM agent_subscription_instances;

CREATE INDEX idx_agent_subscription_instance_identities_parent
    ON agent_subscription_instance_identities(subscription_id,id);

-- Rebuild the live parent without making its display name an identity key.
-- The UUID and numeric id remain the machine-stable identities.
CREATE TABLE agent_subscriptions_v19 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO agent_subscriptions_v19
    (id,uuid,name,currency,valid_from,valid_until,lifecycle_state,created_at,updated_at)
SELECT id,uuid,name,currency,valid_from,valid_until,lifecycle_state,created_at,updated_at
FROM agent_subscriptions;
DROP TABLE agent_subscriptions;
ALTER TABLE agent_subscriptions_v19 RENAME TO agent_subscriptions;

CREATE INDEX idx_agent_subscriptions_name
    ON agent_subscriptions(name COLLATE NOCASE);

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

CREATE TRIGGER agent_subscriptions_identity_ai
AFTER INSERT ON agent_subscriptions
WHEN NOT EXISTS (
    SELECT 1 FROM agent_subscription_identities WHERE id=NEW.id
)
BEGIN
  INSERT INTO agent_subscription_identities
      (id,uuid,name,currency,created_at,updated_at)
  VALUES
      (NEW.id,NEW.uuid,NEW.name,NEW.currency,NEW.created_at,NEW.updated_at);
END;

CREATE TRIGGER agent_subscriptions_identity_au
AFTER UPDATE OF uuid,name,currency,updated_at ON agent_subscriptions
WHEN EXISTS (
    SELECT 1 FROM agent_subscription_identities WHERE id=NEW.id
)
BEGIN
  UPDATE agent_subscription_identities
  SET uuid=NEW.uuid,name=NEW.name,currency=NEW.currency,updated_at=NEW.updated_at
  WHERE id=NEW.id;
END;

-- Rebuild live instances without making a display label an identity key.
CREATE TABLE agent_subscription_instances_v19 (
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
    FOREIGN KEY (subscription_id) REFERENCES agent_subscriptions(id)
);
INSERT INTO agent_subscription_instances_v19
    (id,uuid,subscription_id,label,valid_from,valid_until,lifecycle_state,created_at,updated_at)
SELECT id,uuid,subscription_id,label,valid_from,valid_until,lifecycle_state,created_at,updated_at
FROM agent_subscription_instances;
DROP TABLE agent_subscription_instances;
ALTER TABLE agent_subscription_instances_v19 RENAME TO agent_subscription_instances;

CREATE INDEX idx_agent_subscription_instances_parent
    ON agent_subscription_instances(subscription_id, lifecycle_state, valid_from, id);
CREATE INDEX idx_agent_subscription_instances_label
    ON agent_subscription_instances(subscription_id, label COLLATE NOCASE, id);

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

CREATE TRIGGER agent_subscription_instances_identity_ai
AFTER INSERT ON agent_subscription_instances
WHEN NOT EXISTS (
    SELECT 1 FROM agent_subscription_instance_identities WHERE id=NEW.id
)
BEGIN
  INSERT INTO agent_subscription_instance_identities
      (id,uuid,subscription_id,label,valid_from,created_at,updated_at)
  VALUES
      (NEW.id,NEW.uuid,NEW.subscription_id,NEW.label,NEW.valid_from,
       NEW.created_at,NEW.updated_at);
END;

CREATE TRIGGER agent_subscription_instances_identity_au
AFTER UPDATE OF uuid,subscription_id,label,valid_from,updated_at
ON agent_subscription_instances
WHEN EXISTS (
    SELECT 1 FROM agent_subscription_instance_identities WHERE id=NEW.id
)
BEGIN
  UPDATE agent_subscription_instance_identities
  SET uuid=NEW.uuid,subscription_id=NEW.subscription_id,label=NEW.label,
      valid_from=NEW.valid_from,updated_at=NEW.updated_at
  WHERE id=NEW.id;
END;
