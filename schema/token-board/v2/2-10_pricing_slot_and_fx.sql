-- Token-Board V2.10: restore the pricing trigger's slot window and FX filter.
--
-- V2.0 rebuilt price_usage_event and dropped two behaviours that V1.14 had:
--
--   * the slot subquery never looked at the wall clock, so every request was
--     priced with one arbitrary multiplier (always the lowest slot id);
--   * the cost subquery selected its rule without the FX availability filter
--     that the pricing_status decision above it already applies, so a non-CNY
--     rule without an exchange rate could win "ORDER BY priority,id" and
--     store pricing_status='rated' with equivalent_cost=0.
--
-- This migration re-creates the same trigger with exactly those two fixes and
-- no other change.  Pricing is unchanged for write-time amounts already in
-- request_log: the migration itself updates no existing row, and the trigger
-- only prices the row it was fired for (id=NEW.id).

DROP TRIGGER IF EXISTS price_usage_event;

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
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=pr.currency
          AND fx.quote_currency='CNY' AND fx.date<=date('now')
        ))
    ) THEN 'rated' ELSE 'unrated' END,
    equivalent_cost=COALESCE((
      SELECT ((max(NEW.prompt_tokens-NEW.cache_read_tokens,0)/1000000.0)
          *COALESCE(t.input_price,pr.input_price)
        +(NEW.cache_read_tokens/1000000.0)*COALESCE(t.cache_read_price,pr.cache_read_price)
        +(NEW.completion_tokens/1000000.0)*COALESCE(t.output_price,pr.output_price))
      *COALESCE((SELECT ps.multiplier FROM pricing_slots ps
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
        ORDER BY ps.id LIMIT 1),1.0)
      *CASE WHEN pr.currency='CNY' THEN 1.0 ELSE COALESCE((
        SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency=pr.currency
        AND fx.quote_currency='CNY' AND fx.date<=date('now')
        ORDER BY fx.date DESC LIMIT 1),NULL) END
      FROM pricing_rules pr
      LEFT JOIN pricing_length_tiers t ON t.pricing_rule_id=pr.id
       AND t.threshold_tokens=(SELECT max(t2.threshold_tokens)
          FROM pricing_length_tiers t2 WHERE t2.pricing_rule_id=pr.id
          AND NEW.prompt_tokens>=t2.threshold_tokens)
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=pr.currency
          AND fx.quote_currency='CNY' AND fx.date<=date('now')
        ))
      ORDER BY pr.priority,pr.id LIMIT 1
    ),0)
  WHERE id=NEW.id;
  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
      AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
      AND (bc.ends_at IS NULL OR bc.ends_at>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;
