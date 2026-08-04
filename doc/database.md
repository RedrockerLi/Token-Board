# 数据库

两个 SQLite 库都开在 WAL 模式,统一 `PRAGMA busy_timeout=5000`、`foreign_keys=ON`。schema 由 `schema/<库名>/NNNN_*.sql` 版本化迁移定义,迁移机制与升级流程见 [database-migrations.md](database-migrations.md),本文只讲表结构、索引、触发器与计费口径。

时间字段统一存 UTC(`datetime('now')`),看板界面一律转 UTC+8 显示,峰谷档位边界也按 UTC+0 分钟存储(见 [billing-pricing.md](billing-pricing.md))。

## proxy.db

代理的运行库,表定义在 `schema/proxy/0001_*.sql`(0001–0006),`user_version` 当前为 6。

### upstream_accounts — 上游账户

一个上游账户对应一个真实 API 服务端(或一个聚合账户)。**`id` 是账户身份(自增主键、永不回收),`name` 只是可改属性**——改名不破坏任何引用,一切按 `id` 关联。

| 字段 | 说明 |
|------|------|
| `id` | 主键,递增身份,永不回收 |
| `name` | 账户名,唯一,可随意改 |
| `upstream_key` | 上游 API Key |
| `deleted_at` | 软删除标记:非空 = 账户已停用(行保留、id 不回收,列表/路由过滤 `deleted_at IS NULL`) |
| `base_url` | 上游 Base URL,如 `https://uni-api.cstcloud.cn/v1` |
| `api_format` | `openai` / `openai_responses` / `anthropic`,上游线格式 |
| `is_aggregate` | 是否聚合账户(聚合的模型映射在 `aggregate_entries`) |
| `endpoint_path` | 自定义上游路径;为空则按 `api_format` 推导(`/chat/completions`、`/responses`、`/v1/messages`) |
| `auth_header` | `bearer` / `x-api-key` / `auto`(auto 按格式推导:anthropic 用 x-api-key,其余用 Bearer) |
| `account_type` | `api`(按量计费)或 `plan`(订阅套餐,调用免费) |
| `monthly_price` | plan 账户的月费 |
| `max_concurrency` | 并发限额,NULL=不限;达到限额的请求返回 429(聚合链内自动切下一个) |

### local_keys — 本地密钥

客户端用 `tb-` + 32 位 hex 的密钥访问代理,一把密钥绑定一个上游账户。`account_id` 可空:删除账户时选「仅解绑」会让密钥的 `account_id` 置 NULL(前端显示"未分配",需重新选择);选「级联删除」则密钥随账户删除。删除密钥时 `request_log.local_key_id` 经 `ON DELETE SET NULL` 保留,历史计费不丢。密钥只在此表存明文,看板 API 返回时打码(前 6 后 4)。

### upstream_keys — 本地上游密钥生命周期

一行代表一把本机上游密钥。`valid_from` 是 UTC 日期（NULL 回落 `created_at` 的日期）；`deleted_at` 是 UTC 时间。移除密钥不会物理删除该行，而是写入删除时间和当时的 `cancellation_grace_hours`，因此已归档账单能够稳定重算。明文 `key_value` 永远不上传。

`upstream_keys_cloud` 只保存 `key_masked`、起始日、位置、删除标记和宽限快照，供多机把同一物理密钥的账单归并；不含可用密钥材料。

### request_log — 请求日志与计费载体

每次请求写一行,是计费、消费报告、性能监控的共同数据源。

| 字段 | 说明 |
|------|------|
| `account_id` / `local_key_id` | 用了哪个账户、哪把密钥。账户软删除后行保留,`account_id` 仍指向该账户(id 永存),显示 JOIN `upstream_accounts` 取名字 |
| `model` | 实际调用的模型名(聚合账户为上游模型名) |
| `prompt_tokens` | 总输入,含缓存命中 |
| `completion_tokens` | 输出 |
| `cache_read_tokens` | 输入缓存命中部分 |
| `total_tokens` | 输入 + 输出 |
| `cost` | 真实成本,由触发器写时计价固化 |
| `virtual_cost` | plan 账户的虚拟 api 口径金额;api 账户为 0 |
| `is_streaming` / `status_code` / `duration_ms` | 流式与否、状态码、耗时 |
| `requested_at` | UTC 时间 |

索引:`idx_rl_account`、`idx_rl_time`。

计费触发器 `tr_request_log_insert`(0006 版本)在插入时按"当前定价 × 命中时段倍率"计算 `cost` / `virtual_cost` 固化(plan 判定 JOIN `upstream_accounts.account_type`),公式:

```
cost = (miss/1e6) × input_price
     + (cache_read/1e6) × COALESCE(cache_read_price, input_price)
     + (completion/1e6) × output_price
     ,再乘以 requested_at 命中 pricing_slots 档位的 multiplier(无命中=1.0)
```

模型匹配是 `LOWER(model) GLOB LOWER(model_pattern) ORDER BY mp.id LIMIT 1`,即第一条匹配生效,`reorder_pricing` 交换 `model_pricing.id` 来改变匹配优先级。plan 账户 `cost=0`,`virtual_cost` 按上面公式算出;api 账户反之。`prompt_tokens + completion_tokens = 0` 的行只记日志、不计价。写时固化的含义与改价行为见 [billing-pricing.md](billing-pricing.md)。

清理规则:同步进度由 `sync_state.last_exported_log_id` 单值提交检查点记录(无逐行 exported 标记)。
`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行;未计入存档的行永久保留。
检查点只在上传成功后推进,失败即回滚——见 [sync.md](sync.md)。

### sync_state — 同步检查点

key/value 表,存 `last_exported_log_id`:最近一次**完整成功**的拉取-导出-上传事务导出的最大 `request_log.id`。

### model_pricing 与 pricing_slots — 定价

`model_pricing` 每行一个 `model_pattern`(支持 `*` / `?` GLOB 通配),含 `input_price` / `output_price` / `cache_read_price`(缺省回落 input_price),单位元/百万 tokens。`pricing_slots` 给每个定价挂若干"当日时段×倍率"档位,`start_minute` / `end_minute` 是 UTC+0 当日分钟 `[0,1439]`,end 独占,`start > end` 表示跨午夜。档位删除随定价 `ON DELETE CASCADE`。价格表无状态——只存当前值,改价只影响之后写入的请求与当月 plan 订阅。

### account_models — 账户模型目录

`(account_id, model_id)` 唯一。看板"更新模型"从上游 `GET /models` 拉取后整体替换,供 /v1/models 与模型映射界面使用。

### aggregate_entries — 聚合账户映射

每行:聚合账户 `account_id`、`sort_order`、暴露的 `pattern`(精确模型名,无通配)、目标 `upstream_account_id`、`upstream_model`。同一模型可配多条,按 `sort_order, id` 顺序尝试,构成多账户回退链。

### perf_events 与 in_flight_requests — 运行时监控

`perf_events` 每请求一条性能事件(upstream/total 延迟、状态码、并发数),仅本地,代理每 5 分钟清理超过 24 小时的行。`in_flight_requests` 记录在途请求,供实时并发展示与线程池扩容判断;启动时清空残留,每 5 分钟清理超过 10 分钟的僵死记录。

### sync_config — WebDAV 配置

key/value 表,存同步服务器 `url` / `folder` / `username` / `password`。凭据只在此表,导出到云端的副本会先删除该表。

## dashboard.db

可视化**存档**库,表定义在 `schema/dashboard/0001_*.sql`(0001–0003),`user_version` 当前为 3。**纯存档**:只有用量与总价,无价格表、无任何重算能力。写入是**增量**的(`ON CONFLICT DO UPDATE … +=`),每批导出只加一次,永不双计、永不被改价回溯。

### token_usage / request_usage

`token_usage` 每行一天某用户某模型某 token 类型的量,`token_type` 取值 `output` / `input_cache_hit` / `input_cache_miss`,唯一键 `(date, model, api_key_name, token_type, cost_group_key)`。`request_usage` 同理记请求数,唯一键 `(date, model, api_key_name)`。`cost_group_key` 用于按用户分摊成本——选中某个用户时,`app/cost_allocator.py` 按同组内各用户的 token 占比分摊该组费用。

### cost_entry

`(date, model, cost, cost_group_key)`。代理导出的费用在这里按 `request_log.cost` 聚合固化,一旦写入不再重算。CSV 导入已弃用,存量 CSV 数据已并入 DeepSeek 账户。

### proxy_plan_summary

每个“行政月 × plan 账户 × masked 上游密钥”一行：`subscription_cost` 是该密钥无论使用与否都产生的周期月费，`virtual_cost` 是该密钥所承载请求的 api 口径金额。订阅费由 `valid_from`、删除宽限和 `plan_price_history` 重建；虚拟消费仍按导出批次增量持久化。

### model_pricing / pricing_slots / account_types

已删除(`0003` 迁移)。dashboard 不保留任何价格/账户类型镜像。

## 连接约定

- 两进程都设 WAL、`busy_timeout=5000`、`foreign_keys=ON`。
- C++ 用预编译语句 + 内部 mutex 串行写;Python 每方法独立连接(SQLite 支持多读单写)。
- 迁移执行前对 `<库>.migrate.lock` 加 flock,C++ 与 Python 用同一把锁,见 [database-migrations.md](database-migrations.md)。
