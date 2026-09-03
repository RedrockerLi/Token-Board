# 计费与定价

V1 的 proxy/import 用量都写同一种 `UsageEvent`。数据库在写入 `request_log` 时读取当前模型定价并固化 `equivalent_cost`；metered 合同同时写 `billed_usage_cost=equivalent_cost`，recurring 合同写 0，真实周期费用来自 `billing_period_charges` 或独立的智能体订阅周期表。上游 API 只展示 api/plan 两种类型，智能体配置走独立管理页。

## 定价模型

`pricing_rules` 直接保存 pattern、显式 priority 和当前价格；不再有 `pricing_rates` 历史表或价格有效期。`model_pattern` 支持 `*` / `?` GLOB 通配，单位为每百万 tokens。

- `input_price`:未命中缓存的输入单价
- `output_price`:输出单价
- `cache_read_price`:缓存命中的输入单价,缺省回落 `input_price`

匹配按 `priority,id`。模型定价页通过拖放一次提交完整优先级顺序；改价原地更新当前规则，只影响之后写入的请求。

### 输入长度条件档

每个定价规则可挂零个或多个 `pricing_length_tiers`。条件使用请求的
`prompt_tokens`（包含缓存输入）判断，门槛按 `prompt_tokens >= threshold_tokens`
命中；低于最小门槛时使用基本价格，命中多档时使用已达到的最高门槛档。
界面用十进制单位输入（`1K=1000`、`1M=1000000`），数据库保存整数 token。

条件档的输入、缓存命中、输出价格可以分别留空；留空字段继承基本价格，至少要
覆盖一个字段。先选择长度档，再把时段倍率乘到该档的三类价格上。新增或编辑条件档
会原地替换当前条件档，已有请求的冻结金额不回溯。

## 峰谷档位

`pricing_slots` 给每个定价挂若干档位,每档是"当日一个时段 × 倍率":

| 字段 | 含义 |
|------|------|
| `start_minute` / `end_minute` | 当日分钟,按 UTC+0 存,范围 `[0,1439]`,`end` 独占 |
| `multiplier` | 该时段倍率,默认 1.0 |

请求写入时取当前 UTC 时间换算成当日分钟 `M = CAST(strftime('%s', 'now') AS INTEGER) % 86400 / 60`,落在档位区间即命中,倍率乘到三档基础价上。`start > end` 表示跨午夜档(如 22:00–06:00),命中条件是 `M >= start OR M < end`。同一 pricing 有多档命中时取 `id` 最小的一档。

界面按浏览器所在时区输入和显示,存入库的仍是 UTC+0 当日分钟。换算使用浏览器当前的 `getTimezoneOffset()`:

- 本地分钟 → UTC 分钟: `local + offset`;
- UTC 分钟 → 本地分钟: `utc - offset`。

因此同一组数据库档位在不同电脑时区会显示为不同的当地时间,但代理后端始终按原 UTC 区间匹配。电脑时区变化不会改数据库值,夏令时只会改变前端看到的当地时刻。

## 写时计价固化

计费载体是 `request_log` 的**单列 `api_cost`**(0007 起 `cost` + `virtual_cost` 合并——api 账户记真实账单,plan 记虚拟口径,统一落这一列)。智能体用量同样进入 request log 以便导出，但订阅实际费用单独记录。

- **所有新写入**(`pricing_status='pending'`):由 SQLite 触发器在 INSERT 时按当前规则、当前档位和写入当天汇率计算并固化 `equivalent_cost`。
- 代理、Agent 导入和 spool 重放都走同一触发器；`event_id` 只负责导入/重放幂等。

V1.14 删除了模型定价历史和改价回溯路径,所以:

- 改价、增删档位、调整优先级、改汇率,都不再重算历史 `request_log`。
- 历史行的冻结金额在迁移时原样保留,此后保持冻结。
- `request_log.api_cost` 是唯一可信的记账值,导出到 `dashboard.db` 时直接按它聚合。

## plan 账户与虚拟消费

`account_type = 'plan'` 的账户代表订阅套餐,调用本身不按量收费。计费规则:

- `api_cost` 就是虚拟口径——即"这笔流量不买套餐的话要花多少钱"。真实成本不计入 `api_cost` 聚合(见下)。
- 每把上游密钥独立拥有 `valid_from` 起的行政月周期。锚点日为起始日的日号，短月取月末；密钥不需要产生用量也会产生每周期一次的订阅费。
- 所有边界使用 UTC+0。删除 plan 密钥或账户时按「删除默认操作」(`plan_billing_config.cancellation_mode`，默认 `immediate`)执行：**本期立即删除**(`immediate`，本期计费)把 `deleted_at` 设为删除时刻，即刻停用但本期照收月费；**到期立即删除**(`end_of_period`，本期计费、下期不计费)把 `deleted_at` 设为本期期末，订阅可继续使用至本期最后一天，到期后路由自动停止，本期照收、下期不再计费。api 账户无订阅生命周期，始终立即删除。智能体订阅只记录订阅事实，删除后不再生成未来费用。
- 月费修改会写入价格历史。设置页可选择默认从本期或下一期生效；不同锚点日的密钥会在各自对应的周期边界切换价格。
- plan 单把上游密钥收到 HTTP 429 后冷却 5 小时，同账户其他密钥仍可回退接管(见 [proxy-internals.md](proxy-internals.md))。冷却期内代理每 1 小时探测一次上游 `GET /models`,2xx 即提前解除冷却,不必等满 5 小时;探测不写 request_log、不占并发(见 [proxy-internals.md](proxy-internals.md) 冷却探测小节)。

plan 账户的 `api_cost`(虚拟口径)的意义是衡量套餐划不划算:实际花的钱是月费,虚拟消费是省下来的按量金额。

## 看板上的消费口径

数据来源:代理导出的数据按 `request_log.api_cost`(写时固化)聚合写入 `cost_entry`。CSV 导入已弃用,
存量 CSV 数据已并入 DeepSeek 账户,`cost_entry` 不再有 `source` 列。

- **总消费(真实)**:api 账户按量费用 + plan/智能体月费。主看板为存档全量口径;代理账单页为**近 30 天滚动**口径
  (近30天 api 账户 `SUM(api_cost)` + 当前仍应收费的每把 plan 密钥的本期月费;USD 订阅费按当日汇率折 CNY)。
- **今日消费**:今日全部 `SUM(api_cost)`(api 真实账单 + plan 虚拟口径,`get_stats` 限今日)。
- **理论消费**:api 按量 + plan 虚拟消费,即完全不买 plan 全按 api 计费应花的金额；智能体理论用量可在独立软件用量存档查看。

`proxy_plan_summary` 表按“行政月 × 账户 × masked 密钥”保存。订阅费由生命周期和价格历史校准，
虚拟消费仍是追加式归档。`/api/summary` 据此返回 `plan_subscription_cost` 与 `plan_virtual_cost`,
前端把月费加进总消费、把虚拟消费显示为"理论消费"。日志导出 30 天后清理，因此无法回填已清理的历史虚拟消费。

## 智能体订阅与软件用量

智能体已从上游账户模型中独立出来。**订阅**记录名称、开始时间和币种，并可包含多个独立计费实例；实例记录自己的开始日期和价格，价格历史与周期物化存于 `agent_subscription_rate_events` / `agent_subscription_period_charges`。**软件**记录名称、类型和数据目录，类型由 Python agent adapter registry 提供（当前包含 27 种本地 agent）。订阅和软件通过绑定表多对多关联：一个软件可以使用多个订阅，一个订阅也可以覆盖多个软件。

- 用量由**token-maintenance 进程内的单一 worker** 导入:维护服务启动时立即运行一次,之后每 30 分钟运行一次;浏览器每次打开仪表板还会通过本地 Unix datagram socket 异步唤醒同一个 worker。启动、定时和浏览器触发串行执行,不会并发争抢 SQLite 游标。
- 各 agent adapter 读取自己的本地 JSONL、SQLite、JSON 或云端导出源，统一输出 `UsageEvent`；通用 importer 按 `event_id` 幂等，把用量写入 `request_log` 的统一智能体身份，`project` 与 `session_id` 保存在本机，不展示在请求日志 API。
- 智能体用量和代理用量统一进入 dashboard 的 `daily_usage`；订阅周期费用按“绑定订阅实例 ÷ 同一实例当前绑定的启用软件数”分摊到 `monthly_recurring_costs`。dashboard 条目仍以软件为单位：实际消费来自绑定订阅，理论消费来自软件用量；没有绑定时实际消费为 0。

## 币种与汇率(CNY / USD)

- 模型定价与 plan/智能体订阅价都可选币种,默认 CNY,可选 USD。输入的单价/月费是**原生币种**金额。
- 模型 USD 计费在写时按**写入当天的 USD→CNY 汇率**换算成 CNY 后再进 `request_log.api_cost`；订阅周期费用仍按其独立的周期锁定规则处理。
- 订阅费换算到 CNY 按**计费周期开始日(period_start,UTC)汇率**,首次确定后**永久锁定**:USD 行一旦 `fx_rate_date == date(period_start)` 即锁定,汇率只从 `fx_rates` 的该日精确行读取,永不重新拉取、不随当天汇率漂移;`immediate` 改价时金额按「新价 × 锁定汇率」重算,汇率本身不变。已冻结(finalized)周期金额永远不变,改价也不生效。
- 周期开始日无精确汇率时,按 `?date=period_start` 从 frankfurter **历史接口**拉取并落库后锁定(1999-01-04 起支持,早于此日期不发请求);拉取失败则用最近一条已存汇率作**临时值(provisional,不锁定)**,每轮物化重试;未锁定的 USD 行在周期结束后也**不会冻结**,直到锁定成功(网络恢复后同一次物化内完成锁定并冻结)。
- 汇率来源 `GET https://api.frankfurter.dev/v2/rate/USD/CNY`(带 `?date=` 支持历史日期;v2 对周末请求回显请求日期、汇率为最近交易日值)。看板启动与首次使用时按 UTC 日拉取一次并存入本机 `fx_rate` 表;当天已有则直接用;拉取失败(或仍为旧数据)则用最近一条已存汇率。请求日期早于所有已存记录(如过去月份早于首次拉取)时用**最早一条已存汇率**,避免 USD 订阅被按 1.0 低估;只有该币种对从未存储过任何记录才按 1.0(等价不换算)。
- 锁定承诺的是"不重新拉取、不随当天漂移";手工修改 `fx_rates` 行会被锁定行读到,不做防护。
- `fx_rates`、`agent_software_runtime.cursor_json` 与请求日志均**仅存本机**,同步到云时被剔除(`sync._RUNTIME_TABLES`)。订阅、软件配置和普通用户配置会随配置快照上传；上游 API Key 明文与 WebDAV 密码留在本机。
