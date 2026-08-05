# 代理内部机制

代理是 C++17 单进程,入口 `proxy/src/main.cpp`,基于 httplib 提供 HTTP 服务。源码按职责分目录:`core/`(路由、网关、会话亲和、服务端)、`net/`(上游转发、线程池)、`format/`(IR 与三格式 codec、think 过滤)、`store/`(SQLite 访问、usage 解析)。本文按一条请求的旅程讲内部组件:生命周期、线程池、路由与认证、候选选择与回退、会话亲和、并发闸门、超时与断连、记账与性能事件。

## 生命周期

启动流程:解析 CLI 参数(`core/config.cpp`)→ 打开 SQLite 并跑迁移(`store/db.cpp::open`,schema 目录未指定时由 `--db` 推导为 `<db目录>/../schema/proxy`)→ 组装 Router / UpstreamClient / UsageTracker / CodecRegistry → 建 httplib server 与信号量线程池 → 监听。收到 SIGINT/SIGTERM 后优雅停机退出。

主循环里每 5 分钟做两件事:

- 线程池扩容判断:当 **`pool->queued() > 0` 且 `pool->active() >= 当前线程数`** 且未到上限时,线程数翻倍。只看 `in_flight` 会把长连接误判成积压导致无谓扩容,所以要求确有请求在队列里等线程。
- 周期清理:`cleanup_stale_in_flight(10)` 清掉 `in_flight_requests` 遗留表里超过 10 分钟的僵死记录(旧版本代理的观测表;新版本已不写)。

## 线程池:SemaphorePool

`semaphore_pool.h`(net/) 用 POSIX 计数信号量实现 httplib 的 `TaskQueue`,替代 httplib 自带线程池。初始 8 线程,`main` 里上限 **256**;`resize()` 只增不减。信号量的好处是 `sem_post` 每次恰好唤醒一个等待线程,没有惊群。请求进队时在锁外 post,唤醒的线程可立即进入临界区。

## 路由与认证

`Router::route`(core/router.cpp)把本地密钥映射到上游账户:查 `local_keys` 关联的 `upstream_accounts`,结果缓存在内存。成功结果只缓存 **2 秒**(配置/密钥改动几乎立即生效),失败(无效密钥)一律不缓存。聚合账户只做一级路由,真正的账户由请求里的模型名在 `resolve_candidates` 里二次解析。

认证同时接受两种头:`Authorization: Bearer <密钥>` 与 `x-api-key: <密钥>`,与客户端用什么格式无关,密钥只用来选账户。

## 候选选择与回退

`resolve_candidates` 先给模型名剥掉 Claude Code 的 `[1m]` / `[1M]` 后缀,再按路由结果取候选:

- 普通账户:一个或多个候选(账户的每把上游密钥一个槽位)。
- 聚合账户:按精确模型名匹配 `aggregate_entries`,同一模型可能命中多条,按 `sort_order, id` 排序,构成候选链。

会话带 `session_id` 时,起点由 `SessionAffinity::preferred_index` 决定(见下),否则从头开始。随后 `pick_candidate` 沿顺序挑:跳过处于冷却期的槽位、占用并发槽失败(达到 `max_concurrency`)的也跳过,选中即返回。

请求分两种走法:

- **流式**:chunked 响应头**故意延迟到首个数据块写出前才提交**,所以上游在首字节前返回 429/5xx 时可以在当前请求内换下一候选重试;一旦 `committed`(任何字节已发给客户端)则不再切换,剩余部分按上游流原样转发。首个错误帧仍可触发回退。
- **非流式**:循环候选。上游返回 429 或 5xx、且还没向客户端发任何数据时,释放当前密钥槽的并发槽,试下一个。plan 密钥收到 429 会触发该密钥 5 小时冷却(只冷却这一把)。全部候选失败返回 429。

超时(`is_timeout`,504)与 429/5xx 一样参与回退——上游卡死不吐数据视为 provider 故障,换下一个账户(对齐 cc-switch 把超时归为 Retryable);全部候选失败时,若最后失败是超时则返回 504,否则 429。每次尝试都会写一行 `request_attempts`,失败的上游不会从监控里消失。嵌入端点 `/v1/embeddings` 也是非流式,走同样的候选循环,但没有格式转换。

## 会话亲和与成本平衡

`SessionAffinity`(core/proxy_server.h)是进程内的"会话 → 上游密钥"亲和:同一 `session_id` 的请求倾向复用上次成功服务的密钥槽(成功才绑定、回退会重绑),避免一次会话在多个套餐密钥之间跳来跳去。亲和映射是内存 LRU(上限 10 万条、24 小时过期),不写库——不在请求热路径上加任何同步 SQLite 写。

**新会话**(冷启动)按累计成本选密钥:`KeyCostLedger`(core/key_cost_ledger.h)由记账线程异步喂"每个密钥槽累计花费",新会话选择累计花费**最小**的密钥槽,让各密钥按花费大致均匀磨损;台账不落盘、重启清零,新密钥读作 0.0 会被优先选中。无台账(单元测试路径)退化为 rendezvous hashing。空 `session_id` 或单候选时走普通的顺序填充。

## 并发闸门:AccountGate

`core/account_gate.h` 是纯内存的**按密钥槽**并发闸门。并发与冷却都按"一把上游密钥一个槽位"计(`upstream_keys` 一行一个槽;无密钥的 legacy 账户退回账户级):

- `acquire(key_slot_id, max_concurrency)`:`max_concurrency <= 0` 视为不限,直接放行。一把密钥打满并发,同账户的下一把密钥可立即接管。
- 槽位回收:正常每个 `acquire` 配一个 `release`;客户端断连处理与响应结束都会释放。没有按年龄强回收的 `SLOT_TTL`——健康的长时间流必须保住自己的槽位。
- `mark_cooldown`:plan 密钥槽收到 429 后冷却 `PLAN_COOLDOWN`(5 小时),期间 `in_cooldown` 返回 true,路由跳过该槽位而非整账户——同账户其他密钥不受影响,可继续接管。冷却态不落库,重启即清。
- `mark_failure` 短熔断:401/403/其他 429/5xx/网络失败按失败次数指数退避 **5s → 30s → 2min**,成功清除。plan 429 走上面的 5h 冷却。

实时并发不写库:请求热路径只更新进程内计数器,`GET /health` 的 `concurrency` 字段暴露它;`in_flight_requests` 表是旧版本的遗留,当前代理不再写入(启动与周期清理仍会清它的残留)。

## 超时与客户端断连

上游超时按客户端线格式配置,存在 `proxy_timeout_config` 表(每行一组,默认值对齐 cc-switch:anthropic 90/180/600、openai_responses 与 openai 60/120/600),由仪表板「设置」页编辑、代理每次转发时按 harness 线格式读取,改动即时生效、无需重启:

- `streaming_first_byte_timeout` — 流式等首个数据块的最大时间。
- `streaming_idle_timeout` — 流式两个数据块之间的最大间隔,0=禁用。
- `non_streaming_timeout` — 非流式 body 读取超时,作为整个 Get/Post 的读超时(每读无数据 N 秒即超时,等价于 cc-switch 的整包总超时;连接 10s、写 30s 保持固定)。

流式的首字节/静默区分用 `net/upstream_client.cpp` 里的**超时看门狗线程**:httplib 客户端的读超时是 select-based、连接期固定,无法区分"等首块"和"块间间隔",所以看门狗在首字节截止点(请求发出后 `streaming_first_byte_timeout`)和每个块到达后重置的静默截止点(`now + streaming_idle_timeout`)检测超时,到点就 `shutdown()` 上游 socket 打断阻塞读(与客户端断连监视 `spawn_client_monitor` 同机制),阻塞读因此以读错误返回、记 `is_timeout`。httplib 自身读超时设成大的 backstop,只兜底。两个超时都配 0 时不开看门狗。回退链上多次尝试共享一个首字节预算(`clamp_to_remaining_budget`),避免 N 个坏密钥把一次 60s 请求拖成 N×60s。

超时(`is_timeout=true`)对客户端统一给 `{"error":{"type":"timeout_error","code":504}}`,错误消息带实际触发的秒数。流式路径按客户端格式发终止错误帧:Anthropic `event: error`、Responses `response.failed`、OpenAI `data: {error}` + `[DONE]`,让客户端知道上游没回,而不是静默断连。

客户端断连的检测用 `spawn_client_monitor`:一个线程每 250ms poll 客户端 socket,发现 `POLLHUP / POLLERR / POLLRDHUP` 就 `shutdown()` 上游 socket,把阻塞中的上游读立刻打断,释放线程池线程,而不是干等满超时。注意 TCP 对端 FIN 只表现为 `POLLIN + POLLRDHUP`,所以 poll 必须显式请求 `POLLRDHUP`。非流式路径也有等价的 `client_disconnected` 检查,确认客户端已走就把这次请求记为 499(零 token)结束。

## 请求记账与性能事件

每次请求的结局都写进 `request_log`:`UsageTracker`(store/)解析上游返回的 usage(三种格式各有非流式与 SSE 解析器,见 [format-conversion.md](format-conversion.md)),连同状态码、耗时、TTFT/生成耗时/输出速度一起记入;解析不出 usage 时也记一行零 token,保证日志完整真实。`api_cost` 由代理入队快照(`cost_frozen=1`)或 `tr_request_log_insert` 触发器(`cost_frozen=0`)写时固化,见 [billing-pricing.md](billing-pricing.md)。每次上游尝试再写一行 `request_attempts`(状态码、耗时、TTFT、是否超时、错误),回退诊断与"各上游成功率"据此统计。

**性能数据源是 `request_log` 的列 + `request_attempts`,不再写 `perf_events`**(旧表仅作兼容):`ttft_ms` / `generation_ms` / `output_tps`(输出速度)、`upstream_ttft_ms` / `upstream_duration_ms`(上游侧耗时)、`attempt_count` / `fallback_count` 支撑延迟 P50/P95/P99、输出速度分布(P50/P95/P99)与各模型 TTFT/速度采样;实时并发来自进程内计数器(`/health` 的 `concurrency`)。request_log 的清理按高水位检查点 + 30 天:`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行,见 [sync.md](sync.md)。

## 端点与健康检查

`setup_routes` 注册的端点见 [api-reference.md](api-reference.md)。除转发端点外还有 `GET /health` 返回 `{"status":"ok","service":"token-board-proxy","concurrency":N}`(concurrency 是进程内实时并发数),供 `scripts/status.sh` 探活与看板实时并发展示。所有 `/v1/*` 端点都带 CORS 头,并处理 `OPTIONS` 预检(204)。
