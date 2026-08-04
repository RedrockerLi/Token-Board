# 计费与定价

计费链路从代理写 `request_log` 开始,成本在写入那一刻按当时的定价固化下来,之后改价不回溯。这是迁移 `0002`(峰谷定价 + 写时计价固化)定下的规则。本文讲定价如何配置、时段档位怎么命中、plan 账户如何处理,以及看板上的三种消费口径。

## 定价模型

`model_pricing` 一行定义一个模型的单价,`model_pattern` 支持 `*` / `?` GLOB 通配,单位元/百万 tokens:

- `input_price`:未命中缓存的输入单价
- `output_price`:输出单价
- `cache_read_price`:缓存命中的输入单价,缺省回落 `input_price`

模型匹配取 `LOWER(model) GLOB LOWER(model_pattern) ORDER BY mp.id LIMIT 1`,第一条匹配生效。同一条请求在多个 pattern 都命中时,`id` 小的优先;看板定价页的上下移按钮交换 `id`,从而调整优先级。换序只影响新请求,已固化的历史成本不受影响。

## 峰谷档位

`pricing_slots` 给每个定价挂若干档位,每档是"当日一个时段 × 倍率":

| 字段 | 含义 |
|------|------|
| `start_minute` / `end_minute` | 当日分钟,按 UTC+0 存,范围 `[0,1439]`,`end` 独占 |
| `multiplier` | 该时段倍率,默认 1.0 |

请求命中时段:取 `requested_at`(UTC)换算成当日分钟 `M = CAST(strftime('%s', requested_at) AS INTEGER) % 86400 / 60`,落在档位区间即命中,倍率乘到三档基础价上。`start > end` 表示跨午夜档(如 22:00–06:00),命中条件是 `M >= start OR M < end`。同一 pricing 有多档命中时取 `id` 最小的一档。

界面按 UTC+8 输入:`proxy_manager.js` 里 `minutes8to0` / `minutes0to8` 做 ±480 分钟换算,存入库的是 UTC+0 分钟。换句话说,用户在界面上看到的 08:00 对应 UTC+0 的 00:00。

## 写时计价固化

计费触发器 `tr_request_log_insert` 只在新请求插入 `request_log` 时执行一次:按当时的 `model_pricing` 单价、当时的档位配置,算出 `cost`(api 账户)或 `virtual_cost`(plan 账户)写进当行。迁移 `0002` 同时删掉了 `tr_pricing_insert/update/delete` 三个改价回溯触发器,所以:

- 改价、增删档位、调整优先级,都不再重算历史 `request_log`。
- 历史行在迁移时用"当前配置 + 各记录时刻的档位倍率"回填过一次,此后保持冻结。
- `request_log.cost` 是唯一可信的记账值,导出到 `dashboard.db` 时直接按它聚合。

## plan 账户与虚拟消费

`account_type = 'plan'` 的账户代表订阅套餐,调用本身不按量收费。计费规则:

- `cost = 0`(真实成本),`virtual_cost` 按 api 口径公式算出——即"这笔流量不买套餐的话要花多少钱"。
- 每把上游密钥独立拥有 `valid_from` 起的行政月周期。锚点日为起始日的日号，短月取月末；密钥不需要产生用量也会产生每周期一次的订阅费。
- 所有边界使用 UTC+0。删除密钥或账户时，本期通常仍收费；若删除时间早于本期起点加“取消宽限小时数”，则该期不收费。宽限在删除时快照，之后修改设置不追溯历史。
- 月费修改会写入价格历史。设置页可选择默认从本期或下一期生效；不同锚点日的密钥会在各自对应的周期边界切换价格。
- plan 单把上游密钥收到 HTTP 429 后冷却 5 小时，同账户其他密钥仍可回退接管(见 [proxy-internals.md](proxy-internals.md))。

`virtual_cost` 的意义是衡量套餐划不划算:实际花的钱是月费,虚拟消费是省下来的按量金额。

## 看板上的消费口径

数据来源:代理导出的数据按 `request_log.cost`(写时固化)聚合写入 `cost_entry`。CSV 导入已弃用,
存量 CSV 数据已并入 DeepSeek 账户,`cost_entry` 不再有 `source` 列。

- **总消费(真实)**:api 按量费用 + plan 月费。主看板为存档全量口径;代理账单页为**近 30 天滚动**口径
  (近30天 `SUM(cost)` + 当前仍应收费的每把 plan 密钥的本期月费)。
- **今日消费**:今日 api 按量 + 今日 plan 虚拟消费(`get_stats` 中 `SUM(cost + virtual_cost)` 限今日)。
- **理论消费**:api 按量 + plan 虚拟消费,即完全不买 plan 全按 api 计费应花的金额。

`proxy_plan_summary` 表按“行政月 × 账户 × masked 密钥”保存。订阅费由生命周期和价格历史校准，
虚拟消费仍是追加式归档。`/api/summary` 据此返回 `plan_subscription_cost` 与 `plan_virtual_cost`,
前端把月费加进总消费、把虚拟消费显示为"理论消费"。日志导出 30 天后清理，因此无法回填已清理的历史虚拟消费。
