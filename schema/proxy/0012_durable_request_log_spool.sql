-- 0012: idempotent request-log replay and frozen enqueue-time pricing.
--
-- event_id makes replay of the durable local spool safe even when SQLite
-- reported an indeterminate COMMIT result.  Existing/third-party writers keep
-- cost_frozen=0 and retain trigger-based pricing; the proxy writes a price
-- snapshot with cost_frozen=1 so queue delay cannot change the bill.

ALTER TABLE request_log ADD COLUMN event_id TEXT;
ALTER TABLE request_log ADD COLUMN cost_frozen INTEGER NOT NULL DEFAULT 0
    CHECK (cost_frozen IN (0, 1));

CREATE UNIQUE INDEX idx_request_log_event_id ON request_log(event_id);

-- Hot aggregate routing lookup used by the one-statement routing snapshot.
CREATE INDEX idx_aggregate_entries_route
    ON aggregate_entries(account_id, pattern, sort_order, id);

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
WHEN NEW.cost_frozen = 0
BEGIN
    UPDATE request_log SET
        api_cost = COALESCE((
            SELECT (
                     (MAX(NEW.prompt_tokens - NEW.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                   + (NEW.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                   + (NEW.completion_tokens / 1000000.0) * mp.output_price
                   ) * COALESCE((
                       SELECT ps.multiplier FROM pricing_slots ps
                       WHERE ps.pricing_id = mp.id
                         AND (
                             (ps.start_minute <= ps.end_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute)
                          OR (ps.start_minute >  ps.end_minute
                                 AND (CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                   OR CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute))
                         )
                       ORDER BY ps.id LIMIT 1), 1.0)
            FROM model_pricing mp
            WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
            ORDER BY mp.id LIMIT 1
        ), 0.0)
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;
