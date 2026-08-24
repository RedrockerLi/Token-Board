-- 0005: 账户改名历史表（account_renames）
--
-- 变更目标：
--   1. 记录每次账户改名（account_id, old_name → new_name），供云端配置同步与
--      dashboard 存档 rename 回放使用。改名后 request_log.account_name 快照会被
--      同事务回填为新名（修当场统计），但 dashboard 存档里改名前已导出的历史
--      用量桶仍以旧名分桶——sync_dashboard 在 shadow 上回放本表把旧名桶并入新名桶。
--   2. 本表纳入 CONFIG_TABLES 随配置云端权威同步；回放幂等（合并后旧名桶空，
--      再回放无操作），故不清理、不删除记录。
--
-- 注意：本文件不得包含 BEGIN/COMMIT/PRAGMA user_version —— 事务控制由迁移 runner
-- 统一处理。0001-0004 已应用的库重复执行本迁移是空操作（user_version 已推进）。

CREATE TABLE IF NOT EXISTS account_renames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    old_name    TEXT NOT NULL,
    new_name    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_account_renames_account
    ON account_renames(account_id);
