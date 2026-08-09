-- 0009: truthful TTFT / generation-speed metrics.
--
-- Keep duration_ms for historical compatibility, but all new UI latency
-- queries use ttft_ms.  Existing duration values are NOT copied: they are
-- total response times and are not valid TTFT measurements.

ALTER TABLE request_log ADD COLUMN ttft_ms INTEGER;
ALTER TABLE request_log ADD COLUMN generation_ms INTEGER;
ALTER TABLE request_log ADD COLUMN output_tps REAL;
ALTER TABLE request_log ADD COLUMN upstream_ttft_ms INTEGER;
ALTER TABLE request_log ADD COLUMN upstream_duration_ms INTEGER;
ALTER TABLE request_log ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE request_log ADD COLUMN fallback_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_rl_ttft_time
    ON request_log(requested_at, ttft_ms);
