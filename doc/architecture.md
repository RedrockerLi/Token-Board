# 总体架构

Token Board 由两个独立进程组成,共享同一份版本化数据库 schema。C++17 代理负责转发与计费,Python/Flask 看板负责配置管理与用量可视化。两者都直接读写各自的 SQLite 库,同一个库里也可能被两个进程访问——代理写 `proxy.db`,看板管理 `proxy.db` 的配置并把聚合结果写进 `dashboard.db`。

## 两个进程,一份 schema

```
                 ┌──────────────────────────────────────────┐
 AI 工具        │  token_proxy  (C++17, 端口 8800)          │
 (Claude Code)  │  认证 → 路由 → 并发闸门 → 转发/格式转换    │
     │  POST    │        │ 写 request_log / request_attempts     │
     └─────────▶│        ▼                                 │
                 │    data/proxy.db                        │
                 └──────────────────────────────────────────┘

                 ┌──────────────────────────────────────────┐
                 │  server.py  (Python/Flask, 固定端口 5000) │
                 │  配置 CRUD │ 看板 API │ Agent 导入 │ 同步  │
                 │        │          │          │       │   │
                 │        ▼          ▼          ▼       ▼   │
                 │   data/proxy.db  data/dashboard.db  WebDAV│
                 └──────────────────────────────────────────┘
```

schema 的单一来源是 `schema/` 目录下的版本化迁移文件,见 [database-migrations.md](database-migrations.md)。当前 baseline 位于 `schema/proxy/v1/1-0_baseline.sql` 与 `schema/dashboard/v1/1-0_baseline.sql`；V0 文件按原内容归档在各自 `v0/`。C++ 与 Python runner 共用 flock、`schema_version`、checksum 与 `user_version` 镜像协议。

两个库各管一件事:

- `data/proxy.db` 是代理的运行库:上游账户、多密钥、本地密钥、模型定价、峰谷档位、请求日志与每次上游尝试、Plan 计费台账、汇率与导入游标、WebDAV 配置(`perf_events` / `in_flight_requests` 是旧版本遗留表,新代理不再写入)。
- `data/dashboard.db` 是可视化库:按 日×模型×用户 聚合后的 Token 用量、请求数、费用,以及 plan 经济账。代理本身不写它,由看板的导出流水线写入。

## 目录结构

```
├── server.py               看板入口(解析参数、调 create_app、负责后台任务停机)
├── start.sh                默认安装 token-dashboard 开机自启;--all 再启动代理
├── app/                    Flask 应用包
│   ├── __init__.py         create_app() 应用工厂,按需挂代理管理蓝图,并启动
│   │                       后台线程(汇率预热 + Codex 用量导入等)
│   ├── config.py           应用常量与日志配置
│   ├── routes.py           数据看板蓝图(/、/api/*)
│   ├── proxy_routes.py     代理管理蓝图(/api/proxy/*,含并发测试、性能、同步)
│   ├── proxy_db.py         proxy.db 的 Python 访问层(CRUD/计费/导出/性能/plan 台账)
│   ├── dashboard_db.py     dashboard.db 访问层(增量 upsert / account_id 归并 / 载入 IR)
│   ├── data_loader.py      DataStore 单例:读 dashboard.db 存档
│   ├── sync.py             WebDAV 同步(配置上传/下载、dashboard 云端权威事务)
│   ├── migrations.py       Python 侧迁移 runner
│   ├── cost_allocator.py   V1 已归属成本兼容读取
│   ├── ir.py               平台无关 IR 数据类(TokenUsage/RequestUsage/CostEntry)
│   ├── fx.py               USD→CNY 汇率拉取与缓存(本地,不上云)
│   └── services/codex_import.py  Codex 会话用量后台导入(增量游标,幂等)
├── proxy/                  C++17 代理(CMake 工程)
│   ├── CMakeLists.txt      构建(OpenSSL + Threads,vendored sqlite3/httplib/json)
│   ├── src/
│   │   ├── main.cpp        入口、线程池扩容、周期清理、优雅停机
│   │   ├── core/           路由、并发闸门、会话亲和、成本台账、服务端
│   │   │   ├── config.cpp      CLI 参数
│   │   │   ├── router.cpp      不可变 RoutingSnapshot(250ms generation 刷新)
│   │   │   ├── proxy_server.cpp 三个 chat 端点共用管线、候选回退、流式处理、/health
│   │   │   ├── account_gate.h   按密钥槽并发闸门 + plan 5h 冷却 + 短熔断退避
│   │   │   ├── key_cost_ledger.h 累计成本台账(冷启动选最省密钥,内存)
│   │   │   └── session 亲和(SessionAffinity,在 proxy_server.h)
│   │   ├── net/            上游转发与线程池
│   │   │   ├── upstream_client.cpp/h  上游 HTTP 转发(超时看门狗、客户端断连监视)
│   │   │   └── semaphore_pool.h       信号量线程池(8→256 动态扩容)
│   │   ├── format/         IR 与三格式 codec、think 过滤
│   │   │   ├── ir.*        聊天请求/响应/流的中间表示
│   │   │   ├── codec.*     格式编解码注册表
│   │   │   ├── format_openai/anthropic/responses.* 三种线格式 codec
│   │   │   ├── format_common.* 缓存命中读取、tool_choice 归一化、SSE 分帧
│   │   │   └── think_filter.*  <think> 内容抽取(非流式 + 流式状态机)
│   │   └── store/          SQLite 访问与 usage 解析
│   │       ├── db.cpp/h    SQLite 访问、迁移、快照计价、预编译语句
│   │       └── usage_tracker.cpp/h 三种格式的 usage 解析,写 request_log / request_attempts
│   └── tests/              codec 自测(格式转换矩阵)、上游转发、会话亲和测试
├── schema/proxy/v0,v1/     Proxy 历史迁移与当前 V1 baseline
├── schema/dashboard/v0,v1/ Dashboard 历史迁移与当前 V1 baseline
├── schema/transitions/     不兼容 Major 的离线影子库迁移工具
├── static/                 css + js(9 个模块)+ display_config.json
├── templates/index.html    SPA 页面(ECharts 5.5.0 CDN)
├── scripts/                启动/状态/模拟上游脚本(start-proxy.sh、start-dashboard.sh、status.sh、mock_upstream.py)
└── data/                   运行时 SQLite(proxy.db / dashboard.db)
```

`data/` 与 `proxy/build/` 在 .gitignore 中,不入库。

## 数据流

请求进来先走代理这一路。客户端密钥经 `Router::route` 找到上游账户,请求按客户端 URL 路径识别格式,与上游格式不同时经 IR 编解码转换(见 [format-conversion.md](format-conversion.md)),随后由 `UpstreamClient` 转发。每次请求的结果写进 `proxy.db` 的 `request_log`(计费 + TTFT/速度指标)与 `request_attempts`(每次上游尝试),流式响应实时透传。

看板写配置事务后 `config_state.generation` 自动递增；代理后台最多约 250ms 构建新的不可变 `RoutingSnapshot` 并原子替换，请求线程不查询 SQLite。用量按 日×账户×模型 批量 upsert 到 Dashboard V1 的 `daily_usage`，再以「云端权威」模型同步到 WebDAV。

Codex Agent 用量不再由独立进程采集。`token-dashboard` 内只有一个 importer worker:服务器启动立即执行一次,之后每 1800 秒执行一次;前端在每次页面加载时 POST `/api/proxy/agent-usage/import` 唤醒同一个 worker。所有触发都串行进入同一线程并写 `proxy.db/request_log`,避免定时任务与浏览器刷新并发扫描。

dashboard.db 是**纯存档**，V1 仅有 `accounts`、`daily_usage`、`monthly_recurring_costs`；没有价格表和重算能力。

## 关键设计决策

这些设计取舍直接决定系统的行为,列在这里便于后来者理解"为什么这么设计"。

**数据库是唯一事实来源。** V1 按 `requested_at` 选择 `pricing_rules → pricing_rates → pricing_slots → fx_rates`，把 `equivalent_cost` 与 `billed_usage_cost` 固化；周期费用来自 `billing_period_charges`。看板直接归档这两个口径与 recurring charge，改价不回溯。

**格式转换收敛到 IR。** 三种线格式的编解码统一到 `ir.h` 的中间表示:`parse(harness) → IR → serialize(upstream)`。同格式走透传快速路径,不经过 IR;客户端格式由请求 URL 路径识别,密钥不绑定格式(见 [format-conversion.md](format-conversion.md))。

**request_log 明细绝不上传云端。** 多机同步只同步聚合后的 `dashboard.db` 和配置表;导出进度用一个单值检查点(`sync_state.last_exported_log_id`)跟踪,拉取-导出-上传是一个完整事务,失败即回滚(见 [sync.md](sync.md))。

**schema 单一 DDL 来源。** 文件布局为 `schema/<库>/v<major>/<major>-<minor>_*.sql`；同 Major 自动升级，跨 Major 必须执行 transition。

**计费是合同驱动。** 路由核心不判断 `api/plan/agent`。兼容 API 的三种模板在写入时转换为 `billing_contracts`、`account_importers` 和 route rules；metered/recurring 行为由数据决定。
