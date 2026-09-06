-- V1.16: retain immutable historical account identity separately from the
-- mutable routing/account configuration rows.
--
-- This first step is intentionally additive. Existing runtime foreign keys
-- remain valid until V1.17 decouples frozen ledger rows.

CREATE TABLE account_identities (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    account_kind TEXT NOT NULL
        CHECK (account_kind IN ('proxy','agent','legacy')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO account_identities
    (id,uuid,name,account_kind,created_at,updated_at)
SELECT id,uuid,name,COALESCE(account_kind,'proxy'),created_at,updated_at
FROM accounts;

CREATE INDEX idx_account_identities_kind
    ON account_identities(account_kind,id);

CREATE TRIGGER accounts_identity_ai AFTER INSERT ON accounts
WHEN NOT EXISTS (SELECT 1 FROM account_identities WHERE id=NEW.id)
BEGIN
  INSERT INTO account_identities(id,uuid,name,account_kind,created_at,updated_at)
  VALUES(NEW.id,NEW.uuid,NEW.name,COALESCE(NEW.account_kind,'proxy'),
         NEW.created_at,NEW.updated_at);
END;

CREATE TRIGGER accounts_identity_au AFTER UPDATE OF name,account_kind,updated_at ON accounts
WHEN EXISTS (SELECT 1 FROM account_identities WHERE id=NEW.id)
BEGIN
  UPDATE account_identities SET name=NEW.name,
    account_kind=COALESCE(NEW.account_kind,'proxy'),updated_at=NEW.updated_at
  WHERE id=NEW.id;
END;

ALTER TABLE request_log ADD COLUMN account_identity_id INTEGER;
ALTER TABLE request_log ADD COLUMN billing_unit_id TEXT;
ALTER TABLE request_log ADD COLUMN billing_contract_uuid TEXT;
ALTER TABLE request_log ADD COLUMN billing_anchor_day INTEGER;

UPDATE request_log
SET account_identity_id=account_id
WHERE account_id IS NOT NULL AND account_identity_id IS NULL;

UPDATE request_log
SET billing_contract_uuid=(
        SELECT bc.uuid FROM billing_contracts bc
        WHERE bc.account_id=request_log.account_id
          AND bc.charge_type='recurring'
          AND bc.valid_from<=request_log.requested_at
          AND (bc.valid_until IS NULL OR bc.valid_until>request_log.requested_at)
        ORDER BY bc.id DESC LIMIT 1
    ),
    billing_anchor_day=COALESCE((
        SELECT bc.billing_anchor_day FROM billing_contracts bc
        WHERE bc.account_id=request_log.account_id
          AND bc.charge_type='recurring'
          AND bc.valid_from<=request_log.requested_at
          AND (bc.valid_until IS NULL OR bc.valid_until>request_log.requested_at)
        ORDER BY bc.id DESC LIMIT 1
    ), billing_anchor_day)
WHERE billing_contract_uuid IS NULL;

UPDATE request_log
SET billing_unit_id=CASE
    WHEN credential_uuid IS NOT NULL THEN credential_uuid
    WHEN billing_contract_uuid IS NOT NULL THEN 'contract:' || billing_contract_uuid
    ELSE NULL END
WHERE billing_unit_id IS NULL;

CREATE TRIGGER request_log_identity_snapshot AFTER INSERT ON request_log
WHEN NEW.account_identity_id IS NULL AND NEW.account_id IS NOT NULL
BEGIN
  UPDATE request_log SET account_identity_id=NEW.account_id WHERE id=NEW.id;
END;

ALTER TABLE billing_period_charges ADD COLUMN account_identity_id INTEGER;
ALTER TABLE billing_period_charges ADD COLUMN contract_uuid_snapshot TEXT;
ALTER TABLE billing_period_charges ADD COLUMN billing_unit_id TEXT;
UPDATE billing_period_charges
SET account_identity_id=(SELECT account_id FROM billing_contracts WHERE id=contract_id),
    contract_uuid_snapshot=(SELECT uuid FROM billing_contracts WHERE id=contract_id),
    billing_unit_id=CASE WHEN credential_uuid IS NOT NULL THEN credential_uuid
                         ELSE 'contract:' || (SELECT uuid FROM billing_contracts WHERE id=contract_id)
                    END
WHERE account_identity_id IS NULL;

ALTER TABLE agent_subscription_period_charges ADD COLUMN subscription_uuid_snapshot TEXT;
ALTER TABLE agent_subscription_period_charges ADD COLUMN instance_uuid_snapshot TEXT;
ALTER TABLE agent_subscription_period_charges ADD COLUMN subscription_name_snapshot TEXT;
ALTER TABLE agent_subscription_period_charges ADD COLUMN instance_label_snapshot TEXT;
UPDATE agent_subscription_period_charges
SET subscription_uuid_snapshot=(SELECT uuid FROM agent_subscriptions WHERE id=subscription_id),
    instance_uuid_snapshot=(SELECT uuid FROM agent_subscription_instances WHERE id=instance_id),
    subscription_name_snapshot=(SELECT name FROM agent_subscriptions WHERE id=subscription_id),
    instance_label_snapshot=(SELECT label FROM agent_subscription_instances WHERE id=instance_id)
WHERE subscription_uuid_snapshot IS NULL;
