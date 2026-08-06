# review-2026-08-06 处置记录

> 对 [doc/review-2026-08-06.md](doc/review-2026-08-06.md) 各条结论的处置结果、实验数据与遗留项。
> 除注明"不改"外,均已在 `main` 分阶段提交并配有 ctest 回归。

## 逐条处置

| # | 结论 | 处置 | 提交/证据 |
|---|------|------|----------|
| 1 | 上传残留 auth_header/base_url/endpoint_path 未脱敏 | **修复**:`_sanitize_upload_columns` 掩码 scheme token + URL 剥 userinfo/query/fragment | Phase 4 `dc52243` |
| 2 | 局域网暴露无鉴权 | **修复**:`app/dashboard_auth.py` 访问口令(暴露绑定或 `TB_DASHBOARD_TOKEN` 时启用),`/health` 仅回环返回并发 | Phase 4 `dc52243` |
| 3 | 每请求新建 httplib Client | **修复**:`forward()` 接线 ClientPool+DnsResolver(复用 + DNS 缓存 + 地址级重试),修 `cancel_client` 未赋值、`attempt_budget_ms` 死值,开 TCP_NODELAY(httplib 默认关,否则池化后每次往返 ~40ms) | Phase 0/1 `9452950` |
| 4a | 非流式→Deepseek 全落 | **实测确认**:zen/go 非流式 3 天 259/259 全败(217×500),流式 92% 成功;Deepseek API 非流式 91% 成功。**方向(用户定):客户端配流式 + 文档**,代理不改写 | README FAQ;Phase 3 |
| 4b | 任一次 429 锁 5h | **修复**:`record_failure` 以 `error.type=="GoUsageLimitError"`(真实 zen/go 报错形态)判据,配额耗尽才 `mark_cooldown`,普通 429 走 5s/30s/2min 瞬时退避 | Phase 2 `bb10aaa` |
| 5 | 已 commit 停滞 504 无法回退 | **不改(用户决策:超时就超时)**:B3 实验确认机制(4s 配置 idle → 504,attempt_count=1,不回退);README 补充说明 | `cfad63d`;README |
| 6 | schema 冗余 | **部分修复**:0017 删 perf_events/in_flight_requests 死表 + DROP COLUMN upstream_key(生产 0 账户依赖 legacy 单列);仪表板改读 /health。**遗留**:计价触发器抽函数、monthly_price 双价源、dashboard 双镜像表(见下) | Phase 5/6 `a4d4288` |
| 7 | 代码硬编码点 | **部分修复**:删 C++ legacy 单列双路径(-account_id 负槽位),proxy_db 默认类型改读 spec()(api)。**遗留**:冷却时长数据化(见下) | Phase 5/6 `a4d4288` |

## 实验矩阵(全部并入 ctest,`fallback_matrix` / `cooldown_discrimination`)

| 实验 | mock 场景 | 断言链 |
|------|-----------|--------|
| A1 | plan key1 GoUsageLimit, key2 200 | [plan/429, plan/200];下个请求跳过 key1 |
| A1b | plan key1 普通 429 | 仅瞬时退避;6s 后 key1 恢复可试(非 5h) |
| A2 | 两 plan key 均 GoUsageLimit, Deepseek 200 | [plan/429, plan/429, api/200];下个请求跳过两 key |
| A3 | 无兜底 + 两 key 冷却 | 0-attempt 快速 429 |
| A4 | plan 全冷却 + Deepseek 502 | [429,429,502] → 0-attempt 429 → 6s 后恢复 [200] |
| B1 | 非流式 5xx 快败 | [plan/500, plan/500, api/200] 同请求回退 |
| B2 | 流式未提交 429 | 错体缓冲不落客户端 → [plan/429, plan/429, api/200] |
| B3 | 流式 commit-then-stall(#5) | 语义 idle 4s → 504,attempt_count=1,不回退 |

注:B3 的 `mock_stall_after` 需 ≥2 才能先提交**语义帧**再停滞;`=1` 会在首个语义事件之前停滞,不触发 idle。

## 性能压测 Before/After(连接池接线,并发 16,loopback mock)

| 指标 | Before | After |
|------|--------|-------|
| 连接复用 | 50 请求 = 50 连接 | 50 请求 = 1 连接 |
| 流式吞吐 | 1581 RPS | 1910 RPS(+21%) |
| 流式 p50 | 9.6ms | 7.7ms |
| 非流式吞吐 | 115 RPS | 3071 RPS(26x) |
| 非流式 p99 | 205ms | 6.9ms |

`upstream_client_perf reuse` 为确定性回归门槛;`fresh` 模式保留作对照。压测脚本 `perf_stress_test.py` 可跑任意并发/请求数。

## 进一步发现并修复的 bug

1. **测试断言在 Release 下全部空转**:`-DNDEBUG` 使 `assert()` 编译消失,现有 ctest 从未真正断言。已在 `proxy/tests/CMakeLists.txt` 加 `-UNDEBUG`。
2. **httplib 默认 TCP_NODELAY=false**:池化 keep-alive 下每次小请求/响应往返多付 ~40ms(Linux 延迟 ACK+Nagle);`make_client` 开 `set_tcp_nodelay(true)`,测试 mock 同样开启。
3. **`ForwardWatch::cancel_client` 从未赋值**:池化复用后 `set_socket_options` 不再触发,看门狗取消会失效;现按 lease `attach_client`。
4. **`attempt_budget_ms` 死值**:现 DNS 等待与 connect 共享同一预算(由 `connection_timeout` 派生)。
5. **`in_flight_requests` 死写**:`request_start/request_end` PREPARE 未调用,表恒空;0017 删表并清掉 C++ 侧全部相关语句。
6. **mock_upstream 的 chunked 终止帧错误**:`0\r\n\r\n` 曾通过 `_write_chunk` 被框架化为 `5\r\n0\r\n\r\n\r\n`,导致读取方永久阻塞;已修正为直接写出。

## 遗留项(建议后续专项,本次未做)

- **计价触发器抽函数**:0002/0007/0012/0014 四版重复的巨型计价表达式 + C++ `stmt_snapshot_price_` 双轨。抽成单一 SQL 视图/函数需等价性回归(涉及钱),风险/收益不匹配,本次未动。
- **双价源**:`upstream_accounts.monthly_price` 与 `plan_price_history` 去一存一。
- **dashboard.db 双镜像表**:`account_types`(0001)与 `accounts`(0004)合并。
- **冷却时长数据化**:`PLAN_COOLDOWN=5h` 仍为 C++ 常量;如需按账户类型/上游配置,建议连同上述 schema 专项一起做。

## 部署注意事项

- 迁移 `schema/proxy/0017_drop_dead_tables.sql` 由代理启动时自动应用(v16→v17)。**代理二进制与仪表板代码需同版本升级**(0017 删 `upstream_key` 列,旧 Python 账户 CRUD 会引用该列)。
- 生产库副本已验证:v16→v17,`upstream_key` 列删除、账户/请求日志数据完整、Python 账户增删改查正常。
- 仪表板鉴权:默认 loopback 免鉴权;若用 `--host 0.0.0.0` 暴露且未设 `TB_DASHBOARD_TOKEN`,首次启动会生成 `data/dashboard_token.txt` 并在日志提示 `/login`。
- 压测/实验脚本均以临时库 + mock_upstream 运行,不影响生产 `data/proxy.db`。
