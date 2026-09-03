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

- 配置：`token-board_config_YYYYMMDD_HHMMSS.db.gz`；
- Dashboard：`dashboard_sync_YYYYMMDD_HHMMSS.db.gz`。

新版同时读取时间戳 `.db.gz` 和旧 `.db` artifact；同一时间戳同时存在两种格式时优先 gzip。旧的 `proxy_config_*.db` 不会被新版本拉取。配置的默认裸文件名为 `token-board_config.db`，仅用于生成/测试 URL；正常发布使用时间戳 gzip 文件。

### Artifact 压缩

配置和 Dashboard 的 SQLite artifact 在上传前使用 Python 标准库 gzip level 6 压缩，文件名以 `.db.gz` 表示编码；schema manifest 仍以 JSON 原文上传。下载端先流式保存 gzip payload，再解压到同目录临时文件，校验 gzip 完整性和 SQLite header 后原子替换目标文件。gzip 使用固定 `mtime=0`，但 SHA-256 仍针对解压后的 SQLite 字节。

压缩只作用于网络 artifact，不作用于正在使用的本地数据库、配置 snapshot、draft 或 pending 文件。内存按 1 MiB 分块处理，解压后的 artifact 超过 1 GiB、损坏或不是 SQLite 时会失败且不会覆盖已有文件。

首次 gzip 上传前应先升级所有具有同步写权限的设备。升级后客户端可继续读取旧 `.db`，但只发布 `.db.gz`，不长期双写未压缩文件；因此旧客户端不能继续写入 gzip artifact。首次 gzip 发布后如需回滚到旧客户端，必须先将最新 `.db.gz` 解压并重新发布为 `.db`。

## 配置同步

配置同步采用“启动拉取、单编辑者、云端权威”模型：

1. 看板启动后立即开放用量查看，同时异步列出 `token-board_config_*.db.gz` 和旧 `token-board_config_*.db`，拉取最新文件，通过 `app.db.schema_upgrade.upgrade_downloaded_artifact` 在 shadow 中完成 SQL 和 transition，再合入本机配置。拉取完成前配置 API 为只读。
2. 管理页修改立即写入本机，代理可以立即使用；离开配置页时前端调用 `/api/proxy/sync/config/upload`。
3. 配置上传只执行时间戳 artifact 的 WebDAV PUT；HTTP 2xx 即视为成功，不做 config hash/ETag 冲突检查，不做上传后 PROPFIND 确认。
4. 上传副本保留普通配置和本地代理客户端密钥，删除运行时数据、导入游标、上游 API Key 明文与 WebDAV 密码；副本压缩后上传。成功后推进 `token-board_config_snapshot.db` 本地权威快照。
5. 上传失败立即恢复最近成功的权威快照，不创建或恢复 durable pending。旧版本遗留 pending 会在下一次拉取前清理。

配置合并按稳定 UUID/upstream credential UUID 做 upsert。云端缺失的普通配置行会变成停用 tombstone；本机上游 API Key 和 WebDAV bootstrap 凭证始终保留，需要在每台机器本地填写。

## Dashboard 导出事务

`POST /api/proxy/export` 执行以下完整事务：

1. 拉取最新 `dashboard_sync_*.db.gz` 或旧 `dashboard_sync_*.db` 到 shadow；首次同步则以本地 dashboard 为种子。
2. 通过同一个 Python schema-upgrade 边界对 shadow 应用 dashboard SQL/transition，
   再同步账户/软件名称镜像。
3. 取 `request_log` 的 `(last_exported_log_id, max_id]`，按日×账户/软件×模型聚合到 shadow；同时物化并归档 Plan 与智能体订阅费用。
4. 压缩并上传 shadow 为新的 `dashboard_sync_*.db.gz`。
5. 只有上传成功后才推进 `sync_state.last_exported_log_id`、替换本地 dashboard，并清理已归档且超过 30 天的明细。

任一步失败都会删除 shadow，不推进高水位，不替换本地 dashboard，也不清理未确认上传的明细。多机器同时上传时仍要求避免并发，后完成的完整文件可能覆盖先完成的文件。

### 删除看板用户

`DELETE /api/proxy/dashboard/users` 接收 `{"name":"用户名称","prepare":true/false}`。更多用户窗口中的第一次删除带 `prepare=true`：按“下载云端 dashboard → 本机导出最新用量 → 删除目标用户”的顺序处理，但不上传；同一窗口后续删除只从本机归档移除。窗口关闭时调用 `POST /api/proxy/dashboard/users/upload`，该接口只上传已经修改过的本地 dashboard 存档，不下载、不重新导出，也不接收用户名称。上游账户配置和本机请求明细不在此操作范围内。

## 运行时同步健康

`sync_state.sync_health` 记录最近一次错误，性能 API 会把它反映为 degraded。配置会话状态由仪表板进程内维护，固定为 `syncing`、`writable`、`read_only`、`local_only`。WebDAV 未配置时配置上传只返回 `unconfigured`；Dashboard 导出仍按独立事务处理。Agent 导入、FX 和周期费用物化由 `token-maintenance` 服务负责。
