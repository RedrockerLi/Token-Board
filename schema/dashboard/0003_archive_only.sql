-- 0003: dashboard 纯存档化 —— 折叠 CSV 数据、删价格镜像表、删 source 列
--
-- DESTRUCTIVE: 删 cost_entry.source 列、删 model_pricing/pricing_slots/account_types、折叠存量 CSV 数据 — 先备份 data/dashboard.db
--
-- 变更目标：
--   1. CSV 导入弃用：存量 source='csv' 的 cost_entry/token_usage/request_usage 折叠进
--      'DeepSeek' 账户（按唯一键聚合，与现有 proxy 的 DeepSeek 行相加），再删除旧行。
--   2. cost_entry 去掉 source 列（CSV 弃用后恒为 proxy），唯一索引改为 (date, model, cost_group_key)。
--   3. 删除价格镜像表 model_pricing / pricing_slots / account_types 与改价回溯触发器
--      tr_mp_refresh_insert/update/delete —— dashboard 不再有任何价格/重算能力，纯存档。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner 统一处理。
-- 仅对 user_version=2 的库应用；全新库（0001 直接到 0003）与升级库均安全、幂等。
--
-- 折叠顺序说明：先折叠（INSERT 聚合并入 DeepSeek）再删除，删除顺序 request→token→cost，
-- 保证每步引用的「CSV 来源集」（cost_entry.source='csv'、token_usage 的 csv cost_group_key）仍在。

-- ── 1. 折叠 cost_entry：source='csv' 行按 (date, model) 聚合成 cost_group_key='DeepSeek'（source='proxy' 以便与现有行合并）──

INSERT INTO cost_entry (date, model, cost, cost_group_key, source)
SELECT date, model, SUM(cost), 'DeepSeek', 'proxy'
FROM cost_entry
WHERE source = 'csv'
GROUP BY date, model
ON CONFLICT(date, model, cost_group_key, source)
    DO UPDATE SET cost = cost_entry.cost + excluded.cost;

-- ── 2. 折叠 token_usage：CSV 分组的行 → api_key_name/cost_group_key='DeepSeek'，按 (date, model, token_type) 聚合 ──

INSERT INTO token_usage (date, model, api_key_name, token_type, amount, cost_group_key)
SELECT date, model, 'DeepSeek', token_type, SUM(amount), 'DeepSeek'
FROM token_usage
WHERE cost_group_key IN (SELECT DISTINCT cost_group_key FROM cost_entry WHERE source = 'csv')
GROUP BY date, model, token_type
ON CONFLICT(date, model, api_key_name, token_type, cost_group_key)
    DO UPDATE SET amount = token_usage.amount + excluded.amount;

-- ── 3. 折叠 request_usage：CSV 设备名行 → api_key_name='DeepSeek'，按 (date, model) 聚合 ──

INSERT INTO request_usage (date, model, api_key_name, count)
SELECT date, model, 'DeepSeek', SUM(count)
FROM request_usage
WHERE api_key_name IN (
    SELECT DISTINCT api_key_name FROM token_usage
    WHERE cost_group_key IN (SELECT DISTINCT cost_group_key FROM cost_entry WHERE source = 'csv')
)
GROUP BY date, model
ON CONFLICT(date, model, api_key_name)
    DO UPDATE SET count = request_usage.count + excluded.count;

-- ── 4. 删除旧 CSV 行（先 request，再 token，最后 cost；每步引用的来源集仍在）──────────

DELETE FROM request_usage WHERE api_key_name IN (
    SELECT DISTINCT api_key_name FROM token_usage
    WHERE cost_group_key IN (SELECT DISTINCT cost_group_key FROM cost_entry WHERE source = 'csv')
);
DELETE FROM token_usage WHERE cost_group_key IN (
    SELECT DISTINCT cost_group_key FROM cost_entry WHERE source = 'csv'
);
DELETE FROM cost_entry WHERE source = 'csv';

-- ── 5. 重建 cost_entry：去掉 source 列 ──────────────────────────────────────
-- 索引名是库级全局的：须先 DROP 旧表（连带删旧索引）再建新索引。

CREATE TABLE cost_entry_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    model          TEXT NOT NULL,
    cost           REAL NOT NULL,
    cost_group_key TEXT DEFAULT ''
);
INSERT INTO cost_entry_new (id, date, model, cost, cost_group_key)
    SELECT id, date, model, cost, cost_group_key FROM cost_entry;
DROP TABLE cost_entry;
ALTER TABLE cost_entry_new RENAME TO cost_entry;
CREATE UNIQUE INDEX idx_ce_unique ON cost_entry(date, model, cost_group_key);
CREATE INDEX idx_ce_query ON cost_entry(date, model, cost_group_key);

-- ── 6. 删除价格镜像表 + 改价回溯触发器（幂等）───────────────────────────────

DROP TABLE IF EXISTS pricing_slots;   -- 必须先于 model_pricing（外键父表）
DROP TABLE IF EXISTS model_pricing;
DROP TABLE IF EXISTS account_types;
DROP TRIGGER IF EXISTS tr_mp_refresh_insert;
DROP TRIGGER IF EXISTS tr_mp_refresh_update;
DROP TRIGGER IF EXISTS tr_mp_refresh_delete;
