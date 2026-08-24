-- 0002: 峰谷定价镜像 + cost_entry 写时固化
--
-- 变更目标：
--   1. 镜像新增 pricing_slots 表（与 token-board.db 结构一致，供多机同步/未来使用；
--      本库的 cost 计算不再依赖档位——cost_entry 由导出聚合 request_log.cost 固化）。
--   2. 删除 tr_mp_refresh_insert/update/delete：改价不再回溯重算 cost_entry。
--   3. 用当前 model_pricing 基础价回填现存 cost_entry(source='proxy')
--      （日粒度丢失小时信息，历史无法精确按档位回溯；新数据由导出链路得到精确成本）。
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001 已应用的库重复执行本迁移是空操作（user_version 已推进）。

-- ── 1. 峰谷档位表（镜像，仅结构一致）────────────────────────────────────

CREATE TABLE IF NOT EXISTS pricing_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_id   INTEGER NOT NULL REFERENCES model_pricing(id) ON DELETE CASCADE,
    start_minute INTEGER NOT NULL,   -- 当日分钟（UTC+0），[0,1439]
    end_minute   INTEGER NOT NULL,   -- 独占结束；start>end 表示跨午夜
    multiplier   REAL NOT NULL DEFAULT 1.0
);

-- ── 2. 去掉改价回溯重算触发器 ───────────────────────────────────────────

DROP TRIGGER IF EXISTS tr_mp_refresh_insert;
DROP TRIGGER IF EXISTS tr_mp_refresh_update;
DROP TRIGGER IF EXISTS tr_mp_refresh_delete;

-- ── 3. 回填 cost_entry：按当前 model_pricing 基础价重算（源=proxy）──────
-- 与旧触发器主体同构；CSV 导入行（source='csv'）保留不动。

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
