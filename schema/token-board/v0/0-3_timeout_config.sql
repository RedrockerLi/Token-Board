-- 0003: 代理超时配置（按客户端线格式分组，仿 cc-switch 的 per-app 超时）
--
-- 三档超时（秒），0 表示禁用对应档：
--   streaming_first_byte_timeout — 等待首个流式数据块的最大时间（1-120）
--   streaming_idle_timeout       — 两个数据块之间的最大间隔（0-600，0=禁用静默超时）
--   non_streaming_timeout        — 非流式请求的整体读取超时（60-1200）
--
-- 默认值对齐 cc-switch：anthropic(claude) 90/180/600，openai_responses(codex) 与
-- openai(gemini) 60/120/600。C++ 代理每次转发时按客户端线格式读取本表。
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。

CREATE TABLE IF NOT EXISTS proxy_timeout_config (
    app_type                     TEXT PRIMARY KEY
        CHECK (app_type IN ('anthropic','openai_responses','openai')),
    streaming_first_byte_timeout INTEGER NOT NULL DEFAULT 60,
    streaming_idle_timeout       INTEGER NOT NULL DEFAULT 120,
    non_streaming_timeout        INTEGER NOT NULL DEFAULT 600
);

INSERT OR IGNORE INTO proxy_timeout_config
    (app_type, streaming_first_byte_timeout, streaming_idle_timeout, non_streaming_timeout)
VALUES
    ('anthropic',        90, 180, 600),
    ('openai_responses', 60, 120, 600),
    ('openai',           60, 120, 600);
