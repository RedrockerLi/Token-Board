# 开发指南

面向想在源码上改动的开发者。代码分三块:C++17 代理(`proxy/`)、Python/Flask 看板(`app/` + `server.py`)、前端 SPA(`static/` + `templates/`)。总体结构见 [architecture.md](architecture.md)。

## 构建代理

依赖分两类。系统库:OpenSSL、Threads(CMake `find_package`)。vendored 在 `proxy/third_party/`:`httplib.h`(HTTP 客户端/服务端)、`json.hpp`、`sqlite3.c/h`,无需额外下载。`sqlite3.c` 以 C 编译,开启 `SQLITE_THREADSAFE=1`、FTS5、JSON1。

```bash
cd proxy
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
cd ..
PYTHONPATH=. python3 -m app.db.schema_upgrade.cli \
  --token-board-db data/token-board.db \
  --dashboard-db data/dashboard.db \
  --schema-dir schema \
  --timezone Asia/Shanghai
cd proxy
./build/token_proxy --db ../data/token-board.db --port 8800
```

数据库升级只在 `bash start.sh --all` 中执行。代理的手动启动和
`token-maintenance` 都只做 current schema 验证；`--schema-dir` 仅为旧启动命令
保留，C++ 不读取 SQL、不创建数据库、不执行升级，直接启动旧数据库会快速失败。

Release 构建额外加 `-O3 -march=native -flto`。构建产物有两个:`token_proxy`(完整代理)和 `format_conv_test`(codec 自测,见下)。

## codec 自测

`format_conv_test` 只依赖 codec 层,不碰 sqlite / http,适合快速验证格式转换。

```bash
./build/format_conv_test --self-test
```

内嵌往返矩阵覆盖:OpenAI→Anthropic 流式工具调用、reasoning→thinking 块、Anthropic tool_result→OpenAI tool 消息、`[1m]` 后缀剥离、tool_choice 归一化等。也可以单发转换:

```bash
./build/format_conv_test --request --from openai --to anthropic < body.json
./build/format_conv_test --response --from anthropic --to openai < resp.json
./build/format_conv_test --stream --from openai --to responses < body.json
```

## 模拟上游

`scripts/mock_upstream.py` 起一个模拟 opencode.ai "Console Go" 的服务,支持全部三种格式,用于无真实上游时调试代理。请求会写进 `/tmp/mock_upstream.log`。

```bash
python3 scripts/mock_upstream.py --port 9100
```

它复刻了真实上游的严格校验:chat completions 拒绝 `role: developer` 消息、拒绝非 `function` 类型的工具、拒绝 `parameters` 不是 `{"type":"object"}` 的工具。请求体里可以塞测试钩子:`mock_format`(指定响应格式)、`mock_status`(指定返回状态码)、`mock_delay`、`mock_tool`、`mock_simple_stream`(无 reasoning 的纯流)。

## 脚本

| 脚本 | 用途 |
|------|------|
| `bash start.sh` | 快速前台启动固定端口 5000 的看板；不迁移数据库、不重启后台服务；启动后异步拉取云端配置；`--no-browser` 不开浏览器 |
| `bash start.sh --all` | 编译代理、升级两个数据库、安装/重启 `token-proxy` 与 `token-maintenance`，然后启动前台看板 |
| `bash scripts/start-proxy.sh` | 代理:无参前台调试;`--daemon` 后台;`--install`/`--uninstall` 管理 systemd 用户服务 |
| `bash scripts/start-dashboard.sh [--no-browser]` | 兼容入口，转发到统一的 `start.sh`（不再另起第二个看板进程） |
| `bash scripts/status.sh` | 状态检查:代理二进制、proxy/maintenance systemd、8800 健康、看板端口、数据库行数 |

## 数据写入

用量数据来自代理转发和已注册智能体软件:消费报告页点「导出数据」触发 `sync_dashboard`(见 [sync.md](sync.md)),把 `request_log`
按 日×账户/软件(id)×模型 增量聚合写进 `dashboard.db`(纯存档,写时固化的费用直接入库,改价不回溯)。
Agent 用量导入参考 `ref/vibe-usage` 的各来源 parser：每个 adapter 先把 native 数据归一为 `UsageEvent`，再由 `token-maintenance` 的通用 importer 负责游标、幂等和写入 `request_log`；仪表板打开时通过本地 socket 异步唤醒导入。`project`、`session_id` 只写本机 proxy 请求日志,不作为 API 字段。

## 前端

SPA 是 `templates/index.html` + `static/js/` 下的模块,hash 路由,`app.js` 维护页面注册表。ECharts 5.5.0 走 CDN。

- `utils.js`:UTC 时间戳到浏览器当地时间的显示、当地日期筛选、UTC/当地分钟换算、`fmtNum`、`esc`,最先加载。
- `api.js`:fetch 封装 + 各 API 包装。
- `charts.js`:ECharts 渲染层(SVG)。
- `dashboard.js`:用量仪表板(弃用模型过滤、模型别名分组)。弃用判定:全局(所有用户、全历史)token 占比 <1% **且**最新有数据月份内 0 用量(含调用失败 0 token)的模型/别名组;该集合计算一次后固定,不随月份/用户选择变化,「刷新数据」时重算。
- `proxy_manager.js`:账户/聚合/密钥/定价管理页,含模型定价拖放排序、按浏览器当地时间输入的峰谷时段编辑器;档位以 UTC+0 分钟存储。
- `proxy_billing.js`:消费报告 + 请求日志页。
- `agent_manager.js`:智能体订阅与软件来源管理页。
- `proxy_perf.js`:性能监控页(15s 自动刷新)。
- `proxy_settings.js`:代理设置页(超时三档配置、WebDAV 同步设置)。

前端显示过滤配置在 `static/display_config.json`(后端不读),当前支持 `model_aliases` 把多个模型合并为一个展示单位,如:

```json
{ "model_aliases": [ { "name": "Minimax-M2.7", "models": ["minimax-m27", "minimax-m27-eic"] } ] }
```

## 约定与注意事项

- **schema 只通过升级边界改。** 纯结构变化追加
  `schema/<库>/v<major>/<major>-<minor>_*.sql`；需要数据转换或跨库协调时新增
  `schema/transitions/<transition-id>/transition.json` 和 `transition.py`，并在
  descriptor 中声明 scope、current/prepare/target 版本 route。entrypoint 实现
  `apply(context)` 与 `verify(context)`。两者都由 Python `app.db.schema_upgrade`
  在 shadow/事务中执行，业务 facade、
  reconcile 和 C++ 请求路径不补迁移逻辑。规则见
  [database-migrations.md](database-migrations.md)。
- **C++ schema 版本必须同步。** 每次 Token Board V1 Minor tip 增加时，同时更新
  `proxy/src/store/database_lifecycle.cpp` 的
  `kRequiredRuntimeSchemaMinor`，重新编译代理并运行
  `PYTHONPATH=. python3 proxy/tests/schema_version_test.py schema .`。
  Python 已升级数据库而 C++ 仍要求旧 Minor 时，代理会拒绝启动，表现为
  `start.sh --all` 的 `proxy health check failed`。
- **时间存 UTC,显示浏览器当地时间。** 库内 `datetime('now')` 和请求时间存 UTC;前端通过 `Date` 的本机时区显示时间戳,并把当地日期筛选转换为 UTC 范围。峰谷档位边界按 UTC+0 分钟存储,电脑时区变化只改变显示。订阅起始日是 UTC 日期锚点,前端使用可逆的当地日期映射,避免换时区后原样保存改动账期。
- **计费写时固化。** 改价、换序不回溯 `request_log.api_cost`,见 [billing-pricing.md](billing-pricing.md)。
- **request_log 明细不上传。** 普通用户配置和本地代理密钥上传到配置云端文件；上游 API Key 明文与 WebDAV 密码只保存在本机。用量和账单分别使用 `sync_state.last_exported_log_id` 与 `sync_state.last_exported_billing_event_id`，拉取-导出-上传是完整事务(失败回滚),见 [sync.md](sync.md)。
- **Python 无依赖清单。** 只有 flask、requests 两个第三方包,启动脚本在缺 flask 时自动 pip 安装。
- **数据库路径与 schema 目录的推导约定**:`data/token-board.db` /
  `data/dashboard.db` → `<仓库>/schema/`；Python coordinator 再选择
  `token-board|dashboard/v<major>`。C++ 只校验已准备好的 Proxy V1 数据库。
