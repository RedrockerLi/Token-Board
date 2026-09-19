-- Dashboard V0 rows are folded into one date/account/model grain.  The
-- transition driver supplies :billed_factor after looking up the V0 account's
-- billing contract (metered=1, recurring=0).
INSERT INTO daily_usage(
    date,account_id,model,input_tokens,cache_tokens,output_tokens,
    request_count,equivalent_cost,billed_usage_cost
) VALUES(?,?,?,?,?,?,?,?,?)
ON CONFLICT(date,account_id,model) DO UPDATE SET
    input_tokens=daily_usage.input_tokens+excluded.input_tokens,
    cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
    output_tokens=daily_usage.output_tokens+excluded.output_tokens,
    request_count=daily_usage.request_count+excluded.request_count,
    equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
    billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost;
