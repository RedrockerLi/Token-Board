-- V1.12: recurring charges are immutable from the billing period start.
-- Agent subscription allocations are captured at that same boundary so later
-- binding changes cannot rewrite an already-issued period charge.

INSERT INTO sync_settings(key,value)
VALUES('billing.price_change_effective','next_period')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;

CREATE TABLE agent_subscription_charge_allocations (
    period_charge_id INTEGER NOT NULL,
    software_id INTEGER NOT NULL,
    recurring_charge REAL NOT NULL DEFAULT 0,
    normalized_recurring_cost REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    fx_rate_date TEXT,
    finalized_at TEXT,
    PRIMARY KEY (period_charge_id, software_id),
    FOREIGN KEY (period_charge_id)
        REFERENCES agent_subscription_period_charges(id),
    FOREIGN KEY (software_id) REFERENCES accounts(id)
);

CREATE INDEX idx_agent_charge_allocations_software
    ON agent_subscription_charge_allocations(software_id, period_charge_id);

-- Re-anchor only open proxy charges. Already-finalized historical rows are
-- intentionally immutable. Existing provisional FX values are accepted as
-- the permanent fallback; otherwise use the nearest stored rate.
UPDATE billing_period_charges
SET recurring_charge=COALESCE((
        SELECT r.recurring_price FROM billing_rate_events r
        WHERE r.contract_id=billing_period_charges.contract_id
          AND r.effective_at<=billing_period_charges.period_start
        ORDER BY r.effective_at DESC,r.id DESC LIMIT 1
    ),recurring_charge)
WHERE finalized_at IS NULL;

UPDATE billing_period_charges
SET normalized_recurring_cost=CASE
        WHEN currency='CNY' THEN recurring_charge
        WHEN normalized_recurring_cost IS NOT NULL THEN normalized_recurring_cost
        ELSE recurring_charge * COALESCE((
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
              AND fx.date<=date(billing_period_charges.period_start)
            ORDER BY fx.date DESC LIMIT 1
        ),(
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
            ORDER BY fx.date ASC LIMIT 1
        ))
    END,
    fx_rate_date=CASE
        WHEN currency='CNY' THEN NULL
        WHEN fx_rate_date IS NOT NULL THEN fx_rate_date
        ELSE COALESCE((
            SELECT fx.date FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
              AND fx.date<=date(billing_period_charges.period_start)
            ORDER BY fx.date DESC LIMIT 1
        ),(
            SELECT fx.date FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
            ORDER BY fx.date ASC LIMIT 1
        ))
    END
WHERE finalized_at IS NULL;

UPDATE billing_period_charges
SET finalized_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE finalized_at IS NULL AND normalized_recurring_cost IS NOT NULL;

-- Apply the same one-time period-start re-anchoring to agent instances.
UPDATE agent_subscription_period_charges
SET recurring_charge=COALESCE((
        SELECT r.recurring_price FROM agent_subscription_rate_events r
        WHERE r.instance_id=agent_subscription_period_charges.instance_id
          AND r.effective_at<=agent_subscription_period_charges.period_start
        ORDER BY r.effective_at DESC,r.id DESC LIMIT 1
    ),recurring_charge)
WHERE finalized_at IS NULL;

UPDATE agent_subscription_period_charges
SET normalized_recurring_cost=CASE
        WHEN currency='CNY' THEN recurring_charge
        WHEN normalized_recurring_cost IS NOT NULL THEN normalized_recurring_cost
        ELSE recurring_charge * COALESCE((
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
              AND fx.date<=date(agent_subscription_period_charges.period_start)
            ORDER BY fx.date DESC LIMIT 1
        ),(
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
            ORDER BY fx.date ASC LIMIT 1
        ))
    END,
    fx_rate_date=CASE
        WHEN currency='CNY' THEN NULL
        WHEN fx_rate_date IS NOT NULL THEN fx_rate_date
        ELSE COALESCE((
            SELECT fx.date FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
              AND fx.date<=date(agent_subscription_period_charges.period_start)
            ORDER BY fx.date DESC LIMIT 1
        ),(
            SELECT fx.date FROM fx_rates fx
            WHERE fx.base_currency=currency AND fx.quote_currency='CNY'
            ORDER BY fx.date ASC LIMIT 1
        ))
    END
WHERE finalized_at IS NULL;

UPDATE agent_subscription_period_charges
SET finalized_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE finalized_at IS NULL AND normalized_recurring_cost IS NOT NULL;

-- Snapshot existing active bindings for charges that are already frozen.
INSERT OR IGNORE INTO agent_subscription_charge_allocations
    (period_charge_id,software_id,recurring_charge,normalized_recurring_cost,
     currency,base_currency,fx_rate_date,finalized_at)
SELECT c.id,b.software_id,
       c.recurring_charge / counts.denominator,
       CASE WHEN c.normalized_recurring_cost IS NULL THEN NULL
            ELSE c.normalized_recurring_cost / counts.denominator END,
       c.currency,c.base_currency,c.fx_rate_date,c.finalized_at
FROM agent_subscription_period_charges c
JOIN agent_subscription_instances i ON i.id=c.instance_id
JOIN agent_subscription_bindings b
  ON b.subscription_id=i.subscription_id
 AND b.lifecycle_state='active'
 AND b.valid_from<=c.period_start
 AND (b.valid_until IS NULL OR b.valid_until>c.period_start)
JOIN (
    SELECT c2.id,COUNT(*) denominator
    FROM agent_subscription_period_charges c2
    JOIN agent_subscription_instances i2 ON i2.id=c2.instance_id
    JOIN agent_subscription_bindings b2
      ON b2.subscription_id=i2.subscription_id
     AND b2.lifecycle_state='active'
     AND b2.valid_from<=c2.period_start
     AND (b2.valid_until IS NULL OR b2.valid_until>c2.period_start)
    WHERE c2.finalized_at IS NOT NULL
    GROUP BY c2.id
) counts ON counts.id=c.id
WHERE c.finalized_at IS NOT NULL;
