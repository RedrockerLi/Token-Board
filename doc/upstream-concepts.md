# 上游账户模型：类型(api/plan)、单上游多密钥与聚合上游

本文把「上游」拆成三层讲清楚：**账户类型**、**单账户的多把密钥**、**聚合账户**，然后沿「代理转发 → 保存入库 → 数据导出」这条全链路，说明每种形态在每个环节的行为。配合阅读 [architecture.md](architecture.md)、[proxy-internals.md](proxy-internals.md)、[billing-pricing.md](billing-pricing.md)、[sync.md](sync.md)。

## 一、一张图看懂

```
 本地密钥 tb-xxxx                代理转发                       真实上游
 local_keys                    (C++ proxy)                 upstream_accounts
 ──────────                    ──────────                 ──────────────────
 客户端拿到的                    认证 → 一级路由 →             account_type:
 一把钥匙,绑定一个               (聚合? 按模型二次解析)          api / plan
 上游账户(或聚合)                → 候选链(账户×密钥)            is_aggregate=0|1
                                并发闸门 → 转发 → 记账          upstream_keys(多把密钥)
```

- **账户(account)** 是唯一身份，`id` 永不复用，删除是软删(`deleted_at`)。
- **账户类型 `account_type`**：`api`(按量)或 `plan`(订阅套餐)。它决定**怎么计费**和**能不能路由**；Codex 等智能体不再是上游账户。
- **单上游多密钥(`upstream_keys`)**：一个真实上游账户名下可以挂多把上游 Key，每把 Key 是独立的并发槽位、独立的冷却单元、独立的订阅周期。
- **聚合账户(`is_aggregate=1`)**：不是真实上游，只是**按模型名的路由分组**。它把多个真实账户合并成一个模型目录暴露给客户端，同一个模型可以配多个账户自动回退。

## 二、账户类型：api / plan

两种类型在 `upstream_accounts.account_type` 上区分，默认为 `api`。**类型的行为语义（能不能路由、怎么计费、429 冷却、删除语义等）统一收敛在规格表 `app/domain/account_types.py` 的 `ACCOUNT_TYPES`**（[app/domain/account_types.py:47](app/domain/account_types.py#L47)），创建/修改账户的类型校验也以该表为准。智能体订阅与软件来源由“智能体管理”单独维护。

| 维度 | api(按量) | plan(订阅套餐) |
|------|-----------|----------------|
| 计费方式 | 按调用量×单价(`model_pricing`) | 订阅费：月费 × 每把密钥的订阅周期，与调用量无关 | 同 plan：月费 × 每账户一个「订阅」 |
| 是否持有上游密钥 | 是 | 是（**每把密钥 = 一个订阅**） |
| 是否可被本地密钥路由 | 是 | 是 |
| 调用量来源 | 代理转发自动记账 | 代理转发自动记账 |
| `request_log.api_cost` 含义 | 真实账单 | 虚拟/理论消费(套餐省下的钱) |
| 真实成本存档 | 进 `cost_entry` | 进 `proxy_plan_summary.subscription_cost` |
| 429 处理 | 瞬时退避(5s/30s/2min) | 每把密钥**冷却 5 小时** |

### api 账户（按量计费）

- 常规形态：`base_url` + `api_format` + 一把或多把上游密钥，`max_concurrency` 控制并发。
- 每一笔请求按写时固化的单价算出 `api_cost`，这是**真实账单**。
- 消费报告与导出按**按量计费类型**（`account_type` 属于 `usage_billed_types()`，目前仅 `api`）的 `api_cost` 统计真实消费（[app/db/proxy_db.py:183](app/db/proxy_db.py#L183)）。

### plan 账户（订阅套餐）

- 计费与调用量解耦：**每把上游密钥**按 `valid_from`(订阅起始日) 锚定的**行政月周期**收一次月费；同一账户多把密钥 = 多个独立订阅。
- 调用照常被代理转发、照常记 `request_log`，但 `api_cost` 记的是**虚拟消费**（不买套餐按量算要花多少钱），用于衡量套餐划不划算。
- 429 冷却也是**按密钥**：一把 plan 密钥被限流只冷却它自己，同账户其他密钥立即接管（[proxy/src/core/account_gate.h:18-21](proxy/src/core/account_gate.h#L18-L21)）。冷却期内代理每 1 小时向 `{base_url}/models` 探测一次（auth scheme 按 `api_format` 推导：anthropic 用 x-api-key、其余 Bearer），2xx 提前解除冷却，见 [proxy-internals.md](proxy-internals.md)「冷却探测」。
- 订阅周期、价格历史、删除默认操作见 [billing-pricing.md](billing-pricing.md)「plan 账户与虚拟消费」。

### 智能体管理（统一 IR + 可扩展 adapter registry）

- 智能体不属于上游账户，也不参与本地密钥路由。软件身份使用与上游统一的整数 ID；订阅保存在 `agent_subscriptions`，可有多个 `agent_subscription_instances`，软件与订阅通过 `agent_subscription_bindings` 多对多绑定。
- 用量来自**token-maintenance 导入服务**：[maintenance.py](../maintenance.py) 管理唯一 worker，在维护服务启动时立即运行、每 30 分钟运行，浏览器打开看板时通过 Unix datagram socket 异步唤醒；[agent_usage](../app/services/agent_usage/) 按 `agent_kind` 选择独立 adapter，把各来源归一为 Python Usage IR，再写成 `request_log` 行。`event_id` 幂等，`project` 与 `session_id` 只保存在本机。
- dashboard 的条目以软件/智能体为单位：理论消费来自导入用量，实际消费来自绑定订阅；未绑定时实际消费为 0，一个订阅绑定多个启用软件时按绑定软件数平分。没有绑定关系不会自动推断。

## 三、单上游多密钥

一个真实上游账户（api 或 plan）可以在 `upstream_keys` 表挂多把上游 Key（迁移 `0-7`，[schema/token-board/v0/0-7_multi_key.sql](../schema/token-board/v0/0-7_multi_key.sql)）。按当前同步规则，Key 明文只保存在本机；云端只保存掩码元数据用于跨机账单关联，请求日志等本机生成的数据也不上云。

### 多密钥带来的行为

- **每把 Key 一个并发槽位**：`max_concurrency` 按 Key 计数，一把 Key 打满后请求溢流到下一把（[proxy/src/core/account_gate.h:13-16](proxy/src/core/account_gate.h#L13-L16)）。
- **每把 Key 一个冷却单元**：plan 的 429 冷却、瞬时失败退避都按 `key_slot_id` 隔离，兄弟 Key 不受连坐。
- **固定填充顺序**：`upstream_keys.position` 决定优先顺序，会话亲和与溢流都按 `(position, id)` 走（[proxy/src/core/proxy_server.cpp:462-496](proxy/src/core/proxy_server.cpp#L462-L496)）。
- **会话亲和(session affinity)**：同一会话（`x-session-id` / OpenAI `user` / Anthropic `metadata.user_id` / Responses `previous_response_id`）在成功一次后**绑定到实际服务的 Key**，换取上游 prompt 缓存命中；绑定只在成功后发生，失败的 Key 永远不会成为会话的下一次首选（[proxy/src/core/proxy_server.h:42-138](proxy/src/core/proxy_server.h#L42-L138)）。
- **冷启动成本平衡**：新会话没有绑定记录时，优先选**累计消费最少**的 Key（进程内 `KeyCostLedger`，由记账线程异步喂数据），让多把 Key 按花费磨损得更均匀（[proxy/src/core/key_cost_ledger.h](proxy/src/core/key_cost_ledger.h)）。
- **每把 Key 一个订阅生命周期**：plan 账户的每把 Key 独立拥有 `valid_from` / `deleted_at`，月费按「锚点日」各自切周期。删除密钥/账户按设置页「删除默认操作」执行：`immediate`（本期立即删除，本期计费）或 `end_of_period`（到期立即删除，本期计费、下期不计费——每把 Key 的 `deleted_at` 设为自己的本期期末，到期前仍保持可路由；账户的 `deleted_at` 取所有仍有效 Key 到期时间的最大值）。api 始终立即删除。
- **观测**：会话→密钥分配记在本地 `session_key_log`（7 天滚动，不上云）。

### 兼容旧形态

没有 `upstream_keys` 行的账户回退到旧单列 `account.upstream_key`，作为一个候选，并发槽位为负的账户 id（`-account_id`），互不冲突（[proxy/src/core/proxy_server.cpp:466-481](proxy/src/core/proxy_server.cpp#L466-L481)）。

## 四、聚合上游

聚合账户（`is_aggregate=1`）在「上游账户聚合」页维护，把多个真实账户合成一个**模型目录**暴露给客户端。它没有自己的 Base URL / Key / 计费。

### 模型映射

`aggregate_entries` 一行一条映射：`pattern`(GLOB 模型名) → `upstream_account_id`(真实账户) → `upstream_model`(转发时改写成的上游模型名)。**同一个 pattern 可以配多个账户**，按 `(sort_order, id)` 排序构成回退链。

### 请求如何走

```
客户端:  model=claude-opus-4   →  本地密钥(绑聚合)  →  聚合账户
                                                       │
                         按 model 在 aggregate_entries 里匹配(可多条)
                                                       ▼
                    候选链 = (匹配的每条 entry × 该账户的每把密钥)
                                                       ▼
        按优先级组逐个尝试: 冷却跳过 → 并发满跳过 → 429/5xx 回退
```

- **候选展开**：一条 entry 指向一个真实账户，该账户名下每把 Key 又是一个候选；一个模型配了 2 个账户、各 2 把 Key，就是 4 个候选。
- **优先级组**：`priority_group` 来自 `aggregate_entries.id`，组间**严格按 sort_order 顺序**尝试（更便宜/更靠前的先用），组内多把 Key 轮转磨损（[proxy/src/core/proxy_server.h:157-183](proxy/src/core/proxy_server.h#L157-L183)）。
- **模型改写**：转发前把请求体里的模型名替换成 `upstream_model`（`apply_body_model` / 转换路径 `cReq.model`）。
- **回退时机**：候选在**尚未向客户端发出任何数据**时失败（429/5xx/超时/401/403）才回退；流式请求的响应头延迟到首次写出才提交，因此流式也能在首字节前换下一个候选。
- **全部不可用**：返回 429（忙/冷却/全失败）；若最后是超时则 504（[proxy/src/core/proxy_server.cpp:1162-1211](proxy/src/core/proxy_server.cpp#L1162-L1211)）。
- **模型目录**：`GET /v1/models` 对聚合账户返回所有 `pattern` 作为模型列表（[proxy/src/core/proxy_server.cpp:1872-1886](proxy/src/core/proxy_server.cpp#L1872-L1886)）。

### 关键区别：聚合不是「账户」

- 聚合账户**不能有上游密钥**、**不产生真实用量**、**不参与计费**。
- 它的 `max_concurrency`、冷却等都落在**真实成员账户的密钥**上，聚合自身只是路由表。
- 若请求连一个候选都够不着（全忙/全冷却），`request_log` 会记一行挂在**聚合账户** id 下的零 token 失败行——导出时必须排除（见第六节）。

## 五、代理转发链路行为

一条请求在 C++ 代理里的完整路径（[proxy/src/core/proxy_server.cpp:934-1351](proxy/src/core/proxy_server.cpp#L934-L1351)）：

1. **认证 + 一级路由**：`Authorization: Bearer` 或 `x-api-key` 取本地密钥 → `Router::route` 查出绑定账户（2s 缓存）。聚合账户只到这一步。
2. **模型解析**：从请求体取模型名，剥掉 Claude Code 的 `[1m]`/`[1M]` 后缀 → `resolve_candidates_cached` 按账户形态展开候选链（普通账户=每把 Key 一个候选；聚合=匹配 entry × 每把 Key）。无候选返回 400「模型不可用」。
3. **候选循环**：每个候选先过 `try_acquire_eligible`（冷却检查 + 并发槽），成功才占用槽位转发。
4. **转发**：客户端线格式与账户 `api_format` 相同走直通(passthrough)，不同走 IR 转换；超时按客户端线格式分三档。
5. **失败处理**：429/5xx/401/403 且未向客户端发数据 → 记一次 attempt，标记失败（plan+429 → 5h 冷却；其余瞬时退避）→ 试下一个候选。
6. **成功绑定**：成功后会话亲和绑定到该 Key；记账线程把结果入队写 `request_log`。

## 六、保存入库链路行为

### request_log（每请求一行）

| 字段 | 说明 |
|------|------|
| `account_id` | **实际服务的真实账户** id；聚合全失败时是聚合账户 id（零 token 行） |
| `local_key_id` | 发起请求的本地密钥 |
| `upstream_key_id` | **实际服务的上游 Key** 槽位 id（多密钥归属，无 FK：Key 可删） |
| `model` | 转发后的上游模型名 |
| `prompt/completion/cache_read/total_tokens` | 从上游 usage 解析 |
| `api_cost` | 写时固化的 api 等价价（api=真实账单，plan=虚拟消费），USD 定价按当日汇率换算为 CNY；智能体导入另有独立订阅成本 |
| `status_code` / `duration_ms` | 结局 |
| `ttft_ms` / `generation_ms` / `output_tps` | 性能观测 |
| `event_id` / `cost_frozen` | 智能体导入幂等标识 / 冻结计价标记 |

写时计价的固化：代理自身走 C++ `snapshot_request_cost` 快照（`cost_frozen=1`），智能体导入与兜底走 `tr_request_log_insert` 触发器（`cost_frozen=0`）。改价不回溯。

### request_attempts（每候选一次尝试一行）

候选循环里**每一次**对真实账户的尝试都记一行（账户、Key 槽、状态码、耗时、TTFT、是否超时、错误信息），聚合回退的「失败了几次、换了几家」在这里可见（[schema/token-board/v0/0-10_request_attempts.sql](../schema/token-board/v0/0-10_request_attempts.sql)）。

### 哪些不上云

`request_log`、`request_attempts`、`session_key_log`、`fx_rates`、`agent_software_runtime.cursor_json` 都在同步时的运行时表清理中被剥离。`client_keys` 会随普通配置上传，但 `upstream_secrets`（真实 API Key）和 WebDAV 密码会从上传副本中剥离，始终只保存在本机。

## 七、数据导出链路行为

「导出数据」（或 WebDAV 同步里的导出步骤）把 `request_log` 中 `(高水位 mark, max_id]` 的增量聚合成 `dashboard.db` 存档（[app/db/proxy_db.py:1551-1662](app/db/proxy_db.py#L1551-L1662)）。

### 用量：日×账户×模型 → 存档

- 按 `(date, account_id, model)` 聚合：token 分三种类型进 `token_usage`（output / input_cache_hit / input_cache_miss），请求数进 `request_usage`，`SUM(api_cost)` 进 `cost_entry`。
- **过滤条件**：
  - 排除 `model='unknown'` / 空模型；
  - **排除聚合账户**——聚合产生的零 token 失败行绝不允许污染用量存档（[app/db/proxy_db.py:1590-1596](app/db/proxy_db.py#L1590-L1596)）。
- `api_cost` 是**写时固化**的值，导出直接加总，不再重算——改价不影响已导出金额。
- 存档按 `account_id` 分桶，显示名来自 `accounts` 镜像（账户改名回放见 [sync.md](sync.md)）。

### plan / 智能体经济账

- **订阅费**是派生状态：plan 按每把 Key 的生命周期进入 `proxy_plan_summary`；智能体订阅实例按自身生命周期并依据绑定关系分摊进入统一的 `monthly_recurring_costs`。金额按**计费周期开始日的汇率**换算并锁定，过去周期冻结。
- **虚拟消费**是追加式：订阅类型的 `request_log` 行按 `(行政月, 账户, masked Key)` 累加进 `virtual_cost`，30 天后日志清理无法回填（[app/db/proxy_db.py:1635-1652](app/db/proxy_db.py#L1635-L1652)）。
- USD 订阅按**计费周期开始日**的 USD→CNY 汇率换算并锁定；缺失时通过 frankfurter 历史接口（`?date=period_start`）补拉，失败则用最近已存汇率作临时值持续重试（[app/db/proxy/billing.py](app/db/proxy/billing.py) `_normalized_charge`）。

### 高水位与云同步

- 导出只取 `id > mark` 的行；`mark`（`last_exported_log_id`）在**整个 pull-export-upload 事务成功后才推进**，失败则丢弃 shadow、分毫不动（[app/services/sync.py:736-815](app/services/sync.py#L736-L815)）。
- 30 天后清理已归档(`id <= mark`)的 `request_log` 行；未导出的行永不清理。

## 八、常见误区速查

| 疑问 | 答案 |
|------|------|
| 聚合账户和 plan/api 是一类吗？ | 不是。聚合是路由分组（`is_aggregate=1`），plan/api 是上游计费类型。智能体由独立的订阅/软件表管理。聚合永远不入账。 |
| 智能体为什么不能发代理请求？ | 智能体软件不是上游账户，不参与本地密钥路由；它只读取本机软件日志并导入用量。 |
| 一把 plan 密钥 429 了，整个 plan 账户停摆？ | 不会。冷却按密钥，同账户其他密钥立即接管。 |
| 一个聚合模型配了 3 个账户，请求先试哪个？ | 先试 `sort_order` 最小的 entry（及其账户的多把 Key，按 position），该 entry 全不可用才轮到下一个。 |
| 请求日志里 account_id 是谁？ | 实际服务的**真实账户**；只有「全候选都忙/冷却」的零 token 失败行才挂在聚合账户下，导出时会排除。 |
| 真实上游 Key 会上云吗？ | 不会。`upstream_secrets` 和 WebDAV 密码只保存在本机；普通配置、本地代理密钥和请求聚合存档按各自同步规则上传。 |

## 九、添加一种新的上游类型

新版约定：**给上游加一个「类型」不是到处加 if/else，而是加一行规格**。`account_type` 只是身份字符串，它的行为由三处声明式规格共同描述——Python 为主、C++ 镜像、前端经 API 读取。智能体类型不再加入这套上游规格，而是在智能体管理的 `agent_kind` 中扩展：

| 声明位置 | 内容 | 加类型时 |
|---------|------|---------|
| `app/domain/account_types.py` 的 `ACCOUNT_TYPES`（[app/domain/account_types.py:47](app/domain/account_types.py#L47)） | **唯一权威**：`billing` / `routable` / `holds_keys` / `deletion` / `cooldown` / `subscription_unit` / `label` | 加一行 |
| `proxy/src/core/account_types.h`（[proxy/src/core/account_types.h:22-50](proxy/src/core/account_types.h#L22-L50)） | C++ 请求时行为镜像：`non_routable_types()`（路由排除）+ `cooldown_class()`（429 冷却类别） | 同步两个列表 |
| `GET /api/proxy/account-types` | 前端拉取整个规格表；类型下拉、徽章、价格字段显隐、删除流全部由它驱动，**前端不再硬编码类型** | 自动，无需改前端 |

### 一个上游类型的 7 个属性

定义一种新类型，就是回答这 7 个问题：

| 属性 | 取值 | 它决定什么 | 现在谁在消费 |
|------|------|-----------|-------------|
| `billing` | `usage` / `subscription` | `api_cost` 是真实账单还是虚拟消费；真实成本统计是否包含它 | proxy_db 的计费/导出查询、`_plan_key_billing_meta` |
| `routable` | true / false | 本地密钥能否绑它、代理能否转发给它 | `_assert_routable_account`、C++ 路由快照 SQL、前端 `routableAccounts`/并发测试 |
| `holds_keys` | true / false | 建不建上游密钥；编辑弹窗显不显示密钥区 | `create_account`/`update_account` 的 key 分支、前端字段隐藏 |
| `deletion` | `immediate` / `configurable` | 删除是永远立即（api）还是跟随设置页「删除默认操作」 | `_cancellation_end`、`delete_account`、前端删除流 |
| `cooldown` | `transient` / `subscription_5h` | 429 走 5s/30s/2min 瞬时退避还是该密钥冷却 5 小时 | C++ `account_gate::mark_failure` 的冷却类别 |
| `subscription_unit` | `per_key` / `per_account` | 订阅按每把密钥算一个生命周期（plan），还是每账户一条 | `_plan_key_billing_meta` 合成生命周期 |

### 添加步骤（三步 + 两个按需）

1. **`ACCOUNT_TYPES` 加一行**：把上面 7 个属性取值填好；`label` 是设置页类型下拉文案、`short_label` 是账户列表徽章。
2. **`account_types.h` 同步两个列表**：若新类型不可路由，加进 `non_routable_types()`；若 429 应冷却 5 小时，在 `cooldown_class()` 加分支。
3. **智能体扩展另走智能体管理**：在 `agent_software` 增加 `agent_kind`，为该类型注册一个返回统一 UsageEvent 的解析器；订阅继续保存在独立的 `agent_subscriptions`，通过绑定表连接到软件，不把订阅误当成代理上游。

两个按需的 if：
- **schema**：`account_type` 列无 CHECK 约束，新取值天然兼容，**一般不需要迁移**；仅当上游需要新列时才添加迁移。智能体新增字段应在智能体管理迁移中维护。
- **前端徽章配色**：`badge--type-<type>` 的 CSS 类缺省落到默认样式，想要专属配色就在 `static/css/dashboard.css` 加一个类。

前端类型下拉、价格字段显隐、删除提示、并发测试守卫都经 `/api/proxy/account-types` 读规格自动适配。校验也以规格表为准：`create_account` / `update_account` 拒绝未知类型；智能体软件类型则经 `/api/proxy/agent-types` 校验。
