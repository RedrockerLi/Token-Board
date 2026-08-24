-- V1.3: archive independent agent software usage and subscription charges.

CREATE TABLE agent_software (
    software_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE agent_daily_usage (
    date TEXT NOT NULL,
    software_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    equivalent_cost REAL NOT NULL DEFAULT 0,
    billed_usage_cost REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, software_id, model),
    FOREIGN KEY (software_id) REFERENCES agent_software(software_id)
);
CREATE INDEX idx_agent_daily_usage_software_date
    ON agent_daily_usage(software_id, date, model);

CREATE TABLE agent_monthly_recurring_costs (
    month TEXT NOT NULL,
    subscription_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    billing_incomplete_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, subscription_id)
);
CREATE INDEX idx_agent_monthly_recurring_subscription
    ON agent_monthly_recurring_costs(subscription_id, month);
