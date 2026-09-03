-- V1.14: flatten model pricing to one current configuration.
--
-- Existing request_log amounts are immutable ledger values.  This migration
-- only removes pricing provenance columns and rebuilds the pricing tables;
-- it never updates equivalent_cost or billed_usage_cost.

DROP TRIGGER IF EXISTS price_usage_event;
DROP VIEW IF EXISTS model_pricing;

CREATE TEMP TABLE pricing_v114_sequence (
    name TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
INSERT INTO pricing_v114_sequence(name,seq)
SELECT name,seq FROM sqlite_sequence
WHERE name IN ('pricing_rules','pricing_slots');

-- A transition plugin may populate these staging tables after validating the
-- old versioned data.  The INSERT OR IGNORE fallback keeps a newly-created
-- empty database and direct schema construction usable as well.
CREATE TABLE IF NOT EXISTS pricing_current_stage (
    rule_id INTEGER PRIMARY KEY,
    rate_id INTEGER NOT NULL,
    model_pattern TEXT NOT NULL,
    priority INTEGER NOT NULL,
    input_price REAL NOT NULL,
    cache_read_price REAL NOT NULL,
    output_price REAL NOT NULL,
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_current_stage_meta (
    id INTEGER PRIMARY KEY CHECK (id=1),
    prepared_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_current_slots_stage (
    slot_id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    multiplier REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_current_tiers_stage (
    rule_id INTEGER NOT NULL,
    threshold_tokens INTEGER NOT NULL,
    input_price REAL,
    cache_read_price REAL,
    output_price REAL,
    PRIMARY KEY(rule_id, threshold_tokens)
);

INSERT OR IGNORE INTO pricing_current_stage
    (rule_id,rate_id,model_pattern,priority,input_price,cache_read_price,
     output_price,currency)
SELECT pr.id,r.id,pr.model_pattern,pr.priority,r.input_price,
       r.cache_read_price,r.output_price,r.currency
FROM pricing_rules pr
JOIN pricing_rates r ON r.pricing_rule_id=pr.id
WHERE pr.enabled=1 AND r.valid_until IS NULL;

INSERT OR IGNORE INTO pricing_current_slots_stage
    (slot_id,rule_id,start_minute,end_minute,multiplier)
SELECT ps.id,pr.id,ps.start_minute,ps.end_minute,ps.multiplier
FROM pricing_slots ps
JOIN pricing_rates r ON r.id=ps.pricing_rate_id
JOIN pricing_rules pr ON pr.id=r.pricing_rule_id
WHERE pr.enabled=1 AND r.valid_until IS NULL;

INSERT OR IGNORE INTO pricing_current_tiers_stage
    (rule_id,threshold_tokens,input_price,cache_read_price,output_price)
SELECT pr.id,t.threshold_tokens,t.input_price,t.cache_read_price,t.output_price
FROM pricing_length_tiers t
JOIN pricing_rates r ON r.id=t.pricing_rate_id
JOIN pricing_rules pr ON pr.id=r.pricing_rule_id
WHERE pr.enabled=1 AND r.valid_until IS NULL;

-- A populated database must not silently lose an enabled rule that has no
-- unclosed rate.  V1.13 guarantees at most one current rate; this assertion
-- enforces the other half of the invariant before the old tables disappear.
CREATE TEMP TABLE pricing_v114_guard (
    value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO pricing_v114_guard(value)
SELECT 1
WHERE (EXISTS (SELECT 1 FROM pricing_rules)
       OR EXISTS (SELECT 1 FROM pricing_rates))
  AND NOT EXISTS (SELECT 1 FROM pricing_current_stage_meta WHERE id=1);
INSERT INTO pricing_v114_guard(value)
SELECT 1
FROM pricing_rules pr
WHERE pr.enabled=1
  AND NOT EXISTS (
      SELECT 1 FROM pricing_current_stage s WHERE s.rule_id=pr.id
  );

CREATE TABLE pricing_rules_v114 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    input_price REAL NOT NULL,
    cache_read_price REAL NOT NULL,
    output_price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    UNIQUE(model_pattern, priority)
);

INSERT INTO pricing_rules_v114
    (id,model_pattern,priority,input_price,cache_read_price,output_price,currency)
SELECT rule_id,model_pattern,priority,input_price,cache_read_price,output_price,currency
FROM pricing_current_stage
ORDER BY rule_id;

CREATE TABLE pricing_slots_v114 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_rule_id INTEGER NOT NULL,
    start_minute INTEGER NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
    end_minute INTEGER NOT NULL CHECK (end_minute BETWEEN 0 AND 1440),
    multiplier REAL NOT NULL CHECK (multiplier >= 0),
    FOREIGN KEY (pricing_rule_id) REFERENCES pricing_rules_v114(id) ON DELETE CASCADE
);

INSERT INTO pricing_slots_v114
    (id,pricing_rule_id,start_minute,end_minute,multiplier)
SELECT slot_id,rule_id,start_minute,end_minute,multiplier
FROM pricing_current_slots_stage
ORDER BY slot_id;

CREATE TABLE pricing_length_tiers_v114 (
    pricing_rule_id INTEGER NOT NULL,
    threshold_tokens INTEGER NOT NULL CHECK (threshold_tokens > 0),
    input_price REAL,
    cache_read_price REAL,
    output_price REAL,
    PRIMARY KEY (pricing_rule_id, threshold_tokens),
    FOREIGN KEY (pricing_rule_id) REFERENCES pricing_rules_v114(id) ON DELETE CASCADE
);

INSERT INTO pricing_length_tiers_v114
    (pricing_rule_id,threshold_tokens,input_price,cache_read_price,output_price)
SELECT rule_id,threshold_tokens,input_price,cache_read_price,output_price
FROM pricing_current_tiers_stage
ORDER BY rule_id,threshold_tokens;

-- Remove the old provenance foreign key before dropping pricing_rates.
ALTER TABLE request_log DROP COLUMN pricing_rate_id;

DROP INDEX IF EXISTS idx_pricing_length_tiers_rate_threshold;
DROP INDEX IF EXISTS idx_pricing_rates_one_current;
DROP INDEX IF EXISTS idx_pricing_rates_period;
DROP TABLE pricing_length_tiers;
DROP TABLE pricing_slots;
DROP TABLE pricing_rates;
DROP TABLE pricing_rules;

ALTER TABLE pricing_rules_v114 RENAME TO pricing_rules;
ALTER TABLE pricing_slots_v114 RENAME TO pricing_slots;
ALTER TABLE pricing_length_tiers_v114 RENAME TO pricing_length_tiers;

UPDATE sqlite_sequence
SET seq=(SELECT seq FROM pricing_v114_sequence
         WHERE pricing_v114_sequence.name=sqlite_sequence.name)
WHERE name IN ('pricing_rules','pricing_slots')
  AND EXISTS (SELECT 1 FROM pricing_v114_sequence
              WHERE pricing_v114_sequence.name=sqlite_sequence.name);
INSERT INTO sqlite_sequence(name,seq)
SELECT s.name,s.seq FROM pricing_v114_sequence s
WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence q WHERE q.name=s.name);

CREATE INDEX idx_pricing_rules_priority
    ON pricing_rules(priority, id);
CREATE INDEX idx_pricing_slots_rule
    ON pricing_slots(pricing_rule_id, id);
CREATE INDEX idx_pricing_length_tiers_rule_threshold
    ON pricing_length_tiers(pricing_rule_id, threshold_tokens DESC);

DROP TABLE pricing_current_tiers_stage;
DROP TABLE pricing_current_slots_stage;
DROP TABLE pricing_current_stage;
DROP TABLE pricing_current_stage_meta;
DROP TABLE pricing_v114_sequence;

-- The price clock is the INSERT execution time.  requested_at remains the
-- usage/reporting timestamp and is intentionally absent from price selection.
CREATE TRIGGER price_usage_event AFTER INSERT ON request_log
WHEN NEW.pricing_status='pending'
BEGIN
  UPDATE request_log SET
    pricing_status=CASE WHEN EXISTS(
      SELECT 1 FROM pricing_rules pr
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx
          WHERE fx.base_currency=pr.currency
            AND fx.quote_currency='CNY'
            AND fx.date<=date('now')
        ))
    ) THEN 'rated' ELSE 'unrated' END,
    equivalent_cost=COALESCE((
      SELECT (
        (max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)
          *COALESCE(t.input_price,pr.input_price)
        +(NEW.cache_read_tokens/1000000.0)
          *COALESCE(t.cache_read_price,pr.cache_read_price)
        +(NEW.completion_tokens/1000000.0)
          *COALESCE(t.output_price,pr.output_price)
      )
      *COALESCE((
        SELECT ps.multiplier FROM pricing_slots ps
        WHERE ps.pricing_rule_id=pr.id
          AND (
            (ps.start_minute<=ps.end_minute
             AND CAST(strftime('%s','now') AS INTEGER)%86400/60>=ps.start_minute
             AND CAST(strftime('%s','now') AS INTEGER)%86400/60<ps.end_minute)
            OR
            (ps.start_minute>ps.end_minute
             AND (CAST(strftime('%s','now') AS INTEGER)%86400/60>=ps.start_minute
                  OR CAST(strftime('%s','now') AS INTEGER)%86400/60<ps.end_minute))
          )
        ORDER BY ps.id LIMIT 1
      ),1.0)
      *CASE WHEN pr.currency='CNY' THEN 1.0 ELSE COALESCE((
        SELECT fx.rate FROM fx_rates fx
        WHERE fx.base_currency=pr.currency AND fx.quote_currency='CNY'
          AND fx.date<=date('now')
        ORDER BY fx.date DESC LIMIT 1
      ),NULL) END
      FROM pricing_rules pr
      LEFT JOIN pricing_length_tiers t
        ON t.pricing_rule_id=pr.id
       AND t.threshold_tokens=(
         SELECT max(t2.threshold_tokens)
         FROM pricing_length_tiers t2
         WHERE t2.pricing_rule_id=pr.id
           AND NEW.prompt_tokens>=t2.threshold_tokens
       )
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx
          WHERE fx.base_currency=pr.currency
            AND fx.quote_currency='CNY'
            AND fx.date<=date('now')
        ))
      ORDER BY pr.priority,pr.id LIMIT 1
    ),0)
  WHERE id=NEW.id;

  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc
    WHERE bc.account_id=NEW.account_id
      AND bc.charge_type='metered'
      AND bc.valid_from<=NEW.requested_at
      AND (bc.valid_until IS NULL OR bc.valid_until>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;
