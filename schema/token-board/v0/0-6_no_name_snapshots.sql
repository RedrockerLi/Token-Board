-- 0006: 摒弃名字快照 —— request_log 删快照列、upstream_accounts 加软删除标记
--
-- DESTRUCTIVE: 重建 request_log（删 account_name / account_type / is_aggregate）、
-- 删 account_renames 死表 — 先备份 data/token-board.db
--
-- 变更目标（账户身份统一为 id，名字只是 upstream_accounts 的属性）：
--   1. request_log 删掉三个快照列，只留 account_id（FK）。一切按账户的统计/归档
--      GROUP BY account_id 并 JOIN upstream_accounts 取当前名字——改名只改 name
--      属性，历史数据按 id 自动归属，不再分裂。
--   2. upstream_accounts 加 deleted_at TEXT：软删除账户（id 永存、不回收），
--      request_log 永不 SET NULL；前端列表/路由过滤 deleted_at IS NULL。
--   3. tr_request_log_insert 去掉写快照的两行，保留写时计价（计价本就 JOIN
--      account_type，不依赖快照）。
--   4. 删 account_renames 死表（0005 建的改名历史，本版起用不到）。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001-0005 已应用的库重复执行本迁移是空操作（user_version 已推进）。
--
-- 外键安全：request_log 是子表（引用 upstream_accounts/local_keys），无表引用它，
-- 可直接 DROP 旧表再改名让位新表；先 DROP 旧触发器再重建。

-- ── 1. 重建 request_log（无快照列）────────────────────────────────────────

CREATE TABLE request_log_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER REFERENCES upstream_accounts(id) ON DELETE SET NULL,
    local_key_id      INTEGER REFERENCES local_keys(id) ON DELETE SET NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0.0,
    virtual_cost      REAL NOT NULL DEFAULT 0.0,
    is_streaming      INTEGER NOT NULL DEFAULT 0,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    requested_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO request_log_new (
    id, account_id, local_key_id, model, prompt_tokens,
    completion_tokens, cache_read_tokens, total_tokens, cost, virtual_cost,
    is_streaming, status_code, duration_ms, requested_at
)
SELECT
    id, account_id, local_key_id, model, prompt_tokens,
    completion_tokens, cache_read_tokens, total_tokens, cost, virtual_cost,
    is_streaming, status_code, duration_ms, requested_at
FROM request_log;

-- 删旧触发器与旧表（连带旧索引），新表改名让位
DROP TRIGGER IF EXISTS tr_request_log_insert;
DROP TABLE request_log;
ALTER TABLE request_log_new RENAME TO request_log;

CREATE INDEX IF NOT EXISTS idx_rl_account ON request_log(account_id);
CREATE INDEX IF NOT EXISTS idx_rl_time    ON request_log(requested_at);

-- ── 2. upstream_accounts 加软删除标记 ─────────────────────────────────────

ALTER TABLE upstream_accounts ADD COLUMN deleted_at TEXT;

-- ── 3. 删 account_renames 死表（0005 建的改名历史，本版不再使用）──────────

DROP TABLE IF EXISTS account_renames;

-- ── 4. 重建 tr_request_log_insert：只写时计价，不再写账户快照 ─────────────

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN COALESCE((SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id), 'api') = 'plan'
            THEN 0.0
            ELSE COALESCE((
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
        END,
        virtual_cost = CASE WHEN COALESCE((SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id), 'api') = 'plan'
            THEN COALESCE((
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
            ELSE 0.0
        END
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;
