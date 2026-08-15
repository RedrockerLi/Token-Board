# 数据库

两个 SQLite 库都开在 WAL 模式，统一 `busy_timeout=5000`、`foreign_keys=ON`；V1 新库启用 `auto_vacuum=INCREMENTAL`。schema 位于 `schema/<库>/v<major>/<major>-<minor>_*.sql`。

时间字段统一存 UTC(`datetime('now')`),前端按浏览器当地时区显示,峰谷档位边界按 UTC+0 分钟存储(见 [billing-pricing.md](billing-pricing.md))。

## V1 当前结构

Proxy V1 将身份、转发与计费拆开：`accounts` 是稳定计费主体；`upstreams` 是 endpoint/auth/concurrency；`route_sets` 与 `route_rules` 是唯一的路由模型；`client_keys` 只绑定 route set；`upstream_credentials` 保存稳定 UUID、掩码和生命周期，明文只存在本机 `upstream_secrets`；`account_importers` 表示 Codex 等导入源。

计费由 `billing_contracts`、`billing_rate_events`、`billing_period_charges`、`pricing_rules/rates/slots`、`fx_rates` 驱动。`request_log` 每请求一行，保存 theoretical `equivalent_cost` 与 actual usage `billed_usage_cost`；`request_attempts` 保存每次候选尝试和分段网络耗时。所有时间为 UTC，日志分页索引是 `(requested_at,id)`。

Dashboard V1 只保留 `accounts`、`daily_usage`、`monthly_recurring_costs`。`daily_usage` 的 grain 为 `UTC date × account × model`，同时保存 token、request、equivalent cost 和 billed usage cost。

以下章节记录 V0.19/V0.6 的旧表，供 transition 审计；新装不会创建这些实体表。

## V0 proxy.db（历史参考）

代理的运行库,表定义在 `schema/proxy/0001_*.sql`(0001–0019),`user_version` 当前为 19。

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
| `account_type` | `api`(按量计费)、`plan`(订阅套餐,调用免费)或 `agent`(Agent 订阅,如 Codex)。agent 与 plan 计费一致,但不绑定上游密钥、不可被路由,用量由后台导入 |
| `currency` | 订阅价原生币种 `CNY` / `USD`,默认 CNY |
| `agent_kind` | agent 账户的子类型,目前仅 `codex` |
| `max_concurrency` | 并发限额,NULL=不限;达到限额的请求返回 429(聚合链内自动切下一个) |
| `valid_from` | UTC 日期,per_account 订阅(agent)的订阅起始日;NULL 回落 `created_at` 的日期 |

`upstream_key` 列保留作为"legacy 单密钥"的退路(0007 起密钥改为存 `upstream_keys` 子表,见下)。

### local_keys — 本地密钥

客户端用 `tb-` + 32 位 hex 的密钥访问代理,一把密钥绑定一个上游账户。`account_id` 可空:删除账户时选「仅解绑」会让密钥的 `account_id` 置 NULL(前端显示"未分配",需重新选择);选「级联删除」则密钥随账户删除。删除密钥时 `request_log.local_key_id` 经 `ON DELETE SET NULL` 保留,历史计费不丢。密钥只在此表存明文,看板 API 返回时打码(前 6 后 4)。

### upstream_keys — 本地上游密钥生命周期

一行代表一把本机上游密钥。`valid_from` 是 UTC 日期（NULL 回落 `created_at` 的日期）；`deleted_at` 是 UTC 时间（`immediate` 删除=删除时刻，`end_of_period` 删除=本期期末，未来时间，到期前仍可路由）。移除密钥不会物理删除该行，而是写入 `deleted_at`，因此已归档账单能够稳定重算。明文 `key_value` 永远不上传。

`upstream_keys_cloud` 只保存 `key_masked`、起始日、位置、删除标记，供多机把同一物理密钥的账单归并；不含可用密钥材料。

每把密钥是一个独立的**并发槽位**(`max_concurrency` 按密钥计数,一把打满可溢出到同账户下一把)与独立**plan 订阅**——plan 月费 = 单价 × 本机密钥数。密钥收到 429 只冷却自己这把,同账户其他密钥可立即接管(见 [proxy-internals.md](proxy-internals.md))。

### plan_billing_config 与 plan_price_history — plan 计费设置

`plan_billing_config` 单行全局设置:`price_change_effective`(改价默认从本期还是下期生效)、`cancellation_mode`(删除 plan/agent 的默认操作:`immediate` 本期立即删除(本期计费) / `end_of_period` 到期立即删除(本期计费、下期不计费),默认 `immediate`)。`upstream_accounts.deferred_cleanup_mode` 记录 end_of_period 账户删除的延迟清理意图(detach/cascade)，由删除 finalizer 在 `deleted_at` 到期后执行。`plan_price_history` 记录每次月费变更:`account_id`、`monthly_price`、`changed_at`、`effective_mode`;订阅费由生命周期 + 价格历史重建,历史月份冻结、当月按当前设置刷新。

**`plan_price_history` 是月费唯一事实源**:`upstream_accounts.monthly_price` 列已在 0019 删除,当前价由最新 `current_period` 事件派生(`get_accounts`/`get_agent_accounts` 的子查询,输出键仍叫 `monthly_price`,前端无感知)。改价写入新历史事件,不更新任何列。

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
| `api_cost` | api 等价价:api 账户 = 真实账单,plan/agent = 虚拟口径(买套餐省下的金额)。0007 起 `cost` + `virtual_cost` 两列合并为这一列,由触发器或代理快照写时计价固化 |
| `upstream_key_id` | 实际转发用的上游密钥(0007 起)。无外键,密钥删除后保留 NULL |
| `event_id` | 幂等键(0012):事件日志重放(COMMIT 结果不确定)时保证同一事件只记一行。代理写入的行为 NULL,重放才填充 |
| `cost_frozen` | 0 = 由 `tr_request_log_insert` 触发器在插入时计价;1 = 代理在请求入队时刻已把价格快照写死(排队延迟不改账)。第三方/老写入保持 0 |
| `ttft_ms` / `generation_ms` / `output_tps` | 首字节延迟、生成耗时、输出速度(token/s),0009 起 |
| `upstream_ttft_ms` / `upstream_duration_ms` | 上游侧首字节延迟与总耗时 |
| `attempt_count` / `fallback_count` | 本请求尝试的上游次数 / 回退次数(0010) |
| `is_streaming` / `status_code` / `duration_ms` | 流式与否、状态码、耗时 |
| `requested_at` | UTC 时间 |

### request_attempts — 每次上游尝试的明细

一次客户端请求对应 `request_log` 一行、`request_attempts` 多行(每尝试一个上游密钥一次)。回退链上失败的上游不会从监控里消失:每个尝试记 `account_id`(账户删除后 `ON DELETE SET NULL` 解绑显示身份)、`upstream_key_id`、`status_code`、`duration_ms`、`ttft_ms`、`is_timeout`、`error`,`UNIQUE(request_log_id, attempt_index)`。这就是"每个上游成功率"与回退诊断的数据源(0010/0011)。

索引:`idx_rl_account`、`idx_rl_time`、`idx_rl_key`(upstream_key_id)、`idx_rl_ttft_time`(requested_at, ttft_ms)、`idx_request_log_event_id`(event_id,唯一)。

计费触发器 `tr_request_log_insert`(0018 版本,基于 `v_pricing_rate` 视图)只在 `cost_frozen=0` 时触发,按"当前定价 × 命中时段倍率 × 币种汇率"计算 `api_cost` 固化;代理自身快照计价走 `cost_frozen=1`(入队时定价,排队延迟不改账,`stmt_snapshot_price_` 同样读 `v_pricing_rate` 视图)。公式:

```
api_cost = (miss/1e6) × input_price
         + (cache_read/1e6) × COALESCE(cache_read_price, input_price)
         + (completion/1e6) × output_price
         ,再乘以 requested_at 命中 pricing_slots 档位的 multiplier(无命中=1.0)
         ;若命中定价的 currency='USD',再乘以请求当天最近一条 USD→CNY 汇率(缺失按 1.0)
```

模型匹配是 `LOWER(model) GLOB LOWER(model_pattern) ORDER BY mp.id LIMIT 1`,即第一条匹配生效,`reorder_pricing` 交换 `model_pricing.id` 来改变匹配优先级。所有账户统一记 `api_cost`(api = 真实账单,plan/agent = 虚拟口径)。`prompt_tokens + completion_tokens = 0` 的行只记日志、不计价。写时固化的含义与改价行为见 [billing-pricing.md](billing-pricing.md)。

**`v_pricing_rate` 视图(0018)**:取价部分(`model_pricing` 基本价 + 缓存价/币种 COALESCE)的唯一事实源,触发器与 C++ 快照都从视图取价。峰谷档位(`pricing_slots`)与汇率(`fx_rate`)子查询依赖每行 `minute(requested_at)` / `date(requested_at)`,视图无法参数化,保留在两端。等价回归:`pricing_equivalence`(v17 触发器 vs v18 视图触发器,20 用例逐位相等)+ `pricing_snapshot_equiv`(C++ 快照 vs v18 触发器),均并入 ctest。

清理规则:同步进度由 `sync_state.last_exported_log_id` 单值提交检查点记录(无逐行 exported 标记)。
`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行;未计入存档的行永久保留。
检查点只在上传成功后推进,失败即回滚——见 [sync.md](sync.md)。

### sync_state — 同步检查点

key/value 表,存 `last_exported_log_id`:最近一次**完整成功**的拉取-导出-上传事务导出的最大 `request_log.id`。

### model_pricing 与 pricing_slots — 定价

`model_pricing` 每行一个 `model_pattern`(支持 `*` / `?` GLOB 通配),含 `input_price` / `output_price` / `cache_read_price`(缺省回落 input_price)与 `currency`(`CNY` / `USD`,默认 CNY),单位为原生币种 / 百万 tokens。`pricing_slots` 给每个定价挂若干"当日时段×倍率"档位,`start_minute` / `end_minute` 是 UTC+0 当日分钟 `[0,1439]`,end 独占,`start > end` 表示跨午夜。档位删除随定价 `ON DELETE CASCADE`。价格表无状态——只存当前值,改价只影响之后写入的请求与当月 plan 订阅。

### account_models — 账户模型目录

`(account_id, model_id)` 唯一。看板"更新模型"从上游 `GET /models` 拉取后整体替换,供 /v1/models 与模型映射界面使用。

### aggregate_entries — 聚合账户映射

每行:聚合账户 `account_id`、`sort_order`、暴露的 `pattern`(精确模型名,无通配)、目标 `upstream_account_id`、`upstream_model`。同一模型可配多条,按 `sort_order, id` 顺序尝试,构成多账户回退链。

### perf_events 与 in_flight_requests — 遗留监控表

`perf_events` 与 `in_flight_requests` 是早期版本的监控表,**当前代理已不再写入**——性能数据改走 `request_log` 的 ttft/generation/output_tps 列 + `request_attempts` 明细;实时并发来自代理**进程内**计数器(经 `GET /health` 的 `concurrency` 字段暴露)。两张表仍保留在 schema 中作兼容:启动时清空 `in_flight_requests` 残留,主循环每 5 分钟清理超过 10 分钟的僵死记录(老版本代理可能还在写)。

### sync_config — WebDAV 配置

key/value 表,存同步服务器 `url` / `folder` / `username` / `password`。凭据只在此表,导出到云端的副本会先删除该表。

### fx_rate 与 codex_import_state — 仅本机的运行时表

`fx_rate` 按 `(base, quote, date)` 存 USD→CNY 当日汇率(0013),Python 侧 `app/fx.py` 按 UTC 日拉取一次并落库,触发器和代理快照计价按请求日期取最近一条;拉不到用最近存值,仍无则 1.0(不换算)。`codex_import_state` 是 Codex 会话导入的增量游标(`path` → `size`/`mtime`/`last_line`),保证幂等续传。两表都**仅存本机**,配置上传时被剔除(见 [sync.md](sync.md))。

## dashboard.db

可视化**存档**库,表定义在 `schema/dashboard/0001_*.sql`(0001–0006),`user_version` 当前为 6。**纯存档**:只有用量与总价,无价格表、无任何重算能力。写入是**增量**的(`ON CONFLICT DO UPDATE … +=`),每批导出只加一次,永不双计、永不被改价回溯。

存档分桶键统一为 **`account_id`**(稳定身份),显示名字来自 `accounts` 元数据镜像表(0004 + 应用层 `reconcile_accounts` 把旧的名字列桶迁移成 id 桶、删掉名字列)。`accounts` 每行 `account_id → name`(0006 删除了从未被读的 `account_type`/`deleted_at` 镜像列),随配置同步、含已软删账户,供历史显示 JOIN 出名字。看板按用户筛选即按账户筛选，费用直接汇总该账户名下已归属的 V1 ledger 行。

### token_usage / request_usage

`token_usage` 每行一天某账户某模型某 token 类型的量,`token_type` 取值 `output` / `input_cache_hit` / `input_cache_miss`,唯一键 `(date, model, account_id, token_type)`。`request_usage` 同理记请求数,唯一键 `(date, model, account_id)`。V1 的 `daily_usage` 同时保存 `equivalent_cost`（理论成本）和 `billed_usage_cost`（实际用量成本），报表不再按 token 占比分摊。

### cost_entry

`(date, model, cost, account_id)`,唯一键 `(date, model, account_id)`。代理导出的费用在这里按 `request_log.api_cost` 聚合固化,一旦写入不再重算。CSV 导入已弃用,存量 CSV 数据已并入 DeepSeek 账户。

### proxy_plan_summary

每个”行政月 × 账户 × masked 上游密钥”一行:`subscription_cost` 是该密钥无论使用与否都产生的周期月费,`virtual_cost` 是该密钥所承载请求的 api 口径金额。订阅费由 `valid_from`、`deleted_at`(删除默认操作)和 `plan_price_history` 重建;虚拟消费仍按导出批次增量持久化。

### model_pricing / pricing_slots / account_types

已删除(`0003` 迁移)。dashboard 不保留任何价格/账户类型镜像。

## 连接约定

- 两进程都设 WAL、`busy_timeout=5000`、`foreign_keys=ON`。
- C++ 用预编译语句 + 内部 mutex 串行写;Python 每方法独立连接(SQLite 支持多读单写)。
- 迁移执行前对 `<库>.migrate.lock` 加 flock,C++ 与 Python 用同一把锁,见 [database-migrations.md](database-migrations.md)。
