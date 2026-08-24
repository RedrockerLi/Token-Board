-- V1.1: close correctness gaps discovered by the post-V1 static audit.

CREATE UNIQUE INDEX billing_period_charges_unit_period
ON billing_period_charges(
    contract_id,
    COALESCE(credential_uuid, ''),
    period_start
);

CREATE UNIQUE INDEX billing_contracts_one_open
ON billing_contracts(account_id)
WHERE valid_until IS NULL;

ALTER TABLE billing_period_charges ADD COLUMN finalized_at TEXT;

DROP TRIGGER price_imported_usage;
CREATE TRIGGER price_usage_event AFTER INSERT ON request_log
WHEN NEW.equivalent_cost=0
 AND NEW.prompt_tokens+NEW.completion_tokens>0
BEGIN
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
    ),1.0)*CASE WHEN r.currency='USD' THEN COALESCE((
      SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency='USD'
      AND fx.quote_currency='CNY' AND fx.date<=date(NEW.requested_at)
      ORDER BY fx.date DESC LIMIT 1
    ),1.0) ELSE 1.0 END
    FROM pricing_rules pr JOIN pricing_rates r ON r.pricing_rule_id=pr.id
    WHERE pr.enabled=1 AND LOWER(NEW.model) GLOB LOWER(pr.model_pattern)
      AND r.valid_from<=NEW.requested_at
      AND (r.valid_until IS NULL OR r.valid_until>NEW.requested_at)
    ORDER BY pr.priority,pr.id,r.valid_from DESC LIMIT 1
  ),0)
  WHERE id=NEW.id;
  UPDATE request_log SET billed_usage_cost=CASE WHEN EXISTS(
    SELECT 1 FROM billing_contracts bc WHERE bc.account_id=NEW.account_id
    AND bc.charge_type='metered' AND bc.valid_from<=NEW.requested_at
    AND (bc.valid_until IS NULL OR bc.valid_until>NEW.requested_at)
  ) THEN equivalent_cost ELSE 0 END WHERE id=NEW.id;
END;

CREATE TRIGGER config_accounts_ai AFTER INSERT ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_accounts_au AFTER UPDATE ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_accounts_ad AFTER DELETE ON accounts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_contracts_ai AFTER INSERT ON billing_contracts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_contracts_au AFTER UPDATE ON billing_contracts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_contracts_ad AFTER DELETE ON billing_contracts BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_timeouts_ai AFTER INSERT ON proxy_timeout_config BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_timeouts_au AFTER UPDATE ON proxy_timeout_config BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_timeouts_ad AFTER DELETE ON proxy_timeout_config BEGIN
  UPDATE config_state SET generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
