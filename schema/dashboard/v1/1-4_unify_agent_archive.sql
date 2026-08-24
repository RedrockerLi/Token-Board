-- V1.4: use the generic dashboard archive for both proxy and agent rows.
-- Dedicated agent tables are migrated and then removed.

ALTER TABLE accounts ADD COLUMN account_kind TEXT NOT NULL DEFAULT 'proxy'
    CHECK (account_kind IN ('proxy','agent','legacy'));
CREATE INDEX idx_dashboard_accounts_kind
    ON accounts(account_kind, lifecycle_state, account_id);

-- Agent software ids are the proxy account ids after proxy V1.8.  A primary
-- key conflict is intentional: it prevents the archive from silently
-- combining two different identities.
INSERT INTO accounts(account_id,name,lifecycle_state,updated_at,account_kind)
SELECT software_id,name,
       CASE WHEN enabled=1 THEN 'active' ELSE 'disabled' END,
       updated_at,'agent'
FROM agent_software;

INSERT INTO daily_usage
    (date,account_id,model,input_tokens,cache_tokens,output_tokens,
     request_count,equivalent_cost,billed_usage_cost)
SELECT date,software_id,model,input_tokens,cache_tokens,output_tokens,
       request_count,equivalent_cost,billed_usage_cost
FROM agent_daily_usage
WHERE 1=1
ON CONFLICT(date,account_id,model) DO UPDATE SET
    input_tokens=daily_usage.input_tokens+excluded.input_tokens,
    cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
    output_tokens=daily_usage.output_tokens+excluded.output_tokens,
    request_count=daily_usage.request_count+excluded.request_count,
    equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
    billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost;

-- Preserve the old standalone subscription rows in the generic ledger under a
-- negative legacy account id.  They remain available for forensic/export
-- purposes but are not interpreted as a software's actual cost.  No binding
-- is inferred by this migration; the user binds software manually.
INSERT INTO accounts(account_id,name,lifecycle_state,updated_at,account_kind)
SELECT -subscription_id,MAX(name),'deleted',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'legacy'
FROM agent_monthly_recurring_costs
GROUP BY subscription_id;

INSERT INTO monthly_recurring_costs
    (month,account_id,billing_unit_id,recurring_charge,equivalent_cost,
     currency,normalized_recurring_cost,base_currency,fx_rate_date)
SELECT month,-subscription_id,
       'agent-legacy-subscription:'||subscription_id,
       recurring_charge,0,currency,normalized_recurring_cost,
       base_currency,fx_rate_date
FROM agent_monthly_recurring_costs
WHERE 1=1
ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
    recurring_charge=excluded.recurring_charge,
    normalized_recurring_cost=excluded.normalized_recurring_cost,
    currency=excluded.currency,
    base_currency=excluded.base_currency,
    fx_rate_date=excluded.fx_rate_date;

DROP INDEX idx_agent_daily_usage_software_date;
DROP INDEX idx_agent_monthly_recurring_subscription;
DROP TABLE agent_daily_usage;
DROP TABLE agent_monthly_recurring_costs;
DROP TABLE agent_software;
