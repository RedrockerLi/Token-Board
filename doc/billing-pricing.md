# 计费与定价

V1 的 proxy/import 用量都写同一种 `UsageEvent`。数据库按 `requested_at` 选择历史 rate 并固化 `equivalent_cost`；metered 合同同时写 `billed_usage_cost=equivalent_cost`，recurring 合同写 0，真实周期费用来自 `billing_period_charges`。兼容 API 仍展示 api/plan/agent 模板，但核心计费不按类型字符串分支。

## 定价模型

`pricing_rules` 保存 pattern 与显式 priority；每条规则有带 `valid_from/valid_until` 的 `pricing_rates` 历史。`model_pattern` 支持 `*` / `?` GLOB 通配，单位为每百万 tokens：

- `input_price`:未命中缓存的输入单价
- `output_price`:输出单价
- `cache_read_price`:缓存命中的输入单价,缺省回落 `input_price`

匹配按 `priority,id`，再在请求时间点选择有效 rate。上下移只交换 priority；改价关闭旧 rate 并新建 rate，不修改主键或历史行。

## 峰谷档位

`pricing_slots` 给每个定价挂若干档位,每档是"当日一个时段 × 倍率":

| 字段 | 含义 |
|------|------|
| `start_minute` / `end_minute` | 当日分钟,按 UTC+0 存,范围 `[0,1439]`,`end` 独占 |
| `multiplier` | 该时段倍率,默认 1.0 |

请求命中时段:取 `requested_at`(UTC)换算成当日分钟 `M = CAST(strftime('%s', requested_at) AS INTEGER) % 86400 / 60`,落在档位区间即命中,倍率乘到三档基础价上。`start > end` 表示跨午夜档(如 22:00–06:00),命中条件是 `M >= start OR M < end`。同一 pricing 有多档命中时取 `id` 最小的一档。

界面按浏览器所在时区输入和显示,存入库的仍是 UTC+0 当日分钟。换算使用浏览器当前的 `getTimezoneOffset()`:

- 本地分钟 → UTC 分钟: `local + offset`;
- UTC 分钟 → 本地分钟: `utc - offset`。

因此同一组数据库档位在不同电脑时区会显示为不同的当地时间,但代理后端始终按原 UTC 区间匹配。电脑时区变化不会改数据库值,夏令时只会改变前端看到的当地时刻。

## 写时计价固化

计费载体是 `request_log` 的**单列 `api_cost`**(0007 起 `cost` + `virtual_cost` 合并——api 账户记真实账单,plan/agent 记虚拟口径,统一落这一列)。两种写时计价路径:

- **第三方/老写入**(`cost_frozen=0`):由触发器 `tr_request_log_insert` 在插入时按当时的 `model_pricing` 单价、档位配置与汇率算好写进当行(0018 版本,USD 定价按请求当天汇率折 CNY)。
- **代理自身**(`cost_frozen=1`):C++ 在请求**入队时刻**用 `snapshot_request_cost` 快照定价写入,排队延迟不改账;`event_id` 保证本地 spool 重放幂等。

两条路径共用同一个**取价视图 `v_pricing_rate`**(0018,`model_pricing` 基本价 + 缓存价/币种 COALESCE 的唯一事实源):触发器的子查询与 C++ 的 `stmt_snapshot_price_` 都从视图取价,只有峰谷档位/汇率子查询(依赖每行时刻)留在两端。等价性由两个 ctest 门保证:`pricing_equivalence`(v17 触发器 vs v18 视图触发器逐位相等)+ `pricing_snapshot_equiv`(C++ 快照 vs v18 触发器一致)。

迁移 `0002` 删掉了 `tr_pricing_insert/update/delete` 三个改价回溯触发器,所以:

- 改价、增删档位、调整优先级、改汇率,都不再重算历史 `request_log`。
- 历史行在迁移时用"当前配置 + 各记录时刻的档位倍率"回填过一次,此后保持冻结。
- `request_log.api_cost` 是唯一可信的记账值,导出到 `dashboard.db` 时直接按它聚合。

## plan 账户与虚拟消费

`account_type = 'plan'` 的账户代表订阅套餐,调用本身不按量收费。计费规则:

- `api_cost` 就是虚拟口径——即"这笔流量不买套餐的话要花多少钱"。真实成本不计入 `api_cost` 聚合(见下)。
- 每把上游密钥独立拥有 `valid_from` 起的行政月周期。锚点日为起始日的日号，短月取月末；密钥不需要产生用量也会产生每周期一次的订阅费。
- 所有边界使用 UTC+0。删除 plan/agent 密钥或账户时按「删除默认操作」(`plan_billing_config.cancellation_mode`，默认 `immediate`)执行：**本期立即删除**(`immediate`，本期计费)把 `deleted_at` 设为删除时刻，即刻停用但本期照收月费；**到期立即删除**(`end_of_period`，本期计费、下期不计费)把 `deleted_at` 设为本期期末，订阅可继续使用至本期最后一天，到期后路由自动停止，本期照收、下期不再计费。api 账户无订阅生命周期，始终立即删除。
- 月费修改会写入价格历史。设置页可选择默认从本期或下一期生效；不同锚点日的密钥会在各自对应的周期边界切换价格。
- plan 单把上游密钥收到 HTTP 429 后冷却 5 小时，同账户其他密钥仍可回退接管(见 [proxy-internals.md](proxy-internals.md))。冷却期内代理每 1 小时探测一次上游 `GET /models`,2xx 即提前解除冷却,不必等满 5 小时;探测不写 request_log、不占并发(见 [proxy-internals.md](proxy-internals.md) 冷却探测小节)。

plan 账户的 `api_cost`(虚拟口径)的意义是衡量套餐划不划算:实际花的钱是月费,虚拟消费是省下来的按量金额。

## 看板上的消费口径

数据来源:代理导出的数据按 `request_log.api_cost`(写时固化)聚合写入 `cost_entry`。CSV 导入已弃用,
存量 CSV 数据已并入 DeepSeek 账户,`cost_entry` 不再有 `source` 列。

- **总消费(真实)**:api 账户按量费用 + plan/agent 月费。主看板为存档全量口径;代理账单页为**近 30 天滚动**口径
  (近30天 api 账户 `SUM(api_cost)` + 当前仍应收费的每把 plan 密钥的本期月费;USD 订阅费按当日汇率折 CNY)。
- **今日消费**:今日全部 `SUM(api_cost)`(api 真实账单 + plan/agent 虚拟口径,`get_stats` 限今日)。
- **理论消费**:api 按量 + plan/agent 虚拟消费,即完全不买 plan 全按 api 计费应花的金额。

`proxy_plan_summary` 表按“行政月 × 账户 × masked 密钥”保存。订阅费由生命周期和价格历史校准，
虚拟消费仍是追加式归档。`/api/summary` 据此返回 `plan_subscription_cost` 与 `plan_virtual_cost`,
前端把月费加进总消费、把虚拟消费显示为"理论消费"。日志导出 30 天后清理，因此无法回填已清理的历史虚拟消费。

## agent 账户与 Codex 用量导入

`account_type = 'agent'` 代表 Agent 订阅(目前仅 Codex),计费与 plan 一致:

- 订阅按月计费(`monthly_price` × 每账户一个"订阅"生命周期,`proxy_plan_summary` 以 `key_masked='subscription'` 存档),与 plan 的差异是**不绑定任何上游密钥**。
- agent 账户**不能**作为本地密钥的上游目标(`create_key` 拒绝),路由快照也会跳过它;其用量全部来自导入,不是代理转发。
- 用量由**仪表板服务器进程内的单一 worker** 导入:`token-dashboard` 启动时立即运行一次,之后每 30 分钟运行一次;浏览器每次打开仪表板还会通过非阻塞 POST 唤醒同一个 worker。启动、定时和浏览器触发串行执行,不会并发争抢 SQLite 游标。导入扫描 `~/.codex/sessions`(递归 `YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`,兼容 `.jsonl.gz`),把每个 `token_count` 事件的 `last_token_usage`(每轮增量)写一行 `request_log`(`account_id` 指向第一个 `agent_kind='codex'` 账户,`event_id` 幂等,重复扫描不重复计数)。用量进入消费报告与后续看板导出,口径同 plan:真实成本 = 订阅费,api_cost = 虚拟/理论消费。
- 导入游标存于本机 `account_importers.cursor_json`，不上云。

## 币种与汇率(CNY / USD)

- `model_pricing` 与 plan/agent 订阅价都可选币种,默认 CNY,可选 USD。输入的单价/月费是**原生币种**金额。
- USD 计费在写时按**请求当天的 USD→CNY 汇率**换算成 CNY 后再进 `request_log.api_cost`(代理快照与 `tr_request_log_insert` 触发器同样处理)。
- 订阅费换算到 CNY 按**计费周期开始日(period_start,UTC)汇率**,首次确定后**永久锁定**:USD 行一旦 `fx_rate_date == date(period_start)` 即锁定,汇率只从 `fx_rates` 的该日精确行读取,永不重新拉取、不随当天汇率漂移;`immediate` 改价时金额按「新价 × 锁定汇率」重算,汇率本身不变。已冻结(finalized)周期金额永远不变,改价也不生效。
- 周期开始日无精确汇率时,按 `?date=period_start` 从 frankfurter **历史接口**拉取并落库后锁定(1999-01-04 起支持,早于此日期不发请求);拉取失败则用最近一条已存汇率作**临时值(provisional,不锁定)**,每轮物化重试;未锁定的 USD 行在周期结束后也**不会冻结**,直到锁定成功(网络恢复后同一次物化内完成锁定并冻结)。
- 汇率来源 `GET https://api.frankfurter.dev/v2/rate/USD/CNY`(带 `?date=` 支持历史日期;v2 对周末请求回显请求日期、汇率为最近交易日值)。看板启动与首次使用时按 UTC 日拉取一次并存入本机 `fx_rate` 表;当天已有则直接用;拉取失败(或仍为旧数据)则用最近一条已存汇率。请求日期早于所有已存记录(如过去月份早于首次拉取)时用**最早一条已存汇率**,避免 USD 订阅被按 1.0 低估;只有该币种对从未存储过任何记录才按 1.0(等价不换算)。
- 锁定承诺的是"不重新拉取、不随当天漂移";手工修改 `fx_rates` 行会被锁定行读到,不做防护。
- `fx_rates` 与 `account_importers.cursor_json` 均**仅存本机**,同步到云时被剔除(`sync._RUNTIME_TABLES`)。
