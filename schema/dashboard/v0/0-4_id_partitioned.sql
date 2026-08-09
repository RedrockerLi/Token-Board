-- 0004: dashboard 存档按 account_id 分桶 + accounts 元数据表
--
-- DESTRUCTIVE: 四表加 account_id 列 — 先备份 data/dashboard.db
--
-- 变更目标（账户身份统一为 id，名字只是属性）：
--   1. token_usage / request_usage / cost_entry / proxy_plan_summary 加
--      account_id INTEGER NOT NULL DEFAULT 0。存量行 account_id=0（占位），
--      由应用层 reconcile_accounts 按"名字列 → upstream_accounts.id"回填并
--      归并后，删掉名字列、重建 account_id 唯一索引。account_id=0 表示
--      "unknown"（存量孤儿桶/无 id 的账户），唯一索引保证 additive 不双计。
--   2. 新建 accounts 元数据表：account_id → name / account_type / deleted_at
--      （随配置同步的账户镜像，含已软删账户，供历史显示 JOIN 出名字）。
--   3. 不在本迁移动旧名字索引：存量不同名桶 account_id 均=0，若直接建
--      account_id 唯一索引会冲突。索引治理由 reconcile_accounts 完成。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001-0003 已应用的库重复执行本迁移是空操作（user_version 已推进）。

-- ── 1. 四表加 account_id（默认 0 = unknown）──────────────────────────────

ALTER TABLE token_usage         ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_usage       ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cost_entry          ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proxy_plan_summary  ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0;

-- ── 2. accounts 元数据表（账户镜像，随配置同步）────────────────────────────

CREATE TABLE IF NOT EXISTS accounts (
    account_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT 'api',
    deleted_at   TEXT
);
