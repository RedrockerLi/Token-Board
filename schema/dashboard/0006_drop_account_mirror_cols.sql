-- 0006: 删除 accounts 镜像表从未被读的列。
--
-- account_types(0001) 已于 0003 删除、从未有写入,双镜像合并核实为 no-op;
-- 本迁移清理现存 accounts(0004)的只写不读列:load_rows 四个 JOIN
-- (token_usage/request_usage/cost_entry/proxy_plan_summary)只读 accounts.name
-- (dashboard_db.py),account_type / deleted_at 由 reconcile_accounts 写入但无
-- 任何读取方。删除后镜像只保留 (account_id, name)。
--
-- ⚠ 同版本升级:0006 与 reconcile_accounts 改为只写 (account_id, name) 必须同发;
--   sync_dashboard 的影子库在 reconcile 前先 migrate,远端旧列自动删。

ALTER TABLE accounts DROP COLUMN account_type;
ALTER TABLE accounts DROP COLUMN deleted_at;
