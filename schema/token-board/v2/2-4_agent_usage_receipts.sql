-- Token-Board V2.4: retain Agent import deduplication independently from
-- the 30-day request_log retention window.

CREATE TABLE agent_usage_receipts (
    event_id TEXT NOT NULL PRIMARY KEY,
    software_id INTEGER,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_agent_usage_receipts_software
    ON agent_usage_receipts(software_id);

-- Existing imported rows already represent accepted source events.  Preserve
-- that knowledge before request_log rows become eligible for retention.  Their
-- original import time is unavailable, so the default records migration time.
INSERT OR IGNORE INTO agent_usage_receipts(event_id, software_id)
SELECT event_id,
       COALESCE(agent_software_id, account_identity_id, account_id)
FROM request_log
WHERE source_kind='import';
