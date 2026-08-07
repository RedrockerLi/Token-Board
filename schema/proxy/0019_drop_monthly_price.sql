-- 0019: 双价源去一存一 —— 删 upstream_accounts.monthly_price。
--
-- plan_price_history(0008) 已是计费权威(create_account/update_account 写事件,
-- get_stats/export/sync 全部读它、云端权威整表替换),monthly_price 只是
-- 显示/输入的便捷镜像,且 C++ 侧读取后从不消费(死读)。删除后当前价
-- 统一从 plan_price_history 最新 current_period 事件派生(get_accounts 子查询)。
--
-- 已核实本列无 trigger/view/index/CHECK 引用;0008 的 seed 在 v8 时已把
-- 存量账户的 monthly_price 灌入 plan_price_history,删除不丢计费数据。
--
-- ⚠ 同版本升级要求:0019 与 C++/Python 去除本列读写的改动必须同发
--   (旧二进制 prepare_statements 与旧 Python CRUD 会引用已删列,同 0017)。

ALTER TABLE upstream_accounts DROP COLUMN monthly_price;
