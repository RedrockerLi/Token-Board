-- 0007: 多密钥上游 + 统一 api_cost 计价
--
-- DESTRUCTIVE: 重建 request_log（cost+virtual_cost 合并为 api_cost、加 upstream_key_id）—
-- 先备份 data/proxy.db
--
-- 变更目标：
--   1. 新增 upstream_keys 子表：一个上游账户持有多把密钥（本地机密，不上云），
--      每把密钥一个并发槽位；plan 账户月费 = 单价 × 密钥数（按本机密钥数）。
--   2. 新增 session_key_log 本地观测表（会话→密钥分配，7 天滚动，不上云）。
--   3. request_log 的 cost+virtual_cost 合并为单列 api_cost（api 等价价，所有账户
--      统一记该列），新增可空 upstream_key_id（无 FK：密钥可删）。
--   4. tr_request_log_insert 改为对每行统一写 api_cost（去掉 account_type 分支）。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001-0006 已应用的库重复执行本迁移是空操作（user_version 已推进）。
--
-- 外键安全：upstream_keys 引用 upstream_accounts；request_log 引用 upstream_accounts/
-- local_keys，无表引用它，可直接 DROP 旧表再改名让位新表。先 DROP 旧触发器再重建。

-- ── 1. upstream_keys：账户的多把密钥（本地机密）──────────────────────────

CREATE TABLE IF NOT EXISTS upstream_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES upstream_accounts(id) ON DELETE CASCADE,
    key_value  TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,        -- 填充顺序 / 亲和首选顺序
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(account_id, key_value)
);
CREATE INDEX IF NOT EXISTS idx_uk_account ON upstream_keys(account_id, position);

-- 从旧单列 upstream_key 回填 position=0 的单条（非空才回填）
INSERT OR IGNORE INTO upstream_keys (account_id, key_value, position)
    SELECT id, upstream_key, 0 FROM upstream_accounts
    WHERE upstream_key IS NOT NULL AND upstream_key != '';

-- ── 2. session_key_log：会话→密钥分配观测（本地，7 天滚动，不上云）───────

CREATE TABLE IF NOT EXISTS session_key_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    account_id   INTEGER NOT NULL,
    key_id       INTEGER,                         -- upstream_keys.id，兜底槽 -1
    requested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_skl_session ON session_key_log(session_id);
CREATE INDEX IF NOT EXISTS idx_skl_time    ON session_key_log(requested_at);

-- ── 3. 重建 request_log：cost+virtual_cost → api_cost，加 upstream_key_id ──

CREATE TABLE request_log_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER REFERENCES upstream_accounts(id) ON DELETE SET NULL,
    local_key_id      INTEGER REFERENCES local_keys(id) ON DELETE SET NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    api_cost          REAL NOT NULL DEFAULT 0.0,  -- api 等价价：api=真实账单，plan=虚拟账单
    is_streaming      INTEGER NOT NULL DEFAULT 0,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    upstream_key_id   INTEGER,                    -- 无 FK：密钥可能被删
    requested_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 回填：旧 cost+virtual_cost 恰为每行的 api 等价价（api 行 cost=账单、virtual=0；
-- plan 行 cost=0、virtual=账单）。历史行没有密钥归属，upstream_key_id 置 NULL。
INSERT INTO request_log_new (
    id, account_id, local_key_id, model, prompt_tokens,
    completion_tokens, cache_read_tokens, total_tokens, api_cost,
    is_streaming, status_code, duration_ms, requested_at
)
SELECT
    id, account_id, local_key_id, model, prompt_tokens,
    completion_tokens, cache_read_tokens, total_tokens, (cost + virtual_cost),
    is_streaming, status_code, duration_ms, requested_at
FROM request_log;

-- 先删旧触发器与旧表（连带旧索引），新表改名让位
DROP TRIGGER IF EXISTS tr_request_log_insert;
DROP TABLE request_log;
ALTER TABLE request_log_new RENAME TO request_log;

CREATE INDEX IF NOT EXISTS idx_rl_account ON request_log(account_id);
CREATE INDEX IF NOT EXISTS idx_rl_time    ON request_log(requested_at);
CREATE INDEX IF NOT EXISTS idx_rl_key     ON request_log(upstream_key_id);

-- ── 4. 重建 tr_request_log_insert：每行统一写 api_cost（无 account_type 分支）──

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
BEGIN
    -- 计价表达式与 0002/0004/0006 完全一致（input 去缓存 + cache_read + output，
    -- × 当日时段倍率），只去掉外层 account_type 分支：所有账户统一记 api 等价价。
    UPDATE request_log SET
        api_cost = COALESCE((
            SELECT (
                     (MAX(NEW.prompt_tokens - NEW.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                   + (NEW.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                   + (NEW.completion_tokens / 1000000.0) * mp.output_price
                   ) * COALESCE((
                       SELECT ps.multiplier FROM pricing_slots ps
                       WHERE ps.pricing_id = mp.id
                         AND (
                             (ps.start_minute <= ps.end_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute)
                          OR (ps.start_minute >  ps.end_minute
                                 AND (CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                   OR CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute))
                         )
                       ORDER BY ps.id LIMIT 1), 1.0)
            FROM model_pricing mp
            WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
            ORDER BY mp.id LIMIT 1
        ), 0.0)
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;
