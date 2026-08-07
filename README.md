# Token Board — AI API 代理 + 用量可视化

一个集**高性能 API 代理**与**用量数据可视化**于一体的工具。代理把 AI 工具的请求按不同 API Key 路由到不同的上游账户,自动完成客户端与上游之间的格式转换,并记录每次请求的 Token 消耗与费用;仪表板把这些数据画成图表,还能把用量和配置同步到 WebDAV,多台电脑共用一套代理配置。

核心特性:

- 三种线格式互相转换:OpenAI 兼容(`/v1/chat/completions`)、OpenAI Responses(`/v1/responses`)、Anthropic 兼容(`/v1/messages`)。Claude Code、codex、OpenAI SDK 等工具可以同时接进来,客户端格式由请求 URL 自动识别
- 多账户路由:一个本地密钥绑定一个上游账户;聚合账户把多个上游合并成一个模型列表,同一模型可配置多个账户自动回退
- 峰谷定价:按时段给模型定价配倍率,成本在请求写入时按当时价格固化,改价不回溯
- 用量看板:模型趋势、Token 类型分布、按用户分摊费用;存档只存用量与总价,改价不回溯
- 性能监控:成功率、延迟分布(P50/P95/P99)、输出速度分布、吞吐、实时并发
- WebDAV 同步:配置与聚合用量跨机器同步,明细数据不上传

技术细节(架构、数据库、计费、格式转换、代理内部、同步协议、API 参考、开发)见 [doc/](doc/) 目录下的专题文档。

## 快速开始

```bash
# 仅启动仪表板
bash start.sh

# 启动全部:编译并启动代理(开机自启)+ 仪表板
bash start.sh --all

# 不自动打开浏览器
bash start.sh --no-browser
```

浏览器会打开仪表板,通过左侧导航栏切换功能页面。端口约定:代理监听 **8800**,仪表板在 **5000–5099** 区间自动找一个空闲端口(启动时会打印实际地址)。两个服务都默认只绑定 `127.0.0.1`(仅本机可访问)——仪表板持有全部密钥,代理是本机工具的本地端点;需跨机器直连时用 `--host 0.0.0.0` + 反向代理鉴权/防火墙自行处理。

> 代理的开机自启基于 systemd 用户服务。非 systemd 环境(如 macOS)用 `bash scripts/start-proxy.sh --daemon` 后台启动。

## 配置 AI 工具

先给工具配上代理的地址和本地密钥(密钥在仪表板生成,见下面的配置流程):

```bash
# OpenAI 兼容工具(OpenAI SDK、各类兼容客户端)
export OPENAI_BASE_URL=http://localhost:8800/v1
export OPENAI_API_KEY=<本地密钥>

# Anthropic 兼容工具(Claude Code 等)
export ANTHROPIC_BASE_URL=http://localhost:8800/v1
export ANTHROPIC_AUTH_TOKEN=<本地密钥>

# OpenAI Responses(codex 等)
export OPENAI_BASE_URL=http://localhost:8800/v1
export OPENAI_API_KEY=<本地密钥>
```

几点说明:

- Base URL 的路径是 `/v1`,不是 `/v1/chat/completions`。SDK 会自己拼端点。代理同时兼容 `/v1/v1/...` 这种重复前缀,已把 `/v1` 写进 base URL 的客户端也能直接用
- 本地密钥对三种格式通用,一把密钥可以同时给 OpenAI 和 Anthropic 工具用
- 工具请求的模型名要能被「模型定价」里的 pattern 匹配到,否则请求仍会转发,但计费会按 0 处理

## 配置流程

1. 打开仪表板 → **代理管理 → 上游账户** → 添加账户,填上游 API 的 Base URL、Key,选 API 格式(OpenAI / OpenAI Responses / Anthropic)
2. **代理管理 → 本地密钥** → 生成密钥,选关联的账户(密钥只显示一次,注意保存)
3. 把密钥填入 AI 工具的 `API_KEY`(或 `AUTH_TOKEN`),Base URL 指到代理
4. 发一个请求验证:用量会出现在 **消费报告** 和 **请求日志**,实时并发与延迟看 **性能监控**

## 账户管理

### 账户类型与并发

每个上游账户可配置:

- **账户类型**:`api`(按量计费)、`plan`(订阅套餐)或 `agent`(Agent 订阅,如 Codex)。plan 按每把上游密钥的订阅周期收费,与调用量无关;调用仍会记录一笔 api 口径的**虚拟消费**,用于衡量套餐价值。密钥可设置订阅起始日,周期、价格变更与取消判断统一使用 UTC+0。`agent` 计费与 plan 一致,但不绑定上游密钥、不可作为本地密钥的上游目标,用量由后台导入(见 [doc/billing-pricing.md](doc/billing-pricing.md) 的 agent / 汇率小节)。
- **并发限额**:同一账户同时进行中的请求数上限,留空 = 不限。超限的请求立即返回 HTTP 429;在聚合链里则自动切到下一个账户
- **plan 冷却**:plan 的单把上游密钥只有收到**真正的配额耗尽错误**才会自动冷却 **5 小时**——即上游返回 `429` 且错误体是 `{"type":"error","error":{"type":"GoUsageLimitError",…}}`(如 opencode.ai 的"5 小时/每周使用限额")。普通的瞬时 `429`(限流/过载)只做几秒到几分钟的退避,不会锁死密钥;成功调用即清除退避。同账户的其他密钥在任何冷却下都仍可立即接管。冷却状态只存在内存中(重启即清),且冷却期内代理会**每隔 1 小时主动探测一次上游**的 `GET /models`:只要上游恢复返回 2xx,该密钥的冷却会提前解除、重新进入候选池,不必等满 5 小时(探测不产生任何请求日志、不占并发)。探测间隔默认 1h,可用环境变量 `TB_COOLDOWN_PROBE_SECS` 覆盖(单位秒)

### 上游账户聚合

把多个上游账户合并成一个,对客户端暴露统一的模型列表:

1. **代理管理 → 上游账户聚合** → 新建聚合账户
2. 为每个模型加一条映射:模型名 → 目标上游账户 → 上游模型名
3. 同一模型可以配多个上游账户,请求从上到下依次尝试:当前账户达并发上限或处于冷却期时自动换下一个;上游返回 429 / 5xx 且尚未向客户端发任何数据时,本次请求立即回退;全部不可用时返回 429

### 模型定价

**代理管理 → 模型定价**:

- 添加/编辑模型单价(元/百万 tokens):输入、输出、缓存命中(缺省按输入价)
- 给每个定价配置**时段倍率**(峰谷定价):按「当日几点到几点」配一个倍率,比如夜间 1.2 倍。时段按北京时间(UTC+8)输入,跨午夜用起止倒置表示
- 用上移/下移调整匹配优先级:同一请求命中多个 pattern 时,排在上面的生效
- 改价、调档、换序都只影响新请求,已产生的费用不回溯重算

消费口径的说明见 [doc/billing-pricing.md](doc/billing-pricing.md)。

## 仪表板使用

侧边栏共 9 个页面,时间显示统一为 UTC+8:

| 页面 | 用途 |
|------|------|
| 用量仪表板 | 总 Token / 请求数 / 消费概览、模型占比饼图、Token 类型分布、每个模型的每日用量与月度趋势。支持按用户筛选(费用按 Token 占比分摊)与按月查看;全局占比 <1% 且最新有数据月份内 0 用量(含调用失败)的模型自动隐藏,该判定一次计算后不随月份选择变化 |
| 消费报告 | 代理转发的请求统计:近 30 天滚动 Token / 请求 / 消费概览、每日用量堆叠图、今日各上游明细、导出数据与 WebDAV 同步 |
| 上游账户 | 账户增删改查、从上游拉取模型目录 |
| 上游账户聚合 | 聚合账户的模型映射与回退顺序 |
| 本地密钥 | 生成 / 复制 / 编辑 / 删除密钥,密钥列表打码显示 |
| 模型定价 | 单价与峰谷时段编辑器 |
| 请求日志 | 分页请求明细,按日期 / 模型 / 账户筛选 |
| 性能监控 | 当前并发、RPM、成功率、平均延迟、P50/P95/P99、输出速度分布(P50/P95/P99)、每分钟请求数、各上游成功率、各模型延迟与 TTFT/速度采样,15 秒自动刷新 |
| 设置 | 代理超时配置(按客户端线格式分组)与 WebDAV 同步设置 |

### 数据看板与代理导出

用量数据来自代理转发:消费报告页点「导出数据」,未同步的用量按写时固化的 `request_log.cost` 增量聚合写入存档数据库(dashboard.db),已入库金额不受之后改价影响。CSV 导入已弃用。

### WebDAV 同步

多台电脑共用代理时,用 WebDAV 同步配置与聚合用量:

1. **代理管理 → 设置** → 填 WebDAV 服务器地址、账号、密码 → **测试连接** → **保存配置**
2. 消费报告页点 **导出数据** 执行一次完整事务:拉取云端最新 → 导出本地新用量 → 上传云端 → 成功后替换本地存档

用量存档采用**追加模式**:每次导出上传生成带时间戳的文件,云端旧文件保留不删,拉取时自动取最新。**云端永远是最新版本**,每台机器的本地存档永远是云端的一个历史版本;上传失败自动回滚(本地与检查点不动)。

配置同步是**云端权威镜像**:仪表板启动时从云端拉取配置合并(改名/编辑/删除跨机生效);在管理页改配置会立即写入本机(代理即时生效),退出设置界面时作为**一次事务**上传云端——上传前校验云端未被其他机器改过(本地落后则拒绝覆盖),上传失败弹窗让用户选择**重试上传**或**丢弃设置**(回滚到上次同步快照)。

同步的是配置(账户、**不含上游 API Key**、本地密钥、Plan 价格历史与计费设置、定价、聚合映射、超时三档)与聚合后的用量;云端会保存上游密钥的 masked 身份、订阅起始日和取消标记，以便多机账单关联。**绝不上传**完整上游 API Key、请求明细、性能数据、在途请求和 WebDAV 账号密码。每台机器各自填写的上游 Key 只存本机(`data/proxy.db`),丢弃回滚用的本地快照 `data/config_snapshot.db` 同样不出本机。机制详见 [doc/sync.md](doc/sync.md)。

## 运维

### 编译代理

代理是 C++17 + CMake,依赖已随仓库携带(`proxy/third_party/`),只需系统装了 OpenSSL 和 CMake:

```bash
cd proxy
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
./build/token_proxy --db ../data/proxy.db --schema-dir ../schema/proxy --port 8800
```

命令行参数:

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--db` | `data/proxy.db` | SQLite 数据库路径 |
| `--schema-dir` | 由 `--db` 推导 | 迁移文件目录(`schema/<库名>/NNNN_*.sql`) |
| `--port` | `8800` | 监听端口 |
| `--host` | `127.0.0.1` | 绑定地址(默认仅本机可访问) |
| `--log-level` | `info` | 日志级别 |

### systemd 服务

`start.sh --all` 会自动编译并安装 `token-proxy` 用户服务(开机自启)。单独管理:

```bash
bash scripts/start-proxy.sh --install      # 安装为 systemd 用户服务
bash scripts/start-proxy.sh --uninstall    # 移除服务
systemctl --user status token-proxy        # 查看状态
systemctl --user restart token-proxy       # 重启
journalctl --user -u token-proxy -f        # 查看日志
```

### 状态检查

```bash
bash scripts/status.sh
```

检查代理二进制、systemd 服务、8800 健康检查、仪表板进程与端口、数据库行数。

### 数据目录

运行时数据都在 `data/`(已 gitignore):`proxy.db`(代理运行库:账户、密钥、定价、请求日志)、`dashboard.db`(用量+总价存档库)。两个库的 schema 由版本化迁移管理,升级规则见 [doc/database-migrations.md](doc/database-migrations.md)。

## 常见问题

**改了定价,之前的花费会变吗?**
不会。成本在请求写入时按当时价格固化,改价、调整峰谷档位、换匹配顺序都不回溯历史。原因和公式见 [doc/billing-pricing.md](doc/billing-pricing.md)。

**同一个模型为什么有时不走我想要的账户?**
多账户聚合时,回退顺序是 冷却跳过 → 并发满跳过 → 429/5xx 回退。想固定走某个账户,就把它排在链最前,且只给这个模型配它。

**agent 报 429 是什么意思?**
所有候选账户都忙或都在冷却(聚合链),或单账户并发超限。可以调大 `max_concurrency`,或等 plan 账户的配额冷却结束(仅在上游返回 GoUsageLimitError 时触发;重启代理可立即解除)。

**为什么我的非流式请求总是走到 Deepseek API,而不走 plan?**
这是上游限制,不是路由故障。部分 plan 上游(如 opencode.ai 的 zen/go)**只接受流式请求**——实测对非流式 `/v1/chat/completions` 一律返回 HTTP 500,对流式则正常。代理会把这类非流式请求自动回退到下一候选账户(通常是 Deepseek API),所以客户端能正常拿到结果,只是用不到便宜的 plan 额度。想让 plan 账户接管非流式流量,请在客户端把请求配成 `stream: true`(OpenCode/Cherry Studio 等默认已开;自建调用在请求体里加 `"stream": true` 即可)。

**上游长时间没回,连接被断开?**
这是代理的上游读超时。超时按客户端线格式分三档配置(流式首字节 / 流式静默 / 非流式,默认值及说明见**代理管理 → 设置**),改完即时生效。超时后代理会按客户端格式发一个明确的超时错误(504,`timeout_error`),而不是静默断连;非流式的聚合请求会先尝试下一个账户,全部超时才返回 504。SDK 通常会按这个错误重试。

**流式请求为什么有时会等很久然后才失败?**
如果上游在**已经输出了首 token**之后停滞(部分 plan 上游配额耗尽时的表现——不再返回 429,而是接受请求、吐出一点内容、然后卡住),代理会等到「流式静默超时」(`streaming_idle_timeout`,默认 120s)才以 504 终止。因为内容已经写给了客户端,单请求内**不能再切换到下一个账户**(已提交的流式数据无法撤回),这是流式协议的固有边界。可在设置里调低静默超时缩短等待;配额恢复后该上游会重新进入候选链。

**想把账单明细带出去?**
明细在本地 `data/proxy.db` 的 `request_log` 表,用 sqlite 工具直接查;云端同步的只是聚合结果。

## 文档索引

**文档由AI自动编写，无人工核查，请批判性阅读**
| 文档 | 内容 |
|------|------|
| [doc/architecture.md](doc/architecture.md) | 总体架构、目录结构、数据流、设计决策 |
| [doc/database.md](doc/database.md) | 两个 SQLite 库的表结构、索引、触发器、计费公式 |
| [doc/database-migrations.md](doc/database-migrations.md) | schema 迁移机制与升级步骤 |
| [doc/billing-pricing.md](doc/billing-pricing.md) | 定价、峰谷档位、写时计价固化、plan 虚拟消费 |
| [doc/upstream-concepts.md](doc/upstream-concepts.md) | 上游账户模型：api/plan/agent 类型、单上游多密钥、聚合上游，及其在代理-保存-导出链路的行为 |
| [doc/format-conversion.md](doc/format-conversion.md) | 三格式转换、IR 与 codec、流式处理、think 抽取 |
| [doc/proxy-internals.md](doc/proxy-internals.md) | 路由、并发闸门、超时、回退、性能监控 |
| [doc/sync.md](doc/sync.md) | WebDAV 同步协议、云端权威事务、高水位检查点、追加模式 |
| [doc/api-reference.md](doc/api-reference.md) | 全部 API 端点与命令行参数 |
| [doc/development.md](doc/development.md) | 构建、自测、模拟上游、前端开发 |
