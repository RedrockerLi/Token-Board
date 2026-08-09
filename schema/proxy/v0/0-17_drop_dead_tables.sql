-- 0017: drop dead runtime tables + the legacy single-key column.
--
-- perf_events:          zero reads / zero writes since request logging moved
--                       to request_log (data stops 2026-08-05).
-- in_flight_requests:   the C++ proxy's request_start/request_end were
--                       prepared but never called; the table is emptied at
--                       startup and never repopulated.  Real-time concurrency
--                       comes from /health, not this table.
DROP TABLE IF EXISTS perf_events;
DROP TABLE IF EXISTS in_flight_requests;

-- upstream_key (legacy single-key column): redundant since upstream_keys is
-- the only key source.  Production data had zero accounts depending on it
-- (every account with a legacy value also had upstream_keys slots).  The C++
-- proxy and the dashboard both stopped reading it in the same release.
ALTER TABLE upstream_accounts DROP COLUMN upstream_key;
