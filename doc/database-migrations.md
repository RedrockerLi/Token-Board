# Token Board 数据库迁移指南

Token Board 的两个 SQLite 数据库（`data/proxy.db` 与 `data/dashboard.db`）的 **schema 全部由版本化迁移文件定义**，C++ 代理与 Python 看板共用同一份 `.sql` 文件。本文说明机制、升级步骤与硬性规则。

## 一、机制

```
schema/
  proxy/0001_initial.sql      # proxy.db 的全部表/索引/触发器
  dashboard/0001_initial.sql  # dashboard.db 的全部表/索引/触发器
  proxy/0002_xxx.sql          # 将来的升级步骤（按编号追加）
  ...
```

- **版本号**：每个库用 `PRAGMA user_version` 记录已应用的迁移编号。`0` = 未迁移。
- **迁移文件**：`schema/<库名>/NNNN_描述.sql`，`NNNN` 为 4 位数字（如 `0001`、`0002`），按数字升序应用。允许跳号。
- **谁执行**：C++ 代理启动时（`proxy/src/store/db.cpp::run_migrations`）与 Python 看板启动时（`app/migrations.py`）各跑一次。两处实现相同协议：读取 `user_version`，仅应用编号大于它的步骤，每步设置 `user_version` 在**同一次事务**里。
- **并发安全**：执行前先对 `<数据库>.migrate.lock` 加 `flock` 互斥锁（C++ 与 Python 使用同一把锁），每步再包 `BEGIN IMMEDIATE … COMMIT`。因此 proxy 与 dashboard 谁先启动、是否同时重启都安全：后执行者看到版本已到位则空转。
- **原子性**：一个迁移文件 = 一个事务。执行失败自动回滚，`user_version` 不变，并 **fail-fast**（代理启动失败退出、看板拒绝启动）——不会带着半成品 schema 运行。
- **幂等**：版本已到位时每次启动是空操作，开销可忽略。

## 二、硬性规则

1. **已应用的迁移文件不可修改、不可删除。** 一旦发布过，它就是历史。任何 schema 变更只能**追加**新的 `NNNN` 文件（取当前最大编号 +1）。
2. **`.sql` 文件内禁止写 `BEGIN` / `COMMIT` / `PRAGMA user_version`** —— 事务控制归 runner，写了会破坏事务原子性。
3. **先建表后建触发器**。触发器引用表，顺序错了会失败。
4. **破坏性变更要显式声明并先备份。** 新文件首行写 `-- DESTRUCTIVE: <原因> — 先备份 data/<库>.db`。能做成加性的（新表/新列保留旧对象）就优先加性方案。

## 三、SQLite 注意点

- `PRAGMA foreign_keys` **不能在事务内切换**（事务内是 no-op）。且两个 runner 的外键状态不一致：
  - **C++ 代理**在连接打开时设 `PRAGMA foreign_keys=ON`，之后才跑迁移 → **C++ 应用迁移时外键开启**；
  - **Python runner**（`app/migrations.py`）的连接未显式设 `foreign_keys`，默认关闭。
  - 两者共用同一把 `flock`，谁先启动谁应用 → **每个迁移文件都必须在外键开启下也能正确执行**。
- 外键开启时，`DROP TABLE` 一个「父表」会先隐式删除其所有行，从而触发引用它的表上的 ON DELETE 动作
  （`SET NULL` 会清空子表引用列，`RESTRICT` / `NO ACTION` 会拦截删除）。因此凡涉及表重建的迁移
  （例如把 `NOT NULL` 列改成可空——这类改动无法用加性手段完成），必须满足**外键安全**顺序：
  - 被 `DROP` 的表在那一刻**没有任何表引用它**：先删「子表」，后删「父表」；
  - 重建的新表所引用的「新父表」必须先就位。可用**改名让位**交接同名父表：先把旧父表
    `ALTER TABLE … RENAME` 到临时名（SQLite 会把现存子表的外键引用一并重定向），再把新父表
    `RENAME` 回正式名，之后即可安全 `DROP` 临时名旧表；
  - 建表/建索引时若新对象与现存表同名，先 `DROP` 旧表再建（索引名是库级全局的）。
- WAL 模式下迁移持写锁：并发读者不受影响（看到旧快照），并发写者最多等待 `busy_timeout=5000`。
- 迁移文件若被 runner 读到为空或不可读，会视为失败并 fail-fast，不会静默跳过。

## 四、如何新增一次数据库升级（step-by-step）

以「给 `request_log` 加一列 `foo`」为例：

1. **确定编号**：看 `schema/proxy/` 下现有最大编号。当前是 `0001`，新文件就是 `0002`。
2. **写迁移文件**：新建 `schema/proxy/0002_add_request_log_foo.sql`：

   ```sql
   -- 给 request_log 增加 foo 列（加性变更）
   ALTER TABLE request_log ADD COLUMN foo TEXT NOT NULL DEFAULT '';
   ```

   > 注意：不要写 `BEGIN`/`COMMIT`/`PRAGMA user_version`。runner 会把它包进事务并自动推进版本号。
3. **本机验证**（用副本，别动线上库）：

   ```bash
   sqlite3 data/proxy.db ".backup /tmp/upgrade_test.db"   # 或 cp 三个文件
   python3 - <<'PY'
   from app.migrations import migrate
   migrate("/tmp/upgrade_test.db", "schema/proxy")
   import sqlite3
   c = sqlite3.connect("/tmp/upgrade_test.db")
   print(c.execute("PRAGMA user_version").fetchone()[0])   # 期望 = 0002 的数字
   c.close()
   PY
   ```

   再跑一次应无异常（幂等）。也可用并发脚本模拟 proxy 与看板同时启动。
4. **部署**：重启 proxy 与看板，顺序无所谓（flock 保证安全）：

   ```bash
   systemctl --user restart token-proxy     # C++ 代理
   # 看板：重启 server.py 进程（或 bash start.sh）
   ```

## 五、常见问题

- **升级失败，服务反复重启**：`systemctl --user status token-proxy` 看状态，`journalctl --user -u token-proxy` 看日志。因为每步是原子的，失败后数据库停留在上一个版本，**修好迁移文件后重启即可**，不会二次损坏。
- **看板启动报 `schema dir not found`**：schema 目录由数据库路径按仓库约定推导（`data/proxy.db` → `<仓库>/schema/proxy`）。数据库请放在仓库 `data/` 布局下；C++ 代理可用 `--schema-dir` 显式指定。
- **想给 dashboard.db 升级**：同样流程，目录换 `schema/dashboard/`。
