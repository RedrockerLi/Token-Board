-- Executed against the V1 proxy shadow with the V0 database attached as legacy.
-- Python populates these maps because UUIDv5 and local-time conversion are not
-- deterministic SQLite built-ins.
CREATE TEMP TABLE IF NOT EXISTS migration_credential_map (
    legacy_key_id INTEGER PRIMARY KEY,
    credential_uuid TEXT NOT NULL UNIQUE
);
CREATE TEMP TABLE IF NOT EXISTS migration_account_type (
    account_id INTEGER PRIMARY KEY,
    charge_type TEXT NOT NULL
);
