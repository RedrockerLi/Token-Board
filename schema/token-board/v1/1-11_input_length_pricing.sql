-- V1.11: add optional input-length price overrides to each historical rate.
-- The base prices remain on pricing_rates.  A tier only stores fields that
-- override that base rate; a NULL field inherits the base price.

CREATE TABLE pricing_length_tiers (
    pricing_rate_id INTEGER NOT NULL,
    threshold_tokens INTEGER NOT NULL CHECK (threshold_tokens > 0),
    input_price REAL,
    cache_read_price REAL,
    output_price REAL,
    PRIMARY KEY (pricing_rate_id, threshold_tokens),
    FOREIGN KEY (pricing_rate_id) REFERENCES pricing_rates(id)
);

CREATE INDEX idx_pricing_length_tiers_rate_threshold
    ON pricing_length_tiers(pricing_rate_id, threshold_tokens DESC);

-- Keep SQLite as the single pricing authority for both proxy and imported
-- usage.  The selected length tier is resolved after the historical rate is
-- selected, so edits create a new rate version without changing old costs.
DROP TRIGGER price_usage_event;
CREATE TRIGGER price_usage_event AFTER INSERT ON request_log
WHEN NEW.pricing_status='pending'
BEGIN
  UPDATE request_log SET
    pricing_rate_id=(
      SELECT r.id FROM pricing_rules pr
      JOIN pricing_rates r ON r.pricing_rule_id=pr.id
      WHERE pr.enabled=1 AND LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND r.valid_from<=NEW.requested_at
        AND (r.valid_until IS NULL OR r.valid_until>NEW.requested_at)
        AND (r.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=r.currency
          AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
        ))
      ORDER BY pr.priority,pr.id,r.valid_from DESC LIMIT 1
    ),
    pricing_status=CASE WHEN EXISTS(
      SELECT 1 FROM pricing_rules pr
      JOIN pricing_rates r ON r.pricing_rule_id=pr.id
      WHERE pr.enabled=1 AND LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND r.valid_from<=NEW.requested_at
        AND (r.valid_until IS NULL OR r.valid_until>NEW.requested_at)
        AND (r.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=r.currency
          AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
        ))
    ) THEN 'rated' ELSE 'unrated' END
  WHERE id=NEW.id;

  UPDATE request_log SET equivalent_cost=COALESCE((
    SELECT (
      (max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)*COALESCE(t.input_price,r.input_price)+
      (NEW.cache_read_tokens/1000000.0)*COALESCE(t.cache_read_price,r.cache_read_price)+
      (NEW.completion_tokens/1000000.0)*COALESCE(t.output_price,r.output_price)
    )*COALESCE((
      SELECT ps.multiplier FROM pricing_slots ps
      WHERE ps.pricing_rate_id=r.id AND (
        (ps.start_minute<=ps.end_minute AND
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute AND
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute) OR
        (ps.start_minute>ps.end_minute AND (
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute OR
         CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute))
      ) ORDER BY ps.id LIMIT 1
    ),1.0)*CASE WHEN r.currency='CNY' THEN 1.0 ELSE COALESCE((
        SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency=r.currency
        AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
        ORDER BY fx.date DESC LIMIT 1
      ),NULL) END
    FROM pricing_rates r
    LEFT JOIN pricing_length_tiers t
      ON t.pricing_rate_id=r.id
     AND t.threshold_tokens=(
       SELECT max(t2.threshold_tokens) FROM pricing_length_tiers t2
       WHERE t2.pricing_rate_id=r.id
         AND NEW.prompt_tokens>=t2.threshold_tokens
     )
    WHERE r.id=request_log.pricing_rate_id
  ),0)
  WHERE id=NEW.id;

  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
    AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
    AND (bc.valid_until IS NULL OR bc.valid_until>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;
