# Token Board — AI API 代理 + 用量可视化

一个集 **高性能 API 代理** 与 **用量数据可视化** 于一体的工具。代理负责将 AI 工具的请求按不同 API Key 路由到不同的上游账户，并自动记录每次请求的 Token 消耗与费用；仪表板提供直观的图表展示。

## 快速开始

```bash
# 仅启动仪表板（默认）
bash start.sh

# 启动全部（代理 + 仪表板，代理设为开机自启）
bash start.sh --all

# 不自动打开浏览器
bash start.sh --no-browser
```

浏览器访问仪表板，通过左侧导航栏切换功能页面。

> 代理开机自启基于 systemd 用户服务。非 systemd 环境（macOS 等）请用 `bash scripts/start-proxy.sh --daemon` 后台启动。

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

代理支持三种 API 格式：**OpenAI 兼容**（`/v1/chat/completions`）、**OpenAI Responses**（`/v1/responses`）和 **Anthropic 兼容**（`/v1/messages`）。上游账户与 AI 工具（harness）均可配置为任意格式，代理在中间自动完成请求/响应格式转换（含流式与用量解析）。

### 格式配置

- **上游账户**（上游服务端格式）：添加/编辑账户时选择 `API 格式`（OpenAI / OpenAI Responses / Anthropic），并可自定义 `上游路径`（默认按格式自动推导，如 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`）与 `认证方式`（Bearer 或 x-api-key + anthropic-version）。
- **本地密钥**：一把密钥同时支持全部三种客户端格式。客户端格式由请求的 URL 路径自动识别：`/v1/chat/completions` → OpenAI，`/v1/responses` → OpenAI Responses，`/v1/messages` → Anthropic。代理自动将客户端请求转换为上游账户的格式；与上游格式一致时直接透传。

同一把密钥绑定 openai 账户时，Claude Code 也能通过它（客户端访问 `/v1/messages`，代理自动转换为 OpenAI 请求再转发）。

### OpenAI 兼容工具

```
OPENAI_BASE_URL = http://localhost:8800/v1
OPENAI_API_KEY  = <在仪表板生成的本地密钥>
```

> **注意**：Base URL 的路径是 `/v1`，不是 `/v1/chat/completions`。OpenAI SDK 会自动拼接 `/chat/completions`。

### Anthropic 兼容工具（Claude Code 等）

```bash
export ANTHROPIC_BASE_URL=http://localhost:8800/v1
export ANTHROPIC_AUTH_TOKEN=<在仪表板生成的本地密钥>
```

### 配置流程

1. 打开仪表板 → 侧边栏 **代理管理 → 上游账户** → 添加账户（填写上游 API 的 Base URL 和 Key）
2. **代理管理 → 本地密钥** → 生成密钥（选择关联的账户）
3. 将生成的密钥（格式 `tb-xxxxxxxx...`）填入 AI 工具的 `API_KEY`
4. 发送请求后，用量自动记录到 **费用报告** 和 **请求日志**

### 多账户路由

不同本地密钥可以路由到不同上游账户，适合：
- 团队多人共用一台代理服务器，各自使用独立的 API Key
- 按项目/用途分配不同模型配额
- 多平台统一接入（支持 OpenAI 兼容 + Anthropic 兼容 API）

### 模型映射

支持对下游请求中的模型名进行动态替换，使用 **shell glob 通配符**（`*`、`?`）：

1. 侧边栏 **代理管理 → 模型映射** → 创建模板，设置正则映射规则（如 `*` → `minimax-m27`）
2. **本地密钥** 编辑时选择映射模板
3. 请求中 `model: "qwen3.5"` 自动替换为 `model: "minimax-m27"` 再转发

映射按顺序匹配，命中即停。模板可绑定到不同密钥复用。

---

## 数据看板

### 用量仪表板

展示从 CSV 文件导入的 API 用量数据，支持多平台、按月查看、按用户筛选。

数据来源：
- **CSV 导入**：支持 DeepSeek、Mimo、BoardProxy 平台
- **代理导出**：在费用报告页点击「导出数据」，未同步的用量自动聚合写入仪表板数据库，实时显示

### 费用报告

显示代理转发的请求统计：
- **总 Token / 总请求数 / 总费用** 概览卡片
- **按月费用趋势** 柱状图
- **账户费用明细** 表格
- **导出数据**：将未同步的用量聚合写入仪表板数据库，并自动完成 WebDAV 云端同步（拉取 → 合并 → 上传）
- **同步设置**：点击 **⚙** 配置 WebDAV 服务器，实现在多台电脑间同步配置与用量数据

### WebDAV 同步

多台电脑共用代理时，可通过 WebDAV 同步配置与用量数据：

1. 费用报告页 → 点击 **⚙** → 填写 WebDAV 服务器信息 → **测试连接** → **保存配置**
2. 点击 **导出数据** 执行完整同步（拉取云端最新 → 合并到本地 → 导出本地新用量 → 上传云端）

同步采用**追加模式**：每次上传生成带时间戳的文件（如 `dashboard_sync_20260731_143025.db`），云端旧文件保留不删除；拉取时自动选择时间戳最新的文件。配置修改后约 3 秒自动上传，仪表板启动时自动从云端拉取配置合并到本地（本地已有配置优先）。

同步内容：
- **配置**：上游账户、本地密钥、模型映射模板、模型定价，跨机器自动同步
- **用量数据**：按 日期 + 账户 + 模型 聚合后的 Token 用量与费用

同步规则：
- `request_log` 明细**仅存本地**，不直接上传；通过三态标记（未导出 / 已导出待上传 / 已确认上传）只导出聚合后的用量，不重不漏，云端上传成功后才确认，失败自动重试
- 本地 `request_log` 只清理**已确认上传**的旧记录（最多保留 1 万条），未导出 / 待上传的记录绝不删除
- 云端保留全部历史文件，不做裁剪
- **绝不上传**：`request_log` 明细、性能指标（perf_events）、在途请求、WebDAV 账号密码

---

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
| `/api/proxy/logs` | GET | 分页请求日志 |
| `/api/proxy/export` | POST | 导出未同步用量到仪表板 + WebDAV 云端同步 |
| `/api/proxy/sync/config` | GET/PUT | WebDAV 配置 |
| `/api/proxy/sync/test` | POST | 测试 WebDAV 连接 |

### 代理转发端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容代理转发 |
| `/v1/responses` | POST | OpenAI Responses 代理转发 |
| `/v1/messages` | POST | Anthropic 兼容代理转发 |
| `/v1/embeddings` | POST | 嵌入向量代理转发 |
| `/v1/models` | GET | 模型列表代理 |
| `/health` | GET | 代理健康检查 |

> 三个 chat 端点共享同一条管线：客户端格式由请求 URL 路径自动识别（`/v1/chat/completions` → OpenAI、`/v1/responses` → Responses、`/v1/messages` → Anthropic），与上游账户格式不同时由代理自动转换。

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
