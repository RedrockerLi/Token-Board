-- Dashboard V1.0: one usage grain and one recurring-cost ledger.

PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active','disabled','deleted')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE daily_usage (
    date TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    equivalent_cost REAL NOT NULL DEFAULT 0,
    billed_usage_cost REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, account_id, model),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
CREATE INDEX idx_daily_usage_account_date
    ON daily_usage(account_id, date, model);

CREATE TABLE monthly_recurring_costs (
    month TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    billing_unit_id TEXT NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    equivalent_cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY',
    PRIMARY KEY (month, account_id, billing_unit_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
CREATE INDEX idx_monthly_recurring_account
    ON monthly_recurring_costs(account_id, month);
