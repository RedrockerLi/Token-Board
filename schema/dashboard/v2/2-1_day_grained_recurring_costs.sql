-- Dashboard V2.1: recurring-cost archive rows carry a frozen day/state,
-- never the source operation timestamp.

ALTER TABLE monthly_recurring_costs
    RENAME TO monthly_recurring_costs_v20;
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
    is_frozen INTEGER NOT NULL DEFAULT 0 CHECK (is_frozen IN (0,1)),
    frozen_on TEXT,
    PRIMARY KEY (period_start, account_id, billing_unit_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
INSERT INTO monthly_recurring_costs
    (period_start,account_id,billing_unit_id,recurring_charge,equivalent_cost,
     currency,normalized_recurring_cost,base_currency,fx_rate_date,is_frozen,frozen_on)
SELECT CASE WHEN length(period_start)=7
              THEN period_start || '-01T00:00:00Z'
              ELSE period_start END,
       account_id,billing_unit_id,recurring_charge,equivalent_cost,currency,
       normalized_recurring_cost,base_currency,fx_rate_date,
       CASE WHEN charge_frozen_at IS NULL THEN 0 ELSE 1 END,
       CASE WHEN charge_frozen_at IS NULL THEN NULL ELSE date(charge_frozen_at) END
FROM monthly_recurring_costs_v20;
DROP TABLE monthly_recurring_costs_v20;
CREATE INDEX idx_monthly_recurring_account
    ON monthly_recurring_costs(account_id, period_start);
