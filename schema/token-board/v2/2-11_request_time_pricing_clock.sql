-- Token-Board V2.11: price a row at its own request time, not at write time.
--
-- V1.14 and V2.10 both read the price clock from the wall clock: the FX row
-- filter used date('now') and the slot minute window used
-- strftime('%s','now').  A live proxy request is inserted at its own request
-- moment, so those two clocks agree and write-time pricing equals request-time
-- pricing.  Historical backfill (first import, machine move, re-scan after a
-- repair) is inserted long after the usage happened: those rows used today's
-- price table and today's exchange rate, and the AFTER INSERT trigger freezes
-- the amount, so they can never be corrected afterwards.
--
-- This migration re-creates the same trigger with the price clock taken from
-- NEW.requested_at.  Only the clock source changes; the slot minute window,
-- the FX availability filter inside the cost subquery, the length-tier lookup
-- and the metered contract decision keep the V2.10 shape verbatim:
--
--   * the slot minute is still the UTC minute of day of the row's moment,
--     compared against the same start/end window (including the wrap-around
--     branch) with the same ORDER BY ps.id LIMIT 1;
--   * the FX filter is still "an fx_rates row exists for the rule currency on
--     or before the row's date", and the rate is still the latest such row.
--
-- The migration updates no existing row: the trigger definition is replaced
-- and the body only prices the row it fired for (id=NEW.id).  Amounts already
-- frozen by an earlier trigger definition stay untouched.
--
-- The V1 line stays frozen: a database that reaches this V2 tip runs this
-- definition, V1.14's wall-clock trigger is dropped during the V1 -> V2
-- transition (V2.0) and never fires at runtime.

DROP TRIGGER IF EXISTS price_usage_event;

-- The price clock is the row's own requested_at.  requested_at doubles as the
-- usage/reporting timestamp, which is why the FX date and the slot minute both
-- come from it.
CREATE TRIGGER price_usage_event AFTER INSERT ON request_log
WHEN NEW.pricing_status='pending'
BEGIN
  UPDATE request_log SET
    pricing_status=CASE WHEN EXISTS(
      SELECT 1 FROM pricing_rules pr
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=pr.currency
          AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
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
             AND CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute
             AND CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute)
            OR
            (ps.start_minute>ps.end_minute
             AND (CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60>=ps.start_minute
                  OR CAST(strftime('%s',NEW.requested_at) AS INTEGER)%86400/60<ps.end_minute))
          )
        ORDER BY ps.id LIMIT 1),1.0)
      *CASE WHEN pr.currency='CNY' THEN 1.0 ELSE COALESCE((
        SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency=pr.currency
        AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
        ORDER BY fx.date DESC LIMIT 1),NULL) END
      FROM pricing_rules pr
      LEFT JOIN pricing_length_tiers t ON t.pricing_rule_id=pr.id
       AND t.threshold_tokens=(SELECT max(t2.threshold_tokens)
          FROM pricing_length_tiers t2 WHERE t2.pricing_rule_id=pr.id
          AND NEW.prompt_tokens>=t2.threshold_tokens)
      WHERE LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
        AND (pr.currency='CNY' OR EXISTS(
          SELECT 1 FROM fx_rates fx WHERE fx.base_currency=pr.currency
          AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
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
