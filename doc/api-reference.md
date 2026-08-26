# API 参考

代理与看板各自暴露 HTTP API。转发端点在 C++ 代理(`proxy/src/proxy_server.cpp::setup_routes`),管理端点在 Flask 看板(`app/routes.py` 与 `app/proxy_routes.py`)。端点表按代码逐条核对,与当前实现一致。

## 代理转发端点

代理监听 8800(可配)。三个 chat 端点共用同一条管线,客户端格式由 URL 路径识别;`/v1/v1/*` 是兼容 base URL 已带 `/v1` 的客户端的别名。认证接受 `Authorization: Bearer <本地密钥>` 或 `x-api-key: <本地密钥>`。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容转发(harness 格式 OpenAI) |
| `/v1/messages` | POST | Anthropic 兼容转发(harness 格式 Anthropic) |
| `/v1/responses` | POST | OpenAI Responses 转发(harness 格式 Responses) |
| `/v1/embeddings` | POST | 嵌入转发(无格式转换) |
| `/v1/models` | GET | 模型列表:聚合账户返回其模型目录;带 `anthropic-version` 头的 Anthropic 客户端返回空目录 `{"models":[]}`;其余透传上游 |
| `/v1/v1/chat/completions` 等 | 同上 | 双 `/v1` 前缀别名 |
| `/v1/*` | OPTIONS | CORS 预检,返回 204 |
| `/health` | GET | 健康检查,返回 `{"status":"ok","service":"token-board-proxy","concurrency":N}`(concurrency 为进程内实时并发数) |

## 仪表板数据 API

看板默认固定监听 5000（可用 `TB_DASHBOARD_PORT` 覆盖）。数据来自 `dashboard.db`(`DataStore` 加载)。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 渲染 `templates/index.html`(SPA) |
| `/api/refresh` | GET | 重新扫描数据目录/重载数据库 |
| `/api/api_key_names` | GET | 用户(api_key_name)列表,按最近使用月份与当月用量排序 |
| `/api/models` | GET | 模型列表 |
| `/api/summary` | GET | 全量汇总:各 token 类型总量、请求数、费用、模型分解、plan 经济账 |
| `/api/monthly` | GET | 按月聚合(token、请求、费用、按模型) |
| `/api/daily` | GET | 指定月份的每日明细(含按模型) |
| `/api/token_types` | GET | Token 类型分布(输出/缓存命中/缓存未命中) |
| `/api/model_breakdown` | GET | 按模型分解 |
| `/api/token_types_by_month` | GET | 指定月份的 Token 类型分布 |

公共查询参数:`api_key_name`(按用户筛选,费用按 token 占比分摊)、`model`、`platform`、`year`、`month`。

## 代理管理 API

前缀 `/api/proxy`,仅在 `server.py --token-board-db` 传入 `data/token-board.db` 时启用。普通配置和本地代理密钥等在**退出配置页面**时作为一次事务上传云端；上游 API Key 明文与 WebDAV 密码不上传(见 [sync.md](sync.md))。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/agent-usage/import` | POST | 非阻塞唤醒服务器内置 Agent 用量 worker；网页每次加载时调用一次，返回 202 |

### 智能体管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/agent-types` | GET | 当前可用的软件解析类型（由 Python adapter registry 提供，包含 27 种本地 agent） |
| `/api/proxy/agent-subscriptions` | GET/POST | 查询/添加独立订阅（名称、开始时间、币种、一个或多个价格/开始日期实例） |
| `/api/proxy/agent-subscriptions/<id>` | PUT/DELETE | 修改或删除订阅 |
| `/api/proxy/agent-subscriptions/<id>/instances` | GET/POST | 查询或添加订阅实例（每个实例独立计费） |
| `/api/proxy/agent-subscription-instances/<id>` | PUT/DELETE | 修改或删除订阅实例 |
| `/api/proxy/agent-software` | GET/POST | 查询/添加软件来源（名称、类型、数据目录） |
| `/api/proxy/agent-software/<id>` | PUT/DELETE | 修改/软删除软件来源，并用 `subscription_ids` 替换订阅绑定；删除后保留身份和历史归属 |

### 账户与密钥

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/stats` | GET | 概览:总/今日请求数、总/今日费用、token 总量、今日活跃上游 |
| `/api/proxy/accounts` | GET | 上游账户列表 |
| `/api/proxy/accounts` | POST | 新建账户(name + upstream_key 必填) |
| `/api/proxy/accounts/<id>` | PUT | 更新账户字段 |
| `/api/proxy/accounts/<id>` | DELETE | 删除账户(有关联密钥或日志时拒绝) |
| `/api/proxy/accounts/<id>/cancel-deletion` | POST | 取消尚未到期的订阅账户删除 |
| `/api/proxy/accounts/<id>/models` | GET | 账户模型目录 |
| `/api/proxy/accounts/<id>/models` | POST | 从上游 `GET /models` 拉取并整体替换模型目录 |
| `/api/proxy/accounts/<id>/test-concurrency` | POST | 并发测试:不经本机代理、直连上游并行发 N 个极小请求(body 可传 `concurrency` 覆盖已保存限额,上限 50),自动挑选定价最便宜的可用模型,返回各档成功的统计与失败原因 |
| `/api/proxy/aggregates` | GET | 聚合账户列表(含模型映射) |
| `/api/proxy/aggregates` | POST | 新建聚合账户(至少一条映射,pattern 禁止通配符) |
| `/api/proxy/aggregates/<id>` | PUT | 更新聚合账户与映射 |
| `/api/proxy/aggregates/<id>` | DELETE | 删除聚合账户(映射级联删除) |
| `/api/proxy/keys` | GET | 本地密钥列表(key 打码:前 6 后 4) |
| `/api/proxy/keys` | POST | 生成新密钥,返回明文(仅此一次) |
| `/api/proxy/keys/<id>` | PUT | 更新标签/绑定账户 |
| `/api/proxy/keys/<id>` | DELETE | 删除密钥,历史日志经 `ON DELETE SET NULL` 保留 |

### 定价

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/pricing` | GET | 定价列表(含峰谷档位,边界为 UTC+0 分钟) |
| `/api/proxy/pricing` | POST | 新建定价(model_pattern 必填,可带 slots) |
| `/api/proxy/pricing/<id>` | PUT | 更新定价与档位(slots 整体替换) |
| `/api/proxy/pricing/<id>` | DELETE | 删除定价(档位级联删除) |
| `/api/proxy/pricing/reorder` | POST | 拖放调整匹配优先级(body: `{ids: [id, ...]}`，必须包含当前全部启用定价 ID) |

改价不影响已固化的历史成本,见 [billing-pricing.md](billing-pricing.md)。

### 超时配置

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/timeout-config` | GET | 三组超时配置(anthropic / openai_responses / openai),每组含 `streaming_first_byte_timeout`、`streaming_idle_timeout`、`non_streaming_timeout`(秒) |
| `/api/proxy/timeout-config` | PUT | 整体保存三组超时配置(body: `{<group>: {三个字段}, ...}`);范围校验:首字节 1-120、静默 0-600(0=禁用)、非流式 60-1200 |

配置存 `proxy_timeout_config` 表,代理每次转发按客户端线格式读取,保存后即时生效、无需重启。超时机制见 [proxy-internals.md](proxy-internals.md)。

### plan 计费设置

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/billing-config` | GET | Plan/智能体订阅计费设置:`price_change_effective`(改价默认本期/下期)、`cancellation_mode`(删除默认操作:`immediate` 本期立即删除(本期计费) / `end_of_period` 到期立即删除(本期计费、下期不计费)) |
| `/api/proxy/billing-config` | PUT | 整体保存 Plan/智能体订阅计费设置 |

### 消费与日志

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/billing` | GET | 按 账户×模型×日 聚合的账单(默认近 30 天,参数 `account_id`、`from`、`to`、`days`) |
| `/api/proxy/billing/daily` | GET | 近 `days` 天(默认 30)每日账单,滚动窗口 |
| `/api/proxy/billing/daily-by-model` | GET | 近 `days` 天每日所有非聚合上游的输入/输出/缓存命中 Token 分解(堆叠柱状图用);`cost` 仅统计 `api` 类型实际费用 |
| `/api/proxy/billing/recent-days` | GET | 近 `days` 天有数据的日期列表 |
| `/api/proxy/billing/today-upstreams` | GET | 今日各真实上游的 真实/理论费用、token、请求数 |
| `/api/proxy/logs` | GET | 简单分页请求日志(仅参数 `page`、`per_page`；`project` 与 `session_id` 不返回) |

### 导出与同步

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/export` | POST | 导出事务:拉取云端 → 导出本地新用量 → 上传 → 成功后推进检查点并替换本地存档(见 [sync.md](sync.md)) |
| `/api/proxy/dashboard/users` | DELETE | body `{"name":"用户名称","prepare":true/false}`；首次删除先下载云端存档并把本机最新用量导出到本地，随后只从本机删除该用户归档；退出“更多用户”界面后上传本地存档。不改上游账户配置和本机请求日志。 |
| `/api/proxy/dashboard/users/upload` | POST | 上传当前本地 dashboard 存档；不下载、不重新导出，也不接收用户名称。 |
| `/api/proxy/sync/config` | GET | 读取 WebDAV 配置(密码脱敏) |
| `/api/proxy/sync/config` | PUT | 保存 WebDAV 配置 |
| `/api/proxy/sync/test` | POST | 测试 WebDAV 连接 |
| `/api/proxy/sync/config/upload` | POST | 配置上传事务:hash 校验 → 副本剥离运行时表及敏感凭据 → 上传 → 记录 config_hash 与本地快照(见 [sync.md](sync.md)) |
| `/api/proxy/sync/config/discard` | POST | 丢弃未保存的设置,从本地快照 `token-board_config_snapshot.db` 单事务回滚 |

### 性能监控

性能数据源是 `request_log`(含 TTFT/生成耗时/输出速度列)与 `request_attempts`(每次上游尝试的明细)。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/perf/summary` | GET | 最近 N 分钟(默认 15)请求数、错误数、成功率、token、平均延迟 |
| `/api/proxy/perf/upstream-success-rate` | GET | 最近 N 分钟各真实上游成功率 |
| `/api/proxy/perf/latency` | GET | 最近 N 分钟按分钟桶的 P50/P95/P99 延迟 |
| `/api/proxy/perf/speed` | GET | 最近 N 分钟(默认 60)按分钟桶的输出速度(token/s)P50/P95/P99 分布(读 request_log.output_tps) |
| `/api/proxy/perf/throughput` | GET | 最近 N 分钟每分钟请求数 |
| `/api/proxy/perf/models` | GET | 最近 N 分钟按模型的请求数/平均延迟/成功率 |
| `/api/proxy/perf/realtime` | GET | 实时:RPM 估计、`in_flight_requests` 中的当前并发、在途明细 |

## 命令行参数

### 代理(token_proxy)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--db` | `data/token-board.db` | SQLite 数据库路径 |
| `--schema-dir` | — | 已废弃，仅兼容旧启动器；C++ 不读取 SQL、不执行升级。请先通过 Python `app.db.schema_upgrade.cli` 准备数据库 |
| `--port` | `8800` | 监听端口 |
| `--host` | `127.0.0.1` | 绑定地址(默认仅本机可访问) |
| `--log-level` | `info` | 日志级别(debug/info/warn/error) |
| `--help` | — | 显示帮助 |

### 看板(server.py)

| 参数 | 必填 | 说明 |
|------|------|------|
| `--port` | 是 | 监听端口 |
| `--host` | 否 | 绑定地址,默认 `127.0.0.1`(仅本机可访问) |
| `--token-board-db` | 否 | 传入 `data/token-board.db` 时启用代理管理功能与云端配置拉取 |
