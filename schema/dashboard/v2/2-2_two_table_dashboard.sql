-- Dashboard V2.2: compact homepage projection.
--
-- The old V2 archive kept account identities, daily usage, and recurring
-- charges as separate ledgers.  The homepage only needs a stable user
-- identity with its cumulative actual spend and a daily model usage grain.
-- Rebuild the archive in that shape while preserving the existing visible
-- token/request/theoretical/actual totals.

PRAGMA foreign_keys = OFF;

ALTER TABLE accounts RENAME TO dashboard_accounts_v22;
ALTER TABLE daily_usage RENAME TO dashboard_daily_usage_v22;
ALTER TABLE monthly_recurring_costs RENAME TO dashboard_recurring_costs_v22;

CREATE TABLE users (
    id INTEGER PRIMARY KEY CHECK (id >= 0),
    name TEXT NOT NULL CHECK (trim(name) <> ''),
    actual_cost_micro_cny INTEGER NOT NULL DEFAULT 0
        CHECK (actual_cost_micro_cny >= 0)
);

CREATE TABLE daily_model_usage (
    user_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL
        CHECK (
            length(usage_date) = 10
            AND date(usage_date) IS usage_date
        ),
    model TEXT NOT NULL CHECK (trim(model) <> ''),
    input_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (input_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (
            cache_read_tokens >= 0
            AND cache_read_tokens <= input_tokens
        ),
    output_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (output_tokens >= 0),
    request_count INTEGER NOT NULL DEFAULT 0
        CHECK (request_count >= 0),
    api_equivalent_cost_micro_cny INTEGER NOT NULL DEFAULT 0
        CHECK (api_equivalent_cost_micro_cny >= 0),
    PRIMARY KEY (user_id, usage_date, model),
    FOREIGN KEY (user_id) REFERENCES users(id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
) WITHOUT ROWID;

INSERT INTO users(id, name, actual_cost_micro_cny)
VALUES (0, '归档', 0);

-- Keep every identity that has a visible usage or a normalized recurring
-- charge.  The old V2 cleanup already removed legacy/empty identities, but
-- the UNION also makes this rebuild safe for hand-created fixtures.
WITH daily_accounts AS (
    SELECT account_id,
           SUM(COALESCE(billed_usage_cost, 0)) AS billed_cost
    FROM dashboard_daily_usage_v22
    GROUP BY account_id
), recurring_accounts AS (
    SELECT account_id,
           SUM(CASE WHEN normalized_recurring_cost IS NULL
                    THEN 0 ELSE normalized_recurring_cost END) AS recurring_cost
    FROM dashboard_recurring_costs_v22
    GROUP BY account_id
), identity_ids AS (
    SELECT account_id FROM daily_accounts
    UNION
    SELECT account_id FROM recurring_accounts
), identity_costs AS (
    SELECT ids.account_id,
           COALESCE(d.billed_cost, 0) + COALESCE(r.recurring_cost, 0) AS cost
    FROM identity_ids ids
    LEFT JOIN daily_accounts d ON d.account_id = ids.account_id
    LEFT JOIN recurring_accounts r ON r.account_id = ids.account_id
)
INSERT INTO users(id, name, actual_cost_micro_cny)
SELECT i.account_id,
       COALESCE(NULLIF(trim(a.name), ''), 'unknown'),
       CAST(ROUND(i.cost * 1000000.0) AS INTEGER)
FROM identity_costs i
LEFT JOIN dashboard_accounts_v22 a ON a.account_id = i.account_id
WHERE i.account_id <> 0;

UPDATE users
   SET actual_cost_micro_cny = actual_cost_micro_cny + CAST(ROUND((
       (SELECT COALESCE(SUM(billed_usage_cost), 0)
          FROM dashboard_daily_usage_v22 WHERE account_id=0)
       + (SELECT COALESCE(SUM(normalized_recurring_cost), 0)
            FROM dashboard_recurring_costs_v22
           WHERE account_id=0 AND normalized_recurring_cost IS NOT NULL)
       ) * 1000000.0) AS INTEGER)
 WHERE id=0;

INSERT INTO daily_model_usage(
    user_id, usage_date, model, input_tokens, cache_read_tokens,
    output_tokens, request_count, api_equivalent_cost_micro_cny
)
SELECT account_id,
       date,
       model,
       SUM(input_tokens),
       CASE WHEN SUM(cache_tokens) > SUM(input_tokens)
            THEN SUM(input_tokens) ELSE SUM(cache_tokens) END,
       SUM(output_tokens),
       SUM(request_count),
       CAST(ROUND(SUM(equivalent_cost) * 1000000.0) AS INTEGER)
FROM dashboard_daily_usage_v22
WHERE model IS NOT NULL
  AND trim(model) <> ''
  AND lower(trim(model)) <> 'unknown'
GROUP BY account_id, date, model;

CREATE INDEX idx_daily_model_usage_date
    ON daily_model_usage(usage_date);

DROP TABLE dashboard_recurring_costs_v22;
DROP TABLE dashboard_daily_usage_v22;
DROP TABLE dashboard_accounts_v22;

PRAGMA foreign_keys = ON;
