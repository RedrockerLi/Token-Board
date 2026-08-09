-- 0001: Token Board dashboard.db 初始 schema（基线迁移）
--
-- 单一 DDL 来源：dashboard.db 的全部表/索引/触发器都由本文件定义，由
-- app/migrations.py 在 DashboardDatabase 初始化时应用。历史库（user_version=0）
-- 首次应用时：表/索引用 IF NOT EXISTS（对已有库无操作），触发器用 DROP+CREATE
-- 刷成当前定义。
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_name TEXT NOT NULL,
    token_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    cost_group_key TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tu_unique
    ON token_usage(date, model, api_key_name, token_type, cost_group_key);
CREATE INDEX IF NOT EXISTS idx_tu_query
    ON token_usage(api_key_name, date, model);

CREATE TABLE IF NOT EXISTS request_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_name TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ru_unique
    ON request_usage(date, model, api_key_name);
CREATE INDEX IF NOT EXISTS idx_ru_query
    ON request_usage(api_key_name, date, model);

CREATE TABLE IF NOT EXISTS cost_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    model TEXT NOT NULL,
    cost REAL NOT NULL,
    cost_group_key TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'proxy'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_unique
    ON cost_entry(date, model, cost_group_key, source);
CREATE INDEX IF NOT EXISTS idx_ce_query
    ON cost_entry(date, model, cost_group_key);

CREATE TABLE IF NOT EXISTS model_pricing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern TEXT NOT NULL UNIQUE,
    input_price REAL NOT NULL,
    output_price REAL NOT NULL,
    cache_read_price REAL,
    currency TEXT NOT NULL DEFAULT 'CNY'
);

-- Account type registry (mirrors upstream_accounts.account_type
-- for proxy-exported data; plan accounts get cost 0 in
-- cost_entry, their subscription + virtual costs live in
-- proxy_plan_summary).
CREATE TABLE IF NOT EXISTS account_types (
    account_name TEXT PRIMARY KEY,
    account_type TEXT NOT NULL DEFAULT 'api'
);

-- Per-month plan economics, written on export:
-- subscription_cost = monthly price (only for months with usage)
-- virtual_cost = api-billed amount of all plan usage that month
CREATE TABLE IF NOT EXISTS proxy_plan_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    account_name TEXT NOT NULL,
    subscription_cost REAL NOT NULL DEFAULT 0,
    virtual_cost REAL NOT NULL DEFAULT 0,
    UNIQUE(month, account_name)
);

-- ── 计价触发器 ─────────────────────────────────────────────────────────
-- 重算 cost_entry：从 token_usage 按 model_pricing 的缓存感知定价重算。
-- 缓存命中按 cache_read_price（缺省回落 input_price），输出按 output_price，
-- 其余按 input_price；account_types 里注册为 'plan' 的账户真实成本记 0。
-- CSV 导入的组（cost_entry.source='csv'）被排除，避免与 model_pricing 重复计价。

DROP TRIGGER IF EXISTS tr_mp_refresh_insert;
CREATE TRIGGER tr_mp_refresh_insert
AFTER INSERT ON model_pricing
BEGIN
    DELETE FROM cost_entry WHERE source = 'proxy';
    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)
    SELECT
        tu.date, tu.model,
        SUM(
            CASE WHEN COALESCE(
                (SELECT at.account_type FROM account_types at
                 WHERE at.account_name = tu.cost_group_key), 'api') = 'plan'
            THEN 0
            ELSE
            CASE tu.token_type
                WHEN 'output' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.output_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                WHEN 'input_cache_hit' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT COALESCE(mp.cache_read_price, mp.input_price) FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                ELSE
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.input_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
            END
            END
        ),
        tu.cost_group_key,
        'proxy'
    FROM token_usage tu
    WHERE NOT EXISTS (
        SELECT 1 FROM cost_entry ce
        WHERE ce.source = 'csv'
          AND ce.date = tu.date
          AND ce.model = tu.model
          AND ce.cost_group_key = tu.cost_group_key
    )
    GROUP BY tu.date, tu.model, tu.cost_group_key;
END;

DROP TRIGGER IF EXISTS tr_mp_refresh_update;
CREATE TRIGGER tr_mp_refresh_update
AFTER UPDATE ON model_pricing
BEGIN
    DELETE FROM cost_entry WHERE source = 'proxy';
    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)
    SELECT
        tu.date, tu.model,
        SUM(
            CASE WHEN COALESCE(
                (SELECT at.account_type FROM account_types at
                 WHERE at.account_name = tu.cost_group_key), 'api') = 'plan'
            THEN 0
            ELSE
            CASE tu.token_type
                WHEN 'output' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.output_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                WHEN 'input_cache_hit' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT COALESCE(mp.cache_read_price, mp.input_price) FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                ELSE
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.input_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
            END
            END
        ),
        tu.cost_group_key,
        'proxy'
    FROM token_usage tu
    WHERE NOT EXISTS (
        SELECT 1 FROM cost_entry ce
        WHERE ce.source = 'csv'
          AND ce.date = tu.date
          AND ce.model = tu.model
          AND ce.cost_group_key = tu.cost_group_key
    )
    GROUP BY tu.date, tu.model, tu.cost_group_key;
END;

DROP TRIGGER IF EXISTS tr_mp_refresh_delete;
CREATE TRIGGER tr_mp_refresh_delete
AFTER DELETE ON model_pricing
BEGIN
    DELETE FROM cost_entry WHERE source = 'proxy';
    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)
    SELECT
        tu.date, tu.model,
        SUM(
            CASE WHEN COALESCE(
                (SELECT at.account_type FROM account_types at
                 WHERE at.account_name = tu.cost_group_key), 'api') = 'plan'
            THEN 0
            ELSE
            CASE tu.token_type
                WHEN 'output' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.output_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                WHEN 'input_cache_hit' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT COALESCE(mp.cache_read_price, mp.input_price) FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                ELSE
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.input_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
            END
            END
        ),
        tu.cost_group_key,
        'proxy'
    FROM token_usage tu
    WHERE NOT EXISTS (
        SELECT 1 FROM cost_entry ce
        WHERE ce.source = 'csv'
          AND ce.date = tu.date
          AND ce.model = tu.model
          AND ce.cost_group_key = tu.cost_group_key
    )
    GROUP BY tu.date, tu.model, tu.cost_group_key;
END;
