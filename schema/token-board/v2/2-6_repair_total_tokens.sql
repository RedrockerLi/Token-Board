-- Token-Board V2.6: repair stored request totals to the token identity
-- total_tokens = prompt_tokens + completion_tokens.
--
-- Rows needing repair were written by the older streaming decoder, whose
-- total dropped the cache-hit share:
--     total_tokens = (prompt_tokens - cache_read_tokens) + completion_tokens.
--
-- Only rows violating the identity are rewritten, so re-running this file
-- affects 0 rows.  total_tokens carries no amount: the price_usage_event
-- trigger reads prompt_tokens, cache_read_tokens and completion_tokens only,
-- so the repair changes no stored amount.

UPDATE request_log
SET total_tokens = prompt_tokens + completion_tokens
WHERE total_tokens <> prompt_tokens + completion_tokens;
