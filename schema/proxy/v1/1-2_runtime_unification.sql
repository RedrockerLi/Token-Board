-- V1.2: make pricing provenance explicit and freeze recurring charges in the
-- dashboard reporting currency. Existing rows are historical/frozen; every
-- new runtime/import event explicitly opts into database pricing with
-- pricing_status='pending'.

ALTER TABLE request_log ADD COLUMN pricing_status TEXT NOT NULL DEFAULT 'frozen'
    CHECK (pricing_status IN ('frozen','pending','rated','unrated'));
ALTER TABLE request_log ADD COLUMN pricing_rate_id INTEGER
    REFERENCES pricing_rates(id);
CREATE INDEX idx_request_log_pricing_status
    ON request_log(pricing_status, requested_at);

ALTER TABLE billing_period_charges
    ADD COLUMN normalized_recurring_cost REAL;
ALTER TABLE billing_period_charges
    ADD COLUMN base_currency TEXT NOT NULL DEFAULT 'CNY';
ALTER TABLE billing_period_charges ADD COLUMN fx_rate_date TEXT;

-- Existing CNY rows are already expressed in the reporting currency. For
-- foreign-currency rows, backfill only when a historical FX rate is present;
-- a NULL normalized value remains visible as incomplete billing instead of
-- silently treating a missing rate as 1.0.
UPDATE billing_period_charges
SET normalized_recurring_cost=recurring_charge
WHERE currency='CNY' AND normalized_recurring_cost IS NULL;
UPDATE billing_period_charges
SET normalized_recurring_cost=recurring_charge*COALESCE((
      SELECT fx.rate FROM fx_rates fx
      WHERE fx.base_currency=billing_period_charges.currency
        AND fx.quote_currency='CNY'
        AND fx.date<=date(billing_period_charges.period_start)
      ORDER BY fx.date DESC LIMIT 1
    ),NULL),
    fx_rate_date=(
      SELECT fx.date FROM fx_rates fx
      WHERE fx.base_currency=billing_period_charges.currency
        AND fx.quote_currency='CNY'
        AND fx.date<=date(billing_period_charges.period_start)
      ORDER BY fx.date DESC LIMIT 1
    )
WHERE currency!='CNY' AND normalized_recurring_cost IS NULL;

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
      (max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)*r.input_price+
      (NEW.cache_read_tokens/1000000.0)*r.cache_read_price+
      (NEW.completion_tokens/1000000.0)*r.output_price
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
    FROM pricing_rates r WHERE r.id=request_log.pricing_rate_id
  ),0)
  WHERE id=NEW.id;

  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
    AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
    AND (bc.valid_until IS NULL OR bc.valid_until>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;
