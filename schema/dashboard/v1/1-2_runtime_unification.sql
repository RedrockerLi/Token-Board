-- V1.2: preserve native recurring charges and the frozen reporting-currency
-- amount independently.

ALTER TABLE monthly_recurring_costs
    ADD COLUMN normalized_recurring_cost REAL;
ALTER TABLE monthly_recurring_costs
    ADD COLUMN base_currency TEXT NOT NULL DEFAULT 'CNY';
ALTER TABLE monthly_recurring_costs ADD COLUMN fx_rate_date TEXT;

UPDATE monthly_recurring_costs
SET normalized_recurring_cost=recurring_charge
WHERE currency='CNY' AND normalized_recurring_cost IS NULL;

