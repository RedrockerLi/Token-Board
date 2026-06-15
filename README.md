# Token Board — AI API 代理 + 用量可视化

一个集 **高性能 API 代理** 与 **用量数据可视化** 于一体的工具。代理负责将 AI 工具的请求按不同 API Key 路由到不同的上游账户，并自动记录每次请求的 Token 消耗与费用；仪表板提供直观的图表展示。

## 快速开始

```bash
# 一键启动（代理后台 + 仪表板前台）
bash start.sh

# 仅启动仪表板
bash scripts/start-dashboard.sh

# 代理开机自启
bash scripts/start-proxy.sh --install
```

浏览器访问仪表板，通过左侧导航栏切换功能页面。

---

## 核心功能：API 代理

### 工作原理

```
AI 工具                       代理                      上游 API
(Claude Code 等)          localhost:8800              (OpenAI 兼容)
     │                         │                           │
     │  POST /v1/chat/         │                           │
     │  completions            │                           │
     │  Authorization:         │                           │
     │  Bearer <本地密钥>       │                           │
     ├────────────────────────▶│                           │
     │                         │  查 SQLite 找到对         │
     │                         │  应的上游账户              │
     │                         │                           │
     │                         │  POST /v1/chat/           │
     │                         │  completions              │
     │                         │  Authorization:           │
     │                         │  Bearer <上游密钥>         │
     │                         ├──────────────────────────▶│
     │                         │                           │
     │                         │      SSE / JSON 响应      │
     │                         │◀──────────────────────────┤
     │                         │                           │
     │                         │  记录 usage → SQLite      │
     │     响应（实时转发）      │                           │
     │◀────────────────────────┤                           │
```

### 配置 AI 工具

代理只转发 `/v1/chat/completions`，要求上游 API **OpenAI 兼容**。在 AI 工具中设置：

```
# 关键：Base URL 必须包含 /v1 路径
OPENAI_BASE_URL = http://localhost:8800/v1
OPENAI_API_KEY  = <在仪表板生成的本地密钥>
```

> **注意**：Base URL 的路径是 `/v1`，不是 `/v1/chat/completions`。OpenAI SDK 会自动拼接 `/chat/completions`。

### 配置流程

1. 打开仪表板 → 侧边栏 **代理管理 → 上游账户** → 添加账户（填写上游 API 的 Base URL 和 Key）
2. **代理管理 → 本地密钥** → 生成密钥（选择关联的账户）
3. 将生成的密钥（格式 `tb-xxxxxxxx...`）填入 AI 工具的 `API_KEY`
4. 发送请求后，用量自动记录到 **费用报告** 和 **请求日志**

### 多账户路由

不同本地密钥可以路由到不同上游账户，适合：
- 团队多人共用一台代理服务器，各自使用独立的 API Key
- 按项目/用途分配不同模型配额
- 多平台统一接入（只要是 OpenAI 兼容 API）

---

## 数据看板

### 用量仪表板

展示从 CSV 文件导入的 API 用量数据，支持多平台、按月查看、按用户筛选。

数据来源：
- **CSV 导入**：支持 DeepSeek、Mimo、BoardProxy 平台
- **代理导出**：在费用报告页选择月份 → 点击「导出数据」，数据写入 `data/boardproxy/`，自动显示在仪表板中

### 费用报告

显示代理转发的请求统计：
- **总 Token / 总请求数 / 总费用** 概览卡片
- **按月费用趋势** 柱状图
- **账户费用明细** 表格
- **导出数据**：按月份将代理用量导出为 CSV，供仪表板展示
- **同步数据**：通过 WebDAV 在多台电脑间同步用量记录

### WebDAV 同步

多台电脑使用代理时，可通过 WebDAV 同步用量数据：

1. 费用报告页 → 点击 **⚙** → 填写 WebDAV 服务器信息 → **测试连接** → **保存配置**
2. 点击 **同步数据** 执行同步

同步规则：
- **本地数据库永远完整**，不做裁剪
- 云端仅保存 **30 天内** 的 `request_log` + `model_pricing`
- **上游账户、本地密钥、WebDAV 凭证绝不泄露到云端**

---

## 项目结构

```
Token_Board/
├── proxy/                    # C++ 高性能代理
│   ├── src/                  #   代理源码
│   │   ├── main.cpp          #     入口
│   │   ├── proxy_server.cpp  #     HTTP 服务 + 路由
│   │   ├── upstream_client.cpp #   上游转发
│   │   ├── router.cpp        #     密钥路由
│   │   ├── usage_tracker.cpp #     用量记录
│   │   ├── db.cpp            #     SQLite 访问
│   │   ├── config.cpp        #     CLI 参数
│   │   └── model_pricing.cpp #     费用计算
│   ├── third_party/          #   第三方头文件
│   └── CMakeLists.txt
├── app/                      # Python 仪表板后端
│   ├── ir.py                 #   中间表示 (IR) 数据模型
│   ├── adapters/             #   平台适配器（CSV → IR）
│   │   ├── deepseek.py
│   │   ├── mimo.py
│   │   └── boardproxy.py
│   ├── data_loader.py        #   数据扫描和加载
│   ├── cost_allocator.py     #   按比例分摊费用
│   ├── routes.py             #   仪表板 API
│   ├── proxy_routes.py       #   代理管理 API
│   ├── proxy_db.py           #   代理数据库访问
│   ├── sync.py               #   WebDAV 云端同步
│   └── config.py
├── static/                   # 前端
│   ├── js/
│   │   ├── app.js            #   SPA 路由 + 侧边栏
│   │   ├── api.js            #   HTTP 通信
│   │   ├── charts.js         #   ECharts 渲染
│   │   ├── dashboard.js      #   仪表板页面
│   │   ├── proxy_billing.js  #   费用报告 + 日志
│   │   └── proxy_manager.js  #   账户/密钥/定价管理
│   └── css/
│       └── dashboard.css
├── templates/
│   └── index.html
├── data/                     # CSV 数据 + 代理数据库
│   ├── deepseek/
│   ├── mimo/
│   ├── boardproxy/           #   代理导出 CSV
│   └── proxy.db              #   代理 SQLite 数据库
├── server.py                 # 仪表板入口
└── start.sh                  # 一键启动
```

## API 端点

### 仪表板 API（数据可视化）

| 端点 | 说明 |
|------|------|
| `/api/summary` | 跨月份聚合统计 |
| `/api/monthly` | 按月聚合统计 |
| `/api/daily` | 指定月份的每日明细 |
| `/api/models` | 模型列表及所属平台 |
| `/api/token_types` | Token 类型分布 |
| `/api/api_key_names` | 用户列表 |
| `/api/refresh` | 重新扫描数据目录 |

查询参数：`api_key_name`、`model`、`platform`、`year`、`month`。

### 代理管理 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/proxy/stats` | GET | 代理统计概览 |
| `/api/proxy/accounts` | GET/POST | 上游账户 CRUD |
| `/api/proxy/accounts/<id>` | PUT/DELETE | 更新/停用账户 |
| `/api/proxy/keys` | GET/POST | 本地密钥 CRUD |
| `/api/proxy/keys/<id>` | PUT/DELETE | 更新/停用密钥 |
| `/api/proxy/pricing` | GET/POST | 模型定价 CRUD |
| `/api/proxy/pricing/<id>` | PUT/DELETE | 更新/删除定价 |
| `/api/proxy/billing` | GET | 聚合费用数据 |
| `/api/proxy/billing/monthly-trend` | GET | 按月费用趋势 |
| `/api/proxy/billing/months` | GET | 可用月份列表 |
| `/api/proxy/billing/by-account` | GET | 按账户费用明细 |
| `/api/proxy/logs` | GET | 分页请求日志 |
| `/api/proxy/export` | POST | 导出月份数据到 CSV |
| `/api/proxy/sync` | POST | 触发 WebDAV 同步 |
| `/api/proxy/sync/config` | GET/PUT | WebDAV 配置 |
| `/api/proxy/sync/test` | POST | 测试 WebDAV 连接 |

### 代理转发端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 代理转发（OpenAI 兼容） |
| `/health` | GET | 代理健康检查 |

---

## 编译代理

代理使用 C++17 + CMake：

```bash
cd proxy
bash setup_deps.sh          # 下载第三方依赖（首次）
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
./build/token_proxy --db ../data/proxy.db --port 8800
```

命令行参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--db` | `data/proxy.db` | SQLite 数据库路径 |
| `--port` | `8800` | 监听端口 |
| `--host` | `0.0.0.0` | 绑定地址 |
| `--log-level` | `info` | 日志级别 |
