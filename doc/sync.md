# WebDAV 同步

Token Board 使用两条 WebDAV 数据链路：`token-board.db` 的配置镜像，以及 `dashboard.db` 的用量聚合存档。代理请求明细永远留在本机；用户点“导出数据”时，聚合结果才进入 dashboard 同步链路。

## 数据边界

### 配置云端文件

配置云端文件包含 `token-board.db` 中可同步的用户配置，包括：

- 上游账户、路由、模型定价、超时和 Plan 计费设置；
- 本地代理客户端密钥 (`client_keys.key_value`)；
- 上游账户的地址、名称、密钥掩码和生命周期（不含上游 API Key 明文）；
- WebDAV 地址、目录和用户名 (`sync_settings`，不含密码)；
- 智能体订阅、订阅实例、价格历史、软件来源和订阅绑定配置。

上游 API Key 明文 (`upstream_secrets.secret_value`) 与 WebDAV 密码
(`sync_settings.password`) 只保存在本机，不进入上传副本，也不会被云端配置覆盖。
上游密钥的掩码、槽位顺序、起始日和生命周期会上传，因此 plan 的密钥数量和订阅计费单位可以跨机器保持一致；没有本机明文 Key 的槽位不能在该机器路由请求。

本机生成、不会放进配置文件的内容包括：

- `request_log`、`request_attempts`、性能事件和在途请求；
- Agent adapter 导入游标 (`agent_software_runtime.cursor_json`) 与旧 importer 游标；
- 周期费用物化行、汇率缓存、同步状态和高水位检查点。

### Dashboard 云端文件

dashboard 导出文件只包含聚合存档及必要的名称镜像：

- 统一用量与费用：`accounts`、`daily_usage`、`monthly_recurring_costs`；其中 `accounts.account_kind` 标识代理上游或智能体软件。

请求明细中的 `project` 与 `session_id` 不会进入 dashboard，也不会从请求日志 API 返回。

## 文件名与改名

每次上传生成带时间戳的新文件，云端旧文件不删除：

- 配置：`token-board_config_YYYYMMDD_HHMMSS.db`；
- Dashboard：`dashboard_sync_YYYYMMDD_HHMMSS.db`。

运行时只识别上述新前缀，旧的 `proxy_config_*.db` 不会被新版本拉取。配置的默认裸文件名为 `token-board_config.db`，仅用于生成/测试 URL；正常发布使用时间戳文件。

## 配置同步

配置同步采用云端权威镜像与冲突保护：

1. 看板启动时列出 `token-board_config_*.db`，拉取最新文件，通过
   `app.db.schema_upgrade.upgrade_downloaded_artifact` 在 shadow 中完成 SQL 和
   transition，再合入本机配置。
2. 管理页修改立即写入本机，代理可以立即使用；离开配置页时前端调用 `/api/proxy/sync/config/upload`。
3. 上传前重新读取云端最新文件并比较 `config_hash`。如果其他机器已经修改，拒绝覆盖并提示重新拉取。
4. 上传副本保留普通配置和本地代理客户端密钥，删除运行时数据、导入游标、上游 API Key 明文与 WebDAV 密码。成功后记录 hash 和 `token-board_config_snapshot.db` 本地快照。
5. 上传失败时可以重试；选择丢弃设置会从本地快照恢复配置，不需要网络。

配置合并按稳定 UUID/upstream credential UUID 做 upsert。云端缺失的普通配置行会变成停用 tombstone；本机上游 API Key 和 WebDAV 密码始终保留，需要在每台机器本地填写。

## Dashboard 导出事务

`POST /api/proxy/export` 执行以下完整事务：

1. 拉取最新 `dashboard_sync_*.db` 到 shadow；首次同步则以本地 dashboard 为种子。
2. 通过同一个 Python schema-upgrade 边界对 shadow 应用 dashboard SQL/transition，
   再同步账户/软件名称镜像。
3. 取 `request_log` 的 `(last_exported_log_id, max_id]`，按日×账户/软件×模型聚合到 shadow；同时物化并归档 Plan 与智能体订阅费用。
4. 上传 shadow 为新的 `dashboard_sync_*.db`。
5. 只有上传成功后才推进 `sync_state.last_exported_log_id`、替换本地 dashboard，并清理已归档且超过 30 天的明细。

任一步失败都会删除 shadow，不推进高水位，不替换本地 dashboard，也不清理未确认上传的明细。多机器同时上传时仍要求避免并发，后完成的完整文件可能覆盖先完成的文件。

## 运行时同步健康

`sync_state.sync_health` 记录最近一次错误，性能 API 会把它反映为 degraded。WebDAV 未配置时配置上传不会报错，只返回 `unconfigured`；Dashboard 导出则提示未配置同步服务器。
