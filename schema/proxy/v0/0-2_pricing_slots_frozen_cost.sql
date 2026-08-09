-- 0002: 峰谷定价（多时段档位×倍率）+ 写时计价（固化价格）
--
-- 变更目标：
--   1. 新增 pricing_slots 表：每个模型定价可配置多个"每日时段×倍率"。
--      档位边界以"当日分钟"存 UTC+0（[0,1439]），end_minute 为独占结束，
--      start_minute > end_minute 表示跨午夜。
--   2. 价格在写入 request_log 时按当时定价（含档位倍率）计算并固化；
--      删除 tr_pricing_insert/update/delete —— 改价不再回溯重算历史成本。
--   3. 用当前 model_pricing + 各记录 requested_at 时刻的档位倍率，
--      回填现存 request_log 的 cost / virtual_cost。
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001 已应用的库重复执行本迁移是空操作（user_version 已推进）。

-- ── 1. 峰谷档位表 ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pricing_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_id   INTEGER NOT NULL REFERENCES model_pricing(id) ON DELETE CASCADE,
    start_minute INTEGER NOT NULL,   -- 当日分钟（UTC+0），[0,1439]
    end_minute   INTEGER NOT NULL,   -- 独占结束；start>end 表示跨午夜
    multiplier   REAL NOT NULL DEFAULT 1.0
);

-- ── 2. 去掉改价回溯重算触发器 ───────────────────────────────────────────

DROP TRIGGER IF EXISTS tr_pricing_insert;
DROP TRIGGER IF EXISTS tr_pricing_update;
DROP TRIGGER IF EXISTS tr_pricing_delete;

-- ── 3. 重建 tr_request_log_insert：插入时按"当前定价 × 命中档位倍率"计价 ──
--
-- 档位倍率：取 requested_at（UTC）当日分钟命中的档位 multiplier；
-- 无命中 = 1.0。M = CAST(strftime('%s', requested_at) AS INTEGER) % 86400 / 60

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
BEGIN
    UPDATE request_log SET
        cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id) = 'plan'
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
        virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = NEW.account_id) = 'plan'
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

-- ── 4. 回填现存数据：按当前配置 + 各记录时刻档位倍率重算 ────────────────

UPDATE request_log SET
    cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = request_log.account_id) = 'plan'
        THEN 0.0
        ELSE COALESCE((
            SELECT (
                     (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                   + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                   + (request_log.completion_tokens / 1000000.0) * mp.output_price
                   ) * COALESCE((
                       SELECT ps.multiplier FROM pricing_slots ps
                       WHERE ps.pricing_id = mp.id
                         AND (
                             (ps.start_minute <= ps.end_minute
                                 AND CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                 AND CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute)
                          OR (ps.start_minute >  ps.end_minute
                                 AND (CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                   OR CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute))
                         )
                       ORDER BY ps.id LIMIT 1), 1.0)
            FROM model_pricing mp
            WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
            ORDER BY mp.id LIMIT 1
        ), 0.0)
    END,
    virtual_cost = CASE WHEN (SELECT account_type FROM upstream_accounts WHERE id = request_log.account_id) = 'plan'
        THEN COALESCE((
            SELECT (
                     (MAX(request_log.prompt_tokens - request_log.cache_read_tokens, 0) / 1000000.0) * mp.input_price
                   + (request_log.cache_read_tokens / 1000000.0) * COALESCE(mp.cache_read_price, mp.input_price)
                   + (request_log.completion_tokens / 1000000.0) * mp.output_price
                   ) * COALESCE((
                       SELECT ps.multiplier FROM pricing_slots ps
                       WHERE ps.pricing_id = mp.id
                         AND (
                             (ps.start_minute <= ps.end_minute
                                 AND CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                 AND CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute)
                          OR (ps.start_minute >  ps.end_minute
                                 AND (CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                   OR CAST(strftime('%s', request_log.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute))
                         )
                       ORDER BY ps.id LIMIT 1), 1.0)
            FROM model_pricing mp
            WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
            ORDER BY mp.id LIMIT 1
        ), 0.0)
        ELSE 0.0
    END
WHERE request_log.prompt_tokens + request_log.completion_tokens > 0;
