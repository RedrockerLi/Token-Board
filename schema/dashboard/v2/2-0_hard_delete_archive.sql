-- Dashboard V2: archive rows are facts, not account lifecycle state.

-- Legacy account history is explicitly removed.  Accounts with no remaining
-- fact are not mirrored into the V2 archive.
DELETE FROM daily_usage
WHERE account_id IN (SELECT account_id FROM accounts WHERE account_kind='legacy');
DELETE FROM monthly_recurring_costs
WHERE account_id IN (SELECT account_id FROM accounts WHERE account_kind='legacy');
DELETE FROM accounts
WHERE account_kind='legacy'
   OR NOT EXISTS (SELECT 1 FROM daily_usage d
                  WHERE d.account_id=accounts.account_id)
  AND NOT EXISTS (SELECT 1 FROM monthly_recurring_costs m
                  WHERE m.account_id=accounts.account_id);

DROP INDEX IF EXISTS idx_dashboard_accounts_kind;
ALTER TABLE accounts DROP COLUMN lifecycle_state;
CREATE INDEX idx_dashboard_accounts_kind ON accounts(account_kind, account_id);

DROP INDEX IF EXISTS idx_monthly_recurring_account;
ALTER TABLE monthly_recurring_costs RENAME TO monthly_recurring_costs_v1;
CREATE TABLE monthly_recurring_costs (
    period_start TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    billing_unit_id TEXT NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    equivalent_cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY',
    normalized_recurring_cost REAL,
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    charge_frozen_at TEXT,
    PRIMARY KEY (period_start, account_id, billing_unit_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
INSERT INTO monthly_recurring_costs
    (period_start,account_id,billing_unit_id,recurring_charge,equivalent_cost,
     currency,normalized_recurring_cost,base_currency,fx_rate_date,charge_frozen_at)
SELECT CASE WHEN length(month)=7 THEN month || '-01T00:00:00Z' ELSE month END,
       account_id,billing_unit_id,recurring_charge,equivalent_cost,currency,
       normalized_recurring_cost,base_currency,fx_rate_date,charge_frozen_at
FROM monthly_recurring_costs_v1;
DROP TABLE monthly_recurring_costs_v1;
CREATE INDEX idx_monthly_recurring_account
    ON monthly_recurring_costs(account_id, period_start);

DROP TABLE IF EXISTS billing_export_receipts;
