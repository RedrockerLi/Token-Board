-- 0005: plan subscription rows are keyed by account + masked upstream key.
--
-- The old account-month table cannot represent independently started keys.
-- Rebuild is intentional: request logs older than the local retention window
-- cannot recreate historical virtual cost, while subscriptions are rebuilt
-- from the current local billing metadata.

DROP TABLE IF EXISTS proxy_plan_summary;
CREATE TABLE proxy_plan_summary (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    month             TEXT NOT NULL,
    account_id        INTEGER NOT NULL DEFAULT 0,
    key_masked        TEXT NOT NULL DEFAULT '',
    subscription_cost REAL NOT NULL DEFAULT 0,
    virtual_cost      REAL NOT NULL DEFAULT 0,
    UNIQUE(month, account_id, key_masked)
);
CREATE INDEX IF NOT EXISTS idx_pps_month ON proxy_plan_summary(month);
