-- 0013: agent 账户类型 + 本地汇率表 + codex 导入状态表。
--
-- 变更目标：
--   1. upstream_accounts 新增 currency（CNY/USD，默认 CNY）：plan/agent 订阅价的原生币种。
--   2. fx_rate 本地汇率缓存：按 (base, quote, date) 存 USD→CNY 当日汇率，
--      Python 侧（app/fx.py）按 UTC 日拉取一次并落库；触发器和 C++ 快照计价时按
--      最近日期取用。本表不上云（见 app/sync.py _RUNTIME_TABLES）。
--   3. codex_import_state：Codex 会话文件导入的增量游标（size/mtime/last_line），
--      幂等续传，同样仅存本机。
--   4. account_type='agent' 走与 plan 相同的订阅计费，但不产生上游密钥、不可被路由。

-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner 统一处理。

ALTER TABLE upstream_accounts ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'
    CHECK (currency IN ('CNY', 'USD'));

ALTER TABLE upstream_accounts ADD COLUMN agent_kind TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS fx_rate (
    base       TEXT NOT NULL DEFAULT 'USD',
    quote      TEXT NOT NULL DEFAULT 'CNY',
    date       TEXT NOT NULL,
    rate       REAL NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (base, quote, date)
);

CREATE TABLE IF NOT EXISTS codex_import_state (
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      INTEGER,
    last_line  INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    parsed_at  TEXT
);
