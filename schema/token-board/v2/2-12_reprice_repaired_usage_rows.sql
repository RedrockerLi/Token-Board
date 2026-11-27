-- Token-Board V2.12: re-price a usage row when a repair changes its tokens.
--
-- Agent re-imports are deduplicated by the permanent receipt in
-- agent_usage_receipts(event_id).  A re-parse of the same source event can
-- recover tokens the first pass could not see (the reply was still streaming,
-- or an importer repair learned the real prompt size).  Before this migration
-- the repair path could only backfill duration_ms, so those tokens were lost
-- for good: the row kept its old amount and the event was never priced again.
--
-- The repair path now rewrites the token columns and resets the row to
-- pricing_status='pending' with zero amounts.  A pending row is otherwise only
-- priced by the AFTER INSERT trigger, which cannot fire for an UPDATE, so this
-- migration adds the matching AFTER UPDATE trigger.  Its body is the V2.11
-- pricing body verbatim: same length-tier lookup, same slot minute window from
-- NEW.requested_at, same FX availability filter and rate selection, same
-- metered contract decision.  Pricing therefore keeps a single authority, and
-- the request-time clock introduced by V2.11 stays in force.
--
-- Scope is one row per firing (id=NEW.id) and only for rows whose pending
-- status the repair just set; this migration updates no existing row by itself
-- and never re-prices history that was not repaired.

DROP TRIGGER IF EXISTS reprice_usage_event;

CREATE TRIGGER reprice_usage_event
AFTER UPDATE OF prompt_tokens,completion_tokens,cache_read_tokens,total_tokens
ON request_log
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
