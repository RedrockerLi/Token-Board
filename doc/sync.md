# WebDAV 同步

多台电脑共用代理时,通过 WebDAV 同步两部分内容:配置(上游账户、本地密钥、定价等)和 dashboard 聚合数据。同步实现都在 `app/sync.py`,凭据与目标存在 `proxy.db` 的 `sync_config` 表。

## 同步什么、不传什么

上传的内容:

- 配置表(`CONFIG_TABLES`):`upstream_accounts`、`local_keys`、`model_pricing`、`pricing_slots`、`account_models`、`aggregate_entries`。
- dashboard 聚合数据:`token_usage`、`request_usage`、`cost_entry`、`model_pricing`、`pricing_slots`、`account_types`、`proxy_plan_summary`。

绝不上传的内容:

- `request_log` 明细(只同步聚合结果,明细仅存本地)
- `perf_events` 性能事件
- `in_flight_requests` 在途请求
- `sync_config`(WebDAV 账号密码)

## 追加模式

每次上传都生成带时间戳的文件名,云端旧文件保留不删:配置是 `proxy_config_20260803_100000.db`,dashboard 是 `dashboard_sync_20260803_100000.db`。拉取时用 PROPFIND 列目录,按前缀过滤后取时间戳最新的一个;没有时间戳文件时回退到裸文件名(兼容旧布局)。由于文件名按时间戳排序即按时间排序,`sorted()` 的最后一个就是最新。

写入先经过 SQLite backup API 做一致性快照(`wal_checkpoint(TRUNCATE)` 后再 `backup`),保证 WAL 里的数据不丢。

## 三态 exported 标记

`request_log.exported` 是导出状态机,配合导出流水线保证"不重不漏":

- `0`:未导出。导出后置为 1。
- `1`:已写入 dashboard 但云端上传未确认。上传失败时保持 1,下次同步重新导出(幂等,`INSERT OR REPLACE`)。
- `2`:云端上传已确认。此后只参与清理,不再导出。

清理规则:`cleanup_exported_logs` 只删 `exported=2` 中最旧的记录,保留最新 1 万条;`exported` 为 0 或 1 的绝不删除。

## 导出流水线

`POST /api/proxy/export` 触发 `sync_dashboard(proxy_db_path, dash_db_path)`,完整流程:

1. 从云端下载最新的 `dashboard_sync_*.db` 到临时目录。
2. 有远端文件就 `_merge_dashboard` 合并进本地 `dashboard.db`:按表 `INSERT OR REPLACE`,远端覆盖本地。合并用的列集合与 dashboard 各表的唯一键对齐。
3. `export_to_dashboard(mark_exported=False)` 把 `request_log` 中 `exported IN (0,1)` 的行按 日×账户×模型 聚合,写入 `token_usage` / `request_usage` / `cost_entry(source='proxy')`,并镜像 `account_types`,全量重写 `proxy_plan_summary`。聚合时排除 model 为空或 `unknown` 的行。此步不推进 exported 标记,只做聚合。
4. 清理旧日志(`cleanup_exported_logs`,保留最新 1 万条已确认上传的)。
5. 把合并后的 `dashboard.db` 快照上传云端(时间戳文件名)。
6. 上传成功后 `mark_uploaded()` 把 `exported` 从 1 推进到 2。

这样一台机器的失败上传不会丢数据:它的行停在 1,下次同步从云端拉回其他机器已合并的数据后再补导出。`mark_exported=False` 的原因是云端确认必须发生在本地写入与云端上传都成功之后。

`export_to_dashboard` 单独调用时(`app/proxy_db.py` 直接调用,`mark_exported=True`)会把 0 推进到 1,不碰云端。

## 配置同步

配置走独立的 `proxy_config_*.db` 文件:

- 上传:看板任何配置写操作后,`_schedule_config_sync` 用 3 秒 debounce 触发 `sync_config_upload`。副本先删掉 `request_log` / `perf_events` / `sync_config` / `in_flight_requests`,`VACUUM` 后上传。
- 下载:看板启动时 `create_app` 调 `sync_config_download`,取云端最新配置按 `INSERT OR IGNORE` 合入本地。注意配置的方向与 dashboard 数据相反:云端只是传输通道,本地已有配置优先,远端只补缺。

`sync_config_upload` / `sync_config_download` 都只在配置了 WebDAV 时生效,未配置直接返回 false。

## 容错细节

- WebDAV 客户端是精简实现,基于 `requests` 的 PROPFIND / MKCOL / GET / PUT,Basic 认证。
- MKCOL 建目录:`405` 表示已存在,`409` 是坚果云风格的返回,都视为成功;失败再 PROPFIND 确认。
- 下载遇 `404 / 409 / 410` 视为首次同步(云端还没有文件)。
- 测试连接 `_webdav_test` 先 MKCOL 探活,`401/403` 时提示"坚果云请使用应用密码"。
- 坚果云的 WebDAV 目录可能不支持直接覆盖,所以一律用带时间戳的新文件名,从不删旧文件。
