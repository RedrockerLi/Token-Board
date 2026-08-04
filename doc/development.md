# 开发指南

面向想在源码上改动的开发者。代码分三块:C++17 代理(`proxy/`)、Python/Flask 看板(`app/` + `server.py`)、前端 SPA(`static/` + `templates/`)。总体结构见 [architecture.md](architecture.md)。

## 构建代理

依赖分两类。系统库:OpenSSL、Threads(CMake `find_package`)。vendored 在 `proxy/third_party/`:`httplib.h`(HTTP 客户端/服务端)、`json.hpp`、`sqlite3.c/h`,无需额外下载。`sqlite3.c` 以 C 编译,开启 `SQLITE_THREADSAFE=1`、FTS5、JSON1。

```bash
cd proxy
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
./build/token_proxy --db ../data/proxy.db --schema-dir ../schema/proxy --port 8800
```

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
| `bash start.sh` | 一键:CSV 导入 + 找空闲端口 + 起看板;`--all` 加编译并起代理(systemd 开机自启);`--no-browser` 不开浏览器 |
| `bash scripts/start-proxy.sh` | 代理:无参前台调试;`--daemon` 后台;`--install`/`--uninstall` 管理 systemd 用户服务 |
| `bash scripts/start-dashboard.sh [--no-browser]` | 单独起看板(自动 `pip install flask` 兜底) |
| `bash scripts/status.sh` | 状态检查:代理二进制、systemd、8800 健康、看板进程与端口、数据库行数 |

## 数据写入

用量数据来自代理转发:消费报告页点「导出数据」触发 `sync_dashboard`(见 [sync.md](sync.md)),把 `request_log`
按 日×账户名×模型 增量聚合写进 `dashboard.db`(纯存档,写时固化的 `cost` 直接入库,改价不回溯)。
CSV 导入已弃用,相关代码与适配器已移除。

## 前端

SPA 是 `templates/index.html` + `static/js/` 下的模块,hash 路由,`app.js` 维护页面注册表。ECharts 5.5.0 走 CDN。

- `utils.js`:`fmtUtc8`(UTC→UTC+8 显示)、`fmtNum`、`esc`,最先加载。
- `api.js`:fetch 封装 + 各 API 包装。
- `charts.js`:ECharts 渲染层(SVG)。
- `dashboard.js`:用量仪表板(弃用模型过滤、模型别名分组)。弃用判定:全局(所有用户、全历史)token 占比 <1% **且**最新有数据月份内 0 用量(含调用失败 0 token)的模型/别名组;该集合计算一次后固定,不随月份/用户选择变化,「刷新数据」时重算。
- `proxy_manager.js`:账户/聚合/密钥/定价管理页,含峰谷时段编辑器(UTC+8 输入,`minutes8to0` / `minutes0to8` 换算成 UTC+0 分钟存储)。
- `proxy_billing.js`:消费报告 + 请求日志页。
- `proxy_perf.js`:性能监控页(15s 自动刷新)。

前端显示过滤配置在 `static/display_config.json`(后端不读),当前支持 `model_aliases` 把多个模型合并为一个展示单位,如:

```json
{ "model_aliases": [ { "name": "Minimax-M2.7", "models": ["minimax-m27", "minimax-m27-eic"] } ] }
```

## 约定与注意事项

- **schema 只通过迁移改。** 任何表/索引/触发器变更都要追加 `schema/<库>/NNNN_*.sql`,规则见 [database-migrations.md](database-migrations.md)。`.sql` 文件内禁止写 `BEGIN`/`COMMIT`/`PRAGMA user_version`。
- **时间存 UTC,显示 UTC+8。** 库内 `datetime('now')` 存 UTC;看板所有时间显示统一经 `fmtUtc8` 转 UTC+8;峰谷档位边界按 UTC+0 分钟存。
- **计费写时固化。** 改价、换序不回溯 `request_log.cost`,见 [billing-pricing.md](billing-pricing.md)。
- **request_log 明细不上传。** 同步进度用 `sync_state.last_exported_log_id` 单值检查点,拉取-导出-上传是完整事务(失败回滚),见 [sync.md](sync.md)。
- **Python 无依赖清单。** 只有 flask、requests 两个第三方包,启动脚本在缺 flask 时自动 pip 安装。
- **数据库路径与 schema 目录的推导约定**:`data/proxy.db` → `<仓库>/schema/proxy`,`data/dashboard.db` → `<仓库>/schema/dashboard`(`app/migrations.py::schema_dir_for`,C++ 侧同理)。
