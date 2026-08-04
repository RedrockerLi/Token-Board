# 代理内部机制

代理是 C++17 单进程,入口 `proxy/src/main.cpp`,基于 httplib 提供 HTTP 服务。本文按一条请求的旅程讲内部组件:生命周期、线程池、路由与认证、候选选择与回退、并发闸门、超时与断连、性能事件。

## 生命周期

启动流程:解析 CLI 参数(`config.cpp`)→ 打开 SQLite 并跑迁移(`db.cpp::open`,schema 目录未指定时由 `--db` 推导为 `<db目录>/../schema/proxy`)→ 组装 Router / UpstreamClient / UsageTracker / CodecRegistry → 建 httplib server 与信号量线程池 → 监听。收到 SIGINT/SIGTERM 后优雅停机,先把在途请求的 `in_flight_requests` 残留清空再退出。

主循环里做两件事,每 5 分钟一次:

- 线程池扩容判断:当 `in_flight >= 当前线程数` 且未到上限时,线程数翻倍。
- 周期清理:删除超过 24 小时的 `perf_events`(`cleanup_old_perf_events(1440)`),清理超过 10 分钟的僵死在途记录(`cleanup_stale_in_flight(10)`)。

## 线程池:SemaphorePool

`semaphore_pool.h` 用 POSIX 计数信号量实现 httplib 的 `TaskQueue`,替代 httplib 自带线程池。初始 8 线程,`main` 里上限 2048;`resize()` 只增不减。信号量的好处是 `sem_post` 每次恰好唤醒一个等待线程,没有惊群。请求进队时在锁外 post,唤醒的线程可立即进入临界区。

## 路由与认证

`Router::route` 把本地密钥映射到上游账户:查 `local_keys` 关联的 `upstream_accounts`,结果缓存在内存。命中缓存 60 秒,未命中(无效密钥)只缓存 10 秒,避免高压下打爆 SQLite。聚合账户只做一级路由,真正的账户由请求里的模型名在 `resolve_candidates` 里二次解析。

认证同时接受两种头:`Authorization: Bearer <密钥>` 与 `x-api-key: <密钥>`,与客户端用什么格式无关,密钥只用来选账户。

## 候选选择与回退

`resolve_candidates` 先给模型名剥掉 Claude Code 的 `[1m]` / `[1M]` 后缀,再按路由结果取候选:

- 普通账户:一个候选,上游模型名就是请求模型名。
- 聚合账户:按精确模型名匹配 `aggregate_entries`,同一模型可能命中多条,按 `sort_order, id` 排序,构成候选链。

`pick_candidate` 从链头开始挑:跳过处于冷却期的账户,占用并发槽失败(达到 `max_concurrency`)的也跳过,选中即返回。

请求分两种走法:

- 流式:校验 harness 请求(避免 400 提前返回漏掉并发槽)后,一次选定候选转发,不做回退——chunked 响应头发出后无法切换账户。
- 非流式:循环候选。上游返回 429 或 5xx、且还没向客户端发任何数据时,释放当前密钥槽的并发槽,试下一个。plan 密钥收到 429 会触发该密钥 5 小时冷却。全部候选失败返回 429。

超时(`is_timeout`,504)与 429/5xx 一样参与回退——上游卡死不吐数据视为 provider 故障,换下一个账户(对齐 cc-switch 把超时归为 Retryable);全部候选失败时,若最后失败是超时则返回 504,否则 429。嵌入端点 `/v1/embeddings` 也是非流式,走同样的候选循环,但没有格式转换。

## 并发闸门:AccountGate

`account_gate.h` 是纯内存的每账户并发闸门,配合 `in_flight_requests` 表:

- `acquire(account_id, max_concurrency)`:`max_concurrency <= 0` 视为不限,直接放行。并发槽按账户计数,超限拒绝。
- 槽位泄漏防护:正常每个 `acquire` 配一个 `release`,但客户端在流式响应头发出前断连、上游永远不回调时,槽位会一直占着。`SLOT_TTL`(10 分钟)兜底——`acquire` 发现某账户槽位占用超过 10 分钟,先整体回收再判断。
- `mark_cooldown`:plan 密钥槽收到 429 后冷却 `PLAN_COOLDOWN`(5 小时),期间 `in_cooldown` 返回 true,路由跳过该槽位而非整账户。冷却态不落库,重启即清。流式响应头在代理首次写出 SSE 前延迟提交，因此上游在首字节前返回 429/5xx 时也可在当前请求内回退。

`in_flight_requests` 表由 `db.cpp` 维护(`request_start` / `request_end`),看板的实时并发与线程池扩容都读它。

## 超时与客户端断连

上游超时按客户端线格式配置,存在 `proxy_timeout_config` 表(每行一组,默认值对齐 cc-switch:anthropic 90/180/600、openai_responses 与 openai 60/120/600),由仪表板「设置」页编辑、代理每次转发时按 harness 线格式读取,改动即时生效、无需重启:

- `streaming_first_byte_timeout` — 流式等首个数据块的最大时间。
- `streaming_idle_timeout` — 流式两个数据块之间的最大间隔,0=禁用。
- `non_streaming_timeout` — 非流式 body 读取超时,作为整个 Get/Post 的读超时(每读无数据 N 秒即超时,等价于 cc-switch 的整包总超时;连接 10s、写 30s 保持固定)。

流式的首字节/静默区分用 `upstream_client.cpp` 里的**超时看门狗线程**:httplib 客户端的读超时是 select-based、连接期固定,无法区分"等首块"和"块间间隔",所以看门狗在首字节截止点(请求发出后 `streaming_first_byte_timeout`)和每个块到达后重置的静默截止点(`now + streaming_idle_timeout`)检测超时,到点就 `shutdown()` 上游 socket 打断阻塞读(与客户端断连监视 `spawn_client_monitor` 同机制),阻塞读因此以读错误返回、记 `is_timeout`。httplib 自身读超时设成大的 backstop,只兜底。两个超时都配 0 时不开看门狗。

超时(`is_timeout=true`)对客户端统一给 `{"error":{"type":"timeout_error","code":504}}`,错误消息带实际触发的秒数。流式路径按客户端格式发终止错误帧:Anthropic `event: error`、Responses `response.failed`、OpenAI `data: {error}` + `[DONE]`,让客户端知道上游没回,而不是静默断连。

客户端断连的检测用 `spawn_client_monitor`:一个线程每 250ms poll 客户端 socket,发现 `POLLHUP / POLLERR / POLLRDHUP` 就 `shutdown()` 上游 socket,把阻塞中的上游读立刻打断,释放线程池线程,而不是干等满超时。注意 TCP 对端 FIN 只表现为 `POLLIN + POLLRDHUP`,所以 poll 必须显式请求 `POLLRDHUP`。非流式路径也有等价的 `client_disconnected` 检查,确认客户端已走就把这次请求记为 499(零 token)结束。

## 请求记账与性能事件

每次请求的结局都写进 `request_log`:`UsageTracker` 解析上游返回的 usage(三种格式各有非流式与 SSE 解析器,见 [format-conversion.md](format-conversion.md)),连同状态码、耗时一起记入;解析不出 usage 时也记一行零 token,保证日志完整真实。cost / virtual_cost 由 `tr_request_log_insert` 触发器写时固化,见 [billing-pricing.md](billing-pricing.md)。

`perf_events` 记性能快照:upstream 首字节耗时(TTFT)、代理侧总耗时、状态码、当时并发数。看板的性能监控页也读 `request_log`(状态码、duration_ms、token),`perf_events` 与 `request_log` 双源互补。request_log 的清理只动 `exported=2` 的最旧行,见 [sync.md](sync.md)。

## 端点与健康检查

`setup_routes` 注册的端点见 [api-reference.md](api-reference.md)。除转发端点外还有 `GET /health` 返回 `{"status":"ok","service":"token-board-proxy"}`,供 `scripts/status.sh` 探活。所有 `/v1/*` 端点都带 CORS 头,并处理 `OPTIONS` 预检(204)。
