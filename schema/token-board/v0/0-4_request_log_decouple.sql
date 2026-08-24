-- 0004: 日志与上游账户彻底解耦 + 快照列 + 高水位同步
--
-- DESTRUCTIVE: 重建 request_log / local_keys（改可空、删 exported 列、加快照列）— 先备份 data/token-board.db
--
-- 变更目标：
--   1. request_log 删掉 exported 列（三态标记废弃），改为 sync_state.last_exported_log_id 单值提交检查点。
--   2. request_log.account_id 可空 + ON DELETE SET NULL：删账户不再拦截、日志保留（账户名/类型/聚合标记快照）。
--   3. request_log 加快照列 account_name / account_type / is_aggregate：账户删除后仍可按名字归档/展示/计费。
--   4. local_keys.account_id 可空 + ON DELETE SET NULL：删账户弹窗二选一（级联删密钥 / 仅解绑，密钥置空待重分配）。
--   5. 重建触发器：对每行（含 0 token 行）写账户快照；对 token 行按 0002 的计价表达式实时算价固化（COALESCE 防护）。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner 统一处理。
-- 0001-0003 已应用的库重复执行本迁移是空操作（user_version 已推进）。
--
-- 外键安全说明：C++ 代理迁移时 foreign_keys=ON（db.cpp open 时设置），Python runner 默认关闭；
-- 本迁移必须在外键开启下也安全。采用「改名让位」顺序：先建新 local_keys 并让旧表改名退位，
-- 再建新 request_log（外键指向已就位的新 local_keys），最后按「先子后父」DROP 旧表。

-- ── 1. 重建 local_keys（account_id 可空 + ON DELETE SET NULL）────────────────

CREATE TABLE local_keys_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_value   TEXT NOT NULL UNIQUE,
    label       TEXT,
    account_id  INTEGER REFERENCES upstream_accounts(id) ON DELETE SET NULL,   -- 可空：仅解绑后置空
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_used_at TEXT
);
INSERT INTO local_keys_new (id, key_value, label, account_id, created_at, last_used_at)
    SELECT id, key_value, label, account_id, created_at, last_used_at FROM local_keys;
-- 旧表改名让位：SQLite 会把现存子表（request_log）指向 local_keys 的外键引用重定向到 local_keys_old
ALTER TABLE local_keys RENAME TO local_keys_old;
ALTER TABLE local_keys_new RENAME TO local_keys;

-- ── 2. 重建 request_log（删 exported；account_id/local_key_id 可空 + SET NULL；加快照列）─────

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
    account_name      TEXT,                              -- 快照：删账户后仍可归档/展示
    account_type      TEXT NOT NULL DEFAULT 'api',       -- 快照：plan/api
    is_aggregate      INTEGER NOT NULL DEFAULT 0,        -- 快照：聚合标记
    is_streaming      INTEGER NOT NULL DEFAULT 0,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    requested_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO request_log_new (
    id, account_id, local_key_id, model, prompt_tokens, completion_tokens,
    cache_read_tokens, total_tokens, cost, virtual_cost, account_name, account_type,
    is_aggregate, is_streaming, status_code, duration_ms, requested_at
)
SELECT
    r.id, r.account_id, r.local_key_id, r.model, r.prompt_tokens, r.completion_tokens,
    r.cache_read_tokens, r.total_tokens, r.cost, r.virtual_cost,
    ua.name, COALESCE(ua.account_type, 'api'), COALESCE(ua.is_aggregate, 0),
    r.is_streaming, r.status_code, r.duration_ms, r.requested_at
FROM request_log r
LEFT JOIN upstream_accounts ua ON r.account_id = ua.id;

-- 子表先删（连带删旧触发器/索引），此时 local_keys_old 无任何表引用，安全
DROP TABLE request_log;
DROP TABLE local_keys_old;
ALTER TABLE request_log_new RENAME TO request_log;

CREATE INDEX IF NOT EXISTS idx_rl_account ON request_log(account_id);
CREATE INDEX IF NOT EXISTS idx_rl_time    ON request_log(requested_at);

-- ── 3. 高水位提交检查点（取代 exported 三态标记）──────────────────────────────

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 初始化 = 当前最大 id：存量行视为已计入存档（其聚合已在现有 dashboard.db / 云端），
-- 之后每轮完整成功的「拉取-导出-上传」事务把高水位推进到当轮最大 id。
INSERT OR IGNORE INTO sync_state (key, value)
    VALUES ('last_exported_log_id', (SELECT COALESCE(MAX(id), 0) FROM request_log));

-- ── 4. 重建 tr_request_log_insert：每行账户快照 + token 行写时计价（COALESCE 防护）────

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
BEGIN
    -- ① 账户快照：对每行（含 0 token 的错误/中止请求），保证删账户后仍可按名字归档。
    UPDATE request_log SET
        account_name = (SELECT name FROM upstream_accounts WHERE id = NEW.account_id),
        account_type = COALESCE((SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id), 'api'),
        is_aggregate = COALESCE((SELECT is_aggregate FROM upstream_accounts WHERE id = NEW.account_id), 0)
    WHERE id = NEW.id;

    -- ② 计价：写时固化（实时按当时定价 × 档位倍率），plan 账户 cost=0、virtual_cost=api 账单；
    --    计价表达式与 0002 完全一致，仅 account_type 判定加 COALESCE（account_id 为空时按 api）。
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
