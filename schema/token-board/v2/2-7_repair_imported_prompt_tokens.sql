-- Token-Board V2.7: restore the cache-read share in imported prompt tokens.
--
-- prompt_tokens is the cache-inclusive input bucket: it holds the cache-miss
-- input plus the cache-write and cache-read shares, and total_tokens stays
-- prompt_tokens + completion_tokens.  A source whose `input` counter is the
-- net input (OpenCode writes `tokens.input` without `tokens.cache.read`)
-- stores prompt_tokens = net input + cache-write until the cache-read share
-- is added back; the dashboard archive rejects such a row because its
-- daily_model_usage CHECK requires cache_read_tokens <= input_tokens.
--
-- equivalent_cost keeps its write-time value: price_usage_event is an AFTER
-- INSERT trigger, so rewriting tokens never re-prices a stored row.
--
-- The repair is guarded by a data_repairs marker so re-executing this file
-- changes nothing.  Rows are matched by the OpenCode software that wrote them
-- plus, defensively, any imported row that still inverts the two buckets.

CREATE TABLE IF NOT EXISTS data_repairs (
    id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

UPDATE request_log
SET prompt_tokens = prompt_tokens + cache_read_tokens,
    total_tokens = prompt_tokens + cache_read_tokens + completion_tokens
WHERE source_kind = 'import'
  AND cache_read_tokens > 0
  AND (
      agent_software_id IN (
          SELECT id FROM agent_software
          WHERE LOWER(TRIM(agent_kind)) = 'opencode')
      OR cache_read_tokens > prompt_tokens
  )
  AND NOT EXISTS (
      SELECT 1 FROM data_repairs
      WHERE id = 'v2.7_imported_prompt_cache_read'
  );

INSERT OR IGNORE INTO data_repairs(id, applied_at)
VALUES (
    'v2.7_imported_prompt_cache_read',
    strftime('%Y-%m-%dT%H:%M:%fZ','now')
);
