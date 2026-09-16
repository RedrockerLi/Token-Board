-- Token-Board V2.3: do not retain Agent project/session context in request_log.
--
-- The adapter may still inspect these values while building an in-memory
-- UsageEvent, but they are not part of the durable usage or billing record.

ALTER TABLE request_log DROP COLUMN project;
ALTER TABLE request_log DROP COLUMN session_id;
