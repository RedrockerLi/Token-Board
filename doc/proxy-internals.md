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
- 周期清理:`cleanup_stale_in_flight(10)` 清掉 `in_flight_requests` 遗留表里超过 10 分钟的僵死记录(旧版本代理的观测表;新版本已不写)。

## 日志系统

代理日志由 `proxy/src/core/logging.h` 提供，级别从低到高为
`error`、`warn`、`info`、`debug`。`--log-level` 设置阈值，默认是 `info`；例如
`--log-level debug` 会启用全部四个级别。日志消息会立即 flush，适合观察前台进程。

输出流按严重程度分开:

- `error`、`warn` 写入 `stderr`。请求级代理错误使用单行 `key=value` 结构化格式，
  经过 `proxy_log_value` 清理并限制长度；这类常规错误记录不包含 API Key、请求头
  或请求/响应 body。
- `info`、`debug` 写入 `stdout`。`debug` 包括 HTTP 请求、路由选择、请求级诊断等
  高频信息，只有显式启用 debug 级别才会产生。

显式进入前台 debug 后，HTTP 边界会另外输出完整的交换内容，格式带有
`[HTTP_DEBUG][downstream]` 或 `[HTTP_DEBUG][upstream]` 标记:

- 下游 request: 方法、路径、脱敏后的请求头和完整请求 body。
- 上游 request: 实际发送的方法、完整 URL 路径、脱敏后的请求头和转换后的 body。
- response: 状态码和响应头；非流式响应输出完整 body，流式响应逐 chunk 输出，所以
  转换前的上游数据和转换后实际写给下游的数据都能逐段对照。

`Authorization`、`x-api-key`、Cookie、名称中带 `api-key` 或 `token` 的头值会显示为
`<redacted>`；会话标识头（`x-opencode-session`、`x-session-id` 等）与 `User-Agent` 不是凭据，
按原值输出。body 不做脱敏，可能包含 prompt、工具参数或 provider 返回内容。因此
这组日志只在需要时以前台 `--debug` 查看，不应作为长期日志保存。

### systemd 常驻模式

`start.sh --all` 和 `scripts/start-proxy.sh --install` 生成的 `token-proxy.service`
使用:

```ini
StandardOutput=null
StandardError=journal
```

因此常驻服务会丢弃 `info`/`debug`，避免详细诊断进入持久化 journal；`warn`/`error`
以及 systemd 自身的启动、停止、失败事件仍可通过下面的命令查看:

```bash
journalctl --user -u token-proxy -f
```

这里的 `journalctl` 只是读取 journald，不能动态把一个已经运行的 systemd 进程的
标准输出接到当前终端。代理的请求用量、计费和性能字段仍按正常业务流程写入
SQLite 的 `request_log`，与 stdout/stderr 日志策略无关。

### 按需前台 debug

需要完整诊断时运行:

```bash
bash start.sh --debug
# 等价的代理专用入口:
# bash scripts/start-proxy.sh --debug
```

脚本先验证 schema；如果 `token-proxy` 正在运行，就先停止该 systemd 服务，然后用
同一个代理二进制在当前终端前台启动 `--log-level debug`。此时 stdout 和 stderr 都
继承当前终端，所以能看到路由日志以及每一次上下游 HTTP request/response 的完整内容，
且这些消息不会写入 journal。调试期间会有短暂服务中断，不会同时运行两个代理；按
`Ctrl+C` 或进程退出后，脚本会恢复调试前的服务状态。

## 线程池:SemaphorePool

`SemaphorePool` 初始 worker 数为 `clamp(2×CPU, 8, 64)`，上限 256，队列硬上限 4096。任务记录 enqueue/start 时间并聚合 queue p95；满载拒绝数进入健康指标，避免无限排队。监听 backlog 为 512。

## 路由与认证

`Router` 只读 `shared_ptr<const RoutingSnapshot>`：`client_key → route_set`、`route_set+model → ordered targets`、endpoint/auth/credential 都在一次配置快照内。请求热路径不查询 SQLite，也不复制整份 URL/secret 候选。普通账户在 V1 中同样是一条 `* → upstream` route rule；聚合只是多条 rule，核心不再检查 `is_aggregate`。

认证同时接受两种头:`Authorization: Bearer <密钥>` 与 `x-api-key: <密钥>`,与客户端用什么格式无关,密钥只用来选账户。

转发到上游的请求头由代理自己组装:认证头(anthropic 线格式用 `x-api-key` + `anthropic-version`,其余用 `Authorization: Bearer`)、`Content-Type`,以及下游带来的 `anthropic-beta`;下游的 User-Agent 不转发。带 OpenCode-Go 子分类(规格里 `session_header=true`)的账户另注入 `x-opencode-session`(下游透传 → 会话标识 → 稳定会话指纹 → 随机),并带固定 `User-Agent: token-board/1.0`。

## 候选选择与回退

`resolve_candidates` 先给模型名剥掉 Claude Code 的 `[1m]` / `[1M]` 后缀,再按路由结果取候选:

- 普通账户:一个或多个候选(账户的每把上游密钥一个槽位)。
- 聚合账户:每一条路由规则是一个 `priority` 组,组内是该账户的全部密钥;组间严格按优先级顺序尝试——组内均衡只在组内重排,绝不会为了均衡把请求转到更低优先级的组。

组内顺序由 `candidate_order`(core/candidate_selection.h)生成,升序依次比较:

1. **两个整体后置档**:**耗尽最末**——任一用量窗口 ≥100% 且其可信重置时刻未到的密钥整体排到最后(跨组),组内按最早可信重置升序,重置未知者再靠后;此后**订阅已停付**(用量轮询器的 403 EntitlementError 判定,`subscription="inactive"`)的密钥再沉到耗尽者之后。只要存在未停付、未耗尽的候选,它们绝不被选中;全部不可用时才先按最快恢复、再轮到停付者兜底尝试(上游仍是最终权威)。窗口判定按请求实时计算——重置时刻一过,该窗口自动重新可用,不必等配置重载;停付判定以其证据时刻 `error_at`(每次失败轮询刷新)保鲜,超过 48 小时未刷新即失效、恢复正常排序。
2. **会话绑定优先**:该会话最近一次成功服务的密钥排所在组最前(缓存亲和优先于均衡;但重不过"耗尽",也只在组内生效)。
3. **月 → 周 → 5h 三级窗口压力**:压力 = (100−percent) / 距重置秒数,大者先,无容差(实现为整数交叉相乘——比浮点除法略省,且天然精确);两边都有可信信号(快照新鲜 ≤10 分钟、重置未到;5h 还须 `state=started`,周/月须 `state≠unknown`)才在该级分出先后,一方有信号另一方没有时有信号者排前,压力相等才降级到下一级。
4. **累计消耗**:等价成本低者先(见下节成本台账);没有用量信息的密钥只在这一级竞争,不会因缺数据被动受损。
5. 全部并列时按请求级轮转偏移在组内环绕,精确并列随请求分散,不钉死最小下标。

尝试循环沿顺序挑:跳过处于配额冷却或各类避让期的槽位,占用并发槽失败(达到 `max_concurrency`)的也跳过,选中即发起转发。

请求分两种走法:

- **流式**:chunked 响应头**故意延迟到首个数据块写出前才提交**,所以上游在首字节前返回 429/5xx 时可以在当前请求内换下一候选重试;一旦 `committed`(任何字节已发给客户端)则不再切换,剩余部分按上游流原样转发。首个错误帧仍可触发回退。
- **非流式**:循环候选。上游返回 429 或 5xx、且还没向客户端发任何数据时,释放当前密钥槽的并发槽,试下一个;plan 密钥收到上游明确的配额耗尽 429 按失败分类进入冷却(只冷却这一把,时长优先取可信重置)。全部候选失败返回 429。

可回退的失败是密钥级故障:401/403、"配额耗尽",普通 429、5xx 与超时;请求内容或参数错误(400 等)不换密钥重试。超时(`is_timeout`,504)属于"结果未知"——上游可能已经处理并计费,这里选择可用性优先,同时保留可审计轨迹(`request_attempts.is_timeout` + `attempt_index` 可还原哪次重试发生在未知结果之后),整条回退链共享一个首字节预算,避免 N 个坏密钥把一次请求拖成 N 倍超时。全部候选失败时,若最后失败是超时则返回 504,否则 429。每次尝试都会写一行 `request_attempts`,失败的上游不会从监控里消失。嵌入端点 `/v1/embeddings` 也是非流式,走同一套候选排序与失败状态,但没有格式转换。

## 会话亲和与成本平衡

`SessionAffinity`(core/session_affinity.h)是进程内的"会话 → 上游密钥"绑定:同一 `session_id` 的请求从上次成功服务的密钥开始(成功才绑定、回退会重绑),保护上游侧缓存命中。绑定是内存 LRU(上限 10 万条、24 小时过期),不写库。空会话、无绑定或绑定的密钥不在本次候选里时,组内起点完全交给上一节的排序。

**成本台账**`KeyCostLedger`(core/key_cost_ledger.h)衡量每把密钥的累计磨损:探测线程每 5 分钟(`TB_LEDGER_REFRESH_SECS`)**从 `request_log` 聚合**保留期内每把密钥的已计价等价成本;已计价为 0 但确实消耗了 token 的行,按全局每 token 单价折算进来(账单为零不等于消耗为零),请求数只用于估算单次平均成本、不充当消耗量。排序用的 `effective_cost = 累计成本 + 在途数 × 该槽单次平均成本`——在途数取自并发闸门的实时计数,同时到达的并发请求因此不会都盯着同一把"当前最便宜"的密钥,而是自然摊开。台账来源于库聚合,重启不丢(启动即刷一次)。

代理按单实例运行:同一 `data/token-board.db` 上第二个代理进程会因 request-log spool 的独占文件锁(`<db>.request-log.spool`)启动失败;会话绑定与冷却/避让状态都是进程内状态,重启即清,成本台账则由首次聚合立即恢复。

## 并发闸门:AccountGate

`core/account_gate.h` 是纯内存的**按密钥槽**闸门,管两件事:并发计数与失败状态(全部进程内,重启即清——见上节的单实例说明)。

- 并发:`try_acquire_eligible` 对每把密钥独立计数,**包括 `max_concurrency <= 0`(不限)的密钥也计数**,`in_flight()` 暴露在途数供成本台账使用;一把密钥打满并发,同账户的下一把立即接管。槽位回收:每个成功 `acquire` 配一个 `release`,重试循环用 `GateLease` RAII 兜底——回调抛异常也不会漏计数或漏释放。没有按年龄强回收的 `SLOT_TTL`,健康的长时间流必须保住自己的槽位。
- 失败状态按类分别记录,不合并成同一种冷却(全部按密钥槽,不波及同账户其它密钥):
  - **配额耗尽**(429 带 `GoUsageLimitError`/`limitName`,且合同声明 `subscription_5h`):冷却时长优先取可信重置——耗尽窗口的最早 `resetsAt`,其次是 429 报文 `metadata.limitName` 命中窗口的 `resetsAt`,再退回 `PLAN_COOLDOWN` 5 小时;同时记录证据 {窗口, 当时的重置时刻, 标记时刻} 供恢复判定。
  - **普通限速**(其余 429):短避让,时长优先取上游 `Retry-After`(clamp 1..86400 秒),否则 60 秒。
  - **凭证失效**(401/403):15 分钟避让。
  - **上游故障**(5xx/超时;本地转换失败不归咎密钥):30 秒避让。
  后三类避让在任意一次成功后立即清除;配额冷却只由恢复证据解除(见下节),成功不清除——冷却中的槽位根本不会被选中。
- 所有候选都被跳过时的终态:本地容量 503(优先) > 配额冷却 429 > 普通限速 429(带"最近到期"的 `Retry-After`) > 其余不可用 503。

实时并发不写库:请求热路径只更新进程内计数器,`GET /health` 的 `concurrency` 字段暴露它;`in_flight_requests` 表是旧版本的遗留,当前代理不再写入(启动与周期清理仍会清它的残留)。

## 冷却探测

plan 密钥进入配额冷却后,后台线程按两条通道观察恢复,任一确认即 `clear_cooldown` 提前解除,该密钥重新进入候选池;冷却到期时间本身(优先取可信重置)仍是兜底:

- **用量通道(opencode-go)**:每 `usage_watch_interval_secs_`(默认 300s,`TB_USAGE_WATCH_SECS` 环境变量可覆盖)读一次该密钥的 `usage_json` 快照(用量轮询器每 ~5 分钟刷新,与调度同源)——纯本地读,不发任何 HTTP。判定有三条纪律:快照必须新鲜(10 分钟内),仍存在"percent ≥100 且重置未到"的窗口时冷却维持(已确认的耗尽跨过期持续生效,过期或不完整的数据不能解除它),解除需要**重置已发生**的证据——标记时记录的窗口在快照里的重置时刻已经改变,从而不会重蹈"百分比恰好读作 99% 就解除"的 429 循环;没有窗口证据可记时(快照缺重置),以"标记之后完成的观测且已无耗尽窗口"为准。该网关的模型目录端点对任何(甚至无效)密钥都返回 200,不能用作健康探测。
- **聊天探针(其余 plan 密钥)**:每 `cooldown_probe_interval_secs_`(默认 3600s,`TB_COOLDOWN_PROBE_SECS` 环境变量可覆盖)向账户发一条最小的"你好"(按账户线格式取 messages/chat/responses 路径,`max_tokens=1`),2xx 即恢复——一次完成的调用才是可用性的真信号;账户没有目录模型名("更新模型"从未运行)时退回免费 `GET {base}/models` 探测。GoUsageLimitError 429 或其它错误保持冷却、下一轮再探。

- 探测线程在 `setup_routes` 启动(`start_cooldown_probe`,幂等),`shutdown()` 内 join(仿 `accounting_thread_` 生命周期);循环按 200ms 切片睡眠,退出迅速;同一线程还有第三条节拍——每 `TB_LEDGER_REFRESH_SECS`(默认 300s)从 `request_log` 刷新成本台账聚合。
- 探测目标按 `upstream_credentials.runtime_id` 查 `lookup_probe_target`(read_db_ 连接,JOIN 账户取 base_url/key/auth,并带该密钥的 usage_json 与目录首模型),已删密钥/账户直接跳过。
- **纪律**:探测不写 `request_log`、不 `acquire`/`release`(不占 `max_concurrency`)、不新增冷却态——只观察并在证据确认时 `clear_cooldown`。冷却状态依旧纯内存,重启即清。

## 超时与客户端断连

上游超时按客户端线格式配置,存在 `proxy_timeout_config` 表(每行一组,默认值对齐 cc-switch:anthropic 90/180/600、openai_responses 与 openai 60/120/600),由仪表盘「通用设置 → 设置」页编辑、代理每次转发时按 harness 线格式读取,改动即时生效、无需重启:

- `streaming_first_byte_timeout` — 流式等首个数据块的最大时间。
- `streaming_idle_timeout` — 流式两个数据块之间的最大间隔,0=禁用。
- `non_streaming_timeout` — 非流式 body 读取超时,作为整个 Get/Post 的读超时(每读无数据 N 秒即超时,等价于 cc-switch 的整包总超时;连接 10s、写 30s 保持固定)。

流式的首字节/静默区分用 `net/upstream_client.cpp` 里的**超时看门狗线程**:httplib 客户端的读超时是 select-based、连接期固定,无法区分"等首块"和"块间间隔",所以看门狗在首字节截止点(请求发出后 `streaming_first_byte_timeout`)和每个块到达后重置的静默截止点(`now + streaming_idle_timeout`)检测超时,到点就 `shutdown()` 上游 socket 打断阻塞读(与客户端断连监视 `spawn_client_monitor` 同机制),阻塞读因此以读错误返回、记 `is_timeout`。httplib 自身读超时设成大的 backstop,只兜底。两个超时都配 0 时不开看门狗。回退链上多次尝试共享一个首字节预算(`clamp_to_remaining_budget`),避免 N 个坏密钥把一次 60s 请求拖成 N×60s。

超时(`is_timeout=true`)对客户端统一给 `{"error":{"type":"timeout_error","code":504}}`,错误消息带实际触发的秒数。流式路径按客户端格式发终止错误帧:Anthropic `event: error`、Responses `response.failed`、OpenAI `data: {error}` + `[DONE]`,让客户端知道上游没回,而不是静默断连。

流式请求另有**终端事件校验**:上游 2xx 流干净结束(chunked 正常收尾或读到 EOF)时,若解析器从未发布终止事件,按截断失败处理——对外状态 502、消息 `Upstream stream truncated before terminal event`,已向客户端提交过的流向其补发终止错误帧,截断的密钥按 5xx 规则获得 30 秒上游故障避让;未提交的截断(空体 200)可换候选重试。终止事件按上游线格式认定:Anthropic `message_delta.stop_reason` 或 `message_stop`,OpenAI chat `choices[].finish_reason` 或 `data: [DONE]`,Responses `response.completed`/`response.incomplete`;结束前仍驻留在 SSE 帧缓冲里的终止帧先冲净再判定。非 2xx 响应不参与该校验(按 HTTP 状态分类)。环境变量 `PROXY_STRICT_TERMINAL=0` 关闭校验(干净结束即按成功计费),默认开启。已知限制:上游忽略 `stream:true`、以整包 JSON 200(无 `data:` 帧)应答时没有终止事件,按截断处理。

客户端断连的检测用 `spawn_client_monitor`:一个线程每 250ms poll 客户端 socket,发现 `POLLHUP / POLLERR / POLLRDHUP` 就 `shutdown()` 上游 socket,把阻塞中的上游读立刻打断,释放线程池线程,而不是干等满超时。注意 TCP 对端 FIN 只表现为 `POLLIN + POLLRDHUP`,所以 poll 必须显式请求 `POLLRDHUP`。非流式路径也有等价的 `client_disconnected` 检查,确认客户端已走就把这次请求记为 499(零 token)结束。

## 请求记账与性能事件

HTTP 线程只移动一个 `UsageEvent` 到有界队列。writer 最多 64 条或等待 5ms 成批落 spool/SQLite；活跃事件不再从 spool 二次反序列化，spool 只用于启动恢复。队列满或持久化失败会令健康检查失败并报警。`request_attempts` 还记录 DNS、connect、TLS 与 connection lease wait。

**性能数据源是 `request_log` 的列 + `request_attempts`,不再写 `perf_events`**(旧表仅作兼容):`ttft_ms` / `generation_ms` / `output_tps`(输出速度)、`upstream_ttft_ms` / `upstream_duration_ms`(上游侧耗时)、`attempt_count` / `fallback_count` 支撑延迟 P50/P95/P99、输出速度分布(P50/P95/P99)与各模型 TTFT/速度采样;实时并发来自进程内计数器(`/health` 的 `concurrency`)。request_log 的清理按高水位检查点 + 30 天:`cleanup_exported_logs` 只删 `id ≤ 检查点 且 请求时间超过 30 天` 的行,见 [sync.md](sync.md)。

## 端点与健康检查

`setup_routes` 注册的端点见 [api-reference.md](api-reference.md)。除转发端点外还有 `GET /health` 返回 `{"status":"ok","service":"token-board-proxy","concurrency":N}`(concurrency 是进程内实时并发数),供 `scripts/status.sh` 探活与看板实时并发展示。所有 `/v1/*` 端点都带 CORS 头,并处理 `OPTIONS` 预检(204)。
