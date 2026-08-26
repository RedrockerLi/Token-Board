# 总体架构

Token Board 由两个独立进程组成，共享同一份版本化数据库 schema。C++17 代理负责转发与计费，Python/Flask 看板负责配置管理与用量可视化。代理写 `token-board.db`，看板管理它的配置并把聚合结果写进 `dashboard.db`。

## 两个进程，一份 schema

```
                 ┌──────────────────────────────────────────┐
 AI 工具        │  token_proxy  (C++17, 端口 8800)          │
 (Claude Code)  │  认证 → 路由 → 并发闸门 → 转发/格式转换    │
     │  POST    │        │ 写 request_log / request_attempts│
     └─────────▶│        ▼                                 │
                 │    data/token-board.db                  │
                 └──────────────────────────────────────────┘

                 ┌──────────────────────────────────────────┐
                 │  server.py  (Python/Flask, 固定端口 5000) │
                 │  配置 CRUD │ 看板 API │ Agent 导入 │ 同步  │
                 │        │          │          │       │   │
                 │        ▼          ▼          ▼       ▼   │
                 │   data/token-board.db  data/dashboard.db WebDAV│
                 └──────────────────────────────────────────┘
```

schema 的单一来源是 `schema/` 目录下的版本化 SQL 与 transition，见
[database-migrations.md](database-migrations.md)。当前 V1 tip 是 Proxy V1.11、
Dashboard V1.4；baseline 位于各自 `v1/1-0_baseline.sql`，V0 文件按原内容归档
在各自 `v0/`。所有生产升级由 Python 的 `app.db.schema_upgrade` 负责；C++
只校验已准备好的 Proxy V1 数据库，不读取 SQL、不执行迁移。

两个库各管一件事：

- `data/token-board.db` 是代理运行库：上游账户、多密钥、本地密钥、模型定价、智能体订阅/软件配置、请求日志、每次上游尝试、计费台账、汇率与导入游标、WebDAV 配置。配置表可同步；请求日志、导入游标、账单物化结果、汇率和性能数据只在本机。
- `data/dashboard.db` 是可视化库：按日×模型×用户/软件聚合后的 Token 用量、请求数、费用，以及 plan/智能体订阅经济账。代理本身不写它，由看板导出流水线写入并同步。

## 目录结构

```
├── server.py               看板入口
├── start.sh                默认安装 token-dashboard；--all 再启动代理
├── app/                    Flask 应用包
│   ├── __init__.py         应用工厂与后台任务
│   ├── routes.py           数据看板蓝图(/、/api/*)
│   ├── routes/proxy/        代理、账单、智能体与同步 API
│   ├── db/proxy/            token-board.db 访问层
│   ├── db/dashboard/        dashboard.db 访问层
│   ├── db/schema_upgrade/   Python 数据库升级边界与原子发布引擎
│   ├── services/agent_usage/     Usage IR、通用游标导入器与逐 agent adapter
│   └── services/sync/       配置与 dashboard WebDAV 同步
├── proxy/                  C++17 代理 CMake 工程
├── schema/token-board/v0,v1/     Token Board 历史迁移与当前 V1
├── schema/dashboard/v0,v1/ Dashboard 历史迁移与当前 V1
├── schema/transitions/     V0 转换与 V1 配对数据 transition
├── static/                 CSS 与 SPA JavaScript
├── templates/index.html    SPA 页面
└── data/                   运行时 SQLite(token-board.db / dashboard.db)
```

`data/` 与 `proxy/build/` 在 `.gitignore` 中，不入库。

## 数据库升级边界

服务启动前，`start.sh`、`scripts/start-proxy.sh` 和 systemd 的
`ExecStartPre` 统一调用 `python3 -m app.db.schema_upgrade.cli`。这个边界负责
初始化数据库、执行同 Major SQL、识别 `schema/transitions/*/transition.json`、
在双 shadow 上执行跨库数据转换、校验并发布 manifest。普通 V1 transition 会在
两个数据库中写入相同的 `generation_id`；发布中断时，下一次 Python 启动会依据
manifest 和备份恢复原始文件。

Python 运行时 facade 和 Dashboard writer 只做只读的当前版本检查。C++ 代理打开
数据库时只验证 `schema_version` 与 `PRAGMA user_version` 一致，并要求当前 Proxy
运行时最低 schema（目前为 V1.11）；不满足时直接退出并提示先运行 Python
升级边界。C++ 的 `--schema-dir` 仅为旧启动器保留，不参与升级。

## 数据流

请求进来先走代理。客户端密钥经 `Router::route` 找到上游账户，请求按客户端 URL 路径识别格式，与上游格式不同时经 IR 编解码转换，随后由 `UpstreamClient` 转发。每次请求的结果写进 `token-board.db` 的 `request_log`（计费与性能指标）和 `request_attempts`（每次上游尝试），流式响应实时透传。

看板写配置事务后 `config_state.generation` 自动递增；代理后台最多约 250ms 构建新的不可变 `RoutingSnapshot` 并原子替换，请求线程不查询 SQLite。代理和智能体用量都按日×统一身份×模型批量导出到 Dashboard V1 的 `daily_usage`，订阅周期费用按绑定关系分摊到 `monthly_recurring_costs`，再以云端权威模型同步到 WebDAV。

智能体用量由看板内的 importer worker 采集。服务器启动立即执行一次，之后每 1800 秒执行一次；前端页面加载时可异步唤醒同一个 worker。worker 串行扫描已登记的软件日志并写入 `token-board.db/request_log`，因此不会因定时任务和浏览器刷新并发扫描而重复计量。`project` 与 `session_id` 仅作为本机数据库字段保存。

`dashboard.db` 是纯存档，V1 使用统一的 `accounts`、`daily_usage` 和 `monthly_recurring_costs`；`accounts.account_kind` 区分代理上游与智能体软件。它不保存价格表，也不承担重算配置价格的职责。

## 关键设计决策

**数据库是唯一事实来源。** V1 按 `requested_at` 选择 `pricing_rules → pricing_rates → pricing_slots → fx_rates`，把 `equivalent_cost` 与 `billed_usage_cost` 固化；周期费用来自 `billing_period_charges`。看板直接归档这两个口径与 recurring charge，改价不回溯。

**格式转换收敛到 IR。** 三种线格式的编解码统一到 `ir.h` 的中间表示：`parse → IR → serialize`。同格式走透传快速路径，不经过 IR。

**request_log 明细绝不上传云端。** 多机同步上传普通用户配置、本地代理密钥和智能体配置，但不上传上游 API Key 明文或 WebDAV 密码；另有单独导出的 `dashboard.db` 聚合存档。导出进度用 `sync_state.last_exported_log_id` 跟踪，拉取-导出-上传是一个完整事务，失败即回滚，见 [sync.md](sync.md)。

**schema 单一 DDL 来源。** 结构变化追加到
`schema/<库>/v<major>/<major>-<minor>_*.sql`，由 Python 在事务中执行；数据转换
和跨库协调追加到 `schema/transitions/<transition-id>/`，由统一升级边界在 shadow
上执行。业务 facade、reconcile 和 C++ 请求路径不承担迁移职责。

**计费是合同驱动。** 路由核心只处理 `api/plan` 上游。智能体订阅使用独立的 `agent_subscriptions`、实例、价格历史和周期费用表；软件用量使用独立的 `agent_software`，通过绑定关系把实际订阅费分摊到统一 dashboard 身份。理论消费来自软件用量，未绑定时实际消费为 0。
