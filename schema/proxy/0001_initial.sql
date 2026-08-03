-- 0001: Token Board proxy.db 初始 schema（基线迁移）
--
-- 单一 DDL 来源：proxy.db 的全部表/索引/触发器都由此文件（及其后的 NNNN_*.sql）
-- 定义，C++ 代理与 Python/Flask 共用同一份。历史库（user_version=0）首次应用时：
--   * 表/索引用 IF NOT EXISTS → 对已有库是无操作；
--   * 触发器用 DROP+CREATE → 把计价触发器刷成当前定义；
--   * 遗留孤儿表（model_map_templates / model_map_template_entries，零引用、0 行）
--     被清理。
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。

CREATE TABLE IF NOT EXISTS upstream_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    upstream_key TEXT NOT NULL,
    base_url    TEXT NOT NULL DEFAULT '',
    api_format  TEXT NOT NULL DEFAULT 'openai',
    is_aggregate INTEGER NOT NULL DEFAULT 0,
    endpoint_path TEXT NOT NULL DEFAULT '',
    auth_header TEXT NOT NULL DEFAULT 'bearer',
    account_type TEXT NOT NULL DEFAULT 'api',
    monthly_price REAL NOT NULL DEFAULT 0,
    max_concurrency INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS local_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_value   TEXT NOT NULL UNIQUE,
    label       TEXT,
    account_id  INTEGER NOT NULL REFERENCES upstream_accounts(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS request_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES upstream_accounts(id),
    local_key_id     INTEGER REFERENCES local_keys(id) ON DELETE SET NULL,
    model            TEXT NOT NULL,
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    cost             REAL NOT NULL DEFAULT 0.0,
    virtual_cost     REAL NOT NULL DEFAULT 0.0,
    exported         INTEGER NOT NULL DEFAULT 0,
    is_streaming     INTEGER NOT NULL DEFAULT 0,
    status_code      INTEGER NOT NULL,
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    requested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rl_account
    ON request_log(account_id);
CREATE INDEX IF NOT EXISTS idx_rl_time
    ON request_log(requested_at);
CREATE INDEX IF NOT EXISTS idx_rl_exported
    ON request_log(exported);

CREATE TABLE IF NOT EXISTS model_pricing (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern  TEXT NOT NULL UNIQUE,
    input_price    REAL NOT NULL,
    output_price   REAL NOT NULL,
    cache_read_price REAL,
    currency       TEXT NOT NULL DEFAULT 'CNY'
);

CREATE TABLE IF NOT EXISTS account_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES upstream_accounts(id),
    model_id    TEXT NOT NULL,
    UNIQUE(account_id, model_id)
);

DROP TABLE IF EXISTS key_model_map;  -- legacy per-key model mapping (removed)

-- Aggregate account model routing: one entry per exposed model,
-- mapping to a real upstream account + upstream model name.
CREATE TABLE IF NOT EXISTS aggregate_entries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id           INTEGER NOT NULL REFERENCES upstream_accounts(id) ON DELETE CASCADE,
    sort_order           INTEGER NOT NULL DEFAULT 0,
    pattern              TEXT NOT NULL,
    upstream_account_id  INTEGER NOT NULL REFERENCES upstream_accounts(id) ON DELETE CASCADE,
    upstream_model       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS perf_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    model               TEXT NOT NULL,
    upstream_latency_ms INTEGER NOT NULL DEFAULT 0,
    total_latency_ms    INTEGER NOT NULL DEFAULT 0,
    status_code         INTEGER NOT NULL,
    is_error            INTEGER NOT NULL DEFAULT 0,
    concurrent_count    INTEGER NOT NULL DEFAULT 0,
    requested_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_perf_events_time
    ON perf_events(requested_at);

CREATE TABLE IF NOT EXISTS in_flight_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    local_key_id    INTEGER NOT NULL,
    account_id      INTEGER NOT NULL,
    model           TEXT NOT NULL,
    is_streaming    INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- WebDAV 同步配置（key/value），原由 app/sync.py 每次调用时自建，现并入迁移。
CREATE TABLE IF NOT EXISTS sync_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 遗留孤儿表清理：无任何代码引用、表中无数据，仅存在于历史库。
DROP TABLE IF EXISTS model_map_templates;
DROP TABLE IF EXISTS model_map_template_entries;

-- ── 计价触发器 ─────────────────────────────────────────────────────────
-- 自动按 model_pricing 计算 request_log 的 cost / virtual_cost。
-- plan 账户：真实成本记 0（订阅已覆盖），api 账单金额记入 virtual_cost。
-- api 账户：真实成本 = api 账单金额，virtual_cost = 0。

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id) = 'plan'
            THEN 0.0
            ELSE COALESCE((
                SELECT (MAX(NEW.prompt_tokens - NEW.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (NEW.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (NEW.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
        END,
        virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id) = 'plan'
            THEN COALESCE((
                SELECT (MAX(NEW.prompt_tokens - NEW.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (NEW.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (NEW.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
            ELSE 0.0
        END
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;

-- Trigger: recalculate all costs when a pricing entry is inserted.
DROP TRIGGER IF EXISTS tr_pricing_insert;
CREATE TRIGGER tr_pricing_insert
AFTER INSERT ON model_pricing
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN 0.0
            ELSE COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
        END,
        virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
            ELSE 0.0
        END;
END;

-- Trigger: recalculate all costs when a pricing entry is updated.
DROP TRIGGER IF EXISTS tr_pricing_update;
CREATE TRIGGER tr_pricing_update
AFTER UPDATE ON model_pricing
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN 0.0
            ELSE COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
        END,
        virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
            ELSE 0.0
        END;
END;

-- Trigger: recalculate all costs when a pricing entry is deleted.
DROP TRIGGER IF EXISTS tr_pricing_delete;
CREATE TRIGGER tr_pricing_delete
AFTER DELETE ON model_pricing
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN 0.0
            ELSE COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
        END,
        virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE upstream_accounts.id = request_log.account_id) = 'plan'
            THEN COALESCE((
                SELECT (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                     + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0)
            ELSE 0.0
        END;
END;
