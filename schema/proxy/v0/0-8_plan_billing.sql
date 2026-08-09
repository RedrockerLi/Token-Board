-- 0008: per-key plan billing, UTC billing settings and price history.
--
-- Upstream secrets remain local.  `upstream_keys_cloud` contains only a
-- masked identity and billing metadata for cross-machine reconciliation.

CREATE TABLE upstream_keys_new (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id                 INTEGER NOT NULL REFERENCES upstream_accounts(id) ON DELETE CASCADE,
    key_value                  TEXT NOT NULL,
    position                   INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    valid_from                 TEXT,
    deleted_at                 TEXT,
    cancellation_grace_hours   INTEGER,
    CHECK (valid_from IS NULL OR valid_from GLOB '????-??-??')
);

INSERT INTO upstream_keys_new (id, account_id, key_value, position, created_at)
SELECT id, account_id, key_value, position, created_at
FROM upstream_keys;

DROP TABLE upstream_keys;
ALTER TABLE upstream_keys_new RENAME TO upstream_keys;
CREATE UNIQUE INDEX idx_uk_active_unique
    ON upstream_keys(account_id, key_value) WHERE deleted_at IS NULL;
CREATE INDEX idx_uk_account ON upstream_keys(account_id, position);

CREATE TABLE IF NOT EXISTS upstream_keys_cloud (
    account_id               INTEGER NOT NULL,
    key_masked               TEXT NOT NULL,
    position                 INTEGER NOT NULL DEFAULT 0,
    valid_from               TEXT,
    deleted_at               TEXT,
    cancellation_grace_hours INTEGER,
    PRIMARY KEY(account_id, key_masked)
);

CREATE TABLE IF NOT EXISTS plan_billing_config (
    id                         INTEGER PRIMARY KEY CHECK (id = 1),
    price_change_effective     TEXT NOT NULL DEFAULT 'current_period'
        CHECK (price_change_effective IN ('current_period', 'next_period')),
    cancellation_grace_hours   INTEGER NOT NULL DEFAULT 24
        CHECK (cancellation_grace_hours >= 0 AND cancellation_grace_hours <= 744)
);
INSERT OR IGNORE INTO plan_billing_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS plan_price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES upstream_accounts(id) ON DELETE CASCADE,
    monthly_price  REAL NOT NULL,
    changed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    effective_mode TEXT NOT NULL CHECK (effective_mode IN ('current_period', 'next_period'))
);
CREATE INDEX IF NOT EXISTS idx_pph_account_changed
    ON plan_price_history(account_id, changed_at, id);

-- A legacy account had only a mutable price.  Seed one baseline event so all
-- historical periods have a deterministic value after dashboard rebuild.
INSERT INTO plan_price_history (account_id, monthly_price, changed_at, effective_mode)
SELECT id, monthly_price, '1970-01-01 00:00:00', 'current_period'
FROM upstream_accounts
WHERE COALESCE(account_type, 'api') = 'plan'
  AND NOT EXISTS (
      SELECT 1 FROM plan_price_history h WHERE h.account_id = upstream_accounts.id
  );
