# 数据库

两个 SQLite 库都开在 WAL 模式,统一 `PRAGMA busy_timeout=5000`、`foreign_keys=ON`。schema 由 `schema/<库名>/NNNN_*.sql` 版本化迁移定义,迁移机制与升级流程见 [database-migrations.md](database-migrations.md),本文只讲表结构、索引、触发器与计费口径。

时间字段统一存 UTC(`datetime('now')`),看板界面一律转 UTC+8 显示,峰谷档位边界也按 UTC+0 分钟存储(见 [billing-pricing.md](billing-pricing.md))。

## proxy.db

代理的运行库,表定义在 `schema/proxy/0001_initial.sql`,`user_version` 当前为 2。

### upstream_accounts — 上游账户

一个上游账户对应一个真实 API 服务端(或一个聚合账户)。

| 字段 | 说明 |
|------|------|
| `name` | 账户名,唯一 |
| `upstream_key` | 上游 API Key |
| `base_url` | 上游 Base URL,如 `https://uni-api.cstcloud.cn/v1` |
| `api_format` | `openai` / `openai_responses` / `anthropic`,上游线格式 |
| `is_aggregate` | 是否聚合账户(聚合的模型映射在 `aggregate_entries`) |
| `endpoint_path` | 自定义上游路径;为空则按 `api_format` 推导(`/chat/completions`、`/responses`、`/v1/messages`) |
| `auth_header` | `bearer` / `x-api-key` / `auto`(auto 按格式推导:anthropic 用 x-api-key,其余用 Bearer) |
| `account_type` | `api`(按量计费)或 `plan`(订阅套餐,调用免费) |
| `monthly_price` | plan 账户的月费 |
| `max_concurrency` | 并发限额,NULL=不限;达到限额的请求返回 429(聚合链内自动切下一个) |

### local_keys — 本地密钥

客户端用 `tb-` + 32 位 hex 的密钥访问代理,一把密钥绑定一个上游账户。删除密钥时 `request_log.local_key_id` 经 `ON DELETE SET NULL` 保留,历史计费不丢。密钥只在此表存明文,看板 API 返回时打码(前 6 后 4)。

### request_log — 请求日志与计费载体

每次请求写一行,是计费、消费报告、性能监控的共同数据源。

| 字段 | 说明 |
|------|------|
| `account_id` / `local_key_id` | 用了哪个账户、哪把密钥 |
| `model` | 实际调用的模型名(聚合账户为上游模型名) |
| `prompt_tokens` | 总输入,含缓存命中 |
| `completion_tokens` | 输出 |
| `cache_read_tokens` | 输入缓存命中部分 |
| `total_tokens` | 输入 + 输出 |
| `cost` | 真实成本,由触发器写时计价固化 |
| `virtual_cost` | plan 账户的虚拟 api 口径金额;api 账户为 0 |
| `exported` | 三态:0 未导出 / 1 已导出待上传 / 2 已确认上传 |
| `is_streaming` / `status_code` / `duration_ms` | 流式与否、状态码、耗时 |
| `requested_at` | UTC 时间 |

索引:`idx_rl_account`、`idx_rl_time`、`idx_rl_exported`。

计费触发器 `tr_request_log_insert`(0002 版本)在插入时按"当前定价 × 命中时段倍率"计算 `cost` / `virtual_cost` 并固化,公式:

```
cost = (miss/1e6) × input_price
     + (cache_read/1e6) × COALESCE(cache_read_price, input_price)
     + (completion/1e6) × output_price
     ,再乘以 requested_at 命中 pricing_slots 档位的 multiplier(无命中=1.0)
```

模型匹配是 `LOWER(model) GLOB LOWER(model_pattern) ORDER BY mp.id LIMIT 1`,即第一条匹配生效,`reorder_pricing` 交换 `model_pricing.id` 来改变匹配优先级。plan 账户 `cost=0`,`virtual_cost` 按上面公式算出;api 账户反之。`prompt_tokens + completion_tokens = 0` 的行不计价。写时固化的含义与改价行为见 [billing-pricing.md](billing-pricing.md)。

清理规则:`cleanup_exported_logs` 只删 `exported=2` 中最旧的行,保留最新 1 万条;`exported` 为 0 或 1 的绝不删除(仍待导出或待确认)。

### model_pricing 与 pricing_slots — 定价

`model_pricing` 每行一个 `model_pattern`(支持 `*` / `?` GLOB 通配),含 `input_price` / `output_price` / `cache_read_price`(缺省回落 input_price),单位元/百万 tokens。`pricing_slots` 给每个定价挂若干"当日时段×倍率"档位,`start_minute` / `end_minute` 是 UTC+0 当日分钟 `[0,1439]`,end 独占,`start > end` 表示跨午夜。档位删除随定价 `ON DELETE CASCADE`。

### account_models — 账户模型目录

`(account_id, model_id)` 唯一。看板"更新模型"从上游 `GET /models` 拉取后整体替换,供 /v1/models 与模型映射界面使用。

### aggregate_entries — 聚合账户映射

每行:聚合账户 `account_id`、`sort_order`、暴露的 `pattern`(精确模型名,无通配)、目标 `upstream_account_id`、`upstream_model`。同一模型可配多条,按 `sort_order, id` 顺序尝试,构成多账户回退链。

### perf_events 与 in_flight_requests — 运行时监控

`perf_events` 每请求一条性能事件(upstream/total 延迟、状态码、并发数),仅本地,代理每 5 分钟清理超过 24 小时的行。`in_flight_requests` 记录在途请求,供实时并发展示与线程池扩容判断;启动时清空残留,每 5 分钟清理超过 10 分钟的僵死记录。

### sync_config — WebDAV 配置

key/value 表,存同步服务器 `url` / `folder` / `username` / `password`。凭据只在此表,导出到云端的副本会先删除该表。

## dashboard.db

可视化库,表定义在 `schema/dashboard/0001_initial.sql`。日粒度聚合,`(date, model, api_key_name, ...)` 上建唯一索引,`INSERT OR REPLACE` 保证重跑幂等。

### token_usage / request_usage

`token_usage` 每行一天某用户某模型某 token 类型的量,`token_type` 取值 `output` / `input_cache_hit` / `input_cache_miss`,唯一键 `(date, model, api_key_name, token_type, cost_group_key)`。`request_usage` 同理记请求数,唯一键 `(date, model, api_key_name)`。`cost_group_key` 用于按用户分摊成本——选中某个用户时,`app/cost_allocator.py` 按同组内各用户的 token 占比分摊该组费用。

### cost_entry

`(date, model, cost, cost_group_key)` + `source`(`proxy` / `csv`)。代理导出的费用在这里按 `request_log.cost` 聚合固化;CSV 导入的费用按 CSV 自带金额写入。同一 `(date, model, cost_group_key)` 上有 csv 行时,proxy 行不再计入,保证两套价格互不重复。

### account_types / proxy_plan_summary

`account_types` 镜像 `upstream_accounts.account_type`,导出时写入,plan 账户在 `cost_entry` 里记 0。`proxy_plan_summary` 每月每 plan 账户一行:`subscription_cost`(当月有使用才计一次月费)、`virtual_cost`(当月该 plan 的 api 口径金额),导出时全量重写。

### model_pricing / pricing_slots

与 proxy.db 结构一致,导出时整表替换,用于多机配置镜像。`0002` 迁移删掉了 `tr_mp_refresh_*` 触发器——本库的 `cost_entry` 不再由触发器重算,历史行按迁移时的当前基础价回填一次,之后由导出链路给出精确成本。

## 连接约定

- 两进程都设 WAL、`busy_timeout=5000`、`foreign_keys=ON`。
- C++ 用预编译语句 + 内部 mutex 串行写;Python 每方法独立连接(SQLite 支持多读单写)。
- 迁移执行前对 `<库>.migrate.lock` 加 flock,C++ 与 Python 用同一把锁,见 [database-migrations.md](database-migrations.md)。
