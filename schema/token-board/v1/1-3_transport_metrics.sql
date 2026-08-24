-- V1.3: separate transport first-byte timing from protocol TTFT and record
-- whether the origin connection was reused.
ALTER TABLE request_attempts ADD COLUMN first_byte_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_attempts ADD COLUMN connection_reused INTEGER NOT NULL DEFAULT 0;
