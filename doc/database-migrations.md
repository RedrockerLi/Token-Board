# Token Board 数据库升级指南

## 责任边界

数据库升级的唯一生产入口是 Python 的
`app.db.schema_upgrade`。C++ 代理不会读取 SQL、创建数据库、执行迁移或运行
数据转换；它只在打开数据库时校验 Python 已经准备好的 V1 schema。

系统升级前由 Python 先执行（唯一入口是 `bash start.sh --all`）：

```bash
PYTHONPATH=. python3 -m app.db.schema_upgrade.cli \
  --token-board-db data/token-board.db \
  --dashboard-db data/dashboard.db \
  --schema-dir schema \
  --timezone Asia/Shanghai
```

只有 `start.sh --all` 把这个命令放在服务重启前。普通 `start.sh`、
`scripts/start-proxy.sh` 和生成的 systemd unit 都不执行迁移。`app.db.proxy`、Dashboard writer 等运行时访问层只做只读版本校验；
如果数据库未准备好，会要求先执行上述 Python 边界。C++ 的 `--schema-dir`
参数仍保留用于兼容旧启动器，但已废弃且会被忽略。

调用链固定为：启动脚本 → `schema_upgrade.cli` →
`coordinator.ensure_local_databases` → SQL engine / transition registry。业务 facade
只调用 `verify_current_database`。`app.db.migrations.apply_sql_migrations` 是
供 coordinator 和 transition 使用的底层 SQL 原语；兼容别名 `migrate()` 只保留给
fixture、测试和 V0 历史转换，不是业务代码的升级入口。

## 数据库命名

代理运行库的逻辑数据库名已经从 `proxy` 改为 `token-board`，schema 目录也对应改为
`schema/token-board/`。这是一次不提供前向兼容的破坏性改名：旧的
`schema_version.database_name='proxy'`、`schema/proxy/` 路径和 `--proxy-db` 参数均不再
被识别。现有旧身份数据库不会被静默接受，必须在切换前按新的数据库边界准备好。

## 目录结构

```text
schema/
├── token-board/
│   ├── v0/0-1_initial.sql … 0-19_drop_monthly_price.sql
│   └── v1/1-0_baseline.sql … 1-17_decouple_frozen_ledgers.sql
├── dashboard/
│   ├── v0/0-1_initial.sql … 0-6_drop_account_mirror_cols.sql
│   └── v1/1-0_baseline.sql … 1-6_remove_account_exclusions.sql
└── transitions/
    ├── 0-to-1/                    # V0 → V1 历史转换插件
    │   ├── transition.json
    │   └── transition.py
    ├── v1-legacy-agent-billing/   # V1 配对数据修复
    │   ├── transition.json
    │   └── transition.py
    ├── v1-agent-identity/         # V1 跨库身份对齐
    │   ├── transition.json
    │   └── transition.py
    └── v1-pricing-current-only/   # 删除模型定价历史并扁平化当前配置
        ├── transition.json
        └── transition.py

app/db/schema_upgrade/
├── cli.py                  # 启动和运维入口
├── coordinator.py          # 本地库及下载 artifact 的编排
├── compound.py             # transition runner、shadow 和发布屏障
├── artifact_runner.py      # 下载 artifact 的同一 transition runner
├── transition_api.py       # 插件上下文接口
├── transition_registry.py  # descriptor、版本 route、checksum 和发现
└── engine_core.py          # shadow、manifest、备份、校验、发布
```

当前仓库的 V1 tip 是 Token Board V1.17、Dashboard V1.6。V0 文件保留用于历史库和
转换测试；新安装只创建当前 V1 baseline，不重放 V0 历史。

## 版本与元数据

- SQL 文件名为 `major-minor_description.sql`，按 `(major, minor)` 数字顺序执行。
- `schema_version` 是权威版本；`PRAGMA user_version` 是兼容镜像，值为
  `major * 10000 + minor`。
- `schema_migrations` 保存已执行 SQL 的文件名、SHA-256 checksum 和时间。
- `schema_transitions` 保存数据 transition 的 ID、源码 checksum、配对
  `generation_id` 和应用时间。版本 route 决定 transition 是否适用；该表只记录
  已完成的 transition，避免重复执行并保证两个数据库使用同一个 generation。
- 运行时要求数据库处于当前 V1 tip。未知的更高版本不能当作已验证的运行时版本；
  应使用匹配版本的 Python schema-upgrade 工具处理。

## 同 Major 的 SQL 升级

普通的同 Major Minor 变化由 Python SQL 引擎执行。每个 SQL 文件、迁移记录、
`schema_version` 和 `user_version` 在同一个 SQLite 事务中提交；SQL 失败会回滚
该数据库的这一步。SQL 引擎使用 `<db>.migrate.lock`，本地双库编排还会使用
数据目录下的 `schema-upgrade.lock`。

同 Major 并不等于“永远只需要 SQL”。如果版本变化会改变两个数据库之间的数据
关系，必须同时注册一个 `schema/transitions/<transition-id>/` transition。启动
边界会先检测 pending transition，再决定是否执行普通 SQL。

## Transition 插件与版本 route

每个 `schema/transitions/<id>/transition.json` 声明插件的 `strategy`、`order`、
适用 `scope` 和当前版本 route。版本 route 中的 `current` 使用 Major 与 Minor
闭区间；`prepare` 是插件要求的中间 SQL 版本，`target` 是插件完成后要到达的
版本，`latest` 表示当前 schema tip，`same` 表示保持当前版本。

registry 根据当前版本向量选择 route，不调用业务数据查询。插件 entrypoint 只实现：

```python
apply(context: TransitionContext) -> None
verify(context: TransitionContext) -> dict
```

`TransitionContext` 提供只读 source/staged 文件、可写 shadow 文件、当前版本、
prepare/target 版本和 scope。插件只能修改 shadow。选择逻辑集中在
`app.db.schema_upgrade.transition_registry`，执行逻辑集中在 runner。

当前 route 顺序为：

- `order: 0` `0-to-1`：V0 source → 当前 V1，使用 `rebuild-shadow`；
- `order: 1` `v1-legacy-agent-billing`：Token Board V1.6/V1.7 billing barrier；
- `order: 2` `v1-agent-identity`：Token Board V1.8 与 Dashboard V1.3/V1.4 identity barrier。
- `order: 3` `v1-pricing-current-only`：模型定价历史到当前配置的扁平化。
- `order: 4` `v1-live-resource-hard-delete`：一次性清理已终态软删除的代理实时资源，保留历史身份、请求日志和冻结账单。

已发布 transition 的 descriptor、源码和附属数据都会进入 checksum。行为变化应
创建新的 transition ID，不应原地修改已发布插件。

## Transition 的原子发布

本地 transition 使用双 shadow 和持久 manifest，流程如下：

1. 获取 `schema-upgrade.lock`，并备份两个数据库、WAL/SHM、请求 spool、配置 snapshot 等
   附属文件。
2. 按 route 的 strategy 创建 source/target shadow；所有 `apply/verify` 操作只对
   shadow 执行。
3. 先把 shadow 准备到 route 要求的版本，执行 descriptor 排序后的 transition，
   再补齐目标 SQL，并执行 `quick_check`、`foreign_key_check` 和版本校验。
4. 在两个 shadow 写入相同的 `generation_id` 和 transition checksum。
5. 记录 manifest 后发布两个数据库；如果发布过程被中断，下次 Python 启动会
   根据 `auto-*.manifest.json` 和备份自动恢复原始数据库，再重新尝试。

这不是把两个 SQLite 文件放进一个跨文件事务，而是用 shadow、发布屏障、持久
manifest 和备份恢复保证：服务不会在未完成的数据转换状态下启动。

当前 V1 transition：

- `v1-legacy-agent-billing`：清理 V1.6 智能体拆分遗留的重复 recurring charge。
- `v1-agent-identity`：把旧 Dashboard 智能体归档 ID 对齐到 Token Board 的共享身份
  ID；必要时在 Token Board SQL 变更与 Dashboard SQL 变更之间执行跨库修复。
- `v1-pricing-current-only`：把模型定价历史转换为单一当前配置，适用于本地 pair
  与下载的 Token Board 配置工件。

## V0 → V1

V0 → V1 是 `0-to-1` 插件负责的跨 Major 历史转换，不由 C++ 执行。正常本地启动
时，Python coordinator 会把 V0/V0 数据库对交给统一 runner，在服务启动前完成
shadow 转换和发布。

如需单独操作旧 V0 库，可使用历史工具：

```bash
python3 schema/transitions/0-to-1/migrate.py \
  --token-board-db data/token-board.db \
  --dashboard-db data/dashboard.db
```

兼容 CLI 的默认行为只构建并校验 shadow。确认 manifest 和统计后，使用
`--apply --confirm-timezone <timezone>` 才替换源数据库。工具会检查服务是否停止、WAL
是否可 checkpoint、spool 是否排空，备份数据库和附属文件，并在失败时支持
`--resume-manifest` 或 `--rollback-manifest`。

下载的配置或 Dashboard artifact 也必须通过
`app.db.schema_upgrade.upgrade_downloaded_artifact` 的同一 shadow 路径升级；同步
合并代码不得自行执行 SQL 或数据转换。

云端没有远程 migration service。云端 artifact 被下载到本地临时文件后，才由
Python runner 按版本 route 升级；原云端文件保持不变。配置 V0 artifact 合并后
可以上传新的 V1 artifact，Dashboard artifact 则在导出事务中升级、导出并在
上传成功后提交本地状态。

## 新增升级的规则

### 只有结构变化

只追加对应数据库 V1 目录中的 SQL 文件，例如：

```text
schema/token-board/v1/1-11_add_request_queue.sql
```

SQL 不得包含 `BEGIN`、`COMMIT` 或 `PRAGMA user_version`，也不得修改已经写入
`schema_migrations` 的文件。提交前需要在副本上验证重复执行无变化、checksum
稳定、`PRAGMA foreign_key_check` 为空。

### 需要数据转换或跨库协调

新增独立目录：

```text
schema/transitions/<transition-id>/transition.json
schema/transitions/<transition-id>/transition.py
```

descriptor 至少声明 `id`、`databases`、`strategy`、`entrypoint`、`order` 和
非空 `routes`；其中 `databases` 必须明确写为 `["token-board", "dashboard"]`。
Python entrypoint 实现 `apply(context)` 和 `verify(context)`。源码、descriptor
和附属数据会计算 checksum；已经发布的 transition 不应原地修改，行为变化应
创建新的 transition ID。

数据转换代码只能操作 shadow，不能从业务 facade、reconcile、C++ 请求路径或
启动后的后台任务中偷偷补数据。普通业务代码只调用统一的
`ensure_local_databases`、`upgrade_shadow` 或 `verify_current_database`。

## 故障处理与验证

升级失败时，CLI 会打印最近的 manifest 和 backup 路径；原始数据库会被恢复，
再次启动会自动处理未完成的 `auto-*.manifest.json`。不要手工删除 manifest 或
backup 后继续启动，除非已经确认数据库和附属文件可从其他备份恢复。

发布前至少验证：

```bash
python3 -m unittest discover -s app/tests
cmake -S proxy -B proxy/build
cmake --build proxy/build -j2
ctest --test-dir proxy/build --output-on-failure
```

还应在生产库副本上演练一次普通 SQL 升级、一次 transition、一次发布中断后的
自动恢复，并保留 manifest 和备份 checksum。
