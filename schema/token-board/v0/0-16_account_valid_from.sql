-- 0016: account-level subscription start date for per_account types (agent).
-- plan 的订阅起始日在 upstream_keys.valid_from（per_key）；agent 不持有密钥，
-- 周期锚点此前取 created_at。加一列让 agent 的订阅起始日可显式设置。
-- NULL = 回落 created_at 日期，与 upstream_keys.valid_from 语义一致。
ALTER TABLE upstream_accounts ADD COLUMN valid_from TEXT
    CHECK (valid_from IS NULL OR valid_from GLOB '????-??-??');
