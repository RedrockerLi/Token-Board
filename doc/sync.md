# WebDAV 同步

多台电脑共用代理时,通过 WebDAV 同步两部分内容:配置(上游账户、本地密钥、定价等)和 dashboard 聚合数据。同步实现都在 `app/sync.py`,凭据与目标存在 `proxy.db` 的 `sync_config` 表。

## 同步什么、不传什么

上传的内容:

- 配置表(`CONFIG_TABLES`):`upstream_accounts`、`local_keys`、`model_pricing`、`pricing_slots`、`account_models`、`aggregate_entries`、超时配置、Plan 计费设置和价格历史。
- dashboard 存档(纯用量+总价,无价格表):`token_usage`、`request_usage`、`cost_entry`、`proxy_plan_summary`。
- Plan 计费配置和价格历史；以及仅含 `mask_key()` 结果、订阅起始日、取消标记的 `upstream_keys_cloud` 元数据。

绝不上传的内容:

- `request_log` 明细(只同步聚合结果,明细仅存本地)
- `upstream_keys.key_value` 与 `upstream_accounts.upstream_key` 的完整明文
- `perf_events` 性能事件
- `in_flight_requests` 在途请求
- `sync_config`(WebDAV 账号密码)

## 追加模式

每次上传都生成带时间戳的文件名,云端旧文件保留不删:配置是 `proxy_config_20260803_100000.db`,dashboard 是 `dashboard_sync_20260803_100000.db`。拉取时用 PROPFIND 列目录,按前缀过滤后取时间戳最新的一个;没有时间戳文件时回退到裸文件名(兼容旧布局)。由于文件名按时间戳排序即按时间排序,`sorted()` 的最后一个就是最新。

写入先经过 SQLite backup API 做一致性快照(`wal_checkpoint(TRUNCATE)` 后再 `backup`),保证 WAL 里的数据不丢。

## 高水位提交检查点

`request_log.exported` 三态标记已废弃。同步进度用一个**单值提交检查点**跟踪:`proxy.db` 的
`sync_state.last_exported_log_id` —— 记录「最近一次**完整成功**的拉取-导出-上传事务把 request_log
导出到了哪一行」。它只有两种语义:一行要么「已计入存档」(id ≤ 检查点),要么「未计入」(id > 检查点),
**没有逐行标记、没有中间态**。

- 检查点只在上传成功后推进(事务提交点);任一步失败 → 检查点保持原值 → 相当于回滚,下次重新导出这些行。
- 清理规则:`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行;未计入存档的行永久保留。

## 导出流水线(云端权威事务)

`POST /api/proxy/export` 触发 `sync_dashboard(proxy_db_path, dash_db_path)`,完整流程是一个事务:

1. **拉取**:下载云端最新的 `dashboard_sync_*.db` 到临时**影子库**;无云端文件(首次同步)→ 以当前本地
   dashboard.db 为种子上传,保留历史基线。
2. **对齐 schema**:对影子库应用 dashboard 迁移(云端可能是旧版本)。
3. **导出**:`export_to_dashboard(shadow, mark, max_id)` 把 `request_log` 中 `id ∈ (mark, max_id]` 的行
   按 日×账户名×模型 增量聚合写入影子库(`token_usage`/`request_usage`/`cost_entry`),并把 plan 订阅费
   按月持久化到 `proxy_plan_summary`(历史月冻结、当月按当前月费刷新)。`max_id` 取导出开始时的最大值,
   期间的并发新行留待下次。聚合时排除 model 为空或 `unknown` 的行。
4. **上传**:把影子库上传云端(时间戳文件名)。
5. **提交**(仅在上传成功后):推进 `last_exported_log_id = max_id`;用影子库替换本地 dashboard.db;
   清理超 30 天的已导出日志行。

任何一步失败 → 删除影子库,本地 dashboard.db / 检查点 / 云端**全部不变**。因此:

- **云端永远是最新版本**,每台机器的本地 dashboard.db 永远是云端的一个历史版本;
- 上传失败不会丢数据(检查点不动,下次整库重传);不重算、不双计;
- 多机同步要求非并发(两机同时 pull-push 时时间戳文件 last-wins,后上传者覆盖先上传者本批)。

## 配置同步

配置走独立的 `proxy_config_*.db` 文件,采用**云端权威镜像**(改名/编辑/删除跨机传播):

- **上传**(一次事务):看板在用户**退出设置界面**时调 `POST /api/proxy/sync/config/upload` → `sync_config_upload`。副本先删掉 `request_log` / `perf_events` / `sync_config` / `in_flight_requests` / `sync_state`,并把 `upstream_accounts.upstream_key` 置空(**上游 API Key 绝不上传,每台机器各自填写的 Key 只存本机**),`VACUUM` 后上传。上传前先拉取最新云端文件算 config hash,与 `sync_state.config_hash` 比对:不一致(云端被其他机器改过,或本机尚未下载过)返回 `conflict` 拒绝覆盖,防止用落后配置冲掉云端新配置。
- **下载**(启动时):`create_app` 调 `sync_config_download`,取云端最新配置按**云端权威**合入本地:账户按 id upsert(保留本机非空上游 Key)、密钥按 key_value upsert、定价按 id upsert,聚合/账户模型/时段/超时整表替换,本地不在云端的行删除(delete-stale)。
- **回滚**:每台机在成功下载/上传的提交点维护本地快照 `data/config_snapshot.db`(含本机 Key,本地独有)。上传失败后用户选「丢弃设置」→ `POST /api/proxy/sync/config/discard` 从快照单事务回滚,不需要网络。

`sync_config_upload` / `sync_config_download` 都只在配置了 WebDAV 时生效,未配置上传返回 `unconfigured`。

## 容错细节

- WebDAV 客户端是精简实现,基于 `requests` 的 PROPFIND / MKCOL / GET / PUT,Basic 认证。
- MKCOL 建目录:`405` 表示已存在,`409` 是坚果云风格的返回,都视为成功;失败再 PROPFIND 确认。
- 下载遇 `404 / 409 / 410` 视为首次同步(云端还没有文件)。
- 测试连接 `_webdav_test` 先 MKCOL 探活,`401/403` 时提示"坚果云请使用应用密码"。
- 坚果云的 WebDAV 目录可能不支持直接覆盖,所以一律用带时间戳的新文件名,从不删旧文件。
