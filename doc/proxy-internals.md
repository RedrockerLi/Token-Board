# 代理内部机制

代理是 C++17 单进程,入口 `proxy/src/main.cpp`,基于 httplib 提供 HTTP 服务。源码按职责分目录:`core/`(路由、网关、会话亲和、服务端)、`net/`(上游转发、线程池)、`format/`(IR 与三格式 codec、think 过滤)、`store/`(SQLite 访问、usage 解析)。本文按一条请求的旅程讲内部组件:生命周期、线程池、路由与认证、候选选择与回退、会话亲和、并发闸门、超时与断连、记账与性能事件。

## 生命周期

启动流程:服务脚本先调用 Python `app.db.schema_upgrade` 完成本地数据库升级与
transition→解析 C++ CLI 参数→打开并校验已准备好的 V1 schema→构建首个
`RoutingSnapshot`→组装转发/codec/记账组件→建线程池并监听。C++ 不读取
`schema/` 中的 SQL，也不执行迁移；数据库版本不满足校验时直接退出并提示先
运行 Python 升级边界。收到 SIGINT/SIGTERM 后停止刷新、探测和记账线程，排空后退出。

后台维护包括:

- enqueue 发现 backlog 且没有空闲 worker 时立即扩容；不等待周期轮询。
- 每 250ms 检查 `config_state.generation`，后台构建并原子替换路由快照。
- info 日志每 10 秒输出 RPS、错误、队列和连接池聚合，成功请求逐条日志仅在 debug。
- 周期清理:`cleanup_stale_in_flight(10)` 清掉 `in_flight_requests` 遗留表里超过 10 分钟的僵死记录(旧版本代理的观测表;新版本已不写)。

## 线程池:SemaphorePool

`SemaphorePool` 初始 worker 数为 `clamp(2×CPU, 8, 64)`，上限 256，队列硬上限 4096。任务记录 enqueue/start 时间并聚合 queue p95；满载拒绝数进入健康指标，避免无限排队。监听 backlog 为 512。

## 路由与认证

`Router` 只读 `shared_ptr<const RoutingSnapshot>`：`client_key → route_set`、`route_set+model → ordered targets`、endpoint/auth/credential 都在一次配置快照内。请求热路径不查询 SQLite，也不复制整份 URL/secret 候选。普通账户在 V1 中同样是一条 `* → upstream` route rule；聚合只是多条 rule，核心不再检查 `is_aggregate`。

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

## 冷却探测

plan 密钥进入 5h 冷却后,后台线程**每隔 `cooldown_probe_interval_secs_`(默认 3600s,`TB_COOLDOWN_PROBE_SECS` 环境变量可覆盖,单位秒)探测一次**该密钥对应的上游:向 `{base_url}/models` 发免费 `GET /models`(OpenAI 与 Anthropic 格式都有此端点),2xx 即 `clear_cooldown` 提前解除,该密钥重新进入候选池;GoUsageLimitError 429 或其它错误保持冷却、下一轮再探。

- 探测线程在 `setup_routes` 启动(`start_cooldown_probe`,幂等),`shutdown()` 内 join(仿 `accounting_thread_` 生命周期);循环按 200ms 切片睡眠,退出迅速。
- 探测目标按 `upstream_keys.id` 查 `lookup_probe_target`(read_db_ 连接,JOIN 账户取 base_url/key/auth),已删密钥/账户直接跳过。
- **纪律**:探测不写 `request_log`、不 `acquire`/`release`(不占 `max_concurrency`)、不动 `mark_failure` 计数——只观察冷却态并清除它。冷却状态依旧纯内存,重启即清。

## 超时与客户端断连

上游超时按客户端线格式配置,存在 `proxy_timeout_config` 表(每行一组,默认值对齐 cc-switch:anthropic 90/180/600、openai_responses 与 openai 60/120/600),由仪表板「通用设置 → 设置」页编辑、代理每次转发时按 harness 线格式读取,改动即时生效、无需重启:

- `streaming_first_byte_timeout` — 流式等首个数据块的最大时间。
- `streaming_idle_timeout` — 流式两个数据块之间的最大间隔,0=禁用。
- `non_streaming_timeout` — 非流式 body 读取超时,作为整个 Get/Post 的读超时(每读无数据 N 秒即超时,等价于 cc-switch 的整包总超时;连接 10s、写 30s 保持固定)。

流式的首字节/静默区分用 `net/upstream_client.cpp` 里的**超时看门狗线程**:httplib 客户端的读超时是 select-based、连接期固定,无法区分"等首块"和"块间间隔",所以看门狗在首字节截止点(请求发出后 `streaming_first_byte_timeout`)和每个块到达后重置的静默截止点(`now + streaming_idle_timeout`)检测超时,到点就 `shutdown()` 上游 socket 打断阻塞读(与客户端断连监视 `spawn_client_monitor` 同机制),阻塞读因此以读错误返回、记 `is_timeout`。httplib 自身读超时设成大的 backstop,只兜底。两个超时都配 0 时不开看门狗。回退链上多次尝试共享一个首字节预算(`clamp_to_remaining_budget`),避免 N 个坏密钥把一次 60s 请求拖成 N×60s。

超时(`is_timeout=true`)对客户端统一给 `{"error":{"type":"timeout_error","code":504}}`,错误消息带实际触发的秒数。流式路径按客户端格式发终止错误帧:Anthropic `event: error`、Responses `response.failed`、OpenAI `data: {error}` + `[DONE]`,让客户端知道上游没回,而不是静默断连。

客户端断连的检测用 `spawn_client_monitor`:一个线程每 250ms poll 客户端 socket,发现 `POLLHUP / POLLERR / POLLRDHUP` 就 `shutdown()` 上游 socket,把阻塞中的上游读立刻打断,释放线程池线程,而不是干等满超时。注意 TCP 对端 FIN 只表现为 `POLLIN + POLLRDHUP`,所以 poll 必须显式请求 `POLLRDHUP`。非流式路径也有等价的 `client_disconnected` 检查,确认客户端已走就把这次请求记为 499(零 token)结束。

## 请求记账与性能事件

HTTP 线程只移动一个 `UsageEvent` 到有界队列。writer 最多 64 条或等待 5ms 成批落 spool/SQLite；活跃事件不再从 spool 二次反序列化，spool 只用于启动恢复。队列满或持久化失败会令健康检查失败并报警。`request_attempts` 还记录 DNS、connect、TLS 与 connection lease wait。

**性能数据源是 `request_log` 的列 + `request_attempts`,不再写 `perf_events`**(旧表仅作兼容):`ttft_ms` / `generation_ms` / `output_tps`(输出速度)、`upstream_ttft_ms` / `upstream_duration_ms`(上游侧耗时)、`attempt_count` / `fallback_count` 支撑延迟 P50/P95/P99、输出速度分布(P50/P95/P99)与各模型 TTFT/速度采样;实时并发来自进程内计数器(`/health` 的 `concurrency`)。request_log 的清理按高水位检查点 + 30 天:`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行,见 [sync.md](sync.md)。

## 端点与健康检查

`setup_routes` 注册的端点见 [api-reference.md](api-reference.md)。除转发端点外还有 `GET /health` 返回 `{"status":"ok","service":"token-board-proxy","concurrency":N}`(concurrency 是进程内实时并发数),供 `scripts/status.sh` 探活与看板实时并发展示。所有 `/v1/*` 端点都带 CORS 头,并处理 `OPTIONS` 预检(204)。
