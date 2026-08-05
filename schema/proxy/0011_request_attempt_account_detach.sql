-- Historical attempts must survive configuration sync/account removal just as
-- request_log rows do.  A cloud-authoritative merge may physically remove an
-- account, so keep the attempt and detach its display identity.

CREATE TABLE request_attempts_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_log_id    INTEGER NOT NULL REFERENCES request_log(id) ON DELETE CASCADE,
    attempt_index     INTEGER NOT NULL,
    account_id        INTEGER REFERENCES upstream_accounts(id) ON DELETE SET NULL,
    upstream_key_id   INTEGER,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    ttft_ms           INTEGER,
    is_timeout        INTEGER NOT NULL DEFAULT 0,
    error             TEXT NOT NULL DEFAULT '',
    requested_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(request_log_id, attempt_index)
);

INSERT INTO request_attempts_new
    (id, request_log_id, attempt_index, account_id, upstream_key_id,
     status_code, duration_ms, ttft_ms, is_timeout, error, requested_at)
SELECT id, request_log_id, attempt_index,
       CASE
           WHEN account_id IS NULL OR EXISTS (
               SELECT 1 FROM upstream_accounts a
               WHERE a.id = request_attempts.account_id
           ) THEN account_id
           ELSE NULL
       END,
       upstream_key_id,
       status_code, duration_ms, ttft_ms, is_timeout, error, requested_at
FROM request_attempts;

DROP TABLE request_attempts;
ALTER TABLE request_attempts_new RENAME TO request_attempts;

CREATE INDEX idx_request_attempts_account_time
    ON request_attempts(account_id, requested_at);
CREATE INDEX idx_request_attempts_time
    ON request_attempts(requested_at);
