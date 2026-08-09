-- 0014: 计价触发器支持 USD 定价 —— 按 fx_rate 自动换算为 CNY。
--
-- tr_request_log_insert 在 cost_frozen=0 时写时计价（proxy 自身快照计价走 cost_frozen=1，
-- C++ 侧独立实现同一换算，见 db.cpp snapshot_request_cost）。本迁移仅把 0012 的触发器
-- 重建为：当命中的 model_pricing.currency='USD' 时，用请求当天（date(requested_at)）
-- 最近一条 USD→CNY 汇率乘回去。汇率表为空或找不到时倍率取 1.0（不失败）。

DROP TRIGGER IF EXISTS tr_request_log_insert;
CREATE TRIGGER tr_request_log_insert
AFTER INSERT ON request_log
WHEN NEW.cost_frozen = 0
BEGIN
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
                   * CASE WHEN COALESCE(mp.currency, 'CNY') = 'USD'
                          THEN COALESCE((
                              SELECT fr.rate FROM fx_rate fr
                              WHERE fr.base = 'USD' AND fr.quote = 'CNY'
                                AND fr.date <= date(NEW.requested_at)
                              ORDER BY fr.date DESC LIMIT 1
                          ), 1.0)
                          ELSE 1.0 END
            FROM model_pricing mp
            WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
            ORDER BY mp.id LIMIT 1
        ), 0.0)
    WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
END;
