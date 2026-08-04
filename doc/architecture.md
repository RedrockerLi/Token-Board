# 总体架构

Token Board 由两个独立进程组成,共享同一份版本化数据库 schema。C++17 代理负责转发与计费,Python/Flask 看板负责配置管理与用量可视化。两者都直接读写各自的 SQLite 库,同一个库里也可能被两个进程访问——代理写 `proxy.db`,看板管理 `proxy.db` 的配置并把聚合结果写进 `dashboard.db`。

## 两个进程,一份 schema

```
                 ┌──────────────────────────────────────────┐
 AI 工具        │  token_proxy  (C++17, 端口 8800)          │
 (Claude Code)  │  认证 → 路由 → 并发闸门 → 转发/格式转换    │
     │  POST    │        │ 写 request_log / perf_events     │
     └─────────▶│        ▼                                 │
                 │    data/proxy.db                        │
                 └──────────────────────────────────────────┘

                 ┌──────────────────────────────────────────┐
                 │  server.py  (Python/Flask, 端口 5000+)   │
                 │  配置管理 CRUD │ 数据看板 API │ 导出/同步   │
                 │        │            │            │       │
                 │        ▼            ▼            ▼       │
                 │   data/proxy.db  data/dashboard.db  WebDAV│
                 └──────────────────────────────────────────┘
```

schema 的单一来源是 `schema/` 目录下的版本化迁移文件,见 [database-migrations.md](database-migrations.md)。`schema/proxy/` 定义 `proxy.db` 的表、索引、触发器,`schema/dashboard/` 定义 `dashboard.db` 的。C++ 侧 `proxy/src/db.cpp::run_migrations` 与 Python 侧 `app/migrations.py` 实现同一套 flock + `user_version` 协议,任一进程先启动都安全。

两个库各管一件事:

- `data/proxy.db` 是代理的运行库:上游账户、本地密钥、模型定价、峰谷档位、请求日志、性能事件、在途请求、WebDAV 配置。
- `data/dashboard.db` 是可视化库:按 日×模型×用户 聚合后的 Token 用量、请求数、费用,以及 plan 经济账。代理本身不写它,由看板的导出流水线写入。

## 目录结构

```
├── server.py               看板入口(解析 --port / --proxy-db,调 create_app)
├── start.sh                一键启动:代理(systemd)+ CSV 导入 + 看板
├── app/                    Flask 应用包
│   ├── __init__.py         create_app() 应用工厂,按需挂代理管理蓝图
│   ├── routes.py           数据看板蓝图(/、/api/*)
│   ├── proxy_routes.py     代理管理蓝图(/api/proxy/*)
│   ├── proxy_db.py         proxy.db 的 Python 访问层(CRUD/计费/导出/性能)
│   ├── dashboard_db.py     dashboard.db 访问层(增量 upsert / 载入 IR)
│   ├── data_loader.py      DataStore 单例:读 dashboard.db 存档
│   ├── sync.py             WebDAV 同步(配置上传/下载、dashboard 云端权威事务)
│   ├── migrations.py       Python 侧迁移 runner
│   ├── cost_allocator.py   按 Token 占比分摊成本
│   └── ir.py               平台无关 IR 数据类(TokenUsage/RequestUsage/CostEntry)
├── proxy/                  C++17 代理(CMake 工程)
│   ├── CMakeLists.txt      构建(OpenSSL + Threads,vendored sqlite3/httplib/json)
│   └── src/
│       ├── main.cpp        入口、线程池扩容、周期清理、优雅停机
│       ├── config.cpp      CLI 参数
│       ├── db.cpp          SQLite 访问、迁移、预编译语句
│       ├── router.cpp      本地密钥 → 上游账户路由(60s 缓存)
│       ├── proxy_server.cpp 三个 chat 端点共用管线、候选回退、流式处理
│       ├── upstream_client.cpp 上游 HTTP 转发(10s/100s/30s 超时)
│       ├── usage_tracker.cpp 三种格式的 usage 解析,写 request_log
│       ├── account_gate.h  每账户并发闸门 + plan 5h 冷却
│       ├── semaphore_pool.h 信号量线程池(8→2048 动态扩容)
│       ├── ir.*            聊天请求/响应/流的中间表示
│       ├── codec.*         格式编解码注册表
│       ├── format_openai/anthropic/responses.* 三种线格式 codec
│       ├── format_common.* 缓存命中读取、tool_choice 归一化、SSE 分帧
│       ├── think_filter.*  <think> 内容抽取(非流式 + 流式状态机)
│       └── format_conv_test.cpp  codec 自测二进制
├── schema/proxy/           迁移:0001_initial、0002_pricing_slots_frozen_cost
├── schema/dashboard/       迁移:0001_initial、0002_pricing_slots_frozen_cost
├── static/                 css + js(7 个模块)+ display_config.json
├── templates/index.html    SPA 页面(ECharts 5.5.0 CDN)
├── scripts/                启动/状态/模拟上游脚本
└── data/                   运行时 SQLite(proxy.db / dashboard.db)+ CSV 导入目录
```

`data/` 与 `proxy/build/` 在 .gitignore 中,不入库。

## 数据流

请求进来先走代理这一路。客户端密钥经 `Router::route` 找到上游账户,请求按客户端 URL 路径识别格式,与上游格式不同时经 IR 编解码转换(见 [format-conversion.md](format-conversion.md)),随后由 `UpstreamClient` 转发。每次请求的结果写进 `proxy.db` 的 `request_log`(计费)与 `perf_events`(性能),流式响应实时透传。

看板这一路负责把 `proxy.db` 变成可读的图。配置(账户、密钥、定价)直接由看板写 `proxy.db`,代理下个请求自动读到新配置,路由缓存最长 60 秒。用量数据不走实时:看板导出事务把 `request_log` 按 日×账户名×模型 增量聚合写进 `dashboard.db` 存档,再以「云端权威」模型同步到 WebDAV(见 [sync.md](sync.md))。`DataStore` 启动时读 `dashboard.db`,数据看板页面全部基于它渲染。

dashboard.db 是**纯存档**:只存用量与总价,无价格表、无重算能力;`cost_entry` 一旦写入不再受改价影响。

## 关键设计决策

这些设计取舍直接决定系统的行为,列在这里便于后来者理解"为什么这么设计"。

**数据库是唯一事实来源。** 请求插入 `request_log` 那一刻,按当时的定价与峰谷档位算好 `cost` 固化下来,之后改价不回溯(见 [billing-pricing.md](billing-pricing.md))。看板的 `cost_entry` 在导出时按 `request_log.cost` 聚合固化,不再由 `model_pricing` 触发器重算。

**格式转换收敛到 IR。** 三种线格式的编解码统一到 `ir.h` 的中间表示:`parse(harness) → IR → serialize(upstream)`。同格式走透传快速路径,不经过 IR;客户端格式由请求 URL 路径识别,密钥不绑定格式(见 [format-conversion.md](format-conversion.md))。

**request_log 明细绝不上传云端。** 多机同步只同步聚合后的 `dashboard.db` 和配置表;导出进度用一个单值检查点(`sync_state.last_exported_log_id`)跟踪,拉取-导出-上传是一个完整事务,失败即回滚(见 [sync.md](sync.md))。

**schema 单一 DDL 来源。** 两库的全部 DDL 收敛到 `schema/<库>/NNNN_*.sql`,C++ 与 Python 共用同一份文件与同一套迁移协议,任何进程都能初始化或升级库(见 [database-migrations.md](database-migrations.md))。

**plan 账户单独建模。** 订阅套餐调用不收费,真实成本记 0;同时按 api 口径算出 `virtual_cost`,用来衡量套餐划不划算(见 [billing-pricing.md](billing-pricing.md))。
