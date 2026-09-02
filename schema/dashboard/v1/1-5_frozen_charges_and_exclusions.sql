-- V1.5: freeze recurring dashboard rows and persist explicit archive deletes.

ALTER TABLE monthly_recurring_costs ADD COLUMN charge_frozen_at TEXT;

CREATE TABLE account_exclusions (
    account_id INTEGER PRIMARY KEY,
    excluded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_account_exclusions_time
    ON account_exclusions(excluded_at, account_id);

-- Zero-only rows are audit noise in the dashboard. Keep the source ledger,
-- but do not let these rows make an account selectable here.
DELETE FROM monthly_recurring_costs
WHERE recurring_charge=0
  AND COALESCE(normalized_recurring_cost,0)=0
  AND equivalent_cost=0;

DELETE FROM accounts
WHERE NOT EXISTS (
          SELECT 1 FROM daily_usage d
          WHERE d.account_id=accounts.account_id
      )
  AND NOT EXISTS (
          SELECT 1 FROM monthly_recurring_costs m
          WHERE m.account_id=accounts.account_id
      );
