-- 0018: 计价公式抽视图 —— v_pricing_rate 成为单一定价事实源。
--
-- 计价表达式的历史副本（0002 回填 / 0004 / 0006 / 0007 / 0012 / 0014）随迁移冻结，
-- 不再追加新副本。本迁移把「取价」部分（model_pricing 基本价 + 缓存价 COALESCE +
-- 币种 COALESCE）抽成视图，写时计价触发器（cost_frozen=0）与 C++ 快照计价
-- （stmt_snapshot_price_，cost_frozen=1）双端都改为从视图取价，消除双轨漂移。
--
-- 已知边界：峰谷档位（pricing_slots）与汇率（fx_rate）子查询依赖每行的
-- minute(requested_at) / date(requested_at)，视图无法参数化，必须留在两端。
-- 触发器与 0014 逐字节等价，仅 mp→v、两个 COALESCE 折叠为视图列。

CREATE VIEW IF NOT EXISTS v_pricing_rate AS
SELECT id,
       model_pattern,
       input_price,
       output_price,
       COALESCE(cache_read_price, input_price) AS cache_read_price,
       COALESCE(currency, 'CNY')              AS currency
FROM model_pricing;

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
WHEN NEW.cost_frozen = 0
BEGIN
    UPDATE request_log SET
        api_cost = COALESCE((
            SELECT (
                     (MAX(NEW.prompt_tokens - NEW.cache_read_tokens, 0) / 1000000.0) * v.input_price
                   + (NEW.cache_read_tokens / 1000000.0) * v.cache_read_price
                   + (NEW.completion_tokens / 1000000.0) * v.output_price
                   ) * COALESCE((
                       SELECT ps.multiplier FROM pricing_slots ps
                       WHERE ps.pricing_id = v.id
                         AND (
                             (ps.start_minute <= ps.end_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                 AND CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute)
                          OR (ps.start_minute >  ps.end_minute
                                 AND (CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 >= ps.start_minute
                                   OR CAST(strftime('%s', NEW.requested_at) AS INTEGER) % 86400 / 60 <  ps.end_minute))
                         )
                       ORDER BY ps.id LIMIT 1), 1.0)
                   * CASE WHEN v.currency = 'USD'
                          THEN COALESCE((
                              SELECT fr.rate FROM fx_rate fr
                              WHERE fr.base = 'USD' AND fr.quote = 'CNY'
                                AND fr.date <= date(NEW.requested_at)
                              ORDER BY fr.date DESC LIMIT 1
                          ), 1.0)
                          ELSE 1.0 END
            FROM v_pricing_rate v
            WHERE LOWER(NEW.model) GLOB LOWER(v.model_pattern)
            ORDER BY v.id LIMIT 1
        ), 0.0)
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;
