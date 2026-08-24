# Token Board 数据库迁移指南

Proxy 与 Dashboard 共用 Major–Minor 版本协议，C++ 和 Python runner 读取同一组 SQL。

```text
schema/
├── proxy/
│   ├── v0/0-1_initial.sql … 0-19_drop_monthly_price.sql
│   └── v1/1-0_baseline.sql … 1-9_stable_agent_instance_identity.sql
├── dashboard/
│   ├── v0/0-1_initial.sql … 0-6_drop_account_mirror_cols.sql
│   └── v1/1-0_baseline.sql … 1-4_unify_agent_archive.sql
└── transitions/0-to-1/
    ├── migrate.py
    ├── proxy_transform.sql
    ├── dashboard_transform.sql
    └── verify.py
```

## 版本语义

- 文件名必须匹配 `major-minor_description.sql`，runner 按解析后的整数二元组排序。
- Minor 增加表示同 Major 向后兼容，启动时自动升级；数据库 Minor 高于程序已知值时允许运行并告警。
- Major 增加表示不兼容。服务拒绝自动启动，必须运行对应 `schema/transitions/<old>-to-<new>` 工具。
- 空数据库只执行最新 Major 的 baseline，不重放 V0 历史。
- `schema_version` 是权威版本，`schema_migrations` 保存文件名、SHA-256 checksum 与应用时间。
- `PRAGMA user_version` 是兼容镜像：`major * 10000 + minor`，所以 V0.19 是 `19`，V1.0 是 `10000`。

每个 Minor 文件在同一事务中完成 SQL、migration 记录、权威版本和 `user_version` 更新。数据库锁为 `<db>.migrate.lock`；任何一步失败都会整体回滚。

## 目录参数

`--schema-dir` 推荐指向 `schema/` 根目录。旧式叶子目录（例如 `schema/proxy/v0`）只用于 V0 测试和 transition；程序会明确选择数据库名与 Major。项目默认路径为 `data/token-board.db` / `data/dashboard.db`。

## 新增兼容迁移

例如当前 Proxy 是 V1.0，则新增 `schema/proxy/v1/1-1_add_request_queue.sql`。只追加文件，不修改任何已被 `schema_migrations` 记录的文件；checksum 不一致会 fail-fast。

SQL 文件不得包含 `BEGIN`、`COMMIT` 或 `PRAGMA user_version`。迁移必须在 `foreign_keys=ON` 下安全，并在副本上验证：重复执行无变化、`PRAGMA foreign_key_check` 为空、Python/C++ runner 得到相同版本。

## V0 → V1

这是维护窗口迁移，不支持 V0/V1 节点混跑：

```bash
python3 schema/transitions/0-to-1/migrate.py \
  --proxy-db data/token-board.db --dashboard-db data/dashboard.db
```

默认只构建和校验影子库。确认 manifest 与统计后追加 `--apply` 才原子替换。脚本会检查服务已停止、WAL 可 checkpoint、spool 已排空，备份数据库和附属文件，转换时间/身份/路由/计费，执行总量与外键对账，再记录每个替换阶段。

中断后使用 `--resume-manifest <manifest>` 继续，或用 `--rollback-manifest <manifest>` 恢复整组备份。掩码碰撞、未知版本、非空 spool、总量不一致或外键错误都会在替换前中止。

## 发布检查

```bash
cmake -S proxy -B proxy/build
cmake --build proxy/build -j2
ctest --test-dir proxy/build --output-on-failure
```

发布前还应在生产库副本上至少完成两次 dry-run 与一次 apply/rollback 演练，并保留 transition manifest 和备份 checksum。
