-- Keep one request_log row per client request while retaining every upstream
-- attempt.  This is required for truthful provider success-rate and fallback
-- diagnostics: a failed provider followed by a successful fallback must not
-- disappear from monitoring.
--
-- ⚠ DO NOT SHIP THIS MIGRATION ALONE: it creates request_attempts.account_id as
--   NOT NULL with no ON DELETE, which makes the C++ writer (which binds a
--   possibly-deleted account id via a SELECT) fail under PRAGMA foreign_keys=ON.
--   0011_request_attempt_account_detach.sql relaxes it to nullable +
--   ON DELETE SET NULL.  0010 and 0011 are a single unit and must be applied
--   together.
CREATE TABLE request_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_log_id    INTEGER NOT NULL REFERENCES request_log(id) ON DELETE CASCADE,
    attempt_index     INTEGER NOT NULL,
    account_id        INTEGER NOT NULL REFERENCES upstream_accounts(id),
    upstream_key_id   INTEGER,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    ttft_ms           INTEGER,
    is_timeout        INTEGER NOT NULL DEFAULT 0,
    error             TEXT NOT NULL DEFAULT '',
    requested_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(request_log_id, attempt_index)
);

CREATE INDEX idx_request_attempts_account_time
    ON request_attempts(account_id, requested_at);
CREATE INDEX idx_request_attempts_request
    ON request_attempts(request_log_id, attempt_index);
